# app.py  (Flask + SQLAlchemy + Telegram buttons)
# Индентация строго 4 пробела, без табов.

import os
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from dateutil import tz
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

# -----------------------------------------------------------------------------
# Конфиг
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGIN", "*")}})

# БД (Neon). Используем драйвер psycopg (v3), совместимый с Python 3.13
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()  # можно оставить пустым
TELEGRAM_WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET", "") or "").strip()

# Cron
CRON_SECRET = (os.getenv("CRON_SECRET", "") or "").strip()
REMIND_EVERY_SEC = int(os.getenv("REMIND_EVERY_SEC", "14400"))  # 4 часа по умолчанию

# Локальная таймзона для форматирования (Алматы)
KZ_TZ = tz.gettz("Asia/Almaty")

logging.basicConfig(level=logging.INFO)
logger = app.logger


# -----------------------------------------------------------------------------
# Модель
# -----------------------------------------------------------------------------
class Ticket(db.Model):
    __tablename__ = "tickets"
    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(50), nullable=False)
    pc = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="new")  # new | in_progress | done
    deadline_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    # Для редактирования исходного сообщения в Telegram
    tg_chat_id = db.Column(db.BigInteger, nullable=True)
    tg_message_id = db.Column(db.BigInteger, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "club": self.club,
            "pc": self.pc,
            "description": self.description,
            "status": self.status,
            "deadline_at": self.deadline_at.astimezone(KZ_TZ).isoformat() if self.deadline_at else None,
            "created_at": self.created_at.astimezone(KZ_TZ).isoformat() if self.created_at else None,
            "updated_at": self.updated_at.astimezone(KZ_TZ).isoformat() if self.updated_at else None,
        }


with app.app_context():
    db.create_all()


# -----------------------------------------------------------------------------
# Вспомогательные
# -----------------------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc)


def fmt_deadline(dt_utc: datetime | None) -> str:
    if not dt_utc:
        return "—"
    local = dt_utc.astimezone(KZ_TZ)
    left = dt_utc - now_utc()
    # Человеческое «осталось/просрочено»
    if left.total_seconds() >= 0:
        # осталось
        hrs = int(left.total_seconds() // 3600)
        mins = int((left.total_seconds() % 3600) // 60)
        left_str = f"через {hrs}ч {mins}м" if hrs else f"через {mins}м"
    else:
        left = -left
        hrs = int(left.total_seconds() // 3600)
        mins = int((left.total_seconds() % 3600) // 60)
        left_str = f"просрочено на {hrs}ч {mins}м" if hrs else f"просрочено на {mins}м"
    return f"{local.strftime('%d.%m %H:%M')} ({left_str})"


def status_human(s: str) -> str:
    return {"new": "🆕 Новая", "in_progress": "🔄 В работе", "done": "✅ Выполнено"}.get(s, s)


def build_keyboard(t: Ticket):
    # Две кнопки статусов + ссылка на историю
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 В работе", "callback_data": f"status:{t.id}:in_progress"},
                {"text": "✅ Выполнено", "callback_data": f"status:{t.id}:done"},
            ],
            [
                {"text": "🗒 История", "url": "https://pyatak.onrender.com/history.html"}
            ]
        ]
    }


def msg_ticket_text(t: Ticket, title: str = "Заявка") -> str:
    return (
        f"<b>{title}</b>\n"
        f"<b>Статус:</b> {status_human(t.status)}\n"
        f"<b>ID:</b> <code>{t.id}</code>\n"
        f"<b>Клуб:</b> {t.club}\n"
        f"<b>ПК:</b> {t.pc or '—'}\n"
        f"<b>Дедлайн:</b> {fmt_deadline(t.deadline_at)}\n"
        f"<b>Описание:</b>\n{(t.description or '').strip()}"
    )


def tg_api(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Telegram API %s -> %s %s", method, r.status_code, r.text[:400])
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except Exception as e:
        logger.exception("Telegram API error: %s", e)
        return None


def send_ticket_to_tg(t: Ticket):
    if not TELEGRAM_BOT_TOKEN:
        return
    # если TELEGRAM_CHAT_ID не указан — отправим туда, где позже нажмут кнопку; но лучше указать числовой id
    chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.isdigit() else TELEGRAM_CHAT_ID or None
    if not chat_id:
        # нет чата — просто выходим
        return
    res = tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": msg_ticket_text(t, "Новая заявка"),
        "parse_mode": "HTML",
        "reply_markup": build_keyboard(t),
        "disable_web_page_preview": True
    })
    try:
        if isinstance(res, dict) and res.get("ok") and res.get("result"):
            t.tg_chat_id = res["result"]["chat"]["id"]
            t.tg_message_id = res["result"]["message_id"]
            db.session.commit()
    except Exception:
        logger.exception("Failed to save tg message id")


def edit_ticket_message_in_tg(t: Ticket, title: str = "Заявка"):
    if not TELEGRAM_BOT_TOKEN or not t.tg_chat_id or not t.tg_message_id:
        return
    tg_api("editMessageText", {
        "chat_id": t.tg_chat_id,
        "message_id": t.tg_message_id,
        "text": msg_ticket_text(t, title),
        "parse_mode": "HTML",
        "reply_markup": build_keyboard(t),
        "disable_web_page_preview": True
    })


def _chat_allowed(chat: dict) -> bool:
    """
    Пускаем апдейты, если TELEGRAM_CHAT_ID пуст,
    либо совпадает с числовым id, либо с @username, либо с title группы/канала.
    """
    expected = TELEGRAM_CHAT_ID
    if not expected:
        return True
    cid = str(chat.get("id", ""))
    uname = chat.get("username")  # без @
    title = chat.get("title")
    variants = {cid}
    if uname:
        variants.add(f"@{uname}")
    if title:
        variants.add(title)
    return expected in variants


# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    return "ok", 200


@app.get("/api/tickets")
def list_tickets():
    q = Ticket.query.order_by(Ticket.created_at.desc()).all()
    return jsonify([t.to_dict() for t in q])


@app.post("/api/tickets")
def create_ticket():
    data = request.get_json(force=True) or {}
    club = (data.get("club") or "").strip()
    pc = (data.get("pc") or "").strip()
    description = (data.get("description") or "").strip()
    due = (data.get("due") or "").strip()  # today|tomorrow|3days (с фронта)

    if not club or not description:
        return jsonify({"error": "club и description обязательны"}), 400

    # дедлайн
    deadline = None
    if due == "today":
        deadline = now_utc().astimezone(KZ_TZ).replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)
    elif due == "tomorrow":
        local = now_utc().astimezone(KZ_TZ) + timedelta(days=1)
        deadline = local.replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)
    elif due == "3days":
        local = now_utc().astimezone(KZ_TZ) + timedelta(days=3)
        deadline = local.replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)

    t = Ticket(
        club=club,
        pc=pc or None,
        description=description,
        status="new",
        deadline_at=deadline
    )
    db.session.add(t)
    db.session.commit()

    # отправим в Telegram
    send_ticket_to_tg(t)

    return jsonify(t.to_dict()), 201


@app.post("/api/tickets/<int:sid>/status")
def set_status(sid: int):
    data = request.get_json(force=True) or {}
    new_status = (data.get("status") or "").strip()
    if new_status not in ("new", "in_progress", "done"):
        return jsonify({"error": "status должен быть: new|in_progress|done"}), 400

    t = db.session.get(Ticket, sid)
    if not t:
        return jsonify({"error": "not found"}), 404

    t.status = new_status
    t.updated_at = now_utc()
    db.session.commit()

    # если есть исходное сообщение — обновим карточку
    edit_ticket_message_in_tg(t)

    return jsonify(t.to_dict())


# -----------------------------------------------------------------------------
# Telegram webhook
# -----------------------------------------------------------------------------
@app.post("/telegram/webhook")
def telegram_webhook():
    # Проверка секрета из заголовка
    recv = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if TELEGRAM_WEBHOOK_SECRET and recv != TELEGRAM_WEBHOOK_SECRET:
        return "forbidden", 403

    upd = request.get_json(force=True) or {}
    logger.info("tg update: %s", json.dumps(upd)[:1000])

    # Обычные сообщения (команды)
    msg = upd.get("message")
    if msg:
        chat = msg.get("chat", {}) or {}
        if not _chat_allowed(chat):
            return "ok"

        chat_id = chat.get("id")
        text_in = (msg.get("text") or "").strip()

        # /start /help
        if text_in in ("/start", "/help"):
            tg_api("sendMessage", {
                "chat_id": chat_id,
                "parse_mode": "HTML",
                "text": (
                    "<b>Пятак — заявки</b>\n"
                    "Я присылаю заявки и меняю статусы по кнопкам.\n\n"
                    "<b>Команды</b>:\n"
                    "• /id — показать ваш chat_id\n"
                    "• /work &lt;ID&gt; — статус «В работе»\n"
                    "• /done &lt;ID&gt; — статус «Выполнено»"
                )
            })
            return "ok"

        # /id — узнать свой chat_id
        if text_in == "/id":
            tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"Ваш chat_id: <code>{chat_id}</code>",
                "parse_mode": "HTML"
            })
            return "ok"

        # /done <ID> — статус → Выполнено
        if text_in.startswith("/done"):
            parts = text_in.split()
            if len(parts) == 2 and parts[1].isdigit():
                sid = int(parts[1])
                t = db.session.get(Ticket, sid)
                if not t:
                    tg_api("sendMessage", {"chat_id": chat_id, "text": f"ID {sid} не найден"})
                else:
                    t.status = "done"
                    t.updated_at = now_utc()
                    db.session.commit()
                    edit_ticket_message_in_tg(t, "Заявка")
                    tg_api("sendMessage", {"chat_id": chat_id, "text": f"Заявка {sid}: статус → Выполнено ✅"})
            else:
                tg_api("sendMessage", {"chat_id": chat_id, "text": "Использование: /done <ID>"})
            return "ok"

        # /work <ID> — статус → В работе
        if text_in.startswith("/work"):
            parts = text_in.split()
            if len(parts) == 2 and parts[1].isdigit():
                sid = int(parts[1])
                t = db.session.get(Ticket, sid)
                if not t:
                    tg_api("sendMessage", {"chat_id": chat_id, "text": f"ID {sid} не найден"})
                else:
                    t.status = "in_progress"
                    t.updated_at = now_utc()
                    db.session.commit()
                    edit_ticket_message_in_tg(t, "Заявка")
                    tg_api("sendMessage", {"chat_id": chat_id, "text": f"Заявка {sid}: статус → В работе 🔄"})
            else:
                tg_api("sendMessage", {"chat_id": chat_id, "text": "Использование: /work <ID>"})
            return "ok"

        return "ok"

    # Нажатия на кнопки
    cq = upd.get("callback_query")
    if cq:
        cb_id = cq.get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        if not _chat_allowed(chat):
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Недоступно"})
            return "ok"

        data = cq.get("data") or ""
        chat_id = chat.get("id")
        message_id = message.get("message_id")

        if not data.startswith("status:"):
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Неизвестное действие"})
            return "ok"

        try:
            _, sid, new_status = data.split(":", 2)
            sid = int(sid)
        except Exception:
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Ошибка данных"})
            return "ok"

        if new_status not in ("in_progress", "done"):
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Неверный статус"})
            return "ok"

        t = db.session.get(Ticket, sid)
        if not t:
            tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Заявка не найдена"})
            return "ok"

        t.status = new_status
        t.updated_at = now_utc()
        db.session.commit()

        # всплывашка
        human = {"in_progress": "В работе", "done": "Выполнено"}[new_status]
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Статус: {human}"})

        # правим карточку — если у нас есть message_id от первого сообщения
        if not t.tg_chat_id:
            t.tg_chat_id = chat_id
        if not t.tg_message_id:
            t.tg_message_id = message_id
        edit_ticket_message_in_tg(t, "Заявка")

        return "ok"

    return "ok"


# -----------------------------------------------------------------------------
# Крон-напоминания (прогрев и дожим)
# -----------------------------------------------------------------------------
@app.get("/cron/remind")
def cron_remind():
    # простая защита
    if CRON_SECRET:
        recv = request.headers.get("X-Cron-Secret", "")
        if recv != CRON_SECRET:
            return "forbidden", 403

    # шлём напоминания по всем тикетам, которые не done
    # • если просрочены — каждые REMIND_EVERY_SEC
    # • если скоро дедлайн (< 3 часов) — напомнить разово
    now = now_utc()
    soon = now + timedelta(hours=3)

    rows = Ticket.query.filter(Ticket.status != "done").order_by(Ticket.created_at.asc()).all()
    sent = 0
    for t in rows:
        need = False
        title = "Напоминание"

        if t.deadline_at:
            if t.deadline_at < now:
                # просрочено — пингуем всегда
                need = True
                title = "Просрочено"
            elif t.deadline_at < soon:
                need = True
                title = "Скоро дедлайн"

        if need:
            # просто отправим новое сообщение (и постараемся обновить исходную карточку)
            edit_ticket_message_in_tg(t, "Заявка")  # привести карточку к актуальному виду
            send_ticket_to_tg(t)
            sent += 1

    return jsonify({"ok": True, "sent": sent})


# -----------------------------------------------------------------------------
# Запуск под gunicorn (на Render это не требуется явно)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
