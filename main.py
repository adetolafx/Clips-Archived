import discord
from discord.ext import commands
from discord.ui import Button, View, button, Modal, TextInput
from discord import app_commands
import aiosqlite
import datetime
import re
import asyncio
import os
import random
import string
import aiohttp
from urllib.parse import urlparse


# ============================================================
# BOT CONFIG
# ============================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = "clips.db"

# PAYOUT RATE
CPM_RATE = 0.60  # $0.60 per 1,000 views


# ============================================================
# DATABASE
# ============================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:

        # Existing users table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                linked_account TEXT,
                pending REAL DEFAULT 0,
                total_paid REAL DEFAULT 0
            )
        """)

        # Existing submissions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                clip_url TEXT,
                submitted_at TEXT,
                status TEXT DEFAULT 'pending',
                earnings REAL DEFAULT 0
            )
        """)

        # Social accounts
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

        # Payment information
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_methods (
                user_id INTEGER PRIMARY KEY,
                method TEXT,
                details TEXT,
                updated_at TEXT
            )
        """)

        # Add views column to submissions if it doesn't exist.
        try:
            await db.execute(
                "ALTER TABLE submissions ADD COLUMN views INTEGER DEFAULT 0"
            )
        except Exception:
            pass

        await db.commit()


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


async def get_verified_accounts(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, platform, profile_url, status
            FROM social_accounts
            WHERE user_id = ?
            ORDER BY id DESC
        """, (user_id,)) as cursor:
            return await cursor.fetchall()


async def get_payment_method(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT method, details
            FROM payment_methods
            WHERE user_id = ?
        """, (user_id,)) as cursor:
            return await cursor.fetchone()


def generate_verification_code():
    characters = string.ascii_uppercase + string.digits
    return "".join(random.choices(characters, k=10))


def calculate_earnings(views: int):
    return (views / 1000) * CPM_RATE


# ============================================================
# SOCIAL ACCOUNT VERIFICATION
# ============================================================

def valid_profile_url(url: str):
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def platform_matches_url(platform: str, url: str):
    try:
        host = urlparse(url).netloc.lower()

        if platform == "TikTok":
            return "tiktok.com" in host

        if platform == "Instagram":
            return "instagram.com" in host

        if platform == "YouTube":
            return (
                "youtube.com" in host
                or "youtu.be" in host
            )

        return False
    except Exception:
        return False


async def check_profile_for_code(profile_url: str, code: str):
    """
    Attempts to retrieve the public social profile and look for
    the verification code.

    Some platforms may block automated requests. If that happens,
    verification simply remains pending.
    """

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "en-US,en;q=0.9"
    }

    timeout = aiohttp.ClientTimeout(total=15)

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

                return code.lower() in html.lower()

    except Exception:
        return False


# ============================================================
# ACCOUNT MODALS
# ============================================================

class ProfileURLModal(Modal):
    def __init__(self, platform: str):
        super().__init__(title=f"Link {platform}")
        self.platform = platform

        self.profile_url = TextInput(
            label=f"{platform} Profile URL",
            placeholder="https://...",
            required=True,
            max_length=500
        )

        self.add_item(self.profile_url)

    async def on_submit(self, interaction: discord.Interaction):

        url = self.profile_url.value.strip()

        if not valid_profile_url(url):
            return await interaction.response.send_message(
                "❌ Please enter a valid profile URL.",
                ephemeral=True
            )

        if not platform_matches_url(self.platform, url):
            return await interaction.response.send_message(
                f"❌ That doesn't appear to be a valid "
                f"**{self.platform}** profile URL.",
                ephemeral=True
            )

        code = generate_verification_code()

        async with aiosqlite.connect(DB_PATH) as db:

            # Prevent duplicate exact profile URL
            async with db.execute("""
                SELECT id
                FROM social_accounts
                WHERE user_id = ? AND profile_url = ?
            """, (interaction.user.id, url)) as cursor:

                existing = await cursor.fetchone()

            if existing:
                return await interaction.response.send_message(
                    "❌ You already have this account added.",
                    ephemeral=True
                )

            await db.execute("""
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
            """, (
                interaction.user.id,
                self.platform,
                url,
                code,
                datetime.datetime.utcnow().isoformat()
            ))

            await db.commit()

        await interaction.response.send_message(
            f"### 🔐 {self.platform} Verification\n\n"
            f"Your verification code is:\n"
            f"**`{code}`**\n\n"
            f"**Step 1:** Put this exact code in your "
            f"**{self.platform} bio/about section**.\n\n"
            f"**Step 2:** Keep it there temporarily.\n\n"
            f"**Step 3:** Come back and click "
            f"**Check Verification**.\n\n"
            f"⚠️ Your account will **not** be linked until "
            f"the code is found on the public profile.",
            ephemeral=True
        )


class PaymentModal(Modal):
    def __init__(self, method: str):
        super().__init__(title=f"{method} Payment")

        self.method = method

        if method == "Apple Pay":
            placeholder = "Email or Apple Pay contact"
        elif method == "PayPal":
            placeholder = "PayPal email"
        elif method == "Zelle":
            placeholder = "Zelle email or phone number"
        else:
            placeholder = "Venmo username"

        self.details = TextInput(
            label=f"{method} Details",
            placeholder=placeholder,
            required=True,
            max_length=200
        )

        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):

        details = self.details.value.strip()

        if not details:
            return await interaction.response.send_message(
                "❌ Payment details cannot be empty.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO payment_methods
                (user_id, method, details, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    method = excluded.method,
                    details = excluded.details,
                    updated_at = excluded.updated_at
            """, (
                interaction.user.id,
                self.method,
                details,
                datetime.datetime.utcnow().isoformat()
            ))

            await db.commit()

        await interaction.response.send_message(
            f"✅ Payment method saved.\n\n"
            f"**Method:** {self.method}\n"
            f"**Details:** `{details}`",
            ephemeral=True
        )


# ============================================================
# ACCOUNT VIEW
# ============================================================

class AccountView(View):
    def __init__(self):
        super().__init__(timeout=180)

    @button(
        label="TikTok",
        style=discord.ButtonStyle.secondary,
        emoji="🎵",
        row=0
    )
    async def tiktok(
        self,
        interaction: discord.Interaction,
        button: Button
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
        interaction: discord.Interaction,
        button: Button
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
        interaction: discord.Interaction,
        button: Button
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
        interaction: discord.Interaction,
        button: Button
    ):

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT id, platform, profile_url, verification_code
                FROM social_accounts
                WHERE user_id = ? AND status = 'pending'
                ORDER BY id DESC
            """, (interaction.user.id,)) as cursor:

                accounts = await cursor.fetchall()

        if not accounts:
            return await interaction.response.send_message(
                "You don't have any accounts waiting for verification.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        verified = []

        for account_id, platform, profile_url, code in accounts:

            found = await check_profile_for_code(
                profile_url,
                code
            )

            if found:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        UPDATE social_accounts
                        SET status = 'verified',
                            verified_at = ?
                        WHERE id = ?
                    """, (
                        datetime.datetime.utcnow().isoformat(),
                        account_id
                    ))

                    await db.commit()

                verified.append(platform)

        if verified:
            platforms = ", ".join(verified)

            await interaction.followup.send(
                f"✅ Successfully verified: **{platforms}**\n\n"
                f"Those accounts can now be used for clip submissions.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ I couldn't find the verification code yet.\n\n"
                "Make sure:\n"
                "• The code is exactly correct\n"
                "• It is visible in your public bio/about section\n"
                "• Your profile URL is correct\n"
                "• The profile is publicly accessible\n\n"
                "Then try **Check Verification** again.",
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
        interaction: discord.Interaction,
        button: Button
    ):

        accounts = await get_verified_accounts(
            interaction.user.id
        )

        if not accounts:
            return await interaction.response.send_message(
                "You don't have any social accounts.",
                ephemeral=True
            )

        view = RemoveAccountView(accounts)

        await interaction.response.send_message(
            "Select the account you want to remove:",
            view=view,
            ephemeral=True
        )


class RemoveAccountView(View):
    def __init__(self, accounts):
        super().__init__(timeout=120)

        for account_id, platform, profile_url, status in accounts:

            button_obj = Button(
                label=f"{platform} ({status})",
                style=discord.ButtonStyle.danger,
                emoji={
                    "TikTok": "🎵",
                    "Instagram": "📸",
                    "YouTube": "▶️"
                }.get(platform, "🔗")
            )

            async def callback(
                interaction: discord.Interaction,
                account_id=account_id,
                platform=platform
            ):

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        DELETE FROM social_accounts
                        WHERE id = ? AND user_id = ?
                    """, (
                        account_id,
                        interaction.user.id
                    ))

                    await db.commit()

                await interaction.response.send_message(
                    f"🗑️ **{platform}** account removed.",
                    ephemeral=True
                )

            button_obj.callback = callback
            self.add_item(button_obj)


# ============================================================
# PAYMENT VIEW
# ============================================================

class PaymentView(View):
    def __init__(self):
        super().__init__(timeout=180)

    @button(
        label="Apple Pay",
        style=discord.ButtonStyle.secondary,
        emoji="",
        row=0
    )
    async def apple_pay(
        self,
        interaction: discord.Interaction,
        button: Button
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
        interaction: discord.Interaction,
        button: Button
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
        interaction: discord.Interaction,
        button: Button
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
        interaction: discord.Interaction,
        button: Button
    ):
        await interaction.response.send_modal(
            PaymentModal("Venmo")
        )


# ============================================================
# USER PANEL
# ============================================================

class ClipPanel(View):
    def __init__(self):
        super().__init__(timeout=None)

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

        accounts = await get_verified_accounts(
            interaction.user.id
        )

        verified_accounts = [
            account for account in accounts
            if account[3] == "verified"
        ]

        if not verified_accounts:
            return await interaction.response.send_message(
                "❌ You must have at least **one verified "
                "TikTok, Instagram, or YouTube account** "
                "before submitting clips.\n\n"
                "Click **Accounts** to link one.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "Please paste the **full link** of your clip now.\n"
            "⏳ You have **60 seconds**.",
            ephemeral=True
        )

        def check(m):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
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

        if not re.match(r"https?://", clip_url):
            return await interaction.followup.send(
                "❌ That doesn't look like a valid link.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute("""
                INSERT INTO submissions
                (
                    user_id,
                    clip_url,
                    submitted_at,
                    status,
                    earnings,
                    views
                )
                VALUES (?, ?, ?, 'pending', 0, 0)
            """, (
                interaction.user.id,
                clip_url,
                datetime.datetime.utcnow().isoformat()
            ))

            await db.commit()

        await interaction.followup.send(
            f"✅ Clip submitted successfully!\n"
            f"`{clip_url}`",
            ephemeral=True
        )

        try:
            await msg.delete()
        except Exception:
            pass

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

        accounts = await get_verified_accounts(
            interaction.user.id
        )

        if accounts:
            text = "**Your Social Accounts**\n\n"

            for account_id, platform, profile_url, status in accounts:
                icon = {
                    "TikTok": "🎵",
                    "Instagram": "📸",
                    "YouTube": "▶️"
                }.get(platform, "🔗")

                text += (
                    f"{icon} **{platform}** — "
                    f"`{status}`\n"
                    f"{profile_url}\n\n"
                )

        else:
            text = (
                "**Your Social Accounts**\n\n"
                "You don't have any accounts yet.\n\n"
            )

        text += (
            "Choose a platform below to add an account.\n"
            "A verification code will be generated that must "
            "be placed in your bio/about section."
        )

        await interaction.response.send_message(
            text,
            view=AccountView(),
            ephemeral=True
        )

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

        user = await get_user(
            interaction.user.id
        )

        await interaction.response.send_message(
            f"**Your Earnings**\n\n"
            f"💰 Pending: `${user['pending']:.2f}`\n"
            f"✅ Total Paid: `${user['total_paid']:.2f}`\n\n"
            f"📈 Current Rate: **$0.60 / 1K views**",
            ephemeral=True
        )

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
            async with db.execute("""
                SELECT
                    id,
                    clip_url,
                    status,
                    earnings,
                    views
                FROM submissions
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 10
            """, (interaction.user.id,)) as cursor:

                rows = await cursor.fetchall()

        if not rows:
            return await interaction.response.send_message(
                "You have no submissions yet.",
                ephemeral=True
            )

        text = "**Your Last Submissions**\n\n"

        for sub_id, url, status, earnings, views in rows:

            text += (
                f"**Submission #{sub_id}**\n"
                f"🔗 `{url[:45]}{'...' if len(url) > 45 else ''}`\n"
                f"📌 Status: **{status}**\n"
                f"👁️ Views: **{views:,}**\n"
                f"💰 Earnings: **${earnings:.2f}**\n\n"
            )

        await interaction.response.send_message(
            text,
            ephemeral=True
        )

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

        payment = await get_payment_method(
            interaction.user.id
        )

        if payment:
            method, details = payment

            current = (
                f"**Current Payment Method**\n"
                f"💳 {method}\n"
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
# ADMIN APPROVAL MODAL
# ============================================================

class ApproveModal(Modal, title="Approve Clip"):

    def __init__(self, submission_id: int, user_id: int):
        super().__init__()

        self.submission_id = submission_id
        self.user_id = user_id

        self.views = TextInput(
            label="Views",
            placeholder="e.g. 87000",
            required=True,
            max_length=15
        )

        self.add_item(self.views)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:
            views = int(
                self.views.value.replace(",", "").strip()
            )

            if views < 0:
                raise ValueError

        except ValueError:
            return await interaction.response.send_message(
                "❌ Invalid views amount. Enter a whole number "
                "such as `87000`.",
                ephemeral=True
            )

        earnings = calculate_earnings(views)

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute("""
                SELECT user_id, status
                FROM submissions
                WHERE id = ?
            """, (self.submission_id,)) as cursor:

                row = await cursor.fetchone()

            if not row:
                return await interaction.response.send_message(
                    "❌ Submission not found.",
                    ephemeral=True
                )

            if row[1] != "pending":
                return await interaction.response.send_message(
                    "❌ This submission has already been handled.",
                    ephemeral=True
                )

            actual_user_id = row[0]

            await db.execute("""
                UPDATE submissions
                SET
                    status = 'approved',
                    views = ?,
                    earnings = ?
                WHERE id = ?
            """, (
                views,
                earnings,
                self.submission_id
            ))

            await db.execute("""
                UPDATE users
                SET pending = pending + ?
                WHERE user_id = ?
            """, (
                earnings,
                actual_user_id
            ))

            await db.commit()

        await interaction.response.send_message(
            f"✅ **Submission #{self.submission_id} approved!**\n\n"
            f"👤 User ID: `{actual_user_id}`\n"
            f"👁️ Views: **{views:,}**\n"
            f"💰 Added: **${earnings:.2f}**\n\n"
            f"Rate: **$0.60 / 1K views**",
            ephemeral=True
        )


# ============================================================
# ADMIN PANEL
# ============================================================

class AdminView(View):

    def __init__(self):
        super().__init__(timeout=None)

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

            async with db.execute("""
                SELECT
                    id,
                    user_id,
                    clip_url,
                    submitted_at
                FROM submissions
                WHERE status = 'pending'
                ORDER BY id DESC
                LIMIT 15
            """) as cursor:

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

        for sub_id, user_id, url, submitted_at in rows:

            embed.add_field(
                name=f"Submission ID: {sub_id}",
                value=(
                    f"👤 **User ID:** `{user_id}`\n"
                    f"👤 User: <@{user_id}>\n"
                    f"🔗 [Open Clip]({url})\n"
                    f"🕐 Submitted: {submitted_at[:16]}"
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
        emoji="✅"
    )
    async def approve_clip(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_message(
            "Please type the **Submission ID** you want to approve.",
            ephemeral=True
        )

        def check(m):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
            )

        try:
            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=30.0
            )

            sub_id = int(msg.content.strip())

        except (asyncio.TimeoutError, ValueError):
            return await interaction.followup.send(
                "❌ Invalid Submission ID or timed out.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute("""
                SELECT user_id
                FROM submissions
                WHERE id = ?
                AND status = 'pending'
            """, (sub_id,)) as cursor:

                row = await cursor.fetchone()

        if not row:
            return await interaction.followup.send(
                "❌ Submission not found or already handled.",
                ephemeral=True
            )

        # IMPORTANT:
        # Modal must be opened using response.send_modal().
        # Since we already responded above, we cannot open it here.
        #
        # Instead, ask for the views through another message.
        await interaction.followup.send(
            f"**Submission #{sub_id}** belongs to User ID `{row[0]}`.\n"
            f"Type the **number of views** for this clip.",
            ephemeral=True
        )

        try:
            views_msg = await bot.wait_for(
                "message",
                check=check,
                timeout=60.0
            )

            views = int(
                views_msg.content.replace(",", "").strip()
            )

            if views < 0:
                raise ValueError

        except (asyncio.TimeoutError, ValueError):
            return await interaction.followup.send(
                "❌ Invalid views amount or timed out.",
                ephemeral=True
            )

        earnings = calculate_earnings(views)

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute("""
                SELECT user_id, status
                FROM submissions
                WHERE id = ?
            """, (sub_id,)) as cursor:

                submission = await cursor.fetchone()

            if not submission:
                return await interaction.followup.send(
                    "❌ Submission no longer exists.",
                    ephemeral=True
                )

            if submission[1] != "pending":
                return await interaction.followup.send(
                    "❌ This submission has already been handled.",
                    ephemeral=True
                )

            user_id = submission[0]

            await db.execute("""
                UPDATE submissions
                SET
                    status = 'approved',
                    views = ?,
                    earnings = ?
                WHERE id = ?
            """, (
                views,
                earnings,
                sub_id
            ))

            await db.execute("""
                UPDATE users
                SET pending = pending + ?
                WHERE user_id = ?
            """, (
                earnings,
                user_id
            ))

            await db.commit()

        await interaction.followup.send(
            f"✅ **Submission #{sub_id} approved!**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"👁️ Views: **{views:,}**\n"
            f"💰 Earnings added: **${earnings:.2f}**\n\n"
            f"💵 Rate: **$0.60 per 1K views**",
            ephemeral=True
        )

        try:
            await msg.delete()
            await views_msg.delete()
        except Exception:
            pass

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
            "Type the **Submission ID** to reject.",
            ephemeral=True
        )

        def check(m):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
            )

        try:
            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=30.0
            )

            sub_id = int(msg.content.strip())

        except (asyncio.TimeoutError, ValueError):
            return await interaction.followup.send(
                "❌ Invalid Submission ID or timed out.",
                ephemeral=True
            )

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute("""
                SELECT status
                FROM submissions
                WHERE id = ?
            """, (sub_id,)) as cursor:

                row = await cursor.fetchone()

            if not row:
                return await interaction.followup.send(
                    "❌ Submission not found.",
                    ephemeral=True
                )

            if row[0] != "pending":
                return await interaction.followup.send(
                    "❌ This submission has already been handled.",
                    ephemeral=True
                )

            await db.execute("""
                UPDATE submissions
                SET status = 'rejected'
                WHERE id = ?
            """, (sub_id,))

            await db.commit()

        await interaction.followup.send(
            f"❌ Submission **#{sub_id}** has been rejected.",
            ephemeral=True
        )

        try:
            await msg.delete()
        except Exception:
            pass

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
            "Type the **User ID** to mark their pending balance as paid.\n"
            "⚠️ This is the **User ID**, not the Submission ID.",
            ephemeral=True
        )

        def check(m):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
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

        except (asyncio.TimeoutError, ValueError):
            return await interaction.followup.send(
                "❌ Invalid User ID or timed out.",
                ephemeral=True
            )

        user = await get_user(user_id)

        if user["pending"] <= 0:
            return await interaction.followup.send(
                "❌ This user has no pending balance.",
                ephemeral=True
            )

        payment = await get_payment_method(user_id)

        payment_text = "⚠️ No payment method saved."

        if payment:
            method, details = payment

            payment_text = (
                f"💳 **{method}**\n"
                f"📌 `{details}`"
            )

        amount = user["pending"]

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute("""
                UPDATE users
                SET
                    total_paid = total_paid + pending,
                    pending = 0
                WHERE user_id = ?
            """, (user_id,))

            await db.commit()

        await interaction.followup.send(
            f"✅ **Payment marked as sent.**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💰 Amount: **${amount:.2f}**\n\n"
            f"**Payment Information:**\n"
            f"{payment_text}",
            ephemeral=True
        )

        try:
            await msg.delete()
        except Exception:
            pass

    @button(
        label="View User Payment",
        style=discord.ButtonStyle.secondary,
        emoji="💳"
    )
    async def view_user_payment(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        await interaction.response.send_message(
            "Type the **User ID** whose payment information "
            "you want to view.",
            ephemeral=True
        )

        def check(m):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
            )

        try:
            msg = await bot.wait_for(
                "message",
                check=check,
                timeout=30.0
            )

            user_id = int(msg.content.strip())

        except (asyncio.TimeoutError, ValueError):
            return await interaction.followup.send(
                "❌ Invalid User ID or timed out.",
                ephemeral=True
            )

        payment = await get_payment_method(user_id)

        if not payment:
            return await interaction.followup.send(
                f"❌ User `{user_id}` has no payment method saved.",
                ephemeral=True
            )

        method, details = payment

        await interaction.followup.send(
            f"**Payment Information**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💳 Method: **{method}**\n"
            f"📌 Details: `{details}`",
            ephemeral=True
        )

        try:
            await msg.delete()
        except Exception:
            pass


# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(
    name="setup-clips",
    description="Send the public clip submission panel"
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_clips(interaction: discord.Interaction):

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
        color=0x2b2d31
    )

    await interaction.response.send_message(
        embed=embed,
        view=ClipPanel()
    )


@bot.tree.command(
    name="admin",
    description="Open the Admin Panel"
)
@app_commands.checks.has_permissions(administrator=True)
async def admin_panel(interaction: discord.Interaction):

    embed = discord.Embed(
        title="🛠️ Admin Panel",
        description=(
            "Manage clip submissions and payments.\n\n"
            "**Payout:** $0.60 per 1K views"
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

    print(f"Logged in as {bot.user}")

    await init_db()

    bot.add_view(ClipPanel())
    bot.add_view(AdminView())

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Command sync error: {e}")


# ============================================================
# RUN
# ============================================================

bot.run(os.getenv("TOKEN"))
