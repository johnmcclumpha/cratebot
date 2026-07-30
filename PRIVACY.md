# Privacy

Cratebot reads Discord messages in the channels its operator configures, in
order to find music links and file them into a Spotify playlist. This
document describes what it stores, for how long, and why.

## What is stored

Cratebot deliberately stores the minimum needed to operate, and **never
stores raw message text**. Message content is parsed in memory to extract
URLs; only the extracted data below is persisted, in a local SQLite
database (`data/cratebot.db` by default):

| Table | What's stored | Why |
|---|---|---|
| `processed_links` | Discord message ID + normalised link | Idempotency: so edits/re-scans don't re-add the same link twice |
| `added_tracks` | Spotify track ID, URI, title, artist, Discord message ID, Discord user ID, timestamp | Dedupe, `/status` reporting, and attributing who added what |
| `suppressed_tracks` | Spotify track ID, timestamp, reason | Remembering tracks a human deliberately removed via Spotify, so the bot never silently re-adds them |
| `scan_cursors` | Discord channel ID, last-seen message ID | Resuming an interrupted backfill without restarting from scratch |
| `odesli_cache` | Source URL, resolved Spotify URL, title, artist | Avoiding repeat calls to the (rate-limited) Odesli API for the same link |
| `oauth_tokens` | Encrypted Spotify access/refresh tokens | Keeping the bot authorised without re-running the OAuth flow |
| `runtime_config` | Small key/value overrides (e.g. the active playlist ID) | Runtime configuration set via `/playlist set` |

Discord user IDs and channel IDs are numeric snowflakes, not usernames -
they are not directly readable without also having access to the Discord
guild, but they are still personal data under most definitions and should
be treated accordingly.

## What is never stored

- Raw message content or embeds beyond the URLs extracted from them.
- Discord usernames, avatars, or any profile data beyond a numeric user ID.
- Spotify account details beyond what's needed to authorise API calls
  (the bot's own OAuth tokens, encrypted at rest).

## Retention

Data in the tables above is kept indefinitely by default, since it exists
to make the bot's own behaviour (dedupe, attribution, resumable scans)
correct over the playlist's lifetime. There is no automatic expiry.

## Deleting your data

Server admins can purge a specific user's rows with:

```sql
DELETE FROM added_tracks WHERE requester_id = '<discord_user_id>';
```

against `data/cratebot.db` (stop the bot first, or use a SQLite client that
supports concurrent readers under WAL mode). This removes the attribution
record; it does not remove the track from the Spotify playlist itself.

A dedicated `/forget` slash command purging a user's rows on request is
planned but not yet implemented in v1 - see the README's known-limitations
section.

## Discord Developer Portal requirements

If this bot is ever exposed beyond a single private server:

- Set a Privacy Policy URL and Terms of Service URL in the
  [Discord Developer Portal](https://discord.com/developers/applications)
  for the bot's application.
- The `MESSAGE_CONTENT` privileged intent requires Discord approval once
  the app is visible to enough unique users (see README for the current
  threshold, which has moved over time), and approved apps must reapply
  annually.
