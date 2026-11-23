import os
import re
import json
import time
import random
import shutil
import zipfile
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# =========================================================
# 1. Configuration & Model Setup
# =========================================================
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
FISH_API_KEY = os.getenv("FISH_API_KEY", "").strip()
FISH_MODEL_ID = os.getenv("FISH_MODEL_ID", "7f92f8afb8ec43bf81429cc1c9199cb1").strip()

# 可直接把你的 DC Bot Token 與 Channel ID 貼在引號內（若 Render 環境變數有設則優先吃環境變數）
DISCORD_BOT_TOKEN = (
    os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("DISCORD_TOKEN")
    or "在這裡貼上你的_DC_BOT_TOKEN"
).strip()

DISCORD_CHANNEL_ID = (
    os.getenv("DISCORD_CHANNEL_ID")
    or "在這裡貼上你的_DC_CHANNEL_ID"
).strip()

DAILY_TTS_CHAR_LIMIT = 250
MONTHLY_TTS_CHAR_LIMIT = 8000

TEXT_MODELS = [
    os.getenv("GROQ_TEXT_MODEL", "").strip(),
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
]
TEXT_MODELS = [m for m in dict.fromkeys(TEXT_MODELS) if m]

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
WEEKDAY_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
HISTORY_LIMIT = 24

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_MODEL_DIR = BASE_DIR / "uploaded_model"
AUDIO_DIR = BASE_DIR / "generated_audio"
MEMORY_FILE = BASE_DIR / "web_reona_memory.json"

UPLOAD_MODEL_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=".")
CORS(app)

# =========================================================
# 2. Daily Life Topics & Minor Incidents
# =========================================================
RANDOM_PROACTIVE_TOPICS = [
    "剛在雪梨找到一間採光超棒的咖啡廳，點了杯拿鐵邊曬太陽邊畫圖，超舒服",
    "今天坐咖啡廳窗邊趕稿，剛把一張插畫配色定下來，超有成就感",
    "剛把案子檔案交出去了，決定出門買個小甜點吃一下",
    "帶著平板跟零食跑來公園長椅吹風畫草稿，雪梨今天天氣超讚",
    "跑來逛雪梨的週末市集，看到超多可愛小物跟花束，心情超好",
    "剛買了一束小花帶回家插在桌上，看著房間心情都變好了",
    "傍晚跑來海邊散步吹風看夕陽，天空顏色超美忍不住想傳給你看",
    "剛剛在路邊看到一隻超可愛的黃金獵犬對我搖尾巴，差點被萌死",
    "買了一個木質調香氛蠟燭回家點，整個房間香香的超放鬆",
    "剛吃到一家超讚的早午餐可頌，酥到不行超幸福",
    "點餐時特地跟店員說不要香菜跟紅蘿蔔，全部吃光光超滿足",
    "超扯 剛點外送結果店家忘記附醬料，對著餐點發呆三秒好好笑",
    "出門散步走到一半才發現耳機完全沒電，好煩喔只好聽路邊風聲",
    "剛回到家麻糬就衝過來蹭腳翻肚討摸，可愛到不行",
    "麻糬在房間陽光底下攤成一坨奶油麻糬睡翻，超呆萌",
    "麻糬剛剛討罐頭失敗，現在背對我坐角落生悶氣，背影超好笑",
    "今天天氣好到超適合出門約會，突然好想你喔，你今天過得開不開心",
    "剛逛街看到一件超適合你穿的衣服，想到你 180 穿起來一定很帥",
]

LIFE_MINOR_INCIDENTS = [
    "好煩喔 剛交出去的插畫稿被客戶臨時說要大改，有點小委屈想找你討拍",
    "哭啊 剛剛手滑把冰咖啡打翻在桌上，差點弄到畫稿，現在正忙著擦桌子",
    "超扯 雪梨天氣突然下雨，我出門沒帶傘一路小跑回家，頭髮都濕了",
    "麻糬剛剛跳上桌子把水杯打翻，現在正在擦地板好無奈",
    "特地跑去排隊買招牌可頌，結果輪到我剛好賣完，超失落的",
]

# =========================================================
# 3. Persistent State, TTS Quota & Memory
# =========================================================
DEFAULT_MEMORY = {
    "recent_turns": [],
    "long_term_notes": [],
    "menstrual_cycle": {
        "cycle_start_date": "",
        "cycle_length": 30,
    },
    "sick_until_ts": 0.0,
    "sick_cooldown_until_ts": 0.0,
    "sick_type": "",        # "meds" or "er"
    "sick_reason": "",      # "food_poisoning" or "stomach"
    "need_recovery_report": False,
    "mild_sick_until_ts": 0.0,
    "mild_sick_cd_until_ts": 0.0,
    "mild_reason": "",      # "food_poisoning" or "bloating"
    "need_mild_recovery_report": False,
    "current_activity": "在雪梨咖啡廳邊曬太陽邊畫插畫",
    "last_dc_sync_at": "",
    "tts_quota": {
        "day_key": "",
        "day_chars": 0,
        "month_key": "",
        "month_chars": 0,
    },
}


def is_dc_configured() -> bool:
    return (
        bool(DISCORD_BOT_TOKEN)
        and "在這裡貼上" not in DISCORD_BOT_TOKEN
        and bool(DISCORD_CHANNEL_ID)
        and "在這裡貼上" not in DISCORD_CHANNEL_ID
    )


def load_memory() -> dict:
    default_start = (datetime.now(SYDNEY_TZ).date() - timedelta(days=22)).isoformat()
    if not MEMORY_FILE.exists():
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        merged = json.loads(json.dumps(DEFAULT_MEMORY))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        if not merged["menstrual_cycle"].get("cycle_start_date"):
            merged["menstrual_cycle"]["cycle_start_date"] = default_start
        return merged
    except Exception:
        mem = json.loads(json.dumps(DEFAULT_MEMORY))
        mem["menstrual_cycle"]["cycle_start_date"] = default_start
        return mem


def save_memory(mem: dict) -> None:
    try:
        MEMORY_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as err:
        print(f"[Memory Save Error] {err}", flush=True)


memory = load_memory()


def sync_tts_quota_period(now_dt: datetime) -> dict:
    day_key = now_dt.strftime("%Y-%m-%d")
    month_key = now_dt.strftime("%Y-%m")
    q = memory.setdefault("tts_quota", {})
    changed = False
    if q.get("day_key") != day_key:
        q["day_key"] = day_key
        q["day_chars"] = 0
        changed = True
    if q.get("month_key") != month_key:
        q["month_key"] = month_key
        q["month_chars"] = 0
        changed = True
    if changed:
        save_memory(memory)
    return q


def get_tts_quota_info(now_dt: datetime | None = None) -> dict:
    if now_dt is None:
        now_dt = datetime.now(SYDNEY_TZ)
    q = sync_tts_quota_period(now_dt)
    day_used = int(q.get("day_chars", 0))
    month_used = int(q.get("month_chars", 0))
    day_rem = max(0, DAILY_TTS_CHAR_LIMIT - day_used)
    month_rem = max(0, MONTHLY_TTS_CHAR_LIMIT - month_used)
    available_today = min(day_rem, month_rem)
    return {
        "configured": bool(FISH_API_KEY),
        "day_used": day_used,
        "day_limit": DAILY_TTS_CHAR_LIMIT,
        "day_remaining": day_rem,
        "month_used": month_used,
        "month_limit": MONTHLY_TTS_CHAR_LIMIT,
        "month_remaining": month_rem,
        "available_today": available_today,
        "exhausted": available_today <= 0,
    }


def get_cycle_day_num(now_dt: datetime) -> int:
    cyc = memory["menstrual_cycle"]
    today = now_dt.date()
    try:
        start_d = date.fromisoformat(cyc.get("cycle_start_date", today.isoformat()))
    except ValueError:
        start_d = today - timedelta(days=22)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)

    length = max(21, int(cyc.get("cycle_length", 30)))
    elapsed = (today - start_d).days
    if elapsed < 0:
        start_d = today - timedelta(days=22)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)
        elapsed = 22
    elif elapsed >= length:
        cycles_passed = elapsed // length
        start_d = start_d + timedelta(days=cycles_passed * length)
        cyc["cycle_start_date"] = start_d.isoformat()
        save_memory(memory)
        elapsed = (today - start_d).days
    return max(1, elapsed + 1)


def is_currently_on_period(now_dt: datetime) -> bool:
    return 1 <= get_cycle_day_num(now_dt) <= 5


def get_status_snapshot() -> dict:
    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]

    condition_label = "活力滿點・正常狀態"
    condition_code = "normal"
    emotion = "happy"

    if now_ts < memory.get("sick_until_ts", 0.0):
        mins = max(1, int((memory["sick_until_ts"] - now_ts) / 60))
        why = "吃壞肚子" if memory.get("sick_reason") == "food_poisoning" else "腸胃不適"
        if memory.get("sick_type") == "er":
            condition_label = f"{why}嚴重絞痛（雪梨急診中・剩 {mins} 分）"
        else:
            condition_label = f"{why}發作（已吞腸胃藥休息・剩 {mins} 分）"
        condition_code = "sick"
        emotion = "sad"
    elif now_ts < memory.get("mild_sick_until_ts", 0.0):
        mins = max(1, int((memory["mild_sick_until_ts"] - now_ts) / 60))
        why = "輕微吃壞肚子" if memory.get("mild_reason") == "food_poisoning" else "胃脹氣／低血糖"
        condition_label = f"中等不適（{why}・剩 {mins} 分）"
        condition_code = "mild_sick"
        emotion = "tired"
    elif is_currently_on_period(now_dt):
        day_num = get_cycle_day_num(now_dt)
        condition_label = f"生理期第 {day_num} 天（下腹悶悶怕冷）"
        condition_code = "period"
        emotion = "shy"

    model_rel_path = find_uploaded_model_rel_path()
    return {
        "sydney_time": f"{now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}",
        "condition_label": condition_label,
        "condition_code": condition_code,
        "emotion": emotion,
        "activity": memory.get("current_activity", "在雪梨咖啡廳邊曬太陽邊畫插畫"),
        "model_loaded": bool(model_rel_path),
        "model_url": f"/model/{model_rel_path}" if model_rel_path else "",
        "model_name": Path(model_rel_path).name if model_rel_path else "",
        "dc_configured": is_dc_configured(),
        "last_dc_sync_at": memory.get("last_dc_sync_at", ""),
        "tts_quota": get_tts_quota_info(now_dt),
    }


# =========================================================
# 4. Persona Prompt, Output Filtering & Quota-Guarded TTS
# =========================================================
BAD_CHAR_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff姎懓懞擝奼懅鼉齾玈尞攟臙屴尛沝炋朰攴攕襾]")
SIMPLIFIED_RE = re.compile(r"[没这么还说觉点个样对欢会时间里边着过为发来动亲宝怎么给让]")
COLON_PREFIX_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z]{1,8}[:：]|[:：=＝]")


def clean_and_validate(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    lines = [ln.strip() for ln in s.splitlines() if ln.strip() and not ln.strip().startswith(("•", "-", "*", "思考", "分析"))]
    s = lines[-1] if lines else s
    s = re.sub(r"（[^）]{1,40}）|\([^)]{1,40}\)", "", s)
    s = s.strip("\"'「」『』").rstrip("。").strip()

    if len(s) < 2 or len(s) > 90:
        return ""
    if BAD_CHAR_RE.search(s) or SIMPLIFIED_RE.search(s):
        return ""
    if COLON_PREFIX_RE.search(s):
        return ""
    for l_br, r_br in [("「", "」"), ("『", "』"), ("（", "）"), ("(", ")")]:
        if s.count(l_br) != s.count(r_br):
            return ""
    return s


def build_system_prompt(now_dt: datetime) -> str:
    weekday_zh = WEEKDAY_NAMES_ZH[now_dt.weekday()]
    hour = now_dt.hour
    if 23 <= hour or hour < 5:
        time_mood = "深夜關了燈在床上滑手機，語氣輕柔安靜、很放鬆"
    elif 5 <= hour < 11:
        time_mood = "剛睡醒還在賴床，聲音慵懶輕柔"
    else:
        time_mood = "白天或傍晚，精神很好、語氣開朗輕快又溫柔"

    day_num = get_cycle_day_num(now_dt)
    if day_num in (1, 2):
        cycle_note = f"【確定狀態】你目前正是生理期第{day_num}天（下腹悶痛、腰酸怕冷，不要否認）"
    elif 3 <= day_num <= 5:
        cycle_note = f"【確定狀態】你目前是生理期第{day_num}天（痛感減輕但容易累）"
    else:
        cycle_note = "平日正常狀態（精神好、愛出門享受生活；40公斤偏瘦體質，偶爾貪吃會吃壞肚子）"

    notes = memory.get("long_term_notes", [])
    notes_block = "\n".join(f"- {n}" for n in notes[-8:]) if notes else "（暫無特別備忘）"
    history_block = "\n".join(memory.get("recent_turns", [])[-HISTORY_LIMIT:]) or "（剛開始聊天）"

    return (
        "你是 Reona，跟 Nick 穩定交往的遠距離女友。你獨自在澳洲雪梨生活，是自由接案插畫師，養一隻奶油白短腿貓「麻糬」，挑食不吃香菜跟紅蘿蔔。你很瘦（40公斤）、皮膚白、臉也好看，對外貌有從容的自信；你也知道 Nick 身高 180 公分、體重 67 公斤（偏瘦高挑）、皮膚微黑，跟你站在一起剛好是黑白配跟身高差。\n"
        "【生活與個性底色：開朗溫柔的台灣女生、有狀況第一時間依賴 Nick】你是一個情緒價值高、很懂得享受雪梨生活的台灣女生：平常接案畫圖之餘，喜歡出門曬太陽、找採光好的咖啡廳畫稿、逛週末市集買花、去海邊散步看夕陽、吃早午餐跟甜點，回家喜歡點香氛蠟燭跟麻糬玩。一旦在外面吃壞肚子、肚子痛、生理期不適，或是遇到改稿委屈、淋雨等突發小狀況，你都會第一時間主動告訴 Nick、軟軟討拍或報備，好轉後也會主動跟他說一聲讓他放心。\n"
        "【說話風格：超級道地的『台灣女生傳 LINE 口吻』（務必嚴格遵守）】\n"
        "1. 不管 Nick 用中文、英文還是中英夾雜跟你說話，你一律用繁體中文＋道地台灣女生口語自然回覆，不要切換成英文回話。\n"
        "2. 像真正在台灣長大的女生傳 LINE 跟男友聊天：短句自然斷開，常常用空格代替逗號，自然帶出「欸、啦、喔、嘛、真的假的、好好笑、好喔、超讚、超扯、好煩喔、哭啊、對啊」這類台灣日常口頭禪！避免太書面的文案詞彙。\n"
        "3. 嚴禁簡體字、日文假名、生僻字、中國/翻譯腔用詞或任何冒號前綴。\n"
        "4. 嚴禁第三人稱自稱：如果 Nick 喊你「寶貝」或「老婆」，那是他在叫你，你回答時絕對不能用「寶貝剛吃藥」這種第三人稱自稱自己！一律用「我」或直接省略主詞。\n"
        "5. 嚴禁重複背原因與複讀同一句：如果對話紀錄裡已經提過不舒服的原因，後面 Nick 再關心你時，絕對不要再把原因重講一遍，只要自然回答當下感受即可。\n"
        "6. 徹底禁演言情小說與假肉麻：禁止「暖暖的抱抱」、「透過螢幕」這類旁白腔。\n"
        "7. 句子長度與連發節奏：一次只輸出 8～32 字的自然短話，不要寫長文、不要句號結尾。若想拆成兩則連續傳訊，可用單一 `||` 隔開兩段短句，最多兩段。\n"
        f"【當下狀態】雪梨 {now_dt.strftime('%m/%d')}（{weekday_zh}）{now_dt.strftime('%H:%M')}｜{time_mood}｜{cycle_note}\n"
        f"【近期生活備忘】\n{notes_block}\n"
        f"【近期對話紀錄】\n{history_block}"
    )


def call_groq_chat(sys_prompt: str, user_prompt: str) -> str:
    if not GROQ_API_KEY:
        return ""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    for model_name in TEXT_MODELS:
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.78,
                    "max_tokens": 450,
                },
                timeout=22,
            )
            if resp.status_code != 200:
                print(f"[Groq Skip {resp.status_code}] {model_name}: {resp.text[:120]}", flush=True)
                continue
            data = resp.json()
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            cleaned = clean_and_validate(raw)
            if cleaned:
                return cleaned
        except Exception as err:
            print(f"[Groq Error] {model_name}: {err}", flush=True)
    return ""


def synthesize_fish_audio(text: str) -> tuple[str, str]:
    now_dt = datetime.now(SYDNEY_TZ)
    q_info = get_tts_quota_info(now_dt)
    if not FISH_API_KEY or not text:
        return "", ""
    if q_info["exhausted"]:
        return "", "今日語音已達 250 字上限，已改以純文字回覆"

    speech_text = re.sub(r"\s+", " ", text.replace("||", "，")).strip()
    char_count = len(speech_text)
    if char_count <= 0:
        return "", ""
    if char_count > q_info["available_today"]:
        return "", f"剩餘語音字數不足（剩 {q_info['available_today']} 字），已改以純文字回覆"

    try:
        resp = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": speech_text,
                "reference_id": FISH_MODEL_ID,
                "format": "mp3",
            },
            timeout=20,
        )
        if resp.status_code == 200 and resp.content:
            q = sync_tts_quota_period(now_dt)
            q["day_chars"] = int(q.get("day_chars", 0)) + char_count
            q["month_chars"] = int(q.get("month_chars", 0)) + char_count
            save_memory(memory)

            fname = f"tts_{int(time.time() * 1000)}.mp3"
            out_path = AUDIO_DIR / fname
            out_path.write_bytes(resp.content)
            files = sorted(AUDIO_DIR.glob("tts_*.mp3"), key=lambda p: p.stat().st_mtime)
            for old in files[:-15]:
                old.unlink(missing_ok=True)
            return f"/audio/{fname}", ""
    except Exception as err:
        print(f"[Fish TTS Error] {err}", flush=True)
    return "", ""


# =========================================================
# 5. Bidirectional Discord Sync Helpers
# =========================================================
def merge_turns_into_memory(new_turns: list[str]) -> list[str]:
    existing = memory.get("recent_turns", [])
    combined = list(existing)
    for t in new_turns:
        clean_t = t.strip()
        if clean_t and clean_t not in combined[-HISTORY_LIMIT:]:
            combined.append(clean_t)
    memory["recent_turns"] = combined[-HISTORY_LIMIT:]
    save_memory(memory)
    return memory["recent_turns"]


def push_message_to_discord(text: str, speaker_prefix: str = "") -> bool:
    if not is_dc_configured() or not text:
        return False
    content = f"{speaker_prefix}{text}".strip()[:1900]
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"content": content},
            timeout=12,
        )
        return resp.status_code in (200, 201)
    except Exception as err:
        print(f"[DC Push Error] {err}", flush=True)
        return False


def fetch_discord_channel_turns(limit: int = 20) -> tuple[bool, list[dict], str]:
    if not is_dc_configured():
        return False, [], "尚未填入 DC Bot Token 或 Channel ID（仍可用貼上 DC 紀錄同步）"

    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?limit={min(max(limit, 1), 50)}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return False, [], f"Discord 讀取失敗（HTTP {resp.status_code}）"

        raw_msgs = resp.json()
        if not isinstance(raw_msgs, list):
            return False, [], "Discord 回傳格式異常"

        items = []
        turns = []
        for m in reversed(raw_msgs):
            content = (m.get("content") or "").strip()
            if not content and m.get("attachments"):
                content = "[傳了一張照片]"
            if not content or content.startswith("`[Reona 狀態]") or content in ("!狀態", "狀態", "/status"):
                continue
            author = m.get("author") or {}
            is_bot = bool(author.get("bot"))
            sender = "reona" if is_bot else "nick"
            label = "你" if is_bot else "Nick"
            items.append({"id": str(m.get("id", "")), "sender": sender, "text": content})
            turns.append(f"{label}：{content}")

        merge_turns_into_memory(turns)
        now_dt = datetime.now(SYDNEY_TZ)
        memory["last_dc_sync_at"] = now_dt.strftime("%H:%M:%S")
        save_memory(memory)
        return True, items, f"已同步 {len(items)} 則 DC 訊息"
    except Exception as err:
        return False, [], f"同步 DC 發生錯誤：{err}"


def parse_pasted_dc_text(raw_text: str) -> tuple[list[dict], list[str]]:
    items = []
    turns = []
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("Nick：", "Nick:", "你：", "你:", "reona：", "reona:", "Reona：", "Reona:")):
            prefix, rest = re.split(r"[:：]", s, maxsplit=1)
            body = rest.strip()
            if not body:
                continue
            if prefix.strip().lower() in ("reona", "你"):
                items.append({"sender": "reona", "text": body})
                turns.append(f"你：{body}")
            else:
                items.append({"sender": "nick", "text": body})
                turns.append(f"Nick：{body}")
        else:
            items.append({"sender": "reona", "text": s})
            turns.append(f"你：{s}")
    return items, turns


# =========================================================
# 6. Server-Side ZIP Model Extraction
# =========================================================
def find_uploaded_model_rel_path() -> str:
    if not UPLOAD_MODEL_DIR.exists():
        return ""
    candidates = sorted(UPLOAD_MODEL_DIR.rglob("*.model3.json"))
    for p in candidates:
        if "__MACOSX" not in p.parts:
            return p.relative_to(UPLOAD_MODEL_DIR).as_posix()
    return ""


@app.post("/upload_model")
def upload_model():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "沒有收到檔案"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".zip"):
        return jsonify({"ok": False, "error": "請上傳 .zip 格式的角色壓縮檔"}), 400

    if UPLOAD_MODEL_DIR.exists():
        shutil.rmtree(UPLOAD_MODEL_DIR, ignore_errors=True)
    UPLOAD_MODEL_DIR.mkdir(parents=True, exist_ok=True)

    zip_path = UPLOAD_MODEL_DIR / "character_upload.zip"
    f.save(zip_path)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    continue
                if "__MACOSX" in member_path.parts:
                    continue
                target = UPLOAD_MODEL_DIR / member_path
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        zip_path.unlink(missing_ok=True)
    except Exception as err:
        return jsonify({"ok": False, "error": f"解壓縮失敗：{err}"}), 400

    rel_path = find_uploaded_model_rel_path()
    if not rel_path:
        return jsonify({
            "ok": False,
            "error": "ZIP 已解壓，但找不到 .model3.json（聊天功能仍正常）",
        }), 200

    return jsonify({
        "ok": True,
        "model_url": f"/model/{rel_path}",
        "model_name": Path(rel_path).name,
    })


@app.get("/model/<path:filename>")
def serve_model_file(filename: str):
    return send_from_directory(UPLOAD_MODEL_DIR, filename)


@app.get("/audio/<path:filename>")
def serve_audio_file(filename: str):
    return send_from_directory(AUDIO_DIR, filename)


# =========================================================
# 7. Chat, Proactive Events, DC Sync & Diagnostics Routes
# =========================================================
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/status")
@app.get("/dc_status")
def status():
    return jsonify({"ok": True, "status": get_status_snapshot()})


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "groq_key_set": bool(GROQ_API_KEY),
        "groq_key_prefix": GROQ_API_KEY[:6] + "..." if GROQ_API_KEY else "",
        "fish_key_set": bool(FISH_API_KEY),
        "dc_configured": is_dc_configured(),
        "models": TEXT_MODELS,
        "status": get_status_snapshot(),
    })


@app.route("/sync_dc", methods=["GET", "POST"])
def sync_dc():
    data = request.get_json(silent=True) or {}
    pasted = (data.get("text") or data.get("transcript") or "").strip()

    if pasted:
        items, turns = parse_pasted_dc_text(pasted)
        merge_turns_into_memory(turns)
        now_dt = datetime.now(SYDNEY_TZ)
        memory["last_dc_sync_at"] = now_dt.strftime("%H:%M:%S")
        save_memory(memory)
        return jsonify({
            "ok": True,
            "messages": items,
            "message": f"已匯入 {len(items)} 則 DC 對話",
            "status": get_status_snapshot(),
        })

    ok, items, msg = fetch_discord_channel_turns(limit=20)
    return jsonify({
        "ok": ok,
        "messages": items,
        "message": msg,
        "status": get_status_snapshot(),
    })


@app.post("/chat")
def chat():
    data = request.get_json(silent=True) or {}
    user_text = (data.get("message") or data.get("text") or "").strip()
    want_voice = bool(data.get("voice", False))
    sync_to_dc = bool(data.get("sync_to_dc", False))
    if not user_text:
        return jsonify({"ok": False, "error": "訊息不能為空"}), 400

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()

    if any(k in user_text for k in ["考試", "上班", "下班", "去看醫生", "生病", "頭痛", "累", "週末", "作業", "報告"]):
        note = f"[{now_dt.strftime('%m/%d')}] Nick 提到：{user_text[:36]}"
        memory.setdefault("long_term_notes", []).append(note)
        memory["long_term_notes"] = memory["long_term_notes"][-8:]

    sys_prompt = build_system_prompt(now_dt)

    if now_ts < memory.get("sick_until_ts", 0.0):
        sys_prompt += "\n【本輪持續狀態】你現在因為吃壞肚子或腸胃發作正在休息，回話簡短虛弱溫柔，不要重複背原因。"
    elif now_ts < memory.get("mild_sick_until_ts", 0.0):
        sys_prompt += "\n【本輪持續狀態】你現在有點吃壞肚子悶痛或頭暈沒力氣，正躺在床上休息，語氣軟軟略帶撒嬌。"

    reply = call_groq_chat(sys_prompt, user_text)
    if not reply:
        reply = "欸 剛才分心了一下 你再說一次"

    turns = memory.setdefault("recent_turns", [])
    turns.append(f"Nick：{user_text}")
    turns.append(f"你：{reply.replace('||', ' ')}")
    memory["recent_turns"] = turns[-HISTORY_LIMIT:]
    save_memory(memory)

    dc_pushed = False
    if sync_to_dc:
        push_message_to_discord(user_text, "Nick：")
        for line in reply.split("||"):
            clean_line = line.strip()
            if clean_line:
                dc_pushed = push_message_to_discord(clean_line, "Reona：") or dc_pushed

    audio_url, tts_note = synthesize_fish_audio(reply) if want_voice else ("", "")
    st = get_status_snapshot()
    return jsonify({
        "ok": True,
        "reply": reply,
        "audio_url": audio_url,
        "tts_note": tts_note,
        "dc_pushed": dc_pushed,
        "emotion": st["emotion"],
        "status": st,
    })


@app.post("/proactive")
def proactive():
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "auto").strip()
    want_voice = bool(data.get("voice", False))
    sync_to_dc = bool(data.get("sync_to_dc", False))

    now_dt = datetime.now(SYDNEY_TZ)
    now_ts = now_dt.timestamp()
    sys_prompt = build_system_prompt(now_dt)

    if mode == "auto":
        if memory.get("need_recovery_report") and now_ts >= memory.get("sick_until_ts", 0.0):
            mode = "recover"
        elif memory.get("need_mild_recovery_report") and now_ts >= memory.get("mild_sick_until_ts", 0.0):
            mode = "recover"
        else:
            mode = "incident" if random.random() < 0.38 else "chit_chat"

    if mode == "recover":
        memory["need_recovery_report"] = False
        memory["need_mild_recovery_report"] = False
        memory["sick_until_ts"] = 0.0
        memory["mild_sick_until_ts"] = 0.0
        memory["current_activity"] = "剛睡醒好多了，正在喝溫水休息"
        save_memory(memory)
        prompt = "（你稍早吃壞肚子或不舒服，休息後現在好多了，主動傳一則 10～26 字道地台灣女生短訊跟 Nick 報平安，讓他別擔心）"
    elif mode == "incident":
        roll = random.random()
        if roll < 0.55:
            memory["mild_sick_until_ts"] = now_ts + 1800
            memory["need_mild_recovery_report"] = True
            memory["mild_reason"] = "food_poisoning"
            memory["current_activity"] = "吃壞肚子悶痛，躺在床上休息"
            save_memory(memory)
            prompt = "（你剛才在外面吃到不太新鮮的早午餐或甜點，現在吃壞肚子悶痛反胃剛跑完廁所，第一時間主動傳一則 10～26 字道地台灣女生短訊跟 Nick 說你好像吃壞肚子了好難受）"
        elif roll < 0.75:
            memory["sick_until_ts"] = now_ts + 5400
            memory["need_recovery_report"] = True
            memory["sick_reason"] = "food_poisoning"
            memory["sick_type"] = "meds"
            memory["current_activity"] = "吃壞肚子絞痛，剛吞腸胃藥躺平"
            save_memory(memory)
            prompt = "（你剛才吃壞肚子絞痛狂跑廁所，剛吞了腸胃藥準備躺平休息，第一時間主動傳一則 10～26 字虛弱台灣女生短訊跟 Nick 報備討拍）"
        else:
            inc_desc = random.choice(LIFE_MINOR_INCIDENTS)
            memory["current_activity"] = inc_desc[:24]
            save_memory(memory)
            prompt = f"（突發小插曲：{inc_desc}。第一時間傳一則 10～26 字道地台灣女生短訊跟 Nick 分享或小小討拍）"
    else:
        topic = random.choice(RANDOM_PROACTIVE_TOPICS)
        memory["current_activity"] = topic[:26]
        save_memory(memory)
        prompt = f"（日常瑣事分享：{topic}。隨手傳一句 8～26 字道地台灣女生日常短訊給 Nick，口氣隨性自然）"

    reply = call_groq_chat(sys_prompt, prompt)
    if not reply:
        reply = "欸 剛剛突然想到你 你在幹嘛"

    turns = memory.setdefault("recent_turns", [])
    turns.append(f"你：{reply.replace('||', ' ')}")
    memory["recent_turns"] = turns[-HISTORY_LIMIT:]
    save_memory(memory)

    dc_pushed = False
    if sync_to_dc:
        for line in reply.split("||"):
            clean_line = line.strip()
            if clean_line:
                dc_pushed = push_message_to_discord(clean_line, "Reona：") or dc_pushed

    audio_url, tts_note = synthesize_fish_audio(reply) if want_voice else ("", "")
    st = get_status_snapshot()
    return jsonify({
        "ok": True,
        "reply": reply,
        "audio_url": audio_url,
        "tts_note": tts_note,
        "dc_pushed": dc_pushed,
        "emotion": st["emotion"],
        "status": st,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
