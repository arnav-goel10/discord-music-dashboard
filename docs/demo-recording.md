# Recording script: dashboard session clip

A 45-60 second capture of one `/music` session for the README (GIF) and any
future portfolio embed. No narration; the dashboard is the story.

## Ground rules

- A private test server with a neutral name; no member list on screen, no DMs,
  Do Not Disturb on. A second throwaway account in the voice channel makes the
  session real without exposing anyone.
- Bot + Lavalink running locally (`./start_lavalink.sh`, then `python main.py`).
- Pick well-known, uncontroversial tracks; the queue is on camera.
- Discord in light-agnostic default theme, 1280×800 window, 60fps.

## Scene list

| # | Seconds | On screen | The point it proves |
| --- | --- | --- | --- |
| 1 | 0-8 | `/music` in a text channel; the dashboard message renders | one message is the whole control surface |
| 2 | 8-20 | Add a Spotify playlist link; watch tracks resolve into the queue in the background | ingestion + background resolution |
| 3 | 20-32 | Hit the 🎤 button; live-synced lyrics scroll in time with playback | the hardest feature, visibly working |
| 4 | 32-45 | Skip twice quickly; open the queue and show the AI DJ's refreshed upcoming slate | the DJ reacts to skips, the slate changes |
| 5 | 45-55 | Toggle an audio effect, nudge volume; progress bar keeps updating live | effects and the self-healing refresh loop |
| 6 | 55-60 | End card: "one dashboard message · live lyrics · AI DJ · 39 tests in CI" | summary line |

## After the take

- Export MP4 plus a GIF of scenes 3-4 (`assets/dashboard-demo.gif`), embed the
  GIF at the top of the README under the badge.
- Scrub every frame once for usernames, server names, and notification pops.
