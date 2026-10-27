"""Steam client discovery: install path, library folders, installed apps, launching.

Everything here reads Steam's own files (libraryfolders.vdf, appmanifest_*.acf) and
registry keys; nothing talks to Steam's network services.
"""
from __future__ import annotations

import re
import subprocess
import winreg
from dataclasses import dataclass
from pathlib import Path

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|(//[^\n]*)|([^\s{}"]+)')

# Folder/file name fragments that indicate a kernel or user-mode anti-cheat. Sending
# synthetic input to such games can get the Steam account banned, so we refuse to.
_ANTICHEAT_MARKERS = ("easyanticheat", "battleye", "beclient", "anticheat", "vanguard", "nprotect")


def parse_vdf(text: str) -> dict:
    """Parse Valve's KeyValues text format into nested dicts."""
    stack: list[dict] = [{}]
    key: str | None = None
    for m in _TOKEN.finditer(text):
        quoted, brace, comment, bare = m.groups()
        if comment is not None:
            continue
        if brace == "{":
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
        else:
            tok = re.sub(r"\\(.)", r"\1", quoted) if quoted is not None else bare
            if key is None:
                key = tok
            else:
                stack[-1][key] = tok
                key = None
    return stack[0]


def _reg_value(name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def steam_root() -> Path:
    path = _reg_value("SteamPath")
    root = Path(path) if path else Path(r"C:\Program Files (x86)\Steam")
    if not (root / "steam.exe").exists():
        raise FileNotFoundError(f"Steam not found at {root}")
    return root


def running_appid() -> int:
    """AppID Steam reports as currently running (0 if none)."""
    return int(_reg_value("RunningAppID") or 0)


def library_folders() -> list[Path]:
    root = steam_root()
    vdf = root / "steamapps" / "libraryfolders.vdf"
    folders = [root]
    if vdf.exists():
        data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
        for entry in data.get("libraryfolders", {}).values():
            if isinstance(entry, dict) and "path" in entry:
                p = Path(entry["path"])
                if p not in folders:
                    folders.append(p)
    return folders


@dataclass
class Game:
    appid: int
    name: str
    install_dir: Path
    installed: bool
    last_played: int
    anticheat: str | None

    def to_dict(self) -> dict:
        return {
            "appid": self.appid,
            "name": self.name,
            "install_dir": str(self.install_dir),
            "installed": self.installed,
            "last_played": self.last_played,
            "anticheat": self.anticheat,
        }


def detect_anticheat(install_dir: Path) -> str | None:
    """Return the first anti-cheat marker found up to two levels deep, else None."""
    if not install_dir.is_dir():
        return None
    candidates = list(install_dir.iterdir())
    for child in list(candidates):
        if child.is_dir():
            try:
                candidates.extend(child.iterdir())
            except OSError:
                pass
    for p in candidates:
        low = p.name.lower()
        for marker in _ANTICHEAT_MARKERS:
            if marker in low:
                return p.name
    return None


def installed_games() -> list[Game]:
    games: dict[int, Game] = {}
    for lib in library_folders():
        for manifest in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                state = parse_vdf(manifest.read_text(encoding="utf-8", errors="replace"))["AppState"]
            except (OSError, KeyError):
                continue
            appid = int(state.get("appid", 0))
            install_dir = lib / "steamapps" / "common" / state.get("installdir", "")
            games[appid] = Game(
                appid=appid,
                name=state.get("name", f"app {appid}"),
                install_dir=install_dir,
                installed=bool(int(state.get("StateFlags", 0)) & 4),
                last_played=int(state.get("LastPlayed", 0)),
                anticheat=detect_anticheat(install_dir),
            )
    return sorted(games.values(), key=lambda g: g.name.lower())


def find_game(appid: int) -> Game:
    for g in installed_games():
        if g.appid == appid:
            return g
    raise LookupError(f"AppID {appid} is not installed in any Steam library")


def launch(appid: int, args: list[str] | None = None) -> None:
    """Launch a game through the Steam client (so Steam overlay/Input/cloud all work)."""
    exe = steam_root() / "steam.exe"
    subprocess.Popen([str(exe), "-applaunch", str(appid), *(args or [])], close_fds=True)
