# Telegram Gemini Bot (Render.com)

Бесплатный Telegram-бот на Gemini (gemini-3-flash-preview), задеплоенный на Render.com (free tier).

## Деплой на Render

1. Создайте репозиторий на GitHub и загрузите эту папку.
2. Зайдите на https://render.com -> New -> Web Service -> подключите репозиторий.
3. Render автоматически определит Python и использует render.yaml.
4. В разделе Environment добавьте переменные:
   - TELEGRAM_TOKEN = токен бота (BotFather)
   - ADMIN_ID = ваш Telegram ID (число)
   - GEMINI_API_KEY = ключ Google AI Studio
   - MODEL = gemini-3-flash-preview (по умолчанию)
   - SYSTEM_PROMPT = системный промпт (по умолчанию)
5. Нажмите Create Web Service. Дождитесь деплоя.
6. После деплоя откройте в браузере: https://<ваш-сервис>.onrender.com/setwebhook
   - Должно вернуться {"ok": true, ...}
   - Это настроит webhook Telegram на ваш сервис.
7. Проверьте бота в Telegram.

## Keep-alive (чтобы сервис не засыпал)

Render free web service засыпает после 15 минут без HTTP-трафика.
Чтобы бот отвечал мгновенно, настройте бесплатный пинг каждые 10 минут:
- https://cron-job.org -> создайте job, URL: https://<ваш-сервис>.onrender.com/, интервал 10 минут.

## Ограничения free tier
- 512 MB RAM, 0.1 CPU
- Сервис засыпает после 15 мин неактивности (cold start ~30-60 сек)
- Файловая система эфемерная (история диалога хранится в памяти и теряется при рестарте)
- 750 часов/месяц (хватает на один сервис круглосуточно)

## Локальный запуск
pip install -r requirements.txt
python app.py