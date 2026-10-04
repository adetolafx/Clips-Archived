import asyncio
import datetime
import os
import random
import re
import string
from urllib.parse import urlparse

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View, button


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("TOKEN")
DB_PATH = "clips.db"

# $0.60 per 1,000 views
CPM_RATE = 0.60

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
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
                likes INTEGER DEFAULT 0
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

        # Migrate old databases safely.
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
                COALESCE(SUM(earnings), 0)
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

            return await interaction.response.send_message(
                "❌ Please enter a valid profile URL.",
                ephemeral=True
            )

        if not platform_matches_url(
            self.platform,
            url
        ):

            return await interaction.response.send_message(
                f"❌ That doesn't appear to be a valid "
                f"**{self.platform}** profile URL.",
                ephemeral=True
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

                return await interaction.response.send_message(
                    "❌ You already have this account added.",
                    ephemeral=True
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

            return await interaction.response.send_message(
                "You don't have any accounts waiting for verification.",
                ephemeral=True
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

            return await interaction.followup.send(
                f"✅ Successfully verified: "
                f"**{', '.join(verified)}**",
                ephemeral=True
            )

        await interaction.followup.send(
            "❌ I couldn't find the verification code yet.\n\n"
            "Make sure:\n"
            "• The code is exact\n"
            "• It is publicly visible\n"
            "• The profile URL is correct\n"
            "• The account is publicly accessible\n\n"
            "Then try again.",
            ephemeral=True
        )

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

            return await interaction.response.send_message(
                "You don't have any social accounts.",
                ephemeral=True
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

            "Venmo":
                "Venmo username"
        }

        self.details = TextInput(
            label=f"{method} Details",
            placeholder=placeholders[method],
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

            return await interaction.response.send_message(
                "❌ Payment details cannot be empty.",
                ephemeral=True
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
                    method =
                        excluded.method,
                    details =
                        excluded.details,
                    updated_at =
                        excluded.updated_at
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

    # FIXED:
    #  is not a valid Discord emoji.
    # 🍎 is a valid Unicode emoji.
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
        row=1
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

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT id
                FROM social_accounts
                WHERE user_id = ?
                AND status = 'verified'
                LIMIT 1
                """,
                (interaction.user.id,)
            ) as cursor:

                verified = await cursor.fetchone()

        if not verified:

            return await interaction.response.send_message(
                "❌ You must have at least one "
                "**verified TikTok, Instagram, or YouTube account** "
                "before submitting a clip.\n\n"
                "Click **Accounts** to link one.",
                ephemeral=True
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

            return await interaction.followup.send(
                "❌ That doesn't look like a valid link.",
                ephemeral=True
            )

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            await db.execute(
                """
                INSERT INTO submissions
                (
                    user_id,
                    clip_url,
                    submitted_at,
                    status,
                    earnings,
                    views,
                    likes
                )
                VALUES (?, ?, ?, 'pending', 0, 0, 0)
                """,
                (
                    interaction.user.id,
                    clip_url,
                    utc_now()
                )
            )

            await db.commit()

        await interaction.followup.send(
            "✅ Clip submitted successfully!",
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
                    likes
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
            likes
        ) in rows:

            text += (
                f"**Submission #{submission_id}**\n"
                f"📌 Status: **{status}**\n"
                f"👁️ Views: **{views:,}**\n"
                f"❤️ Likes: **{likes:,}**\n"
                f"💰 Earnings: **${earnings:.2f}**\n"
                f"🔗 `{url[:50]}`\n\n"
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
        custom_id="admin_pending"
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
                    submitted_at
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
            submitted_at
        ) in rows:

            embed.add_field(
                name=f"Submission #{submission_id}",
                value=(
                    f"👤 User ID: `{user_id}`\n"
                    f"👤 User: <@{user_id}>\n"
                    f"🔗 [Open Clip]({clip_url})\n"
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
        custom_id="admin_approve"
    )
    async def approve_clip(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            "Type the **Submission ID** you want to approve.",
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
                timeout=30
            )

            submission_id = int(
                message.content.strip()
            )

        except (
            asyncio.TimeoutError,
            ValueError
        ):

            return await interaction.followup.send(
                "❌ Invalid Submission ID or timed out.",
                ephemeral=True
            )

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            async with db.execute(
                """
                SELECT
                    user_id,
                    status
                FROM submissions
                WHERE id = ?
                """,
                (submission_id,)
            ) as cursor:

                row = await cursor.fetchone()

        if not row:

            return await interaction.followup.send(
                "❌ Submission not found.",
                ephemeral=True
            )

        if row[1] != "pending":

            return await interaction.followup.send(
                "❌ This submission has already been handled.",
                ephemeral=True
            )

        await interaction.followup.send(
            f"Submission **#{submission_id}** found.\n\n"
            f"Type the **views and likes** separated by a space.\n"
            f"Example: `87000 4200`",
            ephemeral=True
        )

        try:

            stats_message = await bot.wait_for(
                "message",
                check=check,
                timeout=60
            )

            parts = (
                stats_message
                .content
                .replace(",", "")
                .split()
            )

            if len(parts) != 2:
                raise ValueError

            views = int(parts[0])
            likes = int(parts[1])

            if views < 0 or likes < 0:
                raise ValueError

        except (
            asyncio.TimeoutError,
            ValueError
        ):

            return await interaction.followup.send(
                "❌ Invalid views/likes or timed out.",
                ephemeral=True
            )

        earnings = calculate_earnings(
            views
        )

        user_id = row[0]

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            await db.execute(
                """
                UPDATE submissions
                SET
                    status = 'approved',
                    views = ?,
                    likes = ?,
                    earnings = ?
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
                (user_id)
                VALUES (?)
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

        await interaction.followup.send(
            f"✅ **Submission #{submission_id} approved!**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"👁️ Views: **{views:,}**\n"
            f"❤️ Likes: **{likes:,}**\n"
            f"💰 Added: **${earnings:.2f}**",
            ephemeral=True
        )

        try:

            await message.delete()
            await stats_message.delete()

        except Exception:

            pass

    @button(
        label="Reject Clip",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="admin_reject"
    )
    async def reject_clip(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            "Type the **Submission ID** to reject.",
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
                timeout=30
            )

            submission_id = int(
                message.content.strip()
            )

        except (
            asyncio.TimeoutError,
            ValueError
        ):

            return await interaction.followup.send(
                "❌ Invalid ID or timed out.",
                ephemeral=True
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

                return await interaction.followup.send(
                    "❌ Submission not found.",
                    ephemeral=True
                )

            if row[0] != "pending":

                return await interaction.followup.send(
                    "❌ Submission already handled.",
                    ephemeral=True
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

        await interaction.followup.send(
            f"❌ Submission **#{submission_id}** rejected.",
            ephemeral=True
        )

        try:

            await message.delete()

        except Exception:

            pass

    @button(
        label="Mark Paid",
        style=discord.ButtonStyle.secondary,
        emoji="💸",
        custom_id="admin_paid"
    )
    async def mark_paid(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            "Type the **User ID** whose pending balance "
            "you want to mark as paid.",
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
                timeout=30
            )

            user_id = int(
                message.content.strip()
            )

        except (
            asyncio.TimeoutError,
            ValueError
        ):

            return await interaction.followup.send(
                "❌ Invalid User ID or timed out.",
                ephemeral=True
            )

        user = await get_user(
            user_id
        )

        if user["pending"] <= 0:

            return await interaction.followup.send(
                "❌ This user has no pending balance.",
                ephemeral=True
            )

        amount = user["pending"]

        payment = await get_payment_method(
            user_id
        )

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            await db.execute(
                """
                UPDATE users
                SET
                    total_paid =
                        total_paid + pending,
                    pending = 0
                WHERE user_id = ?
                """,
                (user_id,)
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

        await interaction.followup.send(
            f"✅ **Payment marked as paid.**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💰 Amount: **${amount:.2f}**\n\n"
            f"**Payment Information**\n"
            f"{payment_text}",
            ephemeral=True
        )

        try:

            await message.delete()

        except Exception:

            pass

    @button(
        label="Payment Failed",
        style=discord.ButtonStyle.danger,
        emoji="⚠️",
        custom_id="admin_payment_failed"
    )
    async def payment_failed(
        self,
        interaction,
        button
    ):

        await self.payment_status_flow(
            interaction,
            "failed"
        )

    @button(
        label="Payment Rejected",
        style=discord.ButtonStyle.danger,
        emoji="🚫",
        custom_id="admin_payment_rejected"
    )
    async def payment_rejected(
        self,
        interaction,
        button
    ):

        await self.payment_status_flow(
            interaction,
            "rejected"
        )

    async def payment_status_flow(
        self,
        interaction,
        status
    ):

        await interaction.response.send_message(
            f"Type the **User ID** whose pending payment "
            f"should be marked **{status}**.",
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
                timeout=30
            )

            user_id = int(
                message.content.strip()
            )

        except (
            asyncio.TimeoutError,
            ValueError
        ):

            return await interaction.followup.send(
                "❌ Invalid User ID or timed out.",
                ephemeral=True
            )

        user = await get_user(
            user_id
        )

        if user["pending"] <= 0:

            return await interaction.followup.send(
                "❌ This user has no pending balance.",
                ephemeral=True
            )

        amount = user["pending"]

        # IMPORTANT:
        # Failed/rejected does not erase pending balance.
        await create_payment_record(
            user_id,
            amount,
            status
        )

        await interaction.followup.send(
            f"{'⚠️' if status == 'failed' else '🚫'} "
            f"Payment marked as **{status}**.\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💰 Amount: **${amount:.2f}**\n\n"
            f"The user's pending balance remains available.",
            ephemeral=True
        )

        try:

            await message.delete()

        except Exception:

            pass

    @button(
        label="View User Payment",
        style=discord.ButtonStyle.secondary,
        emoji="💳",
        custom_id="admin_view_payment"
    )
    async def view_user_payment(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            "Type the **User ID** whose payment information "
            "you want to view.",
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
                timeout=30
            )

            user_id = int(
                message.content.strip()
            )

        except (
            asyncio.TimeoutError,
            ValueError
        ):

            return await interaction.followup.send(
                "❌ Invalid User ID or timed out.",
                ephemeral=True
            )

        payment = await get_payment_method(
            user_id
        )

        if not payment:

            return await interaction.followup.send(
                f"❌ User `{user_id}` has no payment method saved.",
                ephemeral=True
            )

        await interaction.followup.send(
            f"**Payment Information**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💳 Method: **{payment[0]}**\n"
            f"📌 Details: `{payment[1]}`",
            ephemeral=True
        )

        try:

            await message.delete()

        except Exception:

            pass


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
            "Manage clip submissions and payments.\n\n"
            f"**Payout:** ${CPM_RATE:.2f} per 1K views"
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

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except Exception as e:

        print(
            f"Could not send error message: {e}"
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

    # Persistent views.
    bot.add_view(
        ClipPanel()
    )

    bot.add_view(
        AdminView()
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
