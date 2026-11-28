"""`cut`: find the objects in each field, measure them and cut their stamps."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial
import json
import os
import time
import traceback
from typing import TYPE_CHECKING, Any
import warnings

from astropy.convolution import Gaussian2DKernel, convolve
from astropy.io import fits
from astropy.utils.exceptions import AstropyWarning
import numpy as np
import pandas as pd
import photutils
from photutils.aperture import CircularAperture, aperture_photometry
from photutils.background import Background2D, MedianBackground
from photutils.segmentation import SourceCatalog, deblend_sources, detect_sources
from PIL import Image
import pyvo
import requests
from scipy.ndimage import map_coordinates, maximum_filter
from typing_extensions import override

from jwst_peculiar import compact
from jwst_peculiar.config import (
    BYTES_PER_PIXEL,
    EMPTY,
    MESH,
    Settings,
    arcsec_per_pixel,
    memory_size,
    sky_wcs,
    split_tile,
    to_mjy_sr,
    written,
)
from jwst_peculiar.flags import blobs, flags, grid, stamp_pixels

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import numpy.typing as npt

    from jwst_peculiar.config import Box, Config, FitsHDUList, SkyWcs

    Floats = npt.NDArray[np.float64]
    Bools = npt.NDArray[np.bool_]

# photutils 3.1 deprecates progress_bar, and before that a bar shows without it
QUIET: dict[str, Any] = (
    {}
    if tuple(map(int, photutils.__version__.split(".")[:2])) >= (3, 1)
    else {"progress_bar": False}
)

# stamps are N pixels across, covering 3.5 semimajor axes and at least 0.6" each side
N = 224
MIN_HALF, HALF_PER_A = 0.6, 3.5
# how hard the asinh compresses a stamp's bright end
STRETCH = 8.0
# a stamp more than 5% blank is dropped, and a centre under 0.6 of the brightest
# pixel is outshone
MAX_ZEROS, OUTSHONE = 0.05, 0.6
# aperture radii in arcsec
RADII = (0.05, 0.1, 0.25)
NJY_PER_MJY_SR_ARCSEC2 = 2.350443e-11 * 1e15
FINE = grid(N)
GAIA_TAP = (
    "https://gea.esac.esa.int/tap-server/tap",
    # Heidelberg's copy of DR3, for when ESA's archive is down
    "https://gaia.ari.uni-heidelberg.de/tap",
)
# seconds Gaia may go quiet before cut gives up on it
TIMEOUT_S = 60
# seconds to wait before each new try, as Gaia turns away fields cut at once (HTTP 429)
GAIA_WAITS = (10, 30, 90)


class NoData(Exception):  # noqa: N818 it isn't an error for a tile off a mosaic's edge
    """A field or tile with no sources to find."""


class PatientSession(requests.Session):
    """A session that gives up on a stalled server, which pyvo's own never does."""

    @override
    def request(
        self, method: str, url: Any, *args: Any, **kwargs: Any
    ) -> requests.Response:
        # read when called, so a test can shorten it
        kwargs.setdefault("timeout", TIMEOUT_S)
        return super().request(method, url, *args, **kwargs)


def asinh_to_uint8(
    img: npt.NDArray[np.floating[Any]], lo: float, hi: float, *, asinh_q: float = 10.0
) -> npt.NDArray[np.uint8]:
    """Stretch flux to 8 bits through an asinh between fixed limits."""
    flux = np.asarray(img, dtype=np.float32)
    flux = np.where(np.isfinite(flux) & (flux != 0), flux, np.float32(0))
    x = np.clip((flux - lo) / (hi - lo + 1e-12), 0.0, 1.0)
    # rounded, as truncating would put the upper limit itself at 254
    out: npt.NDArray[np.uint8] = np.rint(
        255.0 * np.arcsinh(asinh_q * x) / np.arcsinh(asinh_q)
    ).astype(np.uint8)
    return out


def wcs_record(wcs: SkyWcs) -> tuple[dict[str, Any], float]:
    """Return a WCS as the header cards the viewer reads, and its pixel scale."""
    cd = wcs.pixel_scale_matrix
    record = {
        "CTYPE1": wcs.wcs.ctype[0],
        "CTYPE2": wcs.wcs.ctype[1],
        "CRPIX1": float(wcs.wcs.crpix[0]),
        "CRPIX2": float(wcs.wcs.crpix[1]),
        "CRVAL1": float(wcs.wcs.crval[0]),
        "CRVAL2": float(wcs.wcs.crval[1]),
        "CD1_1": float(cd[0, 0]),
        "CD1_2": float(cd[0, 1]),
        "CD2_1": float(cd[1, 0]),
        "CD2_2": float(cd[1, 1]),
    }
    return record, round(arcsec_per_pixel(wcs), 5)


@dataclass
class Band:
    """One band of one field, background-subtracted, with uncovered pixels as NaN."""

    raw: npt.NDArray[np.float32]
    data: npt.NDArray[np.float32]
    rms: npt.NDArray[np.float32]
    wcs: SkyWcs
    scale: float
    roll: float | None

    def pixels(self, ra: Floats, dec: Floats) -> tuple[Floats, Floats]:
        """Return pixel x and y of sky positions."""
        x, y = self.wcs.all_world2pix(ra, dec, 0)
        return np.asarray(x, float), np.asarray(y, float)


def _read(path: Path, hdu: int | str, box: Box | None) -> tuple[Any, Any]:
    try:
        hdul: FitsHDUList = fits.open(path, memmap=True)
        with hdul as h:
            header = h[hdu].header.copy()
            if box is None:
                return np.asarray(h[hdu].data, np.float32), header
            x0, x1, y0, y1 = box
            raw = np.asarray(h[hdu].section[y0:y1, x0:x1], np.float32)
    except (TypeError, ValueError, OSError, EOFError) as err:
        msg = f"Couldn't read {path} ({err}). A short file is a stopped download."
        raise SystemExit(msg) from err
    # moving the reference pixel puts the WCS, distortion included, on the box
    header["CRPIX1"] -= x0
    header["CRPIX2"] -= y0
    return raw, header


def read_band(path: Path, hdu: int | str, box: Box | None = None) -> Band:
    """Read a mosaic, or the box x0, x1, y0, y1 of one, less its background."""
    raw, header = _read(path, hdu, box)
    factor = to_mjy_sr(header) or 1.0
    if factor != 1:
        raw *= np.float32(factor)
    wcs = sky_wcs(header)
    off = ~np.isfinite(raw) | (raw == 0)
    z = np.where(off, 0, raw).astype(np.float32)
    scale = arcsec_per_pixel(wcs)
    roll = float(header["PA_V3"]) if "PA_V3" in header else None
    try:
        bkg = Background2D(
            z, MESH, filter_size=3, bkg_estimator=MedianBackground(), mask=off
        )
    except ValueError:
        # too little of the box has data to fit a sky to, which happens at a tile's
        # corner, so the band counts as having no data there
        nan = np.full(raw.shape, np.nan, np.float32)
        return Band(raw, nan, nan, wcs, scale, roll)
    data = np.where(off, np.nan, z - bkg.background).astype(np.float32)
    return Band(raw, data, bkg.background_rms.astype(np.float32), wcs, scale, roll)


def _parents(
    pieces: npt.NDArray[np.int32],
    parent: npt.NDArray[np.int32],
    labels: npt.NDArray[np.int_],
) -> list[int]:
    # every deblended piece lies inside exactly one segment of the map before deblending
    on = pieces > 0
    pairs = np.unique(pieces[on].astype(np.int64) * (1 << 31) + parent[on])
    keys, values = (pairs >> 31).tolist(), (pairs & ((1 << 31) - 1)).tolist()
    parent_of = dict(zip(keys, values, strict=True))
    return [parent_of[int(lab)] for lab in labels]


def detect(
    band: Band, *, s: Settings = compact.DEFAULT
) -> tuple[pd.DataFrame, npt.NDArray[np.int32]]:
    """Find and deblend sources, and say which parent each piece came from."""
    off = np.isnan(band.data)
    if off.all():
        msg = "The detect band has no data here."
        raise NoData(msg)
    det = np.nan_to_num(band.data)
    with warnings.catch_warnings():
        # Each says something handled here: the NaN left where nothing was observed,
        # which `off` masks, finding nothing, and a deblend falling back to linear.
        warnings.simplefilter("ignore", AstropyWarning)
        smooth = convolve(
            det, Gaussian2DKernel(2 / 2.355), mask=off, normalize_kernel=True
        )
        seg = detect_sources(
            smooth, s.detect_sigma * band.rms, s.detect_pixels, mask=off
        )
        if seg is None:
            msg = "Found no sources at all in this field. Is the detect band right?"
            raise NoData(msg)
        parent = seg.data.copy()
        seg = deblend_sources(
            smooth,
            seg,
            s.detect_pixels,
            n_levels=32,
            contrast=s.deblend_contrast,
            **QUIET,
        )
    cat = SourceCatalog(det, seg, convolved_data=smooth, error=band.rms, mask=off)
    cols = ["label", "x_centroid", "y_centroid", "area", "semimajor_axis"]
    cols += ["semiminor_axis", "segment_flux", "segment_flux_err", "max_value"]
    t = cat.to_table(columns=cols).to_pandas()
    t["parent"] = _parents(seg.data, parent, t.label.to_numpy(int))
    t["area"] = t.area.astype(float)
    t["sn"] = t.segment_flux / t.segment_flux_err
    t["a_arcsec"] = t.semimajor_axis * band.scale
    t["b_arcsec"] = t.semiminor_axis * band.scale
    sky = band.wcs.pixel_to_world(t.x_centroid.to_numpy(), t.y_centroid.to_numpy())
    t["ra"], t["dec"] = sky.ra.deg, sky.dec.deg
    return t.drop(columns=["x_centroid", "y_centroid"]), seg.data.astype(np.int32)


def blank_sky(
    segmap: npt.NDArray[np.int32], band: Band, *, n: int = 2000, clear: float = 0.5
) -> tuple[Floats, Floats]:
    """Return sky positions `clear` arcsec from any segment or uncovered pixel."""
    rng = np.random.default_rng(0)
    k = int(np.ceil(clear / band.scale))
    busy = maximum_filter((segmap > 0) | np.isnan(band.data), size=2 * k + 1)
    yy, xx = np.nonzero(~busy[k:-k, k:-k])
    if len(xx) < n:
        msg = "Too little empty sky in this field to measure aperture noise on."
        raise SystemExit(msg)
    pick = rng.choice(len(xx), n, replace=False)
    sky = band.wcs.pixel_to_world(xx[pick] + k, yy[pick] + k)
    return np.asarray(sky.ra.deg, float), np.asarray(sky.dec.deg, float)


def _sums(band: Band, xy: Floats, r: float) -> tuple[Floats, Floats, Bools]:
    # aperture flux, the noise the RMS map predicts for it, and whether it's all covered
    ap = CircularAperture(xy, r / band.scale)

    def total(img: npt.NDArray[Any]) -> Floats:
        return np.asarray(aperture_photometry(img, ap)["aperture_sum"], float)

    noise = np.sqrt(np.maximum(total(band.rms.astype(np.float64) ** 2), 0))
    # an aperture wholly off this band's image sums to NaN, and NaN > 0 is False
    bad = ~(total(np.isnan(band.data).astype(np.float32)) == 0)
    return total(np.nan_to_num(band.data)), noise, bad


def noise_factor(band: Band, blank: Floats, r: float) -> float:
    """Return how much more blank-sky apertures scatter than the RMS map predicts."""
    # drizzled pixels are correlated, so the map alone understates aperture noise
    flux, noise, bad = _sums(band, blank, r)
    ok = ~bad & (noise > 0)
    spread = 1.4826 * np.median(np.abs(flux[ok] - np.median(flux[ok])))
    return float(spread / np.median(noise[ok]))


def photometry(t: pd.DataFrame, band: Band, *, name: str, blank: Floats) -> str:
    """Add one band's aperture fluxes and errors to `t`, in nJy."""
    njy = band.scale**2 * NJY_PER_MJY_SR_ARCSEC2
    xy = np.column_stack(band.pixels(t.ra.to_numpy(float), t.dec.to_numpy(float)))
    bxy = np.column_stack(band.pixels(blank[:, 0], blank[:, 1]))
    said = []
    for r in RADII:
        flux, noise, bad = _sums(band, xy, r)
        factor = noise_factor(band, bxy, r)
        tag = f"{name}_{round(r * 100):02d}"
        t[f"f_{tag}"] = flux * njy
        t[f"e_{tag}"] = factor * noise * njy
        t["cover"] &= ~bad
        said.append(f"{tag} {factor:.2f}")
    return ", ".join(said)


def render(rgb: Floats) -> npt.NDArray[np.uint8]:
    """Turn a (3, N, N) flux stamp into uint8, asinh in total brightness."""
    edge = np.ones((N, N), bool)
    edge[N // 10 : -N // 10, N // 10 : -N // 10] = False
    rgb = rgb - np.nanmedian(rgb[:, edge], axis=1)[:, None, None]
    total = np.clip(rgb.mean(axis=0), 0, None)
    ref = max(float(np.percentile(total, 99.7)), 1e-12)
    gain = np.arcsinh(STRETCH * total / ref) / np.arcsinh(STRETCH)
    gain /= np.maximum(total, 1e-12)
    out = np.clip(rgb * gain, 0, None)
    out /= np.maximum(out.max(axis=0), 1.0)
    return np.asarray(np.rint(255 * out).transpose(1, 2, 0), np.uint8)


def _rgb(colour: list[Band], centre: tuple[float, float], h: float) -> Floats:
    # the middle band is read on its own grid and the other two mapped onto it
    green = colour[1]
    x, y = centre
    corners = green.wcs.pixel_to_world([x, x + h, x], [y, y, y + h])
    gx, gy = FINE
    own = map_coordinates(green.raw, [y + gy * h, x + gx * h], order=1)
    red, blue = (
        map_coordinates(b.raw, stamp_pixels(b, corners, FINE), order=1)
        for b in (colour[0], colour[2])
    )
    return np.stack([red, own, blue]).astype(float)


def stamps(t: pd.DataFrame, colour: list[Band]) -> tuple[npt.NDArray[np.uint8], Bools]:
    """Cut one colour stamp per object on the middle band's pixel grid."""
    green = colour[1]
    xs, ys = green.pixels(t.ra.to_numpy(float), t.dec.to_numpy(float))
    hs = t.half.to_numpy(float) / green.scale
    ny, nx = green.raw.shape
    out = np.zeros((len(t), N, N, 3), np.uint8)
    ok = np.zeros(len(t), bool)
    for i, (x, y, h) in enumerate(zip(xs, ys, hs, strict=True)):
        if not (h < x < nx - h and h < y < ny - h):
            continue
        rgb = _rgb(colour, (x, y), h)
        if (
            np.isfinite(rgb).all()
            and (rgb[1:] == 0).mean(axis=(1, 2)).max() <= MAX_ZEROS
        ):
            out[i] = render(rgb)
            ok[i] = True
    return out, ok


def outshone(cut: npt.NDArray[np.uint8]) -> Bools:
    """Say which stamps have a pixel well away from the centre much brighter than it."""
    out = np.zeros(len(cut), bool)
    c = slice(N * 3 // 8, N * 5 // 8)
    for i, s in enumerate(cut):
        total = np.asarray(s, float).sum(axis=2)
        out[i] = total[c, c].max() < OUTSHONE * total.max()
    return out


def gaia_stars(cfg: Config, name: str, t: pd.DataFrame) -> pd.DataFrame:
    """Return Gaia DR3 sources to G 21 over the field, cached in the run folder."""
    cols = ["ra", "dec", "phot_g_mean_mag", "classprob_dsc_combmod_star"]
    if cfg.gaia is False:
        return pd.DataFrame({c: [] for c in cols}, dtype=float)
    if cfg.gaia is not True:
        return pd.read_csv(cfg.gaia, usecols=cols)
    cache = cfg.out / "gaia" / f"{name}.csv"
    if cache.exists():
        return pd.read_csv(cache)
    where = f"1 = CONTAINS(POINT('ICRS', ra, dec), {_cone(t)})"
    src, fields = "gaiadr3.gaia_source", ", ".join(cols)
    q = f"SELECT {fields} FROM {src} WHERE phot_g_mean_mag < 21 AND {where}"  # noqa: S608 only numbers go in
    got = _query_gaia(q, name)
    cache.parent.mkdir(parents=True, exist_ok=True)
    got.to_csv(cache, index=False)
    return got


def _query_gaia(q: str, name: str) -> pd.DataFrame:
    """Run a query on each of GAIA_TAP in turn, retrying after each of GAIA_WAITS."""
    errors: list[Exception] = []
    for wait in (0, *GAIA_WAITS):
        time.sleep(wait)
        for url in GAIA_TAP:
            try:
                got: pd.DataFrame = (
                    pyvo.dal
                    .TAPService(url, session=PatientSession())
                    .run_sync(q)
                    .to_table()
                    .to_pandas()
                )
            except (pyvo.dal.DALAccessError, requests.RequestException) as err:
                errors.append(err)
            else:
                return got
    msg = f"Couldn't query Gaia for {name} ({errors[-1]})."
    msg += " Set gaia: false to skip spikes."
    raise SystemExit(msg) from errors[-1]


def _cone(t: pd.DataFrame) -> str:
    # Every copy of Gaia indexes a cone, while a range of RA scans the whole table.
    # Its 0.075 deg margin takes in all of a box 0.05 deg past the sources, corners too.
    ra, dec = t.ra.to_numpy(float), t.dec.to_numpy(float)
    # measured from the first source, so a field across RA 0 stays one piece
    ra = ra[0] + (ra - ra[0] + 180) % 360 - 180
    ra0, dec0 = ((ra.min() + ra.max()) / 2) % 360, (dec.min() + dec.max()) / 2
    half = np.ptp(ra) / 2 * np.cos(np.radians(dec0)), np.ptp(dec) / 2
    return f"CIRCLE('ICRS', {ra0}, {dec0}, {float(np.hypot(*half)) + 0.075})"


def describe(out: Path, band: Band, filters: tuple[str, ...]) -> None:
    """Write a preview of the field and field.json, which `view` shows."""
    h, w = band.data.shape
    step = max(1, int(np.ceil(max(h, w) / 2048)))
    # top row first, so the preview is the right way up like the stamps in the viewer
    ys, xs = np.arange(0, h, step)[::-1], np.arange(0, w, step)
    small = band.data[np.ix_(ys, xs)]
    lo, hi = np.nanpercentile(small, [50, 99.5])
    Image.fromarray(asinh_to_uint8(small, float(lo), float(hi))).save(out / "field.png")
    cards, scale = wcs_record(band.wcs)
    meta = {
        "shape": [h, w],
        "filters": [f.upper() for f in filters],
        "wcs": cards,
        "pixel_scale_arcsec": scale,
    }
    (out / "field.json").write_text(json.dumps(meta), encoding="utf-8")


@dataclass
class Measured:
    """One field's segments, measured in every band, and what they were found in."""

    seg: pd.DataFrame
    segmap: npt.NDArray[np.int32]
    bands: dict[str, Band]
    noise: list[str]
    strong: npt.NDArray[np.bool_]
    core: Box | None = None
    origin: tuple[int, int] = (0, 0)


def _own(
    t: pd.DataFrame, det: Band, *, core: Box | None, origin: tuple[int, int]
) -> Bools:
    # a tile keeps only what's centred in its own square, so neighbours never share one
    if core is None:
        return np.ones(len(t), bool)
    x, y = det.pixels(t.ra.to_numpy(float), t.dec.to_numpy(float))
    x, y = x + origin[0], y + origin[1]
    return (core[0] <= x) & (x < core[1]) & (core[2] <= y) & (y < core[3])


def measure(cfg: Config, name: str) -> Measured:
    """Detect every segment in a field or tile and measure it in every band."""
    boxes = cfg.boxes(name) or {}
    bands = {
        b: read_band(cfg.band_path(name, b), cfg.hdu, boxes.get(b)) for b in cfg.bands
    }
    seg, segmap = detect(bands[cfg.detect], s=cfg.settings)
    # by label, before a tile drops what isn't its own, as a neighbour may own one
    strong = np.zeros(int(np.max(segmap)) + 1, dtype=np.bool_)
    bright = seg.sn.to_numpy(float) >= cfg.settings.compact_sn
    strong[seg.label.to_numpy(int)[bright]] = True
    core = cfg.core(name)
    origin = (boxes[cfg.detect][0], boxes[cfg.detect][2]) if boxes else (0, 0)
    own = _own(seg, bands[cfg.detect], core=core, origin=origin)
    seg = seg.loc[own].reset_index(drop=True)
    if seg.empty:
        msg = "Every source here is centred in a neighbouring tile."
        raise NoData(msg)
    blank = np.column_stack(blank_sky(segmap, bands[cfg.detect]))
    seg["cover"] = True
    seg["field"] = name
    noise = [photometry(seg, bands[b], name=b, blank=blank) for b in cfg.bands]
    return Measured(seg, segmap, bands, noise, strong, core, origin)


def red_only(cfg: Config, name: str, m: Measured) -> pd.DataFrame:
    """Return F444W's segments not centred on a detection-band segment of S/N 10."""
    red = m.bands[cfg.settings.compact_bands[-1]]
    t, redmap = detect(red, s=cfg.settings)
    x, y = m.bands[cfg.detect].pixels(t.ra.to_numpy(float), t.dec.to_numpy(float))
    ny, nx = m.segmap.shape
    ix = np.clip(np.rint(x).astype(int), 0, nx - 1)
    iy = np.clip(np.rint(y).astype(int), 0, ny - 1)
    keep = ~m.strong[m.segmap[iy, ix]] & _own(
        t, m.bands[cfg.detect], core=m.core, origin=m.origin
    )
    t = pd.DataFrame(t.loc[keep].reset_index(drop=True))
    t["label"] += compact.OFFSET
    t["parent"] += compact.OFFSET
    t["cover"] = True
    t["field"] = name
    blank = np.column_stack(blank_sky(redmap, red))
    for b in cfg.bands:
        photometry(t, m.bands[b], name=b, blank=blank)
    return t


def cut_objects(
    cfg: Config, name: str, m: Measured, *, lone: pd.DataFrame
) -> pd.DataFrame:
    """Cut, flag and write one field's stamped objects."""
    seg, bands = m.seg, m.bands
    s = cfg.settings
    big = (
        (seg.area >= s.stamp_area) & (seg.sn > s.stamp_sn) & (seg.a_arcsec > s.stamp_a)
    )
    t = seg.loc[big.to_numpy()].reset_index(drop=True)
    t["half"] = np.maximum(MIN_HALF, HALF_PER_A * t.a_arcsec)
    cut, ok = stamps(t, [bands[b] for b in cfg.colour])
    t, cut = t.loc[ok].reset_index(drop=True), cut[ok]
    # Gaia covers where the objects are, or every segment when none has a stamp
    gaia = gaia_stars(cfg, name, t if len(t) else seg)
    flags(t, bands, cfg=cfg, gaia=gaia, lone=lone)
    t["flag_outshone"] = outshone(cut)
    out = cfg.out / "fields" / name
    seg.to_csv(out / "segments.csv", index=False)
    t.to_csv(out / "objects.csv", index=False)
    (out / "noise.json").write_text(json.dumps(m.noise), encoding="utf-8")
    describe(out, bands[cfg.colour[1]], cfg.colour)
    # embeddings of the stamps these replace would be paired with the wrong objects
    for old in out.glob("embeddings*.npy"):
        old.unlink()
    # last, as a field with stamps.npy counts as cut
    with written(out / "stamps.npy") as tmp:
        np.save(tmp, cut)
    return t


def cut_compact(
    cfg: Config, name: str, m: Measured, *, objects: pd.DataFrame, lone: pd.DataFrame
) -> pd.DataFrame:
    """Flag and write one field's compact candidates."""
    try:
        small = pd.concat([m.seg, red_only(cfg, name, m)], ignore_index=True)
    except NoData:
        small = m.seg
    small = small.loc[compact.is_candidate(small, s=cfg.settings)].reset_index(
        drop=True
    )
    gaia = gaia_stars(cfg, name, objects if len(objects) else m.seg)
    compact.compact_flags(
        small, m.bands, cfg=cfg, objects=objects, segments=m.seg, gaia=gaia, lone=lone
    )
    with written(cfg.out / "fields" / name / compact.LIST) as tmp:
        small.to_csv(tmp, index=False)
    return small


def _unflagged(t: pd.DataFrame, s: Settings) -> int:
    return int((~s.flagged(t)).sum())


def cut_field(cfg: Config, name: str, *, redo: bool = False) -> str:
    """Detect, measure, flag and cut one field into the run folder."""
    # a field already cut keeps its stamps, so its embeddings stay good
    out = cfg.out / "fields" / name
    try:
        m = measure(cfg, name)
    except NoData as err:
        if split_tile(name)[1] is None:
            msg = f"{name}: {err}"
            raise SystemExit(msg) from err
        out.mkdir(parents=True, exist_ok=True)
        (out / EMPTY).touch()
        return f"{name}: no sources, as the tile is off the mosaic's edge"
    out.mkdir(parents=True, exist_ok=True)
    lone = blobs(m.bands, cfg.bands)
    if redo or not (out / "stamps.npy").exists():
        t = cut_objects(cfg, name, m, lone=lone)
    else:
        t = pd.read_csv(out / "objects.csv", dtype={"field": str})
    said = f"{name}: {len(m.seg)} segments, {len(t)} with stamps"
    said += f", {_unflagged(t, cfg.settings)} unflagged"
    if not compact.has_bands(cfg):
        return said
    small = cut_compact(cfg, name, m, objects=t, lone=lone)
    return f"{said}, {len(small)} compact, {_unflagged(small, cfg.settings)} unflagged"


def fitting_workers(cfg: Config, names: list[str]) -> int:
    """Return how many fields to cut at once, a core in four and half the memory."""
    if len(names) <= 1:
        return 1
    need = BYTES_PER_PIXEL * max(cfg.pixels(n) for n in names)
    fit = int(memory_size() / 2 // need)
    return max(1, min(len(names), (os.cpu_count() or 1) // 4, fit))


def main(cfg: Config, argv: list[str]) -> int:
    """Cut every field that hasn't been cut yet."""
    p = argparse.ArgumentParser(prog="jwst-peculiar cut CONFIG")
    p.add_argument("--redo", action="store_true", help="cut fields already cut again")
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        help="fields to cut at once, each needing the memory check gives,"
        " by default as many as fit in half this machine's memory",
    )
    a = p.parse_args(argv)
    want = ["stamps.npy", compact.LIST] if compact.has_bands(cfg) else ["stamps.npy"]
    waiting = [
        name
        for name in cfg.field_names()
        if a.redo
        or not (
            (cfg.out / "fields" / name / EMPTY).exists()
            or all((cfg.out / "fields" / name / f).exists() for f in want)
        )
    ]
    workers = a.workers or fitting_workers(cfg, waiting)
    failed: list[str] = []

    def said(name: str, job: Callable[[], str]) -> None:
        try:
            print(job(), flush=True)
        except SystemExit as err:
            failed.append(f"{name}: {err}")
        except Exception as err:  # noqa: BLE001 it's printed whole and named at the end
            traceback.print_exception(err)
            failed.append(f"{name}: {err}")

    if workers <= 1:
        for name in waiting:
            said(name, partial(cut_field, cfg, name, redo=a.redo))
    else:
        print(f"cutting {len(waiting)} fields, {workers} at a time", flush=True)
        with ProcessPoolExecutor(workers) as pool:
            jobs = {
                pool.submit(cut_field, cfg, name, redo=a.redo): name for name in waiting
            }
            for job in as_completed(jobs):
                said(jobs[job], job.result)
    if failed:
        msg = "These failed, and the rest are cut. Cut again to retry just these.\n"
        raise SystemExit(msg + "\n".join(failed))
    return 0
