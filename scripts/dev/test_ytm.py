import asyncio
import wavelink
import discord

client = discord.Client(intents=discord.Intents.default())

@client.event
async def on_ready():
    # Setup wavelink node
    node = wavelink.Node(uri="http://localhost:2333", password="youshallnotpass")
    await wavelink.Pool.connect(nodes=[node], client=client, cache_capacity=100)
    
    print("Node ready")
    # Search for a track
    tracks = await wavelink.Playable.search("ytmsearch: The Weeknd Blinding Lights")
    if text := tracks:
        track = text[0]
        vid = track.identifier
        print(f"Found track: {track.title} by {track.author} (ID: {vid})")
        
        # Try radio playlist
        url = f"https://music.youtube.com/watch?v={vid}&list=RDAMVM{vid}"
        print(f"Loading radio playlist: {url}")
        
        try:
            playlist = await wavelink.Playable.search(url)
            if isinstance(playlist, wavelink.Playlist):
                print(f"Found playlist with {len(playlist.tracks)} tracks!")
                for t in playlist.tracks[:5]:
                    print(f" - {t.title} by {t.author}")
            else:
                print("Not a playlist:", playlist)
        except Exception as e:
            print("Error loading playlist:", e)
            
    await client.close()

import os
client.run(os.environ["DISCORD_TOKEN"])
