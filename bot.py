import asyncio
from datetime import datetime, timedelta

import discord
from discord.ext import commands

from config import (
  BIRTHDAY_ALERT_CHANNEL_ID,
  BIRTHDAY_INPUT_CHANNEL_ID,
  BIRTHDAY_REMINDER_HOUR,
  BONUS_RESULTS_CHANNEL_ID,
  FAMILY_STATS_CHANNEL_ID,
  FAMILY_STATS_HOUR,
  GUILD_ID,
  PAYOUT_THREADS_CHANNEL_ID,
  LOTTERY_PUBLIC_CHANNEL_ID,
  LOTTERY_MANAGEMENT_CHANNEL_ID,
  RATING_CHANNEL_ID,
  RATING_EVENING_HOUR,
  RATING_MORNING_HOUR,
  TEST_USER_ID,
  TIMEZONE_NAME,
  TOKEN,
  LOCAL_TZ,
)
from database import db, SCHEMA_VERSION
import contracts
import payouts
import stats
import bonuses
import birthdays
import storage
import lottery
import wars

async def scheduled_posts_loop(bot_instance: commands.Bot):
  await bot_instance.wait_until_ready()

  while not bot_instance.is_closed():
    try:
      now = datetime.now(LOCAL_TZ)
      today_key = now.date().isoformat()

      if (
        RATING_CHANNEL_ID
        and now.hour == RATING_MORNING_HOUR
        and db.get_setting(GUILD_ID, "auto_rating_morning_date") != today_key
      ):
        await stats.send_auto_rating(
          bot_instance,
          f"{now.strftime('%d.%m.%Y')} \u2022 \u0440\u0430\u043d\u043e\u043a",
        )
        db.set_setting(GUILD_ID, "auto_rating_morning_date", today_key)
        print(f"[AUTO] Morning rating sent for {today_key}")

      if (
        RATING_CHANNEL_ID
        and now.hour == RATING_EVENING_HOUR
        and db.get_setting(GUILD_ID, "auto_rating_evening_date") != today_key
      ):
        await stats.send_auto_rating(
          bot_instance,
          f"{now.strftime('%d.%m.%Y')} \u2022 \u0432\u0435\u0447\u0456\u0440",
        )
        db.set_setting(GUILD_ID, "auto_rating_evening_date", today_key)
        print(f"[AUTO] Evening rating sent for {today_key}")

      if (
        BIRTHDAY_ALERT_CHANNEL_ID
        and now.hour == BIRTHDAY_REMINDER_HOUR
        and db.get_setting(GUILD_ID, "birthday_reminders_date") != today_key
      ):
        await birthdays.send_birthday_reminders(bot_instance)
        db.set_setting(GUILD_ID, "birthday_reminders_date", today_key)
        print(f"[BIRTHDAY] Reminder check completed for {today_key}")

      await bonuses.maybe_auto_close_bonus(bot_instance, now)
      await lottery.maybe_finish_expired(bot_instance, now)

      if FAMILY_STATS_CHANNEL_ID and now.hour == FAMILY_STATS_HOUR:
        report_day = now.date() - timedelta(days=1)
        report_key = report_day.isoformat()

        if db.get_setting(GUILD_ID, "auto_family_stats_date") != report_key:
          await stats.send_auto_family_stats(bot_instance, report_day)
          db.set_setting(GUILD_ID, "auto_family_stats_date", report_key)
          print(f"[AUTO] Family daily stats sent for {report_key}")

    except Exception as exc:
      print(f"[AUTO] Scheduled post error: {exc}")

    await asyncio.sleep(30)


class AgostoCommandTree(discord.app_commands.CommandTree):
  async def interaction_check(self, interaction: discord.Interaction) -> bool:
    # Hard allow-list: commands can run only inside the configured Agosto guild.
    if interaction.guild_id != GUILD_ID:
      try:
        if not interaction.response.is_done():
          await interaction.response.send_message(
            "\U0001f512 This bot is private and works only on the Agosto server.",
            ephemeral=True,
          )
      except discord.DiscordException:
        pass
      print(
        "[SECURITY] Blocked interaction "
        f"guild_id={interaction.guild_id} user_id={interaction.user.id}"
      )
      return False
    return True


class ContractBot(commands.Bot):
  def __init__(self):
    intents = discord.Intents.default()
    super().__init__(
      command_prefix="!",
      intents=intents,
      tree_cls=AgostoCommandTree,
    )
    self._unpaid_refreshed = False
    self._scheduled_posts_task = None
    self._commands_registered = False

  async def setup_hook(self):
    contracts.set_bot(self)
    storage.set_bot(self)

    self.add_view(contracts.MainContractPanelView(self))
    self.add_view(contracts.UnpaidCompletedView(self))
    self.add_view(birthdays.BirthdayPanelView())
    self.add_view(storage.StoragePanelView())
    
    await wars.restore_active_views(self)

    
    if not self._commands_registered:
      contracts.register_commands(self)
      payouts.register_commands(self)
      stats.register_commands(self)
      bonuses.register_commands(self)
      birthdays.register_commands(self)
      storage.register_commands(self)
      lottery.register_commands(self)
      wars.register_commands(self)
      self._commands_registered = True
     
    if self._scheduled_posts_task is None:
      self._scheduled_posts_task = asyncio.create_task(scheduled_posts_loop(self))

    guild = discord.Object(id=GUILD_ID)
    self.tree.copy_global_to(guild=guild)
    await self.tree.sync(guild=guild)
    print(f"[SYNC] Commands synced only to allowed guild {GUILD_ID}")

    # Remove any global commands left from an older deployment.
    # The guild-specific copy above remains available on Agosto.
    self.tree.clear_commands(guild=None)
    await self.tree.sync()
    print("[SECURITY] Global application commands cleared")

  async def on_guild_join(self, guild: discord.Guild):
    if guild.id == GUILD_ID:
      print(f"[SECURITY] Allowed guild joined: {guild.name} ({guild.id})")
      return

    print(f"[SECURITY] Unauthorized guild rejected: {guild.name} ({guild.id})")
    try:
      await guild.leave()
      print(f"[SECURITY] Left unauthorized guild {guild.id}")
    except discord.DiscordException as exc:
      print(f"[SECURITY] Could not leave unauthorized guild {guild.id}: {exc}")

  async def _leave_unauthorized_guilds(self):
    for guild in list(self.guilds):
      if guild.id == GUILD_ID:
        continue
      print(
        f"[SECURITY] Found unauthorized guild on startup: "
        f"{guild.name} ({guild.id})"
      )
      try:
        await guild.leave()
        print(f"[SECURITY] Left unauthorized guild {guild.id}")
      except discord.DiscordException as exc:
        print(f"[SECURITY] Could not leave unauthorized guild {guild.id}: {exc}")

  async def on_ready(self):
    print(f"[READY] Logged in as {self.user} ({self.user.id})")
    await self._leave_unauthorized_guilds()

    allowed_guild = self.get_guild(GUILD_ID)
    if allowed_guild is None:
      print(f"[SECURITY] WARNING: allowed guild {GUILD_ID} is not connected")
    else:
      print(
        f"[SECURITY] Guild lock active: "
        f"{allowed_guild.name} ({allowed_guild.id})"
      )
    print(
      "[AUTO] "
      f"rating_channel={RATING_CHANNEL_ID or 'disabled'} "
      f"family_stats_channel={FAMILY_STATS_CHANNEL_ID or 'disabled'} "
      f"bonus_results_channel={BONUS_RESULTS_CHANNEL_ID or 'disabled'} "
      f"payout_threads_channel={PAYOUT_THREADS_CHANNEL_ID or 'disabled'} "
      f"lottery_public_channel={LOTTERY_PUBLIC_CHANNEL_ID or 'disabled'} "
      f"lottery_management_channel={LOTTERY_MANAGEMENT_CHANNEL_ID or 'disabled'} "
      f"rating_hours={RATING_MORNING_HOUR}/{RATING_EVENING_HOUR} "
      f"family_stats_hour={FAMILY_STATS_HOUR} "
      f"birthday_input={BIRTHDAY_INPUT_CHANNEL_ID or 'disabled'} "
      f"birthday_alert={BIRTHDAY_ALERT_CHANNEL_ID or 'disabled'} "
      f"birthday_hour={BIRTHDAY_REMINDER_HOUR} "
      f"test_user={TEST_USER_ID or 'disabled'} "
      f"timezone={TIMEZONE_NAME}"
    )

    try:
      await birthdays.ensure_birthday_panel(self)
    except Exception as exc:
      print(f"[BIRTHDAY] Could not ensure panel: {exc}")

    try:
      await storage.ensure_storage_panel(self)
    except Exception as exc:
      print(f"[STORAGE] Could not ensure panel: {exc}")

    try:
      lottery.restore_active_views(self)
      lottery.restore_management_view(self)
      await lottery.ensure_lottery_channels(self)
    except Exception as exc:
      print(f"[LOTTERY] Could not restore/setup lottery panels: {exc}")

    if not self._unpaid_refreshed and GUILD_ID:
      self._unpaid_refreshed = True
      try:
        unpaid_rows = db.unpaid_for_guild(GUILD_ID, limit=100)
        for row in unpaid_rows:
          await contracts.refresh_completed_message(row["message_id"])
        if unpaid_rows:
          print(f"[UI] Refreshed {len(unpaid_rows)} unpaid contract message(s)")
      except Exception as exc:
        print(f"[UI] Could not refresh unpaid messages: {exc}")


bot = ContractBot()
bot.run(TOKEN)
