"""A survey's YAML config, and what the steps share about fields and tiles.

With `tile` set, each mosaic is cut in squares named `FIELD~ROW-COL`, read with an
`OVERLAP` margin and keeping only the objects centred in their own square.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field, fields, replace
import difflib
import glob
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias
import warnings

from astropy import units as u
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
import numpy as np
import yaml

if TYPE_CHECKING:
    from collections.abc import Generator

    import numpy.typing as npt
    import pandas as pd

RGB = ("red", "green", "blue")
TILE_MARK = "~"
# written in place of a tile's outputs when the tile has no sources, off a mosaic's edge
EMPTY = "empty"
OVERLAP = 12.0
# the box size cut measures the sky background in
MESH = 64
# cut committed 3.7 GB at its peak on SMACS 0723, whose four bands add up to 121 Mpx
BYTES_PER_PIXEL = 3.7e9 / 121e6
Box = tuple[int, int, int, int]
# astropy has no type hints, so these only name what a value is
FitsHDUList: TypeAlias = Any
SkyWcs: TypeAlias = Any
SkyCoords: TypeAlias = Any
# every test cut writes, as `flag_NAME`, which `skip` can name
FLAGS = (
    "one_band", "target_star", "blob", "star", "spike", "edge", "merged", "outshone",
    "uncovered", "line", "blue_star", "sharp", "one_pair", "dark_middle",
    "listed_star", "gaia", "piece", "pixel", "row",
)  # fmt: skip
NIRCAM_SHARPEST = (("f115w", "f150w", "f277w", "f444w"), (0.88, 0.865, 0.70, 0.61))


def memory_size() -> float:
    """Return the machine's memory in bytes, or 0 when it can't be read."""
    if os.name == "nt":
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
    """Split a field name into its mosaic and, for a tile, its row and column."""
    field, mark, where = name.partition(TILE_MARK)
    if not mark:
        return field, None
    row, col = where.split("-")
    return field, (int(row), int(col))


def sky_wcs(header: Any) -> Any:  # noqa: ANN401 astropy has no types
    """Return a header's celestial WCS, without astropy's notes on fixing it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        return WCS(header).celestial


def arcsec_per_pixel(wcs: SkyWcs) -> float:
    """Return the side of a WCS's pixels in arcsec."""
    return float(np.sqrt(abs(np.linalg.det(wcs.pixel_scale_matrix))) * 3600)


def to_mjy_sr(header: Any) -> float | None:  # noqa: ANN401 astropy has no types
    """Return what turns a mosaic's pixels into MJy/sr, going by its BUNIT."""
    try:
        unit = u.Unit(str(header.get("BUNIT", "MJy/sr")))
    except (TypeError, ValueError):
        return None
    area = 1.0
    if unit.is_equivalent(u.Jy):
        # square degrees a pixel covers
        area = float(abs(np.linalg.det(sky_wcs(header).pixel_scale_matrix)))
        unit /= u.deg**2
    if not unit.is_equivalent(u.MJy / u.sr):
        return None
    # astropy types the scale as possibly complex, though a unit's never is
    scale: Any = unit.to(u.MJy / u.sr)
    return float(scale) / area


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
class Settings:
    """What a survey can change about detection and the tests, under `settings:`."""

    # the compact list's four bands, shortest first, where NIRCam takes the first with
    # the third and the second with the fourth
    compact_bands: tuple[str, ...] = NIRCAM_SHARPEST[0]
    # each compact band's 99th percentile of 0.1" over 0.25" flux for 742 Gaia stars
    psf_sharpest: tuple[float, ...] = NIRCAM_SHARPEST[1]
    compact_sn: float = 10.0
    compact_a: float = 0.12
    compact_area: float = 60.0
    stamp_sn: float = 40.0
    stamp_a: float = 0.12
    stamp_area: float = 60.0
    detect_sigma: float = 1.5
    detect_pixels: int = 10
    deblend_contrast: float = 0.005
    # arcsec from uncovered sky a compact candidate is flagged within
    edge: float = 1.0
    skip: tuple[str, ...] = ()

    def flagged(self, t: pd.DataFrame) -> npt.NDArray[np.bool_]:
        """Say which rows a test caught, leaving out the tests in `skip`."""
        cols = [
            c for c in t.columns if c.startswith("flag_") and c[5:] not in self.skip
        ]
        return np.asarray(t[cols].to_numpy(bool).any(axis=1), bool)


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
    # the fine-tuned models that drop artefacts from the compact list
    artefacts: Path | None = None
    device: str = "auto"
    tile: int = 0
    settings: Settings = field(default_factory=Settings)

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
        pad = OVERLAP / arcsec_per_pixel(det)
        x0, x1, y0, y1 = core
        xs = np.array([x0 - pad, x1 + pad, x1 + pad, x0 - pad])
        ys = np.array([y0 - pad, y0 - pad, y1 + pad, y1 + pad])
        ra, dec = det.all_pix2world(xs, ys, 0)
        return {b: _box(self._header(name, b), ra, dec) for b in self.bands}

    def cut_fields(self) -> list[str]:
        """Return the fields to read, the configured ones or every one cut."""
        root = self.out / "fields"
        found = sorted(f.name for f in root.iterdir()) if root.is_dir() else []
        names = found
        if self.fields:
            # a configured mosaic cut in tiles is read as its tiles
            names = []
            for f in self.fields:
                names += [n for n in found if n != f and split_tile(n)[0] == f] or [f]
        names = [n for n in names if not (root / n / EMPTY).is_file()]
        uncut = [n for n in names if not (root / n / "stamps.npy").is_file()]
        if not names or uncut:
            what = ", ".join(uncut[:3]) if uncut else "Nothing"
            msg = (
                f"{what} hasn't been cut into {root} yet. Run jwst-peculiar cut first."
            )
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
        """Return each mosaic to search, or each of its tiles with `tile`."""
        names = self.mosaics()
        if not self.tile:
            return names
        out = []
        for name in names:
            if split_tile(name)[1] is not None:
                out.append(name)
                continue
            try:
                h = self._header(name, self.detect)
            except OSError:
                # check names it as missing or cut short, and cut as a field that failed
                out.append(name)
                continue
            rows = math.ceil(int(h["NAXIS2"]) / self.tile)
            cols = math.ceil(int(h["NAXIS1"]) / self.tile)
            out += [
                f"{name}{TILE_MARK}{r}-{c}" for r in range(rows) for c in range(cols)
            ]
        return out

    def mosaics(self) -> list[str]:
        """Return the mosaics to search, the configured ones or every one on disk."""
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
    """Yield a path beside `path` to write to, moved onto `path` once the block ends."""
    tmp = path.with_name(f"{path.stem}.part{path.suffix}")
    try:
        yield tmp
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _check_keys(path: Path, raw: dict[str, Any]) -> None:
    _unknown(path, raw, set(Config.__dataclass_fields__), what="key")
    for key in ("name", "out", "pattern"):
        if key not in raw:
            msg = f"{path} has no {key!r}, which every config needs."
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


def _settings(path: Path, raw: object) -> Settings:
    if not isinstance(raw, dict):
        msg = f"{path}'s settings should be keys like compact_sn: 10, not {raw!r}."
        raise SystemExit(msg)
    _unknown(path, raw, {f.name for f in fields(Settings)}, what="settings key")
    args: dict[str, Any] = {}
    for key, value in raw.items():
        default = getattr(Settings(), key)
        try:
            if isinstance(default, tuple):
                items = [value] if isinstance(value, str | float | int) else value
                args[key] = tuple(
                    float(v) if key == "psf_sharpest" else str(v).lower() for v in items
                )
            else:
                args[key] = type(default)(value)
        except (TypeError, ValueError) as err:
            msg = (
                f"{path}'s settings {key} can't be {value!r}, as it's like {default!r}."
            )
            raise SystemExit(msg) from err
    out = replace(Settings(), **args)
    if out.compact_bands != NIRCAM_SHARPEST[0] and "psf_sharpest" not in raw:
        msg = (
            f"{path} sets compact_bands, so it needs psf_sharpest for those bands too."
        )
        raise SystemExit(msg)
    if len(out.compact_bands) != 4 or len(out.psf_sharpest) != 4:  # noqa: PLR2004 the tests read four bands
        msg = f"{path}'s compact_bands and psf_sharpest need four each, shortest first."
        raise SystemExit(msg)
    _unknown(path, dict.fromkeys(out.skip), set(FLAGS), what="flag in skip")
    return out


def _unknown(path: Path, raw: dict[str, Any], known: set[str], *, what: str) -> None:
    for key in raw:
        if key not in known:
            near = difflib.get_close_matches(key, known, n=1)
            hint = f" Did you mean {near[0]!r}?" if near else ""
            msg = (
                f"{path} has an unknown {what} {key!r}.{hint} It knows {sorted(known)}."
            )
            raise SystemExit(msg)


def _resolve(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    here = path.resolve().parent

    def rel(p: str) -> Path:
        return Path(p) if Path(p).is_absolute() else here / p

    args = dict(raw)
    args["out"] = rel(raw["out"])
    args["pattern"] = rel(raw["pattern"]).as_posix()
    for key in ("stars", "gaia", "artefacts"):
        if isinstance(raw.get(key), str):
            args[key] = rel(raw[key])
    for key in ("bands", "colour", "fields"):
        if key in raw:
            args[key] = _names(raw[key], keep_case=key == "fields")
    if "detect" in raw:
        args["detect"] = str(raw["detect"]).lower()
    args["settings"] = _settings(path, raw.get("settings", {}))
    if len(args.get("colour", RGB)) != len(RGB):
        msg = f"{path}'s colour needs three bands, red to blue, not {args['colour']}."
        raise SystemExit(msg)
    return args


def read_config(path: Path) -> Config:
    """Read a config, with paths in it taken relative to the config's folder."""
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
