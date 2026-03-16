import os
import shlex
from pathlib import Path

from .storage_security import ensure_private_file, write_private_text


MINIMAL_ENV_KEYS = ("CONFIG_PATH", "TELEGRAM_BOT_TOKEN")
PRIVATE_CONFIG_FIELDS = {"telegram_bot_token"}

def resolve_env_path(env_path: str | Path | None = None) -> Path:
    path = Path(env_path or os.getenv("ENV_PATH", ".env"))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def read_env_file(env_path: str | Path | None = None) -> dict[str, str]:
    path = resolve_env_path(env_path)
    values: dict[str, str] = {}

    if not path.exists():
        return values

    ensure_private_file(path)

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        values[key] = value

    return values


def load_env(env_path: str | Path | None = None) -> Path:
    path = resolve_env_path(env_path)
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)
    return path


def _quote_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(ch.isspace() for ch in value) or any(ch in value for ch in ['"', "#", "$"]):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def env_values_from_config(
    config: dict,
    config_path: str | Path | None = None,
    *,
    minimal: bool = False,
) -> dict[str, str]:
    agent = dict(config.get("agent") or {})
    args = agent.get("args", [])
    args_text = shlex.join(args) if args else ""

    values = {
        "CONFIG_PATH": str(config_path or os.getenv("CONFIG_PATH", "config.json")),
        "TELEGRAM_BOT_TOKEN": str(config.get("telegram_bot_token", "") or ""),
        "TELEGRAM_GROUP_ID": str(config.get("telegram_group_id", "") or ""),
        "TELEGRAM_USER_ID": str(config.get("telegram_id", "") or ""),
        "HERD_INVITE_TOKEN": str(config.get("invite_token", "") or ""),
        "HERD_ALIAS": str(config.get("alias", "") or ""),
        "HERD_SCOPE": str(config.get("scope", "") or ""),
        "HERD_CARGO": str(config.get("cargo", "") or ""),
        "HERD_AGENT_TYPE": str(agent.get("type", "") or ""),
        "HERD_AGENT_COMMAND": str(agent.get("command", "") or ""),
        "HERD_AGENT_ARGS": args_text,
        "HERD_AGENT_MODE": str(agent.get("mode", "") or ""),
        "HERD_AGENT_PROMPT_MODE": str(agent.get("prompt_mode", "") or ""),
    }

    if minimal:
        return {key: values.get(key, "") for key in MINIMAL_ENV_KEYS}

    return values


def write_env_file(values: dict[str, str], env_path: str | Path | None = None) -> Path:
    path = resolve_env_path(env_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = [
        "# Core CLI settings",
        f"CONFIG_PATH={_quote_env_value(values.get('CONFIG_PATH', ''))}",
        f"TELEGRAM_BOT_TOKEN={_quote_env_value(values.get('TELEGRAM_BOT_TOKEN', ''))}",
        f"TELEGRAM_GROUP_ID={_quote_env_value(values.get('TELEGRAM_GROUP_ID', ''))}",
        f"TELEGRAM_USER_ID={_quote_env_value(values.get('TELEGRAM_USER_ID', ''))}",
        "",
        "# Herd config overrides",
        f"HERD_INVITE_TOKEN={_quote_env_value(values.get('HERD_INVITE_TOKEN', ''))}",
        f"HERD_ALIAS={_quote_env_value(values.get('HERD_ALIAS', ''))}",
        f"HERD_SCOPE={_quote_env_value(values.get('HERD_SCOPE', ''))}",
        f"HERD_CARGO={_quote_env_value(values.get('HERD_CARGO', ''))}",
        "",
        "# Agent config overrides",
        f"HERD_AGENT_TYPE={_quote_env_value(values.get('HERD_AGENT_TYPE', ''))}",
        f"HERD_AGENT_COMMAND={_quote_env_value(values.get('HERD_AGENT_COMMAND', ''))}",
        f"HERD_AGENT_ARGS={_quote_env_value(values.get('HERD_AGENT_ARGS', ''))}",
        f"HERD_AGENT_MODE={_quote_env_value(values.get('HERD_AGENT_MODE', ''))}",
        f"HERD_AGENT_PROMPT_MODE={_quote_env_value(values.get('HERD_AGENT_PROMPT_MODE', ''))}",
        "",
    ]
    return write_private_text(path, "\n".join(content), encoding="utf-8")


def sanitize_persisted_config(config: dict) -> dict:
    sanitized = json_safe_copy(config)
    for field in PRIVATE_CONFIG_FIELDS:
        sanitized.pop(field, None)
    return sanitized


def json_safe_copy(config: dict) -> dict:
    import json

    return json.loads(json.dumps(config))


def apply_config_env_overrides(config: dict) -> dict:
    merged = dict(config)
    agent = dict(merged.get("agent") or {})

    raw_mapping = {
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "invite_token": os.getenv("HERD_INVITE_TOKEN"),
        "alias": os.getenv("HERD_ALIAS"),
        "scope": os.getenv("HERD_SCOPE"),
        "cargo": os.getenv("HERD_CARGO"),
    }

    int_mapping = {
        "telegram_group_id": os.getenv("TELEGRAM_GROUP_ID"),
        "telegram_id": os.getenv("TELEGRAM_USER_ID"),
    }

    for key, value in raw_mapping.items():
        if value and not merged.get(key):
            merged[key] = value

    for key, value in int_mapping.items():
        if not value:
            continue
        if merged.get(key) not in (None, "", 0):
            continue
        try:
            merged[key] = int(value)
        except ValueError:
            pass

    if os.getenv("HERD_AGENT_TYPE") and not agent.get("type"):
        agent["type"] = os.getenv("HERD_AGENT_TYPE")
    if os.getenv("HERD_AGENT_COMMAND") and not agent.get("command"):
        agent["command"] = os.getenv("HERD_AGENT_COMMAND")
    if os.getenv("HERD_AGENT_MODE") and not agent.get("mode"):
        agent["mode"] = os.getenv("HERD_AGENT_MODE")
    if os.getenv("HERD_AGENT_ARGS") and not agent.get("args"):
        agent["args"] = shlex.split(os.getenv("HERD_AGENT_ARGS", ""))
    if os.getenv("HERD_AGENT_PROMPT_MODE") and not agent.get("prompt_mode"):
        agent["prompt_mode"] = os.getenv("HERD_AGENT_PROMPT_MODE")

    if agent:
        merged["agent"] = agent

    return merged
