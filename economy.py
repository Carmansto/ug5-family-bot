import asyncio
import sqlite3
import time
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import discord

from config import DB_PATH, LOCAL_TZ, GUILD_ID, ECONOMY_CHANNEL_ID, ECONOMY_USER_ID


ECONOMY_VERSION = "3.6"
_DB_DIR = Path(DB_PATH).expanduser().resolve().parent
ECONOMY_DB_PATH = str(_DB_DIR / "economy.db")
PANEL_REFRESH_SECONDS = 120
_panel_message_id = None
_panel_refresh_task = None
UNDO_SECONDS = 60
_undo_actions = {}
_session_locks = {}
_recent_actions = {}


def session_lock(user_id: int, session_id: int) -> asyncio.Lock:
    key = (int(user_id), int(session_id))
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock


def allow_action(user_id: int, key: str, window: float = 1.5) -> bool:
    """Reject the same fast repeated action (double click / double submit)."""
    now = time.monotonic()
    action_key = (int(user_id), key)
    previous = _recent_actions.get(action_key, 0.0)
    if now - previous < window:
        return False
    _recent_actions[action_key] = now

    # Keep this tiny in-memory cache bounded during long bot uptimes.
    if len(_recent_actions) > 500:
        cutoff = now - 30.0
        for old_key, stamp in list(_recent_actions.items()):
            if stamp < cutoff:
                _recent_actions.pop(old_key, None)
    return True


def now_dt() -> datetime:
    return datetime.now(LOCAL_TZ).replace(microsecond=0)


def now_iso() -> str:
    return now_dt().isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def duration_text(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours} год {minutes:02d} хв"
    return f"{minutes} хв"


def parse_amount(value: str) -> int:
    cleaned = (
        value.replace(" ", "")
        .replace(",", "")
        .replace("_", "")
        .replace("$", "")
    )
    amount = int(cleaned)
    if amount <= 0:
        raise ValueError("amount")
    return amount


def period_start(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    if days == 1:
        dt = now_dt().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        dt = now_dt() - timedelta(days=days)
    return dt.isoformat()


def progress_bar(current: int, target: int, blocks: int = 14) -> str:
    if target <= 0:
        return "░" * blocks
    ratio = max(0.0, min(1.0, current / target))
    filled = round(ratio * blocks)
    return "█" * filled + "░" * (blocks - filled)


def _row_dict(row):
    return dict(row) if row is not None else None


def store_undo(user_id: int, action_type: str, payload: dict):
    _undo_actions[user_id] = {
        "type": action_type,
        "payload": payload,
        "expires": time.monotonic() + UNDO_SECONDS,
    }


def get_undo(user_id: int):
    action = _undo_actions.get(user_id)
    if not action:
        return None
    if time.monotonic() > action["expires"]:
        _undo_actions.pop(user_id, None)
        return None
    return action


def clear_undo(user_id: int):
    _undo_actions.pop(user_id, None)


class EconomyDB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _column_exists(self, table: str, column: str) -> bool:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def _init(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]

        # Legacy -> Economy v2 was intentionally a clean reset.
        if version < 2:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript(
                """
                DROP TABLE IF EXISTS goals;
                DROP TABLE IF EXISTS work_segments;
                DROP TABLE IF EXISTS transactions;
                DROP TABLE IF EXISTS work_sessions;
                DROP TABLE IF EXISTS farms;
                DROP TABLE IF EXISTS jobs;
                """
            )
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys=ON")

        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                income_mode TEXT NOT NULL DEFAULT 'later'
                    CHECK(income_mode IN ('instant', 'later')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS work_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('working', 'paused', 'pending_sale', 'completed')),
                worked_seconds INTEGER NOT NULL DEFAULT 0,
                current_started_at TEXT,
                work_finished_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                note TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('income', 'expense')),
                amount INTEGER NOT NULL CHECK(amount > 0),
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES work_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS work_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                seconds INTEGER NOT NULL CHECK(seconds >= 0),
                source TEXT NOT NULL DEFAULT 'timer'
                    CHECK(source IN ('timer', 'manual', 'legacy')),
                FOREIGN KEY(session_id) REFERENCES work_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                goal_type TEXT NOT NULL CHECK(goal_type IN ('income', 'profit')),
                target_amount INTEGER NOT NULL CHECK(target_amount > 0),
                job_id INTEGER,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            );

            CREATE TABLE IF NOT EXISTS economy_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_user
                ON jobs(user_id, active);
            CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON work_sessions(user_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_job
                ON work_sessions(job_id, status, completed_at);
            CREATE INDEX IF NOT EXISTS idx_tx_session
                ON transactions(session_id, kind);
            CREATE INDEX IF NOT EXISTS idx_segments_session
                ON work_segments(session_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_goals_user
                ON goals(user_id, completed_at);
            """
        )

        # v2 -> v3 migration that preserves all existing Economy data.
        if not self._column_exists("work_sessions", "note"):
            self.conn.execute("ALTER TABLE work_sessions ADD COLUMN note TEXT")

        # v3 -> v4: keep every future work approach as a separate segment.
        # Existing sessions are preserved as one legacy segment so no time is lost.
        if version < 4:
            old_rows = self.conn.execute(
                """
                SELECT s.*
                FROM work_sessions s
                WHERE s.worked_seconds > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM work_segments ws WHERE ws.session_id=s.id
                  )
                """
            ).fetchall()
            for row in old_rows:
                started = row["created_at"]
                ended = row["work_finished_at"] or row["completed_at"] or row["created_at"]
                self.conn.execute(
                    """
                    INSERT INTO work_segments(
                        session_id,user_id,started_at,ended_at,seconds,source
                    ) VALUES(?,?,?,?,?,'legacy')
                    """,
                    (row["id"], row["user_id"], started, ended, int(row["worked_seconds"] or 0)),
                )

        self.conn.execute("PRAGMA user_version=4")
        self.conn.commit()

    # ---------- Jobs ----------
    def jobs(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE user_id=? AND active=1 ORDER BY name COLLATE NOCASE",
            (user_id,),
        ).fetchall()

    def all_jobs(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY active DESC, name COLLATE NOCASE",
            (user_id,),
        ).fetchall()

    def job(self, user_id: int, job_id: int):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE user_id=? AND id=?", (user_id, job_id)
        ).fetchone()

    def add_job(self, user_id: int, name: str, income_mode: str):
        name = name.strip()
        if not name:
            raise ValueError("name")
        self.conn.execute(
            "INSERT INTO jobs(user_id,name,income_mode,created_at) VALUES(?,?,?,?)",
            (user_id, name, income_mode, now_iso()),
        )
        self.conn.commit()

    def annul_job_keep_history(self, user_id: int, job_id: int):
        self.conn.execute(
            "UPDATE jobs SET active=0 WHERE user_id=? AND id=?", (user_id, job_id)
        )
        self.conn.commit()

    def annul_job_with_history(self, user_id: int, job_id: int):
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "DELETE FROM goals WHERE user_id=? AND job_id=?", (user_id, job_id)
            )
            session_ids = [
                row["id"]
                for row in self.conn.execute(
                    "SELECT id FROM work_sessions WHERE user_id=? AND job_id=?",
                    (user_id, job_id),
                ).fetchall()
            ]
            if session_ids:
                marks = ",".join("?" for _ in session_ids)
                self.conn.execute(
                    f"DELETE FROM transactions WHERE session_id IN ({marks})", session_ids
                )
                self.conn.execute(
                    f"DELETE FROM work_sessions WHERE id IN ({marks})", session_ids
                )
            self.conn.execute(
                "DELETE FROM jobs WHERE user_id=? AND id=?", (user_id, job_id)
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self.refresh_goals(user_id)

    # ---------- Sessions ----------
    def open_session(self, user_id: int):
        return self.conn.execute(
            """
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND s.status IN ('working','paused')
            ORDER BY s.id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()

    def session(self, session_id: int, user_id: int):
        return self.conn.execute(
            """
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            WHERE s.id=? AND s.user_id=?
            """,
            (session_id, user_id),
        ).fetchone()

    def start_session(self, user_id: int, job_id: int) -> int:
        if self.open_session(user_id):
            raise RuntimeError("open_session")
        stamp = now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO work_sessions(
                user_id,job_id,status,worked_seconds,current_started_at,created_at
            ) VALUES(?,?, 'working',0,?,?)
            """,
            (user_id, job_id, stamp, stamp),
        )
        self.conn.commit()
        return cur.lastrowid

    def create_manual_session(
        self, user_id: int, job_id: int, worked_seconds: int, status: str
    ) -> int:
        if worked_seconds <= 0 or status not in ("pending_sale", "completed"):
            raise ValueError("manual_session")
        stamp = now_iso()
        completed_at = stamp if status == "completed" else None
        cur = self.conn.execute(
            """
            INSERT INTO work_sessions(
                user_id,job_id,status,worked_seconds,current_started_at,
                work_finished_at,completed_at,created_at
            ) VALUES(?,?,?,?,NULL,?,?,?)
            """,
            (
                user_id,
                job_id,
                status,
                int(worked_seconds),
                stamp,
                completed_at,
                stamp,
            ),
        )
        session_id = cur.lastrowid
        self.conn.execute(
            """
            INSERT INTO work_segments(
                session_id,user_id,started_at,ended_at,seconds,source
            ) VALUES(?,?,?,?,?,'manual')
            """,
            (session_id, user_id, stamp, stamp, int(worked_seconds)),
        )
        self.conn.commit()
        return session_id

    def worked_seconds(self, row) -> int:
        total = int(row["worked_seconds"] or 0)
        if row["status"] == "working" and row["current_started_at"]:
            total += max(
                0,
                int(
                    (now_dt() - parse_dt(row["current_started_at"])).total_seconds()
                ),
            )
        return total

    def work_segments(self, session_id: int):
        return self.conn.execute(
            """
            SELECT * FROM work_segments
            WHERE session_id=?
            ORDER BY started_at ASC, id ASC
            """,
            (session_id,),
        ).fetchall()

    def _insert_segment(
        self, session_id: int, user_id: int, started_at: str, ended_at: str,
        seconds: int, source: str = "timer"
    ):
        seconds = max(0, int(seconds))
        if seconds <= 0:
            return
        self.conn.execute(
            """
            INSERT INTO work_segments(
                session_id,user_id,started_at,ended_at,seconds,source
            ) VALUES(?,?,?,?,?,?)
            """,
            (session_id, user_id, started_at, ended_at, seconds, source),
        )

    def add_worked_time(self, session_id: int, user_id: int, seconds: int) -> bool:
        seconds = int(seconds)
        if seconds <= 0:
            return False
        row = self.session(session_id, user_id)
        if not row:
            return False
        stamp = now_iso()
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "UPDATE work_sessions SET worked_seconds=worked_seconds+? WHERE id=? AND user_id=?",
                (seconds, session_id, user_id),
            )
            self._insert_segment(session_id, user_id, stamp, stamp, seconds, "manual")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def get_meta(self, key: str):
        row = self.conn.execute(
            "SELECT value FROM economy_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: Optional[str]):
        if value is None:
            self.conn.execute("DELETE FROM economy_meta WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO economy_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
        self.conn.commit()

    def pause(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "working":
            return False
        ended = now_dt()
        started = parse_dt(row["current_started_at"])
        delta = max(0, int((ended - started).total_seconds()))
        total = int(row["worked_seconds"] or 0) + delta
        try:
            self.conn.execute("BEGIN")
            self._insert_segment(
                session_id, user_id, row["current_started_at"], ended.isoformat(), delta, "timer"
            )
            self.conn.execute(
                """
                UPDATE work_sessions
                SET status='paused', worked_seconds=?, current_started_at=NULL
                WHERE id=? AND user_id=?
                """,
                (total, session_id, user_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def resume(self, session_id: int, user_id: int) -> bool:
        row = self.session(session_id, user_id)
        if not row or row["status"] not in ("paused", "pending_sale"):
            return False
        current = self.open_session(user_id)
        if current and current["id"] != session_id:
            return False
        self.conn.execute(
            """
            UPDATE work_sessions
            SET status='working', current_started_at=?, work_finished_at=NULL
            WHERE id=? AND user_id=?
            """,
            (now_iso(), session_id, user_id),
        )
        self.conn.commit()
        return True

    def finish_work(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] not in ("working", "paused"):
            return False
        ended = now_dt()
        total = int(row["worked_seconds"] or 0)
        delta = 0
        try:
            self.conn.execute("BEGIN")
            if row["status"] == "working" and row["current_started_at"]:
                started = parse_dt(row["current_started_at"])
                delta = max(0, int((ended - started).total_seconds()))
                total += delta
                self._insert_segment(
                    session_id, user_id, row["current_started_at"], ended.isoformat(), delta, "timer"
                )
            self.conn.execute(
                """
                UPDATE work_sessions
                SET status='pending_sale', worked_seconds=?, current_started_at=NULL,
                    work_finished_at=?
                WHERE id=? AND user_id=?
                """,
                (total, ended.isoformat(), session_id, user_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def complete(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "pending_sale":
            return
        self.conn.execute(
            """
            UPDATE work_sessions
            SET status='completed', completed_at=?
            WHERE id=? AND user_id=?
            """,
            (now_iso(), session_id, user_id),
        )
        self.conn.commit()
        self.refresh_goals(user_id)

    def update_note(self, session_id: int, user_id: int, note: Optional[str]):
        self.conn.execute(
            "UPDATE work_sessions SET note=? WHERE id=? AND user_id=?",
            ((note or "").strip() or None, session_id, user_id),
        )
        self.conn.commit()

    def delete_session(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row:
            return
        try:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM transactions WHERE session_id=?", (session_id,))
            self.conn.execute(
                "DELETE FROM work_sessions WHERE id=? AND user_id=?",
                (session_id, user_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self.refresh_goals(user_id)

    def reopen_completed(self, session_id: int, user_id: int) -> bool:
        row = self.session(session_id, user_id)
        if not row or row["status"] != "completed":
            return False
        if self.open_session(user_id):
            # Reopening only changes it to pending_sale, so this is safe even if another
            # job is active. It does not start the timer.
            pass
        self.conn.execute(
            """
            UPDATE work_sessions
            SET status='pending_sale', completed_at=NULL,
                work_finished_at=COALESCE(work_finished_at, ?)
            WHERE id=? AND user_id=?
            """,
            (now_iso(), session_id, user_id),
        )
        self.conn.commit()
        self.refresh_goals(user_id)
        return True

    def latest_job(self, user_id: int):
        return self.conn.execute(
            """
            SELECT j.*, s.id AS session_id, s.status AS session_status
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND j.user_id=? AND j.active=1
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()

    def restore_transaction(self, tx: dict):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO transactions(
                id,session_id,user_id,kind,amount,note,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                tx["id"], tx["session_id"], tx["user_id"], tx["kind"],
                tx["amount"], tx.get("note"), tx["created_at"],
            ),
        )
        self.conn.commit()
        self.refresh_goals(tx["user_id"])

    def restore_session(
        self, session: dict, transactions: list[dict], segments: Optional[list[dict]] = None
    ):
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO work_sessions(
                    id,user_id,job_id,status,worked_seconds,current_started_at,
                    work_finished_at,completed_at,created_at,note
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session["id"], session["user_id"], session["job_id"], session["status"],
                    session["worked_seconds"], session.get("current_started_at"),
                    session.get("work_finished_at"), session.get("completed_at"),
                    session["created_at"], session.get("note"),
                ),
            )
            for tx in transactions:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO transactions(
                        id,session_id,user_id,kind,amount,note,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        tx["id"], tx["session_id"], tx["user_id"], tx["kind"],
                        tx["amount"], tx.get("note"), tx["created_at"],
                    ),
                )
            for seg in segments or []:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO work_segments(
                        id,session_id,user_id,started_at,ended_at,seconds,source
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        seg["id"], seg["session_id"], seg["user_id"],
                        seg["started_at"], seg["ended_at"], seg["seconds"], seg["source"],
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self.refresh_goals(session["user_id"])

    def _history_where(
        self, user_id: int, status: Optional[str] = None,
        job_id: Optional[int] = None, date_filter: Optional[str] = None
    ):
        where = ["s.user_id=?"]
        params = [user_id]
        if status:
            where.append("s.status=?")
            params.append(status)
        if job_id is not None:
            where.append("s.job_id=?")
            params.append(job_id)
        if date_filter:
            where.append(
                "(substr(s.created_at,1,10)=? OR EXISTS ("
                "SELECT 1 FROM work_segments ws "
                "WHERE ws.session_id=s.id AND substr(ws.started_at,1,10)=?"
                "))"
            )
            params.extend([date_filter, date_filter])
        return " AND ".join(where), params

    def history_count(
        self, user_id: int, status: Optional[str] = None,
        job_id: Optional[int] = None, date_filter: Optional[str] = None
    ) -> int:
        where, params = self._history_where(user_id, status, job_id, date_filter)
        return int(
            self.conn.execute(
                f"SELECT COUNT(*) c FROM work_sessions s WHERE {where}", params
            ).fetchone()["c"]
        )

    def history_page(
        self, user_id: int, page: int, page_size: int = 20,
        status: Optional[str] = None, job_id: Optional[int] = None,
        date_filter: Optional[str] = None
    ):
        offset = max(0, page) * page_size
        where, params = self._history_where(user_id, status, job_id, date_filter)
        params = list(params) + [page_size, offset]
        return self.conn.execute(
            f"""
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            WHERE {where}
            ORDER BY s.id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()

    def pending(self, user_id: int):
        return self.conn.execute(
            """
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND s.status='pending_sale'
            ORDER BY COALESCE(s.work_finished_at,s.created_at) DESC
            """,
            (user_id,),
        ).fetchall()

    # ---------- Transactions ----------
    def transactions(self, session_id: int):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()

    def transaction(self, transaction_id: int, user_id: int):
        return self.conn.execute(
            """
            SELECT t.*, s.status AS session_status, s.id AS session_id
            FROM transactions t
            JOIN work_sessions s ON s.id=t.session_id
            WHERE t.id=? AND t.user_id=? AND s.user_id=?
            """,
            (transaction_id, user_id, user_id),
        ).fetchone()

    def add_transaction(
        self,
        session_id: int,
        user_id: int,
        kind: str,
        amount: int,
        note: Optional[str] = None,
    ):
        self.conn.execute(
            """
            INSERT INTO transactions(session_id,user_id,kind,amount,note,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (session_id, user_id, kind, amount, (note or "").strip() or None, now_iso()),
        )
        self.conn.commit()
        row = self.session(session_id, user_id)
        if row and row["status"] == "completed":
            self.refresh_goals(user_id)

    def update_transaction(
        self,
        transaction_id: int,
        user_id: int,
        amount: int,
        note: Optional[str],
    ):
        self.conn.execute(
            "UPDATE transactions SET amount=?, note=? WHERE id=? AND user_id=?",
            (amount, (note or "").strip() or None, transaction_id, user_id),
        )
        self.conn.commit()
        self.refresh_goals(user_id)

    def delete_transaction(self, transaction_id: int, user_id: int):
        self.conn.execute(
            "DELETE FROM transactions WHERE id=? AND user_id=?",
            (transaction_id, user_id),
        )
        self.conn.commit()
        self.refresh_goals(user_id)

    def totals(self, session_id: int):
        row = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income,
                COALESCE(SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END),0) expense
            FROM transactions WHERE session_id=?
            """,
            (session_id,),
        ).fetchone()
        return int(row["income"]), int(row["expense"])

    def pending_totals(self, user_id: int):
        row = self.conn.execute(
            """
            SELECT
                COUNT(DISTINCT s.id) AS sessions,
                COALESCE(SUM(CASE WHEN t.kind='income' THEN t.amount ELSE 0 END),0) income,
                COALESCE(SUM(CASE WHEN t.kind='expense' THEN t.amount ELSE 0 END),0) expense
            FROM work_sessions s
            LEFT JOIN transactions t ON t.session_id=s.id
            WHERE s.user_id=? AND s.status='pending_sale'
            """,
            (user_id,),
        ).fetchone()
        return {
            "sessions": int(row["sessions"]),
            "income": int(row["income"]),
            "expense": int(row["expense"]),
        }

    # ---------- Statistics ----------
    def stats(self, user_id: int, days: Optional[int]):
        start = period_start(days)

        work_params = [user_id]
        work_filter = ""
        if start:
            work_filter = " AND COALESCE(s.work_finished_at,s.created_at)>=?"
            work_params.append(start)

        worked = self.conn.execute(
            f"""
            SELECT COALESCE(SUM(s.worked_seconds),0) sec
            FROM work_sessions s
            WHERE s.user_id=? AND s.status IN ('pending_sale','completed')
            {work_filter}
            """,
            work_params,
        ).fetchone()

        completed_params = [user_id]
        completed_filter = ""
        if start:
            completed_filter = " AND s.completed_at>=?"
            completed_params.append(start)

        sessions = self.conn.execute(
            f"""
            SELECT s.id, s.worked_seconds
            FROM work_sessions s
            WHERE s.user_id=? AND s.status='completed' {completed_filter}
            """,
            completed_params,
        ).fetchall()
        session_ids = [row["id"] for row in sessions]
        completed_seconds = sum(int(row["worked_seconds"] or 0) for row in sessions)
        income = expense = 0
        if session_ids:
            marks = ",".join("?" for _ in session_ids)
            totals = self.conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income,
                    COALESCE(SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END),0) expense
                FROM transactions WHERE session_id IN ({marks})
                """,
                session_ids,
            ).fetchone()
            income = int(totals["income"])
            expense = int(totals["expense"])

        hourly_values = []
        for session_row in sessions:
            sec = int(session_row["worked_seconds"] or 0)
            if sec <= 0:
                continue
            inc, exp = self.totals(session_row["id"])
            hourly_values.append(round((inc - exp) / (sec / 3600)))
        median_hourly = (
            round(statistics.median(hourly_values)) if hourly_values else 0
        )

        pending = self.pending_totals(user_id)
        return {
            "worked_seconds": int(worked["sec"]),
            "completed_seconds": completed_seconds,
            "completed_sessions": len(sessions),
            "income": income,
            "expense": expense,
            "median_hourly": median_hourly,
            "pending_count": pending["sessions"],
            "pending_income": pending["income"],
            "pending_expense": pending["expense"],
        }

    def job_stats(self, user_id: int, days: Optional[int]):
        start = period_start(days)
        params = [user_id]
        extra = ""
        if start:
            extra = " AND s.completed_at>=?"
            params.append(start)

        sessions = self.conn.execute(
            f"""
            SELECT s.id, s.job_id, s.worked_seconds, j.name AS job_name
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND s.status='completed' {extra}
            ORDER BY s.id DESC
            """,
            params,
        ).fetchall()

        by_job = {}
        for row in sessions:
            job = by_job.setdefault(
                row["job_id"],
                {
                    "job_id": row["job_id"],
                    "job_name": row["job_name"],
                    "sessions": 0,
                    "seconds": 0,
                    "income": 0,
                    "expense": 0,
                    "best_profit": None,
                    "best_hourly": None,
                    "best_session_id": None,
                    "hourly_values": [],
                },
            )
            inc, exp = self.totals(row["id"])
            profit = inc - exp
            sec = int(row["worked_seconds"] or 0)
            hourly = round(profit / (sec / 3600)) if sec else 0
            job["sessions"] += 1
            job["seconds"] += sec
            job["income"] += inc
            job["expense"] += exp
            if sec > 0:
                job["hourly_values"].append(hourly)
            if job["best_profit"] is None or profit > job["best_profit"]:
                job["best_profit"] = profit
                job["best_hourly"] = hourly
                job["best_session_id"] = row["id"]

        result = []
        for job in by_job.values():
            profit = job["income"] - job["expense"]
            job["profit"] = profit
            job["hourly"] = round(profit / (job["seconds"] / 3600)) if job["seconds"] else 0
            job["median_hourly"] = (
                round(statistics.median(job["hourly_values"]))
                if job["hourly_values"] else 0
            )
            result.append(job)
        return sorted(result, key=lambda x: x["hourly"], reverse=True)

    # ---------- Goals ----------
    def add_goal(
        self,
        user_id: int,
        goal_type: str,
        target_amount: int,
        job_id: Optional[int],
    ):
        if goal_type not in ("income", "profit") or target_amount <= 0:
            raise ValueError("goal")
        self.conn.execute(
            """
            INSERT INTO goals(user_id,goal_type,target_amount,job_id,created_at)
            VALUES(?,?,?,?,?)
            """,
            (user_id, goal_type, target_amount, job_id, now_iso()),
        )
        self.conn.commit()
        self.refresh_goals(user_id)

    def goals(self, user_id: int, completed: Optional[bool] = None):
        where = "WHERE g.user_id=?"
        params = [user_id]
        if completed is True:
            where += " AND g.completed_at IS NOT NULL"
        elif completed is False:
            where += " AND g.completed_at IS NULL"
        return self.conn.execute(
            f"""
            SELECT g.*, j.name AS job_name
            FROM goals g LEFT JOIN jobs j ON j.id=g.job_id
            {where}
            ORDER BY COALESCE(g.completed_at,g.created_at) DESC, g.id DESC
            """,
            params,
        ).fetchall()

    def goal(self, user_id: int, goal_id: int):
        return self.conn.execute(
            """
            SELECT g.*, j.name AS job_name
            FROM goals g LEFT JOIN jobs j ON j.id=g.job_id
            WHERE g.user_id=? AND g.id=?
            """,
            (user_id, goal_id),
        ).fetchone()

    def goal_progress(self, goal_row) -> int:
        params = [goal_row["user_id"], goal_row["created_at"]]
        job_filter = ""
        if goal_row["job_id"] is not None:
            job_filter = " AND s.job_id=?"
            params.append(goal_row["job_id"])

        sessions = self.conn.execute(
            f"""
            SELECT s.id
            FROM work_sessions s
            WHERE s.user_id=? AND s.status='completed'
              AND s.completed_at>=? {job_filter}
            """,
            params,
        ).fetchall()
        ids = [r["id"] for r in sessions]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        row = self.conn.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income,
                COALESCE(SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END),0) expense
            FROM transactions WHERE session_id IN ({marks})
            """,
            ids,
        ).fetchone()
        income = int(row["income"])
        expense = int(row["expense"])
        return income if goal_row["goal_type"] == "income" else income - expense

    def refresh_goals(self, user_id: int):
        rows = self.goals(user_id, completed=None)
        changed = False
        for goal in rows:
            current = self.goal_progress(goal)
            should_complete = current >= int(goal["target_amount"])
            if should_complete and goal["completed_at"] is None:
                self.conn.execute(
                    "UPDATE goals SET completed_at=? WHERE id=? AND user_id=?",
                    (now_iso(), goal["id"], user_id),
                )
                changed = True
            elif not should_complete and goal["completed_at"] is not None:
                # If a corrected/deleted operation drops the value, reopen the goal.
                self.conn.execute(
                    "UPDATE goals SET completed_at=NULL WHERE id=? AND user_id=?",
                    (goal["id"], user_id),
                )
                changed = True
        if changed:
            self.conn.commit()

    def delete_goal(self, user_id: int, goal_id: int):
        self.conn.execute(
            "DELETE FROM goals WHERE user_id=? AND id=?", (user_id, goal_id)
        )
        self.conn.commit()


db = EconomyDB(ECONOMY_DB_PATH)


# ---------- Access protection ----------
def owner_allowed(interaction: discord.Interaction) -> bool:
    if not ECONOMY_USER_ID:
        return False
    if interaction.user.id != ECONOMY_USER_ID:
        return False
    if GUILD_ID and interaction.guild_id != GUILD_ID:
        return False
    if ECONOMY_CHANNEL_ID and interaction.channel_id != ECONOMY_CHANNEL_ID:
        return False
    return True


async def deny_interaction(interaction: discord.Interaction):
    text = "🔒 Цією Economy може користуватися тільки власник."
    if not ECONOMY_USER_ID:
        text = "🔒 ECONOMY_USER_ID не налаштований."
    elif ECONOMY_CHANNEL_ID and interaction.channel_id != ECONOMY_CHANNEL_ID:
        text = "📍 Economy працює тільки у своєму каналі."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def edit_panel_from_modal(
    interaction: discord.Interaction, *, content=None, embed=None, view=None
):
    """Acknowledge a modal and update the single persistent Economy panel."""
    if not interaction.response.is_done():
        await interaction.response.defer()
    message = await _find_panel_message(interaction.client)
    if not message:
        await interaction.followup.send(
            "⚠️ Панель Economy не знайдена. Виконай /economy для відновлення.",
            ephemeral=True,
        )
        return
    await message.edit(content=content, embed=embed, view=view)


class ProtectedEconomyView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not owner_allowed(interaction):
            await deny_interaction(interaction)
            return False
        return True


class ProtectedEconomyModal(discord.ui.Modal):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not owner_allowed(interaction):
            await deny_interaction(interaction)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        print(f"[ECONOMY] Modal error: {type(error).__name__}: {error}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ Помилка Economy. Спробуй ще раз.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Помилка Economy. Спробуй ще раз.", ephemeral=True)


async def perform_undo(user_id: int):
    action = get_undo(user_id)
    if not action:
        return None
    clear_undo(user_id)
    kind = action["type"]
    payload = action["payload"]
    if kind == "complete":
        db.reopen_completed(payload["session_id"], user_id)
        return ("session", payload["session_id"])
    if kind == "delete_transaction":
        db.restore_transaction(payload["transaction"])
        return ("session", payload["session_id"])
    if kind == "delete_session":
        db.restore_session(
            payload["session"], payload["transactions"], payload.get("segments", [])
        )
        return ("session", payload["session"]["id"])
    return None


# ---------- Embeds ----------
def session_embed(row) -> discord.Embed:
    income, expense = db.totals(row["id"])
    profit = income - expense
    title, color = {
        "working": ("🟢 Робота активна", discord.Color.green()),
        "paused": ("⏸️ Роботу зупинено", discord.Color.gold()),
        "pending_sale": ("📦 Очікує продажу", discord.Color.orange()),
        "completed": ("✅ Завершено", discord.Color.blue()),
    }[row["status"]]

    embed = discord.Embed(
        title=f"💼 {row['job_name']}", description=title, color=color
    )
    embed.add_field(
        name="⏱️ Час", value=duration_text(db.worked_seconds(row)), inline=False
    )
    embed.add_field(name="💰 Отримано", value=f"{money(income)} $", inline=True)
    embed.add_field(name="💸 Витрати", value=f"{money(expense)} $", inline=True)
    embed.add_field(name="📈 Результат", value=f"{money(profit)} $", inline=True)
    if row["note"]:
        embed.add_field(name="📝 Нотатка", value=row["note"][:1024], inline=False)

    marker = f"Economy v{ECONOMY_VERSION} • session:{row['id']}"
    if row["status"] == "pending_sale":
        embed.set_footer(
            text=(
                "Можна продовжити цю ж роботу пізніше. "
                f"Ці гроші ще не входять у фінальну статистику. • {marker}"
            )
        )
    else:
        embed.set_footer(text=marker)
    return embed


def session_details_embed(row) -> discord.Embed:
    embed = session_embed(row)
    created = parse_dt(row["created_at"]).strftime("%d.%m.%Y %H:%M")
    embed.add_field(name="📅 Створено", value=created, inline=False)
    txs = db.transactions(row["id"])
    if txs:
        lines = []
        for tx in txs[-12:]:
            sign = "+" if tx["kind"] == "income" else "-"
            note = f" — {tx['note']}" if tx["note"] else ""
            stamp = parse_dt(tx['created_at']).strftime('%d.%m %H:%M')
            lines.append(f"`{stamp}` • {sign}{money(tx['amount'])} ${note}")
        if len(txs) > 12:
            lines.insert(0, f"… ще {len(txs)-12} операцій")
        embed.add_field(name="🧾 Операції", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🧾 Операції", value="Немає", inline=False)

    segments = db.work_segments(row["id"])
    approach_lines = []
    for seg in segments[-10:]:
        start_dt = parse_dt(seg["started_at"])
        end_dt = parse_dt(seg["ended_at"])
        if start_dt.date() == end_dt.date():
            stamp = start_dt.strftime("%d.%m")
        else:
            stamp = f"{start_dt.strftime('%d.%m')}→{end_dt.strftime('%d.%m')}"
        suffix = " • вручну" if seg["source"] == "manual" else ""
        approach_lines.append(
            f"`{stamp}` • {duration_text(seg['seconds'])}{suffix}"
        )
    if row["status"] == "working" and row["current_started_at"]:
        live_seconds = max(0, int((now_dt() - parse_dt(row["current_started_at"])).total_seconds()))
        approach_lines.append(
            f"`{parse_dt(row['current_started_at']).strftime('%d.%m')}` • "
            f"{duration_text(live_seconds)} • зараз"
        )
    if approach_lines:
        if len(segments) > 10:
            approach_lines.insert(0, f"… ще {len(segments)-10} підходів")
        embed.add_field(
            name="📅 Підходи до роботи", value="\n".join(approach_lines), inline=False
        )

    if row["status"] == "completed":
        income, expense = db.totals(row["id"])
        sec = int(row["worked_seconds"] or 0)
        hourly = round((income - expense) / (sec / 3600)) if sec else 0
        embed.add_field(
            name="⚡ Чистими / год", value=f"{money(hourly)} $", inline=False
        )

    footer = f"Economy v{ECONOMY_VERSION} • details"
    if row["status"] == "pending_sale":
        footer = f"Ці гроші ще не входять у фінальну статистику. • {footer}"
    embed.set_footer(text=footer)
    return embed


def pending_overview_embed(user_id: int) -> discord.Embed:
    rows = db.pending(user_id)
    embed = discord.Embed(title="📦 Очікують продажу", color=discord.Color.orange())
    if not rows:
        embed.description = "Немає незакритих сесій."
        return embed
    lines = []
    for row in rows[:12]:
        inc, exp = db.totals(row["id"])
        lines.append(
            f"**{row['job_name']}** — {duration_text(db.worked_seconds(row))}\n"
            f"💰 Отримано: {money(inc)} $ • 💸 Витрати: {money(exp)} $"
        )
    embed.description = "\n\n".join(lines)
    if len(rows) > 12:
        embed.set_footer(text=f"Ще {len(rows)-12} сесій • обери потрібну нижче")
    else:
        embed.set_footer(text="Обери сесію нижче, щоб відкрити деталі")
    return embed


def history_filter_label(
    user_id: int, status: Optional[str] = None, job_id: Optional[int] = None,
    date_filter: Optional[str] = None
) -> str:
    parts = []
    if status == "completed":
        parts.append("✅ завершені")
    elif status == "pending_sale":
        parts.append("📦 очікують продажу")
    if job_id is not None:
        job = db.job(user_id, job_id)
        parts.append(f"💼 {job['name'] if job else 'робота'}")
    if date_filter:
        try:
            parts.append(f"📅 {datetime.strptime(date_filter, '%Y-%m-%d').strftime('%d.%m.%Y')}")
        except ValueError:
            parts.append(f"📅 {date_filter}")
    return " • ".join(parts) if parts else "Усі сесії"


def history_embed(
    user_id: int, page: int = 0, page_size: int = 20,
    status: Optional[str] = None, job_id: Optional[int] = None,
    date_filter: Optional[str] = None
) -> discord.Embed:
    rows = db.history_page(user_id, page, page_size, status, job_id, date_filter)
    total = db.history_count(user_id, status, job_id, date_filter)
    pages = max(1, (total + page_size - 1) // page_size)
    embed = discord.Embed(
        title=f"📋 Історія сесій — {page+1}/{pages}",
        description=f"**Фільтр:** {history_filter_label(user_id, status, job_id, date_filter)}",
        color=discord.Color.blurple(),
    )
    if not rows:
        embed.description += "\n\nЗа цим фільтром сесій немає."
        return embed
    icons = {"working":"🟢","paused":"⏸️","pending_sale":"📦","completed":"✅"}
    lines=[]
    for row in rows:
        inc, exp = db.totals(row["id"])
        dt = parse_dt(row["created_at"]).strftime("%d.%m.%Y")
        note = f" • {row['note']}" if row["note"] else ""
        lines.append(
            f"{icons.get(row['status'],'•')} **{row['job_name']}** • {dt}{note}\n"
            f"⏱️ {duration_text(db.worked_seconds(row))} • 📈 {money(inc-exp)} $"
        )
    embed.description += "\n\n" + "\n\n".join(lines)[:3800]
    embed.set_footer(text="Обери сесію нижче або зміни фільтр")
    return embed


def comparison_embed(user_id: int, days: Optional[int], label: str) -> discord.Embed:
    rows = db.job_stats(user_id, days)
    embed = discord.Embed(title=f"⚖️ Порівняння робіт — {label}", color=discord.Color.teal())
    if len(rows) < 2:
        embed.description = "Для порівняння потрібно хоча б дві роботи із завершеними сесіями."
        return embed
    lines=[]
    for idx,row in enumerate(rows[:12],1):
        avg_session = round(row["profit"] / row["sessions"]) if row["sessions"] else 0
        reliability = "✅" if row["sessions"] >= 3 else "⚠️ мало даних"
        lines.append(
            f"**{idx}. {row['job_name']}** — ⚡ {money(row['hourly'])} $/год\n"
            f"📈 {money(row['profit'])} $ • середня сесія {money(avg_session)} $ • {row['sessions']} сес. • {reliability}"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Фінанси рахуються лише по повністю закритих сесіях.")
    return embed


def main_embed(user_id: int) -> discord.Embed:
    current = db.open_session(user_id)
    pending = db.pending_totals(user_id)
    db.refresh_goals(user_id)
    active_goals = db.goals(user_id, completed=False)

    embed = discord.Embed(
        title="💰 ECONOMY",
        description="Твій поточний стан",
        color=discord.Color.green(),
    )

    if current:
        icon = "🟢" if current["status"] == "working" else "⏸️"
        state = "працює" if current["status"] == "working" else "пауза"
        embed.add_field(
            name="🧑‍💼 Поточна робота",
            value=(
                f"{icon} **{current['job_name']}** • {state}\n"
                f"⏱️ **{duration_text(db.worked_seconds(current))}**"
            ),
            inline=False,
        )

    if pending["sessions"]:
        profit = pending["income"] - pending["expense"]
        embed.add_field(
            name="📦 Очікують продажу",
            value=(
                f"Сесій: **{pending['sessions']}**\n"
                f"💰 Незакрито отримано: **{money(pending['income'])} $**\n"
                f"📈 Поточний результат: **{money(profit)} $**"
            ),
            inline=False,
        )

    if active_goals:
        goal = active_goals[0]
        current_value = db.goal_progress(goal)
        target = int(goal["target_amount"])
        percent = max(0, min(100, round((current_value / target) * 100))) if target else 0
        kind = "Дохід" if goal["goal_type"] == "income" else "Чистий прибуток"
        scope = goal["job_name"] or "Усі роботи"
        extra_goals = f"\nЩе активних цілей: **{len(active_goals)-1}**" if len(active_goals) > 1 else ""
        embed.add_field(
            name="🎯 Активна ціль",
            value=(
                f"**{kind} • {scope}**\n"
                f"{progress_bar(current_value, target)} **{percent}%**\n"
                f"{money(current_value)} / {money(target)} $"
                f"{extra_goals}"
            ),
            inline=False,
        )

    if not current and not pending["sessions"] and not active_goals:
        embed.description = "Немає активної роботи, незакритих продажів або цілей."

    embed.set_footer(text=f"Economy v{ECONOMY_VERSION} • main")
    return embed


def stats_embed(user_id: int, days: Optional[int], label: str) -> discord.Embed:
    s = db.stats(user_id, days)
    profit = s["income"] - s["expense"]
    hourly = (
        round(profit / (s["completed_seconds"] / 3600))
        if s["completed_seconds"]
        else 0
    )
    pending_profit = s["pending_income"] - s["pending_expense"]

    embed = discord.Embed(
        title=f"📊 Статистика — {label}", color=discord.Color.blue()
    )
    embed.add_field(
        name="⏱️ Відпрацьовано",
        value=duration_text(s["worked_seconds"]),
        inline=False,
    )
    embed.add_field(name="💰 Дохід", value=f"{money(s['income'])} $", inline=True)
    embed.add_field(
        name="💸 Витрати", value=f"{money(s['expense'])} $", inline=True
    )
    embed.add_field(name="📈 Чистими", value=f"{money(profit)} $", inline=True)
    embed.add_field(
        name="⚡ Середнє / год", value=f"{money(hourly)} $", inline=True
    )
    embed.add_field(
        name="📊 Медіана / год", value=f"{money(s['median_hourly'])} $", inline=True
    )
    embed.add_field(
        name="✅ Завершено сесій", value=str(s["completed_sessions"]), inline=True
    )
    embed.add_field(
        name="📦 Очікують продажу", value=str(s["pending_count"]), inline=True
    )
    if s["pending_count"]:
        embed.add_field(
            name="📦 Незакриті гроші",
            value=(
                f"Отримано: **{money(s['pending_income'])} $**\n"
                f"Витрати: **{money(s['pending_expense'])} $**\n"
                f"Поточний результат: **{money(pending_profit)} $**"
            ),
            inline=False,
        )
    embed.set_footer(
        text="Фінанси та $/год рахуються тільки по повністю закритих сесіях."
    )
    return embed


def job_stats_embed(user_id: int, job_id: int, days: Optional[int], label: str):
    rows = db.job_stats(user_id, days)
    row = next((x for x in rows if x["job_id"] == job_id), None)
    if not row:
        return discord.Embed(
            title="💼 Статистика роботи",
            description="За цей період немає завершених сесій.",
            color=discord.Color.blue(),
        )
    embed = discord.Embed(
        title=f"💼 {row['job_name']} — {label}", color=discord.Color.blue()
    )
    embed.add_field(name="⏱️ Час", value=duration_text(row["seconds"]), inline=False)
    embed.add_field(name="💰 Дохід", value=f"{money(row['income'])} $", inline=True)
    embed.add_field(
        name="💸 Витрати", value=f"{money(row['expense'])} $", inline=True
    )
    embed.add_field(
        name="📈 Чистими", value=f"{money(row['profit'])} $", inline=True
    )
    embed.add_field(
        name="⚡ Середнє / год", value=f"{money(row['hourly'])} $", inline=True
    )
    embed.add_field(
        name="📊 Медіана / год", value=f"{money(row['median_hourly'])} $", inline=True
    )
    embed.add_field(name="✅ Сесій", value=str(row["sessions"]), inline=True)
    if row["best_profit"] is not None:
        embed.add_field(
            name="🏆 Найкраща сесія",
            value=(
                f"Чистими: **{money(row['best_profit'])} $**\n"
                f"Темп: **{money(row['best_hourly'])} $/год**"
            ),
            inline=False,
        )
    return embed


def goals_embed(user_id: int, completed: bool = False) -> discord.Embed:
    db.refresh_goals(user_id)
    rows = db.goals(user_id, completed=completed)
    title = "✅ Завершені цілі" if completed else "🎯 Цілі"
    embed = discord.Embed(title=title, color=discord.Color.gold())
    if not rows:
        embed.description = "Поки немає цілей."
        return embed

    for goal in rows[:10]:
        current = db.goal_progress(goal)
        target = int(goal["target_amount"])
        percent = max(0, round((current / target) * 100)) if target else 0
        scope = goal["job_name"] or "Усі роботи"
        kind = "Дохід" if goal["goal_type"] == "income" else "Чистий прибуток"
        status = "✅" if goal["completed_at"] else "🎯"
        embed.add_field(
            name=f"{status} {kind} • {scope}",
            value=(
                f"{progress_bar(current, target)}\n"
                f"**{money(current)} / {money(target)} $** • {percent}%"
            ),
            inline=False,
        )
    if len(rows) > 10:
        embed.set_footer(text=f"Показано 10 із {len(rows)} цілей")
    return embed


# ---------- Modals ----------
class AmountModal(ProtectedEconomyModal):
    def __init__(
        self,
        session_id: int,
        user_id: int,
        kind: str,
        allow_completed: bool = False,
    ):
        super().__init__(title="Додати дохід" if kind == "income" else "Додати витрату")
        self.session_id = session_id
        self.user_id = user_id
        self.kind = kind
        self.allow_completed = allow_completed
        self.amount = discord.ui.TextInput(
            label="Сума", placeholder="Наприклад: 350000", max_length=15
        )
        self.note = discord.ui.TextInput(
            label="Коментар",
            required=False,
            max_length=100,
            placeholder="Необов'язково",
        )
        self.add_item(self.amount)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = parse_amount(str(self.amount.value))
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "❌ Введи коректну суму.", ephemeral=True
            )

        signature = (
            f"tx:{self.session_id}:{self.kind}:{amount}:"
            f"{str(self.note.value).strip()}"
        )
        if not allow_action(self.user_id, signature, 2.5):
            return await interaction.response.send_message(
                "⏳ Ця операція вже обробляється.", ephemeral=True
            )

        async with session_lock(self.user_id, self.session_id):
            row = db.session(self.session_id, self.user_id)
            if not row or (row["status"] == "completed" and not self.allow_completed):
                return await interaction.response.send_message(
                    "❌ Цей запис уже закритий.", ephemeral=True
                )

            db.add_transaction(
                self.session_id,
                self.user_id,
                self.kind,
                amount,
                str(self.note.value).strip() or None,
            )
        row = db.session(self.session_id, self.user_id)
        view = (
            CompletedManualView(self.user_id, self.session_id)
            if self.allow_completed
            else SessionView(self.user_id, self.session_id)
        )
        await edit_panel_from_modal(interaction, content=None, embed=session_embed(row), view=view)


class EditTransactionModal(ProtectedEconomyModal):
    def __init__(self, user_id: int, transaction_id: int):
        tx = db.transaction(transaction_id, user_id)
        super().__init__(title="Редагувати операцію")
        self.user_id = user_id
        self.transaction_id = transaction_id
        self.session_id = tx["session_id"] if tx else None
        self.amount = discord.ui.TextInput(
            label="Сума", default=str(tx["amount"] if tx else ""), max_length=15
        )
        self.note = discord.ui.TextInput(
            label="Коментар",
            default=(tx["note"] or "") if tx else "",
            required=False,
            max_length=100,
        )
        self.add_item(self.amount)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        tx = db.transaction(self.transaction_id, self.user_id)
        if not tx:
            return await interaction.response.send_message(
                "❌ Операцію вже видалено.", ephemeral=True
            )
        try:
            amount = parse_amount(str(self.amount.value))
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "❌ Введи коректну суму.", ephemeral=True
            )
        db.update_transaction(
            self.transaction_id,
            self.user_id,
            amount,
            str(self.note.value).strip() or None,
        )
        row = db.session(tx["session_id"], self.user_id)
        await edit_panel_from_modal(
            interaction, content="✅ Операцію оновлено.",
            embed=session_details_embed(row),
            view=SessionHistoryView(self.user_id, row["id"]),
        )


class AddTimeModal(ProtectedEconomyModal):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(title="Додати час")
        self.user_id = user_id
        self.session_id = session_id
        self.hours = discord.ui.TextInput(
            label="Години", placeholder="Наприклад: 1", default="0", max_length=3
        )
        self.minutes = discord.ui.TextInput(
            label="Хвилини", placeholder="Наприклад: 30", default="0", max_length=2
        )
        self.add_item(self.hours)
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hours = int(str(self.hours.value).strip() or "0")
            minutes = int(str(self.minutes.value).strip() or "0")
            if hours < 0 or minutes < 0 or minutes > 59 or (hours == 0 and minutes == 0):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Вкажи коректний час. Хвилини — від 0 до 59.", ephemeral=True
            )

        seconds = hours * 3600 + minutes * 60
        if not allow_action(self.user_id, f"addtime:{self.session_id}:{seconds}", 2.5):
            return await interaction.response.send_message(
                "⏳ Цей час уже додається.", ephemeral=True
            )
        async with session_lock(self.user_id, self.session_id):
            row = db.session(self.session_id, self.user_id)
            if not row:
                return await interaction.response.send_message(
                    "❌ Сесію не знайдено.", ephemeral=True
                )
            db.add_worked_time(self.session_id, self.user_id, seconds)
        row = db.session(self.session_id, self.user_id)
        if row["status"] == "completed":
            await edit_panel_from_modal(
                interaction, content="✅ Час додано до сесії.",
                embed=session_details_embed(row),
                view=SessionHistoryView(self.user_id, self.session_id),
            )
        else:
            await edit_panel_from_modal(
                interaction, content=None,
                embed=session_embed(row),
                view=SessionView(self.user_id, self.session_id),
            )


class NoteModal(ProtectedEconomyModal):
    def __init__(self, user_id: int, session_id: int):
        row = db.session(session_id, user_id)
        super().__init__(title="Нотатка до сесії")
        self.user_id = user_id
        self.session_id = session_id
        self.note = discord.ui.TextInput(
            label="Нотатка",
            default=(row["note"] or "") if row else "",
            required=False,
            max_length=150,
            placeholder="Наприклад: x2, з другом, вечірній фарм",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        db.update_note(self.session_id, self.user_id, str(self.note.value))
        row = db.session(self.session_id, self.user_id)
        if not row:
            return await interaction.response.send_message(
                "❌ Сесію не знайдено.", ephemeral=True
            )
        await edit_panel_from_modal(
            interaction, content="✅ Нотатку збережено.",
            embed=session_details_embed(row),
            view=SessionHistoryView(self.user_id, self.session_id),
        )


class AddJobModal(ProtectedEconomyModal):
    def __init__(self, user_id: int):
        super().__init__(title="Нова робота")
        self.user_id = user_id
        self.name = discord.ui.TextInput(
            label="Назва", placeholder="Наприклад: Каменяр", max_length=50
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.name.value).strip()
        if not name:
            return await interaction.response.send_message(
                "❌ Вкажи назву.", ephemeral=True
            )
        await edit_panel_from_modal(
            interaction, content="Оберіть тип доходу:", embed=None,
            view=IncomeModeView(self.user_id, name),
        )


class ManualTimeModal(ProtectedEconomyModal):
    def __init__(self, user_id: int, job_id: int):
        super().__init__(title="Ручний запис роботи")
        self.user_id = user_id
        self.job_id = job_id
        self.hours = discord.ui.TextInput(
            label="Години", placeholder="Наприклад: 3", default="0", max_length=3
        )
        self.minutes = discord.ui.TextInput(
            label="Хвилини", placeholder="Наприклад: 30", default="0", max_length=2
        )
        self.add_item(self.hours)
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hours = int(str(self.hours.value).strip() or "0")
            minutes = int(str(self.minutes.value).strip() or "0")
            if hours < 0 or minutes < 0 or minutes > 59 or (hours == 0 and minutes == 0):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Вкажи коректний час. Хвилини — від 0 до 59.", ephemeral=True
            )
        seconds = hours * 3600 + minutes * 60
        await edit_panel_from_modal(
            interaction, content=f"📝 Час роботи: **{duration_text(seconds)}**\nЯк записати цю сесію?",
            embed=None, view=ManualFinishChoiceView(self.user_id, self.job_id, seconds),
        )


class GoalAmountModal(ProtectedEconomyModal):
    def __init__(
        self, user_id: int, goal_type: str, job_id: Optional[int], scope_name: str
    ):
        super().__init__(title="Нова ціль")
        self.user_id = user_id
        self.goal_type = goal_type
        self.job_id = job_id
        self.scope_name = scope_name
        self.amount = discord.ui.TextInput(
            label="Сума цілі",
            placeholder="Наприклад: 5000000",
            max_length=15,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = parse_amount(str(self.amount.value))
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "❌ Введи коректну суму.", ephemeral=True
            )
        db.add_goal(self.user_id, self.goal_type, target, self.job_id)
        kind = "дохід" if self.goal_type == "income" else "чистий прибуток"
        await edit_panel_from_modal(
            interaction, content=f"✅ Ціль створено: **{money(target)} $ {kind}** • {self.scope_name}",
            embed=goals_embed(self.user_id, completed=False),
            view=GoalsView(self.user_id),
        )


# ---------- Session views ----------
class UndoView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id
        if get_undo(user_id):
            undo = discord.ui.Button(label="Повернути", emoji="↩️", style=discord.ButtonStyle.primary)
            undo.callback = self.undo
            self.add_item(undo)
        menu = discord.ui.Button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary)
        menu.callback = self.menu
        self.add_item(menu)

    async def undo(self, interaction):
        result = await perform_undo(self.user_id)
        if not result:
            return await interaction.response.send_message("⌛ Час для повернення дії вже минув.", ephemeral=True)
        _, sid = result
        row = db.session(sid, self.user_id)
        await interaction.response.edit_message(
            content="↩️ Останню дію повернуто.",
            embed=session_details_embed(row),
            view=SessionHistoryView(self.user_id, sid),
        )

    async def menu(self, interaction):
        await interaction.response.edit_message(
            content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id)
        )


class SessionView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id
        row = db.session(session_id, user_id)
        status = row["status"] if row else "completed"

        # 1. Main state action.
        if status == "working":
            b = discord.ui.Button(label="Зупинити роботу", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
            b.callback = self.pause
            self.add_item(b)
        elif status == "paused":
            resume = discord.ui.Button(label="Продовжити роботу", emoji="▶️", style=discord.ButtonStyle.success, row=0)
            finish = discord.ui.Button(label="Завершити роботу", emoji="✅", style=discord.ButtonStyle.primary, row=0)
            resume.callback = self.resume
            finish.callback = self.finish
            self.add_item(resume)
            self.add_item(finish)
        elif status == "pending_sale":
            resume = discord.ui.Button(
                label="Продовжити роботу",
                emoji="▶️",
                style=discord.ButtonStyle.primary,
                row=0,
            )
            done = discord.ui.Button(
                label="Все продано",
                emoji="✅",
                style=discord.ButtonStyle.success,
                row=0,
            )
            resume.callback = self.resume
            done.callback = self.complete
            self.add_item(resume)
            self.add_item(done)

        # 2. Money.
        if status in ("working", "paused", "pending_sale"):
            income = discord.ui.Button(label="Дохід", emoji="💰", style=discord.ButtonStyle.success, row=1)
            expense = discord.ui.Button(label="Витрата", emoji="💸", style=discord.ButtonStyle.danger, row=1)
            income.callback = self.add_income
            expense.callback = self.add_expense
            self.add_item(income)
            self.add_item(expense)

        # 3. Details.
        if status in ("working", "paused", "pending_sale"):
            ops = discord.ui.Button(label="Операції", emoji="🧾", style=discord.ButtonStyle.secondary, row=2)
            note = discord.ui.Button(label="Нотатка", emoji="📝", style=discord.ButtonStyle.secondary, row=2)
            ops.callback = self.operations
            note.callback = self.note
            self.add_item(ops)
            self.add_item(note)
            if status == "pending_sale":
                add_time = discord.ui.Button(label="Додати час", emoji="➕", style=discord.ButtonStyle.secondary, row=2)
                add_time.callback = self.add_time
                self.add_item(add_time)

        # 4. Destructive/navigation.
        if status in ("working", "paused", "pending_sale"):
            annul = discord.ui.Button(label="Анулювати", emoji="🗑️", style=discord.ButtonStyle.danger, row=3)
            annul.callback = self.annul
            self.add_item(annul)
        back = discord.ui.Button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
        back.callback = self.back
        self.add_item(back)

    async def add_income(self, interaction):
        if not allow_action(self.user_id, f"open_income:{self.session_id}", 1.2):
            return await interaction.response.send_message("⏳ Вікно доходу вже відкривається.", ephemeral=True)
        await interaction.response.send_modal(AmountModal(self.session_id, self.user_id, "income"))

    async def add_expense(self, interaction):
        if not allow_action(self.user_id, f"open_expense:{self.session_id}", 1.2):
            return await interaction.response.send_message("⏳ Вікно витрати вже відкривається.", ephemeral=True)
        await interaction.response.send_modal(AmountModal(self.session_id, self.user_id, "expense"))

    async def operations(self, interaction):
        await show_operations(interaction, self.user_id, self.session_id)

    async def add_time(self, interaction):
        await interaction.response.send_modal(AddTimeModal(self.user_id, self.session_id))

    async def pause(self, interaction):
        async with session_lock(self.user_id, self.session_id):
            row = db.session(self.session_id, self.user_id)
            if not row or row["status"] != "working":
                return await interaction.response.send_message(
                    "⏳ Стан сесії вже змінився.", ephemeral=True
                )
            db.pause(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None, embed=session_embed(row),
            view=SessionView(self.user_id, self.session_id)
        )

    async def resume(self, interaction):
        async with session_lock(self.user_id, self.session_id):
            current = db.open_session(self.user_id)
            if current and current["id"] != self.session_id:
                return await interaction.response.send_message(
                    "❌ Уже є інша поточна робота. Спочатку зупини або заверши її.",
                    ephemeral=True,
                )
            if not db.resume(self.session_id, self.user_id):
                return await interaction.response.send_message(
                    "⏳ Стан сесії вже змінився або її не можна продовжити.",
                    ephemeral=True,
                )
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None, embed=session_embed(row),
            view=SessionView(self.user_id, self.session_id),
        )

    async def finish(self, interaction):
        async with session_lock(self.user_id, self.session_id):
            row = db.session(self.session_id, self.user_id)
            if not row or row["status"] not in ("working", "paused"):
                return await interaction.response.send_message(
                    "⏳ Стан сесії вже змінився.", ephemeral=True
                )
            db.finish_work(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None, embed=session_embed(row),
            view=SessionView(self.user_id, self.session_id)
        )

    async def complete(self, interaction):
        async with session_lock(self.user_id, self.session_id):
            row = db.session(self.session_id, self.user_id)
            if not row or row["status"] != "pending_sale":
                return await interaction.response.send_message(
                    "⏳ Сесію вже закрито або її стан змінився.", ephemeral=True
                )
            store_undo(self.user_id, "complete", {"session_id": self.session_id})
            db.complete(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=f"✅ Сесію закрито. ↩️ Повернути можна протягом {UNDO_SECONDS} с.",
            embed=session_details_embed(row), view=UndoView(self.user_id, self.session_id)
        )

    async def note(self, interaction):
        await interaction.response.send_modal(NoteModal(self.user_id, self.session_id))

    async def annul(self, interaction):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(content=f"⚠️ Анулювати сесію **{row['job_name']}** та всі її операції?", embed=None, view=DeleteSessionConfirmView(self.user_id, self.session_id))

    async def back(self, interaction):
        await interaction.response.edit_message(content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id))


class CompletedManualView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id

        for label, emoji, style, callback in [
            ("Дохід", "💰", discord.ButtonStyle.success, self.add_income),
            ("Витрата", "💸", discord.ButtonStyle.danger, self.add_expense),
            ("Операції", "🧾", discord.ButtonStyle.secondary, self.operations),
        ]:
            button = discord.ui.Button(label=label, emoji=emoji, style=style, row=0)
            button.callback = callback
            self.add_item(button)

        add_time = discord.ui.Button(label="Додати час", emoji="➕", style=discord.ButtonStyle.secondary, row=1)
        note = discord.ui.Button(label="Нотатка", emoji="📝", style=discord.ButtonStyle.secondary, row=1)
        add_time.callback = self.add_time
        note.callback = self.note
        self.add_item(add_time)
        self.add_item(note)

        done = discord.ui.Button(label="Готово", emoji="✅", style=discord.ButtonStyle.primary, row=2)
        done.callback = self.done
        self.add_item(done)

    async def add_income(self, interaction):
        await interaction.response.send_modal(AmountModal(self.session_id, self.user_id, "income", allow_completed=True))

    async def add_expense(self, interaction):
        await interaction.response.send_modal(AmountModal(self.session_id, self.user_id, "expense", allow_completed=True))

    async def operations(self, interaction):
        await show_operations(interaction, self.user_id, self.session_id)

    async def add_time(self, interaction):
        await interaction.response.send_modal(AddTimeModal(self.user_id, self.session_id))

    async def note(self, interaction):
        await interaction.response.send_modal(NoteModal(self.user_id, self.session_id))

    async def done(self, interaction):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(content=None, embed=session_details_embed(row), view=SessionHistoryView(self.user_id, self.session_id))


class SessionHistoryView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id
        row = db.session(session_id, user_id)
        if not row:
            return

        if row["status"] != "completed":
            open_actions = discord.ui.Button(label="Відкрити сесію", emoji="↗️", style=discord.ButtonStyle.primary, row=0)
            open_actions.callback = self.open_actions
            self.add_item(open_actions)
        else:
            reopen = discord.ui.Button(label="Відкрити знову", emoji="🔁", style=discord.ButtonStyle.primary, row=0)
            reopen.callback = self.reopen
            self.add_item(reopen)

        ops = discord.ui.Button(label="Операції", emoji="🧾", style=discord.ButtonStyle.secondary, row=1)
        note = discord.ui.Button(label="Нотатка", emoji="📝", style=discord.ButtonStyle.secondary, row=1)
        ops.callback = self.operations
        note.callback = self.note
        self.add_item(ops)
        self.add_item(note)

        if row["status"] in ("pending_sale", "completed"):
            add_time = discord.ui.Button(label="Додати час", emoji="➕", style=discord.ButtonStyle.secondary, row=1)
            add_time.callback = self.add_time
            self.add_item(add_time)

        annul = discord.ui.Button(label="Анулювати сесію", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
        annul.callback = self.annul
        self.add_item(annul)

        history_btn = discord.ui.Button(label="Історія", emoji="📋", style=discord.ButtonStyle.secondary, row=3)
        menu = discord.ui.Button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
        history_btn.callback = self.back_history
        menu.callback = self.menu
        self.add_item(history_btn)
        self.add_item(menu)

    async def open_actions(self, interaction):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(content=None, embed=session_embed(row), view=SessionView(self.user_id, self.session_id))

    async def reopen(self, interaction):
        if not db.reopen_completed(self.session_id, self.user_id):
            return await interaction.response.send_message("❌ Цю сесію не вдалося відкрити знову.", ephemeral=True)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="🔁 Сесію повернуто в «Очікує продажу».",
            embed=session_embed(row), view=SessionView(self.user_id, self.session_id)
        )

    async def operations(self, interaction):
        await show_operations(interaction, self.user_id, self.session_id)

    async def note(self, interaction):
        await interaction.response.send_modal(NoteModal(self.user_id, self.session_id))

    async def add_time(self, interaction):
        await interaction.response.send_modal(AddTimeModal(self.user_id, self.session_id))

    async def annul(self, interaction):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(content=f"⚠️ Анулювати сесію **{row['job_name']}** та всі її операції?", embed=None, view=DeleteSessionConfirmView(self.user_id, self.session_id))

    async def back_history(self, interaction):
        await interaction.response.edit_message(content=None, embed=history_embed(self.user_id, 0), view=HistoryView(self.user_id, 0))

    async def menu(self, interaction):
        await interaction.response.edit_message(content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id))


class DeleteSessionConfirmView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id

    @discord.ui.button(
        label="Так, анулювати", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):
        async with session_lock(self.user_id, self.session_id):
            row = db.session(self.session_id, self.user_id)
            txs = db.transactions(self.session_id)
            segments = db.work_segments(self.session_id)
            if not row:
                return await interaction.response.send_message(
                    "⏳ Цю сесію вже видалено.", ephemeral=True
                )
            store_undo(self.user_id, "delete_session", {
                "session": _row_dict(row),
                "transactions": [_row_dict(tx) for tx in txs],
                "segments": [_row_dict(seg) for seg in segments],
            })
            db.delete_session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=f"🗑️ Сесію анульовано. ↩️ Повернути можна протягом {UNDO_SECONDS} с.",
            embed=history_embed(self.user_id, 0),
            view=UndoView(self.user_id),
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        row = db.session(self.session_id, self.user_id)
        if row:
            await interaction.response.edit_message(
                content=None,
                embed=session_details_embed(row),
                view=SessionHistoryView(self.user_id, self.session_id),
            )
        else:
            await interaction.response.edit_message(
                content=None,
                embed=main_embed(self.user_id),
                view=EconomyMainView(self.user_id),
            )


# ---------- Operations ----------
def operations_embed(user_id: int, session_id: int) -> discord.Embed:
    row = db.session(session_id, user_id)
    txs = db.transactions(session_id)
    embed = discord.Embed(
        title=f"🧾 Операції • {row['job_name'] if row else 'Сесія'}",
        color=discord.Color.blurple(),
    )
    if not txs:
        embed.description = "У цій сесії ще немає операцій."
        return embed
    lines=[]
    for tx in txs[-15:]:
        sign = "+" if tx["kind"] == "income" else "-"
        stamp = parse_dt(tx["created_at"]).strftime("%d.%m %H:%M")
        note = f" • {tx['note']}" if tx["note"] else ""
        lines.append(f"`{stamp}` • **{sign}{money(tx['amount'])} $**{note}")
    embed.description = "\n".join(lines)
    if len(txs)>15:
        embed.set_footer(text=f"Показано останні 15 із {len(txs)} операцій")
    return embed


async def show_operations(
    interaction: discord.Interaction, user_id: int, session_id: int
):
    txs = db.transactions(session_id)
    row = db.session(session_id, user_id)
    if not row:
        return await interaction.response.send_message(
            "❌ Сесію не знайдено.", ephemeral=True
        )
    if not txs:
        return await interaction.response.edit_message(
            content=None, embed=operations_embed(user_id, session_id),
            view=OperationsView(user_id, session_id),
        )
    await interaction.response.edit_message(
        content=None, embed=operations_embed(user_id, session_id),
        view=OperationsView(user_id, session_id),
    )


class OperationSelect(discord.ui.Select):
    def __init__(self, user_id: int, session_id: int):
        self.user_id = user_id
        self.session_id = session_id
        txs = db.transactions(session_id)[-25:]
        options = []
        for tx in reversed(txs):
            sign = "+" if tx["kind"] == "income" else "-"
            kind = "Дохід" if tx["kind"] == "income" else "Витрата"
            options.append(
                discord.SelectOption(
                    label=f"{sign}{money(tx['amount'])} $ • {kind}"[:100],
                    value=str(tx["id"]),
                    description=(
                        f"{parse_dt(tx['created_at']).strftime('%d.%m %H:%M')} • "
                        f"{tx['note'] or 'Без коментаря'}"
                    )[:100],
                )
            )
        super().__init__(placeholder="Оберіть операцію", options=options)

    async def callback(self, interaction: discord.Interaction):
        tx_id = int(self.values[0])
        tx = db.transaction(tx_id, self.user_id)
        if not tx:
            return await interaction.response.send_message(
                "❌ Операцію не знайдено.", ephemeral=True
            )
        sign = "+" if tx["kind"] == "income" else "-"
        kind = "Дохід" if tx["kind"] == "income" else "Витрата"
        note = tx["note"] or "—"
        await interaction.response.edit_message(
            content=(
                f"🧾 **{kind}**\n"
                f"Сума: **{sign}{money(tx['amount'])} $**\n"
                f"Дата: **{parse_dt(tx['created_at']).strftime('%d.%m.%Y %H:%M')}**\n"
                f"Коментар: {note}"
            ),
            view=OperationManageView(self.user_id, tx_id, self.session_id),
        )


class OperationsView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.session_id = session_id
        if db.transactions(session_id):
            self.add_item(OperationSelect(user_id, session_id))
        back = discord.ui.Button(
            label="До сесії", emoji="◀️", style=discord.ButtonStyle.secondary, row=1
        )
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_details_embed(row),
            view=SessionHistoryView(self.user_id, self.session_id),
        )


class OperationManageView(ProtectedEconomyView):
    def __init__(self, user_id: int, transaction_id: int, session_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.transaction_id = transaction_id
        self.session_id = session_id

    @discord.ui.button(label="Редагувати", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        await interaction.response.send_modal(
            EditTransactionModal(self.user_id, self.transaction_id)
        )

    @discord.ui.button(label="Видалити", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction, button):
        await interaction.response.edit_message(
            content="⚠️ Видалити цю операцію?",
            view=DeleteTransactionConfirmView(
                self.user_id, self.transaction_id, self.session_id
            ),
        )

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=operations_embed(self.user_id, self.session_id),
            view=OperationsView(self.user_id, self.session_id),
        )


class DeleteTransactionConfirmView(ProtectedEconomyView):
    def __init__(self, user_id: int, transaction_id: int, session_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.transaction_id = transaction_id
        self.session_id = session_id

    @discord.ui.button(
        label="Так, видалити", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):
        async with session_lock(self.user_id, self.session_id):
            tx = db.transaction(self.transaction_id, self.user_id)
            if not tx:
                return await interaction.response.send_message(
                    "⏳ Цю операцію вже видалено.", ephemeral=True
                )
            store_undo(self.user_id, "delete_transaction", {
                "transaction": _row_dict(tx), "session_id": self.session_id
            })
            db.delete_transaction(self.transaction_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=f"✅ Операцію видалено. ↩️ Повернути можна протягом {UNDO_SECONDS} с.",
            embed=session_details_embed(row),
            view=UndoView(self.user_id, self.session_id),
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=operations_embed(self.user_id, self.session_id),
            view=OperationsView(self.user_id, self.session_id),
        )


# ---------- Start / manual ----------
class JobSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        rows = db.jobs(user_id)
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["id"]),
                description=(
                    "Дохід одразу"
                    if row["income_mode"] == "instant"
                    else "Продаж пізніше"
                ),
            )
            for row in rows[:25]
        ]
        super().__init__(placeholder="Оберіть роботу", options=options)

    async def callback(self, interaction: discord.Interaction):
        job_id = int(self.values[0])
        if not allow_action(self.user_id, f"start_job:{job_id}", 1.5):
            return await interaction.response.send_message("⏳ Робота вже запускається.", ephemeral=True)
        try:
            session_id = db.start_session(self.user_id, job_id)
        except RuntimeError:
            return await interaction.response.send_message(
                "❌ Уже є поточна робота.", ephemeral=True
            )
        row = db.session(session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_embed(row),
            view=SessionView(self.user_id, session_id),
        )


class JobSelectView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.add_item(JobSelect(user_id))
        back = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction):
        await interaction.response.edit_message(content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id))


class ManualJobSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in db.jobs(user_id)[:25]
        ]
        super().__init__(placeholder="Оберіть роботу", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            ManualTimeModal(self.user_id, int(self.values[0]))
        )


class ManualJobSelectView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.add_item(ManualJobSelect(user_id))
        back = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction):
        await interaction.response.edit_message(content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id))


class ManualFinishChoiceView(ProtectedEconomyView):
    def __init__(self, user_id: int, job_id: int, seconds: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.job_id = job_id
        self.seconds = seconds

    @discord.ui.button(
        label="Очікують продажу", emoji="📦", style=discord.ButtonStyle.primary
    )
    async def pending(self, interaction, button):
        if not allow_action(self.user_id, f"manual_pending:{self.job_id}:{self.seconds}", 2.0):
            return await interaction.response.send_message("⏳ Запис уже створюється.", ephemeral=True)
        sid = db.create_manual_session(
            self.user_id, self.job_id, self.seconds, "pending_sale"
        )
        row = db.session(sid, self.user_id)
        await interaction.response.edit_message(
            content="📦 Запис створено. Додавай продажі частинами; коли все продано — закрий запис.",
            embed=session_embed(row),
            view=SessionView(self.user_id, sid),
        )

    @discord.ui.button(label="Записати", emoji="✅", style=discord.ButtonStyle.success)
    async def complete_now(self, interaction, button):
        if not allow_action(self.user_id, f"manual_complete:{self.job_id}:{self.seconds}", 2.0):
            return await interaction.response.send_message("⏳ Запис уже створюється.", ephemeral=True)
        sid = db.create_manual_session(
            self.user_id, self.job_id, self.seconds, "completed"
        )
        row = db.session(sid, self.user_id)
        await interaction.response.edit_message(
            content="✅ Фінальний запис створено. Додай дохід/витрати й натисни «Готово».",
            embed=session_embed(row),
            view=CompletedManualView(self.user_id, sid),
        )

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content="📝 **Оберіть роботу для ручного запису**", embed=None,
            view=ManualJobSelectView(self.user_id),
        )


class IncomeModeView(ProtectedEconomyView):
    def __init__(self, user_id: int, name: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.name = name

    @discord.ui.button(label="Дохід одразу", emoji="💵", style=discord.ButtonStyle.success)
    async def instant(self, interaction, button):
        db.add_job(self.user_id, self.name, "instant")
        await interaction.response.edit_message(
            content=f"✅ Додано роботу **{self.name}**.",
            view=JobsView(self.user_id),
        )

    @discord.ui.button(
        label="Продаж пізніше", emoji="📦", style=discord.ButtonStyle.primary
    )
    async def later(self, interaction, button):
        db.add_job(self.user_id, self.name, "later")
        await interaction.response.edit_message(
            content=f"✅ Додано роботу **{self.name}**.",
            view=JobsView(self.user_id),
        )

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=jobs_text(self.user_id), embed=None, view=JobsView(self.user_id)
        )


# ---------- Pending ----------
class PendingSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        options = []
        for row in db.pending(user_id)[:25]:
            inc, exp = db.totals(row["id"])
            options.append(
                discord.SelectOption(
                    label=row["job_name"][:100],
                    value=str(row["id"]),
                    description=(
                        f"Отримано {money(inc)} $ • витрати {money(exp)} $"
                    )[:100],
                )
            )
        super().__init__(placeholder="Оберіть запис", options=options)

    async def callback(self, interaction):
        sid = int(self.values[0])
        row = db.session(sid, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_details_embed(row),
            view=SessionView(self.user_id, sid),
        )


class PendingView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        if db.pending(user_id):
            self.add_item(PendingSelect(user_id))
        back = discord.ui.Button(
            label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1
        )
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# ---------- History ----------
class HistoryDateModal(ProtectedEconomyModal):
    def __init__(self, user_id: int):
        super().__init__(title="Фільтр історії за датою")
        self.user_id = user_id
        self.date = discord.ui.TextInput(
            label="Дата", placeholder="Наприклад: 08.10.2026", max_length=10
        )
        self.add_item(self.date)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.date.value).strip()
        try:
            parsed = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
        except ValueError:
            return await interaction.response.send_message(
                "❌ Введи дату у форматі ДД.ММ.РРРР.", ephemeral=True
            )
        await edit_panel_from_modal(
            interaction, content=None,
            embed=history_embed(self.user_id, 0, date_filter=parsed),
            view=HistoryView(self.user_id, 0, date_filter=parsed),
        )


class HistoryJobFilterSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        rows = db.all_jobs(user_id)
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in rows[:25]
        ]
        super().__init__(placeholder="Оберіть роботу", options=options)

    async def callback(self, interaction):
        job_id = int(self.values[0])
        await interaction.response.edit_message(
            content=None, embed=history_embed(self.user_id, 0, job_id=job_id),
            view=HistoryView(self.user_id, 0, job_id=job_id),
        )


class HistoryJobFilterView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        if db.all_jobs(user_id):
            self.add_item(HistoryJobFilterSelect(user_id))
        back = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction):
        await interaction.response.edit_message(
            content=None, embed=history_embed(self.user_id, 0),
            view=HistoryView(self.user_id, 0),
        )


class HistoryFilterView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="Усі", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def all(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=history_embed(self.user_id, 0),
            view=HistoryView(self.user_id, 0),
        )

    @discord.ui.button(label="Завершені", emoji="✅", style=discord.ButtonStyle.secondary, row=0)
    async def completed(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=history_embed(self.user_id, 0, status="completed"),
            view=HistoryView(self.user_id, 0, status="completed"),
        )

    @discord.ui.button(label="Очікують продажу", emoji="📦", style=discord.ButtonStyle.secondary, row=0)
    async def pending(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=history_embed(self.user_id, 0, status="pending_sale"),
            view=HistoryView(self.user_id, 0, status="pending_sale"),
        )

    @discord.ui.button(label="За роботою", emoji="💼", style=discord.ButtonStyle.primary, row=1)
    async def by_job(self, interaction, button):
        if not db.all_jobs(self.user_id):
            return await interaction.response.send_message("Немає робіт для фільтра.", ephemeral=True)
        await interaction.response.edit_message(
            content="💼 **Оберіть роботу**", embed=None,
            view=HistoryJobFilterView(self.user_id),
        )

    @discord.ui.button(label="За датою", emoji="📅", style=discord.ButtonStyle.primary, row=1)
    async def by_date(self, interaction, button):
        await interaction.response.send_modal(HistoryDateModal(self.user_id))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=history_embed(self.user_id, 0),
            view=HistoryView(self.user_id, 0),
        )


class HistorySelect(discord.ui.Select):
    def __init__(
        self, user_id: int, page: int, status: Optional[str] = None,
        job_id: Optional[int] = None, date_filter: Optional[str] = None
    ):
        self.user_id = user_id
        self.page = page
        self.status = status
        self.job_id = job_id
        self.date_filter = date_filter
        rows = db.history_page(user_id, page, 20, status, job_id, date_filter)
        icons = {
            "working": "🟢", "paused": "⏸️",
            "pending_sale": "📦", "completed": "✅",
        }
        options = []
        for row in rows:
            date = parse_dt(row["created_at"]).strftime("%d.%m.%Y")
            note = f" • {row['note']}" if row["note"] else ""
            options.append(
                discord.SelectOption(
                    label=f"{icons[row['status']]} {row['job_name']}"[:100],
                    value=str(row["id"]),
                    description=(
                        f"{date} • {duration_text(db.worked_seconds(row))}{note}"
                    )[:100],
                )
            )
        super().__init__(placeholder="Оберіть сесію", options=options)

    async def callback(self, interaction):
        sid = int(self.values[0])
        row = db.session(sid, self.user_id)
        await interaction.response.edit_message(
            content=None, embed=session_details_embed(row),
            view=SessionHistoryView(self.user_id, sid),
        )


class HistoryView(ProtectedEconomyView):
    def __init__(
        self, user_id: int, page: int = 0, status: Optional[str] = None,
        job_id: Optional[int] = None, date_filter: Optional[str] = None
    ):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.page = max(0, page)
        self.status = status
        self.job_id = job_id
        self.date_filter = date_filter
        total = db.history_count(user_id, status, job_id, date_filter)
        rows = db.history_page(user_id, self.page, 20, status, job_id, date_filter)
        if rows:
            self.add_item(HistorySelect(user_id, self.page, status, job_id, date_filter))

        if self.page > 0:
            prev = discord.ui.Button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
            prev.callback = self.prev_page
            self.add_item(prev)
        if (self.page + 1) * 20 < total:
            nxt = discord.ui.Button(label="Далі", emoji="➡️", style=discord.ButtonStyle.secondary, row=1)
            nxt.callback = self.next_page
            self.add_item(nxt)

        filters = discord.ui.Button(label="Фільтри", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
        filters.callback = self.filters
        self.add_item(filters)

        menu = discord.ui.Button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
        menu.callback = self.menu
        self.add_item(menu)

    async def prev_page(self, interaction):
        page = self.page - 1
        await interaction.response.edit_message(
            content=None,
            embed=history_embed(self.user_id, page, 20, self.status, self.job_id, self.date_filter),
            view=HistoryView(self.user_id, page, self.status, self.job_id, self.date_filter),
        )

    async def next_page(self, interaction):
        page = self.page + 1
        await interaction.response.edit_message(
            content=None,
            embed=history_embed(self.user_id, page, 20, self.status, self.job_id, self.date_filter),
            view=HistoryView(self.user_id, page, self.status, self.job_id, self.date_filter),
        )

    async def filters(self, interaction):
        await interaction.response.edit_message(
            content="🔎 **Фільтри історії**", embed=None,
            view=HistoryFilterView(self.user_id),
        )

    async def menu(self, interaction):
        await interaction.response.edit_message(
            content=None, embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# ---------- Statistics ----------
class JobStatsSelect(discord.ui.Select):
    def __init__(self, user_id: int, days: Optional[int], label: str):
        self.user_id = user_id
        self.days = days
        self.label = label
        rows = db.job_stats(user_id, days)
        options = [
            discord.SelectOption(
                label=row["job_name"][:100],
                value=str(row["job_id"]),
                description=(
                    f"{money(row['profit'])} $ чистими • {money(row['hourly'])} $/год"
                )[:100],
            )
            for row in rows[:25]
        ]
        super().__init__(placeholder="Оберіть роботу", options=options)

    async def callback(self, interaction):
        job_id = int(self.values[0])
        await interaction.response.edit_message(
            embed=job_stats_embed(self.user_id, job_id, self.days, self.label),
            view=JobStatsDetailView(self.user_id, self.days, self.label),
        )


class JobStatsDetailView(ProtectedEconomyView):
    def __init__(self, user_id: int, days: Optional[int], label: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.days = days
        self.label = label

    @discord.ui.button(label="По роботах", emoji="💼", style=discord.ButtonStyle.secondary)
    async def back_jobs(self, interaction, button):
        await interaction.response.edit_message(
            embed=job_stats_list_embed(self.user_id, self.days, self.label),
            view=JobStatsView(self.user_id, self.days, self.label),
        )

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary)
    async def back_stats(self, interaction, button):
        await interaction.response.edit_message(
            embed=stats_embed(self.user_id, self.days, self.label),
            view=StatsView(self.user_id, self.days, self.label),
        )


def job_stats_list_embed(user_id: int, days: Optional[int], label: str):
    rows = db.job_stats(user_id, days)
    embed = discord.Embed(
        title=f"💼 Статистика по роботах — {label}", color=discord.Color.blue()
    )
    if not rows:
        embed.description = "За цей період немає завершених сесій."
        return embed
    lines = []
    for row in rows[:15]:
        lines.append(
            f"**{row['job_name']}**\n"
            f"⏱️ {duration_text(row['seconds'])} • ✅ {row['sessions']} сес.\n"
            f"📈 {money(row['profit'])} $ • ⚡ {money(row['hourly'])} $/год"
        )
    embed.description = "\n\n".join(lines)
    return embed


def ranking_embed(user_id: int, days: Optional[int], label: str):
    rows = db.job_stats(user_id, days)
    embed = discord.Embed(
        title=f"🏆 Рейтинг $/год — {label}", color=discord.Color.gold()
    )
    if not rows:
        embed.description = "За цей період немає завершених сесій."
        return embed
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, row in enumerate(rows[:15]):
        prefix = medals[idx] if idx < 3 else f"**{idx+1}.**"
        lines.append(
            f"{prefix} **{row['job_name']}** — {money(row['hourly'])} $/год\n"
            f"└ Чистими {money(row['profit'])} $ • {duration_text(row['seconds'])}"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Рейтинг рахується тільки по завершених сесіях.")
    return embed


class JobStatsView(ProtectedEconomyView):
    def __init__(self, user_id: int, days: Optional[int], label: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.days = days
        self.label = label
        if db.job_stats(user_id, days):
            self.add_item(JobStatsSelect(user_id, days, label))

        ranking = discord.ui.Button(
            label="Рейтинг", emoji="🏆", style=discord.ButtonStyle.secondary, row=1
        )
        stats = discord.ui.Button(
            label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary, row=1
        )
        ranking.callback = self.ranking
        stats.callback = self.stats
        self.add_item(ranking)
        self.add_item(stats)

    async def ranking(self, interaction):
        await interaction.response.edit_message(
            embed=ranking_embed(self.user_id, self.days, self.label),
            view=RankingView(self.user_id, self.days, self.label),
        )

    async def stats(self, interaction):
        await interaction.response.edit_message(
            embed=stats_embed(self.user_id, self.days, self.label),
            view=StatsView(self.user_id, self.days, self.label),
        )


class RankingView(ProtectedEconomyView):
    def __init__(self, user_id: int, days: Optional[int], label: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.days = days
        self.label = label

    @discord.ui.button(label="По роботах", emoji="💼", style=discord.ButtonStyle.secondary)
    async def jobs(self, interaction, button):
        await interaction.response.edit_message(
            embed=job_stats_list_embed(self.user_id, self.days, self.label),
            view=JobStatsView(self.user_id, self.days, self.label),
        )

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction, button):
        await interaction.response.edit_message(
            embed=stats_embed(self.user_id, self.days, self.label),
            view=StatsView(self.user_id, self.days, self.label),
        )


class ComparisonView(ProtectedEconomyView):
    def __init__(self, user_id: int, days: Optional[int], label: str):
        super().__init__(timeout=None)
        self.user_id=user_id; self.days=days; self.label=label

    @discord.ui.button(label="По роботах", emoji="💼", style=discord.ButtonStyle.secondary)
    async def jobs(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=job_stats_list_embed(self.user_id,self.days,self.label),
            view=JobStatsView(self.user_id,self.days,self.label)
        )

    @discord.ui.button(label="Рейтинг", emoji="🏆", style=discord.ButtonStyle.secondary)
    async def ranking(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=ranking_embed(self.user_id,self.days,self.label),
            view=RankingView(self.user_id,self.days,self.label)
        )

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=stats_embed(self.user_id,self.days,self.label),
            view=StatsView(self.user_id,self.days,self.label)
        )


class StatsView(ProtectedEconomyView):
    def __init__(
        self, user_id: int, days: Optional[int] = 30, label: str = "30 днів"
    ):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.days = days
        self.label = label

    async def show(self, interaction, days, label):
        await interaction.response.edit_message(
            embed=stats_embed(self.user_id, days, label),
            view=StatsView(self.user_id, days, label),
        )

    @discord.ui.button(label="Сьогодні", style=discord.ButtonStyle.secondary, row=0)
    async def today(self, interaction, button):
        await self.show(interaction, 1, "сьогодні")

    @discord.ui.button(label="7 днів", style=discord.ButtonStyle.secondary, row=0)
    async def week(self, interaction, button):
        await self.show(interaction, 7, "7 днів")

    @discord.ui.button(label="30 днів", style=discord.ButtonStyle.primary, row=0)
    async def month(self, interaction, button):
        await self.show(interaction, 30, "30 днів")

    @discord.ui.button(label="Весь час", style=discord.ButtonStyle.secondary, row=0)
    async def all_time(self, interaction, button):
        await self.show(interaction, None, "весь час")

    @discord.ui.button(label="По роботах", emoji="💼", style=discord.ButtonStyle.secondary, row=1)
    async def jobs(self, interaction, button):
        await interaction.response.edit_message(
            embed=job_stats_list_embed(self.user_id, self.days, self.label),
            view=JobStatsView(self.user_id, self.days, self.label),
        )

    @discord.ui.button(label="Рейтинг", emoji="🏆", style=discord.ButtonStyle.secondary, row=1)
    async def ranking(self, interaction, button):
        await interaction.response.edit_message(
            embed=ranking_embed(self.user_id, self.days, self.label),
            view=RankingView(self.user_id, self.days, self.label),
        )

    @discord.ui.button(label="Порівняння", emoji="⚖️", style=discord.ButtonStyle.secondary, row=1)
    async def comparison(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=comparison_embed(self.user_id, self.days, self.label),
            view=ComparisonView(self.user_id, self.days, self.label),
        )

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# ---------- Goals ----------
class GoalScopeSelect(discord.ui.Select):
    def __init__(self, user_id: int, goal_type: str):
        self.user_id = user_id
        self.goal_type = goal_type
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in db.jobs(user_id)[:25]
        ]
        super().__init__(placeholder="Або обери конкретну роботу", options=options)

    async def callback(self, interaction):
        job_id = int(self.values[0])
        job = db.job(self.user_id, job_id)
        await interaction.response.send_modal(
            GoalAmountModal(self.user_id, self.goal_type, job_id, job["name"])
        )


class GoalScopeView(ProtectedEconomyView):
    def __init__(self, user_id: int, goal_type: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.goal_type = goal_type
        if db.jobs(user_id):
            self.add_item(GoalScopeSelect(user_id, goal_type))

    @discord.ui.button(label="Усі роботи", emoji="🌐", style=discord.ButtonStyle.primary, row=1)
    async def all_jobs(self, interaction, button):
        await interaction.response.send_modal(
            GoalAmountModal(self.user_id, self.goal_type, None, "Усі роботи")
        )

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content="🎯 Яку ціль створити?", embed=None, view=GoalTypeView(self.user_id)
        )


class GoalTypeView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="Дохід", emoji="💵", style=discord.ButtonStyle.success)
    async def income(self, interaction, button):
        await interaction.response.edit_message(
            content="🎯 Для яких робіт рахувати ціль по доходу?",
            view=GoalScopeView(self.user_id, "income"),
        )

    @discord.ui.button(label="Чистий прибуток", emoji="💰", style=discord.ButtonStyle.primary)
    async def profit(self, interaction, button):
        await interaction.response.edit_message(
            content="🎯 Для яких робіт рахувати ціль по чистому прибутку?",
            view=GoalScopeView(self.user_id, "profit"),
        )

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=goals_embed(self.user_id, False), view=GoalsView(self.user_id)
        )


class GoalSelect(discord.ui.Select):
    def __init__(self, user_id: int, completed: bool):
        self.user_id = user_id
        self.completed = completed
        rows = db.goals(user_id, completed=completed)[:25]
        options = []
        for goal in rows:
            current = db.goal_progress(goal)
            scope = goal["job_name"] or "Усі роботи"
            kind = "Дохід" if goal["goal_type"] == "income" else "Чистими"
            options.append(
                discord.SelectOption(
                    label=f"{kind} • {scope}"[:100],
                    value=str(goal["id"]),
                    description=(
                        f"{money(current)} / {money(goal['target_amount'])} $"
                    )[:100],
                )
            )
        super().__init__(placeholder="Оберіть ціль", options=options)

    async def callback(self, interaction):
        goal_id = int(self.values[0])
        goal = db.goal(self.user_id, goal_id)
        current = db.goal_progress(goal)
        target = int(goal["target_amount"])
        kind = "Дохід" if goal["goal_type"] == "income" else "Чистий прибуток"
        scope = goal["job_name"] or "Усі роботи"
        percent = max(0, round(current / target * 100)) if target else 0
        embed = discord.Embed(title=f"🎯 {kind}", color=discord.Color.gold())
        embed.description = (
            f"**{scope}**\n\n"
            f"{progress_bar(current, target)}\n"
            f"**{money(current)} / {money(target)} $** • {percent}%"
        )
        if goal["completed_at"]:
            embed.add_field(
                name="✅ Виконано",
                value=parse_dt(goal["completed_at"]).strftime("%d.%m.%Y %H:%M"),
                inline=False,
            )
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=GoalDetailView(self.user_id, goal_id, self.completed),
        )


class GoalDetailView(ProtectedEconomyView):
    def __init__(self, user_id: int, goal_id: int, completed: bool):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.goal_id = goal_id
        self.completed = completed

    @discord.ui.button(label="Видалити ціль", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction, button):
        await interaction.response.edit_message(
            content="⚠️ Видалити цю ціль?",
            embed=None,
            view=DeleteGoalConfirmView(self.user_id, self.goal_id),
        )

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id, self.completed),
            view=GoalsListView(self.user_id, self.completed),
        )


class DeleteGoalConfirmView(ProtectedEconomyView):
    def __init__(self, user_id: int, goal_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.goal_id = goal_id

    @discord.ui.button(label="Так, видалити", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        db.delete_goal(self.user_id, self.goal_id)
        await interaction.response.edit_message(
            content="✅ Ціль видалено.",
            embed=goals_embed(self.user_id, False),
            view=GoalsView(self.user_id),
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id, False),
            view=GoalsView(self.user_id),
        )


class GoalsListView(ProtectedEconomyView):
    def __init__(self, user_id: int, completed: bool):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.completed = completed
        rows = db.goals(user_id, completed=completed)
        if rows:
            self.add_item(GoalSelect(user_id, completed))

        back = discord.ui.Button(
            label="Цілі", emoji="🎯", style=discord.ButtonStyle.secondary, row=1
        )
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id, False),
            view=GoalsView(self.user_id),
        )


class GoalsView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        db.refresh_goals(user_id)

        if db.goals(user_id, completed=False):
            self.add_item(GoalSelect(user_id, False))

        new = discord.ui.Button(
            label="Нова ціль", emoji="➕", style=discord.ButtonStyle.success, row=1
        )
        completed = discord.ui.Button(
            label="Завершені", emoji="✅", style=discord.ButtonStyle.secondary, row=1
        )
        menu = discord.ui.Button(
            label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=2
        )
        new.callback = self.new_goal
        completed.callback = self.completed_goals
        menu.callback = self.menu
        self.add_item(new)
        self.add_item(completed)
        self.add_item(menu)

    async def new_goal(self, interaction):
        await interaction.response.edit_message(
            content="🎯 Яку ціль створити?",
            embed=None,
            view=GoalTypeView(self.user_id),
        )

    async def completed_goals(self, interaction):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id, True),
            view=GoalsListView(self.user_id, True),
        )

    async def menu(self, interaction):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# ---------- Jobs settings ----------
class JobSettingsSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in db.jobs(user_id)[:25]
        ]
        super().__init__(placeholder="Оберіть роботу", options=options)

    async def callback(self, interaction):
        job_id = int(self.values[0])
        row = db.job(self.user_id, job_id)
        await interaction.response.edit_message(
            content=f"⚙️ **{row['name']}**\nЩо зробити?",
            view=AnnulChoiceView(self.user_id, job_id, row["name"]),
        )


class JobSettingsView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        if db.jobs(user_id):
            self.add_item(JobSettingsSelect(user_id))
        back = discord.ui.Button(
            label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1
        )
        back.callback = self.back
        self.add_item(back)

    async def back(self, interaction):
        await interaction.response.edit_message(
            content=jobs_text(self.user_id), view=JobsView(self.user_id)
        )


class AnnulChoiceView(ProtectedEconomyView):
    def __init__(self, user_id: int, job_id: int, name: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.job_id = job_id
        self.name = name

    @discord.ui.button(
        label="Зберегти історію", emoji="🗃️", style=discord.ButtonStyle.secondary
    )
    async def keep(self, interaction, button):
        current = db.open_session(self.user_id)
        if current and current["job_id"] == self.job_id:
            return await interaction.response.send_message(
                "❌ Спочатку заверши поточну сесію цієї роботи.", ephemeral=True
            )
        db.annul_job_keep_history(self.user_id, self.job_id)
        await interaction.response.edit_message(
            content=f"🗃️ **{self.name}** анульовано. Історію та статистику збережено.",
            view=EconomyMainView(self.user_id),
        )

    @discord.ui.button(
        label="Видалити з історією", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def delete_all(self, interaction, button):
        await interaction.response.edit_message(
            content=(
                f"⚠️ Видалити **{self.name}** разом з усіма сесіями, "
                "доходами, витратами та цілями цієї роботи?"
            ),
            view=AnnulConfirmView(self.user_id, self.job_id, self.name),
        )

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content="⚙️ **Налаштування робіт**", embed=None,
            view=JobSettingsView(self.user_id),
        )


class AnnulConfirmView(ProtectedEconomyView):
    def __init__(self, user_id: int, job_id: int, name: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.job_id = job_id
        self.name = name

    @discord.ui.button(
        label="Так, видалити все", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):
        db.annul_job_with_history(self.user_id, self.job_id)
        await interaction.response.edit_message(
            content=f"🗑️ **{self.name}** та вся її історія видалені.",
            view=EconomyMainView(self.user_id),
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content=jobs_text(self.user_id), view=JobsView(self.user_id)
        )


def jobs_text(user_id: int) -> str:
    rows = db.all_jobs(user_id)
    if not rows:
        return "💼 **Роботи**\nЩе немає робіт."
    body = "\n".join(
        f"• **{row['name']}** — {'активна' if row['active'] else 'анульована'}"
        for row in rows
    )
    return f"💼 **Роботи**\n{body}"


class JobsView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="Додати роботу", emoji="➕", style=discord.ButtonStyle.success)
    async def add(self, interaction, button):
        await interaction.response.send_modal(AddJobModal(self.user_id))

    @discord.ui.button(
        label="Налаштування", emoji="⚙️", style=discord.ButtonStyle.secondary
    )
    async def settings(self, interaction, button):
        if not db.jobs(self.user_id):
            return await interaction.response.send_message(
                "Немає активних робіт.", ephemeral=True
            )
        await interaction.response.edit_message(
            content="⚙️ **Налаштування робіт**",
            view=JobSettingsView(self.user_id),
        )

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# ---------- Main menu ----------
class EconomyMainView(ProtectedEconomyView):
    def __init__(self, user_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.user_id = user_id
        uid = user_id or ECONOMY_USER_ID

        # Row 0 — work.
        if uid and db.open_session(uid):
            work = discord.ui.Button(label="Поточна робота", emoji="🟢", style=discord.ButtonStyle.success, custom_id="economy:main:current", row=0)
            work.callback = self.current
        else:
            work = discord.ui.Button(label="Почати роботу", emoji="▶️", style=discord.ButtonStyle.primary, custom_id="economy:main:start", row=0)
            work.callback = self.start
        self.add_item(work)

        if uid and not db.open_session(uid):
            last = db.latest_job(uid)
            if last:
                label = (f"{last['name']} знову")[:80]
                quick = discord.ui.Button(
                    label=label, emoji="⚡", style=discord.ButtonStyle.secondary,
                    custom_id="economy:main:quick", row=0
                )
                quick.callback = self.quick_start
                self.add_item(quick)

        manual = discord.ui.Button(label="Записати роботу", emoji="📝", style=discord.ButtonStyle.secondary, custom_id="economy:main:manual", row=0)
        manual.callback = self.manual
        self.add_item(manual)

        # Row 1 — records.
        if uid and db.pending(uid):
            pending = discord.ui.Button(label="Очікують продажу", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="economy:main:pending", row=1)
            pending.callback = self.pending
            self.add_item(pending)

        history = discord.ui.Button(label="Історія", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="economy:main:history", row=1)
        history.callback = self.history
        self.add_item(history)

        # Row 2 — analysis/settings.
        stats = discord.ui.Button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="economy:main:stats", row=2)
        goals = discord.ui.Button(label="Цілі", emoji="🎯", style=discord.ButtonStyle.secondary, custom_id="economy:main:goals", row=2)
        jobs = discord.ui.Button(label="Роботи", emoji="💼", style=discord.ButtonStyle.secondary, custom_id="economy:main:jobs", row=2)
        stats.callback = self.stats
        goals.callback = self.goals
        jobs.callback = self.jobs
        self.add_item(stats)
        self.add_item(goals)
        self.add_item(jobs)

    def uid(self, interaction):
        return self.user_id or interaction.user.id

    async def current(self, interaction):
        uid = self.uid(interaction)
        row = db.open_session(uid)
        if not row:
            return await interaction.response.send_message("Поточної роботи немає.", ephemeral=True)
        await interaction.response.edit_message(content=None, embed=session_embed(row), view=SessionView(uid, row["id"]))

    async def start(self, interaction):
        uid = self.uid(interaction)
        if db.open_session(uid):
            return await interaction.response.send_message("❌ Уже є поточна робота.", ephemeral=True)
        if not db.jobs(uid):
            return await interaction.response.send_message("Спочатку додай хоча б одну роботу через **Роботи**.", ephemeral=True)
        await interaction.response.edit_message(content="▶️ **Оберіть роботу**", embed=None, view=JobSelectView(uid))

    async def quick_start(self, interaction):
        uid = self.uid(interaction)
        if db.open_session(uid):
            return await interaction.response.send_message("❌ Уже є поточна робота.", ephemeral=True)
        last = db.latest_job(uid)
        if not last:
            return await interaction.response.send_message("Немає попередньої роботи для швидкого старту.", ephemeral=True)
        try:
            sid = db.start_session(uid, last["id"])
        except RuntimeError:
            return await interaction.response.send_message("❌ Уже є поточна робота.", ephemeral=True)
        row = db.session(sid, uid)
        await interaction.response.edit_message(
            content=None, embed=session_embed(row), view=SessionView(uid, sid)
        )

    async def manual(self, interaction):
        uid = self.uid(interaction)
        if not db.jobs(uid):
            return await interaction.response.send_message("Спочатку додай хоча б одну роботу через **Роботи**.", ephemeral=True)
        await interaction.response.edit_message(content="📝 **Оберіть роботу для ручного запису**", embed=None, view=ManualJobSelectView(uid))

    async def pending(self, interaction):
        uid = self.uid(interaction)
        rows = db.pending(uid)
        if not rows:
            return await interaction.response.send_message("📦 Немає записів, що очікують продажу.", ephemeral=True)
        await interaction.response.edit_message(content=None, embed=pending_overview_embed(uid), view=PendingView(uid))

    async def history(self, interaction):
        uid = self.uid(interaction)
        if db.history_count(uid) == 0:
            return await interaction.response.send_message("📋 Історія поки порожня.", ephemeral=True)
        await interaction.response.edit_message(content=None, embed=history_embed(uid, 0), view=HistoryView(uid, 0))

    async def stats(self, interaction):
        uid = self.uid(interaction)
        await interaction.response.edit_message(content=None, embed=stats_embed(uid, 30, "30 днів"), view=StatsView(uid, 30, "30 днів"))

    async def goals(self, interaction):
        uid = self.uid(interaction)
        await interaction.response.edit_message(content=None, embed=goals_embed(uid, False), view=GoalsView(uid))

    async def jobs(self, interaction):
        uid = self.uid(interaction)
        await interaction.response.edit_message(content=jobs_text(uid), embed=None, view=JobsView(uid))


# ---------- Entry points expected by bot.py ----------
def economy_access_allowed(interaction: discord.Interaction) -> bool:
    return owner_allowed(interaction)


async def _find_panel_message(bot):
    global _panel_message_id
    if not ECONOMY_CHANNEL_ID:
        return None
    channel = bot.get_channel(ECONOMY_CHANNEL_ID)
    if channel is None:
        return None

    stored = db.get_meta("panel_message_id")
    if not _panel_message_id and stored:
        try:
            _panel_message_id = int(stored)
        except (TypeError, ValueError):
            db.set_meta("panel_message_id", None)

    if _panel_message_id:
        try:
            return await channel.fetch_message(_panel_message_id)
        except discord.DiscordException:
            _panel_message_id = None
            db.set_meta("panel_message_id", None)

    # Recovery for installs that existed before panel_message_id was stored.
    async for message in channel.history(limit=100):
        if message.author.id != bot.user.id:
            continue
        has_main_button = any(
            (getattr(component, "custom_id", "") or "").startswith("economy:main:")
            for row in message.components
            for component in row.children
        ) if message.components else False
        footer = ""
        if message.embeds and message.embeds[0].footer:
            footer = message.embeds[0].footer.text or ""
        if has_main_button or footer.startswith("Economy v"):
            _panel_message_id = message.id
            db.set_meta("panel_message_id", str(message.id))
            return message
    return None


async def _refresh_panel_once(bot):
    message = await _find_panel_message(bot)
    if not message or not ECONOMY_USER_ID:
        return

    footer = ""
    if message.embeds and message.embeds[0].footer:
        footer = message.embeds[0].footer.text or ""

    if "• main" in footer:
        await message.edit(embed=main_embed(ECONOMY_USER_ID))
        return

    if "• session:" in footer:
        try:
            sid = int(footer.rsplit("• session:", 1)[1].strip())
        except (ValueError, IndexError):
            return
        row = db.session(sid, ECONOMY_USER_ID)
        if row and row["status"] in ("working", "paused", "pending_sale"):
            # Only refresh the embed. Discord keeps the existing buttons/selects.
            await message.edit(embed=session_embed(row))


async def _panel_refresh_loop(bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(PANEL_REFRESH_SECONDS)
            await _refresh_panel_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[ECONOMY] Panel refresh error: {exc}")


def _start_panel_refresher(bot):
    global _panel_refresh_task
    if _panel_refresh_task and not _panel_refresh_task.done():
        return
    _panel_refresh_task = asyncio.create_task(_panel_refresh_loop(bot))


async def open_economy(interaction: discord.Interaction):
    if not economy_access_allowed(interaction):
        return await deny_interaction(interaction)

    try:
        result = await ensure_economy_panel(interaction.client)
    except Exception as exc:
        print(f"[ECONOMY] Manual panel restore error: {exc}")
        return await interaction.response.send_message(
            "❌ Не вдалося відновити панель Economy. Перевір права бота на перегляд "
            "каналу, надсилання повідомлень і редагування власних повідомлень.",
            ephemeral=True,
        )

    if result == "created":
        message = "✅ Панель Economy була видалена — я створив нову."
    elif result == "updated":
        message = "✅ Панель Economy оновлено."
    else:
        message = "⚠️ Не вдалося знайти канал Economy."

    await interaction.response.send_message(message, ephemeral=True)


def register_commands(bot):
    @bot.tree.command(name="economy", description="Оновити постійну панель Economy")
    async def economy_command(interaction: discord.Interaction):
        await open_economy(interaction)


async def restore_active_views(bot):
    bot.add_view(EconomyMainView())


async def ensure_economy_panel(bot):
    global _panel_message_id
    if not ECONOMY_CHANNEL_ID or not ECONOMY_USER_ID:
        print("[ECONOMY] ECONOMY_CHANNEL_ID / ECONOMY_USER_ID is not set; panel disabled")
        return None

    channel = bot.get_channel(ECONOMY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ECONOMY_CHANNEL_ID)
        except discord.DiscordException:
            print(f"[ECONOMY] Channel {ECONOMY_CHANNEL_ID} not found")
            return None

    try:
        message = await _find_panel_message(bot)
        if message:
            await message.edit(
                content=None,
                embed=main_embed(ECONOMY_USER_ID),
                view=EconomyMainView(ECONOMY_USER_ID),
            )
            _panel_message_id = message.id
            db.set_meta("panel_message_id", str(message.id))
            print(f"[ECONOMY] Persistent panel updated in channel {ECONOMY_CHANNEL_ID}")
            result = "updated"
        else:
            # The saved panel message may have been deleted manually.
            # Create a fresh one and remember its new message ID.
            message = await channel.send(
                embed=main_embed(ECONOMY_USER_ID),
                view=EconomyMainView(ECONOMY_USER_ID),
            )
            _panel_message_id = message.id
            db.set_meta("panel_message_id", str(message.id))
            print(f"[ECONOMY] Persistent panel recreated in channel {ECONOMY_CHANNEL_ID}")
            result = "created"

        _start_panel_refresher(bot)
        return result
    except discord.DiscordException as exc:
        print(f"[ECONOMY] Could not ensure panel: {exc}")
        raise

