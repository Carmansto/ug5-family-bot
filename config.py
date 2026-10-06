import os
from datetime import timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
CONTRACT_CHANNEL_ID = int(os.getenv("CONTRACT_CHANNEL_ID", "0") or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)

# Automatic reporting channels.
RATING_CHANNEL_ID = int(os.getenv("RATING_CHANNEL_ID", "0") or 0)
FAMILY_STATS_CHANNEL_ID = int(os.getenv("FAMILY_STATS_CHANNEL_ID", "0") or 0)
BONUS_RESULTS_CHANNEL_ID = int(os.getenv("BONUS_RESULTS_CHANNEL_ID", "0") or 0)
PAYOUT_THREADS_CHANNEL_ID = int(os.getenv("PAYOUT_THREADS_CHANNEL_ID", "0") or 0)
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0") or 0)
LOTTERY_PUBLIC_CHANNEL_ID = int(os.getenv("LOTTERY_PUBLIC_CHANNEL_ID", "0") or 0)
LOTTERY_MANAGEMENT_CHANNEL_ID = int(os.getenv("LOTTERY_MANAGEMENT_CHANNEL_ID", "0") or 0)

RATING_MORNING_HOUR = int(os.getenv("RATING_MORNING_HOUR", "9") or 9)
RATING_EVENING_HOUR = int(os.getenv("RATING_EVENING_HOUR", "21") or 21)
FAMILY_STATS_HOUR = int(os.getenv("FAMILY_STATS_HOUR", "0") or 0)

# Birthday module.
BIRTHDAY_INPUT_CHANNEL_ID = int(os.getenv("BIRTHDAY_INPUT_CHANNEL_ID", "0") or 0)
BIRTHDAY_ALERT_CHANNEL_ID = int(os.getenv("BIRTHDAY_ALERT_CHANNEL_ID", "0") or 0)
BIRTHDAY_REMINDER_HOUR = int(os.getenv("BIRTHDAY_REMINDER_HOUR", "9") or 9)

# Only this Discord user can run /test-* commands.
TEST_USER_ID = int(os.getenv("TEST_USER_ID", "0") or 0)

# Personal economy tracker.
# If ECONOMY_USER_ID is set, only this user can use the economy.
# If ECONOMY_CHANNEL_ID is set, economy works only in this channel.
ECONOMY_CHANNEL_ID = int(os.getenv("ECONOMY_CHANNEL_ID", "0") or 0)
ECONOMY_USER_ID = int(os.getenv("ECONOMY_USER_ID", "0") or 0)

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

# Role of the leader. If not set separately, ADMIN_ROLE_ID is used.
# MANAGER_ROLE_IDS = management roles.
LEADER_ROLE_ID = int(
  os.getenv("LEADER_ROLE_ID", str(ADMIN_ROLE_ID or 0)) or 0
)

DB_PATH = os.getenv("DB_PATH", "contracts.db").strip()

FAMILY_PERCENT = Decimal(
  os.getenv(
    "FAMILY_PERCENT",
    os.getenv("FOMO_PERCENT", "15")
  ).strip() or "15"
)

TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"

try:
  LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
  LOCAL_TZ = timezone.utc

if not TOKEN:
  raise RuntimeError("DISCORD_TOKEN is empty.")

if not GUILD_ID:
  raise RuntimeError(
    "GUILD_ID is empty. Security lock requires the Agosto Discord server ID."
  )


__all__ = [
  'TOKEN',
  'GUILD_ID',
  'CONTRACT_CHANNEL_ID',
  'LOG_CHANNEL_ID',
  'RATING_CHANNEL_ID',
  'FAMILY_STATS_CHANNEL_ID',
  'BONUS_RESULTS_CHANNEL_ID',
  'PAYOUT_THREADS_CHANNEL_ID',
  'STORAGE_CHANNEL_ID',
  'LOTTERY_PUBLIC_CHANNEL_ID',
  'LOTTERY_MANAGEMENT_CHANNEL_ID',
  'RATING_MORNING_HOUR',
  'RATING_EVENING_HOUR',
  'FAMILY_STATS_HOUR',
  'BIRTHDAY_INPUT_CHANNEL_ID',
  'BIRTHDAY_ALERT_CHANNEL_ID',
  'BIRTHDAY_REMINDER_HOUR',
  'TEST_USER_ID',
  'ECONOMY_CHANNEL_ID',
  'ECONOMY_USER_ID',
  'ADMIN_ROLE_ID',
  'MANAGER_ROLE_IDS',
  'LEADER_ROLE_ID',
  'DB_PATH',
  'FAMILY_PERCENT',
  'TIMEZONE_NAME',
  'LOCAL_TZ',
]
