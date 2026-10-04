import asyncio
import datetime
import os
import random
import re
import string
from urllib.parse import urlparse, parse_qs, unquote

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

# Optional but recommended for YouTube tracking.
# Add YOUTUBE_API_KEY to Railway Variables.
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

# $0.60 per 1,000 views
CPM_RATE = 0.60

# Check pending clips every 5 minutes.
TRACKER_INTERVAL = 300

# User-facing temporary error messages.
ERROR_DELETE_DELAY = 3

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# ============================================================
# TEMPORARY ERROR HELPER
# ============================================================

async def send_temporary_error(
    interaction: discord.Interaction,
    content: str,
    delay: int = ERROR_DELETE_DELAY
):
    """
    Send an ephemeral error message and remove it automatically.
    """

    try:

        if interaction.response.is_done():

            message = await interaction.followup.send(
                content,
                ephemeral=True,
                wait=True
            )

            await asyncio.sleep(delay)

            try:
                await message.delete()
            except Exception:
                pass

        else:

            await interaction.response.send_message(
                content,
                ephemeral=True
            )

            await asyncio.sleep(delay)

            try:
                await interaction.delete_original_response()
            except Exception:
                pass

    except Exception as e:

        print(
            f"[TEMP ERROR] Could not send/delete error: {e}"
        )

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
                payout_status TEXT DEFAULT 'unpaid'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS social_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                profile_url TEXT NOT NULL,
                verification_code TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                added_at TEXT NOT NULL,
                verified_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_methods (
                user_id INTEGER PRIMARY KEY,
                method TEXT,
                details TEXT,
                updated_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT,
                details TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                processed_at TEXT
            )
        """)

        # --------------------------------------------------------
        # SAFE MIGRATIONS
        # --------------------------------------------------------

        try:
            await db.execute(
                "ALTER TABLE submissions ADD COLUMN views INTEGER DEFAULT 0"
            )
        except Exception:
            pass

        try:
            await db.execute(
                "ALTER TABLE submissions ADD COLUMN likes INTEGER DEFAULT 0"
            )
        except Exception:
            pass

        try:
            await db.execute(
                "ALTER TABLE submissions ADD COLUMN payout_status TEXT DEFAULT 'unpaid'"
            )
        except Exception:
            pass

        await db.execute("""
            UPDATE submissions
            SET payout_status = 'unpaid'
            WHERE status = 'approved'
            AND (
                payout_status IS NULL
                OR payout_status = ''
            )
        """)

        await db.commit()


def utc_now():

    return datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat()

# ============================================================
# USER DATABASE HELPERS
# ============================================================

async def ensure_user(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, pending, total_paid)
            VALUES (?, 0, 0)
            """,
            (user_id,)
        )

        await db.commit()


async def get_user(user_id: int):

    await ensure_user(user_id)

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                user_id,
                linked_account,
                pending,
                total_paid
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ) as cursor:

            row = await cursor.fetchone()

    return {
        "user_id": row[0],
        "linked_account": row[1],
        "pending": row[2] or 0,
        "total_paid": row[3] or 0,
    }

# ============================================================
# PAYMENT DATABASE
# ============================================================

async def get_payment_method(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT method, details
            FROM payment_methods
            WHERE user_id = ?
            """,
            (user_id,)
        ) as cursor:

            return await cursor.fetchone()


async def create_payment_record(
    user_id: int,
    amount: float,
    status: str
):

    payment = await get_payment_method(user_id)

    method = payment[0] if payment else None
    details = payment[1] if payment else None

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO payment_transactions
            (
                user_id,
                amount,
                method,
                details,
                status,
                created_at,
                processed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                amount,
                method,
                details,
                status,
                utc_now(),
                utc_now()
            )
        )

        await db.commit()


async def get_payment_history(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                id,
                amount,
                method,
                details,
                status,
                created_at
            FROM payment_transactions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (user_id,)
        ) as cursor:

            return await cursor.fetchall()

# ============================================================
# DASHBOARD DATABASE
# ============================================================

async def get_dashboard_stats(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(likes), 0),
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'approved'
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ),
                COALESCE(SUM(views), 0),
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'approved'
                            THEN earnings
                            ELSE 0
                        END
                    ),
                    0
                )
            FROM submissions
            WHERE user_id = ?
            """,
            (user_id,)
        ) as cursor:

            row = await cursor.fetchone()

    return {
        "submissions": row[0] or 0,
        "likes": row[1] or 0,
        "approved": row[2] or 0,
        "views": row[3] or 0,
        "earnings": row[4] or 0,
    }


async def get_earnings_stats(user_id: int):

    user = await get_user(user_id)

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'failed'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                ),
                COALESCE(
                    SUM(
                        CASE
                            WHEN status = 'rejected'
                            THEN amount
                            ELSE 0
                        END
                    ),
                    0
                )
            FROM payment_transactions
            WHERE user_id = ?
            """,
            (user_id,)
        ) as cursor:

            failed, rejected = await cursor.fetchone()

    return {
        "pending": user["pending"],
        "paid": user["total_paid"],
        "failed": failed or 0,
        "rejected": rejected or 0,
    }

# ============================================================
# PAYOUT DATABASE
# ============================================================

async def get_pending_payout_users():

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                s.user_id,
                COUNT(s.id) AS clip_count,
                COALESCE(SUM(s.views), 0) AS total_views,
                COALESCE(SUM(s.likes), 0) AS total_likes,
                COALESCE(SUM(s.earnings), 0) AS total_earnings
            FROM submissions s
            WHERE s.status = 'approved'
            AND (
                s.payout_status = 'unpaid'
                OR s.payout_status IS NULL
            )
            GROUP BY s.user_id
            HAVING COALESCE(SUM(s.earnings), 0) > 0
            ORDER BY total_earnings DESC
            """
        ) as cursor:

            return await cursor.fetchall()


async def get_user_unpaid_approved_clips(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                id,
                clip_url,
                views,
                likes,
                earnings,
                submitted_at
            FROM submissions
            WHERE user_id = ?
            AND status = 'approved'
            AND (
                payout_status = 'unpaid'
                OR payout_status IS NULL
            )
            ORDER BY id ASC
            """,
            (user_id,)
        ) as cursor:

            return await cursor.fetchall()


async def get_user_all_submissions(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                id,
                clip_url,
                status,
                payout_status,
                views,
                likes,
                earnings,
                submitted_at
            FROM submissions
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user_id,)
        ) as cursor:

            return await cursor.fetchall()

# ============================================================
# GENERAL HELPERS
# ============================================================

def calculate_earnings(views: int):

    return (views / 1000) * CPM_RATE


def generate_verification_code():

    characters = (
        string.ascii_uppercase +
        string.digits
    )

    return "".join(
        random.choices(
            characters,
            k=10
        )
    )


def valid_profile_url(url: str):

    try:

        parsed = urlparse(url)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:

        return False


def platform_matches_url(
    platform: str,
    url: str
):

    try:

        host = (
            urlparse(url)
            .netloc
            .lower()
            .split(":")[0]
        )

        if platform == "TikTok":

            return (
                host == "tiktok.com"
                or host.endswith(".tiktok.com")
            )

        if platform == "Instagram":

            return (
                host == "instagram.com"
                or host.endswith(".instagram.com")
            )

        if platform == "YouTube":

            return (
                host == "youtube.com"
                or host.endswith(".youtube.com")
                or host == "youtu.be"
            )

        return False

    except Exception:

        return False


def detect_platform_from_url(url: str):

    try:

        host = (
            urlparse(url)
            .netloc
            .lower()
            .split(":")[0]
        )

        if (
            host == "tiktok.com"
            or host.endswith(".tiktok.com")
        ):
            return "TikTok"

        if (
            host == "instagram.com"
            or host.endswith(".instagram.com")
        ):
            return "Instagram"

        if (
            host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtu.be"
        ):
            return "YouTube"

    except Exception:
        pass

    return None

# ============================================================
# VIDEO ID HELPERS
# ============================================================

def extract_youtube_video_id(url: str):

    try:

        parsed = urlparse(url)
        host = parsed.netloc.lower()

        if host == "youtu.be":

            video_id = (
                parsed.path
                .strip("/")
                .split("/")[0]
            )

            return video_id or None

        if "youtube.com" in host:

            query = parse_qs(parsed.query)

            if "v" in query:

                return query["v"][0]

            match = re.search(
                r"/shorts/([A-Za-z0-9_-]{6,})",
                parsed.path
            )

            if match:

                return match.group(1)

            match = re.search(
                r"/embed/([A-Za-z0-9_-]{6,})",
                parsed.path
            )

            if match:

                return match.group(1)

    except Exception:
        pass

    return None

# ============================================================
# PUBLIC PAGE HTTP
# ============================================================

TRACKER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,"
        "*/*;q=0.8"
    ),
}

async def fetch_page_html(url: str):

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    try:

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=TRACKER_HEADERS
        ) as session:

            async with session.get(
                url,
                allow_redirects=True
            ) as response:

                if response.status != 200:

                    print(
                        f"[TRACKER] HTTP {response.status}: {url}"
                    )

                    return None

                return await response.text(
                    errors="ignore"
                )

    except Exception as e:

        print(
            f"[TRACKER] Page request error: {e}"
        )

        return None


# ============================================================
# RESOLVE SHORTENED SOCIAL URL
# ============================================================

async def resolve_final_url(url: str):

    """
    Follows redirects so shortened TikTok share URLs such as:

        https://vm.tiktok.com/...
        https://vt.tiktok.com/...

    can become the actual video URL.
    """

    timeout = aiohttp.ClientTimeout(
        total=15
    )

    try:

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=TRACKER_HEADERS
        ) as session:

            async with session.get(
                url,
                allow_redirects=True
            ) as response:

                final_url = str(
                    response.url
                )

                print(
                    f"[URL] Resolved:\n"
                    f"       Original: {url}\n"
                    f"       Final:    {final_url}"
                )

                return final_url

    except Exception as e:

        print(
            f"[URL] Could not resolve {url}: {e}"
        )

        return url

# ============================================================
# NUMBER PARSING
# ============================================================

def parse_social_number(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    value = value.replace(",", "").replace(" ", "")

    match = re.match(
        r"^([0-9]+(?:\.[0-9]+)?)([KMB])?$",
        value,
        re.IGNORECASE
    )

    if match:

        number = float(match.group(1))
        suffix = (
            match.group(2) or ""
        ).upper()

        multiplier = {
            "": 1,
            "K": 1_000,
            "M": 1_000_000,
            "B": 1_000_000_000,
        }[suffix]

        return int(number * multiplier)

    digits = re.sub(
        r"[^\d]",
        "",
        value
    )

    if digits:

        try:
            return int(digits)
        except Exception:
            pass

    return None


def find_metric(html, names):

    if not html:
        return None

    for name in names:

        patterns = [
            rf'"{re.escape(name)}"\s*:\s*"([^"]+)"',
            rf'"{re.escape(name)}"\s*:\s*([0-9]+)',
            rf"'{re.escape(name)}'\s*:\s*'([^']+)'",
            rf"'{re.escape(name)}'\s*:\s*([0-9]+)",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                html,
                re.IGNORECASE
            )

            if match:

                value = parse_social_number(
                    match.group(1)
                )

                if value is not None:

                    return value

    return None

# ============================================================
# ACCOUNT IDENTITY HELPERS
# ============================================================

def clean_username(value):

    if not value:
        return None

    value = unquote(
        str(value)
    ).strip()

    value = value.lstrip("@").strip()

    # Remove trailing URL fragments/query if present.
    value = value.split("?")[0]
    value = value.split("#")[0]
    value = value.strip("/")

    return value.lower()


def extract_profile_identity_from_url(
    platform,
    profile_url
):

    try:

        parsed = urlparse(profile_url)

        path = unquote(
            parsed.path
        ).strip("/")

        if not path:
            return None

        parts = [
            p for p in path.split("/")
            if p
        ]

        if platform == "TikTok":

            if parts:

                # Normal TikTok profile:
                # /@username
                if parts[0].startswith("@"):

                    return clean_username(
                        parts[0]
                    )

                # Also tolerate a profile URL without @.
                return clean_username(
                    parts[0]
                )

        if platform == "Instagram":

            if parts:

                return clean_username(
                    parts[0]
                )

        if platform == "YouTube":

            if parts:

                # /@handle
                if parts[0].startswith("@"):

                    return clean_username(
                        parts[0]
                    )

                # /channel/CHANNEL_ID
                if (
                    parts[0].lower() == "channel"
                    and len(parts) >= 2
                ):

                    return clean_username(
                        parts[1]
                    )

                # /user/USERNAME
                if (
                    parts[0].lower() == "user"
                    and len(parts) >= 2
                ):

                    return clean_username(
                        parts[1]
                    )

                # /c/CHANNEL
                if (
                    parts[0].lower() == "c"
                    and len(parts) >= 2
                ):

                    return clean_username(
                        parts[1]
                    )

    except Exception:
        pass

    return None


def extract_clip_identity_from_url(
    platform,
    clip_url,
    html=None
):

    try:

        parsed = urlparse(clip_url)

        path = unquote(
            parsed.path
        ).strip("/")

        parts = [
            p for p in path.split("/")
            if p
        ]

        # --------------------------------------------------------
        # TikTok
        #
        # Typical:
        # https://www.tiktok.com/@username/video/123
        # --------------------------------------------------------

        if platform == "TikTok":

            for part in parts:

                if part.startswith("@"):

                    return clean_username(
                        part
                    )

        # --------------------------------------------------------
        # Instagram
        #
        # Typical:
        # https://www.instagram.com/username/reel/123
        # https://www.instagram.com/username/p/123
        # --------------------------------------------------------

        if platform == "Instagram":

            if parts:

                reserved = {
                    "reel",
                    "reels",
                    "p",
                    "tv",
                    "stories",
                    "explore"
                }

                if (
                    parts[0].lower()
                    not in reserved
                ):

                    return clean_username(
                        parts[0]
                    )

        # --------------------------------------------------------
        # YouTube
        # --------------------------------------------------------

        if platform == "YouTube" and html:

            patterns = [

                r'"canonicalBaseUrl"\s*:\s*"(/@[^"]+)"',

                r'"canonicalBaseUrl"\s*:\s*"(/channel/[^"]+)"',

                r'"channelId"\s*:\s*"([^"]+)"',

                r'"externalId"\s*:\s*"([^"]+)"',
            ]

            for pattern in patterns:

                match = re.search(
                    pattern,
                    html,
                    re.IGNORECASE
                )

                if match:

                    value = (
                        match.group(1)
                        .strip()
                    )

                    if value.startswith("/@"):

                        return clean_username(
                            value.split("/", 2)[-1]
                        )

                    if value.startswith("/channel/"):

                        return clean_username(
                            value.split("/", 2)[-1]
                        )

                    return clean_username(
                        value
                    )

    except Exception:
        pass

    return None

# ============================================================
# TIKTOK CREATOR EXTRACTION FROM HTML
# ============================================================

def extract_tiktok_author_from_html(html):

    """
    TikTok share URLs often do not contain the creator username.

    This function looks inside TikTok's page data for the actual
    creator/author username.
    """

    if not html:
        return None

    # First prioritize patterns that specifically associate
    # uniqueId with an author object.
    patterns = [

        # Author object containing uniqueId.
        r'"author"\s*:\s*\{[^{}]{0,2000}?"uniqueId"\s*:\s*"([^"]+)"',

        # Author object containing unique_id.
        r'"author"\s*:\s*\{[^{}]{0,2000}?"unique_id"\s*:\s*"([^"]+)"',

        # Author object represented directly as a username.
        r'"author"\s*:\s*"([^"]+)"',

        # TikTok creator information.
        r'"authorUniqueId"\s*:\s*"([^"]+)"',

        r'"author_unique_id"\s*:\s*"([^"]+)"',

        # Common TikTok page data.
        r'"uniqueId"\s*:\s*"([^"]+)"',

        r'"unique_id"\s*:\s*"([^"]+)"',
    ]

    for pattern in patterns:

        matches = re.finditer(
            pattern,
            html,
            re.IGNORECASE | re.DOTALL
        )

        for match in matches:

            candidate = clean_username(
                match.group(1)
            )

            if not candidate:
                continue

            # Avoid obvious generic values.
            if candidate in {
                "tiktok",
                "video",
                "user",
                "author",
                "undefined",
                "null",
            }:
                continue

            return candidate

    # Last fallback: find a TikTok video URL containing @username.
    match = re.search(
        r'tiktok\.com/@([^/"?]+)/video/',
        html,
        re.IGNORECASE
    )

    if match:

        return clean_username(
            match.group(1)
        )

    return None

# ============================================================
# VERIFIED ACCOUNTS
# ============================================================

async def get_verified_accounts(user_id):

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        async with db.execute(
            """
            SELECT
                id,
                platform,
                profile_url
            FROM social_accounts
            WHERE user_id = ?
            AND status = 'verified'
            ORDER BY id DESC
            """,
            (user_id,)
        ) as cursor:

            return await cursor.fetchall()

# ============================================================
# CHECK CLIP OWNERSHIP
# ============================================================

async def clip_belongs_to_linked_account(
    user_id,
    clip_url,
    platform
):

    """
    Verify that the submitted clip was posted by one of the
    user's verified accounts.

    TikTok is handled specially because share/shortened URLs
    frequently hide the @username.
    """

    accounts = await get_verified_accounts(
        user_id
    )

    if not accounts:

        return False, "no_account"

    # Only accounts from the same platform matter.
    platform_accounts = [
        account
        for account in accounts
        if account[1] == platform
    ]

    if not platform_accounts:

        return False, "not_linked"

    account_identities = set()

    for (
        account_id,
        account_platform,
        profile_url
    ) in platform_accounts:

        identity = extract_profile_identity_from_url(
            platform,
            profile_url
        )

        if identity:

            account_identities.add(
                identity
            )

    print(
        f"[VERIFY] Linked {platform} identities: "
        f"{account_identities}"
    )

    if not account_identities:

        return False, "identity_unknown"

    # ========================================================
    # TIKTOK
    # ========================================================

    if platform == "TikTok":

        # Step 1:
        # Resolve vm.tiktok.com / vt.tiktok.com / other
        # TikTok share URLs to the actual video URL.
        resolved_url = await resolve_final_url(
            clip_url
        )

        # Step 2:
        # Try to get @username directly from final URL.
        clip_identity = extract_clip_identity_from_url(
            platform,
            resolved_url
        )

        print(
            f"[VERIFY] TikTok URL identity: "
            f"{clip_identity}"
        )

        # If the final URL clearly identifies the same account,
        # accept it immediately.
        if (
            clip_identity
            and
            clip_identity in account_identities
        ):

            print(
                "[VERIFY] TikTok matched from URL."
            )

            return True, "matched"

        # Step 3:
        # If the URL doesn't expose the creator, fetch the actual
        # TikTok video page and inspect its page data.
        html = await fetch_page_html(
            resolved_url
        )

        html_identity = (
            extract_tiktok_author_from_html(
                html
            )
        )

        print(
            f"[VERIFY] TikTok HTML identity: "
            f"{html_identity}"
        )

        if (
            html_identity
            and
            html_identity in account_identities
        ):

            print(
                "[VERIFY] TikTok matched from page HTML."
            )

            return True, "matched"

        # Step 4:
        # If both URL and HTML failed to identify the creator,
        # don't falsely reject it as another person's account.
        if (
            not clip_identity
            and
            not html_identity
        ):

            return False, "identity_unknown"

        return False, "not_linked"

    # ========================================================
    # INSTAGRAM
    # ========================================================

    if platform == "Instagram":

        clip_identity = extract_clip_identity_from_url(
            platform,
            clip_url
        )

        if not clip_identity:

            return False, "identity_unknown"

        print(
            f"[VERIFY] Instagram identity: "
            f"{clip_identity}"
        )

        if clip_identity in account_identities:

            return True, "matched"

        return False, "not_linked"

    # ========================================================
    # YOUTUBE
    # ========================================================

    if platform == "YouTube":

        html = await fetch_page_html(
            clip_url
        )

        clip_identity = extract_clip_identity_from_url(
            platform,
            clip_url,
            html
        )

        if not clip_identity:

            return False, "identity_unknown"

        print(
            f"[VERIFY] YouTube identity: "
            f"{clip_identity}"
        )

        if clip_identity in account_identities:

            return True, "matched"

        return False, "not_linked"

    return False, "identity_unknown"

# ============================================================
# TIKTOK TRACKING
# ============================================================

async def fetch_tiktok_stats(url: str):

    # Resolve shortened URLs first.
    resolved_url = await resolve_final_url(
        url
    )

    html = await fetch_page_html(
        resolved_url
    )

    if not html:

        return None

    views = find_metric(
        html,
        [
            "playCount",
            "play_count",
            "viewCount",
            "view_count",
            "views"
        ]
    )

    likes = find_metric(
        html,
        [
            "diggCount",
            "digg_count",
            "likeCount",
            "like_count",
            "likes"
        ]
    )

    if views is None:

        match = re.search(
            r'"playCount"\s*:\s*"?([0-9]+)"?',
            html,
            re.IGNORECASE
        )

        if match:

            views = int(
                match.group(1)
            )

    if likes is None:

        match = re.search(
            r'"diggCount"\s*:\s*"?([0-9]+)"?',
            html,
            re.IGNORECASE
        )

        if match:

            likes = int(
                match.group(1)
            )

    if views is None and likes is None:

        return None

    return {
        "views": views or 0,
        "likes": likes or 0,
    }

# ============================================================
# INSTAGRAM TRACKING
# ============================================================

async def fetch_instagram_stats(url: str):

    html = await fetch_page_html(url)

    if not html:

        return None

    views = find_metric(
        html,
        [
            "play_count",
            "playCount",
            "video_view_count",
            "videoViewCount",
            "view_count",
            "viewCount"
        ]
    )

    likes = find_metric(
        html,
        [
            "like_count",
            "likeCount",
            "edge_media_preview_like"
        ]
    )

    if likes is None:

        match = re.search(
            r'([0-9][0-9,.\s]*[KMB]?)\s+likes',
            html,
            re.IGNORECASE
        )

        if match:

            likes = parse_social_number(
                match.group(1)
            )

    if views is None:

        match = re.search(
            r'([0-9][0-9,.\s]*[KMB]?)\s+views',
            html,
            re.IGNORECASE
        )

        if match:

            views = parse_social_number(
                match.group(1)
            )

    if views is None and likes is None:

        return None

    return {
        "views": views or 0,
        "likes": likes or 0,
    }

# ============================================================
# YOUTUBE TRACKING
# ============================================================

async def fetch_youtube_stats(url: str):

    video_id = extract_youtube_video_id(
        url
    )

    if not video_id:

        return None

    # --------------------------------------------------------
    # Official YouTube API
    # --------------------------------------------------------

    if YOUTUBE_API_KEY:

        timeout = aiohttp.ClientTimeout(
            total=15
        )

        api_url = (
            "https://www.googleapis.com/youtube/v3/videos"
        )

        params = {
            "part": "statistics",
            "id": video_id,
            "key": YOUTUBE_API_KEY,
        }

        try:

            async with aiohttp.ClientSession(
                timeout=timeout
            ) as session:

                async with session.get(
                    api_url,
                    params=params
                ) as response:

                    if response.status == 200:

                        data = await response.json()

                        items = data.get(
                            "items",
                            []
                        )

                        if items:

                            statistics = items[0].get(
                                "statistics",
                                {}
                            )

                            return {
                                "views": int(
                                    statistics.get(
                                        "viewCount",
                                        0
                                    )
                                ),
                                "likes": int(
                                    statistics.get(
                                        "likeCount",
                                        0
                                    )
                                ),
                            }

                    else:

                        print(
                            "[TRACKER] YouTube API "
                            f"HTTP {response.status}"
                        )

        except Exception as e:

            print(
                f"[TRACKER] YouTube API error: {e}"
            )

    # --------------------------------------------------------
    # Public page fallback
    # --------------------------------------------------------

    html = await fetch_page_html(
        url
    )

    if not html:

        return None

    views = find_metric(
        html,
        [
            "viewCount",
            "view_count"
        ]
    )

    likes = find_metric(
        html,
        [
            "likeCount",
            "like_count"
        ]
    )

    if views is None:

        match = re.search(
            r'"viewCount"\s*:\s*"([0-9]+)"',
            html
        )

        if match:

            views = int(
                match.group(1)
            )

    if likes is None:

        match = re.search(
            r'"likeCount"\s*:\s*"([0-9]+)"',
            html
        )

        if match:

            likes = int(
                match.group(1)
            )

    if views is None and likes is None:

        return None

    return {
        "views": views or 0,
        "likes": likes or 0,
    }

# ============================================================
# UNIVERSAL VIDEO TRACKER
# ============================================================

async def fetch_video_stats(url: str):

    platform = detect_platform_from_url(
        url
    )

    if not platform:

        return None

    try:

        if platform == "TikTok":

            stats = await fetch_tiktok_stats(
                url
            )

        elif platform == "Instagram":

            stats = await fetch_instagram_stats(
                url
            )

        elif platform == "YouTube":

            stats = await fetch_youtube_stats(
                url
            )

        else:

            stats = None

        if stats:

            stats["platform"] = platform

            return stats

    except Exception as e:

        print(
            f"[TRACKER] {platform} error: {e}"
        )

    return None

# ============================================================
# UPDATE ONE SUBMISSION
# ============================================================

async def update_submission_stats(
    submission_id: int,
    url: str
):

    stats = await fetch_video_stats(
        url
    )

    if not stats:

        return False

    views = max(
        0,
        int(stats.get("views", 0))
    )

    likes = max(
        0,
        int(stats.get("likes", 0))
    )

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        async with db.execute(
            """
            SELECT status
            FROM submissions
            WHERE id = ?
            """,
            (submission_id,)
        ) as cursor:

            row = await cursor.fetchone()

        if not row:

            return False

        if row[0] != "pending":

            return False

        await db.execute(
            """
            UPDATE submissions
            SET
                views = ?,
                likes = ?
            WHERE id = ?
            AND status = 'pending'
            """,
            (
                views,
                likes,
                submission_id
            )
        )

        await db.commit()

    print(
        f"[TRACKER] Submission #{submission_id}: "
        f"{views:,} views / {likes:,} likes"
    )

    return True

# ============================================================
# BACKGROUND LIVE TRACKER
# ============================================================

@tasks.loop(seconds=TRACKER_INTERVAL)
async def track_pending_clips():

    try:

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT
                    id,
                    clip_url
                FROM submissions
                WHERE status = 'pending'
                ORDER BY id ASC
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

            await asyncio.sleep(2)

    except Exception as e:

        print(
            f"[TRACKER] Background tracker error: {e}"
        )


@track_pending_clips.before_loop
async def before_track_pending_clips():

    await bot.wait_until_ready()

# ============================================================
# SOCIAL VERIFICATION
# ============================================================

async def check_profile_for_code(
    profile_url: str,
    code: str
):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 "
            "Safari/604.1"
        ),
        "Accept-Language": "en-US,en;q=0.9"
    }

    timeout = aiohttp.ClientTimeout(
        total=15
    )

    try:

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers
        ) as session:

            async with session.get(
                profile_url,
                allow_redirects=True
            ) as response:

                if response.status != 200:

                    return False

                html = await response.text(
                    errors="ignore"
                )

                return (
                    code.lower()
                    in html.lower()
                )

    except Exception as e:

        print(
            f"Verification error: {e}"
        )

        return False

# ============================================================
# ACCOUNT LINK MODAL
# ============================================================

class ProfileURLModal(Modal):

    def __init__(self, platform):

        super().__init__(
            title=f"Link {platform}"
        )

        self.platform = platform

        self.profile_url = TextInput(
            label=f"{platform} Profile URL",
            placeholder="https://...",
            required=True,
            max_length=500
        )

        self.add_item(
            self.profile_url
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        url = (
            self.profile_url
            .value
            .strip()
        )

        if not valid_profile_url(url):

            return await send_temporary_error(
                interaction,
                "❌ Please enter a valid profile URL."
            )

        if not platform_matches_url(
            self.platform,
            url
        ):

            return await send_temporary_error(
                interaction,
                f"❌ That doesn't appear to be a valid "
                f"**{self.platform}** profile URL."
            )

        code = generate_verification_code()

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT id
                FROM social_accounts
                WHERE user_id = ?
                AND profile_url = ?
                """,
                (
                    interaction.user.id,
                    url
                )
            ) as cursor:

                existing = await cursor.fetchone()

            if existing:

                return await send_temporary_error(
                    interaction,
                    "❌ You already have this account added."
                )

            await db.execute(
                """
                INSERT INTO social_accounts
                (
                    user_id,
                    platform,
                    profile_url,
                    verification_code,
                    status,
                    added_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    interaction.user.id,
                    self.platform,
                    url,
                    code,
                    utc_now()
                )
            )

            await db.commit()

        await interaction.response.send_message(
            f"### 🔐 {self.platform} Verification\n\n"
            f"Your verification code is:\n"
            f"**`{code}`**\n\n"
            f"**Step 1:** Put this exact code in your "
            f"**{self.platform} bio/about section**.\n\n"
            f"**Step 2:** Keep it visible.\n\n"
            f"**Step 3:** Come back and click "
            f"**Check Verification**.\n\n"
            f"⚠️ Your account will not be linked until "
            f"the code is found.",
            ephemeral=True
        )

# ============================================================
# ACCOUNT VIEW
# ============================================================

class AccountView(View):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @button(
        label="TikTok",
        style=discord.ButtonStyle.secondary,
        emoji="🎵",
        row=0
    )
    async def tiktok(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            ProfileURLModal("TikTok")
        )

    @button(
        label="Instagram",
        style=discord.ButtonStyle.secondary,
        emoji="📸",
        row=0
    )
    async def instagram(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            ProfileURLModal("Instagram")
        )

    @button(
        label="YouTube",
        style=discord.ButtonStyle.secondary,
        emoji="▶️",
        row=0
    )
    async def youtube(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            ProfileURLModal("YouTube")
        )

    @button(
        label="Check Verification",
        style=discord.ButtonStyle.success,
        emoji="🔎",
        row=1
    )
    async def check_verification(
        self,
        interaction,
        button
    ):

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT
                    id,
                    platform,
                    profile_url,
                    verification_code
                FROM social_accounts
                WHERE user_id = ?
                AND status = 'pending'
                ORDER BY id DESC
                """,
                (interaction.user.id,)
            ) as cursor:

                accounts = await cursor.fetchall()

        if not accounts:

            return await send_temporary_error(
                interaction,
                "You don't have any accounts waiting for verification."
            )

        await interaction.response.defer(
            ephemeral=True
        )

        verified = []

        for (
            account_id,
            platform,
            profile_url,
            code
        ) in accounts:

            found = await check_profile_for_code(
                profile_url,
                code
            )

            if found:

                async with aiosqlite.connect(
                    DB_PATH
                ) as db:

                    await db.execute(
                        """
                        UPDATE social_accounts
                        SET
                            status = 'verified',
                            verified_at = ?
                        WHERE id = ?
                        """,
                        (
                            utc_now(),
                            account_id
                        )
                    )

                    await db.commit()

                verified.append(
                    platform
                )

        if verified:

            await interaction.followup.send(
                f"✅ Successfully verified: "
                f"**{', '.join(verified)}**",
                ephemeral=True
            )

        else:

            message = await interaction.followup.send(
                "❌ I couldn't find the verification code yet.\n\n"
                "Make sure:\n"
                "• The code is exact\n"
                "• It is publicly visible\n"
                "• The profile URL is correct\n"
                "• The account is publicly accessible\n\n"
                "Then try again.",
                ephemeral=True,
                wait=True
            )

            await asyncio.sleep(
                ERROR_DELETE_DELAY
            )

            try:
                await message.delete()
            except Exception:
                pass

    @button(
        label="Remove Account",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        row=1
    )
    async def remove_account(
        self,
        interaction,
        button
    ):

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT
                    id,
                    platform,
                    status
                FROM social_accounts
                WHERE user_id = ?
                ORDER BY id DESC
                """,
                (interaction.user.id,)
            ) as cursor:

                accounts = await cursor.fetchall()

        if not accounts:

            return await send_temporary_error(
                interaction,
                "You don't have any social accounts."
            )

        view = View(
            timeout=120
        )

        for (
            account_id,
            platform,
            status
        ) in accounts:

            account_button = Button(
                label=f"{platform} ({status})",
                style=discord.ButtonStyle.danger
            )

            async def callback(
                interaction,
                account_id=account_id,
                platform=platform
            ):

                async with aiosqlite.connect(
                    DB_PATH
                ) as db:

                    await db.execute(
                        """
                        DELETE FROM social_accounts
                        WHERE id = ?
                        AND user_id = ?
                        """,
                        (
                            account_id,
                            interaction.user.id
                        )
                    )

                    await db.commit()

                await interaction.response.send_message(
                    f"🗑️ **{platform}** account removed.",
                    ephemeral=True
                )

            account_button.callback = callback

            view.add_item(
                account_button
            )

        await interaction.response.send_message(
            "Select the account you want to remove:",
            view=view,
            ephemeral=True
        )

# ============================================================
# PAYMENT MODAL
# ============================================================

class PaymentModal(Modal):

    def __init__(self, method):

        super().__init__(
            title=f"{method} Payment"
        )

        self.method = method

        placeholders = {
            "Apple Pay":
                "Email or Apple Pay contact",

            "PayPal":
                "PayPal email",

            "Zelle":
                "Zelle email or phone",

            "Cash":
                "Cash payment details",

            "Venmo":
                "Venmo username"
        }

        self.details = TextInput(
            label=f"{method} Details",
            placeholder=placeholders.get(
                method,
                "Payment details"
            ),
            required=True,
            max_length=200
        )

        self.add_item(
            self.details
        )

    async def on_submit(
        self,
        interaction
    ):

        details = (
            self.details
            .value
            .strip()
        )

        if not details:

            return await send_temporary_error(
                interaction,
                "❌ Payment details cannot be empty."
            )

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            await db.execute(
                """
                INSERT INTO payment_methods
                (
                    user_id,
                    method,
                    details,
                    updated_at
                )
                VALUES (?, ?, ?, ?)

                ON CONFLICT(user_id)
                DO UPDATE SET
                    method = excluded.method,
                    details = excluded.details,
                    updated_at = excluded.updated_at
                """,
                (
                    interaction.user.id,
                    self.method,
                    details,
                    utc_now()
                )
            )

            await db.commit()

        await interaction.response.send_message(
            f"✅ **Payment method saved.**\n\n"
            f"**Method:** {self.method}\n"
            f"**Details:** `{details}`",
            ephemeral=True
        )

# ============================================================
# PAYMENT VIEW
# ============================================================

class PaymentView(View):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @button(
        label="Apple Pay",
        style=discord.ButtonStyle.secondary,
        emoji="🍎",
        row=0
    )
    async def apple_pay(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            PaymentModal("Apple Pay")
        )

    @button(
        label="PayPal",
        style=discord.ButtonStyle.secondary,
        emoji="💙",
        row=0
    )
    async def paypal(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            PaymentModal("PayPal")
        )

    @button(
        label="Zelle",
        style=discord.ButtonStyle.secondary,
        emoji="💜",
        row=0
    )
    async def zelle(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            PaymentModal("Zelle")
        )

    @button(
        label="Cash",
        style=discord.ButtonStyle.secondary,
        emoji="💵",
        row=1
    )
    async def cash(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            PaymentModal("Cash")
        )

    @button(
        label="Venmo",
        style=discord.ButtonStyle.secondary,
        emoji="💰",
        row=1
    )
    async def venmo(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            PaymentModal("Venmo")
        )

# ============================================================
# DASHBOARD VIEW
# ============================================================

class DashboardView(View):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        emoji="🔄"
    )
    async def refresh(
        self,
        interaction,
        button
    ):

        stats = await get_dashboard_stats(
            interaction.user.id
        )

        embed = discord.Embed(
            title="📊 Dashboard",
            color=0x2B2D31
        )

        embed.add_field(
            name="📤 Total Submissions",
            value=f"**{stats['submissions']:,}**",
            inline=True
        )

        embed.add_field(
            name="❤️ Total Likes",
            value=f"**{stats['likes']:,}**",
            inline=True
        )

        embed.add_field(
            name="✅ Approved Clips",
            value=f"**{stats['approved']:,}**",
            inline=True
        )

        embed.add_field(
            name="👁️ Total Views",
            value=f"**{stats['views']:,}**",
            inline=True
        )

        embed.add_field(
            name="💰 Total Earnings",
            value=f"**${stats['earnings']:.2f}**",
            inline=True
        )

        embed.add_field(
            name="💵 Rate",
            value=f"**${CPM_RATE:.2f}/1K views**",
            inline=True
        )

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )

# ============================================================
# EARNINGS VIEW
# ============================================================

class EarningsView(View):

    def __init__(self):

        super().__init__(
            timeout=180
        )

    @button(
        label="Payment History",
        style=discord.ButtonStyle.secondary,
        emoji="📜"
    )
    async def payment_history(
        self,
        interaction,
        button
    ):

        rows = await get_payment_history(
            interaction.user.id
        )

        if not rows:

            return await interaction.response.send_message(
                "No payment history yet.",
                ephemeral=True
            )

        text = "**Payment History**\n\n"

        for (
            payment_id,
            amount,
            method,
            details,
            status,
            created_at
        ) in rows:

            text += (
                f"**#{payment_id}**\n"
                f"💰 Amount: **${amount:.2f}**\n"
                f"💳 Method: **{method or 'N/A'}**\n"
                f"📌 Status: **{status}**\n"
                f"🕐 {created_at[:19].replace('T', ' ')} UTC\n\n"
            )

        await interaction.response.send_message(
            text[:1900],
            ephemeral=True
        )

# ============================================================
# USER CLIP PANEL
# ============================================================

class ClipPanel(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @button(
        label="Submit Video",
        style=discord.ButtonStyle.primary,
        emoji="📩",
        custom_id="clips_submit",
        row=0
    )
    async def submit_video(
        self,
        interaction,
        button
    ):

        accounts = await get_verified_accounts(
            interaction.user.id
        )

        if not accounts:

            return await send_temporary_error(
                interaction,
                "❌ **Account Not Linked**\n\n"
                "You don't have a verified social account linked "
                "to the system.\n\n"
                "Please link and verify your account, "
                "then submit again."
            )

        await interaction.response.send_message(
            "Please paste the **full link** of your clip now.\n"
            "⏳ You have **60 seconds**.",
            ephemeral=True
        )

        def check(message):

            return (
                message.author.id ==
                interaction.user.id
                and
                message.channel.id ==
                interaction.channel.id
            )

        try:

            message = await bot.wait_for(
                "message",
                check=check,
                timeout=60
            )

        except asyncio.TimeoutError:

            return await interaction.followup.send(
                "⏰ Timed out. Please try again.",
                ephemeral=True
            )

        clip_url = (
            message.content
            .strip()
        )

        if not re.match(
            r"^https?://",
            clip_url,
            re.IGNORECASE
        ):

            try:
                await message.delete()
            except Exception:
                pass

            return await send_temporary_error(
                interaction,
                "❌ That doesn't look like a valid link."
            )

        platform = detect_platform_from_url(
            clip_url
        )

        if not platform:

            try:
                await message.delete()
            except Exception:
                pass

            return await send_temporary_error(
                interaction,
                "❌ I currently support TikTok, Instagram, "
                "and YouTube clip links only."
            )

        # --------------------------------------------------------
        # ACCOUNT CHECK
        # --------------------------------------------------------

        checking_message = await interaction.followup.send(
            f"🔎 Checking that this **{platform}** clip "
            f"belongs to one of your verified linked accounts...",
            ephemeral=True,
            wait=True
        )

        belongs, reason = await clip_belongs_to_linked_account(
            interaction.user.id,
            clip_url,
            platform
        )

        # Delete checking message after ownership check.
        try:
            await checking_message.delete()
        except Exception:
            pass

        if not belongs:

            try:
                await message.delete()
            except Exception:
                pass

            if reason == "no_account":

                error_text = (
                    "❌ **Account Not Linked**\n\n"
                    "This account cannot be found or is not "
                    "linked to the system.\n\n"
                    "Please link and verify your account, "
                    "then submit again."
                )

            elif reason == "identity_unknown":

                error_text = (
                    "❌ **Account Could Not Be Verified**\n\n"
                    "I couldn't determine which account posted "
                    "this clip.\n\n"
                    "If you're using TikTok, try submitting the "
                    "normal video/share link again and make sure "
                    "the video is public."
                )

            else:

                error_text = (
                    "❌ **Account Not Linked**\n\n"
                    "This clip appears to be from an account that "
                    "is not linked and verified with the bot.\n\n"
                    "Please link and verify that account, "
                    "then submit again."
                )

            return await send_temporary_error(
                interaction,
                error_text
            )

        # --------------------------------------------------------
        # FIRST STAT CHECK
        # --------------------------------------------------------

        stats_message = await interaction.followup.send(
            f"🔎 Checking the **{platform}** video for its "
            f"current views and likes...",
            ephemeral=True,
            wait=True
        )

        stats = await fetch_video_stats(
            clip_url
        )

        try:
            await stats_message.delete()
        except Exception:
            pass

        initial_views = 0
        initial_likes = 0

        if stats:

            initial_views = int(
                stats.get("views", 0)
            )

            initial_likes = int(
                stats.get("likes", 0)
            )

        # --------------------------------------------------------
        # SAVE SUBMISSION
        # --------------------------------------------------------

        await ensure_user(
            interaction.user.id
        )

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

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
                    payout_status
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?, 'unpaid')
                """,
                (
                    interaction.user.id,
                    clip_url,
                    utc_now(),
                    initial_views,
                    initial_likes
                )
            )

            submission_id = cursor.lastrowid

            await db.commit()

        if stats:

            estimated = calculate_earnings(
                initial_views
            )

            await interaction.followup.send(
                f"✅ **Clip submitted successfully!**\n\n"
                f"🆔 Submission: **#{submission_id}**\n"
                f"📱 Platform: **{platform}**\n"
                f"👁️ Current views: **{initial_views:,}**\n"
                f"❤️ Current likes: **{initial_likes:,}**\n"
                f"💰 Current estimated earnings: "
                f"**${estimated:.2f}**\n\n"
                f"🔄 Your pending clip will be checked "
                f"automatically for updated stats.",
                ephemeral=True
            )

        else:

            await interaction.followup.send(
                f"✅ **Clip submitted successfully!**\n\n"
                f"🆔 Submission: **#{submission_id}**\n"
                f"📱 Platform: **{platform}**\n\n"
                f"⚠️ I couldn't read the current public "
                f"views/likes yet. The tracker will try again "
                f"automatically.",
                ephemeral=True
            )

        try:

            await message.delete()

        except Exception:

            pass

    @button(
        label="Accounts",
        style=discord.ButtonStyle.secondary,
        emoji="🪪",
        custom_id="clips_accounts",
        row=0
    )
    async def accounts(
        self,
        interaction,
        button
    ):

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT
                    platform,
                    profile_url,
                    status
                FROM social_accounts
                WHERE user_id = ?
                ORDER BY id DESC
                """,
                (interaction.user.id,)
            ) as cursor:

                accounts = await cursor.fetchall()

        text = "**Your Social Accounts**\n\n"

        if accounts:

            for (
                platform,
                profile_url,
                status
            ) in accounts:

                icon = {
                    "TikTok": "🎵",
                    "Instagram": "📸",
                    "YouTube": "▶️"
                }.get(
                    platform,
                    "🔗"
                )

                text += (
                    f"{icon} **{platform}** — "
                    f"`{status}`\n"
                    f"{profile_url}\n\n"
                )

        else:

            text += (
                "You don't have any accounts yet.\n\n"
            )

        text += (
            "Choose a platform below to add an account."
        )

        await interaction.response.send_message(
            text,
            view=AccountView(),
            ephemeral=True
        )

    @button(
        label="Dashboard",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="clips_dashboard",
        row=1
    )
    async def dashboard(
        self,
        interaction,
        button
    ):

        stats = await get_dashboard_stats(
            interaction.user.id
        )

        embed = discord.Embed(
            title="📊 Dashboard",
            color=0x2B2D31
        )

        embed.add_field(
            name="📤 Total Submissions",
            value=f"**{stats['submissions']:,}**",
            inline=True
        )

        embed.add_field(
            name="❤️ Total Likes",
            value=f"**{stats['likes']:,}**",
            inline=True
        )

        embed.add_field(
            name="✅ Approved Clips",
            value=f"**{stats['approved']:,}**",
            inline=True
        )

        embed.add_field(
            name="👁️ Total Views",
            value=f"**{stats['views']:,}**",
            inline=True
        )

        embed.add_field(
            name="💰 Total Earnings",
            value=f"**${stats['earnings']:.2f}**",
            inline=True
        )

        embed.add_field(
            name="💵 Rate",
            value=f"**${CPM_RATE:.2f}/1K views**",
            inline=True
        )

        await interaction.response.send_message(
            embed=embed,
            view=DashboardView(),
            ephemeral=True
        )

    @button(
        label="Earnings",
        style=discord.ButtonStyle.secondary,
        emoji="💰",
        custom_id="clips_earnings",
        row=1
    )
    async def earnings(
        self,
        interaction,
        button
    ):

        stats = await get_earnings_stats(
            interaction.user.id
        )

        embed = discord.Embed(
            title="💰 Earnings",
            color=0x2B2D31
        )

        embed.add_field(
            name="⏳ Pending",
            value=f"**${stats['pending']:.2f}**",
            inline=True
        )

        embed.add_field(
            name="✅ Paid",
            value=f"**${stats['paid']:.2f}**",
            inline=True
        )

        embed.add_field(
            name="⚠️ Failed",
            value=f"**${stats['failed']:.2f}**",
            inline=True
        )

        embed.add_field(
            name="❌ Rejected",
            value=f"**${stats['rejected']:.2f}**",
            inline=True
        )

        await interaction.response.send_message(
            embed=embed,
            view=EarningsView(),
            ephemeral=True
        )

    @button(
        label="Say Hi",
        style=discord.ButtonStyle.secondary,
        emoji="👋",
        custom_id="clips_hi",
        row=2
    )
    async def say_hi(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            f"Hello {interaction.user.mention}! 👋",
            ephemeral=True
        )

    @button(
        label="Submission History",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="clips_history",
        row=2
    )
    async def submission_history(
        self,
        interaction,
        button
    ):

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT
                    id,
                    clip_url,
                    status,
                    earnings,
                    views,
                    likes,
                    payout_status
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

        text = "**Your Recent Submissions**\n\n"

        for (
            submission_id,
            url,
            status,
            earnings,
            views,
            likes,
            payout_status
        ) in rows:

            estimated = calculate_earnings(
                views
            )

            text += (
                f"**Submission #{submission_id}**\n"
                f"📌 Status: **{status}**\n"
                f"👁️ Views: **{views:,}**\n"
                f"❤️ Likes: **{likes:,}**\n"
                f"💰 Earnings: **${earnings:.2f}**"
            )

            if status == "pending":

                text += (
                    f"\n📈 Estimated: **${estimated:.2f}**"
                )

            elif status == "approved":

                text += (
                    f"\n💳 Payout: **{payout_status}**"
                )

            text += (
                f"\n🔗 `{url[:50]}`\n\n"
            )

        await interaction.response.send_message(
            text[:1900],
            ephemeral=True
        )

    @button(
        label="Payment",
        style=discord.ButtonStyle.secondary,
        emoji="💳",
        custom_id="clips_payment",
        row=2
    )
    async def payment(
        self,
        interaction,
        button
    ):

        payment = await get_payment_method(
            interaction.user.id
        )

        if payment:

            method, details = payment

            current = (
                "**Current Payment Method**\n"
                f"💳 **{method}**\n"
                f"📌 `{details}`\n\n"
            )

        else:

            current = (
                "**No payment method saved yet.**\n\n"
            )

        await interaction.response.send_message(
            current +
            "Choose your preferred payment method:",
            view=PaymentView(),
            ephemeral=True
        )

# ============================================================
# ADMIN MODALS
# ============================================================

class SubmissionIDModal(Modal):

    def __init__(self, action):

        title = (
            "Approve Clip"
            if action == "approve"
            else "Reject Clip"
        )

        super().__init__(
            title=title
        )

        self.action = action

        self.submission_id = TextInput(
            label="Submission ID",
            placeholder="Example: 123",
            required=True,
            max_length=20
        )

        self.add_item(
            self.submission_id
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:

            submission_id = int(
                self.submission_id
                .value
                .strip()
            )

        except ValueError:

            return await send_temporary_error(
                interaction,
                "❌ Invalid Submission ID."
            )

        if self.action == "reject":

            await reject_submission(
                interaction,
                submission_id
            )

        else:

            await approve_submission(
                interaction,
                submission_id
            )


class UserIDModal(Modal):

    def __init__(self, action):

        titles = {
            "pending_payouts":
                "Pending Payout User",

            "approved_clips":
                "Approved Clips",

            "mark_paid":
                "Mark User Paid",

            "payment_failed":
                "Payment Failed",

            "payment_rejected":
                "Payment Rejected",

            "view_payment":
                "View User Payment"
        }

        super().__init__(
            title=titles.get(
                action,
                "User ID"
            )
        )

        self.action = action

        self.user_id = TextInput(
            label="Discord User ID",
            placeholder="Example: 123456789012345678",
            required=True,
            max_length=25
        )

        self.add_item(
            self.user_id
        )

    async def on_submit(
        self,
        interaction
    ):

        try:

            user_id = int(
                self.user_id
                .value
                .strip()
            )

        except ValueError:

            return await send_temporary_error(
                interaction,
                "❌ Invalid Discord User ID."
            )

        if self.action == "approved_clips":

            await show_approved_clips(
                interaction,
                user_id
            )

        elif self.action == "mark_paid":

            await mark_user_paid(
                interaction,
                user_id
            )

        elif self.action == "payment_failed":

            await process_payment_status(
                interaction,
                user_id,
                "failed"
            )

        elif self.action == "payment_rejected":

            await process_payment_status(
                interaction,
                user_id,
                "rejected"
            )

        elif self.action == "view_payment":

            await show_user_payment(
                interaction,
                user_id
            )

# ============================================================
# ADMIN OPERATIONS
# ============================================================

async def approve_submission(
    interaction,
    submission_id
):

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        async with db.execute(
            """
            SELECT
                user_id,
                clip_url,
                status,
                views,
                likes
            FROM submissions
            WHERE id = ?
            """,
            (submission_id,)
        ) as cursor:

            row = await cursor.fetchone()

    if not row:

        return await send_temporary_error(
            interaction,
            "❌ Submission not found."
        )

    (
        user_id,
        clip_url,
        status,
        stored_views,
        stored_likes
    ) = row

    if status != "pending":

        return await send_temporary_error(
            interaction,
            "❌ This submission has already been handled."
        )

    # One final live check.
    fresh_stats = await fetch_video_stats(
        clip_url
    )

    if fresh_stats:

        views = int(
            fresh_stats.get(
                "views",
                stored_views or 0
            )
        )

        likes = int(
            fresh_stats.get(
                "likes",
                stored_likes or 0
            )
        )

    else:

        views = int(
            stored_views or 0
        )

        likes = int(
            stored_likes or 0
        )

    earnings = calculate_earnings(
        views
    )

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        async with db.execute(
            """
            SELECT status
            FROM submissions
            WHERE id = ?
            """,
            (submission_id,)
        ) as cursor:

            current = await cursor.fetchone()

        if not current or current[0] != "pending":

            return await send_temporary_error(
                interaction,
                "❌ This submission was already handled."
            )

        await db.execute(
            """
            UPDATE submissions
            SET
                status = 'approved',
                views = ?,
                likes = ?,
                earnings = ?,
                payout_status = 'unpaid'
            WHERE id = ?
            AND status = 'pending'
            """,
            (
                views,
                likes,
                earnings,
                submission_id
            )
        )

        await db.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, pending, total_paid)
            VALUES (?, 0, 0)
            """,
            (user_id,)
        )

        await db.execute(
            """
            UPDATE users
            SET pending = pending + ?
            WHERE user_id = ?
            """,
            (
                earnings,
                user_id
            )
        )

        await db.commit()

    await interaction.response.send_message(
        f"✅ **Submission #{submission_id} approved!**\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"👁️ Views: **{views:,}**\n"
        f"❤️ Likes: **{likes:,}**\n"
        f"💰 Added to payout: **${earnings:.2f}**\n\n"
        f"📅 This amount will remain pending until "
        f"the user's payout is processed.",
        ephemeral=True
    )


async def reject_submission(
    interaction,
    submission_id
):

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        async with db.execute(
            """
            SELECT status
            FROM submissions
            WHERE id = ?
            """,
            (submission_id,)
        ) as cursor:

            row = await cursor.fetchone()

        if not row:

            return await send_temporary_error(
                interaction,
                "❌ Submission not found."
            )

        if row[0] != "pending":

            return await send_temporary_error(
                interaction,
                "❌ Submission already handled."
            )

        await db.execute(
            """
            UPDATE submissions
            SET status = 'rejected'
            WHERE id = ?
            """,
            (submission_id,)
        )

        await db.commit()

    await interaction.response.send_message(
        f"❌ Submission **#{submission_id}** rejected.",
        ephemeral=True
    )

# ============================================================
# APPROVED CLIPS / USER PAYOUT VIEW
# ============================================================

async def show_approved_clips(
    interaction,
    user_id
):

    rows = await get_user_unpaid_approved_clips(
        user_id
    )

    if not rows:

        return await send_temporary_error(
            interaction,
            f"❌ User `{user_id}` has no unpaid approved clips."
        )

    total_views = sum(
        int(row[2] or 0)
        for row in rows
    )

    total_likes = sum(
        int(row[3] or 0)
        for row in rows
    )

    total_earnings = sum(
        float(row[4] or 0)
        for row in rows
    )

    embed = discord.Embed(
        title="📊 Approved Clips",
        description=(
            f"👤 User ID: `{user_id}`\n"
            f"👤 User: <@{user_id}>"
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="✅ Approved Clips",
        value=f"**{len(rows):,}**",
        inline=True
    )

    embed.add_field(
        name="👁️ Total Views",
        value=f"**{total_views:,}**",
        inline=True
    )

    embed.add_field(
        name="❤️ Total Likes",
        value=f"**{total_likes:,}**",
        inline=True
    )

    embed.add_field(
        name="💰 Total Amount Due",
        value=f"**${total_earnings:.2f}**",
        inline=True
    )

    embed.add_field(
        name="💵 Rate",
        value=f"**${CPM_RATE:.2f}/1K**",
        inline=True
    )

    details = ""

    for (
        submission_id,
        clip_url,
        views,
        likes,
        earnings,
        submitted_at
    ) in rows:

        details += (
            f"**#{submission_id}** — "
            f"👁️ {int(views):,} — "
            f"❤️ {int(likes):,} — "
            f"💰 ${float(earnings):.2f}\n"
        )

    if len(details) > 1000:

        details = (
            details[:950]
            + "\n…more clips not shown."
        )

    embed.add_field(
        name="📋 Unpaid Approved Clips",
        value=details,
        inline=False
    )

    embed.set_footer(
        text="These are clips awaiting the next payout."
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

# ============================================================
# PENDING PAYOUTS
# ============================================================

async def show_pending_payouts(
    interaction
):

    rows = await get_pending_payout_users()

    if not rows:

        return await interaction.response.send_message(
            "✅ **No Pending Payouts**\n\n"
            "Everyone with approved earnings has been paid.",
            ephemeral=True
        )

    embed = discord.Embed(
        title="💰 Pending Payouts",
        description=(
            "Users with approved earnings that have "
            "not yet been paid."
        ),
        color=discord.Color.gold()
    )

    grand_total = 0

    for (
        user_id,
        clip_count,
        total_views,
        total_likes,
        total_earnings
    ) in rows:

        grand_total += float(
            total_earnings or 0
        )

        embed.add_field(
            name=f"👤 User ID: {user_id}",
            value=(
                f"User: <@{user_id}>\n"
                f"📋 Clips: **{clip_count}**\n"
                f"👁️ Views: **{int(total_views):,}**\n"
                f"❤️ Likes: **{int(total_likes):,}**\n"
                f"💰 Due: **${float(total_earnings):.2f}**"
            ),
            inline=False
        )

    embed.add_field(
        name="💵 Total Pending Across All Users",
        value=f"**${grand_total:.2f}**",
        inline=False
    )

    embed.set_footer(
        text="After Mark Paid, the user automatically disappears from this list."
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

# ============================================================
# ALL USERS
# ============================================================

async def show_all_users(
    interaction
):

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        async with db.execute(
            """
            SELECT
                user_id,
                COUNT(*) AS submissions
            FROM submissions
            GROUP BY user_id
            ORDER BY MAX(id) DESC
            """
        ) as cursor:

            rows = await cursor.fetchall()

    if not rows:

        return await interaction.response.send_message(
            "No users have submitted clips yet.",
            ephemeral=True
        )

    embed = discord.Embed(
        title="👥 All Clip Users",
        description=(
            "Every Discord user who has submitted "
            "at least one clip."
        ),
        color=discord.Color.blue()
    )

    text = ""

    for index, (
        user_id,
        submission_count
    ) in enumerate(rows, start=1):

        line = (
            f"**{index}.** <@{user_id}>\n"
            f"🆔 `{user_id}`\n"
            f"📋 Submissions: **{submission_count}**\n\n"
        )

        if len(text) + len(line) > 3900:

            break

        text += line

    embed.description += (
        "\n\n"
        + text
    )

    if len(rows) > 1:

        embed.set_footer(
            text=(
                f"{len(rows)} users found. "
                "Copy the User ID and use Approved Clips."
            )
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

# ============================================================
# MARK USER PAID
# ============================================================

async def mark_user_paid(
    interaction,
    user_id
):

    rows = await get_user_unpaid_approved_clips(
        user_id
    )

    if not rows:

        return await send_temporary_error(
            interaction,
            f"❌ User `{user_id}` has no unpaid approved earnings."
        )

    amount = sum(
        float(row[4] or 0)
        for row in rows
    )

    total_views = sum(
        int(row[2] or 0)
        for row in rows
    )

    clip_count = len(rows)

    payment = await get_payment_method(
        user_id
    )

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        await db.execute(
            """
            UPDATE submissions
            SET payout_status = 'paid'
            WHERE user_id = ?
            AND status = 'approved'
            AND (
                payout_status = 'unpaid'
                OR payout_status IS NULL
            )
            """,
            (user_id,)
        )

        await db.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, pending, total_paid)
            VALUES (?, 0, 0)
            """,
            (user_id,)
        )

        await db.execute(
            """
            UPDATE users
            SET
                pending = 0,
                total_paid = total_paid + ?
            WHERE user_id = ?
            """,
            (
                amount,
                user_id
            )
        )

        await db.commit()

    await create_payment_record(
        user_id,
        amount,
        "paid"
    )

    if payment:

        payment_text = (
            f"💳 **{payment[0]}**\n"
            f"📌 `{payment[1]}`"
        )

    else:

        payment_text = (
            "⚠️ No payment method saved."
        )

    await interaction.response.send_message(
        f"✅ **User payment marked as paid.**\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"📋 Clips paid: **{clip_count}**\n"
        f"👁️ Total views: **{total_views:,}**\n"
        f"💰 Amount paid: **${amount:.2f}**\n\n"
        f"**Payment Information**\n"
        f"{payment_text}\n\n"
        f"✅ This user has now been removed automatically "
        f"from **Pending Payouts**.",
        ephemeral=True
    )

# ============================================================
# PAYMENT FAILED / REJECTED
# ============================================================

async def process_payment_status(
    interaction,
    user_id,
    status
):

    rows = await get_user_unpaid_approved_clips(
        user_id
    )

    if not rows:

        return await send_temporary_error(
            interaction,
            f"❌ User `{user_id}` has no unpaid approved earnings."
        )

    amount = sum(
        float(row[4] or 0)
        for row in rows
    )

    await create_payment_record(
        user_id,
        amount,
        status
    )

    icon = (
        "⚠️"
        if status == "failed"
        else "🚫"
    )

    await interaction.response.send_message(
        f"{icon} **Payment marked as {status}.**\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"💰 Amount: **${amount:.2f}**\n\n"
        f"⚠️ The user's clips remain unpaid and "
        f"will stay in **Pending Payouts**.",
        ephemeral=True
    )

# ============================================================
# VIEW USER PAYMENT
# ============================================================

async def show_user_payment(
    interaction,
    user_id
):

    payment = await get_payment_method(
        user_id
    )

    if not payment:

        return await send_temporary_error(
            interaction,
            f"❌ User `{user_id}` has no payment method saved."
        )

    user = await get_user(
        user_id
    )

    await interaction.response.send_message(
        f"**Payment Information**\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"👤 User: <@{user_id}>\n"
        f"💳 Method: **{payment[0]}**\n"
        f"📌 Details: `{payment[1]}`\n\n"
        f"⏳ Current balance: **${user['pending']:.2f}**\n"
        f"✅ Total paid historically: **${user['total_paid']:.2f}**",
        ephemeral=True
    )

# ============================================================
# ADMIN VIEW
# ============================================================

class AdminView(View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @button(
        label="View Pending Clips",
        style=discord.ButtonStyle.primary,
        emoji="📥",
        custom_id="admin_pending",
        row=0
    )
    async def view_pending(
        self,
        interaction,
        button
    ):

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT
                    id,
                    user_id,
                    clip_url,
                    submitted_at,
                    views,
                    likes
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
            color=discord.Color.orange()
        )

        for (
            submission_id,
            user_id,
            clip_url,
            submitted_at,
            views,
            likes
        ) in rows:

            estimated = calculate_earnings(
                views
            )

            platform = (
                detect_platform_from_url(
                    clip_url
                )
                or "Unknown"
            )

            embed.add_field(
                name=f"Submission #{submission_id}",
                value=(
                    f"👤 User ID: `{user_id}`\n"
                    f"👤 User: <@{user_id}>\n"
                    f"📱 Platform: **{platform}**\n"
                    f"🔗 [Open Clip]({clip_url})\n"
                    f"👁️ Views: **{views:,}**\n"
                    f"❤️ Likes: **{likes:,}**\n"
                    f"💰 Estimated: **${estimated:.2f}**\n"
                    f"🕐 Submitted: "
                    f"{submitted_at[:16]}"
                ),
                inline=False
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @button(
        label="Approve Clip",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="admin_approve",
        row=0
    )
    async def approve_clip(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            SubmissionIDModal("approve")
        )

    @button(
        label="Reject Clip",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="admin_reject",
        row=0
    )
    async def reject_clip(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            SubmissionIDModal("reject")
        )

    @button(
        label="All Users",
        style=discord.ButtonStyle.secondary,
        emoji="👥",
        custom_id="admin_all_users",
        row=1
    )
    async def all_users(
        self,
        interaction,
        button
    ):

        await show_all_users(
            interaction
        )

    @button(
        label="Pending Payouts",
        style=discord.ButtonStyle.primary,
        emoji="💰",
        custom_id="admin_pending_payouts",
        row=1
    )
    async def pending_payouts(
        self,
        interaction,
        button
    ):

        await show_pending_payouts(
            interaction
        )

    @button(
        label="Approved Clips",
        style=discord.ButtonStyle.success,
        emoji="📊",
        custom_id="admin_approved_clips",
        row=1
    )
    async def approved_clips(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            UserIDModal("approved_clips")
        )

    @button(
        label="Mark Paid",
        style=discord.ButtonStyle.secondary,
        emoji="💸",
        custom_id="admin_paid",
        row=2
    )
    async def mark_paid_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            UserIDModal("mark_paid")
        )

    @button(
        label="Payment Failed",
        style=discord.ButtonStyle.danger,
        emoji="⚠️",
        custom_id="admin_payment_failed",
        row=2
    )
    async def payment_failed_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            UserIDModal("payment_failed")
        )

    @button(
        label="Payment Rejected",
        style=discord.ButtonStyle.danger,
        emoji="🚫",
        custom_id="admin_payment_rejected",
        row=2
    )
    async def payment_rejected_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            UserIDModal("payment_rejected")
        )

    @button(
        label="View User Payment",
        style=discord.ButtonStyle.secondary,
        emoji="💳",
        custom_id="admin_view_payment",
        row=3
    )
    async def view_user_payment_button(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            UserIDModal("view_payment")
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
            "1. Link and verify at least one social account.\n"
            "2. Submit your clip using the button below.\n"
            "3. Clips older than 12 hours will not be accepted.\n\n"
            "**💰 PAYOUT RATE**\n"
            "$0.60 per 1K views\n\n"
            "25K = $15\n"
            "50K = $30\n"
            "100K = $60\n"
            "1M = $600"
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
            "Manage clip submissions and biweekly payouts.\n\n"
            f"**Payout:** ${CPM_RATE:.2f} per 1K views\n\n"
            "📥 Pending Clips → approve/reject clips\n"
            "👥 All Users → find creator Discord IDs\n"
            "📊 Approved Clips → review unpaid creator earnings\n"
            "💰 Pending Payouts → see everyone waiting for payment\n"
            "💸 Mark Paid → clear a creator's current payout"
        ),
        color=discord.Color.dark_grey()
    )

    await interaction.response.send_message(
        embed=embed,
        view=AdminView(),
        ephemeral=True
    )

# ============================================================
# COMMAND ERROR HANDLER
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction,
    error
):

    print(
        f"Command error: {repr(error)}"
    )

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        message = (
            "❌ You need administrator permission "
            "to use this command."
        )

    else:

        message = (
            "❌ Something went wrong "
            "while running that command."
        )

    await send_temporary_error(
        interaction,
        message
    )

# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user}"
    )

    await init_db()

    # Persistent user panel.
    bot.add_view(
        ClipPanel()
    )

    # Persistent admin panel.
    bot.add_view(
        AdminView()
    )

    # Start tracker only once.
    if not track_pending_clips.is_running():

        track_pending_clips.start()

        print(
            "[TRACKER] Live clip tracker started."
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
