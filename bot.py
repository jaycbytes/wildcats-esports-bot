import os
import re
import json
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands
from github import Github, GithubException

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "jaycbytes/wildcats-esports")
EVENTS_JSON_PATH = os.getenv("EVENTS_JSON_PATH", "public/data/events.json")
IMAGES_PATH = os.getenv("IMAGES_PATH", "public/images/events")
ALLOWED_ROLE = os.getenv("ALLOWED_ROLE", "Officer")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

gh = Github(GITHUB_TOKEN)
repo = gh.get_repo(GITHUB_REPO)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def format_date(date_str: str) -> str:
    """'2026-04-15' → 'April 15, 2026'"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def is_officer(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return discord.utils.get(interaction.user.roles, name=ALLOWED_ROLE) is not None


def get_events_file():
    """Returns (data_dict, file_sha). Raises GithubException on failure."""
    file = repo.get_contents(EVENTS_JSON_PATH)
    data = json.loads(file.decoded_content)
    return data, file.sha


def save_events_file(data: dict, sha: str, commit_msg: str):
    updated = json.dumps(data, indent=2) + "\n"
    repo.update_file(
        path=EVENTS_JSON_PATH,
        message=commit_msg,
        content=updated,
        sha=sha,
    )


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} | Slash commands synced")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CheckFailure):
        msg = f"You need the **{ALLOWED_ROLE}** role to use this command."
    else:
        msg = f"Something went wrong: `{error}`"

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# /add-event
# ---------------------------------------------------------------------------

@bot.tree.command(name="add-event", description="Add a new event to the Wildcats Esports website")
@app_commands.describe(
    title="Event title",
    date="Date in YYYY-MM-DD format  (e.g. 2026-04-15)",
    location="Where the event takes place",
    description="Short description of the event",
    status="Past or upcoming?",
    image="Optional JPG/PNG image to upload for the event card",
    gallery_id="Optional gallery slug (auto-generated from title if omitted)",
)
@app_commands.choices(status=[
    app_commands.Choice(name="Past", value="past"),
    app_commands.Choice(name="Upcoming", value="upcoming"),
])
@app_commands.check(is_officer)
async def add_event(
    interaction: discord.Interaction,
    title: str,
    date: str,
    location: str,
    description: str,
    status: app_commands.Choice[str],
    image: discord.Attachment = None,
    gallery_id: str = None,
):
    await interaction.response.defer(ephemeral=True)

    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await interaction.followup.send(
            "Invalid date. Use `YYYY-MM-DD` format, e.g. `2026-04-15`.", ephemeral=True
        )
        return

    # Validate attachment is an image
    if image and not image.content_type.startswith("image/"):
        await interaction.followup.send(
            "Attachment must be an image (JPG or PNG).", ephemeral=True
        )
        return

    event_id = slugify(title)
    status_val = status.value

    # Read current events.json
    try:
        data, sha = get_events_file()
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to read `events.json` from GitHub: `{e}`", ephemeral=True
        )
        return

    # Guard against duplicate IDs
    existing_ids = [e["id"] for e in data.get("events", [])]
    if event_id in existing_ids:
        await interaction.followup.send(
            f"An event with id `{event_id}` already exists. "
            "Use a different title or provide a custom `gallery_id`.",
            ephemeral=True,
        )
        return

    # Upload image to GitHub repo if one was attached
    image_path = None
    if image:
        github_image_path = f"{IMAGES_PATH}/{image.filename}"

        async with aiohttp.ClientSession() as session:
            async with session.get(image.url) as resp:
                if resp.status != 200:
                    await interaction.followup.send(
                        "Could not download the image from Discord. Try again.", ephemeral=True
                    )
                    return
                img_bytes = await resp.read()

        try:
            try:
                existing = repo.get_contents(github_image_path)
                repo.update_file(
                    path=github_image_path,
                    message=f"feat: upload image for '{title}' via Discord bot",
                    content=img_bytes,
                    sha=existing.sha,
                )
            except GithubException:
                repo.create_file(
                    path=github_image_path,
                    message=f"feat: upload image for '{title}' via Discord bot",
                    content=img_bytes,
                )
        except GithubException as e:
            await interaction.followup.send(
                f"Failed to upload image to GitHub: `{e}`", ephemeral=True
            )
            return

        image_path = f"/images/events/{image.filename}"

    # Resolve gallery_id
    resolved_gallery_id = gallery_id or (event_id if image_path else None)

    # Build the new event object
    new_event = {
        "id": event_id,
        "title": title,
        "date": date,
        "dateDisplay": format_date(date),
        "location": location,
        "description": description,
        "status": status_val,
        "badgeClass": "badge-past" if status_val == "past" else "badge-upcoming",
        "badgeText": "Past Event" if status_val == "past" else "Upcoming",
        "dateColor": "text-energy-yellow" if status_val == "past" else "text-baby-blue",
        "image": image_path or "/images/events/placeholder.jpg",
        "imageAlt": title,
        "galleryId": resolved_gallery_id,
    }

    data["events"].append(new_event)

    try:
        save_events_file(data, sha, f"feat: add event '{title}' via Discord bot")
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to write `events.json` to GitHub: `{e}`", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Event Added",
        description=f"**{title}** has been added to the website.",
        color=0x82D6FF,
    )
    embed.add_field(name="Date", value=format_date(date), inline=True)
    embed.add_field(name="Location", value=location, inline=True)
    embed.add_field(name="Status", value=status_val.capitalize(), inline=True)
    embed.add_field(name="Event ID", value=f"`{event_id}`", inline=False)
    if image_path:
        embed.add_field(name="Image", value=f"`{image_path}`", inline=False)
    else:
        embed.set_footer(text="No image attached — using placeholder. Upload one manually later.")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /remove-event
# ---------------------------------------------------------------------------

@bot.tree.command(name="remove-event", description="Remove an event from the Wildcats Esports website")
@app_commands.describe(event_id="The event ID to remove (start typing to search)")
@app_commands.check(is_officer)
async def remove_event(interaction: discord.Interaction, event_id: str):
    await interaction.response.defer(ephemeral=True)

    try:
        data, sha = get_events_file()
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to read `events.json` from GitHub: `{e}`", ephemeral=True
        )
        return

    events = data.get("events", [])
    target = next((e for e in events if e["id"] == event_id), None)

    if target is None:
        valid = ", ".join(f"`{e['id']}`" for e in events)
        await interaction.followup.send(
            f"No event found with id `{event_id}`.\nValid IDs: {valid or 'none'}",
            ephemeral=True,
        )
        return

    data["events"] = [e for e in events if e["id"] != event_id]

    try:
        save_events_file(data, sha, f"feat: remove event '{event_id}' via Discord bot")
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to write `events.json` to GitHub: `{e}`", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Event Removed",
        description=f"**{target['title']}** has been removed from the website.",
        color=0xF7D760,
    )
    embed.add_field(name="Event ID", value=f"`{event_id}`", inline=True)
    embed.add_field(name="Date", value=target.get("dateDisplay", target.get("date")), inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


@remove_event.autocomplete("event_id")
async def remove_event_autocomplete(
    interaction: discord.Interaction, current: str
):
    try:
        data, _ = get_events_file()
        events = data.get("events", [])
    except Exception:
        return []

    matches = [
        e for e in events
        if current.lower() in e["id"].lower() or current.lower() in e["title"].lower()
    ]
    return [
        app_commands.Choice(name=f"{e['id']} — {e['title']}", value=e["id"])
        for e in matches
    ][:25]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

bot.run(DISCORD_TOKEN)
