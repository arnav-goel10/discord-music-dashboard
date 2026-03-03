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
AUTO_SHARDING_ENABLED = (os.getenv("AUTO_SHARDING_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"})

_BotBase = commands.AutoShardedBot if AUTO_SHARDING_ENABLED else commands.Bot


class MusicBot(_BotBase):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        self._startup_sync_done = False

        bot_kwargs: dict[str, object] = {
            "command_prefix": "!",
            "intents": intents,
            "help_command": None,
        }
        if AUTO_SHARDING_ENABLED:
            shard_count_raw = (os.getenv("DISCORD_SHARD_COUNT") or "").strip()
            if shard_count_raw:
                try:
                    shard_count = max(1, int(shard_count_raw))
                    bot_kwargs["shard_count"] = shard_count
                except ValueError:
                    print(f"WARNING: Invalid DISCORD_SHARD_COUNT='{shard_count_raw}', ignoring.")

            shard_ids_raw = (os.getenv("DISCORD_SHARD_IDS") or "").strip()
            if shard_ids_raw:
                try:
                    shard_ids = [
                        int(part.strip())
                        for part in shard_ids_raw.split(",")
                        if part.strip()
                    ]
                    if shard_ids:
                        bot_kwargs["shard_ids"] = shard_ids
                except ValueError:
                    print(f"WARNING: Invalid DISCORD_SHARD_IDS='{shard_ids_raw}', ignoring.")

        super().__init__(**bot_kwargs)

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
            if AUTO_SHARDING_ENABLED:
                print(f"Auto sharding enabled. Active shard count: {self.shard_count}")
            print("Startup sync logic complete!")
        print("Bot is fully ready and commands are live!")

    async def close(self):
        try:
            from utils.resolver import resolver
            await resolver.close()
        except Exception as e:
            logging.warning(f"Resolver cleanup failed: {e}")
        try:
            from utils.lyrics import lyrics_resolver
            await lyrics_resolver.close()
        except Exception as e:
            logging.warning(f"Lyrics resolver cleanup failed: {e}")
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
