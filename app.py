import os, time, datetime, logging
from dateutil import tz
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text
import requests

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGIN", "*")}})

# ---- DB config --------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# устойчивость к «проспавшим» коннектам
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": 5,
    "max_overflow": 5,
}
db = SQLAlchemy(app)

KZ_TZ = tz.gettz("Asia/Almaty")
logging.basicConfig(level=logging.INFO)

class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(64), nullable=False)
    pc = db.Column(db.String(64), nullable=False)
    description = db.Column(db.Text, nullable=False)
    deadline = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(16), default="new")
    created_at = db.Column(db.DateTime, default=lambda: datetime.datetime.now(tz=KZ_TZ))
    last_reminded_at = db.Column(db.DateTime, nullable=True)

def init_db_with_retry(retries=5, delay=2):
    # мягкий старт, если Neon проснулся не сразу
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

def notify_telegram(text_msg: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text_msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        app.logger.error(f"Telegram send error: {e}")

@app.post("/api/tickets")
def create_ticket():
    try:
        data = request.get_json(force=True)
        club = data.get("club")
        pc = data.get("pc")
        desc = data.get("description")
        deadline_iso = data.get("deadline_iso")
        if not all([club, pc, desc, deadline_iso]):
            return jsonify({"ok": False, "error": "club, pc, description, deadline_iso required"}), 400

        deadline = datetime.datetime.fromisoformat(deadline_iso).replace(tzinfo=KZ_TZ)

        t = Ticket(club=club, pc=pc, description=desc, deadline=deadline)
        db.session.add(t)
        db.session.commit()

        notify_telegram(
            f"🆕 <b>Новая заявка</b>\n"
            f"🏢 Клуб: {club}\n💻 ПК: {pc}\n❗ {desc}\n"
            f"⏰ Срок: {deadline.strftime('%d.%m %H:%M')}\nID: {t.id}"
        )
        return jsonify({"ok": True, "id": t.id})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"/api/tickets error: {e}")
        return jsonify({"ok": False, "error": "server_error"}), 500

@app.post("/api/tickets/<int:tid>/status")
def set_status(tid):
    try:
        data = request.get_json(force=True)
        status = data.get("status")
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

@app.get("/cron/remind")
def cron_remind():
    if request.args.get("secret") != os.getenv("CRON_SECRET"):
        return "forbidden", 403
    now = datetime.datetime.now(tz=KZ_TZ)
    period_sec = int(os.getenv("REMIND_EVERY_SEC", "14400"))
    sent = 0
    for t in Ticket.query.filter(Ticket.status != "done").all():
        need = (t.last_reminded_at is None) or ((now - t.last_reminded_at).total_seconds() >= period_sec)
        if need:
            notify_telegram(
                f"⏳ Напоминание · ID {t.id}\n"
                f"🏢 {t.club} · 💻 {t.pc}\n❗ {t.description}\n"
                f"Срок: {t.deadline.strftime('%d.%m %H:%M')}\nСтатус: {t.status.upper()}"
            )
            t.last_reminded_at = now
            sent += 1
    db.session.commit()
    return jsonify({"reminders_sent": sent})

@app.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))  # одновременно «будит» Neon
        return "ok", 200
    except Exception as e:
        app.logger.error(f"health db error: {e}")
        return "db unavailable", 500
