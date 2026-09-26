import json
import os
import sqlite3
from typing import Optional

from utils import (
  PAYMENT_MODE_NORMAL,
  calculate_payment,
  calculate_personal_family_contributions,
  parse_ids,
  utc_now_iso,
)


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
    CREATE TABLE IF NOT EXISTS bonus_periods (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      start_at TEXT NOT NULL,
      end_at TEXT NOT NULL,
      family_earned_cents INTEGER NOT NULL DEFAULT 0,
      threshold_level INTEGER NOT NULL DEFAULT 0,
      prize_pool_cents INTEGER NOT NULL DEFAULT 0,
      thresholds_json TEXT NOT NULL,
      distribution_json TEXT NOT NULL,
      ranking_json TEXT NOT NULL,
      closed_by INTEGER,
      close_mode TEXT NOT NULL DEFAULT 'manual',
      created_at TEXT NOT NULL
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS bonus_awards (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      period_id INTEGER,
      user_id INTEGER NOT NULL,
      amount_cents INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      source TEXT NOT NULL DEFAULT 'auto',
      rank INTEGER,
      points_text TEXT,
      note TEXT,
      created_at TEXT NOT NULL,
      paid_at TEXT,
      paid_by INTEGER
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
    CREATE TABLE IF NOT EXISTS member_resets (
      guild_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      rating_reset_at TEXT,
      earnings_reset_at TEXT,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (guild_id, user_id)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS storage_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      name TEXT COLLATE NOCASE NOT NULL,
      quantity INTEGER NOT NULL DEFAULT 0,
      unit TEXT NOT NULL DEFAULT '\u0448\u0442.',
      active INTEGER NOT NULL DEFAULT 1,
      created_by INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(guild_id, name)
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS storage_movements (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      item_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      movement_type TEXT NOT NULL,
      quantity INTEGER NOT NULL,
      before_quantity INTEGER NOT NULL,
      after_quantity INTEGER NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL
    )
    """)

    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS lotteries (
      id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER, creator_id INTEGER NOT NULL,
      name TEXT NOT NULL, prize_type TEXT NOT NULL, prize_cents INTEGER NOT NULL DEFAULT 0, prize_description TEXT, ticket_price_cents INTEGER NOT NULL,
      total_tickets INTEGER NOT NULL, ticket_limit_per_user INTEGER NOT NULL DEFAULT 0, winner_count INTEGER NOT NULL DEFAULT 1, ends_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, drawn_at TEXT
    )
    """)
    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS lottery_tickets (
      id INTEGER PRIMARY KEY AUTOINCREMENT, lottery_id INTEGER NOT NULL, number INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'reserved',
      reserved_at TEXT NOT NULL, confirmed_at TEXT, verified_by INTEGER, UNIQUE(lottery_id, number)
    )
    """)
    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS lottery_requests (
      id INTEGER PRIMARY KEY AUTOINCREMENT, lottery_id INTEGER NOT NULL, user_id INTEGER NOT NULL, numbers_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'reserved',
      created_at TEXT NOT NULL, verified_at TEXT, verified_by INTEGER, note TEXT
    )
    """)
    self.conn.execute("""
    CREATE TABLE IF NOT EXISTS lottery_payouts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, lottery_id INTEGER NOT NULL, ticket_number INTEGER NOT NULL,
      amount_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, paid_at TEXT, paid_by INTEGER
    )
    """)
    for column, definition in (("cancelled_at", "TEXT"), ("cancelled_by", "INTEGER")):
      if column not in {row["name"] for row in self.conn.execute("PRAGMA table_info(lottery_payouts)")}:
        self.conn.execute(f"ALTER TABLE lottery_payouts ADD COLUMN {column} {definition}")

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
      """
      CREATE INDEX IF NOT EXISTS idx_bonus_periods_guild_end
      ON bonus_periods(guild_id, end_at)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_bonus_awards_guild_user_status
      ON bonus_awards(guild_id, user_id, status)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_bonus_awards_period
      ON bonus_awards(period_id)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_member_resets_guild_user
      ON member_resets(guild_id, user_id)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_storage_items_guild_active
      ON storage_items(guild_id, active, name)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_storage_movements_guild_created
      ON storage_movements(guild_id, created_at)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_storage_movements_item_created
      ON storage_movements(item_id, created_at)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_lotteries_guild_status ON lotteries(guild_id, status, ends_at)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_lottery_tickets_lottery_status ON lottery_tickets(lottery_id, status)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_lottery_tickets_user ON lottery_tickets(lottery_id, user_id, status)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_lottery_requests_status ON lottery_requests(lottery_id, status)
      """,
      """
      CREATE INDEX IF NOT EXISTS idx_lottery_payouts_user_status ON lottery_payouts(guild_id, user_id, status)
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

    if version < 5:
      self._create_performance_indexes()
      self._set_schema_version(5)
      version = 5

    if version < 6:
      self._create_performance_indexes()
      self._set_schema_version(6)
      version = 6

    if version < 7:
      self._create_performance_indexes()
      self._set_schema_version(7)
      version = 7

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
    Старі оплачені контракти вже мають payment_mode / excluded ids,
    тому особистий внесок у Банк сім'ї можна відновити задним числом.
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


  # ----------------------------
  # Bonus / premium system
  # ----------------------------

  def contracts_for_bonus_period(
    self,
    guild_id: int,
    start_at: str,
    end_at: str,
  ):
    return self.conn.execute("""
    SELECT *
    FROM contracts
    WHERE guild_id = ?
     AND status NOT IN ('cancelled', 'annulled')
     AND created_at >= ?
     AND created_at < ?
    ORDER BY id ASC
    """, (guild_id, start_at, end_at)).fetchall()

  def family_fund_earned_between(
    self,
    guild_id: int,
    start_at: str,
    end_at: str,
  ) -> int:
    row = self.conn.execute("""
    SELECT COALESCE(SUM(fomo_cents), 0) AS total
    FROM contracts
    WHERE guild_id = ?
     AND status = 'paid'
     AND paid_at IS NOT NULL
     AND paid_at >= ?
     AND paid_at < ?
    """, (guild_id, start_at, end_at)).fetchone()
    return int(row["total"] or 0)

  def create_bonus_period_and_awards(
    self,
    guild_id: int,
    start_at: str,
    end_at: str,
    family_earned_cents: int,
    threshold_level: int,
    prize_pool_cents: int,
    thresholds_json: str,
    distribution_json: str,
    ranking_json: str,
    closed_by: Optional[int],
    close_mode: str,
    awards: list[tuple[int, int, int, str]],
  ) -> int:
    now = utc_now_iso()

    try:
      self.conn.execute("BEGIN IMMEDIATE")

      cur = self.conn.execute("""
      INSERT INTO bonus_periods (
        guild_id, start_at, end_at, family_earned_cents,
        threshold_level, prize_pool_cents, thresholds_json,
        distribution_json, ranking_json, closed_by,
        close_mode, created_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """, (
        guild_id,
        start_at,
        end_at,
        family_earned_cents,
        threshold_level,
        prize_pool_cents,
        thresholds_json,
        distribution_json,
        ranking_json,
        closed_by,
        close_mode,
        now,
      ))

      period_id = cur.lastrowid

      for user_id, amount_cents, rank, points_text in awards:
        if amount_cents <= 0:
          continue
        self.conn.execute("""
        INSERT INTO bonus_awards (
          guild_id, period_id, user_id, amount_cents,
          status, source, rank, points_text, created_at
        )
        VALUES (?, ?, ?, ?, 'pending', 'auto', ?, ?, ?)
        """, (
          guild_id,
          period_id,
          user_id,
          amount_cents,
          rank,
          points_text,
          now,
        ))

      for key in (
        "bonus_period_start_at",
        "rating_reset_at",
      ):
        self.conn.execute("""
        INSERT INTO bot_settings (guild_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, key)
        DO UPDATE SET value = excluded.value
        """, (
          guild_id,
          key,
          end_at,
        ))

      self.conn.commit()
      return period_id
    except Exception:
      self.conn.rollback()
      raise

  def add_manual_bonus(
    self,
    guild_id: int,
    user_id: int,
    amount_cents: int,
    status: str,
    note: Optional[str],
    created_by: int,
  ) -> int:
    normalized_status = "paid" if status == "paid" else "pending"
    now = utc_now_iso()
    paid_at = now if normalized_status == "paid" else None
    paid_by = created_by if normalized_status == "paid" else None

    cur = self.conn.execute("""
    INSERT INTO bonus_awards (
      guild_id, period_id, user_id, amount_cents,
      status, source, rank, points_text, note,
      created_at, paid_at, paid_by
    )
    VALUES (?, NULL, ?, ?, ?, 'manual', NULL, NULL, ?, ?, ?, ?)
    """, (
      guild_id,
      user_id,
      amount_cents,
      normalized_status,
      (note.strip() if note and note.strip() else None),
      now,
      paid_at,
      paid_by,
    ))
    self.conn.commit()
    return cur.lastrowid

  def bonus_periods_for_guild(self, guild_id: int, limit: int = 10):
    return self.conn.execute("""
    SELECT *
    FROM bonus_periods
    WHERE guild_id = ?
    ORDER BY id DESC
    LIMIT ?
    """, (guild_id, limit)).fetchall()

  def bonus_awards_for_period(self, period_id: int):
    return self.conn.execute("""
    SELECT *
    FROM bonus_awards
    WHERE period_id = ?
    ORDER BY COALESCE(rank, 999) ASC, id ASC
    """, (period_id,)).fetchall()

  def manual_bonus_history(self, guild_id: int, limit: int = 10):
    return self.conn.execute("""
    SELECT *
    FROM bonus_awards
    WHERE guild_id = ?
     AND source = 'manual'
    ORDER BY id DESC
    LIMIT ?
    """, (guild_id, limit)).fetchall()

  def pending_payout_summary(self, guild_id: int):
    return self.conn.execute("""
    SELECT
      user_id,
      COUNT(*) AS accrual_count,
      SUM(amount_cents) AS total_cents
    FROM (
      SELECT
        a.user_id AS user_id,
        a.amount_cents AS amount_cents
      FROM payment_accruals a
      JOIN contracts c ON c.id = a.contract_id
      WHERE c.guild_id = ?
       AND c.status = 'paid'
       AND a.status = 'pending'

      UNION ALL

      SELECT
        b.user_id AS user_id,
        b.amount_cents AS amount_cents
      FROM bonus_awards b
      WHERE b.guild_id = ?
       AND b.status = 'pending'

      UNION ALL

      SELECT l.user_id AS user_id, l.amount_cents AS amount_cents
      FROM lottery_payouts l
      WHERE l.guild_id = ? AND l.status = 'pending'
    )
    GROUP BY user_id
    ORDER BY total_cents DESC, user_id ASC
    """, (guild_id, guild_id, guild_id)).fetchall()

  def pending_payout_items_for_user(self, guild_id: int, user_id: int):
    return self.conn.execute("""
    SELECT
      'contract' AS source_type,
      a.id AS item_id,
      a.user_id AS user_id,
      a.amount_cents AS amount_cents,
      a.created_at AS created_at,
      a.contract_id AS contract_id,
      c.message_id AS message_id,
      c.channel_id AS channel_id,
      c.contract_name AS contract_name,
      NULL AS bonus_id,
      NULL AS period_id,
      NULL AS bonus_source,
      NULL AS rank,
      NULL AS points_text,
      NULL AS note,
      NULL AS period_start, NULL AS period_end, NULL AS lottery_id, NULL AS ticket_number
    FROM payment_accruals a
    JOIN contracts c ON c.id = a.contract_id
    WHERE c.guild_id = ?
     AND c.status = 'paid'
     AND a.user_id = ?
     AND a.status = 'pending'

    UNION ALL

    SELECT
      'bonus' AS source_type,
      b.id AS item_id,
      b.user_id AS user_id,
      b.amount_cents AS amount_cents,
      b.created_at AS created_at,
      NULL AS contract_id,
      NULL AS message_id,
      NULL AS channel_id,
      NULL AS contract_name,
      b.id AS bonus_id,
      b.period_id AS period_id,
      b.source AS bonus_source,
      b.rank AS rank,
      b.points_text AS points_text,
      b.note AS note,
      p.start_at AS period_start, p.end_at AS period_end, NULL AS lottery_id, NULL AS ticket_number
    FROM bonus_awards b
    LEFT JOIN bonus_periods p ON p.id = b.period_id
    WHERE b.guild_id = ?
     AND b.user_id = ?
     AND b.status = 'pending'

    UNION ALL

    SELECT 'lottery' AS source_type, l.id AS item_id, l.user_id, l.amount_cents, l.created_at, NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,l.lottery_id,l.ticket_number
    FROM lottery_payouts l WHERE l.guild_id=? AND l.user_id=? AND l.status='pending'

    ORDER BY 5 ASC
    """, (guild_id, user_id, guild_id, user_id, guild_id, user_id)).fetchall()

  def settle_payouts_for_users(
    self,
    guild_id: int,
    user_ids: list[int],
    paid_by: int,
  ):
    normalized = sorted({int(uid) for uid in user_ids})
    if not normalized:
      return []

    rows = []
    for uid in normalized:
      rows.extend(self.pending_payout_items_for_user(guild_id, uid))

    if not rows:
      return []

    contract_ids = [
      int(row["item_id"])
      for row in rows
      if row["source_type"] == "contract"
    ]
    bonus_ids = [
      int(row["item_id"])
      for row in rows
      if row["source_type"] == "bonus"
    ]
    lottery_ids = [int(row["item_id"]) for row in rows if row["source_type"] == "lottery"]

    now = utc_now_iso()

    try:
      self.conn.execute("BEGIN IMMEDIATE")

      if contract_ids:
        placeholders = ",".join("?" for _ in contract_ids)
        self.conn.execute(
          f"""
          UPDATE payment_accruals
          SET status = 'paid',
            paid_at = ?,
            paid_by = ?
          WHERE id IN ({placeholders})
           AND status = 'pending'
          """,
          (now, paid_by, *contract_ids),
        )

      if bonus_ids:
        placeholders = ",".join("?" for _ in bonus_ids)
        self.conn.execute(
          f"""
          UPDATE bonus_awards
          SET status = 'paid',
            paid_at = ?,
            paid_by = ?
          WHERE id IN ({placeholders})
           AND status = 'pending'
          """,
          (now, paid_by, *bonus_ids),
        )



      if lottery_ids:
        placeholders = ",".join("?" for _ in lottery_ids)
        self.conn.execute(f"UPDATE lottery_payouts SET status='paid', paid_at=?, paid_by=? WHERE id IN ({placeholders}) AND status='pending'", (now, paid_by, *lottery_ids))

      self.conn.commit()
      return rows
    except Exception:
      self.conn.rollback()
      raise

  def settle_payouts_for_user(
    self,
    guild_id: int,
    user_id: int,
    paid_by: int,
  ):
    return self.settle_payouts_for_users(
      guild_id,
      [user_id],
      paid_by,
    )

  def cancel_lottery_payout(self, guild_id: int, payout_id: int, cancelled_by: int):
    """Cancel a pending prize or reverse a marked-paid prize in the bot ledger."""
    try:
      self.conn.execute("BEGIN IMMEDIATE")
      row = self.conn.execute(
        "SELECT * FROM lottery_payouts WHERE guild_id=? AND id=? AND status IN ('pending','paid')",
        (guild_id, payout_id),
      ).fetchone()
      if row:
        new_status = "cancelled" if row["status"] == "pending" else "reversed"
        self.conn.execute(
          "UPDATE lottery_payouts SET status=?, cancelled_at=?, cancelled_by=? "
          "WHERE guild_id=? AND id=? AND status=?",
          (new_status, utc_now_iso(), cancelled_by, guild_id, payout_id, row["status"]),
        )
      self.conn.commit()
      return row
    except Exception:
      self.conn.rollback()
      raise


  def birthdays_for_guild(self, guild_id: int):
    return self.conn.execute("""
    SELECT *
    FROM birthdays
    WHERE guild_id = ?
    ORDER BY month ASC, day ASC, user_id ASC
    """, (guild_id,)).fetchall()



  # ----------------------------
  # Individual member resets
  # ----------------------------

  def get_member_resets(self, guild_id: int, user_id: int):
    return self.conn.execute("""
    SELECT rating_reset_at, earnings_reset_at
    FROM member_resets
    WHERE guild_id = ? AND user_id = ?
    """, (guild_id, user_id)).fetchone()

  def get_member_reset(
    self,
    guild_id: int,
    user_id: int,
    reset_type: str,
  ) -> Optional[str]:
    if reset_type not in ("rating", "earnings"):
      raise ValueError("Unknown member reset type")

    row = self.get_member_resets(guild_id, user_id)
    if not row:
      return None

    column = (
      "rating_reset_at"
      if reset_type == "rating"
      else "earnings_reset_at"
    )
    return row[column]

  def set_member_reset(
    self,
    guild_id: int,
    user_id: int,
    reset_type: str,
    reset_at: str,
  ):
    if reset_type not in ("rating", "earnings"):
      raise ValueError("Unknown member reset type")

    now = utc_now_iso()

    if reset_type == "rating":
      self.conn.execute("""
      INSERT INTO member_resets (
        guild_id, user_id, rating_reset_at, earnings_reset_at, updated_at
      )
      VALUES (?, ?, ?, NULL, ?)
      ON CONFLICT(guild_id, user_id)
      DO UPDATE SET
        rating_reset_at = excluded.rating_reset_at,
        updated_at = excluded.updated_at
      """, (guild_id, user_id, reset_at, now))
    else:
      self.conn.execute("""
      INSERT INTO member_resets (
        guild_id, user_id, rating_reset_at, earnings_reset_at, updated_at
      )
      VALUES (?, ?, NULL, ?, ?)
      ON CONFLICT(guild_id, user_id)
      DO UPDATE SET
        earnings_reset_at = excluded.earnings_reset_at,
        updated_at = excluded.updated_at
      """, (guild_id, user_id, reset_at, now))

    self.conn.commit()

  # ----------------------------
  # Family item storage
  # ----------------------------

  def storage_items_for_guild(
    self,
    guild_id: int,
    active_only: bool = True,
  ):
    sql = """
    SELECT *
    FROM storage_items
    WHERE guild_id = ?
    """
    params = [guild_id]

    if active_only:
      sql += " AND active = 1"

    sql += " ORDER BY name COLLATE NOCASE ASC, id ASC"
    return self.conn.execute(sql, params).fetchall()

  def get_storage_item(
    self,
    guild_id: int,
    item_id: int,
    active_only: bool = True,
  ):
    sql = """
    SELECT *
    FROM storage_items
    WHERE guild_id = ? AND id = ?
    """
    params = [guild_id, item_id]

    if active_only:
      sql += " AND active = 1"

    return self.conn.execute(sql, params).fetchone()

  def search_storage_items(
    self,
    guild_id: int,
    query: str,
    limit: int = 25,
  ):
    needle = query.strip().casefold()
    if not needle:
      return []

    rows = self.storage_items_for_guild(
      guild_id,
      active_only=True,
    )

    matched = [
      row for row in rows
      if needle in row["name"].casefold()
    ]

    matched.sort(
      key=lambda row: (
        0 if row["name"].casefold().startswith(needle) else 1,
        row["name"].casefold(),
        row["id"],
      )
    )
    return matched[:limit]

  def create_storage_item(
    self,
    guild_id: int,
    name: str,
    quantity: int,
    unit: str,
    created_by: int,
  ) -> int:
    clean_name = " ".join(name.strip().split())
    clean_unit = unit.strip() or "шт."

    if not clean_name:
      raise ValueError("Назва предмета порожня.")
    if quantity < 0:
      raise ValueError("Початкова кількість не може бути від'ємною.")

    existing = next(
      (
        row
        for row in self.storage_items_for_guild(
          guild_id,
          active_only=False,
        )
        if row["name"].casefold() == clean_name.casefold()
      ),
      None,
    )

    now = utc_now_iso()

    try:
      self.conn.execute("BEGIN IMMEDIATE")

      if existing:
        if existing["active"]:
          raise ValueError("Такий предмет уже є на складі.")

        self.conn.execute("""
        UPDATE storage_items
        SET active = 1,
          quantity = ?,
          unit = ?,
          created_by = ?,
          updated_at = ?
        WHERE id = ?
        """, (
          quantity,
          clean_unit,
          created_by,
          now,
          existing["id"],
        ))
        item_id = int(existing["id"])
      else:
        cur = self.conn.execute("""
        INSERT INTO storage_items (
          guild_id, name, quantity, unit, active,
          created_by, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """, (
          guild_id,
          clean_name,
          quantity,
          clean_unit,
          created_by,
          now,
          now,
        ))
        item_id = int(cur.lastrowid)

      if quantity > 0:
        self.conn.execute("""
        INSERT INTO storage_movements (
          guild_id, item_id, user_id, movement_type,
          quantity, before_quantity, after_quantity,
          note, created_at
        )
        VALUES (?, ?, ?, 'ADD', ?, 0, ?, ?, ?)
        """, (
          guild_id,
          item_id,
          created_by,
          quantity,
          quantity,
          "Початковий залишок",
          now,
        ))

      self.conn.commit()
      return item_id
    except Exception:
      self.conn.rollback()
      raise

  def rename_storage_item(
    self,
    guild_id: int,
    item_id: int,
    new_name: str,
  ):
    clean_name = " ".join(new_name.strip().split())
    if not clean_name:
      raise ValueError("Назва предмета порожня.")

    duplicate = next(
      (
        row
        for row in self.storage_items_for_guild(
          guild_id,
          active_only=False,
        )
        if row["id"] != item_id
        and row["name"].casefold() == clean_name.casefold()
      ),
      None,
    )

    if duplicate:
      raise ValueError("Предмет з такою назвою вже існує.")

    cur = self.conn.execute("""
    UPDATE storage_items
    SET name = ?, updated_at = ?
    WHERE guild_id = ? AND id = ? AND active = 1
    """, (
      clean_name,
      utc_now_iso(),
      guild_id,
      item_id,
    ))

    if cur.rowcount == 0:
      raise ValueError("Предмет не знайдено.")

    self.conn.commit()

  def archive_storage_item(
    self,
    guild_id: int,
    item_id: int,
  ):
    cur = self.conn.execute("""
    UPDATE storage_items
    SET active = 0, updated_at = ?
    WHERE guild_id = ? AND id = ? AND active = 1
    """, (
      utc_now_iso(),
      guild_id,
      item_id,
    ))

    if cur.rowcount == 0:
      raise ValueError("Предмет не знайдено.")

    self.conn.commit()

  def change_storage_quantity(
    self,
    guild_id: int,
    item_id: int,
    user_id: int,
    movement_type: str,
    quantity: int,
    note: Optional[str] = None,
  ):
    if movement_type not in ("ADD", "TAKE"):
      raise ValueError("Невідомий тип операції.")
    if quantity <= 0:
      raise ValueError("Кількість має бути більшою за 0.")

    try:
      self.conn.execute("BEGIN IMMEDIATE")

      row = self.conn.execute("""
      SELECT *
      FROM storage_items
      WHERE guild_id = ? AND id = ? AND active = 1
      """, (guild_id, item_id)).fetchone()

      if not row:
        raise ValueError("Предмет не знайдено.")

      before = int(row["quantity"])

      if movement_type == "TAKE":
        if quantity > before:
          raise ValueError(
            f"На складі лише {before} {row['unit']}."
          )
        after = before - quantity
      else:
        after = before + quantity

      now = utc_now_iso()

      self.conn.execute("""
      UPDATE storage_items
      SET quantity = ?, updated_at = ?
      WHERE id = ?
      """, (after, now, item_id))

      self.conn.execute("""
      INSERT INTO storage_movements (
        guild_id, item_id, user_id, movement_type,
        quantity, before_quantity, after_quantity,
        note, created_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      """, (
        guild_id,
        item_id,
        user_id,
        movement_type,
        quantity,
        before,
        after,
        (note.strip() if note and note.strip() else None),
        now,
      ))

      self.conn.commit()
      return {
        "before": before,
        "after": after,
        "item_name": row["name"],
        "unit": row["unit"],
      }
    except Exception:
      self.conn.rollback()
      raise

  def latest_storage_movement(
    self,
    guild_id: int,
    item_id: Optional[int] = None,
  ):
    sql = """
    SELECT
      sm.*,
      si.name AS item_name,
      si.unit AS item_unit
    FROM storage_movements sm
    JOIN storage_items si ON si.id = sm.item_id
    WHERE sm.guild_id = ?
    """
    params = [guild_id]

    if item_id is not None:
      sql += " AND sm.item_id = ?"
      params.append(item_id)

    sql += " ORDER BY sm.id DESC LIMIT 1"
    return self.conn.execute(sql, params).fetchone()

  def storage_movements(
    self,
    guild_id: int,
    item_id: Optional[int] = None,
    limit: int = 20,
  ):
    sql = """
    SELECT
      sm.*,
      si.name AS item_name,
      si.unit AS item_unit
    FROM storage_movements sm
    JOIN storage_items si ON si.id = sm.item_id
    WHERE sm.guild_id = ?
    """
    params = [guild_id]

    if item_id is not None:
      sql += " AND sm.item_id = ?"
      params.append(item_id)

    sql += " ORDER BY sm.id DESC LIMIT ?"
    params.append(limit)
    return self.conn.execute(sql, params).fetchall()

  def storage_summary(self, guild_id: int):
    count_row = self.conn.execute("""
    SELECT COUNT(*) AS cnt
    FROM storage_items
    WHERE guild_id = ? AND active = 1
    """, (guild_id,)).fetchone()

    return {
      "positions": int(count_row["cnt"] or 0),
      "latest": self.latest_storage_movement(guild_id),
    }


  def create_lottery(self, guild_id, channel_id, creator_id, name, prize_type, prize_cents, prize_description, ticket_price_cents, total_tickets, ticket_limit_per_user, winner_count, ends_at):
    cur=self.conn.execute("INSERT INTO lotteries (guild_id,channel_id,creator_id,name,prize_type,prize_cents,prize_description,ticket_price_cents,total_tickets,ticket_limit_per_user,winner_count,ends_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (guild_id,channel_id,creator_id,name,prize_type,prize_cents,prize_description,ticket_price_cents,total_tickets,ticket_limit_per_user,winner_count,ends_at,utc_now_iso())); self.conn.commit(); return cur.lastrowid
  def set_lottery_message(self, lottery_id, message_id): self.conn.execute("UPDATE lotteries SET message_id=? WHERE id=?",(message_id,lottery_id)); self.conn.commit()
  def lottery_request(self, request_id): return self.conn.execute("SELECT r.*,l.name AS lottery_name,l.ticket_price_cents,l.guild_id,l.total_tickets,l.status AS lottery_status FROM lottery_requests r JOIN lotteries l ON l.id=r.lottery_id WHERE r.id=?",(request_id,)).fetchone()
  def reserve_lottery_tickets(self, lottery_id, user_id, numbers):
    numbers=sorted(set(int(n) for n in numbers)); row=self.conn.execute("SELECT * FROM lotteries WHERE id=?",(lottery_id,)).fetchone()
    if not row or row['status']!='active': return False,None,'Продаж лотереї вже завершено.'
    now=utc_now_iso()
    try:
      self.conn.execute('BEGIN IMMEDIATE'); ph=','.join('?' for _ in numbers)
      # Перевіряємо тільки АКТИВНІ квитки (reserved/confirmed).
      # Раніше тут не було фільтра по статусу, тому вже
      # звільнені (released) номери після скасування помилково
      # вважалися зайнятими при повторному виборі.
      existing=self.conn.execute(f"SELECT number FROM lottery_tickets WHERE lottery_id=? AND number IN ({ph}) AND status IN ('reserved','confirmed')",(lottery_id,*numbers)).fetchall()
      if existing: self.conn.rollback(); return False,None,'Один або кілька вибраних номерів уже зайняті.'
      current=self.conn.execute("SELECT COUNT(*) c FROM lottery_tickets WHERE lottery_id=? AND status IN ('reserved','confirmed')",(lottery_id,)).fetchone()['c']
      if current+len(numbers)>row['total_tickets']: self.conn.rollback(); return False,None,'Вільних квитків недостатньо.'
      user_count=self.conn.execute("SELECT COUNT(*) c FROM lottery_tickets WHERE lottery_id=? AND user_id=? AND status IN ('reserved','confirmed')",(lottery_id,user_id)).fetchone()['c']
      if row['ticket_limit_per_user'] and user_count+len(numbers)>row['ticket_limit_per_user']: self.conn.rollback(); return False,None,f"Твій ліміт — {row['ticket_limit_per_user']} квитків."
      cur=self.conn.execute("INSERT INTO lottery_requests (lottery_id,user_id,numbers_json,status,created_at) VALUES (?,?,?,?,?)",(lottery_id,user_id,json.dumps(numbers),'reserved',now)); rid=cur.lastrowid
      for n in numbers:
        # Номер міг раніше вже існувати в lottery_tickets
        # (released/rejected) — через UNIQUE(lottery_id, number)
        # звичайний INSERT впав би з IntegrityError, тому
        # перевикористовуємо той самий рядок через upsert.
        self.conn.execute(
          """
          INSERT INTO lottery_tickets (lottery_id, number, user_id, status, reserved_at)
          VALUES (?, ?, ?, 'reserved', ?)
          ON CONFLICT(lottery_id, number)
          DO UPDATE SET
            user_id = excluded.user_id,
            status = 'reserved',
            reserved_at = excluded.reserved_at,
            confirmed_at = NULL,
            verified_by = NULL
          """,
          (lottery_id, n, user_id, now),
        )
      self.conn.commit(); return True,rid,''
    except Exception: self.conn.rollback(); raise
  def mark_lottery_payment_pending(self, request_id): self.conn.execute("UPDATE lottery_requests SET status='payment_pending' WHERE id=? AND status='reserved'",(request_id,)); self.conn.commit()
  def confirm_lottery_request(self, request_id, verified_by):
    req=self.lottery_request(request_id)
    if not req: return False
    nums=json.loads(req['numbers_json']); ph=','.join('?' for _ in nums); now=utc_now_iso()
    self.conn.execute("UPDATE lottery_requests SET status='confirmed',verified_at=?,verified_by=? WHERE id=? AND status='payment_pending'",(now,verified_by,request_id))
    self.conn.execute(f"UPDATE lottery_tickets SET status='confirmed',confirmed_at=?,verified_by=? WHERE lottery_id=? AND user_id=? AND number IN ({ph}) AND status='reserved'",(now,verified_by,req['lottery_id'],req['user_id'],*nums)); self.conn.commit(); return True
  def reject_lottery_request(self, request_id, verified_by, note):
    req=self.lottery_request(request_id)
    if not req: return False
    nums=json.loads(req['numbers_json']); ph=','.join('?' for _ in nums); now=utc_now_iso()
    self.conn.execute("UPDATE lottery_requests SET status='rejected',verified_at=?,verified_by=?,note=? WHERE id=? AND status IN ('reserved','payment_pending')",(now,verified_by,note,request_id))
    self.conn.execute(f"UPDATE lottery_tickets SET status='released' WHERE lottery_id=? AND user_id=? AND number IN ({ph}) AND status='reserved'",(req['lottery_id'],req['user_id'],*nums)); self.conn.commit(); return True
  def pending_lottery_requests(self,guild_id): return self.conn.execute("SELECT r.*,l.name lottery_name,l.ticket_price_cents FROM lottery_requests r JOIN lotteries l ON l.id=r.lottery_id WHERE l.guild_id=? AND r.status='payment_pending' ORDER BY r.created_at",(guild_id,)).fetchall()
  def active_lotteries(self,guild_id): return self.conn.execute("SELECT * FROM lotteries WHERE guild_id=? AND status='active' ORDER BY id DESC",(guild_id,)).fetchall()
  def draw_lottery(self,lottery_id,drawn_by):
    row=self.conn.execute("SELECT * FROM lotteries WHERE id=?",(lottery_id,)).fetchone()
    if not row or row['status'] not in ('active','finished'): return False,'Лотерея вже завершена.',[]
    tickets=self.conn.execute("SELECT number FROM lottery_tickets WHERE lottery_id=? AND status='confirmed'",(lottery_id,)).fetchall()
    if len(tickets)<int(row['winner_count']): return False,'Недостатньо підтверджених квитків для всіх переможців.',[]
    winners=__import__('random').sample([int(r['number']) for r in tickets],int(row['winner_count'])); self.conn.execute("UPDATE lotteries SET status='drawn',drawn_at=? WHERE id=?",(utc_now_iso(),lottery_id)); self.conn.commit(); return True,'',winners
  def create_lottery_payout(self,guild_id,user_id,lottery_id,ticket_number,amount_cents): self.conn.execute("INSERT INTO lottery_payouts (guild_id,user_id,lottery_id,ticket_number,amount_cents,created_at) VALUES (?,?,?,?,?,?)",(guild_id,user_id,lottery_id,ticket_number,amount_cents,utc_now_iso())); self.conn.commit()

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




# Shared application database singleton. Imported by all feature modules.
from config import DB_PATH

db = Database(DB_PATH)
SCHEMA_VERSION = db.run_startup_migrations()
print(f"[DB] Schema version: {SCHEMA_VERSION}")
