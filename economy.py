import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import discord

from config import DB_PATH, LOCAL_TZ, GUILD_ID, ECONOMY_CHANNEL_ID, ECONOMY_USER_ID


_DB_DIR = Path(DB_PATH).expanduser().resolve().parent
ECONOMY_DB_PATH = str(_DB_DIR / "economy.db")
ECONOMY_UI_VERSION = "3.0"


def now_dt() -> datetime:
    return datetime.now(LOCAL_TZ).replace(microsecond=0)


def now_iso() -> str:
    return now_dt().isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def duration_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h} год {m:02d} хв"
    return f"{m} хв"


def parse_amount(value: str) -> int:
    cleaned = (
        value.replace(" ", "")
        .replace(",", "")
        .replace("_", "")
        .replace("$", "")
    )
    amount = int(cleaned)
    if amount <= 0:
        raise ValueError
    return amount


def period_start(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    if days == 1:
        start = now_dt().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now_dt() - timedelta(days=days)
    return start.isoformat()


def progress_bar(current: int, target: int, size: int = 12) -> str:
    if target <= 0:
        return "░" * size
    ratio = max(0.0, min(1.0, current / target))
    filled = round(ratio * size)
    return "█" * filled + "░" * (size - filled)


class EconomyDB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _init(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]

        # Old pre-v2 Economy is incompatible. v2+ is migrated in place.
        if version < 2:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript(
                """
                DROP TABLE IF EXISTS transactions;
                DROP TABLE IF EXISTS work_sessions;
                DROP TABLE IF EXISTS farms;
                DROP TABLE IF EXISTS jobs;
                DROP TABLE IF EXISTS goals;
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
            """
        )

        if "note" not in self._columns("work_sessions"):
            self.conn.execute("ALTER TABLE work_sessions ADD COLUMN note TEXT")

        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                metric TEXT NOT NULL CHECK(metric IN ('income','profit')),
                target_amount INTEGER NOT NULL CHECK(target_amount > 0),
                job_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','completed')),
                created_at TEXT NOT NULL,
                completed_at TEXT,
                completed_value INTEGER,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, active);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON work_sessions(user_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_job ON work_sessions(job_id, status);
            CREATE INDEX IF NOT EXISTS idx_tx_session ON transactions(session_id, kind);
            CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id, status, created_at);
            PRAGMA user_version=3;
            """
        )
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
            "SELECT * FROM jobs WHERE user_id=? AND id=?",
            (user_id, job_id),
        ).fetchone()

    def add_job(self, user_id: int, name: str, income_mode: str):
        self.conn.execute(
            "INSERT INTO jobs(user_id,name,income_mode,created_at) VALUES(?,?,?,?)",
            (user_id, name.strip(), income_mode, now_iso()),
        )
        self.conn.commit()

    def annul_job_keep_history(self, user_id: int, job_id: int):
        self.conn.execute(
            "UPDATE jobs SET active=0 WHERE user_id=? AND id=?",
            (user_id, job_id),
        )
        self.conn.commit()

    def annul_job_with_history(self, user_id: int, job_id: int):
        try:
            self.conn.execute("BEGIN")
            # Goals for the job are removed by ON DELETE CASCADE.
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
                    f"DELETE FROM transactions WHERE session_id IN ({marks})",
                    session_ids,
                )
                self.conn.execute(
                    f"DELETE FROM work_sessions WHERE id IN ({marks})",
                    session_ids,
                )
            self.conn.execute(
                "DELETE FROM goals WHERE user_id=? AND job_id=?", (user_id, job_id)
            )
            self.conn.execute(
                "DELETE FROM jobs WHERE user_id=? AND id=?", (user_id, job_id)
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- Sessions ----------

    def open_session(self, user_id: int):
        return self.conn.execute(
            """
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND s.status IN ('working','paused')
            ORDER BY s.id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()

    def session(self, session_id: int, user_id: int):
        return self.conn.execute(
            """
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.id=? AND s.user_id=?
            """,
            (session_id, user_id),
        ).fetchone()

    def history(self, user_id: int, limit: int = 25):
        return self.conn.execute(
            """
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=?
            ORDER BY s.id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    def start_session(self, user_id: int, job_id: int) -> int:
        if self.open_session(user_id):
            raise RuntimeError("open_session")
        stamp = now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO work_sessions(
                user_id,job_id,status,worked_seconds,current_started_at,created_at,note
            ) VALUES(?,?, 'working',0,?,?,NULL)
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
                work_finished_at,completed_at,created_at,note
            ) VALUES(?,?,?,?,NULL,?,?,?,NULL)
            """,
            (user_id, job_id, status, int(worked_seconds), stamp, completed_at, stamp),
        )
        self.conn.commit()
        return cur.lastrowid

    def worked_seconds(self, row) -> int:
        total = int(row["worked_seconds"] or 0)
        if row["status"] == "working" and row["current_started_at"]:
            total += max(
                0,
                int((now_dt() - parse_dt(row["current_started_at"])).total_seconds()),
            )
        return total

    def pause(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "working":
            return
        total = self.worked_seconds(row)
        self.conn.execute(
            """
            UPDATE work_sessions
            SET status='paused', worked_seconds=?, current_started_at=NULL
            WHERE id=? AND user_id=?
            """,
            (total, session_id, user_id),
        )
        self.conn.commit()

    def resume(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "paused":
            return
        self.conn.execute(
            """
            UPDATE work_sessions SET status='working', current_started_at=?
            WHERE id=? AND user_id=?
            """,
            (now_iso(), session_id, user_id),
        )
        self.conn.commit()

    def finish_work(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] not in ("working", "paused"):
            return
        total = self.worked_seconds(row)
        self.conn.execute(
            """
            UPDATE work_sessions
            SET status='pending_sale', worked_seconds=?, current_started_at=NULL,
                work_finished_at=?
            WHERE id=? AND user_id=?
            """,
            (total, now_iso(), session_id, user_id),
        )
        self.conn.commit()

    def complete(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "pending_sale":
            return
        self.conn.execute(
            """
            UPDATE work_sessions SET status='completed', completed_at=?
            WHERE id=? AND user_id=?
            """,
            (now_iso(), session_id, user_id),
        )
        self.conn.commit()

    def set_session_note(self, session_id: int, user_id: int, note: Optional[str]):
        cleaned = (note or "").strip() or None
        self.conn.execute(
            "UPDATE work_sessions SET note=? WHERE id=? AND user_id=?",
            (cleaned, session_id, user_id),
        )
        self.conn.commit()

    def delete_session(self, session_id: int, user_id: int):
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "DELETE FROM transactions WHERE session_id=? AND user_id=?",
                (session_id, user_id),
            )
            self.conn.execute(
                "DELETE FROM work_sessions WHERE id=? AND user_id=?",
                (session_id, user_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def cancel(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] not in ("working", "paused"):
            return
        self.delete_session(session_id, user_id)

    # ---------- Transactions ----------

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
            (session_id, user_id, kind, amount, note, now_iso()),
        )
        self.conn.commit()

    def transactions(self, session_id: int):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()

    def transaction(self, session_id: int, transaction_id: int):
        return self.conn.execute(
            "SELECT * FROM transactions WHERE id=? AND session_id=?",
            (transaction_id, session_id),
        ).fetchone()

    def update_transaction(
        self,
        transaction_id: int,
        session_id: int,
        user_id: int,
        amount: int,
        note: Optional[str],
    ):
        self.conn.execute(
            """
            UPDATE transactions SET amount=?, note=?
            WHERE id=? AND session_id=? AND user_id=?
            """,
            (amount, (note or "").strip() or None, transaction_id, session_id, user_id),
        )
        self.conn.commit()

    def delete_transaction(self, transaction_id: int, session_id: int, user_id: int):
        self.conn.execute(
            "DELETE FROM transactions WHERE id=? AND session_id=? AND user_id=?",
            (transaction_id, session_id, user_id),
        )
        self.conn.commit()

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

    def pending(self, user_id: int):
        return self.conn.execute(
            """
            SELECT s.*, j.name AS job_name
            FROM work_sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND s.status='pending_sale'
            ORDER BY COALESCE(s.work_finished_at,s.created_at) DESC
            """,
            (user_id,),
        ).fetchall()

    def pending_totals(self, user_id: int, days: Optional[int] = None):
        start = period_start(days)
        extra = ""
        params: list = [user_id]
        if start:
            extra = " AND COALESCE(s.work_finished_at,s.created_at)>=?"
            params.append(start)
        row = self.conn.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN t.kind='income' THEN t.amount ELSE 0 END),0) income,
                COALESCE(SUM(CASE WHEN t.kind='expense' THEN t.amount ELSE 0 END),0) expense
            FROM work_sessions s
            LEFT JOIN transactions t ON t.session_id=s.id
            WHERE s.user_id=? AND s.status='pending_sale' {extra}
            """,
            params,
        ).fetchone()
        return int(row["income"]), int(row["expense"])

    # ---------- Statistics ----------

    def stats(self, user_id: int, days: Optional[int]):
        start = period_start(days)

        time_extra = ""
        time_params: list = [user_id]
        if start:
            time_extra = " AND COALESCE(s.work_finished_at,s.created_at)>=?"
            time_params.append(start)

        time_row = self.conn.execute(
            f"""
            SELECT COALESCE(SUM(s.worked_seconds),0) sec
            FROM work_sessions s
            WHERE s.user_id=? AND s.status IN ('pending_sale','completed') {time_extra}
            """,
            time_params,
        ).fetchone()

        completed_extra = ""
        completed_params: list = [user_id]
        if start:
            completed_extra = " AND s.completed_at>=?"
            completed_params.append(start)

        money_row = self.conn.execute(
            f"""
            WITH tx AS (
                SELECT session_id,
                       SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) income,
                       SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) expense
                FROM transactions GROUP BY session_id
            )
            SELECT
                COALESCE(SUM(COALESCE(tx.income,0)),0) income,
                COALESCE(SUM(COALESCE(tx.expense,0)),0) expense,
                COALESCE(SUM(s.worked_seconds),0) completed_sec,
                COUNT(*) completed_count
            FROM work_sessions s
            LEFT JOIN tx ON tx.session_id=s.id
            WHERE s.user_id=? AND s.status='completed' {completed_extra}
            """,
            completed_params,
        ).fetchone()

        pending_count = self.conn.execute(
            "SELECT COUNT(*) c FROM work_sessions WHERE user_id=? AND status='pending_sale'",
            (user_id,),
        ).fetchone()["c"]
        pending_income, pending_expense = self.pending_totals(user_id, days)

        return {
            "worked_seconds": int(time_row["sec"]),
            "income": int(money_row["income"]),
            "expense": int(money_row["expense"]),
            "completed_seconds": int(money_row["completed_sec"]),
            "completed_count": int(money_row["completed_count"]),
            "pending_count": int(pending_count),
            "pending_income": pending_income,
            "pending_expense": pending_expense,
        }

    def stats_by_job(self, user_id: int, days: Optional[int]):
        start = period_start(days)
        extra = ""
        params: list = [user_id]
        if start:
            extra = " AND s.completed_at>=?"
            params.append(start)
        return self.conn.execute(
            f"""
            WITH tx AS (
                SELECT session_id,
                       SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) income,
                       SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) expense
                FROM transactions GROUP BY session_id
            )
            SELECT
                j.id job_id,
                j.name job_name,
                COUNT(s.id) sessions,
                COALESCE(SUM(s.worked_seconds),0) seconds,
                COALESCE(SUM(COALESCE(tx.income,0)),0) income,
                COALESCE(SUM(COALESCE(tx.expense,0)),0) expense
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            LEFT JOIN tx ON tx.session_id=s.id
            WHERE s.user_id=? AND s.status='completed' {extra}
            GROUP BY j.id,j.name
            ORDER BY (COALESCE(SUM(COALESCE(tx.income,0)),0)-COALESCE(SUM(COALESCE(tx.expense,0)),0)) DESC
            """,
            params,
        ).fetchall()

    def job_session_stats(self, user_id: int, job_id: int, days: Optional[int]):
        start = period_start(days)
        extra = ""
        params: list = [user_id, job_id]
        if start:
            extra = " AND s.completed_at>=?"
            params.append(start)
        return self.conn.execute(
            f"""
            WITH tx AS (
                SELECT session_id,
                       SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) income,
                       SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) expense
                FROM transactions GROUP BY session_id
            )
            SELECT s.*, j.name job_name,
                   COALESCE(tx.income,0) income,
                   COALESCE(tx.expense,0) expense
            FROM work_sessions s
            JOIN jobs j ON j.id=s.job_id
            LEFT JOIN tx ON tx.session_id=s.id
            WHERE s.user_id=? AND s.job_id=? AND s.status='completed' {extra}
            ORDER BY s.completed_at DESC
            """,
            params,
        ).fetchall()

    # ---------- Goals ----------

    def create_goal(
        self,
        user_id: int,
        metric: str,
        target_amount: int,
        job_id: Optional[int],
        title: str,
    ):
        if metric not in ("income", "profit") or target_amount <= 0:
            raise ValueError("goal")
        self.conn.execute(
            """
            INSERT INTO goals(user_id,title,metric,target_amount,job_id,status,created_at)
            VALUES(?,?,?,?,?,'active',?)
            """,
            (user_id, title.strip(), metric, target_amount, job_id, now_iso()),
        )
        self.conn.commit()

    def goals(self, user_id: int, status: str = "active"):
        return self.conn.execute(
            """
            SELECT g.*, j.name job_name
            FROM goals g LEFT JOIN jobs j ON j.id=g.job_id
            WHERE g.user_id=? AND g.status=?
            ORDER BY g.id DESC
            """,
            (user_id, status),
        ).fetchall()

    def goal(self, user_id: int, goal_id: int):
        return self.conn.execute(
            """
            SELECT g.*, j.name job_name
            FROM goals g LEFT JOIN jobs j ON j.id=g.job_id
            WHERE g.user_id=? AND g.id=?
            """,
            (user_id, goal_id),
        ).fetchone()

    def goal_progress(self, goal_row) -> int:
        params: list = [goal_row["user_id"], goal_row["created_at"]]
        job_filter = ""
        if goal_row["job_id"]:
            job_filter = " AND s.job_id=?"
            params.append(goal_row["job_id"])
        row = self.conn.execute(
            f"""
            WITH tx AS (
                SELECT session_id,
                       SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) income,
                       SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) expense
                FROM transactions GROUP BY session_id
            )
            SELECT
                COALESCE(SUM(COALESCE(tx.income,0)),0) income,
                COALESCE(SUM(COALESCE(tx.expense,0)),0) expense
            FROM work_sessions s
            LEFT JOIN tx ON tx.session_id=s.id
            WHERE s.user_id=? AND s.status='completed'
              AND s.completed_at>=? {job_filter}
            """,
            params,
        ).fetchone()
        income = int(row["income"])
        expense = int(row["expense"])
        return income if goal_row["metric"] == "income" else income - expense

    def refresh_goals(self, user_id: int):
        for goal in self.goals(user_id, "active"):
            value = self.goal_progress(goal)
            if value >= int(goal["target_amount"]):
                self.conn.execute(
                    """
                    UPDATE goals
                    SET status='completed', completed_at=?, completed_value=?
                    WHERE id=? AND user_id=?
                    """,
                    (now_iso(), value, goal["id"], user_id),
                )
        self.conn.commit()

    def delete_goal(self, user_id: int, goal_id: int):
        self.conn.execute(
            "DELETE FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)
        )
        self.conn.commit()


db = EconomyDB(ECONOMY_DB_PATH)


async def economy_owner_check(interaction: discord.Interaction) -> bool:
    if not ECONOMY_USER_ID or interaction.user.id != ECONOMY_USER_ID:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "🔒 Цією Economy може користуватися тільки власник.", ephemeral=True
            )
        return False
    return True


class ProtectedEconomyView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await economy_owner_check(interaction)


class ProtectedEconomyModal(discord.ui.Modal):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await economy_owner_check(interaction)


def session_embed(row) -> discord.Embed:
    income, expense = db.totals(row["id"])
    profit = income - expense
    state = {
        "working": ("🟢 Робота активна", discord.Color.green()),
        "paused": ("⏸️ Роботу зупинено", discord.Color.gold()),
        "pending_sale": ("📦 Очікує продажу", discord.Color.orange()),
        "completed": ("✅ Завершено", discord.Color.blue()),
    }[row["status"]]
    embed = discord.Embed(
        title=f"💼 {row['job_name']}", description=state[0], color=state[1]
    )
    embed.add_field(
        name="⏱️ Час", value=duration_text(db.worked_seconds(row)), inline=False
    )
    embed.add_field(name="💰 Отримано", value=f"{money(income)} $", inline=True)
    embed.add_field(name="💸 Витрати", value=f"{money(expense)} $", inline=True)
    embed.add_field(name="📈 Результат", value=f"{money(profit)} $", inline=True)
    if row["note"]:
        embed.add_field(name="📝 Нотатка", value=row["note"][:1024], inline=False)
    return embed


def session_details_embed(row) -> discord.Embed:
    embed = session_embed(row)
    embed.add_field(
        name="📅 Дата",
        value=parse_dt(row["created_at"]).strftime("%d.%m.%Y %H:%M"),
        inline=False,
    )
    txs = db.transactions(row["id"])
    if txs:
        lines = []
        for tx in txs[-12:]:
            sign = "+" if tx["kind"] == "income" else "-"
            note = f" — {tx['note']}" if tx["note"] else ""
            lines.append(f"{sign}{money(tx['amount'])} ${note}")
        if len(txs) > 12:
            lines.insert(0, f"… ще {len(txs)-12} операцій")
        embed.add_field(name="🧾 Операції", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🧾 Операції", value="Немає", inline=False)
    income, expense = db.totals(row["id"])
    sec = db.worked_seconds(row)
    if row["status"] == "completed" and sec:
        hourly = round((income - expense) / (sec / 3600))
        embed.add_field(
            name="⚡ Чистими / год", value=f"{money(hourly)} $", inline=False
        )
    return embed


def main_embed(user_id: int) -> discord.Embed:
    current = db.open_session(user_id)
    pending_rows = db.pending(user_id)
    pending_income, pending_expense = db.pending_totals(user_id)

    embed = discord.Embed(
        title="💰 ECONOMY",
        description="Оберіть дію нижче.",
        color=discord.Color.green(),
    )
    if current:
        income, expense = db.totals(current["id"])
        icon = "🟢" if current["status"] == "working" else "⏸️"
        state = (
            "Робота активна"
            if current["status"] == "working"
            else "Роботу зупинено"
        )
        embed.add_field(
            name=f"{icon} {current['job_name']}",
            value=(
                f"{state}\n"
                f"⏱️ {duration_text(db.worked_seconds(current))}\n"
                f"💰 {money(income)} $  •  💸 {money(expense)} $"
            ),
            inline=False,
        )
    if pending_rows:
        embed.add_field(
            name="📦 Очікують продажу",
            value=(
                f"{len(pending_rows)} запис(и)\n"
                f"💰 Уже отримано: {money(pending_income)} $\n"
                f"💸 Витрати: {money(pending_expense)} $"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Economy v{ECONOMY_UI_VERSION}")
    return embed


# =========================
# Transactions / session UI
# =========================


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
        self.amount = discord.ui.TextInput(label="Сума", placeholder="Наприклад: 350000")
        self.note = discord.ui.TextInput(
            label="Коментар", required=False, max_length=100, placeholder="Необов'язково"
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
        target_view = (
            CompletedManualView(self.user_id, self.session_id)
            if self.allow_completed
            else SessionView(self.user_id, self.session_id)
        )
        await interaction.response.edit_message(embed=session_embed(row), view=target_view)


class NoteModal(ProtectedEconomyModal, title="Нотатка до сесії"):
    def __init__(self, user_id: int, session_id: int, return_to_history: bool = True):
        super().__init__()
        self.user_id = user_id
        self.session_id = session_id
        self.return_to_history = return_to_history
        row = db.session(session_id, user_id)
        self.note = discord.ui.TextInput(
            label="Нотатка",
            required=False,
            max_length=200,
            default=(row["note"] or "") if row else "",
            placeholder="Наприклад: x2, вечір, з другом",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        db.set_session_note(self.session_id, self.user_id, str(self.note.value))
        row = db.session(self.session_id, self.user_id)
        view = (
            HistorySessionView(self.user_id, self.session_id)
            if self.return_to_history
            else SessionView(self.user_id, self.session_id)
        )
        embed = session_details_embed(row) if self.return_to_history else session_embed(row)
        await interaction.response.edit_message(
            content="✅ Нотатку оновлено.", embed=embed, view=view
        )


class SessionView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.session_id = session_id
        row = db.session(session_id, user_id)
        status = row["status"] if row else "completed"

        if status in ("working", "paused", "pending_sale"):
            income = discord.ui.Button(
                label="Дохід", emoji="💰", style=discord.ButtonStyle.success, row=0
            )
            expense = discord.ui.Button(
                label="Витрата", emoji="💸", style=discord.ButtonStyle.danger, row=0
            )
            income.callback = self.add_income
            expense.callback = self.add_expense
            self.add_item(income)
            self.add_item(expense)

        if status == "working":
            button = discord.ui.Button(
                label="Зупинити роботу",
                emoji="⏸️",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            button.callback = self.pause
            self.add_item(button)
        elif status == "paused":
            resume = discord.ui.Button(
                label="Продовжити",
                emoji="▶️",
                style=discord.ButtonStyle.success,
                row=1,
            )
            resume.callback = self.resume
            self.add_item(resume)
            finish = discord.ui.Button(
                label="Завершити роботу",
                emoji="✅",
                style=discord.ButtonStyle.primary,
                row=1,
            )
            finish.callback = self.finish
            self.add_item(finish)
        elif status == "pending_sale":
            done = discord.ui.Button(
                label="Все продано",
                emoji="✅",
                style=discord.ButtonStyle.primary,
                row=1,
            )
            done.callback = self.complete
            self.add_item(done)

        note = discord.ui.Button(
            label="Нотатка", emoji="📝", style=discord.ButtonStyle.secondary, row=2
        )
        note.callback = self.note
        self.add_item(note)

        if status in ("working", "paused"):
            cancel = discord.ui.Button(
                label="Скасувати",
                emoji="🗑️",
                style=discord.ButtonStyle.danger,
                row=2,
            )
            cancel.callback = self.cancel
            self.add_item(cancel)

        back = discord.ui.Button(
            label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=3
        )
        back.callback = self.back
        self.add_item(back)

    async def add_income(self, interaction):
        await interaction.response.send_modal(
            AmountModal(self.session_id, self.user_id, "income")
        )

    async def add_expense(self, interaction):
        await interaction.response.send_modal(
            AmountModal(self.session_id, self.user_id, "expense")
        )

    async def note(self, interaction):
        await interaction.response.send_modal(
            NoteModal(self.user_id, self.session_id, return_to_history=False)
        )

    async def pause(self, interaction):
        db.pause(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            embed=session_embed(row), view=SessionView(self.user_id, self.session_id)
        )

    async def resume(self, interaction):
        current = db.open_session(self.user_id)
        if current and current["id"] != self.session_id:
            return await interaction.response.send_message(
                "❌ Уже є інша поточна робота.", ephemeral=True
            )
        db.resume(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            embed=session_embed(row), view=SessionView(self.user_id, self.session_id)
        )

    async def finish(self, interaction):
        db.finish_work(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            embed=session_embed(row), view=SessionView(self.user_id, self.session_id)
        )

    async def complete(self, interaction):
        db.complete(self.session_id, self.user_id)
        db.refresh_goals(self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="✅ Запис повністю закрито.",
            embed=session_embed(row),
            view=EconomyMainView(self.user_id),
        )

    async def cancel(self, interaction):
        db.cancel(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="🗑️ Роботу скасовано.",
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )

    async def back(self, interaction):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# =========================
# Start / manual recording
# =========================


class JobSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        jobs = db.jobs(user_id)
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
            for row in jobs[:25]
        ]
        super().__init__(
            placeholder="Оберіть роботу",
            min_values=1,
            max_values=1,
            options=options
            or [discord.SelectOption(label="Спочатку додай роботу", value="0")],
        )

    async def callback(self, interaction: discord.Interaction):
        job_id = int(self.values[0])
        if not job_id:
            return await interaction.response.send_message(
                "Спочатку додай роботу.", ephemeral=True
            )
        try:
            session_id = db.start_session(self.user_id, job_id)
        except RuntimeError:
            return await interaction.response.send_message(
                "❌ У тебе вже є поточна робота.", ephemeral=True
            )
        row = db.session(session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_embed(row),
            view=SessionView(self.user_id, session_id),
        )


class JobSelectView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(JobSelect(user_id))


class ManualTimeModal(ProtectedEconomyModal, title="Ручний запис роботи"):
    hours = discord.ui.TextInput(label="Години", default="0", max_length=3)
    minutes = discord.ui.TextInput(label="Хвилини", default="0", max_length=2)

    def __init__(self, user_id: int, job_id: int):
        super().__init__()
        self.user_id = user_id
        self.job_id = job_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hours = int(str(self.hours.value) or 0)
            minutes = int(str(self.minutes.value) or 0)
            if hours < 0 or minutes < 0 or minutes > 59 or hours == minutes == 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Вкажи коректний час.", ephemeral=True
            )
        seconds = hours * 3600 + minutes * 60
        await interaction.response.send_message(
            f"📝 Час: **{duration_text(seconds)}**\nЯк записати сесію?",
            view=ManualChoiceView(self.user_id, self.job_id, seconds),
            ephemeral=True,
        )


class ManualChoiceView(ProtectedEconomyView):
    def __init__(self, user_id: int, job_id: int, seconds: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.job_id = job_id
        self.seconds = seconds

    @discord.ui.button(
        label="Очікують продажу", emoji="📦", style=discord.ButtonStyle.primary
    )
    async def pending(self, interaction, button):
        session_id = db.create_manual_session(
            self.user_id, self.job_id, self.seconds, "pending_sale"
        )
        row = db.session(session_id, self.user_id)
        await interaction.response.edit_message(
            content="📦 Запис створено.",
            embed=session_embed(row),
            view=SessionView(self.user_id, session_id),
        )

    @discord.ui.button(label="Записати", emoji="✅", style=discord.ButtonStyle.success)
    async def record(self, interaction, button):
        session_id = db.create_manual_session(
            self.user_id, self.job_id, self.seconds, "completed"
        )
        row = db.session(session_id, self.user_id)
        await interaction.response.edit_message(
            content="✅ Фінальний запис створено.",
            embed=session_embed(row),
            view=CompletedManualView(self.user_id, session_id),
        )


class CompletedManualView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.session_id = session_id

        income = discord.ui.Button(
            label="Дохід", emoji="💰", style=discord.ButtonStyle.success, row=0
        )
        expense = discord.ui.Button(
            label="Витрата", emoji="💸", style=discord.ButtonStyle.danger, row=0
        )
        income.callback = self.income
        expense.callback = self.expense
        self.add_item(income)
        self.add_item(expense)

        note = discord.ui.Button(
            label="Нотатка", emoji="📝", style=discord.ButtonStyle.secondary, row=1
        )
        note.callback = self.note
        self.add_item(note)

        done = discord.ui.Button(
            label="Готово", emoji="✅", style=discord.ButtonStyle.primary, row=1
        )
        done.callback = self.done
        self.add_item(done)

    async def income(self, interaction):
        await interaction.response.send_modal(
            AmountModal(self.session_id, self.user_id, "income", True)
        )

    async def expense(self, interaction):
        await interaction.response.send_modal(
            AmountModal(self.session_id, self.user_id, "expense", True)
        )

    async def note(self, interaction):
        await interaction.response.send_modal(
            NoteModal(self.user_id, self.session_id, return_to_history=False)
        )

    async def done(self, interaction):
        db.refresh_goals(self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="✅ Запис збережено.",
            embed=session_embed(row),
            view=EconomyMainView(self.user_id),
        )


class ManualJobSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in db.jobs(user_id)[:25]
        ]
        super().__init__(placeholder="Оберіть роботу", options=options)

    async def callback(self, interaction):
        await interaction.response.send_modal(
            ManualTimeModal(self.user_id, int(self.values[0]))
        )


class ManualJobSelectView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(ManualJobSelect(user_id))


# =========================
# Jobs settings
# =========================


class AddJobModal(ProtectedEconomyModal, title="Нова робота"):
    name = discord.ui.TextInput(
        label="Назва", placeholder="Наприклад: Каменяр", max_length=50
    )

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Оберіть тип доходу для нової роботи:",
            view=IncomeModeView(self.user_id, str(self.name.value).strip()),
            ephemeral=True,
        )


class IncomeModeView(ProtectedEconomyView):
    def __init__(self, user_id: int, name: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.name = name

    @discord.ui.button(label="Одразу", emoji="💵", style=discord.ButtonStyle.success)
    async def instant(self, interaction, button):
        db.add_job(self.user_id, self.name, "instant")
        await interaction.response.edit_message(
            content=f"✅ Додано роботу **{self.name}**.", view=None
        )

    @discord.ui.button(
        label="Продаж пізніше", emoji="📦", style=discord.ButtonStyle.primary
    )
    async def later(self, interaction, button):
        db.add_job(self.user_id, self.name, "later")
        await interaction.response.edit_message(
            content=f"✅ Додано роботу **{self.name}**.", view=None
        )


class JobSettingsSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(
            placeholder="Оберіть роботу",
            options=[
                discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
                for row in db.jobs(user_id)[:25]
            ],
        )

    async def callback(self, interaction):
        job_id = int(self.values[0])
        row = db.job(self.user_id, job_id)
        await interaction.response.edit_message(
            content=f"⚙️ **{row['name']}**\nЩо зробити?",
            view=AnnulChoiceView(self.user_id, job_id, row["name"]),
        )


class JobSettingsView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(JobSettingsSelect(user_id))


class AnnulChoiceView(ProtectedEconomyView):
    def __init__(self, user_id: int, job_id: int, name: str):
        super().__init__(timeout=180)
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
                "❌ Спочатку заверши поточну сесію.", ephemeral=True
            )
        db.annul_job_keep_history(self.user_id, self.job_id)
        await interaction.response.edit_message(
            content=f"🗃️ **{self.name}** анульовано, історію збережено.",
            view=EconomyMainView(self.user_id),
        )

    @discord.ui.button(
        label="Видалити з історією", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def wipe(self, interaction, button):
        await interaction.response.edit_message(
            content=f"⚠️ Видалити **{self.name}** і всю історію?",
            view=AnnulConfirmView(self.user_id, self.job_id, self.name),
        )


class AnnulConfirmView(ProtectedEconomyView):
    def __init__(self, user_id: int, job_id: int, name: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.job_id = job_id
        self.name = name

    @discord.ui.button(
        label="Так, видалити все", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def yes(self, interaction, button):
        db.annul_job_with_history(self.user_id, self.job_id)
        await interaction.response.edit_message(
            content=f"🗑️ **{self.name}** та історію видалено.",
            view=EconomyMainView(self.user_id),
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def no(self, interaction, button):
        await interaction.response.edit_message(
            content="Скасовано.", view=EconomyMainView(self.user_id)
        )


class JobsView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(
        label="Додати роботу", emoji="➕", style=discord.ButtonStyle.success
    )
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
            content="⚙️ **Налаштування робіт**", view=JobSettingsView(self.user_id)
        )

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# =========================
# Pending sales
# =========================


class PendingSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        options = []
        for row in db.pending(user_id)[:25]:
            income, expense = db.totals(row["id"])
            options.append(
                discord.SelectOption(
                    label=row["job_name"][:100],
                    value=str(row["id"]),
                    description=(
                        f"Отримано {money(income)} $ • витрати {money(expense)} $"
                    )[:100],
                )
            )
        super().__init__(placeholder="Оберіть запис", options=options)

    async def callback(self, interaction):
        session_id = int(self.values[0])
        row = db.session(session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_embed(row),
            view=SessionView(self.user_id, session_id),
        )


class PendingView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        if db.pending(user_id):
            self.add_item(PendingSelect(user_id))

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# =========================
# History + operation editing
# =========================


def transaction_embed(row, session_row) -> discord.Embed:
    kind = "Дохід" if row["kind"] == "income" else "Витрата"
    sign = "+" if row["kind"] == "income" else "-"
    color = discord.Color.green() if row["kind"] == "income" else discord.Color.red()
    embed = discord.Embed(
        title=f"🧾 {kind}",
        description=f"**{sign}{money(row['amount'])} $**",
        color=color,
    )
    embed.add_field(name="Робота", value=session_row["job_name"], inline=True)
    embed.add_field(
        name="Дата", value=parse_dt(row["created_at"]).strftime("%d.%m.%Y %H:%M"), inline=True
    )
    embed.add_field(name="Коментар", value=row["note"] or "—", inline=False)
    return embed


class EditTransactionModal(ProtectedEconomyModal, title="Редагувати операцію"):
    def __init__(self, user_id: int, session_id: int, transaction_id: int):
        super().__init__()
        self.user_id = user_id
        self.session_id = session_id
        self.transaction_id = transaction_id
        row = db.transaction(session_id, transaction_id)
        self.amount = discord.ui.TextInput(
            label="Сума", default=str(row["amount"]), max_length=16
        )
        self.note = discord.ui.TextInput(
            label="Коментар",
            required=False,
            default=row["note"] or "",
            max_length=100,
        )
        self.add_item(self.amount)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = parse_amount(str(self.amount.value))
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "❌ Некоректна сума.", ephemeral=True
            )
        db.update_transaction(
            self.transaction_id,
            self.session_id,
            self.user_id,
            amount,
            str(self.note.value),
        )
        tx = db.transaction(self.session_id, self.transaction_id)
        session_row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="✅ Операцію оновлено.",
            embed=transaction_embed(tx, session_row),
            view=TransactionDetailView(
                self.user_id, self.session_id, self.transaction_id
            ),
        )


class TransactionSelect(discord.ui.Select):
    def __init__(self, user_id: int, session_id: int):
        self.user_id = user_id
        self.session_id = session_id
        options = []
        for tx in db.transactions(session_id)[:25]:
            sign = "+" if tx["kind"] == "income" else "-"
            label = f"{sign}{money(tx['amount'])} $"
            description = tx["note"] or parse_dt(tx["created_at"]).strftime("%d.%m.%Y %H:%M")
            options.append(
                discord.SelectOption(
                    label=label[:100], value=str(tx["id"]), description=description[:100]
                )
            )
        super().__init__(placeholder="Оберіть операцію", options=options)

    async def callback(self, interaction):
        transaction_id = int(self.values[0])
        tx = db.transaction(self.session_id, transaction_id)
        session_row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=transaction_embed(tx, session_row),
            view=TransactionDetailView(
                self.user_id, self.session_id, transaction_id
            ),
        )


class TransactionsView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.session_id = session_id
        if db.transactions(session_id):
            self.add_item(TransactionSelect(user_id, session_id))

    @discord.ui.button(label="Сесія", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_details_embed(row),
            view=HistorySessionView(self.user_id, self.session_id),
        )


class TransactionDetailView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int, transaction_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.session_id = session_id
        self.transaction_id = transaction_id

    @discord.ui.button(
        label="Редагувати", emoji="✏️", style=discord.ButtonStyle.primary
    )
    async def edit(self, interaction, button):
        await interaction.response.send_modal(
            EditTransactionModal(self.user_id, self.session_id, self.transaction_id)
        )

    @discord.ui.button(label="Видалити", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction, button):
        await interaction.response.edit_message(
            content="⚠️ Видалити цю операцію?",
            view=DeleteTransactionConfirmView(
                self.user_id, self.session_id, self.transaction_id
            ),
        )

    @discord.ui.button(
        label="Операції", emoji="◀️", style=discord.ButtonStyle.secondary, row=1
    )
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content="🧾 **Операції сесії**",
            embed=None,
            view=TransactionsView(self.user_id, self.session_id),
        )


class DeleteTransactionConfirmView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int, transaction_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.session_id = session_id
        self.transaction_id = transaction_id

    @discord.ui.button(
        label="Так, видалити", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):
        db.delete_transaction(self.transaction_id, self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="✅ Операцію видалено.",
            embed=None,
            view=TransactionsView(self.user_id, self.session_id),
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        tx = db.transaction(self.session_id, self.transaction_id)
        session_row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=transaction_embed(tx, session_row),
            view=TransactionDetailView(
                self.user_id, self.session_id, self.transaction_id
            ),
        )


class HistorySelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        icons = {
            "working": "🟢",
            "paused": "⏸️",
            "pending_sale": "📦",
            "completed": "✅",
        }
        options = []
        for row in db.history(user_id):
            note = f" • {row['note']}" if row["note"] else ""
            description = (
                f"{parse_dt(row['created_at']).strftime('%d.%m.%Y')} • "
                f"{duration_text(db.worked_seconds(row))}{note}"
            )
            options.append(
                discord.SelectOption(
                    label=f"{icons[row['status']]} {row['job_name']}"[:100],
                    value=str(row["id"]),
                    description=description[:100],
                )
            )
        super().__init__(placeholder="Оберіть сесію", options=options)

    async def callback(self, interaction):
        session_id = int(self.values[0])
        row = db.session(session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_details_embed(row),
            view=HistorySessionView(self.user_id, session_id),
        )


class HistoryView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        if db.history(user_id):
            self.add_item(HistorySelect(user_id))

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


class HistorySessionView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.session_id = session_id
        row = db.session(session_id, user_id)

        operations = discord.ui.Button(
            label="Операції", emoji="🧾", style=discord.ButtonStyle.primary, row=0
        )
        note = discord.ui.Button(
            label="Нотатка", emoji="📝", style=discord.ButtonStyle.secondary, row=0
        )
        operations.callback = self.operations
        note.callback = self.note
        self.add_item(operations)
        self.add_item(note)

        if row and row["status"] == "pending_sale":
            open_button = discord.ui.Button(
                label="Відкрити продаж", emoji="📦", style=discord.ButtonStyle.success, row=1
            )
            open_button.callback = self.open_session
            self.add_item(open_button)
        elif row and row["status"] in ("working", "paused"):
            open_button = discord.ui.Button(
                label="Відкрити роботу", emoji="🟢", style=discord.ButtonStyle.success, row=1
            )
            open_button.callback = self.open_session
            self.add_item(open_button)

        delete = discord.ui.Button(
            label="Анулювати сесію",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            row=2,
        )
        delete.callback = self.delete_session
        self.add_item(delete)

        back = discord.ui.Button(
            label="Історія", emoji="◀️", style=discord.ButtonStyle.secondary, row=3
        )
        back.callback = self.back
        self.add_item(back)

    async def operations(self, interaction):
        txs = db.transactions(self.session_id)
        if not txs:
            return await interaction.response.send_message(
                "У цій сесії ще немає операцій.", ephemeral=True
            )
        await interaction.response.edit_message(
            content="🧾 **Операції сесії**",
            embed=None,
            view=TransactionsView(self.user_id, self.session_id),
        )

    async def note(self, interaction):
        await interaction.response.send_modal(
            NoteModal(self.user_id, self.session_id, return_to_history=True)
        )

    async def open_session(self, interaction):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_embed(row),
            view=SessionView(self.user_id, self.session_id),
        )

    async def delete_session(self, interaction):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=(
                f"⚠️ Анулювати сесію **{row['job_name']}** від "
                f"{parse_dt(row['created_at']).strftime('%d.%m.%Y')}?\n"
                "Її доходи, витрати та внесок у статистику буде видалено."
            ),
            embed=None,
            view=DeleteSessionConfirmView(self.user_id, self.session_id),
        )

    async def back(self, interaction):
        await interaction.response.edit_message(
            content="📋 **Історія сесій**",
            embed=None,
            view=HistoryView(self.user_id),
        )


class DeleteSessionConfirmView(ProtectedEconomyView):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.session_id = session_id

    @discord.ui.button(
        label="Так, анулювати", emoji="🗑️", style=discord.ButtonStyle.danger
    )
    async def confirm(self, interaction, button):
        db.delete_session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="🗑️ Сесію анульовано.", embed=None, view=HistoryView(self.user_id)
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content=None,
            embed=session_details_embed(row),
            view=HistorySessionView(self.user_id, self.session_id),
        )


# =========================
# Statistics
# =========================


PERIODS = {
    "today": (1, "сьогодні"),
    "week": (7, "7 днів"),
    "month": (30, "30 днів"),
    "all": (None, "весь час"),
}


def stats_embed(user_id: int, days: Optional[int], label: str) -> discord.Embed:
    data = db.stats(user_id, days)
    profit = data["income"] - data["expense"]
    hourly = (
        round(profit / (data["completed_seconds"] / 3600))
        if data["completed_seconds"]
        else 0
    )
    embed = discord.Embed(
        title=f"📊 Статистика — {label}", color=discord.Color.blue()
    )
    embed.add_field(
        name="⏱️ Відпрацьовано", value=duration_text(data["worked_seconds"]), inline=False
    )
    embed.add_field(name="💰 Дохід", value=f"{money(data['income'])} $", inline=True)
    embed.add_field(
        name="💸 Витрати", value=f"{money(data['expense'])} $", inline=True
    )
    embed.add_field(name="📈 Чистими", value=f"{money(profit)} $", inline=True)
    embed.add_field(
        name="⚡ Чистими / год", value=f"{money(hourly)} $", inline=True
    )
    embed.add_field(
        name="✅ Завершено сесій", value=str(data["completed_count"]), inline=True
    )
    embed.add_field(
        name="📦 Очікують продажу", value=str(data["pending_count"]), inline=True
    )
    pending_net = data["pending_income"] - data["pending_expense"]
    embed.add_field(
        name="📦 Незакриті гроші",
        value=(
            f"Отримано: **{money(data['pending_income'])} $**\n"
            f"Витрати: **{money(data['pending_expense'])} $**\n"
            f"Поточний результат: **{money(pending_net)} $**"
        ),
        inline=False,
    )
    embed.set_footer(
        text="Незакриті гроші показуються окремо й не входять у фінальну статистику."
    )
    return embed


def jobs_stats_embed(user_id: int, days: Optional[int], label: str) -> discord.Embed:
    rows = db.stats_by_job(user_id, days)
    embed = discord.Embed(
        title=f"💼 По роботах — {label}", color=discord.Color.blurple()
    )
    if not rows:
        embed.description = "Немає завершених сесій за цей період."
        return embed
    for row in rows[:10]:
        profit = int(row["income"]) - int(row["expense"])
        seconds = int(row["seconds"])
        hourly = round(profit / (seconds / 3600)) if seconds else 0
        embed.add_field(
            name=f"{row['job_name']} • {row['sessions']} сес.",
            value=(
                f"⏱️ {duration_text(seconds)}\n"
                f"📈 {money(profit)} $\n"
                f"⚡ {money(hourly)} $/год"
            ),
            inline=True,
        )
    return embed


def ranking_embed(user_id: int, days: Optional[int], label: str) -> discord.Embed:
    rows = []
    for row in db.stats_by_job(user_id, days):
        seconds = int(row["seconds"])
        profit = int(row["income"]) - int(row["expense"])
        hourly = round(profit / (seconds / 3600)) if seconds else 0
        rows.append((hourly, row["job_name"], int(row["sessions"]), profit, seconds))
    rows.sort(reverse=True, key=lambda x: x[0])
    embed = discord.Embed(
        title=f"🏆 Рейтинг $/год — {label}", color=discord.Color.gold()
    )
    if not rows:
        embed.description = "Немає завершених сесій за цей період."
        return embed
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (hourly, name, sessions, profit, seconds) in enumerate(rows[:10]):
        prefix = medals[idx] if idx < 3 else f"**{idx+1}.**"
        lines.append(
            f"{prefix} **{name}** — {money(hourly)} $/год\n"
            f"└ {sessions} сес. • {duration_text(seconds)} • +{money(profit)} $"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Рейтинг рахується тільки по повністю закритих сесіях.")
    return embed


class JobStatsSelect(discord.ui.Select):
    def __init__(self, user_id: int, days: Optional[int], label: str):
        self.user_id = user_id
        self.days = days
        self.label = label
        rows = db.stats_by_job(user_id, days)
        options = [
            discord.SelectOption(label=row["job_name"][:100], value=str(row["job_id"]))
            for row in rows[:25]
        ]
        super().__init__(placeholder="Деталі конкретної роботи", options=options)

    async def callback(self, interaction):
        job_id = int(self.values[0])
        rows = db.job_session_stats(self.user_id, job_id, self.days)
        job = db.job(self.user_id, job_id)
        income = sum(int(r["income"]) for r in rows)
        expense = sum(int(r["expense"]) for r in rows)
        seconds = sum(int(r["worked_seconds"]) for r in rows)
        profit = income - expense
        hourly = round(profit / (seconds / 3600)) if seconds else 0
        embed = discord.Embed(
            title=f"💼 {job['name']} — {self.label}", color=discord.Color.blurple()
        )
        embed.add_field(name="⏱️ Час", value=duration_text(seconds), inline=False)
        embed.add_field(name="💰 Дохід", value=f"{money(income)} $", inline=True)
        embed.add_field(name="💸 Витрати", value=f"{money(expense)} $", inline=True)
        embed.add_field(name="📈 Чистими", value=f"{money(profit)} $", inline=True)
        embed.add_field(name="⚡ $/год", value=f"{money(hourly)} $", inline=True)
        embed.add_field(name="✅ Сесій", value=str(len(rows)), inline=True)
        if rows:
            best = max(
                rows,
                key=lambda r: (
                    (int(r["income"]) - int(r["expense"]))
                    / max(1, int(r["worked_seconds"]))
                ),
            )
            best_profit = int(best["income"]) - int(best["expense"])
            best_hourly = round(
                best_profit / (int(best["worked_seconds"]) / 3600)
            ) if int(best["worked_seconds"]) else 0
            embed.add_field(
                name="🏆 Найкраща сесія",
                value=(
                    f"{parse_dt(best['completed_at']).strftime('%d.%m.%Y')}\n"
                    f"📈 {money(best_profit)} $ • ⚡ {money(best_hourly)} $/год"
                ),
                inline=False,
            )
        await interaction.response.edit_message(
            embed=embed,
            view=StatsView(self.user_id, self.days, self.label, mode="jobs"),
        )


class StatsView(ProtectedEconomyView):
    def __init__(
        self,
        user_id: int,
        days: Optional[int] = 30,
        label: str = "30 днів",
        mode: str = "general",
    ):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.days = days
        self.label = label
        self.mode = mode

        if mode == "jobs" and db.stats_by_job(user_id, days):
            self.add_item(JobStatsSelect(user_id, days, label))

    async def render(self, interaction, days, label, mode=None):
        mode = mode or self.mode
        if mode == "jobs":
            embed = jobs_stats_embed(self.user_id, days, label)
        elif mode == "ranking":
            embed = ranking_embed(self.user_id, days, label)
        else:
            embed = stats_embed(self.user_id, days, label)
        await interaction.response.edit_message(
            embed=embed,
            view=StatsView(self.user_id, days, label, mode),
        )

    @discord.ui.button(label="Загальна", emoji="📊", style=discord.ButtonStyle.primary, row=1)
    async def general(self, interaction, button):
        await self.render(interaction, self.days, self.label, "general")

    @discord.ui.button(label="По роботах", emoji="💼", style=discord.ButtonStyle.secondary, row=1)
    async def jobs(self, interaction, button):
        await self.render(interaction, self.days, self.label, "jobs")

    @discord.ui.button(label="Рейтинг", emoji="🏆", style=discord.ButtonStyle.secondary, row=1)
    async def ranking(self, interaction, button):
        await self.render(interaction, self.days, self.label, "ranking")

    @discord.ui.button(label="Сьогодні", style=discord.ButtonStyle.secondary, row=2)
    async def today(self, interaction, button):
        await self.render(interaction, 1, "сьогодні")

    @discord.ui.button(label="7 днів", style=discord.ButtonStyle.secondary, row=2)
    async def week(self, interaction, button):
        await self.render(interaction, 7, "7 днів")

    @discord.ui.button(label="30 днів", style=discord.ButtonStyle.secondary, row=2)
    async def month(self, interaction, button):
        await self.render(interaction, 30, "30 днів")

    @discord.ui.button(label="Весь час", style=discord.ButtonStyle.secondary, row=2)
    async def all_time(self, interaction, button):
        await self.render(interaction, None, "весь час")

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            embed=main_embed(self.user_id), view=EconomyMainView(self.user_id)
        )


# =========================
# Goals (money only)
# =========================


def goals_embed(user_id: int, completed: bool = False) -> discord.Embed:
    db.refresh_goals(user_id)
    status = "completed" if completed else "active"
    rows = db.goals(user_id, status)
    title = "✅ Завершені цілі" if completed else "🎯 Цілі"
    embed = discord.Embed(title=title, color=discord.Color.purple())
    if not rows:
        embed.description = "Завершених цілей ще немає." if completed else "Активних цілей немає."
        return embed

    blocks = []
    for goal in rows[:10]:
        if completed:
            value = int(goal["completed_value"] or goal["target_amount"])
        else:
            value = db.goal_progress(goal)
        target = int(goal["target_amount"])
        percent = min(999, round(value / target * 100)) if target else 0
        metric_name = "Дохід" if goal["metric"] == "income" else "Чистими"
        scope = goal["job_name"] or "Усі роботи"
        block = (
            f"**{goal['title']}**\n"
            f"{metric_name} • {scope}\n"
            f"{progress_bar(value, target)} {percent}%\n"
            f"{money(value)} / {money(target)} $"
        )
        if completed and goal["completed_at"]:
            block += f"\n✅ {parse_dt(goal['completed_at']).strftime('%d.%m.%Y')}"
        blocks.append(block)
    embed.description = "\n\n".join(blocks)
    embed.set_footer(text="У прогрес грошей входять тільки повністю закриті сесії.")
    return embed


class GoalAmountModal(ProtectedEconomyModal, title="Нова ціль"):
    def __init__(self, user_id: int, metric: str, job_id: Optional[int]):
        super().__init__()
        self.user_id = user_id
        self.metric = metric
        self.job_id = job_id
        scope = db.job(user_id, job_id)["name"] if job_id else "Усі роботи"
        default_title = ("Дохід" if metric == "income" else "Чистий прибуток") + f" • {scope}"
        self.title_input = discord.ui.TextInput(
            label="Назва цілі",
            default=default_title[:100],
            max_length=100,
        )
        self.amount = discord.ui.TextInput(
            label="Сума цілі", placeholder="Наприклад: 5000000", max_length=16
        )
        self.add_item(self.title_input)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = parse_amount(str(self.amount.value))
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "❌ Введи коректну суму.", ephemeral=True
            )
        db.create_goal(
            self.user_id,
            self.metric,
            target,
            self.job_id,
            str(self.title_input.value),
        )
        await interaction.response.edit_message(
            content="✅ Ціль створено.",
            embed=goals_embed(self.user_id),
            view=GoalsView(self.user_id),
        )


class GoalScopeSelect(discord.ui.Select):
    def __init__(self, user_id: int, metric: str):
        self.user_id = user_id
        self.metric = metric
        options = [
            discord.SelectOption(label="Усі роботи", value="all", emoji="🌐")
        ]
        options.extend(
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]), emoji="💼")
            for row in db.jobs(user_id)[:24]
        )
        super().__init__(placeholder="Для чого ця ціль?", options=options)

    async def callback(self, interaction):
        job_id = None if self.values[0] == "all" else int(self.values[0])
        await interaction.response.send_modal(
            GoalAmountModal(self.user_id, self.metric, job_id)
        )


class GoalScopeView(ProtectedEconomyView):
    def __init__(self, user_id: int, metric: str):
        super().__init__(timeout=180)
        self.add_item(GoalScopeSelect(user_id, metric))


class GoalTypeView(ProtectedEconomyView):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    @discord.ui.button(label="Дохід", emoji="💵", style=discord.ButtonStyle.success)
    async def income(self, interaction, button):
        await interaction.response.edit_message(
            content="🎯 Обери, для яких робіт рахувати ціль:",
            embed=None,
            view=GoalScopeView(self.user_id, "income"),
        )

    @discord.ui.button(
        label="Чистий прибуток", emoji="💰", style=discord.ButtonStyle.primary
    )
    async def profit(self, interaction, button):
        await interaction.response.edit_message(
            content="🎯 Обери, для яких робіт рахувати ціль:",
            embed=None,
            view=GoalScopeView(self.user_id, "profit"),
        )


class GoalManageSelect(discord.ui.Select):
    def __init__(self, user_id: int, status: str):
        self.user_id = user_id
        self.status = status
        options = [
            discord.SelectOption(label=row["title"][:100], value=str(row["id"]))
            for row in db.goals(user_id, status)[:25]
        ]
        super().__init__(placeholder="Оберіть ціль", options=options)

    async def callback(self, interaction):
        goal_id = int(self.values[0])
        goal = db.goal(self.user_id, goal_id)
        await interaction.response.edit_message(
            content=f"🎯 **{goal['title']}**\nВидалити цю ціль?",
            embed=None,
            view=GoalDeleteView(self.user_id, goal_id),
        )


class GoalManageView(ProtectedEconomyView):
    def __init__(self, user_id: int, status: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.status = status
        if db.goals(user_id, status):
            self.add_item(GoalManageSelect(user_id, status))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id, self.status == "completed"),
            view=GoalsView(self.user_id, self.status == "completed"),
        )


class GoalDeleteView(ProtectedEconomyView):
    def __init__(self, user_id: int, goal_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.goal_id = goal_id

    @discord.ui.button(label="Видалити", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction, button):
        db.delete_goal(self.user_id, self.goal_id)
        await interaction.response.edit_message(
            content="🗑️ Ціль видалено.",
            embed=goals_embed(self.user_id),
            view=GoalsView(self.user_id),
        )

    @discord.ui.button(label="Скасувати", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id),
            view=GoalsView(self.user_id),
        )


class GoalsView(ProtectedEconomyView):
    def __init__(self, user_id: int, completed: bool = False):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.completed = completed

    @discord.ui.button(label="Нова ціль", emoji="➕", style=discord.ButtonStyle.success)
    async def add(self, interaction, button):
        await interaction.response.edit_message(
            content="🎯 Що рахувати для цілі?",
            embed=None,
            view=GoalTypeView(self.user_id),
        )

    @discord.ui.button(
        label="Завершені", emoji="✅", style=discord.ButtonStyle.secondary
    )
    async def completed_btn(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id, True),
            view=GoalsView(self.user_id, True),
        )

    @discord.ui.button(label="Активні", emoji="🎯", style=discord.ButtonStyle.secondary)
    async def active_btn(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=goals_embed(self.user_id, False),
            view=GoalsView(self.user_id, False),
        )

    @discord.ui.button(
        label="Керувати", emoji="⚙️", style=discord.ButtonStyle.secondary, row=1
    )
    async def manage(self, interaction, button):
        status = "completed" if self.completed else "active"
        if not db.goals(self.user_id, status):
            return await interaction.response.send_message(
                "Тут немає цілей.", ephemeral=True
            )
        await interaction.response.edit_message(
            content="⚙️ Обери ціль:",
            embed=None,
            view=GoalManageView(self.user_id, status),
        )

    @discord.ui.button(
        label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=1
    )
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None,
            embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id),
        )


# =========================
# Main menu / commands
# =========================


class EconomyMainView(ProtectedEconomyView):
    def __init__(self, user_id=None, persistent: bool = False):
        super().__init__(timeout=None if persistent else 300)
        self.user_id = user_id
        uid = user_id or ECONOMY_USER_ID
        current = db.open_session(uid) if uid else None
        pending_rows = db.pending(uid) if uid else []

        def add(label, emoji, style, custom_id, callback, row):
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=custom_id,
                row=row,
            )
            button.callback = callback
            self.add_item(button)

        if current:
            add(
                "Поточна робота",
                "🟢",
                discord.ButtonStyle.success,
                "economy:main:current",
                self.current,
                0,
            )
        else:
            add(
                "Почати роботу",
                "▶️",
                discord.ButtonStyle.primary,
                "economy:main:start",
                self.start,
                0,
            )
        add(
            "Записати роботу",
            "📝",
            discord.ButtonStyle.secondary,
            "economy:main:manual",
            self.manual,
            0,
        )

        if pending_rows:
            add(
                "Очікують продажу",
                "📦",
                discord.ButtonStyle.secondary,
                "economy:main:pending",
                self.pending,
                1,
            )
        add(
            "Історія",
            "📋",
            discord.ButtonStyle.secondary,
            "economy:main:history",
            self.history,
            1,
        )

        add(
            "Статистика",
            "📊",
            discord.ButtonStyle.secondary,
            "economy:main:stats",
            self.stats,
            2,
        )
        add(
            "Цілі",
            "🎯",
            discord.ButtonStyle.secondary,
            "economy:main:goals",
            self.goals,
            2,
        )
        add(
            "Роботи",
            "💼",
            discord.ButtonStyle.secondary,
            "economy:main:jobs",
            self.jobs,
            2,
        )

    def uid(self, interaction):
        return self.user_id or interaction.user.id

    async def current(self, interaction):
        uid = self.uid(interaction)
        row = db.open_session(uid)
        if not row:
            return await interaction.response.send_message(
                "Поточної роботи немає. Відкрий Economy ще раз — меню оновиться.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=session_embed(row),
            view=SessionView(uid, row["id"]),
            ephemeral=True,
        )

    async def start(self, interaction):
        uid = self.uid(interaction)
        if db.open_session(uid):
            return await interaction.response.send_message(
                "❌ Уже є поточна робота.", ephemeral=True
            )
        if not db.jobs(uid):
            return await interaction.response.send_message(
                "Спочатку додай роботу через **Роботи**.", ephemeral=True
            )
        await interaction.response.send_message(
            "Оберіть роботу:", view=JobSelectView(uid), ephemeral=True
        )

    async def manual(self, interaction):
        uid = self.uid(interaction)
        if not db.jobs(uid):
            return await interaction.response.send_message(
                "Спочатку додай роботу через **Роботи**.", ephemeral=True
            )
        await interaction.response.send_message(
            "📝 Обери роботу для ручного запису:",
            view=ManualJobSelectView(uid),
            ephemeral=True,
        )

    async def pending(self, interaction):
        uid = self.uid(interaction)
        rows = db.pending(uid)
        if not rows:
            return await interaction.response.send_message(
                "📦 Немає записів, що очікують продажу.", ephemeral=True
            )
        income, expense = db.pending_totals(uid)
        await interaction.response.send_message(
            (
                f"📦 **Очікують продажу: {len(rows)}**\n"
                f"💰 Уже отримано: **{money(income)} $**\n"
                f"💸 Витрати: **{money(expense)} $**"
            ),
            view=PendingView(uid),
            ephemeral=True,
        )

    async def history(self, interaction):
        uid = self.uid(interaction)
        if not db.history(uid):
            return await interaction.response.send_message(
                "📋 Історія поки порожня.", ephemeral=True
            )
        await interaction.response.send_message(
            "📋 **Історія сесій**\nОбери сесію, щоб переглянути всі деталі.",
            view=HistoryView(uid),
            ephemeral=True,
        )

    async def stats(self, interaction):
        uid = self.uid(interaction)
        await interaction.response.send_message(
            embed=stats_embed(uid, 30, "30 днів"),
            view=StatsView(uid, 30, "30 днів"),
            ephemeral=True,
        )

    async def goals(self, interaction):
        uid = self.uid(interaction)
        db.refresh_goals(uid)
        await interaction.response.send_message(
            embed=goals_embed(uid), view=GoalsView(uid), ephemeral=True
        )

    async def jobs(self, interaction):
        uid = self.uid(interaction)
        rows = db.all_jobs(uid)
        text = "\n".join(
            f"• **{row['name']}** — {'активна' if row['active'] else 'анульована'}"
            for row in rows
        ) or "Ще немає робіт."
        await interaction.response.send_message(
            f"💼 **Роботи**\n{text}", view=JobsView(uid), ephemeral=True
        )


def economy_access_allowed(interaction: discord.Interaction) -> bool:
    if not ECONOMY_USER_ID or interaction.user.id != ECONOMY_USER_ID:
        return False
    if ECONOMY_CHANNEL_ID and interaction.channel_id != ECONOMY_CHANNEL_ID:
        return False
    return interaction.guild_id == GUILD_ID


async def open_economy(interaction: discord.Interaction):
    if not economy_access_allowed(interaction):
        reason = (
            "🔒 Економіка доступна тільки тобі."
            if ECONOMY_USER_ID and interaction.user.id != ECONOMY_USER_ID
            else "📍 Економіка працює тільки у своєму каналі."
        )
        return await interaction.response.send_message(reason, ephemeral=True)

    await interaction.response.send_message(
        embed=main_embed(interaction.user.id),
        view=EconomyMainView(interaction.user.id),
        ephemeral=True,
    )


def register_commands(bot):
    @bot.tree.command(name="economy", description="Особистий трекер роботи та заробітку")
    async def economy_command(interaction: discord.Interaction):
        await open_economy(interaction)


async def restore_active_views(bot):
    bot.add_view(EconomyMainView(ECONOMY_USER_ID, persistent=True))


def economy_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="💰 ECONOMY",
        description=(
            "Особистий облік роботи та заробітку.\n\n"
            "▶️ **Почати роботу** — запустити облік часу\n"
            "📝 **Записати роботу** — внести сесію вручну\n"
            "📦 **Очікують продажу** — незакриті продажі\n"
            "📋 **Історія** — сесії, операції та нотатки\n"
            "📊 **Статистика** — загальна, по роботах і рейтинг\n"
            "🎯 **Цілі** — дохід або чистий прибуток\n"
            "💼 **Роботи** — список і налаштування"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"Economy v{ECONOMY_UI_VERSION}")
    return embed


async def ensure_economy_panel(bot):
    if not ECONOMY_CHANNEL_ID or not ECONOMY_USER_ID:
        print(
            "[ECONOMY] ECONOMY_CHANNEL_ID / ECONOMY_USER_ID is not set; panel disabled"
        )
        return

    channel = bot.get_channel(ECONOMY_CHANNEL_ID)
    if channel is None:
        print(f"[ECONOMY] Channel {ECONOMY_CHANNEL_ID} not found")
        return

    try:
        async for message in channel.history(limit=50):
            if message.author.id != bot.user.id or not message.components:
                continue
            has_economy = any(
                (getattr(component, "custom_id", "") or "").startswith("economy:main:")
                for row in message.components
                for component in row.children
            )
            if has_economy:
                await message.edit(
                    embed=economy_panel_embed(),
                    view=EconomyMainView(ECONOMY_USER_ID, persistent=True),
                )
                print(f"[ECONOMY] Panel refreshed to v{ECONOMY_UI_VERSION}")
                return

        await channel.send(
            embed=economy_panel_embed(),
            view=EconomyMainView(ECONOMY_USER_ID, persistent=True),
        )
        print(f"[ECONOMY] Panel created in channel {ECONOMY_CHANNEL_ID}")
    except discord.DiscordException as exc:
        print(f"[ECONOMY] Could not ensure panel: {exc}")
