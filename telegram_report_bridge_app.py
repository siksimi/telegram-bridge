from flask import Flask, request, jsonify, send_from_directory
import os
import json
import time
import threading
import secrets
import string
import requests
from urllib.parse import urlparse
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
                state = json.load(f)
                state.setdefault("users", {})
                return state
        except Exception:
            pass
    return {
        "users": {},
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


def empty_audio_state():
    return {
        "audio_url": None,
        "updated_at": None,
        "telegram_file_id": None,
        "telegram_message_id": None,
    }


def get_user_by_alias(alias):
    normalized = (alias or "").strip().lower()
    if not normalized:
        return None

    for user in state["users"].values():
        if user.get("alias") == normalized:
            user.setdefault("latest_audio", empty_audio_state())
            return user
    return None


def get_or_create_user(chat_id_str):
    user = state["users"].get(chat_id_str)
    created = False

    if not user:
        user = {
            "alias": generate_alias(),
            "text": "",
            "message_id": None,
            "updated_at": None,
            "bot_text": "",
            "bot_message_id": None,
            "bot_updated_at": None,
            "latest_audio": empty_audio_state(),
        }
        state["users"][chat_id_str] = user
        created = True
    else:
        user.setdefault("latest_audio", empty_audio_state())
        user.setdefault("bot_text", "")
        user.setdefault("bot_message_id", None)
        user.setdefault("bot_updated_at", None)

    return user, created


def get_user_by_chat_id(chat_id):
    chat_id_str = str(chat_id)
    return state["users"].get(chat_id_str)


def get_latest_text_payload(user):
    user_text = user.get("text", "")
    user_message_id = user.get("message_id")
    user_updated_at = user.get("updated_at")
    bot_text = user.get("bot_text", "")
    bot_message_id = user.get("bot_message_id")
    bot_updated_at = user.get("bot_updated_at")

    latest_sender = "user"
    latest_text = user_text
    latest_message_id = user_message_id
    latest_updated_at = user_updated_at

    if bot_updated_at and (not user_updated_at or bot_updated_at >= user_updated_at):
        latest_sender = "bot"
        latest_text = bot_text
        latest_message_id = bot_message_id
        latest_updated_at = bot_updated_at

    return {
        "text": latest_text or "",
        "message_id": latest_message_id,
        "updated_at": latest_updated_at,
        "latest_sender": latest_sender if latest_updated_at else None,
        "user_text": user_text,
        "user_message_id": user_message_id,
        "user_updated_at": user_updated_at,
        "bot_text": bot_text,
        "bot_message_id": bot_message_id,
        "bot_updated_at": bot_updated_at,
    }


def get_referenced_audio_filenames():
    keep_filenames = set()
    for user in state["users"].values():
        audio_url = (user.get("latest_audio") or {}).get("audio_url")
        if audio_url:
            keep_filenames.add(os.path.basename(audio_url))
    return keep_filenames


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


def download_external_audio_file(audio_url):
    parsed = urlparse(audio_url or "")
    ext = os.path.splitext(parsed.path)[1] or ".wav"
    filename = f"audio_{int(time.time())}{ext}"
    save_path = os.path.join(AUDIO_DIR, filename)

    r = requests.get(audio_url, timeout=60)
    r.raise_for_status()

    with open(save_path, "wb") as f:
        f.write(r.content)

    return filename, save_path


def set_latest_audio_for_user(user, filename, file_id=None, message_id=None):
    user["latest_audio"] = {
        "audio_url": f"{PUBLIC_BASE_URL}/audio/{filename}",
        "updated_at": now_kst_str(),
        "telegram_file_id": file_id,
        "telegram_message_id": message_id,
    }
    save_state()
    cleanup_old_audio(keep_filenames=get_referenced_audio_filenames())


def extract_audio_attachment(message):
    voice = message.get("voice")
    if voice and voice.get("file_id"):
        return {
            "file_id": voice.get("file_id"),
            "kind": "voice",
        }

    audio = message.get("audio")
    if audio and audio.get("file_id"):
        return {
            "file_id": audio.get("file_id"),
            "kind": "audio",
        }

    document = message.get("document") or {}
    file_name = (document.get("file_name") or "").lower()
    mime_type = (document.get("mime_type") or "").lower()
    if document.get("file_id") and (
        file_name.endswith((".wav", ".mp3", ".m4a", ".ogg", ".aac"))
        or mime_type.startswith("audio/")
    ):
        return {
            "file_id": document.get("file_id"),
            "kind": "document",
            "file_name": document.get("file_name"),
        }

    return None


# ---------------------------
# 로컬 최신 오디오 정리 (선택)
# ---------------------------

def cleanup_old_audio(keep_filenames=None):
    keep_filenames = set(keep_filenames or set())
    try:
        for name in os.listdir(AUDIO_DIR):
            if name in keep_filenames:
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
        user = get_user_by_alias(alias)
        if user:
            return jsonify(get_latest_text_payload(user))

    return jsonify({
        "text": "",
        "message_id": None,
        "updated_at": None,
        "latest_sender": None,
        "user_text": "",
        "user_message_id": None,
        "user_updated_at": None,
        "bot_text": "",
        "bot_message_id": None,
        "bot_updated_at": None,
    })


@app.get("/api/latest_audio")
def api_latest_audio():
    alias = request.args.get("alias", "").strip().lower()
    if not alias:
        return jsonify({"error": "alias required"}), 400

    with state_lock:
        user = get_user_by_alias(alias)
        if user:
            return jsonify(user.get("latest_audio", empty_audio_state()))
        return jsonify(empty_audio_state())


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


@app.post("/api/register_bot_audio")
def register_bot_audio():
    payload = request.get_json(silent=True) or {}
    alias = (payload.get("alias") or "").strip().lower()
    chat_id = payload.get("chat_id")
    file_id = (payload.get("file_id") or "").strip()
    audio_url = (payload.get("audio_url") or "").strip()
    message_id = payload.get("message_id")

    if not alias and not chat_id:
        return jsonify({"ok": False, "error": "alias or chat_id required"}), 400
    if not file_id and not audio_url:
        return jsonify({"ok": False, "error": "file_id or audio_url required"}), 400

    with state_lock:
        user = get_user_by_alias(alias) if alias else None
        if not user and chat_id is not None:
            user = get_user_by_chat_id(chat_id)
        if not user:
            return jsonify({"ok": False, "error": "user not found"}), 404

        try:
            if file_id:
                filename, _ = download_telegram_file(file_id)
                set_latest_audio_for_user(user, filename, file_id=file_id, message_id=message_id)
            else:
                filename, _ = download_external_audio_file(audio_url)
                set_latest_audio_for_user(user, filename, file_id=None, message_id=message_id)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "latest_audio": user.get("latest_audio")})


@app.post(f"/telegram/webhook/{WEBHOOK_SECRET}")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or {}
    )

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not chat_id:
        return jsonify({"ok": True, "ignored": True})

    text = message.get("text")
    audio_attachment = extract_audio_attachment(message)

    with state_lock:
        chat_id_str = str(chat_id)
        user, created = get_or_create_user(chat_id_str)

        # 1) 텍스트 처리
        if text:
            if text.strip().lower() == "/alias":
                try:
                    bot_text = (
                        f"[Telegram Bridge]\n\n"
                        f"Your alias: {user['alias']}\n"
                        f"Use this alias in RadSYS."
                    )
                    result = send_message(chat_id, bot_text)
                    user["bot_text"] = bot_text
                    user["bot_message_id"] = (result.get("result") or {}).get("message_id")
                    user["bot_updated_at"] = now_kst_str()
                    save_state()
                except Exception as e:
                    print("send_message failed:", e)
                return jsonify({"ok": True})

            user["text"] = text
            user["message_id"] = message_id
            user["updated_at"] = now_kst_str()
            save_state()

        # 2) 음성/오디오 파일 처리
        if audio_attachment:
            file_id = audio_attachment.get("file_id")
            try:
                filename, _ = download_telegram_file(file_id)
                set_latest_audio_for_user(user, filename, file_id=file_id, message_id=message_id)
            except Exception as e:
                print(f"{audio_attachment.get('kind', 'audio')} handling failed:", e)

        if created:
            try:
                bot_text = (
                    f"[Telegram Bridge]\n\n"
                    f"Your alias: {user['alias']}\n"
                    f"Use this alias in RadSYS."
                )
                result = send_message(chat_id, bot_text)
                user["bot_text"] = bot_text
                user["bot_message_id"] = (result.get("result") or {}).get("message_id")
                user["bot_updated_at"] = now_kst_str()
                save_state()
            except Exception as e:
                print("send_message failed:", e)

    return jsonify({"ok": True})


@app.route("/audio/<filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename, as_attachment=False)


# ---------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
