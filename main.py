# -*- coding: utf-8 -*-
"""
Rav Avatars Bot  (النسخة النهائية الشاملة)
=========================================
- ينشر كل 3 ساعات: 15 زوج (أفتار + بنر) في روم الرجال و 15 زوج في روم البنات
- كل زوج برسالة واحدة (صورة بروفايل مدموجة) + زر تنزيل
- زر التنزيل يستخدمه أي عضو، ويرسل الأفتار والبنر الأصليين له فقط (رسالة مخفية عن الباقي)
- لوج في سيرفر الأفتارات: دخول الأعضاء + خروجهم + كل ضغطة تنزيل (الاسم + اليوزر + المنشن + الصورة)
- يتذكر وش اللي اننشر من تاريخ الروم نفسه (ما يتأثر بإعادة تشغيل Render)
- لو البوت كان مطفي وقت النشر، أول ما يشتغل يعوض الدفعة الفايتة تلقائياً

هيكل الصور (داخل GitHub بجانب main.py):

pairs/
  boys/
    dark/
      pair01/
        avatar.png     (أو jpg / webp / gif)
        banner.png
      pair02/ ...
    light/ ...
  girls/
    dark/ ...
    light/ ...

كل مجلد فيه ملفين اسمهم avatar و banner يعتبر "زوج".
تقدر تسمي المجلدات اللي فوق (dark / light / أي اسم) مثل ما تبي، البوت يدور داخلها كلها.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import io
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageOps

# ======================================================================
# الإعدادات  (عدل الأرقام هنا فقط)
# ======================================================================
TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")

GUILD_ID = 1556894802744709122            # ID سيرفر الأفتارات
BOYS_CHANNEL_ID = 1556913295733162024     # ID روم أفتارات الرجال
GIRLS_CHANNEL_ID = 1556913327693889586    # ID روم أفتارات البنات
LOG_CHANNEL_ID = 1556913581285441556      # ID روم اللوج (داخل سيرفر الأفتارات)
OWNER_ROLE_ID = 1556895194832445470       # ID رتبة الأونر (تقدر تستخدم أوامر الإدارة) - الأدمن يقدر دايماً

BATCH_SIZE = 15                 # عدد الأزواج لكل روم في كل دفعة
SEND_DELAY = 1.5                # ثواني بين كل رسالة والثانية (حماية من الريت لميت)
POST_EVERY_HOURS = 3            # كل كم ساعة (بتوقيت السعودية: 12AM, 3AM, 6AM ...)
MIN_GAP_MINUTES = 30            # أقل فاصل بين دفعتين (يمنع النشر المزدوج)
CATCH_UP_ON_START = True        # يعوض الدفعة الفايتة إذا البوت كان مطفي
HISTORY_SCAN = 500              # كم رسالة يقرأ من الروم عشان يعرف وش اللي اننشر
EMBED_COLOR = 0xE8D9C0          # لون البيج للإيمبد
DOWNLOAD_COOLDOWN = 5           # ثواني بين ضغطات التنزيل لنفس الشخص

BASE_DIR = Path(__file__).resolve().parent
PAIRS_DIR = BASE_DIR / "pairs"
STATE_FILE = BASE_DIR / "state.json"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

CATEGORY_LABEL = {"boys": "رجال 👨", "girls": "بنات 👩"}
FOOTER_RE = re.compile(r"#([a-f0-9]{6})\s*$")

# ======================================================================
# اللوق
# ======================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("rav-avatars")

KSA = datetime.timezone(datetime.timedelta(hours=3))
SCHEDULE_TIMES = [
    datetime.time(hour=h, minute=0, tzinfo=KSA)
    for h in range(0, 24, POST_EVERY_HOURS)
]


# ======================================================================
# الأزواج (أفتار + بنر)
# ======================================================================
@dataclass(frozen=True)
class Pair:
    pid: str            # معرف قصير ثابت (hash لمسار المجلد)
    category: str       # boys / girls
    name: str           # اسم المجلد
    avatar: Path
    banner: Path


def scan_pairs(category: str) -> Dict[str, Pair]:
    """يدور داخل pairs/<category> على كل مجلد فيه avatar و banner."""
    root = PAIRS_DIR / category
    found: Dict[str, Pair] = {}
    if not root.exists():
        return found

    for dirpath, _dirs, files in os.walk(root):
        avatar: Optional[Path] = None
        banner: Optional[Path] = None
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.lower() not in IMG_EXT:
                continue
            if stem.lower() == "avatar":
                avatar = Path(dirpath) / f
            elif stem.lower() == "banner":
                banner = Path(dirpath) / f
        if avatar and banner:
            rel = Path(dirpath).relative_to(PAIRS_DIR).as_posix()
            pid = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
            found[pid] = Pair(
                pid=pid,
                category=category,
                name=Path(dirpath).name,
                avatar=avatar,
                banner=banner,
            )
    return found


PAIR_INDEX: Dict[str, Pair] = {}


def refresh_index() -> None:
    PAIR_INDEX.clear()
    for cat in ("boys", "girls"):
        PAIR_INDEX.update(scan_pairs(cat))


def get_pair(pid: str) -> Optional[Pair]:
    if pid not in PAIR_INDEX:
        refresh_index()
    return PAIR_INDEX.get(pid)


# ======================================================================
# حفظ احتياطي لما اننشر (بالإضافة لقراءة تاريخ الروم)
# ======================================================================
def load_state() -> Dict[str, List[str]]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "boys": list(data.get("boys", [])),
            "girls": list(data.get("girls", [])),
        }
    except Exception:
        return {"boys": [], "girls": []}


def save_state(state: Dict[str, List[str]]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر حفظ state.json: %s", exc)


def pick_pairs(category: str, recent_prefixes: Set[str]) -> List[Pair]:
    """يختار دفعة عشوائية بدون تكرار الأزواج اللي اننشرت قريب."""
    pairs = scan_pairs(category)
    PAIR_INDEX.update(pairs)
    if not pairs:
        return []

    state = load_state()
    posted_ids = set(state.get(category, []))
    pool = [
        p for pid, p in pairs.items()
        if pid not in posted_ids and pid[:6] not in recent_prefixes
    ]

    if len(pool) >= BATCH_SIZE:
        chosen = random.sample(pool, BATCH_SIZE)
        posted_ids.update(p.pid for p in chosen)
    else:
        # الجديد كله + نكمل الباقي عشوائي من القديم، ونبدأ دورة جديدة
        pool_ids = {p.pid for p in pool}
        others = [p for pid, p in pairs.items() if pid not in pool_ids]
        extra = random.sample(others, min(BATCH_SIZE - len(pool), len(others)))
        chosen = pool + extra
        posted_ids = {p.pid for p in extra}

    state[category] = list(posted_ids)
    save_state(state)
    random.shuffle(chosen)
    return chosen


# ======================================================================
# دمج الأفتار والبنر في صورة بروفايل وحدة
# ======================================================================
CARD_W = 900
BANNER_H = 330
PANEL_H = 140
AVATAR_D = 230
RING = 14
PANEL_COLOR = (17, 17, 20)
CORNER_R = 28


def _circle_mask(size: int) -> Image.Image:
    scale = 4
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def _rounded_mask(w: int, h: int, r: int) -> Image.Image:
    scale = 4
    big = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, w * scale - 1, h * scale - 1), radius=r * scale, fill=255
    )
    return big.resize((w, h), Image.LANCZOS)


def build_preview(avatar_path: Path, banner_path: Path) -> bytes:
    with Image.open(banner_path) as b:
        banner = ImageOps.fit(b.convert("RGB"), (CARD_W, BANNER_H), Image.LANCZOS)
    with Image.open(avatar_path) as a:
        avatar = ImageOps.fit(a.convert("RGBA"), (AVATAR_D, AVATAR_D), Image.LANCZOS)

    total_h = BANNER_H + PANEL_H
    card = Image.new("RGBA", (CARD_W, total_h), PANEL_COLOR + (255,))
    card.paste(banner, (0, 0))

    ax = 48
    ay = BANNER_H - AVATAR_D // 2

    # الحلقة الغامقة حول الأفتار
    ring_d = AVATAR_D + RING * 2
    ring = Image.new("RGBA", (ring_d, ring_d), PANEL_COLOR + (255,))
    card.paste(ring, (ax - RING, ay - RING), _circle_mask(ring_d))

    # الأفتار بشكل دائرة
    card.paste(avatar, (ax, ay), _circle_mask(AVATAR_D))

    # زوايا مدورة للبطاقة كلها
    card.putalpha(_rounded_mask(CARD_W, total_h, CORNER_R))

    out = io.BytesIO()
    card.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ======================================================================
# البوت
# ======================================================================
intents = discord.Intents.default()
intents.members = True  # لازم تفعله من Developer Portal (Server Members Intent)


class AvatarBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.post_lock = asyncio.Lock()
        self.last_click: Dict[int, float] = {}
        self._startup_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        # زر التنزيل يشتغل حتى بعد إعادة تشغيل البوت
        self.add_dynamic_items(DownloadButton)

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        await start_web()
        refresh_index()
        auto_post.start()
        self._startup_task = asyncio.create_task(startup_routine())

    async def on_ready(self) -> None:
        log.info("البوت شغال باسم %s | الأزواج: %d", self.user, len(PAIR_INDEX))


bot = AvatarBot()


# ----------------------------------------------------------------------
# سيرفر ويب صغير (لو الاستضافة Web Service على Render + UptimeRobot)
# ----------------------------------------------------------------------
async def start_web() -> None:
    port = os.getenv("PORT")
    if not port:
        return

    async def handle(_request: web.Request) -> web.Response:
        return web.Response(text="Rav Avatars Bot is running")

    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    log.info("Web server on port %s", port)


# ----------------------------------------------------------------------
# أدوات مساعدة
# ----------------------------------------------------------------------
async def resolve_channel(channel_id: int):
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:  # noqa: BLE001
            log.error("ما قدرت أوصل للروم %s: %s", channel_id, exc)
            return None
    return channel


async def scan_history(
    channel,
) -> Tuple[Set[str], Optional[datetime.datetime], bool]:
    """
    يقرأ آخر رسائل البوت في الروم ويرجع:
    (الأزواج اللي اننشرت قريب, وقت آخر نشر, هل القراءة نجحت)
    """
    prefixes: Set[str] = set()
    last: Optional[datetime.datetime] = None
    try:
        async for msg in channel.history(limit=HISTORY_SCAN):
            if bot.user is None or msg.author.id != bot.user.id:
                continue
            if last is None:
                last = msg.created_at
            for emb in msg.embeds:
                if emb.footer and emb.footer.text:
                    m = FOOTER_RE.search(emb.footer.text)
                    if m:
                        prefixes.add(m.group(1))
        return prefixes, last, True
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر قراءة تاريخ الروم: %s", exc)
        return prefixes, last, False


async def send_log(embed: discord.Embed) -> None:
    channel = await resolve_channel(LOG_CHANNEL_ID)
    if channel is None:
        return
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("فشل إرسال اللوج: %s", exc)


# ----------------------------------------------------------------------
# لوج الدخول والخروج
# ----------------------------------------------------------------------
@bot.event
async def on_member_join(member: discord.Member) -> None:
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    embed = discord.Embed(
        title="📥 عضو جديد دخل السيرفر",
        color=0x57F287,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="العضو", value=member.mention, inline=True)
    embed.add_field(name="الاسم", value=member.display_name, inline=True)
    embed.add_field(name="اليوزر", value=f"@{member.name}", inline=True)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(
        name="تاريخ إنشاء الحساب",
        value=discord.utils.format_dt(member.created_at, "R"),
        inline=True,
    )
    embed.add_field(
        name="عدد الأعضاء الآن", value=str(member.guild.member_count), inline=True
    )
    await send_log(embed)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    embed = discord.Embed(
        title="📤 عضو طلع من السيرفر",
        color=0xED4245,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="العضو", value=member.mention, inline=True)
    embed.add_field(name="اليوزر", value=f"@{member.name}", inline=True)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(
        name="عدد الأعضاء الآن", value=str(member.guild.member_count), inline=True
    )
    await send_log(embed)


# ----------------------------------------------------------------------
# زر التنزيل (متاح لكل الأعضاء بدون أي شرط رتبة)
# ----------------------------------------------------------------------
def make_files(pair: Pair) -> List[discord.File]:
    return [
        discord.File(pair.avatar, filename=f"avatar{pair.avatar.suffix.lower()}"),
        discord.File(pair.banner, filename=f"banner{pair.banner.suffix.lower()}"),
    ]


class DownloadButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"dl:(?P<pid>[a-f0-9]{10})",
):
    def __init__(self, pid: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="تنزيل",
                emoji="⬇️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"dl:{pid}",
            )
        )
        self.pid = pid

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match,
    ) -> "DownloadButton":
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        user = interaction.user

        # حماية من السبام
        now = time.monotonic()
        last = bot.last_click.get(user.id, 0.0)
        if now - last < DOWNLOAD_COOLDOWN:
            await interaction.response.send_message(
                "⏳ انتظر ثواني وحاول مرة ثانية.", ephemeral=True
            )
            return
        bot.last_click[user.id] = now

        # thinking=True يضمن إن الرد يطلع مخفي (للضاغط فقط)
        await interaction.response.defer(ephemeral=True, thinking=True)

        pair = get_pair(self.pid)
        if pair is None:
            await interaction.followup.send(
                "❌ هذي الصورة ما عادت متوفرة.", ephemeral=True
            )
            return

        try:
            try:
                await interaction.followup.send(
                    content="✅ تفضل الأفتار والبنر 🤍",
                    files=make_files(pair),
                    ephemeral=True,
                )
            except discord.HTTPException:
                # لو الحجم كبير نرسل كل ملف لحاله
                avatar_file, banner_file = make_files(pair)
                await interaction.followup.send(
                    content="✅ الأفتار 🤍", file=avatar_file, ephemeral=True
                )
                await interaction.followup.send(
                    content="✅ البنر 🤍", file=banner_file, ephemeral=True
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("فشل إرسال التنزيل: %s", exc)
            await interaction.followup.send(
                "❌ صار خطأ أثناء الإرسال، حاول مرة ثانية.", ephemeral=True
            )
            return

        # ---------- لوج التنزيل ----------
        embed = discord.Embed(
            title="⬇️ عملية تنزيل",
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="العضو", value=user.mention, inline=True)
        embed.add_field(name="الاسم", value=user.display_name, inline=True)
        embed.add_field(name="اليوزر", value=f"@{user.name}", inline=True)
        embed.add_field(name="ID", value=f"`{user.id}`", inline=True)
        embed.add_field(
            name="القسم",
            value=CATEGORY_LABEL.get(pair.category, pair.category),
            inline=True,
        )
        if interaction.channel is not None:
            embed.add_field(
                name="الروم",
                value=getattr(interaction.channel, "mention", "—"),
                inline=True,
            )
        embed.add_field(
            name="الزوج", value=f"`{pair.name}`  •  #{pair.pid[:6]}", inline=False
        )

        msg = interaction.message
        if msg is not None and msg.embeds and msg.embeds[0].image:
            embed.set_image(url=msg.embeds[0].image.url)
        await send_log(embed)


def make_view(pid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DownloadButton(pid))
    return view


# ----------------------------------------------------------------------
# النشر
# ----------------------------------------------------------------------
async def post_category(
    category: str,
    channel_id: int,
    min_gap_minutes: Optional[float],
) -> int:
    """
    min_gap_minutes: لا ينشر إذا آخر نشر أحدث من هذا الفاصل.
                     None = إجبار (للأمر /post_now)
    """
    channel = await resolve_channel(channel_id)
    if channel is None:
        log.warning("ما تم تحديد/إيجاد روم قسم %s", category)
        return 0

    recent, last, ok = await scan_history(channel)

    if min_gap_minutes is not None and ok and last is not None:
        age = discord.utils.utcnow() - last
        if age < datetime.timedelta(minutes=min_gap_minutes):
            log.info("تخطي قسم %s: آخر نشر قبل %s", category, age)
            return 0

    chosen = pick_pairs(category, recent)
    if not chosen:
        log.warning("ما فيه أزواج صور في pairs/%s", category)
        return 0

    sent = 0
    for pair in chosen:
        try:
            data = await asyncio.to_thread(build_preview, pair.avatar, pair.banner)
            embed = discord.Embed(
                description="⬇️ اضغط **تنزيل** لاستلام الأفتار والبنر",
                color=EMBED_COLOR,
            )
            embed.set_image(url="attachment://preview.png")
            embed.set_footer(text=f"Rav Avatars  •  #{pair.pid[:6]}")
            await channel.send(
                embed=embed,
                file=discord.File(io.BytesIO(data), filename="preview.png"),
                view=make_view(pair.pid),
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.error("فشل نشر الزوج %s: %s", pair.name, exc)
        await asyncio.sleep(SEND_DELAY)

    log.info("تم نشر %d زوج في قسم %s", sent, category)
    return sent


async def run_batches(
    categories: List[str],
    min_gap_minutes: Optional[float] = MIN_GAP_MINUTES,
) -> Dict[str, int]:
    results: Dict[str, int] = {}
    async with bot.post_lock:
        for cat in categories:
            channel_id = BOYS_CHANNEL_ID if cat == "boys" else GIRLS_CHANNEL_ID
            results[cat] = await post_category(cat, channel_id, min_gap_minutes)
    return results


@tasks.loop(time=SCHEDULE_TIMES)
async def auto_post() -> None:
    try:
        await run_batches(["boys", "girls"])
    except Exception as exc:  # noqa: BLE001
        log.exception("خطأ في النشر التلقائي: %s", exc)


@auto_post.before_loop
async def _before_auto_post() -> None:
    await bot.wait_until_ready()


# ----------------------------------------------------------------------
# فحص الإعدادات + تعويض الدفعة الفايتة عند التشغيل
# ----------------------------------------------------------------------
async def check_setup() -> None:
    needed = [
        "view_channel",
        "send_messages",
        "embed_links",
        "attach_files",
        "read_message_history",
    ]
    for label, cid in (
        ("روم الرجال", BOYS_CHANNEL_ID),
        ("روم البنات", GIRLS_CHANNEL_ID),
        ("روم اللوج", LOG_CHANNEL_ID),
    ):
        if not cid:
            log.warning("⚠️ %s: ما حطيت الـ ID في الإعدادات", label)
            continue
        channel = await resolve_channel(cid)
        if channel is None:
            log.warning("⚠️ %s: ما قدرت أوصل له (تأكد من الـ ID ودخول البوت للسيرفر)", label)
            continue
        perms = channel.permissions_for(channel.guild.me)
        missing = [n for n in needed if not getattr(perms, n)]
        if missing:
            log.warning("⚠️ %s: ينقص البوت صلاحيات: %s", label, ", ".join(missing))
    if not any(scan_pairs(c) for c in ("boys", "girls")):
        log.warning("⚠️ ما لقيت أي أزواج صور داخل مجلد pairs/")


async def startup_routine() -> None:
    await bot.wait_until_ready()
    try:
        await check_setup()
        if CATCH_UP_ON_START:
            # ينشر فقط إذا آخر نشر أقدم من فترة النشر (أو ما فيه نشر أبداً)
            await run_batches(
                ["boys", "girls"],
                min_gap_minutes=POST_EVERY_HOURS * 60 - 5,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("خطأ أثناء التشغيل الأولي: %s", exc)


# ----------------------------------------------------------------------
# أوامر الإدارة
# ----------------------------------------------------------------------
def is_staff(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return bool(OWNER_ROLE_ID) and any(r.id == OWNER_ROLE_ID for r in user.roles)


@bot.tree.command(name="post_now", description="نشر دفعة أفتارات الآن (للإدارة)")
@app_commands.describe(section="القسم اللي تبي تنشره")
@app_commands.choices(
    section=[
        app_commands.Choice(name="رجال", value="boys"),
        app_commands.Choice(name="بنات", value="girls"),
        app_commands.Choice(name="الاثنين", value="both"),
    ]
)
async def post_now(
    interaction: discord.Interaction, section: app_commands.Choice[str]
) -> None:
    if not is_staff(interaction):
        await interaction.response.send_message(
            "❌ هذا الأمر للإدارة فقط.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    cats = ["boys", "girls"] if section.value == "both" else [section.value]
    results = await run_batches(cats, min_gap_minutes=None)
    summary = "\n".join(f"{CATEGORY_LABEL[c]}: {n} زوج" for c, n in results.items())
    await interaction.followup.send(f"✅ تم النشر\n{summary}", ephemeral=True)


@bot.tree.command(name="pairs_count", description="عدد الأزواج المتوفرة (للإدارة)")
async def pairs_count(interaction: discord.Interaction) -> None:
    if not is_staff(interaction):
        await interaction.response.send_message(
            "❌ هذا الأمر للإدارة فقط.", ephemeral=True
        )
        return

    boys = len(scan_pairs("boys"))
    girls = len(scan_pairs("girls"))
    note = ""
    if boys < BATCH_SIZE or girls < BATCH_SIZE:
        note = f"\n⚠️ لازم {BATCH_SIZE} زوج على الأقل في كل قسم عشان الدفعة تكتمل."
    await interaction.response.send_message(
        f"📊 الأزواج المتوفرة:\nرجال: **{boys}**\nبنات: **{girls}**\n"
        f"كل دفعة: {BATCH_SIZE} زوج لكل قسم{note}",
        ephemeral=True,
    )


# ======================================================================
# التشغيل
# ======================================================================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("ضع توكن البوت في متغير البيئة DISCORD_TOKEN")
    bot.run(TOKEN)
