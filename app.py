import os
import json
from datetime import datetime
from typing import Optional, Tuple

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError

# -----------------------------
# Flask app (создаём СРАЗУ)
# -----------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# -----------------------------
# DB URL fix для psycopg v3
# -----------------------------
db_url = os.environ.get("DATABASE_URL", "sqlite:///tickets.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://") and "+psycopg" not in db_url and "+psycopg2" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -----------------------------
# Модель
# -----------------------------
class Ticket(db.Model):
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(100), nullable=False)
    pc = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(1000), nullable=False)
    status = db.Column(db.String(50), nullable=False, default="new")

    # ВАЖНО: deadline допускаем NULL (иначе были 500 при None)
    deadline = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
    # раньше падало из-за отсутствия этой колонки — теперь есть и допускает NULL
    updated_at = db.Column(db.DateTime, nullable=True, server_default=db.func.now())

    # Колонки для записи сообщений в ТГ (могут быть NULL)
    tg_chat_id = db.Column(db.BigInteger, nullable=True)
    tg_message_id = db.Column(db.BigInteger, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "club": self.club,
            "pc": self.pc,
            "description": self.description,
            "status": self.status,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "created_at": (self.created_at.isoformat() if isinstance(self.created_at, datetime) else str(self.created_at)),
            "updated_at": (self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else str(self.updated_at) if self.updated_at else None),
            "tg_chat_id": self.tg_chat_id,
            "tg_message_id": self.tg_message_id,
        }

# Обновляем updated_at перед апдейтом
@event.listens_for(Ticket, "before_update")
def _touch_updated_at(mapper, connection, target):
    target.updated_at = datetime.utcnow()


# -----------------------------
# "Мягкая миграция" при старте
# -----------------------------
with app.app_context():
    engine = db.engine
    # если таблицы нет — просто создадим её по модели
    with engine.begin() as conn:
        exists = conn.exec_driver_sql("SELECT to_regclass('tickets')").scalar()

    if not exists:
        db.create_all()
    else:
        # Добьём недостающие колонки и снимем NOT NULL с deadline
        stmts = [
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tg_chat_id BIGINT;",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tg_message_id BIGINT;",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS deadline TIMESTAMP;",
            "ALTER TABLE tickets ALTER COLUMN deadline DROP NOT NULL;",
        ]
       with engine.begin() as conn:
            for s in stmts:
                try:
                    # ВАЖНО: передаём СТРОКУ, а не text(s)
                    conn.exec_driver_sql(s)
                except Exception as e:
                    app.logger.warning(f"Skip stmt `{s}`: {e}")

        # На всякий случай создадим таблицы, если каких-то вообще не было
        db.create_all()


# -----------------------------
# Утилиты
# -----------------------------
ALLOWED_STATUSES = {"new", "in_progress", "done", "cancelled"}

def parse_deadline(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Пытаемся ISO
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        pass
    # Популярный формат DD.MM.YYYY HH:MM
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


# -----------------------------
# Телеграм
# -----------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

def tg_send_ticket(ticket: Ticket) -> Tuple[Optional[int], Optional[int]]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        app.logger.info("Telegram envs not set — skip sending")
        return None, None

    text_lines = [
        f"🆕 *Новая заявка #{ticket.id}*",
        f"*Клуб:* {ticket.club}",
        f"*ПК:* {ticket.pc}",
        f"*Описание:* {ticket.description}",
    ]
    if ticket.deadline:
        text_lines.append(f"*Дедлайн:* {ticket.deadline.strftime('%d.%m.%Y %H:%M')}")

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "В работу", "callback_data": f"ticket:{ticket.id}:in_progress"},
                {"text": "Готово ✅", "callback_data": f"ticket:{ticket.id}:done"},
            ],
            [
                {"text": "Отменить ❌", "callback_data": f"ticket:{ticket.id}:cancelled"},
            ],
        ]
    }

    try:
        resp = requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": int(TELEGRAM_CHAT_ID),
                "text": "\n".join(text_lines),
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
            timeout=10,
        )
        data = resp.json()
        if resp.ok and data.get("ok"):
            msg = data.get("result", {})
            return msg.get("chat", {}).get("id"), msg.get("message_id")
        else:
            app.logger.warning(f"TG sendMessage not ok: {data}")
            return None, None
    except Exception as e:
        app.logger.warning(f"TG send failed: {e}")
        return None, None


def tg_edit_buttons(chat_id: int, message_id: int, new_status: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    label = {
        "new": "Новая",
        "in_progress": "В работе",
        "done": "Готово ✅",
        "cancelled": "Отменено ❌",
    }.get(new_status, new_status)

    keyboard = {
        "inline_keyboard": [
            [{"text": f"Статус: {label}", "callback_data": "noop"}]
        ]
    }
    try:
        requests.post(
            f"{TG_API}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": keyboard,
            },
            timeout=10,
        )
    except Exception as e:
        app.logger.warning(f"TG edit failed: {e}")


# -----------------------------
# Маршруты
# -----------------------------
@app.get("/health")
def health():
    return "OK", 200


@app.get("/api/tickets")
def list_tickets():
    try:
        status = request.args.get("status")
        limit = int(request.args.get("limit") or 300)
        q = Ticket.query
        if status:
            q = q.filter(Ticket.status == status)
        items = q.order_by(Ticket.created_at.desc()).limit(limit).all()
        return jsonify([i.to_dict() for i in items]), 200
    except Exception as e:
        app.logger.exception("Unhandled error in list_tickets")
        return jsonify({"error": "server_error"}), 500


@app.post("/api/tickets")
def create_ticket():
    try:
        data = request.get_json(force=True, silent=False) or {}
        club = (data.get("club") or "").strip()
        pc = (data.get("pc") or "").strip()
        description = (data.get("description") or "").strip()
        status = (data.get("status") or "new").strip() or "new"
        deadline_raw = data.get("deadline")

        if not club or not pc or not description:
            return jsonify({"error": "bad_request", "details": "club, pc, description обязательны"}), 400

        if status not in ALLOWED_STATUSES:
            status = "new"

        deadline = parse_deadline(deadline_raw)

        t = Ticket(
            club=club,
            pc=pc,
            description=description,
            status=status,
            deadline=deadline,
        )
        db.session.add(t)
        db.session.commit()  # нужно получить id

        # Пытаемся отправить в ТГ, но если не получится — это не 500
        chat_id, msg_id = tg_send_ticket(t)
        if chat_id and msg_id:
            t.tg_chat_id = chat_id
            t.tg_message_id = msg_id
            db.session.commit()

        return jsonify(t.to_dict()), 201
    except IntegrityError as e:
        db.session.rollback()
        # Защитимся от NOT NULL и т.п.
        return jsonify({"error": "bad_request", "details": "DB integrity error"}), 400
    except Exception:
        db.session.rollback()
        app.logger.exception("Unhandled error in create_ticket")
        return jsonify({"error": "server_error"}), 500


@app.patch("/api/tickets/<int:ticket_id>")
def update_ticket(ticket_id: int):
    try:
        data = request.get_json(force=True, silent=False) or {}
        t: Ticket = Ticket.query.get_or_404(ticket_id)

        # апдейтим что пришло
        if "status" in data:
            st = str(data["status"]).strip()
            if st in ALLOWED_STATUSES:
                t.status = st

        if "description" in data:
            d = str(data["description"]).strip()
            if d:
                t.description = d

        if "deadline" in data:
            t.deadline = parse_deadline(data["deadline"])

        db.session.commit()

        # Если есть сообщение в ТГ — подправим кнопки
        if t.tg_chat_id and t.tg_message_id:
            tg_edit_buttons(t.tg_chat_id, t.tg_message_id, t.status)

        return jsonify(t.to_dict()), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Unhandled error in update_ticket")
        return jsonify({"error": "server_error"}), 500


# Вебхук Telegram для кнопок
@app.post("/telegram/webhook")
def telegram_webhook():
    try:
        update = request.get_json(force=True, silent=True) or {}
        cb = update.get("callback_query")
        if not cb:
            return jsonify({"ok": True}), 200

        data = cb.get("data") or ""
        # ожидаем формат: ticket:<id>:<status>
        if not data.startswith("ticket:"):
            return jsonify({"ok": True}), 200

        parts = data.split(":")
        if len(parts) != 3:
            return jsonify({"ok": True}), 200

        _, sid, st = parts
        if st not in ALLOWED_STATUSES:
            st = "in_progress"

        ticket = Ticket.query.get(int(sid))
        if not ticket:
            # ответим в ТГ, что не нашли
            requests.post(f"{TG_API}/answerCallbackQuery", json={
                "callback_query_id": cb.get("id"),
                "text": "Заявка не найдена",
                "show_alert": False
            }, timeout=10)
            return jsonify({"ok": True}), 200

        ticket.status = st
        db.session.commit()

        # Ответим на нажатие, поправим клавиатуру
        try:
            requests.post(f"{TG_API}/answerCallbackQuery", json={
                "callback_query_id": cb.get("id"),
                "text": f"Статус: {st}",
                "show_alert": False
            }, timeout=10)
        except Exception:
            pass

        if ticket.tg_chat_id and ticket.tg_message_id:
            tg_edit_buttons(ticket.tg_chat_id, ticket.tg_message_id, st)

        return jsonify({"ok": True}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Unhandled error in telegram_webhook")
        return jsonify({"ok": False}), 200


# -----------------------------
# Точка входа для локала
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
