import asyncio
import datetime
import os
import re
import json
from urllib.parse import urlparse, parse_qs

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View, button


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("TOKEN")
DB_PATH = "clips.db"

# $0.60 per 1,000 views
CPM_RATE = 0.60

# How often pending clips are checked
TRACK_INTERVAL_SECONDS = 300  # 5 minutes

# HTTP timeout
HTTP_TIMEOUT = 20

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.datetime.utcnow().isoformat()


def calculate_earnings(views):
    return round((int(views or 0) / 1000) * CPM_RATE, 2)


def detect_platform(url):
    try:
        host = urlparse(url).netloc.lower()

        if "tiktok.com" in host:
            return "TikTok"

        if "instagram.com" in host:
            return "Instagram"

        if "youtube.com" in host or "youtu.be" in host:
            return "YouTube"

    except Exception:
        pass

    return "Unknown"


def extract_youtube_video_id(url):
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()

        if "youtu.be" in host:
            return parsed.path.strip("/").split("/")[0]

        if "youtube.com" in host:
            query = parse_qs(parsed.query)

            if query.get("v"):
                return query["v"][0]

            path_parts = parsed.path.strip("/").split("/")

            if len(path_parts) >= 2:
                if path_parts[0] in ("shorts", "embed", "live"):
                    return path_parts[1]

    except Exception:
        pass

    return None


def extract_tiktok_video_id(url):
    """
    Attempts to extract a TikTok numeric video ID.

    Examples:
    https://www.tiktok.com/@user/video/123456789
    https://www.tiktok.com/@user/video/123456789?...
    """

    match = re.search(r"/video/(\d+)", url)

    if match:
        return match.group(1)

    return None


# ============================================================
# DATABASE
# ============================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                linked_account TEXT,
                pending REAL DEFAULT 0,
                total_paid REAL DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                clip_url TEXT,
                submitted_at TEXT,
                status TEXT DEFAULT 'pending',
                earnings REAL DEFAULT 0,
                views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                platform TEXT DEFAULT 'Unknown',
                last_checked TEXT,
                stats_error TEXT
            )
        """)

        # ----------------------------------------------------
        # MIGRATION FOR OLD DATABASES
        # ----------------------------------------------------

        async with db.execute("PRAGMA table_info(submissions)") as cursor:
            columns = await cursor.fetchall()

        existing_columns = {row[1] for row in columns}

        migrations = {
            "views": "ALTER TABLE submissions ADD COLUMN views INTEGER DEFAULT 0",
            "likes": "ALTER TABLE submissions ADD COLUMN likes INTEGER DEFAULT 0",
            "platform": "ALTER TABLE submissions ADD COLUMN platform TEXT DEFAULT 'Unknown'",
            "last_checked": "ALTER TABLE submissions ADD COLUMN last_checked TEXT",
            "stats_error": "ALTER TABLE submissions ADD COLUMN stats_error TEXT",
        }

        for column, sql in migrations.items():
            if column not in existing_columns:
                try:
                    await db.execute(sql)
                    print(f"Added database column: {column}")
                except Exception as e:
                    print(f"Migration error for {column}: {e}")

        await db.commit()

    print("Database initialized.")


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        ) as cursor:

            row = await cursor.fetchone()

        if row is None:

            await db.execute(
                "INSERT INTO users (user_id) VALUES (?)",
                (user_id,)
            )

            await db.commit()

            return {
                "user_id": user_id,
                "linked_account": None,
                "pending": 0.0,
                "total_paid": 0.0
            }

        return {
            "user_id": row[0],
            "linked_account": row[1],
            "pending": row[2] or 0.0,
            "total_paid": row[3] or 0.0
        }


# ============================================================
# STATS FETCHING
# ============================================================

async def fetch_html(session, url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

    async with session.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=True
    ) as response:

        if response.status != 200:
            raise Exception(f"HTTP {response.status}")

        return await response.text(errors="ignore")


def find_number_from_text(text, patterns):
    """
    Searches HTML/text for common view/like patterns.
    """

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            try:
                value = match.group(1)

                # Remove commas
                value = value.replace(",", "")

                # Handle simple abbreviations
                if value.lower().endswith("k"):
                    return int(float(value[:-1]) * 1000)

                if value.lower().endswith("m"):
                    return int(float(value[:-1]) * 1000000)

                if value.lower().endswith("b"):
                    return int(float(value[:-1]) * 1000000000)

                return int(float(value))

            except Exception:
                continue

    return None


# ============================================================
# YOUTUBE
# ============================================================

async def fetch_youtube_stats(session, url):
    video_id = extract_youtube_video_id(url)

    if not video_id:
        raise Exception("Could not identify YouTube video ID.")

    # --------------------------------------------------------
    # Official YouTube Data API
    # --------------------------------------------------------

    if YOUTUBE_API_KEY:

        api_url = (
            "https://www.googleapis.com/youtube/v3/videos"
            f"?part=statistics&id={video_id}"
            f"&key={YOUTUBE_API_KEY}"
        )

        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

        async with session.get(
            api_url,
            timeout=timeout
        ) as response:

            data = await response.json(content_type=None)

            if response.status != 200:
                message = data.get("error", {}).get(
                    "message",
                    f"HTTP {response.status}"
                )
                raise Exception(message)

            items = data.get("items", [])

            if not items:
                raise Exception("YouTube video was not found.")

            stats = items[0].get("statistics", {})

            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))

            return views, likes

    # --------------------------------------------------------
    # Fallback page extraction
    # --------------------------------------------------------

    html = await fetch_html(session, url)

    views = find_number_from_text(
        html,
        [
            r'"viewCount":"(\d+)"',
            r'"viewCount":(\d+)',
            r'"views":"([\d,]+)"',
            r'"viewCountText".*?"simpleText":"([\d,]+)',
        ]
    )

    likes = find_number_from_text(
        html,
        [
            r'"likeCount":"(\d+)"',
            r'"likeCount":(\d+)',
        ]
    )

    if views is None:
        raise Exception(
            "Could not read YouTube views. "
            "Add YOUTUBE_API_KEY to Railway."
        )

    return views, likes or 0


# ============================================================
# TIKTOK
# ============================================================

async def fetch_tiktok_stats(session, url):
    html = await fetch_html(session, url)

    # TikTok pages often expose values in JSON embedded in HTML.
    views = find_number_from_text(
        html,
        [
            r'"playCount":(\d+)',
            r'"playCount":"(\d+)"',
            r'"viewCount":(\d+)',
            r'"viewCount":"(\d+)"',
            r'"play_count":(\d+)',
            r'"view_count":(\d+)',
        ]
    )

    likes = find_number_from_text(
        html,
        [
            r'"diggCount":(\d+)',
            r'"diggCount":"(\d+)"',
            r'"likeCount":(\d+)',
            r'"likeCount":"(\d+)"',
            r'"like_count":(\d+)',
            r'"like_count":"(\d+)"',
        ]
    )

    if views is None:
        raise Exception(
            "TikTok did not expose the video statistics to the bot."
        )

    return views, likes or 0


# ============================================================
# INSTAGRAM
# ============================================================

async def fetch_instagram_stats(session, url):
    html = await fetch_html(session, url)

    # Instagram can expose public media information inside
    # embedded JSON depending on the page/account.
    views = find_number_from_text(
        html,
        [
            r'"video_view_count":(\d+)',
            r'"video_view_count":"(\d+)"',
            r'"play_count":(\d+)',
            r'"play_count":"(\d+)"',
            r'"view_count":(\d+)',
            r'"view_count":"(\d+)"',
            r'"video_play_count":(\d+)',
            r'"video_play_count":"(\d+)"',
        ]
    )

    likes = find_number_from_text(
        html,
        [
            r'"like_count":(\d+)',
            r'"like_count":"(\d+)"',
            r'"likes":\{"count":(\d+)',
            r'"edge_media_preview_like".*?"count":(\d+)',
        ]
    )

    # Instagram photos/reels may not expose a separate view count.
    # For video content, if views aren't available but likes are,
    # keep views unavailable rather than falsely treating likes as views.

    if views is None:
        raise Exception(
            "Instagram did not expose the video statistics to the bot."
        )

    return views, likes or 0


# ============================================================
# UNIVERSAL STATS FETCHER
# ============================================================

async def fetch_clip_stats(url):
    platform = detect_platform(url)

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

    connector = aiohttp.TCPConnector(
        limit=10,
        ssl=False
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector
    ) as session:

        if platform == "YouTube":
            views, likes = await fetch_youtube_stats(
                session,
                url
            )

        elif platform == "TikTok":
            views, likes = await fetch_tiktok_stats(
                session,
                url
            )

        elif platform == "Instagram":
            views, likes = await fetch_instagram_stats(
                session,
                url
            )

        else:
            raise Exception(
                "Only TikTok, Instagram and YouTube links are supported."
            )

        return {
            "platform": platform,
            "views": int(views),
            "likes": int(likes),
            "earnings": calculate_earnings(views)
        }


# ============================================================
# UPDATE ONE SUBMISSION
# ============================================================

async def update_submission_stats(submission_id, url):
    try:

        stats = await fetch_clip_stats(url)

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute(
                """
                UPDATE submissions
                SET views = ?,
                    likes = ?,
                    platform = ?,
                    earnings = ?,
                    last_checked = ?,
                    stats_error = NULL
                WHERE id = ?
                """,
                (
                    stats["views"],
                    stats["likes"],
                    stats["platform"],
                    stats["earnings"],
                    utc_now(),
                    submission_id
                )
            )

            await db.commit()

        print(
            f"[STATS] Submission #{submission_id}: "
            f"{stats['platform']} | "
            f"{stats['views']} views | "
            f"{stats['likes']} likes | "
            f"${stats['earnings']:.2f}"
        )

        return stats

    except Exception as e:

        error = str(e)[:500]

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute(
                """
                UPDATE submissions
                SET last_checked = ?,
                    stats_error = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    error,
                    submission_id
                )
            )

            await db.commit()

        print(
            f"[STATS ERROR] Submission #{submission_id}: {error}"
        )

        return None


# ============================================================
# BACKGROUND TRACKER
# ============================================================

@tasks.loop(seconds=TRACK_INTERVAL_SECONDS)
async def track_pending_clips():

    try:

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                """
                SELECT id, clip_url
                FROM submissions
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT 50
                """
            ) as cursor:

                rows = await cursor.fetchall()

        if not rows:
            return

        print(
            f"[TRACKER] Checking {len(rows)} pending clip(s)..."
        )

        for submission_id, clip_url in rows:

            await update_submission_stats(
                submission_id,
                clip_url
            )

            # Small delay to avoid hammering platforms
            await asyncio.sleep(2)

    except Exception as e:
        print(f"[TRACKER ERROR] {e}")


@track_pending_clips.before_loop
async def before_track_pending_clips():

    await bot.wait_until_ready()


# ============================================================
# USER PANEL
# ============================================================

class ClipPanel(View):

    def __init__(self):
        super().__init__(timeout=None)

    # --------------------------------------------------------
    # SUBMIT VIDEO
    # --------------------------------------------------------

    @button(
        label="Submit Video",
        style=discord.ButtonStyle.primary,
        emoji="📩",
        custom_id="submit_video",
        row=0
    )
    async def submit_video(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        user = await get_user(interaction.user.id)

        if not user["linked_account"]:

            return await interaction.response.send_message(
                "❌ You must link an account first! "
                "Click the **Accounts** button.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "Please paste the **full link** of your clip now.\n"
            "⏳ You have **60 seconds**.",
            ephemeral=True
        )

        def check(message):

            return (
                message.author.id == interaction.user.id
                and message.channel.id == interaction.channel.id
            )

        try:

            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=60.0
            )

        except asyncio.TimeoutError:

            return await interaction.followup.send(
                "⏰ Timed out. Please try again.",
                ephemeral=True
            )

        clip_url = msg.content.strip()

        if not re.match(
            r"^https?://",
            clip_url,
            re.IGNORECASE
        ):

            return await interaction.followup.send(
                "❌ That doesn't look like a valid link.",
                ephemeral=True
            )

        platform = detect_platform(clip_url)

        if platform == "Unknown":

            return await interaction.followup.send(
                "❌ Please submit a TikTok, Instagram, "
                "or YouTube link.",
                ephemeral=True
            )

        # ----------------------------------------------------
        # INSERT FIRST
        # ----------------------------------------------------

        async with aiosqlite.connect(DB_PATH) as db:

            cursor = await db.execute(
                """
                INSERT INTO submissions
                (
                    user_id,
                    clip_url,
                    submitted_at,
                    status,
                    earnings,
                    views,
                    likes,
                    platform,
                    last_checked,
                    stats_error
                )
                VALUES (?, ?, ?, 'pending', 0, 0, 0, ?, NULL, NULL)
                """,
                (
                    interaction.user.id,
                    clip_url,
                    utc_now(),
                    platform
                )
            )

            submission_id = cursor.lastrowid

            await db.commit()

        # ----------------------------------------------------
        # IMMEDIATELY FETCH STATS
        # ----------------------------------------------------

        stats = await update_submission_stats(
            submission_id,
            clip_url
        )

        if stats:

            await interaction.followup.send(
                (
                    f"✅ **Clip submitted successfully!**\n\n"
                    f"🆔 Submission: **#{submission_id}**\n"
                    f"📱 Platform: **{stats['platform']}**\n"
                    f"👁️ Views: **{stats['views']:,}**\n"
                    f"❤️ Likes: **{stats['likes']:,}**\n"
                    f"💰 Estimated earnings: "
                    f"**${stats['earnings']:.2f}**\n\n"
                    f"⏳ Your clip is now being tracked."
                ),
                ephemeral=True
            )

        else:

            await interaction.followup.send(
                (
                    f"✅ **Clip submitted successfully!**\n\n"
                    f"🆔 Submission: **#{submission_id}**\n"
                    f"📱 Platform: **{platform}**\n\n"
                    f"⚠️ I could not read the current views/likes yet.\n"
                    f"I will try again automatically while the "
                    f"clip is pending."
                ),
                ephemeral=True
            )

        try:
            await msg.delete()
        except Exception:
            pass

    # --------------------------------------------------------
    # ACCOUNTS
    # --------------------------------------------------------

    @button(
        label="Accounts",
        style=discord.ButtonStyle.secondary,
        emoji="🪪",
        custom_id="accounts",
        row=0
    )
    async def accounts(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        user = await get_user(interaction.user.id)

        if user["linked_account"]:

            return await interaction.response.send_message(
                (
                    f"✅ Currently linked: "
                    f"**{user['linked_account']}**\n\n"
                    f"Reply with a new one to change it."
                ),
                ephemeral=True
            )

        await interaction.response.send_message(
            "Reply with your account "
            "(example: `TikTok @username`):",
            ephemeral=True
        )

        def check(message):

            return (
                message.author.id == interaction.user.id
                and message.channel.id == interaction.channel.id
            )

        try:

            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=60.0
            )

        except asyncio.TimeoutError:

            return await interaction.followup.send(
                "⏰ Timed out.",
                ephemeral=True
            )

        account = msg.content.strip()

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute(
                """
                UPDATE users
                SET linked_account = ?
                WHERE user_id = ?
                """,
                (
                    account,
                    interaction.user.id
                )
            )

            await db.commit()

        await interaction.followup.send(
            f"✅ Account linked: **{account}**",
            ephemeral=True
        )

    # --------------------------------------------------------
    # SAY HI
    # --------------------------------------------------------

    @button(
        label="Say Hi",
        style=discord.ButtonStyle.secondary,
        emoji="👋",
        custom_id="say_hi",
        row=1
    )
    async def say_hi(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_message(
            f"Hello {interaction.user.mention}! 👋",
            ephemeral=True
        )

    # --------------------------------------------------------
    # CHECK EARNINGS
    # --------------------------------------------------------

    @button(
        label="Check Earnings",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="check_earnings",
        row=1
    )
    async def check_earnings(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        user = await get_user(interaction.user.id)

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(views), 0),
                    COALESCE(SUM(likes), 0),
                    COALESCE(SUM(earnings), 0)
                FROM submissions
                WHERE user_id = ?
                AND status = 'approved'
                """,
                (interaction.user.id,)
            ) as cursor:

                row = await cursor.fetchone()

        approved_clips = row[0]
        views = row[1]
        likes = row[2]
        earnings = row[3]

        await interaction.response.send_message(
            (
                f"**Your Earnings**\n\n"
                f"💰 Pending: **${user['pending']:.2f}**\n"
                f"✅ Total Paid: **${user['total_paid']:.2f}**\n"
                f"📋 Approved Clips: **{approved_clips}**\n"
                f"👁️ Approved Views: **{views:,}**\n"
                f"❤️ Approved Likes: **{likes:,}**\n"
                f"📈 Approved Earnings: **${earnings:.2f}**"
            ),
            ephemeral=True
        )

    # --------------------------------------------------------
    # SUBMISSION HISTORY
    # --------------------------------------------------------

    @button(
        label="Submission History",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="submission_history",
        row=2
    )
    async def submission_history(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                """
                SELECT
                    id,
                    clip_url,
                    platform,
                    status,
                    views,
                    likes,
                    earnings
                FROM submissions
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 10
                """,
                (interaction.user.id,)
            ) as cursor:

                rows = await cursor.fetchall()

        if not rows:

            return await interaction.response.send_message(
                "You have no submissions yet.",
                ephemeral=True
            )

        text = "**Your last submissions:**\n\n"

        for (
            sub_id,
            url,
            platform,
            status,
            views,
            likes,
            earnings
        ) in rows:

            text += (
                f"**#{sub_id} — {platform}**\n"
                f"👁️ {views:,} views | "
                f"❤️ {likes:,} likes\n"
                f"Status: **{status}** | "
                f"${earnings:.2f}\n"
                f"[Open Clip]({url})\n\n"
            )

        await interaction.response.send_message(
            text[:1900],
            ephemeral=True
        )

    # --------------------------------------------------------
    # PAYMENT
    # --------------------------------------------------------

    @button(
        label="Payment",
        style=discord.ButtonStyle.secondary,
        emoji="💳",
        custom_id="payment",
        row=2
    )
    async def payment(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_message(
            "Reply with your payment details "
            "(example: `PayPal email@example.com`):",
            ephemeral=True
        )


# ============================================================
# APPROVE MODAL
# ============================================================

class ApproveModal(Modal, title="Approve Clip"):

    def __init__(
        self,
        submission_id: int,
        user_id: int
    ):

        super().__init__()

        self.submission_id = submission_id
        self.user_id = user_id

        self.confirm = TextInput(
            label="Type APPROVE to confirm",
            placeholder="APPROVE",
            required=True,
            max_length=20
        )

        self.add_item(self.confirm)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if self.confirm.value.strip().upper() != "APPROVE":

            return await interaction.response.send_message(
                "❌ Approval cancelled.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                """
                SELECT
                    user_id,
                    views,
                    likes,
                    earnings,
                    clip_url,
                    platform
                FROM submissions
                WHERE id = ?
                AND status = 'pending'
                """,
                (self.submission_id,)
            ) as cursor:

                row = await cursor.fetchone()

            if not row:

                return await interaction.response.send_message(
                    "❌ Submission not found or already handled.",
                    ephemeral=True
                )

            user_id = row[0]
            views = row[1] or 0
            likes = row[2] or 0
            clip_url = row[4]
            platform = row[5]

            # Recalculate from latest stored views.
            amount = calculate_earnings(views)

            await db.execute(
                """
                UPDATE submissions
                SET status = 'approved',
                    earnings = ?
                WHERE id = ?
                """,
                (
                    amount,
                    self.submission_id
                )
            )

            await db.execute(
                """
                UPDATE users
                SET pending = pending + ?
                WHERE user_id = ?
                """,
                (
                    amount,
                    user_id
                )
            )

            await db.commit()

        await interaction.response.send_message(
            (
                f"✅ **Clip Approved!**\n\n"
                f"🆔 Submission: **#{self.submission_id}**\n"
                f"📱 Platform: **{platform}**\n"
                f"👁️ Views: **{views:,}**\n"
                f"❤️ Likes: **{likes:,}**\n"
                f"💰 Added: **${amount:.2f}**\n"
                f"👤 User: <@{user_id}>"
            ),
            ephemeral=True
        )


# ============================================================
# ADMIN PANEL
# ============================================================

class AdminView(View):

    def __init__(self):
        super().__init__(timeout=120)

    # --------------------------------------------------------
    # VIEW PENDING
    # --------------------------------------------------------

    @button(
        label="View Pending Clips",
        style=discord.ButtonStyle.primary,
        emoji="📥"
    )
    async def view_pending(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                """
                SELECT
                    id,
                    user_id,
                    clip_url,
                    submitted_at,
                    platform,
                    views,
                    likes,
                    earnings,
                    last_checked,
                    stats_error
                FROM submissions
                WHERE status = 'pending'
                ORDER BY id DESC
                LIMIT 15
                """
            ) as cursor:

                rows = await cursor.fetchall()

        if not rows:

            return await interaction.response.send_message(
                "No pending clips.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="📥 Pending Clips",
            description=(
                f"Tracking rate: **${CPM_RATE:.2f} / 1,000 views**\n"
                f"Stats refresh approximately every "
                f"**{TRACK_INTERVAL_SECONDS // 60} minutes**."
            ),
            color=discord.Color.orange()
        )

        for row in rows:

            (
                sub_id,
                user_id,
                url,
                submitted_at,
                platform,
                views,
                likes,
                earnings,
                last_checked,
                stats_error
            ) = row

            value = (
                f"**Platform:** {platform}\n"
                f"👁️ **Views:** {views:,}\n"
                f"❤️ **Likes:** {likes:,}\n"
                f"💰 **Estimated:** ${earnings:.2f}\n"
                f"[Open Clip]({url})\n"
                f"Submitted: {submitted_at[:16].replace('T', ' ')}"
            )

            if last_checked:
                value += (
                    f"\nLast checked: "
                    f"{last_checked[:16].replace('T', ' ')}"
                )

            if stats_error:
                value += (
                    f"\n⚠️ Stats issue: "
                    f"`{stats_error[:150]}`"
                )

            embed.add_field(
                name=f"🆔 #{sub_id} | <@{user_id}>",
                value=value,
                inline=False
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    @button(
        label="Approve Clip",
        style=discord.ButtonStyle.success,
        emoji="✅"
    )
    async def approve_clip(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_message(
            "Please type the **Submission ID** you want to approve:",
            ephemeral=True
        )

        def check(message):

            return (
                message.author.id == interaction.user.id
                and message.channel.id == interaction.channel.id
            )

        try:

            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=30.0
            )

            sub_id = int(msg.content.strip())

        except Exception:

            return await interaction.followup.send(
                "❌ Invalid ID or timed out.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                """
                SELECT user_id
                FROM submissions
                WHERE id = ?
                AND status = 'pending'
                """,
                (sub_id,)
            ) as cursor:

                row = await cursor.fetchone()

        if not row:

            return await interaction.followup.send(
                "❌ Submission not found or already handled.",
                ephemeral=True
            )

        modal = ApproveModal(
            sub_id,
            row[0]
        )

        await interaction.response.send_modal(modal)

    # --------------------------------------------------------
    # REJECT
    # --------------------------------------------------------

    @button(
        label="Reject Clip",
        style=discord.ButtonStyle.danger,
        emoji="❌"
    )
    async def reject_clip(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_message(
            "Type the **Submission ID** to reject:",
            ephemeral=True
        )

        def check(message):

            return (
                message.author.id == interaction.user.id
                and message.channel.id == interaction.channel.id
            )

        try:

            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=30.0
            )

            sub_id = int(msg.content.strip())

        except Exception:

            return await interaction.followup.send(
                "❌ Invalid ID or timed out.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:

            cursor = await db.execute(
                """
                UPDATE submissions
                SET status = 'rejected'
                WHERE id = ?
                AND status = 'pending'
                """,
                (sub_id,)
            )

            await db.commit()

            if cursor.rowcount == 0:

                return await interaction.followup.send(
                    "❌ Submission not found or already handled.",
                    ephemeral=True
                )

        await interaction.followup.send(
            f"✅ Submission `{sub_id}` has been rejected.",
            ephemeral=True
        )

    # --------------------------------------------------------
    # MARK PAID
    # --------------------------------------------------------

    @button(
        label="Mark as Paid",
        style=discord.ButtonStyle.secondary,
        emoji="💸"
    )
    async def mark_paid(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_message(
            (
                "Type the **User ID** "
                "(or mention) to mark their pending "
                "balance as paid:"
            ),
            ephemeral=True
        )

        def check(message):

            return (
                message.author.id == interaction.user.id
                and message.channel.id == interaction.channel.id
            )

        try:

            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=30.0
            )

            content = msg.content.strip()

            if content.startswith("<@"):

                user_id = int(
                    content
                    .replace("<@", "")
                    .replace("!", "")
                    .replace(">", "")
                )

            else:

                user_id = int(content)

        except Exception:

            return await interaction.followup.send(
                "❌ Invalid user.",
                ephemeral=True
            )

        user = await get_user(user_id)

        if user["pending"] <= 0:

            return await interaction.followup.send(
                "This user has no pending balance.",
                ephemeral=True
            )

        amount = user["pending"]

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute(
                """
                UPDATE users
                SET total_paid = total_paid + pending,
                    pending = 0
                WHERE user_id = ?
                """,
                (user_id,)
            )

            await db.commit()

        await interaction.followup.send(
            (
                f"✅ Marked **${amount:.2f}** as paid "
                f"for <@{user_id}>."
            ),
            ephemeral=True
        )


# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(
    name="setup-clips",
    description="Send the public clip submission panel"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def setup_clips(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="why Bebe ??? 😻💦",
        description=(
            "**INSTRUCTIONS**\n\n"
            "Please link an account before submitting clips. "
            "Use the buttons below to manage your account, "
            "submit clips, check earnings, view submission "
            "history, and manage payments.\n\n"
            "**Clips older than 12 hours will not be accepted.**\n\n"
            f"💰 Rate: **${CPM_RATE:.2f} per 1,000 views**\n"
            "📊 Views and likes are checked automatically."
        ),
        color=0x2B2D31
    )

    await interaction.response.send_message(
        embed=embed,
        view=ClipPanel()
    )


@bot.tree.command(
    name="admin",
    description="Open the Admin Panel"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def admin_panel(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="🛠️ Admin Panel",
        description=(
            "Manage clip submissions and payments.\n\n"
            "Pending clips are automatically checked for "
            "updated views and likes."
        ),
        color=discord.Color.dark_grey()
    )

    await interaction.response.send_message(
        embed=embed,
        view=AdminView(),
        ephemeral=True
    )


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():

    print("=" * 50)
    print(f"Logged in as {bot.user}")
    print("=" * 50)

    await init_db()

    # Persistent public panel
    bot.add_view(ClipPanel())

    # Start tracker only once
    if not track_pending_clips.is_running():
        track_pending_clips.start()
        print(
            f"Pending clip tracker started. "
            f"Interval: {TRACK_INTERVAL_SECONDS}s"
        )

    try:

        synced = await bot.tree.sync()

        print(
            f"Synced {len(synced)} command(s)"
        )

    except Exception as e:

        print(
            f"Command sync error: {e}"
        )


# ============================================================
# START BOT
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "TOKEN environment variable is missing."
    )


bot.run(TOKEN)
