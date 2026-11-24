"""`objects embed`: Zoobot embeddings of every field's stamps.

The encoder is Zoobot's ConvNeXt-nano, loaded through timm from its Apache-2.0
weights on the Hugging Face hub. Each stamp is embedded in its four turns and their
mirror images, and the mean is normalised to unit length, so an object's embedding
doesn't depend on which way it faces.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

import numpy as np
import torch

from jwst_peculiar.data import resolve_device
from jwst_peculiar.objects import written

if TYPE_CHECKING:
    from jwst_peculiar.objects import Config

BATCH = 32
ENCODERS = {
    size: f"hf_hub:mwalmsley/zoobot-encoder-convnext_{size}"
    for size in ("nano", "small", "base")
}


def load_encoder(size: str, device: torch.device) -> torch.nn.Module:
    """Return a Zoobot encoder with its classifier head removed.

    Raises:
        SystemExit: When timm, which loads the encoder, isn't installed.
    """
    try:
        import timm  # noqa: PLC0415 only embed needs it
    except ImportError as err:
        msg = "objects embed needs timm: pip install timm"
        raise SystemExit(msg) from err
    from huggingface_hub import constants, try_to_load_from_cache  # noqa: PLC0415 comes with timm

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


def embed(stamps: Any, net: torch.nn.Module, *, device: torch.device) -> Any:  # noqa: ANN401 uint8 stamps in, float32 rows out
    """Embed (n, N, N, 3) uint8 stamps as the mean over eight orientations.

    Returns:
        (n, d) float32 rows of unit length.
    """
    out = []
    with torch.inference_mode():
        for i in range(0, len(stamps), BATCH):
            # a copy, as the stamps are a read-only memory map
            chunk = np.array(stamps[i : i + BATCH])
            x = torch.from_numpy(chunk).to(device).float().div(255).permute(0, 3, 1, 2)
            turns = [torch.rot90(x, k, (2, 3)) for k in range(4)]
            views = turns + [v.flip(3) for v in turns]
            out.append(torch.stack([net(v).float() for v in views]).mean(0).cpu())
    return torch.nn.functional.normalize(torch.cat(out), dim=1).numpy()


def main(cfg: Config, argv: list[str]) -> int:
    """Embed every cut field that has no embeddings yet.

    Returns:
        0.
    """
    p = argparse.ArgumentParser(prog="jwst-peculiar objects embed CONFIG")
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
            print(f"{folder.name}: {len(stamps)} stamps embedded", flush=True)  # noqa: T201 the command's progress
    return 0
