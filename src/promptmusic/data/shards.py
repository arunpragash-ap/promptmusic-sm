"""Token shards written by ``sagemaker/entry/prepare.py``.

``tokens_<rank>.bin``   every piece of one shard, concatenated, ``uint16``
``index_<rank>.jsonl``  ``{"offset", "length", "text", "location", "split"}`` per piece

The music vocabulary has 423 tokens, so two bytes per token is enough: the
full MidiCaps corpus fits in about 1 GB instead of many GB of Python ints.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch.utils.data import Dataset


class TokenShardDataset(Dataset):
    """Memory-mapped token shards with random, bar-aligned training windows."""

    def __init__(
        self,
        root: str | Path,
        split: str,
        window: int,
        bar_id: int | None = None,
        random_crop: bool = True,
    ):
        self.root = Path(root)
        self.window = int(window)          # music tokens per example, BOS/EOS excluded
        self.bar_id = bar_id
        self.random_crop = random_crop and split == "train"
        self.items: list[tuple[str, int, int, str]] = []
        for index in sorted(self.root.glob("index_*.jsonl")):
            rank = index.stem.split("_", 1)[1]
            with index.open(encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    if r["split"] == split:
                        self.items.append((rank, int(r["offset"]), int(r["length"]), r["text"]))
        self._maps: dict[str, np.memmap] = {}   # opened lazily, once per DataLoader worker

    def _map(self, rank: str) -> np.memmap:
        if rank not in self._maps:
            self._maps[rank] = np.memmap(self.root / f"tokens_{rank}.bin", dtype=np.uint16, mode="r")
        return self._maps[rank]

    def __len__(self) -> int:
        return len(self.items)

    def window_start(self, piece: np.ndarray, rng: random.Random | None = None) -> int:
        """Random start, snapped back to the closest bar line at or before it."""
        n = len(piece)
        if not self.random_crop or n <= self.window:
            return 0
        start = (rng or random).randint(0, n - self.window)
        if self.bar_id is not None:
            bars = np.flatnonzero(piece[: start + 1] == self.bar_id)
            start = int(bars[-1]) if bars.size else 0
        return start

    def __getitem__(self, i: int) -> dict[str, Any]:
        rank, off, n, text = self.items[i]
        piece = self._map(rank)[off: off + n]
        start = self.window_start(piece)
        ids = np.asarray(piece[start: start + self.window], dtype=np.int64)
        return {"ids": torch.from_numpy(ids), "text": text, "index": i}
