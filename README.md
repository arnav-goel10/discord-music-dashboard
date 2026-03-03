# Music Bot (Discord + Lavalink)

A Discord music bot with an interactive `/music` dashboard, queue controls, Spotify playlist support, and Lavalink-powered playback.

## Features

- Interactive slash-command dashboard (`/music`)
- Queue actions: add, view, save playlists, shuffle
- Playback controls: restart, pause/resume, skip, loop
- Volume and audio effects controls
- Lyrics support (dashboard `🎤` button) with live line sync, exact-first matching, provider fallback, and cache
- Spotify playlist ingestion with background resolution and cache
- Lavalink backend with YouTube plugin + lavasrc integration

## Requirements

- Python 3.11+ (3.13 works in this repo)
- Java 17+ (required by Lavalink)
- A Discord bot token
- Optional but recommended: Spotify app credentials for playlist reliability

## 1) Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2) Configure environment

Copy the example file and fill values:

```bash
cp .env.example .env
```

Required:

- `DISCORD_TOKEN`
- `LAVALINK_PASSWORD` (must match `application.yml` lavalink server password)

Spotify (recommended):

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REDIRECT_URI` (must exactly match Spotify Dashboard)

Resolver quality (optional):

- `TRACK_RESOLVER_PER_QUERY_LIMIT`
- `TRACK_RESOLVER_MAX_CANDIDATES`
- `TRACK_RESOLVER_MIN_CACHE_SCORE`
- `TRACK_RESOLVER_MIN_CACHE_MARGIN`

Optional ML reranker:

- Install extra deps: `pip install sentence-transformers torch`
- Set `TRACK_RESOLVER_ML_RERANK_ENABLED=1`
- Optionally override `TRACK_RESOLVER_ML_RERANK_MODEL`

Lyrics (optional):

- `LYRICS_CACHE_ENABLED`
- `LYRICS_CACHE_TTL_SECONDS`
- `LYRICS_PREFER_SYNCED`
- `LYRICS_LRCLIB_EXACT_MIN_SCORE`
- `LYRICS_LRCLIB_SEARCH_MIN_SCORE`
- Optional unofficial Spotify synced-lyrics path:
- `LYRICS_SPOTIFY_UNOFFICIAL_ENABLED`
- `LYRICS_SPOTIFY_MARKET`
- `LYRICS_SPOTIFY_WEB_TOKEN` / `LYRICS_SPOTIFY_WEB_TOKEN_AUTO`
- Note: Spotify lyrics endpoint is unofficial and may change/break without notice.
- Optional Genius fallback: set `GENIUS_ACCESS_TOKEN`

## 3) Spotify OAuth user token (recommended)

If Spotify playlist loading is blocked/limited with app-only credentials, run:

```bash
python scripts/setup_spotify_oauth.py
```

This updates `.env` with:

- `SPOTIFY_USER_ACCESS_TOKEN`
- `SPOTIFY_USER_REFRESH_TOKEN`
- `SPOTIFY_USER_TOKEN_EXPIRES_AT`

## 4) Start Lavalink

Run in terminal 1:

```bash
./start_lavalink.sh
```

Notes:

- Script auto-downloads `Lavalink.jar` if missing.
- It loads `.env` before launch so plugin placeholders can resolve.
- Lavalink listens on `http://127.0.0.1:2333`.

## 5) Start the bot

Run in terminal 2:

```bash
source venv/bin/activate
python main.py
```

On successful startup you should see:

- Lavalink connection success (or warning if Lavalink is offline)
- global slash command sync log
- `Bot is fully ready and commands are live!`

## Commands

- `/music` - open interactive dashboard
- `/help` - show control guide
- `/stop` - stop playback and disconnect

## Project layout

- `main.py` - bot bootstrap and Lavalink node connection
- `cogs/music.py` - command + player logic
- `cogs/ui.py` - dashboard UI components
- `utils/` - matching/resolver utilities
- `data/` - playlist/cache data
- `logs/` - Lavalink logs (`spring.log`)

## Troubleshooting

- Bot says Lavalink unavailable:
  - Ensure `./start_lavalink.sh` is running.
  - Check password match between `.env` (`LAVALINK_PASSWORD`) and `application.yml`.
- Spotify playlist issues / 403:
  - Verify Spotify app credentials.
  - Ensure redirect URI in Spotify dashboard exactly matches `.env`.
  - Run `python scripts/setup_spotify_oauth.py` and restart bot.
- Slash commands not visible immediately:
  - Wait briefly after startup sync and reopen Discord.

## Development notes

- Bytecode, logs, env files, and local jars are ignored via `.gitignore`.
- Quick syntax check:

```bash
python3 -m py_compile main.py cogs/music.py cogs/ui.py
```
