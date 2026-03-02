import discord
from discord.ext import commands
import wavelink
import os
import asyncio
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        self._startup_sync_done = False
        
        super().__init__(
            command_prefix="!", 
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        # Load the music cog
        await self.load_extension("cogs.music")
        
        # Connect to Lavalink Server
        nodes = [wavelink.Node(uri="http://127.0.0.1:2333", password=LAVALINK_PASSWORD)]
        try:
            await wavelink.Pool.connect(nodes=nodes, client=self, cache_capacity=100)
        except Exception as e:
            print(f"WARNING: Could not connect to Lavalink at startup: {e}")
            print("Music commands will be unavailable until Lavalink comes online.")
        
        # Sync slash commands globally after cogs are fully loaded
        try:
            synced = await self.tree.sync()
            print(f"Global Sync. Registered {len(synced)} commands.")
        except Exception as e:
            print(f"Failed to sync commands globally. Error: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        # Avoid repeating things on every reconnect.
        if not self._startup_sync_done:
            self._startup_sync_done = True
            print("Startup sync logic complete!")
        print("Bot is fully ready and commands are live!")

    async def close(self):
        try:
            from utils.resolver import resolver
            await resolver.close()
        except Exception as e:
            logging.warning(f"Resolver cleanup failed: {e}")
        await super().close()

async def main():
    # Setup discord logging so we can see when it hits the 429 Rate Limit and how long it waits
    discord.utils.setup_logging(level=logging.INFO)
    print("Enabled Dynamic Rate-Limit Logging. If Discord hits the bot with a 429 Timeout, the logger will read the exact retry_after payload and dynamically wait!")
    
    if not TOKEN or TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
        print("ERROR: Please set your DISCORD_TOKEN in the .env file")
        return
        
    bot = MusicBot()
    
    # Start the bot
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
