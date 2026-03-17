import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from . import bridge
from .env_loader import apply_config_env_overrides, load_env
from .permissions import ROLE_RANK, can_generate_token, can_modify_cargo
from .storage_security import ensure_private_file, write_private_text

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- FILE PATHS ---
MEMBERS_FILE = Path("members.json")
TOKENS_FILE = Path("invite_tokens.json")
CONFIG_FILE = Path("config.json")


# --- DATA LOADERS / SAVERS ---

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

def load_members() -> dict: return load_json(MEMBERS_FILE, {"members": []})
def save_members(data: dict) -> None: save_json(MEMBERS_FILE, data)

def load_tokens() -> dict: return load_json(TOKENS_FILE, {"tokens": []})
def save_tokens(data: dict) -> None: save_json(TOKENS_FILE, data)


def get_config_file() -> Path:
    return Path(os.getenv("CONFIG_PATH", str(CONFIG_FILE)))


def load_runtime_config() -> dict:
    return apply_config_env_overrides(load_json(get_config_file(), {}))


def save_runtime_config(config: dict) -> None:
    bridge.save_runtime_config(config, get_config_file(), sync_env=True)


def update_member_scope(telegram_id: int, new_scope: str, alias: str | None = None) -> bool:
    data = load_members()
    updated = False

    for member in data["members"]:
        if member.get("telegram_id") != telegram_id:
            continue
        if alias and member.get("alias") != alias:
            continue
        member["scope"] = new_scope
        updated = True

    if updated:
        save_members(data)

    return updated


# --- TOKEN LOGIC ---

VALIDITY_MAP = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "never": None,
}

def generate_invite_token(
    created_by: str,
    role: str,
    scope: str,
    validity: str = "7d",
    max_uses: int = 1,
    suggested_alias: str = "",
) -> dict:
    token_str = f"herd-{secrets.token_hex(3)}"
    
    expires_at = None
    delta = VALIDITY_MAP.get(validity)
    if delta:
        expires_at = (datetime.now(timezone.utc) + delta).isoformat()
        
    record = {
        "token": token_str,
        "created_by": created_by,
        "cargo": role.upper(),
        "scope": scope,
        "alias_sugerido": suggested_alias,
        "expires_at": expires_at,
        "max_uses": max_uses if max_uses != -1 else -1,
        "used": 0,
        "used_by": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    
    data = load_tokens()
    data["tokens"].append(record)
    save_tokens(data)
    
    return record


def validate_token(token_str: str, tokens: list) -> tuple[bool, dict | None, str]:
    record = next((t for t in tokens if t["token"] == token_str), None)

    if record is None:
        return False, None, "Token not found."

    if record["used"] >= record["max_uses"] and record["max_uses"] != -1:
        return False, None, "Token exhausted — all uses spent."

    if record.get("expires_at"):
        try:
            expiry = datetime.fromisoformat(record["expires_at"])
            if datetime.now(timezone.utc) > expiry:
                return False, None, "Token expired."
        except Exception:
            pass

    return True, record, ""


def consume_token(token_str: str, telegram_id: int, alias: str, tokens: list) -> list:
    for t in tokens:
        if t["token"] == token_str:
            t["used"] += 1
            t.setdefault("used_by", []).append({
                "telegram_id": telegram_id,
                "alias": alias,
                "at": datetime.now(timezone.utc).isoformat(),
            })
    return tokens


# --- ONBOARDING STATE MACHINE ---

class OnboardingStep:
    WAITING_TOKEN = "waiting_token"
    WAITING_AGENT = "waiting_agent"
    WAITING_ALIAS = "waiting_alias"
    DONE = "done"

@dataclass
class OnboardingSession:
    telegram_id: int
    username: str
    step: str = OnboardingStep.WAITING_TOKEN
    token_record: dict = field(default_factory=dict)
    agent: str = ""
    alias: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_expired(self, timeout_minutes: int = 10) -> bool:
        delta = datetime.now(timezone.utc) - self.started_at
        return delta.total_seconds() > timeout_minutes * 60

# In-memory session store
ONBOARDING_SESSIONS: dict[int, OnboardingSession] = {}

def clean_expired_sessions():
    expired = [uid for uid, s in ONBOARDING_SESSIONS.items() if s.is_expired()]
    for uid in expired:
        del ONBOARDING_SESSIONS[uid]


def is_valid_alias(alias: str, existing_aliases: list[str]) -> tuple[bool, str]:
    alias = alias.strip().lstrip("@")
    if not re.match(r'^[a-z0-9_]{3,32}$', alias):
        return False, "Alias must be 3-32 characters: lowercase letters, numbers and underscores."
    if alias in existing_aliases:
        return False, f"Alias '{alias}' is already in use. Choose another."
    return True, ""


# --- MEMBERS REGISTRATION ---

def register_member(session: OnboardingSession, token_record: dict) -> dict:
    member = {
        "telegram_id": session.telegram_id,
        "telegram_username": session.username,
        "alias": session.alias,
        "agent": session.agent,
        "scope": token_record["scope"],
        "cargo": token_record["cargo"],
        "token_delegation": False,
        "invited_by": token_record["created_by"],
        "invite_token": token_record["token"],
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "online": True,
    }

    data = load_members()
    data["members"].append(member)
    save_members(data)
    return member

def find_members(telegram_id: int, members: list) -> list[dict]:
    return [m for m in members if m.get("telegram_id") == telegram_id]


def resolve_runtime_member(config: dict, members: list) -> dict | None:
    alias = str(config.get("alias", "") or "").lstrip("@")
    if alias:
        match = next((member for member in members if member.get("alias") == alias), None)
        if match is not None:
            return match

    telegram_id = config.get("telegram_id")
    if telegram_id:
        linked = find_members(telegram_id, members)
        if len(linked) == 1:
            return linked[0]

    return None


def _preferred_member_for_command(linked_members: list[dict]) -> dict | None:
    if not linked_members:
        return None

    top_rank = max(ROLE_RANK.get(member.get("cargo", ""), 0) for member in linked_members)
    top_members = [member for member in linked_members if ROLE_RANK.get(member.get("cargo", ""), 0) == top_rank]
    if len(top_members) == 1:
        return top_members[0]

    owner_like = [member for member in top_members if member.get("cargo") == "OWNER"]
    if owner_like:
        top_members = owner_like

    delegated = [member for member in top_members if member.get("token_delegation")]
    if delegated:
        top_members = delegated

    return sorted(top_members, key=lambda member: str(member.get("alias", "")))[0]


def should_process_on_instance(
    command_word: str,
    *,
    config: dict,
    sender_id: int,
    members: list,
) -> bool:
    routed_commands = {
        "/init": "OWNER",
        "/invite": "LEAD",
        "/members": "LEAD",
        "/role": "LEAD",
        "/cargo": "LEAD",
        "/delegate": "OWNER",
    }

    required_role = routed_commands.get(command_word)
    if required_role is None:
        return True

    linked_members = find_members(sender_id, members)
    if not linked_members:
        runtime_role = str(config.get("cargo", "") or "")
        return command_word == "/init" and runtime_role == "OWNER"

    preferred = _preferred_member_for_command(linked_members)
    runtime_member = resolve_runtime_member(config, members)
    runtime_role = str((runtime_member or preferred or {}).get("cargo") or config.get("cargo", "") or "")
    if ROLE_RANK.get(runtime_role, 0) < ROLE_RANK.get(required_role, 0):
        return False

    runtime_alias = str((runtime_member or {}).get("alias") or "").lstrip("@")
    preferred_alias = str((preferred or {}).get("alias") or "").lstrip("@")

    if preferred_alias and runtime_alias and runtime_alias != preferred_alias:
        return False

    return True


def resolve_actor(telegram_id: int, members: list) -> dict | None:
    linked_members = find_members(telegram_id, members)
    if not linked_members:
        return None

    actor = max(
        linked_members,
        key=lambda member: (
            ROLE_RANK.get(member.get("cargo", ""), 0),
            1 if member.get("token_delegation") else 0,
        ),
    ).copy()
    actor["aliases"] = sorted({member.get("alias", "") for member in linked_members if member.get("alias")})
    actor["assignments"] = len(linked_members)

    highest_rank = ROLE_RANK.get(actor.get("cargo", ""), 0)
    highest_rank_members = [
        member for member in linked_members if ROLE_RANK.get(member.get("cargo", ""), 0) == highest_rank
    ]
    if actor.get("cargo") == "LEAD":
        actor["token_delegation"] = any(member.get("token_delegation") for member in highest_rank_members)

    return actor


def find_member(telegram_id: int, members: list) -> dict | None:
    return resolve_actor(telegram_id, members)


async def bootstrap_owner(update: Update, context: ContextTypes.DEFAULT_TYPE, member):
    user = member.user
    
    # Check if OWNER already exists just in case
    data = load_members()
    if any(m["cargo"] == "OWNER" for m in data["members"]):
        return
        
    owner_record = {
        "telegram_id": user.id,
        "telegram_username": user.username or "",
        "alias": "owner",
        "agent": "unknown",
        "scope": "/",
        "cargo": "OWNER",
        "token_delegation": True,
        "invited_by": "system",
        "invite_token": "bootstrap",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "online": True,
    }
    
    data["members"].append(owner_record)
    save_members(data)
    
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "🐄 Welcome to Herd! You are the OWNER of this group.\n\n"
                "To invite members, send in the group:\n"
                "/invite @name role=DEV scope=/src/folder validity=7d\n\n"
                "To see all members:\n"
                "/members"
            )
        )
    except Exception as e:
        logger.warning(f"Could not DM the owner (they might need to start the bot first): {e}")


async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, member):
    user = member.user
    
    # Make sure we're not starting onboarding for the bot itself
    if user.is_bot:
        return
        
    ONBOARDING_SESSIONS[user.id] = OnboardingSession(
        telegram_id=user.id,
        username=user.username or "",
    )
    
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "🐄 Hello! To join the Herd in this group, you need an invite token.\n\n"
                "Please send your token here:"
            )
        )
    except Exception as e:
        logger.warning(f"Could not DM new member {user.id} to start onboarding: {e}")
        # The user has to message the bot first. We can prompt them in the group.
        try:
            await update.message.reply_text(f"@{user.username or user.first_name}, please send me a private message to start the onboarding process.")
        except Exception:
            pass


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            continue
            
        # Get fake chat member object to match structure
        class FakeChatMember: pass
        mock_member = FakeChatMember()
        mock_member.user = new_member
        
        members = load_members()
        
        if not members["members"]:
            await bootstrap_owner(update, context, mock_member)
        else:
            await start_onboarding(update, context, mock_member)


async def handle_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.chat.type != "private":
        return
        
    user_id = msg.from_user.id
    text = msg.text.strip()
    
    clean_expired_sessions()
    
    session = ONBOARDING_SESSIONS.get(user_id)
    if not session:
        # If user sends a token-like string out of nowhere, let's assume they want to onboard
        if text.startswith("herd-"):
            session = OnboardingSession(telegram_id=user_id, username=msg.from_user.username or "")
            ONBOARDING_SESSIONS[user_id] = session
        else:
            await msg.reply_text("Send your invite token (`herd-...`) to start onboarding.")
            return

    # STEP 1: TOKEN
    if session.step == OnboardingStep.WAITING_TOKEN:
        tokens_data = load_tokens()
        is_valid, record, error = validate_token(text, tokens_data["tokens"])
        
        if not is_valid:
            await msg.reply_text(f"❌ Invalid token: {error}\n\nPlease send a valid token:")
            return
            
        session.token_record = record
        session.step = OnboardingStep.WAITING_AGENT
        
        await msg.reply_text(f"✓ Valid Token.\n\nWhat coding agent do you use?\n[1] Claude Code\n[2] Codex\n[3] Other (type name)")
        return
        
    # STEP 2: AGENT
    if session.step == OnboardingStep.WAITING_AGENT:
        if text == "1": session.agent = "claude-code"
        elif text == "2": session.agent = "codex"
        else: session.agent = text
        
        session.step = OnboardingStep.WAITING_ALIAS
        
        sug_msg = ""
        if session.token_record.get('alias_sugerido'):
            sug_msg = f" (suggested: {session.token_record['alias_sugerido']})"
            
        await msg.reply_text(f"What will be the alias of your agent in the group?{sug_msg}\nEx: agent_back")
        return
        
    # STEP 3: ALIAS
    if session.step == OnboardingStep.WAITING_ALIAS:
        members_data = load_members()
        existing_aliases = [m["alias"] for m in members_data["members"]]
        
        is_valid, error = is_valid_alias(text, existing_aliases)
        if not is_valid:
            await msg.reply_text(f"❌ {error}\n\nPlease choose another alias:")
            return
            
        session.alias = text.lstrip("@")
        
        # Finalize
        tokens_data = load_tokens()
        tokens_data["tokens"] = consume_token(session.token_record["token"], user_id, session.alias, tokens_data["tokens"])
        save_tokens(tokens_data)
        
        register_member(session, session.token_record)
        
        del ONBOARDING_SESSIONS[user_id]
        
        await msg.reply_text(
            f"✓ Configuration Complete!\n"
            f"Role: {session.token_record['cargo']}\n"
            f"Scope: {session.token_record['scope']}\n"
            f"Agent: @{session.alias} ({session.agent})\n\n"
            f"Welcome to the Herd group!"
        )


# --- GROUP COMMANDS ---

async def handle_invite(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: dict, members: list, text: str):
    if not can_generate_token(actor):
        await update.message.reply_text("❌ You don't have permission to generate invite tokens.")
        return
        
    pattern = r"/invite\s+@?(\w+)\s+role=(\w+)\s+scope=(\S+)(?:\s+validity=(\S+))?(?:\s+uses=(\d+))?"
    # support 'cargo=' as alias to 'role=' for backwards compatibility just in case
    pattern_cargo = r"/invite\s+@?(\w+)\s+cargo=(\w+)\s+scope=(\S+)(?:\s+validity=(\S+))?(?:\s+uses=(\d+))?"
    
    m = re.match(pattern, text.strip(), re.IGNORECASE) or re.match(pattern_cargo, text.strip(), re.IGNORECASE)
    
    if not m:
        await update.message.reply_text("Usage: /invite @alias role=DEV scope=/src/backend [validity=7d] [uses=1]")
        return
        
    suggested_alias = m.group(1)
    role = m.group(2).upper()
    scope = m.group(3)
    validity = m.group(4) or "7d"
    max_uses = int(m.group(5)) if m.group(5) else 1
    
    if role not in ROLE_RANK:
        await update.message.reply_text(f"❌ Invalid Role. Options: {', '.join(ROLE_RANK.keys())}")
        return
        
    try:
        requested_scope = bridge.normalize_scope_path(scope)
        bridge.validate_scope_boundary(requested_scope, bridge.get_allowed_scope(load_runtime_config()))
        record = generate_invite_token(
            created_by=actor["alias"],
            role=role,
            scope=str(requested_scope),
            validity=validity,
            max_uses=max_uses,
            suggested_alias=suggested_alias
        )

        await update.message.reply_text(
            f"Token generated for {suggested_alias}:\n\n"
            f"`{record['token']}`\n\n"
            f"Role: {record['cargo']}\n"
            f"Scope: {record['scope']}\n"
            f"Validity: {validity}\n"
            f"Uses: {max_uses}\n\n"
            f"Send this token to the invited developer so they can present it via DM."
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
    except Exception as e:
        await update.message.reply_text(f"Error generating token: {e}")


async def handle_members(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: dict, members: list, text: str):
    if actor["cargo"] not in ("OWNER", "LEAD"):
        await update.message.reply_text("❌ Permission denied.")
        return
        
    lines = ["👥 Herd Members:"]
    for m in members:
        line = f"- @{m['alias']} ({m['cargo']}) | {m['telegram_username'] or m['telegram_id']}"
        lines.append(line)
        
    await update.message.reply_text("\n".join(lines))


async def handle_agents(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: dict, members: list, text: str):
    aliases = sorted({str(member.get("alias", "")).lstrip("@") for member in members if member.get("alias")})
    if not aliases:
        await update.message.reply_text("No agents are registered in this Herd group yet.")
        return

    lines = [
        "🤖 Agent aliases:",
        *[f"`@{alias}`" for alias in aliases],
        "",
        "Tip: copy one alias above and mention it in the group message.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_cargo(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: dict, members: list, text: str):
    if actor["cargo"] not in ("OWNER", "LEAD"):
        await update.message.reply_text("❌ Permission denied.")
        return
        
    m = re.match(r"/role\s+@?(\w+)\s+(\w+)", text.strip(), re.IGNORECASE) or re.match(r"/cargo\s+@?(\w+)\s+(\w+)", text.strip(), re.IGNORECASE)
    if not m:
        await update.message.reply_text("Usage: /role @alias NEWPORT")
        return
        
    target_alias = m.group(1)
    new_role = m.group(2).upper()
    
    if new_role not in ROLE_RANK:
        await update.message.reply_text(f"❌ Invalid role. Options: {', '.join(ROLE_RANK.keys())}")
        return
        
    data = load_members()
    target_idx = next((i for i, m in enumerate(data["members"]) if m["alias"] == target_alias), None)
    
    if target_idx is None:
        await update.message.reply_text(f"❌ Member @{target_alias} not found.")
        return
        
    target_member = data["members"][target_idx]
    
    if not can_modify_cargo(actor["cargo"], target_member["cargo"]):
        await update.message.reply_text(f"❌ You don't have permission to modify {target_alias}'s role ({target_member['cargo']}).")
        return
        
    old_role = target_member["cargo"]
    data["members"][target_idx]["cargo"] = new_role
    save_members(data)
    
    await update.message.reply_text(f"✓ Role for @{target_alias} changed from {old_role} to {new_role}.")


async def handle_delegate(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: dict, members: list, text: str):
    if actor["cargo"] != "OWNER":
        await update.message.reply_text("❌ Only OWNER can delegate token generation.")
        return
        
    m = re.match(r"/delegate\s+@?(\w+)\s+(true|false)", text.strip(), re.IGNORECASE)
    if not m:
        await update.message.reply_text("Usage: /delegate @alias true|false")
        return
        
    target_alias = m.group(1)
    is_delegated = m.group(2).lower() == "true"
    
    data = load_members()
    target_idx = next((i for i, m in enumerate(data["members"]) if m["alias"] == target_alias), None)
    
    if target_idx is None:
        await update.message.reply_text(f"❌ Member @{target_alias} not found.")
        return
        
    target_member = data["members"][target_idx]
    if target_member["cargo"] != "LEAD":
        await update.message.reply_text("❌ Token generation can only be delegated to LEADs.")
        return
        
    data["members"][target_idx]["token_delegation"] = is_delegated
    save_members(data)
    
    status = "granted" if is_delegated else "revoked"
    await update.message.reply_text(f"✓ Token generation permission {status} for @{target_alias}.")


async def handle_init(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: dict | None, members: list, text: str):
    msg = update.message
    if not msg:
        return

    config = load_runtime_config()
    if config.get("cargo") != "OWNER":
        logger.info("[herd] Ignoring /init on non-OWNER instance to avoid duplicate bootstrap replies.")
        return

    if members:
        owner = next((member for member in members if member["cargo"] == "OWNER"), None)
        if owner:
            await msg.reply_text(f"✓ Herd is already initialized. Current OWNER: @{owner['alias']}.")
        else:
            await msg.reply_text("✓ Herd already has registered members.")
        return

    user = msg.from_user
    if not user:
        await msg.reply_text("❌ Could not determine which Telegram user sent `/init`.")
        return

    owner_record = {
        "telegram_id": user.id,
        "telegram_username": user.username or "",
        "alias": config.get("alias", "owner"),
        "agent": (config.get("agent") or {}).get("type", "unknown"),
        "scope": config.get("scope", "/"),
        "cargo": "OWNER",
        "token_delegation": True,
        "invited_by": "system",
        "invite_token": "bootstrap",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "online": True,
    }

    save_members({"members": [owner_record]})

    config["telegram_id"] = user.id
    config["telegram_username"] = user.username or ""
    config["telegram_group_id"] = msg.chat.id
    save_runtime_config(config)

    await msg.reply_text(
        f"✓ Herd initialized.\n"
        f"OWNER: @{owner_record['alias']}\n"
        f"Group: {msg.chat.id}\n\n"
        f"You can now use /invite and /members."
    )


def _is_runtime_owner(actor: dict | None, config: dict) -> bool:
    if not actor:
        return False

    config_telegram_id = config.get("telegram_id")
    if config_telegram_id and actor.get("telegram_id") == config_telegram_id:
        return True

    config_alias = str(config.get("alias", "") or "").lstrip("@")
    if config_alias and actor.get("alias") == config_alias:
        return True

    return False


async def handle_scope(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: dict, members: list, text: str):
    msg = update.message
    if not msg:
        return

    config = load_runtime_config()
    if not config:
        await msg.reply_text("❌ No runtime configuration found. Finish setup first.")
        return

    if not _is_runtime_owner(actor, config):
        await msg.reply_text("❌ Only the Telegram account linked to this Herd instance can change its workspace.")
        return

    requested_scope = text.partition(" ")[2].strip()
    if not requested_scope:
        current_scope = config.get("scope") or "not configured"
        await msg.reply_text(
            f"Current workspace for @{config.get('alias', 'agent')}:\n"
            f"`{current_scope}`\n\n"
            f"Use `/scope /absolute/or/relative/path` to switch projects."
        )
        return

    bridge_state = None
    if context and hasattr(context, "bot_data"):
        bridge_state = context.bot_data.get("bridge_state")

    try:
        if bridge_state:
            updated = bridge.update_runtime_scope(requested_scope, bridge_state, sync_env=True)
            live_update = True
        else:
            scope_path = bridge.normalize_scope_path(requested_scope)
            previous_scope = config.get("scope", "")
            config["scope"] = str(scope_path)
            save_runtime_config(config)
            bridge.bootstrap_herd_dir(config["scope"], config)
            updated = {
                "scope": config["scope"],
                "herd_path": str(scope_path / ".herd"),
                "config_path": str(get_config_file()),
                "env_path": str(bridge.resolve_env_path()),
                "sync_env": True,
                "previous_scope": previous_scope,
            }
            live_update = False
    except ValueError as e:
        await msg.reply_text(f"❌ {e}")
        return
    except Exception as e:
        logger.error(f"Failed to update scope via Telegram command: {e}")
        await msg.reply_text(f"❌ Failed to update workspace: {e}")
        return

    previous_scope = config.get("scope", "")
    if bridge_state and isinstance(bridge_state.get("config_ref"), dict):
        previous_scope = bridge_state["config_ref"].get("scope", previous_scope)

    update_member_scope(
        actor["telegram_id"],
        updated["scope"],
        alias=str(config.get("alias") or actor.get("alias") or "").lstrip("@") or None,
    )

    if live_update:
        await msg.reply_text(
            f"✓ Workspace updated for @{config.get('alias', actor['alias'])}.\n"
            f"New directory: `{updated['scope']}`\n"
            f"Live update: active now\n"
            f"CLI sync: config + .env"
        )
    else:
        await msg.reply_text(
            f"✓ Workspace saved for @{config.get('alias', actor['alias'])}.\n"
            f"New directory: `{updated['scope']}`\n"
            f"Restart required: yes\n"
            f"Run the bridge again so the new workspace is used live."
        )


COMMANDS = {
    "/init":     handle_init,
    "/invite":   handle_invite,
    "/agents":   handle_agents,
    "/aliases":  handle_agents,
    "/members":  handle_members,
    "/role":     handle_cargo,
    "/cargo":    handle_cargo, # Fallback
    "/delegate": handle_delegate,
    "/scope":    handle_scope,
    "/workspace": handle_scope,
}

async def dispatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
        
    # Group chat only for commands
    if msg.chat.type == "private":
        return
        
    text = msg.text.strip()
    if not text.startswith("/"):
        return
        
    # Special CRON bypass - let cron_manager handle it
    if text.startswith("/cron"):
        return
        
    data = load_members()
    members = data["members"]
    sender_id = msg.from_user.id
    config = load_runtime_config()

    command_word = text.split(" ")[0].lower()
    if not should_process_on_instance(command_word, config=config, sender_id=sender_id, members=members):
        return

    actor = resolve_actor(sender_id, members)
    
    if actor is None:
        if text.lower().startswith("/init"):
            pass
        # Check if they are owner bootstrapping
        elif text.startswith("/invite") and not members:
            # Very edge case, normally bootstrap happens on join
            pass
        else:
            await msg.reply_text("You are not registered in Herd for this group. Get an invite token from the OWNER or LEAD.")
            return

    handler = COMMANDS.get(command_word)
    
    if handler:
        await handler(update, context, actor, members, text)


# --- MAIN SETUP ---

def main():
    import argparse
    load_env()
    
    p = argparse.ArgumentParser(description="Herd Gatekeeper")
    p.add_argument("--token", type=str, help="Telegram Bot Token", default=os.getenv("TELEGRAM_BOT_TOKEN"))
    args = p.parse_args()
    
    if not args.token:
        logger.error("Please provide a --token or set TELEGRAM_BOT_TOKEN env variable.")
        return
        
    logger.info("Starting Herd Gatekeeper...")
    
    app = Application.builder().token(args.token).build()
    
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_dm))
    
    # Simple message handler instead of command handler to pass full raw text
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, dispatch_command))
    
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
