import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("MODEL", "gemini-3-flash-preview")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "Ty poleznyj assistent. Otvechaj na russkom yazyke, kratko i po delu.")

# in-memory history
history = {}

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def tg_request(method, params=None, timeout=30):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    if params:
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
    else:
        req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def tg_send(chat_id, text):
    chunks = [text] if len(text) <= 4000 else []
    if len(text) > 4000:
        cur = ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3900:
                chunks.append(cur)
                cur = line
            else:
                cur = (cur + "\n" + line) if cur else line
        if cur:
            chunks.append(cur)
    for c in chunks:
        try:
            tg_request("sendMessage", {"chat_id": chat_id, "text": c})
        except Exception as e:
            log(f"send error: {e}")

def gemini_chat(messages):
    system_text = ""
    contents = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
            continue
        gemini_role = "user" if m["role"] == "user" else "model"
        contents.append({"role": gemini_role, "parts": [{"text": m["content"]}]})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": contents, "generationConfig": {"maxOutputTokens": 1000, "temperature": 0.7}}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    candidates = resp.get("candidates", [])
    if not candidates:
        return "Пустой ответ от модели."
    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [p.get("text", "") for p in parts if "text" in p]
    return "".join(texts).strip() or "Пустой ответ от модели."

def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")
    if user_id != ADMIN_ID:
        try:
            tg_request("sendMessage", {"chat_id": chat_id, "text": "Доступ запрещён."})
        except Exception:
            pass
        return
    if not text:
        return
    if text.startswith("/start"):
        tg_send(chat_id, "Привет! Я бот-ассистент (Gemini, бесплатно, на Render). Пиши сообщения. /reset — очистить историю, /help — помощь.")
        return
    if text.startswith("/help"):
        tg_send(chat_id, "Команды:\n/start — приветствие\n/reset — очистить историю диалога\n/help — эта справка\n\nПросто пиши текст — отвечу.")
        return
    if text.startswith("/reset"):
        history[str(user_id)] = [{"role": "system", "content": SYSTEM_PROMPT}]
        tg_send(chat_id, "История очищена.")
        return

    if str(user_id) not in history:
        history[str(user_id)] = [{"role": "system", "content": SYSTEM_PROMPT}]
    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass
    history[str(user_id)].append({"role": "user", "content": text})
    if len(history[str(user_id)]) > 21:
        sysmsg = history[str(user_id)][0]
        history[str(user_id)] = [sysmsg] + history[str(user_id)][-20:]
    try:
        reply = gemini_chat(history[str(user_id)])
    except Exception as e:
        log(f"LLM error: {e}")
        reply = f"Ошибка при обращении к модели: {e}"
    history[str(user_id)].append({"role": "assistant", "content": reply})
    tg_send(chat_id, reply)

@app.route("/", methods=["GET"])
def health():
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if data:
        try:
            handle_update(data)
        except Exception as e:
            log(f"handle_update error: {e}")
    return jsonify({"ok": True}), 200

@app.route("/setwebhook", methods=["GET"])
def set_webhook():
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL", "")
    if not base:
        return "WEBHOOK_URL/RENDER_EXTERNAL_URL not set", 400
    url = base.rstrip("/") + "/webhook"
    try:
        res = tg_request("setWebhook", {"url": url, "allowed_updates": json.dumps(["message", "edited_message"])})
        return jsonify(res), 200
    except Exception as e:
        return str(e), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
PY