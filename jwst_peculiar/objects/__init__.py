"""`jwst-peculiar objects`, the per-object search, driven by one YAML config.

`cut` detects objects in each field and cuts their stamps, `embed` turns the stamps
into Zoobot embeddings, `rank` writes the rarity list, `like` finds the objects most
like a seed, `view` opens a list in the web viewer to judge, and `label`
trains the classifier `rank` weights rarity by on the verdicts given there.
`compact` ranks the small sources `cut` leaves without a stamp on their colours,
and `check` says what a config finds before any of that runs.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import glob
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Generator

STEPS = ("check", "cut", "embed", "rank", "like", "label", "view", "compact")


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

    def band_path(self, name: str, band: str) -> Path:
        """Return the mosaic of one band of one field."""
        return Path(self.pattern.format(field=name, band=band))

    def cut_fields(self) -> list[str]:
        """Return the fields to read, the configured ones or every one cut.

        Raises:
            SystemExit: When there's nothing cut yet, or a configured field isn't.
        """
        root = self.out / "fields"
        found = sorted(f.name for f in root.iterdir()) if root.is_dir() else []
        names = list(self.fields) or found
        uncut = [n for n in names if not (root / n / "stamps.npy").is_file()]
        if not names or uncut:
            what = ", ".join(uncut[:3]) if uncut else "Nothing"
            msg = f"{what} hasn't been cut into {root} yet. Run objects cut first."
            raise SystemExit(msg)
        return names

    def missing(self) -> list[Path]:
        """Return the mosaics the fields to search should have and don't."""
        return [
            p
            for name in self.field_names()
            for p in (self.band_path(name, b) for b in self.bands)
            if not p.is_file()
        ]

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


@contextmanager
def written(path: Path) -> Generator[Path]:
    """Yield a path beside `path` to write to, moved onto `path` once the block ends.

    Steps skip a field whose output exists, so a file left half-written by Ctrl+C or
    a full disk would otherwise pass for a finished one.

    Yields:
        The temporary path, with the same suffix so numpy and pandas keep the name.
    """
    tmp = path.with_name(f"{path.stem}.part{path.suffix}")
    try:
        yield tmp
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _check_keys(path: Path, raw: dict[str, Any]) -> None:
    known = set(Config.__dataclass_fields__)
    for key in raw:
        if key not in known:
            near = difflib.get_close_matches(key, known, n=1)
            hint = f" Did you mean {near[0]!r}?" if near else ""
            msg = f"{path} has an unknown key {key!r}.{hint} It knows {sorted(known)}."
            raise SystemExit(msg)
    for key in ("name", "out", "pattern"):
        if key not in raw:
            msg = f"{path} has no {key!r}, which every objects config needs."
            raise SystemExit(msg)


def _check_bands(path: Path, cfg: Config) -> None:
    if "{band}" not in cfg.pattern or ("{field}" not in cfg.pattern and not cfg.fields):
        msg = (
            f"{path}'s pattern {cfg.pattern!r} needs {{band}}, and {{field}} unless"
            " the config lists its fields."
        )
        raise SystemExit(msg)
    missing = [b for b in (cfg.detect, *cfg.colour) if b not in cfg.bands]
    if missing:
        msg = f"{path} uses {missing} for detect or colour, but its bands are"
        msg += f" {list(cfg.bands)}."
        raise SystemExit(msg)


def _resolve(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    here = path.resolve().parent

    def rel(p: str) -> Path:
        return Path(p) if Path(p).is_absolute() else here / p

    args = dict(raw)
    args["out"] = rel(raw["out"])
    args["pattern"] = rel(raw["pattern"]).as_posix()
    for key in ("stars", "gaia"):
        if isinstance(raw.get(key), str):
            args[key] = rel(raw[key])
    for key in ("bands", "colour", "fields"):
        if key in raw:
            value = raw[key]
            one = isinstance(value, str)
            args[key] = (str(value),) if one else tuple(map(str, value))
    if len(args.get("colour", "rgb")) != 3:  # noqa: PLR2004 red, green and blue
        msg = f"{path}'s colour needs three bands, red to blue, not {args['colour']}."
        raise SystemExit(msg)
    return args


def read_config(path: Path) -> Config:
    """Read a config, with paths in it taken relative to the config's folder.

    Returns:
        The config.

    Raises:
        SystemExit: When the file is missing or isn't a config this can run.
    """
    if not path.is_file():
        msg = f"There's no config at {path}."
        raise SystemExit(msg)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as err:
        msg = f"{path} isn't valid YAML: {err}"
        raise SystemExit(msg) from err
    if not isinstance(raw, dict):
        msg = f"{path} should be a list of keys like name: and pattern:, not {raw!r}."
        raise SystemExit(msg)
    _check_keys(path, raw)
    cfg = Config(**_resolve(path, raw))
    _check_bands(path, cfg)
    return cfg


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
    if a.step == "check":
        from jwst_peculiar.objects.check import main as step  # noqa: PLC0415 loads torch for the device
    elif a.step == "cut":
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
