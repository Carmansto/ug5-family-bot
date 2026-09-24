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
    return f"{start_day.strftime('%d.%m.%Y')} \u2014 {end_day.strftime('%d.%m.%Y')}"

  return "\u043f\u0435\u0440\u0456\u043e\u0434"


def payout_thread_setting_key(user_id: int) -> str:
  return f"payout_thread_{user_id}"


def payout_thread_name(member: discord.Member) -> str:
  raw = member.display_name.strip() or str(member.id)
  cleaned = " ".join(raw.split())
  return f"\u0432\u0438\u043f\u043b\u0430\u0442\u0438-{cleaned}"[:100]


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
            reason="\u041d\u043e\u0432\u0430 \u0432\u0438\u043f\u043b\u0430\u0442\u0430 Agosto",
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
      reason="\u041f\u0440\u0438\u0432\u0430\u0442\u043d\u0430 \u0456\u0441\u0442\u043e\u0440\u0456\u044f \u0432\u0438\u043f\u043b\u0430\u0442 Agosto",
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

  paid_local = datetime.now(timezone.utc).astimezone(LOCAL_TZ)

  lines = [
    f"<@{user_id}>",
    "",
    "\U0001f4b8 \u041d\u041e\u0412\u0410 \u0412\u0418\u041f\u041b\u0410\u0422\u0410",
    "",
    f"\U0001f4b0 \u0417\u0430\u0433\u0430\u043b\u044c\u043d\u0430 \u0441\u0443\u043c\u0430: {format_cents(total)}",
  ]

  if contract_rows:
    lines.extend([
      "",
      f"\U0001f4cb \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0418 \u2022 {format_cents(contract_total)}",
    ])

    for row in contract_rows:
      lines.append(
        f"#{row['contract_id']} \u2022 {row['contract_name']} \u2014 "
        f"{format_cents(row['amount_cents'])}"
      )

  if bonus_rows:
    lines.extend([
      "",
      f"\U0001f3c6 \u041f\u0420\u0415\u041c\u0406\u042f \u2022 {format_cents(bonus_total)}",
    ])

    for row in bonus_rows:
      if row["period_start"] and row["period_end"]:
        period_text = payout_period_short(
          row["period_start"],
          row["period_end"],
        )
        rank_text = (
          f" \u2022 {row['rank']} \u043c\u0456\u0441\u0446\u0435"
          if row["rank"]
          else ""
        )
        lines.append(
          f"\u0417\u0430 {period_text}{rank_text}"
        )
      else:
        note_text = f" \u2022 {row['note']}" if row["note"] else ""
        lines.append(
          f"\u0420\u0443\u0447\u043d\u0430 \u043f\u0440\u0435\u043c\u0456\u044f{note_text}"
        )

  if lottery_rows:
    lines.extend([
      "",
      f"🎟️ ВИГРАШІ ЛОТЕРЕЇ • {format_cents(sum(int(r['amount_cents']) for r in lottery_rows))}",
    ])
    for row in lottery_rows:
      lines.append(f"Квиток #{int(row['ticket_number']):02d} • {format_cents(row['amount_cents'])}")

  lines.extend([
    "",
    f"\U0001f4b3 \u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0432 - <@{paid_by}>",
    "",
    f"\U0001f552 \u0412\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e - {paid_local.strftime('%d.%m.%Y %H:%M')}",
  ])

  try:
    if thread.archived:
      await thread.edit(
        archived=False,
        reason="\u041d\u043e\u0432\u0430 \u0432\u0438\u043f\u043b\u0430\u0442\u0430 Agosto",
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
          f"\u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438: {format_cents(row['total_cents'])} \u2022 "
          f"{row['accrual_count']} \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c"
        )[:100],
      )
      for row in rows[:25]
    ]

    if not options:
      options = [
        discord.SelectOption(
          label=(
            "\u041d\u0435\u043c\u0430\u0454 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438"
            if category == "management"
            else "\u041d\u0435\u043c\u0430\u0454 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438"
          ),
          value="none",
        )
      ]

    super().__init__(
      placeholder=(
        "\U0001f6e1 \u041e\u0431\u0440\u0430\u0442\u0438 \u0437 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443"
        if category == "management"
        else "\U0001f465 \u041e\u0431\u0440\u0430\u0442\u0438 \u0437 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443"
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
    label="\u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0442\u0438 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443",
    emoji="\U0001f4b5",
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
        "\u274c \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u043e \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      return

    participant_rows, _ = await split_payout_summary(guild)

    if not participant_rows:
      await interaction.response.send_message(
        "\u2705 \u041e\u0441\u043d\u043e\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443 \u0437\u0430\u0440\u0430\u0437 \u043d\u0435\u043c\u0430\u0454 \u0449\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0443\u0432\u0430\u0442\u0438.",
        ephemeral=True,
      )
      return

    total = sum(row["total_cents"] for row in participant_rows)
    count = sum(row["accrual_count"] for row in participant_rows)

    embed = discord.Embed(
      title="\u26a0\ufe0f \u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0442\u0438 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443?",
      description=(
        f"\u041b\u044e\u0434\u0435\u0439: **{len(participant_rows)}**\n"
        f"\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c: **{count}**\n"
        f"\u0421\u0443\u043c\u0430: **{format_cents(total)}**"
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
    label="\u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0442\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443",
    emoji="\U0001f6e1",
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
        "\U0001f512 \u0412\u0438\u043f\u043b\u0430\u0442\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443 \u043c\u043e\u0436\u0435 LEADER_ROLE_ID \u0430\u0431\u043e \u0432\u043b\u0430\u0441\u043d\u0438\u043a \u0441\u0435\u0440\u0432\u0435\u0440\u0430.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      return

    _, management_rows = await split_payout_summary(guild)

    if not management_rows:
      await interaction.response.send_message(
        "\u2705 \u041a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443 \u0437\u0430\u0440\u0430\u0437 \u043d\u0435\u043c\u0430\u0454 \u0449\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0443\u0432\u0430\u0442\u0438.",
        ephemeral=True,
      )
      return

    total = sum(row["total_cents"] for row in management_rows)
    count = sum(row["accrual_count"] for row in management_rows)

    embed = discord.Embed(
      title="\u26a0\ufe0f \u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0442\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443?",
      description=(
        f"\u041b\u044e\u0434\u0435\u0439: **{len(management_rows)}**\n"
        f"\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c: **{count}**\n"
        f"\u0421\u0443\u043c\u0430: **{format_cents(total)}**"
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
      self.pay.label = "\u0422\u0456\u043b\u044c\u043a\u0438 \u043b\u0456\u0434\u0435\u0440 / owner"
      self.pay.emoji = "\U0001f512"
      self.pay.style = discord.ButtonStyle.secondary

  @discord.ui.button(
    label="\u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0442\u0438",
    emoji="\U0001f4b5",
    style=discord.ButtonStyle.success,
  )
  async def pay(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0417\u0430\u043a\u0440\u0438\u0432\u0430\u0442\u0438 \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u043c\u043e\u0436\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      await interaction.response.send_message(
        "\u274c \u0421\u0435\u0440\u0432\u0435\u0440 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    if (
      await payout_is_management(guild, self.user_id)
      and not can_close_management_payout(interaction.user)
    ):
      await interaction.response.send_message(
        "\U0001f512 \u0412\u0438\u043f\u043b\u0430\u0442\u0443 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443 \u0437\u0430\u043a\u0440\u0438\u0432\u0430\u0454 LEADER_ROLE_ID \u0430\u0431\u043e \u0432\u043b\u0430\u0441\u043d\u0438\u043a \u0441\u0435\u0440\u0432\u0435\u0440\u0430.",
        ephemeral=True,
      )
      return

    rows = db.pending_payout_items_for_user(
      self.guild_id,
      self.user_id,
    )

    if not rows:
      await interaction.response.edit_message(
        content="\u2705 \u0423 \u0446\u0456\u0454\u0457 \u043b\u044e\u0434\u0438\u043d\u0438 \u0432\u0436\u0435 \u043d\u0435\u043c\u0430\u0454 \u0441\u0443\u043c\u0438 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438.",
        embed=None,
        view=None,
      )
      return

    total = sum(row["amount_cents"] for row in rows)

    await interaction.response.edit_message(
      content="\u23f3 \u0417\u0430\u043a\u0440\u0438\u0432\u0430\u044e \u0432\u0438\u043f\u043b\u0430\u0442\u0443...",
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
      "\U0001f4b5 \u0412\u0438\u043f\u043b\u0430\u0442\u0443 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0443 \u0437\u0430\u043a\u0440\u0438\u0442\u043e",
      (
        f"\u0423\u0447\u0430\u0441\u043d\u0438\u043a: <@{self.user_id}>\n"
        f"\u0421\u0443\u043c\u0430: **{format_cents(total)}**\n"
        f"\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c: **{len(settled)}**\n"
        f"\u0421\u043f\u043e\u0432\u0456\u0449\u0435\u043d\u043d\u044f: **{'\u2705 \u043e\u043f\u0443\u0431\u043b\u0456\u043a\u043e\u0432\u0430\u043d\u043e' if notification_sent else '\u26a0\ufe0f \u043d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f'}**\n"
        f"\u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    await interaction.edit_original_response(
      content=(
        f"\u2705 <@{self.user_id}> \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e **{format_cents(total)}**.\n"
        "\u0411\u0430\u043b\u0430\u043d\u0441 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u0437\u0430\u043a\u0440\u0438\u0442\u043e.\n"
        f"\u0413\u0456\u043b\u043a\u0430 \u0432\u0438\u043f\u043b\u0430\u0442: **{'\u2705 \u043e\u043f\u0443\u0431\u043b\u0456\u043a\u043e\u0432\u0430\u043d\u043e' if notification_sent else '\u26a0\ufe0f \u043d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f'}**"
      ),
      embed=None,
      view=None,
    )

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    emoji="\u21a9\ufe0f",
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
    label="\u041f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0438",
    emoji="\u2705",
    style=discord.ButtonStyle.danger,
  )
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(
        content="\u274c \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u043e \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        embed=None,
        view=None,
      )
      return

    if self.category == "management" and not can_close_management_payout(interaction.user):
      await interaction.response.edit_message(
        content="\U0001f512 \u0412\u0438\u043f\u043b\u0430\u0442\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443 \u043c\u043e\u0436\u0435 LEADER_ROLE_ID \u0430\u0431\u043e \u0432\u043b\u0430\u0441\u043d\u0438\u043a \u0441\u0435\u0440\u0432\u0435\u0440\u0430.",
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
        content="\u2705 \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0445 \u0441\u0443\u043c \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u0432\u0436\u0435 \u043d\u0435\u043c\u0430\u0454.",
        embed=None,
        view=None,
      )
      return

    await interaction.response.edit_message(
      content="\u23f3 \u0417\u0430\u043a\u0440\u0438\u0432\u0430\u044e \u0432\u0438\u043f\u043b\u0430\u0442\u0438...",
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
      "\u043a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443"
      if self.category == "management"
      else "\u043e\u0441\u043d\u043e\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443"
    )

    await audit_log(
      interaction.guild,
      "\U0001f4b8 \u041c\u0430\u0441\u043e\u0432\u0443 \u0432\u0438\u043f\u043b\u0430\u0442\u0443 \u0437\u0430\u043a\u0440\u0438\u0442\u043e",
      (
        f"\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0456\u044f: **{category_label}**\n"
        f"\u041b\u044e\u0434\u0435\u0439: **{len(users)}**\n"
        f"\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c: **{len(rows)}**\n"
        f"\u0421\u0443\u043c\u0430: **{format_cents(total)}**\n"
        f"\u0413\u0456\u043b\u043a\u0438 \u0432\u0438\u043f\u043b\u0430\u0442: **{notification_sent_count} \u043e\u043f\u0443\u0431\u043b\u0456\u043a\u043e\u0432\u0430\u043d\u043e / {notification_failed_count} \u043d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f**\n"
        f"\u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    await interaction.edit_original_response(
      content=(
        f"\u2705 \u0412\u0438\u043f\u043b\u0430\u0442\u0438 **{category_label}** \u0437\u0430\u043a\u0440\u0438\u0442\u043e.\n"
        f"\u041b\u044e\u0434\u0435\u0439: **{len(users)}**\n"
        f"\u0421\u0443\u043c\u0430: **{format_cents(total)}**\n"
        f"\u0413\u0456\u043b\u043a\u0438 \u0432\u0438\u043f\u043b\u0430\u0442: **{notification_sent_count} \u043e\u043f\u0443\u0431\u043b\u0456\u043a\u043e\u0432\u0430\u043d\u043e / {notification_failed_count} \u043d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f**"
      ),
      embed=None,
      view=None,
    )

  @discord.ui.button(
    label="\u0421\u043a\u0430\u0441\u0443\u0432\u0430\u0442\u0438",
    emoji="\u21a9\ufe0f",
    style=discord.ButtonStyle.secondary,
  )
  async def cancel(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content="\u0412\u0438\u043f\u043b\u0430\u0442\u0443 \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e.",
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
    "\U0001f6e1 **\u041a\u0415\u0420\u0406\u0412\u041d\u0418\u0419 \u0421\u041a\u041b\u0410\u0414** \u2022 "
    "\u0432\u0438\u043f\u043b\u0430\u0442\u0443 \u0437\u0430\u043a\u0440\u0438\u0432\u0430\u0454 LEADER_ROLE_ID \u0430\u0431\u043e owner \u0441\u0435\u0440\u0432\u0435\u0440\u0430"
    if is_management
    else "\U0001f465 **\u041e\u0421\u041d\u041e\u0412\u041d\u0418\u0419 \u0421\u041a\u041b\u0410\u0414**"
  )

  embed = discord.Embed(
    title="\U0001f4b0 \u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438",
    description=(
      f"{category_text}\n"
      f"**{label}** \u2022 <@{user_id}>\n"
      f"\u0417\u0430\u0433\u0430\u043b\u044c\u043d\u0430 \u0441\u0443\u043c\u0430: **{format_cents(total)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c: **{len(rows)}**"
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

    if contract_rows:
      contract_lines = [
        (
          f"#{row['contract_id']} \u2022 {row['contract_name']} \u2014 "
          f"**{format_cents(row['amount_cents'])}**"
        )
        for row in contract_rows[:12]
      ]
      if len(contract_rows) > 12:
        contract_lines.append(f"\u2026\u0456 \u0449\u0435 {len(contract_rows) - 12}")

      embed.add_field(
        name="\U0001f4cb \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0418",
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
            f"{start_day.strftime('%d.%m')}\u2013{end_day.strftime('%d.%m')}"
            if start_day and end_day
            else "\u043f\u0435\u0440\u0456\u043e\u0434"
          )
          rank_text = f" \u2022 #{row['rank']}" if row["rank"] else ""
          bonus_lines.append(
            f"\U0001f3c6 \u041f\u0440\u0435\u043c\u0456\u044f \u0437\u0430 \u043f\u0435\u0440\u0456\u043e\u0434 {period_text}{rank_text} \u2014 "
            f"**{format_cents(row['amount_cents'])}**"
          )
        else:
          note_text = f" \u2022 {row['note']}" if row["note"] else ""
          bonus_lines.append(
            f"\U0001f3c6 \u0420\u0443\u0447\u043d\u0430 \u043f\u0440\u0435\u043c\u0456\u044f{note_text} \u2014 "
            f"**{format_cents(row['amount_cents'])}**"
          )

      if len(bonus_rows) > 12:
        bonus_lines.append(f"\u2026\u0456 \u0449\u0435 {len(bonus_rows) - 12}")

      embed.add_field(
        name="\U0001f3c6 \u041f\u0420\u0415\u041c\u0406\u0407",
        value="\n".join(bonus_lines),
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
      title="\U0001f4b0 \u0412\u0438\u043f\u043b\u0430\u0442\u0438",
      description="\u2705 \u0417\u0430\u0440\u0430\u0437 \u043d\u0435\u043c\u0430\u0454 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u0441\u0443\u043c \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438.",
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

  participants_total = sum(row["total_cents"] for row in participant_rows)
  management_total = sum(row["total_cents"] for row in management_rows)

  participant_lines = [
    (
      f"<@{row['user_id']}> \u2014 **{format_cents(row['total_cents'])}** "
      f"\u2022 {row['accrual_count']} \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c"
    )
    for row in participant_rows[:20]
  ] or ["\u2014"]

  management_lines = [
    (
      f"<@{row['user_id']}> \u2014 **{format_cents(row['total_cents'])}** "
      f"\u2022 {row['accrual_count']} \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c"
    )
    for row in management_rows[:20]
  ] or ["\u2014"]

  if len(participant_rows) > 20:
    participant_lines.append(
      f"\u2026\u0456 \u0449\u0435 {len(participant_rows) - 20}"
    )

  if len(management_rows) > 20:
    management_lines.append(
      f"\u2026\u0456 \u0449\u0435 {len(management_rows) - 20}"
    )

  embed = discord.Embed(
    title="\U0001f4b0 \u0412\u0418\u041f\u041b\u0410\u0422\u0418",
    description=(
      f"\u0412\u0441\u044c\u043e\u0433\u043e \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438: "
      f"**{format_cents(participants_total + management_total)}**"
    ),
    color=discord.Color.gold(),
  )

  embed.add_field(
    name=(
      f"\U0001f465 \u041e\u0421\u041d\u041e\u0412\u041d\u0418\u0419 \u0421\u041a\u041b\u0410\u0414 \u2022 "
      f"{len(participant_rows)} \u2022 {format_cents(participants_total)}"
    ),
    value="\n".join(participant_lines),
    inline=False,
  )

  embed.add_field(
    name=(
      f"\U0001f6e1 \u041a\u0415\u0420\u0406\u0412\u041d\u0418\u0419 \u0421\u041a\u041b\u0410\u0414 \u2022 "
      f"{len(management_rows)} \u2022 {format_cents(management_total)}"
    ),
    value="\n".join(management_lines),
    inline=False,
  )

  embed.set_footer(
    text="\u0412\u0438\u043f\u043b\u0430\u0442\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u043e\u043c\u0443 \u0441\u043a\u043b\u0430\u0434\u0443: LEADER_ROLE_ID \u0430\u0431\u043e owner \u0441\u0435\u0440\u0432\u0435\u0440\u0430."
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
    description="\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u0438 \u043d\u0430\u043a\u043e\u043f\u0438\u0447\u0435\u043d\u0456 \u0441\u0443\u043c\u0438 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c",
  )
  async def payouts(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    await send_payouts_list(interaction)


