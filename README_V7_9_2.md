# Agosto Contract Bot V7.9.2 — premium eligibility by configured leaderboard roles

- Premium TOP-5 uses the same role IDs stored by `/leaderboard-settings` (one combined leaderboard; any matching role qualifies).
- The selected roles are checked against the member's current Discord server roles when a preview is calculated or a premium period closes.
- Members who do not have one of these roles do not receive weekly top-five premium awards.
- All contract activity still counts towards the weekly family-fund threshold.
- If roles are not configured, the premium preview warns about it, and period closing is blocked until roles are selected.
- Historical premium periods/awards remain unchanged; manually assigned bonuses are not affected.
- Premium previews defer Discord component response while obtaining current member roles.

Update GitHub: replace only `bonuses.py`, or upload all Python files from this full package.
Railway launch remains `python bot.py`; no new variables and no database reset.
