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
from stats import chunk_lines

def parse_birthday_input(raw: str) -> tuple[int, int, Optional[int]]:
  text = raw.strip()
  match = re.fullmatch(
    r"\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?\s*",
    text,
  )
  if not match:
    raise ValueError("\u0412\u043a\u0430\u0436\u0456\u0442\u044c \u0434\u0430\u0442\u0443 \u0443 \u0444\u043e\u0440\u043c\u0430\u0442\u0456 \u0414\u0414.\u041c\u041c \u0430\u0431\u043e \u0414\u0414.\u041c\u041c.\u0420\u0420\u0420\u0420.")

  day = int(match.group(1))
  month = int(match.group(2))
  year = int(match.group(3)) if match.group(3) else None

  validation_year = year or 2000

  try:
    datetime(validation_year, month, day)
  except ValueError:
    raise ValueError("\u0422\u0430\u043a\u043e\u0457 \u0434\u0430\u0442\u0438 \u043d\u0435 \u0456\u0441\u043d\u0443\u0454.")

  if year is not None:
    current_year = datetime.now(LOCAL_TZ).year
    if year < 1900 or year > current_year:
      raise ValueError(
        f"\u0420\u0456\u043a \u043c\u0430\u0454 \u0431\u0443\u0442\u0438 \u0432\u0456\u0434 1900 \u0434\u043e {current_year}."
      )

  return day, month, year


def birthday_display(
  day: int,
  month: int,
  year: Optional[int] = None,
) -> str:
  if year:
    return f"{day:02d}.{month:02d}.{year}"
  return f"{day:02d}.{month:02d}"


def next_birthday_date(
  month: int,
  day: int,
  today,
):
  # Works for 29 February too: searches until the next valid date.
  for year in range(today.year, today.year + 9):
    try:
      candidate = datetime(year, month, day).date()
    except ValueError:
      continue

    if candidate >= today:
      return candidate

  return None


def birthday_days_until(
  month: int,
  day: int,
  today,
) -> Optional[int]:
  target = next_birthday_date(month, day, today)
  if target is None:
    return None
  return (target - today).days


def build_birthday_panel_embed() -> discord.Embed:
  embed = discord.Embed(
    title="\U0001f382 \u0414\u043d\u0456 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f",
    description=(
      "**\u0412\u043a\u0430\u0436\u0456\u0442\u044c \u0441\u0432\u043e\u044e \u0434\u0430\u0442\u0443 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f**, \u0449\u043e\u0431 \u043c\u0438 \u043c\u043e\u0433\u043b\u0438 \u0432\u0430\u0441 \u043f\u0440\u0438\u0432\u0456\u0442\u0430\u0442\u0438 \U0001f973\n\n"
      "\u041d\u0430\u0442\u0438\u0441\u043d\u0456\u0442\u044c \u043a\u043d\u043e\u043f\u043a\u0443 \u043d\u0438\u0436\u0447\u0435 \u0442\u0430 \u0432\u0432\u0435\u0434\u0456\u0442\u044c \u0434\u0430\u0442\u0443."
    ),
    color=discord.Color.magenta(),
  )
  embed.set_footer(
    text="\u042f\u043a\u0449\u043e \u0434\u0430\u0442\u0430 \u0437\u043c\u0456\u043d\u0438\u0442\u044c\u0441\u044f \u2014 \u043f\u0440\u043e\u0441\u0442\u043e \u0437\u0430\u043f\u043e\u0432\u043d\u0456\u0442\u044c \u0444\u043e\u0440\u043c\u0443 \u0449\u0435 \u0440\u0430\u0437."
  )
  return embed


class BirthdayModal(discord.ui.Modal, title="\U0001f382 \u0414\u0430\u0442\u0430 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f"):
  def __init__(self):
    super().__init__(timeout=300)

    self.birthday_input = discord.ui.TextInput(
      label="\u0412\u0430\u0448\u0430 \u0434\u0430\u0442\u0430 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: 21.09 \u0430\u0431\u043e 21.09.2001",
      required=True,
      max_length=10,
    )
    self.add_item(self.birthday_input)

  async def on_submit(self, interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
      await interaction.response.send_message(
        "\u274c \u0426\u0435 \u043f\u0440\u0430\u0446\u044e\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0456.",
        ephemeral=True,
      )
      return

    try:
      day, month, year = parse_birthday_input(
        str(self.birthday_input)
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )
      return

    db.upsert_birthday(
      guild.id,
      interaction.user.id,
      day,
      month,
      year,
    )

    await interaction.response.send_message(
      (
        "\u2705 \u0414\u0430\u0442\u0443 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f \u0437\u0431\u0435\u0440\u0435\u0436\u0435\u043d\u043e: "
        f"**{birthday_display(day, month, year)}**."
      ),
      ephemeral=True,
    )


class BirthdayPanelView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
    label="\u0412\u043a\u0430\u0437\u0430\u0442\u0438 \u0434\u0430\u0442\u0443 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f",
    emoji="\U0001f382",
    style=discord.ButtonStyle.primary,
    custom_id="agosto:birthday:open",
  )
  async def open_birthday(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(BirthdayModal())


async def get_text_channel(
  bot_instance: commands.Bot,
  channel_id: int,
) -> Optional[discord.TextChannel]:
  if not channel_id:
    return None

  channel = bot_instance.get_channel(channel_id)

  if channel is None:
    try:
      channel = await bot_instance.fetch_channel(channel_id)
    except discord.DiscordException:
      return None

  if isinstance(channel, discord.TextChannel):
    return channel

  return None


async def ensure_birthday_panel(
  bot_instance: commands.Bot,
):
  if not GUILD_ID or not BIRTHDAY_INPUT_CHANNEL_ID:
    return

  channel = await get_text_channel(
    bot_instance,
    BIRTHDAY_INPUT_CHANNEL_ID,
  )
  if channel is None:
    print("[BIRTHDAY] Input channel not found")
    return

  old_message_id = db.get_setting(
    GUILD_ID,
    "birthday_panel_message_id",
  )

  if old_message_id:
    try:
      message = await channel.fetch_message(int(old_message_id))
      await message.edit(
        embed=build_birthday_panel_embed(),
        view=BirthdayPanelView(),
      )
      return
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
      pass

  message = await channel.send(
    embed=build_birthday_panel_embed(),
    view=BirthdayPanelView(),
  )

  db.set_setting(
    GUILD_ID,
    "birthday_panel_message_id",
    str(message.id),
  )
  print(f"[BIRTHDAY] Panel posted: {message.id}")


def build_birthday_list_embed(
  guild_id: int,
) -> discord.Embed:
  today = datetime.now(LOCAL_TZ).date()
  rows = db.birthdays_for_guild(guild_id)

  decorated = []
  for row in rows:
    days = birthday_days_until(
      row["month"],
      row["day"],
      today,
    )
    if days is not None:
      decorated.append((days, row))

  decorated.sort(
    key=lambda item: (
      item[0],
      item[1]["month"],
      item[1]["day"],
      item[1]["user_id"],
    )
  )

  embed = discord.Embed(
    title="\U0001f382 \u0421\u043f\u0438\u0441\u043e\u043a \u0434\u043d\u0456\u0432 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f",
    color=discord.Color.magenta(),
  )

  if not decorated:
    embed.description = "\u041f\u043e\u043a\u0438 \u0449\u043e \u043d\u0456\u0445\u0442\u043e \u043d\u0435 \u0432\u043a\u0430\u0437\u0430\u0432 \u0434\u0430\u0442\u0443 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f."
    return embed

  lines = []

  for idx, (days, row) in enumerate(decorated, start=1):
    if days == 0:
      when = "**\u0441\u044c\u043e\u0433\u043e\u0434\u043d\u0456**"
    elif days == 1:
      when = "**\u0437\u0430\u0432\u0442\u0440\u0430**"
    else:
      when = f"\u0447\u0435\u0440\u0435\u0437 **{days} \u0434\u043d.**"

    lines.append(
      f"**{idx}.** <@{row['user_id']}> \u2014 "
      f"**{birthday_display(row['day'], row['month'], row['year'])}** "
      f"\u2022 {when}"
    )

  for page_idx, chunk in enumerate(
    chunk_lines(lines, max_lines=20, max_chars=3500),
    start=1,
  ):
    if page_idx == 1:
      embed.description = "\n".join(chunk)
      embed.set_footer(text=f"\u0423\u0441\u044c\u043e\u0433\u043e \u0437\u0430\u043f\u0438\u0441\u0430\u043d\u043e: {len(lines)}")
      return embed

  return embed


async def send_birthday_reminders(
  bot_instance: commands.Bot,
  test_mode: bool = False,
) -> tuple[bool, str]:
  if not GUILD_ID:
    return False, "\u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e GUILD_ID."

  if not BIRTHDAY_ALERT_CHANNEL_ID:
    return False, "\u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e BIRTHDAY_ALERT_CHANNEL_ID."

  channel = await get_text_channel(
    bot_instance,
    BIRTHDAY_ALERT_CHANNEL_ID,
  )
  if channel is None:
    print("[BIRTHDAY] Alert channel not found")
    return False, (
      "\u041d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u043a\u0430\u043d\u0430\u043b \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u044c. "
      "\u041f\u0435\u0440\u0435\u0432\u0456\u0440 BIRTHDAY_ALERT_CHANNEL_ID."
    )

  guild = bot_instance.get_guild(GUILD_ID)
  if guild is None:
    return False, "\u0411\u043e\u0442 \u043d\u0435 \u0437\u043d\u0430\u0439\u0448\u043e\u0432 \u0441\u0435\u0440\u0432\u0435\u0440 GUILD_ID."

  me = guild.me
  if me is None and bot_instance.user is not None:
    me = guild.get_member(bot_instance.user.id)

  if me is not None:
    permissions = channel.permissions_for(me)

    if not permissions.view_channel:
      return False, "\u0423 \u0431\u043e\u0442\u0430 \u043d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430 View Channel \u0443 \u043a\u0430\u043d\u0430\u043b\u0456 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u044c."

    if not permissions.send_messages:
      return False, "\u0423 \u0431\u043e\u0442\u0430 \u043d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430 Send Messages \u0443 \u043a\u0430\u043d\u0430\u043b\u0456 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u044c."

    if not permissions.embed_links:
      return False, "\u0423 \u0431\u043e\u0442\u0430 \u043d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430 Embed Links \u0443 \u043a\u0430\u043d\u0430\u043b\u0456 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u044c."

  today = datetime.now(LOCAL_TZ).date()
  rows = db.birthdays_for_guild(GUILD_ID)

  today_rows = []
  tomorrow_rows = []
  three_day_rows = []
  week_rows = []

  for row in rows:
    days = birthday_days_until(
      row["month"],
      row["day"],
      today,
    )

    if days == 0:
      today_rows.append(row)
    elif days == 1:
      tomorrow_rows.append(row)
    elif days == 3:
      three_day_rows.append(row)
    elif days == 7:
      week_rows.append(row)

  if not (today_rows or tomorrow_rows or three_day_rows or week_rows) and not test_mode:
    return True, "\u0421\u044c\u043e\u0433\u043e\u0434\u043d\u0456 \u043d\u0435\u043c\u0430\u0454 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u044c \u0434\u043b\u044f \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043d\u044f."

  embed = discord.Embed(
    title=(
      "\U0001f9ea \u0422\u0415\u0421\u0422 \u2022 \U0001f382 \u041d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f \u043f\u0440\u043e \u0434\u043d\u0456 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f"
      if test_mode
      else "\U0001f382 \u041d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f \u043f\u0440\u043e \u0434\u043d\u0456 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f"
    ),
    description=today.strftime("%d.%m.%Y"),
    color=discord.Color.magenta(),
  )

  if test_mode and not (today_rows or tomorrow_rows or three_day_rows or week_rows):
    embed.add_field(
      name="\u2705 \u0422\u0435\u0441\u0442 \u043f\u0440\u043e\u0439\u0434\u0435\u043d\u043e",
      value=(
        "\u041a\u0430\u043d\u0430\u043b \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u044c \u043f\u0440\u0430\u0446\u044e\u0454.\n"
        "\u041d\u0430 \u0441\u044c\u043e\u0433\u043e\u0434\u043d\u0456 \u043d\u0435\u043c\u0430\u0454 \u0414\u041d, \u044f\u043a\u0456 \u043f\u043e\u0442\u0440\u0435\u0431\u0443\u044e\u0442\u044c \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f "
        "\u0437\u0430 7 / 3 / 1 \u0434\u0435\u043d\u044c / \u0441\u044c\u043e\u0433\u043e\u0434\u043d\u0456."
      ),
      inline=False,
    )

  if today_rows:
    embed.add_field(
      name="\U0001f389 \u0421\u042c\u041e\u0413\u041e\u0414\u041d\u0406",
      value="\n".join(
        f"<@{row['user_id']}> \u2014 **{birthday_display(row['day'], row['month'])}**"
        for row in today_rows
      ),
      inline=False,
    )

  if tomorrow_rows:
    embed.add_field(
      name="\u23f0 \u0417\u0410\u0412\u0422\u0420\u0410",
      value="\n".join(
        f"<@{row['user_id']}> \u2014 **{birthday_display(row['day'], row['month'])}**"
        for row in tomorrow_rows
      ),
      inline=False,
    )

  if three_day_rows:
    embed.add_field(
      name="\u23f3 \u0427\u0415\u0420\u0415\u0417 3 \u0414\u041d\u0406",
      value="\n".join(
        f"<@{row['user_id']}> \u2014 **{birthday_display(row['day'], row['month'])}**"
        for row in three_day_rows
      ),
      inline=False,
    )

  if week_rows:
    embed.add_field(
      name="\U0001f4c5 \u0427\u0415\u0420\u0415\u0417 \u0422\u0418\u0416\u0414\u0415\u041d\u042c",
      value="\n".join(
        f"<@{row['user_id']}> \u2014 **{birthday_display(row['day'], row['month'])}**"
        for row in week_rows
      ),
      inline=False,
    )

  if test_mode:
    embed.set_footer(
      text="\U0001f9ea \u0422\u0435\u0441\u0442\u043e\u0432\u0435 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f \u2022 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u0438\u0439 \u0433\u0440\u0430\u0444\u0456\u043a \u043d\u0435 \u0437\u043c\u0456\u043d\u0435\u043d\u043e"
    )

  try:
    await asyncio.wait_for(
      channel.send(embed=embed),
      timeout=15,
    )
    return True, "\u041d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f \u0443\u0441\u043f\u0456\u0448\u043d\u043e \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e."

  except asyncio.TimeoutError:
    print("[BIRTHDAY] Sending reminder timed out")
    return False, "Discord \u043d\u0435 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0432 \u0437\u0430 15 \u0441\u0435\u043a\u0443\u043d\u0434 \u043f\u0456\u0434 \u0447\u0430\u0441 \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043a\u0438."

  except discord.Forbidden as exc:
    print(f"[BIRTHDAY] Forbidden while sending reminder: {exc}")
    return False, (
      "Discord \u0437\u0430\u0431\u043e\u0440\u043e\u043d\u0438\u0432 \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043d\u044f. "
      "\u041f\u0435\u0440\u0435\u0432\u0456\u0440 \u043f\u0440\u0430\u0432\u0430 View Channel, Send Messages \u0442\u0430 Embed Links."
    )

  except discord.HTTPException as exc:
    print(f"[BIRTHDAY] HTTP error while sending reminder: {exc}")
    return False, f"\u041f\u043e\u043c\u0438\u043b\u043a\u0430 Discord API: {exc}"

  except Exception as exc:
    print(f"[BIRTHDAY] Unexpected reminder error: {type(exc).__name__}: {exc}")
    return False, f"{type(exc).__name__}: {exc}"




def register_commands(bot: commands.Bot):
  @bot.tree.command(
    name="test-birthday-reminder",
    description="\u0422\u0435\u0441\u0442\u043e\u0432\u043e \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u0438\u0442\u0438 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f \u043f\u0440\u043e \u0414\u041d \u0443 \u043a\u0430\u043d\u0430\u043b \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0430",
  )
  async def test_birthday_reminder(interaction: discord.Interaction):
    if not test_command_user(interaction.user):
      await interaction.response.send_message(
        "\u274c \u0426\u044f \u0442\u0435\u0441\u0442\u043e\u0432\u0430 \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u0432\u043b\u0430\u0441\u043d\u0438\u043a\u0443 \u0442\u0435\u0441\u0442\u0456\u0432.",
        ephemeral=True,
      )
      return

    if not BIRTHDAY_ALERT_CHANNEL_ID:
      await interaction.response.send_message(
        "\u274c \u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e `BIRTHDAY_ALERT_CHANNEL_ID` \u0443 Railway.",
        ephemeral=True,
      )
      return

    await interaction.response.defer(ephemeral=True)

    try:
      ok, result_text = await send_birthday_reminders(
        bot,
        test_mode=True,
      )
    except Exception as exc:
      print(
        "[BIRTHDAY] /test-birthday-reminder crashed: "
        f"{type(exc).__name__}: {exc}"
      )
      ok = False
      result_text = f"{type(exc).__name__}: {exc}"

    if ok:
      await interaction.followup.send(
        (
          "\u2705 \u0422\u0435\u0441\u0442\u043e\u0432\u0435 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e \u0432 \u043a\u0430\u043d\u0430\u043b \u0414\u041d \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432.\n"
          "\u0426\u0435 **\u043d\u0435 \u0432\u043f\u043b\u0438\u0432\u0430\u0454** \u043d\u0430 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u0435 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f \u0437\u0430 \u0433\u0440\u0430\u0444\u0456\u043a\u043e\u043c."
        ),
        ephemeral=True,
      )
    else:
      await interaction.followup.send(
        (
          "\u274c \u041d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f \u0432\u0456\u0434\u043f\u0440\u0430\u0432\u0438\u0442\u0438 \u0442\u0435\u0441\u0442\u043e\u0432\u0435 \u043d\u0430\u0433\u0430\u0434\u0443\u0432\u0430\u043d\u043d\u044f.\n\n"
          f"\u041f\u0440\u0438\u0447\u0438\u043d\u0430: **{result_text}**"
        ),
        ephemeral=True,
      )


  @bot.tree.command(
    name="birthdays",
    description="\u041f\u0435\u0440\u0435\u0433\u043b\u044f\u043d\u0443\u0442\u0438 \u0441\u043f\u0438\u0441\u043e\u043a \u0434\u043d\u0456\u0432 \u043d\u0430\u0440\u043e\u0434\u0436\u0435\u043d\u043d\u044f \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0456\u0432",
  )
  async def birthdays(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "\u274c \u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
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
      embed=build_birthday_list_embed(guild.id),
      ephemeral=True,
    )


