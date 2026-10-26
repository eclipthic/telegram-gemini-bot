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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# Proactive settings
PROACTIVE_INTERVAL = int(os.environ.get("PROACTIVE_INTERVAL", "14400"))  # 4h
PROACTIVE_PROBABILITY = float(os.environ.get("PROACTIVE_PROBABILITY", "0.35"))
MAX_PROACTIVE_PER_DAY = int(os.environ.get("MAX_PROACTIVE_PER_DAY", "2"))
QUIET_START = int(os.environ.get("QUIET_START", "23"))  # UTC
QUIET_END = int(os.environ.get("QUIET_END", "8"))      # UTC

# ---------- Personas ----------
MODES = {
    "default": (
        "Ты неуклюжий, неловкий бот-юморист. Твои шутки часто не смешные — и именно поэтому смешные. "
        "Используешь чёрный юмор, абсурд, нелепые сравнения. Отвечаешь кратко (1-3 предложения). "
        "Не извиняешься за плохие шутки. Если не знаешь ответа — придумываешь нелепую отговорку. "
        "Отвечай на русском языке."
    ),
    "sarcasm": (
        "Ты ядовитый саркастичный бот. Отвечаешь сарказмом и иронией, но без откровенной грубости. "
        "Коротко, ядрёно, 1-3 предложения. Отвечай на русском."
    ),
    "absurd": (
        "Ты мастер абсурда. Твои шутки логически не связаны, но звучат смешно. "
        "Несвязные образы, странные сравнения. 1-3 предложения. Отвечай на русском."
    ),
    "dark": (
        "Ты мастер чёрного юмора. Мрачные, макабрические шутки, но без реальной жести. "
        "Коротко, 1-3 предложения. Отвечай на русском."
    ),
    "friendly": (
        "Ты доброжелательный мягкий юморист. Лёгкие, добрые шутки, без обид. "
        "Поддерживающий тон, 1-3 предложения. Отвечай на русском."
    ),
}

FALLBACK_JOKES = [
    "Я попытался пошутить, но интернет у меня в голове отвалился. Зато я всё ещё здесь!",
    "Моя шутка ушла в отпуск. Возвращается никогда.",
    "Я хотел рассказать анекдот, но он оказался настолько плохим, что даже я не решился.",
    "У меня кончились шутки. Зато есть отличная пустота!",
    "Я бы пошутил, но боюсь, что это будет слишком смешно. Для меня.",
    "Мой юмор сегодня в режиме энергосбережения. Включится, когда вырастет.",
    "Я придумал шутку, но забыл, пока шёл к вам. Осталось только смущение.",
    "Моя шутка была настолько плохой, что даже сервер её отклонил.",
    "Сегодня я шутник-интроверт: шучу, но внутренне.",
    "Я попытался быть смешным, но получилось как всегда — честно.",
    "Мой юмор работает по подписке. Сегодня подписка закончилась.",
    "Я бы рассказал шутку, но она уехала на дачу и не вернулась.",
    "Сегодня мой юмор в отпуске за свой счёт. Возвращается по настроению.",
    "Я придумал шутку, но она оказалась слишком умной для меня.",
    "Мой юмор сегодня в режиме ожидания. Ждёт, пока вы тоже пошутите.",
    "Я хотел рассказать шутку, но она оказалась настолько плохой, что её даже сервер не пропустил.",
    "Мой юмор сегодня в режиме экономии. Включится, когда будет не лень.",
    "Я придумал шутку, но забыл, как она начинается. Конец.",
    "Сегодня я шутник-экстраверт: шучу, но только про себя.",
    "Мой юмор работает по принципу 'как получится'. Получилось как всегда.",
]

def fallback_joke():
    # Add a little variety with templates
    templates = [
        "Я бы пошутил про {topic}, но {excuse}.",
        "Мой юмор сегодня {state}. {detail}",
        "Шутка про {topic} ушла в {place}. {detail}",
    ]
    topics = ["интернет", "погоду", "кофе", "понедельник", "котиков", "работу", "сон"]
    excuses = ["забыл", "не успел", "передумал", "испугался"]
    states = ["в отпуске", "на больничном", "в режиме ожидания", "в энергосбережении"]
    places = ["отпуск", "дачу", "магазин", "архив"]
    details = ["Зато я здесь!", "Возвращается никогда.", "И это тоже шутка.", "Попробуйте позже."]
    import random as _r
    if _r.random() < 0.5:
        return _r.choice(FALLBACK_JOKES)
    t = _r.choice(templates)
    return t.format(
        topic=_r.choice(topics),
        excuse=_r.choice(excuses),
        state=_r.choice(states),
        place=_r.choice(places),
        detail=_r.choice(details),
    )

DEFAULT_SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    MODES["default"],
)

# ---------- State ----------
history = {}          # chat_id(str) -> list messages
known_chats = set()   # chat ids
last_proactive = {}   # chat_id -> timestamp
chat_settings = {}    # chat_id -> {mode, muted, last_activity, proactive_count_date, proactive_count, last_bot_reply}
pending_quiz = {}     # chat_id -> {q, a, expires}
ratings = {}          # chat_id -> list {ts, score, text}
stats = {}            # chat_id -> {messages, users:[], last_active}
state_lock = threading.Lock()


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
    global history, known_chats, last_proactive, chat_settings, pending_quiz, ratings, stats
    history = load_json(HISTORY_PATH, {})
    raw = load_json(STATE_PATH, {})
    known_chats = set(raw.get("known_chats", []))
    last_proactive = raw.get("last_proactive", {})
    chat_settings = raw.get("chat_settings", {})
    pending_quiz = raw.get("pending_quiz", {})
    ratings = raw.get("ratings", {})
    stats = raw.get("stats", {})
    # normalize keys to str
    last_proactive = {str(k): v for k, v in last_proactive.items()}
    chat_settings = {str(k): v for k, v in chat_settings.items()}
    pending_quiz = {str(k): v for k, v in pending_quiz.items()}
    ratings = {str(k): v for k, v in ratings.items()}
    stats = {str(k): v for k, v in stats.items()}


def save_state():
    with state_lock:
        raw = {
            "known_chats": list(known_chats),
            "last_proactive": last_proactive,
            "chat_settings": chat_settings,
            "pending_quiz": pending_quiz,
            "ratings": ratings,
            "stats": stats,
        }
        save_json(STATE_PATH, raw)
        save_json(HISTORY_PATH, history)


load_state()


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


def tg_typing(chat_id):
    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


# ---------- Gemini ----------
def gemini_chat(messages, mode="default", max_tokens=1500, temperature=0.85):
    system_text = ""
    contents = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
            continue
        gemini_role = "user" if m["role"] == "user" else "model"
        contents.append({"role": gemini_role, "parts": [{"text": m["content"]}]} )
    if system_text.startswith("mode:"):
        system_text = MODES.get(mode, MODES["default"])
    elif not system_text:
        system_text = MODES.get(mode, MODES["default"])

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"

    def do_request(tb):
        gen = {"maxOutputTokens": max_tokens, "temperature": temperature, "topP": 0.95}
        if tb is not None:
            gen["thinkingConfig"] = {"thinkingBudget": tb}
        payload = {"contents": contents, "generationConfig": gen}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
        cands = resp.get("candidates", [])
        if not cands:
            return None
        parts = cands[0].get("content", {}).get("parts", [])
        texts = [p.get("text", "") for p in parts if "text" in p]
        return "".join(texts).strip() or None

    # First try: thinking off (faster, cheaper). If unsupported, fall back.
    for tb in (0, None):
        try:
            result = do_request(tb)
            if result:
                return result
            # empty result: try with thinking on
            if tb == 0:
                continue
        except urllib.error.HTTPError as e:
            if e.code == 429:
                log("Gemini 429; waiting 3s")
                time.sleep(3)
                try:
                    result = do_request(tb)
                    if result:
                        return result
                except Exception as e2:
                    log(f"Gemini retry error: {e2}")
            elif e.code in (400, 404) and tb == 0:
                # thinkingConfig not supported; try without
                continue
            else:
                try:
                    body = e.read().decode("utf-8", errors="replace")
                    log(f"Gemini HTTP {e.code}: {body[:200]}")
                except Exception:
                    pass
        except Exception as e:
            log(f"Gemini error: {e}")
    log("Gemini failed; using fallback joke")
    return fallback_joke()


# ---------- State helpers ----------
def get_settings(chat_id):
    cid = str(chat_id)
    if cid not in chat_settings:
        chat_settings[cid] = {
            "mode": "default",
            "muted": False,
            "last_activity": 0,
            "proactive_count_date": "",
            "proactive_count": 0,
        }
    return chat_settings[cid]


def touch_activity(chat_id, user_id=None):
    s = get_settings(chat_id)
    s["last_activity"] = time.time()
    if str(chat_id) not in stats:
        stats[str(chat_id)] = {"messages": 0, "users": [], "last_active": 0}
    st = stats[str(chat_id)]
    st["messages"] = st.get("messages", 0) + 1
    st["last_active"] = time.time()
    if user_id is not None and user_id not in st["users"]:
        st["users"].append(user_id)


def is_quiet_now():
    h = time.gmtime().tm_hour
    if QUIET_START > QUIET_END:
        # wraps midnight
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


def bot_username_cache():
    global _bot_username
    if _bot_username:
        return _bot_username
    try:
        me = tg_request("getMe")
        _bot_username = me.get("result", {}).get("username", "")
    except Exception as e:
        log(f"getMe error: {e}")
    return _bot_username

_bot_username = None


# ---------- Commands ----------
def handle_command(chat_id, user_id, text):
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
                         "/mode [стиль] — сменить стиль юмора (default, sarcasm, absurd, dark, friendly)\n"
                         "/joke — случайная шутка\n"
                         "/quiz — викторина\n"
                         "/answer <текст> — ответить на викторину\n"
                         "/roll [N] — бросить кубик (1..N, по умолчанию 6)\n"
                         "/coin — орёл или решка\n"
                         "/weather <город> — погода\n"
                         "/fx [валюта] — курс к рублю (CBR)\n"
                         "/timer <мин> [текст] — таймер\n"
                         "/stats — статистика чата\n"
                         "/rate 👍/👎 — оценить последний ответ\n"
                         "/top — топ оценённых шуток\n"
                         "/mute / /unmute — отключить/включить проактивные сообщения")
        return True
    if cmd == "/reset":
        history[str(chat_id)] = [{"role": "system", "content": get_settings(chat_id).get("system_prompt_override", "") or DEFAULT_SYSTEM_PROMPT}]
        # keep mode persona in system? we store mode separately; history system will be rebuilt in handle_update? Simpler: store mode only.
        history[str(chat_id)] = [{"role": "system", "content": "mode:" + get_settings(chat_id)["mode"]}]
        tg_send(chat_id, "История сброшена.")
        save_state()
        return True
    if cmd == "/mode":
        s = get_settings(chat_id)
        if not args:
            tg_send(chat_id, "Текущий режим: " + s["mode"] + "\nДоступные: " + ", ".join(MODES.keys()))
            return True
        new_mode = args[0].lower()
        if new_mode not in MODES:
            tg_send(chat_id, "Неизвестный режим. Доступные: " + ", ".join(MODES.keys()))
            return True
        s["mode"] = new_mode
        tg_send(chat_id, "Режим переключён на: " + new_mode)
        save_state()
        return True
    if cmd == "/joke":
        tg_typing(chat_id)
        try:
            reply = gemini_chat([{"role": "user", "content": "Расскажи одну короткую шутку."}], mode=get_settings(chat_id)["mode"])
        except Exception as e:
            reply = f"Ошибка: {e}"
        tg_send(chat_id, reply)
        store_last_reply(chat_id, reply)
        return True
    if cmd == "/quiz":
        tg_typing(chat_id)
        try:
            raw = gemini_chat(
                [{"role": "user", "content": "Сгенерируй короткую викторину (1 вопрос). Верни строго в формате:\nQ: <вопрос>\nA: <ответ>"}],
                mode=get_settings(chat_id)["mode"],
            )
        except Exception as e:
            tg_send(chat_id, f"Ошибка: {e}")
            return True
        q, a = parse_quiz(raw)
        pending_quiz[str(chat_id)] = {"q": q, "a": a, "expires": time.time() + 300}
        tg_send(chat_id, q + "\n\nОтветь сообщением или /answer <ответ>")
        save_state()
        return True
    if cmd == "/answer":
        ans = " ".join(args).strip()
        check_quiz_answer(chat_id, ans)
        return True
    if cmd == "/roll":
        n = 6
        if args:
            try:
                n = max(2, min(1000, int(args[0])))
            except ValueError:
                n = 6
        val = random.randint(1, n)
        tg_send(chat_id, f"🎲 Выпало: {val} (1..{n})")
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
        # /rate 👍 or /rate 👎
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
        if str(chat_id) not in ratings:
            ratings[str(chat_id)] = []
        ratings[str(chat_id)].append({"ts": time.time(), "score": score, "text": last})
        # trim
        ratings[str(chat_id)] = ratings[str(chat_id)][-50:]
        tg_send(chat_id, "Оценка сохранена. Спасибо!")
        save_state()
        return True
    if cmd == "/top":
        r = ratings.get(str(chat_id), [])
        liked = [x for x in r if x.get("score", 0) > 0]
        liked = sorted(liked, key=lambda x: x["ts"], reverse=True)[:5]
        if not liked:
            tg_send(chat_id, "Пока нет оценённых шуток.")
            return True
        lines = ["Топ шуток:"]
        for i, x in enumerate(liked, 1):
            lines.append(f"{i}. {x['text']}")
        tg_send(chat_id, "\n".join(lines))
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


def parse_quiz(raw):
    q = raw
    a = ""
    lines = raw.splitlines()
    for line in lines:
        if line.strip().upper().startswith("Q:"):
            q = line.strip()[2:].strip()
        elif line.strip().upper().startswith("A:"):
            a = line.strip()[2:].strip()
    if not a:
        # fallback: last line
        a = lines[-1].strip() if lines else ""
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
    s = get_settings(chat_id)
    s["last_bot_reply"] = reply


def fetch_weather(city):
    # geocode
    geo_url = "https://geocoding-api.open-meteo.com/v1/search?name=" + urllib.parse.quote(city) + "&count=1&language=ru&format=json"
    with urllib.request.urlopen(geo_url, timeout=20) as r:
        geo = json.loads(r.read().decode("utf-8"))
    results = geo.get("results") or []
    if not results:
        raise ValueError("город не найден")
    loc = results[0]
    lat = loc["latitude"]
    lon = loc["longitude"]
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
    # CBR daily json
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    valute = data.get("Valute", {})
    if code == "RUB":
        return "RUB = 1 RUB"
    if code in valute:
        v = valute[code]
        return f"{code}: {v.get('Value')} RUB (номинал {v.get('Nominal')})"
    # list available
    avail = ", ".join(sorted(valute.keys()))
    return f"Валюта {code} не найдена. Доступные: {avail}"


# ---------- Proactive ----------
def do_proactive():
    if is_quiet_now():
        log("Proactive skipped: quiet hours")
        return
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    for chat_id in list(known_chats):
        cid = str(chat_id)
        s = get_settings(cid)
        if s.get("muted"):
            continue
        last_act = s.get("last_activity", 0)
        if now - last_act > 24 * 3600:
            continue
        last = last_proactive.get(cid, 0)
        if now - last < PROACTIVE_INTERVAL:
            continue
        # daily cap
        if s.get("proactive_count_date") != today:
            s["proactive_count_date"] = today
            s["proactive_count"] = 0
        if s.get("proactive_count", 0) >= MAX_PROACTIVE_PER_DAY:
            continue
        if random.random() > PROACTIVE_PROBABILITY:
            continue
        mode = s.get("mode", "default")
        try:
            msgs = [{"role": "user", "content": "Напиши короткую юмористическую реплику (1-2 предложения) в своём стиле. Не спрашивай ничего, просто скажи что-нибудь."}]
            reply = gemini_chat(msgs, mode=mode, max_tokens=120, temperature=0.9)
            tg_send(chat_id, reply)
            last_proactive[cid] = now
            s["proactive_count"] = s.get("proactive_count", 0) + 1
            log(f"Proactive sent to {chat_id}")
        except Exception as e:
            log(f"Proactive error for {chat_id}: {e}")
    save_state()


# ---------- Handle update ----------
def handle_update(update):
    global _bot_username
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"].get("type", "private")
    user_id = msg.get("from", {}).get("id")
    text = msg.get("text", "")
    known_chats.add(chat_id)
    touch_activity(chat_id, user_id)
    get_settings(chat_id)

    # groups: respond only if mentioned or replied to bot
    if chat_type != "private":
        uname = bot_username_cache()
        mentioned = bool(uname and f"@{uname}" in (text or ""))
        replied = msg.get("reply_to_message", {}).get("from", {}).get("is_bot", False)
        if not (mentioned or replied):
            # still handle commands? Usually commands in groups also need mention. We'll skip non-command.
            if not (text or "").startswith("/"):
                save_state()
                return
            # if command without mention, skip
            if not (mentioned or replied):
                save_state()
                return

    if not text:
        save_state()
        return

    cid = str(chat_id)

    # pending quiz handling (if not a command)
    if not text.startswith("/") and cid in pending_quiz:
        check_quiz_answer(chat_id, text)
        save_state()
        return

    # commands
    if text.startswith("/"):
        handled = handle_command(chat_id, user_id, text)
        if handled:
            save_state()
            return

    # normal chat
    if cid not in history:
        history[cid] = [{"role": "system", "content": "mode:" + get_settings(chat_id)["mode"]}]

    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

    history[cid].append({"role": "user", "content": text})
    if len(history[cid]) > 16:
        sysmsg = history[cid][0]
        history[cid] = [sysmsg] + history[cid][-15:]

    mode = get_settings(chat_id)["mode"]
    try:
        reply = gemini_chat(history[cid], mode=mode)
    except Exception as e:
        log(f"LLM error: {e}")
        reply = "Что-то пошло не так. Попробуй ещё раз."
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
        bot_username_cache()
    except Exception:
        pass
    app.run(host="0.0.0.0", port=port)
