"""SQLite document store for app state (users/accounts/models/dashboard)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_DB_PATH: Path | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_db_path(state_root: Path | None = None) -> Path:
    if state_root is None:
        state_root = Path(__file__).resolve().parent / "state"
    return state_root / "app.db"


def init_db(db_path: Path | None = None) -> Path:
    """Initialize global SQLite connection and schema."""
    global _CONN, _DB_PATH
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if _CONN is not None and _DB_PATH == path:
            return path
        if _CONN is not None:
            _CONN.close()
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                path TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        _CONN = conn
        _DB_PATH = path
        return path


def get_conn() -> sqlite3.Connection:
    if _CONN is None:
        init_db()
    assert _CONN is not None
    return _CONN


def _norm_path(path: Path | str) -> str:
    return str(Path(path).resolve())


def load_document(path: Path | str) -> dict[str, Any] | None:
    key = _norm_path(path)
    with _LOCK:
        conn = get_conn()
        row = conn.execute(
            "SELECT payload FROM documents WHERE path = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


def save_document(path: Path | str, data: dict[str, Any], *, kind: str = "") -> None:
    key = _norm_path(path)
    payload = json.dumps(data, ensure_ascii=False)
    stamp = now_iso()
    with _LOCK:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO documents(path, kind, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                kind=excluded.kind,
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (key, kind or _guess_kind(key), payload, stamp),
        )
        conn.commit()


def delete_document(path: Path | str) -> bool:
    key = _norm_path(path)
    with _LOCK:
        conn = get_conn()
        cur = conn.execute("DELETE FROM documents WHERE path = ?", (key,))
        conn.commit()
        return cur.rowcount > 0


def list_documents(kind: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        conn = get_conn()
        if kind:
            rows = conn.execute(
                "SELECT path, kind, updated_at FROM documents WHERE kind = ? ORDER BY updated_at DESC",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path, kind, updated_at FROM documents ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]


def _guess_kind(path_key: str) -> str:
    name = Path(path_key).name.lower()
    if name == "users.json":
        return "users"
    if name == "accounts.json":
        return "accounts"
    if name == "models.json":
        return "models"
    if name == "dashboard.json":
        return "dashboard"
    return "document"


def migrate_json_file(path: Path, *, kind: str = "", force: bool = False) -> bool:
    """Import a JSON file into SQLite if DB row missing (or force)."""
    path = Path(path)
    if not path.is_file():
        return False
    key = _norm_path(path)
    with _LOCK:
        conn = get_conn()
        exists = conn.execute("SELECT 1 FROM documents WHERE path = ?", (key,)).fetchone()
        if exists and not force:
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
    save_document(path, data, kind=kind or _guess_kind(key))
    return True


def migrate_state_tree(state_root: Path | None = None) -> dict[str, int]:
    """Migrate known state JSON files under state/ and tenants/."""
    root = Path(state_root) if state_root else default_db_path().parent
    init_db(root / "app.db")
    stats = {"users": 0, "accounts": 0, "models": 0, "dashboard": 0, "other": 0}
    candidates: list[tuple[Path, str]] = []
    for name, kind in [
        ("users.json", "users"),
        ("accounts.json", "accounts"),
        ("models.json", "models"),
        ("dashboard.json", "dashboard"),
    ]:
        candidates.append((root / name, kind))
    tenants = root / "tenants"
    if tenants.is_dir():
        for tenant in tenants.iterdir():
            if not tenant.is_dir():
                continue
            for name, kind in [
                ("accounts.json", "accounts"),
                ("models.json", "models"),
                ("dashboard.json", "dashboard"),
            ]:
                candidates.append((tenant / name, kind))
    for path, kind in candidates:
        if migrate_json_file(path, kind=kind):
            stats[kind if kind in stats else "other"] += 1
    with _LOCK:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO meta(key, value, updated_at)
            VALUES ('last_migration', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (json.dumps(stats, ensure_ascii=False), now_iso()),
        )
        conn.commit()
    return stats


def load_or_migrate(path: Path, *, kind: str = "") -> dict[str, Any] | None:
    """Prefer SQLite document; if missing, import from JSON file once."""
    init_db()
    data = load_document(path)
    if data is not None:
        return data
    if Path(path).is_file():
        migrate_json_file(path, kind=kind)
        return load_document(path)
    return None


def save_store(path: Path, data: dict[str, Any], *, kind: str = "", also_json: bool = True) -> None:
    """Persist to SQLite and optionally mirror to JSON for compatibility."""
    init_db()
    save_document(path, data, kind=kind)
    if also_json:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
