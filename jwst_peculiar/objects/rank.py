"""`objects rank`, `like` and `label`: rarity, search by example and the label model.

An object's rarity is one minus the cosine similarity to its fifth nearest object
that shares no sky with it, so the other crops of one galaxy never vouch for each
other. Flagged objects are left out. Once there are labels, `label` fits a
three-class logistic regression (bad, ordinary, worth a look) on the embeddings and
`rank` multiplies rarity by the chance an object is worth a look. Ranks count
systems: an object only takes a place when it shares no sky with one placed above.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from jwst_peculiar.bundle import read_verdicts

if TYPE_CHECKING:
    from pathlib import Path

    import numpy.typing as npt

    from jwst_peculiar.objects import Config

    Floats = npt.NDArray[np.float64]

K = 5
MODEL = "label_model.npz"
# What each verdict given in the viewer teaches the label model. A known odd object
# teaches as much about what's worth a look as a new one does.
CLASSES = {"artifact": 0, "not_anomalous": 1, "interesting": 2, "known": 2}
WORTH = 2
SAMPLE_FROM, SAMPLE_TO = 100, 2000
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
    """Read every field's objects and embeddings.

    Returns:
        The objects, each with an `id` of field and segment label.

    Raises:
        SystemExit: When a field has been cut but not embedded.
    """
    name = "embeddings.npy" if encoder == "nano" else f"embeddings_{encoder}.npy"
    cats, embs = [], []
    for field in cfg.cut_fields():
        folder = cfg.out / "fields" / field
        if not (folder / name).exists():
            msg = f"{field} has no {name}. Run objects embed first."
            raise SystemExit(msg)
        cats.append(pd.read_csv(folder / "objects.csv"))
        embs.append(np.load(folder / name))
    t = pd.concat(cats, ignore_index=True)
    t["id"] = t.field + ":" + t.label.astype(str)
    return Objects(t, np.concatenate(embs).astype(np.float32))


class Sky:
    """Which objects share sky, so one galaxy's crops never vouch for each other."""

    def __init__(self, t: pd.DataFrame) -> None:
        cos = np.cos(np.radians(float(t.dec.median())))
        self.xy = np.c_[t.ra.to_numpy() * cos * 3600, t.dec.to_numpy() * 3600]
        self.half = t.half.to_numpy()
        self.tree = cKDTree(self.xy)
        self.reach = float(self.half.max())

    def near(self, i: int) -> list[int]:
        """Return the objects closer to `i` than the larger of their two half-widths."""
        hits = self.tree.query_ball_point(self.xy[i], max(self.half[i], self.reach))
        d = np.hypot(*(self.xy[hits] - self.xy[i]).T)
        lim = np.maximum(self.half[i], self.half[hits])
        return [int(j) for j, dj, lj in zip(hits, d, lim, strict=True) if dj < lj]

    def unique(self, order: npt.NDArray[np.intp], n: int) -> list[int]:
        """Walk an ordering and keep the first object of each system, up to `n`.

        Returns:
            The kept objects, in order.
        """
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


def rarity(e: npt.NDArray[np.float32], sky: Sky, block: int = 1024) -> Floats:
    """Return one minus the cosine to the K-th nearest object sharing no sky.

    Returns:
        One value per row.
    """
    out = np.empty(len(e))
    for i in range(0, len(e), block):
        s = e[i : i + block] @ e.T
        for a in range(len(s)):
            s[a, sky.near(i + a)] = -1
        part = np.partition(s, -K, axis=1)[:, -K]
        out[i : i + block] = 1 - part
    return out


def read_labels(cfg: Config) -> pd.DataFrame:
    """Return the verdicts given in the viewer, as id and verdict."""
    saved = read_verdicts(cfg.out)
    verdicts = [r["verdict"] or "" for r in saved.values()]
    return pd.DataFrame({"id": list(saved), "label": verdicts}, dtype=str)


def fit(e: npt.NDArray[np.float32], y: npt.NDArray[np.intp]) -> dict[str, Any]:
    """Fit the label model, a balanced multinomial logistic regression with C 0.1.

    Returns:
        Its weights, as arrays an npz can hold.
    """
    from sklearn.linear_model import LogisticRegression  # noqa: PLC0415 only label and rank need it

    clf = LogisticRegression(C=0.1, class_weight="balanced", max_iter=5000).fit(e, y)
    return {"coef": clf.coef_, "intercept": clf.intercept_, "classes": clf.classes_}


def p_worth(model: dict[str, Any], e: npt.NDArray[np.float32]) -> Floats:
    """Return each row's chance of being worth a look under the model."""
    z = e @ model["coef"].T + model["intercept"]
    z -= z.max(axis=1, keepdims=True)
    p = np.exp(z)
    p /= p.sum(axis=1, keepdims=True)
    return np.asarray(p[:, list(model["classes"]).index(WORTH)], float)


def scores(cfg: Config, obj: Objects) -> tuple[Floats, Floats | None]:
    """Return rarity, and rarity times the chance of worth a look if there's a model.

    Returns:
        Rarity per object, and the weighted score or None.
    """
    pool = obj.pool
    rar = np.full(len(pool), -np.inf)
    rar[pool] = rarity(obj.e[pool], Sky(obj.t.loc[pool].reset_index(drop=True)))
    path = cfg.out / MODEL
    if not path.exists():
        return rar, None
    model = dict(np.load(path))
    return rar, np.where(pool, rar * p_worth(model, obj.e), -np.inf)


def ranked(cfg: Config, obj: Objects, score: Floats, *, n: int) -> list[int]:
    """Return the top `n` systems by `score`, leaving out flagged and known objects.

    Returns:
        One object per system, best first.
    """
    lab = read_labels(cfg)
    known = obj.t.id.isin(set(lab.id[lab.label == "known"])).to_numpy()
    shown = np.where(obj.flagged | known, -np.inf, score)
    return [
        i for i in Sky(obj.t).unique(np.argsort(-shown), n) if np.isfinite(shown[i])
    ]


def write_list(cfg: Config, obj: Objects, score: Floats, *, n: int) -> Path:
    """Write the top `n` systems by `score` to rank.csv.

    Returns:
        The file written.
    """
    top = ranked(cfg, obj, score, n=n)
    cols = ["id", "field", "ra", "dec", "half", "sn", "a_arcsec"]
    out = obj.t.iloc[top][cols].assign(rank=range(1, len(top) + 1), score=score[top])
    path = cfg.out / "rank.csv"
    out.to_csv(path, index=False)
    return path


def write_sample(cfg: Config, obj: Objects, score: Floats, *, n: int) -> Path:
    """Write `n` systems drawn at random from places 101 to 2,000 to sample.csv.

    The label model learns what ordinary looks like from these, which the top of the
    list alone doesn't show it. Systems that already have a verdict are skipped.

    Returns:
        The file written.
    """
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
    """Write the rarity list, or a random sample to label.

    Returns:
        0.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar objects rank CONFIG")
    p.add_argument("-n", type=int, default=500, help="how many systems to list")
    p.add_argument(
        "--image-only", action="store_true", help="plain rarity, no label model"
    )
    p.add_argument(
        "--sample",
        action="store_true",
        help="write n systems from places 101 to 2,000 at random to sample.csv",
    )
    a = p.parse_args(argv)
    obj = load(cfg)
    rar, weighted = scores(cfg, obj)
    use = rar if a.image_only or weighted is None else weighted
    write = write_sample if a.sample else write_list
    path = write(cfg, obj, use, n=a.n)
    print(f"{len(obj.t)} objects, {int(obj.flagged.sum())} flagged, list in {path}")  # noqa: T201 the command's result
    return 0


def find(obj: Objects, sky: Sky, seed: str) -> int:
    """Return the row of a seed given as FIELD:LABEL, or RA,DEC within 1 arcsec.

    Raises:
        SystemExit: When no object matches.
    """
    if ":" in seed:
        hit = np.flatnonzero(obj.t.id.to_numpy() == seed)
        if len(hit):
            return int(hit[0])
    else:
        ra, dec = (float(v) for v in seed.split(","))
        cos = np.cos(np.radians(float(obj.t.dec.median())))
        d, j = sky.tree.query([ra * cos * 3600, dec * 3600])
        if d < 1.0:
            return int(j)
    msg = f"No object in the run matches the seed {seed}."
    raise SystemExit(msg)


def like_main(cfg: Config, argv: list[str]) -> int:
    """Write the objects most like one or more seeds, by mean cosine similarity.

    Returns:
        0.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar objects like CONFIG")
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
    print(f"{len(top)} systems like {', '.join(a.seeds)} in {path}")  # noqa: T201 the command's result
    return 0


def label_main(cfg: Config, argv: list[str]) -> int:
    """Fit the label model on the verdicts given in the viewer, and save it in the run.

    Returns:
        0.

    Raises:
        SystemExit: When there are too few labels of each kind to fit on.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar objects label CONFIG")
    p.parse_args(argv)
    obj = load(cfg)
    lab = read_labels(cfg)
    lab = lab[lab.label.isin(list(CLASSES))]
    idx = pd.Index(obj.t.id).get_indexer(pd.Index(lab.id))
    lab, idx = lab[idx >= 0], idx[idx >= 0]
    y = np.asarray(lab.label.map(CLASSES), dtype=np.intp)
    counts = np.bincount(y, minlength=3)
    if np.min(counts) < 5:  # noqa: PLR2004 too few to fit a class on
        msg = (
            "Need at least 5 verdicts of each kind (artifact, not anomalous, and"
            " interesting or known),"
            f" have {counts.tolist()}."
        )
        raise SystemExit(msg)
    model = fit(obj.e[idx], y)
    np.savez(cfg.out / MODEL, **model)
    kinds = dict(zip(("bad", "ordinary", "worth"), counts.tolist(), strict=True))
    print(json.dumps({"labels": len(y), **kinds}))  # noqa: T201 the command's result
    return 0


def main(cfg: Config, argv: list[str]) -> int:
    """Run rank, like or label, named first in `argv`.

    Returns:
        The step's exit code.
    """
    steps = {"rank": rank_main, "like": like_main, "label": label_main}
    return steps[argv[0]](cfg, argv[1:])
