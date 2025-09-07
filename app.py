import os
import logging
from datetime import datetime
from flask import Flask, request, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests

# ------------------------
# Конфиг и инициализация
# ------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

def _db_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Render даёт postgres:// — конвертируем под psycopg3
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# Разрешаем фронту ходить на API
CORS(app, origins=[os.environ.get("FRONTEND_ORIGIN", "*")])

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
DEFAULT_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0")) or None
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# ------------------------
# Модель
# ------------------------
class Ticket(db.Model):
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(120), nullable=True)
    pc = db.Column(db.String(120), nullable=True)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="new")
    # Главное исправление: используем колонку БД "deadline", но наружу поле называется deadline_at
    deadline_at = db.Column("deadline", db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    tg_chat_id = db.Column(db.BigInteger, nullable=True)
    tg_message_id = db.Column(db.BigInteger, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "club": self.club,
            "pc": self.pc,
            "description": self.description,
            "status": self.status,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "tg_chat_id": self.tg_chat_id,
            "tg_message_id": self.tg_message_id,
        }

with app.app_context():
    db.create_all()

# ------------------------
# Утилиты
# ------------------------
STATUS_EMOJI = {
    "new": "🆕",
    "in_progress": "⏳",
    "done": "✅",
    "cancelled": "🚫",
}

def human_status(s: str) -> str:
    return {
        "new": "Новая",
        "in_progress": "В работе",
        "done": "Выполнено",
        "cancelled": "Отменено",
    }.get(s, s)

def parse_dt(s: str | None):
    if not s:
        return None
    try:
        # ISO 8601
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        try:
            # "YYYY-MM-DD HH:MM"
            return datetime.strptime(s, "%Y-%m-%d %H:%M")
        except Exception:
            return None

def format_ticket_text(t: Ticket) -> str:
    due = f"\n🗓 Дедлайн: {t.deadline_at.strftime('%Y-%m-%d %H:%M')}" if t.deadline_at else ""
    head = f"{STATUS_EMOJI.get(t.status, '')} Заявка #{t.id}"
    body = f"🏢 Клуб: {t.club or '—'}\n💻 ПК: {t.pc or '—'}\n📝 {t.description}"
    return f"{head}\n{body}{due}\n\nСтатус: {STATUS_EMOJI.get(t.status, '')} {human_status(t.status)}"

def keyboard_for_ticket(t: Ticket):
    rows = []
    if t.status == "new":
        rows.append([{"text": "▶️ В работу", "callback_data": f"act:in_progress:{t.id}"}])
        rows.append([
            {"text": "✅ Выполнено", "callback_data": f"act:done:{t.id}"},
            {"text": "🚫 Отменить", "callback_data": f"act:cancelled:{t.id}"},
        ])
    elif t.status == "in_progress":
        rows.append([
            {"text": "✅ Выполнено", "callback_data": f"act:done:{t.id}"},
            {"text": "🚫 Отменить", "callback_data": f"act:cancelled:{t.id}"},
        ])
    elif t.status in ("done", "cancelled"):
        rows.append([{"text": "↩️ Снова в работу", "callback_data": f"act:in_progress:{t.id}"}])
    return {"inline_keyboard": rows}

def tg_call(method: str, payload: dict):
    if not TG_API:
        return {"ok": False, "error": "No BOT token"}
    url = f"{TG_API}/{method}"
    resp = requests.post(url, json=payload, timeout=10)
    if not resp.ok:
        log.error("TG %s %s: %s", method, resp.status_code, resp.text)
    ct = resp.headers.get("content-type", "")
    return resp.json() if "application/json" in ct else {"ok": False, "text": resp.text}

def send_ticket_message(t: Ticket, chat_id: int | None = None):
    if not (BOT_TOKEN and (chat_id or DEFAULT_CHAT_ID)):
        return
    payload = {
        "chat_id": chat_id or DEFAULT_CHAT_ID,
        "text": format_ticket_text(t),
        "reply_markup": keyboard_for_ticket(t),
        "parse_mode": "HTML",
    }
    data = tg_call("sendMessage", payload)
    if data.get("ok"):
        msg = data["result"]
        t.tg_chat_id = msg["chat"]["id"]
        t.tg_message_id = msg["message_id"]
        db.session.commit()

def edit_ticket_message(t: Ticket):
    if not (BOT_TOKEN and t.tg_chat_id and t.tg_message_id):
        return
    payload = {
        "chat_id": t.tg_chat_id,
        "message_id": t.tg_message_id,
        "text": format_ticket_text(t),
        "reply_markup": keyboard_for_ticket(t),
        "parse_mode": "HTML",
    }
    tg_call("editMessageText", payload)

def answer_callback(cb_id: str, text: str):
    tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text, "show_alert": False})

def set_status(t: Ticket, status: str):
    t.status = status
    t.updated_at = datetime.utcnow()
    db.session.commit()
    edit_ticket_message(t)

# ------------------------
# HTTP маршруты
# ------------------------
@app.get("/")
def index():
    return "ok", 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/api/tickets")
def list_tickets():
    limit = int(request.args.get("limit", "100"))
    limit = max(1, min(limit, 500))
    items = Ticket.query.order_by(Ticket.created_at.desc()).limit(limit).all()
    return jsonify([t.to_dict() for t in items])

@app.post("/api/tickets")
def create_ticket():
    data = request.get_json(force=True, silent=True) or {}
    t = Ticket(
        club=data.get("club"),
        pc=data.get("pc"),
        description=(data.get("description") or "").strip() or "—",
        status=data.get("status") or "new",
        deadline_at=parse_dt(data.get("deadline_at")),
    )
    db.session.add(t)
    db.session.commit()
    # отправим карточку в ТГ (если задан чат)
    send_ticket_message(t, chat_id=data.get("tg_chat_id") or DEFAULT_CHAT_ID)
    return jsonify(t.to_dict()), 201

@app.patch("/api/tickets/<int:ticket_id>")
def update_ticket(ticket_id: int):
    t = db.session.get(Ticket, ticket_id)
    if not t:
        abort(404)
    data = request.get_json(force=True, silent=True) or {}
    if "status" in data:
        t.status = data["status"]
    if "club" in data:
        t.club = data["club"]
    if "pc" in data:
        t.pc = data["pc"]
    if "description" in data:
        t.description = data["description"]
    if "deadline_at" in data:
        t.deadline_at = parse_dt(data["deadline_at"])
    t.updated_at = datetime.utcnow()
    db.session.commit()
    edit_ticket_message(t)
    return jsonify(t.to_dict())

# ------------------------
# Telegram webhook
# ------------------------
@app.post("/telegram/webhook")
def telegram_webhook():
    # проверка секрета (Telegram присылает в заголовке)
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != WEBHOOK_SECRET:
            abort(403)

    payload = request.get_json(force=True, silent=True) or {}
    log.info("tg update: %s", payload)

    # callback кнопок
    if "callback_query" in payload:
        cq = payload["callback_query"]
        cb_id = cq.get("id")
        data = (cq.get("data") or "")
        try:
            _, action, sid = data.split(":")
            sid = int(sid)
        except Exception:
            if cb_id:
                answer_callback(cb_id, "Неверные данные кнопки")
            return "ok", 200

        t = db.session.get(Ticket, sid)
        if not t:
            if cb_id:
                answer_callback(cb_id, "Заявка не найдена")
            return "ok", 200

        if action in ("new", "in_progress", "done", "cancelled"):
            set_status(t, action)
            if cb_id:
                answer_callback(cb_id, f"Статус: {human_status(action)}")
        else:
            if cb_id:
                answer_callback(cb_id, "Неизвестное действие")
        return "ok", 200

    # обычные сообщения/команды
    msg = payload.get("message") or {}
    text_in = (msg.get("text") or "").strip()

    if text_in.startswith("/help") or text_in.startswith("/start"):
        _help = (
            "Команды:\n"
            "/help — помощь\n"
            "/done <id> — отметить заявку как Выполнено\n"
            "Также используйте кнопки под карточкой заявки."
        )
        if BOT_TOKEN:
            tg_call("sendMessage", {"chat_id": msg["chat"]["id"], "text": _help})
        return "ok", 200

    if text_in.startswith("/done"):
        parts = text_in.split()
        if len(parts) >= 2 and parts[1].isdigit():
            sid = int(parts[1])
            t = db.session.get(Ticket, sid)
            if not t:
                tg_call("sendMessage", {"chat_id": msg["chat"]["id"], "text": f"Заявка #{sid} не найдена"})
                return "ok", 200
            set_status(t, "done")
            tg_call("sendMessage", {"chat_id": msg["chat"]["id"], "text": f"Заявка #{sid} — ✅ Выполнено"})
        else:
            tg_call("sendMessage", {"chat_id": msg["chat"]["id"], "text": "Формат: /done <id>"})
        return "ok", 200

    # по умолчанию — игнор
    return "ok", 200

# ------------------------
# WSGI
# ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
