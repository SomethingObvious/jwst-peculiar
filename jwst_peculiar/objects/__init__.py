"""`jwst-peculiar objects`, the per-object search, driven by one YAML config.

`cut` detects objects in each field and cuts their stamps, `embed` turns the stamps
into Zoobot embeddings, `rank` writes the rarity list, `like` finds the objects most
like a seed, `view` opens a list in the web viewer to judge, and `label`
trains the classifier `rank` weights rarity by on the verdicts given there.
`compact` ranks the small sources `cut` leaves without a stamp on their colours,
`init` writes a config for a folder of mosaics, and `check` says what a config finds
before any of that runs.

With `tile` set, each mosaic is cut in squares that many detection-band pixels on a
side, named `FIELD~ROW-COL`. Each is read with a margin of `OVERLAP` arcsec so an
object near its edge is whole, and keeps only the objects centred in its own square.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import glob
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
import warnings

from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
import numpy as np
import yaml

if TYPE_CHECKING:
    from collections.abc import Generator

STEPS = (
    "init",
    "check",
    "run",
    "cut",
    "embed",
    "rank",
    "like",
    "label",
    "view",
    "compact",
)
TILE_MARK = "~"
OVERLAP = 12.0
# the box size cut measures the sky background in
MESH = 64
# cut committed 3.7 GB at its peak on SMACS 0723, whose four bands add up to 121 Mpx
BYTES_PER_PIXEL = 3.7e9 / 121e6
Box = tuple[int, int, int, int]


def memory_size() -> float:
    """Return the machine's memory in bytes, or 0 when it can't be read."""
    if os.name == "nt":
        import ctypes  # noqa: PLC0415 Windows only

        # MEMORYSTATUSEX is 64 bytes: its own length, then the total memory at 8
        status = (ctypes.c_ulonglong * 8)(64)
        kernel = getattr(ctypes, "windll").kernel32  # noqa: B009 windll is Windows only
        return float(status[1]) if kernel.GlobalMemoryStatusEx(status) else 0.0
    try:
        conf = getattr(os, "sysconf")  # noqa: B009 sysconf isn't on Windows
        return float(conf("SC_PAGE_SIZE") * conf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return 0.0


def split_tile(name: str) -> tuple[str, tuple[int, int] | None]:
    """Split a field name into its mosaic and, for a tile, its row and column.

    Returns:
        The mosaic's field name, and the tile's row and column or None.
    """
    field, mark, where = name.partition(TILE_MARK)
    if not mark:
        return field, None
    row, col = where.split("-")
    return field, (int(row), int(col))


def sky_wcs(header: Any) -> Any:  # noqa: ANN401 astropy has no types
    """Return a header's celestial WCS, without astropy's notes on fixing it.

    JWST headers get their dates and observatory place filled in on every read,
    a line each per band per field, and none of it matters for positions.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        return WCS(header).celestial


def _box(header: Any, ra: Any, dec: Any) -> Box:  # noqa: ANN401 astropy has no types
    # the pixels of one band that cover the sky corners given, clipped to its image
    x, y = sky_wcs(header).all_world2pix(ra, dec, 0)
    nx, ny = int(header["NAXIS1"]), int(header["NAXIS2"])
    # starting on a multiple of the background mesh keeps a tile's sky the field's
    x0 = max(0, math.floor(float(x.min())) // MESH * MESH)
    y0 = max(0, math.floor(float(y.min())) // MESH * MESH)
    return (
        x0,
        min(nx, math.ceil(float(x.max())) + 1),
        y0,
        min(ny, math.ceil(float(y.max())) + 1),
    )


@dataclass(frozen=True)
class Config:
    """Where a survey's mosaics are and how to search them."""

    name: str
    out: Path
    pattern: str
    bands: tuple[str, ...] = ("f115w", "f150w", "f277w", "f444w")
    detect: str = "f277w"
    colour: tuple[str, str, str] = ("f277w", "f150w", "f115w")
    hdu: int | str = 1
    fields: tuple[str, ...] = ()
    stars: Path | None = None
    gaia: bool | Path = True
    device: str = "auto"
    tile: int = 0

    def band_path(self, name: str, band: str) -> Path:
        """Return the mosaic of one band of one field, or of the field a tile is in."""
        field = split_tile(name)[0]
        return Path(self.pattern.format(field=field, band=band, BAND=band.upper()))

    def _header(self, name: str, band: str) -> Any:  # noqa: ANN401 astropy has no types
        return fits.getheader(self.band_path(name, band), self.hdu)

    def core(self, name: str) -> Box | None:
        """Return a tile's own square, x0, x1, y0, y1 in detection-band pixels."""
        _, where = split_tile(name)
        if where is None:
            return None
        row, col = where
        h = self._header(name, self.detect)
        nx, ny = int(h["NAXIS1"]), int(h["NAXIS2"])
        t = self.tile
        return col * t, min((col + 1) * t, nx), row * t, min((row + 1) * t, ny)

    def boxes(self, name: str) -> dict[str, Box] | None:
        """Return the part of each band to read for a tile, its square plus a margin."""
        core = self.core(name)
        if core is None:
            return None
        det = sky_wcs(self._header(name, self.detect))
        pad = OVERLAP / float(
            np.sqrt(abs(np.linalg.det(det.pixel_scale_matrix))) * 3600
        )
        x0, x1, y0, y1 = core
        xs = np.array([x0 - pad, x1 + pad, x1 + pad, x0 - pad])
        ys = np.array([y0 - pad, y0 - pad, y1 + pad, y1 + pad])
        ra, dec = det.all_pix2world(xs, ys, 0)
        return {b: _box(self._header(name, b), ra, dec) for b in self.bands}

    def cut_fields(self) -> list[str]:
        """Return the fields to read, the configured ones or every one cut.

        Raises:
            SystemExit: When there's nothing cut yet, or a configured field isn't.
        """
        root = self.out / "fields"
        found = sorted(f.name for f in root.iterdir()) if root.is_dir() else []
        names = list(self.fields) or found
        uncut = [n for n in names if not (root / n / "stamps.npy").is_file()]
        if not names or uncut:
            what = ", ".join(uncut[:3]) if uncut else "Nothing"
            msg = f"{what} hasn't been cut into {root} yet. Run objects cut first."
            raise SystemExit(msg)
        return names

    def pixels(self, name: str) -> int:
        """Return the pixels cut reads for a field or tile, over all its bands."""
        boxes = self.boxes(name)
        if boxes is not None:
            return sum((x1 - x0) * (y1 - y0) for x0, x1, y0, y1 in boxes.values())
        shapes = [self._header(name, b) for b in self.bands]
        return sum(int(h["NAXIS1"]) * int(h["NAXIS2"]) for h in shapes)

    def missing(self) -> list[Path]:
        """Return the mosaics the fields to search should have and don't."""
        return [
            p
            for name in self.mosaics()
            for p in (self.band_path(name, b) for b in self.bands)
            if not p.is_file()
        ]

    def field_names(self) -> list[str]:
        """Return the fields to search, each mosaic or, with `tile`, each tile of one.

        Returns:
            The names.
        """
        names = self.mosaics()
        if not self.tile:
            return names
        out = []
        for name in names:
            if split_tile(name)[1] is not None:
                out.append(name)
                continue
            h = self._header(name, self.detect)
            rows = math.ceil(int(h["NAXIS2"]) / self.tile)
            cols = math.ceil(int(h["NAXIS1"]) / self.tile)
            out += [
                f"{name}{TILE_MARK}{r}-{c}" for r in range(rows) for c in range(cols)
            ]
        return out

    def mosaics(self) -> list[str]:
        """Return the mosaics to search, the configured ones or every one on disk.

        Raises:
            SystemExit: When the pattern finds no detection-band mosaic.
        """
        if self.fields:
            return list(self.fields)
        pre, post = (
            part.format(band=self.detect, BAND=self.detect.upper())
            for part in self.pattern.split("{field}", 1)
        )
        # escaped, as a folder like "data [2024]" would otherwise be a character class
        hits = glob.glob(glob.escape(pre) + "*" + glob.escape(post))  # noqa: PTH207 {field} can sit in a folder name
        found = (Path(f).as_posix() for f in hits)
        names = sorted(f[len(pre) : len(f) - len(post)] for f in found)
        if not names:
            msg = f"No {self.detect} mosaics match {self.pattern}."
            raise SystemExit(msg)
        return names


@contextmanager
def written(path: Path) -> Generator[Path]:
    """Yield a path beside `path` to write to, moved onto `path` once the block ends.

    Steps skip a field whose output exists, so a file left half-written by Ctrl+C or
    a full disk would otherwise pass for a finished one.

    Yields:
        The temporary path, with the same suffix so numpy and pandas keep the name.
    """
    tmp = path.with_name(f"{path.stem}.part{path.suffix}")
    try:
        yield tmp
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _check_keys(path: Path, raw: dict[str, Any]) -> None:
    known = set(Config.__dataclass_fields__)
    for key in raw:
        if key not in known:
            near = difflib.get_close_matches(key, known, n=1)
            hint = f" Did you mean {near[0]!r}?" if near else ""
            msg = f"{path} has an unknown key {key!r}.{hint} It knows {sorted(known)}."
            raise SystemExit(msg)
    for key in ("name", "out", "pattern"):
        if key not in raw:
            msg = f"{path} has no {key!r}, which every objects config needs."
            raise SystemExit(msg)


def _check_bands(path: Path, cfg: Config) -> None:
    named = "{band}" in cfg.pattern or "{BAND}" in cfg.pattern
    if not named or ("{field}" not in cfg.pattern and not cfg.fields):
        msg = (
            f"{path}'s pattern {cfg.pattern!r} needs {{band}} (or {{BAND}} for file"
            " names in capitals), and {field} unless the config lists its fields."
        )
        raise SystemExit(msg)
    missing = [b for b in (cfg.detect, *cfg.colour) if b not in cfg.bands]
    if missing:
        msg = f"{path} uses {missing} for detect or colour, but its bands are"
        msg += f" {list(cfg.bands)}."
        raise SystemExit(msg)


def _names(value: str | list[object], *, keep_case: bool) -> tuple[str, ...]:
    # papers write F115W, but the column names and every test use f115w
    names = [str(value)] if isinstance(value, str) else [str(v) for v in value]
    return tuple(names if keep_case else (n.lower() for n in names))


def _resolve(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    here = path.resolve().parent

    def rel(p: str) -> Path:
        return Path(p) if Path(p).is_absolute() else here / p

    args = dict(raw)
    args["out"] = rel(raw["out"])
    args["pattern"] = rel(raw["pattern"]).as_posix()
    for key in ("stars", "gaia"):
        if isinstance(raw.get(key), str):
            args[key] = rel(raw[key])
    for key in ("bands", "colour", "fields"):
        if key in raw:
            args[key] = _names(raw[key], keep_case=key == "fields")
    if "detect" in raw:
        args["detect"] = str(raw["detect"]).lower()
    if len(args.get("colour", "rgb")) != 3:  # noqa: PLR2004 red, green and blue
        msg = f"{path}'s colour needs three bands, red to blue, not {args['colour']}."
        raise SystemExit(msg)
    return args


def read_config(path: Path) -> Config:
    """Read a config, with paths in it taken relative to the config's folder.

    Returns:
        The config.

    Raises:
        SystemExit: When the file is missing or isn't a config this can run.
    """
    if not path.is_file():
        msg = f"There's no config at {path}."
        raise SystemExit(msg)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as err:
        msg = f"{path} isn't valid YAML: {err}"
        raise SystemExit(msg) from err
    if not isinstance(raw, dict):
        msg = f"{path} should be a list of keys like name: and pattern:, not {raw!r}."
        raise SystemExit(msg)
    _check_keys(path, raw)
    cfg = Config(**_resolve(path, raw))
    _check_bands(path, cfg)
    return cfg


def run(cfg: Config, rest: list[str]) -> int:
    """Check, cut, embed, rank and list compact sources, stopping at a failed step.

    Returns:
        The first nonzero exit code, or 0.
    """
    from jwst_peculiar.objects import compact  # noqa: PLC0415 these load torch and scikit-learn
    from jwst_peculiar.objects.check import main as check  # noqa: PLC0415 as above
    from jwst_peculiar.objects.cut import main as cut  # noqa: PLC0415 as above
    from jwst_peculiar.objects.embed import main as embed  # noqa: PLC0415 as above
    from jwst_peculiar.objects.rank import main as rank  # noqa: PLC0415 as above

    steps = [(check, []), (cut, rest), (embed, []), (rank, ["rank"])]
    if compact.has_bands(cfg):
        steps.append((compact.main, []))
    for step, args in steps:
        code = step(cfg, args)
        if code:
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one step of the object search.

    Returns:
        The step's exit code.
    """
    p = argparse.ArgumentParser(description="Run one step of the object search.")
    p.add_argument("step", choices=STEPS)
    p.add_argument(
        "config", type=Path, help="the survey's YAML config, or for init its mosaics"
    )
    p.add_argument("rest", nargs=argparse.REMAINDER, help="the step's own options")
    a = p.parse_args(argv)
    if a.step == "init":
        from jwst_peculiar.objects.init import main as init  # noqa: PLC0415 it reads no config

        return init([str(a.config), *a.rest])
    cfg = read_config(a.config)
    if a.step == "run":
        return run(cfg, a.rest)
    if a.step == "check":
        from jwst_peculiar.objects.check import main as step  # noqa: PLC0415 loads torch for the device
    elif a.step == "cut":
        from jwst_peculiar.objects.cut import main as step  # noqa: PLC0415 torch-free but slow to import
    elif a.step == "embed":
        from jwst_peculiar.objects.embed import main as step  # noqa: PLC0415 loads torch
    elif a.step == "view":
        from jwst_peculiar.objects.view import main as step  # noqa: PLC0415 loads the server
    elif a.step == "compact":
        from jwst_peculiar.objects.compact import main as step  # noqa: PLC0415 loads scikit-learn
    else:
        from jwst_peculiar.objects.rank import main as step  # noqa: PLC0415 loads scikit-learn

        return step(cfg, [a.step, *a.rest])
    return step(cfg, a.rest)
