import asyncio
import wavelink
import os
import discord
from discord.ext import commands

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.event
async def on_ready():
    node = wavelink.Node(uri=os.getenv("LAVALINK_URI", "http://localhost:2333"), password=os.getenv("LAVALINK_PASSWORD", "youshallnotpass"))
    await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100)
    print("Node ready")
    tracks = await wavelink.Playable.search("ytmsearch: The Weeknd Blinding Lights")
    if tracks:
        track = tracks[0]
        vid = track.identifier
        print(f"Found track: {track.title} by {track.author} (ID: {vid})")
        url = f"https://music.youtube.com/watch?v={vid}&list=RDAMVM{vid}"
        print(f"Loading radio playlist: {url}")
        try:
            playlist = await wavelink.Playable.search(url)
            if isinstance(playlist, wavelink.Playlist):
                print(f"Found playlist with {len(playlist.tracks)} tracks!")
            else:
                print("Not a playlist:", playlist)
        except Exception as e:
            print("Error loading playlist:", e)
    await bot.close()

bot.run(os.getenv("DISCORD_TOKEN"))
