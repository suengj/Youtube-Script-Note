"""Shared reader for the owner's input_df.csv queue."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd


def normalize_input_df_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove BOM and leading whitespace from input_df column names."""
    frame.columns = [str(column).lstrip("\ufeff \t\r\n").rstrip() for column in frame.columns]
    return frame


def read_input_df(path: str | Path, *, read_csv: Callable | None = None,
                  return_encoding: bool = False, **kwargs):
    """Read input_df as UTF-8 with BOM, falling back to CP949 on decode errors."""
    reader = read_csv or pd.read_csv
    try:
        frame = reader(path, encoding="utf-8-sig", **kwargs)
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        frame = reader(path, encoding="cp949", **kwargs)
        encoding = "cp949"
    frame = normalize_input_df_columns(frame)
    # ASCII-only CSV bytes are valid in both encodings. Preserve the legacy
    # CP949 write format for an unmarked file in this ambiguous case.
    if encoding == "utf-8-sig" and not Path(path).read_bytes().startswith(b"\xef\xbb\xbf"):
        values = frame.astype(str).to_numpy().ravel()
        if all(value.isascii() for value in values):
            encoding = "cp949"
    if return_encoding:
        return frame, encoding
    return frame
