import os, datetime
from dateutil import tz
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests

# --- базовая конфигурация ---
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGIN", "*")}})

DATABASE_URL = os.getenv("DATABASE_URL")  # возьмём из Neon/Supabase/Render Postgres
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

KZ_TZ = tz.gettz("Asia/Almaty")

class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(64), nullable=False)
    pc = db.Column(db.String(64), nullable=False)
    description = db.Column(db.Text, nullable=False)
    deadline = db.Column(db.DateTime, nullable=False)            # дедлайн в Asia/Almaty
    status = db.Column(db.String(16), default="new")             # new | in_progress | done
    created_at = db.Column(db.DateTime, default=lambda: datetime.datetime.now(tz=KZ_TZ))
    last_reminded_at = db.Column(db.DateTime, nullable=True)

with app.app_context():
    db.create_all()

def notify_telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

@app.post("/api/tickets")
def create_ticket():
    data = request.get_json(force=True)
    club = data.get("club")
    pc = data.get("pc")
    desc = data.get("description")
    deadline_iso = data.get("deadline_iso")  # ожидаем локальное ISO «YYYY-MM-DDTHH:MM:SS»

    if not all([club, pc, desc, deadline_iso]):
        return jsonify({"error": "club, pc, description, deadline_iso required"}), 400

    # трактуем присланное время как Asia/Almaty
    deadline = datetime.datetime.fromisoformat(deadline_iso).replace(tzinfo=KZ_TZ)

    t = Ticket(club=club, pc=pc, description=desc, deadline=deadline)
    db.session.add(t)
    db.session.commit()

    notify_telegram(
        f"🆕 <b>Новая заявка</b>\n"
        f"🏢 Клуб: {club}\n💻 ПК: {pc}\n❗ {desc}\n"
        f"⏰ Срок: {deadline.strftime('%d.%m %H:%M')}\n\n"
        f"Статус: NEW · ID {t.id}"
    )
    return jsonify({"ok": True, "id": t.id})

@app.post("/api/tickets/<int:tid>/status")
def set_status(tid):
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("new", "in_progress", "done"):
        return jsonify({"error": "bad status"}), 400
    t = Ticket.query.get_or_404(tid)
    t.status = status
    db.session.commit()
    return jsonify({"ok": True})

@app.get("/cron/remind")
def cron_remind():
    # защита простым секретом в query
    if request.args.get("secret") != os.getenv("CRON_SECRET"):
        return "forbidden", 403

    now = datetime.datetime.now(tz=KZ_TZ)
    period_sec = int(os.getenv("REMIND_EVERY_SEC", "14400"))  # каждые 4 часа
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
    return "ok", 200

if __name__ == "__main__":
    # локальный запуск
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
