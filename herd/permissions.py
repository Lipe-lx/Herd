ROLE_RANK = {"OWNER": 4, "LEAD": 3, "DEV": 2, "QA": 2, "VIEWER": 1}

def _normalize_aliases(actor_aliases) -> set[str]:
    if isinstance(actor_aliases, str):
        return {actor_aliases}
    if actor_aliases is None:
        return set()
    return {str(alias) for alias in actor_aliases if alias}


def can_schedule_for(actor_role: str, target_alias: str, actor_aliases) -> bool:
    """OWNER and LEAD can schedule for anyone. DEV/QA only for themselves."""
    if ROLE_RANK.get(actor_role, 0) >= 3:
        return True
    return target_alias in _normalize_aliases(actor_aliases)

def can_generate_token(actor: dict) -> bool:
    """OWNER always can. LEAD only if token_delegation is True."""
    if actor["cargo"] == "OWNER":
        return True
    if actor["cargo"] == "LEAD" and actor.get("token_delegation", False):
        return True
    return False

def can_modify_cargo(actor_role: str, target_role: str) -> bool:
    """No one demotes OWNER. LEAD cannot touch other LEADs or OWNER."""
    if target_role == "OWNER":
        return False
    if actor_role == "OWNER":
        return True
    if actor_role == "LEAD" and target_role not in ("LEAD", "OWNER"):
        return True
    return False

def can_remove_task(actor: dict, task: dict) -> bool:
    # Creator can always remove their own task
    actor_aliases = _normalize_aliases(actor.get("aliases") or actor.get("alias"))
    if task["created_by"] in actor_aliases:
        return True
    # OWNER and LEAD can remove any task
    return actor["cargo"] in ("OWNER", "LEAD")
