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

from stats import (
  MyStatsView,
  build_my_stats_embed,
  build_public_rating_embed,
)

_bot: Optional[commands.Bot] = None

def set_bot(bot_instance: commands.Bot):
  global _bot
  _bot = bot_instance

def build_completed_embed(row: sqlite3.Row) -> discord.Embed:
  participants = parse_ids(row["participant_ids"])
  payment_mode = row["payment_mode"] or PAYMENT_MODE_NORMAL
  accruals = db.accruals_for_contract(row["id"]) if row["status"] == "paid" else []

  if row["status"] == "paid":
    if payment_mode == PAYMENT_MODE_LEGACY_FAMILY:
      color = discord.Color.blurple()
      status_text = "\U0001f3e0 **\u041d\u0430 \u0444\u0430\u043c\u0443**"
    elif accruals:
      pending_count = sum(
        1 for accrual in accruals
        if accrual["status"] == "pending"
      )
      paid_count = sum(
        1 for accrual in accruals
        if accrual["status"] == "paid"
      )

      if pending_count and paid_count:
        color = discord.Color.gold()
        status_text = "\U0001f7e1 **\u0427\u0430\u0441\u0442\u043a\u043e\u0432\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e**"
      elif pending_count:
        color = discord.Color.gold()
        status_text = "\U0001f7e1 **\u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438**"
      else:
        color = discord.Color.green()
        status_text = "\U0001f7e2 **\u0412\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e**"
    else:
      color = discord.Color.green()
      status_text = "\U0001f7e2 **\u0412\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e \u2022 \u0441\u0442\u0430\u0440\u0430 \u0441\u0438\u0441\u0442\u0435\u043c\u0430**"
  elif row["status"] == "annulled":
    color = discord.Color.dark_red()
    status_text = "\U0001f6ab **\u0410\u043d\u0443\u043b\u044c\u043e\u0432\u0430\u043d\u043e**"
  elif row["status"] == "cancelled":
    color = discord.Color.dark_grey()
    status_text = "\u26ab **\u0421\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e**"
  else:
    color = discord.Color.orange()
    status_text = "\U0001f534 **\u041d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e**"

  if row["status"] == "annulled":
    title = "\U0001f6ab \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422 \u0410\u041d\u0423\u041b\u042c\u041e\u0412\u0410\u041d\u041e"
  elif row["status"] == "cancelled":
    title = "\u274c \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422 \u0421\u041a\u0410\u0421\u041e\u0412\u0410\u041d\u041e"
  else:
    title = "\u2705 \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422 \u0412\u0418\u041a\u041e\u041d\u0410\u041d\u041e"

  embed = discord.Embed(
    title=title,
    color=color,
  )

  embed.add_field(
    name="\U0001f465 \u0412\u0438\u043a\u043e\u043d\u0443\u0432\u0430\u043b\u0438",
    value=" ".join(f"<@{uid}>" for uid in participants) or "\u2014",
    inline=False,
  )
  embed.add_field(
    name="\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442",
    value=row["contract_name"],
    inline=True,
  )
  embed.add_field(
    name="\U0001f4b0 \u0421\u0443\u043c\u0430",
    value=f"{format_money_dollars(row['price'])} $",
    inline=True,
  )
  embed.add_field(
    name="\u23f3 \u041a\u0414",
    value=row["cooldown"],
    inline=True,
  )

  note = (row["note"] or "").strip() if "note" in row.keys() else ""
  if note:
    embed.add_field(
      name="\U0001f4dd \u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430",
      value=note,
      inline=False,
    )

  embed.add_field(
    name="\U0001f4b3 \u0421\u0442\u0430\u0442\u0443\u0441",
    value=status_text,
    inline=False,
  )

  if row["status"] == "paid":
    calculated_ts = iso_to_unix(row["paid_at"])
    if calculated_ts:
      embed.add_field(
        name="\U0001f552 \u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e",
        value=f"<t:{calculated_ts}:f>",
        inline=True,
      )

    fomo_cents = row["fomo_cents"] or 0
    net_cents = row["net_cents"] or 0

    embed.add_field(
      name="\U0001f3e6 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457",
      value=format_cents(fomo_cents),
      inline=True,
    )
    embed.add_field(
      name="\U0001f4b0 \u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c",
      value=format_cents(net_cents),
      inline=True,
    )

    excluded_payment_ids = parse_ids(row["excluded_payment_ids"] or "[]")
    if excluded_payment_ids and payment_mode != PAYMENT_MODE_LEGACY_FAMILY:
      embed.add_field(
        name="\U0001f6ab \u0411\u0435\u0437 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f",
        value=" ".join(f"<@{uid}>" for uid in excluded_payment_ids),
        inline=False,
      )

    if payment_mode in (
      PAYMENT_MODE_REDISTRIBUTE,
      PAYMENT_MODE_FAMILY_SHARE,
      PAYMENT_MODE_LEGACY_FAMILY,
    ):
      embed.add_field(
        name="\u2699\ufe0f \u0421\u043f\u043e\u0441\u0456\u0431 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f",
        value=payment_mode_label(payment_mode),
        inline=False,
      )

    legacy_payouts = db.payouts_for_contract(row["id"])
    legacy_debts = db.admin_debts_for_contract(row["id"])

    payout_lines = []

    for p in legacy_payouts:
      payout_lines.append(
        f"\u2705 <@{p['user_id']}> \u2014 **{format_cents(p['amount_cents'])}** \u2022 \u0441\u0442\u0430\u0440\u0430 \u0432\u0438\u043f\u043b\u0430\u0442\u0430"
      )

    for debt in legacy_debts:
      if debt["status"] == "paid":
        payout_lines.append(
          f"\u2705 <@{debt['user_id']}> \u2014 **{format_cents(debt['amount_cents'])}** \u2022 \u0441\u0442\u0430\u0440\u0430 \u0432\u0456\u0434\u043a\u043b\u0430\u0434\u0435\u043d\u0430 \u0432\u0438\u043f\u043b\u0430\u0442\u0430 \u0437\u0430\u043a\u0440\u0438\u0442\u0430"
        )

    for accrual in accruals:
      if accrual["status"] == "paid":
        payout_lines.append(
          f"\u2705 <@{accrual['user_id']}> \u2014 **{format_cents(accrual['amount_cents'])}** \u2022 \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e"
        )
      else:
        payout_lines.append(
          f"\u23f3 <@{accrual['user_id']}> \u2014 **{format_cents(accrual['amount_cents'])}** \u2022 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438"
        )

    if payout_lines:
      embed.add_field(
        name="\U0001f464 \u0411\u0430\u043b\u0430\u043d\u0441\u0438 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432",
        value="\n".join(payout_lines),
        inline=False,
      )

  if row["status"] == "annulled":
    calculated_ts = iso_to_unix(row["paid_at"])
    annulled_ts = iso_to_unix(row["annulled_at"])
    annulled_by = row["annulled_by"]

    details = []
    if annulled_by:
      details.append(f"\u0410\u043d\u0443\u043b\u044e\u0432\u0430\u0432/\u043b\u0430: <@{annulled_by}>")
    if annulled_ts:
      details.append(f"<t:{annulled_ts}:f>")

    if calculated_ts:
      embed.add_field(
        name="\u0411\u0443\u043b\u043e \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e",
        value=f"<t:{calculated_ts}:f>",
        inline=True,
      )

    embed.add_field(
      name="\u0411\u0443\u043b\u043e \u0432 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457",
      value=format_cents(row["fomo_cents"] or 0),
      inline=True,
    )
    embed.add_field(
      name="\u0411\u0443\u043b\u043e \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c",
      value=format_cents(row["net_cents"] or 0),
      inline=True,
    )

    if details:
      embed.add_field(
        name="\u0410\u043d\u0443\u043b\u044e\u0432\u0430\u043d\u043d\u044f",
        value=" \u2022 ".join(details),
        inline=False,
      )

  if row["status"] == "cancelled":
    cancelled_ts = iso_to_unix(row["cancelled_at"])
    cancelled_by = row["cancelled_by"]
    details = []

    if cancelled_by:
      details.append(f"\u0421\u043a\u0430\u0441\u0443\u0432\u0430\u0432: <@{cancelled_by}>")
    if cancelled_ts:
      details.append(f"<t:{cancelled_ts}:f>")

    if details:
      embed.add_field(
        name="\u0421\u043a\u0430\u0441\u0443\u0432\u0430\u043d\u043d\u044f",
        value=" \u2022 ".join(details),
        inline=False,
      )

  embed.set_footer(text=f"ID \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443: {row['id']}")
  return embed


async def get_target_channel(guild: discord.Guild, fallback_channel_id: int):
  channel_id = CONTRACT_CHANNEL_ID or fallback_channel_id
  channel = guild.get_channel(channel_id)
  if channel is None:
    try:
      channel = await _bot.fetch_channel(channel_id)
    except discord.DiscordException:
      return None
  return channel


async def refresh_completed_message(message_id: int):
  row = db.get_completed_by_message(message_id)
  if not row:
    return

  try:
    channel = _bot.get_channel(row["channel_id"]) or await _bot.fetch_channel(row["channel_id"])
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
      return
    message = await channel.fetch_message(message_id)

    if row["status"] == "unpaid":
      view = UnpaidCompletedView(_bot)
    else:
      view = None

    await message.edit(embed=build_completed_embed(row), view=view)
  except discord.DiscordException:
    pass


async def audit_log(
  guild: Optional[discord.Guild],
  title: str,
  description: str,
  color: discord.Color = discord.Color.blurple(),
):
  if not LOG_CHANNEL_ID or guild is None:
    return

  try:
    channel = guild.get_channel(LOG_CHANNEL_ID) or await _bot.fetch_channel(LOG_CHANNEL_ID)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
      return

    embed = discord.Embed(
      title=title,
      description=description,
      color=color,
      timestamp=datetime.now(timezone.utc),
    )
    await channel.send(embed=embed)
  except discord.DiscordException:
    pass


def mentions(user_ids: list[int]) -> str:
  return " ".join(f"<@{uid}>" for uid in user_ids) or "\u2014"


class PerformerSelect(discord.ui.UserSelect):
  def __init__(self):
    super().__init__(
      placeholder="\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
      min_values=1,
      max_values=25,
    )

  async def callback(self, interaction: discord.Interaction):
    view: PerformerStepView = self.view # type: ignore
    ids = [u.id for u in self.values if not getattr(u, "bot", False)]

    if not ids:
      await interaction.response.send_message(
        "\u274c \u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0445\u043e\u0447\u0430 \u0431 \u043e\u0434\u043d\u043e\u0433\u043e \u0437\u0432\u0438\u0447\u0430\u0439\u043d\u043e\u0433\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430.",
        ephemeral=True,
      )
      return

    _, page, total_pages = picker_page_data(0)
    await interaction.response.edit_message(
      content=picker_content(ids, page, total_pages),
      view=ContractPickerView(view.bot, ids, page),
    )


class PerformerStepView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", show_self_button: bool = True):
    super().__init__(timeout=300)
    self.bot = bot_instance
    self.add_item(PerformerSelect())

    if not show_self_button:
      self.remove_item(self.myself)

  @discord.ui.button(
    label="\u042f \u0432\u0438\u043a\u043e\u043d\u0430\u0432/\u043b\u0430 \u0441\u0430\u043c/\u0430",
    style=discord.ButtonStyle.primary,
    emoji="\U0001f464",
  )
  async def myself(self, interaction: discord.Interaction, button: discord.ui.Button):
    if getattr(interaction.user, "bot", False):
      await interaction.response.send_message("\u274c \u0411\u043e\u0442 \u043d\u0435 \u043c\u043e\u0436\u0435 \u0431\u0443\u0442\u0438 \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0435\u043c.", ephemeral=True)
      return

    participant_ids = [interaction.user.id]
    _, page, total_pages = picker_page_data(0)
    await interaction.response.edit_message(
      content=picker_content(participant_ids, page, total_pages),
      view=ContractPickerView(self.bot, participant_ids, page),
    )


def picker_rows(query: Optional[str] = None):
  rows = db.list_active_contract_types(limit=500)
  rows = sorted(rows, key=lambda row: ukrainian_sort_key(row["name"]))

  if query:
    q = query.strip().casefold()
    if q:
      rows = [
        row for row in rows
        if q in row["name"].strip().casefold()
      ]

  return rows


def picker_page_data(
  page: int,
  page_size: int = 25,
  query: Optional[str] = None,
):
  rows = picker_rows(query)
  total = len(rows)
  total_pages = max(1, (total + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  start = page * page_size
  return rows[start:start + page_size], page, total_pages


def picker_content(
  participant_ids: list[int],
  page: int,
  total_pages: int,
  query: Optional[str] = None,
) -> str:
  mentions = " ".join(f"<@{uid}>" for uid in participant_ids)

  if query:
    title = f"\U0001f50e \u041f\u043e\u0448\u0443\u043a: **{query}**"
  else:
    title = "\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438"

  return (
    f"\U0001f465 \u0412\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456: {mentions}\n\n"
    f"{title} \u2022 \u0441\u0442\u043e\u0440\u0456\u043d\u043a\u0430 **{page + 1}/{total_pages}**\n"
    "\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437\u0456 \u0441\u043f\u0438\u0441\u043a\u0443."
  )


class ContractPageSelect(discord.ui.Select):
  def __init__(
    self,
    bot_instance: "ContractBot",
    participant_ids: list[int],
    page: int,
    query: Optional[str] = None,
  ):
    self.bot_instance = bot_instance
    self.participant_ids = participant_ids
    self.query = query

    rows, page, total_pages = picker_page_data(
      page,
      query=query,
    )
    self.page = page
    self.total_pages = total_pages

    options = [
      discord.SelectOption(
        label=row["name"][:100],
        value=str(row["id"]),
        description=(
          f"{format_money_dollars(row['price'])} $ \u2022 \u041a\u0414 {row['cooldown']}"
        )[:100],
      )
      for row in rows
    ]

    if not options:
      options = [
        discord.SelectOption(
          label="\u041d\u0456\u0447\u043e\u0433\u043e \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e",
          value="none",
          description="\u0417\u043c\u0456\u043d\u0456\u0442\u044c \u043f\u043e\u0448\u0443\u043a \u0430\u0431\u043e \u043f\u043e\u043a\u0430\u0436\u0456\u0442\u044c \u0443\u0441\u0456 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438",
        )
      ]

    placeholder = (
      f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0438: {query} \u2022 {page + 1}/{total_pages}"
      if query
      else f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u2022 {page + 1}/{total_pages}"
    )

    super().__init__(
      placeholder=placeholder[:150],
      options=options,
      min_values=1,
      max_values=1,
      disabled=(options[0].value == "none"),
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    type_id = int(self.values[0])
    row = db.get_contract_type(type_id)

    if not row or not row["active"]:
      await interaction.response.send_message(
        "\u274c \u0426\u0435\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    await interaction.response.edit_message(
      content="\u041f\u0435\u0440\u0435\u0432\u0456\u0440\u0442\u0435 \u0434\u0430\u043d\u0456 \u0439 \u043f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u044c\u0442\u0435.",
      embed=build_confirmation_embed(row, self.participant_ids),
      view=ConfirmContractView(
        self.bot_instance,
        self.participant_ids,
        type_id,
        return_page=self.page,
        return_query=self.query,
        note=None,
      ),
    )


class ContractSearchModal(discord.ui.Modal, title="\u041f\u043e\u0448\u0443\u043a \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443"):
  def __init__(
    self,
    bot_instance: "ContractBot",
    participant_ids: list[int],
    current_query: Optional[str] = None,
  ):
    super().__init__(timeout=300)
    self.bot_instance = bot_instance
    self.participant_ids = participant_ids

    self.search_input = discord.ui.TextInput(
      label="\u041d\u0430\u0437\u0432\u0430 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: \u0431\u0430\u043b\u043e\u043d\u0438, \u0434\u0440\u043e\u0432\u0430, \u043f\u0435\u0440\u0435\u0440\u043e\u0431\u043a\u0430...",
      default=current_query or None,
      required=True,
      max_length=80,
    )
    self.add_item(self.search_input)

  async def on_submit(self, interaction: discord.Interaction):
    query = str(self.search_input).strip()

    rows, page, total_pages = picker_page_data(
      0,
      query=query,
    )

    content = picker_content(
      self.participant_ids,
      page,
      total_pages,
      query,
    )

    # Modal \u0431\u0443\u0432 \u0432\u0456\u0434\u043a\u0440\u0438\u0442\u0438\u0439 \u043a\u043d\u043e\u043f\u043a\u043e\u044e \u0437 \u0446\u044c\u043e\u0433\u043e \u0436 ephemeral-\u043f\u043e\u0432\u0456\u0434\u043e\u043c\u043b\u0435\u043d\u043d\u044f,
    # \u0442\u043e\u043c\u0443 \u0440\u0435\u0434\u0430\u0433\u0443\u0454\u043c\u043e \u0439\u043e\u0433\u043e, \u0430 \u043d\u0435 \u0441\u0442\u0432\u043e\u0440\u044e\u0454\u043c\u043e \u0449\u0435 \u043e\u0434\u043d\u0435.
    try:
      await interaction.response.edit_message(
        content=content,
        embed=None,
        view=ContractPickerView(
          self.bot_instance,
          self.participant_ids,
          page,
          query,
        ),
      )
    except discord.InteractionResponded:
      await interaction.followup.send(
        content,
        view=ContractPickerView(
          self.bot_instance,
          self.participant_ids,
          page,
          query,
        ),
        ephemeral=True,
      )


class ContractPickerView(discord.ui.View):
  def __init__(
    self,
    bot_instance: "ContractBot",
    participant_ids: list[int],
    page: int = 0,
    query: Optional[str] = None,
  ):
    super().__init__(timeout=300)
    self.bot_instance = bot_instance
    self.participant_ids = participant_ids
    self.query = query

    _, self.page, self.total_pages = picker_page_data(
      page,
      query=query,
    )

    self.add_item(
      ContractPageSelect(
        bot_instance,
        participant_ids,
        self.page,
        query,
      )
    )

    self.previous.disabled = self.page <= 0
    self.page_indicator.label = f"{self.page + 1}/{self.total_pages}"
    self.next_page.disabled = self.page >= self.total_pages - 1
    self.clear_search.disabled = not bool(query)

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    style=discord.ButtonStyle.secondary,
    emoji="\u25c0\ufe0f",
    row=1,
  )
  async def previous(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    new_page = max(0, self.page - 1)
    _, new_page, total_pages = picker_page_data(
      new_page,
      query=self.query,
    )

    await interaction.response.edit_message(
      content=picker_content(
        self.participant_ids,
        new_page,
        total_pages,
        self.query,
      ),
      embed=None,
      view=ContractPickerView(
        self.bot_instance,
        self.participant_ids,
        new_page,
        self.query,
      ),
    )

  @discord.ui.button(
    label="1/1",
    style=discord.ButtonStyle.secondary,
    disabled=True,
    row=1,
  )
  async def page_indicator(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    pass

  @discord.ui.button(
    label="\u0414\u0430\u043b\u0456",
    style=discord.ButtonStyle.secondary,
    emoji="\u25b6\ufe0f",
    row=1,
  )
  async def next_page(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    new_page = min(self.total_pages - 1, self.page + 1)
    _, new_page, total_pages = picker_page_data(
      new_page,
      query=self.query,
    )

    await interaction.response.edit_message(
      content=picker_content(
        self.participant_ids,
        new_page,
        total_pages,
        self.query,
      ),
      embed=None,
      view=ContractPickerView(
        self.bot_instance,
        self.participant_ids,
        new_page,
        self.query,
      ),
    )

  @discord.ui.button(
    label="\u041f\u043e\u0448\u0443\u043a",
    style=discord.ButtonStyle.primary,
    emoji="\U0001f50e",
    row=2,
  )
  async def search(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      ContractSearchModal(
        self.bot_instance,
        self.participant_ids,
        self.query,
      )
    )

  @discord.ui.button(
    label="\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u0438 \u0432\u0441\u0456",
    style=discord.ButtonStyle.secondary,
    emoji="\U0001f4cb",
    row=2,
  )
  async def clear_search(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    _, page, total_pages = picker_page_data(0)

    await interaction.response.edit_message(
      content=picker_content(
        self.participant_ids,
        page,
        total_pages,
      ),
      embed=None,
      view=ContractPickerView(
        self.bot_instance,
        self.participant_ids,
        page,
      ),
    )


def build_confirmation_embed(
  contract_type: sqlite3.Row,
  participant_ids: list[int],
  note: Optional[str] = None,
) -> discord.Embed:
  embed = discord.Embed(
    title="\u041f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0438 \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u043d\u044f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
    color=discord.Color.blurple(),
  )
  embed.add_field(
    name="\U0001f465 \u0412\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456",
    value=" ".join(f"<@{uid}>" for uid in participant_ids),
    inline=False,
  )
  embed.add_field(name="\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442", value=contract_type["name"], inline=True)
  embed.add_field(
    name="\U0001f4b0 \u0421\u0443\u043c\u0430",
    value=f"{format_money_dollars(contract_type['price'])} $",
    inline=True,
  )
  embed.add_field(name="\u23f3 \u041a\u0414", value=contract_type["cooldown"], inline=True)

  cleaned_note = (note or "").strip()
  if cleaned_note:
    embed.add_field(
      name="\U0001f4dd \u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430",
      value=cleaned_note,
      inline=False,
    )
  else:
    embed.set_footer(
      text="\u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430 \u043d\u0435\u043e\u0431\u043e\u0432\u2019\u044f\u0437\u043a\u043e\u0432\u0430."
    )

  return embed


class ContractNoteModal(discord.ui.Modal, title="\U0001f4dd \u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430 \u0434\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443"):
  def __init__(
    self,
    bot_instance: "ContractBot",
    participant_ids: list[int],
    type_id: int,
    return_page: int,
    return_query: Optional[str],
    current_note: Optional[str] = None,
  ):
    super().__init__(timeout=300)
    self.bot_instance = bot_instance
    self.participant_ids = participant_ids
    self.type_id = type_id
    self.return_page = return_page
    self.return_query = return_query

    self.note_input = discord.ui.TextInput(
      label="\u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: \u043e\u0441\u043e\u0431\u043b\u0438\u0432\u0456 \u0443\u043c\u043e\u0432\u0438, \u043f\u043e\u044f\u0441\u043d\u0435\u043d\u043d\u044f...",
      default=current_note or None,
      required=False,
      max_length=500,
      style=discord.TextStyle.paragraph,
    )
    self.add_item(self.note_input)

  async def on_submit(self, interaction: discord.Interaction):
    contract_type = db.get_contract_type(self.type_id)
    if not contract_type or not contract_type["active"]:
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    note = str(self.note_input).strip() or None

    await interaction.response.edit_message(
      content="\u041f\u0435\u0440\u0435\u0432\u0456\u0440\u0442\u0435 \u0434\u0430\u043d\u0456 \u0439 \u043f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u044c\u0442\u0435.",
      embed=build_confirmation_embed(
        contract_type,
        self.participant_ids,
        note,
      ),
      view=ConfirmContractView(
        self.bot_instance,
        self.participant_ids,
        self.type_id,
        return_page=self.return_page,
        return_query=self.return_query,
        note=note,
      ),
    )


class ConfirmContractView(discord.ui.View):
  def __init__(
    self,
    bot_instance: "ContractBot",
    participant_ids: list[int],
    type_id: int,
    return_page: int = 0,
    return_query: Optional[str] = None,
    note: Optional[str] = None,
  ):
    super().__init__(timeout=300)
    self.bot_instance = bot_instance
    self.participant_ids = participant_ids
    self.type_id = type_id
    self.return_page = return_page
    self.return_query = return_query
    self.note = (note or "").strip() or None

  @discord.ui.button(label="\u041f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0438", style=discord.ButtonStyle.success, emoji="\u2705")
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    guild = interaction.guild
    if guild is None:
      await interaction.response.send_message("\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.", ephemeral=True)
      return

    contract_type = db.get_contract_type(self.type_id)
    if not contract_type or not contract_type["active"]:
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u0432\u0438\u0434\u0430\u043b\u0435\u043d\u0438\u0439 \u0456\u0437 \u043f\u0435\u0440\u0435\u043b\u0456\u043a\u0443.",
        ephemeral=True,
      )
      return

    channel = await get_target_channel(guild, interaction.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
      await interaction.response.send_message(
        "\u274c \u041d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u043a\u0430\u043d\u0430\u043b \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432.",
        ephemeral=True,
      )
      return

    # \u041e\u0434\u0440\u0430\u0437\u0443 \u043f\u0440\u0438\u0431\u0438\u0440\u0430\u0454\u043c\u043e \u043a\u043d\u043e\u043f\u043a\u0438, \u0449\u043e\u0431 \u043f\u043e\u0434\u0432\u0456\u0439\u043d\u0438\u0439 \u043a\u043b\u0456\u043a \u043d\u0435 \u0441\u0442\u0432\u043e\u0440\u0438\u0432 \u0434\u0443\u0431\u043b\u044c.
    await interaction.response.edit_message(
      content="\u23f3 \u0417\u0430\u043f\u0438\u0441\u0443\u044e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442...",
      embed=None,
      view=None,
    )

    placeholder = await channel.send("\u23f3 \u0417\u0430\u043f\u0438\u0441\u0443\u044e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442...")

    db.add_completed_contract(
      message_id=placeholder.id,
      guild_id=guild.id,
      channel_id=channel.id,
      creator_id=interaction.user.id,
      participant_ids=self.participant_ids,
      contract_type=contract_type,
      note=self.note,
    )

    row = db.get_completed_by_message(placeholder.id)
    await placeholder.edit(
      content=None,
      embed=build_completed_embed(row),
      view=UnpaidCompletedView(self.bot_instance),
    )

    await audit_log(
      guild,
      "\u2705 \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437\u0430\u043f\u0438\u0441\u0430\u043d\u043e",
      (
        f"\u0417\u0430\u043f\u0438\u0441: **#{row['id']}**\n"
        f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442: **{row['contract_name']}**\n"
        f"\u0421\u0443\u043c\u0430: **{format_money_dollars(row['price'])} $**\n"
        f"\u0412\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456: {mentions(self.participant_ids)}\n"
        + (f"\U0001f4dd \u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430: {self.note}\n" if self.note else "")
        + f"\u0417\u0430\u043f\u0438\u0441\u0430\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    # \u041f\u0430\u043d\u0435\u043b\u044c \u0437\u0430\u0432\u0436\u0434\u0438 \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u0438\u043c\u043e \u0432 \u0441\u0430\u043c\u0438\u0439 \u043d\u0438\u0437 \u043a\u0430\u043d\u0430\u043b\u0443.
    if isinstance(channel, discord.TextChannel):
      await move_main_panel_to_bottom(guild, channel)

    await interaction.edit_original_response(
      content=f"\u2705 \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437\u0430\u043f\u0438\u0441\u0430\u043d\u043e: {placeholder.jump_url}",
      embed=None,
      view=None,
    )


  @discord.ui.button(
    label="\u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430",
    style=discord.ButtonStyle.primary,
    emoji="\U0001f4dd",
  )
  async def add_note(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      ContractNoteModal(
        self.bot_instance,
        self.participant_ids,
        self.type_id,
        self.return_page,
        self.return_query,
        self.note,
      )
    )

  @discord.ui.button(label="\u041d\u0430\u0437\u0430\u0434", style=discord.ButtonStyle.secondary, emoji="\u21a9\ufe0f")
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    _, page, total_pages = picker_page_data(
      self.return_page,
      query=self.return_query,
    )
    await interaction.response.edit_message(
      content=picker_content(
        self.participant_ids,
        page,
        total_pages,
        self.return_query,
      ),
      embed=None,
      view=ContractPickerView(
        self.bot_instance,
        self.participant_ids,
        page,
        self.return_query,
      ),
    )


class CancelCompletedConfirmView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(timeout=60)
    self.bot_instance = bot_instance
    self.message_id = message_id

  @discord.ui.button(label="\u0422\u0430\u043a, \u0441\u043a\u0430\u0441\u0443\u0432\u0430\u0442\u0438", style=discord.ButtonStyle.danger, emoji="\U0001f5d1\ufe0f")
  async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", view=None)
      return

    row_before = db.get_completed_by_message(self.message_id)

    await interaction.response.edit_message(
      content="\u23f3 \u0421\u043a\u0430\u0441\u043e\u0432\u0443\u044e \u0437\u0430\u043f\u0438\u0441...",
      view=None,
    )

    ok = db.cancel_completed(self.message_id, interaction.user.id)
    if not ok:
      await interaction.edit_original_response(
        content="\u274c \u0421\u043a\u0430\u0441\u0443\u0432\u0430\u0442\u0438 \u043c\u043e\u0436\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0435\u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    if row_before:
      await audit_log(
        interaction.guild,
        "\U0001f5d1\ufe0f \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e",
        (
          f"\u0417\u0430\u043f\u0438\u0441: **#{row_before['id']}**\n"
          f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442: **{row_before['contract_name']}**\n"
          f"\u0421\u043a\u0430\u0441\u0443\u0432\u0430\u0432/\u043b\u0430: <@{interaction.user.id}>"
        ),
        discord.Color.red(),
      )

    await interaction.edit_original_response(
      content="\u2705 \u0417\u0430\u043f\u0438\u0441 \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e. \u0412\u0456\u043d \u0431\u0456\u043b\u044c\u0448\u0435 \u043d\u0435 \u0440\u0430\u0445\u0443\u0454\u0442\u044c\u0441\u044f \u0432 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u0446\u0456.",
      view=None,
    )

  @discord.ui.button(label="\u041d\u0456", style=discord.ButtonStyle.secondary)
  async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="\u0421\u043a\u0430\u0441\u0443\u0432\u0430\u043d\u043d\u044f \u0432\u0456\u0434\u043c\u0456\u043d\u0435\u043d\u043e.", view=None)


async def payment_preview_with_accruals(
  interaction: discord.Interaction,
  bot_instance: "ContractBot",
  row: sqlite3.Row,
  payment_mode: str,
  excluded_ids: Optional[list[int]] = None,
  back_to_custom: bool = False,
):
  embed = payment_preview_embed(
    row,
    payment_mode,
    excluded_ids,
  )

  return embed, PaymentConfirmView(
    bot_instance,
    row["message_id"],
    payment_mode,
    excluded_ids,
    back_to_custom=back_to_custom,
  )


class PaymentConfirmView(discord.ui.View):
  def __init__(
    self,
    bot_instance: "ContractBot",
    message_id: int,
    payment_mode: str,
    excluded_ids: Optional[list[int]] = None,
    back_to_custom: bool = False,
  ):
    super().__init__(timeout=180)
    self.bot_instance = bot_instance
    self.message_id = message_id
    self.payment_mode = payment_mode
    self.excluded_ids = excluded_ids or []
    self.back_to_custom = back_to_custom

  @discord.ui.button(
    label="\u041f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0438",
    style=discord.ButtonStyle.success,
    emoji="\u2705",
  )
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(
        content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.",
        view=None,
      )
      return

    row_before = db.get_completed_by_message(self.message_id)
    if not row_before or row_before["status"] != "unpaid":
      await interaction.response.edit_message(
        content="\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0439 \u0430\u0431\u043e \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0439.",
        embed=None,
        view=None,
      )
      return

    await interaction.response.edit_message(
      content="\u23f3 \u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0443\u044e \u0441\u0443\u043c\u0438...",
      embed=None,
      view=None,
    )

    result = db.pay_completed(
      self.message_id,
      interaction.user.id,
      payment_mode=self.payment_mode,
      excluded_payment_ids=self.excluded_ids,
    )

    if not result:
      await interaction.edit_original_response(
        content="\u274c \u041d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f \u0432\u0438\u043a\u043e\u043d\u0430\u0442\u0438 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f.",
        embed=None,
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    accrual_lines = [
      f"<@{uid}> \u2014 **{format_cents(amount)}**"
      for uid, amount in result["accruals"].items()
    ]
    accruals_text = "\n".join(accrual_lines) or "\u2014"

    excluded_text = (
      "\u2014"
      if self.payment_mode == PAYMENT_MODE_LEGACY_FAMILY
      else mentions(result["excluded_payment_ids"])
    )

    await audit_log(
      interaction.guild,
      "\U0001f4b0 \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e",
      (
        f"\u0417\u0430\u043f\u0438\u0441: **#{row_before['id']}**\n"
        f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442: **{row_before['contract_name']}**\n"
        f"\u0421\u043f\u043e\u0441\u0456\u0431: **{payment_mode_label(self.payment_mode)}**\n"
        f"\u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457: **{format_cents(result['fomo_cents'])}**\n"
        f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(result['net_cents'])}**\n"
        f"\u0411\u0435\u0437 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f: {excluded_text}\n"
        f"\u0411\u0430\u043b\u0430\u043d\u0441 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438:\n{accruals_text}\n"
        f"\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.gold(),
    )

    await interaction.edit_original_response(
      content=(
        "\u2705 \u0421\u0443\u043c\u0438 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e.\n"
        "\u0424\u0430\u043a\u0442\u0438\u0447\u043d\u0443 \u0432\u0438\u043f\u043b\u0430\u0442\u0443 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c \u0442\u0435\u043f\u0435\u0440 \u0437\u0430\u043a\u0440\u0438\u0432\u0430\u0439\u0442\u0435 \u0447\u0435\u0440\u0435\u0437 `/payouts`."
      ),
      embed=None,
      view=None,
    )

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    style=discord.ButtonStyle.secondary,
    emoji="\u21a9\ufe0f",
  )
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    row = db.get_completed_by_message(self.message_id)
    if not row or interaction.guild is None:
      await interaction.response.edit_message(
        content="\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439.",
        embed=None,
        view=None,
      )
      return

    if self.back_to_custom:
      await interaction.response.edit_message(
        content=(
          "\u2699\ufe0f **\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u0442\u0438 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f**\n"
          "\u041e\u0431\u0435\u0440\u0456\u0442\u044c, \u043a\u043e\u043c\u0443 \u043d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0443\u0432\u0430\u0442\u0438 \u0433\u0440\u043e\u0448\u0456, \u0430 \u043f\u043e\u0442\u0456\u043c \u0441\u043f\u043e\u0441\u0456\u0431 \u0440\u043e\u0437\u043f\u043e\u0434\u0456\u043b\u0443."
        ),
        embed=None,
        view=CustomPaymentView(
          self.bot_instance,
          self.message_id,
          interaction.guild,
          parse_ids(row["participant_ids"]),
          self.excluded_ids,
        ),
      )
    else:
      await interaction.response.edit_message(
        content="\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f \u043d\u0435 \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u043e.",
        embed=None,
        view=None,
      )


class CustomExcludeSelect(discord.ui.Select):
  def __init__(
    self,
    guild: discord.Guild,
    participant_ids: list[int],
    selected_ids: Optional[list[int]] = None,
  ):
    selected = set(selected_ids or [])
    options = []

    for uid in participant_ids:
      member = guild.get_member(uid)
      label = member.display_name if member else f"ID {uid}"
      options.append(
        discord.SelectOption(
          label=label[:100],
          value=str(uid),
          description="\u041d\u0435 \u0432\u0438\u043f\u043b\u0430\u0447\u0443\u0432\u0430\u0442\u0438 \u0433\u0440\u043e\u0448\u0456 \u0446\u044c\u043e\u043c\u0443 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0443",
          default=uid in selected,
        )
      )

    super().__init__(
      placeholder="\u041a\u043e\u0433\u043e \u0432\u0438\u043a\u043b\u044e\u0447\u0438\u0442\u0438 \u0437 \u043e\u043f\u043b\u0430\u0442\u0438?",
      min_values=1,
      max_values=len(options),
      options=options,
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    view: CustomPaymentView = self.view  # type: ignore
    view.excluded_ids = [int(uid) for uid in self.values]

    view.redistribute.disabled = len(view.excluded_ids) >= len(view.participant_ids)
    view.family_share.disabled = not bool(view.excluded_ids)

    await interaction.response.edit_message(
      content=(
        "\u2699\ufe0f **\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u0442\u0438 \u043e\u043f\u043b\u0430\u0442\u0443**\n"
        f"\U0001f6ab \u0411\u0435\u0437 \u0432\u0438\u043f\u043b\u0430\u0442\u0438: {mentions(view.excluded_ids)}\n\n"
        "\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0441\u043f\u043e\u0441\u0456\u0431:"
      ),
      view=view,
    )


class CustomPaymentView(discord.ui.View):
  def __init__(
    self,
    bot_instance: "ContractBot",
    message_id: int,
    guild: discord.Guild,
    participant_ids: list[int],
    selected_ids: Optional[list[int]] = None,
  ):
    super().__init__(timeout=300)
    self.bot_instance = bot_instance
    self.message_id = message_id
    self.guild = guild
    self.participant_ids = participant_ids
    self.excluded_ids = selected_ids or []

    self.add_item(
      CustomExcludeSelect(
        guild,
        participant_ids,
        self.excluded_ids,
      )
    )

    self.redistribute.disabled = (
      not self.excluded_ids
      or len(self.excluded_ids) >= len(self.participant_ids)
    )
    self.family_share.disabled = not bool(self.excluded_ids)

  @discord.ui.button(
    label="\u0420\u043e\u0437\u0434\u0456\u043b\u0438\u0442\u0438 \u043c\u0456\u0436 \u0440\u0435\u0448\u0442\u043e\u044e",
    style=discord.ButtonStyle.success,
    emoji="\U0001f4b8",
    row=1,
  )
  async def redistribute(self, interaction: discord.Interaction, button: discord.ui.Button):
    row = db.get_completed_by_message(self.message_id)
    if not row:
      await interaction.response.edit_message(content="\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e.", view=None)
      return

    try:
      embed, confirm_view = await payment_preview_with_accruals(
        interaction,
        self.bot_instance,
        row,
        PAYMENT_MODE_REDISTRIBUTE,
        self.excluded_ids,
        back_to_custom=True,
      )
    except ValueError:
      await interaction.response.edit_message(
        content="\u274c \u0414\u043b\u044f \u0446\u044c\u043e\u0433\u043e \u0441\u043f\u043e\u0441\u043e\u0431\u0443 \u043c\u0430\u0454 \u0437\u0430\u043b\u0438\u0448\u0438\u0442\u0438\u0441\u044f \u0445\u043e\u0447\u0430 \u0431 \u043e\u0434\u0438\u043d \u043e\u0442\u0440\u0438\u043c\u0443\u0432\u0430\u0447.",
        view=self,
      )
      return

    await interaction.response.edit_message(
      content=None,
      embed=embed,
      view=confirm_view,
    )

  @discord.ui.button(
    label="\u0427\u0430\u0441\u0442\u043a\u0443 \u0432 \u0441\u0456\u043c'\u044e",
    style=discord.ButtonStyle.primary,
    emoji="\U0001f3e6",
    row=1,
  )
  async def family_share(self, interaction: discord.Interaction, button: discord.ui.Button):
    row = db.get_completed_by_message(self.message_id)
    if not row:
      await interaction.response.edit_message(content="\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e.", view=None)
      return

    embed, confirm_view = await payment_preview_with_accruals(
      interaction,
      self.bot_instance,
      row,
      PAYMENT_MODE_FAMILY_SHARE,
      self.excluded_ids,
      back_to_custom=True,
    )

    await interaction.response.edit_message(
      content=None,
      embed=embed,
      view=confirm_view,
    )

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    style=discord.ButtonStyle.secondary,
    emoji="\u21a9\ufe0f",
    row=1,
  )
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u043d\u043d\u044f \u043e\u043f\u043b\u0430\u0442\u0438 \u0437\u0430\u043a\u0440\u0438\u0442\u043e.",
      embed=None,
      view=None,
    )


class CorrectionPerformerSelect(discord.ui.UserSelect):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(
      placeholder="\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u0438\u0445 \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432",
      min_values=1,
      max_values=25,
    )
    self.bot_instance = bot_instance
    self.message_id = message_id

  async def callback(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", view=None)
      return

    new_ids = [u.id for u in self.values if not getattr(u, "bot", False)]
    row_before = db.get_completed_by_message(self.message_id)

    if not row_before or not db.update_completed_participants(self.message_id, new_ids):
      await interaction.response.edit_message(
        content="\u274c \u0417\u043c\u0456\u043d\u0438\u0442\u0438 \u043c\u043e\u0436\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0435\u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    await audit_log(
      interaction.guild,
      "\u270f\ufe0f \u0417\u043c\u0456\u043d\u0435\u043d\u043e \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432",
      (
        f"\u0417\u0430\u043f\u0438\u0441: **#{row_before['id']}**\n"
        f"\u0411\u0443\u043b\u043e: {mentions(parse_ids(row_before['participant_ids']))}\n"
        f"\u0421\u0442\u0430\u043b\u043e: {mentions(new_ids)}\n"
        f"\u0417\u043c\u0456\u043d\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.orange(),
    )

    await interaction.response.edit_message(
      content=f"\u2705 \u0412\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432 \u043e\u043d\u043e\u0432\u043b\u0435\u043d\u043e: {mentions(new_ids)}",
      view=None,
    )


class CorrectionPerformerView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(timeout=180)
    self.add_item(CorrectionPerformerSelect(bot_instance, message_id))


def correction_contract_page(page: int, page_size: int = 25):
  rows = sorted(
    db.list_active_contract_types(limit=500),
    key=lambda row: ukrainian_sort_key(row["name"]),
  )
  total_pages = max(1, (len(rows) + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  return rows[page * page_size:(page + 1) * page_size], page, total_pages


class CorrectionContractSelect(discord.ui.Select):
  def __init__(self, bot_instance: "ContractBot", message_id: int, page: int):
    self.bot_instance = bot_instance
    self.message_id = message_id
    rows, self.page, self.total_pages = correction_contract_page(page)

    options = [
      discord.SelectOption(
        label=row["name"][:100],
        value=str(row["id"]),
        description=f"{format_money_dollars(row['price'])} $ \u2022 \u041a\u0414 {row['cooldown']}"[:100],
      )
      for row in rows
    ]

    super().__init__(
      placeholder=f"\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u2022 {self.page + 1}/{self.total_pages}",
      min_values=1,
      max_values=1,
      options=options,
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", view=None)
      return

    row_before = db.get_completed_by_message(self.message_id)
    contract_type = db.get_contract_type(int(self.values[0]))

    if (
      not row_before
      or not contract_type
      or not contract_type["active"]
      or not db.update_completed_contract_type(self.message_id, contract_type)
    ):
      await interaction.response.edit_message(
        content="\u274c \u0417\u043c\u0456\u043d\u0438\u0442\u0438 \u043c\u043e\u0436\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0435\u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    await audit_log(
      interaction.guild,
      "\u270f\ufe0f \u0417\u043c\u0456\u043d\u0435\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443 \u0437\u0430\u043f\u0438\u0441\u0456",
      (
        f"\u0417\u0430\u043f\u0438\u0441: **#{row_before['id']}**\n"
        f"\u0411\u0443\u043b\u043e: **{row_before['contract_name']}** \u2014 "
        f"{format_money_dollars(row_before['price'])} $\n"
        f"\u0421\u0442\u0430\u043b\u043e: **{contract_type['name']}** \u2014 "
        f"{format_money_dollars(contract_type['price'])} $\n"
        f"\u0417\u043c\u0456\u043d\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.orange(),
    )

    await interaction.response.edit_message(
      content=f"\u2705 \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437\u043c\u0456\u043d\u0435\u043d\u043e \u043d\u0430 **{contract_type['name']}**.",
      view=None,
    )


class CorrectionContractView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", message_id: int, page: int = 0):
    super().__init__(timeout=180)
    self.bot_instance = bot_instance
    self.message_id = message_id
    _, self.page, self.total_pages = correction_contract_page(page)
    self.add_item(CorrectionContractSelect(bot_instance, message_id, self.page))
    self.previous.disabled = self.page <= 0
    self.page_label.label = f"{self.page + 1}/{self.total_pages}"
    self.next_page.disabled = self.page >= self.total_pages - 1

  @discord.ui.button(label="\u041d\u0430\u0437\u0430\u0434", emoji="\u25c0\ufe0f", style=discord.ButtonStyle.secondary, row=1)
  async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      view=CorrectionContractView(self.bot_instance, self.message_id, self.page - 1)
    )

  @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=1)
  async def page_label(self, interaction: discord.Interaction, button: discord.ui.Button):
    pass

  @discord.ui.button(label="\u0414\u0430\u043b\u0456", emoji="\u25b6\ufe0f", style=discord.ButtonStyle.secondary, row=1)
  async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      view=CorrectionContractView(self.bot_instance, self.message_id, self.page + 1)
    )


class CorrectionMenuView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(timeout=180)
    self.bot_instance = bot_instance
    self.message_id = message_id

  @discord.ui.button(label="\u0412\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456", emoji="\U0001f465", style=discord.ButtonStyle.primary)
  async def performers(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="\U0001f465 \u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u0438\u0439 \u0441\u043f\u0438\u0441\u043e\u043a \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432:",
      view=CorrectionPerformerView(self.bot_instance, self.message_id),
    )

  @discord.ui.button(label="\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442", emoji="\U0001f4cb", style=discord.ButtonStyle.primary)
  async def contract(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="\U0001f4cb \u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442:",
      view=CorrectionContractView(self.bot_instance, self.message_id),
    )

  @discord.ui.button(label="\u041d\u0430\u0437\u0430\u0434", emoji="\u21a9\ufe0f", style=discord.ButtonStyle.secondary)
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="\u0420\u0435\u0434\u0430\u0433\u0443\u0432\u0430\u043d\u043d\u044f \u0437\u0430\u043a\u0440\u0438\u0442\u043e.", view=None)


class AnnulPaidConfirmView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(timeout=60)
    self.bot_instance = bot_instance
    self.message_id = message_id

  @discord.ui.button(
    label="\u0422\u0430\u043a, \u0430\u043d\u0443\u043b\u044e\u0432\u0430\u0442\u0438",
    style=discord.ButtonStyle.danger,
    emoji="\U0001f6ab",
  )
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(
        content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.",
        view=None,
      )
      return

    row_before = db.get_completed_by_message(self.message_id)
    if not row_before or row_before["status"] != "paid":
      await interaction.response.edit_message(
        content="\u274c \u0410\u043d\u0443\u043b\u044e\u0432\u0430\u0442\u0438 \u043c\u043e\u0436\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442.",
        view=None,
      )
      return

    await interaction.response.edit_message(
      content="\u23f3 \u0410\u043d\u0443\u043b\u044e\u044e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442...",
      view=None,
    )

    ok = db.annul_paid(
      self.message_id,
      interaction.user.id,
    )

    if not ok:
      await interaction.edit_original_response(
        content="\u274c \u041d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f \u0430\u043d\u0443\u043b\u044e\u0432\u0430\u0442\u0438 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    await audit_log(
      interaction.guild,
      "\U0001f6ab \u041e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0430\u043d\u0443\u043b\u044c\u043e\u0432\u0430\u043d\u043e",
      (
        f"\u0417\u0430\u043f\u0438\u0441: **#{row_before['id']}**\n"
        f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442: **{row_before['contract_name']}**\n"
        f"\u0421\u0443\u043c\u0430: **{format_money_dollars(row_before['price'])} $**\n"
        f"\u0411\u0443\u043b\u043e \u0432 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457: **{format_cents(row_before['fomo_cents'] or 0)}**\n"
        f"\u0411\u0443\u043b\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(row_before['net_cents'] or 0)}**\n"
        f"\u0410\u043d\u0443\u043b\u044e\u0432\u0430\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.red(),
    )

    await interaction.edit_original_response(
      content=(
        "\u2705 \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0430\u043d\u0443\u043b\u044c\u043e\u0432\u0430\u043d\u043e.\n"
        "\u0419\u043e\u0433\u043e \u0433\u0440\u043e\u0448\u0456 \u0442\u0430 \u0431\u0430\u043b\u0438 \u0431\u0456\u043b\u044c\u0448\u0435 \u043d\u0435 \u0432\u0440\u0430\u0445\u043e\u0432\u0443\u044e\u0442\u044c\u0441\u044f \u0443 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u0446\u0456, "
        "\u0430\u043b\u0435 \u0437\u0430\u043f\u0438\u0441 \u0437\u0430\u043b\u0438\u0448\u0438\u0432\u0441\u044f \u0432 \u0456\u0441\u0442\u043e\u0440\u0456\u0457."
      ),
      view=None,
    )

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    style=discord.ButtonStyle.secondary,
    emoji="\u21a9\ufe0f",
  )
  async def back(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content="\u0410\u043d\u0443\u043b\u044e\u0432\u0430\u043d\u043d\u044f \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e.",
      view=None,
    )


class UnpaidCompletedView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot"):
    super().__init__(timeout=None)
    self.bot_instance = bot_instance

  @discord.ui.button(
    label="\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u0442\u0438",
    style=discord.ButtonStyle.success,
    emoji="\U0001f4b0",
    custom_id="contract_v3:paid",
    row=0,
  )
  async def paid(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0443\u0432\u0430\u0442\u0438 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043c\u043e\u0436\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message("\u274c \u041d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u0437\u0430\u043f\u0438\u0441.", ephemeral=True)
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u0430\u0431\u043e \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    embed, confirm_view = await payment_preview_with_accruals(
      interaction,
      self.bot_instance,
      row,
      PAYMENT_MODE_NORMAL,
      [],
    )

    await interaction.response.send_message(
      embed=embed,
      view=confirm_view,
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u041d\u0430 \u0444\u0430\u043c\u0443",
    style=discord.ButtonStyle.primary,
    emoji="\U0001f3e0",
    custom_id="contract_v3:family",
    row=0,
  )
  async def family_payment(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041e\u043f\u043b\u0430\u0447\u0443\u0432\u0430\u0442\u0438 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u043c\u043e\u0436\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message(
        "\u274c \u041d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u0437\u0430\u043f\u0438\u0441.",
        ephemeral=True,
      )
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u0430\u0431\u043e \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    embed, confirm_view = await payment_preview_with_accruals(
      interaction,
      self.bot_instance,
      row,
      PAYMENT_MODE_LEGACY_FAMILY,
      [],
    )

    await interaction.response.send_message(
      embed=embed,
      view=confirm_view,
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u0442\u0438",
    style=discord.ButtonStyle.primary,
    emoji="\u2699\ufe0f",
    custom_id="contract_v4:custompay",
    row=0,
  )
  async def custom_payment(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041d\u0430\u043b\u0430\u0448\u0442\u043e\u0432\u0443\u0432\u0430\u0442\u0438 \u043e\u043f\u043b\u0430\u0442\u0443 \u043c\u043e\u0436\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e.",
        ephemeral=True,
      )
      return

    if interaction.message is None or interaction.guild is None:
      await interaction.response.send_message("\u274c \u041d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u0437\u0430\u043f\u0438\u0441.", ephemeral=True)
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u0430\u0431\u043e \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    participant_ids = parse_ids(row["participant_ids"])

    await interaction.response.send_message(
      (
        "\u2699\ufe0f **\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u0442\u0438 \u043e\u043f\u043b\u0430\u0442\u0443**\n"
        "\u041e\u0431\u0435\u0440\u0456\u0442\u044c, \u043a\u043e\u0433\u043e \u043d\u0435 \u043f\u043e\u0442\u0440\u0456\u0431\u043d\u043e \u043e\u043f\u043b\u0430\u0447\u0443\u0432\u0430\u0442\u0438.\n\n"
        "**\u0420\u043e\u0437\u0434\u0456\u043b\u0438\u0442\u0438 \u043c\u0456\u0436 \u0440\u0435\u0448\u0442\u043e\u044e** \u2014 85% \u0434\u0456\u043b\u0438\u0442\u044c\u0441\u044f \u043c\u0456\u0436 \u0442\u0438\u043c\u0438, \u0445\u0442\u043e \u0437\u0430\u043b\u0438\u0448\u0438\u0432\u0441\u044f.\n"
        "**\u0427\u0430\u0441\u0442\u043a\u0443 \u0432 \u0441\u0456\u043c'\u044e** \u2014 \u0447\u0430\u0441\u0442\u043a\u0430 \u0432\u0438\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0445 \u043f\u0435\u0440\u0435\u0445\u043e\u0434\u0438\u0442\u044c \u0443 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457."
      ),
      view=CustomPaymentView(
        self.bot_instance,
        interaction.message.id,
        interaction.guild,
        participant_ids,
      ),
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u0412\u0438\u043f\u0440\u0430\u0432\u0438\u0442\u0438",
    style=discord.ButtonStyle.secondary,
    emoji="\u270f\ufe0f",
    custom_id="contract_v4:edit",
    row=1,
  )
  async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0412\u0438\u043f\u0440\u0430\u0432\u043b\u044f\u0442\u0438 \u0437\u0430\u043f\u0438\u0441\u0438 \u043c\u043e\u0436\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message("\u274c \u041d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u0437\u0430\u043f\u0438\u0441.", ephemeral=True)
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "\u274c \u0412\u0438\u043f\u0440\u0430\u0432\u043b\u044f\u0442\u0438 \u043c\u043e\u0436\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0435\u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      "\u270f\ufe0f \u0429\u043e \u043f\u043e\u0442\u0440\u0456\u0431\u043d\u043e \u0432\u0438\u043f\u0440\u0430\u0432\u0438\u0442\u0438?",
      view=CorrectionMenuView(self.bot_instance, interaction.message.id),
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u0421\u043a\u0430\u0441\u0443\u0432\u0430\u0442\u0438",
    style=discord.ButtonStyle.danger,
    emoji="\U0001f5d1\ufe0f",
    custom_id="contract_v3:cancel",
    row=1,
  )
  async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0421\u043a\u0430\u0441\u043e\u0432\u0443\u0432\u0430\u0442\u0438 \u0437\u0430\u043f\u0438\u0441\u0438 \u043c\u043e\u0436\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message("\u274c \u041d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u0437\u0430\u043f\u0438\u0441.", ephemeral=True)
      return

    await interaction.response.send_message(
      "\u26a0\ufe0f \u0421\u043a\u0430\u0441\u0443\u0432\u0430\u0442\u0438 \u0446\u0435\u0439 \u0437\u0430\u043f\u0438\u0441? \u0412\u0456\u043d \u0431\u0443\u0434\u0435 \u0432\u0438\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0439 \u0437\u0456 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0438.",
      view=CancelCompletedConfirmView(self.bot_instance, interaction.message.id),
      ephemeral=True,
    )


class ContractTypeModal(discord.ui.Modal):
  def __init__(self, mode: str, user_id: int, type_id: Optional[int] = None):
    self.mode = mode
    self.user_id = user_id
    self.type_id = type_id

    row = db.get_contract_type(type_id) if type_id else None

    super().__init__(
      title="\u0414\u043e\u0434\u0430\u0442\u0438 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442" if mode == "add" else "\u0420\u0435\u0434\u0430\u0433\u0443\u0432\u0430\u0442\u0438 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442",
      timeout=300,
    )

    self.name_input = discord.ui.TextInput(
      label="\u041d\u0430\u0437\u0432\u0430 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: \u041c\u0430\u0439\u0441\u0442\u0440\u0438 \u0431\u0430\u043a\u0456\u0432",
      default=row["name"] if row else None,
      max_length=100,
    )
    self.price_input = discord.ui.TextInput(
      label="\u0426\u0456\u043d\u0430 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: 100000 \u0430\u0431\u043e 100\u043a",
      default=str(row["price"]) if row else None,
      max_length=20,
    )
    self.cooldown_input = discord.ui.TextInput(
      label="\u041a\u0414 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: 4 \u0433\u043e\u0434",
      default=row["cooldown"] if row else None,
      max_length=50,
    )

    self.add_item(self.name_input)
    self.add_item(self.price_input)
    self.add_item(self.cooldown_input)

  async def on_submit(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", ephemeral=True)
      return

    try:
      price = parse_money(str(self.price_input))
    except ValueError:
      await interaction.response.send_message(
        "\u274c \u041d\u0435\u043a\u043e\u0440\u0435\u043a\u0442\u043d\u0430 \u0446\u0456\u043d\u0430. \u041f\u0440\u0438\u043a\u043b\u0430\u0434\u0438: `100000`, `100\u043a`, `1.2\u043c`.",
        ephemeral=True,
      )
      return

    name = str(self.name_input).strip()
    cooldown = str(self.cooldown_input).strip()

    if self.mode == "add":
      row = db.create_contract_type(name, price, cooldown, interaction.user.id)
      await audit_log(
        interaction.guild,
        "\u2795 \u0414\u043e\u0434\u0430\u043d\u043e \u0442\u0438\u043f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
        (
          f"**{row['name']}**\n"
          f"\u0426\u0456\u043d\u0430: **{format_money_dollars(row['price'])} $**\n"
          f"\u041a\u0414: **{row['cooldown']}**\n"
          f"\u0414\u043e\u0434\u0430\u0432/\u043b\u0430: <@{interaction.user.id}>"
        ),
        discord.Color.green(),
      )
      await interaction.response.send_message(
        f"\u2705 \u0414\u043e\u0434\u0430\u043d\u043e: **{row['name']}** \u2014 {format_money_dollars(row['price'])} $ \u2014 \u041a\u0414 {row['cooldown']}",
        ephemeral=True,
      )
      return

    ok = db.update_contract_type(self.type_id, name, price, cooldown)
    if not ok:
      await interaction.response.send_message(
        "\u274c \u041d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f \u0437\u0431\u0435\u0440\u0435\u0433\u0442\u0438. \u041c\u043e\u0436\u043b\u0438\u0432\u043e, \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437 \u0442\u0430\u043a\u043e\u044e \u043d\u0430\u0437\u0432\u043e\u044e \u0432\u0436\u0435 \u0456\u0441\u043d\u0443\u0454.",
        ephemeral=True,
      )
      return

    row = db.get_contract_type(self.type_id)
    await audit_log(
      interaction.guild,
      "\u270f\ufe0f \u041e\u043d\u043e\u0432\u043b\u0435\u043d\u043e \u0442\u0438\u043f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443",
      (
        f"**{row['name']}**\n"
        f"\u0426\u0456\u043d\u0430: **{format_money_dollars(row['price'])} $**\n"
        f"\u041a\u0414: **{row['cooldown']}**\n"
        f"\u0417\u043c\u0456\u043d\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.orange(),
    )
    await interaction.response.send_message(
      f"\u2705 \u041e\u043d\u043e\u0432\u043b\u0435\u043d\u043e: **{row['name']}** \u2014 {format_money_dollars(row['price'])} $ \u2014 \u041a\u0414 {row['cooldown']}",
      ephemeral=True,
    )


def admin_picker_page_data(page: int, page_size: int = 25):
  rows = sorted(
    db.list_active_contract_types(limit=500),
    key=lambda row: ukrainian_sort_key(row["name"]),
  )
  total = len(rows)
  total_pages = max(1, (total + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  start = page * page_size
  return rows[start:start + page_size], page, total_pages


def admin_picker_text(page: int, total_pages: int) -> str:
  return (
    "\U0001f4cb **\u041a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430\u043c\u0438**\n"
    f"\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437\u0456 \u0441\u043f\u0438\u0441\u043a\u0443 \u2022 \u0441\u0442\u043e\u0440\u0456\u043d\u043a\u0430 **{page + 1}/{total_pages}**"
  )


class AdminManageSelect(discord.ui.Select):
  def __init__(self, page: int):
    rows, page, total_pages = admin_picker_page_data(page)
    self.page = page
    self.total_pages = total_pages

    options = [
      discord.SelectOption(
        label=row["name"][:100],
        value=str(row["id"]),
        description=(
          f"{format_money_dollars(row['price'])} $ \u2022 \u041a\u0414 {row['cooldown']}"
        )[:100],
      )
      for row in rows
    ]

    if not options:
      options = [
        discord.SelectOption(
          label="\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0449\u0435 \u043d\u0435\u043c\u0430\u0454",
          value="none",
          description="\u0421\u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u043d\u0430\u0442\u0438\u0441\u043d\u0456\u0442\u044c \u00ab\u0414\u043e\u0434\u0430\u0442\u0438\u00bb",
        )
      ]

    super().__init__(
      placeholder=f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u2022 {page + 1}/{total_pages}",
      options=options,
      min_values=1,
      max_values=1,
      disabled=(options[0].value == "none"),
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", ephemeral=True)
      return

    type_id = int(self.values[0])
    row = db.get_contract_type(type_id)

    if not row or not row["active"]:
      await interaction.response.edit_message(
        content="\u274c \u0426\u0435\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439.",
        embed=None,
        view=AdminManagePickerView(self.page),
      )
      return

    embed = discord.Embed(
      title=f"\u2699\ufe0f {row['name']}",
      color=discord.Color.blurple(),
    )
    embed.add_field(
      name="\U0001f4b0 \u0426\u0456\u043d\u0430",
      value=f"{format_money_dollars(row['price'])} $",
      inline=True,
    )
    embed.add_field(
      name="\u23f3 \u041a\u0414",
      value=row["cooldown"],
      inline=True,
    )

    await interaction.response.edit_message(
      content=None,
      embed=embed,
      view=ManageOneTypeView(type_id, return_page=self.page),
    )


class AdminManagePickerView(discord.ui.View):
  def __init__(self, page: int = 0):
    super().__init__(timeout=300)

    _, self.page, self.total_pages = admin_picker_page_data(page)
    self.add_item(AdminManageSelect(self.page))

    self.previous.disabled = self.page <= 0
    self.page_indicator.label = f"{self.page + 1}/{self.total_pages}"
    self.next_page.disabled = self.page >= self.total_pages - 1

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    style=discord.ButtonStyle.secondary,
    emoji="\u25c0\ufe0f",
    row=1,
  )
  async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
    new_page = max(0, self.page - 1)
    _, new_page, total_pages = admin_picker_page_data(new_page)

    await interaction.response.edit_message(
      content=admin_picker_text(new_page, total_pages),
      embed=None,
      view=AdminManagePickerView(new_page),
    )

  @discord.ui.button(
    label="1/1",
    style=discord.ButtonStyle.secondary,
    disabled=True,
    row=1,
  )
  async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
    pass

  @discord.ui.button(
    label="\u0414\u0430\u043b\u0456",
    style=discord.ButtonStyle.secondary,
    emoji="\u25b6\ufe0f",
    row=1,
  )
  async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
    new_page = min(self.total_pages - 1, self.page + 1)
    _, new_page, total_pages = admin_picker_page_data(new_page)

    await interaction.response.edit_message(
      content=admin_picker_text(new_page, total_pages),
      embed=None,
      view=AdminManagePickerView(new_page),
    )


class DeleteTypeConfirmView(discord.ui.View):
  def __init__(self, type_id: int):
    super().__init__(timeout=60)
    self.type_id = type_id

  @discord.ui.button(label="\u0422\u0430\u043a, \u0432\u0438\u0434\u0430\u043b\u0438\u0442\u0438", style=discord.ButtonStyle.danger, emoji="\U0001f5d1\ufe0f")
  async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", view=None)
      return

    row = db.get_contract_type(self.type_id)
    db.archive_contract_type(self.type_id)
    await audit_log(
      interaction.guild,
      "\U0001f5d1\ufe0f \u0422\u0438\u043f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443 \u043f\u0440\u0438\u0431\u0440\u0430\u043d\u043e",
      (
        f"**{row['name'] if row else self.type_id}**\n"
        f"\u041f\u0440\u0438\u0431\u0440\u0430\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.red(),
    )
    await interaction.response.edit_message(
      content="\u2705 \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043f\u0440\u0438\u0431\u0440\u0430\u043d\u043e \u0437 \u043f\u0435\u0440\u0435\u043b\u0456\u043a\u0443. \u0421\u0442\u0430\u0440\u0456 \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u043d\u044f \u0437\u0430\u043b\u0438\u0448\u0438\u043b\u0438\u0441\u044f \u0432 \u0456\u0441\u0442\u043e\u0440\u0456\u0457.",
      embed=None,
      view=None,
    )

  @discord.ui.button(label="\u041d\u0456", style=discord.ButtonStyle.secondary)
  async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="\u0412\u0438\u0434\u0430\u043b\u0435\u043d\u043d\u044f \u0432\u0456\u0434\u043c\u0456\u043d\u0435\u043d\u043e.", embed=None, view=None)


class ManageOneTypeView(discord.ui.View):
  def __init__(self, type_id: int, return_page: int = 0):
    super().__init__(timeout=300)
    self.type_id = type_id
    self.return_page = return_page

  @discord.ui.button(label="\u0420\u0435\u0434\u0430\u0433\u0443\u0432\u0430\u0442\u0438", style=discord.ButtonStyle.primary, emoji="\u270f\ufe0f")
  async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", ephemeral=True)
      return

    # \u0422\u0443\u0442 \u0444\u043e\u0440\u043c\u0430 \u0437\u0430\u043b\u0438\u0448\u0430\u0454\u0442\u044c\u0441\u044f, \u0431\u043e Discord \u0434\u043e\u0437\u0432\u043e\u043b\u044f\u0454 \u0432\u0432\u043e\u0434\u0438\u0442\u0438 \u043d\u0430\u0437\u0432\u0443/\u0446\u0456\u043d\u0443/\u041a\u0414
    # \u0441\u0430\u043c\u0435 \u0447\u0435\u0440\u0435\u0437 Modal. \u0410\u043b\u0435 \u043f\u043e\u0448\u0443\u043a\u0443 \u0447\u0435\u0440\u0435\u0437 \u043e\u043a\u0440\u0435\u043c\u0435 \u0432\u0456\u043a\u043d\u043e \u0431\u0456\u043b\u044c\u0448\u0435 \u043d\u0435\u043c\u0430\u0454.
    await interaction.response.send_modal(
      ContractTypeModal("edit", interaction.user.id, self.type_id)
    )

  @discord.ui.button(label="\u0412\u0438\u0434\u0430\u043b\u0438\u0442\u0438", style=discord.ButtonStyle.danger, emoji="\U0001f5d1\ufe0f")
  async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", ephemeral=True)
      return

    row = db.get_contract_type(self.type_id)
    await interaction.response.edit_message(
      content=f"\u26a0\ufe0f \u041f\u0440\u0438\u0431\u0440\u0430\u0442\u0438 **{row['name']}** \u0437 \u043f\u0435\u0440\u0435\u043b\u0456\u043a\u0443?",
      embed=None,
      view=DeleteTypeConfirmView(self.type_id),
    )

  @discord.ui.button(label="\u0414\u043e \u0441\u043f\u0438\u0441\u043a\u0443", style=discord.ButtonStyle.secondary, emoji="\u21a9\ufe0f")
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    _, page, total_pages = admin_picker_page_data(self.return_page)
    await interaction.response.edit_message(
      content=admin_picker_text(page, total_pages),
      embed=None,
      view=AdminManagePickerView(page),
    )


class ResetRatingConfirmView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=60)

  @discord.ui.button(
    label="\u0422\u0430\u043a, \u043e\u0431\u043d\u0443\u043b\u0438\u0442\u0438 \u0440\u0435\u0439\u0442\u0438\u043d\u0433",
    style=discord.ButtonStyle.danger,
    emoji="\u267b\ufe0f",
  )
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", view=None)
      return

    if interaction.guild is None:
      await interaction.response.edit_message(content="\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.", view=None)
      return

    reset_at = utc_now_iso()
    db.set_setting(interaction.guild.id, "rating_reset_at", reset_at)
    reset_ts = iso_to_unix(reset_at)

    when = f"<t:{reset_ts}:f>" if reset_ts else "\u0437\u0430\u0440\u0430\u0437"
    await audit_log(
      interaction.guild,
      "\u267b\ufe0f \u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u043e\u0431\u043d\u0443\u043b\u0435\u043d\u043e",
      f"\u041e\u0431\u043d\u0443\u043b\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>",
      discord.Color.red(),
    )
    await interaction.response.edit_message(
      content=(
        f"\u2705 \u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u043e\u0431\u043d\u0443\u043b\u0435\u043d\u043e {when}.\n"
        "\u0421\u0442\u0430\u0440\u0456 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u0442\u0430 \u0444\u0456\u043d\u0430\u043d\u0441\u0438 \u043d\u0435 \u0437\u043c\u0456\u043d\u0435\u043d\u0456. "
        "\u0417 \u0446\u044c\u043e\u0433\u043e \u043c\u043e\u043c\u0435\u043d\u0442\u0443 \u0437 \u043d\u0443\u043b\u044f \u0440\u0430\u0445\u0443\u0454\u0442\u044c\u0441\u044f \u0442\u0456\u043b\u044c\u043a\u0438 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u0437\u0430 \u0431\u0430\u043b\u0430\u043c\u0438."
      ),
      view=None,
    )

  @discord.ui.button(label="\u041d\u0456", style=discord.ButtonStyle.secondary)
  async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="\u041e\u0431\u043d\u0443\u043b\u0435\u043d\u043d\u044f \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443 \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e.", view=None)


class ResetEarningsConfirmView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=60)

  @discord.ui.button(
    label="\u0422\u0430\u043a, \u043e\u0431\u043d\u0443\u043b\u0438\u0442\u0438 \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a",
    style=discord.ButtonStyle.danger,
    emoji="\U0001f4b8",
  )
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", view=None)
      return

    if interaction.guild is None:
      await interaction.response.edit_message(
        content="\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.",
        view=None,
      )
      return

    reset_at = utc_now_iso()
    db.set_setting(interaction.guild.id, "earnings_reset_at", reset_at)
    reset_ts = iso_to_unix(reset_at)
    when = f"<t:{reset_ts}:f>" if reset_ts else "\u0437\u0430\u0440\u0430\u0437"

    await audit_log(
      interaction.guild,
      "\U0001f4b8 \u0417\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a \u043e\u0431\u043d\u0443\u043b\u0435\u043d\u043e",
      f"\u041e\u0431\u043d\u0443\u043b\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>",
      discord.Color.red(),
    )

    await interaction.response.edit_message(
      content=(
        f"\u2705 \u0417\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a \u043e\u0431\u043d\u0443\u043b\u0435\u043d\u043e {when}.\n"
        "\u0406\u0441\u0442\u043e\u0440\u0456\u044f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0442\u0430 \u043e\u043f\u043b\u0430\u0442 \u043d\u0435 \u0432\u0438\u0434\u0430\u043b\u0435\u043d\u0430. "
        "\u0417 \u0446\u044c\u043e\u0433\u043e \u043c\u043e\u043c\u0435\u043d\u0442\u0443 \u0437 \u043d\u0443\u043b\u044f \u0440\u0430\u0445\u0443\u044e\u0442\u044c\u0441\u044f \u0437\u0430\u0433\u0430\u043b\u044c\u043d\u0438\u0439 \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a, "
        "\u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457 \u0442\u0430 \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a \u043a\u043e\u0436\u043d\u043e\u0433\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430."
      ),
      view=None,
    )

  @discord.ui.button(label="\u041d\u0456", style=discord.ButtonStyle.secondary)
  async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="\u041e\u0431\u043d\u0443\u043b\u0435\u043d\u043d\u044f \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043a\u0443 \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e.",
      view=None,
    )


class ContractAdminPanelView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=300)

  @discord.ui.button(label="\u0414\u043e\u0434\u0430\u0442\u0438", style=discord.ButtonStyle.success, emoji="\u2795")
  async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", ephemeral=True)
      return
    await interaction.response.send_modal(ContractTypeModal("add", interaction.user.id))

  @discord.ui.button(
    label="\u041a\u0435\u0440\u0443\u0432\u0430\u0442\u0438 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430\u043c\u0438",
    style=discord.ButtonStyle.primary,
    emoji="\U0001f4cb",
  )
  async def manage_contracts(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.", ephemeral=True)
      return

    rows, page, total_pages = admin_picker_page_data(0)
    if not rows:
      await interaction.response.send_message(
        "\u041f\u043e\u043a\u0438 \u0449\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u043d\u0435\u043c\u0430\u0454. \u0421\u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u043d\u0430\u0442\u0438\u0441\u043d\u0456\u0442\u044c **\u2795 \u0414\u043e\u0434\u0430\u0442\u0438**.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      admin_picker_text(page, total_pages),
      view=AdminManagePickerView(page),
      ephemeral=True,
    )


def build_main_panel_embed() -> discord.Embed:
  return discord.Embed(
    title="\U0001f4cb \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0418 \u0421\u0406\u041c\u2019\u0407",
    description=(
      "\u0412\u0438\u043a\u043e\u043d\u0430\u0432 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u2014 \u043e\u0431\u0435\u0440\u0438 \u043f\u043e\u0442\u0440\u0456\u0431\u043d\u0443 \u043a\u043d\u043e\u043f\u043a\u0443.\n\n"
      "\U0001f464 **\u042f \u0432\u0438\u043a\u043e\u043d\u0430\u0432/\u043b\u0430** \u2014 \u044f\u043a\u0449\u043e \u0432\u0438\u043a\u043e\u043d\u0443\u0432\u0430\u0432/\u043b\u0430 \u0441\u0430\u043c/\u0430.\n"
      "\U0001f465 **\u041a\u0456\u043b\u044c\u043a\u0430 \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432** \u2014 \u044f\u043a\u0449\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0440\u043e\u0431\u0438\u043b\u0438 \u0440\u0430\u0437\u043e\u043c.\n"
      "\U0001f3c6 **\u0420\u0435\u0439\u0442\u0438\u043d\u0433** \u2014 \u043f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432.\n"
      "\U0001f464 **\u041c\u043e\u044f \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430** \u2014 \u043c\u043e\u0457 \u0431\u0430\u043b\u0438, \u0443\u0447\u0430\u0441\u0442\u0456 \u0442\u0430 \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a.\n"
      "\U0001f50e **\u041f\u043e\u0448\u0443\u043a** \u2014 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439 \u043f\u0440\u044f\u043c\u043e \u0432\u0441\u0435\u0440\u0435\u0434\u0438\u043d\u0456 \u0441\u043f\u0438\u0441\u043a\u0443 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432.\n\n"
      "\u041d\u0430\u0437\u0432\u0430, \u0446\u0456\u043d\u0430 \u0442\u0430 \u041a\u0414 \u043f\u0456\u0434\u0442\u044f\u0433\u0443\u044e\u0442\u044c\u0441\u044f \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u043e."
    ),
    color=discord.Color.blurple(),
  )


async def move_main_panel_to_bottom(
  guild: discord.Guild,
  channel: discord.TextChannel,
) -> Optional[discord.Message]:
  """
  \u0422\u0440\u0438\u043c\u0430\u0454 \u043f\u0430\u043d\u0435\u043b\u044c \u043e\u0441\u0442\u0430\u043d\u043d\u0456\u043c \u043f\u043e\u0432\u0456\u0434\u043e\u043c\u043b\u0435\u043d\u043d\u044f\u043c \u0443 \u043a\u0430\u043d\u0430\u043b\u0456.
  \u0421\u0442\u0430\u0440\u0443 \u043f\u0430\u043d\u0435\u043b\u044c \u0432\u0438\u0434\u0430\u043b\u044f\u0454\u043c\u043e; \u044f\u043a\u0449\u043e Discord \u043d\u0435 \u0434\u0430\u0454 \u2014 \u043f\u0440\u0438\u0431\u0438\u0440\u0430\u0454\u043c\u043e \u0437 \u043d\u0435\u0457 \u043a\u043d\u043e\u043f\u043a\u0438.
  """
  old_panel_id = db.get_setting(guild.id, "panel_message_id")

  if old_panel_id:
    try:
      old = await channel.fetch_message(int(old_panel_id))
      try:
        await old.delete()
      except discord.DiscordException:
        try:
          await old.edit(view=None)
        except discord.DiscordException:
          pass
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
      pass

  try:
    panel = await channel.send(
      embed=build_main_panel_embed(),
      view=MainContractPanelView(_bot),
    )
  except discord.DiscordException:
    return None

  db.set_setting(guild.id, "panel_message_id", str(panel.id))
  return panel


class MainContractPanelView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot"):
    super().__init__(timeout=None)
    self.bot_instance = bot_instance

  @discord.ui.button(
    label="\u042f \u0432\u0438\u043a\u043e\u043d\u0430\u0432/\u043b\u0430",
    style=discord.ButtonStyle.success,
    emoji="\U0001f464",
    custom_id="contract_v34:self",
  )
  async def self_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not db.active_contract_types(limit=1):
      await interaction.response.send_message(
        "\u274c \u041f\u0435\u0440\u0435\u043b\u0456\u043a \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0449\u0435 \u043f\u043e\u0440\u043e\u0436\u043d\u0456\u0439. \u041a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e \u043c\u0430\u0454 \u0434\u043e\u0434\u0430\u0442\u0438 \u0457\u0445 \u0447\u0435\u0440\u0435\u0437 `/contracts_admin`.",
        ephemeral=True,
      )
      return

    participant_ids = [interaction.user.id]
    _, page, total_pages = picker_page_data(0)
    await interaction.response.send_message(
      picker_content(participant_ids, page, total_pages),
      view=ContractPickerView(
        self.bot_instance,
        participant_ids,
        page,
      ),
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u041a\u0456\u043b\u044c\u043a\u0430 \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432",
    style=discord.ButtonStyle.primary,
    emoji="\U0001f465",
    custom_id="contract_v34:group",
  )
  async def group_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not db.active_contract_types(limit=1):
      await interaction.response.send_message(
        "\u274c \u041f\u0435\u0440\u0435\u043b\u0456\u043a \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0449\u0435 \u043f\u043e\u0440\u043e\u0436\u043d\u0456\u0439. \u041a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e \u043c\u0430\u0454 \u0434\u043e\u0434\u0430\u0442\u0438 \u0457\u0445 \u0447\u0435\u0440\u0435\u0437 `/contracts_admin`.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      "\U0001f465 \u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0443\u0441\u0456\u0445 \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443:",
      view=PerformerStepView(self.bot_instance, show_self_button=False),
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u0420\u0435\u0439\u0442\u0438\u043d\u0433",
    style=discord.ButtonStyle.secondary,
    emoji="\U0001f3c6",
    custom_id="contract_v34:rating",
  )
  async def rating(self, interaction: discord.Interaction, button: discord.ui.Button):
    if interaction.guild is None:
      await interaction.response.send_message(
        "\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      embed=build_public_rating_embed(interaction.guild.id),
      ephemeral=True,
    )


  @discord.ui.button(
    label="\u041c\u043e\u044f \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430",
    style=discord.ButtonStyle.secondary,
    emoji="\U0001f464",
    custom_id="contract_v4:mystats",
  )
  async def my_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
    if interaction.guild is None:
      await interaction.response.send_message(
        "\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      embed=build_my_stats_embed(interaction.guild.id, interaction.user.id),
      view=MyStatsView(
        interaction.guild.id,
        interaction.user.id,
        "general",
      ),
      ephemeral=True,
    )




def register_commands(bot: commands.Bot):
  @bot.tree.command(name="setup", description="\u0421\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u043f\u0430\u043d\u0435\u043b\u044c \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432")
  async def setup_panel(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0426\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u0434\u043b\u044f \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0430.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      await interaction.response.send_message(
        "\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.",
        ephemeral=True,
      )
      return

    channel = await get_target_channel(guild, interaction.channel_id)
    if not isinstance(channel, discord.TextChannel):
      await interaction.response.send_message(
        "\u274c \u041f\u0430\u043d\u0435\u043b\u044c \u0442\u0440\u0435\u0431\u0430 \u0441\u0442\u0432\u043e\u0440\u044e\u0432\u0430\u0442\u0438 \u0443 \u0437\u0432\u0438\u0447\u0430\u0439\u043d\u043e\u043c\u0443 \u0442\u0435\u043a\u0441\u0442\u043e\u0432\u043e\u043c\u0443 \u043a\u0430\u043d\u0430\u043b\u0456.",
        ephemeral=True,
      )
      return

    await interaction.response.defer(ephemeral=True)

    panel = await move_main_panel_to_bottom(guild, channel)

    if panel is None:
      await interaction.followup.send(
        "\u274c \u041d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f \u0441\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u043f\u0430\u043d\u0435\u043b\u044c \u0443 \u043a\u0430\u043d\u0430\u043b\u0456.",
        ephemeral=True,
      )
      return

    log_note = (
      ""
      if LOG_CHANNEL_ID
      else "\n\u26a0\ufe0f LOG_CHANNEL_ID \u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e \u2014 \u0436\u0443\u0440\u043d\u0430\u043b \u0434\u0456\u0439 \u043f\u043e\u043a\u0438 \u0432\u0438\u043c\u043a\u043d\u0435\u043d\u0438\u0439."
    )

    await interaction.followup.send(
      (
        f"\u2705 \u041f\u0430\u043d\u0435\u043b\u044c \u0433\u043e\u0442\u043e\u0432\u0430: {panel.jump_url}\n"
        "\u0407\u0457 \u0431\u0456\u043b\u044c\u0448\u0435 \u043d\u0435 \u0442\u0440\u0435\u0431\u0430 \u0448\u0443\u043a\u0430\u0442\u0438 \u0432 \u0437\u0430\u043a\u0440\u0456\u043f\u043b\u0435\u043d\u0438\u0445 \u2014 \u043f\u0456\u0441\u043b\u044f \u043a\u043e\u0436\u043d\u043e\u0433\u043e \u043d\u043e\u0432\u043e\u0433\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443 "
        "\u0431\u043e\u0442 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u043e \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u0438\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c \u0443 \u0441\u0430\u043c\u0438\u0439 \u043d\u0438\u0437 \u043a\u0430\u043d\u0430\u043b\u0443."
        f"{log_note}"
      ),
      ephemeral=True,
    )


  @bot.tree.command(name="contracts_admin", description="\u041a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f \u043f\u0435\u0440\u0435\u043b\u0456\u043a\u043e\u043c \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432")
  async def contracts_admin(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0426\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u0434\u043b\u044f \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0430.",
        ephemeral=True,
      )
      return

    embed = discord.Embed(
      title="\u2699\ufe0f \u041a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430\u043c\u0438",
      description=(
        "\u0422\u0443\u0442 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e \u0441\u0442\u0432\u043e\u0440\u044e\u0454 \u0442\u0430 \u0440\u0435\u0434\u0430\u0433\u0443\u0454 \u043f\u0435\u0440\u0435\u043b\u0456\u043a \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432.\n"
        "\u0414\u043b\u044f \u043a\u043e\u0436\u043d\u043e\u0433\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443 \u0437\u0431\u0435\u0440\u0456\u0433\u0430\u044e\u0442\u044c\u0441\u044f **\u043d\u0430\u0437\u0432\u0430, \u0446\u0456\u043d\u0430 \u0442\u0430 \u041a\u0414**.\n"

      ),
      color=discord.Color.blurple(),
    )
    await interaction.response.send_message(
      embed=embed,
      view=ContractAdminPanelView(),
      ephemeral=True,
    )


  @bot.tree.command(
    name="reset-rating",
    description="\u041e\u0431\u043d\u0443\u043b\u0438\u0442\u0438 \u043f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432",
  )
  async def reset_rating_command(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      "\u26a0\ufe0f \u041e\u0431\u043d\u0443\u043b\u0438\u0442\u0438 \u043f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433?\n"
      "\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438, \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u0442\u0430 \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a \u0437\u0430\u043b\u0438\u0448\u0430\u0442\u044c\u0441\u044f \u0431\u0435\u0437 \u0437\u043c\u0456\u043d. "
      "\u0417 \u043d\u0443\u043b\u044f \u043f\u043e\u0447\u043d\u0443\u0442\u044c\u0441\u044f \u0442\u0456\u043b\u044c\u043a\u0438 \u0431\u0430\u043b\u0438 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443.",
      view=ResetRatingConfirmView(),
      ephemeral=True,
    )


  @bot.tree.command(
    name="reset-earnings",
    description="\u041f\u043e\u0447\u0430\u0442\u0438 \u043d\u043e\u0432\u0438\u0439 \u043f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0444\u0456\u043d\u0430\u043d\u0441\u043e\u0432\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434",
  )
  async def reset_earnings_command(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      "\u26a0\ufe0f \u041f\u043e\u0447\u0430\u0442\u0438 \u043d\u043e\u0432\u0438\u0439 \u0444\u0456\u043d\u0430\u043d\u0441\u043e\u0432\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434?\n"
      "\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u0442\u0430 \u0456\u0441\u0442\u043e\u0440\u0456\u044f \u0437\u0430\u043b\u0438\u0448\u0430\u0442\u044c\u0441\u044f \u0432 \u0431\u0430\u0437\u0456, "
      "\u0430\u043b\u0435 \u043f\u043e\u0442\u043e\u0447\u043d\u0430 \u0444\u0456\u043d\u0430\u043d\u0441\u043e\u0432\u0430 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u043f\u043e\u0447\u043d\u0435\u0442\u044c\u0441\u044f \u0437 \u043d\u0443\u043b\u044f.",
      view=ResetEarningsConfirmView(),
      ephemeral=True,
    )


  @bot.tree.command(
    name="annul",
    description="\u0410\u043d\u0443\u043b\u044e\u0432\u0430\u0442\u0438 \u0432\u0436\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437\u0430 \u0439\u043e\u0433\u043e ID",
  )
  @app_commands.describe(
    contract_id="ID \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443, \u0432\u043a\u0430\u0437\u0430\u043d\u0438\u0439 \u0432\u043d\u0438\u0437\u0443 \u0439\u043e\u0433\u043e \u043a\u0430\u0440\u0442\u043a\u0438",
  )
  async def annul_contract(
    interaction: discord.Interaction,
    contract_id: int,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0410\u043d\u0443\u043b\u044e\u0432\u0430\u0442\u0438 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043c\u043e\u0436\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u043e.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      await interaction.response.send_message(
        "\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.",
        ephemeral=True,
      )
      return

    row = db.get_completed_by_id(contract_id)

    if not row or row["guild_id"] != guild.id:
      await interaction.response.send_message(
        f"\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0437 ID **{contract_id}** \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e \u043d\u0430 \u0446\u044c\u043e\u043c\u0443 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.",
        ephemeral=True,
      )
      return

    if row["status"] == "annulled":
      await interaction.response.send_message(
        f"\u2139\ufe0f \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 **#{contract_id}** \u0443\u0436\u0435 \u0430\u043d\u0443\u043b\u044c\u043e\u0432\u0430\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    if row["status"] != "paid":
      status_names = {
        "unpaid": "\u043d\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439",
        "cancelled": "\u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0439",
      }
      status_name = status_names.get(row["status"], row["status"])
      await interaction.response.send_message(
        (
          f"\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 **#{contract_id}** \u0437\u0430\u0440\u0430\u0437 **{status_name}**.\n"
          "\u0410\u043d\u0443\u043b\u044e\u0432\u0430\u0442\u0438 \u0446\u0456\u0454\u044e \u043a\u043e\u043c\u0430\u043d\u0434\u043e\u044e \u043c\u043e\u0436\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u0432\u0436\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442."
        ),
        ephemeral=True,
      )
      return

    participant_text = mentions(parse_ids(row["participant_ids"]))
    jump_url = (
      f"https://discord.com/channels/{guild.id}/"
      f"{row['channel_id']}/{row['message_id']}"
    )

    embed = discord.Embed(
      title=f"\U0001f6ab \u0410\u043d\u0443\u043b\u044e\u0432\u0430\u043d\u043d\u044f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443 #{contract_id}",
      description=(
        "\u041f\u0435\u0440\u0435\u0432\u0456\u0440 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u043f\u0435\u0440\u0435\u0434 \u043f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u0436\u0435\u043d\u043d\u044f\u043c.\n\n"
        f"\U0001f4cb **{row['contract_name']}**\n"
        f"\U0001f4b0 \u0421\u0443\u043c\u0430: **{format_money_dollars(row['price'])} $**\n"
        f"\U0001f465 \u0412\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456: {participant_text}\n"
        f"\U0001f3e6 \u0411\u0443\u043b\u043e \u0432 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457: **{format_cents(row['fomo_cents'] or 0)}**\n"
        f"\U0001f4b8 \u0411\u0443\u043b\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(row['net_cents'] or 0)}**\n\n"
        f"[\u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0438 \u043f\u043e\u0432\u0456\u0434\u043e\u043c\u043b\u0435\u043d\u043d\u044f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443]({jump_url})"
      ),
      color=discord.Color.red(),
    )

    await interaction.response.send_message(
      embed=embed,
      view=AnnulPaidConfirmView(
        bot,
        row["message_id"],
      ),
      ephemeral=True,
    )


  @bot.tree.command(name="pending", description="\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438, \u044f\u043a\u0456 \u0449\u0435 \u043d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0456")
  async def pending(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u043e \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      await interaction.response.send_message("\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.", ephemeral=True)
      return

    rows = db.unpaid_for_guild(guild.id)
    if not rows:
      await interaction.response.send_message("\u2705 \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0431\u0435\u0437 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f \u043d\u0435\u043c\u0430\u0454.", ephemeral=True)
      return

    lines = []
    for row in rows:
      participants = " ".join(f"<@{uid}>" for uid in parse_ids(row["participant_ids"]))
      jump_url = f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}"
      lines.append(
        f"\u2022 **{row['contract_name']}** \u2014 {format_money_dollars(row['price'])} $ \u2014 "
        f"{participants} \u2014 [\u0432\u0456\u0434\u043a\u0440\u0438\u0442\u0438]({jump_url})"
      )

    embed = discord.Embed(
      title="\U0001f9ee \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0418 \u0411\u0415\u0417 \u041d\u0410\u0420\u0410\u0425\u0423\u0412\u0410\u041d\u041d\u042f",
      description="\n".join(lines),
      color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


