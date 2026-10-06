```python
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
  for x
