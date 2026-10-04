"""Serve the isolated mascot studio and a tightly bounded AI reaction preview."""

from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUDIO = ROOT / "mascot-studio"
SETUP = ROOT / "desktop" / "ui" / "assets" / "setup"
ACCESS_TOKEN = os.environ.get("STUDIO_ACCESS_TOKEN", "")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
ALLOWED_EMOTIONS = set("calm joy curious focused worried sleepy surprised proud shy sad determined affectionate".split())
ALLOWED_ACTIONS = set("idle wave nod peek point offer focus read write think listen speak reassure celebrate dance float stretch sip sleep".split())
# Each prop with the action it is made for (props.js `use`). The model sees the pairs, so a
# reaction holds something in the way the stage can show it instead of a book while dancing.
PROP_USE = {
    "book": "read", "notebook": "focus", "pencil": "write", "tablet": "read", "laptop": "focus", "calendar": "point",
    "scroll": "read", "map": "read", "hourglass": "listen", "clock": "point", "compass": "focus", "magnifier": "focus",
    "gear": "think", "key": "offer", "wrench": "focus", "crystal": "offer", "mug": "sip", "teapot": "sip",
    "plant": "offer", "lantern": "offer", "pillow": "sleep", "blanket": "sip", "palette": "write", "brush": "write",
    "camera": "focus", "music": "dance", "star": "celebrate", "microphone": "speak", "headphones": "listen",
    "envelope": "offer", "plane": "point", "bubble": "speak", "bell": "wave", "gift": "offer", "balloon": "float",
    "trophy": "celebrate", "heart": "reassure", "shield": "reassure",
}
ALLOWED_PROPS = set(PROP_USE)
MODELS: list[dict] = []
MODELS_AT = 0.0
MODEL_LOCK = threading.Lock()
REACTIONS: list[float] = []
REACTION_LOCK = threading.Lock()
# requests are served on threads; one line at a time keeps the log readable
LOG_LOCK = threading.Lock()


def log_line(text: str) -> None:
    with LOG_LOCK:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def request_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "Daedalus-Mascot-Studio/1"}
    if data is not None:
        headers.update({"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def models() -> list[dict]:
    global MODELS, MODELS_AT
    with MODEL_LOCK:
        if MODELS and time.monotonic() - MODELS_AT < 3600:
            return MODELS
        data = request_json("https://openrouter.ai/api/v1/models")
        result = []
        for item in data.get("data", []):
            model_id = item.get("id")
            output = (item.get("architecture") or {}).get("output_modalities") or ["text"]
            if isinstance(model_id, str) and model_id and not model_id.endswith(":batch") and "text" in output:
                result.append({"id": model_id, "name": item.get("name") or model_id})
        MODELS, MODELS_AT = result, time.monotonic()
        return MODELS


def parse_reaction(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("model returned no JSON")
    item = json.loads(match.group())
    emotion = item.get("emotion", "calm")
    action = item.get("action", "idle")
    prop = item.get("prop", "")
    line = str(item.get("line", "")).strip()[:120]
    if emotion not in ALLOWED_EMOTIONS or action not in ALLOWED_ACTIONS or prop not in ALLOWED_PROPS | {""} or not line:
        raise ValueError("model returned an unsupported reaction")
    return {"emotion": emotion, "action": action, "prop": prop, "line": line}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MascotStudio/1"

    def log_message(self, fmt: str, *args: object) -> None:
        log_line(f"{self.log_date_time_string()} {fmt % args}")

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        log_line(f"{self.log_date_time_string()} {self.command} {self.path.partition('?')[0]} {code} {size}")

    def json_response(self, status: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        if not ACCESS_TOKEN:
            return False
        cookies = self.headers.get("Cookie", "")
        return any(part.strip() == f"studio={ACCESS_TOKEN}" for part in cookies.split(";"))

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/" and query.get("access", [""])[0] == ACCESS_TOKEN and ACCESS_TOKEN:
            self.send_response(302)
            self.send_header("Set-Cookie", f"studio={ACCESS_TOKEN}; HttpOnly; SameSite=Lax; Path=/")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if not self.authorized():
            self.json_response(403, {"error": "Access link required"})
            return
        if url.path == "/api/models":
            try:
                q = query.get("q", [""])[0].lower().strip()[:80]
                found = [item for item in models() if q in item["id"].lower() or q in item["name"].lower()]
                self.json_response(200, {"models": found[:80], "total": len(found)})
            except (OSError, ValueError, urllib.error.HTTPError) as exc:
                self.json_response(502, {"error": f"Model catalogue unavailable: {type(exc).__name__}"})
            return
        path = url.path
        if path == "/":
            file = STUDIO / "index.html"
        elif path.startswith("/mascot-studio/"):
            file = (ROOT / path.lstrip("/")).resolve()
            if not file.is_relative_to(STUDIO):
                self.json_response(404, {"error": "Not found"})
                return
        elif path.startswith("/desktop/ui/assets/setup/"):
            file = (ROOT / path.lstrip("/")).resolve()
            if not file.is_relative_to(SETUP) or file.suffix != ".js":
                self.json_response(404, {"error": "Not found"})
                return
        else:
            self.json_response(404, {"error": "Not found"})
            return
        if not file.is_file():
            self.json_response(404, {"error": "Not found"})
            return
        kind = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}.get(file.suffix, "application/octet-stream")
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if not self.authorized() or self.path != "/api/react":
            self.json_response(403, {"error": "Access link required"})
            return
        if not API_KEY:
            self.json_response(503, {"error": "OpenRouter key is not configured"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 2048:
                self.json_response(413, {"error": "Event is too large"})
                return
            body = json.loads(self.rfile.read(size))
            model = str(body.get("model", ""))
            event = str(body.get("event", "")).strip()[:240]
            if model not in {item["id"] for item in models()} or not event:
                self.json_response(400, {"error": "Choose a model and describe an event"})
                return
            with REACTION_LOCK:
                now = time.time()
                REACTIONS[:] = [x for x in REACTIONS if now - x < 86400]
                if len(REACTIONS) >= 30 or (REACTIONS and now - REACTIONS[-1] < 8):
                    self.json_response(429, {"error": "Reaction limit reached; wait before trying again"})
                    return
                REACTIONS.append(now)
            system = (
                "You direct a gentle small 3D mascot inside a work app. Return only compact JSON with keys "
                "emotion, action, prop, line. The line must be in Russian, warm, useful, at most 100 characters; "
                "avoid unsolicited interruptions. Emotions: " + ", ".join(sorted(ALLOWED_EMOTIONS)) +
                ". Actions: " + ", ".join(sorted(ALLOWED_ACTIONS)) +
                ". Props, each with the action it suits: " + ", ".join(f"{prop} ({use})" for prop, use in sorted(PROP_USE.items())) +
                ". Prefer a prop that suits the action, or an empty prop. "
                "Choose one appropriate reaction. No markdown, no private data."
            )
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": event}],
                "reasoning": {"enabled": False},
                "max_tokens": 150,
                "temperature": 0.55,
            }
            forced_reasoning = False
            try:
                response = request_json("https://openrouter.ai/api/v1/chat/completions", payload)
            except urllib.error.HTTPError as exc:
                detail = exc.read(1024).decode(errors="replace").lower()
                if exc.code != 400 or "reasoning is mandatory" not in detail:
                    raise
                # Some models require reasoning. The explicit model choice still works, with its constraint shown in the UI.
                payload.pop("reasoning")
                forced_reasoning = True
                response = request_json("https://openrouter.ai/api/v1/chat/completions", payload)
            raw = response["choices"][0]["message"]["content"]
            reaction = parse_reaction(raw)
            self.json_response(200, {"reaction": reaction, "usage": response.get("usage", {}), "model": model, "forced_reasoning": forced_reasoning})
        except urllib.error.HTTPError as exc:
            self.json_response(502, {"error": f"OpenRouter returned HTTP {exc.code}"})
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            self.json_response(502, {"error": f"Reaction unavailable: {type(exc).__name__}"})


if __name__ == "__main__":
    if not ACCESS_TOKEN:
        raise SystemExit("STUDIO_ACCESS_TOKEN is required")
    http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("STUDIO_PORT", "8206"))), Handler).serve_forever()
