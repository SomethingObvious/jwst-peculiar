"""The compact branch: small sources without a stamp, ranked on how rare they look.

`cut` writes each field's compact candidates to compact.csv. A candidate is
a segment with S/N 10 or more and a semimajor axis of 0.12" or less or an area
under 60 pixels, found in F277W, or in F444W where F277W has no segment of S/N 10
there, as little red dots and brown dwarfs are brightest in F444W. Each gets these
tests as `flag_*` columns.

- `uncovered`: an aperture reaches uncovered sky.
- `edge`: uncovered sky within 1" in any band, where a mosaic's edge has fewer
  exposures and more noise.
- `line`: in some band, a thin straight line running through it from 0.4" to 1.5"
  on both sides, 4 sigma brighter than the sky 0.25" either side of it and at least
  a fifth of its centre, which is a trail, a stripe or a star's spike. A star's own
  spikes are far fainter than its centre, and a galaxy is wider than the gap.
- `one_band`: under 3 sigma in all but one band in the 0.1" aperture, or a one-band
  peak, its best band at S/N 10 or more and every other under max(4, a twentieth).
  A faint red candidate, brightest in F444W and at 2 sigma in F277W with that light
  centred, is seen in both.
- `sharp`: more of its flux in 0.1" than a star has, by 3 sigma in one band or
  combined, which is a cosmic ray or hot pixel.
- `one_pair`: seen in only one of NIRCam's two exposure pairs.
- `dark_middle`: a band under S/N 1 between two at S/N 3 or more.
- `listed_star`: a listed star brighter in F150W than F444W within 0.3", or within
  2" of a star 20 times brighter in F277W, listed or one of the field's segments at
  S/N 100 as concentrated as a star. Cool brown dwarfs are redder, so they stay.
- `blue_star`: a point source in F150W at least a magnitude brighter there than in
  F277W and half a magnitude brighter in F277W than F444W, a star too faint for Gaia.
- `gaia`: within 0.3" of a Gaia star's spikes or halo.
- `piece`: a deblended piece of a stamped object's parent, inside its stamp.
- `pixel`: a lone peak by `flags.blobs` within 0.25" plus 1.5 of that band's
  pixels (beyond 0.1" for an F444W peak on a faint red candidate), or a pixel 5
  sigma under the sky as near in F277W and F444W and within 0.1" plus 1.5 pixels in
  F115W and F150W, as only the 0.1" flux of those is ranked.

`compact` fits a 20-component Gaussian mixture to the unflagged candidates'
three colours and two concentrations, and ranks them on minus the log density with
each candidate's own errors added to every component, averaged over three fits with
different seeds. Two single fits share about 84 of their top 100 on COSMOS-Web, and
two averages of three share 92.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any
import warnings

from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D
import astropy.units as u
import numpy as np
import pandas as pd
from photutils.aperture import CircularAperture, aperture_photometry
from PIL import Image
from scipy.spatial import cKDTree
from scipy.special import logsumexp
from sklearn.mixture import GaussianMixture

from jwst_peculiar.config import sky_wcs, to_mjy_sr
from jwst_peculiar.flags import (
    DETECTED,
    MIN_BANDS,
    known_stars,
    mid_dec,
    on_spikes,
    sky_xy,
)

if TYPE_CHECKING:
    import numpy.typing as npt

    from jwst_peculiar.config import Config, FitsHDUList
    from jwst_peculiar.cut import Band

    Floats = npt.NDArray[np.float64]
    Bools = npt.NDArray[np.bool_]

BANDS = ("f115w", "f150w", "f277w", "f444w")
RED, OFFSET = "f444w", 1_000_000
MIN_SN, MAX_A, MAX_AREA = 10.0, 0.12, 60
PEAK_SN, PEAK_FLOOR, PEAK_SHARE = 10.0, 4.0, 20.0
NEXT, RED_SEEN, RED_CENTRED = "f277w", 2.0, 0.3
# F150W's 0.1" over 0.25" flux for a point source, and the flux ratios F150W over
# F277W and F277W over F444W a star is brighter than (1 and 0.5 magnitudes)
STAR_POINT, STAR_FALL = (0.75, 1.2), (10**0.4, 10**0.2)
# 99th percentile of 0.1" over 0.25" flux for 742 Gaia stars, G 19 to 21, S/N over 30
PSF_SHARPEST = {"f115w": 0.88, "f150w": 0.865, "f277w": 0.70, "f444w": 0.61}
SHARP_SIG, SHARP_FLOOR = 3.0, 5.0
# NIRCam takes F115W with F277W, then F150W with F444W, at the same dither
PAIRS = (("f115w", "f277w"), ("f150w", "f444w"))
SEEN, UNSEEN = 5.0, 2.0
DARK_SEEN, DARK = 3.0, 1.0
POINT, HALO_REACH, HALO_RATIO = 0.3, 2.0, 20.0
# 97% of COSMOS-Web's 911 listed stars over S/N 100 pass, and 213 unlisted sources
POINT_SN, POINT_F150W, POINT_F277W = 100.0, 0.75, (0.5, 0.8)
NEG_SIG, APERTURE, BLOCK = 5.0, 0.25, 1.5
# the bands whose 0.25" flux the ranking reads, and the aperture every band has
CONCENTRATED, INNER = ("f277w", "f444w"), 0.1
EDGE = 1.0
LINE_IN, LINE_OUT, LINE_SIDE = 0.4, 1.5, 0.25
# a median of n pixels has 1.2533 times the noise of their mean
LINE_SIG, LINE_SHARE, MEDIAN_NOISE = 4.0, 0.2, 1.2533
COMPONENTS, FITS = 20, 3
LIST, STAMPS = "compact.csv", "compact_stamps.npy"
TILE, CUTOUT = 112, 2.4


def has_bands(cfg: Config) -> bool:
    """Return whether the run has every band the compact tests are written for."""
    return all(b in cfg.bands for b in BANDS)


def is_candidate(t: pd.DataFrame) -> Bools:
    """Return which segments are small and bright enough to be compact candidates."""
    a, area = t.a_arcsec.to_numpy(float), t.area.to_numpy(float)
    return ((a <= MAX_A) | (area < MAX_AREA)) & (t.sn.to_numpy(float) >= MIN_SN)


def _snr(t: pd.DataFrame, band: str) -> Floats:
    f, e = t[f"f_{band}_10"].to_numpy(float), t[f"e_{band}_10"].to_numpy(float)
    return np.asarray(f / e, float)


def blue_star(t: pd.DataFrame) -> Bools:
    """Return which candidates are point sources with a star's falling spectrum.

    A star too faint for Gaia is a magnitude or more brighter in F150W than F277W
    and brighter again in F277W than F444W. Brown dwarfs and little red dots are
    the other way round in F277W and F444W. Of 1268 labelled COSMOS-Web candidates
    it caught 8 stars and 2 ordinary objects and nothing worth a look.

    Returns:
        One flag per candidate.
    """
    f150, f277, f444 = (t[f"f_{b}_10"].to_numpy(float) for b in BANDS[1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        point = f150 / t.f_f150w_25.to_numpy(float)
        early, late = f150 / f277, f277 / f444
    return np.asarray(
        (point >= STAR_POINT[0])
        & (point <= STAR_POINT[1])
        & (early > STAR_FALL[0])
        & (late > STAR_FALL[1]),
        bool,
    )


def faint_red(t: pd.DataFrame) -> Bools:
    """Return which candidates are brightest in F444W and seen at 2 sigma in F277W.

    The F277W light has to be centred, at least 0.3 of the 0.25" flux inside 0.1",
    as an F444W hot pixel on a galaxy's outskirts gets its 2 sigma from the galaxy.
    Known little red dots have 0.43 or more and those hot pixels 0.09 to 0.22.

    Returns:
        One flag per candidate.
    """
    z = np.column_stack([_snr(t, b) for b in BANDS])
    top = z.argmax(axis=1) == BANDS.index(RED)
    inner, outer = t[f"f_{NEXT}_10"].to_numpy(float), t[f"f_{NEXT}_25"].to_numpy(float)
    centred = inner >= RED_CENTRED * outer
    return np.asarray(top & (z[:, BANDS.index(NEXT)] >= RED_SEEN) & centred, bool)


def one_band(t: pd.DataFrame) -> Bools:
    """Return which candidates show in only one band, or are a one-band peak.

    Where F444W is the brightest band, F277W at 2 sigma counts as seen, as a little
    red dot can be 5 times fainter there. 56 of the 66 known ones found only in F444W
    pass that way, and a lone F444W pixel is that bright in F277W 2% of the time.

    Returns:
        One flag per candidate.
    """
    z = np.column_stack([_snr(t, b) for b in BANDS])
    red = faint_red(t)
    top, rest = z.max(axis=1), np.sort(z, axis=1)[:, -2]
    peak = (top >= PEAK_SN) & (rest < np.maximum(PEAK_FLOOR, top / PEAK_SHARE)) & ~red
    seen = (z >= DETECTED).sum(axis=1) + (red & (z[:, BANDS.index(NEXT)] < DETECTED))
    return np.asarray((seen < MIN_BANDS) | peak, bool)


def _sharpness(t: pd.DataFrame, b: str, top: float) -> tuple[Floats, Bools]:
    # sigma the 0.1" over 0.25" flux ratio is over a star's, and where that counts
    f1, f2 = t[f"f_{b}_10"].to_numpy(float), t[f"f_{b}_25"].to_numpy(float)
    e1, e2 = t[f"e_{b}_10"].to_numpy(float), t[f"e_{b}_25"].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        # the inner aperture and the ring around it have independent noise
        ring, e_ring = f2 - f1, np.sqrt(np.maximum(e2**2 - e1**2, 0))
        err = np.sqrt(ring**2 * e1**2 + f1**2 * e_ring**2) / f2**2
        ok = (f1 / e1 >= SHARP_FLOOR) & (f2 > 0) & (err > 0)
        return np.where(ok, (f1 / f2 - top) / err, 0.0), ok


def sharp(t: pd.DataFrame) -> Bools:
    """Return which candidates have more of their flux in 0.1" than any star does."""
    got = [_sharpness(t, b, top) for b, top in PSF_SHARPEST.items()]
    z = np.array([g[0] for g in got])
    n = np.array([g[1] for g in got]).sum(axis=0)
    combined = z.sum(axis=0) / np.sqrt(np.maximum(n, 1))
    return np.asarray((z.max(axis=0) > SHARP_SIG) | (combined > SHARP_SIG), bool)


def one_pair(t: pd.DataFrame) -> Bools:
    """Return which candidates show in one exposure pair and not at all in the other."""
    z = {b: _snr(t, b) for b in BANDS}
    (a1, a2), (b1, b2) = PAIRS
    first = (np.minimum(z[a1], z[a2]) >= SEEN) & (np.maximum(z[b1], z[b2]) < UNSEEN)
    second = (np.minimum(z[b1], z[b2]) >= SEEN) & (np.maximum(z[a1], z[a2]) < UNSEEN)
    return first | second


def dark_middle(t: pd.DataFrame) -> Bools:
    """Return which candidates have a dark band between two bright ones.

    No smooth spectrum is under S/N 1 in one band and at S/N 3 on both sides of it.

    Returns:
        One flag per candidate.
    """
    z = np.array([_snr(t, b) for b in BANDS])
    out = np.zeros(len(t), bool)
    for i in range(1, len(BANDS) - 1):
        bluer, redder = z[:i].max(axis=0), z[i + 1 :].max(axis=0)
        out |= (z[i] < DARK) & (bluer >= DARK_SEEN) & (redder >= DARK_SEEN)
    return out


def point_sources(segments: pd.DataFrame) -> pd.DataFrame:
    """Return the bright segments as concentrated as a star in F150W and F277W."""
    with np.errstate(divide="ignore", invalid="ignore"):
        c150 = segments.f_f150w_10.to_numpy(float) / segments.f_f150w_25.to_numpy(float)
        c277 = segments.f_f277w_10.to_numpy(float) / segments.f_f277w_25.to_numpy(float)
    keep = (segments.sn.to_numpy(float) >= POINT_SN) & (c150 >= POINT_F150W)
    keep &= (c150 <= 1) & (c277 >= POINT_F277W[0]) & (c277 <= POINT_F277W[1])
    return pd.DataFrame(segments.loc[keep, ["ra", "dec"]])


def listed_star(t: pd.DataFrame, stars: pd.DataFrame, segments: pd.DataFrame) -> Bools:
    """Return which candidates are a blue listed star or in a much brighter star's halo.

    A star's F277W flux is that of the segment within 0.3" of it. Many stars bright
    in the infrared are fainter than Gaia's G 21, so the list catches what Gaia misses,
    and the field's own bright point sources count as stars for the halo too.

    Returns:
        One flag per candidate.
    """
    out = np.zeros(len(t), bool)
    if not len(t):
        return out
    dec0 = mid_dec(t)
    txy = sky_xy(t.ra, t.dec, dec0)
    if len(stars):
        d, _ = cKDTree(sky_xy(stars.ra, stars.dec, dec0)).query(txy)
        blue = t.f_f150w_25.to_numpy(float) > t.f_f444w_25.to_numpy(float)
        out |= (d < POINT) & blue
    halo = pd.concat([stars[["ra", "dec"]], point_sources(segments)])
    if not len(halo) or not len(segments):
        return out
    sxy = sky_xy(halo.ra, halo.dec, dec0)
    dseg, j = cKDTree(sky_xy(segments.ra, segments.dec, dec0)).query(sxy)
    flux = np.where(dseg < POINT, segments.f_f277w_25.to_numpy(float)[j], np.nan)
    mine = np.maximum(t.f_f277w_25.to_numpy(float), 0)
    for i, near in enumerate(cKDTree(sxy).query_ball_point(txy, HALO_REACH)):
        out[i] |= bool(np.any(flux[near] > HALO_RATIO * mine[i]))
    return out


def piece(t: pd.DataFrame, objects: pd.DataFrame) -> Bools:
    """Return which candidates are a piece of a stamped object's parent in its stamp."""
    out = np.zeros(len(t), bool)
    if not len(objects) or not len(t):
        return out
    dec0 = mid_dec(t)
    txy = sky_xy(t.ra, t.dec, dec0)
    groups = objects.groupby("parent").indices
    oxy, half = sky_xy(objects.ra, objects.dec, dec0), objects.half.to_numpy(float)
    for i, parent in enumerate(t.parent.to_numpy(int)):
        if parent in groups:
            k = groups[parent]
            d = np.abs(oxy[k] - txy[i])
            out[i] = bool(((d[:, 0] < half[k]) & (d[:, 1] < half[k])).any())
    return out


def _within(
    t: pd.DataFrame, ra: Floats, dec: Floats, *, r: float, past: float = -1.0
) -> Bools:
    # whether any of the points lies further than `past` and within `r` of each row
    out = np.zeros(len(t), bool)
    if not len(ra) or not len(t):
        return out
    dec0 = mid_dec(t)
    pts, rows = sky_xy(ra, dec, dec0), sky_xy(t.ra, t.dec, dec0)
    for i, near in enumerate(cKDTree(pts).query_ball_point(rows, r)):
        if near:
            out[i] = bool((np.hypot(*(pts[near] - rows[i]).T) > past).any())
    return out


def bad_pixel(t: pd.DataFrame, bands: dict[str, Band], lone: pd.DataFrame) -> Bools:
    """Return which candidates have a pixel 5 sigma under the sky or a lone peak.

    A pixel counts when it lies within 0.25" plus 1.5 of its band's pixels, since a
    hot pixel just outside 0.25" still leaks into the 0.063" long-wave aperture. A
    pixel under the sky in F115W or F150W counts within 0.1", the only aperture of
    theirs the ranking reads, which kept 6 more of 158 known little red dots. A lone
    F444W peak within 0.1" of a faint red candidate is its own centre, which was the
    only reason 31 known little red dots were flagged.

    Returns:
        One flag per candidate.
    """
    out = np.zeros(len(t), bool)
    red = faint_red(t)
    for b in BANDS:
        band = bands[b]
        reach = APERTURE + BLOCK * band.scale
        near = (APERTURE if b in CONCENTRATED else INNER) + BLOCK * band.scale
        yy, xx = np.nonzero(np.nan_to_num(band.data) < -NEG_SIG * band.rms)
        sky = band.wcs.pixel_to_world(xx, yy)
        ra, dec = np.asarray(sky.ra.deg, float), np.asarray(sky.dec.deg, float)
        out |= _within(t, ra, dec, r=near)
        mine = lone.loc[lone.band == b]
        ra, dec = mine.ra.to_numpy(float), mine.dec.to_numpy(float)
        hit = _within(t, ra, dec, r=reach)
        if b == RED:
            # a faint red source's own peak shows in F444W alone, which is its colour
            hit &= ~red | _within(t, ra, dec, r=reach, past=INNER)
        out |= hit
    return out


def near_edge(t: pd.DataFrame, bands: dict[str, Band]) -> Bools:
    """Return which candidates have uncovered sky within 1" in any band.

    Uncovered means NaN, exactly 0 or off the end of the mosaic's array.

    Returns:
        One flag per candidate.
    """
    out = np.zeros(len(t), bool)
    for b in BANDS:
        band = bands[b]
        off = (~np.isfinite(band.raw) | (band.raw == 0)).astype(np.float32)
        xy = np.column_stack(band.pixels(t.ra.to_numpy(float), t.dec.to_numpy(float)))
        r = EDGE / band.scale
        ny, nx = off.shape
        inside = (xy - r >= 0).all(axis=1) & (xy[:, 0] + r < nx) & (xy[:, 1] + r < ny)
        hit = aperture_photometry(off, CircularAperture(xy, r))["aperture_sum"]
        out |= ~inside | ~(np.asarray(hit, float) == 0)
    return out


def _medians(band: Band, x: Floats, y: Floats) -> Floats:
    # the median along each row of sample points, NaN off the image
    ny, nx = band.data.shape
    ix, iy = np.rint(x).astype(int), np.rint(y).astype(int)
    inside = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    v = band.data[np.clip(iy, 0, ny - 1), np.clip(ix, 0, nx - 1)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.asarray(np.nanmedian(np.where(inside, v, np.nan), axis=-1), float)


def _brightest_line(band: Band, x: Floats, y: Floats, *, r: Floats) -> Floats:
    # each candidate's brightest line through it, less the brighter of its two sides
    th = np.arange(0, np.pi, 1 / r[-1])[:, None]
    rx, ry = np.cos(th) * r, np.sin(th) * r
    sx, sy = -np.sin(th) * LINE_SIDE / band.scale, np.cos(th) * LINE_SIDE / band.scale
    xb, yb = x[:, None, None], y[:, None, None]
    arms = []
    for sign in (1, -1):
        mid = _medians(band, xb + sign * rx, yb + sign * ry)
        left = _medians(band, xb + sign * rx + sx, yb + sign * ry + sy)
        right = _medians(band, xb + sign * rx - sx, yb + sign * ry - sy)
        arms.append(mid - np.fmax(left, right))
    line = np.fmin(*arms)
    return np.asarray(np.max(np.where(np.isfinite(line), line, -np.inf), axis=1), float)


def _on_line(band: Band, x: Floats, y: Floats) -> Bools:
    # arms sampled 1 pixel apart, and angles 1 pixel apart at their end
    r = np.arange(LINE_IN / band.scale, LINE_OUT / band.scale)
    best = _brightest_line(band, x, y, r=r)
    ny, nx = band.data.shape
    cx = np.clip(np.rint(x).astype(int), 0, nx - 1)
    cy = np.clip(np.rint(y).astype(int), 0, ny - 1)
    rms = band.rms[cy, cx]
    noise = MEDIAN_NOISE * np.sqrt(2) * rms / np.sqrt(len(r))
    # NaN off the image, so a band that misses the candidate never flags it
    centre = np.maximum(band.data[cy, cx], rms)
    return np.asarray((best > LINE_SIG * noise) & (best > LINE_SHARE * centre), bool)


def on_line(t: pd.DataFrame, bands: dict[str, Band], batch: int = 200) -> Bools:
    """Return which candidates sit on a thin straight line in any band."""
    out = np.zeros(len(t), bool)
    ra, dec = t.ra.to_numpy(float), t.dec.to_numpy(float)
    for band in bands.values():
        x, y = band.pixels(ra, dec)
        for s in range(0, len(t), batch):
            out[s : s + batch] |= _on_line(band, x[s : s + batch], y[s : s + batch])
    return out


def compact_flags(  # noqa: PLR0913 every test needs its own input
    t: pd.DataFrame,
    bands: dict[str, Band],
    *,
    cfg: Config,
    objects: pd.DataFrame,
    segments: pd.DataFrame,
    gaia: pd.DataFrame,
    lone: pd.DataFrame,
) -> None:
    """Add every compact test to `t` as a `flag_*` column."""
    t["flag_uncovered"] = ~t.cover.to_numpy(bool)
    t["flag_edge"] = near_edge(t, bands)
    t["flag_line"] = on_line(t, bands)
    t["flag_one_band"] = one_band(t)
    t["flag_blue_star"] = blue_star(t)
    t["flag_sharp"] = sharp(t)
    t["flag_one_pair"] = one_pair(t)
    t["flag_dark_middle"] = dark_middle(t)
    t["flag_listed_star"] = listed_star(t, known_stars(cfg), segments)
    roll = bands[cfg.detect].roll
    t["flag_gaia"] = on_spikes(t.assign(half=POINT), gaia, roll=roll, square=False)
    t["flag_piece"] = piece(t, objects)
    t["flag_pixel"] = bad_pixel(t, bands, lone)


def _mags(t: pd.DataFrame, ap: str) -> tuple[Floats, Floats]:
    # asinh magnitudes and their errors, which stay finite for faint and negative fluxes
    f = t[[f"f_{b}_{ap}" for b in BANDS]].to_numpy(float)
    e = t[[f"e_{b}_{ap}" for b in BANDS]].to_numpy(float)
    m = -1.0857 * (np.arcsinh(f / (2 * e)) + np.log(e))
    return m, 1.0857 * e / np.sqrt(f**2 + 4 * e**2)


def features(t: pd.DataFrame) -> tuple[Floats, Floats]:
    """Return the three 0.1" colours and two long-wave concentrations, with errors."""
    m1, s1 = _mags(t, "10")
    m2, s2 = _mags(t, "25")
    x = np.c_[m1[:, :-1] - m1[:, 1:], m1[:, 2:] - m2[:, 2:]]
    xe = np.c_[np.hypot(s1[:, :-1], s1[:, 1:]), np.hypot(s1[:, 2:], s2[:, 2:])]
    return x, xe


def fit(x: Floats, seed: int = 0) -> Any:  # noqa: ANN401 scikit-learn has no types
    """Return the mixture the candidates are ranked under, fitted to `x`."""
    # fewer components for a run of fewer than 200 candidates, at least 10 to each
    n = min(COMPONENTS, max(1, len(x) // 10))
    mix = GaussianMixture(n, covariance_type="full", random_state=seed, max_iter=500)
    return mix.fit(x)


def rarity(gmm: Any, x: Floats, xe: Floats) -> Floats:  # noqa: ANN401 scikit-learn has no types
    """Return minus the log density of each row, its errors added to every component.

    There's no normalising term, as with it a bigger error makes the density lower
    and so a noisier source rarer.

    Returns:
        One score per row, higher is rarer.
    """
    means = np.asarray(gmm.means_, float)
    covs = np.asarray(gmm.covariances_, float)
    weights = np.asarray(gmm.weights_, float)
    own = np.einsum("ni,ij->nij", xe**2, np.eye(x.shape[1]))
    ll = np.empty((len(x), len(weights)))
    for k in range(len(weights)):
        d = x - means[k]
        sol = np.linalg.solve(covs[k][None] + own, d[..., None])[..., 0]
        ll[:, k] = np.log(weights[k]) - 0.5 * np.einsum("ni,ni->n", d, sol)
    return np.asarray(-logsumexp(ll, axis=1), float)


def _stretch(a: Floats, scale: float) -> Floats:
    return np.asarray(np.clip(np.arcsinh(10 * a / scale) / np.arcsinh(10), 0, 1), float)


def _panel(hdu: tuple[Any, Any, float], at: Any) -> Floats:  # noqa: ANN401 astropy has no types
    data, wcs, factor = hdu
    cut = Cutout2D(data, at, CUTOUT * u.arcsec, wcs=wcs, mode="partial", fill_value=0)
    a = np.nan_to_num(np.asarray(cut.data, np.float32))
    if factor != 1:
        a *= np.float32(factor)
    small = Image.fromarray(a - np.median(a))
    return np.asarray(small.resize((TILE, TILE), Image.Resampling.BILINEAR), float)


def tile(
    hdus: dict[str, tuple[Any, Any, float]], ra: float, dec: float
) -> npt.NDArray[np.uint8]:
    """Cut a 2.4" four-panel tile, its first row lowest like the stamps.

    The F444W, F277W, F150W colour panel and F115W sit on top, F277W and F444W
    below, every panel scaled to its brightest pixel within 0.2" of the centre, as a
    faint compact source vanishes under a percentile stretch.

    Returns:
        A (224, 224, 3) uint8 tile.
    """
    at = SkyCoord(ra * u.deg, dec * u.deg)
    img = {b: _panel(hdus[b], at) for b in BANDS}
    mid = slice(TILE // 2 - TILE // 12, TILE // 2 + TILE // 12)
    peak = {b: max(float(np.max(v[mid, mid])), 1e-6) for b, v in img.items()}
    top = max(peak["f150w"], peak["f277w"], peak["f444w"])
    rgb = _stretch(np.stack([img["f444w"], img["f277w"], img["f150w"]], -1), top)

    def grey(b: str) -> Floats:
        return np.repeat(_stretch(img[b], peak[b])[..., None], 3, -1)

    below = np.hstack([grey("f277w"), grey("f444w")])
    out = np.vstack([below, np.hstack([rgb, grey("f115w")])])
    return np.asarray(np.rint(255 * out), np.uint8)


def load(cfg: Config) -> pd.DataFrame:
    """Read every field's compact candidates.

    Returns:
        The candidates, each with an `id` of field and segment label.

    Raises:
        SystemExit: When a field has no compact.csv.
    """
    cats = []
    for name in cfg.cut_fields():
        path = cfg.out / "fields" / name / LIST
        if not path.exists():
            msg = f"{name} has no {LIST}. Run jwst-peculiar cut first."
            raise SystemExit(msg)
        cat = pd.read_csv(path)
        # an empty one reads back untyped, as in rank.load
        if len(cat):
            cats.append(cat)
    if not cats:
        msg = "No field has any compact candidates."
        raise SystemExit(msg)
    t = pd.concat(cats, ignore_index=True)
    t["id"] = t.field + ":" + t.label.astype(str)
    return t


def stamps(cfg: Config, top: pd.DataFrame) -> npt.NDArray[np.uint8]:
    """Cut a tile for every listed candidate, a field at a time.

    Returns:
        (n, 224, 224, 3) uint8 tiles in list order.
    """
    out = np.zeros((len(top), 2 * TILE, 2 * TILE, 3), np.uint8)
    for name, rows in top.groupby("field").indices.items():
        opened: list[FitsHDUList] = [
            fits.open(cfg.band_path(str(name), b), memmap=True) for b in BANDS
        ]
        try:
            hdus = {
                b: (
                    h[cfg.hdu].data,
                    sky_wcs(h[cfg.hdu].header),
                    to_mjy_sr(h[cfg.hdu].header) or 1.0,
                )
                for b, h in zip(BANDS, opened, strict=True)
            }
            for i in rows:
                out[i] = tile(hdus, float(top.ra.iloc[i]), float(top.dec.iloc[i]))
        finally:
            for h in opened:
                h.close()
    return out


def ranked(t: pd.DataFrame, n: int) -> pd.DataFrame:
    """Return the `n` rarest unflagged candidates, rarest first.

    Returns:
        Their ids, places, scores and tile numbers.
    """
    flagged = t.filter(like="flag_").to_numpy().any(axis=1)
    keep = t.loc[~flagged].reset_index(drop=True)
    x, xe = features(keep)
    keep["score"] = np.mean([rarity(fit(x, s), x, xe) for s in range(FITS)], axis=0)
    cols = ["id", "field", "label", "ra", "dec", "sn", "a_arcsec", "score"]
    best = keep.sort_values("score", ascending=False).head(n)[cols]
    k = len(best)
    return best.reset_index(drop=True).assign(
        half=CUTOUT / 2, rank=range(1, k + 1), tile=range(k)
    )


def main(cfg: Config, argv: list[str]) -> int:
    """Rank the unflagged compact candidates and cut tiles for the top of the list.

    Returns:
        0.

    Raises:
        SystemExit: When the run lacks one of the four bands.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar compact CONFIG")
    p.add_argument("-n", type=int, default=500, help="how many candidates to list")
    a = p.parse_args(argv)
    if not has_bands(cfg):
        msg = f"The compact branch needs {', '.join(BANDS)} in the config's bands."
        raise SystemExit(msg)
    t = load(cfg)
    top = ranked(t, a.n)
    np.save(cfg.out / STAMPS, stamps(cfg, top))
    top.to_csv(cfg.out / LIST, index=False)
    print(f"{len(t)} compact candidates, list in {cfg.out / LIST}")  # noqa: T201 the command's result
    return 0
