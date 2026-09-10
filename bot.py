import os
import re
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
CONTRACT_CHANNEL_ID = int(os.getenv("CONTRACT_CHANNEL_ID", "0") or 0)

# Backward compatible with your current setup.
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or 0)

# Optional: several management roles separated by commas.
MANAGER_ROLE_IDS = {
    int(x.strip())
    for x in os.getenv("MANAGER_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
}
if ADMIN_ROLE_ID:
    MANAGER_ROLE_IDS.add(ADMIN_ROLE_ID)

DB_PATH = os.getenv("DB_PATH", "contracts.db").strip()
FAMILY_PERCENT = Decimal(os.getenv("FAMILY_PERCENT", os.getenv("FOMO_PERCENT", "15")).strip() or "15")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is empty.")


# ----------------------------
# Helpers
# ----------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_unix(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except Exception:
        return None


def parse_ids(raw: str) -> list[int]:
    try:
        return [int(x) for x in json.loads(raw)]
    except Exception:
        return []


def parse_money(raw: str) -> int:
    """
    Examples:
    110000
    110 000
    110к / 110k
    1.2м / 1.2m
    """
    s = raw.strip().lower()
    s = s.replace("$", "").replace("₴", "").replace(" ", "").replace("_", "")
    s = s.replace(",", ".")

    multiplier = 1
    if s.endswith(("к", "k")):
        multiplier = 1_000
        s = s[:-1]
    elif s.endswith(("м", "m")):
        multiplier = 1_000_000
        s = s[:-1]

    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise ValueError("Некоректна сума")

    value = int(Decimal(s) * multiplier)
    if value <= 0:
        raise ValueError("Сума має бути більшою за 0")
    return value


def format_money_dollars(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    whole, rem = divmod(cents, 100)
    whole_text = f"{whole:,}".replace(",", " ")
    if rem == 0:
        return f"{sign}{whole_text} $"
    return f"{sign}{whole_text}.{rem:02d} $"


def format_points(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    # 0.5 / 0.333 / etc.
    txt = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return txt


def split_payment(gross_dollars: int, member_ids: list[int]) -> tuple[int, int, dict[int, int]]:
    """
    Returns (fomo_cents, net_cents, payout_cents_by_member).
    Family percentage is exact to the cent. Any remainder cent from division is distributed
    deterministically to the first selected members so total payout == net.
    """
    if not member_ids:
        raise ValueError("No participants")

    gross_cents = gross_dollars * 100
    fomo_cents = int((Decimal(gross_cents) * FAMILY_PERCENT / Decimal("100")).quantize(Decimal("1")))
    net_cents = gross_cents - fomo_cents

    base, remainder = divmod(net_cents, len(member_ids))
    payouts: dict[int, int] = {}

    for i, uid in enumerate(member_ids):
        payouts[uid] = base + (1 if i < remainder else 0)

    return fomo_cents, net_cents, payouts


def management_member(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return any(role.id in MANAGER_ROLE_IDS for role in member.roles)


# ----------------------------
# Database
# ----------------------------

class Database:
    def __init__(self, path: str):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self._migrate_old_contracts_table()

    def _init_schema(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS contract_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            price INTEGER NOT NULL,
            cooldown TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """)

        # Keeps compatibility with your MVP table.
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE NOT NULL,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            participant_ids TEXT NOT NULL,
            contract_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            cooldown TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unpaid',
            paid_by INTEGER,
            created_at TEXT NOT NULL,
            paid_at TEXT
        )
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS contract_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(contract_id, user_id)
        )
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        )
        """)

        self.conn.commit()

    def _migrate_old_contracts_table(self):
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(contracts)").fetchall()
        }

        migrations = {
            "contract_type_id": "ALTER TABLE contracts ADD COLUMN contract_type_id INTEGER",
            "cancelled_by": "ALTER TABLE contracts ADD COLUMN cancelled_by INTEGER",
            "cancelled_at": "ALTER TABLE contracts ADD COLUMN cancelled_at TEXT",
            "fomo_cents": "ALTER TABLE contracts ADD COLUMN fomo_cents INTEGER",
            "net_cents": "ALTER TABLE contracts ADD COLUMN net_cents INTEGER",
        }

        for name, sql in migrations.items():
            if name not in columns:
                self.conn.execute(sql)

        self.conn.commit()

    # Contract catalog
    def create_contract_type(self, name: str, price: int, cooldown: str, created_by: int) -> sqlite3.Row:
        existing = self.conn.execute(
            "SELECT * FROM contract_types WHERE name = ? COLLATE NOCASE",
            (name.strip(),),
        ).fetchone()

        now = utc_now_iso()

        if existing:
            self.conn.execute("""
            UPDATE contract_types
            SET price = ?, cooldown = ?, active = 1, updated_at = ?
            WHERE id = ?
            """, (price, cooldown, now, existing["id"]))
            self.conn.commit()
            return self.get_contract_type(existing["id"])

        cur = self.conn.execute("""
        INSERT INTO contract_types
        (name, price, cooldown, active, created_by, created_at)
        VALUES (?, ?, ?, 1, ?, ?)
        """, (name.strip(), price, cooldown.strip(), created_by, now))
        self.conn.commit()
        return self.get_contract_type(cur.lastrowid)

    def update_contract_type(self, type_id: int, name: str, price: int, cooldown: str) -> bool:
        try:
            cur = self.conn.execute("""
            UPDATE contract_types
            SET name = ?, price = ?, cooldown = ?, updated_at = ?
            WHERE id = ?
            """, (name.strip(), price, cooldown.strip(), utc_now_iso(), type_id))
            self.conn.commit()
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    def archive_contract_type(self, type_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE contract_types SET active = 0, updated_at = ? WHERE id = ?",
            (utc_now_iso(), type_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_contract_type(self, type_id: int):
        return self.conn.execute(
            "SELECT * FROM contract_types WHERE id = ?",
            (type_id,),
        ).fetchone()

    def active_contract_types(self, limit: int = 25):
        return self.conn.execute("""
        SELECT ct.*,
               COUNT(c.id) AS usage_count
        FROM contract_types ct
        LEFT JOIN contracts c
          ON c.contract_type_id = ct.id
         AND c.status != 'cancelled'
        WHERE ct.active = 1
        GROUP BY ct.id
        ORDER BY usage_count DESC, ct.name COLLATE NOCASE ASC
        LIMIT ?
        """, (limit,)).fetchall()

    def list_active_contract_types(self, limit: int = 100):
        return self.conn.execute("""
        SELECT *
        FROM contract_types
        WHERE active = 1
        ORDER BY name COLLATE NOCASE ASC
        LIMIT ?
        """, (limit,)).fetchall()

    def search_contract_types(self, query: str, limit: int = 25):
        q = f"%{query.strip()}%"
        return self.conn.execute("""
        SELECT ct.*,
               COUNT(c.id) AS usage_count
        FROM contract_types ct
        LEFT JOIN contracts c
          ON c.contract_type_id = ct.id
         AND c.status != 'cancelled'
        WHERE ct.active = 1
          AND ct.name LIKE ? COLLATE NOCASE
        GROUP BY ct.id
        ORDER BY usage_count DESC, ct.name COLLATE NOCASE ASC
        LIMIT ?
        """, (q, limit)).fetchall()

    # Completed contracts
    def add_completed_contract(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        participant_ids: list[int],
        contract_type: sqlite3.Row,
    ) -> int:
        cur = self.conn.execute("""
        INSERT INTO contracts (
            message_id, guild_id, channel_id, creator_id, participant_ids,
            contract_type_id, contract_name, price, cooldown,
            status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unpaid', ?)
        """, (
            message_id,
            guild_id,
            channel_id,
            creator_id,
            json.dumps(participant_ids),
            contract_type["id"],
            contract_type["name"],
            contract_type["price"],
            contract_type["cooldown"],
            utc_now_iso(),
        ))
        self.conn.commit()
        return cur.lastrowid

    def get_completed_by_message(self, message_id: int):
        return self.conn.execute(
            "SELECT * FROM contracts WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    def get_completed_by_id(self, contract_id: int):
        return self.conn.execute(
            "SELECT * FROM contracts WHERE id = ?",
            (contract_id,),
        ).fetchone()

    def cancel_completed(self, message_id: int, cancelled_by: int) -> bool:
        cur = self.conn.execute("""
        UPDATE contracts
        SET status = 'cancelled',
            cancelled_by = ?,
            cancelled_at = ?
        WHERE message_id = ?
          AND status = 'unpaid'
        """, (cancelled_by, utc_now_iso(), message_id))
        self.conn.commit()
        return cur.rowcount > 0

    def pay_completed(self, message_id: int, paid_by: int):
        row = self.get_completed_by_message(message_id)
        if not row or row["status"] != "unpaid":
            return None

        participant_ids = parse_ids(row["participant_ids"])
        if not participant_ids:
            return None

        fomo_cents, net_cents, payouts = split_payment(row["price"], participant_ids)
        paid_at = utc_now_iso()

        try:
            self.conn.execute("BEGIN IMMEDIATE")

            cur = self.conn.execute("""
            UPDATE contracts
            SET status = 'paid',
                paid_by = ?,
                paid_at = ?,
                fomo_cents = ?,
                net_cents = ?
            WHERE message_id = ?
              AND status = 'unpaid'
            """, (paid_by, paid_at, fomo_cents, net_cents, message_id))

            if cur.rowcount != 1:
                self.conn.rollback()
                return None

            contract_id = row["id"]

            for uid, amount_cents in payouts.items():
                self.conn.execute("""
                INSERT OR REPLACE INTO contract_payouts
                (contract_id, user_id, amount_cents, created_at)
                VALUES (?, ?, ?, ?)
                """, (contract_id, uid, amount_cents, paid_at))

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return {
            "fomo_cents": fomo_cents,
            "net_cents": net_cents,
            "payouts": payouts,
            "paid_at": paid_at,
        }

    def payouts_for_contract(self, contract_id: int):
        return self.conn.execute("""
        SELECT * FROM contract_payouts
        WHERE contract_id = ?
        ORDER BY id ASC
        """, (contract_id,)).fetchall()

    def all_non_cancelled(self, guild_id: int):
        return self.conn.execute("""
        SELECT * FROM contracts
        WHERE guild_id = ?
          AND status != 'cancelled'
        ORDER BY id DESC
        """, (guild_id,)).fetchall()

    def unpaid_for_guild(self, guild_id: int, limit: int = 15):
        return self.conn.execute("""
        SELECT * FROM contracts
        WHERE guild_id = ?
          AND status = 'unpaid'
        ORDER BY id DESC
        LIMIT ?
        """, (guild_id, limit)).fetchall()

    def paid_payouts_for_guild(self, guild_id: int):
        return self.conn.execute("""
        SELECT cp.*
        FROM contract_payouts cp
        JOIN contracts c ON c.id = cp.contract_id
        WHERE c.guild_id = ?
          AND c.status = 'paid'
        """, (guild_id,)).fetchall()

    def get_setting(self, guild_id: int, key: str) -> Optional[str]:
        row = self.conn.execute("""
        SELECT value FROM bot_settings
        WHERE guild_id = ? AND key = ?
        """, (guild_id, key)).fetchone()
        return row["value"] if row else None

    def set_setting(self, guild_id: int, key: str, value: str):
        self.conn.execute("""
        INSERT INTO bot_settings (guild_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, key)
        DO UPDATE SET value = excluded.value
        """, (guild_id, key, value))
        self.conn.commit()


db = Database(DB_PATH)


# ----------------------------
# Completed contract messages
# ----------------------------

def build_completed_embed(row: sqlite3.Row) -> discord.Embed:
    participants = parse_ids(row["participant_ids"])
    n = max(len(participants), 1)
    points_each = Fraction(1, n)

    if row["status"] == "paid":
        color = discord.Color.green()
        status_text = "🟢 **Оплачено**"
    elif row["status"] == "cancelled":
        color = discord.Color.dark_grey()
        status_text = "⚫ **Скасовано**"
    else:
        color = discord.Color.orange()
        status_text = "🔴 **Не оплачено**"

    embed = discord.Embed(
        title="✅ КОНТРАКТ ВИКОНАНО" if row["status"] != "cancelled" else "❌ КОНТРАКТ СКАСОВАНО",
        color=color,
    )

    embed.add_field(
        name="👥 Виконували",
        value=" ".join(f"<@{uid}>" for uid in participants) or "—",
        inline=False,
    )
    embed.add_field(name="📋 Контракт", value=row["contract_name"], inline=True)
    embed.add_field(name="💰 Сума", value=f"{format_money_dollars(row['price'])} $", inline=True)
    embed.add_field(name="⏳ КД", value=row["cooldown"], inline=True)
    embed.add_field(
        name="🏆 Бали",
        value=f"1 бал за контракт • по **{format_points(points_each)}** кожному",
        inline=False,
    )
    embed.add_field(name="💳 Статус", value=status_text, inline=False)

    created_ts = iso_to_unix(row["created_at"])
    if created_ts:
        embed.add_field(name="🕒 Записано", value=f"<t:{created_ts}:f>", inline=True)

    if row["status"] == "paid":
        paid_ts = iso_to_unix(row["paid_at"])
        if paid_ts:
            embed.add_field(name="✅ Оплачено", value=f"<t:{paid_ts}:f>", inline=True)

        fomo_cents = row["fomo_cents"] or 0
        net_cents = row["net_cents"] or 0
        embed.add_field(
            name=f"🏦 Банк сім'ї ({FAMILY_PERCENT.normalize()}%)",
            value=format_cents(fomo_cents),
            inline=True,
        )
        embed.add_field(
            name="💸 Учасникам",
            value=format_cents(net_cents),
            inline=True,
        )

        payouts = db.payouts_for_contract(row["id"])
        if payouts:
            payout_lines = [
                f"<@{p['user_id']}> — **{format_cents(p['amount_cents'])}**"
                for p in payouts
            ]
            embed.add_field(
                name="👤 Розподіл виплати",
                value="\n".join(payout_lines),
                inline=False,
            )

    if row["status"] == "cancelled":
        cancelled_ts = iso_to_unix(row["cancelled_at"])
        cancelled_by = row["cancelled_by"]
        details = []
        if cancelled_by:
            details.append(f"Скасував: <@{cancelled_by}>")
        if cancelled_ts:
            details.append(f"<t:{cancelled_ts}:f>")
        if details:
            embed.add_field(name="Скасування", value=" • ".join(details), inline=False)

    embed.set_footer(text=f"Запис #{row['id']}")
    return embed


async def get_target_channel(guild: discord.Guild, fallback_channel_id: int):
    channel_id = CONTRACT_CHANNEL_ID or fallback_channel_id
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException:
            return None
    return channel


async def refresh_completed_message(message_id: int):
    row = db.get_completed_by_message(message_id)
    if not row:
        return

    try:
        channel = bot.get_channel(row["channel_id"]) or await bot.fetch_channel(row["channel_id"])
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        message = await channel.fetch_message(message_id)

        if row["status"] == "unpaid":
            view = UnpaidCompletedView(bot)
        else:
            view = None

        await message.edit(embed=build_completed_embed(row), view=view)
    except discord.DiscordException:
        pass


# ----------------------------
# New contract flow
# ----------------------------

class PerformerSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Оберіть виконавців контракту",
            min_values=1,
            max_values=25,
        )

    async def callback(self, interaction: discord.Interaction):
        view: PerformerStepView = self.view  # type: ignore
        ids = [u.id for u in self.values if not getattr(u, "bot", False)]

        if not ids:
            await interaction.response.send_message(
                "❌ Оберіть хоча б одного звичайного учасника.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content=(
                "👥 Виконавці: "
                + " ".join(f"<@{uid}>" for uid in ids)
                + "\n\nТепер оберіть контракт."
            ),
            view=ContractPickerView(view.bot, ids),
        )


class PerformerStepView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot"):
        super().__init__(timeout=300)
        self.bot = bot_instance
        self.add_item(PerformerSelect())

    @discord.ui.button(
        label="Я виконав сам",
        style=discord.ButtonStyle.primary,
        emoji="👤",
    )
    async def myself(self, interaction: discord.Interaction, button: discord.ui.Button):
        if getattr(interaction.user, "bot", False):
            await interaction.response.send_message("❌ Бот не може бути виконавцем.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"👤 Виконавець: {interaction.user.mention}\n\nТепер оберіть контракт.",
            view=ContractPickerView(self.bot, [interaction.user.id]),
        )


class QuickContractSelect(discord.ui.Select):
    def __init__(self, bot_instance: "ContractBot", participant_ids: list[int]):
        self.bot_instance = bot_instance
        self.participant_ids = participant_ids
        rows = db.active_contract_types(limit=25)

        options = []
        for row in rows:
            desc = f"{format_money_dollars(row['price'])} $ • КД {row['cooldown']}"
            options.append(
                discord.SelectOption(
                    label=row["name"][:100],
                    value=str(row["id"]),
                    description=desc[:100],
                )
            )

        if not options:
            options = [
                discord.SelectOption(
                    label="Контрактів ще немає",
                    value="none",
                    description="Керівництво має додати їх у /contracts_admin",
                )
            ]

        super().__init__(
            placeholder="Популярні / доступні контракти",
            options=options,
            min_values=1,
            max_values=1,
            disabled=(options[0].value == "none"),
        )

    async def callback(self, interaction: discord.Interaction):
        type_id = int(self.values[0])
        row = db.get_contract_type(type_id)

        if not row or not row["active"]:
            await interaction.response.send_message(
                "❌ Цей контракт уже недоступний. Спробуйте ще раз.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content="Перевірте дані й підтвердьте.",
            embed=build_confirmation_embed(row, self.participant_ids),
            view=ConfirmContractView(self.bot_instance, self.participant_ids, type_id),
        )


class ContractSearchModal(discord.ui.Modal, title="Пошук контракту"):
    query = discord.ui.TextInput(
        label="Введіть частину назви",
        placeholder="Наприклад: балон, дрова, риба...",
        max_length=80,
    )

    def __init__(self, bot_instance: "ContractBot", participant_ids: list[int]):
        super().__init__(timeout=300)
        self.bot_instance = bot_instance
        self.participant_ids = participant_ids

    async def on_submit(self, interaction: discord.Interaction):
        rows = db.search_contract_types(str(self.query), limit=25)

        if not rows:
            await interaction.response.send_message(
                f"❌ Нічого не знайшов за запитом **{str(self.query).strip()}**.",
                view=SearchAgainView(self.bot_instance, self.participant_ids),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🔎 Знайдено: **{len(rows)}**. Оберіть контракт:",
            view=SearchResultsView(self.bot_instance, self.participant_ids, rows),
            ephemeral=True,
        )


class SearchAgainView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot", participant_ids: list[int]):
        super().__init__(timeout=300)
        self.bot_instance = bot_instance
        self.participant_ids = participant_ids

    @discord.ui.button(label="Шукати ще", style=discord.ButtonStyle.primary, emoji="🔎")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ContractSearchModal(self.bot_instance, self.participant_ids)
        )


class SearchResultSelect(discord.ui.Select):
    def __init__(self, bot_instance: "ContractBot", participant_ids: list[int], rows):
        self.bot_instance = bot_instance
        self.participant_ids = participant_ids
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["id"]),
                description=f"{format_money_dollars(row['price'])} $ • КД {row['cooldown']}"[:100],
            )
            for row in rows[:25]
        ]
        super().__init__(
            placeholder="Оберіть знайдений контракт",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        type_id = int(self.values[0])
        row = db.get_contract_type(type_id)

        if not row or not row["active"]:
            await interaction.response.send_message(
                "❌ Цей контракт уже недоступний.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content="Перевірте дані й підтвердьте.",
            embed=build_confirmation_embed(row, self.participant_ids),
            view=ConfirmContractView(self.bot_instance, self.participant_ids, type_id),
        )


class SearchResultsView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot", participant_ids: list[int], rows):
        super().__init__(timeout=300)
        self.add_item(SearchResultSelect(bot_instance, participant_ids, rows))


class ContractPickerView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot", participant_ids: list[int]):
        super().__init__(timeout=300)
        self.bot_instance = bot_instance
        self.participant_ids = participant_ids
        self.add_item(QuickContractSelect(bot_instance, participant_ids))

    @discord.ui.button(label="Пошук", style=discord.ButtonStyle.secondary, emoji="🔎")
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ContractSearchModal(self.bot_instance, self.participant_ids)
        )


def build_confirmation_embed(contract_type: sqlite3.Row, participant_ids: list[int]) -> discord.Embed:
    points_each = Fraction(1, len(participant_ids))
    embed = discord.Embed(
        title="Підтвердити виконання контракту",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="👥 Виконавці",
        value=" ".join(f"<@{uid}>" for uid in participant_ids),
        inline=False,
    )
    embed.add_field(name="📋 Контракт", value=contract_type["name"], inline=True)
    embed.add_field(
        name="💰 Сума",
        value=f"{format_money_dollars(contract_type['price'])} $",
        inline=True,
    )
    embed.add_field(name="⏳ КД", value=contract_type["cooldown"], inline=True)
    embed.add_field(
        name="🏆 Бали",
        value=f"по **{format_points(points_each)}** кожному",
        inline=False,
    )
    return embed


class ConfirmContractView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot", participant_ids: list[int], type_id: int):
        super().__init__(timeout=300)
        self.bot_instance = bot_instance
        self.participant_ids = participant_ids
        self.type_id = type_id

    @discord.ui.button(label="Підтвердити", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
            return

        contract_type = db.get_contract_type(self.type_id)
        if not contract_type or not contract_type["active"]:
            await interaction.response.send_message(
                "❌ Контракт уже видалений із переліку.",
                ephemeral=True,
            )
            return

        channel = await get_target_channel(guild, interaction.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "❌ Не знайшов канал контрактів.",
                ephemeral=True,
            )
            return

        placeholder = await channel.send("⏳ Записую контракт...")

        db.add_completed_contract(
            message_id=placeholder.id,
            guild_id=guild.id,
            channel_id=channel.id,
            creator_id=interaction.user.id,
            participant_ids=self.participant_ids,
            contract_type=contract_type,
        )

        row = db.get_completed_by_message(placeholder.id)
        await placeholder.edit(
            content=None,
            embed=build_completed_embed(row),
            view=UnpaidCompletedView(self.bot_instance),
        )

        await interaction.response.edit_message(
            content=f"✅ Контракт записано: {placeholder.jump_url}",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Оберіть контракт.",
            embed=None,
            view=ContractPickerView(self.bot_instance, self.participant_ids),
        )


# ----------------------------
# Payment / cancel
# ----------------------------

class CancelCompletedConfirmView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot", message_id: int):
        super().__init__(timeout=60)
        self.bot_instance = bot_instance
        self.message_id = message_id

    @discord.ui.button(label="Так, скасувати", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.edit_message(
                content="❌ Немає права.",
                view=None,
            )
            return

        ok = db.cancel_completed(self.message_id, interaction.user.id)
        if not ok:
            await interaction.response.edit_message(
                content="❌ Скасувати можна тільки неоплачений контракт.",
                view=None,
            )
            return

        await refresh_completed_message(self.message_id)
        await interaction.response.edit_message(
            content="✅ Запис скасовано. Він більше не рахується в статистиці.",
            view=None,
        )

    @discord.ui.button(label="Ні", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Скасування відмінено.", view=None)


class UnpaidCompletedView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot"):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance

    @discord.ui.button(
        label="Оплачено",
        style=discord.ButtonStyle.success,
        emoji="💵",
        custom_id="contract_v3:paid",
    )
    async def paid(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message(
                "❌ Оплачувати контракти може тільки керівництво.",
                ephemeral=True,
            )
            return

        if interaction.message is None:
            await interaction.response.send_message("❌ Не знайшов запис.", ephemeral=True)
            return

        result = db.pay_completed(interaction.message.id, interaction.user.id)
        if not result:
            await interaction.response.send_message(
                "❌ Цей контракт уже оплачений або скасований.",
                ephemeral=True,
            )
            return

        row = db.get_completed_by_message(interaction.message.id)
        await interaction.response.edit_message(
            embed=build_completed_embed(row),
            view=None,
        )

    @discord.ui.button(
        label="Скасувати",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="contract_v3:cancel",
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message(
                "❌ Скасовувати записи може тільки керівництво.",
                ephemeral=True,
            )
            return

        if interaction.message is None:
            await interaction.response.send_message("❌ Не знайшов запис.", ephemeral=True)
            return

        await interaction.response.send_message(
            "⚠️ Скасувати цей запис? Він буде виключений зі статистики.",
            view=CancelCompletedConfirmView(self.bot_instance, interaction.message.id),
            ephemeral=True,
        )


# ----------------------------
# Contract catalog admin
# ----------------------------

class ContractTypeModal(discord.ui.Modal):
    def __init__(self, mode: str, user_id: int, type_id: Optional[int] = None):
        self.mode = mode
        self.user_id = user_id
        self.type_id = type_id

        row = db.get_contract_type(type_id) if type_id else None

        super().__init__(
            title="Додати контракт" if mode == "add" else "Редагувати контракт",
            timeout=300,
        )

        self.name_input = discord.ui.TextInput(
            label="Назва контракту",
            placeholder="Наприклад: Майстри баків",
            default=row["name"] if row else None,
            max_length=100,
        )
        self.price_input = discord.ui.TextInput(
            label="Ціна контракту",
            placeholder="Наприклад: 100000 або 100к",
            default=str(row["price"]) if row else None,
            max_length=20,
        )
        self.cooldown_input = discord.ui.TextInput(
            label="КД контракту",
            placeholder="Наприклад: 4 год",
            default=row["cooldown"] if row else None,
            max_length=50,
        )

        self.add_item(self.name_input)
        self.add_item(self.price_input)
        self.add_item(self.cooldown_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return

        try:
            price = parse_money(str(self.price_input))
        except ValueError:
            await interaction.response.send_message(
                "❌ Некоректна ціна. Приклади: `100000`, `100к`, `1.2м`.",
                ephemeral=True,
            )
            return

        name = str(self.name_input).strip()
        cooldown = str(self.cooldown_input).strip()

        if self.mode == "add":
            row = db.create_contract_type(name, price, cooldown, interaction.user.id)
            await interaction.response.send_message(
                f"✅ Додано: **{row['name']}** — {format_money_dollars(row['price'])} $ — КД {row['cooldown']}",
                ephemeral=True,
            )
            return

        ok = db.update_contract_type(self.type_id, name, price, cooldown)
        if not ok:
            await interaction.response.send_message(
                "❌ Не вдалося зберегти. Можливо, контракт з такою назвою вже існує.",
                ephemeral=True,
            )
            return

        row = db.get_contract_type(self.type_id)
        await interaction.response.send_message(
            f"✅ Оновлено: **{row['name']}** — {format_money_dollars(row['price'])} $ — КД {row['cooldown']}",
            ephemeral=True,
        )


class AdminSearchTypeModal(discord.ui.Modal, title="Знайти контракт"):
    query = discord.ui.TextInput(
        label="Назва або частина назви",
        placeholder="Наприклад: бак",
        max_length=80,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return

        rows = db.search_contract_types(str(self.query), limit=25)
        if not rows:
            await interaction.response.send_message("❌ Нічого не знайдено.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Оберіть контракт для керування:",
            view=AdminTypeResultsView(rows),
            ephemeral=True,
        )


class AdminTypeSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [
            discord.SelectOption(
                label=r["name"][:100],
                value=str(r["id"]),
                description=f"{format_money_dollars(r['price'])} $ • КД {r['cooldown']}"[:100],
            )
            for r in rows[:25]
        ]
        super().__init__(placeholder="Контракт", options=options)

    async def callback(self, interaction: discord.Interaction):
        type_id = int(self.values[0])
        row = db.get_contract_type(type_id)
        if not row:
            await interaction.response.send_message("❌ Контракт не знайдено.", ephemeral=True)
            return

        embed = discord.Embed(title=row["name"], color=discord.Color.blurple())
        embed.add_field(name="💰 Ціна", value=f"{format_money_dollars(row['price'])} $", inline=True)
        embed.add_field(name="⏳ КД", value=row["cooldown"], inline=True)

        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=ManageOneTypeView(type_id),
        )


class AdminTypeResultsView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=300)
        self.add_item(AdminTypeSelect(rows))


class DeleteTypeConfirmView(discord.ui.View):
    def __init__(self, type_id: int):
        super().__init__(timeout=60)
        self.type_id = type_id

    @discord.ui.button(label="Так, видалити", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.edit_message(content="❌ Немає права.", view=None)
            return

        db.archive_contract_type(self.type_id)
        await interaction.response.edit_message(
            content="✅ Контракт прибрано з переліку. Старі виконання залишилися в історії.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Ні", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Видалення відмінено.", embed=None, view=None)


class ManageOneTypeView(discord.ui.View):
    def __init__(self, type_id: int):
        super().__init__(timeout=300)
        self.type_id = type_id

    @discord.ui.button(label="Редагувати", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return

        await interaction.response.send_modal(
            ContractTypeModal("edit", interaction.user.id, self.type_id)
        )

    @discord.ui.button(label="Видалити", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return

        row = db.get_contract_type(self.type_id)
        await interaction.response.edit_message(
            content=f"⚠️ Прибрати **{row['name']}** з переліку?",
            embed=None,
            view=DeleteTypeConfirmView(self.type_id),
        )


class ResetRatingConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(
        label="Так, обнулити рейтинг",
        style=discord.ButtonStyle.danger,
        emoji="♻️",
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.edit_message(content="❌ Немає права.", view=None)
            return

        if interaction.guild is None:
            await interaction.response.edit_message(content="❌ Це працює тільки на сервері.", view=None)
            return

        reset_at = utc_now_iso()
        db.set_setting(interaction.guild.id, "rating_reset_at", reset_at)
        reset_ts = iso_to_unix(reset_at)

        when = f"<t:{reset_ts}:f>" if reset_ts else "зараз"
        await interaction.response.edit_message(
            content=(
                f"✅ Рейтинг обнулено {when}.\n"
                "Старі контракти та загальна фінансова історія не видалені. "
                "З цього моменту з нуля рахуються бали, участі та заробіток у рейтингу."
            ),
            view=None,
        )

    @discord.ui.button(label="Ні", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Обнулення рейтингу скасовано.", view=None)


class ContractAdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Додати", style=discord.ButtonStyle.success, emoji="➕")
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return
        await interaction.response.send_modal(ContractTypeModal("add", interaction.user.id))

    @discord.ui.button(label="Знайти / редагувати", style=discord.ButtonStyle.primary, emoji="🔎")
    async def find_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return
        await interaction.response.send_modal(AdminSearchTypeModal())

    @discord.ui.button(label="Список", style=discord.ButtonStyle.secondary, emoji="📋")
    async def list_types(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return

        rows = db.list_active_contract_types(limit=100)
        if not rows:
            await interaction.response.send_message("Поки що контрактів немає.", ephemeral=True)
            return

        lines = [
            f"**{idx}. {row['name']}** — {format_money_dollars(row['price'])} $ — КД {row['cooldown']}"
            for idx, row in enumerate(rows, start=1)
        ]

        # Keep under Discord embed limit by splitting.
        chunks = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 3500:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}".strip()
        if current:
            chunks.append(current)

        embeds = []
        for i, chunk in enumerate(chunks[:10], start=1):
            embeds.append(
                discord.Embed(
                    title=f"📋 Контракти ({i}/{len(chunks)})",
                    description=chunk,
                    color=discord.Color.blurple(),
                )
            )

        await interaction.response.send_message(embeds=embeds, ephemeral=True)


    @discord.ui.button(
        label="Обнулити рейтинг",
        style=discord.ButtonStyle.danger,
        emoji="♻️",
    )
    async def reset_rating(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
            await interaction.response.send_message("❌ Немає права.", ephemeral=True)
            return

        await interaction.response.send_message(
            "⚠️ Обнулити поточний рейтинг?\n"
            "Контракти й загальна фінансова історія залишаться. "
            "З нуля почнуться бали, участі та заробіток у рейтингу.",
            view=ResetRatingConfirmView(),
            ephemeral=True,
        )


# ----------------------------
# User panel
# ----------------------------

class MainContractPanelView(discord.ui.View):
    def __init__(self, bot_instance: "ContractBot"):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance

    @discord.ui.button(
        label="Виконав контракт",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="contract_v3:new",
    )
    async def new_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not db.active_contract_types(limit=1):
            await interaction.response.send_message(
                "❌ Перелік контрактів ще порожній. Керівництво має додати їх через `/contracts_admin`.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Хто виконав контракт?",
            view=PerformerStepView(self.bot_instance),
            ephemeral=True,
        )


# ----------------------------
# Bot + commands
# ----------------------------

class ContractBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(MainContractPanelView(self))
        self.add_view(UnpaidCompletedView(self))

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"[SYNC] Commands synced to guild {GUILD_ID}")
        else:
            await self.tree.sync()
            print("[SYNC] Global commands synced")

    async def on_ready(self):
        print(f"[READY] Logged in as {self.user} ({self.user.id})")


bot = ContractBot()


@bot.tree.command(name="setup", description="Створити та закріпити панель контрактів")
async def setup_panel(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
        await interaction.response.send_message(
            "❌ Ця команда тільки для керівництва.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
        return

    channel = await get_target_channel(guild, interaction.channel_id)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ Панель треба створювати у звичайному текстовому каналі.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="📋 КОНТРАКТИ СІМ’Ї",
        description=(
            "Виконав контракт — натисни кнопку нижче.\n\n"
            "Треба лише:\n"
            "• обрати виконавця / виконавців;\n"
            "• обрати контракт із переліку;\n"
            "• підтвердити.\n\n"
            "Назва, ціна та КД підтягуються автоматично."
        ),
        color=discord.Color.blurple(),
    )

    old_panel_id = db.get_setting(guild.id, "panel_message_id")
    panel_message = None

    if old_panel_id:
        try:
            panel_message = await channel.fetch_message(int(old_panel_id))
            await panel_message.edit(embed=embed, view=MainContractPanelView(bot))
        except Exception:
            panel_message = None

    if panel_message is None:
        panel_message = await channel.send(embed=embed, view=MainContractPanelView(bot))
        db.set_setting(guild.id, "panel_message_id", str(panel_message.id))

    pinned = panel_message.pinned
    if not pinned:
        try:
            await panel_message.pin(reason="Панель сімейних контрактів")
            pinned = True
        except discord.DiscordException:
            pinned = False

    text = f"✅ Панель готова: {panel_message.jump_url}"
    if not pinned:
        text += "\n📌 Не зміг закріпити автоматично — закріпи вручну або дай право «Керувати повідомленнями»."

    await interaction.followup.send(text, ephemeral=True)


@bot.tree.command(name="contracts_admin", description="Керування переліком контрактів")
async def contracts_admin(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
        await interaction.response.send_message(
            "❌ Ця команда тільки для керівництва.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="⚙️ Керування контрактами",
        description=(
            "Тут керівництво створює перелік контрактів.\n"
            "Для кожного контракту зберігаються **назва, ціна та КД**.\n"
            "Тут же можна **обнулити рейтинг** без видалення фінансової історії."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(
        embed=embed,
        view=ContractAdminPanelView(),
        ephemeral=True,
    )


@bot.tree.command(name="stats", description="Статистика контрактів для керівництва")
async def stats(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
        await interaction.response.send_message(
            "❌ Статистика доступна тільки керівництву.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
        return

    rows = db.all_non_cancelled(guild.id)
    payout_rows = db.paid_payouts_for_guild(guild.id)

    total_contracts = len(rows)
    paid_rows = [r for r in rows if r["status"] == "paid"]
    unpaid_rows = [r for r in rows if r["status"] == "unpaid"]

    total_paid_gross_cents = sum(r["price"] * 100 for r in paid_rows)
    total_fomo_cents = sum((r["fomo_cents"] or 0) for r in paid_rows)
    total_net_cents = sum((r["net_cents"] or 0) for r in paid_rows)
    unpaid_gross_cents = sum(r["price"] * 100 for r in unpaid_rows)

    rating_reset_at = db.get_setting(guild.id, "rating_reset_at")
    rating_rows = [
        row for row in rows
        if not rating_reset_at or row["created_at"] >= rating_reset_at
    ]
    rating_contract_ids = {row["id"] for row in rating_rows}

    participation_count = Counter()
    points = defaultdict(lambda: Fraction(0, 1))
    earnings_cents = Counter()

    for row in rating_rows:
        members = parse_ids(row["participant_ids"])
        if not members:
            continue
        p = Fraction(1, len(members))
        for uid in members:
            participation_count[uid] += 1
            points[uid] += p

    for payout in payout_rows:
        if payout["contract_id"] in rating_contract_ids:
            earnings_cents[payout["user_id"]] += payout["amount_cents"]

    user_ids = set(participation_count) | set(earnings_cents)
    ranking = sorted(
        user_ids,
        key=lambda uid: (points[uid], earnings_cents[uid], participation_count[uid]),
        reverse=True,
    )

    embed = discord.Embed(
        title="📊 Статистика контрактів",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📋 Контракти",
        value=(
            f"Всього: **{total_contracts}**\n"
            f"Оплачено: **{len(paid_rows)}**\n"
            f"Не оплачено: **{len(unpaid_rows)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="💰 Фінанси",
        value=(
            f"Оплачені контракти: **{format_cents(total_paid_gross_cents)}**\n"
            f"Банк сім'ї ({FAMILY_PERCENT.normalize()}%): **{format_cents(total_fomo_cents)}**\n"
            f"Учасникам: **{format_cents(total_net_cents)}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="⏳ Не оплачено",
        value=f"На суму: **{format_cents(unpaid_gross_cents)}**",
        inline=False,
    )

    if ranking:
        lines = []
        for idx, uid in enumerate(ranking[:15], start=1):
            lines.append(
                f"**{idx}.** <@{uid}> — "
                f"балів **{format_points(points[uid])}** • "
                f"участей **{participation_count[uid]}** • "
                f"заробив **{format_cents(earnings_cents[uid])}**"
            )
        reset_ts = iso_to_unix(rating_reset_at) if rating_reset_at else None
        leaderboard_name = (
            f"🏆 Лідерство • з <t:{reset_ts}:d>"
            if reset_ts
            else "🏆 Лідерство • від початку"
        )
        embed.add_field(
            name=leaderboard_name,
            value="\n".join(lines),
            inline=False,
        )
    else:
        reset_ts = iso_to_unix(rating_reset_at) if rating_reset_at else None
        leaderboard_name = (
            f"🏆 Лідерство • з <t:{reset_ts}:d>"
            if reset_ts
            else "🏆 Лідерство • від початку"
        )
        embed.add_field(name=leaderboard_name, value="Поки немає даних.", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="unpaid", description="Неоплачені контракти")
async def unpaid(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
        await interaction.response.send_message(
            "❌ Доступно тільки керівництву.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
        return

    rows = db.unpaid_for_guild(guild.id)
    if not rows:
        await interaction.response.send_message("✅ Неоплачених контрактів немає.", ephemeral=True)
        return

    lines = []
    for row in rows:
        participants = " ".join(f"<@{uid}>" for uid in parse_ids(row["participant_ids"]))
        jump_url = f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}"
        lines.append(
            f"• **{row['contract_name']}** — {format_money_dollars(row['price'])} $ — "
            f"{participants} — [відкрити]({jump_url})"
        )

    embed = discord.Embed(
        title="💸 Неоплачені контракти",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.run(TOKEN)
