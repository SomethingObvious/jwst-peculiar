"""`objects init FOLDER CONFIG`: write a config for a folder of NIRCam mosaics.

It reads the filter out of each file name (the last `f` and three or four digits
followed by w, m or n), takes what comes before it, less any `_nircam` or
`_clear`, as the field, and keeps the bands every field has. The band nearest
F277W finds sources and is red, the one nearest F150W is green and the bluest is
blue. F115W, F150W, F277W and F444W are kept too when there, for the compact list.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import re

import yaml

BAND = re.compile(r"f(\d{3,4})[wmn]2?", re.IGNORECASE)
COMPACT = ("f115w", "f150w", "f277w", "f444w")
NOISE = re.compile(r"([-_](nircam|clear))+$", re.IGNORECASE)


def _microns(band: str) -> float:
    digits = re.sub(r"\D", "", band)
    return int(digits) / 100


def split_name(name: str) -> tuple[str, str, str] | None:
    """Split a mosaic's file name into its field, pattern and band.

    Returns:
        The field, the pattern with {field} and {band} in it, and the band, or None
        when the name has no filter in it.
    """
    hits = list(BAND.finditer(name))
    if not hits:
        return None
    m = hits[-1]
    before, band, after = name[: m.start()], m.group(0).lower(), name[m.end() :]
    field = NOISE.sub("", before.rstrip("-_"))
    if not field:
        return None
    return field, "{field}" + before[len(field) :] + "{band}" + after, band


def _nearest(bands: list[str], microns: float) -> str:
    return min(bands, key=lambda b: abs(_microns(b) - microns))


def colours(bands: list[str]) -> tuple[str, str, str]:
    """Pick the red, green and blue bands, red also being the one to detect in.

    Returns:
        The bands nearest F277W and F150W, and the bluest left.
    """
    red = _nearest(bands, 2.77)
    green = _nearest([b for b in bands if b != red], 1.5)
    blue = min((b for b in bands if b not in {red, green}), key=_microns)
    return red, green, blue


def _scan(folder: Path) -> dict[str, dict[str, set[str]]]:
    seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for path in sorted(folder.glob("*.fits")):
        got = split_name(path.name)
        if got:
            field, pattern, band = got
            seen[pattern][field].add(band)
    return seen


def guess(folder: Path) -> dict[str, object]:
    """Work out a config's pattern, bands, detect band and colour from file names.

    Returns:
        The config's keys, with the pattern relative to the folder's parent.

    Raises:
        SystemExit: When no file name in the folder carries a NIRCam filter.
    """
    seen = _scan(folder)
    if not seen:
        msg = f"No .fits file in {folder} has a NIRCam filter like f277w in its name."
        raise SystemExit(msg)
    pattern = max(seen, key=lambda p: sum(map(len, seen[p].values())))
    fields = seen[pattern]
    common: list[str] = sorted(set.intersection(*fields.values()), key=_microns)
    if len(common) < 3:  # noqa: PLR2004 red, green and blue
        msg = f"The fields in {folder} share only {common}, and a colour stamp needs 3."
        raise SystemExit(msg)
    detect, green, blue = colours(common)
    keep = {detect, green, blue} | {b for b in COMPACT if b in common}
    out: dict[str, object] = {
        "name": folder.resolve().name,
        "out": f"runs/{folder.resolve().name}",
        "pattern": f"{folder.name}/{pattern}",
        "bands": sorted(keep, key=_microns),
        "detect": detect,
        "colour": [detect, green, blue],
    }
    if len(fields) == 1:
        out["fields"] = list(fields)
    return out


def main(argv: list[str] | None = None) -> int:
    """Write a config for a folder of mosaics next to it.

    Returns:
        0.

    Raises:
        SystemExit: When the config already exists.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar objects init FOLDER CONFIG")
    p.add_argument("folder", type=Path, help="the folder of mosaics")
    p.add_argument("config", type=Path, help="the config to write, beside the folder")
    a = p.parse_args(argv)
    if a.config.exists():
        msg = f"{a.config} is already there, so it was left alone."
        raise SystemExit(msg)
    keys = guess(a.folder)
    if a.config.resolve().parent != a.folder.resolve().parent:
        keys["pattern"] = (
            a.folder.resolve() / str(keys["pattern"]).split("/", 1)[1]
        ).as_posix()
    a.config.write_text(yaml.safe_dump(keys, sort_keys=False), encoding="utf-8")
    print(f"Wrote {a.config}. Run jwst-peculiar objects check {a.config} next.")  # noqa: T201 the command's result
    return 0
