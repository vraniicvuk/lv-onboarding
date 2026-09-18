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
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
USE_AI = os.getenv("USE_AI", "true").lower() in ("1", "true", "yes", "on")

DB_PATH = os.getenv("DB_PATH", "/data/onboarding.db")
BRIDGE_PORT = int(os.getenv("PORT", os.getenv("BRIDGE_PORT", "8080")))

client = (
    OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    if (USE_AI and OPENAI_API_KEY)
    else None
)
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN nije setovan u .env")

# ---------- TUNABLES ----------
SLEEP_BETWEEN_CALLS = 0.35
REMINDER_STAGE_HOURS = 6  # svaka faza podsetnika na 6 sati

# ---------- BOT ----------
INTENTS = discord.Intents.default()
INTENTS.members = True  # potrebno za on_member_join (uključi Server Members Intent i u Dev Portalu)
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

SHIFT_GRAVEYARD_ROLE_ID = _env_int("SHIFT_GRAVEYARD_ROLE_ID") or 1453781684532019272
SHIFT_AFTERNOON_ROLE_ID = _env_int("SHIFT_AFTERNOON_ROLE_ID") or 1453781654765178934
SHIFT_MAIN_ROLE_ID = _env_int("SHIFT_MAIN_ROLE_ID") or 1453781572460482753

EXPERIENCED_ROLE_ID = _env_int("EXPERIENCED_ROLE_ID") or 1460974714720620604
INEXPERIENCED_ROLE_ID = _env_int("INEXPERIENCED_ROLE_ID") or 1460974855133462608

# Kanal za dnevni shadow prijavu (10:00) i start (20:00)
SHADOW_CHANNEL_ID = _env_int("SHADOW_CHANNEL_ID") or 1460378655829266695
# Rola koja se taguje u dnevnoj shadow prijavi u 10:00 (ujedno i rola shadow učesnika za bump)
SHADOW_SIGNUP_ROLE_ID = _env_int("SHADOW_SIGNUP_ROLE_ID") or 1453764199011319880
# Kanal u kom rade /ci i /co (shadow time tracking)
SHADOW_TIME_CHANNEL_ID = _env_int("SHADOW_TIME_CHANNEL_ID") or 1547914237530210414
# Cilj sati za shadow
SHADOW_GOAL_HOURS = 20
# Kategorija -> rola koja se taguje u regular check poruci
REGULAR_CHECK_CATEGORY_ROLE = {
    1528745104007757924: 1460974855133462608,  # inexperienced
    1528744628164100187: 1460974714720620604,  # experienced
}
# Role koje se taguju kada se ✅ odgovori na regular check pitanje
CHECK_ANSWER_TAG_ROLE_IDS = [
    1532337994726379620,
    1453746980525314099,
    1453746690342391808,
]
# Eskalacija podsetnika (redom): svakih 6h se taguje sledeća rola u listi
REMINDER_ROLE_IDS = _env_int_list("REMINDER_ROLE_IDS")
SUPPORT_ROLE_IDS = _env_int_list("SUPPORT_ROLE_IDS")

# Sve role koje vide tickete, bivaju pingovane pri otvaranju i smeju da kliknu ✅
REVIEW_ROLE_IDS = list(dict.fromkeys(SUPPORT_ROLE_IDS + REMINDER_ROLE_IDS))

# Role koje UVEK imaju pristup ticket kanalima (po zahtevu)
TICKET_ACCESS_ROLE_IDS = [
    1453746980525314099,
    1453746690342391808,
    1532337994726379620,
    1513881905005723758,
]

# Sve role koje vide tickete + bivaju pingovane + smeju ✅
TICKET_TEAM_ROLE_IDS = list(dict.fromkeys(REVIEW_ROLE_IDS + TICKET_ACCESS_ROLE_IDS))

# Redosled pingovanja za pregled domaćeg: odmah 1. rola, pa 6h opet 1., pa 6h 2., pa 6h 3.
# [r0, r0, r1, r2]
REMINDER_SEQUENCE = (
    [REMINDER_ROLE_IDS[0]] + REMINDER_ROLE_IDS if REMINDER_ROLE_IDS else []
)

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
            done INTEGER DEFAULT 0,
            kind TEXT DEFAULT 'mass'
        )
        """
    )
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(domaci)").fetchall()]
    if "kind" not in cols:
        conn.execute("ALTER TABLE domaci ADD COLUMN kind TEXT DEFAULT 'mass'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS check_answers (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            done INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_active (
            user_id INTEGER PRIMARY KEY,
            start_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_hours (
            user_id INTEGER PRIMARY KEY,
            seconds INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            channel_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            level TEXT
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


def add_domaci(message_id, channel_id, user_id, created_at, kind="mass"):
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO domaci (message_id, channel_id, user_id, created_at, kind) VALUES (?,?,?,?,?)",
        (message_id, channel_id, user_id, created_at, kind),
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


def add_check_answer(message_id, channel_id):
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO check_answers (message_id, channel_id) VALUES (?,?)",
        (message_id, channel_id),
    )
    conn.commit()
    conn.close()


def get_check_answer(message_id):
    conn = _db()
    row = conn.execute(
        "SELECT * FROM check_answers WHERE message_id = ?", (message_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_check_answer_done(message_id):
    conn = _db()
    conn.execute(
        "UPDATE check_answers SET done = 1 WHERE message_id = ?", (message_id,)
    )
    conn.commit()
    conn.close()


# ---- shadow time tracking ----
def start_session(user_id, start_at):
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO shadow_active (user_id, start_at) VALUES (?,?)",
        (user_id, start_at),
    )
    conn.commit()
    conn.close()


def get_active_session(user_id):
    conn = _db()
    row = conn.execute(
        "SELECT start_at FROM shadow_active WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["start_at"] if row else None


def end_session(user_id):
    conn = _db()
    conn.execute("DELETE FROM shadow_active WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def add_hours(user_id, seconds):
    conn = _db()
    conn.execute(
        "INSERT INTO shadow_hours (user_id, seconds) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET seconds = seconds + ?",
        (user_id, seconds, seconds),
    )
    conn.commit()
    conn.close()


def get_hours(user_id):
    conn = _db()
    row = conn.execute(
        "SELECT seconds FROM shadow_hours WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["seconds"] if row else 0


# ---- tickets ----
def add_ticket(channel_id, user_id, level=None):
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO tickets (channel_id, user_id, level) VALUES (?,?,?)",
        (channel_id, user_id, level),
    )
    conn.commit()
    conn.close()


def set_ticket_level(channel_id, level):
    conn = _db()
    conn.execute(
        "UPDATE tickets SET level = ? WHERE channel_id = ?", (level, channel_id)
    )
    conn.commit()
    conn.close()


def get_ticket(channel_id):
    conn = _db()
    row = conn.execute(
        "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_tickets_for_user(user_id):
    conn = _db()
    rows = conn.execute(
        "SELECT channel_id FROM tickets WHERE user_id = ?", (user_id,)
    ).fetchall()
    conn.close()
    return [r["channel_id"] for r in rows]


def get_all_ticket_users():
    conn = _db()
    rows = conn.execute("SELECT DISTINCT user_id FROM tickets").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


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
    return any(r.id in TICKET_TEAM_ROLE_IDS for r in member.roles)


def format_hours(seconds):
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


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
MASS_SYSTEM_PROMPT = os.getenv(
    "MASS_PROMPT",
    (
        "Ti si kontrolor 'domaći' zadatka za novajlije u OnlyFans management timu.\n"
        "Novajlija šalje MASS MESSAGE (!mm) — poruku koja se šalje svim fanovima da pokrene konverzaciju.\n"
        "Tvoj zadatak je da proveriš da li je ispravan za predaju i daš kratku, konkretnu povratnu informaciju.\n\n"
        "FORMAT (mora biti ispoštovan):\n"
        "!mm — početna poruka/pitanje (hook)\n"
        "fu1 — flirty odgovor/reakcija na potencijalni fanov odgovor (izjava, NE pitanje)\n"
        "fu1.5 — (opciono) zadirkivanje/prelaz + pitanje na koje odgovor NIJE da/ne, nadovezuje se na fu1\n"
        "fu2 — eskalacija, konkretnije i prljavije\n"
        "fu2.5 — pitanje na koje odgovor NIJE da/ne, nadovezuje se na fu2\n"
        "fu3/fu3.5 — (opciono) dalja eskalacija\n\n"
        "PRAVILA:\n"
        "- sve lowercase, bez interpunkcije i bez emojija\n"
        "- !mm je gotovo uvek PITANJE; mora da zvuči kao trenutak koji se može zamisliti\n"
        "- svaki !mm mora biti kreativan i intrigantan, NE generički (npr. 'hey' je loš — fan dobija 100+ takvih dnevno)\n"
        "- !mm ne sme biti predugačak ni preagresivan na startu — mora ostaviti prostora da se konverzacija zagreje\n"
        "- izbegavaj da/ne pitanja ('do u want me?', 'are u ready?', 'can u handle me?' — ne prolaze)\n"
        "- fu1/fu2 su izjave (reakcije), a fu1.5/fu2.5 su otvorena pitanja\n"
        "- followupovi moraju biti prirodni, kao tok razgovora koji se zahuktava\n"
        "- nema prepisivanja ni generičkih ChatGPT poruka — sve mora biti originalno u tonu tima\n\n"
        "DOBAR PRIMER (referenca za kvalitet):\n"
        "!mm: how long u think u'd last with my tongue working ur tip like candy?\n"
        "fu1: mm yeah? u know i go slow first.. just light circles till u cant take it..\n"
        "fu1.5: do u grab my hair when i tease like that or just beg for more?\n"
        "fu2: goood.. cuz then i'd take u deep in one go, eyes watering, moaning while u hit the back\n"
        "fu2.5: just fucking admit how bad u wanna feel me GAG on u right now??\n\n"
        "Format odgovora (na srpskom):\n"
        "- Ako je sve ispravno, odgovori samo: ✅ OK\n"
        "- Ako nešto fali, navedi kratko i konkretno (nabrojano) šta tačno popraviti (format, dužina, ton, da/ne pitanja, generičnost…)."
    ),
)

PPV_SYSTEM_PROMPT = os.getenv(
    "PPV_PROMPT",
    (
        "Ti si kontrolor 'domaći' zadatka za novajlije u OnlyFans management timu.\n"
        "Novajlija šalje PPV poruku (pay-per-view).\n"
        "PPV NIJE prodajna poruka nego SCENA — mora da zvuči kao da se dešava u trenutku, iz ugla modela.\n"
        "Tvoj zadatak je da proveriš da li je ispravan za predaju i daš kratku, konkretnu povratnu informaciju.\n\n"
        "PRAVILA:\n"
        "- isključivo lowercase, BEZ emojija, bez generičkih fraza\n"
        "- mora imati flow — da vodi čitaoca kroz scenu\n"
        "- ton: vizuelan, senzualan, flirty, dirty ali NE preterano vulgaran\n"
        "- 3 do 4 rečenice MAKSIMUM, svaka rečenica mora imati smisao i ritam\n"
        "- fokus na senzacije, dodire, opis pokreta, telo, ton glasa\n"
        "- NIKAD 'hey baby' ili 'this video is so hot'\n"
        "- mora se završiti pitanjem na koje odgovor NIJE da/ne\n"
        "- ako ne izaziva sliku u glavi dok se čita, nije dobro napisan\n\n"
        "DOBAR PRIMER (referenca za kvalitet):\n"
        "beep beep~ ass delivery incoming\n"
        "i'm dropping this fat, bouncy ass right on your face... smothering you with both holes while your mouth's wide open, trying to breathe through your ears...\n"
        "tell me, who would get more pleasure from this?\n\n"
        "Format odgovora (na srpskom):\n"
        "- Ako je sve ispravno, odgovori samo: ✅ OK\n"
        "- Ako nešto fali, navedi kratko i konkretno (nabrojano) šta tačno popraviti (dužina, ton, struktura, završno pitanje, generičnost…)."
    ),
)


async def _ai_check(system_prompt: str, text: str) -> str:
    if not client:
        return "⚠️ AI nije dostupan (nema OPENAI_API_KEY ili USE_AI=false)."
    def _call():
        rsp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
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


async def submit_domaci(interaction, text, kind, title):
    created = _local_now()
    # 1) pošalji domaći tekst (prva poruka = referenca za ✅ i podsetnik)
    chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)]
    first = None
    for idx, ch in enumerate(chunks):
        content = f"📝 **{title}** — {interaction.user.mention}\n\n{ch}" if idx == 0 else ch
        msg = await interaction.followup.send(content, wait=True)
        if idx == 0:
            first = msg

    # 2) registruj u DB (pre AI, da podsetnik radi i ako AI padne)
    if first:
        add_domaci(first.id, interaction.channel.id, interaction.user.id, created.isoformat(), kind)
        try:
            await first.add_reaction("✅")
        except Exception:
            pass
        # odmah taguj prvu rolu, pa dalje ide 6h eskalacija kroz loop
        if REMINDER_SEQUENCE:
            kind_label = "Mass Message" if kind == "mass" else "PPV"
            try:
                await interaction.channel.send(
                    f"<@&{REMINDER_SEQUENCE[0]}> 🔔 Novi {kind_label} za pregled od "
                    f"{interaction.user.mention}.\n{first.jump_url}"
                )
                set_domaci_stage(first.id, 1)
            except Exception as e:
                print("[REMINDER] immediate ping fail:", e)

    # 3) AI pregled
    prompt = MASS_SYSTEM_PROMPT if kind == "mass" else PPV_SYSTEM_PROMPT
    result = await _ai_check(prompt, text)
    await interaction.followup.send(
        f"🤖 **AI pregled ({title}):**\n\n{result}\n\n"
        f"Support: klikni ✅ na gornju poruku kad pregledaš (zaustavlja podsetnik)."
    )


class MassModal(Modal, title="Mass Message"):
    def __init__(self):
        super().__init__(timeout=None)
        self.domaci = TextInput(
            label="Zalepi mass message",
            style=TextStyle.paragraph,
            placeholder="Mass message…",
            required=True,
            max_length=4000,
        )
        self.add_item(self.domaci)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.domaci.value.strip()
        await interaction.response.defer(thinking=True)
        if not text:
            return await interaction.followup.send("❌ Prazan mass message.", ephemeral=True)
        await submit_domaci(interaction, text, "mass", "Mass Message")


class PpvModal(Modal, title="PPV"):
    def __init__(self):
        super().__init__(timeout=None)
        self.domaci = TextInput(
            label="Zalepi PPV poruke",
            style=TextStyle.paragraph,
            placeholder="PPV poruke…",
            required=True,
            max_length=4000,
        )
        self.add_item(self.domaci)

    async def on_submit(self, interaction: discord.Interaction):
        text = self.domaci.value.strip()
        await interaction.response.defer(thinking=True)
        if not text:
            return await interaction.followup.send("❌ Prazan PPV.", ephemeral=True)
        await submit_domaci(interaction, text, "ppv", "PPV")


@tree.command(name="domacimm", description="Pošalji mass message na pregled (u svom ticketu)", guild=GUILD_OBJ)
async def domacimm(interaction: discord.Interaction):
    if not _is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(
            "❌ Domaći šalješ unutar svog ticket kanala.", ephemeral=True
        )
    await interaction.response.send_modal(MassModal())


@tree.command(name="domacippv", description="Pošalji PPV poruke na pregled (u svom ticketu)", guild=GUILD_OBJ)
async def domacippv(interaction: discord.Interaction):
    if not _is_ticket_channel(interaction.channel):
        return await interaction.response.send_message(
            "❌ Domaći šalješ unutar svog ticket kanala.", ephemeral=True
        )
    await interaction.response.send_modal(PpvModal())


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

EXPERIENCED_QUESTIONS = (
    "**Pitanja za tebe:**\n"
    "1. Ime agencije u kojoj si radio? (poželjno sve da se nabroje)\n"
    "2. Na koliko si naloga radio?\n"
    "3. Koju smenu?\n"
    "4. Koji ti je najjači mesec?\n"
    "5. Zarada u toj agenciji?\n"
    "6. Jesi li radio na procenat ili fix, itd.\n"
    "7. Šta je razlog odlaska/otkaza?\n"
    "8. Koja su ti očekivanja sada?\n"
    "9. Koju bi smenu radio i da li si fleksibilan da radiš neku drugu dok ne kreneš u tu koju si na početku hteo?"
)


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

    async def on_error(self, interaction, error, item):
        print("[TICKET VIEW] error:", repr(error))
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Greška: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Greška: {error}", ephemeral=True)
        except Exception:
            pass

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
        role_id = SHIFT_ROLE_MAP.get(self.shift)
        role = interaction.guild.get_role(role_id) if role_id else None
        if role:
            try:
                await safe_add_roles(interaction.user, [role], reason=f"ticket smena {self.shift}")
            except Exception as e:
                print("[TICKET] shift role add fail:", e)

    async def on_level(self, interaction: discord.Interaction):
        self.level = self._level_select.values[0]
        self._level_select.disabled = True
        await interaction.response.edit_message(
            content=(
                f"✅ Smena: **{SHIFT_LABELS.get(self.shift, self.shift)}** "
                f"({SHIFT_SCHEDULE.get(self.shift, '')})\n"
                f"✅ Nivo: **{self.level.upper()}**\n\n"
                f"Ticket se premešta…"
            ),
            view=self,
        )
        exp_id = EXPERIENCED_ROLE_ID if self.level == "experienced" else INEXPERIENCED_ROLE_ID
        exp_role = interaction.guild.get_role(exp_id) if exp_id else None
        if exp_role:
            try:
                await safe_add_roles(interaction.user, [exp_role], reason=f"ticket nivo {self.level}")
            except Exception as e:
                print("[TICKET] level role add fail:", e)
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

        set_ticket_level(interaction.channel.id, self.level)

        if self.level == "experienced":
            try:
                await interaction.channel.send(
                    f"{interaction.user.mention}\n\n{EXPERIENCED_QUESTIONS}"
                )
            except Exception as e:
                print("[TICKET] experienced questions fail:", e)


async def create_onboarding_ticket(guild, member):
    """Kreira ticket kanal + pokreće flow (smena -> nivo iskustva). Vraća kanal."""
    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    if not category:
        raise RuntimeError("TICKET_CATEGORY_ID nije validan.")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_messages=True
        ),
    }
    for rid in TICKET_TEAM_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_messages=True
            )

    ch = await guild.create_text_channel(
        name=f"ticket-{member.name}",
        category=category,
        overwrites=overwrites,
        reason=f"Ticket by {member}",
    )

    add_ticket(ch.id, member.id)

    mentions = [member.mention] + [f"<@&{rid}>" for rid in TICKET_TEAM_ROLE_IDS]
    await ch.send(" ".join(mentions))
    await ch.send(
        "🎟️ **Novi ticket**\n"
        f"Korisnik: {member.mention}\n\n"
        f"{member.mention} molim te da iz padajućeg menija prvo izabereš **smenu** u kojoj bi radio, "
        "a zatim i da li **imaš iskustva**, kako bismo nastavili dalje.\n\n"
        "Izaberi svoju smenu:",
        view=TicketFlowView(),
    )
    return ch


@tree.command(name="ticket", description="Otvori novi ticket", guild=GUILD_OBJ)
async def ticket(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        ch = await create_onboarding_ticket(interaction.guild, interaction.user)
    except Exception as e:
        return await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)
    await interaction.followup.send(
        f"✅ Ticket otvoren → {ch.mention}", ephemeral=True
    )


@bot.event
async def on_member_join(member: discord.Member):
    """Novi član -> automatski ticket sa izborom smene i nivoa iskustva."""
    if member.bot:
        return
    guild = member.guild
    if GUILD_ID and guild.id != int(GUILD_ID):
        return

    # ako već ima otvoren ticket kanal (rejoin), ne pravimo novi
    try:
        for cid in get_tickets_for_user(member.id):
            if guild.get_channel(cid):
                print(f"[JOIN] {member} već ima ticket {cid}", flush=True)
                return
    except Exception as e:
        print(f"[JOIN] provera postojećih ticketa fail: {e}", flush=True)
    suffix = f"-{member.name}".lower()
    if any(
        isinstance(c, discord.TextChannel) and c.name.startswith("ticket-") and c.name.endswith(suffix)
        for c in guild.channels
    ):
        print(f"[JOIN] {member} već ima ticket kanal", flush=True)
        return

    try:
        ch = await create_onboarding_ticket(guild, member)
        print(f"[JOIN] {member} -> ticket #{ch.name}", flush=True)
    except Exception as e:
        print(f"[JOIN] ticket fail za {member}: {e}", flush=True)


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


# ==================== SHADOW TIME TRACKING ====================
@tree.command(name="ci", description="Clock in — počni shadow sesiju", guild=GUILD_OBJ)
async def ci(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if interaction.channel.id != SHADOW_TIME_CHANNEL_ID:
        return await interaction.followup.send(
            "❌ /ci radi samo u shadow kanalu.", ephemeral=True
        )
    if get_active_session(interaction.user.id):
        return await interaction.followup.send("❌ Već si clocked in.", ephemeral=True)
    start_session(interaction.user.id, _local_now().isoformat())
    await interaction.followup.send(
        f"✅ Clocked in — {interaction.user.mention} u {_local_now().strftime('%H:%M')}.",
        ephemeral=False,
    )


@tree.command(name="co", description="Clock out — završi shadow sesiju", guild=GUILD_OBJ)
async def co(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if interaction.channel.id != SHADOW_TIME_CHANNEL_ID:
        return await interaction.followup.send(
            "❌ /co radi samo u shadow kanalu.", ephemeral=True
        )
    start_at = get_active_session(interaction.user.id)
    if not start_at:
        return await interaction.followup.send("❌ Nisi clocked in.", ephemeral=True)
    end = _local_now()
    try:
        start_dt = datetime.fromisoformat(start_at)
    except Exception:
        start_dt = end
    seconds = int((end - start_dt).total_seconds())
    add_hours(interaction.user.id, seconds)
    end_session(interaction.user.id)
    total = get_hours(interaction.user.id)
    await interaction.followup.send(
        f"✅ Clocked out — {interaction.user.mention}.\n"
        f"Sesija: **{format_hours(seconds)}** • Ukupno: **{format_hours(total)}**",
        ephemeral=False,
    )


@tree.command(name="hours", description="Prikaži shadow sate", guild=GUILD_OBJ)
async def hours(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    total = get_hours(target.id)
    active = get_active_session(target.id)
    text = f"⏱️ {target.mention}: **{format_hours(total)}**"
    if active:
        try:
            start_dt = datetime.fromisoformat(active)
            elapsed = int((_local_now() - start_dt).total_seconds())
            text += f" (trenutna sesija u toku: {format_hours(elapsed)})"
        except Exception:
            pass
    await interaction.response.send_message(text, ephemeral=False)


@tree.command(name="bump", description="Ručno pokreni shadow bump za sve sa shadow rolom", guild=GUILD_OBJ)
async def bump(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    count = await run_all_bumps()
    await interaction.followup.send(
        f"✅ Bump poslat — {count} shadow korisnika.", ephemeral=True
    )


# ==================== 6H REMINDER LOOP ====================
@tasks.loop(minutes=10)
async def domaci_reminder_loop():
    if not REMINDER_SEQUENCE:
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
            d["stage"] < len(REMINDER_SEQUENCE)
            and hours_elapsed >= d["stage"] * REMINDER_STAGE_HOURS
        ):
            role_id = REMINDER_SEQUENCE[d["stage"]]
            link = (
                f"https://discord.com/channels/{GUILD_ID}/{d['channel_id']}/{d['message_id']}"
                if GUILD_ID
                else ""
            )
            kind_label = "Mass Message" if d.get("kind") == "mass" else "PPV"
            try:
                await channel.send(
                    f"<@&{role_id}> 🔔 Podsetnik za pregled {kind_label} od <@{d['user_id']}> "
                    f"(prošlo {d['stage'] * REMINDER_STAGE_HOURS}h).\n{link}"
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


# ==================== SCHEDULER (shadow + regular check) ====================
SHADOW_SIGNUP_TEXT = (
    "<@&{role_id}> 📅 **{date}** — Shadow popodnevne smene u 20:00.\n"
    "Reaguj sa ✅ na ovu poruku ako ćeš učestvovati."
)

REGULAR_CHECK_TEXT = (
    "Regular check <@&{role_id}> Kako ti ide trenutni tok obuke i da li ti je nešto nejasno?\n"
    "Tu smo kao tim ukoliko ti je potrebna pomoć, i otvoreni smo za pitanja kako bi što pre da prešao na sledeći korak procesa zapošljavanja."
)

CHECK_ANSWER_TEXT = (
    "Reaguj sa ✅ kada je odgovoreno na pitanje iznad."
)


async def shadow_signup(now):
    today = now.date()
    if get_state("last_shadow_signup") == today.isoformat():
        return
    channel = bot.get_channel(SHADOW_CHANNEL_ID) if SHADOW_CHANNEL_ID else None
    if not channel:
        print("[SHADOW] kanal nije nađen")
        return
    try:
        msg = await channel.send(
            SHADOW_SIGNUP_TEXT.format(date=today.strftime("%d.%m.%Y"), role_id=SHADOW_SIGNUP_ROLE_ID)
        )
        await msg.add_reaction("✅")
    except Exception as e:
        print("[SHADOW] signup send fail:", e)
        return
    set_state("shadow_msg_id", str(msg.id))
    set_state("last_shadow_signup", today.isoformat())


async def shadow_start(now):
    today = now.date()
    if get_state("last_shadow_start") == today.isoformat():
        return
    channel = bot.get_channel(SHADOW_CHANNEL_ID) if SHADOW_CHANNEL_ID else None
    if not channel:
        return
    msg_id = get_state("shadow_msg_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            participants = []
            for r in msg.reactions:
                if str(r.emoji) == "✅":
                    async for u in r.users():
                        if not u.bot:
                            participants.append(u.mention)
            if participants:
                text = "🔴 **Shadow je počeo!** " + ", ".join(participants)
            else:
                text = "🔴 **Shadow je počeo!** (niko se nije prijavio)"
            await msg.reply(text)
        except Exception as e:
            print("[SHADOW] start fail:", e)
    set_state("last_shadow_start", today.isoformat())


async def regular_check(now):
    today = now.date()
    last = get_state("last_regular_check")
    if last:
        try:
            last_date = datetime.fromisoformat(last).date()
            if (today - last_date).days < 3:
                return
        except Exception:
            pass
    sent = 0
    for cat_id, role_id in REGULAR_CHECK_CATEGORY_ROLE.items():
        cat = bot.get_channel(cat_id) if cat_id else None
        if not cat:
            continue
        for ch in cat.channels:
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(REGULAR_CHECK_TEXT.format(role_id=role_id))
                    await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                    ans = await ch.send(CHECK_ANSWER_TEXT)
                    await ans.add_reaction("✅")
                    add_check_answer(ans.id, ch.id)
                    sent += 1
                    await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                except Exception as e:
                    print("[CHECK] send fail:", e)
    set_state("last_regular_check", today.isoformat())
    print(f"[CHECK] regular check poslat u {sent} kanala")


async def bump_user(user_id):
    total = get_hours(user_id)
    text = (
        f"📊 **Shadow progres** — <@{user_id}> trenutno ima **{format_hours(total)}** "
        f"od **{SHADOW_GOAL_HOURS}h** cilja. Nastavi dalje!"
    )
    for ch_id in get_tickets_for_user(user_id):
        ch = bot.get_channel(ch_id) if ch_id else None
        if ch:
            try:
                await ch.send(text)
            except Exception as e:
                print("[BUMP] send fail:", e)
            await asyncio.sleep(SLEEP_BETWEEN_CALLS)


async def run_all_bumps():
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not guild:
        return 0
    role_id = SHADOW_SIGNUP_ROLE_ID
    bumped = 0
    seen = set()
    for user_id in get_all_ticket_users():
        if user_id in seen:
            continue
        seen.add(user_id)
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except Exception:
            continue
        if member is None:
            continue
        if not any(r.id == role_id for r in member.roles):
            continue
        await bump_user(user_id)
        bumped += 1
    print(f"[BUMP] bumpano {bumped} korisnika")
    return bumped


async def shadow_bump(now):
    today = now.date()
    last = get_state("last_shadow_bump")
    if last:
        try:
            if (today - datetime.fromisoformat(last).date()).days < 3:
                return
        except Exception:
            pass
    await run_all_bumps()
    set_state("last_shadow_bump", today.isoformat())


@tasks.loop(minutes=1)
async def scheduler_loop():
    now = _local_now()
    if now.minute > 1:
        return
    if now.hour == 10:
        await shadow_signup(now)
    elif now.hour == 20:
        await shadow_start(now)
    elif now.hour == 12:
        await regular_check(now)
        await shadow_bump(now)


@scheduler_loop.before_loop
async def _before_scheduler():
    await bot.wait_until_ready()


# ==================== EVENTS ====================
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "✅":
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

    # 1) domaći review ✅ (samo support)
    d = get_domaci_by_message_id(payload.message_id)
    if d and not d["done"]:
        if is_support(member):
            mark_domaci_done(payload.message_id)
        return

    # 2) regular check odgovoren → taguj role
    ca = get_check_answer(payload.message_id)
    if ca and not ca["done"]:
        channel = bot.get_channel(ca["channel_id"]) if ca["channel_id"] else None
        if channel:
            mentions = " ".join(f"<@&{rid}>" for rid in CHECK_ANSWER_TAG_ROLE_IDS)
            try:
                await channel.send(f"{mentions} ✅ Regular check pitanje je odgovoreno.")
            except Exception as e:
                print("[CHECK] answer tag fail:", e)
        mark_check_answer_done(payload.message_id)
        return


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
        if not scheduler_loop.is_running():
            scheduler_loop.start()
            print("✅ Scheduler task pokrenut (shadow + regular check)")
        asyncio.create_task(start_http_server())
    except Exception as e:
        print("sync fail:", e)


# ---------- RUN ----------
bot.run(TOKEN)
