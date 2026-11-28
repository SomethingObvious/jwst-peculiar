"""`view`: open a list of objects in the web viewer, to judge and label them.

A row with a `tile` takes its picture from the compact list's tiles, so one list can
mix both kinds.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import html
import io
import json
import logging
from pathlib import Path
import re
import shutil
from typing import TYPE_CHECKING, Any

from astropy.wcs import WCS
import numpy as np
import pandas as pd
from PIL import Image

from jwst_peculiar.compact import STAMPS
from jwst_peculiar.server import built_viewer, serve
from jwst_peculiar.verdicts import read_verdicts

if TYPE_CHECKING:
    from collections.abc import Iterator

    import numpy.typing as npt

    from jwst_peculiar.config import Config


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
        "mosaic": row.field,
        "x0": round(float(x) - h, 2),
        "y0": round(float(y) - h, 2),
        "size": round(2 * h, 2),
        "ra": float(row.ra),
        "dec": float(row.dec),
    }


class ObjectBundle:
    """The objects in one list, laid out the way the viewer reads a run."""

    def __init__(self, cfg: Config, listing: Path, *, writable: bool) -> None:
        """Read the list and every field it draws on."""
        self.run_dir = cfg.out
        self.root = cfg.out / "fields"
        shown = pd.read_csv(listing, dtype={"field": str})
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
                "created": datetime.fromtimestamp(
                    listing.stat().st_mtime, UTC
                ).isoformat(),
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
        """Read one viewer path."""
        folder, _, name = path.partition("/")
        if path in ("index.json", "verdicts.json"):
            data = self.index if path == "index.json" else read_verdicts(self.run_dir)
            return json.dumps(data).encode(), "application/json"
        if folder == "mosaics" and name.removesuffix(".png") in self.index["mosaics"]:
            png = self.root / name.removesuffix(".png") / "field.png"
            return png.read_bytes(), "image/png"
        stem = Path(name).stem
        if folder != "tiles" or not stem.isdigit() or int(stem) >= len(self.tiles):
            raise FileNotFoundError(path)
        return self._png(int(stem)), "image/png"

    def paths(self) -> Iterator[str]:
        """List every path the viewer can ask this list for."""
        yield "index.json"
        yield "verdicts.json"
        for i in range(len(self.tiles)):
            yield f"tiles/{i:06d}.png"
        for f in self.index["mosaics"]:
            yield f"mosaics/{f}.png"


def export(bundle: ObjectBundle, out_dir: Path, app_dir: Path | None = None) -> int:
    """Write the list under out_dir/run/, with the viewer app beside it when given."""
    out_dir = Path(out_dir)
    run_out = out_dir / "run"
    if (
        out_dir.is_dir()
        and any(out_dir.iterdir())
        and not (run_out / "index.json").is_file()
    ):
        msg = (
            f"{out_dir} isn't empty and isn't an earlier export, so nothing was "
            "written. Pick a new folder."
        )
        raise SystemExit(msg)
    # only the folders an export writes, so a CNAME or the like stays
    for ours in (run_out, out_dir / "assets"):
        if ours.exists():
            shutil.rmtree(ours)
    if app_dir is not None:
        shutil.copytree(app_dir, out_dir, dirs_exist_ok=True)
    bundle.index["writable"] = False
    n = 0
    for path in bundle.paths():
        data, _ = bundle.read(path)
        dest = run_out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        n += 1
    # GitHub Pages runs Jekyll otherwise, which is slow on a thousand files and does
    # nothing here.
    (out_dir / ".nojekyll").touch()
    return n


def add_site_meta(bundle: ObjectBundle, out_dir: Path, site_url: str) -> None:
    """Make an exported list findable once it's hosted at `site_url`."""
    out_dir = Path(out_dir)
    url = site_url.rstrip("/") + "/"
    run = bundle.index["run"]
    title = f"{run['name']} \u00b7 jwst-peculiar"
    desc = (
        f"The {len(bundle.index['tiles'])} oddest objects in {run['name']} as ranked "
        "by jwst-peculiar, which finds odd galaxies in James Webb Space Telescope "
        "images. Step through their colour stamps with RA and Dec."
    )
    _social_card(bundle, out_dir / "social.png")
    page = (out_dir / "index.html").read_text(encoding="utf-8")
    page = re.sub(
        r"<title>.*?</title>",
        lambda _: f"<title>{html.escape(title)}</title>",
        page,
        count=1,
    )
    for attr, value in (
        ('name="description"', desc),
        ('property="og:title"', title),
        ('property="og:description"', desc),
    ):
        page = _set_meta(page, attr, value)
    ld = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": title,
        "url": url,
        "description": desc,
        "applicationCategory": "Science",
        "operatingSystem": "Any current browser",
        "isAccessibleForFree": True,
        "image": url + "social.png",
    }
    page = page.replace("  </head>", _head_tags(url, run["name"], ld), 1)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{html.escape(url)}</loc>"
        f"<lastmod>{run['created'][:10]}</lastmod></url>\n"
        "</urlset>\n",
        encoding="utf-8",
    )


def _set_meta(page: str, attr: str, value: str) -> str:
    escaped = html.escape(value)
    return re.sub(
        # Across lines too, as the built page wraps its longer tags.
        rf'(<meta\s+{attr}\s+content=")[^"]*(")',
        lambda m: m.group(1) + escaped + m.group(2),
        page,
        count=1,
    )


def _head_tags(url: str, name: str, ld: dict[str, Any]) -> str:
    url = html.escape(url)
    # A name holding </script> would otherwise end the block early.
    data = json.dumps(ld).replace("</", "<\\/")
    return "\n".join([
        f'    <link rel="canonical" href="{url}" />',
        f'    <meta property="og:url" content="{url}" />',
        f'    <meta property="og:image" content="{url}social.png" />',
        '    <meta property="og:image:width" content="1200" />',
        '    <meta property="og:image:height" content="630" />',
        f'    <meta property="og:image:alt" content="{html.escape(name)}" />',
        f'    <meta name="twitter:image" content="{url}social.png" />',
        f'    <script type="application/ld+json">{data}</script>',
        "  </head>",
    ])


def _social_card(bundle: ObjectBundle, path: Path) -> None:
    """Save a 1200 by 630 crop of the top object's field preview, centred on it."""
    top = bundle.index["tiles"][0]
    preview, _ = bundle.read(f"mosaics/{top['mosaic']}.png")
    with Image.open(io.BytesIO(preview)) as src:
        im = src.convert("RGB")
        box = _card_box(
            np.asarray(im.convert("L")) > 0,
            top,
            bundle.index["mosaics"][top["mosaic"]]["shape"][0],
        )
        card = im.crop(box).resize((1200, 630), Image.Resampling.LANCZOS)
    card.save(path, optimize=True)


def _card_box(
    lit: npt.NDArray[np.bool_], top: dict[str, Any], h_field: int
) -> tuple[int, int, int, int]:
    height, width = lit.shape
    scale = height / h_field
    cx = (top["x0"] + top["size"] / 2) * scale
    cy = (h_field - top["y0"] - top["size"] / 2) * scale
    # Half the field's height keeps the card on the field, clear of gaps between
    # detectors, and still shows plenty of sky around the object.
    h = min(height // 2, round(width * 630 / 1200))
    w = round(h * 1200 / 630)
    # Kept off blank columns and rows, like the gap between NIRCam's modules.
    left = _card_start(lit.mean(axis=0), cx, w)
    upper = _card_start(lit.mean(axis=1), cy, h)
    return left, upper, left + w, upper + h


def _card_start(lit: npt.NDArray[np.floating[Any]], centre: float, length: int) -> int:
    lo, hi = lit_span(lit, centre, length)
    return int(min(max(centre - length / 2, lo), hi - length))


def lit_span(
    lit: npt.NDArray[np.floating[Any]], centre: float, length: int
) -> tuple[int, int]:
    """Find the run of indices around `centre` that is lit, 5 at a time."""
    good = np.convolve(lit, np.ones(5) / 5, mode="same") >= 0.25 * np.median(lit)
    i = min(max(int(centre), 0), len(lit) - 1)
    if not good[i]:
        return 0, len(lit)
    start, end = i, i + 1
    while start > 0 and good[start - 1]:
        start -= 1
    while end < len(lit) and good[end]:
        end += 1
    return (start, end) if end - start >= length else (0, len(lit))


def main(cfg: Config, argv: list[str]) -> int:
    """Serve a list in the viewer, or export it as a static site."""
    p = argparse.ArgumentParser(prog="jwst-peculiar view CONFIG")
    p.add_argument(
        "--list",
        default="rank.csv",
        help="a list in the run folder, rank.csv by default, or compact.csv",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", dest="browser", action="store_false")
    p.add_argument("--export", type=Path, default=None, help="write a static site here")
    p.add_argument(
        "--site-url",
        default=None,
        help="with --export, where the site will be hosted, for its social card",
    )
    p.add_argument("--name", default=None, help="the list's name in the viewer")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    listing = cfg.out / a.list
    if not listing.is_file():
        msg = (
            f"There's no {listing} yet. Run jwst-peculiar rank, like or compact first."
        )
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
