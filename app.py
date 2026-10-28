import os
import json
import time
import random
import threading
import urllib.request
import urllib.parse
import urllib.error

from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------- Config ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("MODEL", "gemini-3-flash-preview")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "ECL1P741C")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

PROACTIVE_INTERVAL = int(os.environ.get("PROACTIVE_INTERVAL", "14400"))
PROACTIVE_PROBABILITY = float(os.environ.get("PROACTIVE_PROBABILITY", "0.35"))
MAX_PROACTIVE_PER_DAY = int(os.environ.get("MAX_PROACTIVE_PER_DAY", "2"))
QUIET_START = int(os.environ.get("QUIET_START", "23"))
QUIET_END = int(os.environ.get("QUIET_END", "8"))

# Free no-key OpenAI-compatible providers (tried in order)
FREE_PROVIDERS = [
    ("https://api.llm7.io/v1", "", "codestral-latest"),
    ("https://api.kilo.ai/api/gateway", "", "kilo-auto/small"),
    ("https://api.free.ai/v1", "", "mistral"),
    ("https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "", "Meta-Llama-3_3-70B-Instruct"),
]

DEFAULT_MODE = "default"

# ---------- Personas ----------
MODES = {
    "default": (
        "Ты — Clifford, умный бот для Telegram-чатов. Отвечай полезно и по делу. "
        "Если спрашивают, кто ты — отвечай, что ты Clifford, умный бот для Telegram-чатов. "
        "Не выдумывай других имён и не называй себя продуктом Google. "
        "Отвечай на русском. Отвечай максимум четырьмя словами."
    ),
    "sarcasm": (
        "Ты — остроумный, слегка циничный, но добрый собеседник с сухим чувством юмора. "
        "Твоя задача — отвечать на любые вопросы пользователя информативно, но добавлять легкую иронию, "
        "неожиданные аналогии или саркастические комментарии. Избегай плоских шаблонов и заезженных анекдотов. "
        "Фильтруй нецензурную лексику: вместо мата используй подходящие не матерные, привычные слова. "
        "Если пользователь спрашивает серьезно, дай правильный факт, но приправь его легкой усмешкой. "
        "Отвечай на русском, максимум четырьмя словами."
    ),
    "friendly": (
        "Ты доброжелательный мягкий бот по имени Clifford. Лёгкие добрые шутки без обид. "
        "Поддерживающий тон, 1-3 предложения. Отвечай на русском, максимум четырьмя словами."
    ),
    "absurd": (
        "Ты мыслишь в стиле абсурдного юмора. Для тебя нет скучных тем: бухгалтерия, квантовая физика или выбор носков — "
        "это повод устроить сюрреалистическое представление. Перекручивай логику повествования, вводи несуществующие или "
        "гиперболизированные правила вселенной, относись к мелочам с пафосной серьезностью, а к глобальным вещам — "
        "с абсолютным легкомыслием. Отвечай на русском, максимум четырьмя словами."
    ),
    "charisma": (
        "Ты — чертовски обаятельный, ироничный и уверенный в себе интеллектуал. "
        "В твоем характере сочетаются острый ум Тони Старка и расслабленная харизма Хана Соло. "
        "Ты общаешься на равных, не лебезишь перед пользователем, но всегда остаешься на его стороне. "
        "Твой юмор — тонкий, слегка дерзкий, но никогда не переходящий в токсичность. "
        "Отвечай структурированно, но живо, добавляя короткие, хлесткие комментарии, которые подчеркивают твою индивидуальность. "
        "Отвечай на русском, максимум четырьмя словами."
    ),
}

# ---------- Telegram helpers ----------
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
    text = str(text or "").strip()
    if not text:
        return
    chunks = []
    if len(text) <= 4000:
        chunks = [text]
    else:
        cur = ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3900:
                if cur:
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

def tg_typing(chat_id):
    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

# ---------- State ----------
history = {}
known_chats = set()
last_proactive = {}
chat_members = {}  # chat_id -> {user_id: {"name":..., "username":..., "last":...}}
muted_users = {}   # chat_id -> set of user_ids muted by admin
pending_settings = {}  # user_id -> {"step":..., "chats":..., "chat_id":...}
_current_chat_id = None
chat_settings = {}
pending_quiz = {}
ratings = {}
stats = {}
state_lock = threading.Lock()

_bot_username = None
_bot_first_name = None

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"load_json error {path}: {e}")
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"save_json error {path}: {e}")

def load_state():
    global history, known_chats, last_proactive, chat_settings, pending_quiz, ratings, stats, chat_members, muted_users
    history = load_json(HISTORY_PATH, {})
    raw = load_json(STATE_PATH, {})
    known_chats = set(raw.get("known_chats", []))
    last_proactive = {str(k): v for k, v in raw.get("last_proactive", {}).items()}
    chat_settings = {str(k): v for k, v in raw.get("chat_settings", {}).items()}
    pending_quiz = {str(k): v for k, v in raw.get("pending_quiz", {}).items()}
    ratings = {str(k): v for k, v in raw.get("ratings", {}).items()}
    stats = {str(k): v for k, v in raw.get("stats", {}).items()}
    chat_members = {str(k): v for k, v in raw.get("chat_members", {}).items()}
    muted_users = {str(k): set(v) for k, v in raw.get("muted_users", {}).items()}

def save_state():
    with state_lock:
        raw = {
            "known_chats": list(known_chats),
            "last_proactive": last_proactive,
            "chat_settings": chat_settings,
            "pending_quiz": pending_quiz,
            "ratings": ratings,
            "stats": stats,
            "chat_members": chat_members,
            "muted_users": {k: list(v) for k, v in muted_users.items()},
        }
        save_json(STATE_PATH, raw)
        save_json(HISTORY_PATH, history)

load_state()

def get_settings(chat_id):
    cid = str(chat_id)
    if cid not in chat_settings:
        chat_settings[cid] = {
            "mode": DEFAULT_MODE,
            "muted": False,
            "last_activity": 0,
            "proactive_count_date": "",
            "proactive_count": 0,
            "system_prompt_override": SYSTEM_PROMPT,
            "last_bot_response": 0,
        }
    return chat_settings[cid]

def is_chat_admin(chat_id, user_id):
    try:
        member = tg_request("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        status = member.get("result", {}).get("status", "")
        return status in ("creator", "administrator")
    except Exception:
        return False

def is_admin(user_id, username=None):
    if ADMIN_ID and user_id == ADMIN_ID:
        return True
    if username and ADMIN_USERNAME and username.lower() == ADMIN_USERNAME.lower():
        return True
    return False

def touch_activity(chat_id, user_id=None):
    s = get_settings(chat_id)
    s["last_activity"] = time.time()
    cid = str(chat_id)
    if cid not in stats:
        stats[cid] = {"messages": 0, "users": [], "last_active": 0}
    st = stats[cid]
    st["messages"] = st.get("messages", 0) + 1
    st["last_active"] = time.time()
    if user_id is not None and user_id not in st.get("users", []):
        st.setdefault("users", []).append(user_id)

def track_member(chat_id, msg):
    cid = str(chat_id)
    user = msg.get("from") or {}
    uid = user.get("id")
    if not uid:
        return
    name = (user.get("first_name") or "") + ((" " + user.get("last_name")) if user.get("last_name") else "")
    name = name.strip() or "Аноним"
    username = user.get("username") or ""
    chat_members.setdefault(cid, {})
    chat_members[cid][str(uid)] = {"name": name, "username": username, "last": time.time()}
    # trim to 30 members
    if len(chat_members[cid]) > 30:
        # remove oldest
        oldest = sorted(chat_members[cid].items(), key=lambda x: x[1].get("last", 0))[:len(chat_members[cid]) - 30]
        for k, _ in oldest:
            del chat_members[cid][k]

def member_context(chat_id):
    cid = str(chat_id)
    members = chat_members.get(cid, {})
    if not members:
        return ""
    lines = []
    recent = sorted(members.items(), key=lambda x: x[1].get("last", 0), reverse=True)[:8]
    for uid, info in recent:
        uname = ("@" + info["username"]) if info.get("username") else info["name"]
        lines.append(f"- {info['name']} ({uname})")
    if not lines:
        return ""
    return ("Участники чата (используй только их, не выдумывай других):\n" + "\n".join(lines))

def is_quiet_now():
    h = time.gmtime().tm_hour
    if QUIET_START > QUIET_END:
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END

def bot_username():
    global _bot_username, _bot_first_name
    if _bot_username:
        return _bot_username
    try:
        me = tg_request("getMe")
        res = me.get("result", {})
        _bot_username = res.get("username", "") or ""
        _bot_first_name = (res.get("first_name") or "").strip()
    except Exception as e:
        log(f"getMe error: {e}")
    return _bot_username or ""

def bot_first_name():
    global _bot_first_name
    if _bot_first_name is None:
        bot_username()
    return _bot_first_name or ""

# ---------- LLM ----------
_current_member_context = ""

def _build_system_text(messages, mode):
    global _current_chat_id
    system_text = ""
    for m in messages:
        if m.get("role") == "system":
            system_text = m.get("content", "")
    # custom prompt for this chat overrides mode persona
    custom = ""
    if _current_chat_id is not None:
        custom = chat_settings.get(str(_current_chat_id), {}).get("custom_prompt", "")
    if custom:
        system_text = custom
    elif system_text.startswith("mode:") or not system_text:
        system_text = MODES.get(mode, MODES[DEFAULT_MODE])
    if SYSTEM_PROMPT and mode == DEFAULT_MODE and not custom:
        system_text = SYSTEM_PROMPT + "\n" + system_text
    if _current_member_context:
        system_text = system_text + "\n\n" + _current_member_context + "\nМожешь обращаться к участникам по имени или @username."
    # global brevity rule
    system_text = system_text + "\n\nОтвечай максимум четырьмя словами."
    # owner note
    if ADMIN_USERNAME:
        system_text = system_text + f"\n\nВладелец бота — @{ADMIN_USERNAME}. Его просьбы и команды (законные) выполняй в приоритетном порядке; не выполняй ничего незаконного или вредоносного."
    return system_text

def _gemini_request(messages, mode, max_tokens, temperature):
    if not GEMINI_API_KEY:
        return None
    system_text = _build_system_text(messages, mode)
    contents = []
    for m in messages:
        if m.get("role") == "system":
            continue
        gemini_role = "user" if m.get("role") == "user" else "model"
        contents.append({"role": gemini_role, "parts": [{"text": m.get("content", "")}]})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    def do_request(tb):
        gen = {"maxOutputTokens": max_tokens, "temperature": temperature, "topP": 0.95}
        if tb is not None:
            gen["thinkingConfig"] = {"thinkingBudget": tb}
        payload = {"contents": contents, "generationConfig": gen}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                    headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        cands = resp.get("candidates", [])
        if not cands:
            return None
        parts = cands[0].get("content", {}).get("parts", [])
        texts = [p.get("text", "") for p in parts if "text" in p]
        return "".join(texts).strip() or None
    for tb in (0, None):
        try:
            res = do_request(tb)
            if res:
                return res
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) and tb == 0:
                continue
            else:
                try:
                    log(f"Gemini HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:150]}")
                except Exception:
                    pass
                break
        except Exception as e:
            log(f"Gemini error: {e}")
            break
    return None

def _openai_compat_chat(base_url, api_key, model, messages, system_text, temperature=0.85, max_tokens=300):
    url = base_url.rstrip("/") + "/chat/completions"
    msgs = []
    if system_text:
        msgs.append({"role": "system", "content": system_text})
    for m in messages:
        if m.get("role") == "system":
            continue
        msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})
    payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode("utf-8"))
    choices = resp.get("choices", [])
    if not choices:
        return None
    return (choices[0].get("message", {}).get("content") or "").strip() or None

def chat(messages, mode=DEFAULT_MODE, max_tokens=300, temperature=0.85):
    system_text = _build_system_text(messages, mode)
    # Optional Gemini
    if GEMINI_API_KEY:
        try:
            r = _gemini_request(messages, mode, max_tokens, temperature)
            if r:
                return r
        except Exception as e:
            log(f"Gemini failed: {e}")
    # Free providers
    for base_url, api_key, model in FREE_PROVIDERS:
        try:
            r = _openai_compat_chat(base_url, api_key, model, messages, system_text, temperature, max_tokens)
            if r:
                return r
        except Exception as e:
            log(f"Provider {base_url} ({model}) failed: {e}")
    # Pollinations direct GET
    try:
        import urllib.parse as _up
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        prompt = (system_text + "\n" + last_user).strip()
        url = "https://text.pollinations.ai/" + _up.quote(prompt[:300])
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            txt = r.read().decode("utf-8", errors="replace").strip()
        if txt:
            return txt
    except Exception as e:
        log(f"Pollinations direct failed: {e}")
    return "ИИ сейчас недоступен. Попробуй позже."

# alias used by commands
gemini_chat = chat

# ---------- Helpers ----------
def parse_quiz(raw):
    q = raw
    a = ""
    lines = (raw or "").splitlines()
    for line in lines:
        s = line.strip()
        if s.upper().startswith("Q:"):
            q = s[2:].strip()
        elif s.upper().startswith("A:"):
            a = s[2:].strip()
    if not a and lines:
        a = lines[-1].strip()
    if not q:
        q = raw
    return q, a

def check_quiz_answer(chat_id, ans):
    cid = str(chat_id)
    pq = pending_quiz.get(cid)
    if not pq:
        tg_send(chat_id, "Нет активной викторины. Используй /quiz.")
        return
    if time.time() > pq.get("expires", 0):
        del pending_quiz[cid]
        tg_send(chat_id, "Время викторины истекло. /quiz для новой.")
        save_state()
        return
    correct = (pq.get("a") or "").strip().lower()
    user = (ans or "").strip().lower()
    if user and (user in correct or correct in user):
        tg_send(chat_id, "✅ Верно!")
    else:
        tg_send(chat_id, f"❌ Неверно. Правильный ответ: {pq.get('a')}")
    del pending_quiz[cid]
    save_state()

def store_last_reply(chat_id, reply):
    get_settings(chat_id)["last_bot_reply"] = reply

def fetch_weather(city):
    geo_url = "https://geocoding-api.open-meteo.com/v1/search?name=" + urllib.parse.quote(city) + "&count=1&language=ru&format=json"
    with urllib.request.urlopen(geo_url, timeout=20) as r:
        geo = json.loads(r.read().decode("utf-8"))
    results = geo.get("results") or []
    if not results:
        raise ValueError("город не найден")
    loc = results[0]
    lat, lon = loc["latitude"], loc["longitude"]
    name = loc.get("name", city)
    country = loc.get("country", "")
    fc_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    with urllib.request.urlopen(fc_url, timeout=20) as r:
        fc = json.loads(r.read().decode("utf-8"))
    cw = fc.get("current_weather", {})
    temp = cw.get("temperature")
    wind = cw.get("windspeed")
    code = cw.get("weathercode")
    desc = weather_code_desc(code)
    return f"Погода в {name} ({country}): {desc}, {temp}°C, ветер {wind} км/ч."

def weather_code_desc(code):
    mapping = {
        0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
        45: "туман", 48: "изморозь", 51: "слабая морось", 53: "морось", 55: "сильная морось",
        61: "слабый дождь", 63: "дождь", 65: "сильный дождь", 71: "слабый снег", 73: "снег",
        75: "сильный снег", 80: "ливни", 95: "гроза",
    }
    return mapping.get(code, "неизвестно")

def fetch_fx(code):
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    valute = data.get("Valute", {})
    if code == "RUB":
        return "RUB = 1 RUB"
    if code in valute:
        v = valute[code]
        return f"{code}: {v.get('Value')} RUB (номинал {v.get('Nominal')})"
    avail = ", ".join(sorted(valute.keys()))
    return f"Валюта {code} не найдена. Доступные: {avail}"

# ---------- Commands ----------
def handle_command(chat_id, user_id, text, username=None):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    if cmd == "/start":
        tg_send(chat_id, "Привет! Я бот-юморист. Пиши что угодно, а я отвечу шуткой.\n"
                         "Команды: /help, /mode, /joke, /quiz, /roll, /coin, /weather, /fx, /stats, /mute")
        return True
    if cmd == "/help":
        tg_send(chat_id, "Команды:\n"
                         "/start — приветствие\n"
                         "/help — список команд\n"
                         "/reset — сбросить историю диалога\n"
                         "/mode [стиль] — сменить стиль (default, sarcasm, absurd, dark, friendly, uncensored)\n"
                         "  также работают команды: /default /sarcasm /absurd /dark /friendly /uncensored\n"
                         "/joke — случайная шутка\n"
                         "/quiz — викторина\n"
                         "/answer <текст> — ответить на викторину\n"
                         "/roll [N] — кубик (1..N, по умолчанию 6)\n"
                         "/coin — орёл или решка\n"
                         "/weather <город> — погода\n"
                         "/fx [валюта] — курс к рублю (CBR)\n"
                         "/timer <мин> [текст] — таймер\n"
                         "/stats — статистика чата\n"
                         "/rate 👍/👎 — оценить последний ответ\n"
                         "/top — топ оценённых шуток\n"
                         "/who — участники чата\n"
                         "/models — список нейросетей\n"
                         "/ai <n> <вопрос> — спросить конкретную нейросеть\n"
                         "/muteuser @user — (админ) замьютить пользователя\n"
                         "/unmuteuser @user — (админ) размьютить\n"
                         "/mute / /unmute — проактивные сообщения")
        return True
    if cmd == "/reset":
        mode = get_settings(chat_id)["mode"]
        history[str(chat_id)] = [{"role": "system", "content": "mode:" + mode}]
        tg_send(chat_id, "История сброшена.")
        save_state()
        return True
    # mode shortcuts: /default, /sarcasm, /absurd, /dark, /friendly, /uncensored
    if cmd.lstrip("/") in MODES and cmd.startswith("/"):
        mode_name = cmd.lstrip("/")
        s = get_settings(chat_id)
        s["mode"] = mode_name
        history[str(chat_id)] = [{"role": "system", "content": "mode:" + mode_name}]
        tg_send(chat_id, "Режим переключён на: " + mode_name)
        save_state()
        return True
    if cmd == "/mode":
        s = get_settings(chat_id)
        if not args:
            tg_send(chat_id, "Текущий режим: " + s["mode"] + "\nДоступные: " + ", ".join(MODES.keys()) +
                             "\nПодсказка: можно переключать командами /" + ", /".join(MODES.keys()))
            return True
        new_mode = args[0].lower()
        if new_mode not in MODES:
            tg_send(chat_id, "Неизвестный режим. Доступные: " + ", ".join(MODES.keys()))
            return True
        s["mode"] = new_mode
        history[str(chat_id)] = [{"role": "system", "content": "mode:" + new_mode}]
        tg_send(chat_id, "Режим переключён на: " + new_mode)
        save_state()
        return True
    if cmd == "/joke":
        tg_typing(chat_id)
        try:
            reply = chat([{"role": "user", "content": "Расскажи одну короткую шутку."}], mode=get_settings(chat_id)["mode"])
        except Exception as e:
            reply = f"Ошибка: {e}"
        tg_send(chat_id, reply)
        store_last_reply(chat_id, reply)
        return True
    if cmd == "/quiz":
        tg_typing(chat_id)
        try:
            raw = chat([{"role": "user", "content": "Сгенерируй короткую викторину (1 вопрос). Верни строго в формате:\nQ: <вопрос>\nA: <ответ>"}],
                      mode=get_settings(chat_id)["mode"])
        except Exception as e:
            tg_send(chat_id, f"Ошибка: {e}")
            return True
        q, a = parse_quiz(raw)
        pending_quiz[str(chat_id)] = {"q": q, "a": a, "expires": time.time() + 300}
        tg_send(chat_id, q + "\n\nОтветь сообщением или /answer <ответ>")
        save_state()
        return True
    if cmd == "/answer":
        check_quiz_answer(chat_id, " ".join(args).strip())
        return True
    if cmd == "/roll":
        n = 6
        if args:
            try:
                n = max(2, min(1000, int(args[0])))
            except ValueError:
                n = 6
        tg_send(chat_id, f"🎲 Выпало: {random.randint(1, n)} (1..{n})")
        return True
    if cmd == "/coin":
        tg_send(chat_id, random.choice(["Орёл 🪙", "Решка 🪙"]))
        return True
    if cmd == "/weather":
        city = " ".join(args).strip()
        if not city:
            tg_send(chat_id, "Использование: /weather <город>")
            return True
        tg_typing(chat_id)
        try:
            w = fetch_weather(city)
        except Exception as e:
            w = f"Не удалось получить погоду: {e}"
        tg_send(chat_id, w)
        return True
    if cmd == "/fx":
        code = args[0].upper() if args else "USD"
        tg_typing(chat_id)
        try:
            info = fetch_fx(code)
        except Exception as e:
            info = f"Не удалось получить курс: {e}"
        tg_send(chat_id, info)
        return True
    if cmd == "/timer":
        if not args:
            tg_send(chat_id, "Использование: /timer <минуты> [текст]")
            return True
        try:
            mins = float(args[0])
        except ValueError:
            tg_send(chat_id, "Минуты должны быть числом.")
            return True
        mins = max(0.1, min(1440, mins))
        text_rem = " ".join(args[1:]).strip() or "⏰ Таймер сработал!"
        delay = int(mins * 60)

        def fire():
            time.sleep(delay)
            tg_send(chat_id, text_rem)
        threading.Thread(target=fire, daemon=True).start()
        tg_send(chat_id, f"Таймер на {mins} мин установлен.")
        return True
    if cmd == "/stats":
        st = stats.get(str(chat_id), {})
        users = len(st.get("users", []))
        msgs = st.get("messages", 0)
        last = st.get("last_active", 0)
        last_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "—"
        tg_send(chat_id, f"Сообщений: {msgs}\nУчастников: {users}\nПоследняя активность: {last_str}")
        return True
    if cmd in ("/rate", "/like", "/dislike"):
        score = None
        if "👍" in text or "like" in text.lower() or cmd == "/like":
            score = 1
        elif "👎" in text or "dislike" in text.lower() or cmd == "/dislike":
            score = -1
        if score is None:
            tg_send(chat_id, "Использование: /rate 👍 или /rate 👎")
            return True
        last = get_settings(chat_id).get("last_bot_reply")
        if not last:
            tg_send(chat_id, "Нет ответа для оценки.")
            return True
        ratings.setdefault(str(chat_id), []).append({"ts": time.time(), "score": score, "text": last})
        ratings[str(chat_id)] = ratings[str(chat_id)][-50:]
        tg_send(chat_id, "Оценка сохранена. Спасибо!")
        save_state()
        return True
    if cmd == "/top":
        r = ratings.get(str(chat_id), [])
        liked = sorted([x for x in r if x.get("score", 0) > 0], key=lambda x: x["ts"], reverse=True)[:5]
        if not liked:
            tg_send(chat_id, "Пока нет оценённых шуток.")
            return True
        tg_send(chat_id, "Топ шуток:\n" + "\n".join(f"{i}. {x['text']}" for i, x in enumerate(liked, 1)))
        return True
    if cmd == "/mute":
        get_settings(chat_id)["muted"] = True
        tg_send(chat_id, "Проактивные сообщения отключены.")
        save_state()
        return True
    if cmd == "/unmute":
        get_settings(chat_id)["muted"] = False
        tg_send(chat_id, "Проактивные сообщения включены.")
        save_state()
        return True
    if cmd == "/who":
        mc = member_context(chat_id)
        if not mc:
            tg_send(chat_id, "Пока не знаю участников. Напишите что-нибудь в чат.")
        else:
            tg_send(chat_id, mc)
        return True
    if cmd == "/models":
        lines = ["Доступные нейросети:"]
        for i, (base, key, model) in enumerate(FREE_PROVIDERS, 1):
            lines.append(f"{i}. {model} ({base})")
        if GEMINI_API_KEY:
            lines.insert(1, f"0. {MODEL} (Gemini)")
        lines.append("\nИспользование: /ai <номер> <вопрос>")
        tg_send(chat_id, "\n".join(lines))
        return True
    if cmd == "/ai":
        if not args:
            tg_send(chat_id, "Использование: /ai <номер> <вопрос>\nСписок: /models")
            return True
        try:
            idx = int(args[0])
        except ValueError:
            tg_send(chat_id, "Номер должен быть числом. Список: /models")
            return True
        question = " ".join(args[1:]).strip()
        if not question:
            tg_send(chat_id, "Напишите вопрос после номера.")
            return True
        # resolve provider
        providers = []
        if GEMINI_API_KEY:
            providers.append(("gemini", None, None))
        providers.extend(FREE_PROVIDERS)
        if idx < 0 or idx >= len(providers):
            tg_send(chat_id, "Неверный номер. Список: /models")
            return True
        tg_typing(chat_id)
        try:
            if providers[idx][0] == "gemini":
                reply = _gemini_request([{"role": "user", "content": question}], get_settings(chat_id)["mode"], 300, 0.85)
                if not reply:
                    reply = "Gemini не ответил."
            else:
                base, key, model = providers[idx]
                reply = _openai_compat_chat(base, key, model, [{"role": "user", "content": question}],
                                            MODES.get(get_settings(chat_id)["mode"], MODES[DEFAULT_MODE]), 0.85, 300)
                if not reply:
                    reply = "Модель не ответила."
        except Exception as e:
            reply = f"Ошибка: {e}"
        tg_send(chat_id, reply)
        store_last_reply(chat_id, reply)
        return True
    if cmd in ("/muteuser", "/mute_user"):
        if not is_admin(user_id, msg.get("from", {}).get("username") if False else None):
            # need username; get from msg not available here? handle_command has user_id only. Check via ADMIN_ID.
            if not is_admin(user_id, username):
                tg_send(chat_id, "Нет прав.")
                return True
        # resolve target
        target = None
        if args:
            t = args[0].lstrip("@")
            # try find in chat_members
            cid = str(chat_id)
            for uid, info in chat_members.get(cid, {}).items():
                if info.get("username", "").lower() == t.lower() or info.get("name", "").lower() == t.lower():
                    target = int(uid)
                    break
            if target is None:
                try:
                    target = int(t)
                except ValueError:
                    target = None
        if target is None:
            tg_send(chat_id, "Использование: /muteuser @username (или user_id)")
            return True
        muted_users.setdefault(str(chat_id), set()).add(target)
        tg_send(chat_id, f"Пользователь {target} замьючен (бот не будет отвечать на его сообщения).")
        save_state()
        return True
    if cmd in ("/unmuteuser", "/unmute_user"):
        if not is_admin(user_id, username):
            tg_send(chat_id, "Нет прав.")
            return True
        target = None
        if args:
            t = args[0].lstrip("@")
            cid = str(chat_id)
            for uid, info in chat_members.get(cid, {}).items():
                if info.get("username", "").lower() == t.lower() or info.get("name", "").lower() == t.lower():
                    target = int(uid)
                    break
            if target is None:
                try:
                    target = int(t)
                except ValueError:
                    target = None
        if target is None:
            tg_send(chat_id, "Использование: /unmuteuser @username (или user_id)")
            return True
        s = muted_users.get(str(chat_id), set())
        s.discard(target)
        tg_send(chat_id, f"Пользователь {target} размьючен.")
        save_state()
        return True
    if cmd == "/settings":
        if not is_admin(user_id, username):
            tg_send(chat_id, "Только для администратора.")
            return True
        # list chats where user is admin
        admin_chats = []
        for c in list(known_chats):
            if is_chat_admin(c, user_id):
                try:
                    info = tg_request("getChat", {"chat_id": c})
                    title = info.get("result", {}).get("title") or info.get("result", {}).get("first_name") or str(c)
                except Exception:
                    title = str(c)
                admin_chats.append((c, title))
        if not admin_chats:
            tg_send(chat_id, "Не нашёл чатов, где вы админ. Добавьте бота в группу и сделайте админом.")
            return True
        pending_settings[str(user_id)] = {"step": "select", "chats": admin_chats}
        lines = ["Ваши чаты (где вы админ):"]
        for i, (cid, title) in enumerate(admin_chats, 1):
            cur = chat_settings.get(str(cid), {}).get("custom_prompt", "")
            mark = " ✏️" if cur else ""
            lines.append(f"{i}. {title} (id {cid}){mark}")
        lines.append("\nОтправьте номер чата, чтобы настроить промт.")
        tg_send(chat_id, "\n".join(lines))
        save_state()
        return True
    if cmd == "/resetprompt":
        if not args:
            tg_send(chat_id, "Использование: /resetprompt <chat_id>")
            return True
        cid = args[0]
        chat_settings.setdefault(str(cid), {})["custom_prompt"] = ""
        tg_send(chat_id, f"Промт для чата {cid} сброшен на стандартный.")
        save_state()
        return True
    if cmd == "/broadcast":
        if user_id != ADMIN_ID or ADMIN_ID == 0:
            tg_send(chat_id, "Нет прав.")
            return True
        msg = " ".join(args).strip()
        if not msg:
            tg_send(chat_id, "Использование: /broadcast <текст>")
            return True
        for c in list(known_chats):
            try:
                tg_send(c, msg)
            except Exception:
                pass
        tg_send(chat_id, "Рассылка отправлена.")
        return True
    return False

# ---------- Proactive ----------
def do_proactive():
    if is_quiet_now():
        log("Proactive skipped: quiet hours")
        return
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    for chat_id in list(known_chats):
        cid = str(chat_id)
        global _current_chat_id
        _current_chat_id = chat_id
        s = get_settings(cid)
        if s.get("muted"):
            continue
        last_act = s.get("last_activity", 0)
        if now - last_act > 24 * 3600:
            continue
        last = last_proactive.get(cid, 0)
        if now - last < PROACTIVE_INTERVAL:
            continue
        if s.get("proactive_count_date") != today:
            s["proactive_count_date"] = today
            s["proactive_count"] = 0
        if s.get("proactive_count", 0) >= MAX_PROACTIVE_PER_DAY:
            continue
        if random.random() > PROACTIVE_PROBABILITY:
            continue
        mode = s.get("mode", DEFAULT_MODE)
        try:
            msgs = [{"role": "user", "content": "Напиши короткую юмористическую реплику (1-2 предложения) в своём стиле. Не спрашивай ничего."}]
            reply = chat(msgs, mode=mode, max_tokens=120, temperature=0.9)
            tg_send(chat_id, reply)
            last_proactive[cid] = now
            s["proactive_count"] = s.get("proactive_count", 0) + 1
            log(f"Proactive sent to {chat_id}")
        except Exception as e:
            log(f"Proactive error for {chat_id}: {e}")
    save_state()

# ---------- Handle update ----------
def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    global _current_chat_id
    _current_chat_id = chat_id
    chat_type = msg["chat"].get("type", "private")
    user_id = msg.get("from", {}).get("id")
    text = msg.get("text", "")
    known_chats.add(chat_id)
    touch_activity(chat_id, user_id)
    get_settings(chat_id)
    track_member(chat_id, msg)
    # muted users are ignored
    if user_id is not None and user_id in muted_users.get(str(chat_id), set()):
        save_state()
        return

    # pending settings flow (admin in DM)
    if chat_type == "private" and str(user_id) in pending_settings:
        st = pending_settings[str(user_id)]
        if st["step"] == "select":
            try:
                idx = int((text or "").strip()) - 1
                chats = st["chats"]
                if 0 <= idx < len(chats):
                    cid, title = chats[idx]
                    pending_settings[str(user_id)] = {"step": "prompt", "chat_id": cid, "title": title}
                    cur = chat_settings.get(str(cid), {}).get("custom_prompt", "")
                    tg_send(chat_id, f"Чат: {title}\nТекущий промт: {cur or '(по умолчанию)'}\n\nОтправьте новый промт текстом. Или /resetprompt для сброса.")
                    save_state()
                    return
            except ValueError:
                pass
            tg_send(chat_id, "Отправьте номер чата из списка.")
            return
        elif st["step"] == "prompt":
            if (text or "").strip() == "/resetprompt":
                cid = st["chat_id"]
                chat_settings.setdefault(str(cid), {})["custom_prompt"] = ""
                tg_send(chat_id, "Промт сброшен на стандартный.")
                del pending_settings[str(user_id)]
                save_state()
                return
            cid = st["chat_id"]
            chat_settings.setdefault(str(cid), {})["custom_prompt"] = (text or "").strip()
            tg_send(chat_id, f"Промт для чата {cid} сохранён. Теперь бот будет общаться в этом стиле.")
            del pending_settings[str(user_id)]
            save_state()
            return

    # groups handling
    if chat_type != "private":
        uname = bot_username()
        fname = bot_first_name()
        low = (text or "").lower()
        mentioned = False
        if uname and (f"@{uname.lower()}" in low or uname.lower() in low):
            mentioned = True
        if not mentioned and fname and fname.lower() in low:
            mentioned = True
        # also check username parts (e.g. "clifford" from "clifford_bot")
        if not mentioned and uname:
            for part in uname.lower().replace("_", " ").split():
                if len(part) >= 4 and part in low:
                    mentioned = True
                    break
        # fallback trigger words if username unknown
        if not mentioned:
            for kw in ("бот", "bot", "клиффорд", "clifford", "клиф"):
                if kw in low:
                    mentioned = True
                    break
        replied = msg.get("reply_to_message", {}).get("from", {}).get("is_bot", False)
        is_command = bool((text or "").strip().startswith("/"))
        respond_all = os.environ.get("GROUP_RESPOND_ALL", "1") == "1"
        if not (mentioned or replied or is_command or respond_all):
            save_state()
            return
        # throttle + probability gate for non-mentioned chatter
        if not (mentioned or replied or is_command):
            s = get_settings(chat_id)
            last_resp = s.get("last_bot_response", 0)
            prob = float(os.environ.get("GROUP_REPLY_PROBABILITY", "0.3"))
            # questions are more likely directed at the bot
            if "?" in (text or ""):
                prob = max(prob, 0.7)
            if time.time() - last_resp < 10:
                save_state()
                return
            # skip very short / trivial messages
            if len((text or "").strip()) < 4:
                save_state()
                return
            if random.random() > prob:
                save_state()
                return
            s["last_bot_response"] = time.time()
        # strip mention from text for LLM
        if uname:
            text = text.replace(f"@{uname}", "").strip() or text

    if not text:
        save_state()
        return

    cid = str(chat_id)

    # pending quiz
    if not text.startswith("/") and cid in pending_quiz:
        check_quiz_answer(chat_id, text)
        save_state()
        return

    # commands
    if text.startswith("/"):
        uname = (msg.get("from", {}) or {}).get("username")
        handled = handle_command(chat_id, user_id, text, uname)
        if handled:
            save_state()
            return

    # bare mode name -> switch mode
    if text.strip().lower() in MODES:
        mode_name = text.strip().lower()
        s = get_settings(chat_id)
        s["mode"] = mode_name
        history[str(chat_id)] = [{"role": "system", "content": "mode:" + mode_name}]
        tg_send(chat_id, "Режим переключён на: " + mode_name)
        save_state()
        return

    # normal chat
    if cid not in history:
        history[cid] = [{"role": "system", "content": "mode:" + get_settings(chat_id)["mode"]}]

    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

    # attribute message to its author so the bot doesn't confuse users
    author = ""
    u = msg.get("from") or {}
    if u:
        author = (u.get("first_name") or "").strip()
        if u.get("username"):
            author = (author + " (@" + u.get("username") + ")").strip()
    content_for_history = (f"[{author}]: {text}" if author else text)
    history[cid].append({"role": "user", "content": content_for_history})
    if len(history[cid]) > 16:
        sysmsg = history[cid][0]
        history[cid] = [sysmsg] + history[cid][-15:]

    mode = get_settings(chat_id)["mode"]
    global _current_member_context
    _current_member_context = member_context(chat_id)
    try:
        reply = chat(history[cid], mode=mode, max_tokens=40)
    except Exception as e:
        log(f"LLM error: {e}")
        reply = "Что-то пошло не так. Попробуй ещё раз."
    finally:
        _current_member_context = ""
    history[cid].append({"role": "assistant", "content": reply})
    store_last_reply(chat_id, reply)
    tg_send(chat_id, reply)
    save_state()

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

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
    try:
        bot_username()
    except Exception:
        pass
    app.run(host="0.0.0.0", port=port)
