"""SageMaker async endpoint: prompt (+ lyrics) -> zip of raw MIDI, arranged MIDI, WAV, meta.

Contract: listen on 8080, GET /ping -> 200 when ready, POST /invocations.
The model is loaded once per container (generate() would otherwise reload
the 1.3 GB checkpoint on every request).
"""
from __future__ import annotations

import functools
import io
import json
import logging
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import torch
from flask import Flask, Response, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
LOG = logging.getLogger("serve")

MODEL_DIR = Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
MODEL_PT = str(MODEL_DIR / "model.pt")
VOICE = str(MODEL_DIR / "voice") if (MODEL_DIR / "voice").is_dir() else "facebook/mms-tts-tam"
os.environ.setdefault("PROMPTMUSIC_TEXT_ENCODER", str(MODEL_DIR / "encoder"))
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
TOKENS_PER_SECOND = 40          # measured MidiCaps density
MAX_SECONDS = 64                # 2560 training tokens / 40
LYRICS_DIR = Path("/opt/program/data/lyrics")

import promptmusic.generate as G  # noqa: E402
from promptmusic.arrange import arrange_midi  # noqa: E402
from promptmusic.render import render_midi  # noqa: E402
from promptmusic.singing import sing_song  # noqa: E402

G._load_for_inference = functools.lru_cache(maxsize=1)(G._load_for_inference)
_t = time.time()
G._load_for_inference(MODEL_PT, None, DEVICE)
LOG.info("model loaded on %s in %.1fs (voice: %s)", DEVICE, time.time() - _t, VOICE)

app = Flask(__name__)


@app.get("/ping")
def ping():
    return Response("ok", status=200)


@app.post("/invocations")
def invocations():
    req = json.loads(request.get_data().decode("utf-8"))
    prompt = req["prompt"]
    lyrics = req.get("lyrics", "")
    if not lyrics and req.get("lyrics_file"):           # a file baked into the image
        name = Path(req["lyrics_file"]).name              # no path traversal
        lyrics = (LYRICS_DIR / name).read_text(encoding="utf-8")
    language = req.get("language", "en")
    duration = min(float(req.get("duration", 45)), MAX_SECONDS)
    sing = bool(req.get("sing", bool(lyrics)))
    t0 = time.time()
    LOG.info("request: %s", json.dumps({k: v for k, v in req.items() if k != "lyrics"}, ensure_ascii=False))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        res = G.generate(
            prompt, checkpoint=MODEL_PT, out_dir=td, render_audio=False,
            max_new_tokens=int(duration * TOKENS_PER_SECOND), lyrics=lyrics, language=language,
            seed=req.get("seed"), device=DEVICE,
            temperature=float(req.get("temperature", 0.95)), top_p=float(req.get("top_p", 0.92)),
        )[0]
        arranged, report = arrange_midi(res.midi_path, td / "arranged.mid")
        wav = td / "song.wav"
        sung = None
        if sing and lyrics:
            sung = sing_song(arranged, lyrics=lyrics, out_path=wav, voice=VOICE, device="cpu")
        else:
            render_midi(arranged, wav)

        meta = {
            "prompt": prompt, "language": language, "requested_seconds": duration,
            "num_tokens": res.num_tokens, "num_notes": res.num_notes,
            "midi_seconds": res.duration_sec, "sung": bool(sung),
            "sing": sung.to_dict() if sung is not None else None,
            "arrangement": report.to_dict() if hasattr(report, "to_dict") else str(report),
            "compute_seconds": round(time.time() - t0, 1), "device": DEVICE,
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(res.midi_path, "raw.mid")
            z.write(arranged, "arranged.mid")
            z.write(wav, "song.wav")
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2, default=str))
    LOG.info("done in %.1fs", time.time() - t0)
    return Response(buf.getvalue(), status=200, mimetype="application/zip")
