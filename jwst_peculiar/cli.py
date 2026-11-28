"""The `jwst-peculiar` command, one step of the object search at a time.

Each step's module is imported only when it runs, so `--help` doesn't wait for torch.
"""

from __future__ import annotations

import argparse
from importlib import import_module
from importlib.metadata import version
import io
from pathlib import Path
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from jwst_peculiar.config import Config

STEPS = (
    "init",
    "unzip",
    "check",
    "run",
    "cut",
    "embed",
    "rank",
    "like",
    "label",
    "view",
    "compact",
)
STEP_HELP = """steps:
  init     write a config from a folder of mosaics (give the folder for CONFIG)
  unzip    expand the .gz mosaics in a folder (give the folder for CONFIG)
  check    say what the config finds and anything wrong with it
  run      check, cut, embed, rank and compact in turn
  cut      find sources, flag artefacts and cut stamps
  embed    Zoobot embeddings of the stamps, on the GPU if there is one
  rank     the rarity list, with the label model once there are verdicts
  like     the objects that look most like ones you give
  label    fit the label model on the verdicts given in the viewer
  view     open a list in the browser
  compact  the list of sources too small for a stamp

jwst-peculiar STEP CONFIG --help shows a step's own options."""


def run(cfg: Config, rest: list[str]) -> int:
    """Check, cut, embed, rank and list compact sources, stopping at a failed step."""
    steps = [("check", []), ("cut", rest), ("embed", []), ("rank", ["rank"])]
    if import_module("jwst_peculiar.compact").has_bands(cfg):
        steps.append(("compact", []))
    for name, args in steps:
        step: Callable[[Config, list[str]], int] = import_module(
            f"jwst_peculiar.{name}"
        ).main
        code = step(cfg, args)
        if code:
            return code
    return 0


def _step(step: str, config: Path, rest: list[str]) -> int:
    if step in {"init", "unzip"}:
        # these take a folder rather than a config
        folder_step: Callable[[list[str]], int] = import_module(
            f"jwst_peculiar.{step}"
        ).main
        return folder_step([str(config), *rest])
    from jwst_peculiar.config import Config, read_config  # noqa: PLC0415 loads astropy

    asks = {"-h", "--help"} & set(rest)
    # a step's options can be looked up before there's a config to give it
    cfg = Config("help", Path(), "{field}_{band}") if asks else read_config(config)
    if step == "run" and not asks:
        return run(cfg, rest)
    # run's own help is cut's, as run passes its options to cut
    module = {"run": "cut", "like": "rank", "label": "rank"}.get(step, step)
    func: Callable[[Config, list[str]], int] = import_module(
        f"jwst_peculiar.{module}"
    ).main
    # rank, like and label share a module that takes the step as its first argument
    return func(cfg, [step, *rest] if module == "rank" else rest)


def main(argv: list[str] | None = None) -> int:
    """Run one step of the object search."""
    # Piped output on Windows uses the ANSI code page, so printing a path with
    # other letters in it would crash. This escapes them the way stderr does.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    p = argparse.ArgumentParser(
        prog="jwst-peculiar",
        description="Find odd objects in JWST NIRCam mosaics.",
        epilog=STEP_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"jwst-peculiar {version('jwst-peculiar')}",
    )
    p.add_argument("step", choices=STEPS)
    p.add_argument(
        "config",
        type=Path,
        help="the survey's YAML config, or for init and unzip its folder",
    )
    p.add_argument("rest", nargs=argparse.REMAINDER, help="the step's own options")
    a = p.parse_args(argv)
    try:
        return _step(a.step, a.config, a.rest)
    except KeyboardInterrupt:
        # every step writes its outputs whole, so a rerun picks up where this left off
        sys.stderr.write("jwst-peculiar: stopped.\n")
        return 130
