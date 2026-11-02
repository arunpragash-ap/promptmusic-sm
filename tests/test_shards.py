import json

import numpy as np

from promptmusic.data.shards import TokenShardDataset


def _write(tmp_path, pieces, splits):
    offset = 0
    with open(tmp_path / "tokens_0.bin", "wb") as fb, open(tmp_path / "index_0.jsonl", "w") as fi:
        for i, (p, s) in enumerate(zip(pieces, splits)):
            arr = np.asarray(p, dtype=np.uint16)
            fb.write(arr.tobytes())
            fi.write(json.dumps({"offset": offset, "length": len(p), "text": f"t{i}",
                                 "location": f"l{i}", "split": s}) + "\n")
            offset += len(p)


def test_split_and_roundtrip(tmp_path):
    _write(tmp_path, [[5, 6, 7], [8, 9], [10, 11, 12, 13]], ["train", "val", "train"])
    tr = TokenShardDataset(tmp_path, "train", window=100)
    va = TokenShardDataset(tmp_path, "val", window=100)
    assert len(tr) == 2 and len(va) == 1
    assert tr[1]["ids"].tolist() == [10, 11, 12, 13]
    assert va[0]["ids"].tolist() == [8, 9]


def test_window_is_bar_aligned_and_bounded(tmp_path):
    bar = 4
    piece = ([bar] + [7] * 9) * 50                     # a bar every 10 tokens
    _write(tmp_path, [piece], ["train"])
    ds = TokenShardDataset(tmp_path, "train", window=64, bar_id=bar)
    for _ in range(50):
        ids = ds[0]["ids"].tolist()
        assert len(ids) == 64 and ids[0] == bar


def test_val_never_crops(tmp_path):
    _write(tmp_path, [list(range(1, 200))], ["val"])
    ds = TokenShardDataset(tmp_path, "val", window=50)
    assert ds[0]["ids"].tolist() == list(range(1, 51))
