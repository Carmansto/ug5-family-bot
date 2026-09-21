# V7.9.3 — Agosto guild security lock

Security-only patch. No database schema or contract/economy logic changes.

## Added
- Hard allow-list by existing `GUILD_ID`.
- Bot automatically leaves every Discord server whose ID is not `GUILD_ID`.
- `on_guild_join` immediately rejects/leave unauthorized servers.
- Global interaction guard blocks slash-command interactions outside Agosto and in DMs.
- Startup cleanup removes old global application commands; commands are synced only to the configured Agosto guild.
- Startup fails if `GUILD_ID` is missing, preventing an accidentally unlocked deployment.

## Discord Developer Portal
For the strongest protection also set the application to private / disable public installation and remove the public install link.

## Database
No migration. `/data/contracts.db` and the Railway Volume are untouched.
