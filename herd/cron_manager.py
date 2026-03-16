import argparse
import importlib.util
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from .env_loader import load_env
from .permissions import can_schedule_for, can_remove_task
from .storage_security import ensure_private_file, write_private_text

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TASKS_FILE = Path("cron_tasks.json")
HISTORY_FILE = Path("cron_history.json")
MEMBERS_FILE = Path("members.json")
CONFIG_FILE = Path("config.json")


def get_config_file() -> Path:
    return Path(os.getenv("CONFIG_PATH", str(CONFIG_FILE)))

def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        ensure_private_file(path)
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return default

def save_json(path: Path, data: dict) -> None:
    write_private_text(path, json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def load_tasks() -> dict: return load_json(TASKS_FILE, {"tasks": []})
def save_tasks(data: dict) -> None: save_json(TASKS_FILE, data)

def load_history() -> dict: return load_json(HISTORY_FILE, {"runs": []})

def load_members() -> dict: return load_json(MEMBERS_FILE, {"members": []})

def find_member(telegram_id: int, members: list) -> dict | None:
    return next((m for m in members if m["telegram_id"] == telegram_id), None)

def get_group_id() -> int | None:
    config = load_json(get_config_file(), {})
    group = config.get("telegram_group_id", config.get("group_id", 0))
    try:
         return int(group) if group else None
    except ValueError:
         return None


def should_fire_task(task: dict, now: datetime) -> bool:
    if not task.get("active", True):
        return False

    if not _advanced_cron_check(task, now):
        return False

    last_run = task.get("last_run")
    if not last_run:
        return True

    try:
        last_run_dt = datetime.fromisoformat(last_run)
    except ValueError:
        return True

    return (now - last_run_dt).total_seconds() >= 60


def build_trigger_text(task: dict) -> str:
    return (
        f"[CRON -> @{task['alias']}] {task['task']}\n"
        f"Scheduled by: @{task['created_by']} | ID: {task['id']}"
    )


def record_task_run(task: dict) -> None:
    data = load_tasks()
    for existing in data["tasks"]:
        if existing["id"] == task["id"]:
            existing["last_run"] = datetime.now(timezone.utc).isoformat()
            existing["run_count"] += 1
            break
    save_tasks(data)

    history = load_history()
    history["runs"].append({
        "task_id": task["id"],
        "alias": task["alias"],
        "task": task["task"],
        "fired_at": datetime.now(timezone.utc).isoformat(),
    })
    save_json(HISTORY_FILE, history)


# --- NATURAL LANGUAGE PARSER ---

NL_PATTERNS = [
    # "every monday 08:00"
    (r"every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2}):(\d{2})",
     lambda m: nl_weekly(m)),
    # "every day 09:00"
    (r"every\s+day\s+(\d{1,2}):(\d{2})",
     lambda m: f"{m.group(2)} {m.group(1)} * * *"),
    # "every hour"
    (r"every hour",
     lambda m: "0 * * * *"),
    # "in X minutes"
    (r"in (\d+) minutes?",
     lambda m: nl_in_minutes(int(m.group(1)))),
    # "now"
    (r"^now$",
     lambda m: nl_in_minutes(0)),
]

WEEKDAY_MAP = {
    "monday": "1", "tuesday": "2", "wednesday": "3",
    "thursday": "4", "friday": "5", "saturday": "6", "sunday": "0",
}

def nl_weekly(m) -> str:
    day = WEEKDAY_MAP[m.group(1).lower()]
    return f"{m.group(3)} {m.group(2)} * * {day}"

def nl_in_minutes(minutes: int) -> str:
    target = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return f"{target.minute} {target.hour} {target.day} {target.month} *"


def is_valid_cron(expr: str) -> bool:
    parts = expr.strip().split()
    return len(parts) == 5

def parse_schedule(text: str) -> str | None:
    text = text.strip().lower()
    for pattern, converter in NL_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return converter(m)
    if is_valid_cron(text):
        return text
    return None

def _advanced_cron_check(task: dict, check_time: datetime) -> bool:
    """Returns True if the current time matches the cron expr minute/hour."""
    parts = task["cron"].strip().split()
    if len(parts) != 5:
        return False
        
    minute, hour, day, month, dow = parts
    
    if minute != "*" and int(minute) != check_time.minute: return False
    if hour != "*" and int(hour) != check_time.hour: return False
    if day != "*" and int(day) != check_time.day: return False
    if month != "*" and int(month) != check_time.month: return False
    
    if dow != "*":
        # Python weekday: Mon=0, Sun=6
        # Cron weekday: Sun=0, Sat=6
        cron_dow = (check_time.weekday() + 1) % 7
        if int(dow) != cron_dow:
             return False
             
    return True

# --- FIRE TASK ---

async def fire_task(bot, group_id: int, task: dict) -> None:
    trigger_text = build_trigger_text(task)

    try:
        await bot.send_message(chat_id=group_id, text=trigger_text)
    except Exception as e:
        logger.error(f"Failed to fire task {task['id']}: {e}")
        return

    record_task_run(task)

def fire_task_sync(token: str, group_id: int, task: dict) -> None:
    trigger_text = build_trigger_text(task)
    payload = urllib.parse.urlencode({
        "chat_id": str(group_id),
        "text": trigger_text,
    }).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        logger.error(f"Failed to fire task {task['id']}: HTTP {e.code} - {detail}")
        return
    except Exception as e:
        logger.error(f"Failed to fire task {task['id']}: {e}")
        return

    if not body.get("ok"):
        logger.error(f"Failed to fire task {task['id']}: Telegram API returned {body}")
        return

    record_task_run(task)


def start_scheduler(token: str) -> threading.Thread:
    def loop():
        logger.warning(
            "PTB JobQueue unavailable; using the built-in fallback scheduler thread. "
            "Install python-telegram-bot[job-queue] for the native scheduler."
        )

        while True:
            try:
                group_id = get_group_id()
                if group_id:
                    now = datetime.now(timezone.utc)
                    data = load_tasks()

                    for task in data["tasks"]:
                        if should_fire_task(task, now):
                            fire_task_sync(token, group_id, task)
            except Exception:
                logger.exception("Fallback scheduler loop failed.")

            now = datetime.now(timezone.utc)
            sleep_time = 60 - now.second - (now.microsecond / 1_000_000)
            if sleep_time <= 0:
                sleep_time = 60
            time.sleep(sleep_time)

    thread = threading.Thread(target=loop, name="herd-cron-fallback", daemon=True)
    thread.start()
    return thread


# --- REDESIGNED TICK FOR PTB JOB QUEUE ---

async def cron_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    group_id = get_group_id()
    if not group_id:
        return
        
    now = datetime.now(timezone.utc)
    data = load_tasks()
    
    for task in data["tasks"]:
        if should_fire_task(task, now):
            await fire_task(context.bot, group_id, task)


# --- COMMAND HANDLERS ---

async def handle_cron(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
        
    text = msg.text.strip()
    if not text.lower().startswith("/cron"):
        return
        
    group_id = msg.chat.id
    config = load_json(CONFIG_FILE, {})
    if config.get("telegram_group_id") != group_id:
        config["telegram_group_id"] = group_id
        save_json(CONFIG_FILE, config)
        
    sender_id = msg.from_user.id
    members = load_members()["members"]
    actor = find_member(sender_id, members)
    
    if actor is None:
        await msg.reply_text("❌ You are not registered in Herd.")
        return
    
    if re.match(r"^/cron\s+list$", text, re.IGNORECASE):
        await cmd_list(update, actor)
        return
        
    m = re.match(r"^/cron\s+remove\s+(\S+)$", text, re.IGNORECASE)
    if m:
        await cmd_remove(update, actor, m.group(1))
        return
        
    m = re.match(r'^/cron\s+add\s+@?(\w+)\s+"([^"]+)"\s+(.+)$', text, re.IGNORECASE)
    if m:
        alias = m.group(1)
        task_desc = m.group(2)
        schedule_str = m.group(3)
        await cmd_add(update, actor, alias, task_desc, schedule_str)
        return
        
    await msg.reply_text(
        "Usage:\n"
        "/cron add @alias \"task description\" every monday 08:00\n"
        "/cron list\n"
        "/cron remove <task_id>"
    )


async def cmd_add(update: Update, actor: dict, alias: str, task_desc: str, schedule_str: str) -> None:
    if not can_schedule_for(actor["cargo"], alias, actor["alias"]):
        await update.message.reply_text(f"❌ Role {actor['cargo']} cannot schedule tasks for @{alias}.")
        return

    cron_expr = parse_schedule(schedule_str)
    if cron_expr is None:
        await update.message.reply_text(f"❌ Invalid cron expression or natural language schedule: '{schedule_str}'")
        return

    task = {
        "id": f"task_{secrets.token_hex(3)}",
        "alias": alias,
        "task": task_desc,
        "cron": cron_expr,
        "raw_schedule": schedule_str,
        "created_by": actor["alias"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
        "last_run": None,
        "run_count": 0,
    }

    data = load_tasks()
    data["tasks"].append(task)
    save_tasks(data)
    
    await update.message.reply_text(f"✓ Task Scheduled (ID: {task['id']})\nTarget: @{alias}\nSchedule: {cron_expr}\nTask: {task_desc}")


async def cmd_list(update: Update, actor: dict) -> None:
    data = load_tasks()
    active = [t for t in data["tasks"] if t.get("active", True)]
    
    if not active:
        await update.message.reply_text("No active tasks.")
        return
        
    lines = ["🕒 Active Scheduled Tasks:"]
    for t in active:
        lines.append(f"- ID: {t['id']} | @{t['alias']} | {t['raw_schedule']} | `{t['task']}`")
        
    await update.message.reply_text("\n".join(lines))


async def cmd_remove(update: Update, actor: dict, task_id: str) -> None:
    data = load_tasks()
    
    task_idx = next((i for i, t in enumerate(data["tasks"]) if t["id"] == task_id), None)
    if task_idx is None:
        await update.message.reply_text(f"❌ Task {task_id} not found.")
        return
        
    task = data["tasks"][task_idx]
    
    if not can_remove_task(actor, task):
        await update.message.reply_text(f"❌ You do not have permission to remove task {task_id}.")
        return
        
    del data["tasks"][task_idx]
    save_tasks(data)
    
    await update.message.reply_text(f"✓ Task {task_id} removed.")


def main():
    load_env()
    p = argparse.ArgumentParser(description="Herd CRON Manager")
    p.add_argument("--token", type=str, help="Telegram Bot Token", default=os.getenv("TELEGRAM_BOT_TOKEN"))
    args = p.parse_args()
    
    if not args.token:
        logger.error("Please provide a --token or set TELEGRAM_BOT_TOKEN env variable.")
        return
        
    logger.info("Starting Herd CRON Manager...")
    
    app = Application.builder().token(args.token).build()
    
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, handle_cron))
    
    if importlib.util.find_spec("apscheduler") is not None and app.job_queue is not None:
        app.job_queue.run_repeating(cron_tick, interval=60)
        logger.info("CRON scheduler started with PTB JobQueue.")
    else:
        start_scheduler(args.token)
    
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
