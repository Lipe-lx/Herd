import os
from pathlib import Path


PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def ensure_private_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, PRIVATE_DIR_MODE)
    except OSError:
        pass
    return target


def ensure_private_file(path: str | Path, mode: int = PRIVATE_FILE_MODE) -> Path:
    target = Path(path)
    try:
        os.chmod(target, mode)
    except OSError:
        pass
    return target


def write_private_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int = PRIVATE_FILE_MODE,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding=encoding)
    return ensure_private_file(target, mode=mode)
