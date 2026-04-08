from flask import Flask, request, jsonify, render_template_string
import os
import threading
import requests
import json
import time
import secrets
import string

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")

STATE_FILE = "state.json"
state_lock = threading.Lock()

# ---------------------------
# 상태 로드/저장
# ---------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"users": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

state = load_state()

# ---------------------------
# alias 생성 (4자리)
# ---------------------------

def generate_alias():
    alphabet = string.ascii_lowercase + string.digits
    while True:
        alias = ''.join(secrets.choice(alphabet) for _ in range(4))
        # 중복 체크
        if not any(u["alias"] == alias for u in state["users"].values()):
            return alias

# ---------------------------
# 텔레그램 메시지 보내기
# ---------------------------

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": text
    }, timeout=10)

# ---------------------------
# webhook
# ---------------------------

@app.post(f"/telegram/webhook/{WEBHOOK_SECRET}")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or {}
    chat = message.get("chat") or {}

    chat_id = str(chat.get("id"))
    text = message.get("text")

    if not chat_id or not text:
        return jsonify({"ok": True})

    with state_lock:
        user = state["users"].get(chat_id)

        # 신규 사용자
        if not user:
            alias = generate_alias()
            state["users"][chat_id] = {
                "alias": alias,
                "text": text,
                "message_id": message.get("message_id"),
                "updated_at": time.time()
            }
            save_state(state)

            # 사용자에게 alias 알려주기
            internal_url = f"http://192.148.102.51:8080/u/{alias}"

            send_message(chat_id,
                f"[Telegram Bridge]\n\n"
                f"Your alias: {alias}\n"
                f"Internal page:\n{internal_url}"
            )

        else:
            # 기존 사용자 → 텍스트만 업데이트
            user["text"] = text
            user["message_id"] = message.get("message_id")
            user["updated_at"] = time.time()
            save_state(state)

    return jsonify({"ok": True})

# ---------------------------
# API: alias 기반 조회
# ---------------------------

@app.get("/api/latest")
def api_latest():
    alias = request.args.get("alias")

    if not alias:
        return jsonify({"error": "alias required"}), 400

    with state_lock:
        for user in state["users"].values():
            if user["alias"] == alias:
                return jsonify({
                    "text": user.get("text"),
                    "message_id": user.get("message_id")
                })

    return jsonify({"text": ""})

# ---------------------------
# 디버그용 (옵션)
# ---------------------------

@app.get("/api/users")
def api_users():
    with state_lock:
        return jsonify(state)

# ---------------------------
# webhook 설정
# ---------------------------

@app.post("/setup-webhook")
def setup_webhook():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    target = f"{PUBLIC_BASE_URL}/telegram/webhook/{WEBHOOK_SECRET}"

    resp = requests.post(url, json={"url": target}, timeout=10)
    return jsonify(resp.json())

# ---------------------------
# 간단 웹페이지 (테스트용)
# ---------------------------

@app.get("/")
def index():
    return "Telegram Bridge Running"

# ---------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)