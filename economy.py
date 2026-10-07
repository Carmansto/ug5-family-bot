import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import discord

from config import DB_PATH, LOCAL_TZ, GUILD_ID, ECONOMY_CHANNEL_ID, ECONOMY_USER_ID


# Economy v2
# Clean DB schema. If you used the old economy.py, delete/rename economy.db before first launch.
_DB_DIR = Path(DB_PATH).expanduser().resolve().parent
ECONOMY_DB_PATH = str(_DB_DIR / "economy.db")


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
    cleaned = value.replace(" ", "").replace(",", "").replace("_", "").replace("$", "")
    amount = int(cleaned)
    if amount <= 0:
        raise ValueError
    return amount


class EconomyDB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        # Economy v2: one-time clean reset of the old Economy database.
        # PRAGMA user_version is stored inside economy.db, so this runs only once.
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript("""
            DROP TABLE IF EXISTS transactions;
            DROP TABLE IF EXISTS work_sessions;
            DROP TABLE IF EXISTS farms;
            DROP TABLE IF EXISTS jobs;
            """)
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys=ON")

        self.conn.executescript("""
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

        CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, active);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON work_sessions(user_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_tx_session ON transactions(session_id, kind);
        PRAGMA user_version=2;
        """)
        self.conn.commit()

    def jobs(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE user_id=? AND active=1 ORDER BY name COLLATE NOCASE",
            (user_id,),
        ).fetchall()

    def add_job(self, user_id: int, name: str, income_mode: str):
        self.conn.execute(
            "INSERT INTO jobs(user_id,name,income_mode,created_at) VALUES(?,?,?,?)",
            (user_id, name.strip(), income_mode, now_iso()),
        )
        self.conn.commit()

    def archive_job(self, user_id: int, job_id: int):
        self.conn.execute(
            "UPDATE jobs SET active=0 WHERE id=? AND user_id=?", (job_id, user_id)
        )
        self.conn.commit()

    def open_session(self, user_id: int):
        return self.conn.execute("""
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND s.status IN ('working','paused')
            ORDER BY s.id DESC LIMIT 1
        """, (user_id,)).fetchone()

    def session(self, session_id: int, user_id: int):
        return self.conn.execute("""
            SELECT s.*, j.name AS job_name, j.income_mode
            FROM work_sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.id=? AND s.user_id=?
        """, (session_id, user_id)).fetchone()

    def start_session(self, user_id: int, job_id: int) -> int:
        if self.open_session(user_id):
            raise RuntimeError("open_session")
        cur = self.conn.execute("""
            INSERT INTO work_sessions(
                user_id,job_id,status,worked_seconds,current_started_at,created_at
            ) VALUES(?,?, 'working',0,?,?)
        """, (user_id, job_id, now_iso(), now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def create_manual_session(self, user_id: int, job_id: int, worked_seconds: int) -> int:
        if worked_seconds <= 0:
            raise ValueError("worked_seconds")
        stamp = now_iso()
        cur = self.conn.execute("""
            INSERT INTO work_sessions(
                user_id,job_id,status,worked_seconds,current_started_at,
                work_finished_at,created_at
            ) VALUES(?,?, 'pending_sale',?,NULL,?,?)
        """, (user_id, job_id, int(worked_seconds), stamp, stamp))
        self.conn.commit()
        return cur.lastrowid

    def worked_seconds(self, row) -> int:
        total = int(row["worked_seconds"] or 0)
        if row["status"] == "working" and row["current_started_at"]:
            total += max(0, int((now_dt() - parse_dt(row["current_started_at"])).total_seconds()))
        return total

    def pause(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "working":
            return
        total = self.worked_seconds(row)
        self.conn.execute("""
            UPDATE work_sessions
            SET status='paused', worked_seconds=?, current_started_at=NULL
            WHERE id=? AND user_id=?
        """, (total, session_id, user_id))
        self.conn.commit()

    def resume(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "paused":
            return
        self.conn.execute("""
            UPDATE work_sessions SET status='working', current_started_at=?
            WHERE id=? AND user_id=?
        """, (now_iso(), session_id, user_id))
        self.conn.commit()

    def finish_work(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] not in ("working", "paused"):
            return
        total = self.worked_seconds(row)
        # Even "instant" jobs stay pending until the user explicitly confirms
        # that all money for this work has been entered.
        self.conn.execute("""
            UPDATE work_sessions
            SET status='pending_sale', worked_seconds=?, current_started_at=NULL,
                work_finished_at=?
            WHERE id=? AND user_id=?
        """, (total, now_iso(), session_id, user_id))
        self.conn.commit()

    def complete(self, session_id: int, user_id: int):
        row = self.session(session_id, user_id)
        if not row or row["status"] != "pending_sale":
            return
        self.conn.execute("""
            UPDATE work_sessions SET status='completed', completed_at=?
            WHERE id=? AND user_id=?
        """, (now_iso(), session_id, user_id))
        self.conn.commit()

    def cancel(self, session_id: int, user_id: int):
        self.conn.execute(
            "DELETE FROM work_sessions WHERE id=? AND user_id=? AND status IN ('working','paused')",
            (session_id, user_id),
        )
        self.conn.commit()

    def add_transaction(self, session_id: int, user_id: int, kind: str, amount: int, note=None):
        self.conn.execute("""
            INSERT INTO transactions(session_id,user_id,kind,amount,note,created_at)
            VALUES(?,?,?,?,?,?)
        """, (session_id, user_id, kind, amount, note, now_iso()))
        self.conn.commit()

    def totals(self, session_id: int):
        row = self.conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income,
                COALESCE(SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END),0) expense
            FROM transactions WHERE session_id=?
        """, (session_id,)).fetchone()
        return int(row["income"]), int(row["expense"])

    def pending(self, user_id: int):
        return self.conn.execute("""
            SELECT s.*, j.name AS job_name
            FROM work_sessions s JOIN jobs j ON j.id=s.job_id
            WHERE s.user_id=? AND s.status='pending_sale'
            ORDER BY COALESCE(s.work_finished_at,s.created_at) DESC
        """, (user_id,)).fetchall()

    def stats(self, user_id: int, days: Optional[int]):
        # Money/profit/hourly rate: COMPLETED records only.
        # Worked time: all work that has actually been finished, including pending sale.
        params = [user_id]
        date_filter = ""
        if days is not None:
            start = (now_dt().replace(hour=0, minute=0, second=0, microsecond=0)
                     if days == 1 else now_dt() - timedelta(days=days))
            date_filter = " AND COALESCE(s.work_finished_at,s.created_at)>=?"
            params.append(start.isoformat())

        time_row = self.conn.execute(f"""
            SELECT COALESCE(SUM(s.worked_seconds),0) sec
            FROM work_sessions s
            WHERE s.user_id=? AND s.status IN ('pending_sale','completed') {date_filter}
        """, params).fetchone()

        params2 = [user_id]
        completed_filter = ""
        if days is not None:
            start = (now_dt().replace(hour=0, minute=0, second=0, microsecond=0)
                     if days == 1 else now_dt() - timedelta(days=days))
            completed_filter = " AND s.completed_at>=?"
            params2.append(start.isoformat())

        money_row = self.conn.execute(f"""
            SELECT
                COALESCE(SUM(CASE WHEN t.kind='income' THEN t.amount ELSE 0 END),0) income,
                COALESCE(SUM(CASE WHEN t.kind='expense' THEN t.amount ELSE 0 END),0) expense,
                COALESCE(SUM(s.worked_seconds),0) completed_sec
            FROM work_sessions s
            LEFT JOIN transactions t ON t.session_id=s.id
            WHERE s.user_id=? AND s.status='completed' {completed_filter}
        """, params2).fetchone()

        # Avoid multiplying worked_seconds by number of transactions.
        sec_row = self.conn.execute(f"""
            SELECT COALESCE(SUM(s.worked_seconds),0) sec
            FROM work_sessions s
            WHERE s.user_id=? AND s.status='completed' {completed_filter}
        """, params2).fetchone()

        pending_count = self.conn.execute(
            "SELECT COUNT(*) c FROM work_sessions WHERE user_id=? AND status='pending_sale'",
            (user_id,),
        ).fetchone()["c"]

        return {
            "worked_seconds": int(time_row["sec"]),
            "income": int(money_row["income"]),
            "expense": int(money_row["expense"]),
            "completed_seconds": int(sec_row["sec"]),
            "pending_count": int(pending_count),
        }


db = EconomyDB(ECONOMY_DB_PATH)


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
        title=f"💼 {row['job_name']}",
        description=state[0],
        color=state[1],
    )
    embed.add_field(name="⏱️ Час", value=duration_text(db.worked_seconds(row)), inline=False)
    embed.add_field(name="💰 Отримано", value=f"{money(income)} $", inline=True)
    embed.add_field(name="💸 Витрати", value=f"{money(expense)} $", inline=True)
    embed.add_field(name="📈 Результат", value=f"{money(profit)} $", inline=True)
    return embed


def main_embed(user_id: int) -> discord.Embed:
    current = db.open_session(user_id)
    pending_count = len(db.pending(user_id))
    embed = discord.Embed(title="💰 ECONOMY", color=discord.Color.green())
    if current:
        icon = "🟢" if current["status"] == "working" else "⏸️"
        embed.add_field(
            name="Поточна робота",
            value=f"{icon} **{current['job_name']}**\n⏱️ {duration_text(db.worked_seconds(current))}",
            inline=False,
        )
    else:
        embed.add_field(name="Поточна робота", value="Немає", inline=False)
    embed.add_field(name="📦 Очікують продажу", value=str(pending_count), inline=False)
    return embed


class AmountModal(discord.ui.Modal):
    def __init__(self, session_id: int, user_id: int, kind: str):
        super().__init__(title="Додати дохід" if kind == "income" else "Додати витрату")
        self.session_id = session_id
        self.user_id = user_id
        self.kind = kind
        self.amount = discord.ui.TextInput(label="Сума", placeholder="Наприклад: 350000")
        self.note = discord.ui.TextInput(
            label="Коментар", required=False, max_length=100,
            placeholder="Необов'язково"
        )
        self.add_item(self.amount)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = parse_amount(str(self.amount.value))
        except (ValueError, TypeError):
            return await interaction.response.send_message("❌ Введи коректну суму.", ephemeral=True)

        row = db.session(self.session_id, self.user_id)
        if not row or row["status"] == "completed":
            return await interaction.response.send_message("❌ Цей запис уже закритий.", ephemeral=True)

        db.add_transaction(
            self.session_id, self.user_id, self.kind, amount,
            str(self.note.value).strip() or None
        )
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            embed=session_embed(row), view=SessionView(self.user_id, self.session_id)
        )


class SessionView(discord.ui.View):
    def __init__(self, user_id: int, session_id: int):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.session_id = session_id
        row = db.session(session_id, user_id)
        status = row["status"] if row else "completed"

        if status in ("working", "paused", "pending_sale"):
            income = discord.ui.Button(label="Дохід", emoji="💰", style=discord.ButtonStyle.success)
            expense = discord.ui.Button(label="Витрата", emoji="💸", style=discord.ButtonStyle.danger)
            income.callback = self.add_income
            expense.callback = self.add_expense
            self.add_item(income)
            self.add_item(expense)

        if status == "working":
            b = discord.ui.Button(label="Зупинити роботу", emoji="⏸️", style=discord.ButtonStyle.secondary, row=1)
            b.callback = self.pause
            self.add_item(b)
        elif status == "paused":
            b = discord.ui.Button(label="Продовжити", emoji="▶️", style=discord.ButtonStyle.success, row=1)
            b.callback = self.resume
            self.add_item(b)
            f = discord.ui.Button(label="Завершити роботу", emoji="✅", style=discord.ButtonStyle.primary, row=1)
            f.callback = self.finish
            self.add_item(f)
        elif status == "pending_sale":
            done = discord.ui.Button(label="Все продано", emoji="✅", style=discord.ButtonStyle.primary, row=1)
            done.callback = self.complete
            self.add_item(done)

        if status in ("working", "paused"):
            cancel = discord.ui.Button(label="Скасувати", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
            cancel.callback = self.cancel
            self.add_item(cancel)

        back = discord.ui.Button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
        back.callback = self.back
        self.add_item(back)

    async def add_income(self, interaction):
        await interaction.response.send_modal(AmountModal(self.session_id, self.user_id, "income"))

    async def add_expense(self, interaction):
        await interaction.response.send_modal(AmountModal(self.session_id, self.user_id, "expense"))

    async def pause(self, interaction):
        db.pause(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(embed=session_embed(row), view=SessionView(self.user_id, self.session_id))

    async def resume(self, interaction):
        if db.open_session(self.user_id) and db.open_session(self.user_id)["id"] != self.session_id:
            return await interaction.response.send_message("❌ Уже є інша поточна робота.", ephemeral=True)
        db.resume(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(embed=session_embed(row), view=SessionView(self.user_id, self.session_id))

    async def finish(self, interaction):
        db.finish_work(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(embed=session_embed(row), view=SessionView(self.user_id, self.session_id))

    async def complete(self, interaction):
        db.complete(self.session_id, self.user_id)
        row = db.session(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="✅ Запис повністю закрито.",
            embed=session_embed(row),
            view=EconomyMainView(self.user_id),
        )

    async def cancel(self, interaction):
        db.cancel(self.session_id, self.user_id)
        await interaction.response.edit_message(
            content="🗑️ Роботу скасовано.", embed=main_embed(self.user_id),
            view=EconomyMainView(self.user_id)
        )

    async def back(self, interaction):
        await interaction.response.edit_message(
            content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id)
        )


class JobSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        jobs = db.jobs(user_id)
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["id"]),
                description="Дохід одразу" if row["income_mode"] == "instant" else "Продаж пізніше",
            )
            for row in jobs[:25]
        ]
        super().__init__(
            placeholder="Оберіть роботу",
            min_values=1, max_values=1,
            options=options or [discord.SelectOption(label="Спочатку додай роботу", value="0")],
        )

    async def callback(self, interaction: discord.Interaction):
        job_id = int(self.values[0])
        if not job_id:
            return await interaction.response.send_message("Спочатку додай роботу.", ephemeral=True)
        try:
            session_id = db.start_session(self.user_id, job_id)
        except RuntimeError:
            return await interaction.response.send_message("❌ У тебе вже є поточна робота.", ephemeral=True)
        row = db.session(session_id, self.user_id)
        await interaction.response.edit_message(
            content=None, embed=session_embed(row), view=SessionView(self.user_id, session_id)
        )


class JobSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(JobSelect(user_id))


class ManualTimeModal(discord.ui.Modal, title="Ручний запис роботи"):
    hours = discord.ui.TextInput(label="Години", placeholder="Наприклад: 3", default="0", max_length=3)
    minutes = discord.ui.TextInput(label="Хвилини", placeholder="Наприклад: 30", default="0", max_length=2)

    def __init__(self, user_id: int, job_id: int):
        super().__init__()
        self.user_id = user_id
        self.job_id = job_id

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

        sid = db.create_manual_session(self.user_id, self.job_id, hours * 3600 + minutes * 60)
        row = db.session(sid, self.user_id)
        await interaction.response.edit_message(
            content="📝 Ручний запис створено. Тепер можеш додати доходи та витрати.",
            embed=session_embed(row),
            view=SessionView(self.user_id, sid),
        )


class ManualJobSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        jobs = db.jobs(user_id)
        options = [
            discord.SelectOption(label=row["name"][:100], value=str(row["id"]))
            for row in jobs[:25]
        ]
        super().__init__(
            placeholder="Оберіть роботу для ручного запису",
            min_values=1, max_values=1,
            options=options or [discord.SelectOption(label="Спочатку додай роботу", value="0")],
        )

    async def callback(self, interaction: discord.Interaction):
        job_id = int(self.values[0])
        if not job_id:
            return await interaction.response.send_message("Спочатку додай роботу.", ephemeral=True)
        await interaction.response.send_modal(ManualTimeModal(self.user_id, job_id))


class ManualJobSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(ManualJobSelect(user_id))


class AddJobModal(discord.ui.Modal, title="Нова робота"):
    name = discord.ui.TextInput(label="Назва", placeholder="Наприклад: Каменяр", max_length=50)

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Оберіть тип доходу для нової роботи:",
            view=IncomeModeView(self.user_id, str(self.name.value).strip()),
            ephemeral=True,
        )


class IncomeModeView(discord.ui.View):
    def __init__(self, user_id: int, name: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.name = name

    @discord.ui.button(label="Одразу", emoji="💵", style=discord.ButtonStyle.success)
    async def instant(self, interaction, button):
        db.add_job(self.user_id, self.name, "instant")
        await interaction.response.edit_message(content=f"✅ Додано роботу **{self.name}**.", view=None)

    @discord.ui.button(label="Продаж пізніше", emoji="📦", style=discord.ButtonStyle.primary)
    async def later(self, interaction, button):
        db.add_job(self.user_id, self.name, "later")
        await interaction.response.edit_message(content=f"✅ Додано роботу **{self.name}**.", view=None)


class PendingSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        rows = db.pending(user_id)
        options = []
        for row in rows[:25]:
            inc, exp = db.totals(row["id"])
            options.append(discord.SelectOption(
                label=row["job_name"][:100],
                value=str(row["id"]),
                description=f"Отримано {money(inc)} $ • витрати {money(exp)} $"[:100],
            ))
        super().__init__(placeholder="Оберіть запис", options=options)

    async def callback(self, interaction):
        sid = int(self.values[0])
        row = db.session(sid, self.user_id)
        await interaction.response.edit_message(
            content=None, embed=session_embed(row), view=SessionView(self.user_id, sid)
        )


class PendingView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        rows = db.pending(user_id)
        if rows:
            self.add_item(PendingSelect(user_id))

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id)
        )


class StatsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def show(self, interaction, days, label):
        s = db.stats(self.user_id, days)
        profit = s["income"] - s["expense"]
        hourly = round(profit / (s["completed_seconds"] / 3600)) if s["completed_seconds"] else 0
        embed = discord.Embed(title=f"📊 Статистика — {label}", color=discord.Color.blue())
        embed.add_field(name="⏱️ Відпрацьовано", value=duration_text(s["worked_seconds"]), inline=False)
        embed.add_field(name="💰 Дохід", value=f"{money(s['income'])} $", inline=True)
        embed.add_field(name="💸 Витрати", value=f"{money(s['expense'])} $", inline=True)
        embed.add_field(name="📈 Чистими", value=f"{money(profit)} $", inline=True)
        embed.add_field(name="⚡ Чистими / год", value=f"{money(hourly)} $", inline=False)
        embed.add_field(name="📦 Очікують продажу", value=str(s["pending_count"]), inline=False)
        embed.set_footer(text="Гроші та $/год рахуються тільки по записах, де натиснуто «Все продано».")
        await interaction.response.edit_message(embed=embed, view=StatsView(self.user_id))

    @discord.ui.button(label="Сьогодні", style=discord.ButtonStyle.primary)
    async def today(self, interaction, button): await self.show(interaction, 1, "сьогодні")

    @discord.ui.button(label="7 днів", style=discord.ButtonStyle.secondary)
    async def week(self, interaction, button): await self.show(interaction, 7, "7 днів")

    @discord.ui.button(label="30 днів", style=discord.ButtonStyle.secondary)
    async def month(self, interaction, button): await self.show(interaction, 30, "30 днів")

    @discord.ui.button(label="Весь час", style=discord.ButtonStyle.secondary)
    async def all_time(self, interaction, button): await self.show(interaction, None, "весь час")

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            embed=main_embed(self.user_id), view=EconomyMainView(self.user_id)
        )


class EconomyMainView(discord.ui.View):
    def __init__(self, user_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.user_id = user_id

    def uid(self, interaction):
        return self.user_id or interaction.user.id

    @discord.ui.button(label="Поточна робота", emoji="🟢", style=discord.ButtonStyle.success,
                       custom_id="economy:main:current")
    async def current(self, interaction, button):
        uid = self.uid(interaction)
        row = db.open_session(uid)
        if not row:
            return await interaction.response.send_message("Поточної роботи немає.", ephemeral=True)
        await interaction.response.send_message(
            embed=session_embed(row), view=SessionView(uid, row["id"]), ephemeral=True
        )

    @discord.ui.button(label="Почати роботу", emoji="▶️", style=discord.ButtonStyle.primary,
                       custom_id="economy:main:start")
    async def start(self, interaction, button):
        uid = self.uid(interaction)
        if db.open_session(uid):
            return await interaction.response.send_message("❌ Спочатку заверши поточну роботу.", ephemeral=True)
        if not db.jobs(uid):
            return await interaction.response.send_message(
                "Спочатку додай хоча б одну роботу через **Роботи**.", ephemeral=True
            )
        await interaction.response.send_message("Оберіть роботу:", view=JobSelectView(uid), ephemeral=True)

    @discord.ui.button(label="Записати роботу", emoji="📝", style=discord.ButtonStyle.secondary,
                       custom_id="economy:main:manual", row=0)
    async def manual(self, interaction, button):
        uid = self.uid(interaction)
        if not db.jobs(uid):
            return await interaction.response.send_message(
                "Спочатку додай хоча б одну роботу через **Роботи**.", ephemeral=True
            )
        await interaction.response.send_message(
            "📝 Обери роботу, яку хочеш записати вручну:",
            view=ManualJobSelectView(uid), ephemeral=True
        )

    @discord.ui.button(label="Очікують продажу", emoji="📦", style=discord.ButtonStyle.secondary,
                       custom_id="economy:main:pending", row=1)
    async def pending_btn(self, interaction, button):
        uid = self.uid(interaction)
        rows = db.pending(uid)
        if not rows:
            return await interaction.response.send_message("📦 Немає записів, що очікують продажу.", ephemeral=True)
        await interaction.response.send_message(
            f"📦 **Очікують продажу: {len(rows)}**",
            view=PendingView(uid), ephemeral=True
        )

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary,
                       custom_id="economy:main:stats", row=1)
    async def stats_btn(self, interaction, button):
        uid = self.uid(interaction)
        s = db.stats(uid, 30)
        profit = s["income"] - s["expense"]
        hourly = round(profit / (s["completed_seconds"] / 3600)) if s["completed_seconds"] else 0
        embed = discord.Embed(title="📊 Статистика — 30 днів", color=discord.Color.blue())
        embed.add_field(name="⏱️ Відпрацьовано", value=duration_text(s["worked_seconds"]), inline=False)
        embed.add_field(name="💰 Дохід", value=f"{money(s['income'])} $", inline=True)
        embed.add_field(name="💸 Витрати", value=f"{money(s['expense'])} $", inline=True)
        embed.add_field(name="📈 Чистими", value=f"{money(profit)} $", inline=True)
        embed.add_field(name="⚡ Чистими / год", value=f"{money(hourly)} $", inline=False)
        embed.add_field(name="📦 Очікують продажу", value=str(s["pending_count"]), inline=False)
        embed.set_footer(text="Фінанси враховуються лише після «Все продано».")
        await interaction.response.send_message(embed=embed, view=StatsView(uid), ephemeral=True)

    @discord.ui.button(label="Роботи", emoji="💼", style=discord.ButtonStyle.secondary,
                       custom_id="economy:main:jobs", row=2)
    async def jobs_btn(self, interaction, button):
        uid = self.uid(interaction)
        jobs = db.jobs(uid)
        text = "\n".join(
            f"• **{r['name']}** — {'дохід одразу' if r['income_mode']=='instant' else 'продаж пізніше'}"
            for r in jobs
        ) or "Ще немає робіт."
        await interaction.response.send_message(
            f"💼 **Роботи**\n{text}",
            view=JobsView(uid), ephemeral=True
        )


class JobsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="Додати роботу", emoji="➕", style=discord.ButtonStyle.success)
    async def add(self, interaction, button):
        await interaction.response.send_modal(AddJobModal(self.user_id))

    @discord.ui.button(label="Меню", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=None, embed=main_embed(self.user_id), view=EconomyMainView(self.user_id)
        )


def economy_access_allowed(interaction: discord.Interaction) -> bool:
    if ECONOMY_USER_ID and interaction.user.id != ECONOMY_USER_ID:
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
    bot.add_view(EconomyMainView())


async def ensure_economy_panel(bot):
    if not ECONOMY_CHANNEL_ID or not ECONOMY_USER_ID:
        print("[ECONOMY] ECONOMY_CHANNEL_ID / ECONOMY_USER_ID is not set; panel disabled")
        return

    channel = bot.get_channel(ECONOMY_CHANNEL_ID)
    if channel is None:
        print(f"[ECONOMY] Channel {ECONOMY_CHANNEL_ID} not found")
        return

    try:
        async for message in channel.history(limit=50):
            if message.author.id == bot.user.id and message.components:
                if any(
                    (getattr(component, "custom_id", "") or "").startswith("economy:main:")
                    for row in message.components for component in row.children
                ):
                    return

        await channel.send(
            embed=discord.Embed(
                title="💰 ECONOMY",
                description=(
                    "Особистий облік роботи та заробітку.\n\n"
                    "▶️ **Почати роботу** — запустити облік часу\n"
                    "📝 **Записати роботу** — внести минулу роботу вручну\n"
                    "📦 **Очікують продажу** — незакриті результати робіт\n"
                    "📊 **Статистика** — час і фінальний заробіток\n"
                    "💼 **Роботи** — налаштування робіт"
                ),
                color=discord.Color.green(),
            ),
            view=EconomyMainView(),
        )
        print(f"[ECONOMY] Panel created in channel {ECONOMY_CHANNEL_ID}")
    except discord.DiscordException as exc:
        print(f"[ECONOMY] Could not ensure panel: {exc}")
