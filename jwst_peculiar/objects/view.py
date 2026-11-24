"""`objects view`: open a list of objects in the web viewer, to judge and label them.

Each object shows as a tile, its stamp the tile's picture and its field the mosaic,
so verdicts work as they do for tiles and save to verdicts.csv in the run folder.
`objects label` learns from them. A row with a `tile` takes its picture from the
compact list's tiles, as compact candidates have no stamp in their field, so one
list can mix both.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import io
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astropy.wcs import WCS
import numpy as np
import pandas as pd
from PIL import Image

from jwst_peculiar.bundle import add_site_meta, export, read_verdicts
from jwst_peculiar.objects.compact import STAMPS
from jwst_peculiar.server import built_viewer, serve

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jwst_peculiar.objects import Config

_BLANK_MAP = np.zeros(1, "<f4").tobytes()


@dataclass(frozen=True)
class ObjectTile:
    """One listed object, and where its stamp is."""

    key: str
    field: str
    row: int
    compact: bool


def _entry(rank: int, row: pd.Series[Any], meta: dict[str, Any]) -> dict[str, Any]:
    # the stamp's square on the field, which is the middle colour band's grid
    x, y = WCS(meta["wcs"]).all_world2pix(row.ra, row.dec, 0)
    h = float(row.half) / float(meta["pixel_scale_arcsec"])
    score = row.get("score", row.get("similarity"))
    return {
        "key": row.id,
        "rank": rank,
        "score": None if score is None else float(score),
        "in_reference": False,
        "mosaic": row.field,
        "x0": round(float(x) - h, 2),
        "y0": round(float(y) - h, 2),
        "size": round(2 * h, 2),
        "ra": float(row.ra),
        "dec": float(row.dec),
        "peak": [0, 0],
        "has_cutout": False,
    }


class ObjectBundle:
    """The objects in one list, laid out the way the viewer reads a run."""

    def __init__(self, cfg: Config, listing: Path, *, writable: bool) -> None:
        """Read the list and every field it draws on."""
        self.run_dir = cfg.out
        self.root = cfg.out / "fields"
        shown = pd.read_csv(listing)
        tile = shown.get("tile", pd.Series(np.nan, index=shown.index))
        small = tile.notna().to_numpy()
        self._compact = (
            np.load(cfg.out / STAMPS, mmap_mode="r") if small.any() else None
        )
        mosaics: dict[str, dict[str, Any]] = {}
        rows: dict[str, int] = {}
        for f in sorted(set(shown.field)):
            meta = json.loads((self.root / f / "field.json").read_text("utf-8"))
            mosaics[f] = {**meta, "preview": f"mosaics/{f}.png"}
            if not small[shown.field.to_numpy() == f].all():
                ids = pd.read_csv(self.root / f / "objects.csv", usecols=["label"])
                rows.update({f"{f}:{lab}": i for i, lab in enumerate(ids.label)})
        self.tiles = [
            ObjectTile(k, f, int(n) if s else rows[k], compact=bool(s))
            for k, f, n, s in zip(shown.id, shown.field, tile, small, strict=True)
        ]
        entries = [
            _entry(i + 1, r, mosaics[r.field])
            for i, (_, r) in enumerate(shown.iterrows())
        ]
        self._stamps: dict[str, Any] = {}
        self.index: dict[str, Any] = {
            "version": 1,
            "writable": writable,
            "run": {
                "name": f"{cfg.name} {listing.stem}",
                "model": "zoobot-encoder-convnext_nano",
                "detector": "objects",
                "resolution": 224,
                "patch_size": 224,
                "grid": [1, 1],
                "score_mode": listing.stem,
                "passes": 1,
                "n_reference": 0,
                "created": datetime.fromtimestamp(
                    listing.stat().st_mtime, UTC
                ).isoformat(),
                "args": {},
            },
            "mosaics": mosaics,
            "tiles": entries,
        }

    def _png(self, i: int) -> bytes:
        tile = self.tiles[i]
        if tile.compact and self._compact is not None:
            stamp = self._compact[tile.row]
        else:
            if tile.field not in self._stamps:
                path = self.root / tile.field / "stamps.npy"
                self._stamps[tile.field] = np.load(path, mmap_mode="r")
            stamp = self._stamps[tile.field][tile.row]
        # the stamp's first row is its lowest, and the viewer wants the top row first
        out = io.BytesIO()
        Image.fromarray(np.asarray(stamp[::-1])).save(out, "png")
        return out.getvalue()

    def read(self, path: str) -> tuple[bytes, str]:
        """Read one viewer path.

        Returns:
            The file's bytes and its content type.

        Raises:
            FileNotFoundError: The list has nothing at `path`.
        """
        folder, _, name = path.partition("/")
        if path in ("index.json", "verdicts.json"):
            data = self.index if path == "index.json" else read_verdicts(self.run_dir)
            return json.dumps(data).encode(), "application/json"
        if folder == "mosaics" and name.removesuffix(".png") in self.index["mosaics"]:
            png = self.root / name.removesuffix(".png") / "field.png"
            return png.read_bytes(), "image/png"
        stem = Path(name).stem
        if (
            folder not in ("tiles", "maps")
            or not stem.isdigit()
            or int(stem) >= len(self.tiles)
        ):
            raise FileNotFoundError(path)
        if folder == "maps":
            return _BLANK_MAP, "application/octet-stream"
        return self._png(int(stem)), "image/png"

    def paths(self) -> Iterator[str]:
        """List every path the viewer can ask this list for.

        Yields:
            Each path.
        """
        yield "index.json"
        yield "verdicts.json"
        for i in range(len(self.tiles)):
            yield f"tiles/{i:06d}.png"
            yield f"maps/{i:06d}.f32"
        for f in self.index["mosaics"]:
            yield f"mosaics/{f}.png"


def main(cfg: Config, argv: list[str]) -> int:
    """Serve a list in the viewer, or export it as a static site.

    Returns:
        0.

    Raises:
        SystemExit: When the list hasn't been written yet.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar objects view CONFIG")
    p.add_argument(
        "--list",
        default="rank.csv",
        help="a list in the run folder, rank.csv by default, or compact.csv",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", dest="browser", action="store_false")
    p.add_argument("--export", type=Path, default=None, help="write a static site here")
    p.add_argument(
        "--site_url",
        default=None,
        help="with --export, where the site will be hosted, for its social card",
    )
    p.add_argument("--name", default=None, help="the list's name in the viewer")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    listing = cfg.out / a.list
    if not listing.is_file():
        msg = f"There's no {listing} yet. Run objects rank, like or compact first."
        raise SystemExit(msg)
    app = built_viewer()
    bundle = ObjectBundle(cfg, listing, writable=a.export is None)
    if a.name:
        bundle.index["run"]["name"] = a.name
    if a.export is not None:
        export(bundle, a.export, app)
        if a.site_url:
            add_site_meta(bundle, a.export, a.site_url)
    else:
        serve(bundle, port=a.port, browser=a.browser)
    return 0
