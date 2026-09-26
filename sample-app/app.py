import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg2
import psycopg2.extras
from flask import Flask, Response, abort, jsonify, request
from psycopg2.pool import ThreadedConnectionPool


# ---------- Configuration ----------

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in logging.getLevelNamesMapping():
    raise RuntimeError(f"invalid LOG_LEVEL: {LOG_LEVEL!r}")

PORT = int(os.environ.get("PORT", "8000"))
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
MAX_TITLE_LEN = 1024


# ---------- Logging ----------


class JsonFormatter(logging.Formatter):
    # Standard LogRecord attrs we don't surface as extra fields.
    _RESERVED = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # default=str so an `extra={"obj": some_datetime_or_decimal}` does not
        # raise inside the formatter and lose the log line.
        return json.dumps(payload, default=str)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
_root = logging.getLogger()
_root.handlers = [_handler]
_root.setLevel(LOG_LEVEL)
log = logging.getLogger("flask-todo")


# ---------- Database ----------

_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> ThreadedConnectionPool:
    """Lazy, thread-safe pool init. One pool per worker process."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(
                minconn=DB_POOL_MIN,
                maxconn=DB_POOL_MAX,
                dsn=DATABASE_URL,
            )
            log.info(
                "db pool initialized",
                extra={"pool_min": DB_POOL_MIN, "pool_max": DB_POOL_MAX},
            )
    return _pool


@contextmanager
def db_conn() -> Iterator[Any]:
    """Acquire a Postgres connection. Commits on success, rolls back on error.

    Returns the connection to the pool. If the connection is poisoned (closed
    by the server, or in a state we cannot rollback), discards it via
    `putconn(close=True)` so the next borrower does not inherit it.
    """
    pool = get_pool()
    try:
        conn = pool.getconn()
    except Exception:
        log.warning("db pool getconn failed", exc_info=True)
        raise
    poisoned = False
    try:
        yield conn
        conn.commit()
    except (psycopg2.InterfaceError, psycopg2.OperationalError):
        # Connection-level fault. Cannot rollback safely; discard.
        poisoned = True
        log.warning("db connection poisoned; discarding", exc_info=True)
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            poisoned = True
            log.warning("rollback failed; discarding connection", exc_info=True)
        raise
    finally:
        # close=True tells the pool to drop this connection instead of
        # returning it for reuse. Required when the connection is poisoned.
        pool.putconn(conn, close=poisoned or getattr(conn, "closed", 0) != 0)


def init_schema() -> None:
    """Idempotent schema setup. All DDL must be `IF NOT EXISTS`-style so reruns
    are safe; a future addition that adds a non-idempotent statement must wrap
    the whole block in an explicit transaction or risk leaving partial state
    on failure. `db_conn` already commits on success / rolls back on error,
    so the entire DDL block runs as one transaction by default.
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    done BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
    log.info("schema ready")


_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_schema_ready() -> bool:
    """Lazy, thread-safe schema init. Returns True if schema is usable."""
    global _schema_ready
    if _schema_ready:
        return True
    with _schema_lock:
        if _schema_ready:
            return True
        try:
            init_schema()
            _schema_ready = True
            return True
        except Exception:
            log.exception("schema init failed")
            return False


# ---------- App ----------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KiB request bodies


@app.before_request
def _maybe_init_schema() -> None:
    """Real request paths require the schema to be ready before they run.
    Probes (`/healthz`, `/readyz`) handle their own state checks; `/readyz`
    invokes `_ensure_schema_ready()` directly so the readiness signal does
    not lie when the DDL fails.
    """
    if request.path in ("/healthz", "/readyz"):
        return
    if not _ensure_schema_ready():
        abort(503, description="schema not ready")


@app.get("/healthz")
def healthz() -> tuple[Response, int]:
    """Liveness probe. Process is up. Does not touch the DB."""
    return jsonify({"status": "ok"}), 200


@app.get("/readyz")
def readyz() -> tuple[Response, int]:
    """Readiness probe. 200 only when the DB is reachable AND the schema is
    initialized. The DB check is a cheap `SELECT 1`; the schema check is the
    same idempotent init the request path uses, so a successful `/readyz`
    proves the next real request will not 503 on a missing schema.
    """
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception:
        log.warning("readiness check failed: db unreachable", exc_info=True)
        return jsonify({"status": "not ready", "reason": "db unreachable"}), 503

    if not _ensure_schema_ready():
        log.warning("readiness check failed: schema not ready")
        return jsonify({"status": "not ready", "reason": "schema not ready"}), 503

    return jsonify({"status": "ready"}), 200


@app.get("/todos")
def list_todos() -> tuple[Response, int]:
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, title, done, created_at FROM todos ORDER BY id DESC")
            rows = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "done": r["done"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in cur.fetchall()
            ]
    return jsonify(rows), 200


@app.post("/todos")
def create_todo() -> tuple[Response, int]:
    # Distinguish "no JSON / malformed JSON" from "JSON parsed but missing
    # title" so the error message is honest. silent=True swallows parse
    # errors and reports them as "title required", which sends users on a
    # wild goose chase.
    if not request.is_json:
        abort(
            400,
            description="request body must be JSON (Content-Type: application/json)",
        )
    body: dict[str, Any] = {}
    try:
        parsed = request.get_json(silent=False)
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        abort(400, description="request body is not valid JSON")
    title = body.get("title")
    if not isinstance(title, str) or not title:
        abort(400, description="title (string) is required")
    elif len(title) > MAX_TITLE_LEN:
        abort(400, description=f"title must be <= {MAX_TITLE_LEN} characters")
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO todos (title) VALUES (%s) RETURNING id, title, done, created_at",
                (title,),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT RETURNING returned no row")
    result = {
        "id": row["id"],
        "title": row["title"],
        "done": row["done"],
        "created_at": row["created_at"].isoformat(),
    }
    log.info("todo created", extra={"todo_id": result["id"]})
    return jsonify(result), 201


@app.errorhandler(Exception)
def _unhandled_error(exc: Exception) -> tuple[Response, int]:
    """Surface unhandled exceptions as a structured JSON 500 with the
    request_id (when one is present) so the on-call can grep CloudWatch by
    correlation ID. Without this handler, Flask returns the default HTML
    page and the failure is logged by werkzeug without route or correlation.
    HTTPExceptions (4xx) keep their original status; only true 500s land here.
    """
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        # Let Flask's default handler render 4xx responses with the right code.
        return exc.get_response(), exc.code or 500

    # Pull request_id off Flask's `g` if a middleware set it; the M8 lab
    # patch wires this up. Falls back to "" before the patch is applied.
    from flask import g

    rid = g.get("request_id", "") if g else ""
    log.exception(
        "unhandled view error",
        extra={
            "path": request.path,
            "method": request.method,
            "request_id": rid,
        },
    )
    return jsonify({"error": "internal server error", "request_id": rid}), 500


if __name__ == "__main__":
    # Bind all interfaces so the dev server is reachable from the container host.
    # Acceptable for local development only.
    app.run(host="0.0.0.0", port=PORT)  # noqa: S104
