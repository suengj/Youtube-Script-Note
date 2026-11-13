"""Permission handling for files that will be atomically replaced."""
from __future__ import annotations

import errno
import logging
import os
import stat

_LOG = logging.getLogger(__name__)
_UNSUPPORTED = {getattr(errno, name) for name in ("ENOTSUP", "EOPNOTSUPP", "ENODATA")
                if hasattr(errno, name)}


def _read_proc_status():
    with open("/proc/self/status") as status:
        return status.read()


def _current_umask():
    try:
        for line in _read_proc_status().splitlines():
            if line.startswith("Umask:"):
                value = line.split(":", 1)[1].strip()
                if len(value) == 4 and value[0] == "0" and all(c in "01234567" for c in value):
                    return int(value, 8)
    except (OSError, ValueError):
        pass
    return None


def preserve_replace_permissions(temp_path, destination) -> None:
    """Apply the destination's mode and access ACL before replacing it.

    Permission errors are best effort: the caller can still replace the data.
    """
    is_new = False
    try:
        mode = stat.S_IMODE(os.stat(destination).st_mode)
    except FileNotFoundError:
        umask = _current_umask()
        if umask is None:
            return
        mode = 0o666 & ~umask
        is_new = True
    except OSError as exc:
        _LOG.warning("Could not read permissions for %s: %s", destination, exc)
        return

    try:
        os.chmod(temp_path, mode)
    except OSError as exc:
        _LOG.warning("Could not set permissions on %s: %s", temp_path, exc)

    if is_new:
        return

    try:
        acl = os.getxattr(destination, "system.posix_acl_access")
    except AttributeError:
        return
    except OSError as exc:
        if exc.errno == getattr(errno, "ENODATA", None):
            try:
                os.removexattr(temp_path, "system.posix_acl_access")
            except AttributeError:
                pass
            except OSError as remove_exc:
                if remove_exc.errno not in _UNSUPPORTED:
                    _LOG.warning("Could not remove access ACL from %s: %s", temp_path, remove_exc)
            try:
                os.chmod(temp_path, mode)
            except OSError as chmod_exc:
                _LOG.warning("Could not set permissions on %s: %s", temp_path, chmod_exc)
            return
        if exc.errno not in _UNSUPPORTED:
            _LOG.warning("Could not copy access ACL from %s to %s: %s", destination, temp_path, exc)
        return

    try:
        os.setxattr(temp_path, "system.posix_acl_access", acl)
    except AttributeError:
        pass
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED:
            _LOG.warning("Could not copy access ACL from %s to %s: %s", destination, temp_path, exc)
