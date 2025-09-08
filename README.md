# Pyatak Tickets API (fixed)

Готовый минимальный бэкенд для ваших форм заявок (Render/Heroku/локально). Исправляет:
- `server_error` после отправки (безопасная сериализация дат + безопасная отправка в Telegram),
- падения при пустом `deadline`/`updated_at`,
- HTML-ошибки вместо JSON (фронт больше не ломается),
- отсутствие колонок в БД (`updated_at`, `tg_chat_id`, `tg_message_id`) — добавляются автоматически.

## Быстрый старт (Render)

1. Создайте новый Web Service и укажите репозиторий с этими файлами.
2. В **Environment** добавьте:
   - `DATABASE_URL` — Postgres от Render
   - (опционально) `TELEGRAM_BOT_TOKEN`
   - (опционально) `TELEGRAM_CHAT_ID`
3. **Start Command**: `gunicorn app:app`
4. Деплой.

Сервис сам создаст/обновит схему таблицы `tickets` (idempotent).

## Локально

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=sqlite:///local.db
python app.py
```

## API

- `GET /health` → `{ "status": "ok" }`
- `GET /api/tickets?status=new&limit=300` → массив тикетов
- `POST /api/tickets` → создаёт тикет
  ```json
  {
    "club": "Пятак",
    "pc": "ПК 7",
    "description": "сломался принтер",
    "status": "new",
    "deadline": "2025-09-08T12:00:00Z"   // опционально
  }
  ```
- `PATCH /api/tickets/<id>` — обновление полей (`status`, `deadline`, и т.д.)

## Примечания

- Если `TELEGRAM_*` не заданы — заявка всё равно создаётся, а отправка в ТГ просто пропускается.
- Все ответы — **JSON**. Ошибки — тоже JSON (`{"error":"server_error"}`), без HTML.
- Даты сериализуются безопасно, `null` допустим.
