"""The step 2 artefact tests, each written to an object table as a `flag_*` column.

- `one_band`: under 3 sigma in all but one band in the 0.25" aperture.
- `target_star`: a listed star within 0.3" of the centre.
- `blob`: a one-band peak anywhere in the stamp at least a tenth as bright as the
  object. These are cosmic rays and hot pixels that drizzling left behind.
- `star`: a listed star in the stamp, other than the object, at least a tenth as
  bright.
- `spike`: the stamp touches a Gaia star's six diffraction spikes or its halo.
- `edge`: the stamp reaches uncovered sky in a colour band.
- `merged`: a deblended piece inside the stamp of its parent's brightest unflagged
  piece, so the same galaxy twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import warnings

import numpy as np
import pandas as pd
from scipy.ndimage import map_coordinates, maximum_filter
from scipy.spatial import cKDTree

if TYPE_CHECKING:
    import numpy.typing as npt

    from jwst_peculiar.config import Config, SkyCoords
    from jwst_peculiar.cut import Band

    Floats = npt.NDArray[np.float64]
    Bools = npt.NDArray[np.bool_]
    Grid = tuple[Floats, Floats]

PEAK_SIG, FLOOR_SIG, CONTRAST, RING, BOX = 10.0, 4.0, 20.0, 0.25, 0.1
BRIGHT, SELF_R, DETECTED, MIN_BANDS = 0.1, 0.3, 3.0, 2
STAR_PROB = 0.5  # Gaia's own threshold on its star probability
# Measured on 1,921 Gaia star visits in COSMOS-Web, in F277W.
SPIKE_L0, SPIKE_SLOPE, SPIKE_WIDTH = 4.70, 0.215, 0.15
# half the 5-pixel box a star's brightness is read in
HALF_BOX = 2
HALO_R0, HALO_SLOPE = 2.30, 0.073


def grid(n: int) -> Grid:
    """Return an n by n grid over a stamp, from -1 to 1 on both axes."""
    g = np.linspace(-1, 1, n)
    gx, gy = np.meshgrid(g, g)
    return gx, gy


COARSE = grid(56)


def stamp_pixels(band: Band, corners: SkyCoords, grid: Grid) -> list[Floats]:
    """Return the rows and columns in `band` of a grid over one stamp.

    `corners` are the stamp's centre, right edge and top edge on the sky. Over one
    stamp the mapping between two pixel grids is close enough to linear.

    Returns:
        Rows and columns, ready for `map_coordinates`.
    """
    gx, gy = grid
    cx, cy = (np.asarray(v, float) for v in band.wcs.world_to_pixel(corners))
    rows = cy[0] + gx * (cy[1] - cy[0]) + gy * (cy[2] - cy[0])
    cols = cx[0] + gx * (cx[1] - cx[0]) + gy * (cx[2] - cx[0])
    return [rows, cols]


def mid_sky(t: pd.DataFrame) -> tuple[float, float]:
    """Return the RA and Dec to project `t` around, any at all when it's empty.

    RA is a circular mean, so a field across RA 0 has its middle near 0 and not 180.

    Returns:
        Degrees.
    """
    ra = np.radians(t.ra.to_numpy(float))
    ra = ra[np.isfinite(ra)]
    if not len(ra):
        return 0.0, 0.0
    ra0 = np.degrees(np.arctan2(np.sin(ra).mean(), np.cos(ra).mean())) % 360
    return float(ra0), float(t.dec.median())


def sky_xy(
    ra: Floats | pd.Series[float],
    dec: Floats | pd.Series[float],
    mid: tuple[float, float],
) -> Floats:
    """Return positions in arcsec on a flat sky around `mid`, from `mid_sky`.

    One cos(dec) for the whole field keeps offsets between objects right, and RA is
    measured from the middle, so a field across RA 0 stays in one piece.

    Returns:
        (n, 2) east and north in arcsec.
    """
    ra0, dec0 = mid
    east = (np.asarray(ra, float) - ra0 + 180) % 360 - 180
    return np.column_stack([
        east * np.cos(np.radians(dec0)) * 3600,
        np.asarray(dec, float) * 3600,
    ])


def _ring_median(band: Band, x: Floats, y: Floats) -> Floats:
    # the median of 24 points on a circle 0.25" out, read bilinearly
    r = RING / band.scale
    a = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    rx, ry = x[:, None] + r * np.cos(a), y[:, None] + r * np.sin(a)
    v = map_coordinates(band.data, [ry.ravel(), rx.ravel()], order=1, cval=np.nan)
    with warnings.catch_warnings():
        # a ring wholly off the data is NaN, and NaN passes no peak test
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.asarray(np.nanmedian(v.reshape(rx.shape), axis=1), float)


def _peaks(band: Band) -> tuple[Floats, Floats, Floats]:
    z = np.nan_to_num(band.data)
    yy, xx = np.nonzero((z >= PEAK_SIG * band.rms) & (z == maximum_filter(z, size=3)))
    x, y = xx.astype(float), yy.astype(float)
    exc = z[yy, xx] - _ring_median(band, x, y)
    keep = exc >= PEAK_SIG * band.rms[yy, xx]
    return x[keep], y[keep], exc[keep]


def _rises(band: Band, ra: Floats, dec: Floats) -> tuple[Floats, Floats]:
    # the brightest pixel within 0.1" of each position over its ring, and the RMS there
    x, y = band.pixels(ra, dec)
    ix, iy = np.rint(x).astype(int), np.rint(y).astype(int)
    ny, nx = band.data.shape
    ok = (ix >= 0) & (iy >= 0) & (ix < nx) & (iy < ny)
    ok[ok] = np.isfinite(band.data[iy[ok], ix[ok]])
    rise, rms = np.full(len(x), np.inf), np.ones(len(x))
    k = np.flatnonzero(ok)
    if len(k):
        size = 2 * int(np.ceil(BOX / band.scale)) + 1
        top = maximum_filter(np.nan_to_num(band.data, nan=-np.inf), size=size)
        rise[k] = top[iy[k], ix[k]] - _ring_median(band, x[k], y[k])
        rms[k] = band.rms[iy[k], ix[k]]
    return rise, rms


def _lone_peaks(bands: dict[str, Band], name: str) -> pd.DataFrame:
    x, y, exc = _peaks(bands[name])
    sky = bands[name].wcs.pixel_to_world(x, y)
    ra, dec = np.asarray(sky.ra.deg, float), np.asarray(sky.dec.deg, float)
    lone = np.ones(len(x), bool)
    for other, band in bands.items():
        if other != name:
            rise, rms = _rises(band, ra, dec)
            lone &= rise < np.maximum(FLOOR_SIG * rms, exc / CONTRAST)
    peak = np.nan_to_num(bands[name].data)[y.astype(int), x.astype(int)]
    found = pd.DataFrame({"band": name, "ra": ra, "dec": dec, "peak": peak})
    return found.loc[lone]


def blobs(bands: dict[str, Band], colour: tuple[str, ...]) -> pd.DataFrame:
    """Find peaks 10 sigma over a ring 0.25" out in one band that no other band has.

    A peak is lone when in every other band the brightest pixel within 0.1" rises
    less than `max(4 sigma, excess / 20)` over that band's own ring. A cosmic ray on a
    galaxy passes the first test and fails the second, since the galaxy is there in
    every band. A real source of any colour shows in a neighbouring band at more
    than a twentieth of its contrast, as NIRCam PSF peaks differ by at most about 8.

    Returns:
        One row per lone peak, with its band, position and peak value.
    """
    return pd.concat([_lone_peaks(bands, b) for b in colour], ignore_index=True)


class StampBoxes:
    """The objects' stamp squares on the middle colour band's pixel grid."""

    def __init__(self, t: pd.DataFrame, grid: Band) -> None:
        """Place every object's stamp on `grid`."""
        self.t, self.grid = t, grid
        self.mid = mid_sky(t)
        self.cx, self.cy = grid.pixels(t.ra.to_numpy(float), t.dec.to_numpy(float))
        self.half = t.half.to_numpy(float)
        self.hpx = self.half / grid.scale

    def corners(self, i: int) -> SkyCoords:
        """Return stamp `i`'s centre, right edge and top edge on the sky."""
        x, y, h = self.cx[i], self.cy[i], self.hpx[i]
        return self.grid.wcs.pixel_to_world([x, x + h, x], [y, y, y + h])

    def count(
        self, sky: Grid, value: Floats, *, skip_self: bool
    ) -> npt.NDArray[np.int_]:
        """Count positions inside each stamp at least a tenth as bright as its object.

        Returns:
            One count per object.
        """
        n = np.zeros(len(self.t), int)
        if not len(sky[0]):
            return n
        px, py = self.grid.pixels(*sky)
        reach = self.half * np.sqrt(2) + 0.01
        near = cKDTree(sky_xy(*sky, self.mid)).query_ball_point(
            sky_xy(self.t.ra, self.t.dec, self.mid), reach
        )
        floor = BRIGHT * self.t.max_value.to_numpy(float)
        for i, hits in enumerate(near):
            h = np.asarray(hits, int)
            dx, dy = px[h] - self.cx[i], py[h] - self.cy[i]
            ok = (np.maximum(abs(dx), abs(dy)) < self.hpx[i]) & (value[h] >= floor[i])
            if skip_self:
                ok &= np.hypot(dx, dy) * self.grid.scale > SELF_R
            n[i] = int(ok.sum())
        return n

    def uncovered(self, colour: list[Band]) -> Bools:
        """Say which stamps reach uncovered sky, NaN or exactly 0, in a colour band."""
        out = np.zeros(len(self.t), bool)
        for b in colour:
            cov = (np.isfinite(b.raw) & (b.raw != 0)).astype(np.float32)
            for i in range(len(self.t)):
                at = stamp_pixels(b, self.corners(i), COARSE)
                out[i] |= map_coordinates(cov, at, order=0, cval=0).min() < 1
        return out


def reach(g: Floats) -> Floats:
    """Return how far a Gaia star's spikes reach, in arcsec, from its G magnitude."""
    return SPIKE_L0 * 10 ** (SPIKE_SLOPE * (18 - g))


def halo(g: Floats) -> Floats:
    """Return the radius of a Gaia star's halo, in arcsec, from its G magnitude."""
    return HALO_R0 * 10 ** (HALO_SLOPE * (18 - g))


def _touches(
    d: Floats, sizes: Grid, *, pad: float, axes: Floats | None, square: bool
) -> bool:
    # d is each nearby star's offset to the object, axes the spikes' unit vectors
    length, radius = sizes
    if (np.hypot(*d.T) < radius + pad).any():
        return True
    if axes is None:
        return False
    along = d @ axes.T
    across = np.abs(d @ np.c_[-axes[:, 1], axes[:, 0]].T)
    spike = (along > -pad) & (along < length[:, None] + pad)
    side = pad * np.sqrt(2) if square else pad
    return bool((spike & (across < side + SPIKE_WIDTH)).any())


def on_spikes(
    t: pd.DataFrame, gaia: pd.DataFrame, *, roll: float | None, square: bool = True
) -> Bools:
    """Say which stamps touch a Gaia star's six spikes or its halo.

    The spikes run at `PA_V3 + 60 k` degrees, and without a roll angle only halos
    count. With `square` off, `half` is a radius around a point, not a stamp.

    Returns:
        One flag per object.
    """
    hit = np.zeros(len(t), bool)
    star = gaia.loc[gaia.classprob_dsc_combmod_star.to_numpy(float) > STAR_PROB]
    if not len(star) or not len(t):
        return hit
    mid = mid_sky(t)
    sxy, oxy = sky_xy(star.ra, star.dec, mid), sky_xy(t.ra, t.dec, mid)
    mag = star.phot_g_mean_mag.to_numpy(float)
    th = None if roll is None else np.radians(roll + np.arange(0, 360, 60))
    axes = None if th is None else np.c_[np.sin(th), np.cos(th)]
    pad = t.half.to_numpy(float)
    near = cKDTree(sxy).query_ball_point(oxy, np.max(reach(mag)) + pad * np.sqrt(2))
    for i, h in enumerate(near):
        if h:
            sizes = (reach(mag[h]), halo(mag[h]))
            hit[i] = _touches(
                oxy[i] - sxy[h], sizes, pad=pad[i], axes=axes, square=square
            )
    return hit


def merged(t: pd.DataFrame, bad: Bools) -> Bools:
    """Say which deblended pieces sit in the stamp of their parent's brightest piece.

    Only unflagged pieces count, as pieces and as the brightest.

    Returns:
        True for those pieces, which are the same galaxy as that brightest piece.
    """
    out = np.zeros(len(t), bool)
    x, y = sky_xy(t.ra, t.dec, mid_sky(t)).T
    for _, g in t.loc[~bad].groupby("parent"):
        if len(g) <= 1:
            continue
        idx = g.index.to_numpy()
        top = int(idx[np.argmax(g.segment_flux.to_numpy(float))])
        half = float(t.half.iloc[top])
        inside = np.maximum(abs(x[idx] - x[top]), abs(y[idx] - y[top])) < half
        out[idx[inside & (idx != top)]] = True
    return out


def known_stars(cfg: Config) -> pd.DataFrame:
    """Return the configured star list, ra and dec in degrees, or an empty one."""
    if cfg.stars is None:
        return pd.DataFrame({"ra": [], "dec": []}, dtype=float)
    return pd.read_csv(cfg.stars, usecols=["ra", "dec"])


def _star_brightness(band: Band, st: pd.DataFrame) -> Floats:
    # the brightest pixel in a box on each star, which is what a stamp sees
    x, y = band.pixels(st.ra.to_numpy(float), st.dec.to_numpy(float))
    ix, iy = np.rint(x).astype(int), np.rint(y).astype(int)
    ny, nx = band.data.shape
    out = np.zeros(len(st))
    h = HALF_BOX
    for j in np.flatnonzero((ix >= h) & (iy >= h) & (ix < nx - h) & (iy < ny - h)):
        box = band.data[iy[j] - h : iy[j] + h + 1, ix[j] - h : ix[j] + h + 1]
        out[j] = np.nanmax(box) if np.isfinite(box).any() else 0
    return out


def _nearest(st: pd.DataFrame, t: pd.DataFrame) -> Floats:
    if not len(st):
        return np.full(len(t), np.inf)
    mid = mid_sky(t)
    d, _ = cKDTree(sky_xy(st.ra, st.dec, mid)).query(sky_xy(t.ra, t.dec, mid))
    return np.asarray(d, float)


def flags(
    t: pd.DataFrame,
    bands: dict[str, Band],
    *,
    cfg: Config,
    gaia: pd.DataFrame,
    lone: pd.DataFrame,
) -> None:
    """Add every artefact test to `t` as a `flag_*` column.

    `lone` is `blobs` over every band, of which only the colour bands' count here.
    """
    colour = [bands[b] for b in cfg.colour]
    det = bands[cfg.detect]
    box = StampBoxes(t, colour[1])
    snr = [
        t[f"f_{b}_25"].to_numpy(float) / t[f"e_{b}_25"].to_numpy(float)
        for b in cfg.bands
    ]
    t["flag_one_band"] = (np.array(snr) >= DETECTED).sum(axis=0) < MIN_BANDS
    st = known_stars(cfg)
    t["flag_target_star"] = _nearest(st, t) < SELF_R
    lone = lone.loc[lone.band.isin(cfg.colour)]
    at = (lone.ra.to_numpy(float), lone.dec.to_numpy(float))
    t["flag_blob"] = box.count(at, lone.peak.to_numpy(float), skip_self=False) > 0
    at = (st.ra.to_numpy(float), st.dec.to_numpy(float))
    t["flag_star"] = box.count(at, _star_brightness(det, st), skip_self=True) > 0
    t["flag_spike"] = on_spikes(t, gaia, roll=det.roll)
    t["flag_edge"] = box.uncovered(colour)
    t["flag_merged"] = merged(t, t.filter(like="flag_").to_numpy().any(axis=1))
