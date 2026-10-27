"""Source engine (Source 1: Portal, Half-Life 2, ...) adapter.

These builds have no remote console, so commands travel through files:

1. ``claude_bridge.cfg`` (exec'd via ``+exec`` at launch) binds INS to ``exec claude_cmd``.
2. To run a command we write it to ``claude_cmd.cfg`` followed by ``echo <marker>``,
   tap INS, and read the output from ``console.log`` (``-condebug``) up to the marker.

If the bind isn't active (a menu has keyboard focus, or the game was started without our
launch options) we fall back to opening the console and typing the exec line.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

from .controller import Controller

BRIDGE_KEY = "ins"
CONSOLE_KEY = "`"

BRIDGE_CFG = """// Written by steam-claude-controller. Safe to delete.
con_enable 1
bind INS "exec claude_cmd"
// Raw, unaccelerated mouse so that degrees = counts * sensitivity * m_yaw exactly.
m_rawinput 1
m_customaccel 0
m_filter 0
echo CLAUDE_BRIDGE_READY
"""

_GETPOS = re.compile(r"setpos\s+(\S+)\s+(\S+)\s+(\S+);\s*setang\s+(\S+)\s+(\S+)\s+(\S+)")
_MOUSE_CVARS = ("sensitivity", "m_yaw", "m_pitch", "m_rawinput")


def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


class SourceGame:
    def __init__(self, install_dir: Path, mod: str | None = None):
        self.install_dir = install_dir
        mods = self.mod_dirs(install_dir)
        if mod:
            self.mod_dir = install_dir / mod
            if self.mod_dir not in mods:
                raise ValueError(f"{mod!r} has no gameinfo.txt; found {[m.name for m in mods]}")
        else:
            # Content-only dirs (hl2/, platform/) ship next to the actual game dir.
            own = [m for m in mods if m.name.lower() not in ("hl2", "platform")] or mods
            if not own:
                raise ValueError(f"No Source mod directory (gameinfo.txt) under {install_dir}")
            self.mod_dir = own[0]
        self.cfg_dir = self.mod_dir / "cfg"
        self._mouse: tuple[float, float, float] | None = None

    @staticmethod
    def mod_dirs(install_dir: Path) -> list[Path]:
        return sorted(p.parent for p in install_dir.glob("*/gameinfo.txt"))

    @classmethod
    def is_source(cls, install_dir: Path) -> bool:
        return bool(cls.mod_dirs(install_dir)) and any((install_dir / "bin").glob("**/engine.dll"))

    @property
    def console_log(self) -> Path:
        return self.mod_dir / "console.log"

    # ------------------------------------------------------------------ launch

    def write_bridge(self) -> None:
        self.cfg_dir.mkdir(exist_ok=True)
        (self.cfg_dir / "claude_bridge.cfg").write_text(BRIDGE_CFG, encoding="utf-8")
        (self.cfg_dir / "claude_cmd.cfg").write_text("", encoding="utf-8")

    @staticmethod
    def launch_args(width: int, height: int, map_name: str | None = None) -> list[str]:
        args = ["-novid", "-condebug", "-windowed", "-noborder",
                "-w", str(width), "-h", str(height), "+exec", "claude_bridge"]
        if map_name:
            args += ["+map", map_name]
        return args

    # ------------------------------------------------------------------ console

    def _log_size(self) -> int:
        try:
            return self.console_log.stat().st_size
        except FileNotFoundError:
            return 0

    def _read_from(self, offset: int) -> str:
        try:
            with open(self.console_log, "rb") as f:
                f.seek(0, 2)
                if f.tell() < offset:  # log was truncated/recreated
                    offset = 0
                f.seek(offset)
                return f.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def _wait_for(self, marker: str, offset: int, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            text = self._read_from(offset)
            idx = text.find(marker)
            if idx >= 0:
                lines = [ln for ln in text[:idx].splitlines()
                         if ln.strip() and not ln.startswith("execing ")]
                return "\n".join(lines)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def run(self, ctrl: Controller, command: str, timeout: float = 3.0) -> str:
        """Execute console command(s) (newline- or ;-separated) and return their output."""
        marker = f"CLAUDE_DONE_{secrets.token_hex(4)}"
        (self.cfg_dir / "claude_cmd.cfg").write_text(f"{command}\necho {marker}\n", encoding="utf-8")
        start = self._log_size()
        ctrl.tap(BRIDGE_KEY)
        out = self._wait_for(marker, start, min(1.0, timeout))
        if out is None:
            ctrl.tap(CONSOLE_KEY)
            time.sleep(0.3)
            ctrl.type_text("exec claude_bridge; exec claude_cmd")
            ctrl.tap("enter")
            out = self._wait_for(marker, start, timeout)
            ctrl.tap(CONSOLE_KEY)
        if out is None:
            raise TimeoutError(
                f"No console output within {timeout}s. Is the game running with -condebug "
                f"(launch it via source_launch)? Expected log at {self.console_log}")
        if any(c in command for c in _MOUSE_CVARS):
            self._mouse = None
        return out

    # ------------------------------------------------------------------ state & aim

    def state(self, ctrl: Controller) -> dict:
        out = self.run(ctrl, "getpos\nhost_map")
        result: dict = {}
        m = _GETPOS.search(out)
        if m:
            x, y, z, pitch, yaw, roll = (float(v) for v in m.groups())
            result.update(pos=[x, y, z], pitch=pitch, yaw=yaw, roll=roll)
        mm = re.search(r'"host_map"\s*=\s*"([^"]*)"', out)
        if mm:
            result["map"] = mm.group(1)
        if not m:
            result["raw"] = out
        return result

    def _mouse_params(self, ctrl: Controller) -> tuple[float, float, float]:
        if self._mouse is None:
            out = self.run(ctrl, "sensitivity\nm_yaw\nm_pitch")

            def val(name: str, default: float) -> float:
                m = re.search(rf'"{name}"\s*=\s*"([-+\d.eE]+)"', out)
                return float(m.group(1)) if m else default

            self._mouse = (val("sensitivity", 3.0), val("m_yaw", 0.022), val("m_pitch", 0.022))
        return self._mouse

    def turn(self, ctrl: Controller, dyaw: float, dpitch: float, duration: float = 0.0) -> tuple[int, int]:
        """Rotate the view by degrees. +yaw = left (Source convention), +pitch = down."""
        sens, m_yaw, m_pitch = self._mouse_params(ctrl)
        dx = round(-dyaw / (sens * m_yaw))
        dy = round(dpitch / (sens * m_pitch))
        ctrl.move_mouse(dx, dy, duration)
        return dx, dy

    def aim(self, ctrl: Controller, yaw: float | None, pitch: float | None) -> dict:
        """Turn to absolute angles, correcting with a couple of getpos round-trips."""
        st = self.state(ctrl)
        if "yaw" not in st:
            raise RuntimeError(f"Can't read view angles (getpos output: {st.get('raw')!r})")
        for _ in range(3):
            dyaw = _wrap180(yaw - st["yaw"]) if yaw is not None else 0.0
            dpitch = (pitch - st["pitch"]) if pitch is not None else 0.0
            if abs(dyaw) < 0.25 and abs(dpitch) < 0.25:
                break
            self.turn(ctrl, dyaw, dpitch)
            time.sleep(0.08)
            st = self.state(ctrl)
        return st
