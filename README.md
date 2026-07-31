# Cratebot

<img src="cratebot-avatar.png" alt="Cratebot" width="160">

A long-running Discord bot that watches nominated channels for music links
(Spotify primarily, other services best-effort), resolves them to Spotify
tracks, and files them into a nominated Spotify playlist - deduplicated,
with a retrospective `/scan` backfill command and Discord reactions as
feedback. One bot process can serve several Discord servers at once, each
with its own monitored channels and its own target playlist.

## Hard constraints (design these around, don't discover them at 3am)

1. **The Spotify account that authorises the bot must OWN the target
   playlist.** Being a collaborator is not enough - Spotify's API rejects
   `POST /playlists/{id}/items` from anyone but the owner, even though the
   Spotify apps themselves allow collaborators to add tracks. Use a
   dedicated Spotify account as the bot's identity, create the playlist
   under that account, and mark it `collaborative: true` so humans can
   still add/remove via the Spotify apps. `/playlist set` refuses to
   configure a playlist the authorising account doesn't own.
2. **The Spotify app stays in Development Mode permanently** (Extended
   Quota Mode needs a registered business with 250k+ MAU). That means:
   only 5 users can ever authorise this app, and **the account must keep
   an active Spotify Premium subscription** - if it lapses, the bot stops
   working entirely until it's renewed. One authorised account can own
   several playlists though, so serving multiple Discord servers doesn't
   need extra Spotify accounts or OAuth setup - just another playlist per
   server. Dev Mode's quota is shared across all of them.
3. **Passive link monitoring requires the `MESSAGE_CONTENT` privileged
   Discord intent**, both for the live Gateway connection and for
   backfill via the REST API. Toggle it on in the Developer Portal
   (Bot -> Privileged Gateway Intents) *and* make sure the bot process
   requests it (already done in `discordbot/bot.py`) - both have to agree
   or the Gateway connection fails outright. If you ever can't get the
   intent granted, the message **context menu command** ("Add to
   playlist") still works without it.

## One-time setup

You (not the bot) need to do the following before the bot can do anything
useful. None of this can be automated from inside this repo - it requires
your own logins.

### 1. Spotify

1. Log into (or create) the Spotify account that should own the bot's
   playlist. It needs an active **Premium** subscription.
2. Create an app at https://developer.spotify.com/dashboard. Note the
   **Client ID** and **Client Secret**.
3. Add a Redirect URI matching `SPOTIFY_REDIRECT_URI` below exactly
   (`http://127.0.0.1:8888/callback` - use `127.0.0.1`, not `localhost`;
   Spotify's insecure-redirect-URI rules reject bare `localhost` in some
   configurations).
4. Create the playlist (or designate an existing one) under this same
   account. Mark it collaborative if humans should be able to add/remove
   tracks via the Spotify client too.

### 2. Discord

1. Create an application at https://discord.com/developers/applications,
   add a Bot user, and copy the **bot token**.
2. Under Bot -> Privileged Gateway Intents, enable **Message Content**.
3. Under OAuth2 -> URL Generator, pick scopes `bot` and
   `applications.commands`, and permissions: View Channels, Read Message
   History, Send Messages, Add Reactions, Embed Links, Use External
   Emojis (Create Public Threads only if you use threads). Use the
   generated URL to invite the bot to your server.
4. Note your server's **guild ID** and the **channel ID(s)** you want
   monitored (enable Developer Mode in Discord to copy IDs). Repeat steps
   1-4 (playlist + invite + IDs) for each additional server you want this
   bot instance to serve.

### 3. This repo

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# generate an encryption key for the token store:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the result into TOKEN_ENCRYPTION_KEY in .env, fill in the Discord
# bot token and Spotify client ID/secret, and add one GUILDS entry per
# server from steps 1-2 above (see .env.example for the shape).
```

Run the test suite to sanity-check the install:

```bash
pytest
```

### 4. Authorise Spotify (one-time, run on the host - not in Docker)

This spins up a localhost HTTP listener to catch the OAuth redirect, so it
needs to run somewhere you have a browser (or can port-forward
`127.0.0.1:8888`):

```bash
cratebot-auth
```

Log in with the Spotify account from step 1. Tokens are encrypted
(`TOKEN_ENCRYPTION_KEY`) and stored in the SQLite database
(`DATABASE_PATH`, default `./data/cratebot.db`) - the same file the bot
itself will use, so run this against the same `DATABASE_PATH` you'll run
the bot with (if you're about to run via Docker, run this step with
`DATABASE_PATH=./data/cratebot.db` locally first, then mount `./data` as
the container's volume).

### 5. Run it

Either directly:

```bash
cratebot
```

or via Docker:

```bash
docker compose up -d --build
```

Once it's running, you can change a server's target playlist at runtime
without editing `.env` or restarting, via `/playlist set` in that server:

```
/playlist set https://open.spotify.com/playlist/<id>
```

This validates ownership per constraint 1 above and refuses with a clear
error if the playlist isn't owned by the authorising account. It overrides
that one server's `GUILDS` entry until changed again.

## Configuration reference

See `.env.example` for the full list with defaults. Notable ones:

| Variable | Meaning |
|---|---|
| `GUILDS` | JSON array, one entry per Discord server: `guild_id`, `monitored_channel_ids` (threads under these channels are included automatically), `spotify_playlist_id`, and `admin_role_ids` (in addition to Manage Messages, allowed to run `/scan` and `/playlist set` in that server) |
| `EXPAND_ALBUMS` | If true, album links add every track (respecting the 100-per-request batch cap) |
| `EXPAND_EPISODES` | If true, podcast episode links are added (best-effort, no metadata lookup) |
| `MATCH_THRESHOLD` | Minimum fuzzy-match confidence (0-1) before auto-adding a cross-platform match; below this, a human is asked to pick via buttons |
| `SCAN_STRATEGY` | `search` (server-side, fast, default) or `history` (exhaustive walk, used automatically as a fallback) |
| `ODESLI_API_KEY` | Optional; raises Odesli's ~10 req/min unkeyed limit. Request one from developers@song.link for heavy backfills |

## Commands

| Command | Behaviour |
|---|---|
| `/status` | This server's playlist name/URL/size, local track count, scan cursors, match threshold, current strategy |
| `/scan` | Backfill: `days`, `since_message_id`, `limit`, `dry_run` (default true), `channel`. Admin-gated. |
| `/playlist set <url>` | Change this server's target playlist; validates ownership. Admin-gated. |
| **Add to playlist** (message context menu) | Manual one-off add; works even without the Message Content intent |

Reactions on monitored messages: ✅ added · ↩️ duplicate · ❓ ambiguous
(with a button picker) · ⚠️ failed. No reaction at all means the link was
silently skipped - playlist links (never auto-expanded), or album/episode
links while `EXPAND_ALBUMS`/`EXPAND_EPISODES` are off.

## Testing

```bash
pytest
```

Covers (all mocked, no live credentials required): link parsing for every
URL form Spotify/Discord/YouTube produce (bare/`intl-`/URI-scheme/
short-link/query noise), fuzzy matching and ISRC short-circuiting, all
three dedupe layers, Spotify client behaviour against
200/403/404/429-with-`Retry-After`/429-with-`QUOTA_EXCEEDED` responses,
the token-refresh state machine (including the "no new refresh_token in
the response" branch and `invalid_grant` -> re-auth-required), Odesli
caching/circuit-breaker/timeout handling, YouTube oEmbed title cleanup,
and both scan strategies (202-then-200 indexing retry, nested-array
flattening, offset-cap windowing, resumable history cursors).

Manual checklist before trusting this against a real shared playlist:
post a plain track link, an `intl-` link, a `spotify:` URI, a
`spotify.link` short link, an album, a playlist, a YouTube link, a
duplicate, a message edited to add a link, and a forwarded message. Then
run `/scan` with `dry_run:true` over a known range and check the summary
before ever running it live.
