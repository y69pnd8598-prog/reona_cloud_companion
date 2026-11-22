import datetime
import json
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = os.environ.get("FISH_API_KEY", "").strip()
REFERENCE_ID = os.environ.get("FISH_REFERENCE_ID", "7f92f8afb8ec43bf81429cc1c9199cb1").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

MONTHLY_CHAR_LIMIT = 7800
VIDEO_MAX_CHARS = 180
VOICE_MSG_MAX_CHARS = 60
VOICE_MSG_DAILY_LIMIT = 5

BASE = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE, "wav_cache")
QUOTA_FILE = os.path.join(BASE, "fish_quota.json")
os.makedirs(AUDIO_DIR, exist_ok=True)


def load_quota():
    now = datetime.datetime.now()
    ym = now.strftime("%Y-%m")
    ymd = now.strftime("%Y-%m-%d")
    data = {"month": ym, "month_chars": 0, "day": ymd, "voice_msg_count": 0}
    if os.path.isfile(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                saved = json.loads(f.read())
            if saved.get("month") == ym:
                data["month_chars"] = int(saved.get("month_chars", 0))
            if saved.get("day") == ymd:
                data["voice_msg_count"] = int(saved.get("voice_msg_count", 0))
        except Exception:
            pass
    return data


def save_quota(data):
    with open(QUOTA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def trim_text(text, max_len):
    text = str(text or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for p in ("。", "！", "？", "~", "～", "，", ","):
        idx = cut.rfind(p)
        if idx >= int(max_len * 0.5):
            return cut[: idx + 1]
    return cut


def call_fish_tts(text, speed, out_path):
    if not API_KEY:
        raise RuntimeError("Missing FISH_API_KEY environment variable")
    payload = {
        "text": text,
        "reference_id": REFERENCE_ID,
        "format": "mp3",
        "mp3_bitrate": 128,
        "prosody": {"speed": float(speed), "volume": 0},
        "normalize": True,
    }
    req = urllib.request.Request(
        "https://api.fish.audio/v1/tts",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "model": "speech-1.6",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_bytes = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"FishAudio HTTP {e.code}: {detail}") from e
    with open(out_path, "wb") as f:
        f.write(audio_bytes)


def _post_groq_with_model(model_name, prompt, system_prompt):
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + GROQ_API_KEY.strip(),
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
    return res_data["choices"][0]["message"]["content"].strip()


def call_groq_chat(prompt, system_prompt="你是台灣口語感自然的聊天伴侶 Reona／薇薇安，語氣親切自然、簡潔好聊。"):
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY environment variable")

    candidates = []
    for m in (GROQ_MODEL, "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"):
        m_clean = (m or "").strip()
        if m_clean and m_clean not in candidates:
            candidates.append(m_clean)

    last_err = None
    for idx, model_name in enumerate(candidates):
        try:
            return _post_groq_with_model(model_name, prompt, system_prompt)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            last_err = RuntimeError(f"Groq HTTP {e.code} ({model_name}): {detail}")
            if e.code == 404 and "model_not_found" in detail and idx < len(candidates) - 1:
                continue
            raise last_err from e
    if last_err:
        raise last_err
    raise RuntimeError("No available Groq model")


class H(BaseHTTPRequestHandler):
    def _send_json(self, code_num, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code_num)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, fpath, content_type):
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _synth(self, txt, speed=1.0, mode="video"):
        try:
            q = load_quota()
            is_voice = str(mode).lower() in ("voice_msg", "dc_voice", "voice")
            if is_voice and q["voice_msg_count"] >= VOICE_MSG_DAILY_LIMIT:
                self._send_json(429, {"error": "daily voice limit reached", "quota": q})
                return
            max_len = VOICE_MSG_MAX_CHARS if is_voice else VIDEO_MAX_CHARS
            clean_txt = trim_text(txt, max_len)
            cost = len(clean_txt)
            if q["month_chars"] + cost > MONTHLY_CHAR_LIMIT:
                self._send_json(429, {"error": "monthly char limit reached", "quota": q})
                return
            fname = "reona_" + str(mode) + "_" + str(int(time.time() * 1000)) + ".mp3"
            out_path = os.path.join(AUDIO_DIR, fname)
            print("[Cloud-Fish]", mode, cost, "chars:", clean_txt)
            call_fish_tts(clean_txt, speed, out_path)
            q["month_chars"] += cost
            if is_voice:
                q["voice_msg_count"] += 1
            save_quota(q)
            url = "/audio/" + fname
            self._send_json(200, {"audio_url": url, "engine": "FishAudio", "quota": q})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            idx_path = os.path.join(BASE, "index.html")
            if os.path.isfile(idx_path):
                self._send_file(idx_path, "text/html; charset=utf-8")
                return
            self.send_response(404)
            self.end_headers()
            return
        if parsed.path == "/pixi.min.js":
            pixi_path = os.path.join(BASE, "pixi.min.js")
            if os.path.isfile(pixi_path):
                self._send_file(pixi_path, "application/javascript; charset=utf-8")
                return
        if parsed.path.startswith("/model/"):
            rel_path = urllib.parse.unquote(parsed.path[len("/model/") :])
            safe_rel = os.path.normpath(rel_path)
            model_root = os.path.abspath(os.path.join(BASE, "model"))
            target_path = os.path.abspath(os.path.join(model_root, safe_rel))

            found_model3 = None
            for root_dir, _, files in os.walk(model_root):
                for fn in files:
                    if fn.endswith(".model3.json"):
                        found_model3 = os.path.join(root_dir, fn)
                        break
                if found_model3:
                    break

            if not os.path.isfile(target_path) and found_model3:
                if safe_rel.endswith(".model3.json"):
                    target_path = found_model3
                else:
                    alt_path = os.path.abspath(os.path.join(os.path.dirname(found_model3), safe_rel))
                    if alt_path.startswith(model_root) and os.path.isfile(alt_path):
                        target_path = alt_path

            if target_path.startswith(model_root) and os.path.isfile(target_path):
                ext = os.path.splitext(target_path)[1].lower()
                mime_map = {
                    ".json": "application/json; charset=utf-8",
                    ".moc3": "application/octet-stream",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                }
                self._send_file(target_path, mime_map.get(ext, "application/octet-stream"))
                return
            self.send_response(404)
            self.end_headers()
            return
        if parsed.path == "/quota":
            self._send_json(200, load_quota())
            return
        if parsed.path.startswith("/audio/"):
            fname = os.path.basename(parsed.path)
            fpath = os.path.join(AUDIO_DIR, fname)
            if os.path.isfile(fpath):
                self._send_file(fpath, "audio/mpeg")
                return
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(parsed.query)
        if "text" not in q and parsed.path == "/tts":
            self._send_json(200, {"status": "ok"})
            return
        self._synth(
            q.get("text", ["哈囉"])[0],
            float(q.get("speed", ["1.0"])[0]),
            q.get("mode", ["video"])[0],
        )

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}

        if parsed.path == "/chat":
            try:
                user_msg = (
                    body.get("message")
                    or body.get("text")
                    or body.get("msg")
                    or body.get("content")
                    or raw
                    or "哈囉"
                )
                sys_prompt = body.get("system_prompt") or "你是台灣口語感自然的聊天伴侶 Reona／薇薇安，語氣親切自然、簡潔好聊。"
                reply = call_groq_chat(str(user_msg), str(sys_prompt))
                self._send_json(200, {"reply": reply, "text": reply})
            except Exception as e:
                traceback.print_exc()
                self._send_json(500, {"error": str(e)})
            return

        txt = body.get("text") or body.get("msg") or body.get("content") or raw or "哈囉"
        speed = float(body.get("speed", 1.0))
        mode = body.get("mode", "video")
        self._synth(txt, speed, mode)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"Cloud companion running on 0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), H).serve_forever()
