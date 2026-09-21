# Agosto Contract Bot V7.9.1

This release changes `storage.py` only. All other V7.9 modules and database schema are unchanged.

## Fixed timestamps

Warehouse panel, item cards and movement history show the fixed date and time of the operation in the `Europe/Kyiv` timezone, e.g. `20.09.2026 00:24`. The time no longer changes as the message ages.

## Move the main storage panel to the bottom

Run `/sklad` in the storage channel. The bot posts a new panel and deletes the previous one so the panel is the latest message in that channel. It records the new panel ID and the old panel buttons are removed with the old message.

Automatic panel restoration at startup and normal data refreshes still EDIT the existing panel in place without moving it.

If posting fails, the previous panel remains. If removal of the previous panel fails, the bot tries to remove the replacement and reports an error.

## Update on GitHub

Only `storage.py` needs to be replaced when upgrading from V7.9. Alternatively upload the entire archive to replace the V7.9 files. Do not remove the Railway Volume, `/data/contracts.db`, or change the variables. After Railway deploys, run `/sklad` once to place the new panel at the bottom.
