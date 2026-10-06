import sqlite3
from datetime import datetime, timedelta, timezone

from pathlib import Path
from typing import Optional

import discord

from config import DB_PATH, LOCAL_TZ, GUILD_ID, ECONOMY_CHANNEL_ID, ECONOMY_USER_ID

UTC = timezone.utc


# Separate database: never touches the existing Agosto/family economy tables.
_DB_DIR = Path(DB_PATH).expanduser().resolve().parent
ECONOMY_DB_PATH = str(_DB_DIR / "economy.db")


def now_iso() -> str:
  return datetime.now(LOCAL_TZ).replace(microsecond=0).isoformat()


def parse_dt(value: str) -> datetime:
  return datetime.fromisoformat(value)


def period_start(days: Optional[int]) -> Optional[str]:
  if days is None:
    return None
  now = datetime.now(LOCAL_TZ)
  return (now.replace(hour=0, minute=0, second=0, microsecond=0)
          if days == 1 else now-timedelta(days=days)).isoformat()


def money(value: int) -> str:
  return f"{value:,}".replace(",", " ")


def duration_text(seconds: int) -> str:
  seconds = max(0, int(seconds))
  h, rem = divmod(seconds, 3600)
  m, _ = divmod(rem, 60)
  if h:
    return f"{h} год {m:02d} хв"
  return f"{m} хв"


def esc(text: str) -> str:
  return discord.utils.escape_markdown(str(text))


class EconomyDB:
  def __init__(self, path: str):
    self.path = path
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    self.conn = sqlite3.connect(path, check_same_thread=False)
    self.conn.row_factory = sqlite3.Row
    self.conn.execute("PRAGMA journal_mode=WAL")
    self.conn.execute("PRAGMA foreign_keys=ON")
    self._init()

  def _init(self):
    self.conn.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS farms (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      job_id INTEGER NOT NULL,
      started_at TEXT NOT NULL,
      ended_at TEXT,
      status TEXT NOT NULL DEFAULT 'draft',
      source TEXT NOT NULL DEFAULT 'live',
      created_at TEXT NOT NULL,
      FOREIGN KEY(job_id) REFERENCES jobs(id)
    );

    CREATE TABLE IF NOT EXISTS transactions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      farm_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
      amount INTEGER NOT NULL CHECK(amount > 0),
      note TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(farm_id) REFERENCES farms(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, active);
    CREATE INDEX IF NOT EXISTS idx_farms_user ON farms(user_id, status, started_at);
    CREATE INDEX IF NOT EXISTS idx_tx_farm ON transactions(farm_id, kind);
    CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, created_at);
    """)
    self.conn.commit()

  def jobs(self, user_id: int):
    return self.conn.execute(
      "SELECT * FROM jobs WHERE user_id=? AND active=1 ORDER BY name COLLATE NOCASE",
      (user_id,),
    ).fetchall()

  def add_job(self, user_id: int, name: str) -> int:
    cur = self.conn.execute(
      "INSERT INTO jobs(user_id,name,created_at) VALUES(?,?,?)",
      (user_id, name.strip(), now_iso()),
    )
    self.conn.commit()
    return cur.lastrowid

  def get_job(self, user_id: int, job_id: int):
    return self.conn.execute(
      "SELECT * FROM jobs WHERE id=? AND user_id=? AND active=1",
      (job_id, user_id),
    ).fetchone()

  def rename_job(self, user_id: int, job_id: int, name: str):
    self.conn.execute(
      "UPDATE jobs SET name=? WHERE id=? AND user_id=? AND active=1",
      (name.strip(), job_id, user_id),
    )
    self.conn.commit()

  def delete_job(self, user_id: int, job_id: int):
    self.conn.execute(
      "UPDATE jobs SET active=0 WHERE id=? AND user_id=?",
      (job_id, user_id),
    )
    self.conn.commit()

  def active_farm(self, user_id: int):
    return self.conn.execute(
      """SELECT f.*, j.name AS job_name
         FROM farms f JOIN jobs j ON j.id=f.job_id
         WHERE f.user_id=? AND f.status IN ('active','draft')
         ORDER BY f.id DESC LIMIT 1""",
      (user_id,),
    ).fetchone()

  def farm(self, user_id: int, farm_id: int):
    return self.conn.execute(
      """SELECT f.*, j.name AS job_name
         FROM farms f JOIN jobs j ON j.id=f.job_id
         WHERE f.id=? AND f.user_id=?""",
      (farm_id, user_id),
    ).fetchone()

  def create_farm(self, user_id: int, job_id: int, started_at: str, source: str) -> int:
    if not self.get_job(user_id, job_id):
      raise ValueError("Роботу не знайдено або видалено.")
    if source == "live" and self.active_farm(user_id):
      raise ValueError("У тебе вже є активний фарм.")
    cur = self.conn.execute(
      """INSERT INTO farms(user_id,job_id,started_at,status,source,created_at)
         VALUES(?,?,?,'active',?,?)""",
      (user_id, job_id, started_at, source, now_iso()),
    )
    self.conn.commit()
    return cur.lastrowid

  def finish_farm(self, user_id: int, farm_id: int, ended_at: str):
    self.conn.execute(
      "UPDATE farms SET ended_at=?, status='completed' WHERE id=? AND user_id=? AND status IN ('active','draft')",
      (ended_at, farm_id, user_id),
    )
    self.conn.commit()

  def delete_farm(self, user_id: int, farm_id: int):
    self.conn.execute(
      "DELETE FROM farms WHERE id=? AND user_id=?",
      (farm_id, user_id),
    )
    self.conn.commit()

  def add_tx(self, user_id: int, farm_id: int, kind: str, amount: int, note: Optional[str] = None):
    farm = self.farm(user_id, farm_id)
    if not farm or farm["status"] not in ("active", "draft", "completed"):
      raise ValueError("Фарм не знайдено.")
    self.conn.execute(
      "INSERT INTO transactions(farm_id,user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?,?)",
      (farm_id, user_id, kind, amount, (note or "").strip() or None, now_iso()),
    )
    self.conn.commit()

  def totals(self, user_id: int, farm_id: int):
    row = self.conn.execute(
      """SELECT
          COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income,
          COALESCE(SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END),0) expense
         FROM transactions WHERE farm_id=? AND user_id=?""",
      (farm_id, user_id),
    ).fetchone()
    return int(row["income"]), int(row["expense"])

  def transactions(self, user_id: int, farm_id: int):
    return self.conn.execute(
      "SELECT * FROM transactions WHERE farm_id=? AND user_id=? ORDER BY id DESC",
      (farm_id, user_id),
    ).fetchall()

  def recent_farms(self, user_id: int, limit: int = 10, job_id: Optional[int] = None):
    return self.conn.execute(
      """SELECT f.*, j.name AS job_name,
          COALESCE((SELECT SUM(amount) FROM transactions t WHERE t.farm_id=f.id AND t.kind='income'),0) income,
          COALESCE((SELECT SUM(amount) FROM transactions t WHERE t.farm_id=f.id AND t.kind='expense'),0) expense
         FROM farms f JOIN jobs j ON j.id=f.job_id
         WHERE f.user_id=? AND f.status='completed' AND (? IS NULL OR f.job_id=?)
         ORDER BY f.started_at DESC, f.id DESC LIMIT ?""",
      (user_id, job_id, job_id, limit),
    ).fetchall()

  def stats(self, user_id: int, since: Optional[str] = None,
            job_id: Optional[int] = None):
    rows = self.conn.execute(
      """SELECT f.started_at, f.ended_at,
          COALESCE(SUM(CASE WHEN t.kind='income' THEN t.amount ELSE 0 END),0) income,
          COALESCE(SUM(CASE WHEN t.kind='expense' THEN t.amount ELSE 0 END),0) expense
         FROM farms f LEFT JOIN transactions t ON t.farm_id=f.id
         WHERE f.user_id=? AND f.status='completed'
           AND (? IS NULL OR julianday(f.started_at)>=julianday(?))
           AND (? IS NULL OR f.job_id=?)
         GROUP BY f.id""",
      (user_id, since, since, job_id, job_id),
    ).fetchall()
    income = sum(int(r["income"]) for r in rows)
    expense = sum(int(r["expense"]) for r in rows)
    seconds = sum(max(0, int((datetime.fromisoformat(r["ended_at"]).astimezone(UTC)
                            -datetime.fromisoformat(r["started_at"]).astimezone(UTC)).total_seconds()))
                  for r in rows if r["ended_at"])
    return {"farms": len(rows), "income": income, "expense": expense,
            "net": income-expense, "seconds": seconds,
            "net_hour": (income-expense)*3600/seconds if seconds else 0}

  def job_stats(self, user_id: int, job_id: int):
    return self.stats(user_id, job_id=job_id)


db = EconomyDB(ECONOMY_DB_PATH)


def is_owner(interaction: discord.Interaction, user_id: int) -> bool:
  return interaction.user.id == user_id


def embed_main(user_id: int) -> discord.Embed:
  active = db.active_farm(user_id)
  stats = db.stats(user_id)
  embed = discord.Embed(title="💰 Особиста економіка", color=discord.Color.blurple())
  if active:
    start = parse_dt(active["started_at"])
    elapsed = max(0, int((datetime.now(UTC)-start.astimezone(UTC)).total_seconds()))
    income, expense = db.totals(user_id, active["id"])
    embed.add_field(
      name="▶️ Активний фарм",
      value=(f"**{esc(active['job_name'])}**\n"
             f"⏱️ {duration_text(elapsed)}\n"
             f"💵 +{money(income)}\n"
             f"💸 -{money(expense)}\n"
             f"📈 **{money(income-expense)}**"),
      inline=False,
    )
  else:
    embed.add_field(name="▶️ Активний фарм", value="Немає активного фарму.", inline=False)
  embed.add_field(
    name="📊 Загалом",
    value=(f"Фармів: **{stats['farms']}**\n"
           f"Дохід: **{money(stats['income'])}**\n"
           f"Витрати: **{money(stats['expense'])}**\n"
           f"Чистими: **{money(stats['net'])}**\n"
           f"Чистими/год: **{money(round(stats['net_hour']))}**"),
    inline=False,
  )
  return embed


def main_view() -> discord.ui.View:
  return EconomyMainView()


class AmountModal(discord.ui.Modal):
  def __init__(self, user_id: int, farm_id: int, kind: str):
    super().__init__(title="💵 Дохід" if kind == "income" else "💸 Витрата")
    self.user_id = user_id
    self.farm_id = farm_id
    self.kind = kind
    self.amount = discord.ui.TextInput(label="Сума", placeholder="Наприклад: 250000", required=True, max_length=15)
    self.note = discord.ui.TextInput(label="Коментар", required=False, max_length=100, placeholder="Необов'язково")
    self.add_item(self.amount)
    self.add_item(self.note)

  async def on_submit(self, interaction: discord.Interaction):
    if not is_owner(interaction, self.user_id):
      return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    try:
      amount = int(self.amount.value.replace(" ", "").replace(",", ""))
      if amount <= 0:
        raise ValueError
      db.add_tx(self.user_id, self.farm_id, self.kind, amount, self.note.value)
    except (ValueError, OverflowError):
      return await interaction.response.send_message("❌ Вкажи коректну додатну суму; фарм має існувати.", ephemeral=True)
    await refresh_farm_message(interaction, self.user_id, self.farm_id)


class AddJobModal(discord.ui.Modal):
  title = "💼 Нова робота"
  name = discord.ui.TextInput(label="Назва роботи", placeholder="Наприклад: Далекобійник", max_length=60)

  async def on_submit(self, interaction: discord.Interaction):
    name = self.name.value.strip()
    if not name:
      return await interaction.response.send_message("❌ Назва не може бути порожньою.", ephemeral=True)
    db.add_job(interaction.user.id, name)
    await interaction.response.send_message("✅ Роботу додано.", view=JobsView(interaction.user.id), ephemeral=True)


class RenameJobModal(discord.ui.Modal):
  def __init__(self, user_id: int, job_id: int, current: str):
    super().__init__(title="✏️ Редагувати роботу")
    self.user_id = user_id
    self.job_id = job_id
    self.name = discord.ui.TextInput(label="Назва", default=current, max_length=60)
    self.add_item(self.name)

  async def on_submit(self, interaction: discord.Interaction):
    if interaction.user.id != self.user_id:
      return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    name = self.name.value.strip()
    if not name:
      return await interaction.response.send_message("❌ Назва не може бути порожньою.", ephemeral=True)
    db.rename_job(self.user_id, self.job_id, name)
    await interaction.response.send_message("✅ Роботу оновлено.", view=JobsView(self.user_id), ephemeral=True)


class ManualFarmModal(discord.ui.Modal):
  def __init__(self, user_id: int, job_id: int):
    super().__init__(title="📝 Записати фарм")
    self.user_id = user_id
    self.job_id = job_id
    self.start = discord.ui.TextInput(label="Початок", placeholder="07.10.2026 18:00", max_length=20)
    self.end = discord.ui.TextInput(label="Кінець", placeholder="07.10.2026 20:30", max_length=20)
    self.add_item(self.start)
    self.add_item(self.end)

  async def on_submit(self, interaction: discord.Interaction):
    if interaction.user.id != self.user_id:
      return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    try:
      start = datetime.strptime(self.start.value.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=LOCAL_TZ)
      end = datetime.strptime(self.end.value.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=LOCAL_TZ)
      if end.astimezone(UTC) <= start.astimezone(UTC):
        raise ValueError
    except ValueError:
      return await interaction.response.send_message("❌ Формат: `07.10.2026 18:00` і кінець після початку.", ephemeral=True)
    try:
      farm_id = db.create_farm(self.user_id, self.job_id, start.isoformat(), "manual")
    except ValueError as exc:
      return await interaction.response.send_message(str(exc), ephemeral=True)
    db.finish_farm(self.user_id, farm_id, end.isoformat())
    await interaction.response.send_message("📝 Фарм створено. Тепер додай дохід/витрати.", view=FarmView(self.user_id, farm_id), ephemeral=True)


class JobSelect(discord.ui.Select):
  def __init__(self, user_id: int, mode: str, page: int = 0):
    jobs = db.jobs(user_id)
    options = [discord.SelectOption(label=j["name"][:100], value=str(j["id"])) for j in jobs[page*25:(page+1)*25]]
    super().__init__(placeholder="Оберіть роботу", options=options, custom_id=f"economy:job:{mode}")
    self.user_id = user_id
    self.mode = mode

  async def callback(self, interaction: discord.Interaction):
    if interaction.user.id != self.user_id:
      return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    job_id = int(self.values[0])
    if self.mode == "start":
      if db.active_farm(self.user_id):
        return await interaction.response.send_message("⚠️ У тебе вже є активний фарм.", ephemeral=True)
      try:
        farm_id = db.create_farm(self.user_id, job_id, now_iso(), "live")
      except ValueError as exc:
        return await interaction.response.send_message(str(exc), ephemeral=True)
      await interaction.response.edit_message(content="▶️ Фарм розпочато.", embed=await farm_embed(self.user_id, farm_id), view=FarmView(self.user_id, farm_id))
    elif self.mode == "manual":
      await interaction.response.send_modal(ManualFarmModal(self.user_id, job_id))


class JobSelectView(discord.ui.View):
  def __init__(self, user_id: int, mode: str, page: int = 0):
    super().__init__(timeout=180)
    jobs = db.jobs(user_id)
    if jobs:
      self.add_item(JobSelect(user_id, mode, page))
    add_page_buttons(self, user_id, page, len(jobs), 25, lambda p: JobSelectView(user_id, mode, p))
    back = discord.ui.Button(label="⬅️ Назад", style=discord.ButtonStyle.secondary)
    async def back_cb(interaction: discord.Interaction):
      if interaction.user.id != user_id:
        return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
      await interaction.response.edit_message(embed=embed_main(user_id), content=None, view=EconomyMainView())
    back.callback = back_cb
    self.add_item(back)


class FarmView(discord.ui.View):
  def __init__(self, user_id: int, farm_id: int):
    super().__init__(timeout=1800)
    self.user_id = user_id
    self.farm_id = farm_id
    b1 = discord.ui.Button(label="💵 Дохід", style=discord.ButtonStyle.success)
    b2 = discord.ui.Button(label="💸 Витрата", style=discord.ButtonStyle.danger)
    b3 = discord.ui.Button(label="⏹️ Завершити", style=discord.ButtonStyle.primary)
    b4 = discord.ui.Button(label="💾 Зберегти", style=discord.ButtonStyle.success)
    b5 = discord.ui.Button(label="🗑️ Скасувати", style=discord.ButtonStyle.secondary)
    b1.callback = self.income
    b2.callback = self.expense
    b3.callback = self.finish
    b4.callback = self.save
    b5.callback = self.cancel
    for b in (b1,b2,b3,b4,b5): self.add_item(b)
    back = discord.ui.Button(label="⬅️ Меню", style=discord.ButtonStyle.secondary)
    async def back_cb(interaction):
      if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
      await interaction.response.edit_message(content=None, embed=embed_main(self.user_id), view=EconomyMainView())
    back.callback = back_cb
    self.add_item(back)

  async def income(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    await interaction.response.send_modal(AmountModal(self.user_id, self.farm_id, "income"))

  async def expense(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    await interaction.response.send_modal(AmountModal(self.user_id, self.farm_id, "expense"))

  async def finish(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    farm = db.farm(self.user_id, self.farm_id)
    if not farm: return await interaction.response.send_message("❌ Фарм не знайдено.", ephemeral=True)
    if farm["status"] == "active": db.finish_farm(self.user_id, self.farm_id, now_iso())
    await refresh_farm_message(interaction, self.user_id, self.farm_id)

  async def save(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    farm = db.farm(self.user_id, self.farm_id)
    if not farm: return await interaction.response.send_message("❌ Фарм не знайдено.", ephemeral=True)
    if farm["status"] == "active":
      return await interaction.response.send_message("⚠️ Спочатку заверши фарм кнопкою `⏹️ Завершити`.", ephemeral=True)
    await interaction.response.edit_message(content="✅ Фарм збережено.", embed=await farm_embed(self.user_id, self.farm_id), view=EconomyMainView())

  async def cancel(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    db.delete_farm(self.user_id, self.farm_id)
    await interaction.response.edit_message(content="🗑️ Фарм скасовано.", embed=embed_main(self.user_id), view=EconomyMainView())


async def farm_embed(user_id: int, farm_id: int) -> discord.Embed:
  farm = db.farm(user_id, farm_id)
  if not farm:
    return discord.Embed(title="❌ Фарм не знайдено")
  income, expense = db.totals(user_id, farm_id)
  start = parse_dt(farm["started_at"])
  end = parse_dt(farm["ended_at"]) if farm["ended_at"] else datetime.now(LOCAL_TZ)
  seconds = max(0, int((end.astimezone(UTC)-start.astimezone(UTC)).total_seconds()))
  net = income-expense
  eph = net/(seconds/3600) if seconds else 0
  status = "🟢 активний" if farm["status"] == "active" else "✅ завершений"
  embed = discord.Embed(title=f"💼 {esc(farm['job_name'])}", color=discord.Color.green() if farm["status"] == "active" else discord.Color.blurple())
  embed.description = f"{status}\n⏱️ **{duration_text(seconds)}**"
  embed.add_field(name="💵 Дохід", value=f"**{money(income)}**", inline=True)
  embed.add_field(name="💸 Витрати", value=f"**{money(expense)}**", inline=True)
  embed.add_field(name="📈 Чистими", value=f"**{money(net)}**", inline=True)
  embed.add_field(name="⚡ Чистими/год", value=f"**{money(round(eph))}**", inline=False)
  tx = db.transactions(user_id, farm_id)
  if tx:
    lines = []
    for row in tx[:8]:
      sign = "+" if row["kind"] == "income" else "-"
      note = f" — {esc(row['note'])}" if row["note"] else ""
      lines.append(f"{sign}{money(row['amount'])}{note}")
    embed.add_field(name="🧾 Операції", value="\n".join(lines)[:1024], inline=False)
  return embed


async def refresh_farm_message(interaction: discord.Interaction, user_id: int, farm_id: int):
  embed = await farm_embed(user_id, farm_id)
  try:
    if interaction.response.is_done():
      await interaction.edit_original_response(embed=embed, view=FarmView(user_id, farm_id), content=None)
    else:
      await interaction.response.edit_message(embed=embed, view=FarmView(user_id, farm_id), content=None)
  except discord.DiscordException:
    if not interaction.response.is_done():
      await interaction.response.send_message(embed=embed, view=FarmView(user_id, farm_id), ephemeral=True)


def add_page_buttons(view, user_id, page, count, page_size, factory):
  for label, target in (("◀️ Попередні", page-1), ("Наступні ▶️", page+1)):
    if target < 0 or target*page_size >= count:
      continue
    button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
    async def callback(interaction, target=target):
      if interaction.user.id != user_id:
        return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
      await interaction.response.edit_message(view=factory(target))
    button.callback = callback
    view.add_item(button)


class JobsView(discord.ui.View):
  def __init__(self, user_id: int, page: int = 0):
    super().__init__(timeout=300)
    self.user_id = user_id
    add = discord.ui.Button(label="➕ Додати", style=discord.ButtonStyle.success)
    add.callback = self.add_job
    self.add_item(add)
    jobs = db.jobs(user_id)
    add_page_buttons(self, user_id, page, len(jobs), 20, lambda p: JobsView(user_id, p))
    back = discord.ui.Button(label="⬅️ Меню", style=discord.ButtonStyle.secondary)
    async def back_cb(interaction):
      if interaction.user.id != user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
      await interaction.response.edit_message(content=None, embed=embed_main(user_id), view=EconomyMainView())
    back.callback = back_cb
    self.add_item(back)
    for job in jobs[page*20:(page+1)*20]:
      b = discord.ui.Button(label=f"💼 {job['name'][:70]}", style=discord.ButtonStyle.secondary, custom_id=f"economy:jobprofile:{job['id']}")
      b.callback = self.job_profile
      self.add_item(b)

  async def add_job(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    await interaction.response.send_modal(AddJobModal())

  async def job_profile(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    job_id = int(interaction.data["custom_id"].split(":")[-1])
    job = db.get_job(self.user_id, job_id)
    if not job: return await interaction.response.send_message("❌ Роботу не знайдено.", ephemeral=True)
    st = db.job_stats(self.user_id, job_id)
    embed = discord.Embed(title=f"💼 {esc(job['name'])}", color=discord.Color.blurple())
    embed.add_field(name="📊 Статистика", value=(f"Фармів: **{st['farms']}**\nДохід: **{money(st['income'])}**\nВитрати: **{money(st['expense'])}**\nЧистими: **{money(st['net'])}**\nЧистими/год: **{money(round(st['net_hour']))}**"), inline=False)
    view = JobProfileView(self.user_id, job_id)
    await interaction.response.edit_message(embed=embed, view=view)


class JobProfileView(discord.ui.View):
  def __init__(self, user_id: int, job_id: int):
    super().__init__(timeout=300)
    self.user_id = user_id; self.job_id = job_id
    edit = discord.ui.Button(label="✏️ Редагувати", style=discord.ButtonStyle.secondary)
    delete = discord.ui.Button(label="🗑️ Видалити", style=discord.ButtonStyle.danger)
    history = discord.ui.Button(label="📜 Історія", style=discord.ButtonStyle.primary)
    back = discord.ui.Button(label="⬅️ Роботи", style=discord.ButtonStyle.secondary)
    edit.callback = self.edit; delete.callback = self.delete; history.callback = self.history; back.callback = self.back
    for b in (edit, delete, history, back): self.add_item(b)

  async def edit(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    job = db.get_job(self.user_id, self.job_id)
    if not job: return await interaction.response.send_message("❌ Роботу не знайдено.", ephemeral=True)
    await interaction.response.send_modal(RenameJobModal(self.user_id, self.job_id, job["name"]))

  async def delete(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    db.delete_job(self.user_id, self.job_id)
    await interaction.response.edit_message(content="🗑️ Роботу видалено зі списку. Історія збережена.", embed=None, view=JobsView(self.user_id))

  async def history(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    rows = db.recent_farms(self.user_id, 10, self.job_id)
    if not rows:
      text = "Історії ще немає."
    else:
      text = "\n".join(f"• {r['started_at'][:10]} — **{money(int(r['income'])-int(r['expense']))}** — {duration_text(int((parse_dt(r['ended_at']).astimezone(UTC)-parse_dt(r['started_at']).astimezone(UTC)).total_seconds()))}" for r in rows[:10])
    await interaction.response.send_message(f"📜 **Історія**\n{text}", ephemeral=True)

  async def back(self, interaction):
    if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
    await interaction.response.edit_message(embed=jobs_embed(self.user_id), view=JobsView(self.user_id))


def jobs_embed(user_id: int) -> discord.Embed:
  jobs = db.jobs(user_id)
  return discord.Embed(title="💼 Роботи", description=("Обери роботу для профілю та статистики." if jobs else "Робіт поки немає. Додай першу кнопкою нижче."), color=discord.Color.blurple())


class StatsView(discord.ui.View):
  def __init__(self, user_id: int):
    super().__init__(timeout=300)
    self.user_id = user_id
    for label, days, cid in (("Сьогодні",1,"d1"),("7 днів",7,"d7"),("30 днів",30,"d30"),("Весь час",None,"all")):
      b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if cid=="all" else discord.ButtonStyle.secondary, custom_id=f"economy:stats:{cid}")
      async def cb(interaction, days=days):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
        await interaction.response.edit_message(embed=stats_embed(self.user_id, days), view=StatsView(self.user_id))
      b.callback = cb; self.add_item(b)
    back = discord.ui.Button(label="⬅️ Назад", style=discord.ButtonStyle.secondary)
    async def back_cb(interaction):
      if interaction.user.id != self.user_id: return await interaction.response.send_message("❌ Це не твоя панель.", ephemeral=True)
      await interaction.response.edit_message(embed=embed_main(self.user_id), view=EconomyMainView())
    back.callback = back_cb; self.add_item(back)


def stats_embed(user_id: int, days: Optional[int]) -> discord.Embed:
  since = period_start(days)
  st = db.stats(user_id, since)
  period = "весь час" if days is None else ("сьогодні" if days == 1 else f"останні {days} днів")
  return discord.Embed(title="📈 Статистика", description=(f"**{period}**\n\nФармів: **{st['farms']}**\n💵 Дохід: **{money(st['income'])}**\n💸 Витрати: **{money(st['expense'])}**\n📈 Чистими: **{money(st['net'])}**\n⏱️ Час: **{duration_text(st['seconds'])}**\n⚡ Чистими/год: **{money(round(st['net_hour']))}**"), color=discord.Color.blurple())


class EconomyMainView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=None)
    buttons = [
      ("▶️ Почати фарм", "start", discord.ButtonStyle.success),
      ("⏯️ Продовжити фарм", "resume", discord.ButtonStyle.primary),
      ("📝 Записати фарм", "manual", discord.ButtonStyle.primary),
      ("📊 Дашборд", "dashboard", discord.ButtonStyle.secondary),
      ("💼 Роботи", "jobs", discord.ButtonStyle.secondary),
      ("📈 Статистика", "stats", discord.ButtonStyle.secondary),
    ]
    for label, action, style in buttons:
      b = discord.ui.Button(label=label, style=style, custom_id=f"economy:main:{action}")
      b.callback = self.dispatch
      self.add_item(b)

  async def dispatch(self, interaction: discord.Interaction):
    if not economy_access_allowed(interaction):
      return await interaction.response.send_message(
        "🔒 Ця економіка доступна тільки власнику системи.", ephemeral=True
      )
    # Keep the permanent channel panel intact; use a personal workspace.
    if interaction.message and not interaction.message.flags.ephemeral:
      return await open_economy(interaction)
    user_id = interaction.user.id
    action = interaction.data["custom_id"].split(":")[-1]
    if action == "resume":
      active = db.active_farm(user_id)
      if not active:
        return await interaction.response.send_message("Немає активного фарму.", ephemeral=True)
      return await interaction.response.edit_message(content=None, embed=await farm_embed(user_id, active["id"]), view=FarmView(user_id, active["id"]))
    if action == "dashboard":
      return await interaction.response.edit_message(embed=embed_main(user_id), view=EconomyMainView())
    if action == "jobs":
      return await interaction.response.edit_message(embed=jobs_embed(user_id), view=JobsView(user_id))
    if action == "stats":
      return await interaction.response.edit_message(embed=stats_embed(user_id, None), view=StatsView(user_id))
    if action in ("start", "manual"):
      jobs = db.jobs(user_id)
      if not jobs:
        return await interaction.response.edit_message(content="❌ Спочатку створи хоча б одну роботу в розділі **💼 Роботи**.", embed=jobs_embed(user_id), view=JobsView(user_id))
      if action == "start" and db.active_farm(user_id):
        return await interaction.response.edit_message(content="⚠️ У тебе вже є активний фарм.", embed=embed_main(user_id), view=EconomyMainView())
      await interaction.response.edit_message(content="Обери роботу:", embed=None, view=JobSelectView(user_id, action))


def economy_access_allowed(interaction: discord.Interaction) -> bool:
  if ECONOMY_USER_ID and interaction.user.id != ECONOMY_USER_ID:
    return False
  if ECONOMY_CHANNEL_ID and interaction.channel_id != ECONOMY_CHANNEL_ID:
    return False
  return interaction.guild_id == GUILD_ID


async def open_economy(interaction: discord.Interaction):
  if not economy_access_allowed(interaction):
    reason = "🔒 Економіка доступна тільки тобі." if ECONOMY_USER_ID and interaction.user.id != ECONOMY_USER_ID else "📍 Економіка працює тільки у своєму каналі."
    return await interaction.response.send_message(reason, ephemeral=True)
  active = db.active_farm(interaction.user.id)
  content = "⚠️ У тебе є незавершений фарм." if active else None
  await interaction.response.send_message(content=content, embed=embed_main(interaction.user.id), view=EconomyMainView(), ephemeral=True)


def register_commands(bot):
  @bot.tree.command(name="economy", description="Особистий трекер фарму та заробітку")
  async def economy_command(interaction: discord.Interaction):
    await open_economy(interaction)


async def restore_active_views(bot):
  bot.add_view(EconomyMainView())


async def ensure_economy_panel(bot):
  if not ECONOMY_CHANNEL_ID:
    print("[ECONOMY] ECONOMY_CHANNEL_ID is not set; panel disabled")
    return
  if not ECONOMY_USER_ID:
    print("[ECONOMY] ECONOMY_USER_ID is not set; panel disabled")
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
        title="💰 Особиста економіка",
        description=(
          "Тут ведеться твій особистий облік фарму.\n\n"
          "▶️ **Почати фарм** — запустити таймер\n"
          "📝 **Записати фарм** — внести минулий фарм\n"
          "📊 **Дашборд** — поточні показники\n"
          "💼 **Роботи** — керування роботами\n"
          "📈 **Статистика** — аналіз заробітку"
        ),
        color=discord.Color.green(),
      ),
      view=EconomyMainView(),
    )
    print(f"[ECONOMY] Panel created in channel {ECONOMY_CHANNEL_ID}")
  except discord.DiscordException as exc:
    print(f"[ECONOMY] Could not ensure panel: {exc}")
