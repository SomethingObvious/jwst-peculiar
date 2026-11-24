from __future__ import annotations

import codecs
import csv
import dataclasses
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
import pathlib
import re
import socket
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from astropy.io import fits
from astropy.wcs import WCS
import numpy as np
import pandas as pd
from PIL import Image
import pytest
from sklearn.linear_model import LogisticRegression
import torch

from jwst_peculiar import cut, server
from jwst_peculiar.check import report
from jwst_peculiar.cli import main, run
from jwst_peculiar.compact import (
    BANDS,
    bad_pixel,
    blue_star,
    dark_middle,
    near_edge,
    on_line,
    one_band,
    one_pair,
    sharp,
)
from jwst_peculiar.compact import (
    main as compact_main,
)
from jwst_peculiar.config import EMPTY, Config, read_config, written
from jwst_peculiar.cut import (
    Band,
    N,
    NoData,
    detect,
    fitting_workers,
    gaia_stars,
    photometry,
    render,
    wcs_record,
)
from jwst_peculiar.cut import (
    main as cut_main,
)
from jwst_peculiar.embed import embed
from jwst_peculiar.flags import blobs, merged, on_spikes
from jwst_peculiar.init import guess, split_name
from jwst_peculiar.rank import (
    CLASSES,
    MODEL,
    Objects,
    Sky,
    label_main,
    p_worth,
    rank_main,
    rarity,
    reference,
)
from jwst_peculiar.server import make_handler, open_server
from jwst_peculiar.verdicts import read_verdicts, save_verdict
from jwst_peculiar.view import ObjectBundle, add_site_meta, export, lit_span

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import numpy.typing as npt

SCALE = 0.03
RA0, DEC0 = 150.0, 2.0


def band(data: npt.NDArray[np.float32]) -> Band:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [RA0, DEC0]
    w.wcs.crpix = [data.shape[1] / 2 + 1, data.shape[0] / 2 + 1]
    w.wcs.cdelt = [-SCALE / 3600, SCALE / 3600]
    rms = np.ones_like(data)
    return Band(raw=data, data=data, rms=rms, wcs=w, scale=SCALE, roll=None)


def gaussian(
    x0: float, y0: float, *, sigma: float, amp: float
) -> npt.NDArray[np.float32]:
    y, x = np.mgrid[:200, :200]
    r2 = (x - x0) ** 2 + (y - y0) ** 2
    return np.asarray(amp * np.exp(-r2 / (2 * sigma**2)), np.float32)


def offsets(east: list[float], north: list[float]) -> pd.DataFrame:
    # objects at offsets in arcsec from RA0, DEC0, each with a 0.6 arcsec stamp
    dec = DEC0 + np.array(north) / 3600
    ra = RA0 + np.array(east) / 3600 / np.cos(np.radians(np.median(dec)))
    return pd.DataFrame({"ra": ra, "dec": dec, "half": 0.6})


def test_one_band_peaks_are_blobs_and_real_sources_are_not() -> None:
    sky = gaussian(100, 100, sigma=5, amp=50) + gaussian(40, 40, sigma=1.2, amp=200)
    images = {b: sky.copy() for b in ("f115w", "f150w", "f277w")}
    images["f115w"][100, 112] += 1000
    found = blobs({b: band(v) for b, v in images.items()}, ("f115w", "f150w", "f277w"))
    assert found.band.tolist() == ["f115w"]
    x, y = band(sky).pixels(found.ra.to_numpy(), found.dec.to_numpy())
    assert (round(float(x[0])), round(float(y[0]))) == (112, 100)


def test_spike_flags_follow_the_roll_angle() -> None:
    star = pd.DataFrame({
        "ra": [RA0],
        "dec": [DEC0],
        "phot_g_mean_mag": [18.0],
        "classprob_dsc_combmod_star": [0.9],
    })
    # 4" out is past the halo (2.3") and inside the spikes' reach (4.7") at G 18.
    t = offsets([0.0, 4 * np.sin(np.radians(30))], [4.0, 4 * np.cos(np.radians(30))])
    assert on_spikes(t, star, roll=0.0).tolist() == [True, False]
    assert on_spikes(t, star, roll=30.0).tolist() == [False, True]
    assert on_spikes(t, star, roll=None).tolist() == [False, False]


def test_compact_rules_drop_artefacts_and_keep_a_red_point_source() -> None:
    # 0.1" and 0.25" fluxes in F115W, F150W, F277W and F444W, all with error 1
    rows = {
        "little red dot": ([2, 6, 30, 60], [3, 8, 45, 100]),
        "hot pixel in F444W": ([0, 0, 4, 60], [0, 0, 6, 62]),
        "only F150W and F444W": ([0, 20, 0, 30], [0, 30, 0, 50]),
        "dark F150W": ([20, 0, 20, 20], [40, 0, 40, 40]),
        "one band": ([0, 0, 0, 25], [0, 0, 0, 40]),
    }
    t = pd.DataFrame({
        f"{k}_{b}_{ap}": [
            (v[0] if ap == "10" else v[1])[i] if k == "f" else 1.0
            for v in rows.values()
        ]
        for k in ("f", "e")
        for i, b in enumerate(("f115w", "f150w", "f277w", "f444w"))
        for ap in ("10", "25")
    })
    caught = one_band(t) | sharp(t) | one_pair(t) | dark_middle(t)
    assert caught.tolist() == [False, True, True, True, True]
    assert sharp(t).tolist() == [False, True, False, False, False]


def test_pieces_of_one_galaxy_merge_into_the_brightest() -> None:
    t = offsets([0.0, 1.0, 5.0, 1.0], [0.0, 0.0, 0.0, 0.5])
    t["half"] = 2.0
    t["parent"] = [1, 1, 1, 2]
    t["segment_flux"] = [100.0, 10.0, 10.0, 1.0]
    none = np.zeros(4, bool)
    assert merged(t, none).tolist() == [False, True, False, False]
    # A flagged piece can't be the one the others merge into.
    assert merged(t, np.array([True, False, False, False])).tolist() == [False] * 4


def test_render_keeps_colour_and_removes_the_background() -> None:
    rgb = np.full((3, N, N), 5.0)
    y, x = np.mgrid[:N, :N]
    rgb[0] += 100 * np.exp(-((x - N // 2) ** 2 + (y - N // 2) ** 2) / 50)
    out = render(rgb)
    assert out.shape == (N, N, 3)
    assert np.max(out[..., 1:]) == 0
    assert out[N // 2, N // 2, 0] == 255
    assert out[0, 0, 0] == 0


def test_rarity_ignores_neighbours_that_share_its_sky() -> None:
    u, w = np.array([1.0, 0.0]), np.array([0.5, np.sqrt(0.75)])
    e = np.array([u, u, u, u, u, u, w], np.float32)
    # Object 5 sits on object 0, so 0 can only count four objects like it.
    t = offsets([0, 100, 200, 300, 400, 0, 500], [0, 0, 0, 0, 0, 0.1, 0])
    rar = rarity(e, Sky(t))
    assert rar[0] == pytest.approx(0.5, abs=1e-6)
    assert rar[1] == pytest.approx(0.0, abs=1e-6)


def test_a_reference_run_supplies_neighbours_from_elsewhere() -> None:
    u, w = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    # Five objects like w in another field, and one sitting on object 0 here.
    here = Objects(
        offsets([0, 100], [0, 0]).assign(flag_x=False), np.array([u, w], np.float32)
    )
    there = offsets([0.1, 5000, 5100, 5200, 5300, 5400], [0] * 6).assign(flag_x=False)
    other = Objects(there, np.array([u] + [w] * 5, np.float32))
    ref = reference(here, other)
    assert ref.tolist() == [w.tolist()] * 5
    alone = rarity(here.e, Sky(here.t))
    with_ref = rarity(here.e, Sky(here.t), ref=ref)
    assert alone[1] == pytest.approx(1.0, abs=1e-6)
    assert with_ref.tolist() == pytest.approx([1.0, 0.0], abs=1e-6)


def test_label_model_probabilities_match_scikit_learn() -> None:
    rng = np.random.default_rng(0)
    e = rng.normal(size=(90, 8)).astype(np.float32)
    y = np.repeat([0, 1, 2], 30)
    e[y == CLASSES["interesting"]] += 1
    clf = LogisticRegression(C=0.1, class_weight="balanced", max_iter=5000).fit(e, y)
    model = {"coef": clf.coef_, "intercept": clf.intercept_, "classes": clf.classes_}
    assert np.allclose(p_worth(model, e), clf.predict_proba(e)[:, 2], atol=1e-6)


def test_the_example_config_reads() -> None:
    examples = pathlib.Path(__file__).parents[1] / "examples"
    cfg = read_config(examples / "smacs0723.yaml")
    assert cfg.band_path("jw02736-o001_t001", "f090w") == (
        examples / "mosaics" / "jw02736-o001_t001_nircam_clear-f090w_i2d.fits"
    )


def test_config_resolves_paths_and_finds_fields(
    tmp_path: Path,
) -> None:
    # brackets in a folder name mean something to glob
    data = tmp_path / "data [1]"
    data.mkdir()
    for name in ("a_f277w", "b_f277w", "c_f150w"):
        (data / f"{name}.fits").touch()
    (tmp_path / "survey.yaml").write_text(
        "name: test\nout: run\npattern: data [1]/{field}_{band}.fits\n",
        encoding="utf-8",
    )
    cfg = read_config(tmp_path / "survey.yaml")
    assert cfg.out == tmp_path / "run"
    assert cfg.field_names() == ["a", "b"]
    assert cfg.band_path("a", "f150w") == data / "a_f150w.fits"


def test_band_names_in_capitals_mean_the_same_bands(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a_F277W.fits").touch()
    (tmp_path / "survey.yaml").write_text(
        "name: t\nout: run\npattern: d/{field}_{BAND}.fits\n"
        "bands: [F115W, F150W, F277W, F444W]\ndetect: F277W\n",
        encoding="utf-8",
    )
    cfg = read_config(tmp_path / "survey.yaml")
    assert cfg.field_names() == ["a"]
    assert cfg.bands == ("f115w", "f150w", "f277w", "f444w")
    assert cfg.detect == "f277w"
    assert cfg.band_path("a", "f115w") == tmp_path / "d" / "a_F115W.fits"


BASE = "name: t\nout: run\npattern: d/{field}_{band}.fits\n"


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (f"{BASE}devise: cpu\n", "Did you mean 'device'?"),
        ("name: t\nout: run\npattern: d/{field}.fits\n", "needs {band}"),
        (f"{BASE}detect: f200w\n", "uses ['f200w']"),
        (f"{BASE}colour: [f277w]\n", "needs three bands"),
        ("name: t\nout: [run\n", "isn't valid YAML"),
        ("- name\n", "should be a list of keys"),
    ],
)
def test_a_bad_config_says_what_is_wrong(tmp_path: Path, text: str, said: str) -> None:
    (tmp_path / "survey.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(SystemExit, match=re.escape(said)):
        read_config(tmp_path / "survey.yaml")


def save_then_stop(target: Path) -> None:
    with written(target) as tmp:
        np.save(tmp, np.zeros(3))
        raise KeyboardInterrupt


def test_an_interrupted_write_leaves_no_file(tmp_path: Path) -> None:
    target = tmp_path / "stamps.npy"
    with pytest.raises(KeyboardInterrupt):
        save_then_stop(target)
    assert list(tmp_path.iterdir()) == []
    with written(target) as tmp:
        np.save(tmp, np.ones(3))
    assert np.load(target).tolist() == [1, 1, 1]
    assert [p.name for p in tmp_path.iterdir()] == ["stamps.npy"]


def test_check_lists_missing_mosaics(tmp_path: Path) -> None:
    (tmp_path / "a_f277w.fits").touch()
    cfg = Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{{field}}_{{band}}.fits",
        device="cpu",
    )
    problems = report(cfg)
    assert [p for p in problems if p.startswith("missing")] == [
        f"missing {tmp_path / f'a_{b}.fits'}" for b in ("f115w", "f150w", "f444w")
    ]


def fake_run(out: Path, n: int) -> Config:
    # one field of n objects 100" apart, each stamp marked on its first row
    folder = out / "fields" / "a"
    folder.mkdir(parents=True)
    t = offsets(list(np.arange(n) * 100.0), [0.0] * n)
    t = t.assign(label=range(1, n + 1), field="a", sn=50.0, a_arcsec=0.2, flag_x=False)
    t.to_csv(folder / "objects.csv", index=False)
    rng = np.random.default_rng(1)
    e = rng.normal(size=(n, 4))
    np.save(folder / "embeddings.npy", (e / np.linalg.norm(e, axis=1)[:, None]))
    stamps = np.zeros((n, N, N, 3), np.uint8)
    stamps[:, 0] = 255
    np.save(folder / "stamps.npy", stamps)
    cards, scale = wcs_record(band(np.zeros((200, 200), np.float32)).wcs)
    meta = {"shape": [200, 200], "preview_scale": 1, "filters": ["A", "B", "C"]}
    meta |= {"wcs": cards, "pixel_scale_arcsec": scale, "header": {}}
    (folder / "field.json").write_text(json.dumps(meta), encoding="utf-8")
    return Config(name="test", out=out, pattern="{field}_{band}.fits")


def test_verdicts_train_the_model_and_lists_open_in_the_viewer(
    tmp_path: Path,
) -> None:
    cfg = fake_run(tmp_path, 30)
    kinds = ["artifact", "not_anomalous", "interesting", "known"]
    for i in range(24):
        save_verdict(tmp_path, f"a:{i + 1}", kinds[i % 4], False, "")
    assert label_main(cfg, []) == 0
    assert (tmp_path / MODEL).is_file()
    assert rank_main(cfg, ["-n", "10"]) == 0
    listed = pd.read_csv(tmp_path / "rank.csv")
    # Known objects stay off the list.
    assert not set(listed.id) & {f"a:{i + 1}" for i in range(3, 24, 4)}
    bundle = ObjectBundle(cfg, tmp_path / "rank.csv", writable=True)
    assert len(json.loads(bundle.read("index.json")[0])["tiles"]) == len(listed)
    png = np.asarray(Image.open(io.BytesIO(bundle.read("tiles/000000.png")[0])))
    # The stamp's first row is its lowest, so the viewer gets it last.
    assert png.shape == (N, N, 3)
    assert (png[-1] == 255).all()
    assert (png[0] == 0).all()
    with pytest.raises(FileNotFoundError):
        bundle.read("maps/000000.f32")


def test_one_list_can_mix_stamped_and_compact_objects(tmp_path: Path) -> None:
    cfg = fake_run(tmp_path, 3)
    tiles = np.zeros((2, N, N, 3), np.uint8)
    tiles[1] = 77
    np.save(tmp_path / "compact_stamps.npy", tiles)
    listed = offsets([0.0, 50.0], [0.0, 0.0]).assign(
        id=["a:1", "a:1000001"], field="a", tile=[np.nan, 1.0]
    )
    listed.to_csv(tmp_path / "blind.csv", index=False)
    bundle = ObjectBundle(cfg, tmp_path / "blind.csv", writable=True)
    stamped, small = (
        np.asarray(Image.open(io.BytesIO(bundle.read(f"tiles/00000{i}.png")[0])))
        for i in (0, 1)
    )
    assert (stamped[-1] == 255).all()
    assert (small == 77).all()


def test_an_exported_list_gets_a_social_card(tmp_path: Path) -> None:
    cfg = fake_run(tmp_path, 3)
    Image.new("RGB", (200, 200), (40, 40, 40)).save(tmp_path / "fields/a/field.png")
    offsets([0.0], [0.0]).assign(id=["a:1"], field="a", half=1.0).to_csv(
        tmp_path / "rank.csv", index=False
    )
    site = tmp_path / "site"
    bundle = ObjectBundle(cfg, tmp_path / "rank.csv", writable=False)
    export(bundle, site)
    (site / "index.html").write_text(
        '<html><head><title>x</title><meta name="description" content="old" />\n'
        "  </head></html>",
        encoding="utf-8",
    )
    add_site_meta(bundle, site, "https://example.org/demo")
    page = (site / "index.html").read_text(encoding="utf-8")
    assert "The 1 oddest objects in test rank" in page
    with Image.open(site / "social.png") as card:
        assert card.size == (1200, 630)


def test_cut_stops_with_the_way_round_when_gaia_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyvo

    def broken(*args: object, **kwargs: object) -> None:
        raise pyvo.dal.DALFormatError(Exception())

    monkeypatch.setattr(pyvo.dal, "TAPService", broken)
    monkeypatch.setattr("jwst_peculiar.cut.GAIA_WAITS", (0, 0))
    cfg = Config(name="t", out=tmp_path, pattern="{field}_{band}.fits")
    with pytest.raises(SystemExit, match="Set gaia: false to skip spikes"):
        gaia_stars(cfg, "a", offsets([0.0], [0.0]))


@pytest.mark.parametrize(
    ("name", "split"),
    [
        (
            "jw02736-o001_t001_nircam_clear-f090w_i2d.fits",
            ("jw02736-o001_t001", "{field}_nircam_clear-{band}_i2d.fits", "f090w"),
        ),
        (
            "jw01727-o001_t001_F444W_sci.fits",
            ("jw01727-o001_t001", "{field}_{BAND}_sci.fits", "f444w"),
        ),
        ("notes.fits", None),
    ],
)
def test_init_reads_the_field_and_filter_from_mast_and_survey_names(
    name: str, split: tuple[str, str, str] | None
) -> None:
    assert split_name(name) == split


def test_init_keeps_the_bands_every_field_has(tmp_path: Path) -> None:
    folder = tmp_path / "mosaics"
    folder.mkdir()
    for field in ("a1", "b2"):
        for b in ("f090w", "f150w", "f200w", "f277w", "f356w", "f444w"):
            (folder / f"{field}_nircam_clear-{b}_i2d.fits").touch()
    (folder / "a1_nircam_clear-f115w_i2d.fits").touch()
    keys = guess(folder)
    assert keys["pattern"] == "mosaics/{field}_nircam_clear-{band}_i2d.fits"
    assert keys["bands"] == ["f090w", "f150w", "f277w", "f444w"]
    assert keys["colour"] == ["f277w", "f150w", "f090w"]
    assert "fields" not in keys


def test_photometry_drops_cover_where_a_band_has_no_pixels() -> None:
    t = pd.DataFrame({"ra": [RA0, RA0 + 0.01], "dec": [DEC0, DEC0], "cover": True})
    blank = np.array([[RA0 + d, DEC0 + d] for d in np.linspace(-2e-4, 2e-4, 9)])
    photometry(t, band(np.zeros((60, 60), np.float32) + 1), name="f444w", blank=blank)
    assert t.cover.tolist() == [True, False]


def test_a_compact_candidate_near_a_mosaic_edge_is_flagged() -> None:
    img = np.ones((200, 200), np.float32)
    img[:, :20] = np.nan
    b = band(img)
    x = np.array([30.0, 100.0, 196.0])
    ra, dec = b.wcs.pixel_to_world_values(x, np.full(3, 100.0))
    t = pd.DataFrame({"ra": ra, "dec": dec})
    assert near_edge(t, dict.fromkeys(BANDS, b)).tolist() == [True, False, True]


def test_check_says_when_the_flux_columns_will_not_be_in_njy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for b in ("f115w", "f150w", "f277w", "f444w"):
        unit = "10*nJy" if b == "f444w" else "MJy/sr"
        hdu = fits.ImageHDU(np.ones((4, 4), np.float32), fits.Header({"BUNIT": unit}))
        fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(tmp_path / f"a_{b}.fits")
    cfg = Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{{field}}_{{band}}.fits",
        device="cpu",
    )
    report(cfg)
    assert "units are 10*nJy, MJy/sr rather than MJy/sr" in capsys.readouterr().out


def test_check_says_there_is_no_compact_list_without_f115w(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{{field}}_{{band}}.fits",
        bands=("f090w", "f150w", "f277w", "f444w"),
        colour=("f277w", "f150w", "f090w"),
        fields=("a",),
        device="cpu",
    )
    report(cfg)
    said = capsys.readouterr().out
    assert "no compact list, as it needs f115w, f150w, f277w, f444w" in said


def test_detection_splits_a_blend_and_remembers_its_parent() -> None:
    sky = gaussian(90, 100, sigma=4, amp=80) + gaussian(104, 100, sigma=4, amp=60)
    noise = np.random.default_rng(2).normal(0, 1, sky.shape).astype(np.float32)
    seg, _ = detect(band(sky + noise))
    assert len(seg) == 2
    assert seg.parent.tolist()[0] == seg.parent.tolist()[1]


def test_a_failed_check_stops_run_before_anything_is_cut(tmp_path: Path) -> None:
    (tmp_path / "a_f277w.fits").touch()
    cfg = Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{{field}}_{{band}}.fits",
        device="cpu",
    )
    assert run(cfg, []) == 1
    assert not (tmp_path / "run" / "fields").exists()


def test_check_spots_a_mosaic_whose_download_stopped_early(tmp_path: Path) -> None:
    for b in ("f115w", "f150w", "f277w", "f444w"):
        hdu = fits.ImageHDU(np.ones((100, 100), np.float32))
        fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(tmp_path / f"a_{b}.fits")
    short = tmp_path / "a_f277w.fits"
    short.write_bytes(short.read_bytes()[:20000])
    cfg = Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{{field}}_{{band}}.fits",
        device="cpu",
    )
    said = [p for p in report(cfg) if "stopped early" in p]
    assert said == [
        f"{short} is cut short or empty, so its download probably stopped early"
    ]


def test_init_finds_every_tile_when_tiles_follow_the_filter(tmp_path: Path) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    for tile in ("A1", "A10", "B4"):
        for b in BANDS:
            (
                folder / f"mosaic_nircam_{b}_COSMOS-Web_60mas_{tile}_v1.0_i2d.fits"
            ).touch()
    keys = guess(folder)
    assert (
        keys["pattern"]
        == "m/mosaic_nircam_{band}_COSMOS-Web_60mas_{field}_v1.0_i2d.fits"
    )
    assert "fields" not in keys
    assert "hdu" not in keys


def test_init_and_check_take_gzipped_mosaics_with_the_image_first(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    for b in ("f150w", "f277w", "f444w"):
        name = f"egs-grizli-v7.2-{b}-clear_drc_sci.fits.gz"
        fits.PrimaryHDU(np.ones((40, 40), np.float32)).writeto(folder / name)
    keys = guess(folder)
    assert keys["pattern"] == "m/{field}-{band}-clear_drc_sci.fits.gz"
    assert keys["fields"] == ["egs-grizli-v7.2"]
    assert keys["hdu"] == 0
    cfg = Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{keys['pattern']}",
        bands=("f150w", "f277w", "f444w"),
        colour=("f444w", "f277w", "f150w"),
        fields=("egs-grizli-v7.2",),
        hdu=0,
        device="cpu",
    )
    assert not [p for p in report(cfg) if "stopped early" in p]


def test_init_ignores_weight_and_error_maps(tmp_path: Path) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    for kind in ("err", "sci", "wht"):
        for b in BANDS:
            (folder / f"jw01727-o001_t001_{b}_{kind}.fits").touch()
    assert guess(folder)["pattern"] == "m/{field}_{band}_sci.fits"


def test_init_keeps_band_names_in_capitals_when_the_files_have_them(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    for b in BANDS:
        (folder / f"jw01727-o001_t001_{b.upper()}_sci.fits").touch()
    keys = guess(folder)
    assert keys["pattern"] == "m/{field}_{BAND}_sci.fits"
    assert keys["bands"] == list(BANDS)


def mosaic(path: Path, nx: int, ny: int) -> None:
    w = band(np.zeros((ny, nx), np.float32)).wcs
    hdu = fits.ImageHDU(np.ones((ny, nx), np.float32), w.to_header())
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path)


def tiled(tmp_path: Path, tile: int) -> Config:
    for b in BANDS:
        mosaic(tmp_path / f"a_{b}.fits", 1600, 700)
    return Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{{field}}_{{band}}.fits",
        device="cpu",
        tile=tile,
    )


def test_tiles_overlap_so_an_object_on_a_border_is_whole(tmp_path: Path) -> None:
    cfg = tiled(tmp_path, 512)
    assert cfg.field_names() == [f"a~{r}-{c}" for r in range(2) for c in range(4)]
    assert cfg.band_path("a~1-3", "f150w") == tmp_path / "a_f150w.fits"
    assert cfg.core("a~1-3") == (1536, 1600, 512, 700)
    # 12" is 400 pixels, and the box starts on the 64-pixel background mesh
    assert cfg.boxes("a~1-1") == dict.fromkeys(BANDS, (64, 1426, 64, 700))


def test_check_says_what_tile_size_would_fit_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tiled(tmp_path, 0)
    monkeypatch.setattr("jwst_peculiar.check.memory_size", lambda: 1e9)
    monkeypatch.setattr("jwst_peculiar.check.BYTES_PER_PIXEL", 1e5)
    said = [p for p in report(cfg) if "tile:" in p]
    assert said == [
        (
            "cut needs about 448 GB for a and this machine has 1 GB,"
            " so add tile: 1000 to the config to cut it in pieces"
        )
    ]


def test_trails_are_flagged_but_spiky_stars_and_galaxies_are_not() -> None:
    y, x = np.mgrid[:200, :200]
    noise = np.random.default_rng(3).normal(0, 1, (200, 200))
    star = gaussian(100, 100, sigma=1.5, amp=100) + noise
    for k in range(6):
        a = np.radians(30 + 60 * k)
        along = (x - 100) * np.cos(a) + (y - 100) * np.sin(a)
        across = abs(-(x - 100) * np.sin(a) + (y - 100) * np.cos(a))
        star += np.where((across < 1) & (along > 0), 2.0, 0.0)
    cases = {
        "trail": noise + np.where(abs(y - 100) < 1, 3.0, 0.0),
        "star with spikes": star,
        "wide band of light": noise + np.where(abs(y - 100) < 10, 3.0, 0.0),
        "sky": noise,
    }
    t = pd.DataFrame({"ra": [RA0], "dec": [DEC0]})
    flagged = {
        k: bool(on_line(t, {"f115w": band(v.astype(np.float32))})[0])
        for k, v in cases.items()
    }
    assert flagged == {
        "trail": True,
        "star with spikes": False,
        "wide band of light": False,
        "sky": False,
    }


def test_cut_runs_as_many_fields_at_once_as_cores_and_memory_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tiled(tmp_path, 512)
    names = cfg.field_names()
    # the biggest tile reads 3.5 Mpx in all, about 106 MB at 31 bytes a pixel
    monkeypatch.setattr("os.cpu_count", lambda: 16)
    monkeypatch.setattr("jwst_peculiar.cut.memory_size", lambda: 1e12)
    assert fitting_workers(cfg, names) == 4
    monkeypatch.setattr("jwst_peculiar.cut.memory_size", lambda: 0.13e9)
    assert fitting_workers(cfg, names) == 1
    assert fitting_workers(cfg, names[:1]) == 1


def test_a_faint_red_source_counts_as_seen_in_f277w_at_2_sigma() -> None:
    # S/N per band in the 0.1" aperture, F115W to F444W
    # and F277W's 0.25" flux, mostly a galaxy's for a hot pixel on its outskirts
    rows = {
        "red dot": ([0.5, 0.7, 3.0, 15.0], 5.0),
        "F444W hot pixel": ([0.5, 0.7, 0.4, 15.0], 1.0),
        "F444W hot pixel on a galaxy": ([0.5, 0.7, 2.4, 15.0], 20.0),
    }
    t = pd.DataFrame({
        f"{k}_{b}_10": [v[0][i] if k == "f" else 1.0 for v in rows.values()]
        for i, b in enumerate(BANDS)
        for k in ("f", "e")
    })
    t["f_f277w_25"] = [v[1] for v in rows.values()]
    assert dict(zip(rows, one_band(t).tolist(), strict=True)) == {
        "red dot": False,
        "F444W hot pixel": True,
        "F444W hot pixel on a galaxy": True,
    }


def test_a_faint_red_candidate_is_not_flagged_for_its_own_f444w_peak() -> None:
    # S/N per band in the 0.1" aperture, F115W to F444W, its place and its F444W peak's
    rows = {
        "red dot, peak at its centre": ([0.5, 0.7, 3.0, 15.0], 4.0, 0.03),
        "red dot, peak 0.2 arcsec off": ([0.5, 0.7, 3.0, 15.0], 8.0, 0.2),
        "blue galaxy, peak at its centre": ([15.0, 10.0, 5.0, 3.0], 12.0, 0.03),
    }
    t = offsets([v[1] for v in rows.values()], [0.0] * len(rows))
    for i, b in enumerate(BANDS):
        t[f"f_{b}_10"] = [v[0][i] for v in rows.values()]
        t[f"f_{b}_25"] = t[f"f_{b}_10"] * 2
        t[f"e_{b}_10"] = 1.0
    peaks = offsets([v[1] + v[2] for v in rows.values()], [0.0] * len(rows))
    lone = peaks.assign(band="f444w")
    bands = {b: band(np.zeros((800, 800), np.float32)) for b in BANDS}
    assert dict(zip(rows, bad_pixel(t, bands, lone).tolist(), strict=True)) == {
        "red dot, peak at its centre": False,
        "red dot, peak 0.2 arcsec off": True,
        "blue galaxy, peak at its centre": True,
    }


def test_a_faint_star_is_flagged_and_a_brown_dwarf_is_not() -> None:
    # 0.1" fluxes F115W to F444W in nJy, and F150W's 0.25" flux
    rows = {
        "star": ([40.0, 50.0, 15.0, 8.0], 60.0),
        "brown dwarf": ([10.0, 20.0, 15.0, 90.0], 24.0),
        "blue galaxy": ([40.0, 50.0, 15.0, 8.0], 150.0),
    }
    t = pd.DataFrame({
        f"f_{b}_10": [v[0][i] for v in rows.values()] for i, b in enumerate(BANDS)
    })
    t["f_f150w_25"] = [v[1] for v in rows.values()]
    assert dict(zip(rows, blue_star(t).tolist(), strict=True)) == {
        "star": True,
        "brown dwarf": False,
        "blue galaxy": False,
    }


@pytest.mark.parametrize(
    ("cached", "offline"),
    [({"config.json", "pytorch_model.bin"}, True), ({"config.json"}, False)],
)
def test_cached_weights_load_without_asking_the_hub(
    monkeypatch: pytest.MonkeyPatch, cached: set[str], *, offline: bool
) -> None:
    import huggingface_hub
    from huggingface_hub import constants
    import timm
    import torch

    from jwst_peculiar.embed import load_encoder

    seen = []

    def create_model(*_: object, **__: object) -> torch.nn.Module:
        seen.append(constants.HF_HUB_OFFLINE)
        return torch.nn.Identity()

    def from_cache(_: str, name: str) -> str | None:
        return f"/cache/{name}" if name in cached else None

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", from_cache)
    monkeypatch.setattr(timm, "create_model", create_model)
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
    load_encoder("nano", torch.device("cpu"))
    assert seen == [offline]
    assert constants.HF_HUB_OFFLINE is False


@pytest.mark.parametrize("step", ["cut", "run"])
def test_a_steps_options_show_without_a_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    with pytest.raises(SystemExit) as done:
        main([step, str(tmp_path / "none.yaml"), "--help"])
    assert done.value.code == 0
    assert "--workers" in capsys.readouterr().out


@pytest.fixture
def listed(tmp_path: Path) -> Config:
    """Three objects of one field in rank.csv, with the field's preview.

    Returns:
        The run's config.
    """
    cfg = fake_run(tmp_path, 3)
    Image.new("RGB", (200, 200), (40, 40, 40)).save(tmp_path / "fields/a/field.png")
    offsets([0.0, 100.0, 200.0], [0.0] * 3).assign(
        id=["a:1", "a:2", "a:3"], field="a", half=1.0
    ).to_csv(tmp_path / "rank.csv", index=False)
    return cfg


@pytest.fixture
def port(listed: Config) -> Iterator[int]:
    handler = make_handler(ObjectBundle(listed, listed.out / "rank.csv", writable=True))
    # short, so a request that stalls fails the test quickly
    handler.timeout = 1
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port
    server.shutdown()
    server.server_close()


def _ask(
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    **headers: str,
) -> tuple[int, bytes]:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    out = res.status, res.read()
    conn.close()
    return out


def _raw(port: int, request: bytes) -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(request)
        return int(s.recv(1024).split()[1])


def test_its_own_page_reads_and_writes(port: int, listed: Config) -> None:
    status, body = _ask(port, "GET", "/run/index.json")
    assert status == 200
    assert len(json.loads(body)["tiles"]) == 3
    here = f"http://127.0.0.1:{port}"
    verdict = {"key": "a:3", "verdict": "artifact"}
    status, _ = _ask(port, "POST", "/run/api/verdict", body=verdict, Origin=here)
    assert status == 200
    assert read_verdicts(listed.out)["a:3"]["verdict"] == "artifact"
    status, _ = _ask(port, "POST", "/run/api/verdict", body={"key": "a:9"})
    assert status == 400


def test_another_site_gets_nothing(port: int, listed: Config) -> None:
    # a page elsewhere posting to localhost, which a browser allows
    verdict = {"key": "a:3", "verdict": "artifact"}
    status, _ = _ask(
        port, "POST", "/run/api/verdict", body=verdict, Origin="https://example.org"
    )
    assert status == 403
    assert read_verdicts(listed.out) == {}
    # a domain pointed at 127.0.0.1 still names itself in Host
    status, _ = _ask(port, "GET", "/run/index.json", Host=f"example.org:{port}")
    assert status == 403
    assert _raw(port, b"GET /run/index.json HTTP/1.0\r\n\r\n") == 403
    assert _ask(port, "GET", "/run/index.json", Origin="null")[0] == 403
    # a link or image on another site carries no Origin on a GET
    for site, want in (("cross-site", 403), ("same-origin", 200)):
        request = f"GET /run/index.json HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n"
        request += f"Sec-Fetch-Site: {site}\r\n\r\n"
        assert _raw(port, request.encode()) == want


def test_paths_stay_inside_the_viewer_and_the_list(port: int) -> None:
    # each of these is a way of asking for server.py, one folder above the viewer's
    here = pathlib.Path(server.__file__).resolve()
    assert (server.APP_DIR / ".." / "server.py").resolve() == here
    assert _ask(port, "GET", "/run/mosaics/a.png")[0] == 200
    for path in (
        "/../server.py",
        "/%2e%2e/server.py",
        "/%2E%2E%2Fserver.py",
        "/..%5Cserver.py",
        "/assets/../../server.py",
        "/" + quote(str(here)),
        "/run/../server.py",
        "/run/mosaics/a%00.png",
    ):
        want = {"error": f"There is nothing at {unquote(path).lstrip('/')}"}
        assert _ask(port, "GET", path) == (404, json.dumps(want).encode()), path


def test_a_bad_body_is_refused(port: int, listed: Config) -> None:
    body = b'{"key": "a:3"}'
    head = f"POST /run/api/verdict HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n"
    head += f"Content-Length: {len(body) + 10}\r\n\r\n"
    assert _raw(port, head.encode() + body) == 400
    verdict = {"key": "a:3", "verdict": None, "star": "false"}
    status, said = _ask(port, "POST", "/run/api/verdict", body=verdict)
    assert (status, json.loads(said)) == (
        400,
        {"error": "star has to be true or false, not 'false'"},
    )
    assert read_verdicts(listed.out) == {}


def test_verdicts_saved_by_a_spreadsheet_still_load(port: int, listed: Config) -> None:
    # Excel's "CSV UTF-8" starts the file with a byte order mark, and a column of
    # your own has to survive the next save
    (listed.out / "verdicts.csv").write_bytes(
        codecs.BOM_UTF8
        + b"key,verdict,star,note,updated,mine\r\n"
        + b"a:1,artifact,1,x,2026-01-01,keep me\r\n"
    )
    status, body = _ask(port, "GET", "/run/verdicts.json")
    assert status == 200
    assert json.loads(body)["a:1"] == {
        "mine": "keep me",
        "verdict": "artifact",
        "star": True,
        "note": "x",
        "updated": "2026-01-01",
    }
    verdict = {"key": "a:3", "verdict": "known", "star": False, "note": ""}
    assert _ask(port, "POST", "/run/api/verdict", body=verdict)[0] == 200
    save_verdict(listed.out, "a:1", None, False, "")
    with (listed.out / "verdicts.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["key", "verdict", "star", "note", "updated", "mine"]
    assert rows[1][:4] == ["a:1", "", "0", ""]
    assert rows[1][5] == "keep me"
    assert rows[2][:4] == ["a:3", "known", "0", ""]
    with pytest.raises(ValueError, match="Unknown verdict"):
        save_verdict(listed.out, "a:1", "odd", False, "")


def test_the_page_hears_why_verdicts_did_not_load(port: int, listed: Config) -> None:
    (listed.out / "verdicts.csv").write_text("tile,verdict\na:1,known\n", "utf-8")
    status, body = _ask(port, "GET", "/run/verdicts.json")
    assert (status, json.loads(body)) == (
        500,
        {"error": "Couldn't read run/verdicts.json: KeyError: 'key'"},
    )


def test_export_leaves_a_folder_of_other_files_alone(
    listed: Config, tmp_path: Path
) -> None:
    b = ObjectBundle(listed, listed.out / "rank.csv", writable=False)
    mine = tmp_path / "mine"
    (mine / "run").mkdir(parents=True)
    (mine / "run" / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(SystemExit, match="isn't empty and isn't an earlier export"):
        export(b, mine)
    assert (mine / "run" / "notes.txt").read_text(encoding="utf-8") == "mine"
    # an earlier export is replaced, as is an empty folder
    site = tmp_path / "site"
    site.mkdir()
    first = export(b, site)
    assert export(b, site) == first


def test_site_meta_takes_any_name_and_url(listed: Config, tmp_path: Path) -> None:
    b = ObjectBundle(listed, listed.out / "rank.csv", writable=False)
    b.index["run"]["name"] = "COSMOS\\d field</script>"
    site = tmp_path / "site"
    export(b, site)
    (site / "index.html").write_text(
        "<html><head><title>x</title>\n  </head></html>", encoding="utf-8"
    )
    add_site_meta(b, site, "https://example.org/a?x=1&y=2")
    page = (site / "index.html").read_text(encoding="utf-8")
    assert "<title>COSMOS\\d field&lt;/script&gt; · jwst-peculiar</title>" in page
    assert page.count("</script>") == 1
    sitemap = (site / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://example.org/a?x=1&amp;y=2/</loc>" in sitemap


def test_social_card_keeps_off_a_blank_gap() -> None:
    lit = np.r_[np.full(10, 0.4), np.zeros(5), np.full(30, 0.4)]
    lo, hi = lit_span(lit, 20, 20)
    assert 12 < lo <= 15
    assert hi == 45
    # too short a run to fit the card falls back to the whole axis
    assert lit_span(lit, 20, 40) == (0, 45)


def test_a_second_viewer_moves_to_a_free_port(listed: Config) -> None:
    served = ObjectBundle(listed, listed.out / "rank.csv", writable=False)
    first = open_server(served, 0)
    port = first.server_port
    try:
        second = open_server(served, port)
        assert second.server_port not in {0, port}
        second.server_close()
    finally:
        first.server_close()


def test_a_band_with_no_data_has_no_sources() -> None:
    nan = np.full((300, 300), np.nan, np.float32)
    with pytest.raises(NoData):
        detect(Band(nan, nan, nan, None, 0.04, None))
    some = nan.copy()
    some[:3, :3] = 0.1
    with pytest.raises(NoData):
        detect(Band(some, some, np.ones_like(some), None, 0.04, None))


def half_blank(tmp_path: Path) -> Config:
    # galaxies on the left of a 1500 by 600 mosaic, and nothing at all on its right
    rng = np.random.default_rng(3)
    y, x = np.mgrid[:600, :1500]
    sky = rng.normal(0, 1, (600, 1500)).astype(np.float32)
    for i in range(40):
        x0, y0, r = 30 + 12 * i, 40 + (i * 37) % 520, 2 + i % 5
        sky += 400 * np.exp(-((x - x0) ** 2 + (y - y0) ** 2) / (2 * r**2))
    # and one too small for a stamp in the next tile along
    sky += 30 * np.exp(-((x - 600) ** 2 + (y - 250) ** 2) / (2 * 1.5**2))
    sky[:, 700:] = 0
    w = band(sky).wcs
    for b in BANDS:
        data = sky.copy()
        if b == "f150w":
            # a hot pixel in one band, in the tile with no objects
            data[250, 640] = 5000
        hdu = fits.ImageHDU(data, w.to_header())
        fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(tmp_path / f"a_{b}.fits")
    return Config(
        name="t",
        out=tmp_path / "run",
        pattern=f"{tmp_path.as_posix()}/{{field}}_{{band}}.fits",
        fields=("a",),
        gaia=False,
        tile=500,
    )


def test_a_tile_off_the_mosaic_is_left_out_of_every_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = dataclasses.replace(half_blank(tmp_path), gaia=True)
    asked: list[str] = []

    def a_star(q: str, _name: str) -> pd.DataFrame:
        # one bright star in the middle of the box asked for
        asked.append(q)
        (r0, r1), (d0, d1) = re.findall(r"BETWEEN (\S+) AND (\S+)", q)
        return pd.DataFrame({
            "ra": [(float(r0) + float(r1)) / 2],
            "dec": [(float(d0) + float(d1)) / 2],
            "phot_g_mean_mag": [15.0],
            "classprob_dsc_combmod_star": [1.0],
        })

    monkeypatch.setattr(cut, "_query_gaia", a_star)
    assert cut_main(cfg, ["--workers", "1"]) == 0
    # a tile with segments but no stamps still asks Gaia over where they are
    assert asked
    assert not [q for q in asked if "nan" in q]
    root = cfg.out / "fields"
    assert (root / "a~0-2" / EMPTY).is_file()
    # this one only holds the edges of sources centred in its neighbours
    assert (root / "a~1-1" / EMPTY).is_file()
    assert cfg.cut_fields() == ["a~0-0", "a~0-1", "a~1-0"]
    assert len(pd.read_csv(root / "a~0-1" / "objects.csv")) == 0
    # the mean colour stands in for Zoobot
    net = torch.nn.Sequential(torch.nn.AdaptiveAvgPool2d(1), torch.nn.Flatten())
    for f in cfg.cut_fields():
        stamps = np.load(root / f / "stamps.npy")
        rows = embed(stamps, net, device=torch.device("cpu"))
        assert rows.shape == (len(stamps), 3)
        np.save(
            root / f / "embeddings.npy",
            rows + 0.01 * np.arange(rows.size).reshape(rows.shape),
        )
    assert rank_main(cfg, ["-n", "5"]) == 0
    assert len(pd.read_csv(cfg.out / "rank.csv")) == 5
    assert compact_main(cfg, ["-n", "5"]) == 0
    assert len(pd.read_csv(cfg.out / "compact.csv")) == 5
    # a second cut leaves the empty tiles be
    assert cut_main(cfg, ["--workers", "1"]) == 0


def test_one_field_failing_leaves_the_rest_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = half_blank(tmp_path)

    def cut_or_fail(_cfg: Config, name: str, *, redo: bool) -> str:
        assert not redo
        if name == "a~0-1":
            msg = "broken"
            raise ValueError(msg)
        return f"{name}: cut"

    monkeypatch.setattr(cut, "cut_field", cut_or_fail)
    with pytest.raises(SystemExit, match="a~0-1: broken"):
        cut_main(cfg, ["--workers", "1"])
    assert "a~1-1: cut" in capsys.readouterr().out
