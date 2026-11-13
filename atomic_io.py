"""Permission handling for files that will be atomically replaced."""
from __future__ import annotations

import errno
import logging
import os
import stat

_LOG = logging.getLogger(__name__)
_UNSUPPORTED = {getattr(errno, name) for name in ("ENOTSUP", "EOPNOTSUPP", "ENODATA")
                if hasattr(errno, name)}


def preserve_replace_permissions(temp_path, destination) -> None:
    """Apply the destination's mode and access ACL before replacing it.

    Permission errors are best effort: the caller can still replace the data.
    """
    is_new = False
    try:
        mode = stat.S_IMODE(os.stat(destination).st_mode)
    except FileNotFoundError:
        # Python has no read-only umask API. Restore it immediately after reading.
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
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
        os.setxattr(temp_path, "system.posix_acl_access", acl)
    except AttributeError:
        pass
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED:
            _LOG.warning("Could not copy access ACL from %s to %s: %s", destination, temp_path, exc)
