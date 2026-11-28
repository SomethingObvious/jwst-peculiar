"""The compact branch: sources too small for a stamp, ranked on how rare they look.

`cut` writes each field's candidates and their `flag_*` tests to compact.csv, and
`compact` ranks the unflagged ones under a Gaussian mixture averaged over three seeds,
then drops what the config's artefact models call artefacts.
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
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.spatial import cKDTree
from scipy.special import logsumexp
from sklearn.mixture import GaussianMixture

from jwst_peculiar.config import Settings, sky_wcs, to_mjy_sr
from jwst_peculiar.embed import artefact_chances, resolve_device
from jwst_peculiar.flags import (
    DETECTED,
    MIN_BANDS,
    known_stars,
    mid_sky,
    on_spikes,
    sky_xy,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import numpy.typing as npt

    from jwst_peculiar.config import Config, FitsHDUList
    from jwst_peculiar.cut import Band

    Floats = npt.NDArray[np.float64]
    Bools = npt.NDArray[np.bool_]

DEFAULT = Settings()
# the reddest band's own detections get labels past this, clear of the detection band's
OFFSET = 1_000_000
# one_band, and the faint red candidate it lets through
PEAK_SN, PEAK_FLOOR, PEAK_SHARE = 10.0, 4.0, 20.0
RED_SEEN, RED_CENTRED = 2.0, 0.3
# blue_star: the second band's 0.1" over 0.25" flux for a point source, and the flux
# ratios of the second band to the third and the third to the fourth a star beats,
# 0.5 and 0.3 magnitudes, which 75% of COSMOS-Web's listed stars beat and no brown
# dwarf does
STAR_POINT, STAR_FALL = (0.75, 1.2), (10**0.2, 10**0.12)
SHARP_SIG, SHARP_FLOOR = 3.0, 5.0
# sharp also: 0.05" over 0.1" flux in both long bands, where stars and little red dots
# keep to 0.22 to 0.50, a pixel-sharp dot has 0.67 or more and a point between a moving
# source's dots under 0.15
CORE_LOW, CORE_TOP = 0.15, 0.6
SEEN, UNSEEN = 5.0, 2.0
DARK_SEEN, DARK = 3.0, 1.0
# listed_star, and the 0.3" gaia uses too
POINT, HALO_REACH, HALO_RATIO = 0.3, 2.0, 20.0
# a segment concentrated like a star, as 97% of COSMOS-Web's bright listed stars are
POINT_SN, POINT_F150W, POINT_F277W = 100.0, 0.75, (0.5, 0.8)
NEG_SIG, APERTURE, BLOCK = 5.0, 0.25, 1.5
# the aperture every band has
INNER = 0.1
# line, where a median of n pixels has 1.2533 times the noise of their mean
LINE_IN, LINE_OUT, LINE_SIDE = 0.4, 1.5, 0.25
LINE_SIG, LINE_SHARE, MEDIAN_NOISE = 4.0, 0.2, 1.2533
# row: 4 sigma peaks 0.2" to 1.2" out, evenly spaced to 0.1" as peaks sit on pixels
ROW_SIG, ROW_SAME, ROW_IN, ROW_OUT = 4.0, 0.12, 0.2, 1.2
ROW_LIKE, ROW_TOL = 3.0, 0.1
COMPONENTS, FITS = 20, 3
LIST, STAMPS = "compact.csv", "compact_stamps.npy"
# each listed candidate's stamp, 2.4" across at twice 112 pixels
TILE, CUTOUT = 112, 2.4
# candidates the artefact models score for each one listed, as on the six surveys they
# were judged on they dropped up to 70% of the top 100 and the 100th kept sat at 345
DEPTH = 4
# an snr tile's panels run from 0 to 100 sigma
SNR_TOP = 100.0


def has_bands(cfg: Config) -> bool:
    """Return whether the run has every band the compact tests are written for."""
    return all(b in cfg.bands for b in cfg.settings.compact_bands)


def is_candidate(t: pd.DataFrame, *, s: Settings = DEFAULT) -> Bools:
    """Return which segments are small and bright enough to be compact candidates."""
    a, area = t.a_arcsec.to_numpy(float), t.area.to_numpy(float)
    small = (a <= s.compact_a) | (area < s.compact_area)
    return small & (t.sn.to_numpy(float) >= s.compact_sn)


def _snr(t: pd.DataFrame, band: str) -> Floats:
    f, e = t[f"f_{band}_10"].to_numpy(float), t[f"e_{band}_10"].to_numpy(float)
    return np.asarray(f / e, float)


def blue_star(t: pd.DataFrame, *, s: Settings = DEFAULT) -> Bools:
    """Return which candidates are point sources with a star's falling spectrum."""
    b = s.compact_bands
    f150, f277, f444 = (t[f"f_{band}_10"].to_numpy(float) for band in b[1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        point = f150 / t[f"f_{b[1]}_25"].to_numpy(float)
        early, late = f150 / f277, f277 / f444
    return np.asarray(
        (point >= STAR_POINT[0])
        & (point <= STAR_POINT[1])
        & (early > STAR_FALL[0])
        & (late > STAR_FALL[1]),
        bool,
    )


def faint_red(t: pd.DataFrame, *, s: Settings = DEFAULT) -> Bools:
    """Return which candidates peak in the reddest band and show in the next."""
    z = np.column_stack([_snr(t, b) for b in s.compact_bands])
    nxt = s.compact_bands[2]
    inner, outer = t[f"f_{nxt}_10"].to_numpy(float), t[f"f_{nxt}_25"].to_numpy(float)
    centred = inner >= RED_CENTRED * outer
    return np.asarray(
        (z.argmax(axis=1) == len(s.compact_bands) - 1)
        & (z[:, 2] >= RED_SEEN)
        & centred,
        bool,
    )


def one_band(t: pd.DataFrame, *, s: Settings = DEFAULT) -> Bools:
    """Return which candidates show in only one band, or are a one-band peak."""
    z = np.column_stack([_snr(t, b) for b in s.compact_bands])
    red = faint_red(t, s=s)
    top, rest = z.max(axis=1), np.sort(z, axis=1)[:, -2]
    peak = (top >= PEAK_SN) & (rest < np.maximum(PEAK_FLOOR, top / PEAK_SHARE)) & ~red
    seen = (z >= DETECTED).sum(axis=1) + (red & (z[:, 2] < DETECTED))
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


def sharp(t: pd.DataFrame, *, s: Settings = DEFAULT) -> Bools:
    """Return which candidates have their light gathered unlike any star's."""
    pairs = zip(s.compact_bands, s.psf_sharpest, strict=True)
    got = [_sharpness(t, b, top) for b, top in pairs]
    z = np.array([g[0] for g in got])
    n = np.array([g[1] for g in got]).sum(axis=0)
    combined = z.sum(axis=0) / np.sqrt(np.maximum(n, 1))
    high, low = np.ones(len(t), bool), np.ones(len(t), bool)
    for b in s.compact_bands[2:]:
        f05, f10 = t[f"f_{b}_05"].to_numpy(float), t[f"f_{b}_10"].to_numpy(float)
        seen = _snr(t, b) >= SHARP_FLOOR
        high &= seen & (f05 > CORE_TOP * f10)
        low &= seen & (f05 < CORE_LOW * f10)
    flux = (z.max(axis=0) > SHARP_SIG) | (combined > SHARP_SIG)
    return np.asarray(flux | high | low, bool)


def one_pair(t: pd.DataFrame, *, s: Settings = DEFAULT) -> Bools:
    """Return which candidates show in one exposure pair and not at all in the other."""
    z = {b: _snr(t, b) for b in s.compact_bands}
    a1, b1, a2, b2 = s.compact_bands
    first = (np.minimum(z[a1], z[a2]) >= SEEN) & (np.maximum(z[b1], z[b2]) < UNSEEN)
    second = (np.minimum(z[b1], z[b2]) >= SEEN) & (np.maximum(z[a1], z[a2]) < UNSEEN)
    return first | second


def dark_middle(t: pd.DataFrame, *, s: Settings = DEFAULT) -> Bools:
    """Return which candidates have a dark band between two bright ones."""
    z = np.array([_snr(t, b) for b in s.compact_bands])
    out = np.zeros(len(t), bool)
    for i in range(1, len(z) - 1):
        bluer, redder = z[:i].max(axis=0), z[i + 1 :].max(axis=0)
        out |= (z[i] < DARK) & (bluer >= DARK_SEEN) & (redder >= DARK_SEEN)
    return out


def point_sources(segments: pd.DataFrame, *, s: Settings = DEFAULT) -> pd.DataFrame:
    """Return the bright segments as concentrated as a star in the middle two bands."""
    c150, c277 = (
        np.asarray(segments[f"f_{b}_10"] / segments[f"f_{b}_25"], float)
        for b in s.compact_bands[1:3]
    )
    keep = (segments.sn.to_numpy(float) >= POINT_SN) & (c150 >= POINT_F150W)
    keep &= (c150 <= 1) & (c277 >= POINT_F277W[0]) & (c277 <= POINT_F277W[1])
    return pd.DataFrame(segments.loc[keep, ["ra", "dec"]])


def listed_star(
    t: pd.DataFrame,
    stars: pd.DataFrame,
    segments: pd.DataFrame,
    *,
    s: Settings = DEFAULT,
) -> Bools:
    """Return which candidates are a blue listed star or in a bright star's halo."""
    b = s.compact_bands
    out = np.zeros(len(t), bool)
    if not len(t):
        return out
    mid = mid_sky(t)
    txy = sky_xy(t.ra, t.dec, mid)
    if len(stars):
        d = cKDTree(sky_xy(stars.ra, stars.dec, mid)).query(txy)[0]
        blue = t[f"f_{b[1]}_25"].to_numpy(float) > t[f"f_{b[3]}_25"].to_numpy(float)
        out |= (d < POINT) & blue
    halo = pd.concat([stars[["ra", "dec"]], point_sources(segments, s=s)])
    if not len(halo) or not len(segments):
        return out
    sxy = sky_xy(halo.ra, halo.dec, mid)
    dseg, j = cKDTree(sky_xy(segments.ra, segments.dec, mid)).query(sxy)
    flux = np.where(dseg < POINT, segments[f"f_{b[2]}_25"].to_numpy(float)[j], np.nan)
    mine = np.maximum(t[f"f_{b[2]}_25"].to_numpy(float), 0)
    for i, near in enumerate(cKDTree(sxy).query_ball_point(txy, HALO_REACH)):
        out[i] |= bool(np.any(flux[near] > HALO_RATIO * mine[i]))
    return out


def piece(t: pd.DataFrame, objects: pd.DataFrame) -> Bools:
    """Return which candidates are a piece of a stamped object's parent in its stamp."""
    out = np.zeros(len(t), bool)
    if not len(objects) or not len(t):
        return out
    mid = mid_sky(t)
    txy = sky_xy(t.ra, t.dec, mid)
    groups = objects.groupby("parent").indices
    oxy, half = sky_xy(objects.ra, objects.dec, mid), objects.half.to_numpy(float)
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
    mid = mid_sky(t)
    pts, rows = sky_xy(ra, dec, mid), sky_xy(t.ra, t.dec, mid)
    for i, near in enumerate(cKDTree(pts).query_ball_point(rows, r)):
        if near:
            out[i] = bool((np.hypot(*(pts[near] - rows[i]).T) > past).any())
    return out


def bad_pixel(
    t: pd.DataFrame,
    bands: dict[str, Band],
    lone: pd.DataFrame,
    *,
    s: Settings = DEFAULT,
) -> Bools:
    """Return which candidates have a pixel 5 sigma under the sky or a lone peak."""
    out = np.zeros(len(t), bool)
    red = faint_red(t, s=s)
    for i, b in enumerate(s.compact_bands):
        band = bands[b]
        reach = APERTURE + BLOCK * band.scale
        # the ranking reads only the 0.1" flux of the two shorter bands
        near = (APERTURE if i >= 2 else INNER) + BLOCK * band.scale  # noqa: PLR2004 the two longer bands
        yy, xx = np.nonzero(np.nan_to_num(band.data) < -NEG_SIG * band.rms)
        sky = band.wcs.pixel_to_world(xx, yy)
        ra, dec = np.asarray(sky.ra.deg, float), np.asarray(sky.dec.deg, float)
        out |= _within(t, ra, dec, r=near)
        mine = lone.loc[lone.band == b]
        ra, dec = mine.ra.to_numpy(float), mine.dec.to_numpy(float)
        hit = _within(t, ra, dec, r=reach)
        if i == 3:  # noqa: PLR2004 the reddest band
            # a faint red source's own peak shows in the reddest band alone
            hit &= ~red | _within(t, ra, dec, r=reach, past=INNER)
        out |= hit
    return out


def near_edge(
    t: pd.DataFrame, bands: dict[str, Band], *, s: Settings = DEFAULT
) -> Bools:
    """Return which candidates have uncovered sky within `edge` in any band."""
    out = np.zeros(len(t), bool)
    for b in s.compact_bands:
        band = bands[b]
        off = (~np.isfinite(band.raw) | (band.raw == 0)).astype(np.float32)
        xy = np.column_stack(band.pixels(t.ra.to_numpy(float), t.dec.to_numpy(float)))
        r = s.edge / band.scale
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


def _evenness(pts: Floats, sig: Floats) -> float:
    # arcsec the peak nearest the centre and two like peaks are off an evenly spaced row
    r = np.hypot(*pts.T)
    if not len(r) or r.min() > ROW_SAME:
        return np.inf
    c = int(np.argmin(r))
    like = (r >= ROW_IN) & (sig >= sig[c] / ROW_LIKE) & (sig <= sig[c] * ROW_LIKE)
    p = pts[like] - pts[c]
    best = np.inf
    for i in range(len(p)):
        for j in range(len(p)):
            if i != j:
                # one either side of it, or two in a row going away from it
                gap = min(np.hypot(*(p[i] + p[j])), np.hypot(*(p[j] - 2 * p[i])))
                best = min(best, float(gap))
    return best


def in_a_row(
    t: pd.DataFrame, bands: dict[str, Band], *, s: Settings = DEFAULT
) -> Bools:
    """Return which candidates are one of three like dots in an evenly spaced row."""
    a, b = (_band_peaks(t, bands[name]) for name in s.compact_bands[2:])
    return np.array(
        [_evenness(*_merged(p, q)) < ROW_TOL for p, q in zip(a, b, strict=True)], bool
    )


def _merged(
    a: tuple[Floats, Floats], b: tuple[Floats, Floats]
) -> tuple[Floats, Floats]:
    # both bands' peaks, one within ROW_SAME of the other counted once at its stronger
    # significance, as a little red dot can be faint in F277W and bright in F444W
    if not len(a[0]) or not len(b[0]):
        return a if len(a[0]) else b
    d = np.hypot(*(b[0][:, None, :] - a[0][None, :, :]).transpose(2, 0, 1))
    new = d.min(axis=1) > ROW_SAME
    sig = a[1].copy()
    for i, j in zip(np.flatnonzero(~new), d[~new].argmin(axis=1), strict=True):
        sig[j] = max(sig[j], b[1][i])
    return np.r_[a[0], b[0][new]], np.r_[sig, b[1][new]]


def _band_peaks(t: pd.DataFrame, band: Band) -> list[tuple[Floats, Floats]]:
    # each candidate's peaks within ROW_OUT, offset in arcsec on the sky, and how strong
    sm = np.asarray(gaussian_filter(np.nan_to_num(band.data), 1.0), float)
    top = sm == maximum_filter(sm, 3)
    to_sky = np.asarray(band.wcs.pixel_scale_matrix, float).T * 3600
    xs, ys = band.pixels(t.ra.to_numpy(float), t.dec.to_numpy(float))
    out = []
    for x, y in zip(xs, ys, strict=True):
        pix, sig = _peaks(sm, top, (x, y), scale=band.scale)
        pts = pix @ to_sky
        near = np.hypot(*pts.T) <= ROW_OUT
        out.append((pts[near], sig[near]))
    return out


def _peaks(
    sm: Floats, top: Bools, at: tuple[float, float], *, scale: float
) -> tuple[Floats, Floats]:
    # pixel offsets and significance of the peaks at ROW_SIG around `at`
    n = int(np.ceil(ROW_OUT / scale)) + 2
    x0, y0 = round(at[0]), round(at[1])
    ny, nx = sm.shape
    none = np.zeros((0, 2)), np.zeros(0)
    if not (n <= x0 < nx - n and n <= y0 < ny - n):
        return none
    win = sm[y0 - n : y0 + n + 1, x0 - n : x0 + n + 1]
    noise = 1.4826 * np.median(np.abs(win - np.median(win)))
    # a window mostly blank has no noise to measure peaks by
    if noise <= 0:
        return none
    yy, xx = np.nonzero(
        top[y0 - n : y0 + n + 1, x0 - n : x0 + n + 1] & (win > ROW_SIG * noise)
    )
    return np.c_[xx + x0 - n - at[0], yy + y0 - n - at[1]], win[yy, xx] / noise


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
    s = cfg.settings
    t["flag_uncovered"] = ~t.cover.to_numpy(bool)
    t["flag_edge"] = near_edge(t, bands, s=s)
    t["flag_line"] = on_line(t, bands)
    t["flag_one_band"] = one_band(t, s=s)
    t["flag_blue_star"] = blue_star(t, s=s)
    t["flag_sharp"] = sharp(t, s=s)
    t["flag_one_pair"] = one_pair(t, s=s)
    t["flag_dark_middle"] = dark_middle(t, s=s)
    t["flag_listed_star"] = listed_star(t, known_stars(cfg), segments, s=s)
    roll = bands[cfg.detect].roll
    t["flag_gaia"] = on_spikes(t.assign(half=POINT), gaia, roll=roll, square=False)
    t["flag_piece"] = piece(t, objects)
    t["flag_pixel"] = bad_pixel(t, bands, lone, s=s)
    t["flag_row"] = in_a_row(t, bands, s=s)


def _mags(t: pd.DataFrame, ap: str, bands: tuple[str, ...]) -> tuple[Floats, Floats]:
    # asinh magnitudes and their errors, which stay finite for faint and negative fluxes
    f = t[[f"f_{b}_{ap}" for b in bands]].to_numpy(float)
    e = t[[f"e_{b}_{ap}" for b in bands]].to_numpy(float)
    m = -1.0857 * (np.arcsinh(f / (2 * e)) + np.log(e))
    return m, 1.0857 * e / np.sqrt(f**2 + 4 * e**2)


def features(t: pd.DataFrame, *, s: Settings = DEFAULT) -> tuple[Floats, Floats]:
    """Return the three 0.1" colours and two long-wave concentrations, with errors."""
    m1, s1 = _mags(t, "10", s.compact_bands)
    m2, s2 = _mags(t, "25", s.compact_bands)
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
    """Return minus the log density of each row, its errors added to every component."""
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
    hdus: list[tuple[Any, Any, float]], ra: float, dec: float
) -> npt.NDArray[np.uint8]:
    """Cut a 2.4" four-panel tile, its first row lowest like the stamps."""
    at = SkyCoord(ra * u.deg, dec * u.deg)
    img = [_panel(h, at) for h in hdus]
    mid = slice(TILE // 2 - TILE // 12, TILE // 2 + TILE // 12)
    peak = [max(float(np.max(v[mid, mid])), 1e-6) for v in img]
    rgb = _stretch(np.stack([img[3], img[2], img[1]], -1), max(peak[1:]))

    def grey(i: int) -> Floats:
        return np.repeat(_stretch(img[i], peak[i])[..., None], 3, -1)

    below = np.hstack([grey(2), grey(3)])
    out = np.vstack([below, np.hstack([rgb, grey(0)])])
    return np.asarray(np.rint(255 * out), np.uint8)


def _snr_panel(hdu: tuple[Any, Any, float], at: Any) -> Floats:  # noqa: ANN401 astropy has no types
    data, wcs, _ = hdu
    cut = Cutout2D(data, at, CUTOUT * u.arcsec, wcs=wcs, mode="partial", fill_value=0)
    a = np.nan_to_num(np.asarray(cut.data, np.float32))
    a -= np.median(a)
    real = a[a != 0]
    sig = max(1.4826 * float(np.median(np.abs(real))), 1e-12) if len(real) else 1.0
    # asinh(x / 2) is linear to about 2 sigma, so a 3 sigma peak stays faint
    s = np.clip(np.arcsinh(a / sig / 2) / np.arcsinh(SNR_TOP / 2), 0, 1)
    small = Image.fromarray(s.astype(np.float32))
    return np.asarray(small.resize((TILE, TILE), Image.Resampling.BILINEAR), float)


def snr_tile(
    hdus: list[tuple[Any, Any, float]], ra: float, dec: float
) -> npt.NDArray[np.uint8]:
    """Cut a tile laid out like `tile`, each panel drawn by its signal to noise."""
    at = SkyCoord(ra * u.deg, dec * u.deg)
    img = [_snr_panel(h, at) for h in hdus]

    def grey(i: int) -> Floats:
        return np.repeat(img[i][..., None], 3, -1)

    rgb = np.stack([img[3], img[2], img[1]], -1)
    out = np.vstack([np.hstack([grey(2), grey(3)]), np.hstack([rgb, grey(0)])])
    return np.asarray(np.rint(255 * out), np.uint8)


def load(cfg: Config) -> pd.DataFrame:
    """Read every field's compact candidates."""
    cats = []
    for name in cfg.cut_fields():
        path = cfg.out / "fields" / name / LIST
        if not path.exists():
            msg = f"{name} has no {LIST}. Run jwst-peculiar cut first."
            raise SystemExit(msg)
        cat = pd.read_csv(path, dtype={"field": str})
        # an empty one reads back untyped, as in rank.load
        if len(cat):
            cats.append(cat)
    if not cats:
        msg = "No field has any compact candidates."
        raise SystemExit(msg)
    t = pd.concat(cats, ignore_index=True)
    t["id"] = t.field + ":" + t.label.astype(str)
    return t


def stamps(
    cfg: Config,
    top: pd.DataFrame,
    draw: Callable[[list[tuple[Any, Any, float]], float, float], Any] = tile,
) -> npt.NDArray[np.uint8]:
    """Cut a tile for every listed candidate, a field at a time."""
    out = np.zeros((len(top), 2 * TILE, 2 * TILE, 3), np.uint8)
    for name, rows in top.groupby("field").indices.items():
        opened: list[FitsHDUList] = [
            fits.open(cfg.band_path(str(name), b), memmap=True)
            for b in cfg.settings.compact_bands
        ]
        try:
            hdus = [
                (
                    h[cfg.hdu].data,
                    sky_wcs(h[cfg.hdu].header),
                    to_mjy_sr(h[cfg.hdu].header) or 1.0,
                )
                for h in opened
            ]
            for i in rows:
                out[i] = draw(hdus, float(top.ra.iloc[i]), float(top.dec.iloc[i]))
        finally:
            for h in opened:
                h.close()
    return out


def ranked(t: pd.DataFrame, n: int, *, s: Settings = DEFAULT) -> pd.DataFrame:
    """Return the `n` rarest unflagged candidates, rarest first."""
    keep = t.loc[~s.flagged(t)].reset_index(drop=True)
    keep["score"] = 0.0
    # a mixture needs two candidates to fit
    if len(keep) > 1:
        x, xe = features(keep, s=s)
        fits_ = [rarity(fit(x, seed), x, xe) for seed in range(FITS)]
        keep["score"] = np.mean(fits_, axis=0)
    cols = ["id", "field", "label", "ra", "dec", "sn", "a_arcsec", "score"]
    best = keep.sort_values("score", ascending=False).head(n)[cols]
    k = len(best)
    return best.reset_index(drop=True).assign(
        half=CUTOUT / 2, rank=range(1, k + 1), tile=range(k)
    )


def drop_artefacts(
    cfg: Config,
    top: pd.DataFrame,
    tiles: npt.NDArray[np.uint8],
    *,
    models: Path,
    n: int,
) -> tuple[pd.DataFrame, npt.NDArray[np.uint8]]:
    """Return the first `n` candidates and tiles the artefact models let through."""
    drawn = {"peak": tiles, "snr": stamps(cfg, top, snr_tile)}
    chance, cut = artefact_chances(models, drawn, device=resolve_device(cfg.device))
    keep = np.flatnonzero(chance <= cut)[:n]
    print(f"{int((chance > cut).sum())} of the top {len(chance)} look like artefacts")
    k = len(keep)
    out = top.iloc[keep].reset_index(drop=True)
    return out.assign(
        artefact=chance[keep], rank=range(1, k + 1), tile=range(k)
    ), tiles[keep]


def main(cfg: Config, argv: list[str]) -> int:
    """Rank the unflagged compact candidates and cut tiles for the top of the list."""
    p = argparse.ArgumentParser(prog="jwst-peculiar compact CONFIG")
    p.add_argument("-n", type=int, default=500, help="how many candidates to list")
    a = p.parse_args(argv)
    if not has_bands(cfg):
        need = ", ".join(cfg.settings.compact_bands)
        msg = f"The compact branch needs {need} in the config's bands."
        raise SystemExit(msg)
    if cfg.artefacts and not cfg.artefacts.is_file():
        msg = f"The config names artefact models at {cfg.artefacts}, but there's none."
        raise SystemExit(msg)
    t = load(cfg)
    top = ranked(t, a.n * DEPTH if cfg.artefacts else a.n, s=cfg.settings)
    tiles = stamps(cfg, top)
    if cfg.artefacts:
        top, tiles = drop_artefacts(cfg, top, tiles, models=cfg.artefacts, n=a.n)
    np.save(cfg.out / STAMPS, tiles)
    top.to_csv(cfg.out / LIST, index=False)
    print(f"{len(t)} compact candidates, list in {cfg.out / LIST}")
    return 0
