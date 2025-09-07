# Pyatak Tickets API

## Render (Web Service)
- Build Command:   pip install -r requirements.txt
- Start Command:   gunicorn app:app
- Environment:
  DATABASE_URL=postgresql+psycopg2://USER:PASS@HOST/db?sslmode=require
  CORS_ORIGIN=https://<STATIC-SITE>.onrender.com
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  TELEGRAM_WEBHOOK_SECRET=...
  CRON_SECRET=...
  REMIND_EVERY_SEC=14400

## Webhook
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<API>.onrender.com/telegram/webhook" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"

## Health
GET /health

## Tickets
POST /api/tickets     {club, pc, description, deadline_iso}
GET  /api/tickets     ?status=&club=&days=&limit=
POST /api/tickets/<id>/status {status: new|in_progress|done}

## Reminders
GET /cron/remind?secret=<CRON_SECRET>
(создать cron на cron-job.org — 10:00, 13:00, 16:00, 19:00, 22:00 Asia/Almaty)
