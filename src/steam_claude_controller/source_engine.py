"""Source engine (Source 1: Portal, Half-Life 2, ...) adapter.

These builds have no remote console, so commands travel through files:

1. ``claude_bridge.cfg`` (exec'd via ``+exec`` at launch) binds INS to ``exec claude_cmd``.
2. To run a command we write it to ``claude_cmd.cfg`` followed by ``echo <marker>``,
   tap INS, and read the output from ``console.log`` (``-condebug``) up to the marker.

If the bind isn't active (a menu has keyboard focus, or the game was started without our
launch options) we fall back to opening the console and typing the exec line.
"""
from __future__ import annotations

import math
import re
import secrets
import time
from pathlib import Path

from PIL import Image, ImageDraw

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
    def launch_args(width: int, height: int, map_name: str | None = None,
                    save: str | None = None) -> list[str]:
        args = ["-novid", "-condebug", "-windowed", "-noborder",
                "-w", str(width), "-h", str(height), "+exec", "claude_bridge"]
        if save:
            args += ["+load", save]
        elif map_name:
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

    def capture(self, ctrl: Controller, timeout: float = 3.0) -> Image.Image:
        """Screenshot via the engine's own `jpeg` command. It reads the backbuffer, so it
        works even when the window is covered, minimized or not presenting frames
        (D3D9 sometimes stops presenting after a level change while still rendering)."""
        path = self.mod_dir / "screenshots" / "claude_capture.jpg"
        path.unlink(missing_ok=True)
        self.run(ctrl, "jpeg claude_capture 90")
        deadline = time.monotonic() + timeout
        last_size = -1
        while time.monotonic() < deadline:  # written at the end of the next frame
            size = path.stat().st_size if path.exists() else -1
            if size > 0 and size == last_size:
                with Image.open(path) as img:
                    return img.convert("RGB")
            last_size = size
            time.sleep(0.05)
        raise TimeoutError(f"Engine screenshot not written to {path}")

    # ------------------------------------------------------------------ state & aim

    def current_map(self) -> str | None:
        """Last map the engine loaded. (host_map keeps the launch map across changelevels.)"""
        try:
            with open(self.console_log, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 512_000))
                tail = f.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        names = re.findall(r"^Map name: (\S+)", tail, re.MULTILINE)
        return names[-1] if names else None

    def state(self, ctrl: Controller) -> dict:
        out = self.run(ctrl, "getpos")
        result: dict = {}
        m = _GETPOS.search(out)
        if m:
            x, y, z, pitch, yaw, roll = (float(v) for v in m.groups())
            result.update(pos=[x, y, z], pitch=pitch, yaw=yaw, roll=roll)
        else:
            result["raw"] = out
        result["map"] = self.current_map()
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

    def goto(self, ctrl: Controller, x: float, y: float, tolerance: float = 12.0,
             timeout: float = 20.0) -> dict:
        """Walk to (x, y) in closed loop: face the target, walk a burst, re-read getpos.
        Turns are smoothed so a carried object isn't knocked out of the player's hands."""
        deadline = time.monotonic() + timeout
        last: tuple[float, float] | None = None
        stalls = 0
        while True:
            st = self.state(ctrl)
            if "pos" not in st:
                raise RuntimeError(f"Can't read position (getpos output: {st.get('raw')!r})")
            px, py = st["pos"][0], st["pos"][1]
            dist = math.hypot(x - px, y - py)
            result = None
            if dist <= tolerance:
                result = "arrived"
            elif time.monotonic() > deadline:
                result = "timeout"
            elif last and math.hypot(px - last[0], py - last[1]) < 2.0:
                stalls += 1
                if stalls >= 2:
                    result = "blocked"
            else:
                stalls = 0
            if result:
                return {**st, "result": result, "remaining": round(dist, 1)}
            last = (px, py)
            dyaw = _wrap180(math.degrees(math.atan2(y - py, x - px)) - st["yaw"])
            if abs(dyaw) > 3:
                self.turn(ctrl, dyaw, 0, duration=min(0.5, abs(dyaw) / 360))
            ctrl.tap("w", min(0.8, max(0.06, dist / 260)))

    def panorama(self, ctrl: Controller, pitch: float = 0.0, tile_width: int = 480) -> tuple[Image.Image, dict]:
        """2x2 grid of the views at the current yaw, +90 (left), +180 and -90 (right),
        each labelled with its absolute yaw. Ends facing the original direction."""
        st = self.aim(ctrl, None, pitch)
        base = st["yaw"]
        tiles = []
        for i in range(4):
            if i:
                self.turn(ctrl, 90, 0)
                time.sleep(0.12)
            img, _ = ctrl.grab()
            tile = img.resize((tile_width, round(img.height * tile_width / img.width)),
                              Image.Resampling.LANCZOS)
            label = f"{('AHEAD', 'LEFT', 'BEHIND', 'RIGHT')[i]}  yaw {_wrap180(base + 90 * i):.0f}"
            draw = ImageDraw.Draw(tile)
            draw.rectangle((0, 0, 8 + 7 * len(label), 16), fill=(0, 0, 0))
            draw.text((4, 3), label, fill=(255, 255, 0))
            tiles.append(tile)
        self.turn(ctrl, 90, 0)  # back to the start
        tw, th = tiles[0].size
        grid = Image.new("RGB", (tw * 2 + 4, th * 2 + 4), (255, 255, 255))
        # Layout: AHEAD | RIGHT on top, LEFT | BEHIND below.
        for tile, (cx, cy) in zip(tiles, [(0, 0), (0, 1), (1, 1), (1, 0)]):
            grid.paste(tile, (cx * (tw + 4), cy * (th + 4)))
        return grid, st

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
