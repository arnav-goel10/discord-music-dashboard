import asyncio
import wavelink
import os
import discord

async def main():
    client = discord.Client(intents=discord.Intents.default())
    node = wavelink.Node(uri=os.getenv("LAVALINK_URI", "http://localhost:2333"), password=os.getenv("LAVALINK_PASSWORD", "youshallnotpass"))
    await wavelink.Pool.connect(nodes=[node], client=client, cache_capacity=100)
    
    tracks = await wavelink.Playable.search("ytmsearch: The Weeknd Blinding Lights")
    if tracks:
        vid = tracks[0].identifier
        print(f"VID: {vid}")
        
        # Youtube Music Radio
        url = f"https://music.youtube.com/watch?v={vid}&list=RDAMVM{vid}"
        try:
            res = await wavelink.Playable.search(url)
            print("Result:", type(res), getattr(res, "name", "NoName"), len(getattr(res, "tracks", [])))
        except Exception as e:
            print("Error YTM:", e)
            
        # Standard Youtube Mix
        url2 = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
        try:
            res = await wavelink.Playable.search(url2)
            print("Result2:", type(res), getattr(res, "name", "NoName"), len(getattr(res, "tracks", [])))
            for t in res.tracks[:3]:
                print(t.author, t.title)
        except Exception as e:
            print("Error YT:", e)
        
    await wavelink.Pool.close()
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
