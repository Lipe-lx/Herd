import getpass
import importlib.util
import re
import shutil
import sys
from pathlib import Path

REQUIRED_PACKAGES = (
    {
        "module": "telegram",
        "package": 'python-telegram-bot[job-queue]',
        "reason": "Telegram bridge and unified runner",
        "required": True,
    },
    {
        "module": "schedule",
        "package": "schedule",
        "reason": "Fallback scheduler for CRON jobs",
        "required": True,
    },
    {
        "module": "httpx",
        "package": "httpx",
        "reason": "HTTP helpers used by the local UI flow",
        "required": True,
    },
    {
        "module": "apscheduler",
        "package": 'python-telegram-bot[job-queue]',
        "reason": "Native PTB job queue for CRON scheduling",
        "required": False,
    },
)

AGENT_CATALOG = (
    {
        "type": "claude-code",
        "label": "Claude Code",
        "command": "claude",
        "mode": "cli",
    },
    {
        "type": "gemini",
        "label": "Gemini CLI",
        "command": "gemini",
        "mode": "cli",
    },
    {
        "type": "codex",
        "label": "Codex",
        "command": "codex",
        "mode": "cli",
    },
    {
        "type": "cursor",
        "label": "Cursor / VSCode",
        "command": None,
        "mode": "file-watcher",
    },
    {
        "type": "antigravity",
        "label": "Antigravity / VSCode",
        "command": None,
        "mode": "file-watcher",
    },
    {
        "type": "vscode-generic",
        "label": "Other VSCode Agent",
        "command": None,
        "mode": "file-watcher",
    },
)


def normalize_alias(alias: str) -> str:
    text = str(alias or "").strip().lstrip("@").lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def suggest_alias(scope: str | Path | None = None) -> str:
    candidates: list[str] = []
    if scope:
        candidates.append(Path(str(scope)).expanduser().name)

    cwd_name = Path.cwd().name
    if cwd_name:
        candidates.append(cwd_name)

    try:
        username = getpass.getuser()
    except Exception:
        username = ""

    if username:
        candidates.append(username)

    for candidate in candidates:
        normalized = normalize_alias(candidate)
        if len(normalized) < 3:
            continue
        if not normalized.startswith("agent_"):
            normalized = f"agent_{normalized}"
        return normalized[:32]

    return "agent_herd"


def _module_installed(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def detect_agent_runtime() -> tuple[list[dict], str]:
    detected_agents = []
    recommended = "claude-code"

    for agent in AGENT_CATALOG:
        command = agent.get("command")
        available = bool(command and shutil.which(command))
        detected_agents.append({
            "type": agent["type"],
            "label": agent["label"],
            "mode": agent["mode"],
            "command": command,
            "available": available,
            "detectable": bool(command),
        })
        if available and recommended == "claude-code":
            recommended = agent["type"]

    return detected_agents, recommended


def check_python_packages() -> list[dict]:
    checks = []
    for package in REQUIRED_PACKAGES:
        installed = _module_installed(package["module"])
        checks.append({
            "module": package["module"],
            "package": package["package"],
            "reason": package["reason"],
            "required": package["required"],
            "installed": installed,
        })
    return checks


def build_setup_diagnostics(
    config: dict | None = None,
    *,
    project_root: str | Path | None = None,
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> dict:
    config = dict(config or {})
    root = Path(project_root or Path.cwd()).resolve()
    default_scope = str(root)
    alias_source = str(config.get("scope") or root)
    alias = normalize_alias(str(config.get("alias") or "")) or suggest_alias(alias_source)
    agents, recommended_agent = detect_agent_runtime()
    package_checks = check_python_packages()

    missing_required = [
        check["package"]
        for check in package_checks
        if check["required"] and not check["installed"]
    ]
    detected_labels = [agent["label"] for agent in agents if agent["available"]]

    return {
        "cwd": str(root),
        "config_path": str(config_path or "config.json"),
        "env_path": str(env_path or ".env"),
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "defaults": {
            "scope": default_scope,
            "alias": alias,
        },
        "agents": agents,
        "recommended_agent": recommended_agent,
        "detected_agent_labels": detected_labels,
        "package_checks": package_checks,
        "required_packages_ready": not missing_required,
        "missing_required_packages": missing_required,
    }
