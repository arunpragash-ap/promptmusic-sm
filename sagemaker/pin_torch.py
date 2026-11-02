"""Print pip constraints that pin the torch build shipped in the base image."""
import importlib.metadata as md

for name in ("torch", "torchvision", "torchaudio"):
    try:
        print(f"{name}=={md.version(name)}")
    except md.PackageNotFoundError:
        pass
