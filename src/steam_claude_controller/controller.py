"""Game-agnostic controller: attach to a game window, look at it, send timed input."""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Callable

from PIL import Image
from pydantic import BaseModel, Field

from . import steam, win32

MAX_SEQUENCE_SECONDS = 60.0
_MOUSE_ALIASES = {"mouse1": "left", "mouse2": "right", "mouse3": "middle",
                  "lmb": "left", "rmb": "right", "mmb": "middle"}
_CLICK_BUTTONS = {"left": "mouse1", "right": "mouse2", "middle": "mouse3"}


def encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class Step(BaseModel):
    """One input step. Set exactly one action field; `hold`/`duration` modify it."""

    key: str | None = Field(None, description='Tap/hold a key or chord, e.g. "w", "space", "ctrl+e".')
    hold: float = Field(0.06, ge=0, le=30, description="Seconds to hold `key` or `click`.")
    down: list[str] | None = Field(None, description='Press and keep held, e.g. ["w", "mouse1"].')
    up: list[str] | None = Field(None, description="Release keys/buttons previously held with `down`.")
    move: tuple[int, int] | None = Field(None, description="Relative mouse move in raw counts (dx, dy); +dx turns right, +dy looks down.")
    duration: float = Field(0.0, ge=0, le=10, description="Spread `move` over this many seconds.")
    click: str | None = Field(None, description='"left", "right" or "middle".')
    at: tuple[float, float] | None = Field(None, description="Move the cursor to (x, y) in last-screenshot pixels before clicking (menus).")
    text: str | None = Field(None, description="Type literal text (for text fields / console).")
    scroll: int | None = Field(None, description="Mouse wheel notches, + = up.")
    wait: float | None = Field(None, ge=0, le=30, description="Sleep this many seconds.")


class Controller:
    def __init__(self) -> None:
        self.window: win32.Window | None = None
        self.game: steam.Game | None = None
        self.shot_scale = 1.0  # screenshot pixels per client pixel
        # Engine-side capture (e.g. Source's `jpeg`), preferred over window capture.
        self.capture_hook: Callable[[], Image.Image] | None = None
        self._held_keys: set[int] = set()
        self._held_buttons: set[str] = set()

    # ------------------------------------------------------------------ attach

    def attach(self, appid: int | None = None, title: str | None = None,
               timeout: float = 0.0) -> win32.Window:
        """Pick the game window: by AppID (process under its install dir), title, or
        whatever Steam reports as running."""
        if appid is None and title is None:
            appid = steam.running_appid() or None
            if appid is None:
                raise RuntimeError("No appid/title given and Steam reports no running game.")
        game = steam.find_game(appid) if appid is not None else None
        if game and game.anticheat:
            raise PermissionError(
                f"{game.name} ships anti-cheat ({game.anticheat}). Synthetic input could get the "
                "Steam account banned, so this controller refuses to drive it.")
        deadline = time.monotonic() + timeout
        while True:
            win = self._find_window(game, title)
            if win or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        if not win:
            what = game.name if game else repr(title)
            raise LookupError(f"No visible window found for {what}.")
        self.window, self.game = win, game
        return win

    @staticmethod
    def _find_window(game: steam.Game | None, title: str | None) -> win32.Window | None:
        candidates = []
        root = str(game.install_dir).lower() if game else None
        for w in win32.list_windows():
            if root and not w.exe.lower().startswith(root):
                continue
            if title and title.lower() not in w.title.lower():
                continue
            if w.width < 200 or w.height < 150:  # skip splash/tool windows
                continue
            candidates.append(w)
        return max(candidates, key=lambda w: w.width * w.height, default=None)

    def require_window(self) -> win32.Window:
        if not self.window or not win32.window_alive(self.window.hwnd):
            if self.game:  # game may have recreated its window (mode switch)
                self.attach(self.game.appid)
            else:
                raise RuntimeError("Not attached to a game window; call game_attach first.")
        return self.window

    def describe(self) -> str:
        w = self.require_window()
        name = self.game.name if self.game else w.title
        return f"{name} | window '{w.title}' {w.width}x{w.height} pid {w.pid}"

    # ------------------------------------------------------------------ vision

    def grab(self) -> tuple[Image.Image, str]:
        """Full-resolution capture of the client area: (image, method)."""
        w = self.require_window()
        if self.capture_hook:
            return self.capture_hook(), "engine"
        img, method = win32.capture(w.hwnd, allow_focus=True)
        if not img.width or not img.height:
            raise RuntimeError("Game window has no client area (minimized or closing).")
        return img, method

    def screenshot(self, max_width: int = 1024, focus: bool = False) -> tuple[bytes, str]:
        if focus:
            win32.ensure_foreground(self.require_window().hwnd)
        img, method = self.grab()
        cw, ch = img.size
        if cw > max_width:
            img = img.resize((max_width, round(ch * max_width / cw)), Image.Resampling.LANCZOS)
        self.shot_scale = img.width / cw
        info = f"{img.width}x{img.height} (client {cw}x{ch}, via {method})"
        return encode_jpeg(img), info

    def save_screenshot(self, path: Path, max_width: int = 1024) -> str:
        data, info = self.screenshot(max_width)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return info

    # ------------------------------------------------------------------ input

    def _guard(self) -> None:
        w = self.require_window()
        if self.game and self.game.anticheat:
            raise PermissionError("Refusing input: game ships anti-cheat.")
        if not win32.ensure_foreground(w.hwnd):
            raise RuntimeError("Could not bring the game window to the foreground; "
                               "refusing to send input that would land in another app.")

    def _check_still_foreground(self) -> None:
        # If the user alt-tabs away mid-sequence, stop instead of typing into their app.
        if not win32.is_foreground(self.window.hwnd):
            self.release_all()
            raise RuntimeError("Game lost focus mid-sequence (user took over?); "
                               "released all keys and stopped.")

    def _press(self, name: str, up: bool) -> None:
        low = name.strip().lower()
        if low in _MOUSE_ALIASES:  # "left"/"right" alone mean arrow keys
            btn = _MOUSE_ALIASES[low]
            win32.mouse_button(btn, up)
            (self._held_buttons.discard if up else self._held_buttons.add)(btn)
            return
        vk = win32.vk_of(low)
        win32.key_event(vk, up)
        (self._held_keys.discard if up else self._held_keys.add)(vk)

    def tap(self, chord: str, hold: float = 0.06) -> None:
        self._guard()
        self._tap(chord, hold)

    def _tap(self, chord: str, hold: float) -> None:
        keys = [k for k in chord.split("+") if k] if chord != "+" else ["+"]
        for k in keys:
            self._press(k, up=False)
        time.sleep(hold)
        for k in reversed(keys):
            self._press(k, up=True)

    def type_text(self, text: str) -> None:
        self._guard()
        win32.type_unicode(text)

    def move_mouse(self, dx: int, dy: int, duration: float = 0.0) -> None:
        self._guard()
        self._move(dx, dy, duration)

    @staticmethod
    def _move(dx: int, dy: int, duration: float) -> None:
        steps = max(1, int(duration / 0.01)) if duration else max(1, max(abs(dx), abs(dy)) // 200 + 1)
        sent_x = sent_y = 0
        for i in range(1, steps + 1):
            tx, ty = round(dx * i / steps), round(dy * i / steps)
            win32.mouse_move_relative(tx - sent_x, ty - sent_y)
            sent_x, sent_y = tx, ty
            if duration:
                time.sleep(duration / steps)

    def run_steps(self, steps: list[Step]) -> list[str]:
        self._guard()
        log: list[str] = []
        started = time.monotonic()
        try:
            for i, s in enumerate(steps):
                if time.monotonic() - started > MAX_SEQUENCE_SECONDS:
                    raise RuntimeError(f"Sequence exceeded {MAX_SEQUENCE_SECONDS}s; aborted at step {i}.")
                self._check_still_foreground()
                log.append(self._run_step(s))
        except Exception:
            self.release_all()
            raise
        return log

    def _run_step(self, s: Step) -> str:
        if s.key:
            self._tap(s.key, s.hold)
            return f"key {s.key} ({s.hold:.2f}s)"
        if s.down:
            for k in s.down:
                self._press(k, up=False)
            return f"down {s.down}"
        if s.up:
            for k in s.up:
                self._press(k, up=True)
            return f"up {s.up}"
        if s.move:
            self._move(s.move[0], s.move[1], s.duration)
            return f"move {s.move}"
        if s.click:
            if s.at:
                l, t, _, _ = win32.client_rect(self.window.hwnd)
                win32.set_cursor(l + round(s.at[0] / self.shot_scale), t + round(s.at[1] / self.shot_scale))
                time.sleep(0.03)
            self._tap(_CLICK_BUTTONS.get(s.click.lower(), s.click), s.hold)
            return f"click {s.click}" + (f" at {s.at}" if s.at else "")
        if s.text is not None:
            win32.type_unicode(s.text)
            return f"typed {len(s.text)} chars"
        if s.scroll:
            win32.mouse_wheel(s.scroll)
            return f"scroll {s.scroll}"
        if s.wait is not None:
            time.sleep(s.wait)
            return f"wait {s.wait}s"
        return "noop"

    def release_all(self) -> str:
        released = [f"vk{vk:#x}" for vk in self._held_keys] + list(self._held_buttons)
        for vk in list(self._held_keys):
            win32.key_event(vk, up=True)
        for btn in list(self._held_buttons):
            win32.mouse_button(btn, up=True)
        self._held_keys.clear()
        self._held_buttons.clear()
        return ", ".join(released) or "nothing held"
