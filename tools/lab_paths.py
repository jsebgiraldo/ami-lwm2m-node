#!/usr/bin/env python3
"""Where bench and fleet tools put the data they produce.

Every capture tool used to write next to its own source file, so `tools/` ended
up holding 1.9 GB of CSVs beside 166 Python scripts - including a single 1.34 GB
PPK2 stream. That is also why `.gitignore` grew ~50 patterns: one per data
filename shape, added as each new tool appeared.

One directory fixes both. `captures/` is ignored wholesale, so a new tool needs
no .gitignore entry, and `tools/` stays what it claims to be: code.

    from lab_paths import captures_dir, capture_path

    csv_path = capture_path("lab_ppk2", stamp, "csv")   # captures/lab_ppk2_<stamp>.csv

Tools that glob historical data should read from `captures_dir()`.
"""
import os
import pathlib

# tools/ -> repo root -> captures/
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTURES = REPO_ROOT / "captures"


def captures_dir() -> pathlib.Path:
    """The capture directory, created on first use.

    Override with AMI_CAPTURES_DIR to write somewhere else - useful when the
    repo lives on a small disk and a long soak would fill it.
    """
    override = os.environ.get("AMI_CAPTURES_DIR")
    d = pathlib.Path(override) if override else CAPTURES
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture_path(prefix: str, stamp: str, ext: str) -> pathlib.Path:
    """Conventional capture filename: <prefix>_<stamp>.<ext> inside captures/."""
    return captures_dir() / f"{prefix}_{stamp}.{ext}"
