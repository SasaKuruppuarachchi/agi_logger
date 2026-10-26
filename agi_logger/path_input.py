"""Tab completion for file and directory paths in the agi_logger UI prompts.

`input_path()` behaves like `input()`, but Tab completes paths (directories end in "/", `~` stays `~`,
relative paths complete against the current directory). The completer is installed only for the
duration of the prompt, so other prompts keep plain input. Without readline it falls back to `input()`.
"""
from __future__ import annotations

import glob
import os
from typing import List, Optional

try:
    import readline
except ImportError:  # pragma: no cover - platform without readline
    readline = None  # type: ignore[assignment]

PATH_KEY_HINTS = ("path", "dir", "file", "destination", "folder", "uri", "qos_settings")


def complete_path(text: str) -> List[str]:
    """Candidates for a partially typed path."""
    home = os.path.expanduser("~")
    expanded = os.path.expanduser(text)
    out = []
    for m in sorted(glob.glob(glob.escape(expanded) + "*")):
        if os.path.isdir(m):
            m = m.rstrip("/") + "/"
        if text.startswith("~") and m.startswith(home):
            m = "~" + m[len(home):]
        out.append(m)
    return out


def _completer(text: str, state: int) -> Optional[str]:
    matches = complete_path(text)
    return matches[state] if state < len(matches) else None


def is_path_key(name: str) -> bool:
    """Config keys whose values are paths (bag_path, session_path, destination_path, file_path, ...)."""
    name = name.lower()
    return any(h in name for h in PATH_KEY_HINTS)


def input_path(prompt: str) -> str:
    if readline is None:
        return input(prompt)
    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    readline.set_completer(_completer)
    readline.set_completer_delims(" \t\n;")  # keep / . ~ - _ inside the completed word
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        return input(prompt)
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)
