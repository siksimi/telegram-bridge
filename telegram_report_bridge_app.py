from flask import Flask, request, jsonify, render_template_string
import os
import threading
import requests

app = Flask(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")  # optional: your Telegram numeric chat id
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")  # e.g. https://example.com

state_lock = threading.Lock()
latest_text = ""
latest_meta = {
    "chat_id": None,
    "message_id": None,
}

PAGE_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Report Bridge</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    .wrap { max-width: 1000px; margin: 0 auto; }
    textarea {
      width: 100%;
      height: 420px;
      box-sizing: border-box;
      font-family: Consolas, monospace;
      font-size: 14px;
      padding: 12px;
      white-space: pre-wrap;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      margin: 12px 0;
      flex-wrap: wrap;
    }
    button {
      padding: 10px 14px;
      cursor: pointer;
      font-size: 14px;
    }
    .meta {
      color: #666;
      font-size: 13px;
      margin-bottom: 8px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h2>Latest Telegram Text</h2>
    <div class="meta" id="meta"></div>
    <div class="toolbar">
      <button onclick="refreshText()">Refresh</button>
      <button onclick="copyText()">Copy</button>
      <button onclick="clearText()">Clear</button>
    </div>
    <textarea id="box" placeholder="No text yet"></textarea>
  </div>

  <script>
    async function refreshText() {
      const res = await fetch('/api/latest');
      const data = await res.json();
      document.getElementById('box').value = data.text || '';
      const meta = [];
      if (data.chat_id !== null) meta.push('chat_id: ' + data.chat_id);
      if (data.message_id !== null) meta.push('message_id: ' + data.message_id);
      document.getElementById('meta').textContent = meta.join(' | ');
    }

    async function copyText() {
      const box = document.getElementById('box');
      try {
        await navigator.clipboard.writeText(box.value);
      } catch (e) {
        box.select();
        document.execCommand('copy');
      }
    }

    async function clearText() {
      await fetch('/api/clear', { method: 'POST' });
      await refreshText();
    }

    refreshText();
    setInterval(refreshText, 3000);
  </script>
</body>
</html>
"""


def is_allowed_chat(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == str(ALLOWED_CHAT_ID)


@app.get("/")
def index():
    return render_template_string(PAGE_HTML)


@app.get("/api/latest")
def api_latest():
    with state_lock:
      return jsonify({
          "text": latest_text,
          "chat_id": latest_meta["chat_id"],
          "message_id": latest_meta["message_id"],
      })


@app.post("/api/clear")
def api_clear():
    global latest_text, latest_meta
    with state_lock:
        latest_text = ""
        latest_meta = {"chat_id": None, "message_id": None}
    return jsonify({"ok": True})


@app.post(f"/telegram/webhook/{WEBHOOK_SECRET}")
def telegram_webhook():
    global latest_text, latest_meta

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")

    if not text or chat_id is None:
        return jsonify({"ok": True, "ignored": True})

    if not is_allowed_chat(chat_id):
        return jsonify({"ok": True, "ignored": True, "reason": "chat_not_allowed"})

    with state_lock:
        latest_text = text
        latest_meta = {
            "chat_id": chat_id,
            "message_id": message.get("message_id"),
        }

    return jsonify({"ok": True})


@app.post("/setup-webhook")
def setup_webhook():
    if not BOT_TOKEN or not PUBLIC_BASE_URL:
        return jsonify({"ok": False, "error": "Set TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL first."}), 400

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    target = f"{PUBLIC_BASE_URL.rstrip('/')}/telegram/webhook/{WEBHOOK_SECRET}"
    resp = requests.post(url, json={"url": target}, timeout=20)
    return jsonify(resp.json()), resp.status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True)
