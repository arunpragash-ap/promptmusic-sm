# /// script
# requires-python = ">=3.10"
# dependencies = ["gradio>=4.44", "boto3>=1.34"]
# ///
"""PromptMusic Studio - a web GUI for the SageMaker async endpoint.

Uses only the AWS SDK (boto3); no AWS CLI needed. Credentials come from the standard
SDK chain: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars, an AWS_PROFILE, or an
IAM role when hosted on AWS. Give it a dedicated least-privilege user: app/iam-policy.json.

Local:   uv run app/studio.py                         # http://127.0.0.1:7860
Server:  PM_HOST=0.0.0.0 PM_USERS="alice:pw1,bob:pw2" python app/studio.py
Docker:  see app/Dockerfile

Each song: request JSON -> s3://BUCKET/async/in/ -> invoke_endpoint_async -> poll
s3://BUCKET/async/out/ -> zip unpacked to outputs/studio/<time>_<prompt>/.

Env vars: AWS_REGION (ap-south-1), PM_ENDPOINT (promptmusic-async), PM_BUCKET
(sagemaker-promptmusic-<account>), PM_HOST (127.0.0.1), PORT (7860),
PM_USERS ("user:password,..." - required unless PM_HOST is local),
PM_REQUIRE_LOGIN=1 (require PM_USERS even on a local host, e.g. behind a reverse proxy),
PM_OUTPUT_DIR (outputs/studio).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import boto3
import gradio as gr
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("studio")

REGION = os.environ.get("AWS_REGION", "ap-south-1")
ENDPOINT = os.environ.get("PM_ENDPOINT", "promptmusic-async")
OUT_ROOT = Path(os.environ.get("PM_OUTPUT_DIR")
                or Path(__file__).resolve().parent.parent / "outputs" / "studio")
HOST = os.environ.get("PM_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7860"))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
USERS = [tuple(u.split(":", 1)) for u in os.environ.get("PM_USERS", "").split(",") if ":" in u]
POLL_SECONDS = 5
TIMEOUT_SECONDS = 30 * 60
MAX_SECONDS = 64  # server cap: 2 560 training tokens / 40 tokens per second

_session = boto3.Session(region_name=REGION)
s3 = _session.client("s3")
sm = _session.client("sagemaker")
smr = _session.client("sagemaker-runtime")
try:
    ACCOUNT = _session.client("sts").get_caller_identity()["Account"]
except Exception as exc:  # noqa: BLE001 - any credential problem: explain and stop
    raise SystemExit(
        f"\nAWS credentials on this machine are not working:\n  {exc}\n\n"
        "Fix one of these, then run again:\n"
        "  export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...   # keys of the studio IAM user\n"
        "  export AWS_PROFILE=<profile>                              # or a working profile\n"
        "On AWS hosting (EC2, App Runner, ECS), attach an IAM role instead of keys.\n"
    ) from None
BUCKET = os.environ.get("PM_BUCKET") or f"sagemaker-promptmusic-{ACCOUNT}"

EXAMPLES = [
    ["a calm piano ballad with soft strings, slow and emotional", 30, 7],
    ["an upbeat pop song with drums, bass and electric guitar", 40, 3],
    ["a fast electronic dance track with a synth lead", 40, 11],
    ["a gentle acoustic guitar folk tune, warm and nostalgic", 35, 21],
    ["a dramatic orchestral piece with strings and brass", 45, 5],
]


# --------------------------------------------------------------------------- #
# AWS helpers
# --------------------------------------------------------------------------- #
def endpoint_status() -> tuple[str, int, str]:
    """(status, running instances, instance type) - ("Missing", 0, "") if absent."""
    try:
        d = sm.describe_endpoint(EndpointName=ENDPOINT)
    except ClientError:
        return "Missing", 0, ""
    v = (d.get("ProductionVariants") or [{}])[0]
    itype = v.get("CurrentInstanceType") or v.get("InstanceType") or ""
    if not itype:
        cfg = sm.describe_endpoint_config(EndpointConfigName=d["EndpointConfigName"])
        itype = cfg["ProductionVariants"][0]["InstanceType"]
    return d["EndpointStatus"], int(v.get("CurrentInstanceCount", 0)), itype


def status_markdown() -> str:
    status, n, itype = endpoint_status()
    if status == "Missing":
        return f"🔴 Endpoint `{ENDPOINT}` not found in {REGION}. Deploy it first (infra/deploy.sh)."
    if status != "InService":
        return f"🟡 `{ENDPOINT}` is **{status}** ({itype})."
    if n == 0:
        return (f"⚪ `{ENDPOINT}` is **asleep** (0 × {itype}, costs nothing). "
                "The next song wakes it up: about 7 minutes extra.")
    return f"🟢 `{ENDPOINT}` is **awake** ({n} × {itype}, billing). Songs take about 20-90 s."


def set_capacity(n: int) -> str:
    try:
        sm.update_endpoint_weights_and_capacities(
            EndpointName=ENDPOINT,
            DesiredWeightsAndCapacities=[{"VariantName": "AllTraffic", "DesiredInstanceCount": n}],
        )
    except ClientError as exc:
        return f"⚠️ {exc.response['Error']['Message']}"
    return ("Waking up - give it about 7 minutes." if n else "Going to sleep - billing stops in a minute or two.") \
        + "\n\n" + status_markdown()


def _split(uri: str) -> tuple[str, str]:
    u = urlparse(uri)
    return u.netloc, u.path.lstrip("/")


def _exists(uri: str) -> bool:
    b, k = _split(uri)
    try:
        s3.head_object(Bucket=b, Key=k)
        return True
    except ClientError:
        return False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "song"


# --------------------------------------------------------------------------- #
# Generate
# --------------------------------------------------------------------------- #
def build_request(prompt, duration, seed, temperature, top_p, language, lyrics, sing) -> dict:
    req = {
        "prompt": prompt.strip(),
        "duration": int(duration),
        "temperature": float(temperature),
        "top_p": float(top_p),
        "language": language,
    }
    if seed is not None and int(seed) > 0:
        req["seed"] = int(seed)
    if lyrics and lyrics.strip():
        req["lyrics"] = lyrics.strip()
        req["sing"] = bool(sing)
    return req


def generate(*args):
    """Wraps _generate so every failure is logged with a traceback AND shown on the page."""
    try:
        yield from _generate(*args)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("generate failed")
        msg = exc.response["Error"]["Message"] if isinstance(exc, ClientError) else str(exc)
        yield f"❌ {type(exc).__name__}: {msg}", None, None, None


def _generate(prompt, duration, seed, temperature, top_p, language, lyrics, sing):
    """Gradio generator: yields (status, audio, files, meta) as the request progresses."""
    if not prompt or not prompt.strip():
        yield "⚠️ Write a prompt first.", None, None, None
        return
    status, n, itype = endpoint_status()
    if status != "InService":
        yield f"🔴 Endpoint is {status}. Deploy it first (infra/deploy.sh).", None, None, None
        return

    req = build_request(prompt, duration, seed, temperature, top_p, language, lyrics, sing)
    key = f"async/in/studio-{int(time.time() * 1000)}.json"
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(req, ensure_ascii=False).encode(),
                  ContentType="application/json")
    resp = smr.invoke_endpoint_async(
        EndpointName=ENDPOINT, InputLocation=f"s3://{BUCKET}/{key}",
        ContentType="application/json", InvocationTimeoutSeconds=1800,
    )
    out_uri, fail_uri = resp["OutputLocation"], resp.get("FailureLocation", "")
    LOG.info("request %s -> %s", key, out_uri)

    t0 = time.time()
    waking = n == 0
    while not _exists(out_uri):
        if fail_uri and _exists(fail_uri):
            b, k = _split(fail_uri)
            err = s3.get_object(Bucket=b, Key=k)["Body"].read().decode("utf-8", "replace")
            yield f"❌ The endpoint failed this request:\n\n```\n{err[:2000]}\n```", None, None, None
            return
        waited = int(time.time() - t0)
        if waited > TIMEOUT_SECONDS:
            yield f"⏱️ Gave up after {waited // 60} min. The result may still appear at {out_uri}", None, None, None
            return
        if waking:
            _, n, _ = endpoint_status()
            waking = n == 0
            msg = (f"😴 Endpoint was asleep - starting a {itype} machine… {waited // 60} min {waited % 60} s "
                   "(usually ~7 min, then the song itself)")
        else:
            msg = f"🎼 Composing on {itype}… {waited} s"
        yield msg, None, None, None
        time.sleep(POLL_SECONDS)

    b, k = _split(out_uri)
    blob = s3.get_object(Bucket=b, Key=k)["Body"].read()
    folder = OUT_ROOT / f"{datetime.now():%Y%m%d-%H%M%S}_{_slug(req['prompt'])}"
    folder.mkdir(parents=True, exist_ok=True)
    LOG.info("song ready: %s", folder)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(folder)
    (folder / "request.json").write_text(json.dumps(req, ensure_ascii=False, indent=2))
    meta = json.loads((folder / "meta.json").read_text()) if (folder / "meta.json").exists() else {}
    files = [str(p) for p in sorted(folder.iterdir()) if p.suffix in {".wav", ".mid", ".json"}]
    took = int(time.time() - t0)
    yield (f"✅ Done in {took} s (compute {meta.get('compute_seconds', '?')} s on {meta.get('device', '?')}). "
           f"Saved to `{folder}`"), str(folder / "song.wav"), files, meta


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def build_ui() -> gr.Blocks:
    with gr.Blocks(title="PromptMusic Studio") as ui:
        gr.Markdown("# 🎵 PromptMusic Studio\nDescribe the music; the model on SageMaker composes it.")
        with gr.Row():
            status = gr.Markdown(status_markdown())
        with gr.Row():
            refresh_btn = gr.Button("🔄 Refresh status", size="sm")
            wake_btn = gr.Button("☀️ Wake up", size="sm")
            sleep_btn = gr.Button("🌙 Sleep now (stop billing)", size="sm")

        with gr.Row():
            with gr.Column(scale=3):
                prompt = gr.Textbox(label="Prompt", lines=3,
                                    placeholder="e.g. a calm piano ballad with soft strings, slow and emotional")
                with gr.Row():
                    duration = gr.Slider(5, MAX_SECONDS, value=30, step=1, label="Length (seconds)")
                    seed = gr.Number(value=0, precision=0, label="Seed (0 = random; reuse to repeat a song)")
                with gr.Accordion("Advanced", open=False):
                    temperature = gr.Slider(0.5, 1.3, value=0.95, step=0.05,
                                            label="Temperature (higher = more adventurous)")
                    top_p = gr.Slider(0.5, 1.0, value=0.92, step=0.01, label="Top-p")
                    language = gr.Dropdown(["en", "ta"], value="en", label="Language (prompt / lyrics)")
                    lyrics = gr.Textbox(label="Lyrics (optional, one line per phrase)", lines=4)
                    sing = gr.Checkbox(value=True, label="Sing the lyrics (synthetic TTS voice - experimental)")
                go = gr.Button("🎼 Generate", variant="primary")
                gr.Examples(EXAMPLES, inputs=[prompt, duration, seed])
            with gr.Column(scale=2):
                progress = gr.Markdown("")
                audio = gr.Audio(label="Song", type="filepath")
                files = gr.File(label="Downloads (WAV, MIDI, meta)", file_count="multiple")
                with gr.Accordion("Details (meta.json)", open=False):
                    meta = gr.JSON()

        go.click(generate, [prompt, duration, seed, temperature, top_p, language, lyrics, sing],
                 [progress, audio, files, meta])
        refresh_btn.click(status_markdown, None, status)
        wake_btn.click(lambda: set_capacity(1), None, status)
        sleep_btn.click(lambda: set_capacity(0), None, status)
    return ui


if __name__ == "__main__":
    public = HOST not in LOCAL_HOSTS
    if (public or os.environ.get("PM_REQUIRE_LOGIN") == "1") and not USERS:
        raise SystemExit(
            f"Refusing to listen on {HOST} without a login: anyone who reaches the page could "
            "spend your AWS money.\nSet PM_USERS=\"name:password,...\" (or use PM_HOST=127.0.0.1)."
        )
    print(f"endpoint={ENDPOINT} bucket={BUCKET} region={REGION} outputs={OUT_ROOT} "
          f"listen={HOST}:{PORT} login={'on' if USERS else 'off'}")
    build_ui().queue(default_concurrency_limit=2).launch(
        server_name=HOST, server_port=PORT, auth=USERS or None, inbrowser=not public,
        show_error=True, allowed_paths=[str(OUT_ROOT)],
    )
