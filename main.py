import discord
from discord.ext import tasks, commands
import requests
import os
from typing import Optional
try:
  from dotenv import load_dotenv
except ImportError:
  load_dotenv = None  # optional; we'll handle if not installed
import datetime

# === CONFIGURATION ===
# Load environment variables from a .env file if python-dotenv is installed
if load_dotenv is not None:
  load_dotenv()

def _env_required(name: str) -> str:
  val: Optional[str] = os.getenv(name)
  if not val:
    raise RuntimeError(f"Missing required environment variable: {name}")
  return val

def _env_int_required(name: str) -> int:
  raw = _env_required(name)
  try:
    return int(raw)
  except ValueError:
    raise RuntimeError(f"Environment variable {name} must be an integer, got: {raw}")

BOT_TOKEN = _env_required("BOT_TOKEN")  # Discord bot token
CHANNEL_ID = _env_int_required("CHANNEL_ID")  # Discord channel ID (integer)
WATCH_LIST = ["My Hero Academia", "Gachiakuta"]  # titles you want to track

# discord.py v2+ requires explicit intents
intents = discord.Intents.default()
# Enable additional intents only if needed, e.g., message content:
# intents.message_content = True  # requires enabling in the bot portal as well

bot = commands.Bot(command_prefix="!", intents=intents)

# keep track of which episodes we’ve already announced
announced_episodes = set()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    check_new_episodes.start()

@tasks.loop(minutes=30)
async def check_new_episodes():
    query = '''
    query {
      Page(page: 1, perPage: 20) {
        airingSchedules(sort: TIME_DESC) {
          media {
            id
            title {
              romaji
            }
          }
          episode
          airingAt
        }
      }
    }
    '''
    response = requests.post("https://graphql.anilist.co", json={"query": query}).json()
    now = datetime.datetime.now().timestamp()

    for airing in response["data"]["Page"]["airingSchedules"]:
        title = airing["media"]["title"]["romaji"]
        if not any(watch.lower() in title.lower() for watch in WATCH_LIST):
            continue  # skip if not in your watchlist

        episode = airing["episode"]
        airing_time = airing["airingAt"]
        unique_key = f"{title}-{episode}"

        # Check if it aired in the last hour and hasn’t been announced
        if airing_time < now < airing_time + 3600 and unique_key not in announced_episodes:
            announced_episodes.add(unique_key)
            channel = bot.get_channel(CHANNEL_ID)
            if channel:
                await channel.send(f"🎬 New episode out! **{title}** - Episode {episode}")
            else:
                print("⚠️ Channel not found — check CHANNEL_ID")

if __name__ == "__main__":
  bot.run(BOT_TOKEN)
