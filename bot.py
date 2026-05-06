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


async def upload_image(session: aiohttp.ClientSession, attachment: discord.Attachment, label: str) -> str:
    """Download attachment and upload to GitHub. Returns relative web path."""
    async with session.get(attachment.url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Could not download image `{attachment.filename}` from Discord.")
        img_bytes = await resp.read()

    github_path = f"{IMAGES_PATH}/{attachment.filename}"
    try:
        try:
            existing = repo.get_contents(github_path)
            repo.update_file(
                path=github_path,
                message=f"feat: upload image for '{label}' via Discord bot",
                content=img_bytes,
                sha=existing.sha,
            )
        except GithubException:
            repo.create_file(
                path=github_path,
                message=f"feat: upload image for '{label}' via Discord bot",
                content=img_bytes,
            )
    except GithubException as e:
        raise RuntimeError(f"Failed to upload `{attachment.filename}` to GitHub: `{e}`")

    return f"/images/events/{attachment.filename}"


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
    msg = f"Something went wrong: `{error}`"

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# /add-event
# ---------------------------------------------------------------------------

@bot.tree.command(name="add-event", description="Add a new event to the Wildcats Esports website")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    title="Event title",
    date="Date in YYYY-MM-DD format  (e.g. 2026-04-15)",
    location="Where the event takes place",
    description="Short description of the event",
    status="Past or upcoming?",
    image1="Primary event image (used as the card thumbnail)",
    image2="Additional gallery image",
    image3="Additional gallery image",
    image4="Additional gallery image",
    image5="Additional gallery image",
    gallery_id="Optional gallery slug (auto-generated from title if omitted)",
)
@app_commands.choices(status=[
    app_commands.Choice(name="Past", value="past"),
    app_commands.Choice(name="Upcoming", value="upcoming"),
])
async def add_event(
    interaction: discord.Interaction,
    title: str,
    date: str,
    location: str,
    description: str,
    status: app_commands.Choice[str],
    image1: discord.Attachment = None,
    image2: discord.Attachment = None,
    image3: discord.Attachment = None,
    image4: discord.Attachment = None,
    image5: discord.Attachment = None,
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

    # Collect and validate all provided images
    raw_images = [img for img in [image1, image2, image3, image4, image5] if img is not None]
    for img in raw_images:
        if not img.content_type.startswith("image/"):
            await interaction.followup.send(
                f"`{img.filename}` is not an image (JPG or PNG required).", ephemeral=True
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

    # Upload all images
    gallery_paths = []
    if raw_images:
        async with aiohttp.ClientSession() as session:
            for img in raw_images:
                try:
                    path = await upload_image(session, img, title)
                    gallery_paths.append(path)
                except RuntimeError as e:
                    await interaction.followup.send(str(e), ephemeral=True)
                    return

    # Resolve gallery_id
    resolved_gallery_id = gallery_id or (event_id if gallery_paths else None)

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
        "image": gallery_paths[0] if gallery_paths else "/images/events/placeholder.jpg",
        "imageAlt": title,
        "galleryId": resolved_gallery_id,
        "gallery": gallery_paths,
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
    if gallery_paths:
        embed.add_field(
            name=f"Images ({len(gallery_paths)})",
            value="\n".join(f"`{p}`" for p in gallery_paths),
            inline=False,
        )
    else:
        embed.set_footer(text="No images attached — using placeholder. Use /add-images to add photos later.")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /add-images
# ---------------------------------------------------------------------------

@bot.tree.command(name="add-images", description="Add images to an existing event's gallery")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    event_id="The event to add images to (start typing to search)",
    image1="Gallery image to add",
    image2="Gallery image to add",
    image3="Gallery image to add",
    image4="Gallery image to add",
    image5="Gallery image to add",
)
async def add_images(
    interaction: discord.Interaction,
    event_id: str,
    image1: discord.Attachment = None,
    image2: discord.Attachment = None,
    image3: discord.Attachment = None,
    image4: discord.Attachment = None,
    image5: discord.Attachment = None,
):
    await interaction.response.defer(ephemeral=True)

    # Collect and validate provided images
    raw_images = [img for img in [image1, image2, image3, image4, image5] if img is not None]
    if not raw_images:
        await interaction.followup.send("Please attach at least one image.", ephemeral=True)
        return

    for img in raw_images:
        if not img.content_type.startswith("image/"):
            await interaction.followup.send(
                f"`{img.filename}` is not an image (JPG or PNG required).", ephemeral=True
            )
            return

    # Read events.json
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

    # Upload images
    new_paths = []
    async with aiohttp.ClientSession() as session:
        for img in raw_images:
            try:
                path = await upload_image(session, img, target["title"])
                new_paths.append(path)
            except RuntimeError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return

    # Append to gallery array (create it if missing for older events)
    if "gallery" not in target:
        target["gallery"] = []
    target["gallery"].extend(new_paths)

    # Set galleryId if it was null (event now has images)
    if not target.get("galleryId"):
        target["galleryId"] = target["id"]

    try:
        save_events_file(data, sha, f"feat: add {len(new_paths)} image(s) to '{event_id}' via Discord bot")
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to write `events.json` to GitHub: `{e}`", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Images Added",
        description=f"Added **{len(new_paths)}** image(s) to **{target['title']}**.",
        color=0x82D6FF,
    )
    embed.add_field(
        name="New Images",
        value="\n".join(f"`{p}`" for p in new_paths),
        inline=False,
    )
    embed.add_field(
        name="Total Gallery Images",
        value=str(len(target["gallery"])),
        inline=True,
    )

    await interaction.followup.send(embed=embed, ephemeral=True)


@add_images.autocomplete("event_id")
async def add_images_autocomplete(
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
# /remove-event
# ---------------------------------------------------------------------------

@bot.tree.command(name="remove-event", description="Remove an event from the Wildcats Esports website")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(event_id="The event ID to remove (start typing to search)")
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
# /list-events
# ---------------------------------------------------------------------------

@bot.tree.command(name="list-events", description="List all events on the Wildcats Esports website")
@app_commands.default_permissions(administrator=True)
async def list_events(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        data, _ = get_events_file()
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to read `events.json` from GitHub: `{e}`", ephemeral=True
        )
        return

    events = data.get("events", [])
    if not events:
        await interaction.followup.send("No events found.", ephemeral=True)
        return

    sorted_events = sorted(events, key=lambda e: e.get("date", ""), reverse=True)
    upcoming = [e for e in sorted_events if e.get("status") == "upcoming"]
    past = [e for e in sorted_events if e.get("status") == "past"]

    embed = discord.Embed(
        title="Wildcats Esports Events",
        description=f"**{len(events)}** total event(s)",
        color=0x82D6FF,
    )

    if upcoming:
        lines = [
            f"`{e['id']}` — {e['title']} ({e.get('dateDisplay', e.get('date'))})"
            for e in upcoming
        ]
        embed.add_field(name=f"Upcoming ({len(upcoming)})", value="\n".join(lines), inline=False)

    if past:
        lines = [
            f"`{e['id']}` — {e['title']} ({e.get('dateDisplay', e.get('date'))})"
            for e in past
        ]
        embed.add_field(name=f"Past ({len(past)})", value="\n".join(lines), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /update-event-status
# ---------------------------------------------------------------------------

@bot.tree.command(name="update-event-status", description="Change an event's status between upcoming and past")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    event_id="The event to update (start typing to search)",
    status="New status for the event",
)
@app_commands.choices(status=[
    app_commands.Choice(name="Past", value="past"),
    app_commands.Choice(name="Upcoming", value="upcoming"),
])
async def update_event_status(
    interaction: discord.Interaction,
    event_id: str,
    status: app_commands.Choice[str],
):
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

    old_status = target.get("status", "unknown")
    new_status = status.value

    if old_status == new_status:
        await interaction.followup.send(
            f"**{target['title']}** is already marked as `{new_status}`. No changes made.",
            ephemeral=True,
        )
        return

    target["status"] = new_status
    target["badgeClass"] = "badge-past" if new_status == "past" else "badge-upcoming"
    target["badgeText"] = "Past Event" if new_status == "past" else "Upcoming"
    target["dateColor"] = "text-energy-yellow" if new_status == "past" else "text-baby-blue"

    try:
        save_events_file(data, sha, f"feat: update event '{event_id}' status to '{new_status}' via Discord bot")
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to write `events.json` to GitHub: `{e}`", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Event Status Updated",
        description=f"**{target['title']}** status changed.",
        color=0x82D6FF if new_status == "upcoming" else 0xF7D760,
    )
    embed.add_field(name="Event ID", value=f"`{event_id}`", inline=True)
    embed.add_field(name="Old Status", value=old_status.capitalize(), inline=True)
    embed.add_field(name="New Status", value=new_status.capitalize(), inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


@update_event_status.autocomplete("event_id")
async def update_status_autocomplete(
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
        app_commands.Choice(name=f"{e['id']} — {e['title']} [{e.get('status', '?')}]", value=e["id"])
        for e in matches
    ][:25]


# ---------------------------------------------------------------------------
# /edit-event
# ---------------------------------------------------------------------------

@bot.tree.command(name="edit-event", description="Edit the details of an existing event")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    event_id="The event to edit (start typing to search)",
    title="New title (leave blank to keep current)",
    date="New date in YYYY-MM-DD format (leave blank to keep current)",
    location="New location (leave blank to keep current)",
    description="New description (leave blank to keep current)",
)
async def edit_event(
    interaction: discord.Interaction,
    event_id: str,
    title: str = None,
    date: str = None,
    location: str = None,
    description: str = None,
):
    await interaction.response.defer(ephemeral=True)

    if not any([title, date, location, description]):
        await interaction.followup.send(
            "Please provide at least one field to update.", ephemeral=True
        )
        return

    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            await interaction.followup.send(
                "Invalid date. Use `YYYY-MM-DD` format, e.g. `2026-04-15`.", ephemeral=True
            )
            return

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

    changes = []

    if title:
        target["title"] = title
        target["imageAlt"] = title
        changes.append(f"**Title** → `{title}`")

    if date:
        target["date"] = date
        target["dateDisplay"] = format_date(date)
        changes.append(f"**Date** → `{target['dateDisplay']}`")

    if location:
        target["location"] = location
        changes.append(f"**Location** → `{location}`")

    if description:
        target["description"] = description
        changes.append("**Description** updated")

    try:
        save_events_file(data, sha, f"feat: edit event '{event_id}' via Discord bot")
    except GithubException as e:
        await interaction.followup.send(
            f"Failed to write `events.json` to GitHub: `{e}`", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Event Updated",
        description=f"**{target['title']}** has been updated.",
        color=0x82D6FF,
    )
    embed.add_field(name="Event ID", value=f"`{event_id}`", inline=False)
    embed.add_field(name="Changes", value="\n".join(changes), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@edit_event.autocomplete("event_id")
async def edit_event_autocomplete(
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
