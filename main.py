import discord
from discord.ext import commands
from discord.ui import Button, View, button, Modal, TextInput
from discord import app_commands
import aiosqlite
import datetime
import re
import asyncio
import os

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = "clips.db"

# ====================== DATABASE ======================
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
                earnings REAL DEFAULT 0
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                await db.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
                await db.commit()
                return {"user_id": user_id, "linked_account": None, "pending": 0.0, "total_paid": 0.0}
            return {
                "user_id": row[0],
                "linked_account": row[1],
                "pending": row[2],
                "total_paid": row[3]
            }

# ====================== USER PANEL ======================
class ClipPanel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Submit Video", style=discord.ButtonStyle.primary, emoji="📩", custom_id="submit_video", row=0)
    async def submit_video(self, interaction: discord.Interaction, button: Button):
        user = await get_user(interaction.user.id)
        if not user["linked_account"]:
            return await interaction.response.send_message(
                "❌ You must link an account first! Click the **Accounts** button.", ephemeral=True)

        await interaction.response.send_message(
            "Please paste the **full link** of your clip now.\n⏳ You have **60 seconds**.", ephemeral=True)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await bot.wait_for("message", check=check, timeout=60.0)
        except asyncio.TimeoutError:
            return await interaction.followup.send("⏰ Timed out. Please try again.", ephemeral=True)

        clip_url = msg.content.strip()
        if not re.match(r"https?://", clip_url):
            return await interaction.followup.send("❌ That doesn't look like a valid link.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO submissions (user_id, clip_url, submitted_at) VALUES (?, ?, ?)",
                (interaction.user.id, clip_url, datetime.datetime.utcnow().isoformat())
            )
            await db.commit()

        await interaction.followup.send(f"✅ Clip submitted successfully!\n`{clip_url}`", ephemeral=True)
        try:
            await msg.delete()
        except:
            pass

    @button(label="Accounts", style=discord.ButtonStyle.secondary, emoji="🪪", custom_id="accounts", row=0)
    async def accounts(self, interaction: discord.Interaction, button: Button):
        user = await get_user(interaction.user.id)
        if user["linked_account"]:
            return await interaction.response.send_message(
                f"✅ Currently linked: **{user['linked_account']}**\nReply with a new one to change it.", ephemeral=True)

        await interaction.response.send_message(
            "Reply with your account (example: `TikTok @username`):", ephemeral=True)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await bot.wait_for("message", check=check, timeout=60.0)
        except asyncio.TimeoutError:
            return await interaction.followup.send("⏰ Timed out.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET linked_account = ? WHERE user_id = ?", (msg.content.strip(), interaction.user.id))
            await db.commit()

        await interaction.followup.send(f"✅ Account linked: **{msg.content.strip()}**", ephemeral=True)

    @button(label="Say Hi", style=discord.ButtonStyle.secondary, emoji="👋", custom_id="say_hi", row=1)
    async def say_hi(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(f"Hello {interaction.user.mention}! 👋", ephemeral=True)

    @button(label="Check Earnings", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="check_earnings", row=1)
    async def check_earnings(self, interaction: discord.Interaction, button: Button):
        user = await get_user(interaction.user.id)
        await interaction.response.send_message(
            f"**Your Earnings**\n💰 Pending: `${user['pending']:.2f}`\n✅ Total Paid: `${user['total_paid']:.2f}`",
            ephemeral=True)

    @button(label="Submission History", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="submission_history", row=2)
    async def submission_history(self, interaction: discord.Interaction, button: Button):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT clip_url, status, earnings FROM submissions WHERE user_id = ? ORDER BY id DESC LIMIT 10",
                (interaction.user.id,)
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await interaction.response.send_message("You have no submissions yet.", ephemeral=True)

        text = "**Your last submissions:**\n"
        for url, status, earnings in rows:
            text += f"• `{url[:45]}...` → **{status}** (${earnings:.2f})\n"
        await interaction.response.send_message(text, ephemeral=True)

    @button(label="Payment", style=discord.ButtonStyle.secondary, emoji="💳", custom_id="payment", row=2)
    async def payment(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Reply with your payment details (example: `PayPal email@example.com`):", ephemeral=True)

# ====================== ADMIN PANEL ======================
class ApproveModal(Modal, title="Approve Clip"):
    def __init__(self, submission_id: int, user_id: int):
        super().__init__()
        self.submission_id = submission_id
        self.user_id = user_id
        self.amount = TextInput(label="Earnings amount ($)", placeholder="e.g. 5.00", required=True)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = float(self.amount.value)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid number.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE submissions SET status = 'approved', earnings = ? WHERE id = ?",
                (amount, self.submission_id)
            )
            await db.execute(
                "UPDATE users SET pending = pending + ? WHERE user_id = ?",
                (amount, self.user_id)
            )
            await db.commit()

        await interaction.response.send_message(f"✅ Approved! Added **${amount:.2f}** to the user.", ephemeral=True)

class AdminView(View):
    def __init__(self):
        super().__init__(timeout=120)

    @button(label="View Pending Clips", style=discord.ButtonStyle.primary, emoji="📥")
    async def view_pending(self, interaction: discord.Interaction, button: Button):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, user_id, clip_url, submitted_at FROM submissions WHERE status = 'pending' ORDER BY id DESC LIMIT 15"
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await interaction.response.send_message("No pending clips.", ephemeral=True)

        embed = discord.Embed(title="Pending Clips", color=discord.Color.orange())
        for sub_id, user_id, url, submitted_at in rows:
            embed.add_field(
                name=f"ID: {sub_id} | User: <@{user_id}>",
                value=f"[Link]({url})\nSubmitted: {submitted_at[:16]}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @button(label="Approve Clip", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_clip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Please type the **Submission ID** you want to approve:", ephemeral=True)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await bot.wait_for("message", check=check, timeout=30.0)
            sub_id = int(msg.content.strip())
        except:
            return await interaction.followup.send("❌ Invalid ID or timed out.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM submissions WHERE id = ? AND status = 'pending'", (sub_id,)) as cursor:
                row = await cursor.fetchone()

        if not row:
            return await interaction.followup.send("❌ Submission not found or already handled.", ephemeral=True)

        modal = ApproveModal(sub_id, row[0])
        await interaction.followup.send_modal(modal)

    @button(label="Reject Clip", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject_clip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Type the **Submission ID** to reject:", ephemeral=True)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await bot.wait_for("message", check=check, timeout=30.0)
            sub_id = int(msg.content.strip())
        except:
            return await interaction.followup.send("❌ Invalid ID or timed out.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE submissions SET status = 'rejected' WHERE id = ?", (sub_id,))
            await db.commit()

        await interaction.followup.send(f"✅ Submission `{sub_id}` has been rejected.", ephemeral=True)

    @button(label="Mark as Paid", style=discord.ButtonStyle.secondary, emoji="💸")
    async def mark_paid(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Type the **User ID** (or mention) to mark their pending balance as paid:", ephemeral=True)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await bot.wait_for("message", check=check, timeout=30.0)
            content = msg.content.strip()
            if content.startswith("<@"):
                user_id = int(content.replace("<@", "").replace("!", "").replace(">", ""))
            else:
                user_id = int(content)
        except:
            return await interaction.followup.send("❌ Invalid user.", ephemeral=True)

        user = await get_user(user_id)
        if user["pending"] <= 0:
            return await interaction.followup.send("This user has no pending balance.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET total_paid = total_paid + pending, pending = 0 WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()

        await interaction.followup.send(
            f"✅ Marked **${user['pending']:.2f}** as paid for <@{user_id}>.", ephemeral=True)

# ====================== COMMANDS ======================
@bot.tree.command(name="setup-clips", description="Send the public clip submission panel")
@app_commands.checks.has_permissions(administrator=True)
async def setup_clips(interaction: discord.Interaction):
    embed = discord.Embed(
        title="why Bebe ??? 😻💦",
        description=(
            "**INSTRUCTIONS**\n\n"
            "Please link an account before submitting clips. Use the buttons below to manage your account, "
            "submit clips, check earnings, view submission history, and manage payments.\n\n"
            "**Clips older than 12 hours will not be accepted.**"
        ),
        color=0x2b2d31
    )
    await interaction.response.send_message(embed=embed, view=ClipPanel())

@bot.tree.command(name="admin", description="Open the Admin Panel")
@app_commands.checks.has_permissions(administrator=True)
async def admin_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛠️ Admin Panel",
        description="Manage clip submissions and payments.",
        color=discord.Color.dark_grey()
    )
    await interaction.response.send_message(embed=embed, view=AdminView(), ephemeral=True)

# ====================== READY ======================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await init_db()
    bot.add_view(ClipPanel())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

bot.run(os.getenv("TOKEN"))
