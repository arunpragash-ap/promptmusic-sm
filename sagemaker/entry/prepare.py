"""SageMaker Processing entry: MidiCaps -> validated uint16 token shards.

Each host downloads MidiCaps from Hugging Face, tokenises its round-robin share
of the captions on every core, validates what it produced, and writes:

    /opt/ml/processing/output/tokens/tokens_<rank>.bin
    /opt/ml/processing/output/tokens/index_<rank>.jsonl
    /opt/ml/processing/output/tokens/validation_<rank>.json
    /opt/ml/processing/output/tokens/vocab.json            (host 0)
    /opt/ml/processing/output/encoder/...                  (host 0, encoder-only mT5)

The job FAILS (non-zero exit) when a validation check fails, so a bad dataset
never reaches a training job.
"""
from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import os
import sys
import time
import zlib
from collections import Counter
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
LOG = logging.getLogger("prepare")

WORK = Path("/opt/ml/processing/work")            # on the job's EBS volume
TOKENS_OUT = Path("/opt/ml/processing/output/tokens")
ENCODER_OUT = Path("/opt/ml/processing/output/encoder")

_TOK = None
_MAX = 16384


def host_rank() -> tuple[int, int]:
    rc = Path("/opt/ml/config/resourceconfig.json")
    if not rc.exists():
        return 0, 1
    cfg = json.loads(rc.read_text())
    hosts = sorted(cfg["hosts"])
    return hosts.index(cfg["current_host"]), len(hosts)


def _init(tok_cfg: dict, max_tokens: int) -> None:
    global _TOK, _MAX
    from promptmusic.data.tokenizer import build_tokenizer
    _TOK, _MAX = build_tokenizer(tok_cfg), max_tokens


def _encode(job: tuple[str, str, str, float]):
    midi_path, text, location, duration = job
    try:
        ids = _TOK.encode_midi(midi_path, max_tokens=_MAX)
    except Exception as exc:                 # one corrupt file must not stop the shard
        return ("error", type(exc).__name__, location)
    if len(ids) < 8:
        return ("too_short", len(ids), location)
    return ("ok", np.asarray(ids, dtype=np.uint16), text, location, duration)


def validate(report: dict, thresholds: argparse.Namespace) -> list[str]:
    """Return the list of failed checks (empty = dataset is usable)."""
    failed = []
    if report["kept"] < thresholds.min_pieces:
        failed.append(f"kept {report['kept']} pieces < min {thresholds.min_pieces}")
    if report["kept_ratio"] < thresholds.min_kept_ratio:
        failed.append(f"kept ratio {report['kept_ratio']:.3f} < {thresholds.min_kept_ratio}")
    if report["max_token_id"] >= report["vocab_size"]:
        failed.append(f"token id {report['max_token_id']} outside vocab {report['vocab_size']}")
    if report["split"].get("val", 0) == 0:
        failed.append("no validation pieces")
    if report["empty_text"] > 0:
        failed.append(f"{report['empty_text']} pieces without caption text")
    if report["duplicate_locations"] > 0:
        failed.append(f"{report['duplicate_locations']} duplicate MIDI locations")
    return failed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=16384)   # keep whole pieces
    ap.add_argument("--val-percent", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)            # 0 = all 168k
    ap.add_argument("--min-pieces", type=int, default=100)
    ap.add_argument("--min-kept-ratio", type=float, default=0.80)
    args = ap.parse_args()

    rank, world = host_rank()
    LOG.info("host %d of %d, %d cores", rank, world, os.cpu_count())
    os.environ["PROMPTMUSIC_DATA"] = str(WORK / "dataset")
    for d in (WORK, TOKENS_OUT, ENCODER_OUT):
        d.mkdir(parents=True, exist_ok=True)

    from promptmusic.config import load_config
    from promptmusic.data.captions import load_caption_index, resolve_midi_path
    from promptmusic.data.dataset import _pick_text
    from promptmusic.data.download import download_source
    from promptmusic.data.tokenizer import build_tokenizer

    t0 = time.time()
    # Same code path as `promptmusic download`; everything stays on the EBS volume.
    # The archive's top folder is stripped on extraction, so the MIDI shard
    # folders land directly in dataset/midicaps/ and train.json stays in downloads/.
    download_source("midicaps", data_root=WORK / "dataset", downloads_root=WORK / "downloads")
    caption_file = WORK / "downloads" / "train.json"
    midi_root = WORK / "dataset" / "midicaps"
    LOG.info("downloaded + extracted in %.0fs", time.time() - t0)

    cfg = load_config("sagemaker")
    tok_cfg = cfg.section("tokenizer")
    vocab = build_tokenizer(tok_cfg).vocab
    records = load_caption_index(caption_file, limit=args.limit or None)
    mine = records[rank::world]
    jobs, missing_midi, empty_text = [], 0, 0
    for r in mine:
        path = resolve_midi_path(r.location, midi_root)
        text, _ = _pick_text(r, "en", True)
        if path is None:
            missing_midi += 1
        elif not text.strip():
            empty_text += 1
        else:
            jobs.append((str(path), text, r.location, float(r.duration or 0.0)))
    LOG.info("%d records for this host: %d to tokenise, %d missing MIDI, %d empty text",
             len(mine), len(jobs), missing_midi, empty_text)

    bin_path = TOKENS_OUT / f"tokens_{rank}.bin"
    idx_path = TOKENS_OUT / f"index_{rank}.jsonl"
    offset = kept = 0
    lengths, densities = [], []
    errors, splits, token_hist = Counter(), Counter(), np.zeros(65536, dtype=np.int64)
    seen = Counter()
    t1 = time.time()
    with bin_path.open("wb") as fbin, idx_path.open("w", encoding="utf-8") as fidx, \
         mp.Pool(os.cpu_count(), initializer=_init, initargs=(tok_cfg, args.max_tokens)) as pool:
        for i, res in enumerate(pool.imap(_encode, jobs, chunksize=32)):
            if res[0] != "ok":
                errors[res[0] if res[0] == "too_short" else f"error:{res[1]}"] += 1
                continue
            _, ids, text, location, duration = res
            fbin.write(ids.tobytes())
            split = "val" if zlib.crc32(location.encode()) % 100 < args.val_percent else "train"
            fidx.write(json.dumps({"offset": offset, "length": int(ids.size), "text": text,
                                   "location": location, "split": split},
                                  ensure_ascii=False) + "\n")
            offset += int(ids.size)
            kept += 1
            splits[split] += 1
            seen[location] += 1
            lengths.append(int(ids.size))
            if duration > 0:
                densities.append(ids.size / duration)
            token_hist += np.bincount(ids, minlength=65536)
            if (i + 1) % 2000 == 0:
                LOG.info("tokenised %d/%d (kept %d, %d tokens, %.0f files/s)",
                         i + 1, len(jobs), kept, offset, (i + 1) / (time.time() - t1))

    lengths_a = np.asarray(lengths or [0])
    used = np.flatnonzero(token_hist)
    report = {
        "rank": rank, "hosts": world,
        "records": len(mine), "missing_midi": missing_midi, "empty_text": empty_text,
        "tokenise_errors": dict(errors), "kept": kept,
        "kept_ratio": round(kept / max(1, len(mine)), 4),
        "split": dict(splits),
        "duplicate_locations": sum(1 for c in seen.values() if c > 1),
        "tokens_total": offset, "bytes": bin_path.stat().st_size,
        "length_percentiles": {str(p): int(np.percentile(lengths_a, p)) for p in (5, 25, 50, 75, 95, 99)},
        "share_longer_than_2560": round(float((lengths_a > 2560).mean()), 4),
        "tokens_per_second_median": round(float(np.median(densities)), 1) if densities else None,
        "vocab_size": len(vocab), "vocab_used": int(used.size),
        "max_token_id": int(used.max()) if used.size else -1,
        "seconds": round(time.time() - t0, 1),
    }
    failed = validate(report, args)
    report["checks_failed"] = failed
    report["valid"] = not failed
    (TOKENS_OUT / f"validation_{rank}.json").write_text(json.dumps(report, indent=2))
    LOG.info("validation report: %s", json.dumps(report))

    if rank == 0:
        (TOKENS_OUT / "vocab.json").write_text(json.dumps(vocab, ensure_ascii=False))
        from transformers import AutoTokenizer, T5EncoderModel
        T5EncoderModel.from_pretrained("google/mt5-base").save_pretrained(
            ENCODER_OUT, safe_serialization=True)
        AutoTokenizer.from_pretrained("google/mt5-base").save_pretrained(ENCODER_OUT)
        LOG.info("encoder-only mT5 exported to %s", ENCODER_OUT)

    if failed:
        LOG.error("DATASET VALIDATION FAILED: %s", "; ".join(failed))
        sys.exit(2)
    LOG.info("dataset validation passed")


if __name__ == "__main__":
    main()
