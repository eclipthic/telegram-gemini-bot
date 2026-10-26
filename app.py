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

# ---------- LLM providers ----------
def _build_system_text(messages, mode):
    system_text = ""
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
    if system_text.startswith("mode:"):
        system_text = MODES.get(mode, MODES["default"])
    elif not system_text:
        system_text = MODES.get(mode, MODES["default"])
    return system_text


def _gemini_request(messages, mode, max_tokens, temperature):
    system_text = _build_system_text(messages, mode)
    contents = []
    for m in messages:
        if m["role"] == "system":
            continue
        gemini_role = "user" if m["role"] == "user" else "model"
        contents.append({"role": gemini_role, "parts": [{"text": m["content"]}]} )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    def do_request(tb):
        gen = {"maxOutputTokens": max_tokens, "temperature": temperature, "topP": 0.95}
        if tb is not None:
            gen["thinkingConfig"] = {"thinkingBudget": tb}
        payload = {"contents": contents, "generationConfig": gen}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
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
            if e.code == 429:
                time.sleep(3)
                try:
                    res = do_request(tb)
                    if res:
                        return res
                except Exception:
                    pass
            elif e.code in (400, 404) and tb == 0:
                continue
            else:
                try:
                    log(f"Gemini HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
                except Exception:
                    pass
        except Exception as e:
            log(f"Gemini error: {e}")
    return None


def _openai_compat_chat(base_url, api_key, model, messages, system_text, temperature=0.85, max_tokens=1000, extra_headers=None):
    url = base_url.rstrip("/") + "/chat/completions"
    msgs = []
    if system_text:
        msgs.append({"role": "system", "content": system_text})
    for m in messages:
        if m["role"] == "system":
            continue
        msgs.append({"role": m["role"], "content": m["content"]})
    payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode("utf-8"))
    choices = resp.get("choices", [])
    if not choices:
        return None
    return (choices[0].get("message", {}).get("content") or "").strip() or None


def gemini_chat(messages, mode="default", max_tokens=1000, temperature=0.85):
    system_text = _build_system_text(messages, mode)
    # 1. Gemini
    if GEMINI_API_KEY:
        try:
            r = _gemini_request(messages, mode, max_tokens, temperature)
            if r:
                return r
        except Exception as e:
            log(f"Gemini failed: {e}")
    # 2. Pollinations (free, no key) - try several models
    pollinations_models = ["openai", "mistral", "llama", "gemini", "qwen"]
    if os.environ.get("POLLINATIONS_MODEL"):
        pollinations_models = [os.environ.get("POLLINATIONS_MODEL")] + pollinations_models
    for pm in pollinations_models:
        try:
            r = _openai_compat_chat(
                "https://text.pollinations.ai/openai",
                "",
                pm,
                messages, system_text, temperature, max_tokens,
            )
            if r:
                return r
        except Exception as e:
            log(f"Pollinations ({pm}) failed: {e}")
    # fallback: direct text endpoint
    try:
        import urllib.parse as _up
        last_user = ""
        for m in reversed(messages):
            if m["role"] == "user":
                last_user = m["content"]
                break
        prompt = (system_text + "\n" + last_user).strip()
        url = "https://text.pollinations.ai/" + _up.quote(prompt[:500])
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode("utf-8", errors="replace").strip()
        if txt:
            return txt
    except Exception as e:
        log(f"Pollinations direct failed: {e}")
    # 3. Groq
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        try:
            r = _openai_compat_chat(
                "https://api.groq.com/openai/v1",
                groq_key,
                os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"),
                messages, system_text, temperature, max_tokens,
            )
            if r:
                return r
        except Exception as e:
            log(f"Groq failed: {e}")
    # 4. OpenRouter (free models)
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if or_key:
        try:
            r = _openai_compat_chat(
                "https://openrouter.ai/api/v1",
                or_key,
                os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"),
                messages, system_text, temperature, max_tokens,
                extra_headers={"HTTP-Referer": "https://github.com/eclipthic/telegram-gemini-bot", "X-Title": "Telegram Humor Bot"},
            )
            if r:
                return r
        except Exception as e:
            log(f"OpenRouter failed: {e}")
    # 5. Hugging Face
    hf_key = os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_API_KEY", "")
    if hf_key:
        try:
            r = _openai_compat_chat(
                "https://api-inference.huggingface.co/v1",
                hf_key,
                os.environ.get("HF_MODEL", "meta-llama/Llama-3.1-8B-Instruct"),
                messages, system_text, temperature, max_tokens,
            )
            if r:
                return r
        except Exception as e:
            log(f"HuggingFace failed: {e}")
    # 6. Ollama (local)
    ollama_url = os.environ.get("OLLAMA_URL", "")
    if ollama_url:
        try:
            r = _openai_compat_chat(
                ollama_url.rstrip("/") + "/v1",
                "",
                os.environ.get("OLLAMA_MODEL", "llama3.1"),
                messages, system_text, temperature, max_tokens,
            )
            if r:
                return r
        except Exception as e:
            log(f"Ollama failed: {e}")
    return "ИИ сейчас недоступен. Попробуй позже."


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
