import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands

from config import GUILD_ID
from database import db
from utils import management_member, has_leader_role

# Railway ENV channel IDs. They are optional: commands still work without panels.
WARS_CHANNEL_ID = int(os.getenv("WARS_CHANNEL_ID", "0") or 0)
WARS_MANAGEMENT_CHANNEL_ID = int(os.getenv("WARS_MANAGEMENT_CHANNEL_ID", "0") or 0)
WARS_MODERATION_CHANNEL_ID = int(os.getenv("WARS_MODERATION_CHANNEL_ID", "0") or 0)
WARS_REPORTS_CHANNEL_ID = int(os.getenv("WARS_REPORTS_CHANNEL_ID", "0") or 0)

# Commander roles: comma-separated Discord role IDs in Railway.
COMMANDER_ROLE_IDS = {
    int(x.strip())
    for x in os.getenv("COMMANDER_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
}

DEFAULTS = {
    "wars_money_per_hour": "0",
    "wars_rating_per_hour": "0",
    "wars_authority_per_hour": "0",
    "wars_points_participation": "10",
    "wars_points_win": "20",
    "wars_points_mvp": "10",
    "wars_rating_period_started_at": "",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def unix(value: str) -> int:
    return int(parse_dt(value).timestamp())


def fmt_money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def setting(guild_id: int, key: str) -> str:
    value = db.get_setting(guild_id, key)
    return value if value not in (None, "") else DEFAULTS.get(key, "")


def set_setting(guild_id: int, key: str, value) -> None:
    db.set_setting(guild_id, key, str(value))


def ensure_schema() -> None:
    conn = db.conn
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS war_complexes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            ownership_started_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(guild_id, name)
        );

        CREATE TABLE IF NOT EXISTS war_ownership_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            complex_id INTEGER NOT NULL,
            owner_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            money_per_hour INTEGER NOT NULL DEFAULT 0,
            rating_per_hour INTEGER NOT NULL DEFAULT 0,
            authority_per_hour INTEGER NOT NULL DEFAULT 0,
            money_awarded INTEGER NOT NULL DEFAULT 0,
            rating_awarded INTEGER NOT NULL DEFAULT 0,
            authority_awarded INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS wars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            complex_id INTEGER NOT NULL,
            attack_type TEXT NOT NULL,
            opponent TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'recruiting',
            registration_mode TEXT NOT NULL DEFAULT 'recruitment',
            participant_limit INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            ended_at TEXT,
            result TEXT,
            mvp_user_id INTEGER,
            commander_comment TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS war_participants (
            war_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'registered',
            participation_points INTEGER NOT NULL DEFAULT 0,
            win_points INTEGER NOT NULL DEFAULT 0,
            mvp_points INTEGER NOT NULL DEFAULT 0,
            total_points INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY(war_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS war_resource_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            complex_id INTEGER NOT NULL,
            ownership_period_id INTEGER,
            owner_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            seconds_owned INTEGER NOT NULL,
            money INTEGER NOT NULL DEFAULT 0,
            rating INTEGER NOT NULL DEFAULT 0,
            authority INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_wars_guild_status ON wars(guild_id, status);
        CREATE INDEX IF NOT EXISTS idx_wars_participants_user ON war_participants(guild_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_wars_periods_complex ON war_ownership_periods(complex_id, closed);
        CREATE INDEX IF NOT EXISTS idx_wars_resources_owner ON war_resource_ledger(guild_id, owner_name, ended_at);
        """
    )
    # Seed settings without overwriting existing values.
    for key, value in DEFAULTS.items():
        if db.get_setting(GUILD_ID, key) is None:
            db.set_setting(GUILD_ID, key, value)
    # If no period exists yet, start it now.
    if not db.get_setting(GUILD_ID, "wars_rating_period_started_at"):
        db.set_setting(GUILD_ID, "wars_rating_period_started_at", now_iso())
    conn.commit()


ensure_schema()


def is_commander(member: discord.Member) -> bool:
    if management_member(member):
        return True
    return any(role.id in COMMANDER_ROLE_IDS for role in member.roles)


def is_management(member: discord.Member) -> bool:
    return management_member(member) or has_leader_role(member)


def is_member_of_augusto(member: discord.Member) -> bool:
    # Any server member can see/use the participant-facing panel.
    return member.guild is not None and member.guild.id == GUILD_ID


def get_complex(complex_id: int):
    return db.conn.execute(
        "SELECT * FROM war_complexes WHERE id=? AND guild_id=? AND active=1",
        (complex_id, GUILD_ID),
    ).fetchone()


def get_active_war(war_id: int):
    return db.conn.execute(
        "SELECT * FROM wars WHERE id=? AND guild_id=? AND status IN ('recruiting','active')",
        (war_id, GUILD_ID),
    ).fetchone()


def begin_ownership(complex_id: int, owner_name: str, started_at: Optional[str] = None) -> None:
    started_at = started_at or now_iso()
    money = int(setting(GUILD_ID, "wars_money_per_hour"))
    rating = int(setting(GUILD_ID, "wars_rating_per_hour"))
    authority = int(setting(GUILD_ID, "wars_authority_per_hour"))
    db.conn.execute(
        """
        INSERT INTO war_ownership_periods
        (guild_id, complex_id, owner_name, started_at, money_per_hour, rating_per_hour, authority_per_hour)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (GUILD_ID, complex_id, owner_name, started_at, money, rating, authority),
    )


def settle_ownership(complex_id: int, ended_at: Optional[str] = None) -> Optional[dict]:
    row = db.conn.execute(
        """
        SELECT * FROM war_ownership_periods
        WHERE guild_id=? AND complex_id=? AND closed=0
        ORDER BY id DESC LIMIT 1
        """,
        (GUILD_ID, complex_id),
    ).fetchone()
    if not row:
        return None
    ended_at = ended_at or now_iso()
    start = parse_dt(row["started_at"])
    end = parse_dt(ended_at)
    seconds = max(0, int((end - start).total_seconds()))
    # Fractional hour is intentionally accumulated exactly in seconds and only
    # the final integer resource is recorded. No time is lost between restarts.
    money = int(seconds * row["money_per_hour"] / 3600)
    rating = int(seconds * row["rating_per_hour"] / 3600)
    authority = int(seconds * row["authority_per_hour"] / 3600)

    db.conn.execute(
        """
        UPDATE war_ownership_periods
        SET ended_at=?, money_awarded=?, rating_awarded=?, authority_awarded=?, closed=1
        WHERE id=?
        """,
        (ended_at, money, rating, authority, row["id"]),
    )
    db.conn.execute(
        """
        INSERT INTO war_resource_ledger
        (guild_id, complex_id, ownership_period_id, owner_name, started_at, ended_at,
         seconds_owned, money, rating, authority, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (GUILD_ID, complex_id, row["id"], row["owner_name"], row["started_at"], ended_at,
         seconds, money, rating, authority, now_iso()),
    )
    return {
        "owner_name": row["owner_name"],
        "seconds": seconds,
        "money": money,
        "rating": rating,
        "authority": authority,
    }


def change_owner(complex_id: int, new_owner: str, at: Optional[str] = None) -> Optional[dict]:
    at = at or now_iso()
    settled = settle_ownership(complex_id, at)
    db.conn.execute(
        "UPDATE war_complexes SET owner_name=?, ownership_started_at=? WHERE id=? AND guild_id=?",
        (new_owner, at, complex_id, GUILD_ID),
    )
    begin_ownership(complex_id, new_owner, at)
    return settled


def current_complexes(owner_name: Optional[str] = None):
    if owner_name:
        return db.conn.execute(
            "SELECT * FROM war_complexes WHERE guild_id=? AND active=1 AND owner_name=? ORDER BY id",
            (GUILD_ID, owner_name),
        ).fetchall()
    return db.conn.execute(
        "SELECT * FROM war_complexes WHERE guild_id=? AND active=1 ORDER BY id",
        (GUILD_ID,),
    ).fetchall()


def settle_open_complexes() -> None:
    # Do not close ownership here. The ledger is finalized when ownership ends.
    # This function exists as a safe hook for the scheduler and deliberately
    # leaves timestamps untouched so restarts cannot double-count resources.
    return


def participant_ids(war_id: int):
    rows = db.conn.execute(
        "SELECT user_id FROM war_participants WHERE war_id=? ORDER BY created_at, user_id",
        (war_id,),
    ).fetchall()
    return [int(r["user_id"]) for r in rows]


def add_member_to_war(war_id: int, user_id: int) -> None:
    war = get_active_war(war_id)
    if not war or war["status"] != "recruiting":
        raise ValueError("Війна не набирає учасників.")
    ids = participant_ids(war_id)
    if user_id in ids:
        raise ValueError("Цей учасник уже записаний.")
    if war["participant_limit"] and len(ids) >= war["participant_limit"]:
        raise ValueError("Ліміт учасників уже заповнений.")
    with db.conn:
        db.conn.execute(
            "INSERT INTO war_participants(war_id,guild_id,user_id,status,created_at) VALUES(?,?,?,?,?)",
            (war_id, GUILD_ID, user_id, "registered", now_iso()),
        )


def war_embed(war_id: int) -> discord.Embed:
    row = db.conn.execute(
        """
        SELECT w.*, c.name AS complex_name, c.owner_name
        FROM wars w JOIN war_complexes c ON c.id=w.complex_id
        WHERE w.id=? AND w.guild_id=?
        """,
        (war_id, GUILD_ID),
    ).fetchone()
    if not row:
        return discord.Embed(title="⚔️ Війну не знайдено")
    parts = participant_ids(war_id)
    kind = "⚔️ Атака" if row["attack_type"] == "attack" else "🛡️ Захист"
    status_map = {"recruiting": "🟡 Набір", "active": "🔴 Активна", "finished": "🟢 Завершена", "cancelled": "⚫ Скасована"}
    status = status_map.get(row["status"], row["status"])
    lines = [f"**{i}.** <@{uid}>" for i, uid in enumerate(parts[:20], 1)]
    if len(parts) > 20:
        lines.append(f"…і ще {len(parts) - 20} учасників")
    if not lines:
        lines = ["Поки ніхто не записався."]
    embed = discord.Embed(
        title=f"{kind} • війна за комплекс #{row['complex_id']}",
        description=(
            f"🏢 **{row['complex_name']}**\n"
            f"👑 Власник: **{row['owner_name']}**\n"
            f"🎯 Противник: **{row['opponent']}**\n"
            f"📌 Статус: **{status}**\n"
            f"👥 Учасників: **{len(parts)}" + (f"/{row['participant_limit']}**" if row["participant_limit"] else "**")
        ),
        color=discord.Color.dark_red() if row["attack_type"] == "attack" else discord.Color.blue(),
    )
    embed.add_field(name="👥 Склад", value="\n".join(lines), inline=False)
    if row["result"]:
        result = "🏆 Перемога" if row["result"] == "win" else "💀 Поразка"
        embed.add_field(name="Результат", value=result, inline=True)
    return embed


class RecruitView(discord.ui.View):
    def __init__(self, war_id: int):
        super().__init__(timeout=None)
        self.war_id = war_id
        join = discord.ui.Button(
            label="Беру участь", emoji="🪖", style=discord.ButtonStyle.success,
            custom_id=f"wars:join:{war_id}", row=0,
        )
        join.callback = self.join_callback
        self.add_item(join)
        profile = discord.ui.Button(
            label="Профіль", emoji="👤", style=discord.ButtonStyle.secondary,
            custom_id=f"wars:profile:{war_id}", row=0,
        )
        profile.callback = self.profile_callback
        self.add_item(profile)

    async def join_callback(self, interaction: discord.Interaction):
        try:
            add_member_to_war(self.war_id, interaction.user.id)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.edit_message(embed=war_embed(self.war_id), view=self)

    async def profile_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_profile(interaction.user.id), ephemeral=True)


def resource_totals(owner_name: str = "Agosto", period: bool = False) -> dict:
    start = setting(GUILD_ID, "wars_rating_period_started_at") if period else ""
    rows = db.conn.execute(
        """SELECT started_at, ended_at, closed, money_per_hour,
                  rating_per_hour, authority_per_hour
           FROM war_ownership_periods WHERE guild_id=? AND owner_name=?""",
        (GUILD_ID, owner_name),
    ).fetchall()
    totals = {"money": 0, "rating": 0, "authority": 0}
    period_start = parse_dt(start) if start else None
    now = datetime.now(timezone.utc)
    for row in rows:
        started = parse_dt(row["started_at"])
        if period_start:
            started = max(started, period_start)
        ended = parse_dt(row["ended_at"]) if row["closed"] and row["ended_at"] else now
        seconds = max(0, int((ended - started).total_seconds()))
        totals["money"] += int(seconds * row["money_per_hour"] / 3600)
        totals["rating"] += int(seconds * row["rating_per_hour"] / 3600)
        totals["authority"] += int(seconds * row["authority_per_hour"] / 3600)
    return totals


def build_history_embed() -> discord.Embed:
    rows = db.conn.execute(
        "SELECT w.*, c.name complex_name FROM wars w JOIN war_complexes c ON c.id=w.complex_id WHERE w.guild_id=? ORDER BY w.id DESC LIMIT 15",
        (GUILD_ID,),
    ).fetchall()
    embed = discord.Embed(title="📜 Історія воєн за комплекси", color=discord.Color.dark_teal())
    if not rows:
        embed.description = "Історія поки порожня."
        return embed
    lines=[]
    for r in rows:
        kind = "⚔️" if r["attack_type"] == "attack" else "🛡️"
        result = "🏆" if r["result"] == "win" else "💀" if r["result"] == "loss" else "🟡"
        lines.append(f"**#{r['id']}** {kind} **{r['complex_name']}** — {r['opponent']} — {result}")
    embed.description = "\n".join(lines)
    return embed


def build_stats_embed() -> discord.Embed:
    wars = db.conn.execute("SELECT COUNT(*) n FROM wars WHERE guild_id=? AND status='finished'", (GUILD_ID,)).fetchone()["n"]
    wins = db.conn.execute("SELECT COUNT(*) n FROM wars WHERE guild_id=? AND status='finished' AND result='win'", (GUILD_ID,)).fetchone()["n"]
    losses = db.conn.execute("SELECT COUNT(*) n FROM wars WHERE guild_id=? AND status='finished' AND result='loss'", (GUILD_ID,)).fetchone()["n"]
    complexes = current_complexes("Agosto")
    cur = resource_totals("Agosto", True)
    total = resource_totals("Agosto", False)
    embed = discord.Embed(title="📊 Статистика війни за комплекси", color=discord.Color.gold())
    embed.add_field(name="⚔️ Війни", value=f"Всього: **{wars}**\n🏆 Перемог: **{wins}**\n💀 Поразок: **{losses}**", inline=True)
    embed.add_field(name="🏢 Комплекси", value=f"Під контролем Agosto: **{len(complexes)}**", inline=True)
    embed.add_field(
        name="💰 Дохід з комплексів",
        value=(
            f"**За поточний період**\n"
            f"💵 {fmt_money(cur['money'])} грошей · 🏆 {cur['rating']} рейтингу · ⭐ {cur['authority']} авторитету\n\n"
            f"**За весь час**\n"
            f"💵 {fmt_money(total['money'])} грошей · 🏆 {total['rating']} рейтингу · ⭐ {total['authority']} авторитету"
        ),
        inline=False,
    )
    return embed


class MainWarView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Активні війни", emoji="⚔️", style=discord.ButtonStyle.primary, custom_id="wars:active")
    async def active(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = db.conn.execute(
            "SELECT id FROM wars WHERE guild_id=? AND status IN ('recruiting','active') ORDER BY id DESC LIMIT 10",
            (GUILD_ID,),
        ).fetchall()
        if not rows:
            return await interaction.response.send_message("Активних воєн за комплекси зараз немає.", ephemeral=True)
        await interaction.response.send_message(
            "\n\n".join(f"**#{r['id']}**" for r in rows),
            ephemeral=True,
        )

    @discord.ui.button(label="Комплекси", emoji="🏢", style=discord.ButtonStyle.secondary, custom_id="wars:complexes")
    async def complexes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_complexes_embed(), ephemeral=True)

    @discord.ui.button(label="Бойовий рейтинг", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="wars:rating")
    async def rating(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_rating_embed(), ephemeral=True)

    @discord.ui.button(label="Мій профіль", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="wars:my_profile")
    async def my_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_profile(interaction.user.id), ephemeral=True)


def build_complexes_embed() -> discord.Embed:
    rows = current_complexes()
    embed = discord.Embed(title="🏢 Комплекси", color=discord.Color.gold())
    if not rows:
        embed.description = "Комплексів ще немає."
        return embed
    lines = []
    for row in rows:
        lines.append(f"**#{row['id']} {row['name']}** — 👑 {row['owner_name']}")
    embed.description = "\n".join(lines)
    embed.add_field(
        name="💰 Нагорода",
        value=(
            f"💵 {fmt_money(int(setting(GUILD_ID,'wars_money_per_hour')))} / год\n"
            f"🏆 {int(setting(GUILD_ID,'wars_rating_per_hour'))} / год\n"
            f"⭐ {int(setting(GUILD_ID,'wars_authority_per_hour'))} / год"
        ),
        inline=False,
    )
    return embed


def rating_rows(period: bool = True, user_id: Optional[int] = None):
    period_start = setting(GUILD_ID, "wars_rating_period_started_at") if period else ""
    params = [GUILD_ID]
    where = "w.guild_id=? AND w.status='finished'"
    if period_start:
        where += " AND w.ended_at >= ?"
        params.append(period_start)
    if user_id is not None:
        where += " AND p.user_id = ?"
        params.append(user_id)
    return db.conn.execute(
        f"""
        SELECT p.user_id,
               SUM(p.total_points) points,
               COUNT(*) participations,
               SUM(CASE WHEN w.result='win' THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN w.result='loss' THEN 1 ELSE 0 END) losses,
               SUM(CASE WHEN w.mvp_user_id=p.user_id THEN 1 ELSE 0 END) mvp
        FROM war_participants p JOIN wars w ON w.id=p.war_id
        WHERE {where}
        GROUP BY p.user_id
        ORDER BY points DESC, participations DESC, p.user_id
        """,
        params,
    ).fetchall()


def build_rating_embed() -> discord.Embed:
    current = rating_rows(True)
    total = rating_rows(False)
    def block(rows):
        if not rows:
            return "Поки немає даних."
        return "\n".join(
            f"**{i}.** <@{r['user_id']}> — **{r['points']}** бал. "
            f"({r['wins']} 🏆 / {r['mvp']} ⭐)"
            for i, r in enumerate(rows[:10], 1)
        )
    embed = discord.Embed(title="🏆 Бойовий рейтинг", color=discord.Color.gold())
    embed.add_field(name="Поточний період", value=block(current), inline=False)
    embed.add_field(name="За весь час", value=block(total), inline=False)
    return embed


def build_profile(user_id: int) -> discord.Embed:
    def one(period):
        rows = rating_rows(period, user_id)
        return rows[0] if rows else None
    cur = one(True)
    all_time = one(False)
    def txt(r):
        if not r:
            return "Бойової статистики ще немає."
        return (
            f"🏆 Бали: **{r['points']}**\n"
            f"🪖 Участей: **{r['participations']}**\n"
            f"🏆 Перемог: **{r['wins']}**\n"
            f"💀 Поразок: **{r['losses']}**\n"
            f"⭐ MVP: **{r['mvp']}**"
        )
    embed = discord.Embed(title=f"👤 Бойовий профіль • {user_id}", color=discord.Color.blurple())
    embed.add_field(name="Поточний період", value=txt(cur), inline=True)
    embed.add_field(name="За весь час", value=txt(all_time), inline=True)
    return embed


async def send_to_channel(bot, channel_id: int, **kwargs):
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    try:
        return await channel.send(**kwargs)
    except Exception as exc:
        print(f"[WARS] Channel send failed {channel_id}: {exc}")
        return None


class RewardModal(discord.ui.Modal, title="💰 Нагорода за комплекс"):
    money = discord.ui.TextInput(label="Гроші / комплекс / год", required=True, default="0")
    rating = discord.ui.TextInput(label="Рейтинг / комплекс / год", required=True, default="0")
    authority = discord.ui.TextInput(label="Авторитет / комплекс / год", required=True, default="0")
    async def on_submit(self, interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        try:
            values = [int(self.money.value), int(self.rating.value), int(self.authority.value)]
            if any(v < 0 for v in values):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Введи цілі невід'ємні числа.", ephemeral=True)
        set_setting(GUILD_ID, "wars_money_per_hour", values[0])
        set_setting(GUILD_ID, "wars_rating_per_hour", values[1])
        set_setting(GUILD_ID, "wars_authority_per_hour", values[2])
        await interaction.response.edit_message(content="✅ Нагороду за комплекс збережено.", view=SettingsView())


class PointsModal(discord.ui.Modal, title="🎖️ Бали рейтингу"):
    participation = discord.ui.TextInput(label="Участь", required=True, default="10")
    win = discord.ui.TextInput(label="Перемога", required=True, default="20")
    mvp = discord.ui.TextInput(label="MVP", required=True, default="10")
    async def on_submit(self, interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        try:
            values = [int(self.participation.value), int(self.win.value), int(self.mvp.value)]
            if any(v < 0 for v in values):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Введи цілі невід'ємні числа.", ephemeral=True)
        set_setting(GUILD_ID, "wars_points_participation", values[0])
        set_setting(GUILD_ID, "wars_points_win", values[1])
        set_setting(GUILD_ID, "wars_points_mvp", values[2])
        await interaction.response.edit_message(content="✅ Бали рейтингу збережено.", view=SettingsView())


class ComplexCreateModal(discord.ui.Modal, title="🏢 Створити комплекс"):
    name = discord.ui.TextInput(label="Назва комплексу", required=True, max_length=80)
    owner = discord.ui.TextInput(label="Початковий власник", required=True, max_length=80)
    async def on_submit(self, interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        name = self.name.value.strip()
        owner = self.owner.value.strip()
        try:
            started = now_iso()
            with db.conn:
                cur = db.conn.execute(
                    "INSERT INTO war_complexes(guild_id,name,owner_name,ownership_started_at,created_at) VALUES(?,?,?,?,?)",
                    (GUILD_ID, name, owner, started, started),
                )
                complex_id = cur.lastrowid
                begin_ownership(complex_id, owner, started)
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Комплекс із такою назвою вже існує.", ephemeral=True)
        await interaction.response.edit_message(content=f"✅ Комплекс **#{complex_id} {name}** створено. Власник: **{owner}**.", view=SettingsView())


class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Нагорода за комплекс", emoji="💰", style=discord.ButtonStyle.primary, row=0)
    async def rewards(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        await interaction.response.send_modal(RewardModal())

    @discord.ui.button(label="Бали рейтингу", emoji="🎖️", style=discord.ButtonStyle.primary, row=0)
    async def points(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        await interaction.response.send_modal(PointsModal())

    @discord.ui.button(label="Комплекси", emoji="🏢", style=discord.ButtonStyle.secondary, row=1)
    async def complexes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        await interaction.response.edit_message(content="🏢 Керування комплексами", view=ComplexesManagementView())

    @discord.ui.button(label="Скинути поточний рейтинг", emoji="🔄", style=discord.ButtonStyle.danger, row=1)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        set_setting(GUILD_ID, "wars_rating_period_started_at", now_iso())
        await interaction.response.edit_message(content="✅ Поточний бойовий рейтинговий період скинуто. Загальна статистика не змінена.", view=SettingsView())


class ComplexesManagementView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Створити", emoji="➕", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ComplexCreateModal())

    @discord.ui.button(label="Список", emoji="📋", style=discord.ButtonStyle.secondary)
    async def list_(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_complexes_embed(), content=None, view=ComplexesManagementView())

    @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="⚙️ Налаштування системи", embed=None, view=SettingsView())


class CreateWarModal(discord.ui.Modal, title="⚔️ Створити війну за комплекс"):
    complex_id = discord.ui.TextInput(label="ID комплексу", required=True)
    attack_type = discord.ui.TextInput(label="Тип: атака або захист", placeholder="атака / захист", required=True)
    opponent = discord.ui.TextInput(label="Противник", required=True, max_length=80)
    limit = discord.ui.TextInput(label="Ліміт учасників (0 = без ліміту)", default="0", required=True)
    mode = discord.ui.TextInput(label="Склад: вручну або набір", placeholder="вручну / набір", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        try:
            cid = int(self.complex_id.value)
            limit = max(0, int(self.limit.value))
        except ValueError:
            return await interaction.response.send_message("ID комплексу та ліміт мають бути числами.", ephemeral=True)
        complex_row = get_complex(cid)
        if not complex_row:
            return await interaction.response.send_message("Комплекс не знайдено.", ephemeral=True)
        typ = self.attack_type.value.strip().lower()
        if typ in ("атака", "атакувати", "attack"):
            typ = "attack"
        elif typ in ("захист", "захищати", "defense", "defence"):
            typ = "defense"
        else:
            return await interaction.response.send_message("Тип має бути `атака` або `захист`.", ephemeral=True)
        mode = self.mode.value.strip().lower()
        if mode in ("набір", "рекрутинг", "recruitment"):
            mode = "recruitment"
        elif mode in ("вручну", "manual"):
            mode = "manual"
        else:
            return await interaction.response.send_message("Склад має бути `вручну` або `набір`.", ephemeral=True)
        # Prevent two simultaneous wars for the same complex.
        exists = db.conn.execute(
            "SELECT 1 FROM wars WHERE guild_id=? AND complex_id=? AND status IN ('recruiting','active') LIMIT 1",
            (GUILD_ID, cid),
        ).fetchone()
        if exists:
            return await interaction.response.send_message("Для цього комплексу вже є активна війна.", ephemeral=True)
        cur = db.conn.execute(
            """
            INSERT INTO wars(guild_id,complex_id,attack_type,opponent,creator_id,status,registration_mode,participant_limit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (GUILD_ID, cid, typ, self.opponent.value.strip(), interaction.user.id, "recruiting", mode, limit, now_iso()),
        )
        war_id = cur.lastrowid
        db.conn.commit()
        if mode == "manual":
            msg = await send_to_channel(interaction.client, WARS_CHANNEL_ID, embed=war_embed(war_id))
            await interaction.response.send_message(
                f"✅ Війну **#{war_id}** створено. Додавай склад через `/wars-add-member`." +
                (" Панель опубліковано." if msg else " Канал війни не налаштований."),
                ephemeral=True,
            )
        else:
            msg = await send_to_channel(interaction.client, WARS_CHANNEL_ID, embed=war_embed(war_id), view=RecruitView(war_id))
            await interaction.response.send_message(f"✅ Війну **#{war_id}** створено." + (" Панель опубліковано." if msg else " Канал війни не налаштований."), ephemeral=True)


class ManageWarView(discord.ui.View):
    def __init__(self, war_id: int):
        super().__init__(timeout=300)
        self.war_id = war_id

    @discord.ui.button(label="Почати війну", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        war = get_active_war(self.war_id)
        if not war:
            return await interaction.response.send_message("Війну не знайдено.", ephemeral=True)
        db.conn.execute("UPDATE wars SET status='active', started_at=? WHERE id=? AND guild_id=?", (now_iso(), self.war_id, GUILD_ID))
        db.conn.commit()
        await interaction.response.edit_message(embed=war_embed(self.war_id), view=self)

    @discord.ui.button(label="Скасувати", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        war = get_active_war(self.war_id)
        if not war:
            return await interaction.response.send_message("Війну не знайдено.", ephemeral=True)
        db.conn.execute("UPDATE wars SET status='cancelled', ended_at=? WHERE id=? AND guild_id=?", (now_iso(), self.war_id, GUILD_ID))
        db.conn.commit()
        await interaction.response.edit_message(embed=war_embed(self.war_id), view=None)



def complete_war_record(war_id: int, result: str, mvp_user_id: Optional[str], comment: Optional[str]) -> None:
    war = get_active_war(war_id)
    if not war:
        raise ValueError("Активну війну не знайдено.")
    result = result.strip().lower()
    if result in ("перемога", "win", "победа"):
        result = "win"
    elif result in ("поразка", "loss", "поражение"):
        result = "loss"
    else:
        raise ValueError("Результат: `win` або `loss`.")
    try:
        mvp = int(mvp_user_id) if mvp_user_id and mvp_user_id.strip() else None
    except ValueError as exc:
        raise ValueError("MVP має бути Discord ID.") from exc
    ids = participant_ids(war_id)
    if mvp is not None and mvp not in ids:
        raise ValueError("MVP має бути учасником цієї війни.")
    participation_points = int(setting(GUILD_ID, "wars_points_participation"))
    win_points = int(setting(GUILD_ID, "wars_points_win")) if result == "win" else 0
    mvp_points = int(setting(GUILD_ID, "wars_points_mvp"))
    ended = now_iso()
    with db.conn:
        for uid in ids:
            mp = mvp_points if uid == mvp else 0
            total = participation_points + win_points + mp
            db.conn.execute(
                "UPDATE war_participants SET participation_points=?, win_points=?, mvp_points=?, total_points=?, status='completed' WHERE war_id=? AND user_id=?",
                (participation_points, win_points, mp, total, war_id, uid),
            )
        db.conn.execute(
            "UPDATE wars SET status='finished', ended_at=?, result=?, mvp_user_id=?, commander_comment=? WHERE id=? AND guild_id=?",
            (ended, result, mvp, comment.strip() if comment else None, war_id, GUILD_ID),
        )
        if (war["attack_type"] == "attack" and result == "win") or (war["attack_type"] == "defense" and result == "loss"):
            change_owner(war["complex_id"], war["opponent"] if war["attack_type"] == "defense" else "Agosto", ended)
        else:
            complex_row = get_complex(war["complex_id"])
            if complex_row:
                settle_ownership(war["complex_id"], ended)
                begin_ownership(war["complex_id"], complex_row["owner_name"], ended)


class WarManageModal(discord.ui.Modal, title="🎛️ Керувати війною"):
    war_id = discord.ui.TextInput(label="ID війни", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        try:
            war_id = int(self.war_id.value)
        except ValueError:
            return await interaction.response.send_message("ID війни має бути числом.", ephemeral=True)
        if not get_active_war(war_id):
            return await interaction.response.send_message("Активну війну не знайдено.", ephemeral=True)
        await interaction.response.send_message(embed=war_embed(war_id), view=ManageWarView(war_id), ephemeral=True)


class AddMemberModal(discord.ui.Modal, title="👥 Додати учасника"):
    war_id = discord.ui.TextInput(label="ID війни", required=True)
    user_id = discord.ui.TextInput(label="Discord ID учасника", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            war_id, user_id = int(self.war_id.value), int(self.user_id.value)
            if user_id <= 0:
                raise ValueError("Учасника з таким ID немає на сервері.")
            if interaction.guild.get_member(user_id) is None:
                try:
                    await interaction.guild.fetch_member(user_id)
                except discord.NotFound as exc:
                    raise ValueError("Учасника з таким ID немає на сервері.") from exc
                except discord.HTTPException as exc:
                    raise ValueError("Не вдалося перевірити учасника. Спробуй ще раз.") from exc
            add_member_to_war(war_id, user_id)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        await interaction.followup.send(f"✅ <@{user_id}> доданий/а до війни **#{war_id}**.", ephemeral=True)


class CompleteWarModal(discord.ui.Modal, title="🏁 Завершити війну"):
    war_id = discord.ui.TextInput(label="ID війни", required=True)
    result = discord.ui.TextInput(label="Результат: win або loss", required=True)
    mvp_user_id = discord.ui.TextInput(label="Discord ID MVP (необов'язково)", required=False)
    comment = discord.ui.TextInput(label="Коментар (необов'язково)", required=False, style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        try:
            war_id = int(self.war_id.value)
            complete_war_record(war_id, self.result.value, self.mvp_user_id.value, self.comment.value)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        embed = war_embed(war_id)
        await interaction.response.send_message(f"✅ Війну **#{war_id}** завершено.", embed=embed, ephemeral=True)
        if WARS_REPORTS_CHANNEL_ID:
            await send_to_channel(interaction.client, WARS_REPORTS_CHANNEL_ID, embed=embed)


class CommanderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Створити війну", emoji="➕", style=discord.ButtonStyle.success, custom_id="wars:commander:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_modal(CreateWarModal())

    @discord.ui.button(label="Історія", emoji="📜", style=discord.ButtonStyle.secondary, row=1, custom_id="wars:commander:history")
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message(embed=build_history_embed(), ephemeral=True)

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary, row=1, custom_id="wars:commander:stats")
    async def statistics(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message(embed=build_stats_embed(), ephemeral=True)

    @discord.ui.button(label="Керувати війною", emoji="🎛️", style=discord.ButtonStyle.secondary, row=1, custom_id="wars:commander:manage")
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_modal(WarManageModal())

    @discord.ui.button(label="Додати учасника", emoji="👥", style=discord.ButtonStyle.secondary, row=2, custom_id="wars:commander:add")
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_modal(AddMemberModal())

    @discord.ui.button(label="Завершити війну", emoji="🏁", style=discord.ButtonStyle.primary, row=2, custom_id="wars:commander:complete")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_modal(CompleteWarModal())


class ModeratorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Налаштування", emoji="⚙️", style=discord.ButtonStyle.primary, custom_id="wars:moderator:settings")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_message("⚙️ Налаштування системи", view=SettingsView(), ephemeral=True)

    @discord.ui.button(label="Комплекси", emoji="🏢", style=discord.ButtonStyle.secondary, custom_id="wars:moderator:complexes")
    async def complexes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_message("🏢 Керування комплексами", view=ComplexesManagementView(), ephemeral=True)

    @discord.ui.button(label="Статистика", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="wars:moderator:stats")
    async def statistics(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_message(embed=build_stats_embed(), ephemeral=True)


async def publish_panels(bot) -> int:
    published = 0
    if WARS_CHANNEL_ID:
        message = await send_to_channel(bot, WARS_CHANNEL_ID, embed=discord.Embed(
            title="⚔️ Війни за комплекси",
            description="Запис, активні війни, комплекси, бойовий рейтинг та особистий профіль.",
            color=discord.Color.dark_red(),
        ), view=MainWarView())
        published += message is not None
    if WARS_MANAGEMENT_CHANNEL_ID:
        message = await send_to_channel(bot, WARS_MANAGEMENT_CHANNEL_ID, embed=discord.Embed(
            title="🎖️ Панель командирів",
            description="Створення, склад, початок і завершення воєн.",
            color=discord.Color.gold(),
        ), view=CommanderView())
        published += message is not None
    moderation_channel = WARS_MODERATION_CHANNEL_ID or WARS_MANAGEMENT_CHANNEL_ID
    if moderation_channel:
        message = await send_to_channel(bot, moderation_channel, embed=discord.Embed(
            title="⚙️ Панель модераторів",
            description="Налаштування ресурсів, балів і комплексів доступні керівництву.",
            color=discord.Color.blurple(),
        ), view=ModeratorView())
        published += message is not None
    return published


async def restore_active_views(bot) -> None:
    """Restore persistent recruitment buttons after a Railway restart."""
    rows = db.conn.execute(
        "SELECT id FROM wars WHERE guild_id=? AND status='recruiting' ORDER BY id",
        (GUILD_ID,),
    ).fetchall()
    for row in rows:
        bot.add_view(RecruitView(int(row["id"])))
    bot.add_view(MainWarView())
    bot.add_view(CommanderView())
    bot.add_view(ModeratorView())


def register_commands(bot) -> None:
    @bot.tree.command(name="wars", description="Панель війн за комплекси")
    async def wars_command(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚔️ Війни за комплекси",
                description="Керування бойовою системою Agosto.",
                color=discord.Color.dark_red(),
            ),
            view=MainWarView(),
            ephemeral=True,
        )

    @bot.tree.command(name="wars-setup", description="Опублікувати панелі війн за комплекси")
    async def wars_setup(interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        published = await publish_panels(interaction.client)
        await interaction.followup.send(
            f"✅ Опубліковано панелей: **{published}**." if published else
            "Не вдалося опублікувати панелі. Перевір ID каналів і дозвіл бота писати в них.",
            ephemeral=True,
        )

    @bot.tree.command(name="wars-settings", description="Налаштування системи війн")
    async def wars_settings(interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_message("⚙️ Налаштування системи", view=SettingsView(), ephemeral=True)

    @bot.tree.command(name="wars-create", description="Створити війну за комплекс")
    async def wars_create(interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_modal(CreateWarModal())

    @bot.tree.command(name="wars-profile", description="Мій бойовий профіль")
    async def wars_profile(interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_profile(interaction.user.id), ephemeral=True)

    @bot.tree.command(name="wars-rating", description="Бойовий рейтинг")
    async def wars_rating(interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_rating_embed(), ephemeral=True)

    @bot.tree.command(name="wars-complexes", description="Список комплексів")
    async def wars_complexes(interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_complexes_embed(), ephemeral=True)

    @bot.tree.command(name="wars-add-member", description="Додати учасника до війни вручну")
    @app_commands.describe(war_id="ID війни", member="Учасник")
    async def wars_add_member(interaction: discord.Interaction, war_id: int, member: discord.Member):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        try:
            add_member_to_war(war_id, member.id)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.send_message(f"✅ {member.mention} доданий/а до війни **#{war_id}**.", ephemeral=True)

    @bot.tree.command(name="wars-history", description="Історія воєн за комплекси")
    async def wars_history(interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message(embed=build_history_embed(), ephemeral=True)

    @bot.tree.command(name="wars-stats", description="Статистика воєн за комплекси")
    async def wars_stats(interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message(embed=build_stats_embed(), ephemeral=True)

    @bot.tree.command(name="wars-manage", description="Керувати конкретною війною")
    @app_commands.describe(war_id="ID війни")
    async def wars_manage(interaction: discord.Interaction, war_id: int):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        if not get_active_war(war_id):
            return await interaction.response.send_message("Активну війну не знайдено.", ephemeral=True)
        await interaction.response.send_message(embed=war_embed(war_id), view=ManageWarView(war_id), ephemeral=True)

    @bot.tree.command(name="wars-complete", description="Завершити війну за комплекс")
    @app_commands.describe(war_id="ID війни", result="win/loss", mvp_user_id="Discord ID MVP", comment="Необов'язковий коментар")
    async def wars_complete(interaction: discord.Interaction, war_id: int, result: str, mvp_user_id: Optional[str] = None, comment: Optional[str] = None):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        try:
            complete_war_record(war_id, result, mvp_user_id, comment)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.send_message(f"✅ Війну **#{war_id}** завершено. {war_embed(war_id).description}", ephemeral=True)
        if WARS_REPORTS_CHANNEL_ID:
            await send_to_channel(interaction.client, WARS_REPORTS_CHANNEL_ID, embed=war_embed(war_id))
