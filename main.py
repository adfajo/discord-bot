import discord
from discord.ext import tasks, commands
import requests
import os
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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
WATCH_LIST: List[str] = []  # single watch list; prefer English title, fallback to Romaji
NEW_EPISODE_TIMES = []

# discord.py v2+ requires explicit intents
intents = discord.Intents.default()
intents.message_content = True  # Required for prefix commands like !addwatch
# Enable additional intents only if needed, e.g., message content:
# intents.message_content = True  # requires enabling in the bot portal as well

announced_episodes = set()
scheduler = AsyncIOScheduler()

bot = commands.Bot(command_prefix="!", intents=intents)

# --- AniList lookups ---
ANILIST_URL = "https://graphql.anilist.co"

@dataclass
class AddWatchInfo:
  title: str
  updates_at: Optional[int]
  episode: Optional[int]
  note: Optional[str] = None

def parse_addwatch_result(result_obj, fallback_title: str) -> Optional[AddWatchInfo]:
  """Normalize get_anime_by_english_name result to AddWatchInfo.

  - (media, updates_at, episode) -> resolved romaji/english title with times
  - str (error note) -> fallback title with note
  - None -> not found
  """
  if isinstance(result_obj, tuple) and len(result_obj) == 3:
    media, updates_at, episode = result_obj
    titles = media.get("title") or {}
    resolved = titles.get("romaji") or titles.get("english") or fallback_title
    return AddWatchInfo(resolved, updates_at, episode, None)
  if isinstance(result_obj, str):
    return AddWatchInfo(fallback_title, None, None, result_obj)
  return None

def format_airing_info(updates_at: Optional[int], episode: Optional[int]) -> str:
  if updates_at is None:
    return ""
  try:
    dt = datetime.datetime.fromtimestamp(updates_at)
    when = dt.strftime("%Y-%m-%d %H:%M:%S")
    if episode:
      return f" Next episode {episode} airs at {when}."
    return f" Next episode airs at {when}."
  except Exception:
    return ""

def get_anime_by_english_name(english_name: str) -> Optional[Dict[str, Any]]:
  """Return the first Media whose English title matches the provided name (case-insensitive).

  Falls back to searching and then filtering for exact English match.
  """
  query = """
  query ($search: String) {
    Page(page: 1, perPage: 10) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { english romaji native }
      format
      status
      episodes
      siteUrl
      nextAiringEpisode {
        airingAt
        episode
      }
    }
    }
  }
  """
  try:
    resp = requests.post(ANILIST_URL, json={"query": query, "variables": {"search": english_name}})
    data = resp.json()
    items = data.get("data", {}).get("Page", {}).get("media", [])
    lowered = english_name.strip().lower()
    updates_at = data.get("data", {}).get("Page", {}).get("media", [])[0].get("nextAiringEpisode", {}).get("airingAt") if items else None
    if (updates_at is None):
       return "No upcoming episodes found."
    episode = data.get("data", {}).get("Page", {}).get("media", [])[0].get("nextAiringEpisode", {}).get("episode") if items else None
    for m in items:
      en = (m.get("title") or {}).get("english")
      if en and en.strip().lower() == lowered:
        return m, updates_at, episode
    # If no exact English match, return None
    return None
  except Exception:
    return None

# --- Top-5 search and UI selection ---
def search_anime_top5(query_text: str) -> List[Dict[str, Any]]:
  query = """
  query ($search: String) {
    Page(page: 1, perPage: 5) {
      media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
        id
        title { english romaji native }
        format
        status
        episodes
        siteUrl
        nextAiringEpisode { airingAt episode }
      }
    }
  }
  """
  try:
    resp = requests.post(ANILIST_URL, json={"query": query, "variables": {"search": query_text}})
    data = resp.json()
    return data.get("data", {}).get("Page", {}).get("media", []) or []
  except Exception:
    return []

def _format_anime_title(item: Dict[str, Any]) -> str:
  t = item.get("title") or {}
  en = t.get("english")
  ro = t.get("romaji")
  if en and ro and en != ro:
    return f"{en} (romaji: {ro})"
  return en or ro or "<unknown title>"

def build_anime_embed(item: Dict[str, Any], index: int, total: int) -> discord.Embed:
  t = item.get("title") or {}
  en = t.get("english") or "<no english>"
  ro = t.get("romaji") or "<no romaji>"
  url = item.get("siteUrl") or ""
  fmt = item.get("format") or ""
  status = item.get("status") or ""
  eps = item.get("episodes")
  nae = item.get("nextAiringEpisode") or {}
  next_ep = nae.get("episode")
  next_at = nae.get("airingAt")

  title_line = _format_anime_title(item)
  embed = discord.Embed(title=f"Result {index}/{total}: {title_line}", color=discord.Color.blurple())
  if url:
    embed.url = url
  embed.add_field(name="English", value=en, inline=True)
  embed.add_field(name="Romaji", value=ro, inline=True)
  meta = f"Format: {fmt or 'N/A'}\nStatus: {status or 'N/A'}\nEpisodes: {eps or 'N/A'}"
  embed.add_field(name="Info", value=meta, inline=False)
  next_text = format_airing_info(int(next_at), next_ep) if next_at is not None else ""
  if next_text:
    embed.add_field(name="Next Airing", value=next_text.strip(), inline=False)
  return embed

async def check_and_announce_episode(title: str, episode: int):
    """Announce a specific episode at its scheduled time."""
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print("⚠️ Channel not found — check CHANNEL_ID")
        return
    
    unique_key = f"{title}-{episode}"
    if unique_key not in announced_episodes:
        announced_episodes.add(unique_key)
        await channel.send(f"🎬 New episode alert! **{title}** - Episode {episode} is now airing!")

def schedule_episode_from_watchlist(title: str, airing_at: int, episode: int):
    """Schedule an announcement based on airing time to repeat weekly."""
    dt = datetime.datetime.fromtimestamp(airing_at)
    job_id = f"{title}-weekly"
    
    # Schedule to run every week at the same day and time
    scheduler.add_job(
        check_and_announce_episode,
        trigger=CronTrigger(
            day_of_week=dt.weekday(),  # 0=Monday, 6=Sunday
            hour=dt.hour,
            minute=dt.minute
        ),
        args=[title, episode],
        id=job_id,
        replace_existing=True
    )
    print(f"📅 Scheduled {title} for every {dt.strftime('%A')} at {dt.strftime('%H:%M')}")

class AnimePager(discord.ui.View):
  def __init__(self, user_id: int, results: List[Dict[str, Any]]):
    super().__init__(timeout=60)
    self.user_id = user_id
    self.results = results
    self.index = 0
    self.message: Optional[discord.Message] = None
    self.selected: Optional[Dict[str, Any]] = None

  async def interaction_check(self, interaction: discord.Interaction) -> bool:
    return interaction.user.id == self.user_id

  async def on_timeout(self) -> None:
    for child in self.children:
      if isinstance(child, discord.ui.Button):
        child.disabled = True
    if self.message:
      try:
        await self.message.edit(view=self)
      except Exception:
        pass

  def _update_buttons_state(self):
    # Disable prev on first, next on last
    for child in self.children:
      if isinstance(child, discord.ui.Button) and child.custom_id:
        if child.custom_id == "prev":
          child.disabled = self.index <= 0
        elif child.custom_id == "next":
          child.disabled = self.index >= len(self.results) - 1

  async def _refresh(self, interaction: discord.Interaction):
    self._update_buttons_state()
    embed = build_anime_embed(self.results[self.index], self.index + 1, len(self.results))
    await interaction.response.edit_message(embed=embed, view=self)

  @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, custom_id="prev")
  async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    if self.index > 0:
      self.index -= 1
    await self._refresh(interaction)

  @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="next")
  async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    if self.index < len(self.results) - 1:
      self.index += 1
    await self._refresh(interaction)

  @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary, custom_id="confirm")
  async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    self.selected = self.results[self.index]
    # Disable all buttons
    for child in self.children:
      if isinstance(child, discord.ui.Button):
        child.disabled = True
    await interaction.response.edit_message(view=self)
    self.stop()

  @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel")
  async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    self.selected = None
    for child in self.children:
      if isinstance(child, discord.ui.Button):
        child.disabled = True
    await interaction.response.edit_message(view=self)
    self.stop()

@bot.command(name="anus")
async def anus(ctx):
    await ctx.send("Hei, my name is George Biden. I am fat and have a big anus.")

@bot.command(name="watchlist")
async def watchlist(ctx):
    """Display the user's watchlist."""
    if not WATCH_LIST:
        await ctx.send("❌ Your watchlist is empty.")
        return

    watchlist_embed = discord.Embed(title="Your Watchlist", color=discord.Color.blue())
    for item in WATCH_LIST:
        watchlist_embed.add_field(name=item, value="\u200b", inline=False)

    await ctx.send(embed=watchlist_embed)

@bot.command(name="addwatch")
async def add_watch(ctx, *, title: str):
  """Let the user choose among the top 5 AniList matches, with pagination, then add it.

  We store both English and Romaji (if available):
  - WATCH_LIST_ENGLISH: for display/confirmation
  - WATCH_LIST_ROMANJI: for broadcast matching
  """
  results = search_anime_top5(title)
  if not results:
    await ctx.send(f"❌ No results for '{title}'.")
    return

  view = AnimePager(ctx.author.id, results)
  embed = build_anime_embed(results[0], 1, len(results))
  msg = await ctx.send(content="Select the correct anime (Prev/Next, then Confirm)", embed=embed, view=view)
  view.message = msg
  await view.wait()

  if view.selected is None:
    await ctx.send("❌ Selection cancelled or timed out.")
    try:
      await msg.delete()
    except Exception:
      pass
    return
  
  try:
    await msg.delete()
  except Exception:
    pass

  item = view.selected
  t = item.get("title") or {}
  en = t.get("english")
  ro = t.get("romaji")
  display_title = en or ro or title

  # Add a single entry, prefer English title; fallback to Romaji or the input
  chosen = en or ro or title
  added = False
  if chosen not in WATCH_LIST:
    WATCH_LIST.append(chosen)
    added = True

  # Schedule based on next airing info if available
  nae = item.get("nextAiringEpisode") or {}
  airing_at = nae.get("airingAt")
  episode = nae.get("episode")
  
  if airing_at is not None and episode is not None:
    schedule_episode_from_watchlist(display_title, airing_at, episode)

  if added:
    when_text = ""
    if airing_at is not None:
      when_text = format_airing_info(airing_at, episode)
    await ctx.send(f"✅ Added **{display_title}** to your watch list.{when_text}")
  else:
    await ctx.send(f"⚠️ **{display_title}** is already in your watch list.")

# keep track of which episodes we’ve already announced
announced_episodes = set()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    if not scheduler.running:
        scheduler.start()

if __name__ == "__main__":
  bot.run(BOT_TOKEN)
