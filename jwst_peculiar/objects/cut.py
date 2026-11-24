"""`objects cut`: find the objects in each field, measure them and cut their stamps.

Detection is on one band (F277W by default), with a 64-pixel background, a
Gaussian of FWHM 2 pixels, sources at 1.5 sigma over 10 pixels, and deblending at
32 levels with contrast 0.005. Every segment is measured in 0.1" and 0.25"
apertures in every band, with errors scaled by how much more blank-sky apertures
scatter than the RMS map says. Segments with area 60 or more, S/N over 40 and
semimajor axis over 0.12" get a 224-pixel colour stamp and the artefact tests in
`flags`, and the small ones, with F444W's detections that F277W lacks, go to
`compact`.

Each field is held in memory whole, so a mosaic far bigger than about 15,000
pixels across wants splitting into fields first.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any

from astropy.convolution import Gaussian2DKernel, convolve
from astropy.io import fits
from astropy.wcs import WCS
import numpy as np
import pandas as pd
from photutils.aperture import CircularAperture, aperture_photometry
from photutils.background import Background2D, MedianBackground
from photutils.segmentation import SourceCatalog, deblend_sources, detect_sources
from PIL import Image
import requests
from scipy.ndimage import map_coordinates, maximum_filter

from jwst_peculiar.catalogues import PatientSession
from jwst_peculiar.objects import compact, written
from jwst_peculiar.objects.flags import blobs, flags, grid, stamp_pixels
from jwst_peculiar.prep import asinh_to_uint8, wcs_record

if TYPE_CHECKING:
    from pathlib import Path

    import numpy.typing as npt

    from jwst_peculiar.objects import Config
    from jwst_peculiar.utils import FitsHDUList, SkyWcs

    Floats = npt.NDArray[np.float64]
    Bools = npt.NDArray[np.bool_]

N = 224
MIN_HALF, SIZE, Q = 0.6, 3.5, 8.0
MIN_AREA, MIN_SN, MIN_A = 60, 40.0, 0.12
MAX_ZEROS, OUTSHONE = 0.05, 0.6
RADII = (0.1, 0.25)
NJY_PER_MJY_SR_ARCSEC2 = 2.350443e-11 * 1e15
FINE = grid(N)
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap"


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


def read_band(path: Path, hdu: int | str) -> Band:
    """Read a mosaic and subtract the same background detection uses.

    Returns:
        The band.
    """
    hdul: FitsHDUList = fits.open(path, memmap=True)
    with hdul as h:
        raw = np.asarray(h[hdu].data, np.float32)
        header = h[hdu].header
    wcs = WCS(header).celestial
    off = ~np.isfinite(raw) | (raw == 0)
    z = np.where(off, 0, raw).astype(np.float32)
    bkg = Background2D(z, 64, filter_size=3, bkg_estimator=MedianBackground(), mask=off)
    data = np.where(off, np.nan, z - bkg.background).astype(np.float32)
    scale = float(np.sqrt(abs(np.linalg.det(wcs.pixel_scale_matrix))) * 3600)
    roll = float(header["PA_V3"]) if "PA_V3" in header else None
    return Band(raw, data, bkg.background_rms.astype(np.float32), wcs, scale, roll)


def _parents(
    pieces: npt.NDArray[np.int32],
    parent: npt.NDArray[np.int32],
    labels: Any,  # noqa: ANN401 an int array
) -> list[int]:
    # every deblended piece lies inside exactly one segment of the map before deblending
    on = pieces > 0
    pairs = np.unique(pieces[on].astype(np.int64) * (1 << 31) + parent[on])
    keys, values = (pairs >> 31).tolist(), (pairs & ((1 << 31) - 1)).tolist()
    parent_of = dict(zip(keys, values, strict=True))
    return [parent_of[int(lab)] for lab in labels]


def detect(band: Band) -> tuple[pd.DataFrame, npt.NDArray[np.int32]]:
    """Find and deblend sources, and say which parent each piece came from.

    Returns:
        One row per segment, and the deblended segmentation map.

    Raises:
        SystemExit: When the band has no sources at all.
    """
    off = np.isnan(band.data)
    det = np.nan_to_num(band.data)
    smooth = convolve(det, Gaussian2DKernel(2 / 2.355), mask=off, normalize_kernel=True)
    seg = detect_sources(smooth, 1.5 * band.rms, 10, mask=off)
    if seg is None:
        msg = "Found no sources at all in this field. Is the detect band right?"
        raise SystemExit(msg)
    parent = seg.data.copy()
    seg = deblend_sources(
        smooth, seg, 10, n_levels=32, contrast=0.005, progress_bar=False
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
    """Return sky positions at least `clear` arcsec from any segment or uncovered pixel.

    Returns:
        RA and Dec in degrees.

    Raises:
        SystemExit: When the field has too little empty sky to measure noise on.
    """
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
    bad = total(np.isnan(band.data).astype(np.float32)) > 0
    return total(np.nan_to_num(band.data)), noise, bad


def noise_factor(band: Band, blank: Floats, r: float) -> float:
    """Return how much more blank-sky apertures scatter than the RMS map predicts.

    Drizzled pixels are correlated, so the map alone understates aperture noise.

    Returns:
        1.4826 times the median absolute deviation of the blank fluxes, over
        their median predicted noise.
    """
    flux, noise, bad = _sums(band, blank, r)
    ok = ~bad & (noise > 0)
    spread = 1.4826 * np.median(np.abs(flux[ok] - np.median(flux[ok])))
    return float(spread / np.median(noise[ok]))


def photometry(t: pd.DataFrame, band: Band, *, name: str, blank: Floats) -> str:
    """Add one band's aperture fluxes and errors to `t`, in nJy.

    Returns:
        The noise factors, for the log.
    """
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
    """Turn a (3, N, N) flux stamp into uint8, asinh in total brightness.

    Stretching the total rather than each band keeps colour in the core.

    Returns:
        The stamp, (N, N, 3).
    """
    edge = np.ones((N, N), bool)
    edge[N // 10 : -N // 10, N // 10 : -N // 10] = False
    rgb = rgb - np.nanmedian(rgb[:, edge], axis=1)[:, None, None]
    total = np.clip(rgb.mean(axis=0), 0, None)
    ref = max(float(np.percentile(total, 99.7)), 1e-12)
    gain = np.arcsinh(Q * total / ref) / np.arcsinh(Q) / np.maximum(total, 1e-12)
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
    red = map_coordinates(
        colour[0].raw, stamp_pixels(colour[0], corners, FINE), order=1
    )
    blue = map_coordinates(
        colour[2].raw, stamp_pixels(colour[2], corners, FINE), order=1
    )
    return np.stack([red, own, blue]).astype(float)


def stamps(t: pd.DataFrame, colour: list[Band]) -> tuple[Any, Bools]:
    """Cut one colour stamp per object on the middle band's pixel grid.

    Returns:
        The stamps, (n, N, N, 3) uint8, and which objects got one.
    """
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


def outshone(cut: Any) -> Bools:  # noqa: ANN401 a uint8 stamp array
    """Say which stamps have a pixel well away from the centre much brighter than it.

    Returns:
        One flag per stamp.
    """
    out = np.zeros(len(cut), bool)
    c = slice(N * 3 // 8, N * 5 // 8)
    for i, s in enumerate(cut):
        total = np.asarray(s, float).sum(axis=2)
        out[i] = total[c, c].max() < OUTSHONE * total.max()
    return out


def gaia_stars(cfg: Config, name: str, t: pd.DataFrame) -> pd.DataFrame:
    """Return Gaia DR3 sources to G 21 over the field, cached in the run folder.

    Returns:
        Position, G magnitude and star probability, one row per source.

    Raises:
        SystemExit: When Gaia's archive can't be reached and there's no cache.
    """
    cols = ["ra", "dec", "phot_g_mean_mag", "classprob_dsc_combmod_star"]
    if cfg.gaia is False:
        return pd.DataFrame({c: [] for c in cols}, dtype=float)
    if cfg.gaia is not True:
        return pd.read_csv(cfg.gaia, usecols=cols)
    cache = cfg.out / "gaia" / f"{name}.csv"
    if cache.exists():
        return pd.read_csv(cache)
    import pyvo  # noqa: PLC0415 only needed once per field

    box = [float(t.ra.min()) - 0.05, float(t.ra.max()) + 0.05]
    box += [float(t.dec.min()) - 0.05, float(t.dec.max()) + 0.05]
    where = f"ra BETWEEN {box[0]} AND {box[1]} AND dec BETWEEN {box[2]} AND {box[3]}"
    src, fields = "gaiadr3.gaia_source", ", ".join(cols)
    q = f"SELECT {fields} FROM {src} WHERE phot_g_mean_mag < 21 AND {where}"  # noqa: S608 only numbers go in
    try:
        got: pd.DataFrame = (
            pyvo.dal
            .TAPService(GAIA_TAP, session=PatientSession())
            .run_sync(q)
            .to_table()
            .to_pandas()
        )
    except (pyvo.dal.DALAccessError, requests.RequestException) as err:
        msg = f"Couldn't query Gaia for {name} ({err}). Set gaia: false to skip spikes."
        raise SystemExit(msg) from err
    cache.parent.mkdir(parents=True, exist_ok=True)
    got.to_csv(cache, index=False)
    return got


def describe(out: Path, band: Band, filters: tuple[str, ...]) -> None:
    """Write a preview of the field and field.json, which `objects view` shows."""
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
        "preview_scale": len(xs) / w,
        "filters": [f.upper() for f in filters],
        "wcs": cards,
        "pixel_scale_arcsec": scale,
        "header": {"BUNIT": "MJy/sr"},
    }
    (out / "field.json").write_text(json.dumps(meta), encoding="utf-8")


@dataclass
class Measured:
    """One field's segments, measured in every band, and what they were found in."""

    seg: pd.DataFrame
    segmap: npt.NDArray[np.int32]
    bands: dict[str, Band]
    noise: list[str]


def measure(cfg: Config, name: str) -> Measured:
    """Detect every segment in a field and measure it in every band.

    Returns:
        The segments, their map, the bands, and each band's noise factors for the log.
    """
    bands = {b: read_band(cfg.band_path(name, b), cfg.hdu) for b in cfg.bands}
    seg, segmap = detect(bands[cfg.detect])
    blank = np.column_stack(blank_sky(segmap, bands[cfg.detect]))
    seg["cover"] = True
    seg["field"] = name
    noise = [photometry(seg, bands[b], name=b, blank=blank) for b in cfg.bands]
    return Measured(seg, segmap, bands, noise)


def red_only(cfg: Config, name: str, m: Measured) -> pd.DataFrame:
    """Return F444W's segments whose centre isn't on a detection-band segment of S/N 10.

    Their labels and parents get a million added so they never meet the detection
    band's.

    Returns:
        The segments, measured in every band like the detection band's.
    """
    red = m.bands[compact.RED]
    t, redmap = detect(red)
    strong = np.zeros(int(np.max(m.segmap)) + 1, dtype=np.bool_)
    strong[m.seg.label.to_numpy(int)[m.seg.sn.to_numpy(float) >= compact.MIN_SN]] = True
    x, y = m.bands[cfg.detect].pixels(t.ra.to_numpy(float), t.dec.to_numpy(float))
    ny, nx = m.segmap.shape
    ix = np.clip(np.rint(x).astype(int), 0, nx - 1)
    iy = np.clip(np.rint(y).astype(int), 0, ny - 1)
    t = pd.DataFrame(t.loc[~strong[m.segmap[iy, ix]]].reset_index(drop=True))
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
    """Cut, flag and write one field's stamped objects.

    Returns:
        The objects.
    """
    seg, bands = m.seg, m.bands
    big = (seg.area >= MIN_AREA) & (seg.sn > MIN_SN) & (seg.a_arcsec > MIN_A)
    t = seg.loc[big.to_numpy()].reset_index(drop=True)
    t["half"] = np.maximum(MIN_HALF, SIZE * t.a_arcsec)
    cut, ok = stamps(t, [bands[b] for b in cfg.colour])
    t, cut = t.loc[ok].reset_index(drop=True), cut[ok]
    flags(t, bands, cfg=cfg, gaia=gaia_stars(cfg, name, t), lone=lone)
    t["flag_outshone"] = outshone(cut)
    out = cfg.out / "fields" / name
    seg.to_csv(out / "segments.csv", index=False)
    t.to_csv(out / "objects.csv", index=False)
    (out / "noise.json").write_text(json.dumps(m.noise), encoding="utf-8")
    describe(out, bands[cfg.colour[1]], cfg.colour)
    # last, as a field with stamps.npy counts as cut
    with written(out / "stamps.npy") as tmp:
        np.save(tmp, cut)
    return t


def cut_compact(
    cfg: Config, name: str, m: Measured, *, objects: pd.DataFrame, lone: pd.DataFrame
) -> pd.DataFrame:
    """Flag and write one field's compact candidates.

    Returns:
        The candidates.
    """
    small = pd.concat([m.seg, red_only(cfg, name, m)], ignore_index=True)
    small = small.loc[compact.is_candidate(small)].reset_index(drop=True)
    gaia = gaia_stars(cfg, name, objects)
    compact.compact_flags(
        small, m.bands, cfg=cfg, objects=objects, segments=m.seg, gaia=gaia, lone=lone
    )
    with written(cfg.out / "fields" / name / compact.LIST) as tmp:
        small.to_csv(tmp, index=False)
    return small


def _unflagged(t: pd.DataFrame) -> int:
    return int((~t.filter(like="flag_").to_numpy().any(axis=1)).sum())


def cut_field(cfg: Config, name: str, *, redo: bool = False) -> str:
    """Detect, measure, flag and cut one field into the run folder.

    A field already cut keeps its stamps and only gets the compact candidates it
    lacks, so its embeddings stay good.

    Returns:
        A line for the log.
    """
    m = measure(cfg, name)
    out = cfg.out / "fields" / name
    out.mkdir(parents=True, exist_ok=True)
    lone = blobs(m.bands, cfg.bands)
    if redo or not (out / "stamps.npy").exists():
        t = cut_objects(cfg, name, m, lone=lone)
    else:
        t = pd.read_csv(out / "objects.csv")
    said = f"{name}: {len(m.seg)} segments, {len(t)} with stamps"
    said += f", {_unflagged(t)} unflagged"
    if not compact.has_bands(cfg):
        return said
    small = cut_compact(cfg, name, m, objects=t, lone=lone)
    return f"{said}, {len(small)} compact, {_unflagged(small)} unflagged"


def main(cfg: Config, argv: list[str]) -> int:
    """Cut every field that hasn't been cut yet.

    Returns:
        0.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar objects cut CONFIG")
    p.add_argument("--redo", action="store_true", help="cut fields already cut again")
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="fields to cut at once, each needing the memory objects check gives",
    )
    a = p.parse_args(argv)
    want = ["stamps.npy", compact.LIST] if compact.has_bands(cfg) else ["stamps.npy"]
    waiting = [
        name
        for name in cfg.field_names()
        if a.redo or not all((cfg.out / "fields" / name / f).exists() for f in want)
    ]
    if a.workers <= 1:
        for name in waiting:
            print(cut_field(cfg, name, redo=a.redo), flush=True)  # noqa: T201 the command's progress
        return 0
    with ProcessPoolExecutor(a.workers) as pool:
        jobs = [pool.submit(cut_field, cfg, name, redo=a.redo) for name in waiting]
        for job in as_completed(jobs):
            print(job.result(), flush=True)  # noqa: T201 the command's progress
    return 0
