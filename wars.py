import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord

from config import GUILD_ID, LOCAL_TZ
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

MARKERS = {
    "industrial": ("🏭", "Промислова зона"),
    "military": ("🛡️", "Військовий об'єкт"),
    "agriculture": ("🌾", "Сільське господарство"),
    "other": ("📍", "Інший об'єкт"),
    "commercial": ("🏬", "Комерційний об'єкт"),
    "energy": ("⚡", "Енергетичний об'єкт"),
}


def complex_marker(row):
    return MARKERS.get(row["marker_type"], ("🏢", "Комплекс"))


def complex_name(row):
    """Old rows stored the ID twice; show it only in the numbered prefix."""
    name = row["name"]
    suffix = f" #{row['id']}"
    return name[:-len(suffix)] if name.endswith(suffix) else name


def capture_date(row) -> str:
    return parse_dt(row["ownership_started_at"]).astimezone(LOCAL_TZ).strftime("%d.%m.%Y о %H:%M")

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
    for table, column, definition in (
        ("war_complexes", "marker_type", "TEXT NOT NULL DEFAULT 'complex'"),
        ("wars", "message_id", "INTEGER"),
        ("wars", "channel_id", "INTEGER"),
    ):
        if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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
        "UPDATE war_complexes SET owner_name=?, active=?, ownership_started_at=? WHERE id=? AND guild_id=?",
        (new_owner, int(new_owner == "Agosto"), at, complex_id, GUILD_ID),
    )
    if new_owner == "Agosto":
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
        SELECT w.*, c.name AS complex_name, c.owner_name, c.marker_type
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
    mode_label = "Відкритий набір" if row["registration_mode"] == "recruitment" else "Записує командир"
    name = complex_name({"name": row["complex_name"], "id": row["complex_id"]})
    lines = [f"**{i}.** <@{uid}>" for i, uid in enumerate(parts[:20], 1)]
    if len(parts) > 20:
        lines.append(f"…і ще {len(parts) - 20} учасників")
    if not lines:
        lines = ["Поки ніхто не записався."]
    embed = discord.Embed(
        title=f"{kind} • війна #{war_id} · {complex_marker(row)[0]} {name}",
        description=(
            f"📍 Об'єкт: **#{row['complex_id']:03d} {name}**\n"
            f"🎯 Суперник: **{row['opponent']}**\n"
            f"👑 Контроль: **{row['owner_name']}**\n"
            f"📝 Склад: **{mode_label}**\n"
            f"📌 Статус: **{status}**\n"
            f"👥 Учасників: **{len(parts)}" + (f"/{row['participant_limit']}**" if row["participant_limit"] else "**")
        ),
        color=discord.Color.dark_red() if row["attack_type"] == "attack" else discord.Color.blue(),
    )
    embed.add_field(name="👥 Склад", value="\n".join(lines), inline=False)
    if row["result"]:
        result = "🏆 Перемога" if row["result"] == "win" else "💀 Поразка"
        embed.add_field(name="Результат", value=result, inline=True)
    if row["mvp_user_id"]:
        embed.add_field(name="⭐ MVP", value=f"<@{row['mvp_user_id']}>", inline=True)
    if row["started_at"] or row["ended_at"]:
        times = []
        if row["started_at"]:
            times.append(f"Початок: <t:{unix(row['started_at'])}:f>")
        if row["ended_at"]:
            times.append(f"Завершення: <t:{unix(row['ended_at'])}:f>")
        embed.add_field(name="🕒 Час", value="\n".join(times), inline=False)
    if row["commander_comment"]:
        embed.add_field(name="💬 Коментар командира", value=row["commander_comment"][:1024], inline=False)
    return embed


class WarCardView(discord.ui.View):
    def __init__(self, war_id: int, status: str, mode: str):
        super().__init__(timeout=None)
        self.war_id = war_id
        if status == "recruiting" and mode == "recruitment":
            self.add_action("Беру участь", "🪖", discord.ButtonStyle.success, "join", self.join_callback)
        if status == "recruiting":
            self.add_action("Додати учасника", "👥", discord.ButtonStyle.secondary, "member", self.member_callback)
            self.add_action("Почати", "▶️", discord.ButtonStyle.primary, "start", self.start_callback)
        if status == "active":
            self.add_action("Завершити війну", "🏁", discord.ButtonStyle.success, "finish", self.finish_callback)
        self.add_action("Скасувати", "🗑️", discord.ButtonStyle.danger, "cancel", self.cancel_callback, row=1)

    def add_action(self, label, emoji, style, action, callback, row=0):
        button = discord.ui.Button(label=label, emoji=emoji, style=style,
                                   custom_id=f"wars:{action}:{self.war_id}", row=row)
        button.callback = callback
        self.add_item(button)

    async def join_callback(self, interaction: discord.Interaction):
        war = get_active_war(self.war_id)
        if not war or war["status"] != "recruiting" or war["registration_mode"] != "recruitment":
            return await interaction.response.send_message("Набір уже закритий.", ephemeral=True)
        try:
            add_member_to_war(self.war_id, interaction.user.id)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await interaction.response.edit_message(embed=war_embed(self.war_id), view=self)

    async def member_callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message("Оберіть учасника:", view=WarMemberView(self.war_id), ephemeral=True)

    async def start_callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        with db.conn:
            changed = db.conn.execute(
                "UPDATE wars SET status='active', started_at=? WHERE id=? AND guild_id=? AND status='recruiting'",
                (now_iso(), self.war_id, GUILD_ID),
            ).rowcount
        if not changed:
            return await interaction.response.send_message("Війна вже почалася або завершена.", ephemeral=True)
        war = get_active_war(self.war_id)
        await interaction.response.edit_message(embed=war_embed(self.war_id), view=WarCardView(self.war_id, war["status"], war["registration_mode"]))

    async def finish_callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        war = get_active_war(self.war_id)
        if not war or war["status"] != "active":
            return await interaction.response.send_message("Можна завершити тільки розпочату війну.", ephemeral=True)
        if len(participant_ids(self.war_id)) > 24:
            return await interaction.response.send_message(
                "Оберіть MVP зі складу (або «Без MVP»):", view=WarMVPView(self.war_id, interaction.guild), ephemeral=True,
            )
        await interaction.response.send_modal(CompleteWarModal(self.war_id, interaction.guild))

    async def cancel_callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        with db.conn:
            changed = db.conn.execute(
                "UPDATE wars SET status='cancelled', ended_at=? WHERE id=? AND guild_id=? AND status IN ('recruiting','active')",
                (now_iso(), self.war_id, GUILD_ID),
            ).rowcount
        if not changed:
            return await interaction.response.send_message("Ця війна вже завершена.", ephemeral=True)
        await interaction.response.edit_message(embed=war_embed(self.war_id), view=None)

async def refresh_war_message(bot, war_id: int) -> None:
    war = db.conn.execute("SELECT * FROM wars WHERE id=? AND guild_id=?", (war_id, GUILD_ID)).fetchone()
    if not war or not war["channel_id"] or not war["message_id"]:
        return
    channel = bot.get_channel(war["channel_id"])
    try:
        if channel is None:
            channel = await bot.fetch_channel(war["channel_id"])
        message = await channel.fetch_message(war["message_id"])
        view = WarCardView(war_id, war["status"], war["registration_mode"]) if war["status"] in ("recruiting", "active") else None
        await message.edit(embed=war_embed(war_id), view=view)
    except discord.HTTPException as exc:
        print(f"[WARS] Cannot update war card #{war_id}: {exc}")


async def refresh_existing_war_cards(bot) -> None:
    """Remove stale controls from already posted war cards after an update."""
    await bot.wait_until_ready()
    rows = db.conn.execute(
        "SELECT id FROM wars WHERE guild_id=? AND status IN ('recruiting','active') "
        "AND message_id IS NOT NULL ORDER BY id", (GUILD_ID,),
    ).fetchall()
    for row in rows:
        await refresh_war_message(bot, int(row["id"]))


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
        lines.append(f"**#{r['id']}** {kind} **{complex_name({'name': r['complex_name'], 'id': r['complex_id']})}** — {r['opponent']} — {result}")
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
            f"$ {fmt_money(cur['money'])} · 🏆 {cur['rating']} рейтингу · 👑 {cur['authority']} авторитету\n\n"
            f"**За весь час**\n"
            f"$ {fmt_money(total['money'])} · 🏆 {total['rating']} рейтингу · 👑 {total['authority']} авторитету"
        ),
        inline=False,
    )
    return embed


def clear_war_history() -> list[tuple[int, int]]:
    """Clear this guild's war data, including captured holdings and accrual history."""
    cards = db.conn.execute(
        "SELECT channel_id,message_id FROM wars WHERE guild_id=? AND channel_id IS NOT NULL AND message_id IS NOT NULL",
        (GUILD_ID,),
    ).fetchall()
    with db.conn:
        db.conn.execute("DELETE FROM war_participants WHERE guild_id=?", (GUILD_ID,))
        db.conn.execute("DELETE FROM war_resource_ledger WHERE guild_id=?", (GUILD_ID,))
        db.conn.execute("DELETE FROM war_ownership_periods WHERE guild_id=?", (GUILD_ID,))
        db.conn.execute("DELETE FROM wars WHERE guild_id=?", (GUILD_ID,))
        db.conn.execute("DELETE FROM war_complexes WHERE guild_id=?", (GUILD_ID,))
        db.conn.execute(
            "INSERT INTO bot_settings(guild_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
            (GUILD_ID, "wars_rating_period_started_at", now_iso()),
        )
    return [(int(row["channel_id"]), int(row["message_id"])) for row in cards]


class ClearWarsModal(discord.ui.Modal, title="🗑️ Очистити всі дані воєн"):
    confirmation = discord.ui.TextInput(
        label="Введіть ОЧИСТИТИ", placeholder="ОЧИСТИТИ", required=True, max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        if self.confirmation.value.strip() != "ОЧИСТИТИ":
            return await interaction.response.send_message("Очищення скасовано: підтвердження не збігається.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cards = clear_war_history()
        failed = 0
        for channel_id, message_id in cards:
            try:
                channel = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
                await message.edit(content="🗑️ Тестову історію воєн очищено.", embed=None, view=None)
            except discord.DiscordException:
                failed += 1
        await interaction.followup.send(
            f"✅ Історію воєн, учасників, комплекси й дохід очищено. Карток: **{len(cards)}**. "
            + (f"Не вдалося прибрати кнопки на **{failed}** старих картках." if failed else ""),
            ephemeral=True,
        )


class MainWarView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Активні війни", emoji="⚔️", style=discord.ButtonStyle.primary, custom_id="wars:active")
    async def active(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = db.conn.execute(
            "SELECT id,channel_id,message_id,status FROM wars WHERE guild_id=? AND status IN ('recruiting','active') ORDER BY id DESC LIMIT 10",
            (GUILD_ID,),
        ).fetchall()
        if not rows:
            return await interaction.response.send_message("Активних воєн за комплекси зараз немає.", ephemeral=True)
        await interaction.response.send_message(
            "\n".join(
                f"**Війна #{r['id']}** · {'Набір' if r['status']=='recruiting' else 'Триває'} · "
                + (f"[Відкрити](https://discord.com/channels/{GUILD_ID}/{r['channel_id']}/{r['message_id']})"
                   if r['message_id'] and r['channel_id'] else "Картка недоступна")
                for r in rows
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Комплекси", emoji="🏢", style=discord.ButtonStyle.secondary, custom_id="wars:complexes")
    async def complexes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_complexes_embed(), view=HoldingsView(), ephemeral=True)

    @discord.ui.button(label="Бойовий рейтинг", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="wars:rating")
    async def rating(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_rating_embed(), ephemeral=True)

    @discord.ui.button(label="Мій профіль", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="wars:my_profile")
    async def my_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_profile(interaction.user.id), ephemeral=True)


def build_complexes_embed(page: int = 0) -> discord.Embed:
    rows = current_complexes("Agosto")
    pages = max(1, (len(rows) + 14) // 15)
    page = min(max(page, 0), pages - 1)
    embed = discord.Embed(title="🏢 Наші комплекси", color=discord.Color.gold())
    if not rows:
        embed.description = "Підконтрольних комплексів ще немає. Виграйте напад, щоб отримати перший."
        return embed
    lines = []
    for row in rows[page * 15:(page + 1) * 15]:
        rate = db.conn.execute(
            "SELECT money_per_hour,rating_per_hour,authority_per_hour FROM war_ownership_periods "
            "WHERE complex_id=? AND guild_id=? AND closed=0 ORDER BY id DESC LIMIT 1",
            (row["id"], GUILD_ID),
        ).fetchone()
        earnings = (f" · $ {fmt_money(rate['money_per_hour'])} · 🏆 {rate['rating_per_hour']} · 👑 {rate['authority_per_hour']} / год"
                    if rate else "")
        lines.append(f"**#{row['id']:03d}** {complex_marker(row)[0]} **{complex_name(row)}**{earnings}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Сторінка {page + 1}/{pages} · Ставки фіксуються при отриманні")
    return embed


class HoldingsView(discord.ui.View):
    def __init__(self, page: int = 0):
        super().__init__(timeout=300)
        count = len(current_complexes("Agosto"))
        self.page = min(max(page, 0), max(0, (count - 1) // 15))
        self.previous.disabled = self.page == 0
        self.next.disabled = (self.page + 1) * 15 >= count

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_complexes_embed(self.page - 1), view=HoldingsView(self.page - 1))

    @discord.ui.button(label="Далі", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_complexes_embed(self.page + 1), view=HoldingsView(self.page + 1))


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
    money = discord.ui.TextInput(label="$ / комплекс / год", required=True, default="0")
    rating = discord.ui.TextInput(label="Рейтинг / комплекс / год", required=True, default="0")
    authority = discord.ui.TextInput(label="Авторитет 👑 / комплекс / год", required=True, default="0")
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

    @discord.ui.button(label="Скинути поточний рейтинг", emoji="🔄", style=discord.ButtonStyle.danger, row=1)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        set_setting(GUILD_ID, "wars_rating_period_started_at", now_iso())
        await interaction.response.edit_message(content="✅ Поточний бойовий рейтинговий період скинуто. Загальна статистика не змінена.", view=SettingsView())

    @discord.ui.button(label="Очистити всі дані воєн", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_modal(ClearWarsModal())


class WarTypeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Оберіть тип об'єкта для нападу", options=[
            discord.SelectOption(label=label, value=key, emoji=emoji)
            for key, (emoji, label) in MARKERS.items()
        ])

    async def callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message(
            "Як набирати учасників?", view=RecruitModeView("attack", marker_type=self.values[0]), ephemeral=True,
        )


class AttackTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(WarTypeSelect())


class DefenseSelect(discord.ui.Select):
    def __init__(self, rows):
        super().__init__(placeholder="Оберіть наш комплекс", options=[
            discord.SelectOption(
                label=f"#{r['id']:03d} {complex_name(r)[:90]}",
                value=str(r["id"]), emoji=complex_marker(r)[0],
                description=f"Захоплено: {capture_date(r)}",
            )
            for r in rows
        ])

    async def callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message(
            "Як набирати учасників?", view=RecruitModeView("defense", complex_id=int(self.values[0])), ephemeral=True,
        )


class DefenseView(discord.ui.View):
    def __init__(self, page=0):
        super().__init__(timeout=300)
        rows = current_complexes("Agosto")
        self.page = min(max(page, 0), max(0, (len(rows) - 1) // 25))
        portion = rows[self.page * 25:(self.page + 1) * 25]
        if portion:
            self.add_item(DefenseSelect(portion))
        self.previous.disabled = self.page == 0
        self.next.disabled = (self.page + 1) * 25 >= len(rows)

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Оберіть наш комплекс для захисту:", view=DefenseView(self.page - 1))

    @discord.ui.button(label="Далі", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Оберіть наш комплекс для захисту:", view=DefenseView(self.page + 1))


class RecruitModeView(discord.ui.View):
    def __init__(self, attack_type: str, marker_type: str = "", complex_id: int = 0):
        super().__init__(timeout=300)
        self.attack_type = attack_type
        self.marker_type = marker_type
        self.complex_id = complex_id

    @discord.ui.button(label="Відкритий набір", emoji="🪖", style=discord.ButtonStyle.success)
    async def open_recruitment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_modal(CreateWarModal(
            self.attack_type, "recruitment", self.marker_type, self.complex_id,
        ))

    @discord.ui.button(label="Записати учасників", emoji="👥", style=discord.ButtonStyle.primary)
    async def manual_recruitment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_modal(CreateWarModal(
            self.attack_type, "manual", self.marker_type, self.complex_id,
        ))


class CreateWarModal(discord.ui.Modal, title="⚔️ Почати війну"):
    opponent = discord.ui.TextInput(label="Сім'я суперника", placeholder="Назва сім'ї", required=True, max_length=80)

    def __init__(self, attack_type: str, mode: str, marker_type: str = "", complex_id: int = 0):
        super().__init__()
        self.attack_type = attack_type
        self.mode = mode
        self.marker_type = marker_type
        self.complex_id = complex_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        if not WARS_CHANNEL_ID:
            return await interaction.response.send_message("Спочатку налаштуйте WARS_CHANNEL_ID.", ephemeral=True)
        opponent = self.opponent.value.strip()
        if opponent.casefold() == "agosto":
            return await interaction.response.send_message("Вкажіть сім'ю суперника.", ephemeral=True)
        if self.attack_type == "attack" and self.marker_type not in MARKERS:
            return await interaction.response.send_message("Невідомий тип комплексу.", ephemeral=True)
        if self.attack_type == "defense":
            row = get_complex(self.complex_id)
            if not row or row["owner_name"] != "Agosto":
                return await interaction.response.send_message("Цей комплекс більше нам не належить.", ephemeral=True)
            exists = db.conn.execute(
                "SELECT 1 FROM wars WHERE guild_id=? AND complex_id=? AND status IN ('recruiting','active')",
                (GUILD_ID, self.complex_id),
            ).fetchone()
            if exists:
                return await interaction.response.send_message("За цей комплекс уже триває війна.", ephemeral=True)
        started = now_iso()
        with db.conn:
            if self.attack_type == "attack":
                label = MARKERS[self.marker_type][1]
                cur = db.conn.execute(
                    "INSERT INTO war_complexes(guild_id,name,owner_name,active,ownership_started_at,created_at,marker_type) VALUES(?,?,?,?,?,?,?)",
                    (GUILD_ID, f"Об'єкт {started}", opponent, 0, started, started, self.marker_type),
                )
                cid = cur.lastrowid
                db.conn.execute("UPDATE war_complexes SET name=? WHERE id=?", (f"{label} #{cid}", cid))
            else:
                cid = self.complex_id
            cur = db.conn.execute(
                "INSERT INTO wars(guild_id,complex_id,attack_type,opponent,creator_id,status,registration_mode,participant_limit,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (GUILD_ID, cid, self.attack_type, opponent, interaction.user.id, "recruiting", self.mode, 0, started),
            )
            war_id = cur.lastrowid
        msg = await send_to_channel(interaction.client, WARS_CHANNEL_ID,
                                    embed=war_embed(war_id), view=WarCardView(war_id, "recruiting", self.mode))
        if not msg:
            with db.conn:
                db.conn.execute("UPDATE wars SET status='cancelled',ended_at=? WHERE id=?", (now_iso(), war_id))
            return await interaction.response.send_message("Не вдалося опублікувати війну. Перевірте доступ бота до каналу.", ephemeral=True)
        with db.conn:
            db.conn.execute("UPDATE wars SET message_id=?, channel_id=? WHERE id=?", (msg.id, msg.channel.id, war_id))
        await interaction.response.send_message(f"✅ Війну створено. [Відкрити запис]({msg.jump_url})", ephemeral=True)


def complete_war_record(war_id: int, result: str, mvp_user_id: Optional[int], comment: Optional[str]) -> None:
    war = get_active_war(war_id)
    if not war or war["status"] != "active":
        raise ValueError("Можна завершити тільки розпочату війну.")
    result = result.strip().lower()
    if result in ("перемога", "win", "победа"):
        result = "win"
    elif result in ("поразка", "loss", "поражение"):
        result = "loss"
    else:
        raise ValueError("Результат: `win` або `loss`.")
    try:
        mvp = int(mvp_user_id) if mvp_user_id else None
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
        if war["attack_type"] == "attack" and result == "win":
            change_owner(war["complex_id"], "Agosto", ended)
        elif war["attack_type"] == "defense" and result == "loss":
            change_owner(war["complex_id"], war["opponent"], ended)
        # A lost attack never joins our holdings. A successful defense keeps
        # the same ownership period and its resource accrual intact.


class WarMemberSelect(discord.ui.UserSelect):
    def __init__(self, war_id: int):
        super().__init__(placeholder="Оберіть учасника")
        self.war_id = war_id

    async def callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        user_id = self.values[0].id
        try:
            war_id = self.war_id
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
        await refresh_war_message(interaction.client, war_id)
        await interaction.followup.send(f"✅ <@{user_id}> доданий/а до війни **#{war_id}**.", ephemeral=True)


class WarMemberView(discord.ui.View):
    def __init__(self, war_id: int):
        super().__init__(timeout=300)
        self.add_item(WarMemberSelect(war_id))


class CompleteWarModal(discord.ui.Modal):
    def __init__(self, war_id: int, guild, preselected: bool = False, mvp_user_id: Optional[int] = None):
        title = f"🏁 Завершити війну #{war_id}"
        if preselected:
            title += " · MVP обрано" if mvp_user_id else " · без MVP"
        super().__init__(title=title[:45])
        self.war_id = war_id
        self.preselected = preselected
        self.mvp_user_id = mvp_user_id
        self.result_choice = discord.ui.Label(
            text="Результат війни",
            component=discord.ui.Select(
                placeholder="Оберіть результат",
                options=[discord.SelectOption(label="Перемога", value="win", emoji="🏆"),
                         discord.SelectOption(label="Поразка", value="loss", emoji="💀")],
            ),
        )
        self.add_item(self.result_choice)
        if not preselected:
            options = [discord.SelectOption(label="Без MVP", value="none", emoji="➖")]
            for uid in participant_ids(war_id):
                member = guild.get_member(uid) if guild else None
                label = member.display_name if member else f"Учасник {uid}"
                options.append(discord.SelectOption(label=label[:100], value=str(uid), emoji="⭐"))
            self.mvp_choice = discord.ui.Label(
                text="MVP зі складу",
                component=discord.ui.Select(placeholder="Оберіть учасника або «Без MVP»", options=options),
            )
            self.add_item(self.mvp_choice)
        self.comment_choice = discord.ui.Label(
            text="Коментар (необов'язково)",
            component=discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=600),
        )
        self.add_item(self.comment_choice)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        result = self.result_choice.component.values[0]
        if self.preselected:
            mvp = self.mvp_user_id
        else:
            chosen = self.mvp_choice.component.values[0]
            mvp = None if chosen == "none" else int(chosen)
        try:
            complete_war_record(self.war_id, result, mvp, self.comment_choice.component.value)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        embed = war_embed(self.war_id)
        await interaction.response.defer(ephemeral=True)
        await refresh_war_message(interaction.client, self.war_id)
        await interaction.followup.send(f"✅ Війну **#{self.war_id}** завершено.", embed=embed, ephemeral=True)
        if WARS_REPORTS_CHANNEL_ID:
            await send_to_channel(interaction.client, WARS_REPORTS_CHANNEL_ID, embed=embed)


class WarMVPSelect(discord.ui.Select):
    def __init__(self, war_id: int, guild, page: int):
        self.war_id = war_id
        ids = participant_ids(war_id)[page * 24:(page + 1) * 24]
        options = [discord.SelectOption(label="Без MVP", value="none", emoji="➖")]
        for uid in ids:
            member = guild.get_member(uid) if guild else None
            label = member.display_name if member else f"Учасник {uid}"
            options.append(discord.SelectOption(label=label[:100], value=str(uid), emoji="⭐"))
        super().__init__(placeholder="Оберіть MVP або «Без MVP»", options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        chosen = self.values[0]
        mvp = None if chosen == "none" else int(chosen)
        if mvp is not None and mvp not in participant_ids(self.war_id):
            return await interaction.response.send_message("Учасника вже немає в цій війні.", ephemeral=True)
        await interaction.response.send_modal(CompleteWarModal(self.war_id, interaction.guild, preselected=True, mvp_user_id=mvp))


class WarMVPView(discord.ui.View):
    def __init__(self, war_id: int, guild, page: int = 0):
        super().__init__(timeout=300)
        self.war_id = war_id
        ids = participant_ids(war_id)
        self.page = min(max(page, 0), max(0, (len(ids) - 1) // 24))
        self.add_item(WarMVPSelect(war_id, guild, self.page))
        self.previous.disabled = self.page == 0
        self.next.disabled = (self.page + 1) * 24 >= len(ids)

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        await interaction.response.edit_message(view=WarMVPView(self.war_id, interaction.guild, self.page - 1))

    @discord.ui.button(label="Далі", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Недостатньо прав.", ephemeral=True)
        await interaction.response.edit_message(view=WarMVPView(self.war_id, interaction.guild, self.page + 1))


class CommanderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Почати війну", emoji="⚔️", style=discord.ButtonStyle.success, custom_id="wars:commander:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message("Оберіть дію:", view=WarCreationView(), ephemeral=True)

    @discord.ui.button(label="Поточні війни", emoji="📌", style=discord.ButtonStyle.primary, custom_id="wars:commander:manage")
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        rows = db.conn.execute(
            "SELECT w.*,c.name complex_name FROM wars w JOIN war_complexes c ON c.id=w.complex_id "
            "WHERE w.guild_id=? AND w.status IN ('recruiting','active') ORDER BY w.id DESC LIMIT 25", (GUILD_ID,)
        ).fetchall()
        embed = discord.Embed(title="⚔️ Поточні війни", color=discord.Color.dark_red())
        embed.description = "\n".join(
            f"**#{r['id']}** · {r['complex_name']} · {'Набір' if r['status']=='recruiting' else 'Триває'} · "
            + (f"[Відкрити](https://discord.com/channels/{GUILD_ID}/{r['channel_id']}/{r['message_id']})"
               if r['message_id'] and r['channel_id'] else "Картка недоступна")
            for r in rows
        ) or "Поточних воєн немає."
        embed.set_footer(text="Керування — під повідомленням кожної війни")
        await interaction.response.send_message(embed=embed, ephemeral=True)

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


class WarCreationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Напад", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.edit_message(content="Оберіть тип комплексу:", view=AttackTypeView())

    @discord.ui.button(label="Захист", emoji="🛡️", style=discord.ButtonStyle.primary)
    async def defense(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        if not current_complexes("Agosto"):
            return await interaction.response.send_message("Підконтрольних комплексів ще немає.", ephemeral=True)
        await interaction.response.edit_message(content="Оберіть наш комплекс для захисту:", view=DefenseView())


class ModeratorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Налаштування", emoji="⚙️", style=discord.ButtonStyle.primary, custom_id="wars:moderator:settings")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_message("⚙️ Налаштування системи", view=SettingsView(), ephemeral=True)

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
        recruiting = db.conn.execute(
            "SELECT COUNT(*) FROM wars WHERE guild_id=? AND status='recruiting'", (GUILD_ID,)
        ).fetchone()[0]
        active = db.conn.execute(
            "SELECT COUNT(*) FROM wars WHERE guild_id=? AND status='active'", (GUILD_ID,)
        ).fetchone()[0]
        embed = discord.Embed(
            title="⚔️ Штаб · війни за комплекси",
            description="Оберіть дію нижче. Кожна війна має окрему картку з усіма кнопками керування.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="📍 Стан", value=f"Набір: **{recruiting}**\nТривають: **{active}**", inline=True)
        embed.add_field(name="🏢 Території", value=f"Наших комплексів: **{len(current_complexes('Agosto'))}**", inline=True)
        embed.add_field(
            name="Як це працює",
            value="**Напад:** тип об’єкта → сім’я суперника → перемога додає комплекс.\n"
                  "**Захист:** обрати наш комплекс → програш прибирає його з переліку.",
            inline=False,
        )
        embed.set_footer(text="Початок, склад і завершення війни — під її повідомленням")
        message = await send_to_channel(bot, WARS_MANAGEMENT_CHANNEL_ID, embed=embed, view=CommanderView())
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
    """Restore persistent war cards after a Railway restart."""
    rows = db.conn.execute(
        "SELECT id,status,registration_mode FROM wars WHERE guild_id=? AND status IN ('recruiting','active') ORDER BY id",
        (GUILD_ID,),
    ).fetchall()
    for row in rows:
        bot.add_view(WarCardView(int(row["id"]), row["status"], row["registration_mode"]))
    bot.add_view(MainWarView())
    bot.add_view(CommanderView())
    bot.add_view(ModeratorView())
    asyncio.create_task(refresh_existing_war_cards(bot))


def register_commands(bot) -> None:
    @bot.tree.command(name="wars-member", description="Відкрити панель воєн для учасника")
    async def wars_member(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(title="⚔️ Війни за комплекси", description="Активні війни, наші комплекси, рейтинг і профіль.", color=discord.Color.dark_red()),
            view=MainWarView(), ephemeral=True,
        )

    @bot.tree.command(name="wars-commander", description="Відкрити панель командирів воєн")
    async def wars_commander(interaction: discord.Interaction):
        if not is_commander(interaction.user):
            return await interaction.response.send_message("Тільки командир або керівництво.", ephemeral=True)
        await interaction.response.send_message(
            embed=discord.Embed(title="⚔️ Штаб командирів", description="Початок війни, поточні війни, історія та статистика.", color=discord.Color.gold()),
            view=CommanderView(), ephemeral=True,
        )

    @bot.tree.command(name="wars-moderator", description="Відкрити налаштування воєн")
    async def wars_moderator(interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_message(
            embed=discord.Embed(title="⚙️ Налаштування воєн", description="Ресурси, бали, статистика й очищення тестових даних.", color=discord.Color.blurple()),
            view=ModeratorView(), ephemeral=True,
        )

    @bot.tree.command(name="wars-reset", description="Повністю очистити історію та комплекси воєн")
    async def wars_reset(interaction: discord.Interaction):
        if not is_management(interaction.user):
            return await interaction.response.send_message("Тільки керівництво.", ephemeral=True)
        await interaction.response.send_modal(ClearWarsModal())

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
