"""`rank`, `like` and `label`: rarity, search by example and the label model.

Rarity is one minus the cosine similarity to the fifth nearest object that shares no
sky with it, so the crops of one galaxy never vouch for each other.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sklearn.linear_model import LogisticRegression

from jwst_peculiar.config import read_config
from jwst_peculiar.verdicts import read_verdicts

if TYPE_CHECKING:
    import numpy.typing as npt

    from jwst_peculiar.config import Config

    Floats = npt.NDArray[np.float64]

K = 5
MODEL = "label_model.npz"
# What each verdict given in the viewer teaches the label model. A known odd object
# teaches as much about what's worth a look as a new one does.
CLASSES = {"artifact": 0, "not_anomalous": 1, "interesting": 2, "known": 2}
WORTH = 2
SAMPLE_FROM, SAMPLE_TO = 100, 2000
# verdicts of each kind label needs before it fits
MIN_VERDICTS = 5
# An object a brighter neighbour outshines is still a real galaxy, so it stays a
# neighbour for rarity and is only kept off the list.
HIDE_ONLY = ("flag_outshone",)


@dataclass
class Objects:
    """Every cut object in a run, with its embedding and its place on the sky."""

    t: pd.DataFrame
    e: npt.NDArray[np.float32]

    @property
    def flagged(self) -> npt.NDArray[np.bool_]:
        """Say which objects any test caught."""
        return self.t.filter(like="flag_").to_numpy().any(axis=1)

    @property
    def pool(self) -> npt.NDArray[np.bool_]:
        """Say which objects count as neighbours, the ones no artefact test caught."""
        rules = self.t.filter(like="flag_").drop(
            columns=list(HIDE_ONLY), errors="ignore"
        )
        return ~rules.to_numpy().any(axis=1)


def load(cfg: Config, encoder: str = "nano") -> Objects:
    """Read every field's objects and embeddings."""
    name = "embeddings.npy" if encoder == "nano" else f"embeddings_{encoder}.npy"
    cats, embs = [], []
    for field in cfg.cut_fields():
        folder = cfg.out / "fields" / field
        if not (folder / name).exists():
            msg = f"{field} has no {name}. Run jwst-peculiar embed first."
            raise SystemExit(msg)
        cat = pd.read_csv(folder / "objects.csv", dtype={"field": str})
        # a tile with no objects reads back untyped, which would turn every column
        # into Python objects
        if len(cat):
            cats.append(cat)
            embs.append(np.load(folder / name))
    if not cats:
        msg = "No field has any objects."
        raise SystemExit(msg)
    t = pd.concat(cats, ignore_index=True)
    t["id"] = t.field + ":" + t.label.astype(str)
    return Objects(t, np.concatenate(embs).astype(np.float32))


def on_sphere(
    ra: Floats | pd.Series[float] | list[float],
    dec: Floats | pd.Series[float] | list[float],
) -> Floats:
    """Return points on a sphere of radius one radian in arcsec."""
    a, d = np.radians(np.asarray(ra, float)), np.radians(np.asarray(dec, float))
    unit = np.column_stack([np.cos(d) * np.cos(a), np.cos(d) * np.sin(a), np.sin(d)])
    return np.asarray(unit * np.degrees(1) * 3600, float)


class Sky:
    """Which objects share sky, so one galaxy's crops never vouch for each other."""

    def __init__(self, t: pd.DataFrame) -> None:
        self.xy = on_sphere(t.ra, t.dec)
        self.half = t.half.to_numpy()
        self.tree = cKDTree(self.xy)
        self.reach = float(self.half.max(initial=0))

    def near(self, i: int) -> list[int]:
        """Return the objects closer to `i` than the larger of their two half-widths."""
        hits = self.tree.query_ball_point(self.xy[i], max(self.half[i], self.reach))
        d = np.hypot(*(self.xy[hits] - self.xy[i]).T)
        lim = np.maximum(self.half[i], self.half[hits])
        return [int(j) for j, dj, lj in zip(hits, d, lim, strict=True) if dj < lj]

    def unique(self, order: npt.NDArray[np.intp], n: int) -> list[int]:
        """Walk an ordering and keep the first object of each system, up to `n`."""
        keep: list[int] = []
        taken: set[int] = set()
        for i in order:
            if int(i) in taken:
                continue
            keep.append(int(i))
            taken.update(self.near(int(i)))
            if len(keep) == n:
                break
        return keep


def rarity(
    e: npt.NDArray[np.float32],
    sky: Sky,
    *,
    block: int = 1024,
    ref: npt.NDArray[np.float32] | None = None,
) -> Floats:
    """Return one minus the cosine to the K-th nearest object sharing no sky."""
    others = e if ref is None else np.concatenate([e, ref])
    if len(others) <= 1:
        return np.ones(len(e))
    # a block of similarities stays near 200 MB however big the reference is
    block = max(16, min(block, 50_000_000 // len(others)))
    # a run of a handful of objects makes do with the farthest neighbour it has
    k = min(K, len(others) - 1)
    out = np.empty(len(e))
    for i in range(0, len(e), block):
        s = e[i : i + block] @ others.T
        for a in range(len(s)):
            s[a, sky.near(i + a)] = -1
        part = np.partition(s, -k, axis=1)[:, -k]
        out[i : i + block] = 1 - part
    return out


def read_labels(cfg: Config) -> pd.DataFrame:
    """Return the verdicts given in the viewer, as id and verdict."""
    saved = read_verdicts(cfg.out)
    verdicts = [r["verdict"] or "" for r in saved.values()]
    return pd.DataFrame({"id": list(saved), "label": verdicts}, dtype=str)


def fit(e: npt.NDArray[np.float32], y: npt.NDArray[np.intp]) -> dict[str, Any]:
    """Fit the label model, a balanced multinomial logistic regression with C 0.1."""
    clf = LogisticRegression(C=0.1, class_weight="balanced", max_iter=5000).fit(e, y)
    return {"coef": clf.coef_, "intercept": clf.intercept_, "classes": clf.classes_}


def p_worth(model: dict[str, Any], e: npt.NDArray[np.float32]) -> Floats:
    """Return each row's chance of being worth a look under the model."""
    z = e @ model["coef"].T + model["intercept"]
    z -= z.max(axis=1, keepdims=True)
    p = np.exp(z)
    p /= p.sum(axis=1, keepdims=True)
    return np.asarray(p[:, list(model["classes"]).index(WORTH)], float)


def reference(obj: Objects, other: Objects) -> npt.NDArray[np.float32]:
    """Return the embeddings of another run's neighbours, less any on this run's sky."""
    sky = Sky(obj.t)
    pool = other.t.loc[other.pool]
    xy = on_sphere(pool.ra, pool.dec)
    half = np.maximum(pool.half.to_numpy(), sky.reach)
    d, _ = sky.tree.query(xy, distance_upper_bound=float(half.max(initial=0)))
    away = d > half
    return np.asarray(other.e[other.pool][away], np.float32)


def scores(
    cfg: Config, obj: Objects, ref: npt.NDArray[np.float32] | None = None
) -> tuple[Floats, Floats | None]:
    """Return rarity, and rarity times the chance of worth a look if there's a model."""
    pool = obj.pool
    rar = np.full(len(pool), -np.inf)
    sky = Sky(obj.t.loc[pool].reset_index(drop=True))
    rar[pool] = rarity(obj.e[pool], sky, ref=ref)
    path = cfg.out / MODEL
    if not path.exists():
        return rar, None
    model = dict(np.load(path))
    return rar, np.where(pool, rar * p_worth(model, obj.e), -np.inf)


def ranked(cfg: Config, obj: Objects, score: Floats, *, n: int) -> list[int]:
    """Return the top `n` systems by `score`, leaving out flagged and known objects."""
    lab = read_labels(cfg)
    known = obj.t.id.isin(set(lab.id[lab.label == "known"])).to_numpy()
    shown = np.where(obj.flagged | known, -np.inf, score)
    return [
        i for i in Sky(obj.t).unique(np.argsort(-shown), n) if np.isfinite(shown[i])
    ]


def write_list(cfg: Config, obj: Objects, score: Floats, *, n: int) -> Path:
    """Write the top `n` systems by `score` to rank.csv."""
    top = ranked(cfg, obj, score, n=n)
    cols = ["id", "field", "ra", "dec", "half", "sn", "a_arcsec"]
    out = obj.t.iloc[top][cols].assign(rank=range(1, len(top) + 1), score=score[top])
    path = cfg.out / "rank.csv"
    out.to_csv(path, index=False)
    return path


def write_sample(cfg: Config, obj: Objects, score: Floats, *, n: int) -> Path:
    """Write `n` systems drawn at random from places 101 to 2,000 to sample.csv."""
    judged = set(read_labels(cfg).id)
    order = ranked(cfg, obj, score, n=SAMPLE_TO)
    pool = [
        (place, i)
        for place, i in enumerate(order, start=1)
        if place > SAMPLE_FROM and obj.t.id.iloc[i] not in judged
    ]
    rng = np.random.default_rng(len(judged))
    picks = sorted(rng.choice(len(pool), min(n, len(pool)), replace=False))
    places = [pool[k][0] for k in picks]
    rows = [pool[k][1] for k in picks]
    cols = ["id", "field", "ra", "dec", "half", "sn", "a_arcsec"]
    out = obj.t.iloc[rows][cols].assign(rank=places, score=score[rows])
    path = cfg.out / "sample.csv"
    out.to_csv(path, index=False)
    return path


def rank_main(cfg: Config, argv: list[str]) -> int:
    """Write the rarity list, or a random sample to label."""
    p = argparse.ArgumentParser(prog="jwst-peculiar rank CONFIG")
    p.add_argument("-n", type=int, default=500, help="how many systems to list")
    p.add_argument(
        "--image-only", action="store_true", help="plain rarity, no label model"
    )
    p.add_argument(
        "--sample",
        action="store_true",
        help="write n systems from places 101 to 2,000 at random to sample.csv",
    )
    p.add_argument(
        "--reference",
        type=Path,
        help="another run's config whose objects count as neighbours too",
    )
    a = p.parse_args(argv)
    obj = load(cfg)
    ref = None
    if a.reference:
        ref = reference(obj, load(read_config(a.reference)))
        print(f"{len(ref)} reference objects")
    rar, weighted = scores(cfg, obj, ref)
    use = rar if a.image_only or weighted is None else weighted
    write = write_sample if a.sample else write_list
    path = write(cfg, obj, use, n=a.n)
    print(f"{len(obj.t)} objects, {int(obj.flagged.sum())} flagged, list in {path}")
    return 0


def find(obj: Objects, sky: Sky, seed: str) -> int:
    """Return the row of a seed given as FIELD:LABEL, or RA,DEC within 1 arcsec."""
    if ":" in seed:
        hit = np.flatnonzero(obj.t.id.to_numpy() == seed)
        if len(hit):
            return int(hit[0])
    else:
        ra, dec = (float(v) for v in seed.split(","))
        d, j = sky.tree.query(on_sphere([ra], [dec])[0])
        if d < 1.0:
            return int(j)
    msg = f"No object in the run matches the seed {seed}."
    raise SystemExit(msg)


def like_main(cfg: Config, argv: list[str]) -> int:
    """Write the objects most like one or more seeds, by mean cosine similarity."""
    p = argparse.ArgumentParser(prog="jwst-peculiar like CONFIG")
    p.add_argument("seeds", nargs="+", help="FIELD:LABEL ids, or RA,DEC in degrees")
    p.add_argument("-n", type=int, default=100)
    a = p.parse_args(argv)
    obj = load(cfg)
    sky = Sky(obj.t)
    rows = [find(obj, sky, s) for s in a.seeds]
    sim = (obj.e @ obj.e[rows].T).mean(axis=1)
    sim[[j for r in rows for j in sky.near(r)]] = -np.inf
    top = sky.unique(np.argsort(-sim), a.n)
    cols = ["id", "field", "ra", "dec", "half"]
    path = cfg.out / "like.csv"
    obj.t.iloc[top][cols].assign(similarity=sim[top]).to_csv(path, index=False)
    print(f"{len(top)} systems like {', '.join(a.seeds)} in {path}")
    return 0


def label_main(cfg: Config, argv: list[str]) -> int:
    """Fit the label model on the viewer's verdicts and save it in the run."""
    p = argparse.ArgumentParser(prog="jwst-peculiar label CONFIG")
    p.parse_args(argv)
    obj = load(cfg)
    lab = read_labels(cfg)
    lab = lab[lab.label.isin(list(CLASSES))]
    idx = pd.Index(obj.t.id).get_indexer(pd.Index(lab.id))
    lab, idx = lab[idx >= 0], idx[idx >= 0]
    y = np.asarray(lab.label.map(CLASSES), dtype=np.intp)
    counts = np.bincount(y, minlength=3)
    if np.min(counts) < MIN_VERDICTS:
        msg = (
            f"Need at least {MIN_VERDICTS} verdicts of each kind (artifact, not"
            " anomalous, and"
            " interesting or known),"
            f" have {counts.tolist()}."
        )
        raise SystemExit(msg)
    model = fit(obj.e[idx], y)
    np.savez(cfg.out / MODEL, **model)
    kinds = dict(zip(("bad", "ordinary", "worth"), counts.tolist(), strict=True))
    print(json.dumps({"labels": len(y), **kinds}))
    return 0


def main(cfg: Config, argv: list[str]) -> int:
    """Run rank, like or label, named first in `argv`."""
    steps = {"rank": rank_main, "like": like_main, "label": label_main}
    return steps[argv[0]](cfg, argv[1:])
