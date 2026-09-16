import os
import re
import asyncio
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
CONTRACT_CHANNEL_ID = int(os.getenv("CONTRACT_CHANNEL_ID", "0") or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)

# Automatic reporting channels.
RATING_CHANNEL_ID = int(os.getenv("RATING_CHANNEL_ID", "0") or 0)
FAMILY_STATS_CHANNEL_ID = int(os.getenv("FAMILY_STATS_CHANNEL_ID", "0") or 0)

RATING_MORNING_HOUR = int(os.getenv("RATING_MORNING_HOUR", "9") or 9)
RATING_EVENING_HOUR = int(os.getenv("RATING_EVENING_HOUR", "21") or 21)
FAMILY_STATS_HOUR = int(os.getenv("FAMILY_STATS_HOUR", "0") or 0)

# Birthday module.
BIRTHDAY_INPUT_CHANNEL_ID = int(os.getenv("BIRTHDAY_INPUT_CHANNEL_ID", "0") or 0)
BIRTHDAY_ALERT_CHANNEL_ID = int(os.getenv("BIRTHDAY_ALERT_CHANNEL_ID", "0") or 0)
BIRTHDAY_REMINDER_HOUR = int(os.getenv("BIRTHDAY_REMINDER_HOUR", "9") or 9)

# Only this Discord user can run /test-* commands.
TEST_USER_ID = int(os.getenv("TEST_USER_ID", "0") or 0)

# Backward compatible with your current setup.
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or 0)

# Optional: several management roles separated by commas.
MANAGER_ROLE_IDS = {
  int(x.strip())
  for x in os.getenv("MANAGER_ROLE_IDS", "").split(",")
  if x.strip().isdigit()
}
if ADMIN_ROLE_ID:
  MANAGER_ROLE_IDS.add(ADMIN_ROLE_ID)

# \u0420\u043e\u043b\u044c \u043b\u0456\u0434\u0435\u0440\u0430. \u042f\u043a\u0449\u043e \u043e\u043a\u0440\u0435\u043c\u043e \u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u0430 \u2014 \u0432\u0438\u043a\u043e\u0440\u0438\u0441\u0442\u043e\u0432\u0443\u0454\u043c\u043e ADMIN_ROLE_ID.
# MANAGER_ROLE_IDS = \u0440\u043e\u043b\u0456 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0430.
LEADER_ROLE_ID = int(os.getenv("LEADER_ROLE_ID", str(ADMIN_ROLE_ID or 0)) or 0)

DB_PATH = os.getenv("DB_PATH", "contracts.db").strip()
FAMILY_PERCENT = Decimal(os.getenv("FAMILY_PERCENT", os.getenv("FOMO_PERCENT", "15")).strip() or "15")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"
try:
  LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
  LOCAL_TZ = timezone.utc

if not TOKEN:
  raise RuntimeError("DISCORD_TOKEN is empty.")


# ----------------------------
# Helpers
# ----------------------------

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
  deferred_ids: Optional[list[int]] = None,
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
  """Old /debts access rule: LEADER_ROLE_ID or Discord server owner."""
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




# ----------------------------
# Database
# ----------------------------

class Database:
  def __init__(self, path: str):
    parent = os.path.dirname(path)
    if parent:
      os.makedirs(parent, exist_ok=True)

    self.conn = sqlite3.connect(path)
    self.conn.row_factory = sqlite3.Row
    self._init_schema()
    self._migrate_old_contracts_table()

  def _init_schema(self):
    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS contract_types (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE COLLATE NOCASE,
      price INTEGER NOT NULL,
      cooldown TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      created_by INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT
    )
    """)

    # Keeps compatibility with your MVP table.
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
      note TEXT,
      status TEXT NOT NULL DEFAULT 'unpaid',
      payment_mode TEXT NOT NULL DEFAULT 'normal',
      excluded_payment_ids TEXT NOT NULL DEFAULT '[]',
      paid_by INTEGER,
      created_at TEXT NOT NULL,
      paid_at TEXT
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS contract_payouts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      contract_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      amount_cents INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(contract_id, user_id)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS admin_debts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      contract_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      amount_cents INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL,
      settled_at TEXT,
      settled_by INTEGER,
      UNIQUE(contract_id, user_id)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS payment_accruals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      contract_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      amount_cents INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL,
      paid_at TEXT,
      paid_by INTEGER,
      UNIQUE(contract_id, user_id)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS family_contributions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      contract_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      amount_cents INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(contract_id, user_id)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS birthdays (
      guild_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      day INTEGER NOT NULL,
      month INTEGER NOT NULL,
      year INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (guild_id, user_id)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS bot_settings (
      guild_id INTEGER NOT NULL,
      key TEXT NOT NULL,
      value TEXT NOT NULL,
      PRIMARY KEY (guild_id, key)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """)

    self.conn.commit()

  def _migrate_old_contracts_table(self):
    columns = {
      row["name"]
      for row in self.conn.execute("PRAGMA table_info(contracts)").fetchall()
    }

    migrations = {
      "contract_type_id": "ALTER TABLE contracts ADD COLUMN contract_type_id INTEGER",
      "cancelled_by": "ALTER TABLE contracts ADD COLUMN cancelled_by INTEGER",
      "cancelled_at": "ALTER TABLE contracts ADD COLUMN cancelled_at TEXT",
      "fomo_cents": "ALTER TABLE contracts ADD COLUMN fomo_cents INTEGER",
      "net_cents": "ALTER TABLE contracts ADD COLUMN net_cents INTEGER",
      "payment_mode": "ALTER TABLE contracts ADD COLUMN payment_mode TEXT NOT NULL DEFAULT 'normal'",
      "excluded_payment_ids": "ALTER TABLE contracts ADD COLUMN excluded_payment_ids TEXT NOT NULL DEFAULT '[]'",
      "annulled_by": "ALTER TABLE contracts ADD COLUMN annulled_by INTEGER",
      "annulled_at": "ALTER TABLE contracts ADD COLUMN annulled_at TEXT",
      "note": "ALTER TABLE contracts ADD COLUMN note TEXT",
    }

    for name, sql in migrations.items():
      if name not in columns:
        self.conn.execute(sql)

    self.conn.commit()

  def _get_schema_version(self) -> int:
    row = self.conn.execute(
      "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()

    if not row:
      return 0

    try:
      return int(row["value"])
    except (TypeError, ValueError):
      return 0

  def _set_schema_version(self, version: int):
    self.conn.execute("""
    INSERT INTO schema_meta (key, value)
    VALUES ('schema_version', ?)
    ON CONFLICT(key)
    DO UPDATE SET value = excluded.value
    """, (str(version),))
    self.conn.commit()

  def _create_performance_indexes(self):
    statements = [
      """
      CREATE INDEX IF NOT EXISTS idx_contracts_guild_status_created
      ON contracts(guild_id, status, created_at)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_contracts_guild_status_paid
      ON contracts(guild_id, status, paid_at)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_payment_accruals_user_status
      ON payment_accruals(user_id, status)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_payment_accruals_status_contract
      ON payment_accruals(status, contract_id)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_family_contributions_user_created
      ON family_contributions(user_id, created_at)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_admin_debts_user_status
      ON admin_debts(user_id, status)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_birthdays_guild_month_day
      ON birthdays(guild_id, month, day)
      """,
    ]

    for statement in statements:
      self.conn.execute(statement)

    self.conn.commit()

  def run_startup_migrations(self):
    """
    One-time startup migrations.

    V6.11 and older executed historical backfills on every restart.
    On the first V6.12 start they run one final idempotent pass and the
    resulting schema version is saved. Later restarts skip those scans.
    """
    version = self._get_schema_version()

    if version < 1:
      self.backfill_legacy_paid_contracts()
      self._set_schema_version(1)
      version = 1

    if version < 2:
      self.backfill_family_contributions()
      self._set_schema_version(2)
      version = 2

    if version < 3:
      self.migrate_pending_admin_debts_to_accruals()
      self._set_schema_version(3)
      version = 3

    if version < 4:
      self._create_performance_indexes()
      self._set_schema_version(4)
      version = 4

    return version

  def backfill_legacy_paid_contracts(self):
    """
    Backfill only historical paid contracts that predate the accrual system.
    """
    rows = self.conn.execute("""
    SELECT *
    FROM contracts
    WHERE status = 'paid'
    ORDER BY id ASC
    """).fetchall()

    changed = 0

    for row in rows:
      member_ids = parse_ids(row["participant_ids"])
      if not member_ids:
        continue

      accrual_count = self.conn.execute(
        "SELECT COUNT(*) AS cnt FROM payment_accruals WHERE contract_id = ?",
        (row["id"],),
      ).fetchone()["cnt"]

      if accrual_count:
        continue

      payout_count = self.conn.execute(
        "SELECT COUNT(*) AS cnt FROM contract_payouts WHERE contract_id = ?",
        (row["id"],),
      ).fetchone()["cnt"]

      debt_count = self.conn.execute(
        "SELECT COUNT(*) AS cnt FROM admin_debts WHERE contract_id = ?",
        (row["id"],),
      ).fetchone()["cnt"]

      payment_mode = row["payment_mode"] or PAYMENT_MODE_NORMAL
      excluded_ids = parse_ids(row["excluded_payment_ids"] or "[]")
      needs_totals = row["fomo_cents"] is None or row["net_cents"] is None

      try:
        family_cents, net_cents, payouts, _ = calculate_payment(
          row["price"],
          member_ids,
          payment_mode,
          excluded_ids,
        )
      except ValueError:
        continue

      needs_payouts = bool(payouts) and payout_count == 0 and debt_count == 0

      if not needs_totals and not needs_payouts:
        continue

      payout_date = row["paid_at"] or row["created_at"] or utc_now_iso()

      try:
        self.conn.execute("BEGIN IMMEDIATE")

        if needs_totals:
          self.conn.execute("""
          UPDATE contracts
          SET fomo_cents = ?, net_cents = ?
          WHERE id = ?
          """, (family_cents, net_cents, row["id"]))

        if needs_payouts:
          for uid, amount_cents in payouts.items():
            self.conn.execute("""
            INSERT OR IGNORE INTO contract_payouts
            (contract_id, user_id, amount_cents, created_at)
            VALUES (?, ?, ?, ?)
            """, (row["id"], uid, amount_cents, payout_date))

        self.conn.commit()
        changed += 1
      except Exception:
        self.conn.rollback()
        raise

    if changed:
      print(f"[MIGRATION] Backfilled {changed} legacy paid contract(s)")


  def backfill_family_contributions(self):
    """
    \u0421\u0442\u0430\u0440\u0456 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0456 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0438 \u0432\u0436\u0435 \u043c\u0456\u0441\u0442\u044f\u0442\u044c payment_mode / excluded ids,
    \u0442\u043e\u043c\u0443 \u043e\u0441\u043e\u0431\u0438\u0441\u0442\u0438\u0439 \u0432\u043d\u0435\u0441\u043e\u043a \u0443 \u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457 \u043c\u043e\u0436\u043d\u0430 \u0432\u0456\u0434\u043d\u043e\u0432\u0438\u0442\u0438 \u0437\u0430\u0434\u043d\u0456\u043c \u0447\u0438\u0441\u043b\u043e\u043c.
    """
    rows = self.conn.execute("""
    SELECT *
    FROM contracts
    WHERE status IN ('paid', 'annulled')
    ORDER BY id ASC
    """).fetchall()

    inserted = 0

    for row in rows:
      participant_ids = parse_ids(row["participant_ids"])
      if not participant_ids:
        continue

      contributions = calculate_personal_family_contributions(
        row["price"],
        participant_ids,
        row["payment_mode"] or PAYMENT_MODE_NORMAL,
        parse_ids(row["excluded_payment_ids"] or "[]"),
      )

      if not contributions:
        continue

      created_at = row["paid_at"] or row["created_at"] or utc_now_iso()

      for uid, amount_cents in contributions.items():
        cur = self.conn.execute("""
        INSERT OR IGNORE INTO family_contributions
        (contract_id, user_id, amount_cents, created_at)
        VALUES (?, ?, ?, ?)
        """, (
          row["id"],
          uid,
          amount_cents,
          created_at,
        ))
        inserted += cur.rowcount

    self.conn.commit()

    if inserted:
      print(f"[MIGRATION] Backfilled {inserted} personal family contribution(s)")

  def migrate_pending_admin_debts_to_accruals(self):
    """
    Move only still-unpaid legacy deputy balances into the unified payout queue.
    Already-paid historical payouts stay historical and are not recreated.
    """
    rows = self.conn.execute("""
    SELECT d.*, c.status AS contract_status
    FROM admin_debts d
    JOIN contracts c ON c.id = d.contract_id
    WHERE d.status = 'pending'
     AND c.status = 'paid'
    ORDER BY d.id ASC
    """).fetchall()

    migrated = 0
    now = utc_now_iso()

    try:
      self.conn.execute("BEGIN IMMEDIATE")

      for row in rows:
        self.conn.execute("""
        INSERT OR IGNORE INTO payment_accruals
        (contract_id, user_id, amount_cents, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """, (
          row["contract_id"],
          row["user_id"],
          row["amount_cents"],
          row["created_at"] or now,
        ))

        cur = self.conn.execute("""
        UPDATE admin_debts
        SET status = 'migrated'
        WHERE id = ?
         AND status = 'pending'
        """, (row["id"],))
        migrated += cur.rowcount

      self.conn.commit()
    except Exception:
      self.conn.rollback()
      raise

    if migrated:
      print(f"[MIGRATION] Moved {migrated} pending deputy balance(s) to /payouts")


  # Contract catalog
  def create_contract_type(self, name: str, price: int, cooldown: str, created_by: int) -> sqlite3.Row:
    existing = self.conn.execute(
      "SELECT * FROM contract_types WHERE name = ? COLLATE NOCASE",
      (name.strip(),),
    ).fetchone()

    now = utc_now_iso()

    if existing:
      self.conn.execute("""
      UPDATE contract_types
      SET price = ?, cooldown = ?, active = 1, updated_at = ?
      WHERE id = ?
      """, (price, cooldown, now, existing["id"]))
      self.conn.commit()
      return self.get_contract_type(existing["id"])

    cur = self.conn.execute("""
    INSERT INTO contract_types
    (name, price, cooldown, active, created_by, created_at)
    VALUES (?, ?, ?, 1, ?, ?)
    """, (name.strip(), price, cooldown.strip(), created_by, now))
    self.conn.commit()
    return self.get_contract_type(cur.lastrowid)

  def update_contract_type(self, type_id: int, name: str, price: int, cooldown: str) -> bool:
    try:
      cur = self.conn.execute("""
      UPDATE contract_types
      SET name = ?, price = ?, cooldown = ?, updated_at = ?
      WHERE id = ?
      """, (name.strip(), price, cooldown.strip(), utc_now_iso(), type_id))
      self.conn.commit()
      return cur.rowcount > 0
    except sqlite3.IntegrityError:
      return False

  def archive_contract_type(self, type_id: int) -> bool:
    cur = self.conn.execute(
      "UPDATE contract_types SET active = 0, updated_at = ? WHERE id = ?",
      (utc_now_iso(), type_id),
    )
    self.conn.commit()
    return cur.rowcount > 0

  def get_contract_type(self, type_id: int):
    return self.conn.execute(
      "SELECT * FROM contract_types WHERE id = ?",
      (type_id,),
    ).fetchone()

  def active_contract_types(self, limit: int = 25):
    return self.conn.execute("""
    SELECT ct.*,
        COUNT(c.id) AS usage_count
    FROM contract_types ct
    LEFT JOIN contracts c
     ON c.contract_type_id = ct.id
     AND c.status NOT IN ('cancelled', 'annulled')
    WHERE ct.active = 1
    GROUP BY ct.id
    ORDER BY usage_count DESC, ct.name COLLATE NOCASE ASC
    LIMIT ?
    """, (limit,)).fetchall()

  def list_active_contract_types(self, limit: int = 100):
    return self.conn.execute("""
    SELECT *
    FROM contract_types
    WHERE active = 1
    ORDER BY name COLLATE NOCASE ASC
    LIMIT ?
    """, (limit,)).fetchall()


  # Completed contracts
  def add_completed_contract(
    self,
    message_id: int,
    guild_id: int,
    channel_id: int,
    creator_id: int,
    participant_ids: list[int],
    contract_type: sqlite3.Row,
    note: Optional[str] = None,
  ) -> int:
    cur = self.conn.execute("""
    INSERT INTO contracts (
      message_id, guild_id, channel_id, creator_id, participant_ids,
      contract_type_id, contract_name, price, cooldown, note,
      status, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unpaid', ?)
    """, (
      message_id,
      guild_id,
      channel_id,
      creator_id,
      json.dumps(participant_ids),
      contract_type["id"],
      contract_type["name"],
      contract_type["price"],
      contract_type["cooldown"],
      (note.strip() if note and note.strip() else None),
      utc_now_iso(),
    ))
    self.conn.commit()
    return cur.lastrowid

  def get_completed_by_message(self, message_id: int):
    return self.conn.execute(
      "SELECT * FROM contracts WHERE message_id = ?",
      (message_id,),
    ).fetchone()

  def get_completed_by_id(self, contract_id: int):
    return self.conn.execute(
      "SELECT * FROM contracts WHERE id = ?",
      (contract_id,),
    ).fetchone()

  def cancel_completed(self, message_id: int, cancelled_by: int) -> bool:
    cur = self.conn.execute("""
    UPDATE contracts
    SET status = 'cancelled',
      cancelled_by = ?,
      cancelled_at = ?
    WHERE message_id = ?
     AND status = 'unpaid'
    """, (cancelled_by, utc_now_iso(), message_id))
    self.conn.commit()
    return cur.rowcount > 0

  def annul_paid(self, message_id: int, annulled_by: int) -> bool:
    """
    \u0410\u043d\u0443\u043b\u044e\u0454 \u0432\u0436\u0435 \u043e\u043f\u043b\u0430\u0447\u0435\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0431\u0435\u0437 \u0444\u0456\u0437\u0438\u0447\u043d\u043e\u0433\u043e \u0432\u0438\u0434\u0430\u043b\u0435\u043d\u043d\u044f.
    \u0421\u0442\u0430\u0440\u0456 payout-\u0438 \u0437\u0430\u043b\u0438\u0448\u0430\u044e\u0442\u044c\u0441\u044f \u0432 \u0411\u0414 \u044f\u043a \u0456\u0441\u0442\u043e\u0440\u0438\u0447\u043d\u0438\u0439 \u0441\u043b\u0456\u0434,
    \u0430\u043b\u0435 \u0447\u0435\u0440\u0435\u0437 status='annulled' \u0431\u0456\u043b\u044c\u0448\u0435 \u043d\u0435 \u043f\u043e\u0442\u0440\u0430\u043f\u043b\u044f\u044e\u0442\u044c \u0443 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0443.
    """
    cur = self.conn.execute("""
    UPDATE contracts
    SET status = 'annulled',
      annulled_by = ?,
      annulled_at = ?
    WHERE message_id = ?
     AND status = 'paid'
    """, (
      annulled_by,
      utc_now_iso(),
      message_id,
    ))
    self.conn.commit()
    return cur.rowcount > 0


  def pay_completed(
    self,
    message_id: int,
    paid_by: int,
    payment_mode: str = PAYMENT_MODE_NORMAL,
    excluded_payment_ids: Optional[list[int]] = None,
    deferred_payout_ids: Optional[list[int]] = None,
  ):
    """
    Finalizes the contract calculation.
    Participant money is accrued to payment_accruals and is NOT considered paid
    until leadership closes it through /payouts.
    """
    row = self.get_completed_by_message(message_id)
    if not row or row["status"] != "unpaid":
      return None

    participant_ids = parse_ids(row["participant_ids"])
    if not participant_ids:
      return None

    try:
      family_cents, net_cents, payouts, excluded_ids = calculate_payment(
        row["price"],
        participant_ids,
        payment_mode,
        excluded_payment_ids,
      )
    except ValueError:
      return None

    contributions = calculate_personal_family_contributions(
      row["price"],
      participant_ids,
      payment_mode,
      excluded_ids,
    )

    calculated_at = utc_now_iso()

    try:
      self.conn.execute("BEGIN IMMEDIATE")

      cur = self.conn.execute("""
      UPDATE contracts
      SET status = 'paid',
        payment_mode = ?,
        excluded_payment_ids = ?,
        paid_by = ?,
        paid_at = ?,
        fomo_cents = ?,
        net_cents = ?
      WHERE message_id = ?
       AND status = 'unpaid'
      """, (
        payment_mode,
        json.dumps(excluded_ids),
        paid_by,
        calculated_at,
        family_cents,
        net_cents,
        message_id,
      ))

      if cur.rowcount != 1:
        self.conn.rollback()
        return None

      contract_id = row["id"]

      self.conn.execute(
        "DELETE FROM payment_accruals WHERE contract_id = ?",
        (contract_id,),
      )
      self.conn.execute(
        "DELETE FROM family_contributions WHERE contract_id = ?",
        (contract_id,),
      )

      for uid, amount_cents in payouts.items():
        self.conn.execute("""
        INSERT INTO payment_accruals
        (contract_id, user_id, amount_cents, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """, (
          contract_id,
          uid,
          amount_cents,
          calculated_at,
        ))

      for uid, amount_cents in contributions.items():
        self.conn.execute("""
        INSERT INTO family_contributions
        (contract_id, user_id, amount_cents, created_at)
        VALUES (?, ?, ?, ?)
        """, (
          contract_id,
          uid,
          amount_cents,
          calculated_at,
        ))

      self.conn.commit()
    except Exception:
      self.conn.rollback()
      raise

    return {
      "payment_mode": payment_mode,
      "excluded_payment_ids": excluded_ids,
      "fomo_cents": family_cents,
      "net_cents": net_cents,
      "accruals": payouts,
      "family_contributions": contributions,
      "calculated_at": calculated_at,
    }


  def update_completed_participants(
    self,
    message_id: int,
    participant_ids: list[int],
  ) -> bool:
    participant_ids = list(dict.fromkeys(int(uid) for uid in participant_ids))
    if not participant_ids:
      return False

    cur = self.conn.execute("""
    UPDATE contracts
    SET participant_ids = ?
    WHERE message_id = ?
     AND status = 'unpaid'
    """, (
      json.dumps(participant_ids),
      message_id,
    ))
    self.conn.commit()
    return cur.rowcount > 0

  def update_completed_contract_type(
    self,
    message_id: int,
    contract_type: sqlite3.Row,
  ) -> bool:
    cur = self.conn.execute("""
    UPDATE contracts
    SET contract_type_id = ?,
      contract_name = ?,
      price = ?,
      cooldown = ?
    WHERE message_id = ?
     AND status = 'unpaid'
    """, (
      contract_type["id"],
      contract_type["name"],
      contract_type["price"],
      contract_type["cooldown"],
      message_id,
    ))
    self.conn.commit()
    return cur.rowcount > 0

  def all_for_guild(self, guild_id: int):
    return self.conn.execute("""
    SELECT * FROM contracts
    WHERE guild_id = ?
    ORDER BY id DESC
    """, (guild_id,)).fetchall()


  def payouts_for_contract(self, contract_id: int):
    return self.conn.execute("""
    SELECT * FROM contract_payouts
    WHERE contract_id = ?
    ORDER BY id ASC
    """, (contract_id,)).fetchall()

  def all_non_cancelled(self, guild_id: int):
    return self.conn.execute("""
    SELECT * FROM contracts
    WHERE guild_id = ?
     AND status NOT IN ('cancelled', 'annulled')
    ORDER BY id DESC
    """, (guild_id,)).fetchall()

  def unpaid_for_guild(self, guild_id: int, limit: int = 15):
    return self.conn.execute("""
    SELECT * FROM contracts
    WHERE guild_id = ?
     AND status = 'unpaid'
    ORDER BY id DESC
    LIMIT ?
    """, (guild_id, limit)).fetchall()

  def paid_payouts_for_guild(self, guild_id: int):
    """
    Actual money already paid to members.
    Priority: new accruals > legacy admin debts > legacy instant payouts.
    This avoids double-counting contracts that passed through older migrations.
    """
    return self.conn.execute("""
    SELECT
      cp.user_id AS user_id,
      cp.amount_cents AS amount_cents,
      cp.created_at AS created_at
    FROM contract_payouts cp
    JOIN contracts c ON c.id = cp.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND NOT EXISTS (
       SELECT 1
       FROM payment_accruals a
       WHERE a.contract_id = cp.contract_id
        AND a.user_id = cp.user_id
     )
     AND NOT EXISTS (
       SELECT 1
       FROM admin_debts d
       WHERE d.contract_id = cp.contract_id
        AND d.user_id = cp.user_id
     )

    UNION ALL

    SELECT
      d.user_id AS user_id,
      d.amount_cents AS amount_cents,
      d.settled_at AS created_at
    FROM admin_debts d
    JOIN contracts c ON c.id = d.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND d.status = 'paid'
     AND d.settled_at IS NOT NULL
     AND NOT EXISTS (
       SELECT 1
       FROM payment_accruals a
       WHERE a.contract_id = d.contract_id
        AND a.user_id = d.user_id
     )

    UNION ALL

    SELECT
      a.user_id AS user_id,
      a.amount_cents AS amount_cents,
      a.paid_at AS created_at
    FROM payment_accruals a
    JOIN contracts c ON c.id = a.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND a.status = 'paid'
     AND a.paid_at IS NOT NULL
    """, (guild_id, guild_id, guild_id)).fetchall()

  def accruals_for_contract(self, contract_id: int):
    return self.conn.execute("""
    SELECT *
    FROM payment_accruals
    WHERE contract_id = ?
    ORDER BY id ASC
    """, (contract_id,)).fetchall()

  def accruals_for_guild(self, guild_id: int):
    return self.conn.execute("""
    SELECT
      a.*,
      c.message_id,
      c.channel_id,
      c.contract_name,
      c.price
    FROM payment_accruals a
    JOIN contracts c ON c.id = a.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
    ORDER BY a.id ASC
    """, (guild_id,)).fetchall()

  def pending_accrual_summary(self, guild_id: int):
    return self.conn.execute("""
    SELECT
      a.user_id,
      COUNT(*) AS accrual_count,
      SUM(a.amount_cents) AS total_cents
    FROM payment_accruals a
    JOIN contracts c ON c.id = a.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND a.status = 'pending'
    GROUP BY a.user_id
    ORDER BY total_cents DESC, a.user_id ASC
    """, (guild_id,)).fetchall()

  def pending_accruals_for_user(self, guild_id: int, user_id: int):
    return self.conn.execute("""
    SELECT
      a.*,
      c.message_id,
      c.channel_id,
      c.contract_name,
      c.price
    FROM payment_accruals a
    JOIN contracts c ON c.id = a.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND a.user_id = ?
     AND a.status = 'pending'
    ORDER BY a.id ASC
    """, (guild_id, user_id)).fetchall()

  def pending_accrual_total(self, guild_id: int, user_id: Optional[int] = None) -> int:
    sql = """
    SELECT COALESCE(SUM(a.amount_cents), 0) AS total
    FROM payment_accruals a
    JOIN contracts c ON c.id = a.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND a.status = 'pending'
    """
    params: list = [guild_id]

    if user_id is not None:
      sql += " AND a.user_id = ?"
      params.append(user_id)

    row = self.conn.execute(sql, params).fetchone()
    return int(row["total"] or 0)

  def settle_accruals_for_user(
    self,
    guild_id: int,
    user_id: int,
    paid_by: int,
  ):
    rows = self.pending_accruals_for_user(guild_id, user_id)
    if not rows:
      return []

    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    now = utc_now_iso()

    self.conn.execute(
      f"""
      UPDATE payment_accruals
      SET status = 'paid',
        paid_at = ?,
        paid_by = ?
      WHERE id IN ({placeholders})
       AND status = 'pending'
      """,
      (now, paid_by, *ids),
    )
    self.conn.commit()
    return rows

  def settle_accruals_for_users(
    self,
    guild_id: int,
    user_ids: list[int],
    paid_by: int,
  ):
    normalized = sorted({int(uid) for uid in user_ids})
    if not normalized:
      return []

    user_placeholders = ",".join("?" for _ in normalized)

    rows = self.conn.execute(
      f"""
      SELECT
        a.*,
        c.message_id,
        c.channel_id,
        c.contract_name
      FROM payment_accruals a
      JOIN contracts c ON c.id = a.contract_id
      WHERE c.guild_id = ?
       AND c.status = 'paid'
       AND a.status = 'pending'
       AND a.user_id IN ({user_placeholders})
      ORDER BY a.id ASC
      """,
      (guild_id, *normalized),
    ).fetchall()

    if not rows:
      return []

    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    now = utc_now_iso()

    self.conn.execute(
      f"""
      UPDATE payment_accruals
      SET status = 'paid',
        paid_at = ?,
        paid_by = ?
      WHERE id IN ({placeholders})
       AND status = 'pending'
      """,
      (now, paid_by, *ids),
    )
    self.conn.commit()
    return rows



  def admin_debts_for_contract(self, contract_id: int):
    return self.conn.execute("""
    SELECT *
    FROM admin_debts
    WHERE contract_id = ?
    ORDER BY id ASC
    """, (contract_id,)).fetchall()





  def family_contribution_for_user(
    self,
    guild_id: int,
    user_id: int,
    since: Optional[str] = None,
  ) -> int:
    sql = """
    SELECT COALESCE(SUM(fc.amount_cents), 0) AS total
    FROM family_contributions fc
    JOIN contracts c ON c.id = fc.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND fc.user_id = ?
    """
    params: list = [guild_id, user_id]

    if since:
      sql += " AND fc.created_at >= ?"
      params.append(since)

    row = self.conn.execute(sql, params).fetchone()
    return int(row["total"] or 0)

  def participant_ids_for_guild(self, guild_id: int) -> list[int]:
    rows = self.all_non_cancelled(guild_id)
    user_ids = {
      uid
      for row in rows
      for uid in parse_ids(row["participant_ids"])
    }
    return sorted(user_ids)


  def family_contributions_for_guild(self, guild_id: int):
    return self.conn.execute("""
    SELECT fc.*
    FROM family_contributions fc
    JOIN contracts c ON c.id = fc.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
    ORDER BY fc.id ASC
    """, (guild_id,)).fetchall()

  def upsert_birthday(
    self,
    guild_id: int,
    user_id: int,
    day: int,
    month: int,
    year: Optional[int],
  ):
    now = utc_now_iso()
    self.conn.execute("""
    INSERT INTO birthdays
    (guild_id, user_id, day, month, year, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(guild_id, user_id)
    DO UPDATE SET
      day = excluded.day,
      month = excluded.month,
      year = excluded.year,
      updated_at = excluded.updated_at
    """, (
      guild_id,
      user_id,
      day,
      month,
      year,
      now,
      now,
    ))
    self.conn.commit()

  def birthdays_for_guild(self, guild_id: int):
    return self.conn.execute("""
    SELECT *
    FROM birthdays
    WHERE guild_id = ?
    ORDER BY month ASC, day ASC, user_id ASC
    """, (guild_id,)).fetchall()


  def get_setting(self, guild_id: int, key: str) -> Optional[str]:
    row = self.conn.execute("""
    SELECT value FROM bot_settings
    WHERE guild_id = ? AND key = ?
    """, (guild_id, key)).fetchone()
    return row["value"] if row else None

  def set_setting(self, guild_id: int, key: str, value: str):
    self.conn.execute("""
    INSERT INTO bot_settings (guild_id, key, value)
    VALUES (?, ?, ?)
    ON CONFLICT(guild_id, key)
    DO UPDATE SET value = excluded.value
    """, (guild_id, key, value))
    self.conn.commit()


db = Database(DB_PATH)
SCHEMA_VERSION = db.run_startup_migrations()
print(f"[DB] Schema version: {SCHEMA_VERSION}")


# ----------------------------
# Completed contract messages
# ----------------------------

def build_completed_embed(row: sqlite3.Row) -> discord.Embed:
  participants = parse_ids(row["participant_ids"])
  payment_mode = row["payment_mode"] or PAYMENT_MODE_NORMAL
  accruals = db.accruals_for_contract(row["id"]) if row["status"] == "paid" else []

  if row["status"] == "paid":
    if payment_mode == PAYMENT_MODE_LEGACY_FAMILY:
      color = discord.Color.blurple()
      status_text = "\U0001f3e0 **\u041d\u0430 \u0444\u0430\u043c\u0443**"
    elif accruals:
      if any(a["status"] == "pending" for a in accruals):
        color = discord.Color.gold()
        status_text = "\U0001f7e1 **\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u2022 \u043e\u0447\u0456\u043a\u0443\u0454 \u0432\u0438\u043f\u043b\u0430\u0442\u0438**"
      else:
        color = discord.Color.green()
        status_text = "\U0001f7e2 **\u0412\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e**"
    else:
      color = discord.Color.green()
      status_text = "\U0001f7e2 **\u041e\u043f\u043b\u0430\u0447\u0435\u043d\u043e \u2022 \u0441\u0442\u0430\u0440\u0430 \u0441\u0438\u0441\u0442\u0435\u043c\u0430**"
  elif row["status"] == "annulled":
    color = discord.Color.dark_red()
    status_text = "\U0001f6ab **\u0410\u043d\u0443\u043b\u044c\u043e\u0432\u0430\u043d\u043e**"
  elif row["status"] == "cancelled":
    color = discord.Color.dark_grey()
    status_text = "\u26ab **\u0421\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u043e**"
  else:
    color = discord.Color.orange()
    status_text = "\U0001f534 **\u041d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e**"

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
        name="\U0001f9ee \u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e",
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
        name="\u2699\ufe0f \u0421\u043f\u043e\u0441\u0456\u0431 \u0440\u043e\u0437\u0440\u0430\u0445\u0443\u043d\u043a\u0443",
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
        name="\u0411\u0443\u043b\u043e \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e",
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
      channel = await bot.fetch_channel(channel_id)
    except discord.DiscordException:
      return None
  return channel


async def refresh_completed_message(message_id: int):
  row = db.get_completed_by_message(message_id)
  if not row:
    return

  try:
    channel = bot.get_channel(row["channel_id"]) or await bot.fetch_channel(row["channel_id"])
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
      return
    message = await channel.fetch_message(message_id)

    if row["status"] == "unpaid":
      view = UnpaidCompletedView(bot)
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
    channel = guild.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
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


# ----------------------------
# New contract flow
# ----------------------------

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


# ----------------------------
# Payment / cancel / correction
# ----------------------------

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


async def payment_preview_with_debts(
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
    [],
  )

  return embed, PaymentConfirmView(
    bot_instance,
    row["message_id"],
    payment_mode,
    excluded_ids,
    back_to_custom=back_to_custom,
    deferred_ids=[],
  )


class PaymentConfirmView(discord.ui.View):
  def __init__(
    self,
    bot_instance: "ContractBot",
    message_id: int,
    payment_mode: str,
    excluded_ids: Optional[list[int]] = None,
    back_to_custom: bool = False,
    deferred_ids: Optional[list[int]] = None,
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
        content="\u274c \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0443\u0436\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0439 \u0430\u0431\u043e \u0441\u043a\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0439.",
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
      "\U0001f9ee \u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e",
      (
        f"\u0417\u0430\u043f\u0438\u0441: **#{row_before['id']}**\n"
        f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442: **{row_before['contract_name']}**\n"
        f"\u0421\u043f\u043e\u0441\u0456\u0431: **{payment_mode_label(self.payment_mode)}**\n"
        f"\u0411\u0430\u043d\u043a \u0441\u0456\u043c'\u0457: **{format_cents(result['fomo_cents'])}**\n"
        f"\u041d\u0430\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0430\u043c: **{format_cents(result['net_cents'])}**\n"
        f"\u0411\u0435\u0437 \u043d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f: {excluded_text}\n"
        f"\u0411\u0430\u043b\u0430\u043d\u0441 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438:\n{accruals_text}\n"
        f"\u0420\u043e\u0437\u0440\u0430\u0445\u0443\u0432\u0430\u0432/\u043b\u0430: <@{interaction.user.id}>"
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
      embed, confirm_view = await payment_preview_with_debts(
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

    embed, confirm_view = await payment_preview_with_debts(
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

    embed, confirm_view = await payment_preview_with_debts(
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

    embed, confirm_view = await payment_preview_with_debts(
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


# ----------------------------
# Contract catalog admin
# ----------------------------

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
      f"\u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(paid_period_rows)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="\u23f3 \u0417\u0430\u0440\u0430\u0437",
    value=(
      f"\u041d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(unpaid_now_rows)}**"
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
      f"\u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0432 \u043f\u0435\u0440\u0456\u043e\u0434\u0456: **{len(calculated_rows)}**\n"
      f"\u041d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0437\u0430\u0440\u0430\u0437: **{len(uncalculated_rows)}**\n"
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
      f"\u0420\u043e\u0437\u0440\u0430\u0445\u0443\u043d\u043a\u0456\u0432 \u0437 \u0432\u0438\u043d\u044f\u0442\u043a\u0430\u043c\u0438: **{exception_count}**\n"
      f"\u041d\u0430\u043b\u0430\u0448\u0442\u043e\u0432\u0430\u043d\u0438\u0445 \u0440\u043e\u0437\u0440\u0430\u0445\u0443\u043d\u043a\u0456\u0432: **{custom_count}**"
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
      f"\u041d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u043d\u0430: **{format_cents(uncalculated)}**\n"
      f"\u0421\u0435\u0440\u0435\u0434\u043d\u0456\u0439 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442: **{format_cents(avg_contract)}**"
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
      f"\u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{len(paid_rows)}**\n"
      f"\u041d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u0437\u0430\u0440\u0430\u0437: **{len(unpaid_rows)}**\n"
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
      f"\u041d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432 \u043d\u0430: **{format_cents(unpaid)}**"
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
    embed.description = "\u0429\u0435 \u043d\u0435\u043c\u0430\u0454 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432."
  else:
    for stat in slice_rows:
      embed.add_field(
        name=stat["name"],
        value=(
          f"\u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{stat['count']}**\n"
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
      f"\u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(paid_period)}**\n"
      f"\u041d\u0435 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(unpaid_now)}**"
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
      f"\u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{len(calculated_rows)}**\n"
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
      view=MainContractPanelView(bot),
    )
  except discord.DiscordException:
    return None

  db.set_setting(guild.id, "panel_message_id", str(panel.id))
  return panel


# ----------------------------
# User panel
# ----------------------------

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



# ----------------------------
# Birthday module
# ----------------------------

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


# ----------------------------
# Automatic channel reports
# ----------------------------

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
      f"\u0420\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u043e: **{len(calculated_today)}**\n"
      f"\u0417\u0430\u0433\u0430\u043b\u044c\u043d\u0430 \u0441\u0443\u043c\u0430 \u0440\u043e\u0437\u0440\u0430\u0445\u043e\u0432\u0430\u043d\u0438\u0445: **{format_cents(gross_cents)}**"
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


async def scheduled_posts_loop(bot_instance: commands.Bot):
  await bot_instance.wait_until_ready()

  while not bot_instance.is_closed():
    try:
      now = datetime.now(LOCAL_TZ)
      today_key = now.date().isoformat()

      # Morning rating: once during the whole scheduled hour.
      if (
        RATING_CHANNEL_ID
        and now.hour == RATING_MORNING_HOUR
        and db.get_setting(GUILD_ID, "auto_rating_morning_date") != today_key
      ):
        await send_auto_rating(
          bot_instance,
          f"{now.strftime('%d.%m.%Y')} \u2022 \u0440\u0430\u043d\u043e\u043a",
        )
        db.set_setting(
          GUILD_ID,
          "auto_rating_morning_date",
          today_key,
        )
        print(f"[AUTO] Morning rating sent for {today_key}")

      # Evening rating.
      if (
        RATING_CHANNEL_ID
        and now.hour == RATING_EVENING_HOUR
        and db.get_setting(GUILD_ID, "auto_rating_evening_date") != today_key
      ):
        await send_auto_rating(
          bot_instance,
          f"{now.strftime('%d.%m.%Y')} \u2022 \u0432\u0435\u0447\u0456\u0440",
        )
        db.set_setting(
          GUILD_ID,
          "auto_rating_evening_date",
          today_key,
        )
        print(f"[AUTO] Evening rating sent for {today_key}")

      # Birthday reminders: 7 days before, 1 day before, and on the day.
      if (
        BIRTHDAY_ALERT_CHANNEL_ID
        and now.hour == BIRTHDAY_REMINDER_HOUR
        and db.get_setting(GUILD_ID, "birthday_reminders_date") != today_key
      ):
        await send_birthday_reminders(bot_instance)
        db.set_setting(
          GUILD_ID,
          "birthday_reminders_date",
          today_key,
        )
        print(f"[BIRTHDAY] Reminder check completed for {today_key}")

      # At midnight post the previous completed calendar day.
      if FAMILY_STATS_CHANNEL_ID and now.hour == FAMILY_STATS_HOUR:
        report_day = now.date() - timedelta(days=1)
        report_key = report_day.isoformat()

        if db.get_setting(GUILD_ID, "auto_family_stats_date") != report_key:
          await send_auto_family_stats(
            bot_instance,
            report_day,
          )
          db.set_setting(
            GUILD_ID,
            "auto_family_stats_date",
            report_key,
          )
          print(f"[AUTO] Family daily stats sent for {report_key}")

    except Exception as exc:
      print(f"[AUTO] Scheduled post error: {exc}")

    await asyncio.sleep(30)


# ----------------------------
# Bot + commands
# ----------------------------

class ContractBot(commands.Bot):
  def __init__(self):
    intents = discord.Intents.default()
    super().__init__(command_prefix="!", intents=intents)
    self._unpaid_refreshed = False
    self._scheduled_posts_task = None

  async def setup_hook(self):
    self.add_view(MainContractPanelView(self))
    self.add_view(UnpaidCompletedView(self))
    self.add_view(BirthdayPanelView())

    if self._scheduled_posts_task is None:
      self._scheduled_posts_task = asyncio.create_task(
        scheduled_posts_loop(self)
      )

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
    print(
      "[AUTO] "
      f"rating_channel={RATING_CHANNEL_ID or 'disabled'} "
      f"family_stats_channel={FAMILY_STATS_CHANNEL_ID or 'disabled'} "
      f"rating_hours={RATING_MORNING_HOUR}/{RATING_EVENING_HOUR} "
      f"family_stats_hour={FAMILY_STATS_HOUR} "
      f"birthday_input={BIRTHDAY_INPUT_CHANNEL_ID or 'disabled'} "
      f"birthday_alert={BIRTHDAY_ALERT_CHANNEL_ID or 'disabled'} "
      f"birthday_hour={BIRTHDAY_REMINDER_HOUR} "
      f"test_user={TEST_USER_ID or 'disabled'} "
      f"timezone={TIMEZONE_NAME}"
    )

    try:
      await ensure_birthday_panel(self)
    except Exception as exc:
      print(f"[BIRTHDAY] Could not ensure panel: {exc}")

    if not self._unpaid_refreshed and GUILD_ID:
      self._unpaid_refreshed = True
      try:
        unpaid_rows = db.unpaid_for_guild(GUILD_ID, limit=100)

        for row in unpaid_rows:
          await refresh_completed_message(row["message_id"])

        if unpaid_rows:
          print(f"[UI] Refreshed {len(unpaid_rows)} unpaid contract message(s)")
      except Exception as exc:
        print(f"[UI] Could not refresh unpaid messages: {exc}")


bot = ContractBot()


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





# ----------------------------
# Unified participant payouts
# ----------------------------



async def payout_user_label(
  guild: discord.Guild,
  user_id: int,
) -> str:
  member = guild.get_member(user_id)
  return member.display_name if member else f"ID {user_id}"




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
          f"{row['accrual_count']} \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442(\u0456\u0432)"
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

    rows = db.pending_accruals_for_user(
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

    settled = db.settle_accruals_for_user(
      self.guild_id,
      self.user_id,
      interaction.user.id,
    )

    message_ids = sorted({
      row["message_id"]
      for row in settled
      if row["message_id"]
    })

    for message_id in message_ids:
      await refresh_completed_message(message_id)

    await audit_log(
      interaction.guild,
      "\U0001f4b5 \u0412\u0438\u043f\u043b\u0430\u0442\u0443 \u0443\u0447\u0430\u0441\u043d\u0438\u043a\u0443 \u0437\u0430\u043a\u0440\u0438\u0442\u043e",
      (
        f"\u0423\u0447\u0430\u0441\u043d\u0438\u043a: <@{self.user_id}>\n"
        f"\u0421\u0443\u043c\u0430: **{format_cents(total)}**\n"
        f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(settled)}**\n"
        f"\u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    await interaction.edit_original_response(
      content=(
        f"\u2705 <@{self.user_id}> \u0432\u0438\u043f\u043b\u0430\u0447\u0435\u043d\u043e **{format_cents(total)}**.\n"
        "\u0411\u0430\u043b\u0430\u043d\u0441 \u0434\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438 \u0437\u0430\u043a\u0440\u0438\u0442\u043e."
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

    rows = db.settle_accruals_for_users(
      self.guild_id,
      user_ids,
      interaction.user.id,
    )

    total = sum(row["amount_cents"] for row in rows)
    users = {row["user_id"] for row in rows}

    message_ids = sorted({
      row["message_id"]
      for row in rows
      if row["message_id"]
    })

    for message_id in message_ids:
      await refresh_completed_message(message_id)

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
        f"\u0412\u0438\u043f\u043b\u0430\u0442\u0438\u0432/\u043b\u0430: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    await interaction.edit_original_response(
      content=(
        f"\u2705 \u0412\u0438\u043f\u043b\u0430\u0442\u0438 **{category_label}** \u0437\u0430\u043a\u0440\u0438\u0442\u043e.\n"
        f"\u041b\u044e\u0434\u0435\u0439: **{len(users)}**\n"
        f"\u0421\u0443\u043c\u0430: **{format_cents(total)}**"
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
  summary = db.pending_accrual_summary(guild.id)
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

  rows = db.pending_accruals_for_user(
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
    "\u0432\u0438\u043f\u043b\u0430\u0442\u0443 \u0437\u0430\u043a\u0440\u0438\u0432\u0430\u0454 \u0442\u0456\u043b\u044c\u043a\u0438 \u043b\u0456\u0434\u0435\u0440"
    if is_management
    else "\U0001f465 **\u041e\u0421\u041d\u041e\u0412\u041d\u0418\u0419 \u0421\u041a\u041b\u0410\u0414**"
  )

  embed = discord.Embed(
    title="\U0001f4b0 \u0414\u043e \u0432\u0438\u043f\u043b\u0430\u0442\u0438",
    description=(
      f"{category_text}\n"
      f"**{label}** \u2022 <@{user_id}>\n"
      f"\u0417\u0430\u0433\u0430\u043b\u044c\u043d\u0430 \u0441\u0443\u043c\u0430: **{format_cents(total)}**\n"
      f"\u041a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0456\u0432: **{len(rows)}**"
    ),
    color=discord.Color.gold(),
  )

  if rows:
    lines = [
      (
        f"#{row['contract_id']} \u2022 {row['contract_name']} \u2014 "
        f"**{format_cents(row['amount_cents'])}**"
      )
      for row in rows[:15]
    ]

    if len(rows) > 15:
      lines.append(f"\u2026\u0456 \u0449\u0435 {len(rows) - 15}")

    embed.add_field(
      name="\u041d\u0430\u0440\u0430\u0445\u0443\u0432\u0430\u043d\u043d\u044f",
      value="\n".join(lines),
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
      f"\u2022 {row['accrual_count']} \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442(\u0456\u0432)"
    )
    for row in participant_rows[:20]
  ] or ["\u2014"]

  management_lines = [
    (
      f"<@{row['user_id']}> \u2014 **{format_cents(row['total_cents'])}** "
      f"\u2022 {row['accrual_count']} \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442(\u0456\u0432)"
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


bot.run(TOKEN)
