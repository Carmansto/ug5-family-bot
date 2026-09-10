import os
import re
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
CONTRACT_CHANNEL_ID = int(os.getenv("CONTRACT_CHANNEL_ID", "0") or 0)
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "contracts.db")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is empty. Copy .env.example to .env and add your bot token.")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def parse_money(raw: str) -> int:
    """
    Supported examples:
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

    value = int(float(s) * multiplier)
    if value <= 0:
        raise ValueError("Сума має бути більшою за 0")
    return value


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
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
        self.conn.commit()

    def add_contract(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        participant_ids: list[int],
        contract_name: str,
        price: int,
        cooldown: str,
    ) -> int:
        cur = self.conn.execute("""
        INSERT INTO contracts (
            message_id, guild_id, channel_id, creator_id, participant_ids,
            contract_name, price, cooldown, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unpaid', ?)
        """, (
            message_id, guild_id, channel_id, creator_id,
            json.dumps(participant_ids), contract_name, price, cooldown, utc_now_iso()
        ))
        self.conn.commit()
        return cur.lastrowid

    def get_by_message(self, message_id: int):
        return self.conn.execute(
            "SELECT * FROM contracts WHERE message_id = ?",
            (message_id,)
        ).fetchone()

    def mark_paid(self, message_id: int, paid_by: int) -> bool:
        cur = self.conn.execute("""
        UPDATE contracts
        SET status = 'paid', paid_by = ?, paid_at = ?
        WHERE message_id = ? AND status = 'unpaid'
        """, (paid_by, utc_now_iso(), message_id))
        self.conn.commit()
        return cur.rowcount > 0

    def summary(self, guild_id: int):
        return self.conn.execute("""
        SELECT
            COUNT(*) AS total_count,
            COALESCE(SUM(price), 0) AS total_sum,
            COALESCE(SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END), 0) AS paid_count,
            COALESCE(SUM(CASE WHEN status='paid' THEN price ELSE 0 END), 0) AS paid_sum,
            COALESCE(SUM(CASE WHEN status='unpaid' THEN 1 ELSE 0 END), 0) AS unpaid_count,
            COALESCE(SUM(CASE WHEN status='unpaid' THEN price ELSE 0 END), 0) AS unpaid_sum
        FROM contracts
        WHERE guild_id = ?
        """, (guild_id,)).fetchone()

    def all_for_guild(self, guild_id: int):
        return self.conn.execute(
            "SELECT * FROM contracts WHERE guild_id = ? ORDER BY id DESC",
            (guild_id,)
        ).fetchall()

    def unpaid_for_guild(self, guild_id: int, limit: int = 10):
        return self.conn.execute("""
        SELECT * FROM contracts
        WHERE guild_id = ? AND status='unpaid'
        ORDER BY id DESC
        LIMIT ?
        """, (guild_id, limit)).fetchall()


db = Database(DB_PATH)


def is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if ADMIN_ROLE_ID and any(role.id == ADMIN_ROLE_ID for role in member.roles):
        return True
    return False


def build_contract_embed(
    participants: list[int],
    contract_name: str,
    price: int,
    cooldown: str,
    creator_id: int,
    paid: bool = False,
    paid_by: Optional[int] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ КОНТРАКТ ВИКОНАНО",
        color=discord.Color.green() if paid else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )

    mentions = " ".join(f"<@{uid}>" for uid in participants)
    embed.add_field(name="👥 Виконували", value=mentions or "—", inline=False)
    embed.add_field(name="📋 Контракт", value=contract_name, inline=True)
    embed.add_field(name="💰 Ціна", value=f"{format_money(price)} $", inline=True)
    embed.add_field(name="⏳ CD", value=cooldown, inline=True)

    if paid:
        status = "🟢 Оплачено"
        if paid_by:
            status += f"\nОплатив: <@{paid_by}>"
    else:
        status = "🔴 Не оплачено"

    embed.add_field(name="💳 Статус", value=status, inline=False)
    embed.set_footer(text=f"Додав: {creator_id}")
    return embed


class ContractModal(discord.ui.Modal, title="Виконаний контракт"):
    contract_name = discord.ui.TextInput(
        label="Назва контракту",
        placeholder="Наприклад: Баки / Балони / Дрова / Переробка",
        max_length=80,
    )
    price = discord.ui.TextInput(
        label="Ціна",
        placeholder="Наприклад: 150000 або 150к",
        max_length=20,
    )
    cooldown = discord.ui.TextInput(
        label="CD контракту",
        placeholder="Наприклад: 4 год",
        max_length=30,
    )

    def __init__(self, bot: "ContractBot", participant_ids: list[int], source_channel_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.participant_ids = participant_ids
        self.source_channel_id = source_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = parse_money(str(self.price))
        except ValueError:
            await interaction.response.send_message(
                "❌ Не розібрав суму. Приклади: `150000`, `150 000`, `150к`.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
            return

        target_channel_id = CONTRACT_CHANNEL_ID or self.source_channel_id
        channel = guild.get_channel(target_channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(target_channel_id)
            except discord.DiscordException:
                channel = None

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "❌ Не знайшов канал для контрактів. Перевір `CONTRACT_CHANNEL_ID` у `.env`.",
                ephemeral=True,
            )
            return

        embed = build_contract_embed(
            participants=self.participant_ids,
            contract_name=str(self.contract_name).strip(),
            price=price,
            cooldown=str(self.cooldown).strip(),
            creator_id=interaction.user.id,
        )

        try:
            message = await channel.send(embed=embed, view=PaidButtonView(self.bot))
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У бота немає права писати в канал контрактів.",
                ephemeral=True,
            )
            return

        db.add_contract(
            message_id=message.id,
            guild_id=guild.id,
            channel_id=channel.id,
            creator_id=interaction.user.id,
            participant_ids=self.participant_ids,
            contract_name=str(self.contract_name).strip(),
            price=price,
            cooldown=str(self.cooldown).strip(),
        )

        await interaction.response.send_message(
            f"✅ Контракт записано: {message.jump_url}",
            ephemeral=True,
        )


class ParticipantSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Хто виконував контракт?",
            min_values=1,
            max_values=10,
            custom_id="contract:participants",
        )

    async def callback(self, interaction: discord.Interaction):
        view: ParticipantSelectView = self.view  # type: ignore
        view.selected_user_ids = [user.id for user in self.values]
        view.next_button.disabled = False
        mentions = " ".join(user.mention for user in self.values)
        await interaction.response.edit_message(
            content=f"👥 Обрано: {mentions}\nНатисни **Далі**.",
            view=view,
        )


class NextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Далі",
            style=discord.ButtonStyle.primary,
            emoji="➡️",
            disabled=True,
            custom_id="contract:next",
        )

    async def callback(self, interaction: discord.Interaction):
        view: ParticipantSelectView = self.view  # type: ignore
        if not view.selected_user_ids:
            await interaction.response.send_message("Спочатку обери виконавців.", ephemeral=True)
            return

        await interaction.response.send_modal(
            ContractModal(
                bot=view.bot,
                participant_ids=view.selected_user_ids,
                source_channel_id=interaction.channel_id,
            )
        )


class ParticipantSelectView(discord.ui.View):
    def __init__(self, bot: "ContractBot"):
        super().__init__(timeout=300)
        self.bot = bot
        self.selected_user_ids: list[int] = []

        self.add_item(ParticipantSelect())
        self.next_button = NextButton()
        self.add_item(self.next_button)


class ContractPanelView(discord.ui.View):
    def __init__(self, bot: "ContractBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Виконали контракт",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="contract:create",
    )
    async def create_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Спочатку обери всіх, хто виконував контракт.",
            view=ParticipantSelectView(self.bot),
            ephemeral=True,
        )


class DisabledPaidView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="Оплачено",
            style=discord.ButtonStyle.secondary,
            emoji="✅",
            disabled=True,
        )
        self.add_item(button)


class PaidButtonView(discord.ui.View):
    def __init__(self, bot: "ContractBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Оплачено",
        style=discord.ButtonStyle.success,
        emoji="💵",
        custom_id="contract:paid",
    )
    async def paid(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Цю кнопку може натискати тільки керівництво.",
                ephemeral=True,
            )
            return

        if interaction.message is None:
            await interaction.response.send_message("❌ Не знайшов повідомлення.", ephemeral=True)
            return

        row = db.get_by_message(interaction.message.id)
        if row is None:
            await interaction.response.send_message(
                "❌ Цього контракту немає в базі.",
                ephemeral=True,
            )
            return

        if row["status"] == "paid":
            await interaction.response.send_message("✅ Цей контракт уже оплачено.", ephemeral=True)
            return

        updated = db.mark_paid(interaction.message.id, interaction.user.id)
        if not updated:
            await interaction.response.send_message("✅ Цей контракт уже оплачено.", ephemeral=True)
            return

        participants = json.loads(row["participant_ids"])
        embed = build_contract_embed(
            participants=participants,
            contract_name=row["contract_name"],
            price=row["price"],
            cooldown=row["cooldown"],
            creator_id=row["creator_id"],
            paid=True,
            paid_by=interaction.user.id,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=DisabledPaidView(),
        )


class ContractBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(ContractPanelView(self))
        self.add_view(PaidButtonView(self))

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


@bot.tree.command(name="setup", description="Створити панель сімейних контрактів")
async def setup_panel(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ Ця команда тільки для керівництва.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="📋 Сімейні контракти",
        description=(
            "Натисни кнопку нижче після виконання контракту.\n\n"
            "Бот попросить:\n"
            "• обрати виконавців;\n"
            "• ввести назву контракту;\n"
            "• ввести ціну;\n"
            "• ввести CD.\n\n"
            "Кількість предметів **не записуємо**."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=ContractPanelView(bot))


@bot.tree.command(name="stats", description="Статистика сімейних контрактів")
async def stats(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
        return

    summary = db.summary(guild.id)
    rows = db.all_for_guild(guild.id)

    participant_counter = Counter()
    contract_counter = Counter()
    display_names: dict[str, str] = {}

    for row in rows:
        for uid in json.loads(row["participant_ids"]):
            participant_counter[uid] += 1

        normalized = row["contract_name"].strip().casefold()
        contract_counter[normalized] += 1
        display_names.setdefault(normalized, row["contract_name"].strip())

    top_members = participant_counter.most_common(5)
    top_contracts = contract_counter.most_common(5)

    embed = discord.Embed(
        title="📊 Статистика контрактів",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Загалом",
        value=(
            f"Контрактів: **{summary['total_count']}**\n"
            f"Сума: **{format_money(summary['total_sum'])} $**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Оплачено",
        value=(
            f"Контрактів: **{summary['paid_count']}**\n"
            f"Сума: **{format_money(summary['paid_sum'])} $**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Не оплачено",
        value=(
            f"Контрактів: **{summary['unpaid_count']}**\n"
            f"Борг: **{format_money(summary['unpaid_sum'])} $**"
        ),
        inline=True,
    )

    if top_members:
        member_text = "\n".join(
            f"{idx}. <@{uid}> — **{count}**"
            for idx, (uid, count) in enumerate(top_members, start=1)
        )
    else:
        member_text = "Поки немає даних."

    if top_contracts:
        contract_text = "\n".join(
            f"{idx}. **{display_names[key]}** — {count}"
            for idx, (key, count) in enumerate(top_contracts, start=1)
        )
    else:
        contract_text = "Поки немає даних."

    embed.add_field(name="🏆 Найактивніші", value=member_text, inline=False)
    embed.add_field(name="📋 Найчастіші контракти", value=contract_text, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="unpaid", description="Показати неоплачені контракти")
async def unpaid(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
        return

    rows = db.unpaid_for_guild(guild.id, limit=10)

    if not rows:
        await interaction.response.send_message("✅ Неоплачених контрактів немає.")
        return

    lines = []
    for row in rows:
        participants = " ".join(f"<@{uid}>" for uid in json.loads(row["participant_ids"]))
        jump_url = f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}"
        lines.append(
            f"• **{row['contract_name']}** — {format_money(row['price'])} $ — "
            f"{participants} — [відкрити]({jump_url})"
        )

    embed = discord.Embed(
        title="💸 Неоплачені контракти",
        description="\n".join(lines),
        color=discord.Color.red(),
    )
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
