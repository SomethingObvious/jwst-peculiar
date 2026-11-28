"""`embed`: Zoobot embeddings of every field's stamps.

Each stamp is embedded in its eight turns and mirror images and the mean normalised,
so an embedding doesn't depend on which way the object faces.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, cast

from huggingface_hub import constants, try_to_load_from_cache
import numpy as np
import timm
import torch

from jwst_peculiar.config import written

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import numpy.typing as npt

    from jwst_peculiar.config import Config

BATCH = 32
ENCODERS = {
    size: f"hf_hub:mwalmsley/zoobot-encoder-convnext_{size}"
    for size in ("nano", "small", "base")
}


def resolve_device(spec: str) -> torch.device:
    """Find the device a --device value names, failing early when it isn't there."""
    if spec == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        if torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")
    device = torch.device(spec)
    if device.type == "cuda" and not torch.cuda.is_available():
        msg = f"--device {spec} asks for CUDA, but torch can't see a CUDA device"
        raise RuntimeError(msg)
    if device.type == "mps" and not torch.backends.mps.is_available():
        msg = f"--device {spec} asks for MPS, but it isn't available here"
        raise RuntimeError(msg)
    return device


def load_encoder(size: str, device: torch.device) -> torch.nn.Module:
    """Return a Zoobot encoder with its classifier head removed."""
    repo = ENCODERS[size].removeprefix("hf_hub:")

    def have(name: str) -> bool:
        return isinstance(try_to_load_from_cache(repo, name), str)

    # Zoobot's encoders ship pytorch_model.bin, but timm looks for safetensors first
    cached = have("config.json") and (
        have("model.safetensors") or have("pytorch_model.bin")
    )
    # otherwise the hub checks for a newer copy first, and without internet that
    # takes five retries a file before it falls back to the cache anyway
    was = constants.HF_HUB_OFFLINE
    constants.HF_HUB_OFFLINE = was or cached
    try:
        net: torch.nn.Module = timm.create_model(
            ENCODERS[size], pretrained=True, num_classes=0
        )
    except OSError as err:
        msg = (
            f"Couldn't load {ENCODERS[size]} ({err}). The first embed downloads it from"
            " the Hugging Face hub, so it needs the internet once."
        )
        raise SystemExit(msg) from err
    finally:
        constants.HF_HUB_OFFLINE = was
    return net.eval().to(device)


def _over_views(
    stamps: npt.NDArray[np.uint8],
    f: Callable[[torch.Tensor], torch.Tensor],
    *,
    device: torch.device,
) -> torch.Tensor:
    # the mean of f over each stamp's four turns and their mirror images
    out = []
    with torch.inference_mode():
        # once even with no stamps, so a tile with no objects gets (0, d) rows
        for i in range(0, max(len(stamps), 1), BATCH):
            # a copy, as the stamps are a read-only memory map
            chunk = np.array(stamps[i : i + BATCH])
            x = torch.from_numpy(chunk).to(device).float().div(255).permute(0, 3, 1, 2)
            turns = [torch.rot90(x, k, (2, 3)) for k in range(4)]
            views = turns + [v.flip(3) for v in turns]
            out.append(torch.stack([f(v).float() for v in views]).mean(0).cpu())
    return torch.cat(out)


def embed(
    stamps: npt.NDArray[np.uint8], net: torch.nn.Module, *, device: torch.device
) -> npt.NDArray[np.float32]:
    """Embed (n, N, N, 3) uint8 stamps as the mean over eight orientations."""
    return torch.nn.functional.normalize(
        _over_views(stamps, net, device=device), dim=1
    ).numpy()


def classifier() -> torch.nn.Sequential:
    """Return Zoobot nano's shape with a one-unit head, as artefact models are saved."""
    net = timm.create_model("convnext_nano", pretrained=False, num_classes=0)
    return torch.nn.Sequential(net, torch.nn.Linear(cast("int", net.num_features), 1))


def artefact_chances(
    path: Path, tiles: dict[str, npt.NDArray[np.uint8]], *, device: torch.device
) -> tuple[npt.NDArray[np.float32], float]:
    """Return each tile's mean chance of being an artefact, and the cut saved with them.

    `tiles` holds the same candidates drawn each way the file has a model for.
    """
    saved = torch.load(path, map_location="cpu", weights_only=True)
    out = []
    for which, drawn in tiles.items():
        net = classifier()
        net.load_state_dict(saved[which])
        net.eval().to(device)
        chance = torch.nn.Sequential(net, torch.nn.Sigmoid())
        out.append(_over_views(drawn, chance, device=device)[:, 0])
    return torch.stack(out).mean(0).numpy(), float(saved["cut"])


def main(cfg: Config, argv: list[str]) -> int:
    """Embed every cut field that has no embeddings yet."""
    p = argparse.ArgumentParser(prog="jwst-peculiar embed CONFIG")
    p.add_argument("--encoder", choices=ENCODERS, default="nano")
    p.add_argument("--redo", action="store_true", help="embed fields already embedded")
    a = p.parse_args(argv)
    fields = cfg.cut_fields()
    device = resolve_device(cfg.device)
    net = load_encoder(a.encoder, device)
    name = "embeddings.npy" if a.encoder == "nano" else f"embeddings_{a.encoder}.npy"
    for field in fields:
        folder = cfg.out / "fields" / field
        target = folder / name
        if a.redo or not target.exists():
            stamps = np.load(folder / "stamps.npy", mmap_mode="r")
            rows = embed(stamps, net, device=device).astype(np.float32)
            with written(target) as tmp:
                np.save(tmp, rows)
            print(f"{folder.name}: {len(stamps)} stamps embedded", flush=True)
    return 0
