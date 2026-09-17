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

def build_public_rating_embed(guild_id: int) -> discord.Embed:
  rows = db.all_non_cancelled(guild_id)
  rating_reset_at = db.get_setting(guild_id, "rating_reset_at")

  rating_rows = [
    row for row in rows
    if not rating_reset_at or row["created_at"] >= rating_reset_at
  ]

  points = defaultdict(lambda: Fraction(0, 1))

  for row in rating_rows:
    members = parse_ids(row["participant_ids"])
    if not members:
      continue

    share = Fraction(10, len(members))
    for uid in members:
      points[uid] += share

  ranking = sorted(
    points,
    key=lambda uid: points[uid],
    reverse=True,
  )

  reset_ts = iso_to_unix(rating_reset_at) if rating_reset_at else None
  subtitle = (
    f"\u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u2022 \u0437 <t:{reset_ts}:d>"
    if reset_ts
    else "\u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u2022 \u0432\u0456\u0434 \u043f\u043e\u0447\u0430\u0442\u043a\u0443"
  )

  embed = discord.Embed(
    title="\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432",
    description=subtitle,
    color=discord.Color.gold(),
  )

  if not ranking:
    embed.add_field(
      name="\u0420\u0435\u0439\u0442\u0438\u043d\u0433",
      value="\u041f\u043e\u043a\u0438 \u043d\u0435\u043c\u0430\u0454 \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432.",
      inline=False,
    )
    return embed

  lines = [
    f"**{idx}.** <@{uid}> \u2014 **{format_points_with_word(points[uid])}**"
    for idx, uid in enumerate(ranking[:25], start=1)
  ]
  embed.add_field(
    name="\u0422\u0430\u0431\u043b\u0438\u0446\u044f",
    value="\n".join(lines),
    inline=False,
  )
  embed.set_footer(text="\u0423 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443 \u043f\u043e\u043a\u0430\u0437\u0443\u044e\u0442\u044c\u0441\u044f \u0442\u0456\u043b\u044c\u043a\u0438 \u0431\u0430\u043b\u0438.")
  return embed


def rating_data_for_guild(guild_id: int):
  rows = db.all_non_cancelled(guild_id)
  rating_reset_at = db.get_setting(guild_id, "rating_reset_at")

  rating_rows = [
    row for row in rows
    if not rating_reset_at or row["created_at"] >= rating_reset_at
  ]

  points = defaultdict(lambda: Fraction(0, 1))
  participations = Counter()

  for row in rating_rows:
    members = parse_ids(row["participant_ids"])
    if not members:
      continue

    share = Fraction(10, len(members))
    for uid in members:
      points[uid] += share
      participations[uid] += 1

  users = sorted(
    points,
    key=lambda uid: (points[uid], participations[uid]),
    reverse=True,
  )
  return points, participations, users, rating_reset_at


def rating_data_for_guild_all_time(guild_id: int):
  rows = db.all_non_cancelled(guild_id)

  points = defaultdict(lambda: Fraction(0, 1))
  participations = Counter()

  for row in rows:
    members = parse_ids(row["participant_ids"])
    if not members:
      continue

    share = Fraction(10, len(members))
    for uid in members:
      points[uid] += share
      participations[uid] += 1

  users = sorted(
    points,
    key=lambda uid: (points[uid], participations[uid]),
    reverse=True,
  )
  return points, participations, users


def get_leaderboard_role_ids(guild_id: int) -> list[int]:
  raw = db.get_setting(guild_id, "leaderboard_role_ids")
  if not raw:
    return []

  try:
    values = json.loads(raw)
  except Exception:
    return []

  result = []
  for value in values:
    try:
      role_id = int(value)
    except Exception:
      continue
    if role_id not in result:
      result.append(role_id)

  return result


async def build_role_leaderboard_embed(
  guild: discord.Guild,
  guild_id: int,
  slot_label: Optional[str] = None,
) -> discord.Embed:
  role_ids = get_leaderboard_role_ids(guild_id)
  points, participations, rating_users, _ = rating_data_for_guild(guild_id)

  embed = discord.Embed(
    title="\U0001f3c6 \u0420\u0415\u0419\u0422\u0418\u041d\u0413 \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0406\u0412 \u2022 \u041e\u0421\u041d\u041e\u0412\u041d\u0418\u0419 \u0421\u041a\u041b\u0410\u0414",
    description=(
      slot_label
      if slot_label
      else "\u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u0441\u0435\u0440\u0435\u0434 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443."
    ),
    color=discord.Color.purple(),
  )

  if not role_ids:
    embed.description = (
      "\u0420\u043e\u043b\u0456 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443 \u0449\u0435 \u043d\u0435 \u043d\u0430\u043b\u0430\u0448\u0442\u043e\u0432\u0430\u043d\u0456.\n"
      "\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0439\u0442\u0435 \u0457\u0445 \u0447\u0435\u0440\u0435\u0437 `/leaderboard-settings`."
    )
    return embed

  valid_role_ids = {
    role_id
    for role_id in role_ids
    if guild.get_role(role_id) is not None
  }

  if not valid_role_ids:
    embed.description = (
      "\u041d\u0430\u043b\u0430\u0448\u0442\u043e\u0432\u0430\u043d\u0456 \u0440\u043e\u043b\u0456 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443 \u0431\u0456\u043b\u044c\u0448\u0435 \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u0456 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.\n"
      "\u041e\u043d\u043e\u0432\u0456\u0442\u044c \u0457\u0445 \u0447\u0435\u0440\u0435\u0437 `/leaderboard-settings`."
    )
    return embed

  filtered = []

  for uid in rating_users:
    member = guild.get_member(uid)

    if member is None:
      member = await fetch_member_safe(guild, uid)

    if member and any(
      role.id in valid_role_ids
      for role in member.roles
    ):
      filtered.append(uid)

  if not filtered:
    embed.add_field(
      name="\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433",
      value="\u0423 \u043f\u043e\u0442\u043e\u0447\u043d\u043e\u043c\u0443 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443 \u043f\u043e\u043a\u0438 \u043d\u0435\u043c\u0430\u0454 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443.",
      inline=False,
    )
    return embed

  lines = [
    (
      f"**{idx}.** <@{uid}> \u2014 "
      f"**{format_points_with_word(points[uid])}** "
      f"\u2022 {participations[uid]} \u0443\u0447\u0430\u0441\u0442\u0435\u0439"
    )
    for idx, uid in enumerate(filtered[:25], start=1)
  ]

  if len(filtered) > 25:
    lines.append(f"\u2026\u0456 \u0449\u0435 {len(filtered) - 25}")

  embed.add_field(
    name="\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433",
    value="\n".join(lines),
    inline=False,
  )

  return embed


class LeaderboardRoleSelect(discord.ui.RoleSelect):
  def __init__(self, guild_id: int):
    self.guild_id = guild_id
    super().__init__(
      placeholder="\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0440\u043e\u043b\u0456 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443",
      min_values=1,
      max_values=10,
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    selected_ids = [
      role.id
      for role in self.values
      if role.id != interaction.guild_id
    ]

    db.set_setting(
      self.guild_id,
      "leaderboard_role_ids",
      json.dumps(selected_ids),
    )

    role_mentions = " ".join(f"<@&{rid}>" for rid in selected_ids)

    await interaction.response.edit_message(
      content=(
        "\u2705 \u0420\u043e\u043b\u0456 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443 \u0437\u0431\u0435\u0440\u0435\u0436\u0435\u043d\u043e.\n"
        f"{role_mentions or '\u2014'}"
      ),
      view=LeaderboardSettingsView(self.guild_id),
    )


class LeaderboardSettingsView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.add_item(LeaderboardRoleSelect(guild_id))

  @discord.ui.button(
    label="\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u0438 \u0440\u043e\u043b\u0456",
    emoji="\U0001f5d1\ufe0f",
    style=discord.ButtonStyle.danger,
    row=1,
  )
  async def clear(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    db.set_setting(
      self.guild_id,
      "leaderboard_role_ids",
      "[]",
    )

    await interaction.response.edit_message(
      content="\u2705 \u0420\u043e\u043b\u0456 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443 \u043e\u0447\u0438\u0449\u0435\u043d\u043e.",
      view=LeaderboardSettingsView(self.guild_id),
    )


def earnings_data_for_guild(guild_id: int):
  rows = db.all_non_cancelled(guild_id)
  paid_rows_all = [r for r in rows if r["status"] == "paid"]
  unpaid_rows = [r for r in rows if r["status"] == "unpaid"]
  payouts_all = db.paid_payouts_for_guild(guild_id)

  reset_at = db.get_setting(guild_id, "earnings_reset_at")

  paid_rows = [
    row for row in paid_rows_all
    if not reset_at
    or (row["paid_at"] and row["paid_at"] >= reset_at)
  ]

  payouts = [
    payout for payout in payouts_all
    if not reset_at or payout["created_at"] >= reset_at
  ]

  member_earnings = Counter()
  for payout in payouts:
    member_earnings[payout["user_id"]] += payout["amount_cents"]

  return {
    "rows": rows,
    "paid_rows_all": paid_rows_all,
    "unpaid_rows": unpaid_rows,
    "paid_rows": paid_rows,
    "payouts": payouts,
    "member_earnings": member_earnings,
    "reset_at": reset_at,
  }


def earnings_data_for_guild_all_time(guild_id: int):
  rows = db.all_non_cancelled(guild_id)
  paid_rows = [r for r in rows if r["status"] == "paid"]
  unpaid_rows = [r for r in rows if r["status"] == "unpaid"]
  payouts = db.paid_payouts_for_guild(guild_id)

  member_earnings = Counter()
  for payout in payouts:
    member_earnings[payout["user_id"]] += payout["amount_cents"]

  return {
    "rows": rows,
    "paid_rows": paid_rows,
    "unpaid_rows": unpaid_rows,
    "payouts": payouts,
    "member_earnings": member_earnings,
  }


def build_my_stats_embed(guild_id: int, user_id: int) -> discord.Embed:
  points, participations, users, rating_reset_at = rating_data_for_guild(guild_id)
  earnings = earnings_data_for_guild(guild_id)

  position = users.index(user_id) + 1 if user_id in users else None
  personal_earnings = earnings["member_earnings"].get(user_id, 0)
  pending_payout = db.pending_accrual_total(guild_id, user_id)

  paid_period_rows = [
    row for row in earnings["paid_rows"]
    if user_id in parse_ids(row["participant_ids"])
  ]
  unpaid_now_rows = [
    row for row in earnings["unpaid_rows"]
    if user_id in parse_ids(row["participant_ids"])
  ]

  rating_ts = iso_to_unix(rating_reset_at) if rating_reset_at else None
  earnings_ts = iso_to_unix(earnings["reset_at"]) if earnings["reset_at"] else None

  period_lines = []
  period_lines.append(
    f"\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: \u0437 <t:{rating_ts}:d>"
    if rating_ts
    else "\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: \u0432\u0456\u0434 \u043f\u043e\u0447\u0430\u0442\u043a\u0443"
  )
  period_lines.append(
    f"\U0001f4b5 \u0424\u0456\u043d\u0430\u043d\u0441\u0438: \u0437 <t:{earnings_ts}:d>"
    if earnings_ts
    else "\U0001f4b5 \u0424\u0456\u043d\u0430\u043d\u0441\u0438: \u0432\u0456\u0434 \u043f\u043e\u0447\u0430\u0442\u043a\u0443"
  )

  embed = discord.Embed(
    title="\U0001f464 \u041c\u043e\u044f \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u2022 \u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434",
    description="\n".join(period_lines),
    color=discord.Color.blurple(),
  )

  embed.add_field(
    name="\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433",
    value=(
      (f"\u041c\u0456\u0441\u0446\u0435: **#{position}**\n" if position else "\u041c\u0456\u0441\u0446\u0435: **\u2014**\n")
      + f"\u0411\u0430\u043b\u0438: **{format_points_with_word(points[user_id])}**\n"
      + f"\u0423\u0447\u0430\u0441\u0442\u0435\u0439: **{participations[user_id]}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4b5 \u0417\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a",
    value=(
      f"\u041e\u0442\u0440\u0438\u043c\u0430\u043d\u043e: **{format_cents(personal_earnings)}**\n"
      f"\u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438: **{format_cents(pending_payout)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(paid_period_rows)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\u23f3 \u0417\u0430\u0440\u0430\u0437",
    value=(
      f"\u041d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(unpaid_now_rows)}**"
    ),
    inline=False,
  )

  return embed


def build_my_history_embed(guild_id: int, user_id: int) -> discord.Embed:
  points, participations, users = rating_data_for_guild_all_time(guild_id)
  earnings = earnings_data_for_guild_all_time(guild_id)

  position = users.index(user_id) + 1 if user_id in users else None

  involved_rows = [
    row for row in earnings["rows"]
    if user_id in parse_ids(row["participant_ids"])
  ]
  paid_rows = [row for row in involved_rows if row["status"] == "paid"]
  unpaid_rows = [row for row in involved_rows if row["status"] == "unpaid"]

  full_family_count = sum(
    1
    for row in paid_rows
    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) == PAYMENT_MODE_LEGACY_FAMILY
  )

  personal_earnings = earnings["member_earnings"].get(user_id, 0)

  embed = discord.Embed(
    title="\U0001f5c2\ufe0f \u041c\u043e\u044f \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u2022 \u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    description="\u0417\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441. \u041e\u0431\u043d\u0443\u043b\u0435\u043d\u043d\u044f \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443 \u0430\u0431\u043e \u0433\u0440\u043e\u0448\u0435\u0439 \u043d\u0430 \u0446\u044e \u0432\u043a\u043b\u0430\u0434\u043a\u0443 \u043d\u0435 \u0432\u043f\u043b\u0438\u0432\u0430\u044e\u0442\u044c.",
    color=discord.Color.dark_teal(),
  )

  embed.add_field(
    name="\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
    value=(
      (f"\u041c\u0456\u0441\u0446\u0435: **#{position}**\n" if position else "\u041c\u0456\u0441\u0446\u0435: **\u2014**\n")
      + f"\u0411\u0430\u043b\u0438: **{format_points_with_word(points[user_id])}**\n"
      + f"\u0423\u0447\u0430\u0441\u0442\u0435\u0439: **{participations[user_id]}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4b5 \u0417\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
    value=(
      f"\u041e\u0442\u0440\u0438\u043c\u0430\u043d\u043e: **{format_cents(personal_earnings)}**\n"
      f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u043f\u043e\u0432\u043d\u0456\u0441\u0442\u044e \u043d\u0430 \u0444\u0430\u043c\u0443: **{full_family_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
    value=(
      f"\u0423\u0447\u0430\u0441\u0442\u0435\u0439: **{len(involved_rows)}**\n"
      f"\u041e\u043f\u043b\u0430\u0447\u0435\u043d\u043e: **{len(paid_rows)}**\n"
      f"\u041d\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u043e \u0437\u0430\u0440\u0430\u0437: **{len(unpaid_rows)}**"
    ),
    inline=False,
  )

  return embed


def personal_daily_rows(guild_id: int, user_id: int):
  earnings = earnings_data_for_guild(guild_id)
  grouped = {}

  for payout in earnings["payouts"]:
    if payout["user_id"] != user_id:
      continue

    day = local_date_from_iso(payout["created_at"])
    if day is None:
      continue

    stat = grouped.setdefault(
      day,
      {
        "day": day,
        "amount": 0,
        "payments": 0,
      },
    )
    stat["amount"] += payout["amount_cents"]
    stat["payments"] += 1

  return sorted(
    grouped.values(),
    key=lambda stat: stat["day"],
    reverse=True,
  )


def build_my_daily_stats_embed(guild_id: int, user_id: int, page: int = 0):
  stats = personal_daily_rows(guild_id, user_id)
  page_size = 7
  total_pages = max(1, (len(stats) + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  slice_rows = stats[page * page_size:(page + 1) * page_size]

  embed = discord.Embed(
    title="\U0001f4c5 \u041c\u0456\u0439 \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043e\u043a \u2022 \u041f\u043e \u0434\u043d\u044f\u0445",
    color=discord.Color.blurple(),
  )

  if not slice_rows:
    embed.description = "\u041f\u043e\u043a\u0438 \u043d\u0435\u043c\u0430\u0454 \u0432\u0438\u043f\u043b\u0430\u0442."
  else:
    lines = [
      f"**{format_day(stat['day'])}** \u2014 {format_cents(stat['amount'])} \u2022 \u0432\u0438\u043f\u043b\u0430\u0442: {stat['payments']}"
      for stat in slice_rows
    ]
    embed.description = "\n".join(lines)

  embed.set_footer(
    text=f"\u0427\u0430\u0441\u043e\u0432\u0430 \u0437\u043e\u043d\u0430: {TIMEZONE_NAME} \u2022 \u0421\u0442\u043e\u0440\u0456\u043d\u043a\u0430 {page + 1}/{total_pages}"
  )
  return embed, page, total_pages


class MyStatsView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    user_id: int,
    mode: str = "general",
    page: int = 0,
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.user_id = user_id
    self.mode = mode

    if mode == "daily":
      _, self.page, self.total_pages = build_my_daily_stats_embed(
        guild_id,
        user_id,
        page,
      )
    else:
      self.page = 0
      self.total_pages = 1

    self.previous.disabled = self.mode != "daily" or self.page <= 0
    self.next_page.disabled = self.mode != "daily" or self.page >= self.total_pages - 1

  @discord.ui.button(
    label="\u041f\u043e\u0442\u043e\u0447\u043d\u0430",
    emoji="\U0001f464",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def general(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_my_stats_embed(self.guild_id, self.user_id),
      view=MyStatsView(self.guild_id, self.user_id, "general"),
    )

  @discord.ui.button(
    label="\u041f\u043e \u0434\u043d\u044f\u0445",
    emoji="\U0001f4c5",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def daily(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    embed, page, _ = build_my_daily_stats_embed(
      self.guild_id,
      self.user_id,
      0,
    )
    await interaction.response.edit_message(
      embed=embed,
      view=MyStatsView(self.guild_id, self.user_id, "daily", page),
    )

  @discord.ui.button(
    label="\u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    emoji="\U0001f5c2\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=0,
  )
  async def history(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_my_history_embed(self.guild_id, self.user_id),
      view=MyStatsView(self.guild_id, self.user_id, "history"),
    )

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    emoji="\u25c0\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def previous(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    embed, page, _ = build_my_daily_stats_embed(
      self.guild_id,
      self.user_id,
      self.page - 1,
    )
    await interaction.response.edit_message(
      embed=embed,
      view=MyStatsView(self.guild_id, self.user_id, "daily", page),
    )

  @discord.ui.button(
    label="\u0414\u0430\u043b\u0456",
    emoji="\u25b6\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def next_page(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    embed, page, _ = build_my_daily_stats_embed(
      self.guild_id,
      self.user_id,
      self.page + 1,
    )
    await interaction.response.edit_message(
      embed=embed,
      view=MyStatsView(self.guild_id, self.user_id, "daily", page),
    )


def build_admin_general_stats_embed(guild_id: int) -> discord.Embed:
  earnings = earnings_data_for_guild(guild_id)
  points, participations, rating_users, rating_reset_at = rating_data_for_guild(guild_id)

  calculated_rows = earnings["paid_rows"]
  uncalculated_rows = earnings["unpaid_rows"]

  gross = sum(row["price"] * 100 for row in calculated_rows)
  family = sum((row["fomo_cents"] or 0) for row in calculated_rows)
  accrued = sum((row["net_cents"] or 0) for row in calculated_rows)
  actually_paid = sum(earnings["member_earnings"].values())
  pending_payouts = db.pending_accrual_total(guild_id)
  uncalculated = sum(row["price"] * 100 for row in uncalculated_rows)

  custom_count = sum(
    1 for row in calculated_rows
    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) in (
      PAYMENT_MODE_REDISTRIBUTE,
      PAYMENT_MODE_FAMILY_SHARE,
    )
  )
  exception_count = sum(
    1 for row in calculated_rows
    if parse_ids(row["excluded_payment_ids"] or "[]")
    and (row["payment_mode"] or PAYMENT_MODE_NORMAL) != PAYMENT_MODE_LEGACY_FAMILY
  )
  full_family_count = sum(
    1 for row in calculated_rows
    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) == PAYMENT_MODE_LEGACY_FAMILY
  )

  avg_contract = gross // len(calculated_rows) if calculated_rows else 0

  rating_rows = [
    row for row in earnings["rows"]
    if not rating_reset_at or row["created_at"] >= rating_reset_at
  ]
  avg_team = (
    sum(len(parse_ids(row["participant_ids"])) for row in rating_rows) / len(rating_rows)
    if rating_rows else 0
  )

  active_users = set(rating_users)

  rating_ts = iso_to_unix(rating_reset_at) if rating_reset_at else None
  earnings_ts = iso_to_unix(earnings["reset_at"]) if earnings["reset_at"] else None

  description_lines = [
    (
      f"\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: \u0437 <t:{rating_ts}:d>"
      if rating_ts
      else "\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: \u0432\u0456\u0434 \u043f\u043e\u0447\u0430\u0442\u043a\u0443"
    ),
    (
      f"\U0001f4b5 \u0424\u0456\u043d\u0430\u043d\u0441\u0438: \u0437 <t:{earnings_ts}:d>"
      if earnings_ts
      else "\U0001f4b5 \u0424\u0456\u043d\u0430\u043d\u0441\u0438: \u0432\u0456\u0434 \u043f\u043e\u0447\u0430\u0442\u043a\u0443"
    ),
  ]

  embed = discord.Embed(
    title="\U0001f4ca \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u2022 \u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434",
    description="\n".join(description_lines),
    color=discord.Color.blurple(),
  )

  embed.add_field(
    name="\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438",
    value=(
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0432 \u043f\u0435\u0440\u0456\u043e\u0434\u0456: **{len(calculated_rows)}**\n"
      f"\u041d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0437\u0430\u0440\u0430\u0437: **{len(uncalculated_rows)}**\n"
      f"\u041f\u043e\u0432\u043d\u0456\u0441\u0442\u044e \u043d\u0430 \u0444\u0430\u043c\u0443: **{full_family_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f465 \u0410\u043a\u0442\u0438\u0432\u043d\u0456\u0441\u0442\u044c",
    value=(
      f"\u0423\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432 \u0443 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443: **{len(active_users)}**\n"
      f"\u0423\u0447\u0430\u0441\u0442\u0435\u0439 \u0443 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443: **{sum(participations.values())}**\n"
      f"\u0421\u0435\u0440\u0435\u0434\u043d\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430: **{avg_team:.1f}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c \u0437 \u0432\u0438\u043d\u044f\u0442\u043a\u0430\u043c\u0438: **{exception_count}**\n"
      f"\u041d\u0430\u043b\u0430\u0448\u0442\u043e\u0432\u0430\u043d\u0438\u0445 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u044c: **{custom_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4b0 \u0424\u0456\u043d\u0430\u043d\u0441\u0438",
    value=(
      f"\u0417\u0430\u0433\u0430\u043b\u043e\u043c \u043f\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430\u0445: **{format_cents(gross)}**\n"
      f"\u041d\u0430 \u0444\u0430\u043c\u0443: **{format_cents(family)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(accrued)}**\n"
      f"\u0424\u0430\u043a\u0442\u0438\u0447\u043d\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e: **{format_cents(actually_paid)}**\n"
      f"\u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u0437\u0430\u0440\u0430\u0437: **{format_cents(pending_payouts)}**\n"
      f"\u041d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u043d\u0430: **{format_cents(uncalculated)}**\n"
      f"\u0421\u0435\u0440\u0435\u0434\u043d\u0456\u0439 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442: **{format_cents(avg_contract)}**"
    ),
    inline=False,
  )

  return embed


def build_admin_history_embed(guild_id: int) -> discord.Embed:
  all_rows = db.all_for_guild(guild_id)
  valid_rows = [
    row for row in all_rows
    if row["status"] not in ("cancelled", "annulled")
  ]
  cancelled = [row for row in all_rows if row["status"] == "cancelled"]
  annulled = [row for row in all_rows if row["status"] == "annulled"]

  earnings = earnings_data_for_guild_all_time(guild_id)
  points, participations, rating_users = rating_data_for_guild_all_time(guild_id)

  paid_rows = earnings["paid_rows"]
  unpaid_rows = earnings["unpaid_rows"]

  gross = sum(row["price"] * 100 for row in paid_rows)
  family = sum((row["fomo_cents"] or 0) for row in paid_rows)
  members = sum((row["net_cents"] or 0) for row in paid_rows)
  actually_paid = sum(earnings["member_earnings"].values())
  pending_payouts = db.pending_accrual_total(guild_id)
  unpaid = sum(row["price"] * 100 for row in unpaid_rows)

  exception_count = sum(
    1 for row in paid_rows
    if parse_ids(row["excluded_payment_ids"] or "[]")
    and (row["payment_mode"] or PAYMENT_MODE_NORMAL) != PAYMENT_MODE_LEGACY_FAMILY
  )
  full_family_count = sum(
    1 for row in paid_rows
    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) == PAYMENT_MODE_LEGACY_FAMILY
  )

  active_users = {
    uid
    for row in valid_rows
    for uid in parse_ids(row["participant_ids"])
  }

  avg_team = (
    sum(len(parse_ids(row["participant_ids"])) for row in valid_rows) / len(valid_rows)
    if valid_rows else 0
  )

  # Top contracts by all-time gross.
  contract_grouped = {}
  for row in paid_rows:
    stat = contract_grouped.setdefault(
      row["contract_name"],
      {"gross": 0, "count": 0},
    )
    stat["gross"] += row["price"] * 100
    stat["count"] += 1

  top_contracts = sorted(
    contract_grouped.items(),
    key=lambda item: item[1]["gross"],
    reverse=True,
  )[:5]

  embed = discord.Embed(
    title="\U0001f5c2\ufe0f \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u2022 \u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    description="\u0417\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441. \u041e\u0431\u043d\u0443\u043b\u0435\u043d\u043d\u044f \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443 \u0442\u0430 \u0433\u0440\u043e\u0448\u0435\u0439 \u0446\u0456 \u0434\u0430\u043d\u0456 \u043d\u0435 \u0441\u0442\u0438\u0440\u0430\u044e\u0442\u044c.",
    color=discord.Color.dark_teal(),
  )

  embed.add_field(
    name="\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
    value=(
      f"\u0412\u0441\u044c\u043e\u0433\u043e \u0434\u0456\u0439\u0441\u043d\u0438\u0445: **{len(valid_rows)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{len(paid_rows)}**\n"
      f"\u041d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0437\u0430\u0440\u0430\u0437: **{len(unpaid_rows)}**\n"
      f"\u0421\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e: **{len(cancelled)}**\n"
      f"\u0410\u043d\u0443\u043b\u044c\u043e\u0432\u0430\u043d\u043e \u043f\u0456\u0441\u043b\u044f \u043e\u043f\u043b\u0430\u0442\u0438: **{len(annulled)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f465 \u0410\u043a\u0442\u0438\u0432\u043d\u0456\u0441\u0442\u044c \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
    value=(
      f"\u0423\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432: **{len(active_users)}**\n"
      f"\u0423\u0447\u0430\u0441\u0442\u0435\u0439: **{sum(participations.values())}**\n"
      f"\u0421\u0435\u0440\u0435\u0434\u043d\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430: **{avg_team:.1f}**\n"
      f"\u041e\u043f\u043b\u0430\u0442 \u0437 \u0432\u0438\u043d\u044f\u0442\u043a\u0430\u043c\u0438: **{exception_count}**\n"
      f"\u041f\u043e\u0432\u043d\u0456\u0441\u0442\u044e \u043d\u0430 \u0444\u0430\u043c\u0443: **{full_family_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4b0 \u0424\u0456\u043d\u0430\u043d\u0441\u0438 \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
    value=(
      f"\u0417\u0430\u0433\u0430\u043b\u043e\u043c \u043f\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430\u0445: **{format_cents(gross)}**\n"
      f"\u041d\u0430 \u0444\u0430\u043c\u0443: **{format_cents(family)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(members)}**\n"
      f"\u0424\u0430\u043a\u0442\u0438\u0447\u043d\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e: **{format_cents(actually_paid)}**\n"
      f"\u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u0437\u0430\u0440\u0430\u0437: **{format_cents(pending_payouts)}**\n"
      f"\u041d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u043d\u0430: **{format_cents(unpaid)}**"
    ),
    inline=False,
  )

  if rating_users:
    rating_lines = [
      f"**{idx}.** <@{uid}> \u2014 **{format_points_with_word(points[uid])}**"
      for idx, uid in enumerate(rating_users[:5], start=1)
    ]
    embed.add_field(
      name="\U0001f3c6 \u0422\u043e\u043f \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443 \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
      value="\n".join(rating_lines),
      inline=False,
    )

  if top_contracts:
    contract_lines = [
      (
        f"**{idx}. {name}** \u2014 {format_cents(stat['gross'])} "
        f"\u2022 {stat['count']} \u0440\u0430\u0437(\u0438)"
      )
      for idx, (name, stat) in enumerate(top_contracts, start=1)
    ]
    embed.add_field(
      name="\U0001f4cb \u0422\u043e\u043f \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0437\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
      value="\n".join(contract_lines),
      inline=False,
    )

  return embed


def contract_stats_rows(guild_id: int):
  earnings = earnings_data_for_guild(guild_id)
  grouped = {}

  for row in earnings["paid_rows"]:
    key = row["contract_name"]
    stat = grouped.setdefault(
      key,
      {
        "name": key,
        "count": 0,
        "gross": 0,
        "family": 0,
        "members": 0,
        "exceptions": 0,
      },
    )
    stat["count"] += 1
    stat["gross"] += row["price"] * 100
    stat["family"] += row["fomo_cents"] or 0
    stat["members"] += row["net_cents"] or 0
    if parse_ids(row["excluded_payment_ids"] or "[]"):
      stat["exceptions"] += 1

  return sorted(
    grouped.values(),
    key=lambda x: (-x["gross"], ukrainian_sort_key(x["name"])),
  )


def build_contract_stats_embed(guild_id: int, page: int = 0):
  stats = contract_stats_rows(guild_id)
  page_size = 6
  total_pages = max(1, (len(stats) + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  slice_rows = stats[page * page_size:(page + 1) * page_size]

  embed = discord.Embed(
    title="\U0001f4cb \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u2022 \u041f\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430\u0445",
    description=(
      "\u0414\u043b\u044f \u043a\u043e\u0436\u043d\u043e\u0433\u043e \u0442\u0438\u043f\u0443: \u0437\u0430\u0433\u0430\u043b\u044c\u043d\u0438\u0439 \u043e\u0431\u043e\u0440\u043e\u0442 \u0456 \u0441\u043a\u0456\u043b\u044c\u043a\u0438 \u0437 \u043d\u044c\u043e\u0433\u043e \u043f\u0456\u0448\u043b\u043e \u043d\u0430 \u0444\u0430\u043c\u0443."
    ),
    color=discord.Color.blurple(),
  )

  if not slice_rows:
    embed.description = "\u0429\u0435 \u043d\u0435\u043c\u0430\u0454 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432."
  else:
    for stat in slice_rows:
      embed.add_field(
        name=stat["name"],
        value=(
          f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{stat['count']}**\n"
          f"\u0417\u0430\u0433\u0430\u043b\u043e\u043c: **{format_cents(stat['gross'])}**\n"
          f"\u041d\u0430 \u0444\u0430\u043c\u0443: **{format_cents(stat['family'])}**\n"
          f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(stat['members'])}**\n"
          f"\u0417 \u0432\u0438\u043d\u044f\u0442\u043a\u0430\u043c\u0438: **{stat['exceptions']}**"
        ),
        inline=True,
      )

  embed.set_footer(text=f"\u0421\u0442\u043e\u0440\u0456\u043d\u043a\u0430 {page + 1}/{total_pages}")
  return embed, page, total_pages


def build_member_stats_embed(
  guild_id: int,
  user_id: Optional[int] = None,
) -> discord.Embed:
  if user_id is None:
    points, participations, rating_users, _ = rating_data_for_guild(guild_id)
    earnings = earnings_data_for_guild(guild_id)

    embed = discord.Embed(
      title="\U0001f465 \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u2022 \u0423\u0447\u0430\u0441\u043d\u0438\u043a\u0438",
      description=(
        "\u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u0456 \u0442\u043e\u043f \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043a\u0443.\n"
        "\u0414\u043b\u044f \u0434\u0435\u0442\u0430\u043b\u044c\u043d\u043e\u0457 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0438 \u043e\u0431\u0435\u0440\u0456\u0442\u044c \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u0443 \u043b\u044e\u0434\u0438\u043d\u0443 \u0437\u0456 \u0441\u043f\u0438\u0441\u043a\u0443 \u043d\u0438\u0436\u0447\u0435."
      ),
      color=discord.Color.blurple(),
    )

    if rating_users:
      rating_lines = [
        (
          f"**{idx}.** <@{uid}> \u2014 "
          f"**{format_points_with_word(points[uid])}** "
          f"\u2022 {participations[uid]} \u0443\u0447\u0430\u0441\u0442\u0435\u0439"
        )
        for idx, uid in enumerate(rating_users[:10], start=1)
      ]
      embed.add_field(
        name="\U0001f3c6 \u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433",
        value="\n".join(rating_lines),
        inline=False,
      )
    else:
      embed.add_field(
        name="\U0001f3c6 \u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433",
        value="\u041f\u043e\u043a\u0438 \u043d\u0435\u043c\u0430\u0454 \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0443 \u043f\u043e\u0442\u043e\u0447\u043d\u043e\u043c\u0443 \u043f\u0435\u0440\u0456\u043e\u0434\u0456.",
        inline=False,
      )

    earning_users = sorted(
      earnings["member_earnings"],
      key=lambda uid: earnings["member_earnings"][uid],
      reverse=True,
    )

    if earning_users:
      earning_lines = [
        (
          f"**{idx}.** <@{uid}> \u2014 "
          f"**{format_cents(earnings['member_earnings'][uid])}**"
        )
        for idx, uid in enumerate(earning_users[:5], start=1)
      ]
      embed.add_field(
        name="\U0001f4b5 \u0422\u043e\u043f-5 \u043f\u043e \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043a\u0443",
        value="\n".join(earning_lines),
        inline=False,
      )
    else:
      embed.add_field(
        name="\U0001f4b5 \u0422\u043e\u043f-5 \u043f\u043e \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043a\u0443",
        value="\u041f\u043e\u043a\u0438 \u043d\u0435\u043c\u0430\u0454 \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e\u0433\u043e \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043a\u0443 \u0432 \u043f\u043e\u0442\u043e\u0447\u043d\u043e\u043c\u0443 \u043f\u0435\u0440\u0456\u043e\u0434\u0456.",
        inline=False,
      )

    return embed

  points, participations, rating_users, _ = rating_data_for_guild(guild_id)
  earnings = earnings_data_for_guild(guild_id)

  all_points, all_participations, all_users = rating_data_for_guild_all_time(guild_id)
  all_earnings = earnings_data_for_guild_all_time(guild_id)

  position = rating_users.index(user_id) + 1 if user_id in rating_users else None
  all_position = all_users.index(user_id) + 1 if user_id in all_users else None

  current_received = earnings["member_earnings"].get(user_id, 0)
  all_received = all_earnings["member_earnings"].get(user_id, 0)

  pending_payout = db.pending_accrual_total(guild_id, user_id)

  current_family_contribution = db.family_contribution_for_user(
    guild_id,
    user_id,
    earnings["reset_at"],
  )
  all_family_contribution = db.family_contribution_for_user(
    guild_id,
    user_id,
    None,
  )

  unpaid_now = [
    row for row in earnings["unpaid_rows"]
    if user_id in parse_ids(row["participant_ids"])
  ]
  paid_period = [
    row for row in earnings["paid_rows"]
    if user_id in parse_ids(row["participant_ids"])
  ]

  embed = discord.Embed(
    title="\U0001f464 \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430",
    description=f"<@{user_id}>",
    color=discord.Color.blurple(),
  )

  embed.add_field(
    name="\U0001f3c6 \u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433",
    value=(
      (f"\u041c\u0456\u0441\u0446\u0435: **#{position}**\n" if position else "\u041c\u0456\u0441\u0446\u0435: **\u2014**\n")
      + f"\u0411\u0430\u043b\u0438: **{format_points_with_word(points[user_id])}**\n"
      + f"\u0423\u0447\u0430\u0441\u0442\u0435\u0439: **{participations[user_id]}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4b5 \u041f\u043e\u0442\u043e\u0447\u043d\u0456 \u0444\u0456\u043d\u0430\u043d\u0441\u0438",
    value=(
      f"\u041e\u0442\u0440\u0438\u043c\u0430\u043d\u043e: **{format_cents(current_received)}**\n"
      f"\u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438: **{format_cents(pending_payout)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(paid_period)}**\n"
      f"\u041d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(unpaid_now)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f3e6 \u041e\u0441\u043e\u0431\u0438\u0441\u0442\u0438\u0439 \u0432\u043d\u0435\u0441\u043e\u043a \u0443 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457",
    value=(
      f"\u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434: **{format_cents(current_family_contribution)}**\n"
      f"\u0417\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441: **{format_cents(all_family_contribution)}**"
    ),
    inline=False,
  )

  embed.add_field(
    name="\U0001f5c2\ufe0f \u0417\u0430 \u0432\u0435\u0441\u044c \u0447\u0430\u0441",
    value=(
      (f"\u041c\u0456\u0441\u0446\u0435: **#{all_position}**\n" if all_position else "\u041c\u0456\u0441\u0446\u0435: **\u2014**\n")
      + f"\u0411\u0430\u043b\u0438: **{format_points_with_word(all_points[user_id])}**\n"
      + f"\u0423\u0447\u0430\u0441\u0442\u0435\u0439: **{all_participations[user_id]}**\n"
      + f"\u041e\u0442\u0440\u0438\u043c\u0430\u043d\u043e: **{format_cents(all_received)}**"
    ),
    inline=False,
  )

  return embed


def member_stats_page(guild_id: int, page: int, page_size: int = 25):
  user_ids = db.participant_ids_for_guild(guild_id)
  total_pages = max(1, (len(user_ids) + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  return user_ids[page * page_size:(page + 1) * page_size], page, total_pages


async def member_labels(
  guild: discord.Guild,
  user_ids: list[int],
) -> dict[int, str]:
  labels = {}

  for uid in user_ids:
    member = guild.get_member(uid)

    if member is None:
      member = await fetch_member_safe(guild, uid)

    labels[uid] = member.display_name if member else f"ID {uid}"

  return labels


class MemberStatsSelect(discord.ui.Select):
  def __init__(
    self,
    guild_id: int,
    page: int,
    user_ids: list[int],
    labels: dict[int, str],
  ):
    self.guild_id = guild_id
    self.page = page

    options = [
      discord.SelectOption(
        label=labels.get(uid, f"ID {uid}")[:100],
        value=str(uid),
        description=f"\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430 \u2022 {uid}"[:100],
      )
      for uid in user_ids
    ]

    if not options:
      options = [
        discord.SelectOption(
          label="\u0423\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432 \u0449\u0435 \u043d\u0435\u043c\u0430\u0454",
          value="none",
        )
      ]

    super().__init__(
      placeholder="\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430",
      min_values=1,
      max_values=1,
      options=options,
      disabled=(options[0].value == "none"),
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    await interaction.response.defer()

    uid = int(self.values[0])
    view = await build_member_stats_picker_view(
      interaction.guild,
      self.guild_id,
      self.page,
    )
    await interaction.edit_original_response(
      embed=build_member_stats_embed(self.guild_id, uid),
      view=view,
    )


class MemberStatsPickerView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    page: int,
    total_pages: int,
    user_ids: list[int],
    labels: dict[int, str],
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.page = page
    self.total_pages = total_pages

    self.add_item(
      MemberStatsSelect(
        guild_id,
        page,
        user_ids,
        labels,
      )
    )

    self.previous.disabled = page <= 0
    self.page_label.label = f"{page + 1}/{total_pages}"
    self.next_page.disabled = page >= total_pages - 1

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    emoji="\u25c0\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def previous(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.defer()

    view = await build_member_stats_picker_view(
      interaction.guild,
      self.guild_id,
      self.page - 1,
    )
    await interaction.edit_original_response(
      embed=build_member_stats_embed(self.guild_id),
      view=view,
    )

  @discord.ui.button(
    label="1/1",
    style=discord.ButtonStyle.secondary,
    disabled=True,
    row=1,
  )
  async def page_label(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    pass

  @discord.ui.button(
    label="\u0414\u0430\u043b\u0456",
    emoji="\u25b6\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def next_page(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.defer()

    view = await build_member_stats_picker_view(
      interaction.guild,
      self.guild_id,
      self.page + 1,
    )
    await interaction.edit_original_response(
      embed=build_member_stats_embed(self.guild_id),
      view=view,
    )

  @discord.ui.button(
    label="\u0414\u043e \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0438",
    emoji="\u21a9\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=2,
  )
  async def back(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_admin_general_stats_embed(self.guild_id),
      view=AdminStatsView(self.guild_id, "general"),
    )


async def build_member_stats_picker_view(
  guild: Optional[discord.Guild],
  guild_id: int,
  page: int = 0,
):
  user_ids, page, total_pages = member_stats_page(guild_id, page)

  if guild is not None:
    labels = await member_labels(guild, user_ids)
  else:
    labels = {uid: f"ID {uid}" for uid in user_ids}

  return MemberStatsPickerView(
    guild_id,
    page,
    total_pages,
    user_ids,
    labels,
  )


def parse_period_date(raw: str):
  value = raw.strip()

  for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
    try:
      return datetime.strptime(value, fmt).date()
    except ValueError:
      pass

  raise ValueError("\u0412\u043a\u0430\u0436\u0456\u0442\u044c \u0434\u0430\u0442\u0443 \u0443 \u0444\u043e\u0440\u043c\u0430\u0442\u0456 \u0414\u0414.\u041c\u041c.\u0420\u0420\u0420\u0420.")


def date_in_period(
  iso_value: Optional[str],
  start_day,
  end_day,
) -> bool:
  day = local_date_from_iso(iso_value)
  return bool(day and start_day <= day <= end_day)


def build_period_stats_embed(
  guild_id: int,
  start_day,
  end_day,
) -> discord.Embed:
  rows = db.all_non_cancelled(guild_id)

  contract_rows = [
    row for row in rows
    if date_in_period(
      row["created_at"],
      start_day,
      end_day,
    )
  ]

  calculated_rows = [
    row for row in rows
    if row["status"] == "paid"
    and date_in_period(
      row["paid_at"],
      start_day,
      end_day,
    )
  ]

  gross = sum(row["price"] * 100 for row in calculated_rows)
  family = sum((row["fomo_cents"] or 0) for row in calculated_rows)
  accrued = sum((row["net_cents"] or 0) for row in calculated_rows)

  payouts = [
    row for row in db.paid_payouts_for_guild(guild_id)
    if date_in_period(
      row["created_at"],
      start_day,
      end_day,
    )
  ]
  actually_paid = sum(row["amount_cents"] for row in payouts)

  personal_family = sum(
    row["amount_cents"] or 0
    for row in db.family_contributions_for_guild(guild_id)
    if date_in_period(
      row["created_at"],
      start_day,
      end_day,
    )
  )

  pending_from_period = sum(
    row["amount_cents"] or 0
    for row in db.accruals_for_guild(guild_id)
    if row["status"] == "pending"
    and date_in_period(
      row["created_at"],
      start_day,
      end_day,
    )
  )

  rating_points = defaultdict(lambda: Fraction(0, 1))
  participations = Counter()

  for row in contract_rows:
    members = parse_ids(row["participant_ids"])
    if not members:
      continue

    share = Fraction(10, len(members))

    for uid in members:
      rating_points[uid] += share
      participations[uid] += 1

  rating_users = sorted(
    rating_points,
    key=lambda uid: (
      rating_points[uid],
      participations[uid],
    ),
    reverse=True,
  )

  earning_counter = Counter()
  for payout in payouts:
    earning_counter[payout["user_id"]] += payout["amount_cents"]

  unique_users = {
    uid
    for row in contract_rows
    for uid in parse_ids(row["participant_ids"])
  }

  embed = discord.Embed(
    title="\U0001f4c6 \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0437\u0430 \u043f\u0435\u0440\u0456\u043e\u0434",
    description=(
      f"**{start_day.strftime('%d.%m.%Y')} \u2014 "
      f"{end_day.strftime('%d.%m.%Y')}**"
    ),
    color=discord.Color.blurple(),
  )

  embed.add_field(
    name="\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438",
    value=(
      f"\u0412\u0438\u043a\u043e\u043d\u0430\u043d\u043e: **{len(contract_rows)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{len(calculated_rows)}**\n"
      f"\u0423\u043d\u0456\u043a\u0430\u043b\u044c\u043d\u0438\u0445 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432: **{len(unique_users)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4b0 \u0424\u0456\u043d\u0430\u043d\u0441\u0438",
    value=(
      f"\u0417\u0430\u0433\u0430\u043b\u044c\u043d\u0430 \u0441\u0443\u043c\u0430: **{format_cents(gross)}**\n"
      f"\u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457: **{format_cents(family)}**\n"
      f"\u041e\u0441\u043e\u0431\u0438\u0441\u0442\u0438\u0445 \u0432\u043d\u0435\u0441\u043a\u0456\u0432 \u0443 \u0411\u0430\u043d\u043a: **{format_cents(personal_family)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(accrued)}**\n"
      f"\u0424\u0430\u043a\u0442\u0438\u0447\u043d\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e: **{format_cents(actually_paid)}**\n"
      f"\u0417 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u043f\u0435\u0440\u0456\u043e\u0434\u0443 \u0449\u0435 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438: **{format_cents(pending_from_period)}**"
    ),
    inline=True,
  )

  if rating_users:
    rating_lines = [
      (
        f"**{idx}.** <@{uid}> \u2014 "
        f"**{format_points_with_word(rating_points[uid])}** "
        f"\u2022 {participations[uid]} \u0443\u0447\u0430\u0441\u0442\u0435\u0439"
      )
      for idx, uid in enumerate(rating_users[:10], start=1)
    ]

    embed.add_field(
      name="\U0001f3c6 \u0420\u0435\u0439\u0442\u0438\u043d\u0433 \u0437\u0430 \u043f\u0435\u0440\u0456\u043e\u0434",
      value="\n".join(rating_lines),
      inline=False,
    )

  top_earnings = earning_counter.most_common(5)

  if top_earnings:
    earning_lines = [
      f"**{idx}.** <@{uid}> \u2014 **{format_cents(amount)}**"
      for idx, (uid, amount) in enumerate(top_earnings, start=1)
    ]

    embed.add_field(
      name="\U0001f4b5 \u0422\u043e\u043f-5 \u0444\u0430\u043a\u0442\u0438\u0447\u043d\u043e\u0433\u043e \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043a\u0443",
      value="\n".join(earning_lines),
      inline=False,
    )

  embed.set_footer(
    text=f"\u0427\u0430\u0441\u043e\u0432\u0430 \u0437\u043e\u043d\u0430: {TIMEZONE_NAME}"
  )
  return embed


class PeriodStatsModal(discord.ui.Modal, title="\U0001f4c6 \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430: \u0434\u0430\u0442\u0430 / \u043f\u0435\u0440\u0456\u043e\u0434"):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id

    self.start_input = discord.ui.TextInput(
      label="\u0414\u0430\u0442\u0430 \u0432\u0456\u0434",
      placeholder="01.09.2026",
      required=True,
      max_length=10,
    )
    self.end_input = discord.ui.TextInput(
      label="\u0414\u0430\u0442\u0430 \u0434\u043e (\u0434\u043b\u044f 1 \u0434\u043d\u044f \u2014 \u0442\u0430 \u0441\u0430\u043c\u0430)",
      placeholder="15.09.2026",
      required=True,
      max_length=10,
    )

    self.add_item(self.start_input)
    self.add_item(self.end_input)

  async def on_submit(self, interaction: discord.Interaction):
    try:
      start_day = parse_period_date(str(self.start_input))
      end_day = parse_period_date(str(self.end_input))
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )
      return

    if end_day < start_day:
      await interaction.response.send_message(
        "\u274c \u0414\u0430\u0442\u0430 \u00ab\u0434\u043e\u00bb \u043d\u0435 \u043c\u043e\u0436\u0435 \u0431\u0443\u0442\u0438 \u0440\u0430\u043d\u0456\u0448\u0435 \u0434\u0430\u0442\u0438 \u00ab\u0432\u0456\u0434\u00bb.",
        ephemeral=True,
      )
      return

    if (end_day - start_day).days > 3660:
      await interaction.response.send_message(
        "\u274c \u0417\u0430\u043d\u0430\u0434\u0442\u043e \u0432\u0435\u043b\u0438\u043a\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      embed=build_period_stats_embed(
        self.guild_id,
        start_day,
        end_day,
      ),
      ephemeral=True,
    )


class AdminStatsView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    mode: str = "general",
    page: int = 0,
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.mode = mode

    if mode == "contracts":
      _, self.page, self.total_pages = build_contract_stats_embed(
        guild_id,
        page,
      )
    else:
      self.page = 0
      self.total_pages = 1

    self.previous.disabled = self.total_pages <= 1 or self.page <= 0
    self.next_page.disabled = self.total_pages <= 1 or self.page >= self.total_pages - 1

  @discord.ui.button(
    label="\u041f\u043e\u0442\u043e\u0447\u043d\u0430",
    emoji="\U0001f4ca",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def general(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_admin_general_stats_embed(self.guild_id),
      view=AdminStatsView(self.guild_id, "general"),
    )

  @discord.ui.button(
    label="\u041f\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430\u0445",
    emoji="\U0001f4cb",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def contracts(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    embed, page, _ = build_contract_stats_embed(self.guild_id, 0)
    await interaction.response.edit_message(
      embed=embed,
      view=AdminStatsView(self.guild_id, "contracts", page),
    )

  @discord.ui.button(
    label="\u0423\u0447\u0430\u0441\u043d\u0438\u043a\u0438",
    emoji="\U0001f465",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def members(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.defer()

    view = await build_member_stats_picker_view(
      interaction.guild,
      self.guild_id,
      0,
    )
    await interaction.edit_original_response(
      embed=build_member_stats_embed(self.guild_id),
      view=view,
    )

  @discord.ui.button(
    label="\u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    emoji="\U0001f5c2\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=0,
  )
  async def history(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_admin_history_embed(self.guild_id),
      view=AdminStatsView(self.guild_id, "history"),
    )

  @discord.ui.button(
    label="\u041e\u0441\u043d\u043e\u0432\u043d\u0438\u0439 \u0441\u043a\u043b\u0430\u0434",
    emoji="\U0001f396",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def roles(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if interaction.guild is None:
      await interaction.response.send_message(
        "\u274c \u0421\u0435\u0440\u0432\u0435\u0440 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0438\u0439.",
        ephemeral=True,
      )
      return

    await interaction.response.defer()

    embed = await build_role_leaderboard_embed(
      interaction.guild,
      self.guild_id,
    )

    await interaction.edit_original_response(
      embed=embed,
      view=AdminStatsView(self.guild_id, "roles"),
    )

  @discord.ui.button(
    label="\u0414\u0430\u0442\u0430 / \u043f\u0435\u0440\u0456\u043e\u0434",
    emoji="\U0001f4c6",
    style=discord.ButtonStyle.secondary,
    row=0,
  )
  async def period(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      PeriodStatsModal(self.guild_id)
    )

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    emoji="\u25c0\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def previous(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if self.mode == "contracts":
      embed, page, _ = build_contract_stats_embed(
        self.guild_id,
        self.page - 1,
      )
    else:
      await interaction.response.defer()
      return

    await interaction.response.edit_message(
      embed=embed,
      view=AdminStatsView(self.guild_id, self.mode, page),
    )

  @discord.ui.button(
    label="\u0414\u0430\u043b\u0456",
    emoji="\u25b6\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def next_page(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if self.mode == "contracts":
      embed, page, _ = build_contract_stats_embed(
        self.guild_id,
        self.page + 1,
      )
    else:
      await interaction.response.defer()
      return

    await interaction.response.edit_message(
      embed=embed,
      view=AdminStatsView(self.guild_id, self.mode, page),
    )


def chunk_lines(
  lines: list[str],
  max_lines: int = 20,
  max_chars: int = 3500,
) -> list[list[str]]:
  chunks = []
  current = []
  current_chars = 0

  for line in lines:
    projected = current_chars + len(line) + 1
    if current and (
      len(current) >= max_lines
      or projected > max_chars
    ):
      chunks.append(current)
      current = []
      current_chars = 0

    current.append(line)
    current_chars += len(line) + 1

  if current:
    chunks.append(current)

  return chunks


def build_auto_rating_embeds(
  guild_id: int,
  slot_label: str,
) -> list[discord.Embed]:
  points, participations, users, _ = rating_data_for_guild(guild_id)

  if not users:
    return [
      discord.Embed(
        title="\U0001f3c6 \u0420\u0415\u0419\u0422\u0418\u041d\u0413 \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0406\u0412",
        description=(
          f"**{slot_label}**\n\n"
          "\u0423 \u043f\u043e\u0442\u043e\u0447\u043d\u043e\u043c\u0443 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u043e\u0432\u043e\u043c\u0443 \u043f\u0435\u0440\u0456\u043e\u0434\u0456 \u0449\u0435 \u043d\u0435\u043c\u0430\u0454 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432."
        ),
        color=discord.Color.gold(),
      )
    ]

  lines = [
    (
      f"**{idx}.** <@{uid}> \u2014 "
      f"**{format_points_with_word(points[uid])}** "
      f"\u2022 {participations[uid]} \u0443\u0447\u0430\u0441\u0442\u0435\u0439"
    )
    for idx, uid in enumerate(users, start=1)
    if points[uid] > 0
  ]

  chunks = chunk_lines(lines, max_lines=40, max_chars=3800)
  total_participations = sum(participations[uid] for uid in users)

  embeds = []
  for idx, chunk in enumerate(chunks, start=1):
    embed = discord.Embed(
      title=(
        "\U0001f3c6 \u0420\u0415\u0419\u0422\u0418\u041d\u0413 \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0406\u0412"
        if idx == 1
        else "\U0001f3c6 \u0420\u0415\u0419\u0422\u0418\u041d\u0413 \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0406\u0412 \u2022 \u043f\u0440\u043e\u0434\u043e\u0432\u0436\u0435\u043d\u043d\u044f"
      ),
      description="\n".join(chunk),
      color=discord.Color.gold(),
    )

    if idx == 1:
      embed.add_field(
        name="\u041f\u0435\u0440\u0456\u043e\u0434",
        value=slot_label,
        inline=False,
      )

    embed.set_footer(
      text=(
        f"\u0423 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443: {len(lines)} \u2022 "
        f"\u0412\u0441\u044c\u043e\u0433\u043e \u0443\u0447\u0430\u0441\u0442\u0435\u0439: {total_participations} \u2022 "
        f"\u0421\u0442\u043e\u0440\u0456\u043d\u043a\u0430 {idx}/{len(chunks)}"
      )
    )
    embeds.append(embed)

  return embeds


def build_family_daily_embed(
  guild_id: int,
  target_day,
) -> discord.Embed:
  rows = db.all_non_cancelled(guild_id)

  completed_today = [
    row for row in rows
    if local_date_from_iso(row["created_at"]) == target_day
  ]

  calculated_today = [
    row for row in rows
    if row["status"] == "paid"
    and local_date_from_iso(row["paid_at"]) == target_day
  ]

  gross_cents = sum((row["price"] or 0) * 100 for row in calculated_today)
  family_cents = sum(row["fomo_cents"] or 0 for row in calculated_today)
  accrued_cents = sum(row["net_cents"] or 0 for row in calculated_today)

  participant_counter = Counter()
  for row in completed_today:
    for uid in parse_ids(row["participant_ids"]):
      participant_counter[uid] += 1

  unique_participants = len(participant_counter)
  participations_total = sum(participant_counter.values())

  all_payouts = db.paid_payouts_for_guild(guild_id)
  payouts_today = [
    row for row in all_payouts
    if local_date_from_iso(row["created_at"]) == target_day
  ]
  paid_to_members_cents = sum(
    row["amount_cents"] or 0
    for row in payouts_today
  )

  earnings_counter = Counter()
  for row in payouts_today:
    earnings_counter[row["user_id"]] += row["amount_cents"] or 0

  accruals = db.accruals_for_guild(guild_id)

  # Outstanding balances exactly as they were at the end of target_day.
  pending_at_day_end = []

  for row in accruals:
    created_day = local_date_from_iso(row["created_at"])
    paid_day = (
      local_date_from_iso(row["paid_at"])
      if row["paid_at"]
      else None
    )

    if (
      created_day is not None
      and created_day <= target_day
      and (paid_day is None or paid_day > target_day)
    ):
      pending_at_day_end.append(row)

  pending_cents = sum(
    row["amount_cents"] or 0
    for row in pending_at_day_end
  )

  pending_by_user = Counter()
  for row in pending_at_day_end:
    pending_by_user[row["user_id"]] += row["amount_cents"] or 0

  contributions = db.family_contributions_for_guild(guild_id)
  personal_family_cents = sum(
    row["amount_cents"] or 0
    for row in contributions
    if local_date_from_iso(row["created_at"]) == target_day
  )

  embed = discord.Embed(
    title=f"\U0001f4ca \u041f\u0406\u0414\u0421\u0423\u041c\u041a\u0418 \u0421\u0406\u041c'\u0407 \u2022 {target_day.strftime('%d.%m.%Y')}",
    description="\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u0438\u0439 \u0437\u0432\u0456\u0442 \u0437\u0430 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0439 \u0434\u0435\u043d\u044c.",
    color=discord.Color.blurple(),
  )

  embed.add_field(
    name="\U0001f4cb \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438",
    value=(
      f"\u0412\u0438\u043a\u043e\u043d\u0430\u043d\u043e: **{len(completed_today)}**\n"
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{len(calculated_today)}**\n"
      f"\u0417\u0430\u0433\u0430\u043b\u044c\u043d\u0430 \u0441\u0443\u043c\u0430 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445: **{format_cents(gross_cents)}**"
    ),
    inline=False,
  )

  embed.add_field(
    name="\U0001f3e6 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457",
    value=(
      f"\u041d\u0430\u0434\u0456\u0439\u0448\u043b\u043e: **{format_cents(family_cents)}**\n"
      f"\u0417 \u043d\u0438\u0445 \u043e\u0441\u043e\u0431\u0438\u0441\u0442\u0438\u0445 \u0432\u043d\u0435\u0441\u043a\u0456\u0432: **{format_cents(personal_family_cents)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f4b5 \u0412\u0438\u043f\u043b\u0430\u0442\u0438",
    value=(
      f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(accrued_cents)}**\n"
      f"\u0424\u0430\u043a\u0442\u0438\u0447\u043d\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e \u0437\u0430 \u0434\u0435\u043d\u044c: **{format_cents(paid_to_members_cents)}**\n"
      f"\u0417\u0430\u043b\u0438\u0448\u043e\u043a \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u043d\u0430 \u043a\u0456\u043d\u0435\u0446\u044c \u0434\u043d\u044f: **{format_cents(pending_cents)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\U0001f465 \u0410\u043a\u0442\u0438\u0432\u043d\u0456\u0441\u0442\u044c",
    value=(
      f"\u0423\u043d\u0456\u043a\u0430\u043b\u044c\u043d\u0438\u0445 \u0432\u0438\u043a\u043e\u043d\u0430\u0432\u0446\u0456\u0432: **{unique_participants}**\n"
      f"\u0412\u0441\u044c\u043e\u0433\u043e \u0443\u0447\u0430\u0441\u0442\u0435\u0439: **{participations_total}**"
    ),
    inline=False,
  )

  top_activity = participant_counter.most_common(5)
  if top_activity:
    activity_lines = [
      f"**{idx}.** <@{uid}> \u2014 **{count}** \u0443\u0447\u0430\u0441\u0442\u0435\u0439"
      for idx, (uid, count) in enumerate(top_activity, start=1)
    ]
    embed.add_field(
      name="\U0001f3c6 \u041d\u0430\u0439\u0430\u043a\u0442\u0438\u0432\u043d\u0456\u0448\u0456 \u0437\u0430 \u0434\u0435\u043d\u044c",
      value="\n".join(activity_lines),
      inline=False,
    )

  top_earnings = earnings_counter.most_common(5)
  if top_earnings:
    earning_lines = [
      f"**{idx}.** <@{uid}> \u2014 **{format_cents(amount)}**"
      for idx, (uid, amount) in enumerate(top_earnings, start=1)
    ]
    embed.add_field(
      name="\U0001f4b0 \u0422\u043e\u043f-5 \u0444\u0430\u043a\u0442\u0438\u0447\u043d\u043e\u0433\u043e \u0437\u0430\u0440\u043e\u0431\u0456\u0442\u043a\u0443 \u0437\u0430 \u0434\u0435\u043d\u044c",
      value="\n".join(earning_lines),
      inline=False,
    )

  if pending_by_user:
    pending_lines = [
      f"<@{uid}> \u2014 **{format_cents(amount)}**"
      for uid, amount in pending_by_user.most_common(15)
    ]

    if len(pending_by_user) > 15:
      pending_lines.append(
        f"\u2026\u0456 \u0449\u0435 {len(pending_by_user) - 15}"
      )

    embed.add_field(
      name="\U0001f4b3 \u0414\u041e \u0412\u0418\u041f\u041b\u0410\u0422\u0418 \u0423\u0427\u0410\u0421\u041d\u0418\u041a\u0410\u041c",
      value=(
        f"**\u0417\u0410\u041b\u0418\u0428\u041e\u041a: {format_cents(pending_cents)}**\n\n"
        + "\n".join(pending_lines)
      ),
      inline=False,
    )
  else:
    embed.add_field(
      name="\U0001f4b3 \u0414\u041e \u0412\u0418\u041f\u041b\u0410\u0422\u0418 \u0423\u0427\u0410\u0421\u041d\u0418\u041a\u0410\u041c",
      value="**\u2705 \u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u0441\u0443\u043c \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u043d\u0435\u043c\u0430\u0454.**",
      inline=False,
    )

  embed.set_footer(
    text=f"\u0427\u0430\u0441\u043e\u0432\u0430 \u0437\u043e\u043d\u0430: {TIMEZONE_NAME}"
  )
  return embed


async def send_auto_rating(
  bot_instance: commands.Bot,
  slot_label: str,
):
  if not GUILD_ID or not RATING_CHANNEL_ID:
    return

  guild = bot_instance.get_guild(GUILD_ID)
  if guild is None:
    return

  channel = guild.get_channel(RATING_CHANNEL_ID)
  if not isinstance(channel, discord.TextChannel):
    try:
      channel = await bot_instance.fetch_channel(RATING_CHANNEL_ID)
    except discord.DiscordException:
      return

  if not isinstance(channel, discord.TextChannel):
    return

  # 1) General leaderboard.
  for embed in build_auto_rating_embeds(GUILD_ID, slot_label):
    await channel.send(embed=embed)

  # 2) One combined leaderboard for all configured roles.
  if get_leaderboard_role_ids(GUILD_ID):
    role_embed = await build_role_leaderboard_embed(
      guild,
      GUILD_ID,
      slot_label,
    )
    await channel.send(embed=role_embed)


async def send_auto_family_stats(
  bot_instance: commands.Bot,
  target_day,
):
  if not GUILD_ID or not FAMILY_STATS_CHANNEL_ID:
    return

  guild = bot_instance.get_guild(GUILD_ID)
  if guild is None:
    return

  channel = guild.get_channel(FAMILY_STATS_CHANNEL_ID)
  if not isinstance(channel, discord.TextChannel):
    try:
      channel = await bot_instance.fetch_channel(FAMILY_STATS_CHANNEL_ID)
    except discord.DiscordException:
      return

  if not isinstance(channel, discord.TextChannel):
    return

  await channel.send(
    embed=build_family_daily_embed(
      GUILD_ID,
      target_day,
    )
  )




def register_commands(bot: commands.Bot):
  @bot.tree.command(
    name="leaderboard-settings",
    description="\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u0442\u0438 \u0440\u043e\u043b\u0456 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0433\u043e \u0441\u043a\u043b\u0430\u0434\u0443",
  )
  async def leaderboard_settings(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    current_ids = get_leaderboard_role_ids(interaction.guild_id)
    current = (
      " ".join(f"<@&{rid}>" for rid in current_ids)
      if current_ids
      else "\u041d\u0435 \u043d\u0430\u043b\u0430\u0448\u0442\u043e\u0432\u0430\u043d\u043e."
    )

    await interaction.response.send_message(
      (
        "\U0001f3c6 **\u0420\u0415\u0419\u0422\u0418\u041d\u0413 \u041a\u041e\u041d\u0422\u0420\u0410\u041a\u0422\u0406\u0412 \u2022 \u041e\u0421\u041d\u041e\u0412\u041d\u0418\u0419 \u0421\u041a\u041b\u0410\u0414**\n"
        f"\u041f\u043e\u0442\u043e\u0447\u043d\u0456 \u0440\u043e\u043b\u0456: {current}\n\n"
        "\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0440\u043e\u043b\u0456 \u043d\u0438\u0436\u0447\u0435. \u0423\u0441\u0456 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0438, \u044f\u043a\u0456 \u043c\u0430\u044e\u0442\u044c \u0445\u043e\u0447\u0430 \u0431 \u043e\u0434\u043d\u0443 \u0437 \u043d\u0438\u0445, \u043f\u043e\u0442\u0440\u0430\u043f\u043b\u044f\u0442\u044c \u0432 \u043e\u0434\u0438\u043d \u0441\u043f\u0456\u043b\u044c\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433."
      ),
      view=LeaderboardSettingsView(interaction.guild_id),
      ephemeral=True,
    )


  @bot.tree.command(
    name="test-rating",
    description="\u0422\u0435\u0441\u0442\u043e\u0432\u043e \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u0438\u0442\u0438 \u043f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u0443 \u043a\u0430\u043d\u0430\u043b \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443",
  )
  async def test_rating(interaction: discord.Interaction):
    if not test_command_user(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0426\u044f \u0442\u0435\u0441\u0442\u043e\u0432\u0430 \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u0432\u043b\u0430\u0441\u043d\u0438\u043a\u0443 \u0442\u0435\u0441\u0442\u0456\u0432.",
        ephemeral=True,
      )
      return

    if not RATING_CHANNEL_ID:
      await interaction.response.send_message(
        "\u274c \u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e `RATING_CHANNEL_ID` \u0443 Railway.",
        ephemeral=True,
      )
      return

    await interaction.response.defer(ephemeral=True)

    now = datetime.now(LOCAL_TZ)
    await send_auto_rating(
      bot,
      f"{now.strftime('%d.%m.%Y')} \u2022 \u0442\u0435\u0441\u0442",
    )

    await interaction.followup.send(
      "\u2705 \u0422\u0435\u0441\u0442\u043e\u0432\u0438\u0439 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e \u0432 \u043a\u0430\u043d\u0430\u043b \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443.\n"
      "\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u0438\u0439 \u043f\u043e\u0441\u0442 \u043e 09:00 / 21:00 \u0446\u0438\u043c \u043d\u0435 \u043f\u043e\u0437\u043d\u0430\u0447\u0430\u0454\u0442\u044c\u0441\u044f \u044f\u043a \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u0438\u0439.",
      ephemeral=True,
    )


  @bot.tree.command(
    name="test-family-stats",
    description="\u0422\u0435\u0441\u0442\u043e\u0432\u043e \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u0438\u0442\u0438 \u0449\u043e\u0434\u0435\u043d\u043d\u0443 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0443 \u0441\u0456\u043c'\u0457",
  )
  async def test_family_stats(interaction: discord.Interaction):
    if not test_command_user(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0426\u044f \u0442\u0435\u0441\u0442\u043e\u0432\u0430 \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u0432\u043b\u0430\u0441\u043d\u0438\u043a\u0443 \u0442\u0435\u0441\u0442\u0456\u0432.",
        ephemeral=True,
      )
      return

    if not FAMILY_STATS_CHANNEL_ID:
      await interaction.response.send_message(
        "\u274c \u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e `FAMILY_STATS_CHANNEL_ID` \u0443 Railway.",
        ephemeral=True,
      )
      return

    await interaction.response.defer(ephemeral=True)

    target_day = datetime.now(LOCAL_TZ).date()

    await send_auto_family_stats(
      bot,
      target_day,
    )

    await interaction.followup.send(
      (
        "\u2705 \u0422\u0435\u0441\u0442\u043e\u0432\u0443 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0443 \u0441\u0456\u043c'\u0457 \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e.\n"
        f"\u041f\u0435\u0440\u0456\u043e\u0434: **{target_day.strftime('%d.%m.%Y')}**.\n"
        "\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u0438\u0439 \u0437\u0432\u0456\u0442 \u043e 00:00 \u0446\u0438\u043c \u043d\u0435 \u043f\u043e\u0437\u043d\u0430\u0447\u0430\u0454\u0442\u044c\u0441\u044f \u044f\u043a \u0432\u0438\u043a\u043e\u043d\u0430\u043d\u0438\u0439."
      ),
      ephemeral=True,
    )


  @bot.tree.command(name="stats", description="\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u0434\u043b\u044f \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0430")
  async def stats(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
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

    await interaction.response.send_message(
      embed=build_admin_general_stats_embed(guild.id),
      view=AdminStatsView(guild.id, "general"),
      ephemeral=True,
    )


