"""`unzip FOLDER`: expand every .gz mosaic in a folder and delete the .gz.

astropy can read a .fits.gz directly, but it can't memory-map one, so a big mosaic
would have to fit in RAM.
"""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path
import shutil


def decompress_dir(base_dir: Path) -> int:
    """Expand each x.gz in `base_dir` to x and delete x.gz.

    Returns:
        How many archives there were.
    """
    gz_paths = sorted(base_dir.glob("*.gz"))
    for gz_path in gz_paths:
        print(f"expanding {gz_path.name}", flush=True)
        out_path = gz_path.with_suffix("")
        # under another name until it's whole, so a broken archive or a Ctrl+C
        # doesn't leave half a FITS file that cut would open
        part = out_path.with_name(out_path.name + ".part")
        try:
            with gzip.open(gz_path, "rb") as f_in, part.open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.replace(out_path)
        gz_path.unlink()
    return len(gz_paths)


def main(argv: list[str]) -> int:
    """Expand the archives in the folder named on the command line."""
    p = argparse.ArgumentParser(prog="jwst-peculiar unzip FOLDER")
    p.add_argument("folder", type=Path)
    n = decompress_dir(p.parse_args(argv).folder)
    print(f"expanded {n} files")
    return 0
