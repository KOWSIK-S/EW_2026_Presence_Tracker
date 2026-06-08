"""
Home Presence Tracker — Raspberry Pi 4 Edition
Optimised for permanent deployment alongside Pi-hole.

Environment variables (set in .env):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID   — admin/owner chat ID (integer)
    ADMIN_PIN
    SECRET_KEY
    DEV_MODE           — optional, 'True' for debug logging

Ports:
    5000 — Viewer  (public LAN, read-only)
    5001 — Admin   (PIN-protected)

Telegram commands (register with BotFather → /setcommands):
    start      - Main menu
    live       - Who is home right now
    entry      - Most recent arrival
    exit       - Most recent departure
    activity   - Device activity log (last 25 events)
    links      - Local dashboard URLs
    syslog     - System on/off log (last 15 days)
    sysinfo    - Live CPU / RAM / disk / network stats
    reboot     - Restart the tracker service (confirms first)
"""

# ─────────────────────────────────────────────────────────────────────────────
# STDLIB
# ─────────────────────────────────────────────────────────────────────────────
import os
import re
import signal
import time
import asyncio
import json
import logging
import threading
import uuid
import socket
import html as html_lib
import queue
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from collections import defaultdict

# ─────────────────────────────────────────────────────────────────────────────
# THIRD-PARTY
# ─────────────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
from flask import Flask, request, redirect, Response, session
import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from bleak import BleakScanner
from waitress import serve

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG & SECRETS
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
ADMIN_PIN  = os.environ.get("ADMIN_PIN", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DEV_MODE   = os.environ.get("DEV_MODE", "False").lower() == "true"

_missing = [k for k, v in {
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    "TELEGRAM_CHAT_ID":   CHAT_ID,
    "ADMIN_PIN":          ADMIN_PIN,
    "SECRET_KEY":         SECRET_KEY,
}.items() if not v]
if _missing:
    raise SystemExit(f"FATAL: missing env vars: {', '.join(_missing)}")

try:
    ALLOWED_CHAT_ID = int(CHAT_ID)
except ValueError:
    raise SystemExit("FATAL: TELEGRAM_CHAT_ID must be an integer")

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if DEV_MODE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tracker")

if not DEV_MODE:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("waitress").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_MAC_RE    = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_SUBNET_RE = re.compile(r"^(\d{1,3}\.){3}$")

def is_valid_mac(mac: str) -> bool:
    return bool(_MAC_RE.match(mac.lower()))

def is_valid_subnet(subnet: str) -> bool:
    if not _SUBNET_RE.match(subnet):
        return False
    return all(0 <= int(p) <= 255 for p in subnet.rstrip(".").split("."))

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
DB_FILE = Path(__file__).parent / "tracker_db.json"
db_lock = threading.Lock()
db: dict = {}

DEFAULT_DB: dict = {
    "subnet_prefix":    "192.168.137.",
    "cooldown_seconds": 600,
    "devices":          {},
    "viewer_ids":       {},
    "system_events":    [],
}

ADMIN_VIEWER_ID = "admin"
ADMIN_VIEWER_NAME = "Admin"

_MAX_DEVICE_LOGS = 1000
_MAX_SYS_EVENTS  = 500   # higher so syslog covers 15 days reliably

def _enforce_caps(data: dict) -> None:
    for dev in data.get("devices", {}).values():
        dev["logs"] = dev["logs"][:_MAX_DEVICE_LOGS]
    data["system_events"] = data["system_events"][:_MAX_SYS_EVENTS]

def load_db() -> None:
    global db
    if DB_FILE.exists():
        try:
            with open(DB_FILE) as f:
                db = json.load(f)
            for k, v in DEFAULT_DB.items():
                db.setdefault(k, v)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("DB load failed (%s) — starting fresh", exc)
            db = DEFAULT_DB.copy()
    else:
        db = DEFAULT_DB.copy()

    for dev in db.get("devices", {}).values():
        dev.setdefault("ble_name",          "")
        dev.setdefault("location",          "Location")
        dev.setdefault("enabled",           True)
        dev.setdefault("is_home",           False)
        dev.setdefault("last_seen",         0)
        dev.setdefault("logs",              [])
        dev.setdefault("notify_viewer_ids", [])
        dev["_needs_revalidation"] = True   # cleared after first scan

    viewers = db.setdefault("viewer_ids", {})
    viewers.setdefault(ADMIN_VIEWER_ID, {
        "nickname": ADMIN_VIEWER_NAME,
        "chat_id": ALLOWED_CHAT_ID,
    })

    _enforce_caps(db)
    _save_db_unlocked()

def _save_db_unlocked() -> None:
    import copy
    snapshot = copy.deepcopy(db)
    for dev in snapshot.get("devices", {}).values():
        dev.pop("_needs_revalidation", None)
    tmp = DB_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2)
        tmp.replace(DB_FILE)
    except OSError as exc:
        logger.error("DB save failed: %s", exc)

load_db()

_boot_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
db["system_events"].insert(0, f"{_boot_ts} - BOOT (DEV_MODE={DEV_MODE})")
_enforce_caps(db)
_save_db_unlocked()

# ─────────────────────────────────────────────────────────────────────────────
# GENERAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def _esc(v) -> str:
    return html_lib.escape(str(v))

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def _now_sec() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _parse_log_dt(log_line: str) -> str:
    return log_line[:16] if len(log_line) >= 16 else ""

def _uptime_str() -> str:
    up = int(time.time() - psutil.boot_time())
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)

def trigger_hardware_failsafe(reason: str) -> None:
    logger.critical("FAIL-SAFE TRIGGERED: %s", reason)
    # Placeholder: add GPIO relay logic here
    with db_lock:
        db["system_events"].insert(0, f"{_now_sec()} - FAILSAFE: {reason}")
        _enforce_caps(db)
        _save_db_unlocked()

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN LOGIN RATE LIMITER
# ─────────────────────────────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = defaultdict(list)
_login_lock = threading.Lock()
_LOGIN_MAX  = 5
_LOGIN_WIN  = 300  # seconds

def is_login_allowed(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WIN]
        return len(_login_attempts[ip]) < _LOGIN_MAX

def record_failed_login(ip: str) -> None:
    with _login_lock:
        _login_attempts[ip].append(time.time())

# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATION QUEUE  (thread-safe; dedicated sender daemon)
# ─────────────────────────────────────────────────────────────────────────────
_notify_queue: queue.Queue = queue.Queue()
scan_lock = threading.Lock()

def _enqueue(text: str, chat_ids: list) -> None:
    _notify_queue.put_nowait((text, chat_ids))

def _telegram_sender_thread() -> None:
    while True:
        try:
            text, chat_ids = _notify_queue.get(timeout=5)
        except queue.Empty:
            continue
        for cid in chat_ids:
            try:
                bot.send_message(cid, text, parse_mode="Markdown")
            except Exception as exc:
                logger.warning("Telegram send to %s failed: %s", cid, exc)
        _notify_queue.task_done()

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT
# ─────────────────────────────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

def is_authorized(obj) -> bool:
    cid = obj.chat.id if hasattr(obj, "chat") else obj.message.chat.id
    return cid == ALLOWED_CHAT_ID

def _menu_markup() -> InlineKeyboardMarkup:
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("Who's Home",       callback_data="cmd_live"))
    m.row(
        InlineKeyboardButton("Last Arrival",  callback_data="cmd_entry"),
        InlineKeyboardButton("Last Departure", callback_data="cmd_exit"),
    )
    m.row(InlineKeyboardButton("Device Activity",  callback_data="cmd_activity"))
    m.row(InlineKeyboardButton("Dashboard Links",  callback_data="cmd_links"))
    m.row(InlineKeyboardButton("System Log",       callback_data="cmd_syslog"))
    m.row(InlineKeyboardButton("System Info",      callback_data="cmd_sysinfo"))
    return m

# ── Content builders (pure functions, no bot calls) ───────────────────────────

def _build_live() -> str:
    with db_lock:
        devices = {k: dict(v) for k, v in db["devices"].items()}
    home = [
        (d["nickname"],
         datetime.fromtimestamp(d["last_seen"]).strftime("%H:%M") if d["last_seen"] else "?")
        for d in devices.values()
        if d.get("is_home") and d.get("enabled")
    ]
    if not home:
        return f"⚪️ *Nobody home* as of {_now()}"
    lines = "\n".join(f"• *{n}* (last seen {t})" for n, t in home)
    return f"🟢 *Currently home* — {_now()}\n\n{lines}"

def _build_last_event(keyword: str, icon: str, label: str) -> str:
    with db_lock:
        devices = {k: dict(v) for k, v in db["devices"].items()}
    candidates = [
        log
        for dev in devices.values()
        for log in dev.get("logs", [])
        if keyword in log
    ]
    if not candidates:
        return f"{icon} No {label} recorded yet."
    latest = max(candidates, key=_parse_log_dt)
    return f"{icon} *Most recent {label}:*\n`{latest}`\n\n_Checked {_now()}_"

def _build_links() -> str:
    ip = get_local_ip()
    return (
        f"🔗 *Dashboard Links* — {_now()}\n\n"
        f"👀 Viewer:  `http://{ip}:5000`\n"
        f"⚙️ Admin:   `http://{ip}:5001`"
    )

def _build_syslog() -> str:
    with db_lock:
        events = list(db.get("system_events", []))

    cutoff  = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
    recent  = [e for e in events if e[:10] >= cutoff]
    uptime  = _uptime_str()

    # Calculate last offline duration from SHUTDOWN → next BOOT pair
    offline_note = ""
    last_shutdown = next((e for e in events if "SHUTDOWN" in e or "Signal" in e), None)
    last_boot     = next((e for e in events if "BOOT" in e), None)
    if last_shutdown and last_boot:
        try:
            t_off  = datetime.strptime(last_shutdown[:19], "%Y-%m-%d %H:%M:%S")
            t_boot = datetime.strptime(last_boot[:19],     "%Y-%m-%d %H:%M:%S")
            if t_boot > t_off:
                diff = t_boot - t_off
                h, r = divmod(int(diff.total_seconds()), 3600)
                offline_note = f"\n⏱ Last offline: *{h}h {r // 60}m*"
        except ValueError:
            pass

    body = "\n".join(f"`{e}`" for e in recent[:30]) if recent else "_No events in last 15 days._"
    return (
        f"📜 *System Log* — last 15 days\n"
        f"🕐 Uptime: *{uptime}*{offline_note}\n\n"
        f"{body}"
    )

def _build_sysinfo() -> str:
    cpu    = psutil.cpu_percent(interval=0.5)
    ram    = psutil.virtual_memory()
    disk   = psutil.disk_usage("/")
    ip     = get_local_ip()
    uptime = _uptime_str()

    # 1-second network I/O sample
    n1 = psutil.net_io_counters()
    time.sleep(1)
    n2 = psutil.net_io_counters()
    tx = (n2.bytes_sent - n1.bytes_sent) / 1024
    rx = (n2.bytes_recv - n1.bytes_recv) / 1024

    # CPU temperature (Pi-specific, graceful fallback)
    temp_line = ""
    try:
        temps = psutil.sensors_temperatures()
        sensor = temps.get("cpu_thermal") or temps.get("coretemp")
        if sensor:
            temp_line = f"🌡 CPU temp:  *{sensor[0].current:.1f}°C*\n"
    except (AttributeError, KeyError):
        pass

    return (
        f"📊 *System Info* — {_now()}\n\n"
        f"🖥 CPU:      *{cpu}%*\n"
        f"{temp_line}"
        f"🧠 RAM:      *{ram.percent}%* ({ram.used // 1_048_576} / {ram.total // 1_048_576} MB)\n"
        f"💾 Disk:     *{disk.percent}%* "
        f"({disk.used // 1_073_741_824:.1f} / {disk.total // 1_073_741_824:.1f} GB)\n"
        f"📶 Network:  ↑ {tx:.1f} KB/s  ↓ {rx:.1f} KB/s\n"
        f"🌐 LAN IP:   `{ip}`\n"
        f"⏱ Uptime:   *{uptime}*"
    )

def _device_picker_markup() -> tuple[str, InlineKeyboardMarkup | None]:
    """Returns (message_text, markup). markup is None if no devices."""
    with db_lock:
        devices = {k: dict(v) for k, v in db["devices"].items()}
    enabled = {k: v for k, v in devices.items() if v.get("enabled")}
    if not enabled:
        return "No devices configured.", None
    markup = InlineKeyboardMarkup()
    for d_id, dev in enabled.items():
        icon = "🟢" if dev["is_home"] else "⚫"
        markup.row(InlineKeyboardButton(
            f"{icon} {dev['nickname']}",
            callback_data=f"devlog_{d_id}",
        ))
    return f"📱 *Device Activity* — {_now()}\nSelect a device:", markup

# ── Command handlers ──────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_authorized(message):
        return
    bot.send_message(
        message.chat.id,
        f"🤖 *Home Tracker* — {_now()}\nSelect an option:",
        reply_markup=_menu_markup(),
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["live"])
def cmd_live(message):
    if not is_authorized(message):
        return
    bot.send_message(message.chat.id, _build_live(), parse_mode="Markdown")

@bot.message_handler(commands=["entry"])
def cmd_entry(message):
    if not is_authorized(message):
        return
    bot.send_message(
        message.chat.id,
        _build_last_event("ARRIVED", "➡️", "arrival"),
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["exit"])
def cmd_exit(message):
    if not is_authorized(message):
        return
    bot.send_message(
        message.chat.id,
        _build_last_event("DEPARTED", "⬅️", "departure"),
        parse_mode="Markdown",
    )

@bot.message_handler(commands=["activity"])
def cmd_activity(message):
    if not is_authorized(message):
        return
    text, markup = _device_picker_markup()
    if markup:
        bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["links"])
def cmd_links(message):
    if not is_authorized(message):
        return
    bot.send_message(message.chat.id, _build_links(), parse_mode="Markdown")

@bot.message_handler(commands=["syslog"])
def cmd_syslog(message):
    if not is_authorized(message):
        return
    bot.send_message(message.chat.id, _build_syslog(), parse_mode="Markdown")

@bot.message_handler(commands=["sysinfo"])
def cmd_sysinfo(message):
    if not is_authorized(message):
        return
    bot.send_message(message.chat.id, _build_sysinfo(), parse_mode="Markdown")

@bot.message_handler(commands=["reboot"])
def cmd_reboot(message):
    if not is_authorized(message):
        return
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Yes, restart", callback_data="reboot_confirm"),
        InlineKeyboardButton("❌ Cancel",        callback_data="reboot_cancel"),
    )
    bot.send_message(
        message.chat.id,
        "⚠️ *Restart tracker service?*\nSystemd will restore it automatically.",
        reply_markup=markup,
        parse_mode="Markdown",
    )

# ── Catch-all: unknown messages just show the menu ────────────────────────────
@bot.message_handler(func=lambda m: True)
def catch_all(message):
    if not is_authorized(message):
        return
    bot.send_message(
        message.chat.id,
        f"🤖 *Home Tracker* — {_now()}\nSelect an option:",
        reply_markup=_menu_markup(),
        parse_mode="Markdown",
    )

# ── Callback query router ─────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: is_authorized(call))
def handle_query(call):
    data    = call.data
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    if data == "cmd_live":
        bot.send_message(chat_id, _build_live(), parse_mode="Markdown")

    elif data == "cmd_entry":
        bot.send_message(chat_id, _build_last_event("ARRIVED", "➡️", "arrival"), parse_mode="Markdown")

    elif data == "cmd_exit":
        bot.send_message(chat_id, _build_last_event("DEPARTED", "⬅️", "departure"), parse_mode="Markdown")

    elif data == "cmd_activity":
        text, markup = _device_picker_markup()
        if markup:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, text)

    elif data == "cmd_links":
        bot.send_message(chat_id, _build_links(), parse_mode="Markdown")

    elif data == "cmd_syslog":
        bot.send_message(chat_id, _build_syslog(), parse_mode="Markdown")

    elif data == "cmd_sysinfo":
        bot.send_message(chat_id, _build_sysinfo(), parse_mode="Markdown")

    elif data.startswith("devlog_"):
        d_id = data.split("_", 1)[1]
        with db_lock:
            dev = db["devices"].get(d_id)
            if not dev:
                bot.send_message(chat_id, "Device not found.")
                return
            logs   = list(dev["logs"][:25])
            nname  = dev["nickname"]
            status = "🟢 HOME" if dev["is_home"] else "⚫ AWAY"
            last_t = (
                datetime.fromtimestamp(dev["last_seen"]).strftime("%Y-%m-%d %H:%M")
                if dev["last_seen"] else "never"
            )
        body = "\n".join(f"`{l}`" for l in logs) if logs else "_No events recorded yet._"
        back = InlineKeyboardMarkup()
        back.row(InlineKeyboardButton("🔙 Main menu", callback_data="cmd_back_menu"))
        bot.send_message(
            chat_id,
            f"📱 *{nname}* — {status}\n"
            f"_Last seen: {last_t}_\n"
            f"_Last 25 events as of {_now()}_\n\n"
            f"{body}",
            reply_markup=back,
            parse_mode="Markdown",
        )

    elif data == "cmd_back_menu":
        bot.send_message(
            chat_id,
            f"🤖 *Home Tracker* — {_now()}\nSelect an option:",
            reply_markup=_menu_markup(),
            parse_mode="Markdown",
        )

    elif data == "reboot_confirm":
        bot.send_message(
            chat_id,
            "🔄 *Restarting…* systemd will restore service.",
            parse_mode="Markdown",
        )
        logger.info("Remote reboot triggered via Telegram.")
        with db_lock:
            db["system_events"].insert(0, f"{_now_sec()} - REMOTE REBOOT via Telegram")
            _enforce_caps(db)
            _save_db_unlocked()
        os._exit(1)

    elif data == "reboot_cancel":
        bot.send_message(chat_id, "❌ Restart cancelled.")

# ─────────────────────────────────────────────────────────────────────────────
# DETECTION SCANNERS
# ─────────────────────────────────────────────────────────────────────────────
async def get_active_macs(subnet: str) -> set[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmap", "-sn", "--host-timeout", "1s", f"{subnet}0/24",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except FileNotFoundError:
        sem = asyncio.Semaphore(30)
        async def _ping(ip: str):
            async with sem:
                p = await asyncio.create_subprocess_exec(
                    "ping", "-c", "1", "-W", "1", ip,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await p.wait()
        await asyncio.gather(*[_ping(f"{subnet}{i}") for i in range(1, 255)])

    active: set[str] = set()
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[2] not in ("0x0", "0x00"):
                    active.add(parts[3].lower())
    except OSError as exc:
        logger.warning("ARP read failed: %s", exc)
    return active

async def get_active_bles() -> set[str]:
    try:
        found = await BleakScanner.discover(timeout=2.0)
        return {d.name for d in found if d.name}
    except Exception as exc:
        logger.debug("BLE scan error: %s", exc)
        return set()

# ─────────────────────────────────────────────────────────────────────────────
# TRACKING ENGINE
# ─────────────────────────────────────────────────────────────────────────────
async def perform_scan_cycle(first_scan: bool) -> None:
    logger.debug("Scan cycle started (first_scan=%s)", first_scan)
    with scan_lock:
        with db_lock:
            subnet   = db.get("subnet_prefix", "192.168.137.")
            cooldown = db.get("cooldown_seconds", 600)

        try:
            active_macs, active_bles = await asyncio.gather(
                get_active_macs(subnet),
                get_active_bles(),
            )
        except Exception as exc:
            logger.error("Scan error: %s", exc)
            return

        current_time  = time.time()
        db_changed    = False
        notifications: list[tuple[str, list]] = []

        with db_lock:
            viewer_registry = db.get("viewer_ids", {})

            for dev in db["devices"].values():
                if not dev.get("enabled", True):
                    continue

                detected = (
                    dev["mac"].lower() in active_macs
                    or (dev.get("ble_name") and dev["ble_name"] in active_bles)
                )

                recipients = [
                    viewer_registry[vid]["chat_id"]
                    for vid in dev.get("notify_viewer_ids", [])
                    if vid in viewer_registry
                ]

                needs_rev = dev.pop("_needs_revalidation", False)

                if detected:
                    dev["last_seen"] = current_time
                    if not dev["is_home"]:
                        dev["is_home"] = True
                        db_changed = True
                        if not (first_scan and needs_rev):
                            dev["logs"].insert(0, f"{_now()} - {dev['nickname']} ARRIVED")
                            dev["logs"] = dev["logs"][:_MAX_DEVICE_LOGS]
                            notifications.append(
                                (f"{dev['nickname']} arrived — {_now()}", recipients)
                            )
                else:
                    if first_scan and needs_rev and dev["is_home"]:
                        # Ghost presence from previous run — clear silently
                        dev["is_home"] = False
                        db_changed = True
                    elif not first_scan and dev["is_home"] and                             (current_time - dev["last_seen"] > cooldown):
                        dev["is_home"] = False
                        dev["logs"].insert(0, f"{_now()} - {dev['nickname']} DEPARTED")
                        dev["logs"] = dev["logs"][:_MAX_DEVICE_LOGS]
                        db_changed = True
                        notifications.append(
                            (f"{dev['nickname']} left — {_now()}", recipients)
                        )

            if db_changed:
                _enforce_caps(db)
                _save_db_unlocked()

        for text, chat_ids in notifications:
            _enqueue(text, chat_ids)


async def tracking_engine_async() -> None:
    logger.info("Tracking engine started")
    _enqueue("Tracker online. Send /start for the menu.", [ALLOWED_CHAT_ID])
    first_scan = True

    while True:
        await asyncio.sleep(30)
        await perform_scan_cycle(first_scan)
        first_scan = False


def start_tracking_thread() -> None:
    while True:
        try:
            asyncio.run(tracking_engine_async())
        except Exception as exc:
            logger.error("Tracking engine crashed: %s — restarting in 10 s", exc)
            trigger_hardware_failsafe(f"Tracking engine exception: {exc}")
            time.sleep(10)

# ─────────────────────────────────────────────────────────────────────────────
# SHARED HTML/CSS
# ─────────────────────────────────────────────────────────────────────────────
_VIEWER_CSS = (
    "<style>"
    "*{box-sizing:border-box}"
    "body{font-family:system-ui,sans-serif;background:#f7fafc;margin:0;padding:16px;color:#1a202c}"
    ".wrap{max-width:720px;margin:auto}"
    ".topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}"
    ".title h2{margin:0;font-size:1.25rem;color:#1a202c}"
    ".subtitle{font-size:.82rem;color:#718096;margin-top:2px}"
    ".btn{display:inline-block;padding:8px 12px;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:.85rem;color:#fff;background:#4a5568;text-decoration:none}"
    ".btn:hover{opacity:.95}"
    ".card{background:#fff;padding:16px 20px;border-radius:12px;margin-bottom:12px;"
    "box-shadow:0 1px 3px rgba(0,0,0,.1);display:flex;align-items:center;justify-content:space-between;gap:12px}"
    ".badge{padding:4px 10px;border-radius:20px;font-size:.8rem;font-weight:600;white-space:nowrap}"
    ".home{background:#e6fffa;color:#234e52}.away{background:#edf2f7;color:#4a5568}"
    ".sub{font-size:.8rem;color:#718096;margin-top:4px}"
    "a{color:#3182ce;text-decoration:none;font-size:.85rem}a:hover{text-decoration:underline}"
    "ul{list-style:none;padding:0;margin-top:12px}li{padding:9px 0;border-bottom:1px solid #e2e8f0}"
    "li:last-child{border-bottom:none}"
    ".ts{font-size:.75rem;color:#a0aec0;margin-top:14px;text-align:right}"
    "form{margin:0}"
    "</style>"
)
_VIEWER_HEAD = (
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + _VIEWER_CSS
)

_ADMIN_CSS = (
    "<style>"
    "*{box-sizing:border-box}"
    "body{font-family:system-ui,sans-serif;background:#edf2f7;margin:0;padding:16px;color:#2d3748}"
    ".wrap{max-width:960px;margin:auto}"
    ".card{background:#fff;padding:20px;border-radius:12px;margin-bottom:20px;"
    "box-shadow:0 2px 4px rgba(0,0,0,.08)}"
    "h1{font-size:1.4rem;margin:0}"
    "h3{font-size:.9rem;font-weight:700;margin:0 0 14px;color:#4a5568;"
    "border-bottom:1px solid #e2e8f0;padding-bottom:8px;text-transform:uppercase;letter-spacing:.04em}"
    "label{display:block;font-size:.8rem;color:#718096;margin-bottom:2px;margin-top:8px}"
    "label:first-of-type{margin-top:0}"
    "input[type=text],input[type=number],input[type=password]{"
    "width:100%;padding:8px 10px;border:1px solid #cbd5e0;border-radius:6px;"
    "font-size:.9rem;background:#fff;color:#2d3748}"
    ".row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1;min-width:180px}"
    "button,a.btn{display:inline-block;padding:6px 12px;border:none;border-radius:6px;"
    "font-weight:600;cursor:pointer;font-size:.8rem;color:#fff;text-decoration:none;"
    "line-height:1.4}"
    ".bp{background:#4299e1}.bs{background:#38a169}.bd{background:#e53e3e}"
    ".bw{background:#d69e2e}.bg{background:#718096}"
    "table{width:100%;border-collapse:collapse;font-size:.84rem}"
    "th,td{text-align:left;padding:9px 7px;border-bottom:1px solid #e2e8f0;vertical-align:middle}"
    "th{font-size:.72rem;color:#718096;text-transform:uppercase;letter-spacing:.05em;background:#f7fafc}"
    "tr:last-child td{border-bottom:none}"
    ".tag{display:inline-block;background:#ebf4ff;color:#2b6cb0;border-radius:4px;"
    "padding:2px 7px;font-size:.72rem;margin:1px}"
    ".tag-none{color:#a0aec0;font-style:italic;font-size:.78rem}"
    ".on{color:#38a169;font-weight:700}.off{color:#a0aec0}"
    ".note{font-size:.8rem;color:#718096;margin:0 0 14px;line-height:1.55;"
    "background:#f7fafc;border-left:3px solid #bee3f8;padding:8px 12px;border-radius:0 4px 4px 0}"
    ".err{background:#fff5f5;border-left-color:#fc8181;color:#c53030}"
    ".hint{font-size:.74rem;color:#a0aec0;margin:4px 0 8px;line-height:1.5}"
    ".two{display:grid;grid-template-columns:1fr 1fr;gap:20px}"
    "@media(max-width:700px){.two{grid-template-columns:1fr}}"
    ".topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:20px}"
    ".topbar-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}"
    "a.back{color:#718096;font-size:.85rem;text-decoration:none;display:inline-block;"
    "margin-bottom:16px}"
    "a.back:hover{color:#2d3748}"
    "form{margin:0}"
    ".actions form{display:inline}"
    ".actions>*{margin:2px 2px 2px 0;vertical-align:middle}"
    ".flash{padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:.85rem}"
    ".flash-ok{background:#e6fffa;color:#234e52;border:1px solid #9ae6b4}"
    ".flash-err{background:#fff5f5;color:#c53030;border:1px solid #fc8181}"
    "code{background:#edf2f7;padding:2px 5px;border-radius:4px;font-size:.82rem}"
    "</style>"
)
_ADMIN_HEAD = (
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + _ADMIN_CSS
)

def _topbar(title: str = "Admin Panel") -> str:
    return (
        f"<div class='topbar'>"
        f"<h1>{_esc(title)}</h1>"
        f"<div class='topbar-actions'>"
        f"<form action='/refresh' method='POST'><button type='submit' class='bp'>Refresh</button></form>"
        f"<a href='/logout' style='color:#e53e3e;font-weight:700;font-size:.85rem;"
        f"text-decoration:none'>Logout</a>"
        f"</div></div>"
    )

def _flash_html(msg: str, ok: bool = True) -> str:
    if not msg:
        return ""
    cls = "flash-ok" if ok else "flash-err"
    return f"<div class='flash {cls}'>{_esc(msg)}</div>"

# ─────────────────────────────────────────────────────────────────────────────
# FLASK — VIEWER  (port 5000)
# ─────────────────────────────────────────────────────────────────────────────
viewer_app = Flask("viewer")


def _run_manual_refresh() -> None:
    asyncio.run(perform_scan_cycle(False))


@viewer_app.route("/refresh", methods=["POST"])
def viewer_refresh():
    try:
        _run_manual_refresh()
    except Exception as exc:
        logger.error("Manual refresh failed: %s", exc)
    return redirect("/")


@viewer_app.route("/")
def viewer_dashboard():
    with db_lock:
        devices = {k: dict(v) for k, v in db["devices"].items()}
    cards = ""
    for d_id, dev in devices.items():
        if not dev.get("enabled"):
            continue
        status = dev.get("location", "Location") if dev.get("is_home") else "Away"
        cls = "home" if dev.get("is_home") else "away"
        last = (
            datetime.fromtimestamp(dev["last_seen"]).strftime("%Y-%m-%d %H:%M")
            if dev["last_seen"] else "—"
        )
        cards += (
            f"<div class='card'>"
            f"<div><strong>{_esc(dev['nickname'])}</strong>"
            f"<div class='sub'>Last seen: {last} · "
            f"<a href='/logs/{_esc(d_id)}'>Activity</a></div></div>"
            f"<span class='badge {cls}'>{_esc(status)}</span></div>"
        )
    if not cards:
        cards = "<p style='color:#a0aec0'>No devices configured.</p>"
    return (
        f"<html><head>{_VIEWER_HEAD}</head><body>"
        f"<div class='wrap'>"
        f"<div class='topbar'><div class='title'><h2>Home Tracker</h2><div class='subtitle'>Read-only view</div></div>"
        f"<form action='/refresh' method='POST'><button type='submit' class='btn'>Refresh</button></form></div>"
        f"{cards}"
        f"<div class='ts'>Updated {_now()}</div>"
        f"</div></body></html>"
    )

@viewer_app.route("/logs/<d_id>")
def viewer_logs(d_id):
    with db_lock:
        dev = db["devices"].get(d_id)
        if not dev:
            return "Device not found", 404
        logs   = list(dev["logs"][:50])
        nname  = dev["nickname"]
        status = dev.get("location", "Location") if dev.get("is_home") else "Away"
    cls   = "home" if dev.get("is_home") else "away"
    items = "".join(f"<li>{_esc(l)}</li>" for l in logs) or "<li>No events yet.</li>"
    return (
        f"<html><head>{_VIEWER_HEAD}</head><body>"
        f"<div class='wrap'>"
        f"<div class='topbar'><div class='title'><h2>{_esc(nname)}</h2><div class='subtitle'>Activity log</div></div>"
        f"<form action='/refresh' method='POST'><button type='submit' class='btn'>Refresh</button></form></div>"
        f"<div style='margin-top:8px'><span class='badge {cls}' style='font-size:.8rem'>{_esc(status)}</span></div>"
        f"<a href='/'>Back</a><ul>{items}</ul>"
        f"<div class='ts'>Last 50 events · {_now()}</div>"
        f"</div></body></html>"
    )

# ─────────────────────────────────────────────────────────────────────────────
# FLASK — ADMIN  (port 5001)
# ─────────────────────────────────────────────────────────────────────────────
admin_app = Flask("admin")
admin_app.secret_key = SECRET_KEY

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

@admin_app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    ip  = request.remote_addr or "unknown"
    if request.method == "POST":
        if not is_login_allowed(ip):
            err = "Too many attempts — try again in a few minutes."
            logger.warning("Login rate-limit: %s", ip)
        elif request.form.get("pin") == ADMIN_PIN:
            session["logged_in"] = True
            logger.info("Admin login from %s", ip)
            return redirect("/")
        else:
            record_failed_login(ip)
            err = "Invalid PIN."
            logger.warning("Failed login from %s", ip)
    err_html = (
        f"<p class='note err' style='margin-bottom:12px'>{_esc(err)}</p>" if err else ""
    )
    return (
        f"<html><head>{_ADMIN_HEAD}</head><body>"
        f"<div style='display:flex;justify-content:center;align-items:center;min-height:80vh'>"
        f"<div class='card' style='width:100%;max-width:300px;text-align:center'>"
        f"<h3 style='border:none;padding:0;margin-bottom:14px;font-size:1rem;"
        f"text-transform:none;letter-spacing:0;color:#2d3748'>Admin Login</h3>"
        f"{err_html}"
        f"<form method='POST'>"
        f"<input type='password' name='pin' placeholder='PIN' inputmode='numeric' "
        f"maxlength='12' required autofocus style='letter-spacing:5px;text-align:center'>"
        f"<button type='submit' class='bp' style='width:100%;margin-top:10px;padding:9px'>"
        f"Enter</button></form>"
        f"</div></div></body></html>"
    )

@admin_app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect("/login")

# ── Main dashboard ────────────────────────────────────────────────────────────
@admin_app.route("/refresh", methods=["POST"])
@login_required
def admin_refresh():
    try:
        _run_manual_refresh()
        session["flash"] = "Presence data refreshed."
        session["flash_ok"] = True
    except Exception as exc:
        logger.error("Admin refresh failed: %s", exc)
        session["flash"] = f"Refresh failed: {exc}"
        session["flash_ok"] = False
    return redirect("/")


@admin_app.route("/")
@login_required
def admin_dashboard():
    flash_msg = session.pop("flash", "")
    flash_ok  = session.pop("flash_ok", True)

    with db_lock:
        cooldown   = db.get("cooldown_seconds", 600)
        subnet     = db.get("subnet_prefix", "192.168.137.")
        devices    = {k: dict(v) for k, v in db["devices"].items()}
        viewer_ids = {k: dict(v) for k, v in db.get("viewer_ids", {}).items()}

    # Viewer → device name map for the viewer table
    v_device_map: dict[str, list[str]] = {vid: [] for vid in viewer_ids}
    for dev in devices.values():
        for vid in dev.get("notify_viewer_ids", []):
            if vid in v_device_map:
                v_device_map[vid].append(dev["nickname"])

    # ── Global settings ───────────────────────────────────────────────────────
    sec_settings = (
        f"<div class='card'><h3>Global Settings</h3>"
        f"<form action='/update_config' method='POST'>"
        f"<div class='row'>"
        f"<div><label>Cooldown (seconds)</label>"
        f"<input type='number' name='cooldown' value='{cooldown}' min='30' required></div>"
        f"<div><label>Subnet prefix</label>"
        f"<input type='text' name='subnet' value='{_esc(subnet)}' "
        f"placeholder='192.168.1.' required></div>"
        f"</div>"
        f"<p class='hint'>Subnet format: 192.168.x. — validated server-side.</p>"
        f"<button type='submit' class='bp' style='margin-top:10px'>Save</button>"
        f"</form></div>"
    )

    # ── Viewer IDs ────────────────────────────────────────────────────────────
    v_rows = ""
    for vid, vdata in viewer_ids.items():
        linked_tags = "".join(
            f"<span class='tag'>{_esc(n)}</span>"
            for n in v_device_map.get(vid, [])
        ) or "<span class='tag-none'>no devices linked</span>"
        if vid == ADMIN_VIEWER_ID:
            action_cell = "<span class='tag-none'>built in</span>"
        else:
            action_cell = (
                f"<form action='/delete_viewer/{_esc(vid)}' method='POST'>"
                f"<button class='bd' onclick=\"return confirm('Delete {_esc(vdata['nickname'])}?')\">"
                f"Delete</button></form>"
            )
        v_rows += (
            f"<tr><td><b>{_esc(vdata['nickname'])}</b></td>"
            f"<td><code>{_esc(vdata['chat_id'])}</code></td>"
            f"<td>{linked_tags}</td>"
            f"<td>{action_cell}</td></tr>"
        )
    if not v_rows:
        v_rows = "<tr><td colspan='4' class='tag-none'>No users yet.</td></tr>"

    sec_viewers = (
        f"<div class='card'><h3>Telegram Users</h3>"
        f"<p class='note'>Recipients receive movement alerts for the devices linked to them. "
        f"New devices default to Admin, and any recipient can be unlinked. "
        f"A device may also remain without any recipients.</p>"
        f"<div class='two'>"
        f"<div style='overflow-x:auto'>"
        f"<table><tr><th>Nickname</th><th>Chat ID</th><th>Linked devices</th><th></th></tr>"
        f"{v_rows}</table></div>"
        f"<div><form action='/add_viewer' method='POST'>"
        f"<label>Nickname</label>"
        f"<input type='text' name='nickname' placeholder='Kowsik' required maxlength='64'>"
        f"<label>Telegram Chat ID</label>"
        f"<input type='text' name='chat_id' placeholder='e.g. 123456789' required "
        f"pattern='-?[0-9]+'>"
        f"<p class='hint'>Ask the viewer to message @userinfobot to get their chat ID.</p>"
        f"<button type='submit' class='bs' style='margin-top:6px'>Add user</button>"
        f"</form></div></div></div>"
    )

    # ── Add device ────────────────────────────────────────────────────────────
    sec_add = (
        f"<div class='card'><h3>Add Tracking Device</h3>"
        f"<form action='/add_device' method='POST'>"
        f"<div class='row'>"
        f"<div><label>Nickname</label>"
        f"<input type='text' name='nickname' placeholder='Kowsik Phone' "
        f"required maxlength='64'></div>"
        f"<div><label>MAC address</label>"
        f"<input type='text' name='mac' placeholder='aa:bb:cc:dd:ee:ff' required "
        f"pattern='[0-9a-fA-F]{{2}}(:[0-9a-fA-F]{{2}}){{5}}'></div>"
        f"</div>"
        f"<label>Location</label>"
        f"<input type='text' name='location' value='Location' maxlength='64'>"
        f"<label>BLE broadcast name "
        f"<span style='color:#a0aec0;font-weight:400'>(optional)</span></label>"
        f"<input type='text' name='ble_name' "
        f"placeholder='Leave blank if not using Bluetooth'>"
        f"<p class='hint'>Find MAC via <code>arp -a</code> or your router DHCP table.</p>"
        f"<button type='submit' class='bp' style='margin-top:6px'>Add device</button>"
        f"</form></div>"
    )

    # ── Device table ──────────────────────────────────────────────────────────
    d_rows = ""
    for d_id, dev in devices.items():
        enabled      = dev.get("enabled", True)
        status_html  = "<span class='on'>Active</span>" if enabled \
                       else "<span class='off'>Disabled</span>"
        presence     = dev.get("location", "Location") if dev.get("is_home") else "Away"
        last_seen    = (
            datetime.fromtimestamp(dev["last_seen"]).strftime("%m-%d %H:%M")
            if dev.get("last_seen") else "—"
        )
        linked_vids  = dev.get("notify_viewer_ids", [])
        viewer_tags  = "".join(
            f"<span class='tag'>{_esc(viewer_ids[vid]['nickname'])}</span>"
            for vid in linked_vids if vid in viewer_ids
        ) or "<span class='tag-none'>no recipients</span>"
        location_note = f"<br><span style='font-size:.74rem;color:#718096'>Location: {_esc(dev.get('location', 'Location'))}</span>"
        ble_note = (
            f"<br><code style='font-size:.72rem;color:#a0aec0'>{_esc(dev['ble_name'])}</code>"
            if dev.get("ble_name") else ""
        )
        safe_id   = _esc(d_id)
        safe_nick = _esc(dev["nickname"])
        toggle_lbl = "Disable" if enabled else "Enable"
        d_rows += (
            f"<tr>"
            f"<td><b>{safe_nick}</b><br>"
            f"<code style='font-size:.75rem'>{_esc(dev['mac'])}</code>{ble_note}<br>{location_note}</td>"
            f"<td>{status_html}<br>"
            f"<span style='font-size:.74rem;color:#718096'>{_esc(presence)}</span></td>"
            f"<td><span style='font-size:.75rem;color:#718096'>{last_seen}</span></td>"
            f"<td>{viewer_tags}</td>"
            f"<td class='actions'>"
            f"<form action='/toggle/{safe_id}' method='POST'>"
            f"<button class='bw'>{toggle_lbl}</button></form>"
            f"<form action='/device_links/{safe_id}' method='GET'><button type='submit' class='bp'>Recipients</button></form>"
            f"<form action='/download/{safe_id}' method='GET'><button type='submit' class='bg'>Logs</button></form>"
            f"<form action='/delete_device/{safe_id}' method='POST'>"
            f"<button class='bd' onclick=\"return confirm('Delete {safe_nick}?')\">"
            f"Delete</button></form>"
            f"</td></tr>"
        )
    if not d_rows:
        d_rows = "<tr><td colspan='5' class='tag-none'>No devices yet.</td></tr>"

    sec_devices = (
        f"<div class='card'><h3>Managed Devices</h3>"
        f"<div style='overflow-x:auto'><table>"
        f"<tr><th>Device</th><th>Status</th><th>Last seen</th>"
        f"<th>Recipients</th><th>Actions</th></tr>"
        f"{d_rows}</table></div></div>"
    )

    return (
        f"<html><head>{_ADMIN_HEAD}</head><body><div class='wrap'>"
        f"{_topbar()}"
        f"{_flash_html(flash_msg, flash_ok)}"
        f"{sec_settings}{sec_viewers}{sec_add}{sec_devices}"
        f"</div></body></html>"
    )

# ── Config ────────────────────────────────────────────────────────────────────
@admin_app.route("/update_config", methods=["POST"])
@login_required
def update_config():
    try:
        cooldown = int(request.form.get("cooldown", 600))
        subnet   = request.form.get("subnet", "").strip()
    except ValueError:
        session["flash"] = "Invalid input."; session["flash_ok"] = False
        return redirect("/")
    if not is_valid_subnet(subnet):
        session["flash"] = f"Invalid subnet '{subnet}'."; session["flash_ok"] = False
        return redirect("/")
    cooldown = max(cooldown, 30)
    with db_lock:
        db["cooldown_seconds"] = cooldown
        db["subnet_prefix"]    = subnet
        _save_db_unlocked()
    session["flash"] = "Settings saved."; session["flash_ok"] = True
    return redirect("/")

# ── Viewer CRUD ───────────────────────────────────────────────────────────────
@admin_app.route("/add_viewer", methods=["POST"])
@login_required
def add_viewer():
    nickname = request.form.get("nickname", "").strip()
    raw_id   = request.form.get("chat_id", "").strip()
    if not nickname or not raw_id:
        session["flash"] = "Nickname and Chat ID required."; session["flash_ok"] = False
        return redirect("/")
    try:
        chat_id = int(raw_id)
    except ValueError:
        session["flash"] = "Chat ID must be an integer."; session["flash_ok"] = False
        return redirect("/")
    if chat_id == ALLOWED_CHAT_ID:
        session["flash"] = "Admin is already available as a built-in user."; session["flash_ok"] = False
        return redirect("/")
    vid = uuid.uuid4().hex
    with db_lock:
        db.setdefault("viewer_ids", {})[vid] = {"nickname": nickname, "chat_id": chat_id}
        _save_db_unlocked()
    session["flash"] = f"Viewer '{nickname}' added."; session["flash_ok"] = True
    return redirect("/")

@admin_app.route("/delete_viewer/<vid>", methods=["POST"])
@login_required
def delete_viewer(vid):
    if vid == ADMIN_VIEWER_ID:
        session["flash"] = "Admin user cannot be deleted."; session["flash_ok"] = False
        return redirect("/")
    with db_lock:
        viewers = db.get("viewer_ids", {})
        if vid not in viewers:
            session["flash"] = "User not found."; session["flash_ok"] = False
            return redirect("/")
        name = viewers[vid]["nickname"]
        del viewers[vid]
        for dev in db["devices"].values():
            nv = dev.get("notify_viewer_ids", [])
            if vid in nv:
                nv.remove(vid)
        _save_db_unlocked()
    session["flash"] = f"User '{name}' deleted and unlinked."; session["flash_ok"] = True
    return redirect("/")

# ── Device CRUD ───────────────────────────────────────────────────────────────
@admin_app.route("/add_device", methods=["POST"])
@login_required
def add_device():
    nickname = request.form.get("nickname", "").strip()
    location = request.form.get("location", "Location").strip() or "Location"
    mac      = request.form.get("mac", "").strip().lower()
    ble_name = request.form.get("ble_name", "").strip()
    if not nickname or not mac:
        session["flash"] = "Nickname and MAC required."; session["flash_ok"] = False
        return redirect("/")
    if not is_valid_mac(mac):
        session["flash"] = f"Invalid MAC: {mac}"; session["flash_ok"] = False
        return redirect("/")
    with db_lock:
        db["devices"][uuid.uuid4().hex] = {
            "nickname":          nickname,
            "location":          location,
            "mac":               mac,
            "ble_name":          ble_name,
            "enabled":           True,
            "is_home":           False,
            "last_seen":         0,
            "logs":              [],
            "notify_viewer_ids": [ADMIN_VIEWER_ID],
        }
        _save_db_unlocked()
    session["flash"] = f"Device '{nickname}' added."; session["flash_ok"] = True
    return redirect("/")

@admin_app.route("/toggle/<d_id>", methods=["POST"])
@login_required
def toggle_device(d_id):
    with db_lock:
        dev = db["devices"].get(d_id)
        if not dev:
            session["flash"] = "Device not found."; session["flash_ok"] = False
            return redirect("/")
        dev["enabled"] = not dev["enabled"]
        name  = dev["nickname"]
        state = "enabled" if dev["enabled"] else "disabled"
        _save_db_unlocked()
    session["flash"] = f"'{name}' {state}."; session["flash_ok"] = True
    return redirect("/")

@admin_app.route("/delete_device/<d_id>", methods=["POST"])
@login_required
def delete_device(d_id):
    with db_lock:
        dev = db["devices"].pop(d_id, None)
        if dev:
            _save_db_unlocked()
    session["flash"] = "Device deleted."; session["flash_ok"] = True
    return redirect("/")

@admin_app.route("/download/<d_id>")
@login_required
def download_logs(d_id):
    with db_lock:
        dev = db["devices"].get(d_id)
        if not dev:
            return "Device not found", 404
        text  = "\n".join(dev["logs"])
        fname = dev["nickname"].replace(" ", "_") + "_history.txt"
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )

# ── Device notification link management ──────────────────────────────────────
@admin_app.route("/device_links/<d_id>", methods=["GET", "POST"])
@login_required
def device_links(d_id):
    if request.method == "POST":
        action = request.form.get("action", "")
        vid    = request.form.get("vid", "")
        with db_lock:
            dev = db["devices"].get(d_id)
            if dev and vid in db.get("viewer_ids", {}):
                links = dev.setdefault("notify_viewer_ids", [])
                if action == "link" and vid not in links:
                    links.append(vid)
                elif action == "unlink" and vid in links:
                    links.remove(vid)
                _save_db_unlocked()
        return redirect(f"/device_links/{d_id}")

    with db_lock:
        dev = db["devices"].get(d_id)
        if not dev:
            return "Device not found", 404
        dev_name     = dev["nickname"]
        dev_mac      = dev["mac"]
        dev_ble      = dev.get("ble_name", "")
        dev_location = dev.get("location", "Location")
        current_vids = list(dev.get("notify_viewer_ids", []))
        all_viewers  = {k: dict(v) for k, v in db.get("viewer_ids", {}).items()}

    linked   = {v: all_viewers[v] for v in current_vids if v in all_viewers}
    unlinked = {v: d for v, d in all_viewers.items() if v not in current_vids}

    def _viewer_rows(vmap: dict, action: str, btn_cls: str, btn_lbl: str) -> str:
        if not vmap:
            return "<tr><td colspan='2' class='tag-none'>None.</td></tr>"
        rows = []
        for vid, vd in vmap.items():
            rows.append(
                f"<tr><td><b>{_esc(vd['nickname'])}</b><br>"
                f"<code style='font-size:.74rem'>{_esc(vd['chat_id'])}</code></td>"
                f"<td><form action='/device_links/{_esc(d_id)}' method='POST'>"
                f"<input type='hidden' name='action' value='{action}'>"
                f"<input type='hidden' name='vid' value='{_esc(vid)}'>"
                f"<button type='submit' class='{btn_cls}'>{btn_lbl}</button></form></td></tr>"
            )
        return "".join(rows)

    ble_line = f"<br>BLE: <code>{_esc(dev_ble)}</code>" if dev_ble else ""
    location_line = f"<br>Location: <code>{_esc(dev_location)}</code>"
    return (
        f"<html><head>{_ADMIN_HEAD}</head><body><div class='wrap'>"
        f"{_topbar('Recipients')}"
        f"<a class='back' href='/'>Back to admin panel</a>"
        f"<div class='card'><h3>{_esc(dev_name)}</h3>"
        f"<p style='font-size:.8rem;color:#718096;margin:0'>"
        f"MAC: <code>{_esc(dev_mac)}</code>{location_line}{ble_line}</p>"
        f"<p class='hint' style='margin-top:8px'>"
        f"Recipients receive movement alerts for this device. New devices default to Admin, and any recipient can be removed."
        f"</p></div>"
        f"<div class='two'>"
        f"<div class='card'><h3>Assigned recipients</h3>"
        f"<table>{_viewer_rows(linked, 'unlink', 'bd', 'Remove')}</table></div>"
        f"<div class='card'><h3>Available users</h3>"
        f"<table>{_viewer_rows(unlinked, 'link', 'bs', 'Add')}</table></div>"
        f"</div></div></body></html>"
    )

# ─────────────────────────────────────────────────────────────────────────────
# GRACEFUL SHUTDOWN
# ─────────────────────────────────────────────────────────────────────────────
def _handle_signal(signum, frame):
    logger.info("Signal %s — shutting down", signum)
    with db_lock:
        db["system_events"].insert(0, f"{_now_sec()} - SHUTDOWN (signal {signum})")
        _enforce_caps(db)
        _save_db_unlocked()
    os._exit(0)

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=start_tracking_thread,   daemon=True, name="tracker").start()
    threading.Thread(target=_telegram_sender_thread, daemon=True, name="notifier").start()
    threading.Thread(
        target=serve,
        kwargs={"app": viewer_app, "host": "0.0.0.0", "port": 5000,
                "threads": 2, "_quiet": not DEV_MODE},
        daemon=True, name="viewer",
    ).start()
    threading.Thread(
        target=serve,
        kwargs={"app": admin_app, "host": "0.0.0.0", "port": 5001,
                "threads": 2, "_quiet": not DEV_MODE},
        daemon=True, name="admin",
    ).start()

    logger.info("Tracker fully online — polling Telegram…")
    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as exc:
        logger.error("Telegram polling crashed: %s", exc)
        trigger_hardware_failsafe(f"Telegram polling exception: {exc}")
    finally:
        logger.info("Tracker shutting down.")
