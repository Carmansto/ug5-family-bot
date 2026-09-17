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

from contracts import audit_log

BONUS_SETTING_START = "bonus_period_start_at"

BONUS_SETTING_THRESHOLDS = "bonus_thresholds"

BONUS_SETTING_DISTRIBUTION = "bonus_distribution"

BONUS_SETTING_AUTO = "bonus_auto_enabled"

BONUS_SETTING_WEEKDAY = "bonus_close_weekday"

BONUS_SETTING_TIME = "bonus_close_time"


def bonus_parse_local_datetime(raw: str) -> datetime:
  value = raw.strip()
  for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
    try:
      parsed = datetime.strptime(value, fmt)
      return parsed.replace(tzinfo=LOCAL_TZ)
    except ValueError:
      pass
  raise ValueError("\u0424\u043e\u0440\u043c\u0430\u0442: \u0414\u0414.\u041c\u041c.\u0420\u0420\u0420\u0420 \u0430\u0431\u043e \u0414\u0414.\u041c\u041c.\u0420\u0420\u0420\u0420 \u0413\u0413:\u0425\u0425")


def bonus_parse_weekday(raw: str) -> int:
  value = raw.strip().lower().replace(".", "")
  mapping = {
    "\u043f\u043d": 0,
    "\u043f\u043e\u043d\u0435\u0434\u0456\u043b\u043e\u043a": 0,
    "\u0432\u0442": 1,
    "\u0432\u0456\u0432\u0442\u043e\u0440\u043e\u043a": 1,
    "\u0441\u0440": 2,
    "\u0441\u0435\u0440\u0435\u0434\u0430": 2,
    "\u0447\u0442": 3,
    "\u0447\u0435\u0442\u0432\u0435\u0440": 3,
    "\u043f\u0442": 4,
    "\u043f'\u044f\u0442\u043d\u0438\u0446\u044f": 4,
    "\u043f\u044f\u0442\u043d\u0438\u0446\u044f": 4,
    "\u0441\u0431": 5,
    "\u0441\u0443\u0431\u043e\u0442\u0430": 5,
    "\u043d\u0434": 6,
    "\u043d\u0435\u0434\u0456\u043b\u044f": 6,
  }
  if value in mapping:
    return mapping[value]
  if value.isdigit() and 1 <= int(value) <= 7:
    return int(value) - 1
  raise ValueError("\u0414\u0435\u043d\u044c: \u041f\u043d/\u0412\u0442/\u0421\u0440/\u0427\u0442/\u041f\u0442/\u0421\u0431/\u041d\u0434 \u0430\u0431\u043e \u0447\u0438\u0441\u043b\u043e 1\u20137")


def bonus_parse_time(raw: str) -> tuple[int, int]:
  try:
    parsed = datetime.strptime(raw.strip(), "%H:%M")
    return parsed.hour, parsed.minute
  except ValueError:
    raise ValueError("\u0427\u0430\u0441 \u043c\u0430\u0454 \u0431\u0443\u0442\u0438 \u0443 \u0444\u043e\u0440\u043c\u0430\u0442\u0456 \u0413\u0413:\u0425\u0425, \u043d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434 00:00")


def bonus_get_thresholds(guild_id: int) -> list[dict]:
  raw = db.get_setting(guild_id, BONUS_SETTING_THRESHOLDS)
  if not raw:
    return []
  try:
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) != 3:
      return []
    result = []
    for item in values:
      result.append({
        "target_dollars": int(item["target_dollars"]),
        "pool_dollars": int(item["pool_dollars"]),
      })
    return result
  except Exception:
    return []


def bonus_get_distribution(guild_id: int) -> list[Decimal]:
  raw = db.get_setting(guild_id, BONUS_SETTING_DISTRIBUTION)
  if not raw:
    return []
  try:
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) != 5:
      return []
    result = [Decimal(str(value)) for value in values]
    if sum(result, Decimal("0")) != Decimal("100"):
      return []
    return result
  except Exception:
    return []


def bonus_config_ready(guild_id: int) -> bool:
  return (
    len(bonus_get_thresholds(guild_id)) == 3
    and len(bonus_get_distribution(guild_id)) == 5
  )


def bonus_get_start(guild_id: int) -> Optional[str]:
  return db.get_setting(guild_id, BONUS_SETTING_START)


def bonus_ensure_start(guild_id: int) -> str:
  current = bonus_get_start(guild_id)
  if current:
    return current

  fallback = db.get_setting(guild_id, "rating_reset_at") or utc_now_iso()
  db.set_setting(guild_id, BONUS_SETTING_START, fallback)
  return fallback


def bonus_schedule_values(guild_id: int):
  weekday_raw = db.get_setting(guild_id, BONUS_SETTING_WEEKDAY)
  time_raw = db.get_setting(guild_id, BONUS_SETTING_TIME)
  if weekday_raw is None or not time_raw:
    return None

  try:
    weekday = int(weekday_raw)
    hour, minute = bonus_parse_time(time_raw)
    if weekday < 0 or weekday > 6:
      return None
    return weekday, hour, minute
  except Exception:
    return None


def bonus_weekday_label(value: int) -> str:
  labels = ["\u041f\u043d", "\u0412\u0442", "\u0421\u0440", "\u0427\u0442", "\u041f\u0442", "\u0421\u0431", "\u041d\u0434"]
  return labels[value] if 0 <= value < 7 else "?"


def bonus_next_close(
  guild_id: int,
  start_iso: Optional[str] = None,
) -> Optional[datetime]:
  schedule = bonus_schedule_values(guild_id)
  start_iso = start_iso or bonus_get_start(guild_id)

  if not schedule or not start_iso:
    return None

  try:
    start_dt = datetime.fromisoformat(start_iso)
    if start_dt.tzinfo is None:
      start_dt = start_dt.replace(tzinfo=timezone.utc)
    start_local = start_dt.astimezone(LOCAL_TZ)
  except Exception:
    return None

  weekday, hour, minute = schedule
  days_ahead = (weekday - start_local.weekday()) % 7

  # Bonus periods are weekly. If the configured closing weekday is the
  # same weekday as the period start, close on the NEXT week's occurrence,
  # not a few hours later on the same day.
  if days_ahead == 0:
    days_ahead = 7

  candidate = (start_local + timedelta(days=days_ahead)).replace(
    hour=hour,
    minute=minute,
    second=0,
    microsecond=0,
  )

  return candidate


def bonus_rating_between(
  guild_id: int,
  start_at: str,
  end_at: str,
):
  rows = db.contracts_for_bonus_period(
    guild_id,
    start_at,
    end_at,
  )

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
    key=lambda uid: (
      points[uid],
      participations[uid],
      -uid,
    ),
    reverse=True,
  )

  return points, participations, users


def bonus_reached_level(
  family_cents: int,
  thresholds: list[dict],
):
  level = 0
  pool_cents = 0

  for index, item in enumerate(thresholds, start=1):
    if family_cents >= int(item["target_dollars"]) * 100:
      level = index
      pool_cents = int(item["pool_dollars"]) * 100

  return level, pool_cents


def bonus_allocations(
  pool_cents: int,
  distribution: list[Decimal],
) -> list[int]:
  if pool_cents <= 0 or len(distribution) != 5:
    return [0, 0, 0, 0, 0]

  result = []
  used = 0

  for index, percent in enumerate(distribution):
    if index == 4:
      amount = pool_cents - used
    else:
      amount = int(
        (
          Decimal(pool_cents)
          * percent
          / Decimal("100")
        ).quantize(Decimal("1"))
      )
      used += amount

    result.append(max(0, amount))

  return result


def bonus_preview_data(
  guild_id: int,
  end_at: Optional[str] = None,
):
  start_at = bonus_ensure_start(guild_id)
  end_at = end_at or utc_now_iso()

  thresholds = bonus_get_thresholds(guild_id)
  distribution = bonus_get_distribution(guild_id)

  family_cents = db.family_fund_earned_between(
    guild_id,
    start_at,
    end_at,
  )

  points, participations, users = bonus_rating_between(
    guild_id,
    start_at,
    end_at,
  )

  top5 = users[:5]
  level, pool_cents = bonus_reached_level(
    family_cents,
    thresholds,
  )
  allocations = bonus_allocations(
    pool_cents,
    distribution,
  )

  ranking = []

  for index, uid in enumerate(top5, start=1):
    ranking.append({
      "user_id": uid,
      "rank": index,
      "points": format_points(points[uid]),
      "participations": participations[uid],
      "amount_cents": allocations[index - 1],
    })

  return {
    "start_at": start_at,
    "end_at": end_at,
    "thresholds": thresholds,
    "distribution": [str(x) for x in distribution],
    "family_cents": family_cents,
    "level": level,
    "pool_cents": pool_cents,
    "ranking": ranking,
  }


def bonus_period_text(
  start_at: str,
  end_at: str,
) -> str:
  start_day = local_date_from_iso(start_at)
  end_day = local_date_from_iso(end_at)

  if start_day and end_day:
    return (
      f"{start_day.strftime('%d.%m.%Y')} \u2014 "
      f"{end_day.strftime('%d.%m.%Y')}"
    )

  return f"{start_at} \u2014 {end_at}"


def build_bonus_preview_embed(
  guild_id: int,
  end_at: Optional[str] = None,
) -> discord.Embed:
  data = bonus_preview_data(guild_id, end_at)

  level_text = (
    str(data["level"])
    if data["level"]
    else "\u043d\u0435 \u0434\u043e\u0441\u044f\u0433\u043d\u0443\u0442\u043e"
  )

  embed = discord.Embed(
    title="\U0001f4ca \u041f\u041e\u0422\u041e\u0427\u041d\u0418\u0419 \u0420\u041e\u0417\u0420\u0410\u0425\u0423\u041d\u041e\u041a \u041f\u0420\u0415\u041c\u0406\u0419",
    description=(
      f"\u041f\u0435\u0440\u0456\u043e\u0434: **{bonus_period_text(data['start_at'], data['end_at'])}**\n"
      f"\u0424\u043e\u043d\u0434 \u0441\u0456\u043c'\u0457 \u0437\u0430\u0440\u043e\u0431\u0438\u0432: **{format_cents(data['family_cents'])}**\n"
      f"\u0414\u043e\u0441\u044f\u0433\u043d\u0443\u0442\u0438\u0439 \u043f\u043e\u0440\u0456\u0433: **{level_text}**\n"
      f"\u041f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434: **{format_cents(data['pool_cents'])}**"
    ),
    color=discord.Color.gold(),
  )

  if not bonus_config_ready(guild_id):
    embed.add_field(
      name="\u26a0\ufe0f \u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u043d\u043d\u044f \u043d\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0456",
      value="\u0417\u0430\u0434\u0430\u0439 3 \u043f\u043e\u0440\u043e\u0433\u0438/\u0444\u043e\u043d\u0434\u0438 \u0442\u0430 \u0440\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5.",
      inline=False,
    )

  if data["ranking"]:
    medals = ["\U0001f947", "\U0001f948", "\U0001f949", "4\ufe0f\u20e3", "5\ufe0f\u20e3"]
    lines = []

    for row in data["ranking"]:
      lines.append(
        f"{medals[row['rank'] - 1]} <@{row['user_id']}> \u2022 "
        f"**{row['points']} \u0431\u0430\u043b\u0456\u0432** \u2022 "
        f"**{format_cents(row['amount_cents'])}**"
      )

    embed.add_field(
      name="\U0001f3c6 \u0422\u041e\u041f-5",
      value="\n".join(lines),
      inline=False,
    )
  else:
    embed.add_field(
      name="\U0001f3c6 \u0422\u041e\u041f-5",
      value="\u0423 \u0446\u044c\u043e\u043c\u0443 \u043f\u0435\u0440\u0456\u043e\u0434\u0456 \u0449\u0435 \u043d\u0435\u043c\u0430\u0454 \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u0443.",
      inline=False,
    )

  return embed


def build_bonus_panel_embed(guild_id: int) -> discord.Embed:
  start_at = bonus_ensure_start(guild_id)
  thresholds = bonus_get_thresholds(guild_id)
  distribution = bonus_get_distribution(guild_id)
  auto_enabled = (
    db.get_setting(guild_id, BONUS_SETTING_AUTO) == "1"
  )

  next_close = bonus_next_close(guild_id, start_at)
  family_cents = db.family_fund_earned_between(
    guild_id,
    start_at,
    utc_now_iso(),
  )
  level, pool_cents = bonus_reached_level(
    family_cents,
    thresholds,
  )

  start_ts = iso_to_unix(start_at)
  next_ts = (
    int(next_close.timestamp())
    if next_close
    else None
  )

  level_text = str(level) if level else "\u043d\u0435 \u0434\u043e\u0441\u044f\u0433\u043d\u0443\u0442\u043e"
  auto_text = "\u2705 \u0423\u0432\u0456\u043c\u043a\u043d\u0435\u043d\u0430" if auto_enabled else "\u26d4 \u0412\u0438\u043c\u043a\u043d\u0435\u043d\u0430"
  next_text = (
    f"<t:{next_ts}:f>"
    if next_ts
    else "\u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e"
  )

  embed = discord.Embed(
    title="\U0001f3c6 \u041f\u0420\u0415\u041c\u0406\u042e\u0412\u0410\u041d\u041d\u042f \u0421\u0406\u041c'\u0407",
    description=(
      f"\u041f\u043e\u0447\u0430\u0442\u043e\u043a \u043f\u0435\u0440\u0456\u043e\u0434\u0443: **<t:{start_ts}:f>**\n"
      f"\u0424\u043e\u043d\u0434 \u0437\u0430 \u043f\u0435\u0440\u0456\u043e\u0434: **{format_cents(family_cents)}**\n"
      f"\u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u043f\u043e\u0440\u0456\u0433: **{level_text}**\n"
      f"\u041f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434 \u0437\u0430\u0440\u0430\u0437: **{format_cents(pool_cents)}**\n"
      f"\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u043a\u0430: **{auto_text}**\n"
      f"\u041d\u0430\u0441\u0442\u0443\u043f\u043d\u0435 \u0437\u0430\u043a\u0440\u0438\u0442\u0442\u044f: **{next_text}**"
    ),
    color=discord.Color.gold(),
  )

  if thresholds:
    lines = [
      (
        f"{index}. {format_money_dollars(item['target_dollars'])}$ "
        f"\u2192 {format_money_dollars(item['pool_dollars'])}$"
      )
      for index, item in enumerate(
        thresholds,
        start=1,
      )
    ]
    embed.add_field(
      name="\U0001f3e6 \u041f\u043e\u0440\u043e\u0433\u0438 \u2192 \u043f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434",
      value="\n".join(lines),
      inline=False,
    )
  else:
    embed.add_field(
      name="\U0001f3e6 \u041f\u043e\u0440\u043e\u0433\u0438 \u2192 \u043f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434",
      value="\u0429\u0435 \u043d\u0435 \u043d\u0430\u043b\u0430\u0448\u0442\u043e\u0432\u0430\u043d\u043e.",
      inline=False,
    )

  if distribution:
    embed.add_field(
      name="\U0001f3c6 \u0420\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5",
      value=" \u2022 ".join(
        f"{index}: {value}%"
        for index, value in enumerate(
          distribution,
          start=1,
        )
      ),
      inline=False,
    )

  if not BONUS_RESULTS_CHANNEL_ID:
    embed.set_footer(
      text=(
        "\u26a0\ufe0f BONUS_RESULTS_CHANNEL_ID \u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e \u2014 "
        "\u043f\u0456\u0434\u0441\u0443\u043c\u043a\u0438 \u043d\u0435 \u0437\u043c\u043e\u0436\u0443\u0442\u044c \u043f\u0443\u0431\u043b\u0456\u043a\u0443\u0432\u0430\u0442\u0438\u0441\u044f \u0432 \u043a\u0430\u043d\u0430\u043b."
      )
    )

  return embed


def build_bonus_settings_embed(
  guild_id: int,
) -> discord.Embed:
  thresholds = bonus_get_thresholds(guild_id)
  distribution = bonus_get_distribution(guild_id)

  embed = discord.Embed(
    title="\U0001f3e6 \u041d\u0410\u041b\u0410\u0428\u0422\u0423\u0412\u0410\u041d\u041d\u042f \u041f\u0420\u0415\u041c\u0406\u042e\u0412\u0410\u041d\u041d\u042f",
    description=(
      "\u0422\u0443\u0442 \u0432 \u043e\u0434\u043d\u043e\u043c\u0443 \u0440\u043e\u0437\u0434\u0456\u043b\u0456 \u0437\u0430\u0434\u0430\u044e\u0442\u044c\u0441\u044f 3 \u043f\u043e\u0440\u043e\u0433\u0438 \u0444\u043e\u043d\u0434\u0443, "
      "\u043f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434 \u043a\u043e\u0436\u043d\u043e\u0433\u043e \u043f\u043e\u0440\u043e\u0433\u0443 \u0442\u0430 \u0440\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u043c\u0456\u0436 \u0422\u041e\u041f-5."
    ),
    color=discord.Color.blurple(),
  )

  if thresholds:
    embed.add_field(
      name="3 \u043f\u043e\u0440\u043e\u0433\u0438",
      value="\n".join(
        (
          f"{index}. \u0424\u043e\u043d\u0434 \u0441\u0456\u043c'\u0457 "
          f"**{format_money_dollars(item['target_dollars'])}$** "
          f"\u2192 \u043f\u0440\u0435\u043c\u0456\u0457 "
          f"**{format_money_dollars(item['pool_dollars'])}$**"
        )
        for index, item in enumerate(
          thresholds,
          start=1,
        )
      ),
      inline=False,
    )
  else:
    embed.add_field(
      name="3 \u043f\u043e\u0440\u043e\u0433\u0438",
      value="\u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u0456.",
      inline=False,
    )

  if distribution:
    embed.add_field(
      name="\u0420\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5",
      value="\n".join(
        f"{index} \u043c\u0456\u0441\u0446\u0435: **{value}%**"
        for index, value in enumerate(
          distribution,
          start=1,
        )
      ),
      inline=False,
    )
  else:
    embed.add_field(
      name="\u0420\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5",
      value="\u041d\u0435 \u0437\u0430\u0434\u0430\u043d\u0438\u0439.",
      inline=False,
    )

  return embed


def build_bonus_period_embed(
  guild_id: int,
) -> discord.Embed:
  start_at = bonus_ensure_start(guild_id)
  start_ts = iso_to_unix(start_at)
  schedule = bonus_schedule_values(guild_id)
  next_close = bonus_next_close(
    guild_id,
    start_at,
  )

  next_ts = (
    int(next_close.timestamp())
    if next_close
    else None
  )

  if schedule:
    weekday, hour, minute = schedule
    schedule_text = (
      f"{bonus_weekday_label(weekday)} "
      f"{hour:02d}:{minute:02d}"
    )
  else:
    schedule_text = "\u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e"

  next_text = (
    f"<t:{next_ts}:f>"
    if next_ts
    else "\u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u043e"
  )

  return discord.Embed(
    title="\U0001f4c5 \u041f\u0415\u0420\u0406\u041e\u0414 \u041f\u0420\u0415\u041c\u0406\u042e\u0412\u0410\u041d\u041d\u042f",
    description=(
      f"\u041f\u043e\u0447\u0430\u0442\u043e\u043a: **<t:{start_ts}:f>**\n"
      f"\u0410\u0432\u0442\u043e\u0437\u0430\u043a\u0440\u0438\u0442\u0442\u044f: **{schedule_text}**\n"
      f"\u041d\u0430\u0441\u0442\u0443\u043f\u043d\u0435 \u0437\u0430\u043a\u0440\u0438\u0442\u0442\u044f: **{next_text}**\n\n"
      "\u041f\u0440\u0438 \u0437\u0430\u043a\u0440\u0438\u0442\u0442\u0456 \u0431\u043e\u0442 \u0441\u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u0444\u0456\u043a\u0441\u0443\u0454 \u0444\u043e\u043d\u0434 \u0456 \u0422\u041e\u041f-5, "
      "\u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0443\u0454 \u043f\u0440\u0435\u043c\u0456\u0457, \u0430 \u043f\u043e\u0442\u0456\u043c \u043e\u0431\u043d\u0443\u043b\u044f\u0454 \u0440\u0435\u0439\u0442\u0438\u043d\u0433 "
      "\u043d\u0430 \u043d\u043e\u0432\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434."
    ),
    color=discord.Color.blurple(),
  )


def build_bonus_history_embed(
  guild_id: int,
) -> discord.Embed:
  periods = db.bonus_periods_for_guild(
    guild_id,
    5,
  )
  manual = db.manual_bonus_history(
    guild_id,
    8,
  )

  embed = discord.Embed(
    title="\U0001f4dc \u0406\u0421\u0422\u041e\u0420\u0406\u042f \u041f\u0420\u0415\u041c\u0406\u042e\u0412\u0410\u041d\u042c",
    color=discord.Color.dark_gold(),
  )

  if periods:
    for period in periods:
      awards = db.bonus_awards_for_period(
        period["id"]
      )

      lines = [
        (
          f"\u0424\u043e\u043d\u0434 \u0441\u0456\u043c'\u0457: **{format_cents(period['family_earned_cents'])}**\n"
          f"\u041f\u043e\u0440\u0456\u0433: **{period['threshold_level'] or '\u2014'}** \u2022 "
          f"\u041f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434: **{format_cents(period['prize_pool_cents'])}**"
        )
      ]

      if awards:
        lines.append("")
        medals = ["\U0001f947", "\U0001f948", "\U0001f949", "4\ufe0f\u20e3", "5\ufe0f\u20e3"]

        for award in awards[:5]:
          rank = int(award["rank"] or 0)
          medal = (
            medals[rank - 1]
            if 1 <= rank <= 5
            else "\U0001f3c6"
          )
          status = (
            "\u2705"
            if award["status"] == "paid"
            else "\U0001f4b0"
          )
          points_text = award["points_text"] or "0"

          lines.append(
            f"{medal} <@{award['user_id']}> \u2022 "
            f"{points_text} \u0431\u0430\u043b\u0456\u0432 \u2022 "
            f"**{format_cents(award['amount_cents'])}** {status}"
          )
      else:
        lines.append("")
        lines.append(
          "\u041f\u0440\u0435\u043c\u0456\u0457 \u043d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0443\u0432\u0430\u043b\u0438\u0441\u044c."
        )

      embed.add_field(
        name=(
          f"#{period['id']} \u2022 "
          f"{bonus_period_text(period['start_at'], period['end_at'])}"
        ),
        value="\n".join(lines),
        inline=False,
      )
  else:
    embed.add_field(
      name="\u0417\u0430\u043a\u0440\u0438\u0442\u0456 \u043f\u0435\u0440\u0456\u043e\u0434\u0438",
      value="\u0429\u0435 \u043d\u0435\u043c\u0430\u0454.",
      inline=False,
    )

  if manual:
    lines = []

    for row in manual:
      status = (
        "\u2705 \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e"
        if row["status"] == "paid"
        else "\U0001f4b0 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438"
      )
      note = (
        f" \u2022 {row['note']}"
        if row["note"]
        else ""
      )

      lines.append(
        f"<@{row['user_id']}> \u2014 "
        f"**{format_cents(row['amount_cents'])}** \u2022 "
        f"{status}{note}"
      )

    embed.add_field(
      name="\u2795 \u0420\u0443\u0447\u043d\u0456 \u043f\u0440\u0435\u043c\u0456\u0457",
      value="\n".join(lines),
      inline=False,
    )

  return embed


def build_bonus_results_embed(
  data: dict,
  period_id: int,
) -> discord.Embed:
  level_text = (
    str(data["level"])
    if data["level"]
    else "\u043d\u0435 \u0434\u043e\u0441\u044f\u0433\u043d\u0443\u0442\u043e"
  )

  embed = discord.Embed(
    title="\U0001f3c6 \u041f\u0406\u0414\u0421\u0423\u041c\u041a\u0418 \u041f\u0420\u0415\u041c\u0406\u042e\u0412\u0410\u041d\u041d\u042f",
    description=(
      f"\u041f\u0435\u0440\u0456\u043e\u0434: **{bonus_period_text(data['start_at'], data['end_at'])}**\n"
      f"\u0424\u043e\u043d\u0434 \u0441\u0456\u043c'\u0457 \u0437\u0430\u0440\u043e\u0431\u0438\u0432: **{format_cents(data['family_cents'])}**\n"
      f"\u0414\u043e\u0441\u044f\u0433\u043d\u0443\u0442\u0438\u0439 \u043f\u043e\u0440\u0456\u0433: **{level_text}**\n"
      f"\u041f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434: **{format_cents(data['pool_cents'])}**"
    ),
    color=discord.Color.gold(),
  )

  if data["ranking"]:
    medals = ["\U0001f947", "\U0001f948", "\U0001f949", "4\ufe0f\u20e3", "5\ufe0f\u20e3"]
    lines = []

    for row in data["ranking"]:
      lines.append(
        f"{medals[row['rank'] - 1]} <@{row['user_id']}> \u2022 "
        f"**{row['points']} \u0431\u0430\u043b\u0456\u0432** \u2022 "
        f"**{format_cents(row['amount_cents'])}**"
      )

    embed.add_field(
      name="\u0422\u041e\u041f-5 \u0422\u0418\u0416\u041d\u042f",
      value="\n".join(lines),
      inline=False,
    )

  if data["level"] == 0:
    embed.add_field(
      name="\u041f\u0440\u0435\u043c\u0456\u0457",
      value=(
        "\u041f\u0435\u0440\u0448\u0438\u0439 \u043f\u043e\u0440\u0456\u0433 \u0444\u043e\u043d\u0434\u0443 \u043d\u0435 \u0434\u043e\u0441\u044f\u0433\u043d\u0443\u0442\u043e \u2014 "
        "\u043f\u0440\u0435\u043c\u0456\u0457 \u0437\u0430 \u0446\u0435\u0439 \u043f\u0435\u0440\u0456\u043e\u0434 \u043d\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0456."
      ),
      inline=False,
    )

  embed.set_footer(
    text=f"\u041f\u0435\u0440\u0456\u043e\u0434 \u043f\u0440\u0435\u043c\u0456\u044e\u0432\u0430\u043d\u043d\u044f #{period_id}"
  )

  return embed


async def send_bonus_results(
  bot_instance: commands.Bot,
  guild: discord.Guild,
  embed: discord.Embed,
):
  if not BONUS_RESULTS_CHANNEL_ID:
    return

  channel = guild.get_channel(
    BONUS_RESULTS_CHANNEL_ID
  )

  if channel is None:
    try:
      channel = await bot_instance.fetch_channel(
        BONUS_RESULTS_CHANNEL_ID
      )
    except discord.DiscordException:
      return

  if hasattr(channel, "send"):
    try:
      await channel.send(embed=embed)
    except discord.DiscordException as exc:
      print(f"[BONUS] Could not send results: {exc}")


async def close_bonus_period(
  guild: discord.Guild,
  closed_by: Optional[int],
  close_mode: str,
  end_local: Optional[datetime] = None,
  bot_instance: Optional[commands.Bot] = None,
):
  if not bonus_config_ready(guild.id):
    raise ValueError(
      "\u0421\u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u043d\u0430\u043b\u0430\u0448\u0442\u0443\u0439 3 \u043f\u043e\u0440\u043e\u0433\u0438 \u0442\u0430 \u0440\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5."
    )

  start_at = bonus_ensure_start(guild.id)
  end_local = end_local or datetime.now(LOCAL_TZ)

  if end_local.tzinfo is None:
    end_local = end_local.replace(
      tzinfo=LOCAL_TZ
    )

  end_at = (
    end_local
    .astimezone(timezone.utc)
    .isoformat()
  )

  try:
    start_dt = datetime.fromisoformat(
      start_at
    )
    if start_dt.tzinfo is None:
      start_dt = start_dt.replace(
        tzinfo=timezone.utc
      )
  except Exception:
    raise ValueError(
      "\u041d\u0435\u043a\u043e\u0440\u0435\u043a\u0442\u043d\u0430 \u0434\u0430\u0442\u0430 \u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u043f\u0435\u0440\u0456\u043e\u0434\u0443."
    )

  if start_dt >= datetime.fromisoformat(end_at):
    raise ValueError(
      "\u041a\u0456\u043d\u0435\u0446\u044c \u043f\u0435\u0440\u0456\u043e\u0434\u0443 \u043c\u0430\u0454 \u0431\u0443\u0442\u0438 \u043f\u0456\u0437\u043d\u0456\u0448\u0435 \u0437\u0430 \u0439\u043e\u0433\u043e \u043f\u043e\u0447\u0430\u0442\u043e\u043a."
    )

  data = bonus_preview_data(
    guild.id,
    end_at,
  )

  awards = [
    (
      row["user_id"],
      row["amount_cents"],
      row["rank"],
      row["points"],
    )
    for row in data["ranking"]
    if row["amount_cents"] > 0
  ]

  period_id = db.create_bonus_period_and_awards(
    guild.id,
    data["start_at"],
    data["end_at"],
    data["family_cents"],
    data["level"],
    data["pool_cents"],
    json.dumps(
      data["thresholds"],
      ensure_ascii=True,
    ),
    json.dumps(
      data["distribution"],
      ensure_ascii=True,
    ),
    json.dumps(
      data["ranking"],
      ensure_ascii=True,
    ),
    closed_by,
    close_mode,
    awards,
  )

  result_embed = build_bonus_results_embed(
    data,
    period_id,
  )

  if bot_instance is not None:
    await send_bonus_results(
      bot_instance,
      guild,
      result_embed,
    )

  await audit_log(
    guild,
    "\U0001f3c6 \u041f\u0435\u0440\u0456\u043e\u0434 \u043f\u0440\u0435\u043c\u0456\u044e\u0432\u0430\u043d\u043d\u044f \u0437\u0430\u043a\u0440\u0438\u0442\u043e",
    (
      f"\u041f\u0435\u0440\u0456\u043e\u0434: {bonus_period_text(data['start_at'], data['end_at'])}\n"
      f"\u0424\u043e\u043d\u0434: {format_cents(data['family_cents'])}\n"
      f"\u041f\u043e\u0440\u0456\u0433: {data['level'] or '\u2014'}\n"
      f"\u041f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434: {format_cents(data['pool_cents'])}\n"
      f"\u0420\u0435\u0436\u0438\u043c: {close_mode}"
    ),
    discord.Color.gold(),
  )

  return period_id, data, result_embed


class BonusStartModal(discord.ui.Modal):
  def __init__(self, guild_id: int):
    super().__init__(
      title="\u041f\u043e\u0447\u0430\u0442\u043e\u043a \u043f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u043f\u0435\u0440\u0456\u043e\u0434\u0443"
    )
    self.guild_id = guild_id

    current = bonus_get_start(guild_id)
    current_value = ""

    if current:
      try:
        dt = datetime.fromisoformat(current)
        if dt.tzinfo is None:
          dt = dt.replace(
            tzinfo=timezone.utc
          )
        current_value = (
          dt.astimezone(LOCAL_TZ)
          .strftime("%d.%m.%Y %H:%M")
        )
      except Exception:
        pass

    self.start_value = discord.ui.TextInput(
      label="\u041f\u043e\u0447\u0430\u0442\u043e\u043a \u043f\u0435\u0440\u0456\u043e\u0434\u0443",
      placeholder="09.09.2026 00:00",
      default=current_value or None,
      required=True,
      max_length=16,
    )
    self.add_item(self.start_value)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    try:
      local_dt = bonus_parse_local_datetime(
        str(self.start_value)
      )

      if local_dt > datetime.now(LOCAL_TZ):
        raise ValueError(
          "\u041f\u043e\u0447\u0430\u0442\u043e\u043a \u043f\u0435\u0440\u0456\u043e\u0434\u0443 \u043d\u0435 \u043c\u043e\u0436\u0435 \u0431\u0443\u0442\u0438 \u0432 \u043c\u0430\u0439\u0431\u0443\u0442\u043d\u044c\u043e\u043c\u0443."
        )

      iso_value = (
        local_dt
        .astimezone(timezone.utc)
        .isoformat()
      )

      db.set_setting(
        self.guild_id,
        BONUS_SETTING_START,
        iso_value,
      )
      db.set_setting(
        self.guild_id,
        "rating_reset_at",
        iso_value,
      )

      await interaction.response.edit_message(
        embed=build_bonus_period_embed(
          self.guild_id
        ),
        view=BonusPeriodView(
          self.guild_id
        ),
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class BonusScheduleModal(discord.ui.Modal):
  def __init__(self, guild_id: int):
    super().__init__(
      title="\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u0435 \u0437\u0430\u043a\u0440\u0438\u0442\u0442\u044f \u043f\u0435\u0440\u0456\u043e\u0434\u0443"
    )
    self.guild_id = guild_id

    schedule = bonus_schedule_values(
      guild_id
    )
    weekday_default = ""
    time_default = "00:00"

    if schedule:
      weekday, hour, minute = schedule
      weekday_default = bonus_weekday_label(
        weekday
      )
      time_default = (
        f"{hour:02d}:{minute:02d}"
      )

    self.weekday = discord.ui.TextInput(
      label="\u0414\u0435\u043d\u044c \u0442\u0438\u0436\u043d\u044f",
      placeholder="\u041f\u043d / \u0412\u0442 / ... / \u041d\u0434",
      default=weekday_default or None,
      required=True,
      max_length=12,
    )

    self.time_value = discord.ui.TextInput(
      label="\u0427\u0430\u0441",
      placeholder="00:00",
      default=time_default,
      required=True,
      max_length=5,
    )

    self.add_item(self.weekday)
    self.add_item(self.time_value)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    try:
      weekday = bonus_parse_weekday(
        str(self.weekday)
      )
      hour, minute = bonus_parse_time(
        str(self.time_value)
      )

      db.set_setting(
        self.guild_id,
        BONUS_SETTING_WEEKDAY,
        str(weekday),
      )
      db.set_setting(
        self.guild_id,
        BONUS_SETTING_TIME,
        f"{hour:02d}:{minute:02d}",
      )

      await interaction.response.edit_message(
        embed=build_bonus_period_embed(
          self.guild_id
        ),
        view=BonusPeriodView(
          self.guild_id
        ),
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class BonusThresholdsModal(discord.ui.Modal):
  def __init__(self, guild_id: int):
    super().__init__(
      title="3 \u043f\u043e\u0440\u043e\u0433\u0438 \u0442\u0430 \u043f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0456 \u0444\u043e\u043d\u0434\u0438"
    )
    self.guild_id = guild_id
    current = bonus_get_thresholds(
      guild_id
    )
    self.inputs = []

    for index in range(3):
      default = None

      if len(current) == 3:
        default = (
          f"{current[index]['target_dollars']} | "
          f"{current[index]['pool_dollars']}"
        )

      item = discord.ui.TextInput(
        label=(
          f"\u041f\u043e\u0440\u0456\u0433 {index + 1}: "
          "\u0444\u043e\u043d\u0434 | \u043f\u0440\u0435\u043c\u0456\u0457"
        ),
        placeholder="1000000 | 200000",
        default=default,
        required=True,
        max_length=50,
      )

      self.inputs.append(item)
      self.add_item(item)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    try:
      thresholds = []

      for item in self.inputs:
        raw = str(item).strip()
        parts = re.split(
          r"[|;/]",
          raw,
        )

        if len(parts) != 2:
          raise ValueError(
            "\u041a\u043e\u0436\u0435\u043d \u0440\u044f\u0434\u043e\u043a: \u043f\u043e\u0440\u0456\u0433 | \u043f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u043e\u043d\u0434"
          )

        target = parse_money(parts[0])
        pool = parse_money(parts[1])

        thresholds.append({
          "target_dollars": target,
          "pool_dollars": pool,
        })

      targets = [
        item["target_dollars"]
        for item in thresholds
      ]

      if (
        targets != sorted(targets)
        or len(set(targets)) != 3
      ):
        raise ValueError(
          "\u041f\u043e\u0440\u043e\u0433\u0438 \u043c\u0430\u044e\u0442\u044c \u0437\u0440\u043e\u0441\u0442\u0430\u0442\u0438: 1 < 2 < 3."
        )

      db.set_setting(
        self.guild_id,
        BONUS_SETTING_THRESHOLDS,
        json.dumps(
          thresholds,
          ensure_ascii=True,
        ),
      )

      await interaction.response.edit_message(
        embed=build_bonus_settings_embed(
          self.guild_id
        ),
        view=BonusSettingsView(
          self.guild_id
        ),
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class BonusDistributionModal(discord.ui.Modal):
  def __init__(self, guild_id: int):
    super().__init__(
      title="\u0420\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5"
    )
    self.guild_id = guild_id
    current = bonus_get_distribution(
      guild_id
    )
    default = (
      ", ".join(str(x) for x in current)
      if current
      else None
    )

    self.values_input = discord.ui.TextInput(
      label="1, 2, 3, 4, 5 \u043c\u0456\u0441\u0446\u0435 (%)",
      placeholder="35, 25, 18, 13, 9",
      default=default,
      required=True,
      max_length=80,
    )
    self.add_item(self.values_input)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    try:
      parts = [
        x.strip().replace("%", "")
        for x in re.split(
          r"[,;]",
          str(self.values_input),
        )
        if x.strip()
      ]

      if len(parts) != 5:
        raise ValueError(
          "\u041f\u043e\u0442\u0440\u0456\u0431\u043d\u043e \u0440\u0456\u0432\u043d\u043e 5 \u0437\u043d\u0430\u0447\u0435\u043d\u044c \u2014 "
          "\u0434\u043b\u044f \u043c\u0456\u0441\u0446\u044c 1\u20135."
        )

      values = [
        Decimal(x)
        for x in parts
      ]

      if any(x < 0 for x in values):
        raise ValueError(
          "\u0412\u0456\u0434\u0441\u043e\u0442\u043a\u0438 \u043d\u0435 \u043c\u043e\u0436\u0443\u0442\u044c \u0431\u0443\u0442\u0438 \u0432\u0456\u0434'\u0454\u043c\u043d\u0438\u043c\u0438."
        )

      if (
        sum(values, Decimal("0"))
        != Decimal("100")
      ):
        raise ValueError(
          "\u0421\u0443\u043c\u0430 \u0440\u043e\u0437\u043f\u043e\u0434\u0456\u043b\u0443 \u043c\u0430\u0454 \u0431\u0443\u0442\u0438 \u0440\u0456\u0432\u043d\u043e 100%."
        )

      db.set_setting(
        self.guild_id,
        BONUS_SETTING_DISTRIBUTION,
        json.dumps(
          [str(x) for x in values],
          ensure_ascii=True,
        ),
      )

      await interaction.response.edit_message(
        embed=build_bonus_settings_embed(
          self.guild_id
        ),
        view=BonusSettingsView(
          self.guild_id
        ),
      )
    except Exception as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class ManualBonusModal(discord.ui.Modal):
  def __init__(
    self,
    guild_id: int,
    user_id: int,
    status: str,
  ):
    super().__init__(
      title="\u0420\u0443\u0447\u043d\u0430 \u043f\u0440\u0435\u043c\u0456\u044f"
    )
    self.guild_id = guild_id
    self.user_id = user_id
    self.status = status

    self.amount = discord.ui.TextInput(
      label="\u0421\u0443\u043c\u0430 \u043f\u0440\u0435\u043c\u0456\u0457",
      placeholder="200000",
      required=True,
      max_length=30,
    )

    self.note = discord.ui.TextInput(
      label="\u041f\u0440\u0438\u043c\u0456\u0442\u043a\u0430 (\u043d\u0435\u043e\u0431\u043e\u0432'\u044f\u0437\u043a\u043e\u0432\u043e)",
      placeholder=(
        "\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: \u0441\u0442\u0430\u0440\u0430 \u043f\u0440\u0435\u043c\u0456\u044f "
        "\u0434\u043e \u0437\u0430\u043f\u0443\u0441\u043a\u0443 \u0441\u0438\u0441\u0442\u0435\u043c\u0438"
      ),
      required=False,
      max_length=300,
      style=discord.TextStyle.paragraph,
    )

    self.add_item(self.amount)
    self.add_item(self.note)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    try:
      dollars = parse_money(
        str(self.amount)
      )

      bonus_id = db.add_manual_bonus(
        self.guild_id,
        self.user_id,
        dollars * 100,
        self.status,
        str(self.note),
        interaction.user.id,
      )

      status_text = (
        "\u2705 \u0412\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e"
        if self.status == "paid"
        else "\U0001f4b0 \u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438"
      )

      await interaction.response.edit_message(
        content=(
          f"\u2705 \u0420\u0443\u0447\u043d\u0443 \u043f\u0440\u0435\u043c\u0456\u044e #{bonus_id} \u0434\u043e\u0434\u0430\u043d\u043e "
          f"\u0434\u043b\u044f <@{self.user_id}>: "
          f"**{format_cents(dollars * 100)}**\n"
          f"\u0421\u0442\u0430\u0442\u0443\u0441: **{status_text}**"
        ),
        embed=None,
        view=BonusBackView(
          self.guild_id
        ),
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class ManualBonusUserSelect(discord.ui.UserSelect):
  def __init__(self, guild_id: int):
    self.guild_id = guild_id
    super().__init__(
      placeholder="\u041e\u0431\u0440\u0430\u0442\u0438 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430",
      min_values=1,
      max_values=1,
      row=0,
    )

  async def callback(
    self,
    interaction: discord.Interaction,
  ):
    user = self.values[0]

    await interaction.response.edit_message(
      content=(
        f"\u0423\u0447\u0430\u0441\u043d\u0438\u043a: <@{user.id}>\n"
        "\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u0441\u0442\u0430\u0442\u0443\u0441 \u0440\u0443\u0447\u043d\u043e\u0457 \u043f\u0440\u0435\u043c\u0456\u0457:"
      ),
      embed=None,
      view=ManualBonusStatusView(
        self.guild_id,
        user.id,
      ),
    )


class ManualBonusUserView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.add_item(
      ManualBonusUserSelect(guild_id)
    )

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434",
    emoji="\u21a9\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def back(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content=None,
      embed=build_bonus_panel_embed(
        self.guild_id
      ),
      view=BonusPanelView(
        self.guild_id
      ),
    )


class ManualBonusStatusView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    user_id: int,
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.user_id = user_id

  @discord.ui.button(
    label="\u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438",
    emoji="\U0001f4b0",
    style=discord.ButtonStyle.success,
  )
  async def pending(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      ManualBonusModal(
        self.guild_id,
        self.user_id,
        "pending",
      )
    )

  @discord.ui.button(
    label="\u0412\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e",
    emoji="\u2705",
    style=discord.ButtonStyle.primary,
  )
  async def paid(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      ManualBonusModal(
        self.guild_id,
        self.user_id,
        "paid",
      )
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
    await interaction.response.edit_message(
      content="\u2795 \u041e\u0431\u0435\u0440\u0438 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430 \u0434\u043b\u044f \u0440\u0443\u0447\u043d\u043e\u0457 \u043f\u0440\u0435\u043c\u0456\u0457:",
      embed=None,
      view=ManualBonusUserView(
        self.guild_id
      ),
    )


class BonusBackView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id

  @discord.ui.button(
    label="\u041d\u0430\u0437\u0430\u0434 \u0434\u043e \u043f\u0440\u0435\u043c\u0456\u044e\u0432\u0430\u043d\u043d\u044f",
    emoji="\u21a9\ufe0f",
    style=discord.ButtonStyle.secondary,
  )
  async def back(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content=None,
      embed=build_bonus_panel_embed(
        self.guild_id
      ),
      view=BonusPanelView(
        self.guild_id
      ),
    )


class BonusPeriodView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id

  @discord.ui.button(
    label="\u041f\u043e\u0447\u0430\u0442\u043e\u043a \u043f\u0435\u0440\u0456\u043e\u0434\u0443",
    emoji="\U0001f4c5",
    style=discord.ButtonStyle.primary,
  )
  async def start(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      BonusStartModal(
        self.guild_id
      )
    )

  @discord.ui.button(
    label="\u0413\u0440\u0430\u0444\u0456\u043a \u0437\u0430\u043a\u0440\u0438\u0442\u0442\u044f",
    emoji="\u23f0",
    style=discord.ButtonStyle.primary,
  )
  async def schedule(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      BonusScheduleModal(
        self.guild_id
      )
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
    await interaction.response.edit_message(
      embed=build_bonus_panel_embed(
        self.guild_id
      ),
      view=BonusPanelView(
        self.guild_id
      ),
    )


class BonusSettingsView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id

  @discord.ui.button(
    label="3 \u043f\u043e\u0440\u043e\u0433\u0438 + \u0444\u043e\u043d\u0434\u0438",
    emoji="\U0001f3e6",
    style=discord.ButtonStyle.primary,
  )
  async def thresholds(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      BonusThresholdsModal(
        self.guild_id
      )
    )

  @discord.ui.button(
    label="\u0420\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5",
    emoji="\U0001f3c6",
    style=discord.ButtonStyle.primary,
  )
  async def distribution(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      BonusDistributionModal(
        self.guild_id
      )
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
    await interaction.response.edit_message(
      embed=build_bonus_panel_embed(
        self.guild_id
      ),
      view=BonusPanelView(
        self.guild_id
      ),
    )


class BonusCloseConfirmView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=180)
    self.guild_id = guild_id

  @discord.ui.button(
    label="\u0417\u0430\u043a\u0440\u0438\u0442\u0438 \u043f\u0435\u0440\u0456\u043e\u0434",
    emoji="\u2705",
    style=discord.ButtonStyle.danger,
  )
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if (
      not isinstance(
        interaction.user,
        discord.Member,
      )
      or not management_member(
        interaction.user
      )
    ):
      await interaction.response.send_message(
        "\u274c \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u043e \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    guild = interaction.guild
    if guild is None:
      return

    await interaction.response.edit_message(
      content="\u23f3 \u0417\u0430\u043a\u0440\u0438\u0432\u0430\u044e \u043f\u0440\u0435\u043c\u0456\u0430\u043b\u044c\u043d\u0438\u0439 \u043f\u0435\u0440\u0456\u043e\u0434...",
      embed=None,
      view=None,
    )

    try:
      _, _, result_embed = await close_bonus_period(
        guild,
        interaction.user.id,
        "manual",
        bot_instance=interaction.client,
      )

      await interaction.edit_original_response(
        content=None,
        embed=result_embed,
        view=BonusBackView(
          self.guild_id
        ),
      )
    except ValueError as exc:
      await interaction.edit_original_response(
        content=f"\u274c {exc}",
        embed=None,
        view=BonusBackView(
          self.guild_id
        ),
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
      content=None,
      embed=build_bonus_panel_embed(
        self.guild_id
      ),
      view=BonusPanelView(
        self.guild_id
      ),
    )


class BonusPanelView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id

  @discord.ui.button(
    label="\u041f\u0435\u0440\u0456\u043e\u0434",
    emoji="\U0001f4c5",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def period(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_bonus_period_embed(
        self.guild_id
      ),
      view=BonusPeriodView(
        self.guild_id
      ),
    )

  @discord.ui.button(
    label="\u041d\u0430\u043b\u0430\u0448\u0442\u0443\u0432\u0430\u043d\u043d\u044f \u043f\u0440\u0435\u043c\u0456\u044e\u0432\u0430\u043d\u043d\u044f",
    emoji="\U0001f3e6",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def settings(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_bonus_settings_embed(
        self.guild_id
      ),
      view=BonusSettingsView(
        self.guild_id
      ),
    )

  @discord.ui.button(
    label="\u041f\u043e\u0442\u043e\u0447\u043d\u0438\u0439 \u0440\u043e\u0437\u0440\u0430\u0445\u0443\u043d\u043e\u043a",
    emoji="\U0001f4ca",
    style=discord.ButtonStyle.secondary,
    row=1,
  )
  async def preview(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_bonus_preview_embed(
        self.guild_id
      ),
      view=BonusBackView(
        self.guild_id
      ),
    )

  @discord.ui.button(
    label="\u0417\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u0438 \u043f\u0435\u0440\u0456\u043e\u0434",
    emoji="\u2705",
    style=discord.ButtonStyle.danger,
    row=1,
  )
  async def close_period(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not bonus_config_ready(
      self.guild_id
    ):
      await interaction.response.send_message(
        (
          "\u274c \u0421\u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u043d\u0430\u043b\u0430\u0448\u0442\u0443\u0439 3 \u043f\u043e\u0440\u043e\u0433\u0438 "
          "\u0442\u0430 \u0440\u043e\u0437\u043f\u043e\u0434\u0456\u043b \u0422\u041e\u041f-5."
        ),
        ephemeral=True,
      )
      return

    await interaction.response.edit_message(
      embed=build_bonus_preview_embed(
        self.guild_id
      ),
      view=BonusCloseConfirmView(
        self.guild_id
      ),
    )

  @discord.ui.button(
    label="\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u043a\u0430 ON/OFF",
    emoji="\u2699\ufe0f",
    style=discord.ButtonStyle.secondary,
    row=2,
  )
  async def toggle_auto(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    enabled = (
      db.get_setting(
        self.guild_id,
        BONUS_SETTING_AUTO,
      )
      == "1"
    )

    if not enabled:
      if not bonus_schedule_values(
        self.guild_id
      ):
        await interaction.response.send_message(
          (
            "\u274c \u0421\u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u0437\u0430\u0434\u0430\u0439 \u0434\u0435\u043d\u044c \u0456 \u0447\u0430\u0441 "
            "\u0443 \u0440\u043e\u0437\u0434\u0456\u043b\u0456 \u00ab\u041f\u0435\u0440\u0456\u043e\u0434\u00bb."
          ),
          ephemeral=True,
        )
        return

      if not bonus_config_ready(
        self.guild_id
      ):
        await interaction.response.send_message(
          "\u274c \u0421\u043f\u043e\u0447\u0430\u0442\u043a\u0443 \u043d\u0430\u043b\u0430\u0448\u0442\u0443\u0439 \u043f\u0440\u0435\u043c\u0456\u044e\u0432\u0430\u043d\u043d\u044f.",
          ephemeral=True,
        )
        return

    db.set_setting(
      self.guild_id,
      BONUS_SETTING_AUTO,
      "0" if enabled else "1",
    )

    await interaction.response.edit_message(
      embed=build_bonus_panel_embed(
        self.guild_id
      ),
      view=BonusPanelView(
        self.guild_id
      ),
    )

  @discord.ui.button(
    label="\u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    emoji="\U0001f4dc",
    style=discord.ButtonStyle.secondary,
    row=2,
  )
  async def history(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_bonus_history_embed(
        self.guild_id
      ),
      view=BonusBackView(
        self.guild_id
      ),
    )

  @discord.ui.button(
    label="\u0420\u0443\u0447\u043d\u0430 \u043f\u0440\u0435\u043c\u0456\u044f",
    emoji="\u2795",
    style=discord.ButtonStyle.success,
    row=3,
  )
  async def manual_bonus(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content=(
        "\u2795 \u041e\u0431\u0435\u0440\u0438 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430 \u0434\u043b\u044f \u0440\u0443\u0447\u043d\u043e\u0457 \u043f\u0440\u0435\u043c\u0456\u0457:"
      ),
      embed=None,
      view=ManualBonusUserView(
        self.guild_id
      ),
    )


async def maybe_auto_close_bonus(
  bot_instance: commands.Bot,
  now: datetime,
):
  if not GUILD_ID:
    return

  if (
    db.get_setting(
      GUILD_ID,
      BONUS_SETTING_AUTO,
    )
    != "1"
  ):
    return

  start_at = bonus_get_start(GUILD_ID)

  if (
    not start_at
    or not bonus_config_ready(GUILD_ID)
  ):
    return

  due = bonus_next_close(
    GUILD_ID,
    start_at,
  )

  if due is None or now < due:
    return

  guild = bot_instance.get_guild(
    GUILD_ID
  )
  if guild is None:
    return

  try:
    period_id, _, _ = await close_bonus_period(
      guild,
      None,
      "auto",
      end_local=due,
      bot_instance=bot_instance,
    )
    print(
      f"[BONUS] Auto-closed period #{period_id}"
    )
  except Exception as exc:
    print(
      f"[BONUS] Auto-close error: {exc}"
    )




def register_commands(bot: commands.Bot):
  @bot.tree.command(
    name="premii",
    description="\u041f\u0430\u043d\u0435\u043b\u044c \u043f\u0440\u0435\u043c\u0456\u044e\u0432\u0430\u043d\u043d\u044f \u0441\u0456\u043c'\u0457",
  )
  async def bonus_panel_command(
    interaction: discord.Interaction,
  ):
    if (
      not isinstance(
        interaction.user,
        discord.Member,
      )
      or not management_member(
        interaction.user
      )
    ):
      await interaction.response.send_message(
        "\u274c \u041f\u0430\u043d\u0435\u043b\u044c \u043f\u0440\u0435\u043c\u0456\u044e\u0432\u0430\u043d\u043d\u044f \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
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

    bonus_ensure_start(guild.id)

    await interaction.response.send_message(
      embed=build_bonus_panel_embed(
        guild.id
      ),
      view=BonusPanelView(
        guild.id
      ),
      ephemeral=True,
    )


