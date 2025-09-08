import os
from datetime import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func

# -----------------------------------------------------------------------------
# App & Config
# -----------------------------------------------------------------------------
def normalize_database_url(url: str) -> str:
    """Render/Heroku выдают postgres://; SQLAlchemy+psycopg3 ждёт postgresql+psycopg://"""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url

app = Flask(__name__)
CORS(app)

db_url_env = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("SQLALCHEMY_DATABASE_URI")
)

if db_url_env:
    db_url = normalize_database_url(db_url_env)
else:
    # локально можно юзать SQLite
    db_url = "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

db = SQLAlchemy(app)

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class Ticket(db.Model):
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    club = db.Column(db.String(255), nullable=False)
    pc = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(2048), nullable=False)

    # бизнес-поля
    status = db.Column(db.String(64), nullable=False, default="new", index=True)
    deadline = db.Column(db.DateTime, nullable=True)  # может быть NULL

    # служебные поля
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=True,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        index=True,
    )

    # интеграция с Telegram
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
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "tg_chat_id": self.tg_chat_id,
            "tg_message_id": self.tg_message_id,
        }

# -----------------------------------------------------------------------------
# Soft-migrations (idempotent)
# -----------------------------------------------------------------------------
def soft_migrate(engine, logger):
    stmts = [
        # Базовая таблица, если вдруг её нет (idempotent)
        """
        CREATE TABLE IF NOT EXISTS tickets (
          id SERIAL PRIMARY KEY,
          club VARCHAR(255) NOT NULL,
          pc VARCHAR(255) NOT NULL,
          description VARCHAR(2048) NOT NULL,
          status VARCHAR(64) NOT NULL DEFAULT 'new',
          deadline TIMESTAMP NULL,
          created_at TIMESTAMP NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMP NULL DEFAULT NOW(),
          tg_chat_id BIGINT NULL,
          tg_message_id BIGINT NULL
        );
        """,
        # Дальше — безопасные ALTER'ы
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tg_chat_id BIGINT;",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS tg_message_id BIGINT;",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS deadline TIMESTAMP;",
        "ALTER TABLE tickets ALTER COLUMN deadline DROP NOT NULL;",
        "ALTER TABLE tickets ALTER COLUMN status SET DEFAULT 'new';",
        "ALTER TABLE tickets ALTER COLUMN created_at SET DEFAULT NOW();",
        "ALTER TABLE tickets ALTER COLUMN updated_at SET DEFAULT NOW();",
    ]
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.exec_driver_sql(s)  # ВАЖНО: передаём строку, не TextClause
            except Exception as e:
                first_line = s.strip().splitlines()[0]
                logger.warning(f"Skip stmt `{first_line}`: {e}")

with app.app_context():
    db.create_all()  # создаёт таблицу, если её нет (для SQLite/пустой PG)
    soft_migrate(db.engine, app.logger)

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET", "HEAD"])
def root():
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/api/tickets", methods=["GET"])
def list_tickets():
    status = request.args.get("status")
    limit = request.args.get("limit", type=int) or 100

    q = Ticket.query
    if status:
        q = q.filter(Ticket.status == status)

    items = q.order_by(Ticket.created_at.desc()).limit(limit).all()
    return jsonify([t.to_dict() for t in items])

def _parse_deadline(raw):
    if not raw:
        return None
    # пробуем ISO 8601
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None

@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    data = request.get_json(silent=True) or {}

    club = (data.get("club") or "").strip()
    pc = (data.get("pc") or "").strip()
    description = (data.get("description") or "").strip()
    status = (data.get("status") or "new").strip() or "new"
    deadline = _parse_deadline(data.get("deadline"))

    if not club or not pc or not description:
        return jsonify({"error": "bad_request", "message": "club, pc, description — обязательны"}), 400

    t = Ticket(
        club=club,
        pc=pc,
        description=description,
        status=status,
        deadline=deadline,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        tg_chat_id=data.get("tg_chat_id"),
        tg_message_id=data.get("tg_message_id"),
    )

    try:
        db.session.add(t)
        db.session.commit()
        return jsonify(t.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Create ticket failed")
        return jsonify({"error": "server_error"}), 500

@app.route("/api/tickets/<int:ticket_id>", methods=["PATCH"])
def update_ticket(ticket_id: int):
    t = Ticket.query.get_or_404(ticket_id)
    data = request.get_json(silent=True) or {}

    # Разрешаем менять ограниченный набор полей
    for field in ("club", "pc", "description", "status"):
        if field in data and isinstance(data[field], str):
            setattr(t, field, data[field].strip())

    if "deadline" in data:
        t.deadline = _parse_deadline(data.get("deadline"))

    # Поля из ТГ
    if "tg_chat_id" in data:
        t.tg_chat_id = data.get("tg_chat_id")
    if "tg_message_id" in data:
        t.tg_message_id = data.get("tg_message_id")

    t.updated_at = datetime.utcnow()
    try:
        db.session.commit()
        return jsonify(t.to_dict())
    except Exception:
        db.session.rollback()
        app.logger.exception("Update ticket failed")
        return jsonify({"error": "server_error"}), 500

# Возвращаем аккуратный JSON на неожиданные 500, но не трогаем осознанные HTTP ошибки
from werkzeug.exceptions import HTTPException
@app.errorhandler(Exception)
def handle_unexpected(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled error")
    return jsonify({"error": "server_error"}), 500

# -----------------------------------------------------------------------------
# Dev run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # локальный запуск (Render всё равно стартует через gunicorn)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
