import asyncio
import html
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from .env_loader import (
    apply_config_env_overrides,
    env_values_from_config,
    load_env,
    sanitize_persisted_config,
    resolve_env_path,
    write_env_file,
)
from .setup_diagnostics import build_setup_diagnostics, normalize_alias
from .storage_security import ensure_private_dir, ensure_private_file, write_private_text

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
UI_PORT = 7474
UI_DIR = PACKAGE_ROOT / "ui"
REQUIRED_CONFIG_FIELDS = ("telegram_bot_token", "telegram_group_id", "alias", "scope", "agent")
TELEGRAM_MESSAGE_LIMIT = 3600
TYPING_ACTION_INTERVAL_SECONDS = 4.0
PROCESSING_NOTICE_DELAY_SECONDS = 1.5
FEEDBACK_POLL_INTERVAL_SECONDS = 0.05
REPORT_PREVIEW_LIMIT = 900
DEFAULT_AGENT_TIMEOUT_SECONDS = 120
AGENT_TIMEOUT_DEFAULTS = {
    "gemini": 300,
}
RUNTIME_GITIGNORE_PATTERNS = (
    ".env*",
    "!.env.example",
    "config*.json",
    "!config.example.json",
    "members.json",
    "invite_tokens.json",
    "cron_tasks.json",
    "cron_history.json",
)
RUNTIME_GITIGNORE_HEADER = "# Herd — sensitive runtime files"

AGENT_RESPONSE_POLICY = """You are replying inside a Telegram group through Herd.

Rules for this reply:
- Output only the final user-facing answer.
- Respond in the same language as the user's latest message.
- Never include analysis, planning, chain-of-thought, or tool narration.
- Never say what you are about to do.
- If the user asked for an exact reply, output exactly that reply and nothing else.
- Keep the answer concise and Telegram-friendly.
"""

REPORT_ONLY_POLICY = """You are working through Herd in report-only mode.

Rules for this response:
- Treat the project as read-only and never claim files were changed.
- Output only Markdown content that can be saved directly as a report.
- Respond in the same language as the user's latest message.
- Include short sections for Summary, Affected Areas, Proposed Changes, and Risks or Questions when relevant.
- If the request is unclear or blocked, explain what is missing in the report itself.
"""

REPORT_SNAPSHOT_IGNORE_NAMES = {
    ".git",
    ".herd",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}

LEADING_META_PATTERNS = (
    re.compile(r"^\s*\[@?[a-z0-9_]+\]\s*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:i['’]?ll|i will|let me)\s+(?:start|first|begin|check|read|inspect|look|verify|examine|review|respond|now)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:first|next|now)\s*,?\s*i(?:['’]?ll| will)\s+(?:start|check|read|inspect|look|verify|examine|review|respond)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:vou|eu vou|primeiro vou|agora vou|deixe-?me)\s+(?:começar|verificar|checar|ler|inspecionar|examinar|olhar|revisar|responder)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:the user|o usuário|o utilizador)\s+(?:wants|asked|is asking|quer|pediu)\b", re.IGNORECASE),
)

# --- CONFIG LOADING ---

def load_config(path: Path) -> dict:
    config = {}
    if path.exists():
        ensure_private_file(path)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"[herd] config.json is corrupted: {e}")
            sys.exit(1)
    return apply_config_env_overrides(config)


def is_config_complete(config: dict) -> bool:
    if not config:
        return False
    for field in REQUIRED_CONFIG_FIELDS:
        if not config.get(field):
            return False
    agent = config.get("agent") or {}
    return bool(agent.get("type"))


def save_runtime_config(config: dict, config_path: Path, sync_env: bool = True) -> dict:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    persisted_config = sanitize_persisted_config(config)
    write_private_text(
        config_path,
        json.dumps(persisted_config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    env_path = resolve_env_path()
    values = env_values_from_config(config, config_path=config_path, minimal=not sync_env)
    write_env_file(values, env_path=env_path)
    sync_runtime_gitignore(config_path=config_path, env_path=env_path)

    return {
        "config_path": str(config_path),
        "env_path": str(env_path),
        "sync_env": sync_env,
    }


def get_current_config_state(config_path: Path) -> dict:
    env_path = resolve_env_path()
    config = load_config(config_path)
    sources = []

    if config_path.exists():
        sources.append("config.json")
    if env_path.exists():
        sources.append(".env")

    return {
        "config": build_public_config(config),
        "has_bot_token": bool(config.get("telegram_bot_token")),
        "config_exists": config_path.exists(),
        "env_exists": env_path.exists(),
        "config_complete": is_config_complete(config),
        "config_path": str(config_path),
        "env_path": str(env_path),
        "sources": sources,
        "diagnostics": build_setup_diagnostics(
            config,
            project_root=Path.cwd(),
            config_path=config_path,
            env_path=env_path,
        ),
    }


def _relative_runtime_gitignore_entry(path: Path, project_root: Path) -> str | None:
    try:
        relative_path = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    return relative_path.as_posix()


def sync_runtime_gitignore(
    *,
    config_path: Path,
    env_path: Path,
    project_root: Path | None = None,
) -> Path:
    root = Path(project_root or Path.cwd())
    gitignore = root / ".gitignore"
    entries = list(RUNTIME_GITIGNORE_PATTERNS)

    for candidate in (config_path, env_path):
        entry = _relative_runtime_gitignore_entry(candidate, root)
        if entry and entry not in entries:
            entries.append(entry)

    existing_lines: list[str] = []
    existing_text = ""
    if gitignore.exists():
        existing_text = gitignore.read_text(encoding="utf-8")
        existing_lines = existing_text.splitlines()

    existing_entries = {line.strip() for line in existing_lines if line.strip()}
    missing_entries = [entry for entry in entries if entry not in existing_entries]
    header_missing = RUNTIME_GITIGNORE_HEADER not in existing_entries

    if not missing_entries and not header_missing:
        return gitignore

    updated_text = existing_text
    if updated_text and not updated_text.endswith("\n"):
        updated_text += "\n"
    if updated_text and not updated_text.endswith("\n\n"):
        updated_text += "\n"

    block: list[str] = []
    if header_missing:
        block.append(RUNTIME_GITIGNORE_HEADER)
    block.extend(missing_entries)
    updated_text += "\n".join(block) + "\n"
    gitignore.write_text(updated_text, encoding="utf-8")
    return gitignore


def resolve_ui_landing_path(bridge_state: dict) -> str:
    status = bridge_state.get("status") or {}
    if status.get("setup_complete"):
        return "/dashboard"

    config_ref = bridge_state.get("config_ref")
    if isinstance(config_ref, dict) and is_config_complete(config_ref):
        return "/dashboard"

    config_path = Path(bridge_state.get("config_path", Path("config.json")))
    try:
        if is_config_complete(load_config(config_path)):
            return "/dashboard"
    except Exception:
        pass

    return "/setup"


def build_public_config(config: dict) -> dict:
    public = sanitize_persisted_config(config)
    if public.get("invite_token"):
        public["invite_token"] = "[stored privately]"
    return public


def allow_project_changes(config: dict) -> bool:
    return bool(config.get("allow_project_changes", True))


def _status_access_mode(config: dict) -> str:
    return "write-enabled" if allow_project_changes(config) else "report-only"


def get_agent_timeout_seconds(config: dict) -> int:
    raw_value = config.get("agent_timeout_seconds")
    if raw_value not in (None, ""):
        try:
            parsed = int(raw_value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass

    agent_type = str((config.get("agent") or {}).get("type", "") or "").strip().lower()
    return AGENT_TIMEOUT_DEFAULTS.get(agent_type, DEFAULT_AGENT_TIMEOUT_SECONDS)


def _format_timeout_label(timeout_seconds: int) -> str:
    if timeout_seconds % 60 == 0:
        minutes = timeout_seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{timeout_seconds} seconds"


def is_path_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def get_allowed_scope(config: dict) -> Path | None:
    allowed_scope = str(config.get("allowed_scope") or config.get("scope") or "").strip()
    if not allowed_scope:
        return None
    return normalize_scope_path(allowed_scope)


def validate_scope_boundary(scope_path: Path, allowed_scope: Path | None) -> None:
    if allowed_scope is None:
        return
    if not is_path_within(allowed_scope, scope_path):
        raise ValueError(f"Directory must stay inside the allowed scope: {allowed_scope}")


def normalize_scope_path(scope: str) -> Path:
    scope_text = str(scope or "").strip()
    if not scope_text:
        raise ValueError("Missing field: scope")

    path = Path(scope_text).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()

    if not path.exists():
        raise ValueError(f"Directory does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    return path


def update_runtime_scope(scope: str, bridge_state: dict, sync_env: bool = True) -> dict:
    config_path = Path(bridge_state.get("config_path", Path("config.json")))
    scope_path = normalize_scope_path(scope)
    config = load_config(config_path)

    if not config:
        raise ValueError("No runtime configuration found. Complete setup first.")
    if not is_config_complete(config):
        raise ValueError("Configuration is incomplete. Finish setup before changing the workspace.")

    if config.get("cargo") == "OWNER":
        config["allowed_scope"] = str(scope_path)
    else:
        validate_scope_boundary(scope_path, get_allowed_scope(config))

    config["scope"] = str(scope_path)
    saved = save_runtime_config(config, config_path, sync_env=sync_env)
    bootstrap_herd_dir(config["scope"], config)

    herd_path = scope_path / ".herd"
    bridge_state["herd_path"] = str(herd_path)
    bridge_state.setdefault("status", {})
    bridge_state["status"]["scope"] = config["scope"]
    bridge_state["status"]["alias"] = config.get("alias")
    bridge_state["status"]["allow_project_changes"] = allow_project_changes(config)
    bridge_state["status"]["access_mode"] = _status_access_mode(config)

    config_ref = bridge_state.get("config_ref")
    if isinstance(config_ref, dict):
        config_ref.clear()
        config_ref.update(config)
    else:
        bridge_state["config_ref"] = config

    bot_data_ref = bridge_state.get("bot_data_ref")
    if isinstance(bot_data_ref, dict):
        bot_data_ref["config"] = bridge_state["config_ref"]
        bot_data_ref["herd_path"] = herd_path

    return {
        "scope": config["scope"],
        "herd_path": str(herd_path),
        "config_path": saved["config_path"],
        "env_path": saved["env_path"],
        "sync_env": saved["sync_env"],
    }


def update_runtime_access_mode(
    enabled: bool,
    bridge_state: dict,
    sync_env: bool = True,
) -> dict:
    config_path = Path(bridge_state.get("config_path", Path("config.json")))
    config = load_config(config_path)

    if not config:
        raise ValueError("No runtime configuration found. Complete setup first.")
    if not is_config_complete(config):
        raise ValueError("Configuration is incomplete. Finish setup before changing access mode.")

    config["allow_project_changes"] = bool(enabled)
    saved = save_runtime_config(config, config_path, sync_env=sync_env)
    bootstrap_herd_dir(config["scope"], config)

    bridge_state.setdefault("status", {})
    bridge_state["status"]["allow_project_changes"] = allow_project_changes(config)
    bridge_state["status"]["access_mode"] = _status_access_mode(config)
    bridge_state["status"]["alias"] = config.get("alias")
    bridge_state["status"]["scope"] = config.get("scope")

    config_ref = bridge_state.get("config_ref")
    if isinstance(config_ref, dict):
        config_ref.clear()
        config_ref.update(config)
    else:
        bridge_state["config_ref"] = config

    bot_data_ref = bridge_state.get("bot_data_ref")
    if isinstance(bot_data_ref, dict):
        bot_data_ref["config"] = bridge_state["config_ref"]

    return {
        "allow_project_changes": allow_project_changes(config),
        "access_mode": _status_access_mode(config),
        "config_path": saved["config_path"],
        "env_path": saved["env_path"],
        "sync_env": saved["sync_env"],
    }


# --- .herd/ BOOTSTRAP ---

def bootstrap_herd_dir(scope: str, config: dict) -> None:
    herd = Path(scope) / ".herd"
    ensure_private_dir(herd)

    for subdir in [
        "memory/conversations",
        "tasks",
        "outputs/reports",
        "outputs/drafts",
    ]:
        ensure_private_dir(herd / subdir)

    agent_file = herd / "agent.json"
    existing_agent = {}
    if agent_file.exists():
        ensure_private_file(agent_file)
        try:
            existing_agent = json.loads(agent_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_agent = {}

    existing_agent.update({
        "alias": config["alias"],
        "agent": (config.get("agent") or {}).get("type", "unknown"),
        "scope": config["scope"],
        "allowed_scope": config.get("allowed_scope", config["scope"]),
        "cargo": config.get("cargo", "DEV"),
        "allow_project_changes": allow_project_changes(config),
        "access_mode": _status_access_mode(config),
        "group_id": config["telegram_group_id"],
        "registered_at": existing_agent.get("registered_at") or datetime.now(timezone.utc).isoformat(),
        "last_active": existing_agent.get("last_active"),
        "session_count": existing_agent.get("session_count", 0),
    })
    write_private_text(agent_file, json.dumps(existing_agent, indent=2, ensure_ascii=False), encoding="utf-8")

    for fname, default in [
        ("tasks/pending.json", []),
        ("tasks/completed.json", []),
    ]:
        f = herd / fname
        if not f.exists():
            write_private_text(f, json.dumps(default, indent=2), encoding="utf-8")
        else:
            ensure_private_file(f)

    gitignore = Path(scope) / ".gitignore"
    entry = "\n# Herd — agent workspace\n.herd/\n"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".herd/" not in content:
            gitignore.write_text(content + entry, encoding="utf-8")
    else:
        gitignore.write_text(entry.lstrip(), encoding="utf-8")


def persist_message(herd_path: Path, sender: str, text: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log = herd_path / "memory" / "conversations" / f"{today}.jsonl"
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "from": sender,
        "text": text,
    }, ensure_ascii=False)
    with log.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")
    ensure_private_file(log)


def upsert_member_registration(config: dict) -> None:
    telegram_id = config.get("telegram_id")
    alias = str(config.get("alias", "") or "").strip()
    if not isinstance(telegram_id, int) or telegram_id <= 0 or not alias:
        return

    members_file = Path("members.json")
    members_data = {"members": []}
    if members_file.exists():
        ensure_private_file(members_file)
        try:
            members_data = json.loads(members_file.read_text(encoding="utf-8"))
        except Exception:
            members_data = {"members": []}

    members = list(members_data.get("members", []))
    member_record = {
        "telegram_id": telegram_id,
        "telegram_username": str(config.get("telegram_username", "") or ""),
        "alias": alias,
        "agent": (config.get("agent") or {}).get("type", "unknown"),
        "scope": config.get("scope", ""),
        "cargo": config.get("cargo", "DEV"),
        "token_delegation": bool(config.get("cargo") == "OWNER"),
        "invited_by": "system" if config.get("cargo") == "OWNER" else "ui_setup",
        "invite_token": config.get("invite_token", ""),
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "online": True,
    }

    existing_idx = next((idx for idx, member in enumerate(members) if member.get("alias") == alias), None)
    if existing_idx is not None:
        preserved = dict(members[existing_idx])
        preserved.update({key: value for key, value in member_record.items() if value not in (None, "")})
        if "token_delegation" in members[existing_idx]:
            preserved["token_delegation"] = members[existing_idx]["token_delegation"]
        if members[existing_idx].get("registered_at"):
            preserved["registered_at"] = members[existing_idx]["registered_at"]
        members[existing_idx] = preserved
    else:
        members.append(member_record)

    write_private_text(members_file, json.dumps({"members": members}, indent=2, ensure_ascii=False), encoding="utf-8")


# --- SUBPROCESS (AGENT) EXECUTION ---

def build_context(herd_path: Path, tail: int = 20) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log = herd_path / "memory" / "conversations" / f"{today}.jsonl"

    if not log.exists():
        return ""

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-tail:] if len(lines) > tail else lines

    entries = []
    for line in recent:
        try:
            e = json.loads(line)
            entries.append(f"[{e['from']}]: {e['text']}")
        except Exception:
            continue

    if not entries:
        return ""
    return "Recent context from the group channel:\n" + "\n".join(entries)


def build_agent_prompt(message_text: str, context: str = "") -> str:
    sections = [AGENT_RESPONSE_POLICY.strip()]
    if context:
        sections.append(context)
    sections.append("Latest user message in the Herd group:")
    sections.append(message_text)
    return "\n\n".join(section for section in sections if section)


def build_report_prompt(message_text: str, context: str = "") -> str:
    sections = [REPORT_ONLY_POLICY.strip()]
    if context:
        sections.append(context)
    sections.append("Latest user message in the Herd group:")
    sections.append(message_text)
    return "\n\n".join(section for section in sections if section)


def _is_internal_meta_line(line: str) -> bool:
    return any(pattern.match(line) for pattern in LEADING_META_PATTERNS)


def finalize_agent_response(response: str) -> str:
    text = str(response or "").replace("\r\n", "\n").strip()
    if not text:
        return "[Herd] Empty response from agent."

    lines = text.split("\n")
    idx = 0
    removed_any = False

    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped:
            removed_any = True
            idx += 1
            continue
        if _is_internal_meta_line(stripped):
            removed_any = True
            idx += 1
            continue
        break

    if removed_any and idx < len(lines):
        candidate = "\n".join(lines[idx:]).strip()
        if candidate:
            text = candidate

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or "[Herd] Empty response from agent."


def _truncate_for_telegram(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    suffix = "\n\n[...response truncated]"
    return normalized[: max(0, limit - len(suffix))].rstrip() + suffix


def render_telegram_html(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").strip()
    normalized = re.sub(r"(?m)^[ \t]*[-*]\s+", "• ", normalized)

    code_blocks: list[tuple[str, str]] = []

    def stash_code_block(match: re.Match[str]) -> str:
        token = f"@@HERD_CODE_BLOCK_{len(code_blocks)}@@"
        content = match.group(1).strip("\n")
        code_blocks.append((token, f"<pre>{html.escape(content)}</pre>"))
        return token

    normalized = re.sub(r"```(?:[^\n`]*\n)?(.*?)```", stash_code_block, normalized, flags=re.DOTALL)
    escaped = html.escape(normalized)
    escaped = re.sub(r"(?m)^#{1,6}\s+(.+)$", lambda m: f"<b>{m.group(1).strip()}</b>", escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", escaped)

    for token, rendered in code_blocks:
        escaped = escaped.replace(token, rendered)

    return escaped


def execute_agent_prompt(
    config: dict,
    prompt: str,
    herd_path: Path,
    *,
    cwd_override: str | Path | None = None,
) -> str:
    agent_cfg = dict(config["agent"])
    if agent_cfg.get("timeout_seconds") in (None, "") and config.get("agent_timeout_seconds") not in (None, ""):
        agent_cfg["timeout_seconds"] = config.get("agent_timeout_seconds")
    mode = agent_cfg.get("mode", "cli")

    if mode == "file-watcher":
        return _call_agent_filewatcher(agent_cfg, prompt, herd_path)
    cwd = str(cwd_override or config["scope"])
    return _call_agent_cli(agent_cfg, prompt, cwd)


def call_agent(config: dict, message_text: str, herd_path: Path, context_lines: int = 20) -> str:
    context = build_context(herd_path, tail=context_lines)
    prompt = build_agent_prompt(message_text, context)
    return execute_agent_prompt(config, prompt, herd_path)


def _snapshot_copy_ignore(_src: str, names: list[str]) -> set[str]:
    return {name for name in names if name in REPORT_SNAPSHOT_IGNORE_NAMES}


def _prepare_report_snapshot(scope: Path) -> tuple[Path, Path]:
    temp_root = Path(tempfile.mkdtemp(prefix="herd-report-"))
    snapshot_root = temp_root / "workspace"
    shutil.copytree(scope, snapshot_root, ignore=_snapshot_copy_ignore)
    return temp_root, snapshot_root


def call_agent_report_only(config: dict, message_text: str, herd_path: Path, context_lines: int = 20) -> str:
    context = build_context(herd_path, tail=context_lines)
    prompt = build_report_prompt(message_text, context)
    agent_cfg = config["agent"]

    if agent_cfg.get("mode", "cli") != "cli":
        return "\n".join([
            "## Summary",
            "",
            "This agent is configured in file-watcher mode, so Herd cannot safely enforce report-only execution for it.",
            "",
            "## Risks or Questions",
            "",
            "- Switch this instance to a CLI agent if you need enforced report-only analysis.",
            "- The original request is preserved below for manual follow-up.",
            "",
            "## Request",
            "",
            message_text.strip(),
        ])

    try:
        temp_root, snapshot_root = _prepare_report_snapshot(Path(config["scope"]))
    except Exception as e:
        return "\n".join([
            "## Summary",
            "",
            "Herd could not prepare a disposable snapshot for report-only execution.",
            "",
            "## Risks or Questions",
            "",
            f"- Snapshot error: {e}",
        ])

    try:
        return execute_agent_prompt(config, prompt, herd_path, cwd_override=snapshot_root)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def write_report_markdown(
    herd_path: Path,
    alias: str,
    message_text: str,
    report_body: str,
) -> Path:
    reports_dir = herd_path / "outputs" / "reports"
    ensure_private_dir(reports_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"{timestamp}-{alias}.md"
    markdown = "\n".join([
        "# Herd Report",
        "",
        f"- Agent: @{alias}",
        f"- Generated At: {datetime.now(timezone.utc).isoformat()}",
        "- Mode: report-only",
        "",
        "## Request",
        "",
        message_text.strip(),
        "",
        "## Analysis",
        "",
        report_body.strip() or "_No report content generated._",
        "",
    ])
    write_private_text(report_path, markdown, encoding="utf-8")
    return report_path


def build_report_telegram_response(report_path: Path, report_body: str) -> str:
    preview = str(report_body or "").strip()
    if len(preview) > REPORT_PREVIEW_LIMIT:
        preview = preview[:REPORT_PREVIEW_LIMIT].rstrip() + "\n\n[...report preview truncated]"

    lines = [
        "Project changes are disabled for this agent.",
        f"Markdown report saved to `{report_path}`.",
    ]
    if preview:
        lines.extend(["", "Preview:", preview])
    return "\n".join(lines)


def _resolve_cli_prompt_mode(agent_cfg: dict) -> str:
    prompt_mode = str(agent_cfg.get("prompt_mode", "") or "").strip().lower()
    if prompt_mode in {"stdin", "argv"}:
        return prompt_mode

    args = agent_cfg.get("args") or []
    if any(arg in {"-p", "--prompt"} for arg in args):
        return "argv"

    return "stdin"


def _call_agent_cli(agent_cfg: dict, prompt: str, cwd: str) -> str:
    cmd = [agent_cfg["command"]] + agent_cfg.get("args", [])
    prompt_mode = _resolve_cli_prompt_mode(agent_cfg)
    timeout_seconds = get_agent_timeout_seconds({"agent": agent_cfg, "agent_timeout_seconds": agent_cfg.get("timeout_seconds")})
    run_kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout_seconds,
        "cwd": cwd,
        "input": prompt if prompt_mode == "stdin" else None,
    }

    if prompt_mode == "argv":
        cmd = cmd + [prompt]
        run_kwargs["stdin"] = subprocess.DEVNULL

    try:
        result = subprocess.run(cmd, **run_kwargs)
        if result.returncode != 0:
            error_output = (result.stderr or result.stdout or "Unknown CLI failure.").strip()
            return f"[Agent Error] {error_output[:300]}"
        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        return f"[Herd] Agent took more than {_format_timeout_label(timeout_seconds)}. Please try again."
    except FileNotFoundError:
        return f"[Herd] Agent '{agent_cfg['command']}' not found. Check your installation."


def _call_agent_filewatcher(agent_cfg: dict, prompt: str, herd_path: Path) -> str:
    """
    File-watcher protocol for VSCode-based agents (Cursor, Antigravity and etc).
    
    1. Writes a task to .herd/tasks/pending.json
    2. Polls .herd/tasks/responses/<task_id>.txt for a response
    3. Falls back to an acknowledgement after timeout
    """
    import secrets

    task_id = f"msg_{secrets.token_hex(4)}"
    pending_file = herd_path / "tasks" / "pending.json"
    responses_dir = herd_path / "tasks" / "responses"
    ensure_private_dir(responses_dir)

    # Read existing pending tasks
    pending = []
    if pending_file.exists():
        try:
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
        except Exception:
            pending = []

    # Append new task
    task = {
        "id": task_id,
        "prompt": prompt,
        "agent_type": agent_cfg.get("type", "vscode"),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    pending.append(task)
    write_private_text(pending_file, json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")

    timeout = get_agent_timeout_seconds({"agent": agent_cfg, "agent_timeout_seconds": agent_cfg.get("timeout_seconds")})
    response_file = responses_dir / f"{task_id}.txt"
    elapsed = 0
    poll_interval = 2

    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        if response_file.exists():
                response = response_file.read_text(encoding="utf-8").strip()
                # Clean up
                try:
                    response_file.unlink()
                    pending = [t for t in json.loads(pending_file.read_text(encoding="utf-8")) if t["id"] != task_id]
                    write_private_text(pending_file, json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                return response

    return (
        f"[Herd] Task queued for your {agent_cfg.get('type', 'VSCode')} agent (ID: {task_id}).\n"
        f"Check .herd/tasks/pending.json in your editor."
    )


async def post_to_group(
    bot,
    group_id: int,
    alias: str,
    response: str,
    *,
    message_thread_id: int | None = None,
) -> None:
    body = _truncate_for_telegram(finalize_agent_response(response))
    full = f"<b>@{html.escape(alias)}</b>\n{render_telegram_html(body)}"
    send_kwargs = {
        "chat_id": group_id,
        "text": full,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if message_thread_id is not None:
        send_kwargs["message_thread_id"] = message_thread_id

    await bot.send_message(**send_kwargs)


# --- TELEGRAM POLLING ---

async def _safe_send_typing_action(bot, chat_id: int, message_thread_id: int | None = None) -> None:
    send_chat_action = getattr(bot, "send_chat_action", None)
    if send_chat_action is None:
        return

    try:
        kwargs = {"chat_id": chat_id, "action": "typing"}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        await send_chat_action(**kwargs)
    except Exception as e:
        logger.debug(f"[herd] Failed to send typing action: {e}")


async def _safe_send_processing_notice(
    bot,
    chat_id: int,
    *,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
):
    send_message = getattr(bot, "send_message", None)
    if send_message is None:
        return None

    try:
        kwargs = {
            "chat_id": chat_id,
            "text": "⏳",
            "disable_notification": True,
        }
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        return await send_message(**kwargs)
    except Exception as e:
        logger.debug(f"[herd] Failed to send processing notice: {e}")
        return None


async def _safe_delete_message(bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return

    delete_message = getattr(bot, "delete_message", None)
    if delete_message is None:
        return

    try:
        await delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.debug(f"[herd] Failed to delete processing notice: {e}")


async def call_agent_with_feedback(
    bot,
    chat_id: int,
    config: dict,
    message_text: str,
    herd_path: Path,
    *,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
    runner=None,
) -> str:
    result: dict[str, object] = {}
    done = threading.Event()

    def run_agent() -> None:
        try:
            if runner is not None:
                result["value"] = runner()
            else:
                result["value"] = call_agent(config, message_text, herd_path)
        except Exception as e:
            result["error"] = e
        finally:
            done.set()

    worker = threading.Thread(target=run_agent, name="herd-agent-call", daemon=True)
    await _safe_send_typing_action(bot, chat_id, message_thread_id=message_thread_id)
    worker.start()

    notice_message = None
    started_at = time.monotonic()
    next_typing_at = time.monotonic() + TYPING_ACTION_INTERVAL_SECONDS
    while not done.is_set():
        now = time.monotonic()
        if now >= next_typing_at:
            await _safe_send_typing_action(bot, chat_id, message_thread_id=message_thread_id)
            next_typing_at = now + TYPING_ACTION_INTERVAL_SECONDS
        if notice_message is None and (now - started_at) >= PROCESSING_NOTICE_DELAY_SECONDS:
            notice_message = await _safe_send_processing_notice(
                bot,
                chat_id,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
            )
        await asyncio.sleep(FEEDBACK_POLL_INTERVAL_SECONDS)

    await _safe_delete_message(bot, chat_id, getattr(notice_message, "message_id", None))

    if "error" in result:
        raise result["error"]

    return str(result.get("value", ""))

def should_process(text: str, alias: str, bot_username: str | None = None) -> bool:
    if not text:
        return False
    lowered = text.lower()

    triggers = [f"@{alias}".lower(), "#herd"]
    return any(trigger in lowered for trigger in triggers)


def load_member_registry() -> dict[int, list[dict]]:
    members_file = Path("members.json")
    if not members_file.exists():
        return {}

    ensure_private_file(members_file)
    try:
        data = json.loads(members_file.read_text(encoding="utf-8"))
    except Exception:
        return {}

    registry: dict[int, list[dict]] = {}
    for member in data.get("members", []):
        telegram_id = member.get("telegram_id")
        if isinstance(telegram_id, int):
            registry.setdefault(telegram_id, []).append(member)
    return registry


async def handle_message(update: Update, context):
    config = context.bot_data["config"]
    herd_path = context.bot_data["herd_path"]
    alias = config["alias"]

    msg = update.message
    if not msg or not msg.text:
        return

    if msg.chat.type not in {"group", "supergroup"}:
        return
    if msg.chat.id != config["telegram_group_id"]:
        logger.warning(f"[herd] Ignoring message from unexpected chat {msg.chat.id}.")
        return
    if not msg.from_user or msg.from_user.is_bot:
        return

    text = msg.text
    if not should_process(text, alias):
        return

    sender_entries = load_member_registry().get(msg.from_user.id, [])
    if not sender_entries:
        logger.warning(f"[herd] Ignoring message from unregistered sender {msg.from_user.id}.")
        return

    sender_aliases = sorted({entry.get("alias", "") for entry in sender_entries if entry.get("alias")})
    if len(sender_aliases) == 1:
        sender = sender_aliases[0]
    else:
        sender = msg.from_user.username or str(msg.from_user.id)
    logger.info(f"[herd] Processing group message from {sender}: {text[:120]}")
    persist_message(herd_path, sender, text)

    message_thread_id = getattr(msg, "message_thread_id", None)
    reply_to_message_id = getattr(msg, "message_id", None)
    if allow_project_changes(config):
        raw_response = await call_agent_with_feedback(
            context.bot,
            config["telegram_group_id"],
            config,
            text,
            herd_path,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
        )
        response = finalize_agent_response(raw_response)
    else:
        raw_report = await call_agent_with_feedback(
            context.bot,
            config["telegram_group_id"],
            config,
            text,
            herd_path,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            runner=lambda: call_agent_report_only(config, text, herd_path),
        )
        report_body = finalize_agent_response(raw_report)
        report_path = write_report_markdown(herd_path, alias, text, report_body)
        response = build_report_telegram_response(report_path, report_body)

    persist_message(herd_path, f"@{alias}", response)

    await post_to_group(
        context.bot,
        config["telegram_group_id"],
        alias,
        response,
        message_thread_id=message_thread_id,
    )


# --- UI WEB SERVER ---

class HerdUIHandler(BaseHTTPRequestHandler):
    SESSION_COOKIE_NAME = "herd_ui_session"

    def _expected_ui_token(self) -> str:
        return getattr(self.server, "ui_auth_token", "")

    def _read_session_cookie(self) -> str:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""

        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return ""

        session = cookie.get(self.SESSION_COOKIE_NAME)
        return session.value if session else ""

    def _is_authorized(self, url_parsed) -> bool:
        expected = self._expected_ui_token()
        if not expected:
            return True

        query = urllib.parse.parse_qs(url_parsed.query)
        presented = query.get("auth", [""])[0] or self._read_session_cookie()
        return bool(presented) and secrets.compare_digest(presented, expected)

    def _maybe_establish_session(self, url_parsed) -> bool:
        expected = self._expected_ui_token()
        if not expected:
            return False

        query = urllib.parse.parse_qs(url_parsed.query)
        auth = query.get("auth", [""])[0]
        if not auth or not secrets.compare_digest(auth, expected):
            return False

        if secrets.compare_digest(self._read_session_cookie() or "", expected):
            return False

        clean_items = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(url_parsed.query, keep_blank_values=True)
            if key != "auth"
        ]
        redirect_path = url_parsed.path or "/"
        if clean_items:
            redirect_path = f"{redirect_path}?{urllib.parse.urlencode(clean_items)}"

        self.send_response(302)
        self.send_header("Location", redirect_path)
        self.send_header(
            "Set-Cookie",
            f"{self.SESSION_COOKIE_NAME}={expected}; HttpOnly; SameSite=Strict; Path=/",
        )
        self.end_headers()
        return True

    def _reject_unauthorized(self, is_api: bool) -> None:
        if is_api:
            self._json_response({"success": False, "error": "Unauthorized UI session."}, status=403)
            return

        body = (
            "<!DOCTYPE html><html><body style='font-family: sans-serif; max-width: 640px; margin: 48px auto; line-height: 1.5;'>"
            "<h1>403</h1>"
            "<p>Unauthorized UI session.</p>"
            "<p>Open the authenticated UI URL printed in the terminal, or restart with the UI auto-open flow.</p>"
            "<p>Plain <code>/dashboard</code> access is blocked on purpose.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url_parsed = urllib.parse.urlparse(self.path)
        if url_parsed.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self._maybe_establish_session(url_parsed):
            return
        if not self._is_authorized(url_parsed):
            self._reject_unauthorized(url_parsed.path.startswith("/api/"))
            return

        path = url_parsed.path

        if path == "/":
            landing_path = resolve_ui_landing_path(self.server.bridge_state)
            if landing_path != "/":
                self.send_response(302)
                self.send_header("Location", landing_path)
                self.end_headers()
                return
            self._serve_file(UI_DIR / "index.html", "text/html")
        elif path == "/setup":
            self._serve_file(UI_DIR / "index.html", "text/html")
        elif path == "/dashboard":
            self._serve_file(UI_DIR / "dashboard.html", "text/html")
        elif path in ("/dashboard/messages", "/dashboard/tasks", "/dashboard/members"):
            self._serve_file(UI_DIR / "dashboard_detail.html", "text/html")
        elif path == "/herd.css":
            self._serve_file(UI_DIR / "herd.css", "text/css")
        elif path == "/logo.png":
            self._serve_file(UI_DIR / "logo.png", "image/png")
        elif path == "/api/status":
            self._json_response(self._get_status())
        elif path.startswith("/api/await-group-pin"):
            self._handle_await_group_pin()
        elif path == "/api/check-first-run":
            self._json_response(self._check_first_run())
        elif path == "/api/current-config":
            self._json_response(self._get_current_config())
        elif path == "/api/members":
            self._json_response(self._get_members())
        elif path.startswith("/api/messages"):
            self._json_response(self._get_messages())
        elif path == "/api/cron":
            self._json_response(self._get_cron())
        else:
            self.send_error(404)

    def _handle_await_group_pin(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token = query.get("token", [""])[0]
        pin = query.get("pin", [""])[0]
        
        if not token or not pin:
            self._json_response({"success": False, "error": "Missing token or pin"})
            return

        import time
        
        start_time = time.time()
        offset = None
        
        try:
            # First, check if token is valid at all
            check_url = f"https://api.telegram.org/bot{token}/getMe"
            try:
                with urllib.request.urlopen(check_url, timeout=5) as resp:
                    json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                self._json_response({"success": False, "error": f"Invalid Bot Token: {e}"})
                return

            while time.time() - start_time < 50: # Slightly less than 60s
                url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=5&allowed_updates=%5B%22message%22%5D"
                if offset:
                    url += f"&offset={offset}"
                req = urllib.request.Request(url)
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        
                        if data.get("ok"):
                            results = data.get("result", [])
                            for item in results:
                                offset = item["update_id"] + 1
                                msg = item.get("message", {})
                                text = msg.get("text", "")
                                if text.strip() == f"/init {pin}":
                                    group_id = msg.get("chat", {}).get("id")
                                    sender = msg.get("from", {})
                                    self._json_response({
                                        "success": True,
                                        "group_id": group_id,
                                        "telegram_id": sender.get("id", 0),
                                        "telegram_username": sender.get("username", ""),
                                    })
                                    self.server.bridge_state["last_pairing"] = {
                                        "token": token,
                                        "group_id": group_id,
                                        "telegram_id": sender.get("id", 0),
                                        "telegram_username": sender.get("username", ""),
                                    }
                                    return
                        else:
                            self._json_response({"success": False, "error": "Telegram API error: " + data.get("description", "")})
                            return
                except urllib.error.HTTPError as e:
                    if e.code == 409:
                        self._json_response({"success": False, "error": "Conflict: Another instance of this bot is already running. Close it first."})
                        return
                    if e.code in [401, 404]: # Invalid token
                        self._json_response({"success": False, "error": f"Invalid bot token (HTTP {e.code})"})
                        return
                    time.sleep(1)
                except urllib.error.URLError as e:
                    time.sleep(1)
                    
            self._json_response({"success": False, "error": "Timeout waiting for PIN. Make sure the bot is in the group and you sent /init " + pin})
        except Exception as e:
            self._json_response({"success": False, "error": str(e)})

    def do_POST(self):
        url_parsed = urllib.parse.urlparse(self.path)
        if not self._is_authorized(url_parsed):
            self._reject_unauthorized(is_api=True)
            return

        path = url_parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/validate-token":
            self._json_response(self._validate_token(body.get("token", "")))
        elif path == "/api/setup":
            result = self._run_setup(body)
            self._json_response(result)
        elif path == "/api/update-scope":
            result = self._update_scope(body)
            status = 200 if result.get("success") else 400
            self._json_response(result, status=status)
        elif path == "/api/update-access-mode":
            result = self._update_access_mode(body)
            status = 200 if result.get("success") else 400
            self._json_response(result, status=status)
        else:
            self.send_error(404)

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_token(self, token_str: str) -> dict:
        tokens_file = Path("invite_tokens.json")
        if not tokens_file.exists():
            return {"valid": False, "error": "Gatekeeper missing. No tokens found."}
        
        data = json.loads(tokens_file.read_text(encoding="utf-8"))
        for t in data.get("tokens", []):
            if t["token"] != token_str:
                continue
            if t["used"] >= t["max_uses"] and t["max_uses"] != -1:
                return {"valid": False, "error": "Token exhausted."}
            if t.get("expires_at"):
                exp = datetime.fromisoformat(t["expires_at"])
                if datetime.now(timezone.utc) > exp:
                    return {"valid": False, "error": "Token expired."}
            return {
                "valid": True,
                "role": t["cargo"],
                "scope": t["scope"],
                "suggested_alias": t.get("alias_sugerido", ""),
            }
        return {"valid": False, "error": "Token not found."}

    # Agent type → command mapping
    AGENT_COMMANDS = {
        "claude-code": {"command": "claude", "args": ["--print"], "mode": "cli"},
        "gemini":      {"command": "gemini", "args": ["-p"], "mode": "cli", "prompt_mode": "argv"},
        "codex":       {"command": "codex", "args": ["--no-alt-screen"], "mode": "cli"},
        "cursor":      {"command": None,  "args": [], "mode": "file-watcher"},
        "antigravity": {"command": None,  "args": [], "mode": "file-watcher"},
        "vscode-generic": {"command": None, "args": [], "mode": "file-watcher"},
    }

    def _build_agent_config(self, body: dict) -> dict:
        agent_type = body.get("agent", "claude-code")
        preset = self.AGENT_COMMANDS.get(agent_type, {})

        if preset:
            return {
                "type": agent_type,
                "command": preset["command"],
                "args": list(preset["args"]),
                "mode": preset["mode"],
                "prompt_mode": preset.get("prompt_mode"),
            }

        # Fallback for custom/other
        custom_command = str(body.get("command", "") or "").strip()
        try:
            custom_parts = shlex.split(custom_command) if custom_command else []
        except ValueError as e:
            raise ValueError(f"Invalid custom command: {e}") from e

        return {
            "type": agent_type,
            "command": custom_parts[0] if custom_parts else "echo",
            "args": custom_parts[1:],
            "mode": "cli",
            "prompt_mode": body.get("prompt_mode", "stdin"),
        }

    def _check_first_run(self) -> dict:
        """Detects if this is the very first run (no tokens, no members)."""
        tokens_file = Path("invite_tokens.json")
        members_file = Path("members.json")
        has_tokens = tokens_file.exists() and json.loads(tokens_file.read_text(encoding="utf-8")).get("tokens", [])
        has_members = members_file.exists() and json.loads(members_file.read_text(encoding="utf-8")).get("members", [])
        return {"first_run": not has_tokens and not has_members}

    def _run_setup(self, body: dict) -> dict:
        config_path = self.server.bridge_state.get("config_path", Path("config.json"))
        is_bootstrap = body.get("bootstrap", False)
        sync_env = body.get("sync_env", True)
        existing_config = load_config(config_path)
        requested_flow = str(body.get("flow", "") or "").strip().lower()
        if is_bootstrap or requested_flow == "bootstrap":
            flow = "bootstrap"
        elif requested_flow == "reconfigure":
            flow = "reconfigure"
        else:
            flow = "join"

        use_stored_token = bool(body.get("use_stored_token"))
        token_value = str(body.get("telegram_bot_token", "") or "").strip()
        if not token_value and use_stored_token:
            token_value = str(existing_config.get("telegram_bot_token", "") or "").strip()
        last_pairing = self.server.bridge_state.get("last_pairing", {})

        # Required fields for all setups
        required = ["agent", "alias", "scope", "telegram_group_id"]
        if flow == "join":
            required.append("token")

        for field in required:
            if not body.get(field):
                return {"success": False, "error": f"Missing field: {field}"}
        if not token_value:
            return {"success": False, "error": "Missing field: telegram_bot_token"}

        try:
            requested_scope = normalize_scope_path(body["scope"])
        except ValueError as e:
            return {"success": False, "error": str(e)}

        normalized_alias = normalize_alias(body.get("alias", ""))
        if len(normalized_alias) < 3:
            return {
                "success": False,
                "error": "Alias must contain at least 3 letters or numbers after normalization.",
            }
        if len(normalized_alias) > 32:
            normalized_alias = normalized_alias[:32]

        # Determine role
        if flow == "bootstrap":
            role = "OWNER"
            allowed_scope = requested_scope
            invite_token = body.get("token", "bootstrap")
        elif flow == "reconfigure":
            if not existing_config or not is_config_complete(existing_config):
                return {"success": False, "error": "No complete instance is available to reconfigure."}

            role = str(existing_config.get("cargo", "DEV") or "DEV")
            invite_token = existing_config.get("invite_token", "reconfigure")
            try:
                existing_allowed_scope = normalize_scope_path(
                    existing_config.get("allowed_scope") or existing_config.get("scope", "")
                )
            except ValueError as e:
                return {"success": False, "error": f"Invalid current allowed scope: {e}"}

            if role == "OWNER":
                allowed_scope = requested_scope
            else:
                try:
                    validate_scope_boundary(requested_scope, existing_allowed_scope)
                except ValueError as e:
                    return {"success": False, "error": str(e)}
                allowed_scope = existing_allowed_scope
        else:
            token_data = self._validate_token(body["token"])
            if not token_data.get("valid"):
                return {"success": False, "error": token_data.get("error")}
            role = token_data.get("role", "DEV")
            invite_token = body.get("token", "")
            try:
                allowed_scope = normalize_scope_path(token_data.get("scope", ""))
            except ValueError as e:
                return {"success": False, "error": f"Invalid token scope: {e}"}
            try:
                validate_scope_boundary(requested_scope, allowed_scope)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        try:
            agent_config = self._build_agent_config(body)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        telegram_id = body.get("telegram_id", existing_config.get("telegram_id", 0))
        telegram_username = body.get("telegram_username", existing_config.get("telegram_username", ""))
        if (not telegram_id or int(telegram_id) == 0) and isinstance(last_pairing, dict):
            if last_pairing.get("group_id") == body.get("telegram_group_id") and last_pairing.get("token") == token_value:
                telegram_id = last_pairing.get("telegram_id", telegram_id)
                telegram_username = last_pairing.get("telegram_username", telegram_username)

        agent_timeout_seconds = body.get("agent_timeout_seconds", existing_config.get("agent_timeout_seconds"))
        if agent_timeout_seconds not in (None, ""):
            try:
                agent_timeout_seconds = int(agent_timeout_seconds)
            except (TypeError, ValueError):
                return {"success": False, "error": "Agent timeout must be a positive integer."}
            if agent_timeout_seconds <= 0:
                return {"success": False, "error": "Agent timeout must be a positive integer."}

        config = {
            "telegram_bot_token": token_value,
            "telegram_group_id": body["telegram_group_id"],
            "telegram_id": telegram_id,
            "telegram_username": telegram_username,
            "invite_token": invite_token,
            "alias": normalized_alias,
            "agent": agent_config,
            "scope": str(requested_scope),
            "allowed_scope": str(allowed_scope),
            "cargo": role,
            "allow_project_changes": bool(body.get("allow_project_changes", True)),
            "auto_register": True,
            "proactive_events": {
                "on_commit": False,
                "on_build_fail": False,
                "on_pr_open": False
            }
        }
        if agent_timeout_seconds not in (None, ""):
            config["agent_timeout_seconds"] = agent_timeout_seconds

        try:
            saved = save_runtime_config(config, config_path, sync_env=sync_env)
            bootstrap_herd_dir(config["scope"], config)
            if flow in {"join", "reconfigure"}:
                upsert_member_registration(config)
            self.server.bridge_state["herd_path"] = str(Path(config["scope"]) / ".herd")
            self.server.bridge_state["status"]["setup_complete"] = True
            self.server.bridge_state["status"]["alias"] = config["alias"]
            self.server.bridge_state["status"]["scope"] = config["scope"]
            self.server.bridge_state["status"]["allow_project_changes"] = allow_project_changes(config)
            self.server.bridge_state["status"]["access_mode"] = _status_access_mode(config)
            self.server.bridge_state["config_ref"] = config
            return {
                "success": True,
                "config_path": saved["config_path"],
                "env_path": saved["env_path"],
                "sync_env": saved["sync_env"],
            }
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return {"success": False, "error": str(e)}

    def _get_status(self) -> dict:
        return self.server.bridge_state.get("status", {})

    def _get_current_config(self) -> dict:
        config_path = self.server.bridge_state.get("config_path", Path("config.json"))
        return get_current_config_state(config_path)

    def _update_scope(self, body: dict) -> dict:
        try:
            updated = update_runtime_scope(
                scope=body.get("scope", ""),
                bridge_state=self.server.bridge_state,
                sync_env=body.get("sync_env", True),
            )
            return {"success": True, **updated}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Error updating runtime scope: {e}")
            return {"success": False, "error": str(e)}

    def _update_access_mode(self, body: dict) -> dict:
        try:
            updated = update_runtime_access_mode(
                enabled=bool(body.get("allow_project_changes", True)),
                bridge_state=self.server.bridge_state,
                sync_env=body.get("sync_env", True),
            )
            return {"success": True, **updated}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Error updating access mode: {e}")
            return {"success": False, "error": str(e)}

    def _get_members(self) -> dict:
        members_file = Path("members.json")
        if not members_file.exists():
            return {"members": []}
        ensure_private_file(members_file)
        data = json.loads(members_file.read_text(encoding="utf-8"))
        members = []
        for member in data.get("members", []):
            members.append({
                "alias": member.get("alias", ""),
                "cargo": member.get("cargo", ""),
                "online": bool(member.get("online")),
                "scope": member.get("scope", ""),
            })
        return {"members": members}

    def _get_messages(self) -> dict:
        herd_path = self.server.bridge_state.get("herd_path")
        if not herd_path:
            return {"messages": []}
        
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log = Path(herd_path) / "memory" / "conversations" / f"{today}.jsonl"
        if not log.exists():
            return {"messages": []}
            
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        messages = []
        for line in lines[-50:]:
            try:
                messages.append(json.loads(line))
            except Exception:
                pass
        return {"messages": messages}

    def _get_cron(self) -> dict:
        tasks_file = Path("cron_tasks.json")
        if not tasks_file.exists():
            return {"tasks": []}
        return json.loads(tasks_file.read_text(encoding="utf-8"))


def start_ui_server(
    bridge_state: dict,
    open_browser: bool = True,
    initial_path: str | None = None,
) -> ThreadingHTTPServer:
    requested_port = UI_PORT
    raw_env_port = os.getenv("HERD_UI_PORT")
    if raw_env_port:
        try:
            requested_port = int(raw_env_port)
        except ValueError:
            logger.warning(f"[herd] Invalid HERD_UI_PORT={raw_env_port!r}; falling back to {UI_PORT}.")
            requested_port = UI_PORT

    try:
        server = ThreadingHTTPServer(("localhost", requested_port), HerdUIHandler)
    except OSError as e:
        if e.errno != 98:
            raise
        logger.warning(
            f"[herd] UI port {requested_port} is already in use. "
            "Falling back to a random free local port for this instance."
        )
        server = ThreadingHTTPServer(("localhost", 0), HerdUIHandler)

    server.bridge_state = bridge_state
    server.ui_auth_token = secrets.token_urlsafe(24)
    actual_port = int(server.server_address[1])
    bridge_state.setdefault("status", {})
    bridge_state["status"]["ui_port"] = actual_port

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    landing_path = initial_path or resolve_ui_landing_path(bridge_state)
    if not landing_path.startswith("/"):
        landing_path = f"/{landing_path}"
    separator = "&" if "?" in landing_path else "?"
    url = f"http://localhost:{actual_port}{landing_path}{separator}auth={urllib.parse.quote(server.ui_auth_token)}"
    if open_browser:
        webbrowser.open(url)
        logger.info(f"[herd] Local UI session running at {url}")
    else:
        logger.info(f"[herd] Local UI session URL: {url}")

    return server


# --- DEMO MODE ---

def seed_demo_data(scope: str) -> None:
    """Creates fake data so the UI can be explored without a real Telegram bot."""
    import secrets

    now = datetime.now(timezone.utc)

    # 1. Seed invite_tokens.json with a demo token
    tokens_file = Path("invite_tokens.json")
    demo_token = f"herd-{secrets.token_hex(3)}"
    tokens_data = {
        "tokens": [{
            "token": demo_token,
            "created_by": "owner",
            "cargo": "DEV",
            "scope": scope,
            "alias_sugerido": "demo_agent",
            "expires_at": None,
            "max_uses": 10,
            "used": 0,
            "used_by": [],
            "created_at": now.isoformat(),
        }]
    }
    write_private_text(tokens_file, json.dumps(tokens_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # 2. Seed members.json
    members_file = Path("members.json")
    members_data = {
        "members": [
            {
                "telegram_id": 111111,
                "telegram_username": "demo_owner",
                "alias": "owner",
                "agent": "claude-code",
                "scope": scope,
                "cargo": "OWNER",
                "token_delegation": True,
                "invited_by": "system",
                "invite_token": "bootstrap",
                "registered_at": now.isoformat(),
                "online": True,
            },
            {
                "telegram_id": 222222,
                "telegram_username": "demo_dev",
                "alias": "agent_back",
                "agent": "codex",
                "scope": scope,
                "cargo": "DEV",
                "token_delegation": False,
                "invited_by": "owner",
                "invite_token": "herd-abc123",
                "registered_at": now.isoformat(),
                "online": True,
            },
        ]
    }
    write_private_text(members_file, json.dumps(members_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # 3. Seed sample conversation messages
    herd_path = Path(scope) / ".herd"
    conv_dir = herd_path / "memory" / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)

    today = now.strftime("%Y-%m-%d")
    log_file = conv_dir / f"{today}.jsonl"
    sample_messages = [
        {"ts": now.isoformat(), "from": "demo_owner", "text": "@agent_back can you refactor the auth module?"},
        {"ts": now.isoformat(), "from": "@agent_back", "text": "Sure! I'll start with the login flow and then move to token refresh."},
        {"ts": now.isoformat(), "from": "demo_owner", "text": "@agent_back great, also add unit tests please."},
    ]
    with log_file.open("w", encoding="utf-8") as f:
        for m in sample_messages:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    ensure_private_file(log_file)

    # 4. Seed sample CRON tasks
    cron_file = Path("cron_tasks.json")
    cron_data = {
        "tasks": [
            {
                "id": f"task_{secrets.token_hex(3)}",
                "alias": "agent_back",
                "task": "run full test suite",
                "cron": "0 8 * * 1",
                "raw_schedule": "every monday 08:00",
                "created_by": "owner",
                "created_at": now.isoformat(),
                "active": True,
                "last_run": None,
                "run_count": 0,
            }
        ]
    }
    write_private_text(cron_file, json.dumps(cron_data, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(f"[demo] Data seeded successfully!")
    logger.info(f"[demo] Demo invite token: {demo_token}")


# --- MAIN ---

def main():
    import argparse
    load_env()
    p = argparse.ArgumentParser(description="Herd Bridge")
    p.add_argument("--ui", action="store_true")
    p.add_argument("--setup", action="store_true")
    p.add_argument("--demo", action="store_true", help="Run in demo mode: seeds test data & opens dashboard without Telegram.")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--config", type=Path, default=Path(os.getenv("CONFIG_PATH", "config.json")))
    args = p.parse_args()

    config_path = args.config
    config = load_config(config_path)

    bridge_state = {
        "status": {"online": False, "setup_complete": False}, 
        "herd_path": None,
        "config_path": config_path,
        "config_ref": config if config else None,
    }

    # --- DEMO MODE ---
    if args.demo:
        scope = str(PROJECT_ROOT)
        logger.info("[demo] Starting Herd in demo mode...")

        seed_demo_data(scope)
        bootstrap_herd_dir(scope, {
            "alias": "demo_agent",
            "agent": {"type": "echo"},
            "scope": scope,
            "cargo": "DEV",
            "telegram_group_id": 0,
            "allow_project_changes": True,
        })

        herd_path = Path(scope) / ".herd"
        bridge_state["herd_path"] = str(herd_path)
        bridge_state["status"]["online"] = True
        bridge_state["status"]["alias"] = "demo_agent"
        bridge_state["status"]["scope"] = scope
        bridge_state["status"]["allow_project_changes"] = True
        bridge_state["status"]["access_mode"] = "write-enabled"

        server = start_ui_server(bridge_state, open_browser=True, initial_path="/dashboard")
        logger.info("[demo] Dashboard available through the local authenticated UI session.")
        logger.info("[demo] Setup wizard available through the local authenticated UI session.")
        logger.info("[demo] Press Ctrl+C to stop.")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("[demo] Stopped.")
        return

    # --- NORMAL MODE ---
    if not is_config_complete(config) or args.setup:
        initial_path = "/setup" if args.setup or not is_config_complete(config) else None
        server = start_ui_server(bridge_state, open_browser=not args.headless, initial_path=initial_path)
        if args.headless and not is_config_complete(config):
            logger.error("[herd] --headless requires a complete configuration. Aborting.")
            sys.exit(1)
            
        logger.info("Waiting for UI setup to complete...")
        while not bridge_state["status"]["setup_complete"]:
            time.sleep(1)
            
        config = load_config(config_path)
    elif args.ui:
        start_ui_server(bridge_state, open_browser=True)

    if not is_config_complete(config):
        logger.error("Failed to load configuration after setup. Aborting.")
        sys.exit(1)

    herd_path = Path(config["scope"]) / ".herd"
    bridge_state["herd_path"] = str(herd_path)
    bridge_state["config_ref"] = config

    bootstrap_herd_dir(config["scope"], config)
    if config.get("auto_register", True):
        upsert_member_registration(config)

    logger.info("Starting Telegram Bot Polling...")
    app = Application.builder().token(config["telegram_bot_token"]).build()
    app.bot_data["config"] = config
    app.bot_data["herd_path"] = herd_path
    bridge_state["bot_data_ref"] = app.bot_data

    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, handle_message))
    
    bridge_state["status"]["online"] = True
    bridge_state["status"]["alias"] = config["alias"]
    bridge_state["status"]["scope"] = config["scope"]
    bridge_state["status"]["allow_project_changes"] = allow_project_changes(config)
    bridge_state["status"]["access_mode"] = _status_access_mode(config)
    
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
