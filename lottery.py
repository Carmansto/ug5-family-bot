import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_ID, LOTTERY_PUBLIC_CHANNEL_ID, LOTTERY_MANAGEMENT_CHANNEL_ID
from database import db
from utils import format_cents, format_money_dollars, management_payout_member, parse_money, utc_now_iso
from contracts import audit_log


def parse_duration(raw: str) -> Optional[int]:
    s = raw.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", s)
    if not m or not any(m.groups()):
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    mins = int(m.group(3) or 0)
    total = days * 1440 + hours * 60 + mins
    return total if total > 0 else None


def fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def lottery_status_text(row) -> str:
    if row["status"] == "active":
        return "🟢 Прийом квитків відкритий"
    if row["status"] == "finished":
        return "🔒 Продаж завершено"
    if row["status"] == "drawn":
        return "🏆 Розіграш проведено"
    return row["status"]


def lottery_embed(row, reserved=0, confirmed=0, winner=None):
    total = int(row["total_tickets"])
    free = max(0, total - reserved - confirmed)
    prize = (
        format_cents(int(row["prize_cents"]))
        if row["prize_type"] == "cash"
        else (row["prize_description"] or "Не вказано")
    )
    e = discord.Embed(
        title=f"🎟️ {row['name']}",
        description=lottery_status_text(row),
        color=discord.Color.gold(),
    )
    e.add_field(name="🏆 Приз", value=prize, inline=False)
    e.add_field(name="🎫 Ціна квитка", value=format_cents(row["ticket_price_cents"]), inline=True)
    e.add_field(name="🎫 Квитків", value=f"{total}", inline=True)
    e.add_field(name="👤 Ліміт на людину", value="Безліміт" if not row["ticket_limit_per_user"] else str(row["ticket_limit_per_user"]), inline=True)
    e.add_field(name="🎲 Переможців", value=str(row["winner_count"]), inline=True)
    e.add_field(name="🟢 Підтверджено", value=str(confirmed), inline=True)
    e.add_field(name="🟡 Зарезервовано", value=str(reserved), inline=True)
    e.add_field(name="⚪ Вільно", value=str(free), inline=True)
    e.add_field(name="⏰ До", value=fmt_dt(row["ends_at"]), inline=False)
    if winner:
        e.add_field(name="🏆 Результат", value=winner, inline=False)
    e.set_footer(text=f"ID лотереї: {row['id']}")
    return e


def get_counts(lottery_id: int):
    rows = db.conn.execute("""
      SELECT status, COUNT(*) AS cnt FROM lottery_tickets
      WHERE lottery_id = ? GROUP BY status
    """, (lottery_id,)).fetchall()
    counts = {r["status"]: int(r["cnt"]) for r in rows}
    return counts.get("reserved", 0), counts.get("confirmed", 0)


def get_lottery(lottery_id: int):
    return db.conn.execute("SELECT * FROM lotteries WHERE id = ?", (lottery_id,)).fetchone()


def user_confirmed_count(lottery_id: int, user_id: int) -> int:
    row = db.conn.execute("""
      SELECT COUNT(*) AS cnt FROM lottery_tickets
      WHERE lottery_id = ? AND user_id = ? AND status IN ('reserved','confirmed')
    """, (lottery_id, user_id)).fetchone()
    return int(row["cnt"])


class LotteryCreateStep1(discord.ui.Modal, title="Створення лотереї • 1/2"):
    name = discord.ui.TextInput(label="Назва", max_length=80)
    prize_type = discord.ui.TextInput(label="Приз: cash або item", placeholder="cash / item", max_length=10)
    prize = discord.ui.TextInput(label="Сума або опис призу", max_length=200)
    ticket_price = discord.ui.TextInput(label="Ціна одного квитка", placeholder="500000", max_length=30)
    total_tickets = discord.ui.TextInput(label="Кількість квитків", placeholder="100", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
            await interaction.response.send_message("❌ Доступ тільки для керівництва.", ephemeral=True)
            return
        kind = self.prize_type.value.strip().lower()
        if kind not in {"cash", "item"}:
            await interaction.response.send_message("❌ У полі призу вкажи `cash` або `item`.", ephemeral=True)
            return
        try:
            price = parse_money(self.ticket_price.value) * 100
            total = int(self.total_tickets.value)
        except Exception:
            await interaction.response.send_message("❌ Перевір ціну та кількість квитків.", ephemeral=True)
            return
        if price <= 0 or total <= 0:
            await interaction.response.send_message("❌ Ціна і кількість мають бути більшими за 0.", ephemeral=True)
            return
        prize_cents = 0
        prize_desc = self.prize.value.strip()
        if kind == "cash":
            try:
                prize_cents = parse_money(self.prize.value) * 100
            except Exception:
                await interaction.response.send_message("❌ Для cash вкажи суму призу, наприклад `2000000`.", ephemeral=True)
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
        await interaction.response.send_modal(LotteryCreateStep2(state))


class LotteryCreateStep2(discord.ui.Modal, title="Створення лотереї • 2/2"):
    per_user = discord.ui.TextInput(label="Ліміт квитків на людину", placeholder="0 = безліміт", max_length=10)
    winners = discord.ui.TextInput(label="Кількість переможців", placeholder="1", max_length=5)
    duration = discord.ui.TextInput(label="Час до завершення", placeholder="2d 12h / 6h / 90m", max_length=30)

    def __init__(self, state):
        super().__init__()
        self.state = state

    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit = int(self.per_user.value)
            winners = int(self.winners.value)
        except Exception:
            await interaction.response.send_message("❌ Перевір ліміт і кількість переможців.", ephemeral=True)
            return
        minutes = parse_duration(self.duration.value)
        if limit < 0 or winners <= 0 or minutes is None:
            await interaction.response.send_message("❌ Перевір ліміт, переможців і час.", ephemeral=True)
            return
        if winners > int(self.state["total_tickets"]):
            await interaction.response.send_message("❌ Переможців не може бути більше, ніж квитків.", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        ends = now + timedelta(minutes=minutes)
        lottery_id = db.create_lottery(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            creator_id=interaction.user.id,
            **self.state,
            ticket_limit_per_user=limit,
            winner_count=winners,
            ends_at=ends.isoformat(),
        )
        row = get_lottery(lottery_id)
        public_channel = interaction.client.get_channel(LOTTERY_PUBLIC_CHANNEL_ID)
        if public_channel is None:
            await interaction.response.send_message(
                "❌ Не налаштований LOTTERY_PUBLIC_CHANNEL_ID у Railway.",
                ephemeral=True,
            )
            return
        await public_channel.send(
            embed=lottery_embed(row),
            view=LotteryPublicView(lottery_id),
        )
        msg = await public_channel.fetch_message(public_channel.last_message_id)
        db.set_lottery_message(lottery_id, msg.id)
        await interaction.response.send_message(
            f"✅ Лотерею **{row['name']}** створено та опубліковано в <#{LOTTERY_PUBLIC_CHANNEL_ID}>.",
            ephemeral=True,
        )
        await audit_log(interaction.guild, "🎟️ Створено лотерею", f"Лотерея: **{row['name']}**\nID: **{lottery_id}**\nСтворив/ла: <@{interaction.user.id}>", discord.Color.gold())


class LotteryPublicView(discord.ui.View):
    def __init__(self, lottery_id: int):
        super().__init__(timeout=None)
        self.lottery_id = lottery_id
        self.add_item(LotteryBuyButton(lottery_id))
        self.add_item(LotteryMyTicketsButton(lottery_id))


class LotteryBuyButton(discord.ui.Button):
    def __init__(self, lottery_id):
        super().__init__(label="Купити квитки", emoji="🎟️", style=discord.ButtonStyle.success, custom_id=f"lottery:buy:{lottery_id}")
        self.lottery_id = lottery_id

    async def callback(self, interaction):
        row = get_lottery(self.lottery_id)
        if not row or row["status"] != "active":
            await interaction.response.send_message("❌ Продаж цієї лотереї вже завершено.", ephemeral=True)
            return
        await interaction.response.send_message("Оберіть номери квитків нижче.", view=TicketPickerView(self.lottery_id, interaction.user.id), ephemeral=True)


class LotteryMyTicketsButton(discord.ui.Button):
    def __init__(self, lottery_id):
        super().__init__(label="Мої квитки", emoji="🎫", style=discord.ButtonStyle.secondary, custom_id=f"lottery:mine:{lottery_id}")
        self.lottery_id = lottery_id

    async def callback(self, interaction):
        rows = db.conn.execute("""
          SELECT number, status FROM lottery_tickets
          WHERE lottery_id = ? AND user_id = ? AND status IN ('reserved','confirmed')
          ORDER BY number
        """, (self.lottery_id, interaction.user.id)).fetchall()
        if not rows:
            await interaction.response.send_message("🎫 У тебе поки немає квитків у цій лотереї.", ephemeral=True)
            return
        lines = [f"#{int(r['number']):02d} — {'🟢 підтверджено' if r['status']=='confirmed' else '🟡 очікує оплати'}" for r in rows]
        await interaction.response.send_message("🎫 **Твої квитки**\n" + "\n".join(lines), ephemeral=True)


class TicketPickerView(discord.ui.View):
    def __init__(self, lottery_id, user_id, page=0, selected=None):
        super().__init__(timeout=300)
        self.lottery_id = lottery_id
        self.user_id = user_id
        self.page = page
        self.selected = set(selected or [])
        row = get_lottery(lottery_id)
        total = int(row["total_tickets"])
        start = page * 20 + 1
        end = min(total, start + 19)
        occupied = {int(r["number"]) for r in db.conn.execute("SELECT number FROM lottery_tickets WHERE lottery_id = ? AND status IN ('reserved','confirmed')", (lottery_id,)).fetchall()}
        for n in range(start, end + 1):
            if n in occupied and n not in self.selected:
                continue
            b = discord.ui.Button(label=f"{n:02d}", style=discord.ButtonStyle.primary if n in self.selected else discord.ButtonStyle.secondary, custom_id=f"pick:{n}", row=(n-start)//5)
            b.callback = self.make_callback(n)
            self.add_item(b)
        max_page = max(0, (total - 1) // 20)
        prev = discord.ui.Button(label="Сторінка", emoji="⬅️", style=discord.ButtonStyle.secondary, disabled=page == 0, row=4)
        prev.callback = self.page_callback(page - 1)
        self.add_item(prev)
        nextb = discord.ui.Button(label=f"{page+1}/{max_page+1}", emoji="➡️", style=discord.ButtonStyle.secondary, disabled=page >= max_page, row=4)
        nextb.callback = self.page_callback(page + 1)
        self.add_item(nextb)
        confirm = discord.ui.Button(label=f"Підтвердити вибір ({len(self.selected)})", emoji="✅", style=discord.ButtonStyle.success, row=4)
        confirm.callback = self.confirm_callback
        self.add_item(confirm)
        clear = discord.ui.Button(label="Очистити", emoji="🔄", style=discord.ButtonStyle.danger, row=4)
        clear.callback = self.clear_callback
        self.add_item(clear)

    def make_callback(self, n):
        async def cb(interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Це меню відкрив інший гравець.", ephemeral=True)
                return
            if n in self.selected:
                self.selected.remove(n)
            else:
                row = get_lottery(self.lottery_id)
                limit = int(row["ticket_limit_per_user"])
                already = user_confirmed_count(self.lottery_id, self.user_id)
                if limit and already + len(self.selected) + 1 > limit:
                    await interaction.response.send_message(f"❌ Ліміт: {limit} квитків на людину.", ephemeral=True)
                    return
                self.selected.add(n)
            await interaction.response.edit_message(content=f"🎫 Обрано: **{len(self.selected)}**", view=TicketPickerView(self.lottery_id, self.user_id, self.page, self.selected))
        return cb

    def page_callback(self, page):
        async def cb(interaction):
            await interaction.response.edit_message(content=f"🎫 Обрано: **{len(self.selected)}**", view=TicketPickerView(self.lottery_id, self.user_id, page, self.selected))
        return cb

    async def clear_callback(self, interaction):
        self.selected.clear()
        await interaction.response.edit_message(content="🎫 Обрано: **0**", view=TicketPickerView(self.lottery_id, self.user_id, self.page))

    async def confirm_callback(self, interaction):
        if not self.selected:
            await interaction.response.send_message("❌ Обери хоча б один номер.", ephemeral=True)
            return
        ok, request_id, reason = db.reserve_lottery_tickets(self.lottery_id, self.user_id, sorted(self.selected))
        if not ok:
            await interaction.response.edit_message(content=f"❌ {reason}", view=None)
            return
        row = get_lottery(self.lottery_id)
        total = len(self.selected) * int(row["ticket_price_cents"])
        await interaction.response.edit_message(
            content=(f"🎫 **Резерв створено**\n\nКвитків: **{len(self.selected)}**\n"
                     f"Сума: **{format_cents(total)}**\n\n"
                     "Після внесення коштів натисни кнопку нижче. Квитки залишаються зарезервованими, поки керівництво не підтвердить або не відхилить оплату."),
            view=LotteryPaymentView(request_id),
        )


class LotteryPaymentView(discord.ui.View):
    def __init__(self, request_id):
        super().__init__(timeout=None)
        self.request_id = request_id
        b = discord.ui.Button(label="Кошти внесено", emoji="💰", style=discord.ButtonStyle.success, custom_id=f"lottery:paid:{request_id}")
        b.callback = self.paid
        self.add_item(b)
        c = discord.ui.Button(label="Скасувати", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"lottery:cancel:{request_id}")
        c.callback = self.cancel
        self.add_item(c)

    async def paid(self, interaction):
        req = db.lottery_request(self.request_id)
        if not req or req["user_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Цей запит недоступний.", ephemeral=True)
            return
        if req["status"] != "reserved":
            await interaction.response.send_message("❌ Запит уже обробляється або завершений.", ephemeral=True)
            return
        db.mark_lottery_payment_pending(self.request_id)
        await interaction.response.edit_message(content="🟡 **Кошти внесено — очікується перевірка керівництвом.**\n\nКвитки залишаються зарезервованими.", view=None)

    async def cancel(self, interaction):
        req = db.lottery_request(self.request_id)
        if not req or req["user_id"] != interaction.user.id:
            await interaction.response.send_message("❌ Цей запит недоступний.", ephemeral=True)
            return
        if req["status"] not in {"reserved", "payment_pending"}:
            await interaction.response.send_message("❌ Запит уже завершений.", ephemeral=True)
            return
        db.reject_lottery_request(self.request_id, interaction.user.id, "Скасовано гравцем")
        await interaction.response.edit_message(content="❌ Резерв скасовано. Номери знову доступні.", view=None)


class LotteryAdminView(discord.ui.View):
    def __init__(self, request_id):
        super().__init__(timeout=None)
        self.request_id = request_id
        ok = discord.ui.Button(label="Підтвердити оплату", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"lottery:confirm:{request_id}")
        ok.callback = self.confirm
        no = discord.ui.Button(label="Відхилити оплату", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"lottery:reject:{request_id}")
        no.callback = self.reject
        self.add_item(ok)
        self.add_item(no)

    async def confirm(self, interaction):
        if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
            await interaction.response.send_message("❌ Доступ тільки для керівництва.", ephemeral=True)
            return
        req = db.lottery_request(self.request_id)
        if not req or req["status"] != "payment_pending":
            await interaction.response.send_message("❌ Цей запит уже оброблений.", ephemeral=True)
            return
        db.confirm_lottery_request(self.request_id, interaction.user.id)
        await interaction.response.edit_message(content=f"✅ Оплату підтверджено. Квитки: {req['numbers_json']}", view=None)
        await refresh_lottery_message(interaction.client, req["lottery_id"])

    async def reject(self, interaction):
        if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
            await interaction.response.send_message("❌ Доступ тільки для керівництва.", ephemeral=True)
            return
        req = db.lottery_request(self.request_id)
        if not req or req["status"] != "payment_pending":
            await interaction.response.send_message("❌ Цей запит уже оброблений.", ephemeral=True)
            return
        db.reject_lottery_request(self.request_id, interaction.user.id, "Оплату відхилено керівництвом")
        await interaction.response.edit_message(content="❌ Оплату відхилено. Зарезервовані номери звільнено.", view=None)
        await refresh_lottery_message(interaction.client, req["lottery_id"])


async def refresh_lottery_message(bot, lottery_id):
    row = get_lottery(lottery_id)
    if not row or not row["message_id"]:
        return
    channel = bot.get_channel(row["channel_id"])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(row["message_id"])
        reserved, confirmed = get_counts(lottery_id)
        await msg.edit(embed=lottery_embed(row, reserved, confirmed), view=LotteryPublicView(lottery_id) if row["status"] == "active" else None)
    except discord.DiscordException:
        pass


async def send_pending_request(interaction, request_id):
    req = db.lottery_request(request_id)
    if not req:
        return
    lottery = get_lottery(req["lottery_id"])
    channel = interaction.client.get_channel(LOTTERY_MANAGEMENT_CHANNEL_ID)
    if not channel:
        return
    member = interaction.guild.get_member(req["user_id"]) if interaction.guild else None
    name = member.mention if member else f"<@{req['user_id']}>"
    nums = [int(x) for x in json.loads(req["numbers_json"])]
    total = len(nums) * int(lottery["ticket_price_cents"])
    await channel.send(
        f"🎟️ **Очікує перевірки оплати**\n{lottery['name']}\n{ name }\n"
        f"Квитків: **{len(nums)}**\nНомери: **{', '.join(f'{n:02d}' for n in nums)}**\n"
        f"Сума: **{format_cents(total)}**",
        view=LotteryAdminView(request_id),
    )


class LotteryManagementView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        b = discord.ui.Button(label="Створити лотерею", emoji="🎟️", style=discord.ButtonStyle.success, custom_id="lottery:management:create")
        b.callback = self.create
        self.add_item(b)
        p = discord.ui.Button(label="Очікують оплати", emoji="🟡", style=discord.ButtonStyle.secondary, custom_id="lottery:management:pending")
        p.callback = self.pending
        self.add_item(p)
        d = discord.ui.Button(label="Завершити/розіграти", emoji="🎲", style=discord.ButtonStyle.primary, custom_id="lottery:management:draw")
        d.callback = self.draw_list
        self.add_item(d)

    async def create(self, interaction):
        if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
            await interaction.response.send_message("❌ Доступ тільки для керівництва.", ephemeral=True)
            return
        await interaction.response.send_modal(LotteryCreateStep1())

    async def pending(self, interaction):
        if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
            await interaction.response.send_message("❌ Доступ тільки для керівництва.", ephemeral=True)
            return
        rows = db.pending_lottery_requests(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("🟢 Немає оплат, які очікують перевірки.", ephemeral=True)
            return
        await interaction.response.send_message("🟡 Запити на підтвердження:", ephemeral=True)
        for req in rows[:10]:
            nums = [int(x) for x in json.loads(req["numbers_json"])]
            await interaction.followup.send(
                f"🎟️ **{req['lottery_name']}** • <@{req['user_id']}>\n"
                f"Номери: {', '.join(f'{n:02d}' for n in nums)}\n"
                f"Сума: {format_cents(len(nums)*req['ticket_price_cents'])}",
                view=LotteryAdminView(req["id"]),
                ephemeral=True,
            )

    async def draw_list(self, interaction):
        if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
            await interaction.response.send_message("❌ Доступ тільки для керівництва.", ephemeral=True)
            return
        rows = db.conn.execute("SELECT * FROM lotteries WHERE guild_id=? AND status IN ('active','finished') ORDER BY id DESC", (interaction.guild_id,)).fetchall()
        if not rows:
            await interaction.response.send_message("🟢 Немає активних лотерей.", ephemeral=True)
            return
        await interaction.response.send_message("🎲 Обери лотерею для завершення:", view=LotteryDrawListView(rows), ephemeral=True)


class LotteryDrawListView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=300)
        for row in rows[:25]:
            b = discord.ui.Button(label=row["name"][:70], style=discord.ButtonStyle.primary)
            b.callback = self.make(row["id"])
            self.add_item(b)
    def make(self, lottery_id):
        async def cb(interaction):
            if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
                await interaction.response.send_message("❌ Доступ тільки для керівництва.", ephemeral=True); return
            result = db.draw_lottery(lottery_id, interaction.user.id)
            if not result[0]:
                await interaction.response.send_message(f"❌ {result[1]}", ephemeral=True); return
            row = get_lottery(lottery_id)
            nums = result[2]
            text = ", ".join(f"#{n:02d}" for n in nums)
            await interaction.response.edit_message(content=f"🏆 **Розіграш проведено**\n\n{row['name']}\nПереможні квитки: **{text}**", view=None)
            await refresh_lottery_message(interaction.client, lottery_id)
            if row["prize_type"] == "cash":
                for n in nums:
                    ticket = db.conn.execute("SELECT user_id FROM lottery_tickets WHERE lottery_id=? AND number=?", (lottery_id,n)).fetchone()
                    if ticket:
                        db.create_lottery_payout(interaction.guild_id, ticket["user_id"], lottery_id, n, row["prize_cents"])
            await audit_log(interaction.guild, "🏆 Лотерею розіграно", f"**{row['name']}**\nПереможні квитки: **{text}**", discord.Color.green())
        return cb


def register_commands(bot: commands.Bot):
    @bot.tree.command(name="lottery", description="Керування лотереями Agosto")
    async def lottery(interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not management_payout_member(interaction.user):
            await interaction.response.send_message("❌ Команда доступна тільки керівництву.", ephemeral=True)
            return
        if LOTTERY_MANAGEMENT_CHANNEL_ID and interaction.channel_id != LOTTERY_MANAGEMENT_CHANNEL_ID:
            await interaction.response.send_message(
                f"🎟️ Керування лотереями знаходиться в <#{LOTTERY_MANAGEMENT_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("🎟️ **Лотереї Agosto**", view=LotteryManagementView(), ephemeral=True)



async def ensure_lottery_channels(bot):
    """Ensure the management panel exists in the dedicated management channel."""
    if not LOTTERY_MANAGEMENT_CHANNEL_ID:
        return
    channel = bot.get_channel(LOTTERY_MANAGEMENT_CHANNEL_ID)
    if channel is None:
        print("[LOTTERY] Management channel not found")
        return
    key = "lottery_management_panel_message_id"
    existing_id = db.get_setting(GUILD_ID, key)
    if existing_id:
        try:
            msg = await channel.fetch_message(int(existing_id))
            await msg.edit(content="🎟️ **Лотереї Agosto — керування**", view=LotteryManagementView())
            return
        except discord.DiscordException:
            pass
    msg = await channel.send(
        content=(
            "🎟️ **Лотереї Agosto — керування**\n"
            "Створення лотерей, перевірка оплат та проведення розіграшів."
        ),
        view=LotteryManagementView(),
    )
    db.set_setting(GUILD_ID, key, str(msg.id))


def restore_management_view(bot):
    try:
        bot.add_view(LotteryManagementView())
    except Exception:
        pass


def restore_active_views(bot):
    for row in db.active_lotteries(GUILD_ID):
        if row["message_id"]:
            try:
                bot.add_view(LotteryPublicView(row["id"]), message_id=row["message_id"])
            except Exception:
                pass


async def maybe_finish_expired(bot, now):
    now_utc = now.astimezone(timezone.utc)
    rows = db.conn.execute("SELECT * FROM lotteries WHERE guild_id=? AND status='active'", (GUILD_ID,)).fetchall()
    for row in rows:
        try:
            ends = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if now_utc < ends:
            continue
        db.conn.execute("UPDATE lotteries SET status='finished' WHERE id=? AND status='active'", (row["id"],))
        db.conn.commit()
        await refresh_lottery_message(bot, row["id"])
        channel = bot.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.send(f"🔒 **Лотерея «{row['name']}» завершила продаж квитків.**\n🎲 Керівництво може провести розіграш через `/lottery`.")
            except discord.DiscordException:
                pass
