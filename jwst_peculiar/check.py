"""`check`: what a config finds, before a long cut."""

from __future__ import annotations

import argparse
import math
import shutil
from typing import TYPE_CHECKING, cast
import warnings

from astropy.io import fits
from huggingface_hub import try_to_load_from_cache

from jwst_peculiar.compact import has_bands
from jwst_peculiar.config import BYTES_PER_PIXEL, memory_size, to_mjy_sr
from jwst_peculiar.embed import resolve_device

if TYPE_CHECKING:
    from pathlib import Path

    from jwst_peculiar.config import Config

# SMACS 0723's run folder took 307 MB for 121 Mpx, the most per pixel measured
DISK_PER_PIXEL = 307e6 / 121e6
WEIGHTS = "mwalmsley/zoobot-encoder-convnext_nano"

# missing mosaics named one by one before the rest are only counted
SHOWN = 10


def _fits_memory(cfg: Config, biggest: str, need: float) -> list[str]:
    ram = memory_size()
    if not ram or need <= ram:
        return []
    h = fits.getheader(cfg.band_path(biggest, cfg.detect), cfg.hdu)
    side = cfg.tile or max(int(h["NAXIS1"]), int(h["NAXIS2"]))
    tile = max(1000, int(side * math.sqrt(ram / 2 / need)) // 1000 * 1000)
    said = (
        f"cut needs about {need / 1e9:.0f} GB for {biggest} and this machine has"
        f" {ram / 1e9:.0f} GB, so add tile: {tile} to the config to cut it in pieces"
    )
    return [said]


def _units(cfg: Config, name: str) -> None:
    heads = [fits.getheader(cfg.band_path(name, b), cfg.hdu) for b in cfg.bands]
    units = ", ".join(sorted({str(h.get("BUNIT", "not given")) for h in heads}))
    factors = [to_mjy_sr(h) for h in heads]
    if None in factors:
        print(
            f"the mosaics' units are {units}, which can't be turned into MJy/sr, so"
            " colours and the f_ and e_ columns are only right if every band has the"
            " same unit and pixel size"
        )
    elif set(factors) != {1.0}:
        print(
            f"the mosaics are in {units}, so each band is turned into MJy/sr with its"
            " own pixel size"
        )


def _short(path: Path, hdu: int | str) -> bool:
    # a download that stopped early leaves a file shorter than its header says, or
    # no header at all, and astropy's own warning would only repeat this
    try:
        with warnings.catch_warnings(), fits.open(path, memmap=True) as h:
            warnings.simplefilter("ignore")
            info = cast("dict[str, int] | None", h.fileinfo(h.index_of(hdu)))
    except (OSError, KeyError, IndexError):
        return True
    end = 0 if info is None else info["datLoc"] + info["datSpan"]
    # a gzipped file is smaller than its data, and cut finds one that's broken
    return not path.name.endswith(".gz") and path.stat().st_size < end


def _weights_cached() -> bool:
    return isinstance(try_to_load_from_cache(WEIGHTS, "config.json"), str)


def _memory(cfg: Config, names: list[str], gone: set[str]) -> list[str]:
    have = [
        n
        for n in names
        if not any(cfg.band_path(n, b).as_posix() in gone for b in cfg.bands)
    ]
    if not have:
        return []
    try:
        sizes = {n: cfg.pixels(n) for n in have}
    except (OSError, KeyError) as err:
        return [f"couldn't read the mosaics' size ({err})"]
    biggest = max(sizes, key=sizes.__getitem__)
    px = sizes[biggest]
    said = f"biggest field {biggest}, {px / 1e6:.0f} Mpx"
    print(f"{said}, cut needs about {px * BYTES_PER_PIXEL / 1e9:.1f} GB")
    problems = _fits_memory(cfg, biggest, px * BYTES_PER_PIXEL)
    _units(cfg, biggest)
    need = sum(sizes.values()) * DISK_PER_PIXEL / 1e9
    cfg.out.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cfg.out).free / 1e9
    print(f"the run needs up to about {need:.1f} GB of disk, {free:.0f} GB is free")
    if need > free:
        problems.append(
            f"the run folder's disk has {free:.1f} GB free, not {need:.1f} GB"
        )
    return problems


def report(cfg: Config) -> list[str]:
    """Check a config's fields, mosaics, device, weights and run folder."""
    names = cfg.field_names()
    missing = cfg.missing()
    print(f"{len(names)} fields, bands {', '.join(cfg.bands)}")
    if not has_bands(cfg):
        print(f"no compact list, as it needs {', '.join(cfg.settings.compact_bands)}")
    problems = [f"missing {p}" for p in missing[:SHOWN]]
    if len(missing) > SHOWN:
        problems.append(f"and {len(missing) - SHOWN} more missing mosaics")
    gone = {p.as_posix() for p in missing}
    problems += [
        f"{p} is cut short or empty, so its download probably stopped early"
        for n in cfg.mosaics()
        for p in (cfg.band_path(n, b) for b in cfg.bands)
        if p.as_posix() not in gone and _short(p, cfg.hdu)
    ]
    problems += _memory(cfg, names, gone)
    try:
        print(f"embed would run on {resolve_device(cfg.device)}")
    except RuntimeError as err:
        problems.append(str(err))
    state = (
        "downloaded" if _weights_cached() else "not downloaded yet, embed fetches them"
    )
    print(f"Zoobot weights {state}")
    try:
        cfg.out.mkdir(parents=True, exist_ok=True)
        probe = cfg.out / ".check"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as err:
        problems.append(f"can't write the run folder {cfg.out} ({err})")
    return problems


def main(cfg: Config, argv: list[str]) -> int:
    """Print what the config finds and any problem with it."""
    argparse.ArgumentParser(prog="jwst-peculiar check CONFIG").parse_args(argv)
    problems = report(cfg)
    for p in problems:
        print(f"problem: {p}")
    print("ready" if not problems else f"{len(problems)} problems")
    return 1 if problems else 0
