"""`init FOLDER CONFIG`: write a config for a folder of NIRCam mosaics.

The field is what's left of each file name around its filter, less `_nircam` or
`_clear`, or the part that varies when the field comes after the filter.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import math
import os
from pathlib import Path
import re

from astropy.io import fits
import yaml

from jwst_peculiar.config import BYTES_PER_PIXEL, RGB, Settings

BAND = re.compile(r"f(\d{3,4})[wmn]2?", re.IGNORECASE)
COMPACT = Settings().compact_bands
NOISE = re.compile(r"([-_](nircam|clear))+$", re.IGNORECASE)
SEPARATORS = "-_."
IMAGE_AXES = 2
# a field needing more than twice this for cut is cut in tiles of about this
TILE_BYTES = 4e9
# maps that come beside a mosaic and cover the same fields
AUXILIARY = re.compile(
    r"[-_.](wht|err|var|exp|ctx|con|rms|weight|mask)[-_.]", re.IGNORECASE
)


def _microns(band: str) -> float:
    # the digits after the f, as the 2 of a wide filter like F150W2 isn't part of them
    return int(re.sub(r"\D.*", "", band[1:])) / 100


def split_name(name: str) -> tuple[str, str, str] | None:
    """Split a mosaic's file name into its field, pattern and band."""
    hits = list(BAND.finditer(name))
    if not hits:
        return None
    m = hits[-1]
    before, band, after = name[: m.start()], m.group(0).lower(), name[m.end() :]
    field = NOISE.sub("", before.rstrip("-_"))
    if not field:
        return None
    # {BAND} fills in capitals, for file systems where case matters
    slot = "{BAND}" if m.group(0).isupper() else "{band}"
    return field, "{field}" + before[len(field) :] + slot + after, band


def _nearest(bands: list[str], microns: float) -> str:
    return min(bands, key=lambda b: abs(_microns(b) - microns))


def colours(bands: list[str]) -> tuple[str, str, str]:
    """Pick the red, green and blue bands, red also being the one to detect in."""
    red = _nearest(bands, 2.77)
    green = _nearest([b for b in bands if b != red], 1.5)
    blue = min((b for b in bands if b not in {red, green}), key=_microns)
    return red, green, blue


Seen = dict[str, dict[str, set[str]]]


def _after_band(seen: Seen) -> Seen:
    # names that put the field after the filter leave one identical field per pattern
    names = {
        p.replace("{field}", f): b for p, fs in seen.items() for f, b in fs.items()
    }
    if len(names) <= 1 or any(len(fs) > 1 for fs in seen.values()):
        return seen
    pre = os.path.commonprefix(list(names))  # noqa: RUF071 names, not paths, character by character
    pre = pre[: max(pre.rfind(c) for c in SEPARATORS) + 1]
    suf = os.path.commonprefix([n[::-1] for n in names])[::-1]  # noqa: RUF071 as above
    cut = [i for i in (suf.find(c) for c in SEPARATORS) if i >= 0]
    suf = suf[min(cut) :] if cut else ""
    named = "{band}" in pre + suf or "{BAND}" in pre + suf
    if not named or len(pre) + len(suf) >= min(map(len, names)):
        return seen
    out: Seen = defaultdict(lambda: defaultdict(set))
    for name, bands in names.items():
        out[pre + "{field}" + suf][name[len(pre) : len(name) - len(suf)]] |= bands
    return out


def _scan(folder: Path) -> Seen:
    seen: Seen = defaultdict(lambda: defaultdict(set))
    for path in sorted([*folder.glob("*.fits"), *folder.glob("*.fits.gz")]):
        got = split_name(path.name)
        if got:
            field, pattern, band = got
            seen[pattern][field].add(band)
    science = {p: fs for p, fs in seen.items() if not AUXILIARY.search(p)}
    return _after_band(science or seen)


def _image_hdu(path: Path) -> int:
    # DJA and JADES mosaics keep the image in the primary HDU, MAST's in the next
    try:
        primary = fits.getheader(path, 0)
    except OSError:
        return 1
    return 0 if primary.get("NAXIS") == IMAGE_AXES else 1


def guess(folder: Path) -> dict[str, object]:
    """Work out a config's pattern, bands, detect band and colour from file names."""
    seen = _scan(folder)
    if not seen:
        msg = f"No .fits or .fits.gz in {folder} has a filter like f277w in its name."
        raise SystemExit(msg)
    pattern = max(seen, key=lambda p: sum(map(len, seen[p].values())))
    fields = seen[pattern]
    common: list[str] = sorted(set.intersection(*fields.values()), key=_microns)
    if len(common) < len(RGB):
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
    one = min(fields)
    paths = {
        b: folder / pattern.format(field=one, band=b, BAND=b.upper()) for b in keep
    }
    return out | _reading(paths, detect)


def _reading(paths: dict[str, Path], detect: str) -> dict[str, int]:
    # the HDU the image is in and the tile size, where they aren't the defaults
    hdu = _image_hdu(paths[detect])
    tile = _tile(list(paths.values()), paths[detect], hdu)
    return ({"hdu": 0} if hdu == 0 else {}) | ({"tile": tile} if tile else {})


def _tile(paths: list[Path], det: Path, hdu: int) -> int:
    # a tile side in detection pixels that keeps cut near TILE_BYTES, or 0 for none
    try:
        shapes = [fits.getheader(p, hdu) for p in paths]
        h = fits.getheader(det, hdu)
    except (OSError, KeyError, IndexError):
        return 0
    need = BYTES_PER_PIXEL * sum(int(x["NAXIS1"]) * int(x["NAXIS2"]) for x in shapes)
    if need <= 2 * TILE_BYTES:
        return 0
    side = max(int(h["NAXIS1"]), int(h["NAXIS2"])) * math.sqrt(TILE_BYTES / need)
    return max(2000, int(side) // 1000 * 1000)


def main(argv: list[str] | None = None) -> int:
    """Write a config for a folder of mosaics next to it."""
    p = argparse.ArgumentParser(prog="jwst-peculiar init FOLDER CONFIG")
    p.add_argument("folder", type=Path, help="the folder of mosaics")
    p.add_argument("config", type=Path, help="the config to write, beside the folder")
    a = p.parse_args(argv)
    if a.config.exists():
        msg = f"{a.config} is already there, so it was left alone."
        raise SystemExit(msg)
    keys = guess(a.folder)
    keys["name"] = a.config.stem
    keys["out"] = f"runs/{a.config.stem}"
    if a.config.resolve().parent != a.folder.resolve().parent:
        keys["pattern"] = (
            a.folder.resolve() / str(keys["pattern"]).split("/", 1)[1]
        ).as_posix()
    a.config.write_text(yaml.safe_dump(keys, sort_keys=False), encoding="utf-8")
    print(f"Wrote {a.config}. Run jwst-peculiar check {a.config} next.")
    return 0
