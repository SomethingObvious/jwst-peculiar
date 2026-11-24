from __future__ import annotations

import io
import json
import pathlib
import re
from typing import TYPE_CHECKING

from astropy.wcs import WCS
import numpy as np
import pandas as pd
from PIL import Image
import pytest
from sklearn.linear_model import LogisticRegression

from jwst_peculiar.bundle import add_site_meta, export, save_verdict
from jwst_peculiar.objects import Config, read_config, written
from jwst_peculiar.objects.check import report
from jwst_peculiar.objects.compact import dark_middle, one_band, one_pair, sharp
from jwst_peculiar.objects.cut import Band, N, gaia_stars, render
from jwst_peculiar.objects.flags import blobs, merged, on_spikes
from jwst_peculiar.objects.init import guess, split_name
from jwst_peculiar.objects.rank import (
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
from jwst_peculiar.objects.view import ObjectBundle
from jwst_peculiar.prep import wcs_record

if TYPE_CHECKING:
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
    (tmp_path / "data").mkdir()
    for name in ("a_f277w", "b_f277w", "c_f150w"):
        (tmp_path / "data" / f"{name}.fits").touch()
    (tmp_path / "survey.yaml").write_text(
        "name: test\nout: run\npattern: data/{field}_{band}.fits\n", encoding="utf-8"
    )
    cfg = read_config(tmp_path / "survey.yaml")
    assert cfg.out == tmp_path / "run"
    assert cfg.field_names() == ["a", "b"]
    assert cfg.band_path("a", "f150w") == tmp_path / "data" / "a_f150w.fits"


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
    assert len(bundle.read("maps/000000.f32")[0]) == 4


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
            ("jw01727-o001_t001", "{field}_{band}_sci.fits", "f444w"),
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
