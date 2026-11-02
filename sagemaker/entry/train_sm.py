"""SageMaker Training entry: dataset gate -> promptmusic.train -> package -> evaluate.

Container paths (fixed by SageMaker):
    /opt/ml/input/config/hyperparameters.json   string -> string overrides
    /opt/ml/input/data/train/                   token shards + validation_*.json
    /opt/ml/input/data/encoder/                 encoder-only mT5
    /opt/ml/checkpoints/                        synced to S3 (spot resume)
    /opt/ml/model/                              -> model.tar.gz
    /opt/ml/output/data/                        -> output.tar.gz (samples, reports)
    /opt/ml/output/failure                      -> DescribeTrainingJob.FailureReason
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
LOG = logging.getLogger("train_sm")

PREFIX = Path("/opt/ml")
HP_FILE = PREFIX / "input/config/hyperparameters.json"
CH_TRAIN = PREFIX / "input/data/train"
CH_ENCODER = PREFIX / "input/data/encoder"
CKPT = PREFIX / "checkpoints"
MODEL = PREFIX / "model"
OUT_DATA = PREFIX / "output/data"
FAILURE = PREFIX / "output/failure"
RUN = "prompt_music_gen"
VOICE = "facebook/mms-tts-tam"

EVAL_PROMPTS = [
    "a romantic tamil love song, gentle and emotional",
    "an upbeat pop song with drums, bass and electric guitar",
    "a calm piano ballad in a minor key",
    "a fast electronic dance track with synth lead",
]


def hyperparameters() -> dict[str, str]:
    return json.loads(HP_FILE.read_text()) if HP_FILE.exists() else {}


def dataset_gate() -> dict:
    """Refuse to train on shards whose validation report failed."""
    reports = [json.loads(p.read_text()) for p in sorted(CH_TRAIN.glob("validation_*.json"))]
    if not reports:
        raise RuntimeError(f"no validation_*.json in {CH_TRAIN}; run the prepare job first")
    bad = [r for r in reports if not r.get("valid")]
    if bad:
        raise RuntimeError(f"dataset validation failed: {[r['checks_failed'] for r in bad]}")
    summary = {
        "pieces": sum(r["kept"] for r in reports),
        "tokens": sum(r["tokens_total"] for r in reports),
        "train": sum(r["split"].get("train", 0) for r in reports),
        "val": sum(r["split"].get("val", 0) for r in reports),
    }
    LOG.info("dataset gate passed: %s", summary)
    return summary


def main() -> None:
    import torch

    LOG.info("torch %s, cuda %s, gpu %s", torch.__version__, torch.version.cuda,
             torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
    hp = hyperparameters()
    config = hp.pop("config", "sagemaker")
    num_eval = int(hp.pop("eval_samples", len(EVAL_PROMPTS)))
    data_summary = dataset_gate()

    os.environ["PROMPTMUSIC_TEXT_ENCODER"] = str(CH_ENCODER)
    overrides = [f"paths.output_dir={CKPT}", f"data.token_dir={CH_TRAIN}", "train.device=cuda"]
    overrides += [f"{k}={v}" for k, v in hp.items()]

    last = CKPT / RUN / "checkpoints" / "last.pt"
    if last.exists():
        LOG.info("resuming from %s (spot restart or re-run)", last)
        overrides.append(f"train.resume={last}")
    LOG.info("overrides: %s", overrides)

    from promptmusic.train import train
    summary = train(config, overrides=overrides)
    LOG.info("train summary: %s", json.dumps(summary, default=str))
    if torch.cuda.is_available():
        LOG.info("peak GPU memory: %.2f GB", torch.cuda.max_memory_allocated() / 1e9)

    best = CKPT / RUN / "checkpoints" / "best.pt"
    package(best if best.exists() else last)
    metrics = evaluate(MODEL / "model.pt", num_eval)
    metrics["dataset"] = data_summary
    metrics["train"] = {k: v for k, v in summary.items() if k != "history"}
    text = json.dumps(metrics, indent=2, default=str)
    (MODEL / "evaluation.json").write_text(text)
    (OUT_DATA / "evaluation.json").write_text(text)
    upload_eval(MODEL / "evaluation.json")


def package(ckpt: Path) -> None:
    """model.pt (no optimizer/scheduler) + encoder + Tamil voice = self-contained model dir."""
    import torch
    from huggingface_hub import snapshot_download

    payload = torch.load(ckpt, map_location="cpu", weights_only=False)
    for key in [k for k in payload if k.startswith(("optimizer", "scheduler"))]:
        payload.pop(key)
    MODEL.mkdir(parents=True, exist_ok=True)
    torch.save(payload, MODEL / "model.pt")
    shutil.copytree(CH_ENCODER, MODEL / "encoder", dirs_exist_ok=True)
    snapshot_download(VOICE, local_dir=MODEL / "voice",
                      allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"])
    LOG.info("packaged %s from %s (%.0f MB)", MODEL / "model.pt", ckpt.name,
             (MODEL / "model.pt").stat().st_size / 1e6)


def evaluate(model_pt: Path, n: int) -> dict:
    from promptmusic.evaluate import evaluate_files
    from promptmusic.generate import generate

    out = OUT_DATA / "samples"
    out.mkdir(parents=True, exist_ok=True)
    mids = []
    for i, prompt in enumerate(EVAL_PROMPTS[:n]):
        res = generate(prompt, checkpoint=model_pt, out_dir=out, max_new_tokens=1600,
                       render_audio=False, seed=1000 + i, device="cuda")
        mids += [r.midi_path for r in res if r.midi_path]
    result = evaluate_files(mids, keep_per_file=True)
    metrics = result.to_dict()
    LOG.info("evaluation: %s", json.dumps(metrics, default=str)[:3000])
    return metrics


def upload_eval(path: Path) -> None:
    uri = os.environ.get("EVAL_S3_URI")
    if not uri:
        return
    import boto3
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    boto3.client("s3").upload_file(str(path), bucket, key)
    LOG.info("uploaded %s", uri)


if __name__ == "__main__":
    try:
        OUT_DATA.mkdir(parents=True, exist_ok=True)
        main()
    except Exception:
        FAILURE.parent.mkdir(parents=True, exist_ok=True)
        FAILURE.write_text(traceback.format_exc()[-1000:])
        LOG.exception("training job failed")
        sys.exit(1)
