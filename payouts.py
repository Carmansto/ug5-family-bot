import os
import re
import asyncio
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from config import *
from database import db
from utils import *

from contracts import audit_log, refresh_completed_message


async def payout_user_label(
  guild: discord.Guild,
  user_id: int,
) -> str:
  member = guild.get_member(user_id)
  return member.display_name if member else f"ID {user_id}"


def payout_period_short(start_at: Optional[str], end_at: Optional[str]) -> str:
  start_day = local_date_from_iso(start_at) if start_at else None
  end_day = local_date_from_iso(end_at) if end_at else None

  if start_day and end_day:
    return f"{start_day.strftime('%d.%m.%Y')} — {end_day.strftime('%d.%m.%Y')}"

  return "період"


def payout_thread_setting_key(user_id: int) -> str:
  return f"payout_thread_{user_id}"


def payout_thread_name(member: discord.Member) -> str:
  raw = member.display_name.strip() or str(member.id)
  cleaned = " ".join(raw.split())
  return f"виплати-{cleaned}"[:100]


async def resolve_payout_thread(
  guild: discord.Guild,
  member: discord.Member,
) -> Optional[discord.Thread]:
  """
  Find or create one private payout thread for this member.
  Thread id is persisted in bot_settings, so the same personal
  payout history is reused on every future payout.
  """
  if not PAYOUT_THREADS_CHANNEL_ID:
    return None

  parent = guild.get_channel(PAYOUT_THREADS_CHANNEL_ID)

  if parent is None:
    try:
      parent = await guild.fetch_channel(PAYOUT_THREADS_CHANNEL_ID)
    except discord.DiscordException:
      return None

  if not isinstance(parent, discord.TextChannel):
    return None

  key = payout_thread_setting_key(member.id)
  saved_thread_id = db.get_setting(guild.id, key)
  thread = None

  if saved_thread_id and saved_thread_id.isdigit():
    thread_id = int(saved_thread_id)
    thread = guild.get_thread(thread_id)

    if thread is None:
      try:
        async for archived in parent.archived_threads(
          private=True,
          joined=True,
          limit=None,
        ):
          if archived.id == thread_id:
            thread = archived
            break
      except discord.DiscordException:
        thread = None

    if thread is not None:
      try:
        if thread.archived:
          await thread.edit(
            archived=False,
            reason="Нова виплата Agosto",
          )
        await thread.add_user(member)
        return thread
      except discord.DiscordException:
        thread = None

  try:
    thread = await parent.create_thread(
      name=payout_thread_name(member),
      type=discord.ChannelType.private_thread,
      auto_archive_duration=10080,
      invitable=False,
      reason="Приватна історія виплат Agosto",
    )

    await thread.add_user(member)
    db.set_setting(guild.id, key, str(thread.id))
    return thread

  except (discord.Forbidden, discord.HTTPException):
    return None


async def send_payout_notification(
  guild: discord.Guild,
  user_id: int,
  rows,
  paid_by: int,
) -> bool:
  """
  Post a payout receipt in the recipient's private payout thread.
  Ordinary members cannot see other members' private threads.
  Failure to publish never rolls back the payout.
  """
  if not rows:
    return False

  member = await fetch_member_safe(guild, user_id)
  if member is None:
    return False

  thread = await resolve_payout_thread(guild, member)
  if thread is None:
    return False

  contract_rows = [
    row for row in rows
    if row["source_type"] == "contract"
  ]

  bonus_rows = [
    row for row in rows
    if row["source_type"] == "bonus"
  ]

  lottery_rows = [
    row for row in rows
    if row["source_type"] == "lottery"
  ]

  total = sum(int(row["amount_cents"]) for row in rows)
  contract_total = sum(int(row["amount_cents"]) for row in contract_rows)
  bonus_total = sum(int(row["amount_cents"]) for row in bonus_rows)
  lottery_total = sum(int(row["amount_cents"]) for row in lottery_rows)

  paid_local = datetime.now(timezone.utc).astimezone(LOCAL_TZ)

  lines = [
    f"<@{user_id}>",
    "",
    "💸 НОВА ВИПЛАТА",
    "",
    f"💰 Загальна сума: {format_cents(total)}",
  ]

  if contract_rows:
    lines.extend([
      "",
      f"📋 КОНТРАКТИ • {format_cents(contract_total)}",
    ])

    for row in contract_rows:
      lines.append(
        f"#{row['contract_id']} • {row['contract_name']} — "
        f"{format_cents(row['amount_cents'])}"
      )

  if bonus_rows:
    lines.extend([
      "",
      f"🏆 ПРЕМІЯ • {format_cents(bonus_total)}",
    ])

    for row in bonus_rows:
      if row["period_start"] and row["period_end"]:
        period_text = payout_period_short(
          row["period_start"],
          row["period_end"],
        )

        rank_text = (
          f" • {row['rank']} місце"
          if row["rank"]
          else ""
        )

        lines.append(
          f"За {period_text}{rank_text}"
        )

      else:
        note_text = f" • {row['note']}" if row["note"] else ""

        lines.append(
          f"Ручна премія{note_text}"
        )

  if lottery_rows:
    lines.extend([
      "",
      f"🎟️ ВИГРАШ У ЛОТЕРЕЇ • {format_cents(lottery_total)}",
    ])

    for row in lottery_rows:
      lines.append(
        f"Квиток #{int(row['ticket_number']):02d} • "
        f"{format_cents(row['amount_cents'])}"
      )

  lines.extend([
    "",
    f"💳 Виплатив - <@{paid_by}>",
    "",
    f"🕒 Виплачено - {paid_local.strftime('%d.%m.%Y %H:%M')}",
  ])

  try:
    if thread.archived:
      await thread.edit(
        archived=False,
        reason="Нова виплата Agosto",
      )

    await thread.send(
      "\n".join(lines),
      allowed_mentions=discord.AllowedMentions(
        users=True,
        roles=False,
        everyone=False,
      ),
    )

    return True

  except (discord.Forbidden, discord.HTTPException):
    return False


class PayoutUserSelect(discord.ui.Select):
  def __init__(
    self,
    guild_id: int,
    rows,
    labels: dict[int, str],
    category: str,
  ):
    self.guild_id = guild_id
    self.category = category

    options = [
      discord.SelectOption(
        label=labels.get(row["user_id"], f"ID {row['user_id']}")[:100],
        value=str(row["user_id"]),
        description=(
          f"До виплати: {format_cents(row['total_cents'])} • "
          f"{row['accrual_count']} нарахувань"
        )[:100],
      )
      for row in rows[:25]
    ]

    if not options:
      options = [
        discord.SelectOption(
          label=(
            "Немає керівного складу до виплати"
            if category == "management"
            else "Немає основного складу до виплати"
          ),
          value="none",
        )
      ]

    super().__init__(
      placeholder=(
        "🛡 Обрати з керівного складу"
        if category == "management"
        else "👥 Обрати з основного складу"
      ),
      min_values=1,
      max_values=1,
      options=options,
      disabled=(options[0].value == "none"),
      row=(1 if category == "management" else 0),
    )

  async def callback(self, interaction: discord.Interaction):
    if self.values[0] == "none":
      return

    await show_payout_user(
      interaction,
      int(self.values[0]),
    )


class PayoutListView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    participant_rows,
    management_rows,
    labels: dict[int, str],
    can_pay_management: bool,
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.pay_management.disabled = not can_pay_management

    self.add_item(
      PayoutUserSelect(
        guild_id,
        participant_rows,
        labels,
        "participants",
      )
    )

    self.add_item(
      PayoutUserSelect(
        guild_id,
        management_rows,
        labels,
        "management",
      )
    )

  @discord.ui.button(
    label="Виплатити основному складу",
    emoji="💵",
    style=discord.ButtonStyle.success,
    row=2,
  )
  async def pay_participants(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "❌ Доступно тільки керівництву.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      return

    participant_rows, _ = await split_payout_summary(guild)

    if not participant_rows:
      await interaction.response.send_message(
        "✅ Основному складу зараз немає що виплачувати.",
        ephemeral=True,
      )
      return

    total = sum(row["total_cents"] for row in participant_rows)
    count = sum(row["accrual_count"] for row in participant_rows)

    embed = discord.Embed(
      title="⚠️ Виплатити основному складу?",
      description=(
        f"Людей: **{len(participant_rows)}**\n"
        f"Нарахувань: **{count}**\n"
        f"Сума: **{format_cents(total)}**"
      ),
      color=discord.Color.orange(),
    )

    await interaction.response.send_message(
      embed=embed,
      view=PayoutCategoryConfirmView(
        self.guild_id,
        "participants",
      ),
      ephemeral=True,
    )

  @discord.ui.button(
    label="Виплатити керівному складу",
    emoji="🛡",
    style=discord.ButtonStyle.danger,
    row=2,
  )
  async def pay_management(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not can_close_management_payout(interaction.user):
      await interaction.response.send_message(
        "🔒 Виплати керівному складу може LEADER_ROLE_ID або власник сервера.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      return

    _, management_rows = await split_payout_summary(guild)

    if not management_rows:
      await interaction.response.send_message(
        "✅ Керівному складу зараз немає що виплачувати.",
        ephemeral=True,
      )
      return

    total = sum(row["total_cents"] for row in management_rows)
    count = sum(row["accrual_count"] for row in management_rows)

    embed = discord.Embed(
      title="⚠️ Виплатити керівному складу?",
      description=(
        f"Людей: **{len(management_rows)}**\n"
        f"Нарахувань: **{count}**\n"
        f"Сума: **{format_cents(total)}**"
      ),
      color=discord.Color.red(),
    )

    await interaction.response.send_message(
      embed=embed,
      view=PayoutCategoryConfirmView(
        self.guild_id,
        "management",
      ),
      ephemeral=True,
    )


class PayoutPayView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    user_id: int,
    can_pay: bool = True,
  ):
    super().__init__(timeout=180)
    self.guild_id = guild_id
    self.user_id = user_id
    self.pay.disabled = not can_pay

    if not can_pay:
      self.pay.label = "Тільки лідер / owner"
      self.pay.emoji = "🔒"
      self.pay.style = discord.ButtonStyle.secondary

  @discord.ui.button(
    label="Виплатити",
    emoji="💵",
    style=discord.ButtonStyle.success,
  )
  async def pay(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "❌ Закривати виплати може тільки керівництво.",
        ephemeral=True,
      )
      return

    guild = interaction.guild

    if guild is None:
      await interaction.response.send_message(
        "❌ Сервер недоступний.",
        ephemeral=True,
      )
      return

    if (
      await payout_is_management(guild, self.user_id)
      and not can_close_management_payout(interaction.user)
    ):
      await interaction.response.send_message(
        "🔒 Виплату керівному складу закриває LEADER_ROLE_ID або власник сервера.",
        ephemeral=True,
      )
      return

    rows = db.pending_payout_items_for_user(
      self.guild_id,
      self.user_id,
    )

    if not rows:
      await interaction.response.edit_message(
        content="✅ У цієї людини вже немає суми до виплати.",
        embed=None,
        view=None,
      )
      return

    total = sum(row["amount_cents"] for row in rows)

    await interaction.response.edit_message(
      content="⏳ Закриваю виплату...",
      embed=None,
      view=None,
    )

    settled = db.settle_payouts_for_user(
      self.guild_id,
      self.user_id,
      interaction.user.id,
    )

    message_ids = sorted({
      row["message_id"]
      for row in settled
      if row["source_type"] == "contract" and row["message_id"]
    })

    for message_id in message_ids:
      await refresh_completed_message(message_id)

    notification_sent = await send_payout_notification(
      interaction.guild,
      self.user_id,
      settled,
      interaction.user.id,
    )

    await audit_log(
      interaction.guild,
      "💵 Виплату учаснику закрито",
      (
        f"Учасник: <@{self.user_id}>\n"
        f"Сума: **{format_cents(total)}**\n"
        f"Нарахувань: **{len(settled)}**\n"
        f"Сповіщення: **{'✅ опубліковано' if notification_sent else '⚠️ не вдалося'}**\n"
        f"Виплатив/ла: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    await interaction.edit_original_response(
      content=(
        f"✅ <@{self.user_id}> виплачено **{format_cents(total)}**.\n"
        "Баланс до виплати закрито.\n"
        f"Гілка виплат: **{'✅ опубліковано' if notification_sent else '⚠️ не вдалося'}**"
      ),
      embed=None,
      view=None,
    )

  @discord.ui.button(
    label="Назад",
    emoji="↩️",
    style=discord.ButtonStyle.secondary,
  )
  async def back(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await send_payouts_list(
      interaction,
      edit=True,
    )


class PayoutCategoryConfirmView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    category: str,
  ):
    super().__init__(timeout=120)
    self.guild_id = guild_id
    self.category = category

  @discord.ui.button(
    label="Підтвердити",
    emoji="✅",
    style=discord.ButtonStyle.danger,
  )
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(
        content="❌ Доступно тільки керівництву.",
        embed=None,
        view=None,
      )
      return

    if self.category == "management" and not can_close_management_payout(interaction.user):
      await interaction.response.edit_message(
        content="🔒 Виплати керівному складу може LEADER_ROLE_ID або власник сервера.",
        embed=None,
        view=None,
      )
      return

    guild = interaction.guild
    if guild is None:
      return

    participant_rows, management_rows = await split_payout_summary(guild)

    selected_rows = (
      management_rows
      if self.category == "management"
      else participant_rows
    )

    user_ids = [row["user_id"] for row in selected_rows]

    if not user_ids:
      await interaction.response.edit_message(
        content="✅ Доступних сум до виплати вже немає.",
        embed=None,
        view=None,
      )
      return

    await interaction.response.edit_message(
      content="⏳ Закриваю виплати...",
      embed=None,
      view=None,
    )

    rows = db.settle_payouts_for_users(
      self.guild_id,
      user_ids,
      interaction.user.id,
    )

    total = sum(row["amount_cents"] for row in rows)
    users = {row["user_id"] for row in rows}

    message_ids = sorted({
      row["message_id"]
      for row in rows
      if row["source_type"] == "contract" and row["message_id"]
    })

    for message_id in message_ids:
      await refresh_completed_message(message_id)

    rows_by_user = defaultdict(list)

    for row in rows:
      rows_by_user[int(row["user_id"])].append(row)

    notification_sent_count = 0
    notification_failed_count = 0

    for user_id, user_rows in rows_by_user.items():
      if await send_payout_notification(
        guild,
        user_id,
        user_rows,
        interaction.user.id,
      ):
        notification_sent_count += 1
      else:
        notification_failed_count += 1

    category_label = (
      "керівному складу"
      if self.category == "management"
      else "основному складу"
    )

    await audit_log(
      interaction.guild,
      "💸 Масову виплату закрито",
      (
        f"Категорія: **{category_label}**\n"
        f"Людей: **{len(users)}**\n"
        f"Нарахувань: **{len(rows)}**\n"
        f"Сума: **{format_cents(total)}**\n"
        f"Гілки виплат: **{notification_sent_count} опубліковано / {notification_failed_count} не вдалося**\n"
        f"Виплатив/ла: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    await interaction.edit_original_response(
      content=(
        f"✅ Виплати **{category_label}** закрито.\n"
        f"Людей: **{len(users)}**\n"
        f"Сума: **{format_cents(total)}**\n"
        f"Гілки виплат: **{notification_sent_count} опубліковано / {notification_failed_count} не вдалося**"
      ),
      embed=None,
      view=None,
    )

  @discord.ui.button(
    label="Скасувати",
    emoji="↩️",
    style=discord.ButtonStyle.secondary,
  )
  async def cancel(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content="Виплату скасовано.",
      embed=None,
      view=None,
    )


async def split_payout_summary(
  guild: discord.Guild,
):
  summary = db.pending_payout_summary(guild.id)

  participants = []
  management = []

  for row in summary:
    if await payout_is_management(guild, row["user_id"]):
      management.append(row)
    else:
      participants.append(row)

  return participants, management


async def show_payout_user(
  interaction: discord.Interaction,
  user_id: int,
):
  guild = interaction.guild

  if guild is None:
    return

  rows = db.pending_payout_items_for_user(
    guild.id,
    user_id,
  )

  total = sum(row["amount_cents"] for row in rows)
  label = await payout_user_label(guild, user_id)

  is_management = await payout_is_management(guild, user_id)

  can_pay_selected = (
    isinstance(interaction.user, discord.Member)
    and (
      not is_management
      or can_close_management_payout(interaction.user)
    )
  )

  category_text = (
    "🛡 **КЕРІВНИЙ СКЛАД** • "
    "виплату закриває LEADER_ROLE_ID або owner сервера"
    if is_management
    else "👥 **ОСНОВНИЙ СКЛАД**"
  )

  embed = discord.Embed(
    title="💰 До виплати",
    description=(
      f"{category_text}\n"
      f"**{label}** • <@{user_id}>\n"
      f"Загальна сума: **{format_cents(total)}**\n"
      f"Нарахувань: **{len(rows)}**"
    ),
    color=discord.Color.gold(),
  )

  if rows:
    contract_rows = [
      row for row in rows
      if row["source_type"] == "contract"
    ]

    bonus_rows = [
      row for row in rows
      if row["source_type"] == "bonus"
    ]

    lottery_rows = [
      row for row in rows
      if row["source_type"] == "lottery"
    ]

    if contract_rows:
      contract_lines = [
        (
          f"#{row['contract_id']} • {row['contract_name']} — "
          f"**{format_cents(row['amount_cents'])}**"
        )
        for row in contract_rows[:12]
      ]

      if len(contract_rows) > 12:
        contract_lines.append(
          f"…і ще {len(contract_rows) - 12}"
        )

      embed.add_field(
        name="📋 КОНТРАКТИ",
        value="\n".join(contract_lines),
        inline=False,
      )

    if bonus_rows:
      bonus_lines = []

      for row in bonus_rows[:12]:
        if row["period_start"] and row["period_end"]:
          start_day = local_date_from_iso(row["period_start"])
          end_day = local_date_from_iso(row["period_end"])

          period_text = (
            f"{start_day.strftime('%d.%m')}–{end_day.strftime('%d.%m')}"
            if start_day and end_day
            else "період"
          )

          rank_text = (
            f" • #{row['rank']}"
            if row["rank"]
            else ""
          )

          bonus_lines.append(
            f"🏆 Премія за період {period_text}{rank_text} — "
            f"**{format_cents(row['amount_cents'])}**"
          )

        else:
          note_text = (
            f" • {row['note']}"
            if row["note"]
            else ""
          )

          bonus_lines.append(
            f"🏆 Ручна премія{note_text} — "
            f"**{format_cents(row['amount_cents'])}**"
          )

      if len(bonus_rows) > 12:
        bonus_lines.append(
          f"…і ще {len(bonus_rows) - 12}"
        )

      embed.add_field(
        name="🏆 ПРЕМІЇ",
        value="\n".join(bonus_lines),
        inline=False,
      )

    # НОВЕ: виграші лотереї
    if lottery_rows:
      lottery_lines = [
        (
          f"🎟️ Квиток #{int(row['ticket_number']):02d} — "
          f"**{format_cents(row['amount_cents'])}**"
        )
        for row in lottery_rows[:12]
      ]

      if len(lottery_rows) > 12:
        lottery_lines.append(
          f"…і ще {len(lottery_rows) - 12}"
        )

      lottery_total = sum(
        int(row["amount_cents"])
        for row in lottery_rows
      )

      embed.add_field(
        name=(
          f"🎟️ ВИГРАШ У ЛОТЕРЕЇ • "
          f"{format_cents(lottery_total)}"
        ),
        value="\n".join(lottery_lines),
        inline=False,
      )

  await interaction.response.edit_message(
    content=None,
    embed=embed,
    view=PayoutPayView(
      guild.id,
      user_id,
      can_pay=can_pay_selected,
    ),
  )


async def send_payouts_list(
  interaction: discord.Interaction,
  edit: bool = False,
):
  guild = interaction.guild

  if guild is None:
    return

  participant_rows, management_rows = await split_payout_summary(guild)
  all_rows = participant_rows + management_rows

  if not all_rows:
    embed = discord.Embed(
      title="💰 Виплати",
      description="✅ Зараз немає нарахованих сум до виплати.",
      color=discord.Color.green(),
    )

    if edit:
      await interaction.response.edit_message(
        content=None,
        embed=embed,
        view=None,
      )
    else:
      await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
      )

    return

  labels = {}

  for row in all_rows[:50]:
    labels[row["user_id"]] = await payout_user_label(
      guild,
      row["user_id"],
    )

  participants_total = sum(
    row["total_cents"]
    for row in participant_rows
  )

  management_total = sum(
    row["total_cents"]
    for row in management_rows
  )

  participant_lines = [
    (
      f"<@{row['user_id']}> — **{format_cents(row['total_cents'])}** "
      f"• {row['accrual_count']} нарахувань"
    )
    for row in participant_rows[:20]
  ] or ["—"]

  management_lines = [
    (
      f"<@{row['user_id']}> — **{format_cents(row['total_cents'])}** "
      f"• {row['accrual_count']} нарахувань"
    )
    for row in management_rows[:20]
  ] or ["—"]

  if len(participant_rows) > 20:
    participant_lines.append(
      f"…і ще {len(participant_rows) - 20}"
    )

  if len(management_rows) > 20:
    management_lines.append(
      f"…і ще {len(management_rows) - 20}"
    )

  embed = discord.Embed(
    title="💰 ВИПЛАТИ",
    description=(
      f"Всього до виплати: "
      f"**{format_cents(participants_total + management_total)}**"
    ),
    color=discord.Color.gold(),
  )

  embed.add_field(
    name=(
      f"👥 ОСНОВНИЙ СКЛАД • "
      f"{len(participant_rows)} • "
      f"{format_cents(participants_total)}"
    ),
    value="\n".join(participant_lines),
    inline=False,
  )

  embed.add_field(
    name=(
      f"🛡 КЕРІВНИЙ СКЛАД • "
      f"{len(management_rows)} • "
      f"{format_cents(management_total)}"
    ),
    value="\n".join(management_lines),
    inline=False,
  )

  embed.set_footer(
    text="Виплати керівному складу: LEADER_ROLE_ID або owner сервера."
  )

  can_pay_management = (
    isinstance(interaction.user, discord.Member)
    and can_close_management_payout(interaction.user)
  )

  view = PayoutListView(
    guild.id,
    participant_rows,
    management_rows,
    labels,
    can_pay_management,
  )

  if edit:
    await interaction.response.edit_message(
      content=None,
      embed=embed,
      view=view,
    )
  else:
    await interaction.response.send_message(
      embed=embed,
      view=view,
      ephemeral=True,
    )


def register_commands(bot: commands.Bot):
  @bot.tree.command(
    name="payouts",
    description="Показати накопичені суми до виплати учасникам",
  )
  async def payouts(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "❌ Команда доступна тільки керівництву.",
        ephemeral=True,
      )
      return

    await send_payouts_list(interaction)
