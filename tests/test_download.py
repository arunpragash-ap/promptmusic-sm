"""download_file must survive flaky servers (regression: SHA-256 mismatch on one
SageMaker host after a truncated MidiCaps train.json download)."""
from __future__ import annotations

import hashlib
import http.server
import threading

import pytest

from promptmusic.data import download as dl

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB


class _Handler(http.server.BaseHTTPRequestHandler):
    plan: list[str] = []  # one behaviour per request: "short", "norange", "corrupt", "ok"

    def log_message(self, *a):  # keep pytest output quiet
        pass

    def do_GET(self):
        mode = self.plan.pop(0) if self.plan else "ok"
        rng = self.headers.get("Range")
        start = int(rng.split("=")[1].rstrip("-")) if rng and mode != "norange" else 0
        body = PAYLOAD[start:]
        if mode == "corrupt":
            body = b"x" * len(body)
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # "short": promise the full length, send a third, then drop the connection.
        self.wfile.write(body[: len(body) // 3] if mode == "short" else body)
        if mode == "short":
            self.close_connection = True


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _spec(srv):
    return dl.FileSpec(
        url=f"http://127.0.0.1:{srv.server_address[1]}/f.bin", filename="f.bin",
        size=len(PAYLOAD), sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    )


@pytest.mark.parametrize("plan", [["short", "ok"], ["short", "norange"], ["corrupt", "ok"]])
def test_download_recovers(server, tmp_path, monkeypatch, plan):
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)
    _Handler.plan = list(plan)
    path = dl.download_file(_spec(server), tmp_path)
    assert path.read_bytes() == PAYLOAD


def test_download_gives_up_on_persistent_corruption(server, tmp_path, monkeypatch):
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)
    _Handler.plan = ["corrupt"] * 10
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        dl.download_file(_spec(server), tmp_path, hash_retries=1)
