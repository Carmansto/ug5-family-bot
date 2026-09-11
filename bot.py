import os
import re
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
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
  110к / 110k
  1.2м / 1.2m
  """
  s = raw.strip().lower()
  s = s.replace("$", "").replace("₴", "").replace(" ", "").replace("_", "")
  s = s.replace(",", ".")

  multiplier = 1
  if s.endswith(("к", "k")):
    multiplier = 1_000
    s = s[:-1]
  elif s.endswith(("м", "m")):
    multiplier = 1_000_000
    s = s[:-1]

  if not re.fullmatch(r"\d+(\.\d+)?", s):
    raise ValueError("Некоректна сума")

  value = int(Decimal(s) * multiplier)
  if value <= 0:
    raise ValueError("Сума має бути більшою за 0")
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


UA_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
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
  """1 бал, 2 бали, 10 балів, 1.65 бала."""
  number = format_points(value)

  if value.denominator != 1:
    return f"{number} бала"

  n = abs(value.numerator)
  last_two = n % 100
  last = n % 10

  if last_two in (11, 12, 13, 14):
    word = "балів"
  elif last == 1:
    word = "бал"
  elif last in (2, 3, 4):
    word = "бали"
  else:
    word = "балів"

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
    "Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"
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
    PAYMENT_MODE_NORMAL: "Звичайна оплата",
    PAYMENT_MODE_REDISTRIBUTE: "Розділити між рештою",
    PAYMENT_MODE_FAMILY_SHARE: "Частку винятків у сім'ю",
    PAYMENT_MODE_LEGACY_FAMILY: "На фаму",
  }
  return labels.get(mode, "Оплата")


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
    title="💵 Перевірка оплати",
    description=(
      f"**{row['contract_name']}**\n"
      f"Сума контракту: **{format_money_dollars(row['price'])} $**"
    ),
    color=discord.Color.gold(),
  )

  embed.add_field(
    name="Спосіб",
    value=payment_mode_label(payment_mode),
    inline=False,
  )
  embed.add_field(
    name="🏦 Банк сім'ї",
    value=format_cents(family_cents),
    inline=True,
  )
  embed.add_field(
    name="💸 Учасникам",
    value=format_cents(net_cents),
    inline=True,
  )

  if normalized_excluded and payment_mode != PAYMENT_MODE_LEGACY_FAMILY:
    embed.add_field(
      name="🚫 Без виплати",
      value=" ".join(f"<@{uid}>" for uid in normalized_excluded),
      inline=False,
    )

  if payouts:
    lines = [
      f"<@{uid}> — **{format_cents(amount)}**"
      for uid, amount in payouts.items()
    ]
    embed.add_field(
      name="👤 Розподіл",
      value="\n".join(lines),
      inline=False,
    )

  embed.set_footer(text="Перевірте суми перед підтвердженням")
  return embed



def management_member(member: discord.Member) -> bool:
  if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
    return True
  return any(role.id in MANAGER_ROLE_IDS for role in member.roles)


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
    CREATE TABLE IF NOT EXISTS bot_settings (
      guild_id INTEGER NOT NULL,
      key TEXT NOT NULL,
      value TEXT NOT NULL,
      PRIMARY KEY (guild_id, key)
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
    }

    for name, sql in migrations.items():
      if name not in columns:
        self.conn.execute(sql)

    self.conn.commit()

  def backfill_legacy_paid_contracts(self):
    """
    Старі MVP/V2 записи могли бути позначені як paid ще до появи
    Банку сім'ї та персональних payout-ів. Дораховуємо їх один раз.
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

      payout_count = self.conn.execute(
        "SELECT COUNT(*) AS cnt FROM contract_payouts WHERE contract_id = ?",
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

      needs_payouts = bool(payouts) and payout_count == 0

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

  def search_contract_types(self, query: str, limit: int = 25):
    q = f"%{query.strip()}%"
    return self.conn.execute("""
    SELECT ct.*,
        COUNT(c.id) AS usage_count
    FROM contract_types ct
    LEFT JOIN contracts c
     ON c.contract_type_id = ct.id
     AND c.status NOT IN ('cancelled', 'annulled')
    WHERE ct.active = 1
     AND ct.name LIKE ? COLLATE NOCASE
    GROUP BY ct.id
    ORDER BY usage_count DESC, ct.name COLLATE NOCASE ASC
    LIMIT ?
    """, (q, limit)).fetchall()

  # Completed contracts
  def add_completed_contract(
    self,
    message_id: int,
    guild_id: int,
    channel_id: int,
    creator_id: int,
    participant_ids: list[int],
    contract_type: sqlite3.Row,
  ) -> int:
    cur = self.conn.execute("""
    INSERT INTO contracts (
      message_id, guild_id, channel_id, creator_id, participant_ids,
      contract_type_id, contract_name, price, cooldown,
      status, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unpaid', ?)
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
    Анулює вже оплачений контракт без фізичного видалення.
    Старі payout-и залишаються в БД як історичний слід,
    але через status='annulled' більше не потрапляють у статистику.
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
  ):
    row = self.get_completed_by_message(message_id)
    if not row or row["status"] != "unpaid":
      return None

    participant_ids = parse_ids(row["participant_ids"])
    if not participant_ids:
      return None

    try:
      fomo_cents, net_cents, payouts, excluded_ids = calculate_payment(
        row["price"],
        participant_ids,
        payment_mode,
        excluded_payment_ids,
      )
    except ValueError:
      return None

    paid_at = utc_now_iso()

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
        paid_at,
        fomo_cents,
        net_cents,
        message_id,
      ))

      if cur.rowcount != 1:
        self.conn.rollback()
        return None

      contract_id = row["id"]

      self.conn.execute(
        "DELETE FROM contract_payouts WHERE contract_id = ?",
        (contract_id,),
      )

      for uid, amount_cents in payouts.items():
        self.conn.execute("""
        INSERT INTO contract_payouts
        (contract_id, user_id, amount_cents, created_at)
        VALUES (?, ?, ?, ?)
        """, (contract_id, uid, amount_cents, paid_at))

      self.conn.commit()
    except Exception:
      self.conn.rollback()
      raise

    return {
      "payment_mode": payment_mode,
      "excluded_payment_ids": excluded_ids,
      "fomo_cents": fomo_cents,
      "net_cents": net_cents,
      "payouts": payouts,
      "paid_at": paid_at,
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
    return self.conn.execute("""
    SELECT cp.*
    FROM contract_payouts cp
    JOIN contracts c ON c.id = cp.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
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
db.backfill_legacy_paid_contracts()


# ----------------------------
# Completed contract messages
# ----------------------------

def build_completed_embed(row: sqlite3.Row) -> discord.Embed:
  participants = parse_ids(row["participant_ids"])

  if row["status"] == "paid":
    color = discord.Color.green()
    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) == PAYMENT_MODE_LEGACY_FAMILY:
      status_text = "🏠 **На фаму**"
    else:
      status_text = "🟢 **Оплачено**"
  elif row["status"] == "annulled":
    color = discord.Color.dark_red()
    status_text = "🚫 **Анульовано**"
  elif row["status"] == "cancelled":
    color = discord.Color.dark_grey()
    status_text = "⚫ **Скасовано**"
  else:
    color = discord.Color.orange()
    status_text = "🔴 **Не оплачено**"

  if row["status"] == "annulled":
    title = "🚫 КОНТРАКТ АНУЛЬОВАНО"
  elif row["status"] == "cancelled":
    title = "❌ КОНТРАКТ СКАСОВАНО"
  else:
    title = "✅ КОНТРАКТ ВИКОНАНО"

  embed = discord.Embed(
    title=title,
    color=color,
  )

  embed.add_field(
    name="👥 Виконували",
    value=" ".join(f"<@{uid}>" for uid in participants) or "—",
    inline=False,
  )
  embed.add_field(name="📋 Контракт", value=row["contract_name"], inline=True)
  embed.add_field(name="💰 Сума", value=f"{format_money_dollars(row['price'])} $", inline=True)
  embed.add_field(name="⏳ КД", value=row["cooldown"], inline=True)
  embed.add_field(name="💳 Статус", value=status_text, inline=False)

  if row["status"] == "paid":
    paid_ts = iso_to_unix(row["paid_at"])
    if paid_ts:
      embed.add_field(name="✅ Оплачено", value=f"<t:{paid_ts}:f>", inline=True)

    fomo_cents = row["fomo_cents"] or 0
    net_cents = row["net_cents"] or 0
    payment_mode = row["payment_mode"] or PAYMENT_MODE_NORMAL

    embed.add_field(
      name="🏦 Банк сім'ї",
      value=format_cents(fomo_cents),
      inline=True,
    )
    embed.add_field(
      name="💸 Учасникам",
      value=format_cents(net_cents),
      inline=True,
    )

    excluded_payment_ids = parse_ids(row["excluded_payment_ids"] or "[]")
    if excluded_payment_ids and payment_mode != PAYMENT_MODE_LEGACY_FAMILY:
      embed.add_field(
        name="🚫 Без виплати",
        value=" ".join(f"<@{uid}>" for uid in excluded_payment_ids),
        inline=False,
      )

    if payment_mode in (
      PAYMENT_MODE_REDISTRIBUTE,
      PAYMENT_MODE_FAMILY_SHARE,
      PAYMENT_MODE_LEGACY_FAMILY,
    ):
      embed.add_field(
        name="⚙️ Спосіб оплати",
        value=payment_mode_label(payment_mode),
        inline=False,
      )

    payouts = db.payouts_for_contract(row["id"])
    if payouts:
      payout_lines = [
        f"<@{p['user_id']}> — **{format_cents(p['amount_cents'])}**"
        for p in payouts
      ]
      embed.add_field(
        name="👤 Розподіл виплати",
        value="\n".join(payout_lines),
        inline=False,
      )

  if row["status"] == "annulled":
    paid_ts = iso_to_unix(row["paid_at"])
    annulled_ts = iso_to_unix(row["annulled_at"])
    annulled_by = row["annulled_by"]

    details = []
    if annulled_by:
      details.append(f"Анулював/ла: <@{annulled_by}>")
    if annulled_ts:
      details.append(f"<t:{annulled_ts}:f>")

    if paid_ts:
      embed.add_field(
        name="Було оплачено",
        value=f"<t:{paid_ts}:f>",
        inline=True,
      )

    embed.add_field(
      name="Було в Банк сім'ї",
      value=format_cents(row["fomo_cents"] or 0),
      inline=True,
    )
    embed.add_field(
      name="Було учасникам",
      value=format_cents(row["net_cents"] or 0),
      inline=True,
    )

    if details:
      embed.add_field(
        name="Анулювання",
        value=" • ".join(details),
        inline=False,
      )

  if row["status"] == "cancelled":
    cancelled_ts = iso_to_unix(row["cancelled_at"])
    cancelled_by = row["cancelled_by"]
    details = []
    if cancelled_by:
      details.append(f"Скасував: <@{cancelled_by}>")
    if cancelled_ts:
      details.append(f"<t:{cancelled_ts}:f>")
    if details:
      embed.add_field(name="Скасування", value=" • ".join(details), inline=False)

  embed.set_footer(text=f"Запис #{row['id']}")
  embed.set_footer(text=f"ID контракту: {row['id']}")
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
  return " ".join(f"<@{uid}>" for uid in user_ids) or "—"


# ----------------------------
# New contract flow
# ----------------------------

class PerformerSelect(discord.ui.UserSelect):
  def __init__(self):
    super().__init__(
      placeholder="Оберіть виконавців контракту",
      min_values=1,
      max_values=25,
    )

  async def callback(self, interaction: discord.Interaction):
    view: PerformerStepView = self.view # type: ignore
    ids = [u.id for u in self.values if not getattr(u, "bot", False)]

    if not ids:
      await interaction.response.send_message(
        "❌ Оберіть хоча б одного звичайного учасника.",
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
    label="Я виконав/ла сам/а",
    style=discord.ButtonStyle.primary,
    emoji="👤",
  )
  async def myself(self, interaction: discord.Interaction, button: discord.ui.Button):
    if getattr(interaction.user, "bot", False):
      await interaction.response.send_message("❌ Бот не може бути виконавцем.", ephemeral=True)
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
    title = f"🔎 Пошук: **{query}**"
  else:
    title = "📋 Контракти"

  return (
    f"👥 Виконавці: {mentions}\n\n"
    f"{title} • сторінка **{page + 1}/{total_pages}**\n"
    "Оберіть контракт зі списку."
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
          f"{format_money_dollars(row['price'])} $ • КД {row['cooldown']}"
        )[:100],
      )
      for row in rows
    ]

    if not options:
      options = [
        discord.SelectOption(
          label="Нічого не знайдено",
          value="none",
          description="Змініть пошук або покажіть усі контракти",
        )
      ]

    placeholder = (
      f"Результати: {query} • {page + 1}/{total_pages}"
      if query
      else f"Контракти • {page + 1}/{total_pages}"
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
        "❌ Цей контракт уже недоступний.",
        ephemeral=True,
      )
      return

    await interaction.response.edit_message(
      content="Перевірте дані й підтвердьте.",
      embed=build_confirmation_embed(row, self.participant_ids),
      view=ConfirmContractView(
        self.bot_instance,
        self.participant_ids,
        type_id,
        return_page=self.page,
        return_query=self.query,
      ),
    )


class ContractSearchModal(discord.ui.Modal, title="Пошук контракту"):
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
      label="Назва контракту",
      placeholder="Наприклад: балони, дрова, переробка...",
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

    # Modal був відкритий кнопкою з цього ж ephemeral-повідомлення,
    # тому редагуємо його, а не створюємо ще одне.
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
    label="Назад",
    style=discord.ButtonStyle.secondary,
    emoji="◀️",
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
    label="Далі",
    style=discord.ButtonStyle.secondary,
    emoji="▶️",
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
    label="Пошук",
    style=discord.ButtonStyle.primary,
    emoji="🔎",
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
    label="Показати всі",
    style=discord.ButtonStyle.secondary,
    emoji="📋",
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



def build_confirmation_embed(contract_type: sqlite3.Row, participant_ids: list[int]) -> discord.Embed:
  embed = discord.Embed(
    title="Підтвердити виконання контракту",
    color=discord.Color.blurple(),
  )
  embed.add_field(
    name="👥 Виконавці",
    value=" ".join(f"<@{uid}>" for uid in participant_ids),
    inline=False,
  )
  embed.add_field(name="📋 Контракт", value=contract_type["name"], inline=True)
  embed.add_field(
    name="💰 Сума",
    value=f"{format_money_dollars(contract_type['price'])} $",
    inline=True,
  )
  embed.add_field(name="⏳ КД", value=contract_type["cooldown"], inline=True)
  return embed


class ConfirmContractView(discord.ui.View):
  def __init__(
    self,
    bot_instance: "ContractBot",
    participant_ids: list[int],
    type_id: int,
    return_page: int = 0,
    return_query: Optional[str] = None,
  ):
    super().__init__(timeout=300)
    self.bot_instance = bot_instance
    self.participant_ids = participant_ids
    self.type_id = type_id
    self.return_page = return_page
    self.return_query = return_query

  @discord.ui.button(label="Підтвердити", style=discord.ButtonStyle.success, emoji="✅")
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    guild = interaction.guild
    if guild is None:
      await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
      return

    contract_type = db.get_contract_type(self.type_id)
    if not contract_type or not contract_type["active"]:
      await interaction.response.send_message(
        "❌ Контракт уже видалений із переліку.",
        ephemeral=True,
      )
      return

    channel = await get_target_channel(guild, interaction.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
      await interaction.response.send_message(
        "❌ Не знайшов канал контрактів.",
        ephemeral=True,
      )
      return

    # Одразу прибираємо кнопки, щоб подвійний клік не створив дубль.
    await interaction.response.edit_message(
      content="⏳ Записую контракт...",
      embed=None,
      view=None,
    )

    placeholder = await channel.send("⏳ Записую контракт...")

    db.add_completed_contract(
      message_id=placeholder.id,
      guild_id=guild.id,
      channel_id=channel.id,
      creator_id=interaction.user.id,
      participant_ids=self.participant_ids,
      contract_type=contract_type,
    )

    row = db.get_completed_by_message(placeholder.id)
    await placeholder.edit(
      content=None,
      embed=build_completed_embed(row),
      view=UnpaidCompletedView(self.bot_instance),
    )

    await audit_log(
      guild,
      "✅ Контракт записано",
      (
        f"Запис: **#{row['id']}**\n"
        f"Контракт: **{row['contract_name']}**\n"
        f"Сума: **{format_money_dollars(row['price'])} $**\n"
        f"Виконавці: {mentions(self.participant_ids)}\n"
        f"Записав/ла: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    # Панель завжди переносимо в самий низ каналу.
    if isinstance(channel, discord.TextChannel):
      await move_main_panel_to_bottom(guild, channel)

    await interaction.edit_original_response(
      content=f"✅ Контракт записано: {placeholder.jump_url}",
      embed=None,
      view=None,
    )

  @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, emoji="↩️")
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

  @discord.ui.button(label="Так, скасувати", style=discord.ButtonStyle.danger, emoji="🗑️")
  async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="❌ Немає права.", view=None)
      return

    row_before = db.get_completed_by_message(self.message_id)

    await interaction.response.edit_message(
      content="⏳ Скасовую запис...",
      view=None,
    )

    ok = db.cancel_completed(self.message_id, interaction.user.id)
    if not ok:
      await interaction.edit_original_response(
        content="❌ Скасувати можна тільки неоплачений контракт.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    if row_before:
      await audit_log(
        interaction.guild,
        "🗑️ Контракт скасовано",
        (
          f"Запис: **#{row_before['id']}**\n"
          f"Контракт: **{row_before['contract_name']}**\n"
          f"Скасував/ла: <@{interaction.user.id}>"
        ),
        discord.Color.red(),
      )

    await interaction.edit_original_response(
      content="✅ Запис скасовано. Він більше не рахується в статистиці.",
      view=None,
    )

  @discord.ui.button(label="Ні", style=discord.ButtonStyle.secondary)
  async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="Скасування відмінено.", view=None)


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
    label="Підтвердити",
    style=discord.ButtonStyle.success,
    emoji="✅",
  )
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="❌ Немає права.", view=None)
      return

    row_before = db.get_completed_by_message(self.message_id)
    if not row_before or row_before["status"] != "unpaid":
      await interaction.response.edit_message(
        content="❌ Контракт уже оплачений або скасований.",
        embed=None,
        view=None,
      )
      return

    # Блокуємо повторне натискання одразу.
    await interaction.response.edit_message(
      content="⏳ Проводжу оплату...",
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
        content="❌ Не вдалося провести оплату.",
        embed=None,
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    payouts_text = "\n".join(
      f"<@{uid}> — **{format_cents(amount)}**"
      for uid, amount in result["payouts"].items()
    ) or "—"

    excluded_text = (
      "—"
      if self.payment_mode == PAYMENT_MODE_LEGACY_FAMILY
      else mentions(result["excluded_payment_ids"])
    )

    await audit_log(
      interaction.guild,
      "💵 Контракт оплачено",
      (
        f"Запис: **#{row_before['id']}**\n"
        f"Контракт: **{row_before['contract_name']}**\n"
        f"Спосіб: **{payment_mode_label(self.payment_mode)}**\n"
        f"Банк сім'ї: **{format_cents(result['fomo_cents'])}**\n"
        f"Учасникам: **{format_cents(result['net_cents'])}**\n"
        f"Без виплати: {excluded_text}\n"
        f"Розподіл:\n{payouts_text}\n"
        f"Оплатив/ла: <@{interaction.user.id}>"
      ),
      discord.Color.green(),
    )

    await interaction.edit_original_response(
      content="✅ Оплату проведено.",
      embed=None,
      view=None,
    )

  @discord.ui.button(
    label="Назад",
    style=discord.ButtonStyle.secondary,
    emoji="↩️",
  )
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    row = db.get_completed_by_message(self.message_id)
    if not row or interaction.guild is None:
      await interaction.response.edit_message(
        content="❌ Контракт уже недоступний.",
        embed=None,
        view=None,
      )
      return

    if self.back_to_custom:
      await interaction.response.edit_message(
        content=(
          "⚙️ **Налаштувати оплату**\n"
          "Оберіть, кого не потрібно оплачувати, а потім спосіб розподілу."
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
        content="Оплату не проведено.",
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
          description="Не виплачувати гроші цьому учаснику",
          default=uid in selected,
        )
      )

    super().__init__(
      placeholder="Кого виключити з оплати?",
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
        "⚙️ **Налаштувати оплату**\n"
        f"🚫 Без виплати: {mentions(view.excluded_ids)}\n\n"
        "Оберіть спосіб:"
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
    label="Розділити між рештою",
    style=discord.ButtonStyle.success,
    emoji="💸",
    row=1,
  )
  async def redistribute(self, interaction: discord.Interaction, button: discord.ui.Button):
    row = db.get_completed_by_message(self.message_id)
    if not row:
      await interaction.response.edit_message(content="❌ Контракт не знайдено.", view=None)
      return

    try:
      embed = payment_preview_embed(
        row,
        PAYMENT_MODE_REDISTRIBUTE,
        self.excluded_ids,
      )
    except ValueError:
      await interaction.response.edit_message(
        content="❌ Для цього способу має залишитися хоча б один отримувач.",
        view=self,
      )
      return

    await interaction.response.edit_message(
      content=None,
      embed=embed,
      view=PaymentConfirmView(
        self.bot_instance,
        self.message_id,
        PAYMENT_MODE_REDISTRIBUTE,
        self.excluded_ids,
        back_to_custom=True,
      ),
    )

  @discord.ui.button(
    label="Частку в сім'ю",
    style=discord.ButtonStyle.primary,
    emoji="🏦",
    row=1,
  )
  async def family_share(self, interaction: discord.Interaction, button: discord.ui.Button):
    row = db.get_completed_by_message(self.message_id)
    if not row:
      await interaction.response.edit_message(content="❌ Контракт не знайдено.", view=None)
      return

    embed = payment_preview_embed(
      row,
      PAYMENT_MODE_FAMILY_SHARE,
      self.excluded_ids,
    )

    await interaction.response.edit_message(
      content=None,
      embed=embed,
      view=PaymentConfirmView(
        self.bot_instance,
        self.message_id,
        PAYMENT_MODE_FAMILY_SHARE,
        self.excluded_ids,
        back_to_custom=True,
      ),
    )

  @discord.ui.button(
    label="Назад",
    style=discord.ButtonStyle.secondary,
    emoji="↩️",
    row=1,
  )
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="Налаштування оплати закрито.",
      embed=None,
      view=None,
    )


class CorrectionPerformerSelect(discord.ui.UserSelect):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(
      placeholder="Оберіть правильних виконавців",
      min_values=1,
      max_values=25,
    )
    self.bot_instance = bot_instance
    self.message_id = message_id

  async def callback(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="❌ Немає права.", view=None)
      return

    new_ids = [u.id for u in self.values if not getattr(u, "bot", False)]
    row_before = db.get_completed_by_message(self.message_id)

    if not row_before or not db.update_completed_participants(self.message_id, new_ids):
      await interaction.response.edit_message(
        content="❌ Змінити можна тільки неоплачений контракт.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    await audit_log(
      interaction.guild,
      "✏️ Змінено виконавців",
      (
        f"Запис: **#{row_before['id']}**\n"
        f"Було: {mentions(parse_ids(row_before['participant_ids']))}\n"
        f"Стало: {mentions(new_ids)}\n"
        f"Змінив/ла: <@{interaction.user.id}>"
      ),
      discord.Color.orange(),
    )

    await interaction.response.edit_message(
      content=f"✅ Виконавців оновлено: {mentions(new_ids)}",
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
        description=f"{format_money_dollars(row['price'])} $ • КД {row['cooldown']}"[:100],
      )
      for row in rows
    ]

    super().__init__(
      placeholder=f"Оберіть контракт • {self.page + 1}/{self.total_pages}",
      min_values=1,
      max_values=1,
      options=options,
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="❌ Немає права.", view=None)
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
        content="❌ Змінити можна тільки неоплачений контракт.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    await audit_log(
      interaction.guild,
      "✏️ Змінено контракт у записі",
      (
        f"Запис: **#{row_before['id']}**\n"
        f"Було: **{row_before['contract_name']}** — "
        f"{format_money_dollars(row_before['price'])} $\n"
        f"Стало: **{contract_type['name']}** — "
        f"{format_money_dollars(contract_type['price'])} $\n"
        f"Змінив/ла: <@{interaction.user.id}>"
      ),
      discord.Color.orange(),
    )

    await interaction.response.edit_message(
      content=f"✅ Контракт змінено на **{contract_type['name']}**.",
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

  @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
  async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      view=CorrectionContractView(self.bot_instance, self.message_id, self.page - 1)
    )

  @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=1)
  async def page_label(self, interaction: discord.Interaction, button: discord.ui.Button):
    pass

  @discord.ui.button(label="Далі", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
  async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      view=CorrectionContractView(self.bot_instance, self.message_id, self.page + 1)
    )


class CorrectionMenuView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(timeout=180)
    self.bot_instance = bot_instance
    self.message_id = message_id

  @discord.ui.button(label="Виконавці", emoji="👥", style=discord.ButtonStyle.primary)
  async def performers(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="👥 Оберіть правильний список виконавців:",
      view=CorrectionPerformerView(self.bot_instance, self.message_id),
    )

  @discord.ui.button(label="Контракт", emoji="📋", style=discord.ButtonStyle.primary)
  async def contract(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="📋 Оберіть правильний контракт:",
      view=CorrectionContractView(self.bot_instance, self.message_id),
    )

  @discord.ui.button(label="Назад", emoji="↩️", style=discord.ButtonStyle.secondary)
  async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="Редагування закрито.", view=None)


class AnnulPaidConfirmView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot", message_id: int):
    super().__init__(timeout=60)
    self.bot_instance = bot_instance
    self.message_id = message_id

  @discord.ui.button(
    label="Так, анулювати",
    style=discord.ButtonStyle.danger,
    emoji="🚫",
  )
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(
        content="❌ Немає права.",
        view=None,
      )
      return

    row_before = db.get_completed_by_message(self.message_id)
    if not row_before or row_before["status"] != "paid":
      await interaction.response.edit_message(
        content="❌ Анулювати можна тільки оплачений контракт.",
        view=None,
      )
      return

    await interaction.response.edit_message(
      content="⏳ Анулюю контракт...",
      view=None,
    )

    ok = db.annul_paid(
      self.message_id,
      interaction.user.id,
    )

    if not ok:
      await interaction.edit_original_response(
        content="❌ Не вдалося анулювати контракт.",
        view=None,
      )
      return

    await refresh_completed_message(self.message_id)

    await audit_log(
      interaction.guild,
      "🚫 Оплачений контракт анульовано",
      (
        f"Запис: **#{row_before['id']}**\n"
        f"Контракт: **{row_before['contract_name']}**\n"
        f"Сума: **{format_money_dollars(row_before['price'])} $**\n"
        f"Було в Банк сім'ї: **{format_cents(row_before['fomo_cents'] or 0)}**\n"
        f"Було учасникам: **{format_cents(row_before['net_cents'] or 0)}**\n"
        f"Анулював/ла: <@{interaction.user.id}>"
      ),
      discord.Color.red(),
    )

    await interaction.edit_original_response(
      content=(
        "✅ Контракт анульовано.\n"
        "Його гроші та бали більше не враховуються у статистиці, "
        "але запис залишився в історії."
      ),
      view=None,
    )

  @discord.ui.button(
    label="Назад",
    style=discord.ButtonStyle.secondary,
    emoji="↩️",
  )
  async def back(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content="Анулювання скасовано.",
      view=None,
    )


class UnpaidCompletedView(discord.ui.View):
  def __init__(self, bot_instance: "ContractBot"):
    super().__init__(timeout=None)
    self.bot_instance = bot_instance

  @discord.ui.button(
    label="Оплата",
    style=discord.ButtonStyle.success,
    emoji="💵",
    custom_id="contract_v3:paid",
    row=0,
  )
  async def paid(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "❌ Оплачувати контракти може тільки керівництво.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message("❌ Не знайшов запис.", ephemeral=True)
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "❌ Контракт уже оплачений або скасований.",
        ephemeral=True,
      )
      return

    embed = payment_preview_embed(
      row,
      PAYMENT_MODE_NORMAL,
      [],
    )

    await interaction.response.send_message(
      embed=embed,
      view=PaymentConfirmView(
        self.bot_instance,
        interaction.message.id,
        PAYMENT_MODE_NORMAL,
      ),
      ephemeral=True,
    )

  @discord.ui.button(
    label="На фаму",
    style=discord.ButtonStyle.primary,
    emoji="🏠",
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
        "❌ Оплачувати контракти може тільки керівництво.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message(
        "❌ Не знайшов запис.",
        ephemeral=True,
      )
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "❌ Контракт уже оплачений або скасований.",
        ephemeral=True,
      )
      return

    embed = payment_preview_embed(
      row,
      PAYMENT_MODE_LEGACY_FAMILY,
      [],
    )

    await interaction.response.send_message(
      embed=embed,
      view=PaymentConfirmView(
        self.bot_instance,
        interaction.message.id,
        PAYMENT_MODE_LEGACY_FAMILY,
      ),
      ephemeral=True,
    )

  @discord.ui.button(
    label="Налаштувати оплату",
    style=discord.ButtonStyle.primary,
    emoji="⚙️",
    custom_id="contract_v4:custompay",
    row=1,
  )
  async def custom_payment(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "❌ Налаштовувати оплату може тільки керівництво.",
        ephemeral=True,
      )
      return

    if interaction.message is None or interaction.guild is None:
      await interaction.response.send_message("❌ Не знайшов запис.", ephemeral=True)
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "❌ Контракт уже оплачений або скасований.",
        ephemeral=True,
      )
      return

    participant_ids = parse_ids(row["participant_ids"])

    await interaction.response.send_message(
      (
        "⚙️ **Налаштувати оплату**\n"
        "Оберіть, кого не потрібно оплачувати.\n\n"
        "**Розділити між рештою** — 85% ділиться між тими, хто залишився.\n"
        "**Частку в сім'ю** — частка виключених переходить у Банк сім'ї."
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
    label="Виправити",
    style=discord.ButtonStyle.secondary,
    emoji="✏️",
    custom_id="contract_v4:edit",
    row=2,
  )
  async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "❌ Виправляти записи може тільки керівництво.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message("❌ Не знайшов запис.", ephemeral=True)
      return

    row = db.get_completed_by_message(interaction.message.id)
    if not row or row["status"] != "unpaid":
      await interaction.response.send_message(
        "❌ Виправляти можна тільки неоплачений контракт.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      "✏️ Що потрібно виправити?",
      view=CorrectionMenuView(self.bot_instance, interaction.message.id),
      ephemeral=True,
    )

  @discord.ui.button(
    label="Скасувати",
    style=discord.ButtonStyle.danger,
    emoji="🗑️",
    custom_id="contract_v3:cancel",
    row=2,
  )
  async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message(
        "❌ Скасовувати записи може тільки керівництво.",
        ephemeral=True,
      )
      return

    if interaction.message is None:
      await interaction.response.send_message("❌ Не знайшов запис.", ephemeral=True)
      return

    await interaction.response.send_message(
      "⚠️ Скасувати цей запис? Він буде виключений зі статистики.",
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
      title="Додати контракт" if mode == "add" else "Редагувати контракт",
      timeout=300,
    )

    self.name_input = discord.ui.TextInput(
      label="Назва контракту",
      placeholder="Наприклад: Майстри баків",
      default=row["name"] if row else None,
      max_length=100,
    )
    self.price_input = discord.ui.TextInput(
      label="Ціна контракту",
      placeholder="Наприклад: 100000 або 100к",
      default=str(row["price"]) if row else None,
      max_length=20,
    )
    self.cooldown_input = discord.ui.TextInput(
      label="КД контракту",
      placeholder="Наприклад: 4 год",
      default=row["cooldown"] if row else None,
      max_length=50,
    )

    self.add_item(self.name_input)
    self.add_item(self.price_input)
    self.add_item(self.cooldown_input)

  async def on_submit(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return

    try:
      price = parse_money(str(self.price_input))
    except ValueError:
      await interaction.response.send_message(
        "❌ Некоректна ціна. Приклади: `100000`, `100к`, `1.2м`.",
        ephemeral=True,
      )
      return

    name = str(self.name_input).strip()
    cooldown = str(self.cooldown_input).strip()

    if self.mode == "add":
      row = db.create_contract_type(name, price, cooldown, interaction.user.id)
      await audit_log(
        interaction.guild,
        "➕ Додано тип контракту",
        (
          f"**{row['name']}**\n"
          f"Ціна: **{format_money_dollars(row['price'])} $**\n"
          f"КД: **{row['cooldown']}**\n"
          f"Додав/ла: <@{interaction.user.id}>"
        ),
        discord.Color.green(),
      )
      await interaction.response.send_message(
        f"✅ Додано: **{row['name']}** — {format_money_dollars(row['price'])} $ — КД {row['cooldown']}",
        ephemeral=True,
      )
      return

    ok = db.update_contract_type(self.type_id, name, price, cooldown)
    if not ok:
      await interaction.response.send_message(
        "❌ Не вдалося зберегти. Можливо, контракт з такою назвою вже існує.",
        ephemeral=True,
      )
      return

    row = db.get_contract_type(self.type_id)
    await audit_log(
      interaction.guild,
      "✏️ Оновлено тип контракту",
      (
        f"**{row['name']}**\n"
        f"Ціна: **{format_money_dollars(row['price'])} $**\n"
        f"КД: **{row['cooldown']}**\n"
        f"Змінив/ла: <@{interaction.user.id}>"
      ),
      discord.Color.orange(),
    )
    await interaction.response.send_message(
      f"✅ Оновлено: **{row['name']}** — {format_money_dollars(row['price'])} $ — КД {row['cooldown']}",
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
    "📋 **Керування контрактами**\n"
    f"Оберіть контракт зі списку • сторінка **{page + 1}/{total_pages}**"
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
          f"{format_money_dollars(row['price'])} $ • КД {row['cooldown']}"
        )[:100],
      )
      for row in rows
    ]

    if not options:
      options = [
        discord.SelectOption(
          label="Контрактів ще немає",
          value="none",
          description="Спочатку натисніть «Додати»",
        )
      ]

    super().__init__(
      placeholder=f"Контракти • {page + 1}/{total_pages}",
      options=options,
      min_values=1,
      max_values=1,
      disabled=(options[0].value == "none"),
      row=0,
    )

  async def callback(self, interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return

    type_id = int(self.values[0])
    row = db.get_contract_type(type_id)

    if not row or not row["active"]:
      await interaction.response.edit_message(
        content="❌ Цей контракт уже недоступний.",
        embed=None,
        view=AdminManagePickerView(self.page),
      )
      return

    embed = discord.Embed(
      title=f"⚙️ {row['name']}",
      color=discord.Color.blurple(),
    )
    embed.add_field(
      name="💰 Ціна",
      value=f"{format_money_dollars(row['price'])} $",
      inline=True,
    )
    embed.add_field(
      name="⏳ КД",
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
    label="Назад",
    style=discord.ButtonStyle.secondary,
    emoji="◀️",
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
    label="Далі",
    style=discord.ButtonStyle.secondary,
    emoji="▶️",
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

  @discord.ui.button(label="Так, видалити", style=discord.ButtonStyle.danger, emoji="🗑️")
  async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="❌ Немає права.", view=None)
      return

    row = db.get_contract_type(self.type_id)
    db.archive_contract_type(self.type_id)
    await audit_log(
      interaction.guild,
      "🗑️ Тип контракту прибрано",
      (
        f"**{row['name'] if row else self.type_id}**\n"
        f"Прибрав/ла: <@{interaction.user.id}>"
      ),
      discord.Color.red(),
    )
    await interaction.response.edit_message(
      content="✅ Контракт прибрано з переліку. Старі виконання залишилися в історії.",
      embed=None,
      view=None,
    )

  @discord.ui.button(label="Ні", style=discord.ButtonStyle.secondary)
  async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="Видалення відмінено.", embed=None, view=None)


class ManageOneTypeView(discord.ui.View):
  def __init__(self, type_id: int, return_page: int = 0):
    super().__init__(timeout=300)
    self.type_id = type_id
    self.return_page = return_page

  @discord.ui.button(label="Редагувати", style=discord.ButtonStyle.primary, emoji="✏️")
  async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return

    # Тут форма залишається, бо Discord дозволяє вводити назву/ціну/КД
    # саме через Modal. Але пошуку через окреме вікно більше немає.
    await interaction.response.send_modal(
      ContractTypeModal("edit", interaction.user.id, self.type_id)
    )

  @discord.ui.button(label="Видалити", style=discord.ButtonStyle.danger, emoji="🗑️")
  async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return

    row = db.get_contract_type(self.type_id)
    await interaction.response.edit_message(
      content=f"⚠️ Прибрати **{row['name']}** з переліку?",
      embed=None,
      view=DeleteTypeConfirmView(self.type_id),
    )

  @discord.ui.button(label="До списку", style=discord.ButtonStyle.secondary, emoji="↩️")
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
    label="Так, обнулити рейтинг",
    style=discord.ButtonStyle.danger,
    emoji="♻️",
  )
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="❌ Немає права.", view=None)
      return

    if interaction.guild is None:
      await interaction.response.edit_message(content="❌ Це працює тільки на сервері.", view=None)
      return

    reset_at = utc_now_iso()
    db.set_setting(interaction.guild.id, "rating_reset_at", reset_at)
    reset_ts = iso_to_unix(reset_at)

    when = f"<t:{reset_ts}:f>" if reset_ts else "зараз"
    await audit_log(
      interaction.guild,
      "♻️ Рейтинг обнулено",
      f"Обнулив/ла: <@{interaction.user.id}>",
      discord.Color.red(),
    )
    await interaction.response.edit_message(
      content=(
        f"✅ Рейтинг обнулено {when}.\n"
        "Старі контракти та фінанси не змінені. "
        "З цього моменту з нуля рахується тільки рейтинг за балами."
      ),
      view=None,
    )

  @discord.ui.button(label="Ні", style=discord.ButtonStyle.secondary)
  async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(content="Обнулення рейтингу скасовано.", view=None)


class ResetEarningsConfirmView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=60)

  @discord.ui.button(
    label="Так, обнулити заробіток",
    style=discord.ButtonStyle.danger,
    emoji="💸",
  )
  async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.edit_message(content="❌ Немає права.", view=None)
      return

    if interaction.guild is None:
      await interaction.response.edit_message(
        content="❌ Це працює тільки на сервері.",
        view=None,
      )
      return

    reset_at = utc_now_iso()
    db.set_setting(interaction.guild.id, "earnings_reset_at", reset_at)
    reset_ts = iso_to_unix(reset_at)
    when = f"<t:{reset_ts}:f>" if reset_ts else "зараз"

    await audit_log(
      interaction.guild,
      "💸 Заробіток обнулено",
      f"Обнулив/ла: <@{interaction.user.id}>",
      discord.Color.red(),
    )

    await interaction.response.edit_message(
      content=(
        f"✅ Заробіток обнулено {when}.\n"
        "Історія контрактів та оплат не видалена. "
        "З цього моменту з нуля рахуються загальний заробіток, "
        "Банк сім'ї та заробіток кожного учасника."
      ),
      view=None,
    )

  @discord.ui.button(label="Ні", style=discord.ButtonStyle.secondary)
  async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.edit_message(
      content="Обнулення заробітку скасовано.",
      view=None,
    )


class ContractAdminPanelView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=300)

  @discord.ui.button(label="Додати", style=discord.ButtonStyle.success, emoji="➕")
  async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return
    await interaction.response.send_modal(ContractTypeModal("add", interaction.user.id))

  @discord.ui.button(
    label="Керувати контрактами",
    style=discord.ButtonStyle.primary,
    emoji="📋",
  )
  async def manage_contracts(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return

    rows, page, total_pages = admin_picker_page_data(0)
    if not rows:
      await interaction.response.send_message(
        "Поки що контрактів немає. Спочатку натисніть **➕ Додати**.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      admin_picker_text(page, total_pages),
      view=AdminManagePickerView(page),
      ephemeral=True,
    )


  @discord.ui.button(
    label="Обнулити рейтинг",
    style=discord.ButtonStyle.danger,
    emoji="♻️",
  )
  async def reset_rating(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return

    await interaction.response.send_message(
      "⚠️ Обнулити поточний рейтинг?\n"
      "Контракти, виплати та заробіток залишаться без змін. "
      "З нуля почнуться тільки бали рейтингу.",
      view=ResetRatingConfirmView(),
      ephemeral=True,
    )


  @discord.ui.button(
    label="Обнулити заробіток",
    style=discord.ButtonStyle.danger,
    emoji="💸",
  )
  async def reset_earnings(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
      await interaction.response.send_message("❌ Немає права.", ephemeral=True)
      return

    await interaction.response.send_message(
      "⚠️ Обнулити статистику заробітку?\n"
      "Контракти й історія оплат залишаться в базі. "
      "Але загальний заробіток, Банк сім'ї та заробіток учасників "
      "у статистиці почнуться з нуля.",
      view=ResetEarningsConfirmView(),
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
    f"Поточний рейтинг • з <t:{reset_ts}:d>"
    if reset_ts
    else "Поточний рейтинг • від початку"
  )

  embed = discord.Embed(
    title="🏆 Рейтинг учасників",
    description=subtitle,
    color=discord.Color.gold(),
  )

  if not ranking:
    embed.add_field(
      name="Рейтинг",
      value="Поки немає виконаних контрактів.",
      inline=False,
    )
    return embed

  lines = [
    f"**{idx}.** <@{uid}> — **{format_points_with_word(points[uid])}**"
    for idx, uid in enumerate(ranking[:25], start=1)
  ]
  embed.add_field(
    name="Таблиця",
    value="\n".join(lines),
    inline=False,
  )
  embed.set_footer(text="У рейтингу показуються тільки бали.")
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
    f"🏆 Рейтинг: з <t:{rating_ts}:d>"
    if rating_ts
    else "🏆 Рейтинг: від початку"
  )
  period_lines.append(
    f"💵 Фінанси: з <t:{earnings_ts}:d>"
    if earnings_ts
    else "💵 Фінанси: від початку"
  )

  embed = discord.Embed(
    title="👤 Моя статистика • Поточний період",
    description="\n".join(period_lines),
    color=discord.Color.blurple(),
  )

  embed.add_field(
    name="🏆 Рейтинг",
    value=(
      (f"Місце: **#{position}**\n" if position else "Місце: **—**\n")
      + f"Бали: **{format_points_with_word(points[user_id])}**\n"
      + f"Участей: **{participations[user_id]}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="💵 Заробіток",
    value=(
      f"Отримано: **{format_cents(personal_earnings)}**\n"
      f"Оплачених контрактів: **{len(paid_period_rows)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="⏳ Зараз",
    value=(
      f"Очікують оплати: **{len(unpaid_now_rows)}**"
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
    title="🗂️ Моя статистика • Історія",
    description="За весь час. Обнулення рейтингу або грошей на цю вкладку не впливають.",
    color=discord.Color.dark_teal(),
  )

  embed.add_field(
    name="🏆 Рейтинг за весь час",
    value=(
      (f"Місце: **#{position}**\n" if position else "Місце: **—**\n")
      + f"Бали: **{format_points_with_word(points[user_id])}**\n"
      + f"Участей: **{participations[user_id]}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="💵 Заробіток за весь час",
    value=(
      f"Отримано: **{format_cents(personal_earnings)}**\n"
      f"Контрактів повністю на фаму: **{full_family_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="📋 Контракти за весь час",
    value=(
      f"Участей: **{len(involved_rows)}**\n"
      f"Оплачено: **{len(paid_rows)}**\n"
      f"Не оплачено зараз: **{len(unpaid_rows)}**"
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
    title="📅 Мій заробіток • По днях",
    color=discord.Color.blurple(),
  )

  if not slice_rows:
    embed.description = "Поки немає виплат."
  else:
    lines = [
      f"**{format_day(stat['day'])}** — {format_cents(stat['amount'])} • виплат: {stat['payments']}"
      for stat in slice_rows
    ]
    embed.description = "\n".join(lines)

  embed.set_footer(
    text=f"Часова зона: {TIMEZONE_NAME} • Сторінка {page + 1}/{total_pages}"
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
    label="Поточна",
    emoji="👤",
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
    label="По днях",
    emoji="📅",
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
    label="Історія",
    emoji="🗂️",
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
    label="Назад",
    emoji="◀️",
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
    label="Далі",
    emoji="▶️",
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

  paid_rows = earnings["paid_rows"]
  unpaid_rows = earnings["unpaid_rows"]

  gross = sum(row["price"] * 100 for row in paid_rows)
  family = sum((row["fomo_cents"] or 0) for row in paid_rows)
  members = sum((row["net_cents"] or 0) for row in paid_rows)
  unpaid = sum(row["price"] * 100 for row in unpaid_rows)

  custom_count = sum(
    1 for row in paid_rows
    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) in (
      PAYMENT_MODE_REDISTRIBUTE,
      PAYMENT_MODE_FAMILY_SHARE,
    )
  )
  exception_count = sum(
    1 for row in paid_rows
    if parse_ids(row["excluded_payment_ids"] or "[]")
    and (row["payment_mode"] or PAYMENT_MODE_NORMAL) != PAYMENT_MODE_LEGACY_FAMILY
  )
  full_family_count = sum(
    1 for row in paid_rows
    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) == PAYMENT_MODE_LEGACY_FAMILY
  )

  avg_contract = gross // len(paid_rows) if paid_rows else 0

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
      f"🏆 Рейтинг: з <t:{rating_ts}:d>"
      if rating_ts
      else "🏆 Рейтинг: від початку"
    ),
    (
      f"💵 Фінанси: з <t:{earnings_ts}:d>"
      if earnings_ts
      else "💵 Фінанси: від початку"
    ),
  ]

  embed = discord.Embed(
    title="📊 Статистика • Поточний період",
    description="\n".join(description_lines),
    color=discord.Color.blurple(),
  )

  embed.add_field(
    name="📋 Контракти",
    value=(
      f"Оплачено в періоді: **{len(paid_rows)}**\n"
      f"Не оплачено зараз: **{len(unpaid_rows)}**\n"
      f"Повністю на фаму: **{full_family_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="👥 Активність",
    value=(
      f"Учасників у рейтингу: **{len(active_users)}**\n"
      f"Участей у рейтингу: **{sum(participations.values())}**\n"
      f"Середня команда: **{avg_team:.1f}**\n"
      f"Оплат з винятками: **{exception_count}**\n"
      f"Налаштованих оплат: **{custom_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="💰 Фінанси",
    value=(
      f"Загалом по контрактах: **{format_cents(gross)}**\n"
      f"На фаму: **{format_cents(family)}**\n"
      f"Учасникам: **{format_cents(members)}**\n"
      f"Очікує оплати: **{format_cents(unpaid)}**\n"
      f"Середній оплачений контракт: **{format_cents(avg_contract)}**"
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
    title="🗂️ Статистика • Історія",
    description="За весь час. Обнулення рейтингу та грошей ці дані не стирають.",
    color=discord.Color.dark_teal(),
  )

  embed.add_field(
    name="📋 Контракти за весь час",
    value=(
      f"Всього дійсних: **{len(valid_rows)}**\n"
      f"Оплачено: **{len(paid_rows)}**\n"
      f"Не оплачено зараз: **{len(unpaid_rows)}**\n"
      f"Скасовано: **{len(cancelled)}**\n"
      f"Анульовано після оплати: **{len(annulled)}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="👥 Активність за весь час",
    value=(
      f"Учасників: **{len(active_users)}**\n"
      f"Участей: **{sum(participations.values())}**\n"
      f"Середня команда: **{avg_team:.1f}**\n"
      f"Оплат з винятками: **{exception_count}**\n"
      f"Повністю на фаму: **{full_family_count}**"
    ),
    inline=True,
  )

  embed.add_field(
    name="💰 Фінанси за весь час",
    value=(
      f"Загалом по контрактах: **{format_cents(gross)}**\n"
      f"На фаму: **{format_cents(family)}**\n"
      f"Учасникам: **{format_cents(members)}**\n"
      f"Не оплачено зараз: **{format_cents(unpaid)}**"
    ),
    inline=False,
  )

  if rating_users:
    rating_lines = [
      f"**{idx}.** <@{uid}> — **{format_points_with_word(points[uid])}**"
      for idx, uid in enumerate(rating_users[:5], start=1)
    ]
    embed.add_field(
      name="🏆 Топ рейтингу за весь час",
      value="\n".join(rating_lines),
      inline=False,
    )

  if top_contracts:
    contract_lines = [
      (
        f"**{idx}. {name}** — {format_cents(stat['gross'])} "
        f"• {stat['count']} раз(и)"
      )
      for idx, (name, stat) in enumerate(top_contracts, start=1)
    ]
    embed.add_field(
      name="📋 Топ контрактів за весь час",
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
    title="📋 Статистика • По контрактах",
    description=(
      "Для кожного типу: загальний оборот і скільки з нього пішло на фаму."
    ),
    color=discord.Color.blurple(),
  )

  if not slice_rows:
    embed.description = "Ще немає оплачених контрактів."
  else:
    for stat in slice_rows:
      embed.add_field(
        name=stat["name"],
        value=(
          f"Оплачено: **{stat['count']}**\n"
          f"Загалом: **{format_cents(stat['gross'])}**\n"
          f"На фаму: **{format_cents(stat['family'])}**\n"
          f"Учасникам: **{format_cents(stat['members'])}**\n"
          f"З винятками: **{stat['exceptions']}**"
        ),
        inline=True,
      )

  embed.set_footer(text=f"Сторінка {page + 1}/{total_pages}")
  return embed, page, total_pages



def daily_stats_rows(guild_id: int):
  earnings = earnings_data_for_guild(guild_id)
  grouped = {}

  for row in earnings["paid_rows"]:
    day = local_date_from_iso(row["paid_at"])
    if day is None:
      continue

    stat = grouped.setdefault(
      day,
      {
        "day": day,
        "count": 0,
        "gross": 0,
        "family": 0,
        "members": 0,
        "exceptions": 0,
        "full_family": 0,
      },
    )

    stat["count"] += 1
    stat["gross"] += row["price"] * 100
    stat["family"] += row["fomo_cents"] or 0
    stat["members"] += row["net_cents"] or 0

    if parse_ids(row["excluded_payment_ids"] or "[]") and (
      (row["payment_mode"] or PAYMENT_MODE_NORMAL) != PAYMENT_MODE_LEGACY_FAMILY
    ):
      stat["exceptions"] += 1

    if (row["payment_mode"] or PAYMENT_MODE_NORMAL) == PAYMENT_MODE_LEGACY_FAMILY:
      stat["full_family"] += 1

  return sorted(
    grouped.values(),
    key=lambda stat: stat["day"],
    reverse=True,
  )


def build_daily_stats_embed(guild_id: int, page: int = 0):
  stats = daily_stats_rows(guild_id)
  page_size = 5
  total_pages = max(1, (len(stats) + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  slice_rows = stats[page * page_size:(page + 1) * page_size]

  earnings = earnings_data_for_guild(guild_id)
  reset_at = earnings["reset_at"]
  reset_ts = iso_to_unix(reset_at) if reset_at else None

  description = (
    f"Заробіток по днях • з <t:{reset_ts}:d>"
    if reset_ts
    else "Заробіток по днях • від початку"
  )

  embed = discord.Embed(
    title="📅 Статистика • По днях",
    description=description,
    color=discord.Color.blurple(),
  )

  if not slice_rows:
    embed.add_field(
      name="Немає даних",
      value="Ще немає оплачених контрактів.",
      inline=False,
    )
  else:
    for stat in slice_rows:
      embed.add_field(
        name=format_day(stat["day"]),
        value=(
          f"Контрактів: **{stat['count']}**\n"
          f"Загалом: **{format_cents(stat['gross'])}**\n"
          f"На фаму: **{format_cents(stat['family'])}**\n"
          f"Учасникам: **{format_cents(stat['members'])}**\n"
          f"Повністю на фаму: **{stat['full_family']}**"
        ),
        inline=False,
      )

  embed.set_footer(
    text=f"Часова зона: {TIMEZONE_NAME} • Сторінка {page + 1}/{total_pages}"
  )
  return embed, page, total_pages


def build_member_stats_embed(guild_id: int) -> discord.Embed:
  points, participations, rating_users, _ = rating_data_for_guild(guild_id)
  earnings = earnings_data_for_guild(guild_id)

  money_users = sorted(
    earnings["member_earnings"],
    key=lambda uid: earnings["member_earnings"][uid],
    reverse=True,
  )

  embed = discord.Embed(
    title="👥 Статистика • Учасники • Поточний період",
    color=discord.Color.blurple(),
  )

  if rating_users:
    rating_lines = [
      f"**{idx}.** <@{uid}> — **{format_points_with_word(points[uid])}** • {participations[uid]} участей"
      for idx, uid in enumerate(rating_users[:10], start=1)
    ]
    embed.add_field(
      name="🏆 Рейтинг",
      value="\n".join(rating_lines),
      inline=False,
    )

  if money_users:
    money_lines = [
      f"**{idx}.** <@{uid}> — **{format_cents(earnings['member_earnings'][uid])}**"
      for idx, uid in enumerate(money_users[:10], start=1)
    ]
    embed.add_field(
      name="💵 Заробіток",
      value="\n".join(money_lines),
      inline=False,
    )

  if not rating_users and not money_users:
    embed.description = "Поки немає статистики."

  return embed


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
    elif mode == "daily":
      _, self.page, self.total_pages = build_daily_stats_embed(
        guild_id,
        page,
      )
    else:
      self.page = 0
      self.total_pages = 1

    self.previous.disabled = self.total_pages <= 1 or self.page <= 0
    self.next_page.disabled = self.total_pages <= 1 or self.page >= self.total_pages - 1

  @discord.ui.button(
    label="Поточна",
    emoji="📊",
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
    label="По контрактах",
    emoji="📋",
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
    label="По днях",
    emoji="📅",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def daily(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    embed, page, _ = build_daily_stats_embed(self.guild_id, 0)
    await interaction.response.edit_message(
      embed=embed,
      view=AdminStatsView(self.guild_id, "daily", page),
    )

  @discord.ui.button(
    label="Учасники",
    emoji="👥",
    style=discord.ButtonStyle.primary,
    row=0,
  )
  async def members(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=build_member_stats_embed(self.guild_id),
      view=AdminStatsView(self.guild_id, "members"),
    )

  @discord.ui.button(
    label="Історія",
    emoji="🗂️",
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
    label="Назад",
    emoji="◀️",
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
    elif self.mode == "daily":
      embed, page, _ = build_daily_stats_embed(
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
    label="Далі",
    emoji="▶️",
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
    elif self.mode == "daily":
      embed, page, _ = build_daily_stats_embed(
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
    title="📋 КОНТРАКТИ СІМ’Ї",
    description=(
      "Виконав контракт — обери потрібну кнопку.\n\n"
      "👤 **Я виконав/ла** — якщо виконував/ла сам/а.\n"
      "👥 **Кілька виконавців** — якщо контракт робили разом.\n"
      "🏆 **Рейтинг** — поточний рейтинг учасників.\n"
      "👤 **Моя статистика** — мої бали, участі та заробіток.\n"
      "🔎 **Пошук** — доступний прямо всередині списку контрактів.\n\n"
      "Назва, ціна та КД підтягуються автоматично."
    ),
    color=discord.Color.blurple(),
  )


async def move_main_panel_to_bottom(
  guild: discord.Guild,
  channel: discord.TextChannel,
) -> Optional[discord.Message]:
  """
  Тримає панель останнім повідомленням у каналі.
  Стару панель видаляємо; якщо Discord не дає — прибираємо з неї кнопки.
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
    label="Я виконав/ла",
    style=discord.ButtonStyle.success,
    emoji="👤",
    custom_id="contract_v34:self",
  )
  async def self_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not db.active_contract_types(limit=1):
      await interaction.response.send_message(
        "❌ Перелік контрактів ще порожній. Керівництво має додати їх через `/contracts_admin`.",
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
    label="Кілька виконавців",
    style=discord.ButtonStyle.primary,
    emoji="👥",
    custom_id="contract_v34:group",
  )
  async def group_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not db.active_contract_types(limit=1):
      await interaction.response.send_message(
        "❌ Перелік контрактів ще порожній. Керівництво має додати їх через `/contracts_admin`.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      "👥 Оберіть усіх виконавців контракту:",
      view=PerformerStepView(self.bot_instance, show_self_button=False),
      ephemeral=True,
    )

  @discord.ui.button(
    label="Рейтинг",
    style=discord.ButtonStyle.secondary,
    emoji="🏆",
    custom_id="contract_v34:rating",
  )
  async def rating(self, interaction: discord.Interaction, button: discord.ui.Button):
    if interaction.guild is None:
      await interaction.response.send_message(
        "❌ Це працює тільки на сервері.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      embed=build_public_rating_embed(interaction.guild.id),
      ephemeral=True,
    )



  @discord.ui.button(
    label="Моя статистика",
    style=discord.ButtonStyle.secondary,
    emoji="👤",
    custom_id="contract_v4:mystats",
  )
  async def my_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
    if interaction.guild is None:
      await interaction.response.send_message(
        "❌ Це працює тільки на сервері.",
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
# Bot + commands
# ----------------------------

class ContractBot(commands.Bot):
  def __init__(self):
    intents = discord.Intents.default()
    super().__init__(command_prefix="!", intents=intents)
    self._unpaid_refreshed = False

  async def setup_hook(self):
    self.add_view(MainContractPanelView(self))
    self.add_view(UnpaidCompletedView(self))

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


@bot.tree.command(name="setup", description="Створити панель контрактів")
async def setup_panel(interaction: discord.Interaction):
  if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
    await interaction.response.send_message(
      "❌ Ця команда тільки для керівництва.",
      ephemeral=True,
    )
    return

  guild = interaction.guild
  if guild is None:
    await interaction.response.send_message(
      "❌ Це працює тільки на сервері.",
      ephemeral=True,
    )
    return

  channel = await get_target_channel(guild, interaction.channel_id)
  if not isinstance(channel, discord.TextChannel):
    await interaction.response.send_message(
      "❌ Панель треба створювати у звичайному текстовому каналі.",
      ephemeral=True,
    )
    return

  await interaction.response.defer(ephemeral=True)

  panel = await move_main_panel_to_bottom(guild, channel)

  if panel is None:
    await interaction.followup.send(
      "❌ Не вдалося створити панель у каналі.",
      ephemeral=True,
    )
    return

  log_note = (
    ""
    if LOG_CHANNEL_ID
    else "\n⚠️ LOG_CHANNEL_ID не задано — журнал дій поки вимкнений."
  )

  await interaction.followup.send(
    (
      f"✅ Панель готова: {panel.jump_url}\n"
      "Її більше не треба шукати в закріплених — після кожного нового контракту "
      "бот автоматично переносить панель у самий низ каналу."
      f"{log_note}"
    ),
    ephemeral=True,
  )




@bot.tree.command(name="contracts_admin", description="Керування переліком контрактів")
async def contracts_admin(interaction: discord.Interaction):
  if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
    await interaction.response.send_message(
      "❌ Ця команда тільки для керівництва.",
      ephemeral=True,
    )
    return

  embed = discord.Embed(
    title="⚙️ Керування контрактами",
    description=(
      "Тут керівництво створює та редагує перелік контрактів.\n"
      "Для кожного контракту зберігаються **назва, ціна та КД**.\n"
      "Рейтинг і статистика заробітку обнуляються **окремо**."
    ),
    color=discord.Color.blurple(),
  )
  await interaction.response.send_message(
    embed=embed,
    view=ContractAdminPanelView(),
    ephemeral=True,
  )




@bot.tree.command(
  name="annul",
  description="Анулювати вже оплачений контракт за його ID",
)
@app_commands.describe(
  contract_id="ID контракту, вказаний внизу його картки",
)
async def annul_contract(
  interaction: discord.Interaction,
  contract_id: int,
):
  if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
    await interaction.response.send_message(
      "❌ Анулювати оплачений контракт може тільки керівництво.",
      ephemeral=True,
    )
    return

  guild = interaction.guild
  if guild is None:
    await interaction.response.send_message(
      "❌ Це працює тільки на сервері.",
      ephemeral=True,
    )
    return

  row = db.get_completed_by_id(contract_id)

  if not row or row["guild_id"] != guild.id:
    await interaction.response.send_message(
      f"❌ Контракт з ID **{contract_id}** не знайдено на цьому сервері.",
      ephemeral=True,
    )
    return

  if row["status"] == "annulled":
    await interaction.response.send_message(
      f"ℹ️ Контракт **#{contract_id}** уже анульований.",
      ephemeral=True,
    )
    return

  if row["status"] != "paid":
    status_names = {
      "unpaid": "не оплачений",
      "cancelled": "скасований",
    }
    status_name = status_names.get(row["status"], row["status"])
    await interaction.response.send_message(
      (
        f"❌ Контракт **#{contract_id}** зараз **{status_name}**.\n"
        "Анулювати цією командою можна тільки вже оплачений контракт."
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
    title=f"🚫 Анулювання контракту #{contract_id}",
    description=(
      "Перевір контракт перед підтвердженням.\n\n"
      f"📋 **{row['contract_name']}**\n"
      f"💰 Сума: **{format_money_dollars(row['price'])} $**\n"
      f"👥 Виконавці: {participant_text}\n"
      f"🏦 Було в Банк сім'ї: **{format_cents(row['fomo_cents'] or 0)}**\n"
      f"💸 Було учасникам: **{format_cents(row['net_cents'] or 0)}**\n\n"
      f"[Відкрити повідомлення контракту]({jump_url})"
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


@bot.tree.command(name="stats", description="Статистика контрактів для керівництва")
async def stats(interaction: discord.Interaction):
  if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
    await interaction.response.send_message(
      "❌ Статистика доступна тільки керівництву.",
      ephemeral=True,
    )
    return

  guild = interaction.guild
  if guild is None:
    await interaction.response.send_message(
      "❌ Це працює тільки на сервері.",
      ephemeral=True,
    )
    return

  await interaction.response.send_message(
    embed=build_admin_general_stats_embed(guild.id),
    view=AdminStatsView(guild.id, "general"),
    ephemeral=True,
  )


@bot.tree.command(name="unpaid", description="Неоплачені контракти")
async def unpaid(interaction: discord.Interaction):
  if not isinstance(interaction.user, discord.Member) or not management_member(interaction.user):
    await interaction.response.send_message(
      "❌ Доступно тільки керівництву.",
      ephemeral=True,
    )
    return

  guild = interaction.guild
  if guild is None:
    await interaction.response.send_message("❌ Це працює тільки на сервері.", ephemeral=True)
    return

  rows = db.unpaid_for_guild(guild.id)
  if not rows:
    await interaction.response.send_message("✅ Неоплачених контрактів немає.", ephemeral=True)
    return

  lines = []
  for row in rows:
    participants = " ".join(f"<@{uid}>" for uid in parse_ids(row["participant_ids"]))
    jump_url = f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}"
    lines.append(
      f"• **{row['contract_name']}** — {format_money_dollars(row['price'])} $ — "
      f"{participants} — [відкрити]({jump_url})"
    )

  embed = discord.Embed(
    title="💸 Неоплачені контракти",
    description="\n".join(lines),
    color=discord.Color.orange(),
  )
  await interaction.response.send_message(embed=embed, ephemeral=True)


bot.run(TOKEN)
