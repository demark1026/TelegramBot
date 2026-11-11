"""
Universal Video/Audio Downloader - Telegram bot.

Needs on the machine that runs it:
    pip install -U "python-telegram-bot>=21" "yt-dlp[default]" python-dotenv
    ffmpeg (includes ffprobe)  - required for MP3 and for merging video+audio
    deno                       - required by YouTube
BOT_TOKEN must be set as an environment variable (or in a .env file).

Optional environment values:
    CHANNEL=@coreviewz     channel users must join
    REQUIRE_SUB=1          set 0 to disable the join requirement
    MAX_UPLOAD_MB=50       raise to 2000 only if you run a local Bot API server
"""
import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.constants import ChatAction, ChatMemberStatus, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("downloader")

TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.getenv("CHANNEL", "@coreviewz")
REQUIRE_SUB = os.getenv("REQUIRE_SUB", "1") == "1"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
BRAND = "Universal Video/Audio Downloader"
DOWNLOAD_DIR = Path("downloads")
SEM = asyncio.Semaphore(3)  # max simultaneous downloads
URL_RE = re.compile(r"https?://\S+")
HTML = ParseMode.HTML

NSFW_WORDS = [
    "porn", "pornhub", "xxx", "xvideos", "xhamster", "redtube", "brazzers",
    "nsfw", "onlyfans", "nude", "nudes", "fetish", "erotic", "hentai", "sex",
]
NSFW_RE = re.compile(r"\b(" + "|".join(NSFW_WORDS) + r")\b", re.I)

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MOONS = "🌑🌒🌓🌔🌕🌖🌗🌘"
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".3gp", ".avi", ".ts"}
KEEP = ("format_id", "ext", "vcodec", "acodec", "height", "width", "fps",
        "tbr", "abr", "filesize", "filesize_approx")


# ───────────────────────── small helpers ─────────────────────────
def is_nsfw(*parts: str) -> bool:
    return bool(NSFW_RE.search(" ".join(p for p in parts if p)))


def esc(s) -> str:
    return html.escape(str(s or ""))


def short_err(e, n=160) -> str:
    """One clean line of an error message, safe to show in chat."""
    t = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    t = t.replace("ERROR:", "").strip().splitlines()[0] if t.strip() else "unknown error"
    return t[:n]


def fmt_dur(s):
    if not s:
        return "—"
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_speed(b):
    return f"{b / 1048576:.1f} MB/s" if b else "…"


def fmt_eta(s):
    return fmt_dur(s) if s else "…"


def fmt_mb(b):
    return f"{b / 1048576:.1f} MB"


def size_label(est):
    return f" · {est / 1048576:.0f} MB" if est else ""


def bar(pct, n=14):
    filled = int(max(0, min(100, pct)) / 100 * n)
    return "▰" * filled + "▱" * (n - filled)


def shuttle(i, n=14, w=4):
    cells = ["▱"] * n
    for k in range(w):
        cells[(i + k) % n] = "▰"
    return "".join(cells)


def safe_name(title: str, suffix: str) -> str:
    base = re.sub(r'[\\/:*?"<>|\r\n]+', "", title or "").strip()[:80] or "media"
    return base + suffix


# ───────────────────────── progress state + animation ─────────────────────────
class Job:
    """Progress state shared between the download thread and the animator."""

    def __init__(self):
        self.done = False
        self.reset()

    def reset(self):
        self.phase = "Starting"
        self.pct = None
        self.speed = None
        self.eta = None
        self.got = None
        self.grand = None


def render(job: Job, i: int, title: str) -> str:
    body = f"{bar(job.pct)} {job.pct:5.1f}%" if job.pct is not None else shuttle(i)
    lines = [
        f"{MOONS[i % len(MOONS)]} <b>{esc(job.phase)}</b> {SPIN[i % len(SPIN)]}",
        "",
        f"<code>{body}</code>",
    ]
    if job.pct is not None:
        info = f"⚡ {fmt_speed(job.speed)}  ·  ⏳ {fmt_eta(job.eta)}"
        if job.got and job.grand:
            info += f"\n📦 {fmt_mb(job.got)} / {fmt_mb(job.grand)}"
        lines.append(info)
    lines += ["", f"🎞️ {esc(title[:90])}"]
    return "\n".join(lines)


def wait_secs(e) -> float:
    ra = getattr(e, "retry_after", 1)
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def safe_edit(msg, text, markup=None):
    for _ in range(2):
        try:
            if msg.photo:
                await msg.edit_caption(caption=text[:1024], parse_mode=HTML, reply_markup=markup)
            else:
                await msg.edit_text(text, parse_mode=HTML, reply_markup=markup)
            return
        except RetryAfter as e:
            wait = wait_secs(e)
            log.info("Telegram asked to slow down: waiting %.0fs", wait)
            await asyncio.sleep(wait + 0.5)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("edit failed: %s", e)
            return
        except TelegramError as e:
            log.warning("edit failed: %s", e)
            return


async def ack(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


async def animate(bot, msg, job: Job, title: str, action):
    i = 0
    while not job.done:
        try:
            await safe_edit(msg, render(job, i, title))
            await bot.send_chat_action(msg.chat_id, action)
        except Exception as e:
            log.warning("animation frame failed: %s", e)
        i += 1
        for _ in range(5):  # one frame every 2.5 s
            if job.done:
                break
            await asyncio.sleep(0.5)


async def spin_until(msg, label, stop: asyncio.Event):
    i = 0
    while not stop.is_set():
        await safe_edit(
            msg,
            f"{MOONS[i % len(MOONS)]} <b>{label}</b> {SPIN[i % len(SPIN)]}\n\n<code>{shuttle(i)}</code>",
        )
        i += 1
        try:
            await asyncio.wait_for(stop.wait(), 2.0)
        except asyncio.TimeoutError:
            pass


async def send_with_retry(fn, tries=3):
    for i in range(tries):
        try:
            return await fn()
        except RetryAfter as e:
            await asyncio.sleep(wait_secs(e) + 1)
        except NetworkError:
            if i == tries - 1:
                raise
            await asyncio.sleep(2)
    return await fn()


# ───────────────────────── channel gate ─────────────────────────
async def is_subscribed(bot, user_id: int) -> bool:
    if not REQUIRE_SUB:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
        return m.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError as e:
        log.warning("Subscription check failed (is the bot admin in %s?): %s", CHANNEL, e)
        return False


def join_markup():
    return Markup([
        [Btn("📢 Join Channel", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
        [Btn("✅ I've Joined", callback_data="sub")],
    ])


def purge(store: dict):
    now = time.time()
    for k in [k for k, v in store.items() if now - v["ts"] > 3600]:
        store.pop(k, None)


# ───────────────────────── format selection ─────────────────────────
def is_video(f):
    return f.get("ext") != "mhtml" and f.get("vcodec") != "none" and bool(f.get("vcodec") or f.get("height"))


def is_audio_only(f):
    return f.get("ext") != "mhtml" and f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")


def is_avc(f):
    vc = f.get("vcodec")
    return vc is None or str(vc).startswith(("avc1", "h264"))


def pick_video(videos, limit):
    cands = [f for f in videos if (f.get("height") or 0) <= limit] if limit else videos
    cands = cands or videos
    top = max((f.get("height") or 0) for f in cands)
    if limit:
        pool = [f for f in cands if (f.get("height") or 0) >= top * 0.9]
    else:
        pool = [f for f in cands if (f.get("height") or 0) == top]

    def score(f):
        combined = 1 if f.get("acodec") != "none" else 0
        return (1 if is_avc(f) else 0, f.get("height") or 0, combined, f.get("fps") or 0, f.get("tbr") or 0)

    return max(pool, key=score)


def pick_audio(formats):
    auds = [f for f in formats if is_audio_only(f)]
    if not auds:
        return None
    return max(auds, key=lambda f: (1 if f.get("ext") == "m4a" else 0, f.get("abr") or f.get("tbr") or 0))


def plan_video(formats, limit):
    videos = [f for f in formats if is_video(f)]
    if not videos:
        return []
    v = pick_video(videos, limit)
    if v.get("acodec") != "none":
        return [v]
    a = pick_audio(formats)
    return [v, a] if a else [v]


def est_size(plan):
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in plan]
    return sum(sizes) if plan and all(sizes) else None


def video_tiers(formats):
    seen, tiers = set(), []
    for h in (1080, 720, 480, 360):
        plan = plan_video(formats, h)
        if not plan:
            continue
        hh = plan[0].get("height") or 0
        if hh == 0 or hh > h or hh in seen:
            continue
        seen.add(hh)
        tiers.append((h, hh, est_size(plan)))
    return tiers[:3]


# ───────────────────────── yt-dlp work ─────────────────────────
def extract(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    last = None
    for _ in range(2):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            log.warning("extract failed: %s", e)
            last = e
    raise last


def probe(path: Path) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(r.stdout or "{}")
        streams = data.get("streams") or []
        vs = [s for s in streams
              if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
        return {
            "has_video": bool(vs),
            "width": vs[0].get("width") if vs else None,
            "height": vs[0].get("height") if vs else None,
            "duration": float((data.get("format") or {}).get("duration") or 0) or None,
            "vcodec": vs[0].get("codec_name") if vs else None,
        }
    except Exception as e:
        log.warning("ffprobe failed: %s", e)
        return {"has_video": True, "width": None, "height": None, "duration": None, "vcodec": None}


class NoVideo(Exception):
    pass


class TooBig(Exception):
    pass


def final_path(info, outdir: Path, key: str) -> Path:
    want = {".mp3"} if key == "mp3" else VIDEO_EXT
    for d in info.get("requested_downloads") or []:
        fp = d.get("filepath")
        if fp and Path(fp).is_file() and Path(fp).suffix.lower() in want:
            return Path(fp)
    files = [p for p in outdir.iterdir() if p.is_file() and p.suffix.lower() in want]
    if not files:
        raise RuntimeError("No output file was produced.")
    return max(files, key=lambda p: p.stat().st_size)


def download_once(url, fmt, key, plan, job: Job, outdir: Path, mp3_q: str):
    n = len(plan) or 1
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in plan]
    grand = sum(sizes) if plan and all(sizes) else None
    job.reset()
    job.phase = "Getting stream info"
    job.grand = grand
    files, finished = {}, set()

    def hook(d):
        name = d.get("filename") or ""
        if d["status"] == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            files[name] = done
            got = sum(files.values())
            if grand:
                pct = got / grand * 100
                spd = d.get("speed")
                job.eta = int(max(grand - got, 0) / spd) if spd else None
                job.got = got
            elif total:
                pct = (len(finished) + done / total) / n * 100
                job.eta = d.get("eta")
            else:
                pct = None
            if pct is not None:
                job.pct = min(99.0, max(job.pct or 0.0, pct))
            job.speed = d.get("speed")
            job.phase = "Downloading"
        elif d["status"] == "finished":
            finished.add(name)
            files[name] = d.get("downloaded_bytes") or d.get("total_bytes") or files.get(name, 0)

    def pp_hook(d):
        if d["status"] == "started":
            job.phase = {
                "Merger": "Merging video & audio",
                "FFmpegExtractAudio": "Converting to MP3",
            }.get(d.get("postprocessor", ""), "Processing")
            job.pct = job.speed = job.eta = job.got = None

    opts = {
        "format": fmt,
        "outtmpl": str(outdir / "%(id).80B.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "overwrites": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10485760,
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
    }
    if key == "mp3":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": mp3_q},
            {"key": "FFmpegMetadata"},
        ]
    else:
        opts["merge_output_format"] = "mp4/mkv" if key == "best" else "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    chosen = [(f.get("format_id"), f.get("vcodec"), f.get("acodec"), f.get("height"))
              for f in (info.get("requested_formats") or [info])]
    log.info("downloaded with %r -> %s", fmt, chosen)
    return final_path(info, outdir, key), info


def run_download(url, key, plan, job: Job, outdir: Path, mp3_q: str):
    ids = [f["format_id"] for f in plan if f.get("format_id")]
    if key == "mp3":
        attempts = ["bestaudio/best"]
    else:
        generic = "bv*+ba/b"
        primary = "+".join(ids) if plan and len(ids) == len(plan) else None
        attempts = [f"{primary}/{generic}" if primary else generic, "b"]
    last = None
    for fmt in attempts:
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            path, info = download_once(url, fmt, key, plan, job, outdir, mp3_q)
            meta = None
            if key != "mp3":
                meta = probe(path)
                if not meta["has_video"]:
                    raise NoVideo("The downloaded file has no video stream.")
            return path, info, meta
        except Exception as e:
            log.warning("attempt %r failed: %s", fmt, e)
            last = e
    raise last


# ───────────────────────── handlers ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(context.bot, update.effective_user.id):
        await update.message.reply_text(
            f"🔒 Please join {esc(CHANNEL)} first, then tap <b>I've Joined</b>.",
            reply_markup=join_markup(), parse_mode=HTML,
        )
        return
    await update.message.reply_text(
        f"⚡ <b>{BRAND}</b>\n\nSend a link from YouTube, Instagram, TikTok, Facebook, X and more.",
        reply_markup=Markup([[Btn("📢 Visit Channel", url=f"https://t.me/{CHANNEL.lstrip('@')}")]]),
        parse_mode=HTML,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("👋 Send me a media link, or use /start.")
        return
    url = m.group(0)

    if not await is_subscribed(context.bot, update.effective_user.id):
        await update.message.reply_text(
            f"🔒 Please join {esc(CHANNEL)} first.", reply_markup=join_markup(), parse_mode=HTML
        )
        return

    status = await update.message.reply_text("🔎 <b>Scanning link…</b>", parse_mode=HTML)
    stop = asyncio.Event()
    spin = asyncio.create_task(spin_until(status, "Scanning link", stop))
    info, err = None, ""
    try:
        info = await asyncio.to_thread(extract, url)
    except Exception as e:
        err = short_err(e)
    finally:
        stop.set()
        await spin
    if info is None:
        await safe_edit(status, f"❌ <b>Couldn't read that link.</b>\n<code>{esc(err)}</code>")
        return

    title = info.get("title") or "Untitled"
    uploader = info.get("uploader") or info.get("channel") or ""
    formats = info.get("formats") or [info]
    compact = [{k: f.get(k) for k in KEEP} for f in formats]

    uid = uuid.uuid4().hex[:8]
    pending = context.bot_data.setdefault("pending", {})
    purge(pending)
    pending[uid] = {
        "url": url, "user": update.effective_user.id, "ts": time.time(),
        "title": title, "uploader": uploader, "thumb": info.get("thumbnail"),
        "duration": info.get("duration"), "formats": compact, "busy": False,
    }
    await show_panel(update.message, pending[uid], uid, replace=status)


def panel_text(e: dict, note: str = "") -> str:
    t = f"🎬 <b>{esc(e['title'][:120])}</b>\n"
    if e["uploader"]:
        t += f"👤 {esc(e['uploader'][:60])}\n"
    t += f"⏱️ {fmt_dur(e['duration'])}\n\n"
    if note:
        t += f"{note}\n\n"
    return t + "<b>Choose a format</b>"


def panel_markup(e: dict, uid: str) -> Markup:
    fm = e["formats"]
    rows = []
    for h, hh, est in video_tiers(fm):
        warn = " ⚠️" if est and est > MAX_UPLOAD else ""
        rows.append([Btn(f"🎥 {hh}p{size_label(est)}{warn}", callback_data=f"q:{uid}:{h}")])
    best = plan_video(fm, None)
    if best:
        hh = best[0].get("height")
        est = est_size(best)
        warn = " ⚠️" if est and est > MAX_UPLOAD else ""
        label = "🏆 Best original" + (f" ({hh}p)" if hh else "") + size_label(est) + warn
        rows.append([Btn(label, callback_data=f"q:{uid}:best")])
    rows.append([Btn("🎵 MP3 audio", callback_data=f"q:{uid}:mp3")])
    rows.append([Btn("✖️ Cancel", callback_data=f"x:{uid}")])
    return Markup(rows)


async def show_panel(src_msg, entry, uid, replace=None):
    text, markup = panel_text(entry), panel_markup(entry, uid)
    sent = False
    if entry.get("thumb"):
        try:
            await src_msg.reply_photo(entry["thumb"], caption=text, reply_markup=markup, parse_mode=HTML)
            sent = True
        except TelegramError:
            pass
    if not sent:
        await src_msg.reply_text(text, reply_markup=markup, parse_mode=HTML)
    if replace:
        try:
            await replace.delete()
        except TelegramError:
            pass


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    action = parts[0]

    if action == "sub":
        if await is_subscribed(context.bot, q.from_user.id):
            await ack(q, "Access granted ✅")
            await safe_edit(q.message, "✅ <b>Access granted.</b>\nSend a link to start.")
        else:
            await ack(q, "You haven't joined the channel yet.", True)
        return

    pending = context.bot_data.setdefault("pending", {})
    uid = parts[1] if len(parts) > 1 else ""
    entry = pending.get(uid)
    if not entry:
        await ack(q, "Session expired. Send the link again.", True)
        return
    if q.from_user.id != entry["user"]:
        await ack(q, "This panel belongs to someone else.", True)
        return

    if action == "x":
        await ack(q, "Cancelled")
        pending.pop(uid, None)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return

    if action != "q" or len(parts) < 3:
        await ack(q)
        return

    if not await is_subscribed(context.bot, q.from_user.id):
        await ack(q, "Join the channel first.", True)
        return
    if entry.get("busy"):
        await ack(q, "Already working on it…")
        return

    # MP3 and video+audio merging both need ffmpeg: say so clearly instead of failing silently
    if not shutil.which("ffmpeg"):
        await ack(q, "The server is missing ffmpeg, so this can't work yet. Ask the bot owner to install it.", True)
        return

    key = parts[2]
    limit_mb = MAX_UPLOAD // 1048576
    plan, mp3_q = [], "320"

    if key == "mp3":
        dur = entry.get("duration")
        if dur:
            for br in (320, 192, 128):
                if dur * br * 125 <= MAX_UPLOAD:
                    mp3_q = str(br)
                    break
            else:
                await ack(q, f"Too long for MP3 under {limit_mb} MB.", True)
                return
    else:
        try:
            limit = None if key == "best" else int(key)
        except ValueError:
            await ack(q)
            return
        plan = plan_video(entry["formats"], limit)
        if not plan:
            await ack(q, "No video found. Try MP3.", True)
            return
        est = est_size(plan)
        if est and est > MAX_UPLOAD:
            await ack(q, f"About {est / 1048576:.0f} MB, over the {limit_mb} MB limit.", True)
            return

    entry["busy"] = True
    await ack(q, "⏳ Starting download…")

    msg = q.message
    title = entry["title"]
    job = Job()
    outdir = DOWNLOAD_DIR / f"{uid}_{key}"
    error, detail = None, ""
    action_type = ChatAction.UPLOAD_VOICE if key == "mp3" else ChatAction.UPLOAD_VIDEO
    log.info("plan for %s: %s", key,
             [(f.get("format_id"), f.get("vcodec"), f.get("acodec"), f.get("height")) for f in plan])

    try:
        anim = asyncio.create_task(animate(context.bot, msg, job, title, action_type))
        async with SEM:
            try:
                path, info, meta = await asyncio.to_thread(
                    run_download, entry["url"], key, plan, job, outdir, mp3_q
                )
                job.reset()
                job.phase = "Uploading to Telegram"
                if path.stat().st_size > MAX_UPLOAD:
                    raise TooBig()

                chat_id = msg.chat_id
                await context.bot.send_chat_action(chat_id, action_type)

                bits = []
                if meta and meta.get("height"):
                    bits.append(f"{meta['height']}p")
                if meta and meta.get("vcodec"):
                    bits.append(meta["vcodec"])
                bits.append(fmt_mb(path.stat().st_size))
                qlabel = " · ".join(bits) + " · "
                caption = f"<b>{esc(title[:200])}</b>\n<i>{qlabel}via {BRAND}</i>"
                net = dict(read_timeout=600, write_timeout=600, connect_timeout=30, pool_timeout=30)
                fname = safe_name(title, path.suffix)
                duration = int((meta or {}).get("duration") or info.get("duration") or 0) or None

                async def send_file():
                    with open(path, "rb") as f:
                        if key == "mp3":
                            return await context.bot.send_audio(
                                chat_id, f, filename=fname, title=title[:64],
                                performer=(entry["uploader"] or None), duration=duration,
                                caption=caption, parse_mode=HTML, **net,
                            )
                        return await context.bot.send_video(
                            chat_id, f, filename=fname, supports_streaming=True,
                            width=meta.get("width"), height=meta.get("height"), duration=duration,
                            caption=caption, parse_mode=HTML, **net,
                        )

                await send_with_retry(send_file)
            except TooBig:
                error = "big"
            except NoVideo:
                error = "novideo"
            except yt_dlp.utils.DownloadError as e:
                log.warning("download failed: %s", e)
                error, detail = "dl", short_err(e)
            except Exception as e:
                log.exception("job failed")
                error, detail = "other", short_err(e)
            finally:
                job.done = True
                await anim
    finally:
        entry["busy"] = False
        shutil.rmtree(outdir, ignore_errors=True)

    if error is None:
        pending.pop(uid, None)
        try:
            await msg.delete()
        except TelegramError:
            pass
        return

    notes = {
        "big": f"⚠️ Over the {limit_mb} MB limit. Try a lower quality or MP3.",
        "novideo": "⚠️ No video stream came through. Try another quality or MP3.",
        "dl": "❌ Download failed.",
        "other": "❌ Something went wrong.",
    }
    note = notes[error]
    if detail:
        note += f"\n<code>{esc(detail)}</code>"
    await safe_edit(msg, panel_text(entry, note), panel_markup(entry, uid))


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


def main():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not shutil.which("ffmpeg"):
        log.error("ffmpeg NOT found: MP3 and video+audio merging will fail.")
    if not shutil.which("ffprobe"):
        log.warning("ffprobe not found: files will not be verified before sending.")
    if not (shutil.which("deno") or shutil.which("node")):
        log.warning("No JavaScript runtime (deno) found: YouTube will likely fail.")
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(32)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
