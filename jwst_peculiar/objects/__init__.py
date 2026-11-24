"""`jwst-peculiar objects`, the per-object search, driven by one YAML config.

`cut` detects objects in each field and cuts their stamps, `embed` turns the stamps
into Zoobot embeddings, `rank` writes the rarity list, `like` finds the objects most
like a seed, `view` opens a list in the web viewer to judge, and `label`
trains the classifier `rank` weights rarity by on the verdicts given there.
`compact` ranks the small sources `cut` leaves without a stamp on their colours.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import glob
from pathlib import Path
from typing import Any

import yaml

STEPS = ("cut", "embed", "rank", "like", "label", "view", "compact")


@dataclass(frozen=True)
class Config:
    """Where a survey's mosaics are and how to search them."""

    name: str
    out: Path
    pattern: str
    bands: tuple[str, ...] = ("f115w", "f150w", "f277w", "f444w")
    detect: str = "f277w"
    colour: tuple[str, str, str] = ("f277w", "f150w", "f115w")
    hdu: int | str = 1
    fields: tuple[str, ...] = ()
    stars: Path | None = None
    gaia: bool | Path = True
    device: str = "auto"
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    def band_path(self, name: str, band: str) -> Path:
        """Return the mosaic of one band of one field."""
        return Path(self.pattern.format(field=name, band=band))

    def cut_fields(self) -> list[str]:
        """Return the fields to read, the configured ones or every one cut."""
        if self.fields:
            return list(self.fields)
        return sorted(f.name for f in (self.out / "fields").iterdir())

    def field_names(self) -> list[str]:
        """Return the fields to search, the configured ones or every one on disk.

        Raises:
            SystemExit: When the pattern finds no detection-band mosaic.
        """
        if self.fields:
            return list(self.fields)
        pre, post = (
            part.format(band=self.detect) for part in self.pattern.split("{field}", 1)
        )
        found = (Path(f).as_posix() for f in glob.glob(pre + "*" + post))  # noqa: PTH207 {field} can sit in a folder name
        names = sorted(f[len(pre) : len(f) - len(post)] for f in found)
        if not names:
            msg = f"No {self.detect} mosaics match {self.pattern}."
            raise SystemExit(msg)
        return names


def read_config(path: Path) -> Config:
    """Read a config, with paths in it taken relative to the config's folder.

    Returns:
        The config.

    Raises:
        SystemExit: When a required key is missing.
    """
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key in ("name", "out", "pattern"):
        if key not in raw:
            msg = f"{path} has no {key!r}, which every objects config needs."
            raise SystemExit(msg)
    here = path.resolve().parent

    def rel(p: str) -> Path:
        return Path(p) if Path(p).is_absolute() else here / p

    known = set(Config.__dataclass_fields__) - {"extra"}
    args = {k: v for k, v in raw.items() if k in known}
    args["out"] = rel(raw["out"])
    args["pattern"] = rel(raw["pattern"]).as_posix()
    for key in ("stars", "gaia"):
        if isinstance(raw.get(key), str):
            args[key] = rel(raw[key])
    for key in ("bands", "colour", "fields"):
        if key in raw:
            args[key] = tuple(raw[key])
    extra = {k: v for k, v in raw.items() if k not in known}
    return Config(**args, extra=extra)


def main(argv: list[str] | None = None) -> int:
    """Run one step of the object search.

    Returns:
        The step's exit code.
    """
    p = argparse.ArgumentParser(description="Run one step of the object search.")
    p.add_argument("step", choices=STEPS)
    p.add_argument("config", type=Path, help="the survey's YAML config")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="the step's own options")
    a = p.parse_args(argv)
    cfg = read_config(a.config)
    if a.step == "cut":
        from jwst_peculiar.objects.cut import main as step  # noqa: PLC0415 torch-free but slow to import
    elif a.step == "embed":
        from jwst_peculiar.objects.embed import main as step  # noqa: PLC0415 loads torch
    elif a.step == "view":
        from jwst_peculiar.objects.view import main as step  # noqa: PLC0415 loads the server
    elif a.step == "compact":
        from jwst_peculiar.objects.compact import main as step  # noqa: PLC0415 loads scikit-learn
    else:
        from jwst_peculiar.objects.rank import main as step  # noqa: PLC0415 loads scikit-learn

        return step(cfg, [a.step, *a.rest])
    return step(cfg, a.rest)
