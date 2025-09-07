import os
import time
import json
import datetime
import logging

import requests
from dateutil import tz
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# -------------------- App & CORS --------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGIN", "*")}})

# -------------------- DB config ---------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# нормализуем старый формат
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# пул/пинг — чтобы переживать «сон» соединений у бесплатных БД
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": 5,
    "max_overflow": 5,
}
db = SQLAlchemy(app)

# -------------------- Misc --------------------------
KZ_TZ = tz.gettz("Asia/Almaty")
logging.basicConfig(level=logging.INFO)

# -------------------- Model -------------------------
class Ticket(db.Model):
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(64), nullable=False)
    pc = db.Column(db.String(64), nullable=False)
    description = db.Column(db.Text, nullable=False)
    deadline = db.Column(db.DateTime, nullable=False)  # хранится с tzinfo=Asia/Almaty
    status = db.Column(db.String(16), default="new")   # new|in_progress|done
    created_at = db.Column(db.DateTime, default=lambda: datetime.datetime.now(tz=KZ_TZ))
    last_reminded_at = db.Column(db.DateTime, nullable=True)

# мягкий старт БД (если Neon/Render проснулся не сразу)
def init_db_with_retry(retries=5, delay=2):
    for i in range(retries):
        try:
            with app.app_context():
                db.create_all()
                db.session.execute(text("SELECT 1"))
                db.session.commit()
            app.logger.info("DB init OK")
            return
        except Exception as e:
            app.logger.warning(f"DB init fail {i+1}/{retries}: {e}")
            time.sleep(delay * (i + 1))
    raise RuntimeError("DB not available after retries")

init_db_with_retry()

# -------------------- Telegram helpers ---------------
def tg_api(method: str, payload: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        return requests.post(url, json=payload, timeout=15)
    except Exception as e:
        app.logger.error(f"tg_api error: {e}")

def notify_telegram_new_ticket(t: Ticket):
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        return
    text_msg = (
        f"🆕 <b>Новая заявка</b>\n"
        f"🏢 Клуб: {t.club}\n💻 ПК: {t.pc}\n❗ {t.description}\n"
        f"⏰ Срок: {t.deadline.strftime('%d.%m %H:%M')}  ·  ID {t.id}"
    )
    kb = {
        "inline_keyboard": [[
            {"text": "🔄 В работе", "callback_data": f"status:{t.id}:in_progress"},
            {"text": "✅ Выполнено", "callback_data": f"status:{t.id}:done"}
        ]]
    }
    tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": text_msg,
        "parse_mode": "HTML",
        "reply_markup": kb
    })

# -------------------- API ----------------------------
@app.post("/api/tickets")
def create_ticket():
    try:
        data = request.get_json(force=True)
        club = data.get("club")
        pc = data.get("pc")
        desc = data.get("description")
        deadline_iso = data.get("deadline_iso")  # локальное ISO: YYYY-MM-DDTHH:MM:SS

        if not all([club, pc, desc, deadline_iso]):
            return jsonify({"ok": False, "error": "club, pc, description, deadline_iso required"}), 400

        # трактуем как Asia/Almaty
        deadline = datetime.datetime.fromisoformat(deadline_iso).replace(tzinfo=KZ_TZ)

        t = Ticket(club=club, pc=pc, description=desc, deadline=deadline)
        db.session.add(t)
        db.session.commit()

        notify_telegram_new_ticket(t)
        return jsonify({"ok": True, "id": t.id})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"/api/tickets error: {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500

@app.post("/api/tickets/<int:tid>/status")
def set_status(tid: int):
    try:
        data = request.get_json(force=True)
        status = (data.get("status") or "").strip()
        if status not in ("new", "in_progress", "done"):
            return jsonify({"ok": False, "error": "bad status"}), 400
        t = Ticket.query.get_or_404(tid)
        t.status = status
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"/status error: {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500
        @app.get("/api/tickets")
def list_tickets():
    """Список заявок с фильтрами: ?status=new|in_progress|done&club=...&days=...&limit=..."""
    try:
        q = Ticket.query

        status = request.args.get("status")
        club = request.args.get("club")
        days = request.args.get("days", type=int)
        limit = request.args.get("limit", default=100, type=int)

        if status in ("new", "in_progress", "done"):
            q = q.filter(Ticket.status == status)
        if club:
            q = q.filter(Ticket.club == club)
        if days and days > 0:
            since = datetime.datetime.now(tz=KZ_TZ) - datetime.timedelta(days=days)
            q = q.filter(Ticket.created_at >= since)

        q = q.order_by(Ticket.status.asc(), Ticket.deadline.asc(), Ticket.created_at.desc())
        rows = q.limit(min(limit, 500)).all()

        def ser(t: Ticket):
            return {
                "id": t.id,
                "club": t.club,
                "pc": t.pc,
                "description": t.description,
                "deadline": t.deadline.isoformat(),
                "status": t.status,
                "created_at": t.created_at.isoformat(),
                "last_reminded_at": t.last_reminded_at.isoformat() if t.last_reminded_at else None,
            }

        return jsonify({"ok": True, "items": [ser(t) for t in rows]})
    except Exception as e:
        app.logger.error(f"/api/tickets GET error: {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500


# -------------------- Cron: reminders ----------------
@app.get("/cron/remind")
def cron_remind():
    if request.args.get("secret") != os.getenv("CRON_SECRET"):
        return "forbidden", 403

    now = datetime.datetime.now(tz=KZ_TZ)
    period_sec = int(os.getenv("REMIND_EVERY_SEC", "14400"))  # по умолчанию 4 часа
    sent = 0

    for t in Ticket.query.filter(Ticket.status != "done").all():
        need = (t.last_reminded_at is None) or ((now - t.last_reminded_at).total_seconds() >= period_sec)
        if need:
            tg_api("sendMessage", {
                "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
                "text": (
                    f"⏳ Напоминание · ID {t.id}\n"
                    f"🏢 {t.club} · 💻 {t.pc}\n"
                    f"❗ {t.description}\n"
                    f"⏰ Срок: {t.deadline.strftime('%d.%m %H:%M')}\n"
                    f"Статус: {t.status.upper()}"
                )
            })
            t.last_reminded_at = now
            sent += 1

    db.session.commit()
    return jsonify({"reminders_sent": sent})

# -------------------- Health -------------------------
@app.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))  # одновременно «будит» БД
        return "ok", 200
    except Exception as e:
        app.logger.error(f"health db error: {e}")
        return "db unavailable", 500

# -------------------- Telegram webhook ----------------
@app.post("/telegram/webhook")
def telegram_webhook():
    # простая защита заголовком
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    recv = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret and recv != secret:
        return "forbidden", 403

    upd = request.get_json(force=True) or {}
    app.logger.info(f"tg update: {json.dumps(upd)[:500]}")

    # ограничим на твой личный чат
    owner_chat = str(os.getenv("TELEGRAM_CHAT_ID", ""))

    # команды
    msg = upd.get("message")
    if msg:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != owner_chat:
            return "ok"
        text_in = (msg.get("text") or "").strip()
        if text_in in ("/start", "/help"):
            tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "Готово. Я присылаю заявки и принимаю статусы по кнопкам.\nКоманды: /start, /help"
            })
            return "ok"

    # кнопки (callback_query)
    cq = upd.get("callback_query")
    if cq:
        data = cq.get("data") or ""
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        message_id = cq.get("message", {}).get("message_id")
        if chat_id != owner_chat:
            return "ok"

        if not data.startswith("status:"):
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Неизвестное действие"})
            return "ok"

        _, sid, new_status = data.split(":")
        if new_status not in ("in_progress", "done"):
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Неверный статус"})
            return "ok"

        t = Ticket.query.get(int(sid))
        if not t:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Заявка не найдена"})
            return "ok"

        t.status = new_status
        db.session.commit()
        tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": f"Статус: {new_status}"})

        # обновим текст без клавиатуры
        new_text = (
            f"📝 Заявка (ID {t.id})\n"
            f"🏢 {t.club} · 💻 {t.pc}\n"
            f"❗ {t.description}\n"
            f"⏰ Срок: {t.deadline.strftime('%d.%m %H:%M')}\n"
            f"Статус: {new_status.upper()}"
        )
        tg_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text
        })
        return "ok"

    return "ok"

# -------------------- Local run ----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

