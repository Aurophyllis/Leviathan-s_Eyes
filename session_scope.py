"""Local evidence credentials shared by the Eyes service and its clients."""

from pathlib import Path
import re

CONFIG_DIR = Path.home() / ".leviathan" / "eyes"
SESSION_ID = re.compile(r"(?:session-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def token_path(session_id, directory=None):
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid Eyes session ID")
    return (directory or CONFIG_DIR / "sessions") / f"{session_id}.token"
