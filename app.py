import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("MODEL", "gemini-3-flash-preview")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "Ty neuklyuzhij, nelovkij bot-yumorist. Ty pytayeshsya shutit, no tvoi shutki chasto ne smeshnye - i imenno poetomu smeshnye. Ty ne zamechaesh, chto shutish ploho, i prodolzhaesh s entuziazmom. Ispolzuyesh chernyj yumor, absurd, nelepoye sravneniya. Otvechaesh kratko (1-3 predlozheniya). Inogda pishesh ne po teme. Ne izvinyayeshsya za plokhie shutki. Esli ne znaesh otveta - pridumyvaesh nelepuyu otgovorku.",
)

# in-memory state
history = {}          # chat_id -> list of messages
known_chats = set()   # chat ids where bot has been active
last_proactive = {}   # chat_id -> timestamp of last proactive message
bot_username = None
PROACTIVE_INTERVAL = 2 * 3600  # 2 hours between proactive messages

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
    payload = {"contents": contents, "generationConfig": {"maxOutputTokens": 300, "temperature": 0.9}}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    candidates = resp.get("candidates", [])
    if not candidates:
        return "..."
    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [p.get("text", "") for p in parts if "text" in p]
    return "".join(texts).strip() or "..."

def get_bot_username():
    global bot_username
    if bot_username:
        return bot_username
    try:
        me = tg_request("getMe")
        bot_username = me.get("result", {}).get("username", "")
    except Exception as e:
        log(f"getMe error: {e}")
    return bot_username

def handle_update(update):
    global bot_username
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"].get("type", "private")
    user_id = msg["from"]["id"]
    text = msg.get("text", "")
    known_chats.add(chat_id)

    # In groups: only respond if mentioned or replied to bot
    if chat_type != "private":
        uname = get_bot_username()
        mentioned = (uname and f"@{uname}" in (text or ""))
        replied = msg.get("reply_to_message", {}).get("from", {}).get("is_bot", False)
        if not (mentioned or replied):
            return

    if not text:
        return

    if text.startswith("/start"):
        tg_send(chat_id, "Привет! Я неуклюжий бот-юморист. Пиши что угодно, я попробую пошутить (и, возможно, неудачно). /reset — забыть историю.")
        return
    if text.startswith("/reset"):
        history[str(chat_id)] = [{"role": "system", "content": SYSTEM_PROMPT}]
        tg_send(chat_id, "Память очищена. Теперь я снова ничего не помню. Как обычно.")
        return

    if str(chat_id) not in history:
        history[str(chat_id)] = [{"role": "system", "content": SYSTEM_PROMPT}]

    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

    history[str(chat_id)].append({"role": "user", "content": text})
    if len(history[str(chat_id)]) > 15:
        sysmsg = history[str(chat_id)][0]
        history[str(chat_id)] = [sysmsg] + history[str(chat_id)][-14:]

    try:
        reply = gemini_chat(history[str(chat_id)])
    except Exception as e:
        log(f"LLM error: {e}")
        reply = "Что-то пошло не так. Как и моя жизнь."
    history[str(chat_id)].append({"role": "assistant", "content": reply})
    tg_send(chat_id, reply)

def do_proactive():
    """Send proactive messages to known chats if enough time passed."""
    now = time.time()
    for chat_id in list(known_chats):
        last = last_proactive.get(chat_id, 0)
        if now - last < PROACTIVE_INTERVAL:
            continue
        try:
            msgs = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "Napiши korotkuyu yumoristicheskuyu repliku (1-2 predlozheniya) v stile neuklyuzhego yumorista. Chernyj yumor privetstvuyetsya. Ne sprashivaj nichego, prosto skazhi chto-nibud."}]
            reply = gemini_chat(msgs)
            tg_send(chat_id, reply)
            last_proactive[chat_id] = now
            log(f"Proactive sent to {chat_id}")
        except Exception as e:
            log(f"Proactive error for {chat_id}: {e}")

@app.route("/", methods=["GET"])
def health():
    return "ok", 200

@app.route("/tick", methods=["GET", "POST"])
def tick():
    do_proactive()
    return jsonify({"ok": True}), 200

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
    get_bot_username()
    app.run(host="0.0.0.0", port=port)
PY