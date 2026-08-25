# main.py — lv-onboarding
# Onboarding bot za novajlije: ticket sistem, domaći pregled (AI), self-serve biranje smene
import os
import re
import sqlite3
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Select, Modal, TextInput
from discord import TextStyle
from dotenv import load_dotenv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from openai import OpenAI
from aiohttp import web

# --- env first ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
USE_AI = os.getenv("USE_AI", "true").lower() in ("1", "true", "yes", "on")

DB_PATH = os.getenv("DB_PATH", "/data/onboarding.db")
BRIDGE_PORT = int(os.getenv("PORT", os.getenv("BRIDGE_PORT", "8080")))

client = OpenAI(api_key=OPENAI_API_KEY) if (USE_AI and OPENAI_API_KEY) else None
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN nije setovan u .env")

# ---------- TUNABLES ----------
SLEEP_BETWEEN_CALLS = 0.35
REMINDER_STAGE_HOURS = 6  # svaka faza podsetnika na 6 sati

# ---------- BOT ----------
INTENTS = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None


def _local_now():
    return datetime.now(ZoneInfo("Europe/Belgrade"))


# ==================== CONFIG (iz .env) ====================
def _env_int(key):
    v = os.getenv(key, "").strip()
    return int(v) if v.isdigit() else None


def _env_int_list(key):
    v = os.getenv(key, "")
    return [int(x.strip()) for x in v.split(",") if x.strip().isdigit()]


TICKET_CATEGORY_ID = _env_int("TICKET_CATEGORY_ID")
INEXPERIENCED_CATEGORY_ID = _env_int("INEXPERIENCED_CATEGORY_ID")
EXPERIENCED_CATEGORY_ID = _env_int("EXPERIENCED_CATEGORY_ID")
TRANSCRIPT_CATEGORY_ID = _env_int("TRANSCRIPT_CATEGORY_ID")

SHIFT_GRAVEYARD_ROLE_ID = _env_int("SHIFT_GRAVEYARD_ROLE_ID")
SHIFT_AFTERNOON_ROLE_ID = _env_int("SHIFT_AFTERNOON_ROLE_ID")
SHIFT_MAIN_ROLE_ID = _env_int("SHIFT_MAIN_ROLE_ID")
# Eskalacija podsetnika (redom): svakih 6h se taguje sledeća rola u listi
REMINDER_ROLE_IDS = _env_int_list("REMINDER_ROLE_IDS")
SUPPORT_ROLE_IDS = _env_int_list("SUPPORT_ROLE_IDS")

# Sve role koje vide tickete, bivaju pingovane pri otvaranju i smeju da kliknu ✅
REVIEW_ROLE_IDS = list(dict.fromkeys(SUPPORT_ROLE_IDS + REMINDER_ROLE_IDS))

SHIFT_ROLE_MAP = {
    "graveyard": SHIFT_GRAVEYARD_ROLE_ID,
    "afternoon": SHIFT_AFTERNOON_ROLE_ID,
    "main": SHIFT_MAIN_ROLE_ID,
}

# Satnice smena (potvrđeno: graveyard ~10:00, afternoon ~18:00, main ~02:00)
SHIFT_SCHEDULE = {
    "graveyard": "~10:00",
    "afternoon": "~18:00",
    "main": "~02:00",
}
SHIFT_LABELS = {"graveyard": "GRAVEYARD", "afternoon": "AFTERNOON", "main": "MAIN"}


# ==================== DB ====================
def _db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domaci (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE,
            channel_id INTEGER,
            user_id INTEGER,
            created_at TEXT,
            stage INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def get_state(key):
    conn = _db()
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_state(key, value):
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, str(value))
    )
    conn.commit()
    conn.close()


def add_domaci(message_id, channel_id, user_id, created_at):
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO domaci (message_id, channel_id, user_id, created_at) VALUES (?,?,?,?)",
        (message_id, channel_id, user_id, created_at),
    )
    conn.commit()
    conn.close()


def get_pending_domaci():
    conn = _db()
    rows = conn.execute("SELECT * FROM domaci WHERE done = 0").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_domaci_by_message_id(message_id):
    conn = _db()
    row = conn.execute(
        "SELECT * FROM domaci WHERE message_id = ?", (message_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_domaci_stage(message_id, stage):
    conn = _db()
    conn.execute(
        "UPDATE domaci SET stage = ? WHERE message_id = ?", (stage, message_id)
    )
    conn.commit()
    conn.close()


def mark_domaci_done(message_id):
    conn = _db()
    conn.execute(
        "UPDATE domaci SET done = 1 WHERE message_id = ?", (message_id,)
    )
    conn.commit()
    conn.close()


# ==================== UTILS ====================
def get_user_shift(member):
    found = [
        name
        for name, rid in SHIFT_ROLE_MAP.items()
        if rid and any(r.id == rid for r in member.roles)
    ]
    if len(found) == 1:
        return found[0]
    if len(found) == 0:
        return None
    return "multiple"


def can_touch_role(bot_member: discord.Member, role: discord.Role) -> bool:
    if role is None:
        return False
    if role.is_default():
        return False
    if role.managed:
        return False
    return bot_member.guild_permissions.manage_roles and bot_member.top_role > role


def is_support(member):
    if member.guild_permissions.manage_roles or member.guild_permissions.administrator:
        return True
    return any(r.id in REVIEW_ROLE_IDS for r in member.roles)


async def safe_add_roles(member, roles, reason):
    added = []
    for role in roles:
        if role is None:
            continue
        for attempt in range(1, 6):
            try:
                await member.add_roles(role, reason=reason)
                added.append(role)
                await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                break
            except discord.Forbidden:
                raise
            except Exception:
                if attempt >= 5:
                    raise
                await asyncio.sleep(0.8 * attempt)
    return added


async def safe_remove_roles(member, roles, reason):
    removed = []
    for role in roles:
        if role is None:
            continue
        for attempt in range(1, 6):
            try:
                await member.remove_roles(role, reason=reason)
                removed.append(role)
                await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                break
            except discord.Forbidden:
                raise
            except Exception:
                if attempt >= 5:
                    raise
                await asyncio.sleep(0.8 * attempt)
    return removed


def need_manage_roles():
    def predicate(interaction: discord.Interaction):
        gp = interaction.user.guild_permissions
        if gp.manage_roles or gp.administrator:
            return True
        raise app_commands.CheckFailure("treba ti Manage Roles.")

    return app_commands.check(predicate)


# ==================== AI — pregled domaćeg ====================
DOMACI_SYSTEM_PROMPT = os.getenv(
    "DOMACI_PROMPT",
    (
        "Ti si kontrolor zadatka ('domaći') za novajlije u OnlyFans management timu.\n"
        "Novajlija šalje tekst koji sadrži: (1) mass message i (2) ispisane PPV poruke.\n"
        "Tvoj zadatak je da proveriš da li je zadatak ispravno odrađen i daš kratku povratnu informaciju.\n\n"
        "Proveri sledeće:\n"
        "- Mass message: pravilno formatiran (bez grešaka, prikladna dužina, poziv na akciju, profesionalan ton, prikladna upotreba emojija).\n"
        "- PPV poruke: ispravno ispisane, u skladu sa traženim formatom, dovoljno dugačke (kvota reči), zanimljive i prodajne.\n"
        "- Da li su ispunjene kvote reči/formata za oba dela.\n\n"
        "Format odgovora:\n"
        "- Ako je sve ispravno, odgovori samo: ✅ OK\n"
        "- Ako nešto fali, navedi kratko i konkretno šta tačno treba popraviti (nabrojano), na srpskom."
    ),
)


async def check_domaci(text: str) -> str:
    if not client:
        return "⚠️ AI nije dostupan (nema OPENAI_API_KEY ili USE_AI=false)."
    def _call():
        rsp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": DOMACI_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=300,
        )
        return rsp.choices[0].message.content.strip()

    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        print("[DOMACI] AI fail:", e)
        return "⚠️ AI pregled nije uspeo — proverite ručno."


class DomaciModal(Modal, title="Domaći"):
    def __init__(self):
        super().__init__(timeout=None)
        self.domaci = TextInput(
            label="Zalepi domaći",
            style=TextStyle.paragraph,
            placeholder="Mass message + PPV poruke…",
            required=True,
            max_length=4000,
        )
        self.add_item(self.domaci)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.domaci.value.strip()
        await interaction.response.defer(thinking=True)
        if not text:
            return await interaction.followup.send("❌ Prazan domaći.", ephemeral=True)

        created = _local_now()
        # 1) pošalji domaći tekst (prva poruka = referenca za ✅ i podsetnik)
        chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)]
        first = None
        for idx, ch in enumerate(chunks):
            content = f"📝 **Domaći** — {interaction.user.mention}\n\n{ch}" if idx == 0 else ch
            msg = await interaction.followup.send(content, wait=True)
            if idx == 0:
                first = msg

        # 2) registruj u DB (pre AI, da podsetnik radi i ako AI padne)
        if first:
            add_domaci(first.id, interaction.channel.id, interaction.user.id, created.isoformat())
            try:
                await first.add_reaction("✅")
            except Exception:
                pass

        # 3) AI pregled
        result = await check_domaci(text)
        await interaction.followup.send(
            f"🤖 **AI pregled:**\n\n{result}\n\n"
            f"Support: klikni ✅ na gornju poruku kad pregledaš (zaustavlja podsetnik)."
        )


@tree.command(name="domaci", description="Pošalji domaći na pregled (u svom ticketu)", guild=GUILD_OBJ)
async def domaci(interaction: discord.Interaction):
    if not _is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(
            "❌ Domaći šalješ unutar svog ticket kanala.", ephemeral=True
        )
    await interaction.response.send_modal(DomaciModal())


# ==================== TICKET FLOW ====================
SHIFT_OPTIONS = [
    discord.SelectOption(label="GRAVEYARD", value="graveyard"),
    discord.SelectOption(label="AFTERNOON", value="afternoon"),
    discord.SelectOption(label="MAIN", value="main"),
]

LEVEL_OPTIONS = [
    discord.SelectOption(label="INEXPERIENCED", value="inexperienced"),
    discord.SelectOption(label="EXPERIENCED", value="experienced"),
]


class TicketFlowView(View):
    def __init__(self):
        super().__init__(timeout=1800)
        self.shift = None
        self.level = None
        self._shift_select = Select(
            placeholder="Izaberi smenu", options=SHIFT_OPTIONS
        )
        self._shift_select.callback = self.on_shift
        self.add_item(self._shift_select)

    async def on_shift(self, interaction: discord.Interaction):
        self.shift = self._shift_select.values[0]
        hours = SHIFT_SCHEDULE.get(self.shift, "")
        self.clear_items()
        self._level_select = Select(
            placeholder="Izaberi nivo iskustva", options=LEVEL_OPTIONS
        )
        self._level_select.callback = self.on_level
        self.add_item(self._level_select)
        await interaction.response.edit_message(
            content=(
                f"✅ Smena: **{SHIFT_LABELS.get(self.shift, self.shift)}**\n"
                f"🕐 Satnica: **{hours}**\n\n"
                f"Sada izaberi nivo iskustva:"
            ),
            view=self,
        )

    async def on_level(self, interaction: discord.Interaction):
        self.level = self._level_select.values[0]
        self.disable_all_items()
        await interaction.response.edit_message(
            content=(
                f"✅ Smena: **{SHIFT_LABELS.get(self.shift, self.shift)}** "
                f"({SHIFT_SCHEDULE.get(self.shift, '')})\n"
                f"✅ Nivo: **{self.level.upper()}**\n\n"
                f"Ticket se premešta…"
            ),
            view=self,
        )
        target_id = (
            INEXPERIENCED_CATEGORY_ID
            if self.level == "inexperienced"
            else EXPERIENCED_CATEGORY_ID
        )
        target = interaction.guild.get_channel(target_id) if target_id else None
        try:
            if target and interaction.channel:
                await interaction.channel.edit(
                    name=f"ticket-{self.level}-{interaction.user.name}",
                    category=target,
                    reason=f"Routing by {interaction.user}",
                )
            await interaction.followup.send(
                f"✅ Ticket premešten u kategoriju **{target.name if target else 'nepoznatu'}**."
            )
        except Exception as e:
            print("[TICKET] routing fail:", e)


@tree.command(name="ticket", description="Otvori novi ticket", guild=GUILD_OBJ)
async def ticket(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    if not category:
        return await interaction.followup.send(
            "❌ TICKET_CATEGORY_ID nije validan.", ephemeral=True
        )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_messages=True
        ),
    }
    for rid in REVIEW_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_messages=True
            )

    try:
        ch = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket by {interaction.user}",
        )
    except Exception as e:
        return await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)

    mentions = [interaction.user.mention] + [f"<@&{rid}>" for rid in REVIEW_ROLE_IDS]
    await ch.send(" ".join(mentions))
    await ch.send(
        "🎟️ **Novi ticket**\n"
        f"Korisnik: {interaction.user.mention}\n\n"
        "Izaberi svoju smenu:",
        view=TicketFlowView(),
    )
    await interaction.followup.send(
        f"✅ Ticket otvoren → {ch.mention}", ephemeral=True
    )


# ==================== /close + /delete ====================
def _is_ticket_channel(channel):
    return channel and channel.name.startswith("ticket-")


@tree.command(
    name="close", description="Zatvori ticket i sačuvaj transcript", guild=GUILD_OBJ
)
async def close_ticket(interaction: discord.Interaction):
    if not _is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(
            "❌ Ova komanda radi samo unutar ticketa.", ephemeral=True
        )
    await interaction.response.send_message(
        "**Zatvaram ticket i čuvam transcript...**", ephemeral=False
    )
    transcript_cat = (
        interaction.guild.get_channel(TRANSCRIPT_CATEGORY_ID)
        if TRANSCRIPT_CATEGORY_ID
        else None
    )
    if transcript_cat:
        try:
            messages = [
                m
                async for m in interaction.channel.history(
                    limit=1000, oldest_first=True
                )
            ]
            lines = [
                f"**Transcript ticketa:** {interaction.channel.name}",
                f"**Vreme zatvaranja:** {_local_now().strftime('%d.%m.%Y %H:%M')}",
                "=" * 60,
                "",
            ]
            for m in messages:
                t = m.created_at.strftime("%d.%m.%Y %H:%M")
                lines.append(f"[{t}] {m.author.display_name}: {m.content}")
                if m.attachments:
                    lines.append(
                        f"   Prilozi: {', '.join(a.url for a in m.attachments)}"
                    )
            transcript_text = "\n".join(lines)
            tc = await transcript_cat.create_text_channel(
                name=f"transcript-{interaction.channel.name.replace('ticket-', '')}"
            )
            for i in range(0, len(transcript_text), 1900):
                await tc.send(transcript_text[i : i + 1900] or "prazno")
        except Exception as e:
            print(f"Transcript error: {e}")
    await asyncio.sleep(2)
    try:
        await interaction.channel.delete(
            reason=f"Closed with transcript by {interaction.user}"
        )
    except Exception:
        await interaction.followup.send(
            "Ticket zatvoren, ali transcript nije uspeo da se sačuva.", ephemeral=True
        )


@tree.command(
    name="delete", description="Obriši ticket bez čuvanja transcripta", guild=GUILD_OBJ
)
async def delete_ticket(interaction: discord.Interaction):
    if not _is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(
            "❌ Ova komanda radi samo unutar ticketa.", ephemeral=True
        )
    await interaction.response.send_message("**Brišem ticket...**", ephemeral=False)
    await asyncio.sleep(2)
    try:
        await interaction.channel.delete(reason=f"Deleted by {interaction.user}")
    except Exception as e:
        await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)


# ==================== SELF-SERVE SMENA ====================
@tree.command(
    name="shift", description="Izaberi svoju smenu (self-serve)", guild=GUILD_OBJ
)
@app_commands.choices(
    smena=[
        app_commands.Choice(name="GRAVEYARD", value="graveyard"),
        app_commands.Choice(name="AFTERNOON", value="afternoon"),
        app_commands.Choice(name="MAIN", value="main"),
    ]
)
async def shift(interaction: discord.Interaction, smena: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    target_id = SHIFT_ROLE_MAP.get(smena)
    target_role = guild.get_role(target_id) if target_id else None
    if not target_role:
        return await interaction.followup.send(
            "❌ Ta smenska rola nije konfigurisana (nedostaje ID u .env).",
            ephemeral=True,
        )
    all_ids = {
        SHIFT_GRAVEYARD_ROLE_ID,
        SHIFT_AFTERNOON_ROLE_ID,
        SHIFT_MAIN_ROLE_ID,
    }
    to_remove = [
        guild.get_role(rid)
        for rid in all_ids
        if rid and rid != target_id
    ]
    to_remove = [r for r in to_remove if r and r in interaction.user.roles]
    try:
        await safe_remove_roles(interaction.user, to_remove, reason=f"/shift by {interaction.user}")
        await safe_add_roles(interaction.user, [target_role], reason=f"/shift by {interaction.user}")
    except discord.Forbidden as e:
        return await interaction.followup.send(
            f"❌ Nemam permisije za role: {e}", ephemeral=True
        )
    await interaction.followup.send(
        f"✅ Dodeljena smena **{SHIFT_LABELS.get(smena, smena)}** "
        f"(satnica {SHIFT_SCHEDULE.get(smena, '')}).",
        ephemeral=True,
    )


# ==================== 6H REMINDER LOOP ====================
@tasks.loop(minutes=10)
async def domaci_reminder_loop():
    if not REMINDER_ROLE_IDS:
        return
    now = _local_now()
    for d in get_pending_domaci():
        channel = bot.get_channel(d["channel_id"]) if d["channel_id"] else None
        if not channel:
            continue
        try:
            created = datetime.fromisoformat(d["created_at"])
        except Exception:
            continue
        hours_elapsed = (now - created).total_seconds() / 3600
        while (
            d["stage"] < len(REMINDER_ROLE_IDS)
            and hours_elapsed >= (d["stage"] + 1) * REMINDER_STAGE_HOURS
        ):
            role_id = REMINDER_ROLE_IDS[d["stage"]]
            link = (
                f"https://discord.com/channels/{GUILD_ID}/{d['channel_id']}/{d['message_id']}"
                if GUILD_ID
                else ""
            )
            try:
                await channel.send(
                    f"<@&{role_id}> 🔔 Podsetnik za pregled domaćeg od <@{d['user_id']}> "
                    f"(prošlo {(d['stage'] + 1) * REMINDER_STAGE_HOURS}h).\n{link}"
                )
            except Exception as e:
                print("[REMINDER] send fail:", e)
                break
            d["stage"] += 1
            set_domaci_stage(d["message_id"], d["stage"])
            await asyncio.sleep(SLEEP_BETWEEN_CALLS)


@domaci_reminder_loop.before_loop
async def _before_domaci_reminder():
    await bot.wait_until_ready()


# ==================== EVENTS ====================
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "✅":
        return
    d = get_domaci_by_message_id(payload.message_id)
    if not d or d["done"]:
        return
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return
    try:
        member = payload.member or await guild.fetch_member(payload.user_id)
    except Exception:
        return
    if member is None or member.bot:
        return
    if not is_support(member):
        return
    mark_domaci_done(payload.message_id)


# ==================== RESYNC + ERROR ====================
@tree.command(name="resync", description="force guild sync instant", guild=GUILD_OBJ)
@need_manage_roles()
async def resync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if GUILD_OBJ is None:
            return await interaction.followup.send("GUILD_ID nije setovan.", ephemeral=True)
        tree.copy_global_to(guild=GUILD_OBJ)
        cmds = await tree.sync(guild=GUILD_OBJ)
        names = ", ".join(sorted(c.name for c in cmds))
        await interaction.followup.send(
            f"Guild sync OK. {len(cmds)} komandi → {names}", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"Resync FAIL: {e}", ephemeral=True)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"greška: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"greška: {error}", ephemeral=True)
    except Exception:
        pass


# ==================== HTTP HEALTH (za Render Web Service) ====================
async def handle_health(request):
    return web.Response(text="ok")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BRIDGE_PORT)
    await site.start()
    print(f"[HTTP] server sluša na portu {BRIDGE_PORT}")


# ==================== ON READY ====================
@bot.event
async def on_ready():
    try:
        init_db()
        if GUILD_OBJ:
            cmds = await tree.sync(guild=GUILD_OBJ)
            print(f"synced {len(cmds)} slash komandi na server {GUILD_ID}")
        else:
            cmds = await tree.sync()
            print(f"synced {len(cmds)} globalnih slash komandi")
        print(f"✅ logged in as {bot.user}")
        if not domaci_reminder_loop.is_running():
            domaci_reminder_loop.start()
            print("✅ Domaći reminder task pokrenut")
        asyncio.create_task(start_http_server())
    except Exception as e:
        print("sync fail:", e)


# ---------- RUN ----------
bot.run(TOKEN)
