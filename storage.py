
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_ID, STORAGE_CHANNEL_ID
from database import db
from utils import (
  iso_to_unix,
  management_member,
  ukrainian_sort_key,
)


_bot: Optional[commands.Bot] = None


def set_bot(bot_instance: commands.Bot):
  global _bot
  _bot = bot_instance


def parse_storage_quantity(raw: str, allow_zero: bool = False) -> int:
  text = raw.strip().replace(" ", "").replace("_", "")

  if not text.isdigit():
    raise ValueError("\u041a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c \u043c\u0430\u0454 \u0431\u0443\u0442\u0438 \u0446\u0456\u043b\u0438\u043c \u0447\u0438\u0441\u043b\u043e\u043c.")

  value = int(text)

  if allow_zero:
    if value < 0:
      raise ValueError("\u041a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c \u043d\u0435 \u043c\u043e\u0436\u0435 \u0431\u0443\u0442\u0438 \u0432\u0456\u0434'\u0454\u043c\u043d\u043e\u044e.")
  elif value <= 0:
    raise ValueError("\u041a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c \u043c\u0430\u0454 \u0431\u0443\u0442\u0438 \u0431\u0456\u043b\u044c\u0448\u043e\u044e \u0437\u0430 0.")

  return value


def storage_channel_id_for_guild(guild_id: int) -> int:
  if STORAGE_CHANNEL_ID:
    return STORAGE_CHANNEL_ID

  saved = db.get_setting(guild_id, "storage_channel_id")
  if saved and saved.isdigit():
    return int(saved)

  return 0


async def get_storage_channel(
  bot_instance: commands.Bot,
  guild_id: int,
) -> Optional[discord.TextChannel]:
  channel_id = storage_channel_id_for_guild(guild_id)
  if not channel_id:
    return None

  channel = bot_instance.get_channel(channel_id)

  if channel is None:
    try:
      channel = await bot_instance.fetch_channel(channel_id)
    except discord.DiscordException:
      return None

  return channel if isinstance(channel, discord.TextChannel) else None


def storage_panel_embed(guild_id: int) -> discord.Embed:
  summary = db.storage_summary(guild_id)
  latest = summary["latest"]

  description = [
    f"\U0001f4e6 \u041f\u043e\u0437\u0438\u0446\u0456\u0439 \u043d\u0430 \u0441\u043a\u043b\u0430\u0434\u0456: **{summary['positions']}**",
  ]

  if latest:
    action = "\U0001f4e5 \u0434\u043e\u0434\u0430\u0432" if latest["movement_type"] == "ADD" else "\U0001f4e4 \u0432\u0437\u044f\u0432"
    ts = iso_to_unix(latest["created_at"])
    when = f"<t:{ts}:R>" if ts else latest["created_at"]

    description.extend([
      "",
      "**\u041e\u0441\u0442\u0430\u043d\u043d\u044f \u0437\u043c\u0456\u043d\u0430**",
      (
        f"{action.capitalize()} <@{latest['user_id']}> \u2022 "
        f"**{latest['item_name']} \xd7{latest['quantity']} {latest['item_unit']}**"
      ),
      when,
    ])
  else:
    description.extend([
      "",
      "\u041e\u043f\u0435\u0440\u0430\u0446\u0456\u0439 \u0437\u0456 \u0441\u043a\u043b\u0430\u0434\u043e\u043c \u0449\u0435 \u043d\u0435 \u0431\u0443\u043b\u043e.",
    ])

  embed = discord.Embed(
    title="\U0001f4e6 \u0421\u041a\u041b\u0410\u0414 \u2022 AGOSTO",
    description="\n".join(description),
    color=discord.Color.blurple(),
  )
  embed.set_footer(
    text="\u0417\u043d\u0430\u0439\u0434\u0456\u0442\u044c \u043f\u0440\u0435\u0434\u043c\u0435\u0442 \u2192 \u0434\u043e\u0434\u0430\u0439\u0442\u0435 \u0430\u0431\u043e \u0432\u0456\u0437\u044c\u043c\u0456\u0442\u044c \u043f\u043e\u0442\u0440\u0456\u0431\u043d\u0443 \u043a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c."
  )
  return embed


def storage_item_embed(guild_id: int, item_id: int) -> discord.Embed:
  item = db.get_storage_item(guild_id, item_id)

  if not item:
    return discord.Embed(
      title="\U0001f4e6 \u041f\u0440\u0435\u0434\u043c\u0435\u0442 \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e",
      color=discord.Color.red(),
    )

  latest = db.latest_storage_movement(guild_id, item_id)

  description = [
    f"# **{item['quantity']} {item['unit']}**",
  ]

  if latest:
    action = "\U0001f4e5 \u0414\u043e\u0434\u0430\u043d\u043e" if latest["movement_type"] == "ADD" else "\U0001f4e4 \u0412\u0437\u044f\u0442\u043e"
    ts = iso_to_unix(latest["created_at"])
    when = f"<t:{ts}:R>" if ts else latest["created_at"]

    description.extend([
      "",
      "**\u041e\u0441\u0442\u0430\u043d\u043d\u044f \u0437\u043c\u0456\u043d\u0430**",
      (
        f"{action}: **{latest['quantity']} {item['unit']}** "
        f"\u2022 <@{latest['user_id']}>"
      ),
      when,
    ])

  return discord.Embed(
    title=f"\U0001f4e6 {item['name']}",
    description="\n".join(description),
    color=discord.Color.blurple(),
  )


def storage_history_embed(
  guild_id: int,
  item_id: Optional[int] = None,
) -> discord.Embed:
  rows = db.storage_movements(
    guild_id,
    item_id=item_id,
    limit=15,
  )

  if item_id is not None:
    item = db.get_storage_item(
      guild_id,
      item_id,
      active_only=False,
    )
    title = (
      f"\U0001f4dc \u0406\u0421\u0422\u041e\u0420\u0406\u042f \u2022 {item['name']}"
      if item
      else "\U0001f4dc \u0406\u0421\u0422\u041e\u0420\u0406\u042f \u041f\u0420\u0415\u0414\u041c\u0415\u0422\u0410"
    )
  else:
    title = "\U0001f4dc \u041e\u0421\u0422\u0410\u041d\u041d\u0406 \u041e\u041f\u0415\u0420\u0410\u0426\u0406\u0407 \u0421\u041a\u041b\u0410\u0414\u0423"

  embed = discord.Embed(
    title=title,
    color=discord.Color.dark_teal(),
  )

  if not rows:
    embed.description = "\u0406\u0441\u0442\u043e\u0440\u0456\u044f \u043f\u043e\u043a\u0438 \u043f\u043e\u0440\u043e\u0436\u043d\u044f."
    return embed

  lines = []

  for row in rows:
    emoji = "\U0001f4e5" if row["movement_type"] == "ADD" else "\U0001f4e4"
    sign = "+" if row["movement_type"] == "ADD" else "\u2212"
    ts = iso_to_unix(row["created_at"])
    when = f"<t:{ts}:R>" if ts else row["created_at"]
    note = f" \u2022 {row['note']}" if row["note"] else ""

    lines.append(
      (
        f"{emoji} <@{row['user_id']}> \u2022 **{row['item_name']}** "
        f"{sign}{row['quantity']} {row['item_unit']} \u2022 {when}{note}"
      )
    )

  embed.description = "\n".join(lines)
  return embed


def storage_page_data(
  guild_id: int,
  page: int,
  page_size: int = 20,
):
  rows = list(db.storage_items_for_guild(guild_id))
  rows.sort(
    key=lambda row: ukrainian_sort_key(row["name"])
  )

  total_pages = max(1, (len(rows) + page_size - 1) // page_size)
  page = max(0, min(page, total_pages - 1))
  slice_rows = rows[
    page * page_size:(page + 1) * page_size
  ]

  return slice_rows, page, total_pages, len(rows)


def storage_page_embed(
  guild_id: int,
  page: int,
) -> tuple[discord.Embed, int, int]:
  rows, page, total_pages, total = storage_page_data(
    guild_id,
    page,
  )

  embed = discord.Embed(
    title=f"\U0001f4e6 \u0421\u041a\u041b\u0410\u0414 \u2022 {page + 1}/{total_pages}",
    color=discord.Color.blurple(),
  )

  if not rows:
    embed.description = "\u0421\u043a\u043b\u0430\u0434 \u043f\u043e\u043a\u0438 \u043f\u043e\u0440\u043e\u0436\u043d\u0456\u0439."
  else:
    lines = [
      (
        f"**{page * 20 + idx}. {row['name']}** \u2014 "
        f"{row['quantity']} {row['unit']}"
      )
      for idx, row in enumerate(rows, start=1)
    ]
    embed.description = "\n".join(lines)

  embed.set_footer(
    text=f"\u0412\u0441\u044c\u043e\u0433\u043e \u043f\u043e\u0437\u0438\u0446\u0456\u0439: {total}"
  )
  return embed, page, total_pages


async def install_storage_panel(
  bot_instance: commands.Bot,
  guild: discord.Guild,
  channel: discord.TextChannel,
):
  old_message_id = db.get_setting(
    guild.id,
    "storage_panel_message_id",
  )

  if old_message_id:
    try:
      old_message = await channel.fetch_message(
        int(old_message_id)
      )
      await old_message.edit(
        embed=storage_panel_embed(guild.id),
        view=StoragePanelView(),
      )
      db.set_setting(
        guild.id,
        "storage_channel_id",
        str(channel.id),
      )
      return old_message
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
      pass

  message = await channel.send(
    embed=storage_panel_embed(guild.id),
    view=StoragePanelView(),
  )

  db.set_setting(
    guild.id,
    "storage_channel_id",
    str(channel.id),
  )
  db.set_setting(
    guild.id,
    "storage_panel_message_id",
    str(message.id),
  )
  return message


async def ensure_storage_panel(
  bot_instance: commands.Bot,
):
  if not GUILD_ID:
    return

  channel = await get_storage_channel(
    bot_instance,
    GUILD_ID,
  )
  if channel is None:
    return

  guild = channel.guild
  try:
    await install_storage_panel(
      bot_instance,
      guild,
      channel,
    )
  except discord.DiscordException as exc:
    print(f"[STORAGE] Could not ensure panel: {exc}")


async def refresh_storage_panel(guild_id: int):
  if _bot is None:
    return

  channel = await get_storage_channel(
    _bot,
    guild_id,
  )
  if channel is None:
    return

  message_id = db.get_setting(
    guild_id,
    "storage_panel_message_id",
  )
  if not message_id:
    return

  try:
    message = await channel.fetch_message(
      int(message_id)
    )
    await message.edit(
      embed=storage_panel_embed(guild_id),
      view=StoragePanelView(),
    )
  except discord.DiscordException:
    pass


class StorageQuantityModal(discord.ui.Modal):
  def __init__(
    self,
    guild_id: int,
    item_id: int,
    movement_type: str,
  ):
    title = (
      "\u2795 \u0414\u043e\u0434\u0430\u0442\u0438 \u043d\u0430 \u0441\u043a\u043b\u0430\u0434"
      if movement_type == "ADD"
      else "\u2796 \u0412\u0437\u044f\u0442\u0438 \u0437\u0456 \u0441\u043a\u043b\u0430\u0434\u0443"
    )
    super().__init__(title=title)
    self.guild_id = guild_id
    self.item_id = item_id
    self.movement_type = movement_type

    self.quantity_input = discord.ui.TextInput(
      label="\u041a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: 25",
      required=True,
      max_length=12,
    )
    self.add_item(self.quantity_input)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    try:
      quantity = parse_storage_quantity(
        str(self.quantity_input)
      )

      result = db.change_storage_quantity(
        self.guild_id,
        self.item_id,
        interaction.user.id,
        self.movement_type,
        quantity,
      )

      await refresh_storage_panel(self.guild_id)

      verb = (
        "\u0414\u043e\u0434\u0430\u043d\u043e"
        if self.movement_type == "ADD"
        else "\u0412\u0437\u044f\u0442\u043e"
      )

      await interaction.response.send_message(
        content=(
          f"\u2705 **{result['item_name']}**\n"
          f"{verb}: **{quantity} {result['unit']}**\n"
          f"\u0411\u0443\u043b\u043e: **{result['before']} {result['unit']}**\n"
          f"\u0421\u0442\u0430\u043b\u043e: **{result['after']} {result['unit']}**"
        ),
        embed=storage_item_embed(
          self.guild_id,
          self.item_id,
        ),
        view=StorageItemView(
          self.guild_id,
          self.item_id,
        ),
        ephemeral=True,
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class StorageSearchModal(discord.ui.Modal):
  def __init__(
    self,
    guild_id: int,
    mode: str,
  ):
    titles = {
      "view": "\U0001f50e \u041f\u043e\u0448\u0443\u043a \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0430",
      "add": "\u2795 \u0429\u043e \u0434\u043e\u0434\u0430\u0442\u0438?",
      "take": "\u2796 \u0429\u043e \u0432\u0437\u044f\u0442\u0438?",
      "manage": "\u2699\ufe0f \u0417\u043d\u0430\u0439\u0442\u0438 \u043f\u0440\u0435\u0434\u043c\u0435\u0442",
    }
    super().__init__(
      title=titles.get(mode, "\U0001f50e \u041f\u043e\u0448\u0443\u043a \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0430")
    )
    self.guild_id = guild_id
    self.mode = mode

    self.query_input = discord.ui.TextInput(
      label="\u041d\u0430\u0437\u0432\u0430 \u0430\u0431\u043e \u0447\u0430\u0441\u0442\u0438\u043d\u0430 \u043d\u0430\u0437\u0432\u0438",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: \u0431\u0440\u043e\u043d",
      required=True,
      max_length=100,
    )
    self.add_item(self.query_input)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    query = str(self.query_input).strip()
    rows = db.search_storage_items(
      self.guild_id,
      query,
      25,
    )

    if not rows:
      await interaction.response.send_message(
        f"\U0001f50e \u0417\u0430 \u0437\u0430\u043f\u0438\u0442\u043e\u043c **{query}** \u043d\u0456\u0447\u043e\u0433\u043e \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e.",
        ephemeral=True,
      )
      return

    embed = discord.Embed(
      title=f"\U0001f50e \u0417\u043d\u0430\u0439\u0434\u0435\u043d\u043e: {len(rows)}",
      description="\n".join(
        (
          f"\u2022 **{row['name']}** \u2014 "
          f"{row['quantity']} {row['unit']}"
        )
        for row in rows
      ),
      color=discord.Color.blurple(),
    )

    await interaction.response.send_message(
      embed=embed,
      view=StorageSearchResultsView(
        self.guild_id,
        rows,
        self.mode,
      ),
      ephemeral=True,
    )


class StorageItemSelect(discord.ui.Select):
  def __init__(
    self,
    guild_id: int,
    rows,
    mode: str,
    row: int = 0,
  ):
    self.guild_id = guild_id
    self.mode = mode

    options = [
      discord.SelectOption(
        label=row_data["name"][:100],
        value=str(row_data["id"]),
        description=(
          f"{row_data['quantity']} {row_data['unit']}"
        )[:100],
      )
      for row_data in rows[:25]
    ]

    super().__init__(
      placeholder="\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043f\u0440\u0435\u0434\u043c\u0435\u0442",
      min_values=1,
      max_values=1,
      options=options,
      row=row,
    )

  async def callback(
    self,
    interaction: discord.Interaction,
  ):
    item_id = int(self.values[0])

    if self.mode == "add":
      await interaction.response.send_modal(
        StorageQuantityModal(
          self.guild_id,
          item_id,
          "ADD",
        )
      )
      return

    if self.mode == "take":
      await interaction.response.send_modal(
        StorageQuantityModal(
          self.guild_id,
          item_id,
          "TAKE",
        )
      )
      return

    if self.mode == "manage":
      if (
        not isinstance(interaction.user, discord.Member)
        or not management_member(interaction.user)
      ):
        await interaction.response.send_message(
          "\u274c \u041a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0430\u043c\u0438 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
          ephemeral=True,
        )
        return

      await interaction.response.edit_message(
        embed=storage_item_embed(
          self.guild_id,
          item_id,
        ),
        view=StorageManageItemView(
          self.guild_id,
          item_id,
        ),
      )
      return

    await interaction.response.edit_message(
      embed=storage_item_embed(
        self.guild_id,
        item_id,
      ),
      view=StorageItemView(
        self.guild_id,
        item_id,
      ),
    )


class StorageSearchResultsView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    rows,
    mode: str,
  ):
    super().__init__(timeout=300)
    self.add_item(
      StorageItemSelect(
        guild_id,
        rows,
        mode,
      )
    )


class StoragePageSelect(discord.ui.Select):
  def __init__(
    self,
    guild_id: int,
    rows,
    page: int,
  ):
    self.guild_id = guild_id
    self.page = page

    options = [
      discord.SelectOption(
        label=row_data["name"][:100],
        value=str(row_data["id"]),
        description=(
          f"{row_data['quantity']} {row_data['unit']}"
        )[:100],
      )
      for row_data in rows[:25]
    ]

    super().__init__(
      placeholder="\u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0438 \u043f\u0440\u0435\u0434\u043c\u0435\u0442 \u0437\u0456 \u0441\u0442\u043e\u0440\u0456\u043d\u043a\u0438",
      min_values=1,
      max_values=1,
      options=options,
      row=0,
    )

  async def callback(
    self,
    interaction: discord.Interaction,
  ):
    item_id = int(self.values[0])
    await interaction.response.edit_message(
      embed=storage_item_embed(
        self.guild_id,
        item_id,
      ),
      view=StorageItemView(
        self.guild_id,
        item_id,
        return_page=self.page,
      ),
    )


class StorageAllView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    page: int = 0,
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id

    rows, page, total_pages, _ = storage_page_data(
      guild_id,
      page,
    )
    self.page = page
    self.total_pages = total_pages

    if rows:
      self.add_item(
        StoragePageSelect(
          guild_id,
          rows,
          page,
        )
      )

    self.previous.disabled = page <= 0
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
    embed, page, _ = storage_page_embed(
      self.guild_id,
      self.page - 1,
    )
    await interaction.response.edit_message(
      embed=embed,
      view=StorageAllView(
        self.guild_id,
        page,
      ),
    )

  @discord.ui.button(
    label="\u041f\u043e\u0448\u0443\u043a",
    emoji="\U0001f50e",
    style=discord.ButtonStyle.primary,
    row=1,
  )
  async def search(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageSearchModal(
        self.guild_id,
        "view",
      )
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
    embed, page, _ = storage_page_embed(
      self.guild_id,
      self.page + 1,
    )
    await interaction.response.edit_message(
      embed=embed,
      view=StorageAllView(
        self.guild_id,
        page,
      ),
    )


class StorageItemView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    item_id: int,
    return_page: Optional[int] = None,
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.item_id = item_id
    self.return_page = return_page
    self.back.disabled = return_page is None

  @discord.ui.button(
    label="\u0414\u043e\u0434\u0430\u0442\u0438",
    emoji="\u2795",
    style=discord.ButtonStyle.success,
  )
  async def add(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageQuantityModal(
        self.guild_id,
        self.item_id,
        "ADD",
      )
    )

  @discord.ui.button(
    label="\u0412\u0437\u044f\u0442\u0438",
    emoji="\u2796",
    style=discord.ButtonStyle.danger,
  )
  async def take(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageQuantityModal(
        self.guild_id,
        self.item_id,
        "TAKE",
      )
    )

  @discord.ui.button(
    label="\u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    emoji="\U0001f4dc",
    style=discord.ButtonStyle.secondary,
  )
  async def history(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      embed=storage_history_embed(
        self.guild_id,
        self.item_id,
      ),
      view=StorageItemView(
        self.guild_id,
        self.item_id,
        self.return_page,
      ),
    )

  @discord.ui.button(
    label="\u0414\u043e \u0441\u043f\u0438\u0441\u043a\u0443",
    emoji="\u21a9\ufe0f",
    style=discord.ButtonStyle.secondary,
  )
  async def back(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if self.return_page is None:
      return

    embed, page, _ = storage_page_embed(
      self.guild_id,
      self.return_page,
    )
    await interaction.response.edit_message(
      embed=embed,
      view=StorageAllView(
        self.guild_id,
        page,
      ),
    )


class StorageCreateModal(discord.ui.Modal):
  def __init__(self, guild_id: int):
    super().__init__(title="\u2795 \u041d\u043e\u0432\u0438\u0439 \u043f\u0440\u0435\u0434\u043c\u0435\u0442")
    self.guild_id = guild_id

    self.name_input = discord.ui.TextInput(
      label="\u041d\u0430\u0437\u0432\u0430 \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0430",
      placeholder="\u041d\u0430\u043f\u0440\u0438\u043a\u043b\u0430\u0434: \u0411\u0440\u043e\u043d\u0435\u043f\u043b\u0430\u0441\u0442\u0438\u043d\u0430",
      required=True,
      max_length=100,
    )
    self.quantity_input = discord.ui.TextInput(
      label="\u041f\u043e\u0447\u0430\u0442\u043a\u043e\u0432\u0430 \u043a\u0456\u043b\u044c\u043a\u0456\u0441\u0442\u044c",
      placeholder="0",
      default="0",
      required=True,
      max_length=12,
    )
    self.unit_input = discord.ui.TextInput(
      label="\u041e\u0434\u0438\u043d\u0438\u0446\u044f",
      placeholder="\u0448\u0442.",
      default="\u0448\u0442.",
      required=False,
      max_length=20,
    )

    self.add_item(self.name_input)
    self.add_item(self.quantity_input)
    self.add_item(self.unit_input)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    if (
      not isinstance(interaction.user, discord.Member)
      or not management_member(interaction.user)
    ):
      await interaction.response.send_message(
        "\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.",
        ephemeral=True,
      )
      return

    try:
      quantity = parse_storage_quantity(
        str(self.quantity_input),
        allow_zero=True,
      )
      item_id = db.create_storage_item(
        self.guild_id,
        str(self.name_input),
        quantity,
        str(self.unit_input),
        interaction.user.id,
      )

      await refresh_storage_panel(self.guild_id)

      await interaction.response.send_message(
        content="\u2705 \u041f\u0440\u0435\u0434\u043c\u0435\u0442 \u0441\u0442\u0432\u043e\u0440\u0435\u043d\u043e.",
        embed=storage_item_embed(
          self.guild_id,
          item_id,
        ),
        view=StorageManageItemView(
          self.guild_id,
          item_id,
        ),
        ephemeral=True,
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class StorageRenameModal(discord.ui.Modal):
  def __init__(
    self,
    guild_id: int,
    item_id: int,
  ):
    item = db.get_storage_item(guild_id, item_id)
    super().__init__(title="\u270f\ufe0f \u041f\u0435\u0440\u0435\u0439\u043c\u0435\u043d\u0443\u0432\u0430\u0442\u0438 \u043f\u0440\u0435\u0434\u043c\u0435\u0442")
    self.guild_id = guild_id
    self.item_id = item_id

    self.name_input = discord.ui.TextInput(
      label="\u041d\u043e\u0432\u0430 \u043d\u0430\u0437\u0432\u0430",
      default=item["name"] if item else None,
      required=True,
      max_length=100,
    )
    self.add_item(self.name_input)

  async def on_submit(
    self,
    interaction: discord.Interaction,
  ):
    if (
      not isinstance(interaction.user, discord.Member)
      or not management_member(interaction.user)
    ):
      await interaction.response.send_message(
        "\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.",
        ephemeral=True,
      )
      return

    try:
      db.rename_storage_item(
        self.guild_id,
        self.item_id,
        str(self.name_input),
      )
      await refresh_storage_panel(self.guild_id)

      await interaction.response.send_message(
        content="\u2705 \u041d\u0430\u0437\u0432\u0443 \u0437\u043c\u0456\u043d\u0435\u043d\u043e.",
        embed=storage_item_embed(
          self.guild_id,
          self.item_id,
        ),
        view=StorageManageItemView(
          self.guild_id,
          self.item_id,
        ),
        ephemeral=True,
      )
    except ValueError as exc:
      await interaction.response.send_message(
        f"\u274c {exc}",
        ephemeral=True,
      )


class StorageDeleteConfirmView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    item_id: int,
  ):
    super().__init__(timeout=90)
    self.guild_id = guild_id
    self.item_id = item_id

  @discord.ui.button(
    label="\u0422\u0430\u043a, \u043f\u0440\u0438\u0431\u0440\u0430\u0442\u0438",
    emoji="\U0001f5d1\ufe0f",
    style=discord.ButtonStyle.danger,
  )
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if (
      not isinstance(interaction.user, discord.Member)
      or not management_member(interaction.user)
    ):
      await interaction.response.edit_message(
        content="\u274c \u041d\u0435\u043c\u0430\u0454 \u043f\u0440\u0430\u0432\u0430.",
        embed=None,
        view=None,
      )
      return

    try:
      item = db.get_storage_item(
        self.guild_id,
        self.item_id,
      )
      name = item["name"] if item else f"ID {self.item_id}"

      db.archive_storage_item(
        self.guild_id,
        self.item_id,
      )
      await refresh_storage_panel(self.guild_id)

      await interaction.response.edit_message(
        content=f"\u2705 **{name}** \u043f\u0440\u0438\u0431\u0440\u0430\u043d\u043e \u0437\u0456 \u0441\u043a\u043b\u0430\u0434\u0443.",
        embed=None,
        view=None,
      )
    except ValueError as exc:
      await interaction.response.edit_message(
        content=f"\u274c {exc}",
        embed=None,
        view=None,
      )

  @discord.ui.button(
    label="\u041d\u0456",
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
      embed=storage_item_embed(
        self.guild_id,
        self.item_id,
      ),
      view=StorageManageItemView(
        self.guild_id,
        self.item_id,
      ),
    )


class StorageManageItemView(discord.ui.View):
  def __init__(
    self,
    guild_id: int,
    item_id: int,
  ):
    super().__init__(timeout=300)
    self.guild_id = guild_id
    self.item_id = item_id

  @discord.ui.button(
    label="\u041f\u0435\u0440\u0435\u0439\u043c\u0435\u043d\u0443\u0432\u0430\u0442\u0438",
    emoji="\u270f\ufe0f",
    style=discord.ButtonStyle.primary,
  )
  async def rename(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageRenameModal(
        self.guild_id,
        self.item_id,
      )
    )

  @discord.ui.button(
    label="\u0412\u0438\u0434\u0430\u043b\u0438\u0442\u0438",
    emoji="\U0001f5d1\ufe0f",
    style=discord.ButtonStyle.danger,
  )
  async def delete(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    item = db.get_storage_item(
      self.guild_id,
      self.item_id,
    )
    if not item:
      await interaction.response.send_message(
        "\u274c \u041f\u0440\u0435\u0434\u043c\u0435\u0442 \u043d\u0435 \u0437\u043d\u0430\u0439\u0434\u0435\u043d\u043e.",
        ephemeral=True,
      )
      return

    await interaction.response.edit_message(
      content=(
        f"\u26a0\ufe0f \u041f\u0440\u0438\u0431\u0440\u0430\u0442\u0438 **{item['name']}** \u0437\u0456 \u0441\u043a\u043b\u0430\u0434\u0443?\n"
        "\u0406\u0441\u0442\u043e\u0440\u0456\u044f \u0440\u0443\u0445\u0456\u0432 \u0437\u0430\u043b\u0438\u0448\u0438\u0442\u044c\u0441\u044f."
      ),
      embed=None,
      view=StorageDeleteConfirmView(
        self.guild_id,
        self.item_id,
      ),
    )

  @discord.ui.button(
    label="\u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    emoji="\U0001f4dc",
    style=discord.ButtonStyle.secondary,
  )
  async def history(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.edit_message(
      content=None,
      embed=storage_history_embed(
        self.guild_id,
        self.item_id,
      ),
      view=StorageManageItemView(
        self.guild_id,
        self.item_id,
      ),
    )


class StorageManageView(discord.ui.View):
  def __init__(self, guild_id: int):
    super().__init__(timeout=300)
    self.guild_id = guild_id

  @discord.ui.button(
    label="\u0421\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u043f\u0440\u0435\u0434\u043c\u0435\u0442",
    emoji="\u2795",
    style=discord.ButtonStyle.success,
  )
  async def create(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageCreateModal(self.guild_id)
    )

  @discord.ui.button(
    label="\u0417\u043d\u0430\u0439\u0442\u0438 / \u0440\u0435\u0434\u0430\u0433\u0443\u0432\u0430\u0442\u0438",
    emoji="\U0001f50e",
    style=discord.ButtonStyle.primary,
  )
  async def find(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageSearchModal(
        self.guild_id,
        "manage",
      )
    )


class StoragePanelView(discord.ui.View):
  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
    label="\u041f\u043e\u0448\u0443\u043a",
    emoji="\U0001f50e",
    style=discord.ButtonStyle.primary,
    custom_id="agosto:storage:search",
    row=0,
  )
  async def search(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageSearchModal(
        interaction.guild_id,
        "view",
      )
    )

  @discord.ui.button(
    label="\u0412\u0435\u0441\u044c \u0441\u043a\u043b\u0430\u0434",
    emoji="\U0001f4e6",
    style=discord.ButtonStyle.primary,
    custom_id="agosto:storage:all",
    row=0,
  )
  async def all_items(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    embed, page, _ = storage_page_embed(
      interaction.guild_id,
      0,
    )
    await interaction.response.send_message(
      embed=embed,
      view=StorageAllView(
        interaction.guild_id,
        page,
      ),
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u0414\u043e\u0434\u0430\u0442\u0438",
    emoji="\u2795",
    style=discord.ButtonStyle.success,
    custom_id="agosto:storage:add",
    row=0,
  )
  async def add(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageSearchModal(
        interaction.guild_id,
        "add",
      )
    )

  @discord.ui.button(
    label="\u0412\u0437\u044f\u0442\u0438",
    emoji="\u2796",
    style=discord.ButtonStyle.danger,
    custom_id="agosto:storage:take",
    row=0,
  )
  async def take(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_modal(
      StorageSearchModal(
        interaction.guild_id,
        "take",
      )
    )

  @discord.ui.button(
    label="\u0406\u0441\u0442\u043e\u0440\u0456\u044f",
    emoji="\U0001f4dc",
    style=discord.ButtonStyle.secondary,
    custom_id="agosto:storage:history",
    row=1,
  )
  async def history(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    await interaction.response.send_message(
      embed=storage_history_embed(
        interaction.guild_id,
      ),
      ephemeral=True,
    )

  @discord.ui.button(
    label="\u041a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f",
    emoji="\u2699\ufe0f",
    style=discord.ButtonStyle.secondary,
    custom_id="agosto:storage:manage",
    row=1,
  )
  async def manage(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button,
  ):
    if (
      not isinstance(interaction.user, discord.Member)
      or not management_member(interaction.user)
    ):
      await interaction.response.send_message(
        "\u274c \u041a\u0435\u0440\u0443\u0432\u0430\u043d\u043d\u044f \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0430\u043c\u0438 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0435 \u0442\u0456\u043b\u044c\u043a\u0438 \u043a\u0435\u0440\u0456\u0432\u043d\u0438\u0446\u0442\u0432\u0443.",
        ephemeral=True,
      )
      return

    await interaction.response.send_message(
      embed=discord.Embed(
        title="\u2699\ufe0f \u041a\u0415\u0420\u0423\u0412\u0410\u041d\u041d\u042f \u0421\u041a\u041b\u0410\u0414\u041e\u041c",
        description=(
          "\u0421\u0442\u0432\u043e\u0440\u0456\u0442\u044c \u043d\u043e\u0432\u0438\u0439 \u043f\u0440\u0435\u0434\u043c\u0435\u0442 \u0430\u0431\u043e \u0437\u043d\u0430\u0439\u0434\u0456\u0442\u044c \u0456\u0441\u043d\u0443\u044e\u0447\u0438\u0439 "
          "\u0434\u043b\u044f \u043f\u0435\u0440\u0435\u0439\u043c\u0435\u043d\u0443\u0432\u0430\u043d\u043d\u044f \u0447\u0438 \u0432\u0438\u0434\u0430\u043b\u0435\u043d\u043d\u044f."
        ),
        color=discord.Color.blurple(),
      ),
      view=StorageManageView(
        interaction.guild_id,
      ),
      ephemeral=True,
    )


def register_commands(bot: commands.Bot):
  @bot.tree.command(
    name="sklad",
    description="\u0421\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u0430\u0431\u043e \u043e\u043d\u043e\u0432\u0438\u0442\u0438 \u043f\u0430\u043d\u0435\u043b\u044c \u0441\u043a\u043b\u0430\u0434\u0443 \u0441\u0456\u043c'\u0457",
  )
  async def sklad(interaction: discord.Interaction):
    if (
      not isinstance(interaction.user, discord.Member)
      or not management_member(interaction.user)
    ):
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

    channel = None

    if STORAGE_CHANNEL_ID:
      possible = guild.get_channel(STORAGE_CHANNEL_ID)
      if possible is None:
        try:
          possible = await bot.fetch_channel(
            STORAGE_CHANNEL_ID
          )
        except discord.DiscordException:
          possible = None

      if isinstance(possible, discord.TextChannel):
        channel = possible

    if channel is None:
      if isinstance(interaction.channel, discord.TextChannel):
        channel = interaction.channel

    if channel is None:
      await interaction.response.send_message(
        "\u274c \u0417\u0430\u043f\u0443\u0441\u0442\u0438 \u043a\u043e\u043c\u0430\u043d\u0434\u0443 \u0443 \u0437\u0432\u0438\u0447\u0430\u0439\u043d\u043e\u043c\u0443 \u0442\u0435\u043a\u0441\u0442\u043e\u0432\u043e\u043c\u0443 \u043a\u0430\u043d\u0430\u043b\u0456.",
        ephemeral=True,
      )
      return

    await interaction.response.defer(ephemeral=True)

    try:
      message = await install_storage_panel(
        bot,
        guild,
        channel,
      )

      await interaction.followup.send(
        (
          f"\u2705 \u041f\u0430\u043d\u0435\u043b\u044c \u0441\u043a\u043b\u0430\u0434\u0443 \u0433\u043e\u0442\u043e\u0432\u0430: {message.jump_url}\n"
          f"\u041a\u0430\u043d\u0430\u043b: <#{channel.id}>"
        ),
        ephemeral=True,
      )
    except discord.DiscordException as exc:
      await interaction.followup.send(
        f"\u274c \u041d\u0435 \u0432\u0434\u0430\u043b\u043e\u0441\u044f \u0441\u0442\u0432\u043e\u0440\u0438\u0442\u0438 \u043f\u0430\u043d\u0435\u043b\u044c: {exc}",
        ephemeral=True,
      )
