# Telegram Gemini Bot (Render.com)

Telegram-бот на Gemini с юмористической персоной, историей диалога, групповыми чатами и проактивными сообщениями.

## Возможности

- Личные чаты и группы (в группах отвечает по упоминанию или reply).
- История диалога на чат (до ~15 сообщений + системный промпт).
- Режимы юмора: `default`, `sarcasm`, `absurd`, `dark`, `friendly`.
- Команды: `/start`, `/help`, `/reset`, `/mode`, `/joke`, `/quiz`, `/answer`, `/roll`, `/coin`, `/weather`, `/fx`, `/timer`, `/stats`, `/rate`, `/top`, `/mute`, `/unmute`.
- Проактивные сообщения (не чаще раз в 4 часа, с тихими часами и дневным лимитом).
- Keep-alive: GitHub Actions пингует `/tick` каждые 10 минут.
- Health: `/`, `/ping`.

## Переменные окружения

- `TELEGRAM_TOKEN` — токен бота (BotFather)
- `ADMIN_ID` — Telegram ID администратора (для `/broadcast`)
- `GEMINI_API_KEY` — ключ Google AI Studio
- `MODEL` — модель Gemini (по умолчанию `gemini-3-flash-preview`)
- `SYSTEM_PROMPT` — базовый системный промпт (опционально)
- `PROACTIVE_INTERVAL` — интервал проактивных сообщений (сек, по умолчанию 14400)
- `PROACTIVE_PROBABILITY` — вероятность отправки (0..1, по умолчанию 0.35)
- `MAX_PROACTIVE_PER_DAY` — лимит проактивных сообщений в день на чат (по умолчанию 2)
- `QUIET_START`, `QUIET_END` — тихие часы (UTC, по умолчанию 23 и 8)

## Деплой на Render

1. Загрузите репозиторий на GitHub.
2. Render -> New -> Web Service -> выберите репозиторий.
3. Render прочитает `render.yaml`.
4. В Environment задайте переменные (см. выше).
5. Create Web Service.
6. Откройте `https://<имя>.onrender.com/setwebhook` — должен вернуть `{"ok": true}`.
7. Проверьте `https://<имя>.onrender.com/ping`.

## Локальный запуск

```bash
pip install -r requirements.txt
python app.py
```

## Keep-alive

GitHub Actions (`.github/workflows/keepalive.yml`) пингует `/tick` каждые 10 минут, что не даёт сервису заснуть и запускает проактивные сообщения.
