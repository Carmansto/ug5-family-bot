import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    GUILD_ID,
    LOTTERY_PUBLIC_CHANNEL_ID,
    LOTTERY_MANAGEMENT_CHANNEL_ID,
)
from database import db
from utils import (
    format_cents,
    format_money_dollars,
    management_payout_member,
    parse_money,
    utc_now_iso,
)
from contracts import audit_log


# ============================================================
# CONSTANTS
# ============================================================

TEMP_MESSAGE_SECONDS = 10


# ============================================================
# HELPERS — TEMPORARY MESSAGES
# ============================================================

async def delete_after(
    interaction: discord.Interaction,
    delay: float = TEMP_MESSAGE_SECONDS,
):
    """
    Видаляє оригінальне ephemeral-повідомлення
    через задану кількість секунд.
    """
    try:
        await asyncio.sleep(delay)

        await interaction.delete_original_response()

    except asyncio.CancelledError:
        pass

    except (
        discord.NotFound,
        discord.HTTPException,
    ):
        pass


def schedule_delete(
    interaction: discord.Interaction,
    delay: float = TEMP_MESSAGE_SECONDS,
):
    """
    Запускає фонове видалення ephemeral-повідомлення.
    """
    return asyncio.create_task(
        delete_after(
            interaction,
            delay,
        )
    )


def schedule_message_delete(
    message: discord.Message,
    delay: float = TEMP_MESSAGE_SECONDS,
):
    """
    Видаляє звичайне повідомлення через заданий час.
    """
    async def _delete():
        try:
            await asyncio.sleep(delay)
            await message.delete()

        except asyncio.CancelledError:
            pass

        except (
            discord.NotFound,
            discord.HTTPException,
        ):
            pass

    return asyncio.create_task(_delete())


async def temporary_error(
    interaction: discord.Interaction,
    content: str,
    delay: float = TEMP_MESSAGE_SECONDS,
):
    """
    Тимчасова ephemeral-помилка.
    Автоматично зникає через 10 секунд.
    """
    await interaction.response.send_message(
        content,
        ephemeral=True,
    )

    schedule_delete(
        interaction,
        delay,
    )


async def temporary_message(
    interaction: discord.Interaction,
    content: str,
    *,
    view=None,
    embed=None,
    delay: float = TEMP_MESSAGE_SECONDS,
):
    """
    Тимчасове ephemeral-повідомлення.
    """
    await interaction.response.send_message(
        content,
        view=view,
        embed=embed,
        ephemeral=True,
    )

    if isinstance(
        view,
        AutoDeleteEphemeralView,
    ):
        view.start_auto_delete(
            interaction,
            delay,
        )
    else:
        schedule_delete(
            interaction,
            delay,
        )


async def temporary_followup(
    interaction: discord.Interaction,
    content: str,
    *,
    view=None,
    embed=None,
    delay: float = TEMP_MESSAGE_SECONDS,
):
    """
    Тимчасовий ephemeral followup.
    """
    message = await interaction.followup.send(
        content,
        view=view,
        embed=embed,
        ephemeral=True,
        wait=True,
    )

    schedule_message_delete(
        message,
        delay,
    )

    return message


# ============================================================
# AUTO DELETE VIEW
# ============================================================

class AutoDeleteEphemeralView(
    discord.ui.View
):
    """
    View для ephemeral-повідомлень.

    Повідомлення автоматично видаляється через 10 секунд.
    При новій дії таймер запускається заново.
    """

    def __init__(
        self,
        timeout=300,
    ):
        super().__init__(
            timeout=timeout
        )

        self._delete_task = None

    def cancel_auto_delete(self):
        if (
            self._delete_task
            and not self._delete_task.done()
        ):
            self._delete_task.cancel()

        self._delete_task = None

    def start_auto_delete(
        self,
        interaction: discord.Interaction,
        delay: float = TEMP_MESSAGE_SECONDS,
    ):
        self.cancel_auto_delete()

        self._delete_task = asyncio.create_task(
            self._delete_original_later(
                interaction,
                delay,
            )
        )

    async def _delete_original_later(
        self,
        interaction: discord.Interaction,
        delay: float,
    ):
        current_task = asyncio.current_task()

        try:
            await asyncio.sleep(delay)

            await interaction.delete_original_response()

        except asyncio.CancelledError:
            pass

        except (
            discord.NotFound,
            discord.HTTPException,
        ):
            pass

        finally:
            if self._delete_task is current_task:
                self._delete_task = None

    async def delete_now(
        self,
        interaction: discord.Interaction,
    ):
        self.cancel_auto_delete()

        try:
            await interaction.delete_original_response()

        except (
            discord.NotFound,
            discord.HTTPException,
        ):
            pass


# ============================================================
# BASIC HELPERS
# ============================================================

def parse_duration(raw: str) -> Optional[int]:
    """
    Формати:

    2d 12h
    6h
    90m
    1d
    0 = без обмеження часу
    """

    s = raw.strip().lower().replace(
        " ",
        "",
    )

    if s == "0":
        return 0

    m = re.fullmatch(
        r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?",
        s,
    )

    if not m or not any(
        m.groups()
    ):
        return None

    days = int(
        m.group(1) or 0
    )

    hours = int(
        m.group(2) or 0
    )

    mins = int(
        m.group(3) or 0
    )

    total = (
        days * 1440
        + hours * 60
        + mins
    )

    return (
        total
        if total > 0
        else None
    )


def fmt_dt(
    iso: str,
) -> str:
    if not iso:
        return "Без обмеження"

    try:
        dt = datetime.fromisoformat(
            iso.replace(
                "Z",
                "+00:00",
            )
        )

        return dt.astimezone().strftime(
            "%d.%m.%Y %H:%M"
        )

    except Exception:
        return iso


def lottery_status_text(
    row,
) -> str:
    if row["status"] == "active":
        return "🟢 Прийом квитків відкритий"

    if row["status"] == "finished":
        return "🔒 Продаж завершено"

    if row["status"] == "drawn":
        return "🏆 Розіграш проведено"

    return row["status"]


def lottery_embed(
    row,
    reserved=0,
    confirmed=0,
    winner=None,
):
    total = int(
        row["total_tickets"]
    )

    free = max(
        0,
        total - reserved - confirmed,
    )

    prize = (
        format_cents(
            int(row["prize_cents"])
        )
        if row["prize_type"] == "cash"
        else (
            row["prize_description"]
            or "Не вказано"
        )
    )

    e = discord.Embed(
        title=f"🎟️ {row['name']}",
        description=lottery_status_text(row),
        color=discord.Color.gold(),
    )

    e.add_field(
        name="🏆 Приз",
        value=prize,
        inline=False,
    )

    e.add_field(
        name="🎫 Ціна квитка",
        value=format_cents(
            row["ticket_price_cents"]
        ),
        inline=True,
    )

    e.add_field(
        name="🎫 Квитків",
        value=str(total),
        inline=True,
    )

    e.add_field(
        name="👤 Ліміт на людину",
        value=(
            "Безліміт"
            if not row["ticket_limit_per_user"]
            else str(
                row["ticket_limit_per_user"]
            )
        ),
        inline=True,
    )

    e.add_field(
        name="🎲 Переможців",
        value=str(
            row["winner_count"]
        ),
        inline=True,
    )

    e.add_field(
        name="🟢 Підтверджено",
        value=str(
            confirmed
        ),
        inline=True,
    )

    e.add_field(
        name="🟡 Зарезервовано",
        value=str(
            reserved
        ),
        inline=True,
    )

    e.add_field(
        name="⚪ Вільно",
        value=str(
            free
        ),
        inline=True,
    )

    e.add_field(
        name="⏰ До",
        value=(
            "Без обмеження"
            if not row["ends_at"]
            else fmt_dt(
                row["ends_at"]
            )
        ),
        inline=False,
    )

    if winner:
        e.add_field(
            name="🏆 Результат",
            value=winner,
            inline=False,
        )

    e.set_footer(
        text=f"ID лотереї: {row['id']}"
    )

    return e


def get_counts(
    lottery_id: int,
):
    rows = db.conn.execute(
        """
        SELECT status, COUNT(*) AS cnt
        FROM lottery_tickets
        WHERE lottery_id = ?
        GROUP BY status
        """,
        (lottery_id,),
    ).fetchall()

    counts = {
        r["status"]: int(
            r["cnt"]
        )
        for r in rows
    }

    return (
        counts.get(
            "reserved",
            0,
        ),
        counts.get(
            "confirmed",
            0,
        ),
    )


def get_lottery(
    lottery_id: int,
):
    return db.conn.execute(
        """
        SELECT *
        FROM lotteries
        WHERE id = ?
        """,
        (lottery_id,),
    ).fetchone()


def user_confirmed_count(
    lottery_id: int,
    user_id: int,
) -> int:
    row = db.conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM lottery_tickets
        WHERE lottery_id = ?
        AND user_id = ?
        AND status IN ('reserved','confirmed')
        """,
        (
            lottery_id,
            user_id,
        ),
    ).fetchone()

    return int(
        row["cnt"]
    )


def get_occupied_numbers(
    lottery_id: int,
):
    rows = db.conn.execute(
        """
        SELECT number
        FROM lottery_tickets
        WHERE lottery_id = ?
        AND status IN ('reserved','confirmed')
        """,
        (lottery_id,),
    ).fetchall()

    return {
        int(row["number"])
        for row in rows
    }


# ============================================================
# LOTTERY CREATE — STEP 1
# ============================================================

class LotteryCreateStep1(
    discord.ui.Modal,
    title="Створення лотереї • 1/2",
):
    name = discord.ui.TextInput(
        label="Назва",
        max_length=80,
    )

    prize_type = discord.ui.TextInput(
        label="Приз: cash або item",
        placeholder="cash / item",
        max_length=10,
    )

    prize = discord.ui.TextInput(
        label="Сума або опис призу",
        max_length=200,
    )

    ticket_price = discord.ui.TextInput(
        label="Ціна одного квитка",
        placeholder="500000",
        max_length=30,
    )

    total_tickets = discord.ui.TextInput(
        label="Кількість квитків",
        placeholder="100",
        max_length=10,
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):
        if (
            not isinstance(
                interaction.user,
                discord.Member,
            )
            or not management_payout_member(
                interaction.user
            )
        ):
            await temporary_error(
                interaction,
                "❌ Доступ тільки для керівництва.",
            )
            return

        kind = (
            self.prize_type.value
            .strip()
            .lower()
        )

        if kind not in {
            "cash",
            "item",
        }:
            await temporary_error(
                interaction,
                "❌ У полі призу вкажи `cash` або `item`.",
            )
            return

        try:
            price = (
                parse_money(
                    self.ticket_price.value
                )
                * 100
            )

            total = int(
                self.total_tickets.value
            )

        except Exception:
            await temporary_error(
                interaction,
                "❌ Перевір ціну та кількість квитків.",
            )
            return

        if price <= 0 or total <= 0:
            await temporary_error(
                interaction,
                "❌ Ціна і кількість мають бути більшими за 0.",
            )
            return

        prize_cents = 0

        prize_desc = (
            self.prize.value.strip()
        )

        if kind == "cash":
            try:
                prize_cents = (
                    parse_money(
                        self.prize.value
                    )
                    * 100
                )

            except Exception:
                await temporary_error(
                    interaction,
                    "❌ Для cash вкажи суму призу, "
                    "наприклад `2000000`.",
                )
                return

            if prize_cents <= 0:
                await temporary_error(
                    interaction,
                    "❌ Сума призу має бути більшою за 0.",
                )
                return

            prize_desc = ""

        state = {
            "name": self.name.value.strip(),
            "prize_type": kind,
            "prize_cents": prize_cents,
            "prize_description": prize_desc,
            "ticket_price_cents": price,
            "total_tickets": total,
        }

        view = LotteryStep2Button(
            state
        )

        await temporary_message(
            interaction,
            "✅ **Крок 1 з 2 завершено.**\n\n"
            "Основні дані лотереї збережено.\n"
            "Натисни кнопку нижче, щоб перейти "
            "до налаштування лімітів і розіграшу.",
            view=view,
        )


# ============================================================
# BUTTON BETWEEN STEP 1 AND STEP 2
# ============================================================

class LotteryStep2Button(
    AutoDeleteEphemeralView
):
    def __init__(
        self,
        state,
    ):
        super().__init__(
            timeout=300
        )

        self.state = state

        button = discord.ui.Button(
            label="Продовжити",
            emoji="➡️",
            style=discord.ButtonStyle.success,
        )

        button.callback = (
            self.continue_callback
        )

        self.add_item(
            button
        )

    async def continue_callback(
        self,
        interaction: discord.Interaction,
    ):
        self.cancel_auto_delete()

        await interaction.response.send_modal(
            LotteryCreateStep2(
                self.state
            )
        )

        # Після натискання "Продовжити"
        # старе тимчасове повідомлення зникає.
        try:
            await interaction.delete_original_response()

        except (
            discord.NotFound,
            discord.HTTPException,
        ):
            pass


# ============================================================
# LOTTERY CREATE — STEP 2
# ============================================================

class LotteryCreateStep2(
    discord.ui.Modal,
    title="Створення лотереї • 2/2",
):
    per_user = discord.ui.TextInput(
        label="Ліміт квитків на людину",
        placeholder="0 = безліміт",
        max_length=10,
    )

    winners = discord.ui.TextInput(
        label="Кількість переможців",
        placeholder="1",
        max_length=5,
    )

    duration = discord.ui.TextInput(
        label="Час до завершення",
        placeholder="2d 12h / 6h / 90m / 0",
        max_length=30,
    )

    def __init__(
        self,
        state,
    ):
        super().__init__()

        self.state = state

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):
        try:
            limit = int(
                self.per_user.value
            )

            winners = int(
                self.winners.value
            )

        except Exception:
            await temporary_error(
                interaction,
                "❌ Перевір ліміт і кількість переможців.",
            )
            return

        minutes = parse_duration(
            self.duration.value
        )

        if (
            limit < 0
            or winners <= 0
            or minutes is None
        ):
            await temporary_error(
                interaction,
                "❌ Перевір ліміт, переможців і час.\n"
                "Для часу `0` — без обмеження.",
            )
            return

        if winners > int(
            self.state["total_tickets"]
        ):
            await temporary_error(
                interaction,
                "❌ Переможців не може бути більше, "
                "ніж квитків.",
            )
            return

        now = datetime.now(
            timezone.utc
        )

        if minutes == 0:
            ends_at = ""

        else:
            ends_at = (
                now
                + timedelta(
                    minutes=minutes
                )
            ).isoformat()

        lottery_id = db.create_lottery(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            creator_id=interaction.user.id,
            **self.state,
            ticket_limit_per_user=limit,
            winner_count=winners,
            ends_at=ends_at,
        )

        row = get_lottery(
            lottery_id
        )

        public_channel = (
            interaction.client.get_channel(
                LOTTERY_PUBLIC_CHANNEL_ID
            )
        )

        if public_channel is None:
            await temporary_error(
                interaction,
                "❌ Не налаштований "
                "`LOTTERY_PUBLIC_CHANNEL_ID` у Railway.",
            )
            return

        msg = await public_channel.send(
            embed=lottery_embed(
                row,
                0,
                0,
            ),
            view=LotteryPublicView(
                lottery_id
            ),
        )

        db.set_lottery_message(
            lottery_id,
            msg.id,
        )

        await temporary_message(
            interaction,
            f"✅ Лотерею **{row['name']}** створено "
            f"та опубліковано в "
            f"<#{LOTTERY_PUBLIC_CHANNEL_ID}>.",
        )

        await audit_log(
            interaction.guild,
            "🎟️ Створено лотерею",
            f"Лотерея: **{row['name']}**\n"
            f"ID: **{lottery_id}**\n"
            f"Створив/ла: <@{interaction.user.id}>",
            discord.Color.gold(),
        )


# ============================================================
# PUBLIC LOTTERY VIEW
# ============================================================

class LotteryPublicView(
    discord.ui.View
):
    def __init__(
        self,
        lottery_id: int,
    ):
        super().__init__(
            timeout=None
        )

        self.lottery_id = lottery_id

        self.add_item(
            LotteryBuyButton(
                lottery_id
            )
        )

        self.add_item(
            LotteryMyTicketsButton(
                lottery_id
            )
        )


class LotteryBuyButton(
    discord.ui.Button
):
    def __init__(
        self,
        lottery_id,
    ):
        super().__init__(
            label="Купити квитки",
            emoji="🎟️",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"lottery:buy:{lottery_id}"
            ),
        )

        self.lottery_id = lottery_id

    async def callback(
        self,
        interaction,
    ):
        row = get_lottery(
            self.lottery_id
        )

        if (
            not row
            or row["status"] != "active"
        ):
            await temporary_error(
                interaction,
                "❌ Продаж цієї лотереї вже завершено.",
            )
            return

        view = TicketPickerView(
            self.lottery_id,
            interaction.user.id,
        )

        await temporary_message(
            interaction,
            "🎟️ **Вибір квитків**\n"
            "Натискай на номери, щоб вибрати їх.\n"
            "Після завершення натисни "
            "**Підтвердити вибір**.",
            view=view,
        )


class LotteryMyTicketsButton(
    discord.ui.Button
):
    def __init__(
        self,
        lottery_id,
    ):
        super().__init__(
            label="Мої квитки",
            emoji="🎫",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"lottery:mine:{lottery_id}"
            ),
        )

        self.lottery_id = lottery_id

    async def callback(
        self,
        interaction,
    ):
        rows = db.conn.execute(
            """
            SELECT number, status
            FROM lottery_tickets
            WHERE lottery_id = ?
            AND user_id = ?
            AND status IN ('reserved','confirmed')
            ORDER BY number
            """,
            (
                self.lottery_id,
                interaction.user.id,
            ),
        ).fetchall()

        if not rows:
            await temporary_error(
                interaction,
                "🎫 У тебе поки немає квитків "
                "у цій лотереї.",
            )
            return

        lines = [
            f"#{int(r['number']):02d} — "
            f"{'🟢 підтверджено' if r['status'] == 'confirmed' else '🟡 очікує оплати'}"
            for r in rows
        ]

        await temporary_message(
            interaction,
            "🎫 **Твої квитки**\n\n"
            + "\n".join(lines),
        )


# ============================================================
# TICKET PICKER
# ============================================================

class TicketPickerView(
    AutoDeleteEphemeralView
):
    def __init__(
        self,
        lottery_id,
        user_id,
        page=0,
        selected=None,
    ):
        super().__init__(
            timeout=300
        )

        self.lottery_id = lottery_id
        self.user_id = user_id
        self.page = page

        self.selected = set(
            selected or []
        )

        self.rebuild()

    def rebuild(self):
        """
        Повністю перебудовує кнопки поточної сторінки.

        Важливо:
        використовується ТОЙ САМИЙ View,
        тому таймер авто-видалення можна
        коректно перезапускати після кожного кліку.
        """

        self.clear_items()

        row = get_lottery(
            self.lottery_id
        )

        if not row:
            return

        total = int(
            row["total_tickets"]
        )

        start = (
            self.page * 20
            + 1
        )

        end = min(
            total,
            start + 19,
        )

        occupied = get_occupied_numbers(
            self.lottery_id
        )

        for n in range(
            start,
            end + 1,
        ):
            if (
                n in occupied
                and n not in self.selected
            ):
                continue

            button = discord.ui.Button(
                label=f"{n:02d}",
                style=(
                    discord.ButtonStyle.primary
                    if n in self.selected
                    else discord.ButtonStyle.secondary
                ),
                custom_id=(
                    f"pick:{self.lottery_id}:{n}"
                ),
                row=(n - start) // 5,
            )

            button.callback = (
                self.make_callback(n)
            )

            self.add_item(
                button
            )

        max_page = max(
            0,
            (total - 1) // 20,
        )

        prev = discord.ui.Button(
            label="Сторінка",
            emoji="⬅️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page == 0,
            row=4,
        )

        prev.callback = (
            self.page_callback(
                self.page - 1
            )
        )

        self.add_item(
            prev
        )

        next_button = discord.ui.Button(
            label=f"{self.page + 1}/{max_page + 1}",
            emoji="➡️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= max_page,
            row=4,
        )

        next_button.callback = (
            self.page_callback(
                self.page + 1
            )
        )

        self.add_item(
            next_button
        )

        confirm = discord.ui.Button(
            label=(
                f"Підтвердити вибір "
                f"({len(self.selected)})"
            ),
            emoji="✅",
            style=discord.ButtonStyle.success,
            row=4,
        )

        confirm.callback = (
            self.confirm_callback
        )

        self.add_item(
            confirm
        )

        clear = discord.ui.Button(
            label="Очистити",
            emoji="🔄",
            style=discord.ButtonStyle.danger,
            row=4,
        )

        clear.callback = (
            self.clear_callback
        )

        self.add_item(
            clear
        )

    async def refresh_view(
        self,
        interaction,
        text=None,
    ):
        self.rebuild()

        if text is None:
            text = (
                "🎟️ **Вибір квитків**\n"
                f"Обрано: **{len(self.selected)}**"
            )

        await interaction.response.edit_message(
            content=text,
            view=self,
        )

        # Кожна дія оновлює 10-секундний таймер.
        self.start_auto_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )

    def make_callback(
        self,
        n,
    ):
        async def cb(
            interaction,
        ):
            if (
                interaction.user.id
                != self.user_id
            ):
                await temporary_error(
                    interaction,
                    "❌ Це меню відкрив інший гравець.",
                )
                return

            row = get_lottery(
                self.lottery_id
            )

            if (
                not row
                or row["status"] != "active"
            ):
                self.cancel_auto_delete()

                await interaction.response.edit_message(
                    content=(
                        "🔒 **Продаж квитків завершено.**"
                    ),
                    view=None,
                )

                schedule_delete(
                    interaction,
                    TEMP_MESSAGE_SECONDS,
                )

                return

            # Якщо квиток уже зайняв інший користувач,
            # не дозволяємо його вибрати.
            occupied = get_occupied_numbers(
                self.lottery_id
            )

            if (
                n in occupied
                and n not in self.selected
            ):
                await temporary_error(
                    interaction,
                    f"❌ Квиток **#{n:02d}** "
                    "вже зарезервований або підтверджений.",
                )
                return

            if n in self.selected:
                self.selected.remove(n)

            else:
                limit = int(
                    row["ticket_limit_per_user"]
                )

                already = user_confirmed_count(
                    self.lottery_id,
                    self.user_id,
                )

                if (
                    limit
                    and already
                    + len(self.selected)
                    + 1
                    > limit
                ):
                    await temporary_error(
                        interaction,
                        f"❌ Ліміт: {limit} "
                        "квитків на людину.",
                    )
                    return

                self.selected.add(n)

            await self.refresh_view(
                interaction
            )

        return cb

    def page_callback(
        self,
        page,
    ):
        async def cb(
            interaction,
        ):
            if (
                interaction.user.id
                != self.user_id
            ):
                await temporary_error(
                    interaction,
                    "❌ Це меню відкрив інший гравець.",
                )
                return

            self.page = page

            await self.refresh_view(
                interaction
            )

        return cb

    async def clear_callback(
        self,
        interaction,
    ):
        if (
            interaction.user.id
            != self.user_id
        ):
            await temporary_error(
                interaction,
                "❌ Це меню відкрив інший гравець.",
            )
            return

        self.selected.clear()

        await self.refresh_view(
            interaction,
            (
                "🎟️ **Вибір квитків**\n"
                "Обрано: **0**"
            ),
        )

    async def confirm_callback(
        self,
        interaction,
    ):
        if (
            interaction.user.id
            != self.user_id
        ):
            await temporary_error(
                interaction,
                "❌ Це меню відкрив інший гравець.",
            )
            return

        if not self.selected:
            await temporary_error(
                interaction,
                "❌ Обери хоча б один номер.",
            )
            return

        row = get_lottery(
            self.lottery_id
        )

        if (
            not row
            or row["status"] != "active"
        ):
            self.cancel_auto_delete()

            await interaction.response.edit_message(
                content=(
                    "🔒 **Продаж квитків завершено.**"
                ),
                view=None,
            )

            schedule_delete(
                interaction,
                TEMP_MESSAGE_SECONDS,
            )

            return

        selected_numbers = sorted(
            self.selected
        )

        ok, request_id, reason = (
            db.reserve_lottery_tickets(
                self.lottery_id,
                self.user_id,
                selected_numbers,
            )
        )

        if not ok:
            self.cancel_auto_delete()

            await interaction.response.edit_message(
                content=f"❌ {reason}",
                view=None,
            )

            schedule_delete(
                interaction,
                TEMP_MESSAGE_SECONDS,
            )

            # Навіть при помилці оновлюємо публічний
            # стан, бо причиною міг бути вже зайнятий квиток.
            await refresh_lottery_message(
                interaction.client,
                self.lottery_id,
            )

            return

        # ====================================================
        # ГОЛОВНЕ ВИПРАВЛЕННЯ:
        # після резервування одразу оновлюємо
        # "🟡 Зарезервовано" у публічній лотереї.
        # ====================================================

        await refresh_lottery_message(
            interaction.client,
            self.lottery_id,
        )

        total = (
            len(selected_numbers)
            * int(
                row["ticket_price_cents"]
            )
        )

        self.cancel_auto_delete()

        payment_view = LotteryPaymentView(
            request_id
        )

        await interaction.response.edit_message(
            content=(
                f"🎫 **Резерв створено**\n\n"
                f"Квитків: **{len(selected_numbers)}**\n"
                f"Номери: **"
                f"{', '.join(f'{n:02d}' for n in selected_numbers)}"
                f"**\n"
                f"Сума: **{format_cents(total)}**\n\n"
                "Після внесення коштів натисни "
                "кнопку нижче. Квитки залишаються "
                "зарезервованими, поки керівництво "
                "не підтвердить або не відхилить оплату."
            ),
            view=payment_view,
        )

        payment_view.start_auto_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )


# ============================================================
# PAYMENT VIEW
# ============================================================

class LotteryPaymentView(
    AutoDeleteEphemeralView
):
    def __init__(
        self,
        request_id,
    ):
        super().__init__(
            timeout=None
        )

        self.request_id = request_id

        paid_button = discord.ui.Button(
            label="Кошти внесено",
            emoji="💰",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"lottery:paid:{request_id}"
            ),
        )

        paid_button.callback = (
            self.paid
        )

        self.add_item(
            paid_button
        )

        cancel_button = discord.ui.Button(
            label="Скасувати",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=(
                f"lottery:cancel:{request_id}"
            ),
        )

        cancel_button.callback = (
            self.cancel
        )

        self.add_item(
            cancel_button
        )

    async def paid(
        self,
        interaction,
    ):
        req = db.lottery_request(
            self.request_id
        )

        if (
            not req
            or req["user_id"]
            != interaction.user.id
        ):
            await temporary_error(
                interaction,
                "❌ Цей запит недоступний.",
            )
            return

        if req["status"] != "reserved":
            await temporary_error(
                interaction,
                "❌ Запит уже обробляється або завершений.",
            )
            return

        db.mark_lottery_payment_pending(
            self.request_id
        )

        self.cancel_auto_delete()

        await interaction.response.edit_message(
            content=(
                "🟡 **Кошти внесено.**\n"
                "Очікується перевірка керівництвом."
            ),
            view=None,
        )

        schedule_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )

        try:
            await send_pending_request(
                interaction,
                self.request_id,
            )

        except discord.DiscordException as exc:
            print(
                "[LOTTERY] Could not send "
                f"pending payment request: {exc}"
            )

    async def cancel(
        self,
        interaction,
    ):
        req = db.lottery_request(
            self.request_id
        )

        if (
            not req
            or req["user_id"]
            != interaction.user.id
        ):
            await temporary_error(
                interaction,
                "❌ Цей запит недоступний.",
            )
            return

        if req["status"] not in {
            "reserved",
            "payment_pending",
        }:
            await temporary_error(
                interaction,
                "❌ Запит уже завершений.",
            )
            return

        db.reject_lottery_request(
            self.request_id,
            interaction.user.id,
            "Скасовано гравцем",
        )

        self.cancel_auto_delete()

        await interaction.response.edit_message(
            content=(
                "❌ **Резерв скасовано.**\n"
                "Номери знову доступні."
            ),
            view=None,
        )

        schedule_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )

        # Повертаємо квитки у "вільні".
        await refresh_lottery_message(
            interaction.client,
            req["lottery_id"],
        )


# ============================================================
# ADMIN PAYMENT VIEW
# ============================================================

class LotteryAdminView(
    discord.ui.View
):
    def __init__(
        self,
        request_id,
    ):
        super().__init__(
            timeout=None
        )

        self.request_id = request_id

        ok = discord.ui.Button(
            label="Підтвердити оплату",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"lottery:confirm:{request_id}"
            ),
        )

        ok.callback = self.confirm

        no = discord.ui.Button(
            label="Відхилити оплату",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=(
                f"lottery:reject:{request_id}"
            ),
        )

        no.callback = self.reject

        self.add_item(ok)
        self.add_item(no)

    async def confirm(
        self,
        interaction,
    ):
        if (
            not isinstance(
                interaction.user,
                discord.Member,
            )
            or not management_payout_member(
                interaction.user
            )
        ):
            await temporary_error(
                interaction,
                "❌ Доступ тільки для керівництва.",
            )
            return

        req = db.lottery_request(
            self.request_id
        )

        if (
            not req
            or req["status"]
            != "payment_pending"
        ):
            await temporary_error(
                interaction,
                "❌ Цей запит уже оброблений.",
            )
            return

        db.confirm_lottery_request(
            self.request_id,
            interaction.user.id,
        )

        await interaction.response.edit_message(
            content=(
                f"✅ **Оплату підтверджено.**\n"
                f"Квитки: {req['numbers_json']}"
            ),
            view=None,
        )

        schedule_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )

        # reserved -> confirmed.
        await refresh_lottery_message(
            interaction.client,
            req["lottery_id"],
        )

    async def reject(
        self,
        interaction,
    ):
        if (
            not isinstance(
                interaction.user,
                discord.Member,
            )
            or not management_payout_member(
                interaction.user
            )
        ):
            await temporary_error(
                interaction,
                "❌ Доступ тільки для керівництва.",
            )
            return

        req = db.lottery_request(
            self.request_id
        )

        if (
            not req
            or req["status"]
            != "payment_pending"
        ):
            await temporary_error(
                interaction,
                "❌ Цей запит уже оброблений.",
            )
            return

        db.reject_lottery_request(
            self.request_id,
            interaction.user.id,
            "Оплату відхилено керівництвом",
        )

        await interaction.response.edit_message(
            content=(
                "❌ **Оплату відхилено.**\n"
                "Зарезервовані номери звільнено."
            ),
            view=None,
        )

        schedule_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )

        # reserved -> free.
        await refresh_lottery_message(
            interaction.client,
            req["lottery_id"],
        )


# ============================================================
# REFRESH LOTTERY MESSAGE
# ============================================================

async def refresh_lottery_message(
    bot,
    lottery_id,
):
    """
    Повністю оновлює публічне повідомлення лотереї.

    Тут рахується фактична кількість:
        reserved
        confirmed

    Тому після резерву:
        🟡 Зарезервовано
    збільшується одразу.

    Після підтвердження:
        🟡 Зарезервовано зменшується
        🟢 Підтверджено збільшується

    Після відхилення/скасування:
        🟡 Зарезервовано зменшується
        ⚪ Вільно збільшується
    """

    row = get_lottery(
        lottery_id
    )

    if (
        not row
        or not row["message_id"]
    ):
        return

    channel = bot.get_channel(
        LOTTERY_PUBLIC_CHANNEL_ID
    )

    if not channel:
        return

    try:
        msg = await channel.fetch_message(
            row["message_id"]
        )

        reserved, confirmed = get_counts(
            lottery_id
        )

        await msg.edit(
            embed=lottery_embed(
                row,
                reserved,
                confirmed,
            ),
            view=(
                LotteryPublicView(
                    lottery_id
                )
                if row["status"] == "active"
                else None
            ),
        )

    except discord.DiscordException as exc:
        print(
            "[LOTTERY] Could not refresh "
            f"lottery #{lottery_id}: {exc}"
        )


# ============================================================
# PENDING PAYMENT REQUEST
# ============================================================

async def send_pending_request(
    interaction,
    request_id,
):
    req = db.lottery_request(
        request_id
    )

    if not req:
        return

    lottery = get_lottery(
        req["lottery_id"]
    )

    if not lottery:
        return

    channel = interaction.client.get_channel(
        LOTTERY_MANAGEMENT_CHANNEL_ID
    )

    if not channel:
        return

    member = (
        interaction.guild.get_member(
            req["user_id"]
        )
        if interaction.guild
        else None
    )

    name = (
        member.mention
        if member
        else f"<@{req['user_id']}>"
    )

    nums = [
        int(x)
        for x in json.loads(
            req["numbers_json"]
        )
    ]

    total = (
        len(nums)
        * int(
            lottery["ticket_price_cents"]
        )
    )

    await channel.send(
        f"🎟️ **Очікує перевірки оплати**\n"
        f"{lottery['name']}\n"
        f"{name}\n"
        f"Квитків: **{len(nums)}**\n"
        f"Номери: **"
        f"{', '.join(f'{n:02d}' for n in nums)}"
        f"**\n"
        f"Сума: **{format_cents(total)}**",
        view=LotteryAdminView(
            request_id
        ),
    )


# ============================================================
# PENDING REQUEST LIST
# ============================================================

class LotteryPendingView(
    discord.ui.View
):
    def __init__(
        self,
        requests,
    ):
        super().__init__(
            timeout=300
        )

        for req in requests[:25]:
            button = discord.ui.Button(
                label=(
                    f"{req['lottery_name'][:25]} • "
                    f"{req['user_id']}"
                ),
                emoji="🎟️",
                style=discord.ButtonStyle.secondary,
            )

            button.callback = (
                self.make_callback(
                    req["id"]
                )
            )

            self.add_item(
                button
            )

    def make_callback(
        self,
        request_id,
    ):
        async def callback(
            interaction,
        ):
            if (
                not isinstance(
                    interaction.user,
                    discord.Member,
                )
                or not management_payout_member(
                    interaction.user
                )
            ):
                await temporary_error(
                    interaction,
                    "❌ Доступ тільки для керівництва.",
                )
                return

            req = db.lottery_request(
                request_id
            )

            if not req:
                await interaction.response.edit_message(
                    content="❌ Заявку не знайдено.",
                    view=None,
                )

                schedule_delete(
                    interaction,
                    TEMP_MESSAGE_SECONDS,
                )

                return

            if req["status"] != "payment_pending":
                await interaction.response.edit_message(
                    content=(
                        "❌ Ця заявка вже оброблена."
                    ),
                    view=None,
                )

                schedule_delete(
                    interaction,
                    TEMP_MESSAGE_SECONDS,
                )

                return

            nums = [
                int(x)
                for x in json.loads(
                    req["numbers_json"]
                )
            ]

            total = (
                len(nums)
                * req["ticket_price_cents"]
            )

            await interaction.response.edit_message(
                content=(
                    f"🎟️ **{req['lottery_name']}**\n\n"
                    f"👤 <@{req['user_id']}>\n"
                    f"🎫 Квитки: "
                    f"{', '.join(f'{n:02d}' for n in nums)}\n"
                    f"💰 Сума: **{format_cents(total)}**"
                ),
                view=LotteryAdminView(
                    request_id
                ),
            )

        return callback


# ============================================================
# MANAGEMENT VIEW
# ============================================================

class LotteryManagementView(
    AutoDeleteEphemeralView
):
    def __init__(self):
        super().__init__(
            timeout=None
        )

        create_button = discord.ui.Button(
            label="Створити лотерею",
            emoji="🎟️",
            style=discord.ButtonStyle.success,
            custom_id=(
                "lottery:management:create"
            ),
        )

        create_button.callback = (
            self.create
        )

        self.add_item(
            create_button
        )

        pending_button = discord.ui.Button(
            label="Очікують оплати",
            emoji="🟡",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                "lottery:management:pending"
            ),
        )

        pending_button.callback = (
            self.pending
        )

        self.add_item(
            pending_button
        )

        draw_button = discord.ui.Button(
            label="Завершити/розіграти",
            emoji="🎲",
            style=discord.ButtonStyle.primary,
            custom_id=(
                "lottery:management:draw"
            ),
        )

        draw_button.callback = (
            self.draw_list
        )

        self.add_item(
            draw_button
        )

    async def create(
        self,
        interaction,
    ):
        if (
            not isinstance(
                interaction.user,
                discord.Member,
            )
            or not management_payout_member(
                interaction.user
            )
        ):
            await temporary_error(
                interaction,
                "❌ Доступ тільки для керівництва.",
            )
            return

        # Панель тимчасова.
        self.cancel_auto_delete()

        await interaction.response.send_modal(
            LotteryCreateStep1()
        )

        try:
            await interaction.delete_original_response()

        except (
            discord.NotFound,
            discord.HTTPException,
        ):
            pass

    async def pending(
        self,
        interaction,
    ):
        if (
            not isinstance(
                interaction.user,
                discord.Member,
            )
            or not management_payout_member(
                interaction.user
            )
        ):
            await temporary_error(
                interaction,
                "❌ Доступ тільки для керівництва.",
            )
            return

        rows = db.pending_lottery_requests(
            interaction.guild_id
        )

        if not rows:
            await temporary_message(
                interaction,
                "🟢 Немає оплат, які очікують перевірки.",
            )
            return

        lines = []

        for req in rows[:25]:
            nums = [
                int(x)
                for x in json.loads(
                    req["numbers_json"]
                )
            ]

            lines.append(
                f"🎟️ **{req['lottery_name']}** — "
                f"<@{req['user_id']}> — "
                f"{len(nums)} кв."
            )

        await interaction.response.send_message(
            "🟡 **Очікують перевірки оплати**\n\n"
            + "\n".join(lines)
            + "\n\n"
            "Натисни на заявку, щоб переглянути її.",
            view=LotteryPendingView(
                rows[:25]
            ),
            ephemeral=True,
        )

        schedule_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )

    async def draw_list(
        self,
        interaction,
    ):
        if (
            not isinstance(
                interaction.user,
                discord.Member,
            )
            or not management_payout_member(
                interaction.user
            )
        ):
            await temporary_error(
                interaction,
                "❌ Доступ тільки для керівництва.",
            )
            return

        rows = db.conn.execute(
            """
            SELECT *
            FROM lotteries
            WHERE guild_id=?
            AND status IN ('active','finished')
            ORDER BY id DESC
            """,
            (interaction.guild_id,),
        ).fetchall()

        if not rows:
            await temporary_message(
                interaction,
                "🟢 Немає активних лотерей.",
            )
            return

        view = LotteryDrawListView(
            rows
        )

        await interaction.response.send_message(
            "🎲 **Обери лотерею для завершення:**",
            view=view,
            ephemeral=True,
        )

        view.start_auto_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )


# ============================================================
# DRAW LIST
# ============================================================

class LotteryDrawListView(
    AutoDeleteEphemeralView
):
    def __init__(
        self,
        rows,
    ):
        super().__init__(
            timeout=300
        )

        for row in rows[:25]:
            button = discord.ui.Button(
                label=row["name"][:70],
                style=discord.ButtonStyle.primary,
            )

            button.callback = (
                self.make(
                    row["id"]
                )
            )

            self.add_item(
                button
            )

    def make(
        self,
        lottery_id,
    ):
        async def cb(
            interaction,
        ):
            if (
                not isinstance(
                    interaction.user,
                    discord.Member,
                )
                or not management_payout_member(
                    interaction.user
                )
            ):
                await temporary_error(
                    interaction,
                    "❌ Доступ тільки для керівництва.",
                )
                return

            result = db.draw_lottery(
                lottery_id,
                interaction.user.id,
            )

            if not result[0]:
                await temporary_error(
                    interaction,
                    f"❌ {result[1]}",
                )
                return

            row = get_lottery(
                lottery_id
            )

            nums = result[2]

            text = ", ".join(
                f"#{n:02d}"
                for n in nums
            )

            winner_lines = []
            winner_rows = []

            for n in nums:
                ticket = db.conn.execute(
                    """
                    SELECT user_id
                    FROM lottery_tickets
                    WHERE lottery_id=?
                    AND number=?
                    """,
                    (
                        lottery_id,
                        n,
                    ),
                ).fetchone()

                if ticket:
                    winner_rows.append(
                        (
                            n,
                            ticket["user_id"],
                        )
                    )

                    winner_lines.append(
                        f"🎫 **#{n:02d}** — "
                        f"<@{ticket['user_id']}>"
                    )

            winner_text = (
                "\n".join(
                    winner_lines
                )
                if winner_lines
                else "Переможців не знайдено."
            )

            self.cancel_auto_delete()

            await interaction.response.edit_message(
                content=(
                    "🏆 **Розіграш проведено**\n\n"
                    f"{row['name']}\n"
                    f"Переможні квитки: **{text}**"
                ),
                view=None,
            )

            schedule_delete(
                interaction,
                TEMP_MESSAGE_SECONDS,
            )

            await refresh_lottery_message(
                interaction.client,
                lottery_id,
            )

            if row["prize_type"] == "cash":
                for n, user_id in winner_rows:
                    db.create_lottery_payout(
                        interaction.guild_id,
                        user_id,
                        lottery_id,
                        n,
                        row["prize_cents"],
                    )

            public_channel = (
                interaction.client.get_channel(
                    LOTTERY_PUBLIC_CHANNEL_ID
                )
            )

            if public_channel:
                prize_text = (
                    format_cents(
                        row["prize_cents"]
                    )
                    if row["prize_type"] == "cash"
                    else (
                        row["prize_description"]
                        or "Не вказано"
                    )
                )

                try:
                    await public_channel.send(
                        "🏆 **РЕЗУЛЬТАТИ ЛОТЕРЕЇ**\n\n"
                        f"🎟️ **{row['name']}**\n"
                        f"🎁 Приз: **{prize_text}**\n\n"
                        f"{winner_text}"
                    )

                except discord.DiscordException as exc:
                    print(
                        "[LOTTERY] Could not send "
                        f"public result: {exc}"
                    )

            await audit_log(
                interaction.guild,
                "🏆 Лотерею розіграно",
                f"**{row['name']}**\n"
                f"Переможні квитки: **{text}**",
                discord.Color.green(),
            )

        return cb


# ============================================================
# SLASH COMMAND
# ============================================================

def register_commands(
    bot: commands.Bot
):
    @bot.tree.command(
        name="lottery",
        description="Керування лотереями Agosto",
    )
    async def lottery(
        interaction: discord.Interaction,
    ):
        if (
            not isinstance(
                interaction.user,
                discord.Member,
            )
            or not management_payout_member(
                interaction.user
            )
        ):
            await temporary_error(
                interaction,
                "❌ Команда доступна тільки керівництву.",
            )
            return

        if (
            LOTTERY_MANAGEMENT_CHANNEL_ID
            and interaction.channel_id
            != LOTTERY_MANAGEMENT_CHANNEL_ID
        ):
            await temporary_error(
                interaction,
                f"🎟️ Керування лотереями знаходиться "
                f"в <#{LOTTERY_MANAGEMENT_CHANNEL_ID}>.",
            )
            return

        view = LotteryManagementView()

        await interaction.response.send_message(
            "🎟️ **Лотереї Agosto**",
            view=view,
            ephemeral=True,
        )

        view.start_auto_delete(
            interaction,
            TEMP_MESSAGE_SECONDS,
        )


# ============================================================
# MANAGEMENT PANEL
# ============================================================

async def ensure_lottery_channels(
    bot,
):
    """
    Забезпечує наявність постійної панелі
    керування в management-каналі.

    Це НЕ тимчасове повідомлення.
    Воно не видаляється через 10 секунд.
    """

    if not LOTTERY_MANAGEMENT_CHANNEL_ID:
        return

    channel = bot.get_channel(
        LOTTERY_MANAGEMENT_CHANNEL_ID
    )

    if channel is None:
        print(
            "[LOTTERY] Management channel not found"
        )
        return

    key = (
        "lottery_management_panel_message_id"
    )

    existing_id = db.get_setting(
        GUILD_ID,
        key,
    )

    if existing_id:
        try:
            msg = await channel.fetch_message(
                int(existing_id)
            )

            await msg.edit(
                content=(
                    "🎟️ **Лотереї Agosto — керування**"
                ),
                view=LotteryManagementView(),
            )

            return

        except discord.DiscordException:
            pass

    msg = await channel.send(
        content=(
            "🎟️ **Лотереї Agosto — керування**\n"
            "Створення лотерей, перевірка оплат "
            "та проведення розіграшів."
        ),
        view=LotteryManagementView(),
    )

    db.set_setting(
        GUILD_ID,
        key,
        str(msg.id),
    )


# ============================================================
# RESTORE VIEWS
# ============================================================

def restore_management_view(
    bot,
):
    try:
        bot.add_view(
            LotteryManagementView()
        )

    except Exception:
        pass


def restore_active_views(
    bot,
):
    for row in db.active_lotteries(
        GUILD_ID
    ):
        if row["message_id"]:
            try:
                bot.add_view(
                    LotteryPublicView(
                        row["id"]
                    ),
                    message_id=row["message_id"],
                )

            except Exception:
                pass


# ============================================================
# AUTO FINISH EXPIRED LOTTERIES
# ============================================================

async def maybe_finish_expired(
    bot,
    now,
):
    now_utc = now.astimezone(
        timezone.utc
    )

    rows = db.conn.execute(
        """
        SELECT *
        FROM lotteries
        WHERE guild_id=?
        AND status='active'
        """,
        (GUILD_ID,),
    ).fetchall()

    for row in rows:

        # 0 = без обмеження часу.
        # Така лотерея завершується тільки вручну.
        if not row["ends_at"]:
            continue

        try:
            ends = datetime.fromisoformat(
                row["ends_at"].replace(
                    "Z",
                    "+00:00",
                )
            )

        except Exception:
            continue

        if now_utc < ends:
            continue

        db.conn.execute(
            """
            UPDATE lotteries
            SET status='finished'
            WHERE id=?
            AND status='active'
            """,
            (row["id"],),
        )

        db.conn.commit()

        await refresh_lottery_message(
            bot,
            row["id"],
        )

        channel = bot.get_channel(
            LOTTERY_PUBLIC_CHANNEL_ID
        )

        if channel:
            try:
                await channel.send(
                    f"🔒 **Лотерея «{row['name']}» "
                    f"завершила продаж квитків.**\n"
                    "🎲 Керівництво може провести "
                    "розіграш через `/lottery`."
                )

            except discord.DiscordException:
                pass
