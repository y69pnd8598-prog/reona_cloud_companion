import datetime, json, os, time, traceback, urllib.parse, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

API_KEY = ""
REFERENCE_ID = "7f92f8afb8ec43bf81429cc1c9199cb1"

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
            saved = json.loads(open(QUOTA_FILE, "r", encoding="utf-8").read())
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
            return cut[:idx + 1]
    return cut

def call_fish_tts(text, speed, out_path):
    payload = {
        "text": text,
        "reference_id": REFERENCE_ID,
        "format": "mp3",
        "mp3_bitrate": 128,
        "prosody": {"speed": float(speed), "volume": 0},
        "normalize": True
    }
    req = urllib.request.Request(
        "https://api.fish.audio/v1/tts",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
            "model": "speech-1.6"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        audio_bytes = resp.read()
    with open(out_path, "wb") as f:
        f.write(audio_bytes)

class H(BaseHTTPRequestHandler):
    def _send_json(self, code_num, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code_num)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
            print("[8889-Fish]", mode, cost, "chars:", clean_txt)
            call_fish_tts(clean_txt, speed, out_path)
            q["month_chars"] += cost
            if is_voice:
                q["voice_msg_count"] += 1
            save_quota(q)
            url = "http://127.0.0.1:8889/audio/" + fname
            self._send_json(200, {"audio_url": url, "engine": "FishAudio", "quota": q})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/quota":
            self._send_json(200, load_quota())
            return
        if parsed.path.startswith("/audio/"):
            fname = os.path.basename(parsed.path)
            fpath = os.path.join(AUDIO_DIR, fname)
            if os.path.isfile(fpath):
                data = open(fpath, "rb").read()
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(parsed.query)
        self._synth(q.get("text", ["哈囉"])[0], float(q.get("speed", ["1.0"])[0]), q.get("mode", ["video"])[0])

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        try:
            body = json.loads(raw)
            txt = body.get("text") or body.get("msg") or body.get("content") or "哈囉"
            speed = float(body.get("speed", 1.0))
            mode = body.get("mode", "video")
        except Exception:
            txt, speed, mode = raw or "哈囉", 1.0, "video"
        self._synth(txt, speed, mode)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *a):
        pass

print("8889 FishAudio TTS running on http://127.0.0.1:8889")
HTTPServer(("127.0.0.1", 8889), H).serve_forever()