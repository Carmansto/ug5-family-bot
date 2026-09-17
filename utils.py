import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Optional

import discord

from config import (
  FAMILY_PERCENT,
  LEADER_ROLE_ID,
  LOCAL_TZ,
  MANAGER_ROLE_IDS,
  TEST_USER_ID,
)


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
  110\u043a / 110k
  1.2\u043c / 1.2m
  """
  s = raw.strip().lower()
  s = s.replace("$", "").replace("\u20b4", "").replace(" ", "").replace("_", "")
  s = s.replace(",", ".")

  multiplier = 1
  if s.endswith(("\u043a", "k")):
    multiplier = 1_000
    s = s[:-1]
  elif s.endswith(("\u043c", "m")):
    multiplier = 1_000_000
    s = s[:-1]

  if not re.fullmatch(r"\d+(\.\d+)?", s):
    raise ValueError("\u041d\u0435\u043a\u043e\u0440\u0435\u043a\u0442\u043d\u0430 \u0441\u0443\u043c\u0430")

  value = int(Decimal(s) * multiplier)
  if value <= 0:
    raise ValueError("\u0421\u0443\u043c\u0430 \u043c\u0430\u0454 \u0431\u0443\u0442\u0438 \u0431\u0456\u043b\u044c\u0448\u043e\u044e \u0437\u0430 0")
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


UA_ALPHABET = "\u0430\u0431\u0432\u0433\u0491\u0434\u0435\u0454\u0436\u0437\u0438\u0456\u0457\u0439\u043a\u043b\u043c\u043d\u043e\u043f\u0440\u0441\u0442\u0443\u0444\u0445\u0446\u0447\u0448\u0449\u044c\u044e\u044f"
UA_ORDER = {char: idx for idx, char in enumerate(UA_ALPHABET)}


def ukrainian_sort_key(text: str):
  s = text.strip().casefold()
  return tuple(
    UA_ORDER.get(char, 1000 + ord(char))
    for char in s
  )


def format_points(value: Fraction) -> str:
  if value.denominator == 1:
    return str(value.numerator)
  txt = f"{float(value):.3f}".rstrip("0").rstrip(".")
  return txt


def format_points_with_word(value: Fraction) -> str:
  """1 \u0431\u0430\u043b, 2 \u0431\u0430\u043b\u0438, 10 \u0431\u0430\u043b\u0456\u0432, 1.65 \u0431\u0430\u043b\u0430."""
  number = format_points(value)

  if value.denominator != 1:
    return f"{number} \u0431\u0430\u043b\u0430"

  n = abs(value.numerator)
  last_two = n % 100
  last = n % 10

  if last_two in (11, 12, 13, 14):
    word = "\u0431\u0430\u043b\u0456\u0432"
  elif last == 1:
    word = "\u0431\u0430\u043b"
  elif last in (2, 3, 4):
    word = "\u0431\u0430\u043b\u0438"
  else:
    word = "\u0431\u0430\u043b\u0456\u0432"

  return f"{number} {word}"


def local_date_from_iso(value: Optional[str]):
  if not value:
    return None

  try:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).date()
  except Exception:
    return None


def format_day(day) -> str:
  weekdays = (
    "\u041f\u043d", "\u0412\u0442", "\u0421\u0440", "\u0427\u0442", "\u041f\u0442", "\u0421\u0431", "\u041d\u0434"
  )
  return f"{weekdays[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


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


PAYMENT_MODE_NORMAL = "normal"
PAYMENT_MODE_REDISTRIBUTE = "redistribute"
PAYMENT_MODE_FAMILY_SHARE = "family_share"
PAYMENT_MODE_LEGACY_FAMILY = "family"


def payment_mode_label(mode: str) -> str:
  labels = {
    PAYMENT_MODE_NORMAL: "\u0417\u0432\u0438\u0447\u0430\u0439\u043d\u0435 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f",
    PAYMENT_MODE_REDISTRIBUTE: "\u0420\u043e\u0437\u0434\u0456\u043b\u0438\u0442\u0438 \u043c\u0456\u0436 \u0440\u0435\u0448\u0442\u043e\u044e",
    PAYMENT_MODE_FAMILY_SHARE: "\u0427\u0430\u0441\u0442\u043a\u0443 \u0432\u0438\u043d\u044f\u0442\u043a\u0456\u0432 \u0443 \u0441\u0456\u043c'\u044e",
    PAYMENT_MODE_LEGACY_FAMILY: "\u041d\u0430 \u0444\u0430\u043c\u0443",
  }
  return labels.get(mode, "\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f")


def calculate_payment(
  gross_dollars: int,
  participant_ids: list[int],
  payment_mode: str = PAYMENT_MODE_NORMAL,
  excluded_payment_ids: Optional[list[int]] = None,
) -> tuple[int, int, dict[int, int], list[int]]:
  """
  Returns:
    family_cents,
    participant_pool_cents,
    payouts_by_user,
    normalized_excluded_ids

  normal:
    FAMILY_PERCENT -> family, rest -> all performers.

  redistribute:
    FAMILY_PERCENT -> family, rest -> performers who were NOT excluded.

  family_share:
    First calculate normal shares for ALL original performers.
    Excluded performers' exact shares are moved to family bank.
    Non-excluded performers keep their original shares.

  legacy family:
    100% -> family.
  """
  if not participant_ids:
    raise ValueError("No participants")

  participant_ids = list(dict.fromkeys(int(uid) for uid in participant_ids))

  excluded_set = {
    int(uid)
    for uid in (excluded_payment_ids or [])
    if int(uid) in participant_ids
  }
  excluded_ids = [
    uid for uid in participant_ids
    if uid in excluded_set
  ]

  if payment_mode == PAYMENT_MODE_LEGACY_FAMILY:
    return gross_dollars * 100, 0, {}, participant_ids[:]

  if payment_mode == PAYMENT_MODE_NORMAL:
    family_cents, net_cents, payouts = split_payment(
      gross_dollars,
      participant_ids,
    )
    return family_cents, net_cents, payouts, []

  if payment_mode == PAYMENT_MODE_REDISTRIBUTE:
    eligible_ids = [
      uid for uid in participant_ids
      if uid not in excluded_set
    ]
    if not eligible_ids:
      raise ValueError("No eligible payout recipients")

    family_cents, net_cents, payouts = split_payment(
      gross_dollars,
      eligible_ids,
    )
    return family_cents, net_cents, payouts, excluded_ids

  if payment_mode == PAYMENT_MODE_FAMILY_SHARE:
    base_family_cents, _, original_payouts = split_payment(
      gross_dollars,
      participant_ids,
    )

    family_cents = base_family_cents
    payouts: dict[int, int] = {}

    for uid in participant_ids:
      amount = original_payouts[uid]
      if uid in excluded_set:
        family_cents += amount
      else:
        payouts[uid] = amount

    net_cents = sum(payouts.values())
    return family_cents, net_cents, payouts, excluded_ids

  raise ValueError("Unknown payment mode")


def calculate_personal_family_contributions(
  gross_dollars: int,
  participant_ids: list[int],
  payment_mode: str,
  excluded_payment_ids: Optional[list[int]] = None,
) -> dict[int, int]:
  """
  \u0420\u0430\u0445\u0443\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u041e\u0421\u041e\u0411\u0418\u0421\u0422\u0406 \u0433\u0440\u043e\u0448\u0456 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430, \u044f\u043a\u0456 \u043f\u0456\u0448\u043b\u0438 \u0432 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457.
  \u0421\u0442\u0430\u043d\u0434\u0430\u0440\u0442\u043d\u0456 FAMILY_PERCENT \u043d\u0435 \u043f\u0440\u0438\u043f\u0438\u0441\u0443\u044e\u0442\u044c\u0441\u044f \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u0456\u0439 \u043b\u044e\u0434\u0438\u043d\u0456.

  \u041d\u0430 \u0444\u0430\u043c\u0443:
    \u043a\u043e\u0436\u043d\u043e\u043c\u0443 \u0437\u0430\u0440\u0430\u0445\u043e\u0432\u0443\u0454\u0442\u044c\u0441\u044f \u0439\u043e\u0433\u043e \u043d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u0430 \u0447\u0430\u0441\u0442\u043a\u0430 \u0437 participant pool.

  \u0427\u0430\u0441\u0442\u043a\u0443 \u0432 \u0441\u0456\u043c'\u044e:
    \u043e\u0441\u043e\u0431\u0438\u0441\u0442\u0438\u043c \u0432\u043d\u0435\u0441\u043a\u043e\u043c \u0454 \u043d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u0430 \u0447\u0430\u0441\u0442\u043a\u0430 \u0441\u0430\u043c\u0435 \u0432\u0438\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0445 \u043b\u044e\u0434\u0435\u0439.

  \u0417\u0432\u0438\u0447\u0430\u0439\u043d\u0430 / \u0420\u043e\u0437\u0434\u0456\u043b\u0438\u0442\u0438 \u043c\u0456\u0436 \u0440\u0435\u0448\u0442\u043e\u044e:
    \u043e\u0441\u043e\u0431\u0438\u0441\u0442\u0438\u0439 \u0432\u043d\u0435\u0441\u043e\u043a = 0.
  """
  if not participant_ids:
    return {}

  participant_ids = list(dict.fromkeys(int(uid) for uid in participant_ids))
  _, _, normal_payouts = split_payment(gross_dollars, participant_ids)
  excluded = set(int(uid) for uid in (excluded_payment_ids or []))

  if payment_mode == PAYMENT_MODE_LEGACY_FAMILY:
    return dict(normal_payouts)

  if payment_mode == PAYMENT_MODE_FAMILY_SHARE:
    return {
      uid: amount
      for uid, amount in normal_payouts.items()
      if uid in excluded
    }

  return {}


def payment_preview_embed(
  row: sqlite3.Row,
  payment_mode: str,
  excluded_ids: Optional[list[int]] = None,
) -> discord.Embed:
  participants = parse_ids(row["participant_ids"])

  family_cents, net_cents, payouts, normalized_excluded = calculate_payment(
    row["price"],
    participants,
    payment_mode,
    excluded_ids,
  )

  embed = discord.Embed(
    title="\U0001f9ee \u041f\u0435\u0440\u0435\u0432\u0456\u0440\u043a\u0430 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f",
    description=(
      f"**{row['contract_name']}**\n"
      f"\u0421\u0443\u043c\u0430 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0443: **{format_money_dollars(row['price'])} $**"
    ),
    color=discord.Color.gold(),
  )

  embed.add_field(
    name="\u0421\u043f\u043e\u0441\u0456\u0431",
    value=payment_mode_label(payment_mode),
    inline=False,
  )
  embed.add_field(
    name="\U0001f3e6 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457",
    value=format_cents(family_cents),
    inline=True,
  )
  embed.add_field(
    name="\U0001f4b0 \u0411\u0443\u0434\u0435 \u043d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c",
    value=format_cents(net_cents),
    inline=True,
  )

  if normalized_excluded and payment_mode != PAYMENT_MODE_LEGACY_FAMILY:
    embed.add_field(
      name="\U0001f6ab \u0411\u0435\u0437 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f",
      value=" ".join(f"<@{uid}>" for uid in normalized_excluded),
      inline=False,
    )

  if payouts:
    lines = [
      f"\u2795 <@{uid}> \u2014 **{format_cents(amount)}**"
      for uid, amount in payouts.items()
    ]
    embed.add_field(
      name="\U0001f464 \u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f",
      value="\n".join(lines),
      inline=False,
    )

  embed.set_footer(
    text="\u041f\u0456\u0441\u043b\u044f \u043f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u0436\u0435\u043d\u043d\u044f \u0441\u0443\u043c\u0438 \u043f\u043e\u0442\u0440\u0430\u043f\u043b\u044f\u0442\u044c \u0443 /payouts. \u0424\u0430\u043a\u0442\u0438\u0447\u043d\u043e \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u043c\u0438 \u0432\u043e\u043d\u0438 \u0449\u0435 \u043d\u0435 \u0432\u0432\u0430\u0436\u0430\u044e\u0442\u044c\u0441\u044f."
  )
  return embed


def management_member(member: discord.Member) -> bool:
  if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
    return True
  return any(role.id in MANAGER_ROLE_IDS for role in member.roles)


def test_command_user(user: discord.abc.User) -> bool:
  return bool(TEST_USER_ID and user.id == TEST_USER_ID)


def has_leader_role(member: discord.Member) -> bool:
  """Exact LEADER_ROLE_ID check. No owner/admin bypass."""
  if not LEADER_ROLE_ID:
    return False
  return any(role.id == LEADER_ROLE_ID for role in member.roles)


def can_close_management_payout(member: discord.Member) -> bool:
  """Management payout close rule: LEADER_ROLE_ID or Discord server owner."""
  if member.guild.owner_id == member.id:
    return True
  return has_leader_role(member)


def management_payout_member(member: discord.Member) -> bool:
  """\u041a\u0435\u0440\u0456\u0432\u043d\u0438\u0439 \u0441\u043a\u043b\u0430\u0434: LEADER_ROLE_ID \u0430\u0431\u043e \u0431\u0443\u0434\u044c-\u044f\u043a\u0430 \u0440\u043e\u043b\u044c \u0456\u0437 MANAGER_ROLE_IDS."""
  if has_leader_role(member):
    return True
  return any(role.id in MANAGER_ROLE_IDS for role in member.roles)


async def fetch_member_safe(
  guild: discord.Guild,
  user_id: int,
) -> Optional[discord.Member]:
  member = guild.get_member(user_id)
  if member is not None:
    return member
  try:
    return await guild.fetch_member(user_id)
  except discord.DiscordException:
    return None


async def payout_is_management(
  guild: discord.Guild,
  user_id: int,
) -> bool:
  member = await fetch_member_safe(guild, user_id)
  return bool(member and management_payout_member(member))


__all__ = ['PAYMENT_MODE_FAMILY_SHARE', 'PAYMENT_MODE_LEGACY_FAMILY', 'PAYMENT_MODE_NORMAL', 'PAYMENT_MODE_REDISTRIBUTE', 'UA_ALPHABET', 'UA_ORDER', 'calculate_payment', 'calculate_personal_family_contributions', 'can_close_management_payout', 'fetch_member_safe', 'format_cents', 'format_day', 'format_money_dollars', 'format_points', 'format_points_with_word', 'has_leader_role', 'iso_to_unix', 'local_date_from_iso', 'management_member', 'management_payout_member', 'parse_ids', 'parse_money', 'payment_mode_label', 'payment_preview_embed', 'payout_is_management', 'split_payment', 'test_command_user', 'ukrainian_sort_key', 'utc_now_iso']
