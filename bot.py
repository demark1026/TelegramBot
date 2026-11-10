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
BRAND = "CoreViewz Universal Media Downloader"
DOWNLOAD_DIR = Path("downloads")
SEM = asyncio.Semaphore(4)
URL_RE = re.compile(r"https?://\S+")
HTML = ParseMode.HTML

NSFW_WORDS = [
    "porn", "pornhub", "xxx", "xvideos", "xhamster", "redtube", "brazzers",
    "nsfw", "onlyfans", "nude", "nudes", "fetish", "erotic", "hentai", "sex",
    "strip", "camgirl", "cammodel", "bdsm", "fuck", "pussy", "dick"
]
NSFW_RE = re.compile(r"\b(" + "|".join(NSFW_WORDS) + r")\b", re.I)

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MOONS = "🌑🌒🌓🌕🌖🌗🌘"
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".3gp", ".avi", ".ts"}
KEEP = ("format_id", "ext", "vcodec", "acodec", "height", "width", "fps",
        "tbr", "abr", "filesize", "filesize_approx")


def is_nsfw(*parts: str) -> bool:
    return bool(NSFW_RE.search(" ".join(p for p in parts if p)))


def esc(s) -> str:
    return html.escape(str(s or ""))


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


def bar(pct, n=12):
    filled = int(max(0, min(100, pct)) / 100 * n)
    return "▰" * filled + "▱" * (n - filled)


class Job:
    def _init_(self):
        self.done = False
        self.phase = "Starting"
        self.pct = 0.0
        self.speed = None
        self.eta = None
        self.got = None
        self.grand = None

    def reset(self):
        self.phase = "Starting"
        self.pct = 0.0
        self.speed = None
        self.eta = None
        self.got = None
        self.grand = None


def render(job: Job, i: int, title: str) -> str:
    body = f"{bar(job.pct)} {job.pct:5.1f}%" if job.pct is not None else "▱▱▱▱▱▱▱▱▱▱▱▱"
    lines = [
        f"🚀 <b>{esc(job.phase)}</b> {SPIN[i % len(SPIN)]}",
        f"<code>{body}</code>",
    ]
    if job.pct is not None and job.pct > 0:
        info = f"⚡ {fmt_speed(job.speed)}  ·  ⏳ {fmt_eta(job.eta)}"
        if job.got and job.grand:
            info += f"  ·  📦 {fmt_mb(job.got)}/{fmt_mb(job.grand)}"
        lines.append(info)
    lines.append(f"🎞️ <i>{esc(title[:60])}</i>")
    return "\n".join(lines)


async def safe_edit(msg, text, markup=None):
    try:
        if msg.photo:
            await msg.edit_caption(caption=text[:1024], parse_mode=HTML, reply_markup=markup)
        else:
            await msg.edit_text(text, parse_mode=HTML, reply_markup=markup)
    except Exception:
        pass


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
        except Exception:
            pass
        i += 1
        await asyncio.sleep(0.8)


async def spin_until(msg, label, stop: asyncio.Event):
    i = 0
    while not stop.is_set():
        await safe_edit(msg, f"🔎 <b>{label}</b> {SPIN[i % len(SPIN)]}")
        i += 1
        try:
            await asyncio.wait_for(stop.wait(), 0.6)
        except asyncio.TimeoutError:
            pass


async def send_with_retry(fn, tries=3):
    for i in range(tries):
        try:
            return await fn()
        except NetworkError:
            if i == tries - 1:
                raise
            await asyncio.sleep(2)
    return await fn()


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
    except TelegramError:
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
    if not cands:
        return None
    top = max((f.get("height") or 0) for f in cands)
    if limit:
        pool = [f for f in cands if (f.get("height") or 0) >= top * 0.9]
    else:
        pool = [f for f in cands if (f.get("height") or 0) == top]

    def score(f):
        combined = 1 if f.get("acodec") != "none" else 0
        return (1 if is_avc(f) else 0, f.get("height") or 0, combined, f.get("fps") or 0, f.get("tbr") or 0)

    return max(pool, key=score) if pool else None


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
    if not v:
        return []
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
    
    if not tiers:
        best = plan_video(formats, None)
        if best:
            hh = best[0].get("height") or 0
            tiers.append((hh or 720, hh, est_size(best)))
            
    return tiers[:3]


def get_ydl_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 10,
        "extractor_args": {"instagram": {"max_comments": 0}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        }
    }


def extract(url: str) -> dict:
    with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def probe(path: Path) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(r.stdout or "{}")
        streams = data.get("streams") or []
        vs = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
        return {
            "has_video": bool(vs),
            "width": vs[0].get("width") if vs else None,
            "height": vs[0].get("height") if vs else None,
            "duration": float((data.get("format") or {}).get("duration") or 0) or None,
            "vcodec": vs[0].get("codec_name") if vs else None,
        }
    except Exception:
        return {"has_video": True, "width": None, "height": None, "duration": None}


class NoVideo(Exception): pass
class TooBig(Exception): pass


def final_path(info, outdir: Path, key: str) -> Path:
    want = {".mp3"} if key == "mp3" else VIDEO_EXT
    for d in info.get("requested_downloads") or []:
        fp = d.get("filepath")
        if fp and Path(fp).is_file() and Path(fp).suffix.lower() in want:
            return Path(fp)
    files = [p for p in outdir.iterdir() if p.is_file() and p.suffix.lower() in want]
    if not files:
        raise RuntimeError("No output file produced.")
    return max(files, key=lambda p: p.stat().st_size)


def download_once(url, fmt, key, plan, job: Job, outdir: Path, mp3_q: str):
    n = len(plan) or 1
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in plan]
    grand = sum(sizes) if plan and all(sizes) else None
    job.reset()
    job.phase = "Downloading stream"
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
        elif d["status"] == "finished":
            finished.add(name)

    def pp_hook(d):
        if d["status"] == "started":
            job.phase = "Muxing & Processing"
            job.pct = 99.0

    opts = get_ydl_opts()
    opts.update({
        "format": fmt,
        "outtmpl": str(outdir / "%(id).80B.%(ext)s"),
        "noprogress": True,
        "overwrites": True,
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 8,
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
    })
    
    if key == "mp3":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": mp3_q}]
    else:
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
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
                    raise NoVideo()
            return path, info, meta
        except Exception as e:
            last = e
    raise last


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(context.bot, update.effective_user.id):
        await update.message.reply_text(f"🔒 Please join {esc(CHANNEL)} first.", reply_markup=join_markup())
        return
    await update.message.reply_text(
        f"⚡ <b>{BRAND}</b>\n\n"
        "📥 Send any media link from *Instagram, YouTube, TikTok, Facebook, X & more*.\n\n"
        "🚨 *Zero-Tolerance Policy:* Adult or pornographic content is strictly blocked.",
        parse_mode=HTML,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("👋 Send a media link to start downloading.")
        return
    url = m.group(0)

    if not await is_subscribed(context.bot, update.effective_user.id):
        await update.message.reply_text(f"🔒 Please join {esc(CHANNEL)} first.", reply_markup=join_markup())
        return
    
    if is_nsfw(url):
        await update.message.reply_text("⛔ <b>Access Denied:</b> Adult content is strictly prohibited.")
        return

    status = await update.message.reply_text("🔎 <b>Scanning link…</b>", parse_mode=HTML)
    stop = asyncio.Event()
    spin = asyncio.create_task(spin_until(status, "Inspecting link", stop))
    try:
        info = await asyncio.to_thread(extract, url)
    except Exception:
        info = None
    finally:
        stop.set()
        await spin
    
    if info is None:
        await safe_edit(status, "❌ <b>Couldn't process link.</b> Private or restricted.")
        return

    title = info.get("title") or "Untitled"
    uploader = info.get("uploader") or info.get("channel") or ""
    
    if (info.get("age_limit") or 0) >= 18 or is_nsfw(title, uploader):
        await safe_edit(status, "⛔ <b>Blocked:</b> Mature/explicit content.")
        return

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
    
    text_content = f"🎬 <b>{esc(title[:100])}</b>\n\n<b>👇 Choose format:</b>"
    rows = []
    for h, hh, est in video_tiers(compact):
        rows.append([Btn(f"🎥 Video {hh}p{size_label(est)}", callback_data=f"q:{uid}:{h}")])
    best = plan_video(compact, None)
    if best:
        hh = best[0].get("height")
        rows.append([Btn(f"🏆 Best Quality" + (f" ({hh}p)" if hh else ""), callback_data=f"q:{uid}:best")])
    rows.append([Btn("🎵 MP3 Audio", callback_data=f"q:{uid}:mp3")])
    rows.append([Btn("✖️ Cancel", callback_data=f"x:{uid}")])
    
    try:
        await status.delete()
    except Exception:
        pass
    
    if entry_thumb := info.get("thumbnail"):
        try:
            await update.message.reply_photo(entry_thumb, caption=text_content, reply_markup=Markup(rows), parse_mode=HTML)
            return
        except Exception:
            pass
    await update.message.reply_text(text_content, reply_markup=Markup(rows), parse_mode=HTML)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":")
    action = parts[0]

    if action == "sub":
        if await is_subscribed(context.bot, q.from_user.id):
            await ack(q, "Access granted ✅")
            await safe_edit(q.message, "✅ <b>Access granted.</b> Send link again.")
        else:
            await ack(q, "Join channel first.", True)
        return

    pending = context.bot_data.setdefault("pending", {})
    uid = parts[1] if len(parts) > 1 else ""
    entry = pending.get(uid)
    if not entry:
        await ack(q, "Session expired.", True)
        return

    if action == "x":
        await ack(q, "Cancelled")
        pending.pop(uid, None)
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if action != "q" or len(parts) < 3:
        return

    key = parts[2]
    plan, mp3_q = [], "320"
    if key == "mp3":
        pass
    else:
        try:
            limit = None if key == "best" else int(key)
        except ValueError:
            return
        plan = plan_video(entry["formats"], limit)
        if not plan:
            await ack(q, "No video stream found.", True)
            return

    await ack(q, "Starting download...")
    msg = q.message
    try:
        await msg.delete()  # INSTANTLY VANISH THE BUTTON PANEL
    except Exception:
        pass

    msg = await context.bot.send_message(chat_id=msg.chat_id, text="🚀 <code>Initializing...</code>", parse_mode=HTML)
    
    title = entry["title"]
    job = Job()
    outdir = DOWNLOAD_DIR / f"{uid}_{key}"
    error = None

    anim = asyncio.create_task(animate(context.bot, msg, job, title, ChatAction.UPLOAD_VIDEO))
    try:
        async with SEM:
            path, info, meta = await asyncio.to_thread(
                run_download, entry["url"], key, plan, job, outdir, mp3_q
            )
            job.done = await anim
            job.phase = "Uploading file"
            await safe_edit(msg, "🚀 <b>Uploading file to Telegram...</b>")

            chat_id = msg.chat_id
            async def send_file():
                with open(path, "rb") as f:
                    if key == "mp3":
                        return await context.bot.send_audio(chat_id, f, title=title[:64], parse_mode=HTML)
                    return await context.bot.send_video(chat_id, f, supports_streaming=True, parse_mode=HTML)

            await send_with_retry(send_file)
    except Exception as e:
        log.error("Download error: %s", e)
        error = True
    finally:
        job.done = True
        shutil.rmtree(outdir, ignore_errors=True)

    # AUTO-VANISH ANIMATION MESSAGE COMPLETELY WHEN DONE
    try:
        await msg.delete()
    except Exception:
        pass

    if error:
        await context.bot.send_message(chat_id=msg.chat_id, text="❌ <b>Download failed.</b> Try another link or MP3.")


def main():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
