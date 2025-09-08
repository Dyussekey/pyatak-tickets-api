import os
import logging
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")  # Render/Heroku-style URL
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    # SQLAlchemy 2.x expects postgresql://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # optional
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")      # optional

app = Flask(__name__)
CORS(app)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JSON_SORT_KEYS"] = False

db = SQLAlchemy(app)

# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------

class Ticket(db.Model):
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(255), nullable=False, default="")
    pc = db.Column(db.String(255), nullable=False, default="")
    description = db.Column(db.String(1024), nullable=False, default="")
    status = db.Column(db.String(50), nullable=False, default="new")

    # deadline может быть пустым
    deadline = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow, onupdate=datetime.utcnow)

    tg_chat_id = db.Column(db.BigInteger, nullable=True)
    tg_message_id = db.Column(db.BigInteger, nullable=True)


# ----------------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------------

def _ts(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None

def serialize_ticket(t: Ticket) -> dict:
    return {
        "id": t.id,
        "club": t.club,
        "pc": t.pc,
        "description": t.description,
        "status": t.status,
        "deadline": _ts(t.deadline),
        "created_at": _ts(t.created_at),
        "updated_at": _ts(t.updated_at),
        "tg_chat_id": t.tg_chat_id,
        "tg_message_id": t.tg_message_id,
    }

def safe_fromisoformat(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # поддержка "Z"
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        return None

def telegram_send(ticket: Ticket) -> Optional[int]:
    """Отправляем в Telegram. Возвращаем message_id или None. Никогда не кидаем исключения наружу."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        app.logger.info("Telegram is not configured, skip send.")
        return None

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        text_msg = f"🆕 Заявка #{ticket.id}\nКлуб: {ticket.club}\nПК: {ticket.pc}\nОписание: {ticket.description}"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text_msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        return (data.get("result") or {}).get("message_id")
    except Exception as e:
        app.logger.error("Telegram send failed: %s", e, exc_info=True)
        return None

# ----------------------------------------------------------------------------
# Schema bootstrap (idempotent). Ensures columns exist and deadline is nullable.
# ----------------------------------------------------------------------------

def ensure_schema():
    with app.app_context():
        engine = db.engine
        # 1) Создаём таблицу если её нет
        engine.execute(text("""
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            club VARCHAR(255) NOT NULL DEFAULT '',
            pc VARCHAR(255) NOT NULL DEFAULT '',
            description VARCHAR(1024) NOT NULL DEFAULT '',
            status VARCHAR(50) NOT NULL DEFAULT 'new',
            deadline TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NULL,
            tg_chat_id BIGINT NULL,
            tg_message_id BIGINT NULL
        );
        """))

        # 2) На всякий случай докидываем отсутствующие колонки и дефолты
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS club VARCHAR(255) NOT NULL DEFAULT '';"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS pc VARCHAR(255) NOT NULL DEFAULT '';"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS description VARCHAR(1024) NOT NULL DEFAULT '';"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'new';"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS deadline TIMESTAMP NULL;"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW();"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL;"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tg_chat_id BIGINT NULL;"))
        engine.execute(text("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tg_message_id BIGINT NULL;"))

        # 3) Убедимся, что deadline допускает NULL (если раньше был NOT NULL)
        engine.execute(text("ALTER TABLE tickets ALTER COLUMN deadline DROP NOT NULL;"))

        # 4) Индексы (опционально)
        engine.execute(text("CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets (created_at DESC);"))
        engine.execute(text("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status);"))

# В некоторых окружениях SQLAlchemy ленится создавать файл SQLite — подстрахуемся
with app.app_context():
    db.create_all()
    ensure_schema()

# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@app.get("/")
def root():
    return "OK", 200

@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

@app.get("/api/tickets")
def list_tickets():
    status = request.args.get("status")
    try:
        limit = int(request.args.get("limit", 100))
    except Exception:
        limit = 100
    limit = max(1, min(limit, 500))

    q = Ticket.query
    if status:
        q = q.filter(Ticket.status == status)

    items = q.order_by(Ticket.created_at.desc()).limit(limit).all()
    return jsonify([serialize_ticket(t) for t in items]), 200

@app.post("/api/tickets")
def create_ticket():
    payload = request.get_json(force=True, silent=True) or {}

    deadline = safe_fromisoformat(payload.get("deadline"))
    t = Ticket(
        club=(payload.get("club") or "").strip(),
        pc=(payload.get("pc") or "").strip(),
        description=(payload.get("description") or "").strip(),
        status=(payload.get("status") or "new").strip() or "new",
        deadline=deadline,
    )
    db.session.add(t)
    db.session.commit()  # сначала создаём тикет

    # Параллельно пытаемся отправить в ТГ, но не валим ответ
    try:
        msg_id = telegram_send(t)
        if msg_id:
            t.tg_message_id = msg_id
            # tg_chat_id нам известен из конфигурации; сохранять не обязательно
            if TELEGRAM_CHAT_ID:
                try:
                    t.tg_chat_id = int(TELEGRAM_CHAT_ID)
                except Exception:
                    pass
            db.session.commit()
    except Exception as e:
        app.logger.error("Telegram notify failed: %s", e, exc_info=True)

    return jsonify(serialize_ticket(t)), 201

@app.patch("/api/tickets/<int:ticket_id>")
def update_ticket(ticket_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    t = Ticket.query.get_or_404(ticket_id)

    # Обновляем только разрешённые поля
    if "club" in payload:
        t.club = (payload.get("club") or "").strip()
    if "pc" in payload:
        t.pc = (payload.get("pc") or "").strip()
    if "description" in payload:
        t.description = (payload.get("description") or "").strip()
    if "status" in payload:
        t.status = (payload.get("status") or t.status).strip() or t.status
    if "deadline" in payload:
        t.deadline = safe_fromisoformat(payload.get("deadline"))

    db.session.commit()
    return jsonify(serialize_ticket(t)), 200

# ----------------------------------------------------------------------------
# Error handlers — всегда возвращаем JSON, чтобы фронт не спотыкался о HTML
# ----------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not_found"}), 404

@app.errorhandler(Exception)
def handle_error(e):
    # Логируем, но наружу — аккуратный JSON
    app.logger.error("Unhandled error: %s", e, exc_info=True)
    return jsonify({"error": "server_error"}), 500

# ----------------------------------------------------------------------------
# WSGI
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    # Локальный запуск: python app.py
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
