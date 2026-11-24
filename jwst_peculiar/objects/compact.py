"""The compact branch: small sources without a stamp, ranked on how rare they look.

`objects cut` writes each field's compact candidates to compact.csv. A candidate is
a segment with S/N 10 or more and a semimajor axis of 0.12" or less or an area
under 60 pixels, found in F277W, or in F444W where F277W has no segment of S/N 10
there, as little red dots and brown dwarfs are brightest in F444W. Each gets these
tests as `flag_*` columns.

- `uncovered`: an aperture reaches uncovered sky.
- `one_band`: under 3 sigma in all but one band in the 0.1" aperture, or a one-band
  peak, its best band at S/N 10 or more and every other under max(4, a twentieth).
- `sharp`: more of its flux in 0.1" than a star has, by 3 sigma in one band or
  combined, which is a cosmic ray or hot pixel.
- `one_pair`: seen in only one of NIRCam's two exposure pairs.
- `dark_middle`: a band under S/N 1 between two at S/N 3 or more.
- `listed_star`: a listed star brighter in F150W than F444W within 0.3", or within
  2" of a listed star 20 times brighter in F277W. Cool brown dwarfs are redder, so
  they stay.
- `gaia`: within 0.3" of a Gaia star's spikes or halo.
- `piece`: a deblended piece of a stamped object's parent, inside its stamp.
- `pixel`: a pixel 5 sigma under the sky, or a lone peak by `flags.blobs`, within
  0.25" plus 1.5 of that band's pixels.

`objects compact` fits a 20-component Gaussian mixture to the unflagged candidates'
three colours and two concentrations, and ranks them on minus the log density with
each candidate's own errors added to every component.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D
import astropy.units as u
from astropy.wcs import WCS
import numpy as np
import pandas as pd
from PIL import Image
from scipy.spatial import cKDTree
from scipy.special import logsumexp

from jwst_peculiar.objects.flags import (
    DETECTED,
    MIN_BANDS,
    known_stars,
    on_spikes,
    sky_xy,
)

if TYPE_CHECKING:
    import numpy.typing as npt

    from jwst_peculiar.objects import Config
    from jwst_peculiar.objects.cut import Band
    from jwst_peculiar.utils import FitsHDUList

    Floats = npt.NDArray[np.float64]
    Bools = npt.NDArray[np.bool_]

BANDS = ("f115w", "f150w", "f277w", "f444w")
RED, OFFSET = "f444w", 1_000_000
MIN_SN, MAX_A, MAX_AREA = 10.0, 0.12, 60
PEAK_SN, PEAK_FLOOR, PEAK_SHARE = 10.0, 4.0, 20.0
# 99th percentile of 0.1" over 0.25" flux for 742 Gaia stars, G 19 to 21, S/N over 30
PSF_SHARPEST = {"f115w": 0.88, "f150w": 0.865, "f277w": 0.70, "f444w": 0.61}
SHARP_SIG, SHARP_FLOOR = 3.0, 5.0
# NIRCam takes F115W with F277W, then F150W with F444W, at the same dither
PAIRS = (("f115w", "f277w"), ("f150w", "f444w"))
SEEN, UNSEEN = 5.0, 2.0
DARK_SEEN, DARK = 3.0, 1.0
POINT, HALO_REACH, HALO_RATIO = 0.3, 2.0, 20.0
NEG_SIG, APERTURE, BLOCK = 5.0, 0.25, 1.5
COMPONENTS = 20
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


def one_band(t: pd.DataFrame) -> Bools:
    """Return which candidates show in only one band, or are a one-band peak."""
    z = np.column_stack([_snr(t, b) for b in BANDS])
    top, rest = z.max(axis=1), np.sort(z, axis=1)[:, -2]
    peak = (top >= PEAK_SN) & (rest < np.maximum(PEAK_FLOOR, top / PEAK_SHARE))
    return np.asarray(((z >= DETECTED).sum(axis=1) < MIN_BANDS) | peak, bool)


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


def listed_star(t: pd.DataFrame, stars: pd.DataFrame, segments: pd.DataFrame) -> Bools:
    """Return which candidates are a blue listed star or in a much brighter one's halo.

    A star's F277W flux is that of the segment within 0.3" of it. Many stars bright
    in the infrared are fainter than Gaia's G 21, so the list catches what Gaia misses.

    Returns:
        One flag per candidate.
    """
    out = np.zeros(len(t), bool)
    if not len(stars) or not len(t):
        return out
    dec0 = float(t.dec.median())
    sxy, txy = sky_xy(stars.ra, stars.dec, dec0), sky_xy(t.ra, t.dec, dec0)
    d, _ = cKDTree(sxy).query(txy)
    blue = t.f_f150w_25.to_numpy(float) > t.f_f444w_25.to_numpy(float)
    out |= (d < POINT) & blue
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
    dec0 = float(t.dec.median())
    txy = sky_xy(t.ra, t.dec, dec0)
    groups = objects.groupby("parent").indices
    oxy, half = sky_xy(objects.ra, objects.dec, dec0), objects.half.to_numpy(float)
    for i, parent in enumerate(t.parent.to_numpy(int)):
        if parent in groups:
            k = groups[parent]
            d = np.abs(oxy[k] - txy[i])
            out[i] = bool(((d[:, 0] < half[k]) & (d[:, 1] < half[k])).any())
    return out


def _within(t: pd.DataFrame, ra: Floats, dec: Floats, *, r: float) -> Bools:
    if not len(ra) or not len(t):
        return np.zeros(len(t), bool)
    dec0 = float(t.dec.median())
    tree = cKDTree(sky_xy(ra, dec, dec0))
    d, _ = tree.query(sky_xy(t.ra, t.dec, dec0), distance_upper_bound=r)
    return np.asarray(np.isfinite(d), bool)


def bad_pixel(t: pd.DataFrame, bands: dict[str, Band], lone: pd.DataFrame) -> Bools:
    """Return which candidates have a pixel 5 sigma under the sky or a lone peak.

    A pixel counts when it lies within 0.25" plus 1.5 of its band's pixels, since a
    hot pixel just outside 0.25" still leaks into the 0.063" long-wave aperture.

    Returns:
        One flag per candidate.
    """
    out = np.zeros(len(t), bool)
    for b in BANDS:
        band = bands[b]
        reach = APERTURE + BLOCK * band.scale
        yy, xx = np.nonzero(np.nan_to_num(band.data) < -NEG_SIG * band.rms)
        sky = band.wcs.pixel_to_world(xx, yy)
        ra, dec = np.asarray(sky.ra.deg, float), np.asarray(sky.dec.deg, float)
        out |= _within(t, ra, dec, r=reach)
        mine = lone.loc[lone.band == b]
        out |= _within(t, mine.ra.to_numpy(float), mine.dec.to_numpy(float), r=reach)
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
    t["flag_one_band"] = one_band(t)
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


def fit(x: Floats) -> Any:  # noqa: ANN401 scikit-learn has no types
    """Return the mixture the candidates are ranked under, fitted to `x`."""
    from sklearn.mixture import GaussianMixture  # noqa: PLC0415 only compact needs it

    mix = GaussianMixture(
        COMPONENTS, covariance_type="full", random_state=0, max_iter=500
    )
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


def _panel(data: Any, wcs: Any, at: Any) -> Floats:  # noqa: ANN401 astropy has no types
    cut = Cutout2D(data, at, CUTOUT * u.arcsec, wcs=wcs, mode="partial", fill_value=0)
    a = np.nan_to_num(np.asarray(cut.data, np.float32))
    small = Image.fromarray(a - np.median(a))
    return np.asarray(small.resize((TILE, TILE), Image.Resampling.BILINEAR), float)


def tile(hdus: dict[str, tuple[Any, Any]], ra: float, dec: float) -> Any:  # noqa: ANN401 a uint8 tile
    """Cut a 2.4" four-panel tile, its first row lowest like the stamps.

    The F444W, F277W, F150W colour panel and F115W sit on top, F277W and F444W
    below, every panel scaled to its brightest pixel within 0.2" of the centre, as a
    faint compact source vanishes under a percentile stretch.

    Returns:
        A (224, 224, 3) uint8 tile.
    """
    at = SkyCoord(ra * u.deg, dec * u.deg)
    img = {b: _panel(*hdus[b], at) for b in BANDS}
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
            msg = f"{name} has no {LIST}. Run objects cut first."
            raise SystemExit(msg)
        cats.append(pd.read_csv(path))
    t = pd.concat(cats, ignore_index=True)
    t["id"] = t.field + ":" + t.label.astype(str)
    return t


def stamps(cfg: Config, top: pd.DataFrame) -> Any:  # noqa: ANN401 uint8 tiles
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
                b: (h[cfg.hdu].data, WCS(h[cfg.hdu].header).celestial)
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
    keep["score"] = rarity(fit(x), x, xe)
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
    p = argparse.ArgumentParser(prog="jwst-peculiar objects compact CONFIG")
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
