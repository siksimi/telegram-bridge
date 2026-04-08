from flask import Flask, request, jsonify, send_from_directory
import os
import json
import time
import threading
import secrets
import string
import requests
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

STATE_FILE = "state.json"
AUDIO_DIR = "/tmp/radsys_audio"

state_lock = threading.Lock()


# ---------------------------
# 상태 로드/저장
# ---------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "users": {},
        "latest_audio": {
            "audio_url": None,
            "updated_at": None,
            "telegram_file_id": None,
            "telegram_message_id": None
        }
    }


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = load_state()

os.makedirs(AUDIO_DIR, exist_ok=True)

KST = timezone(timedelta(hours=9))


# ---------------------------
# alias 생성 (4자리)
# ---------------------------

def generate_alias():
    alphabet = string.ascii_lowercase + string.digits
    while True:
        alias = "".join(secrets.choice(alphabet) for _ in range(4))
        exists = any(u.get("alias") == alias for u in state["users"].values())
        if not exists:
            return alias


def now_kst_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------
# Telegram API helper
# ---------------------------

def telegram_api(method, params=None, files=None, timeout=20):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if files:
        r = requests.post(url, data=params or {}, files=files, timeout=timeout)
    else:
        r = requests.post(url, json=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def send_message(chat_id, text):
    return telegram_api("sendMessage", {
        "chat_id": chat_id,
        "text": text
    })


def get_telegram_file_path(file_id):
    result = telegram_api("getFile", {"file_id": file_id})
    return result["result"]["file_path"]


def download_telegram_file(file_id):
    file_path = get_telegram_file_path(file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    ext = os.path.splitext(file_path)[1] or ".bin"
    filename = f"audio_{int(time.time())}{ext}"
    save_path = os.path.join(AUDIO_DIR, filename)

    r = requests.get(file_url, timeout=60)
    r.raise_for_status()

    with open(save_path, "wb") as f:
        f.write(r.content)

    return filename, save_path


# ---------------------------
# 로컬 최신 오디오 정리 (선택)
# ---------------------------

def cleanup_old_audio(keep_filename=None):
    try:
        for name in os.listdir(AUDIO_DIR):
            if keep_filename and name == keep_filename:
                continue
            path = os.path.join(AUDIO_DIR, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------
# API
# ---------------------------

@app.get("/")
def index():
    return "Telegram Report Bridge Running"


@app.get("/api/latest")
def api_latest():
    alias = request.args.get("alias", "").strip().lower()
    if not alias:
        return jsonify({"error": "alias required"}), 400

    with state_lock:
        for user in state["users"].values():
            if user.get("alias") == alias:
                return jsonify({
                    "text": user.get("text", ""),
                    "message_id": user.get("message_id"),
                    "updated_at": user.get("updated_at")
                })

    return jsonify({
        "text": "",
        "message_id": None,
        "updated_at": None
    })


@app.get("/api/latest_audio")
def api_latest_audio():
    with state_lock:
        return jsonify(state.get("latest_audio", {}))


@app.get("/api/users")
def api_users():
    # 내부 디버그용
    with state_lock:
        return jsonify(state)


@app.post("/setup-webhook")
def setup_webhook():
    if not BOT_TOKEN or not PUBLIC_BASE_URL:
        return jsonify({
            "ok": False,
            "error": "Set TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL first."
        }), 400

    target = f"{PUBLIC_BASE_URL}/telegram/webhook/{WEBHOOK_SECRET}"
    result = telegram_api("setWebhook", {"url": target})
    return jsonify(result)


@app.post(f"/telegram/webhook/{WEBHOOK_SECRET}")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not chat_id:
        return jsonify({"ok": True, "ignored": True})

    text = message.get("text")
    voice = message.get("voice")

    with state_lock:
        # 1) 텍스트 처리
        if text:
            chat_id_str = str(chat_id)
            user = state["users"].get(chat_id_str)

            if not user:
                alias = generate_alias()
                state["users"][chat_id_str] = {
                    "alias": alias,
                    "text": text,
                    "message_id": message_id,
                    "updated_at": now_kst_str()
                }
                save_state()

                try:
                    send_message(
                        chat_id,
                        f"[Telegram Bridge]\n\n"
                        f"Your alias: {alias}\n"
                        f"Use this alias in RadSYS."
                    )
                except Exception as e:
                    print("send_message failed:", e)
            else:
                user["text"] = text
                user["message_id"] = message_id
                user["updated_at"] = now_kst_str()
                save_state()

        # 2) 음성 처리
        if voice:
            file_id = voice.get("file_id")
            if file_id:
                try:
                    filename, _ = download_telegram_file(file_id)

                    cleanup_old_audio(keep_filename=filename)

                    state["latest_audio"] = {
                        "audio_url": f"{PUBLIC_BASE_URL}/audio/{filename}",
                        "updated_at": now_kst_str(),
                        "telegram_file_id": file_id,
                        "telegram_message_id": message_id
                    }
                    save_state()
                except Exception as e:
                    print("voice handling failed:", e)

    return jsonify({"ok": True})


@app.route("/audio/<filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename, as_attachment=False)


# ---------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
