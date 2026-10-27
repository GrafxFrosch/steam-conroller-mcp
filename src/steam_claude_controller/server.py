"""MCP server exposing Steam library, game vision/input and the Source engine adapter."""
from __future__ import annotations

import functools
import json
import time

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import steam
from .controller import Controller, Step, encode_jpeg
from .source_engine import SourceGame

INSTRUCTIONS = """\
Tools to see and play Steam games on this Windows PC.

Loop: look (game_screenshot) -> act in short bursts (game_act, which returns a fresh
screenshot) -> look again. Input only goes to the attached game while it is the
foreground window; if the user clicks away, sequences abort and all keys are released.

Source engine games (Portal, Half-Life 2, ...): start them with source_launch so the
console bridge works. Then source_status gives exact position/view angles, source_look
turns by exact degrees, source_goto walks to map coordinates, source_console runs
console commands, and game_act's
freeze_after=true pauses the world while you think. Default Source keys: wasd move,
space jump, ctrl crouch, e use, mouse1/mouse2 primary/secondary fire (Portal: blue/orange
portal).

Games that ship anti-cheat are refused: never use these tools for online multiplayer.
"""

mcp = MCPServer("steam-controller", instructions=INSTRUCTIONS)
ctrl = Controller()
_source: SourceGame | None = None

# Failures the model can act on. Anything else stays a crash (logged with traceback).
_EXPECTED = (RuntimeError, LookupError, PermissionError, OSError, ValueError)


def tool(fn):
    """Register a tool whose expected failures reach the model as readable errors
    (MCPServer otherwise replaces them with a bare "Error executing tool")."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
    return mcp.tool(structured_output=False)(wrapper)


def _shot(max_width: int, prefix: str = "") -> list:
    data, info = ctrl.screenshot(max_width)
    return [f"{prefix}{ctrl.describe()} | screenshot {info}", Image(data=data, format="jpeg")]


def _source_game() -> SourceGame:
    global _source
    ctrl.require_window()
    game = ctrl.game
    if game is None or not SourceGame.is_source(game.install_dir):
        raise RuntimeError("The attached game is not a Source engine game.")
    if _source is None or _source.install_dir != game.install_dir:
        _source = SourceGame(game.install_dir)
    return _source


def _attached() -> None:
    """After (re)attaching: Source games get engine-side screenshots."""
    game = ctrl.game
    if game and SourceGame.is_source(game.install_dir):
        src = _source_game()
        ctrl.capture_hook = lambda: src.capture(ctrl)
    else:
        ctrl.capture_hook = None


# --------------------------------------------------------------------------- steam

@tool
def steam_list_games() -> str:
    """List installed Steam games: appid, name, install dir, whether it is a Source
    engine game, and any detected anti-cheat (such games are refused)."""
    rows = []
    for g in steam.installed_games():
        d = g.to_dict()
        d["source_engine"] = SourceGame.is_source(g.install_dir)
        rows.append(d)
    return json.dumps({"running_appid": steam.running_appid(), "games": rows}, indent=1)


@tool
def steam_launch(appid: int, args: list[str] | None = None, wait_seconds: float = 90) -> str:
    """Launch a game through Steam (optionally with launch options) and attach to its
    window once it appears. For Source engine games prefer source_launch."""
    game = steam.find_game(appid)
    if game.anticheat:
        raise PermissionError(f"{game.name} ships anti-cheat ({game.anticheat}); refusing.")
    steam.launch(appid, args)
    win = ctrl.attach(appid, timeout=wait_seconds)
    _attached()
    return f"Launched and attached: {ctrl.describe()} (hwnd {win.hwnd})"


# --------------------------------------------------------------------------- generic game

@tool
def game_attach(appid: int | None = None, title: str | None = None) -> str:
    """Attach to a running game window by Steam appid or window-title substring.
    With neither, attaches to whatever Steam reports as running."""
    ctrl.attach(appid, title)
    _attached()
    return f"Attached: {ctrl.describe()}"


@tool
def game_screenshot(max_width: int = 1024) -> list:
    """Capture the attached game's window (works even if it is covered). Coordinates in
    this image are what game_act's `at` expects."""
    return _shot(max_width)


@tool
def game_act(steps: list[Step], screenshot: bool = True, settle: float = 0.15,
             freeze_after: bool = False, max_width: int = 1024) -> list:
    """Run a timed input sequence in the attached game, then (by default) screenshot.

    Each step sets ONE action: key (tap/hold, chords like "shift+w"), down/up (keep keys or
    "mouse1"/"mouse2" held across steps), move (relative raw mouse counts, + = right/down),
    click (+ optional `at` in screenshot pixels, for menus), text, scroll, wait.
    Example - walk forward 1.2 s while turning right, then jump:
      [{"down":["w"]},{"move":[400,0],"duration":0.6},{"wait":0.6},{"up":["w"]},{"key":"space"}]
    freeze_after (Source games only): unpause before the steps and pause after them.
    """
    src = _source_game() if freeze_after else None
    if src:
        src.run(ctrl, "unpause")
    log = ctrl.run_steps(steps)
    if src:
        src.run(ctrl, "setpause")
    summary = "Done: " + "; ".join(log)
    if not screenshot:
        return [summary]
    time.sleep(settle)
    return _shot(max_width, summary + "\n")


@tool
def game_release_all() -> str:
    """Release every key/mouse button this server is holding down."""
    return "Released: " + ctrl.release_all()


# --------------------------------------------------------------------------- source engine

@tool
def source_launch(appid: int, map: str | None = None, save: str | None = None,
                  width: int = 1280, height: int = 720, mod: str | None = None,
                  wait_seconds: float = 120) -> str:
    """Launch a Source engine game windowed with the console bridge enabled
    (-condebug, INS bound to exec claude_cmd), optionally straight into `map`
    (Portal's first chamber is testchmb_a_00) or a named `save` (made with
    source_console "save <name>"), and attach to it."""
    global _source
    game = steam.find_game(appid)
    if not SourceGame.is_source(game.install_dir):
        raise RuntimeError(f"{game.name} does not look like a Source engine game.")
    _source = SourceGame(game.install_dir, mod)
    _source.write_bridge()
    steam.launch(appid, _source.launch_args(width, height, map, save))
    ctrl.attach(appid, timeout=wait_seconds)
    _attached()
    return (f"Launched {game.name} (mod dir {_source.mod_dir.name}); attached to "
            f"{ctrl.describe()}. Wait for loading to finish before acting.")


@tool
def source_console(command: str, timeout: float = 3.0) -> str:
    """Run Source console command(s) (separate with newlines or ';') and return the
    console output they produced."""
    return _source_game().run(ctrl, command, timeout) or "(no output)"


@tool
def source_status() -> str:
    """Exact player position [x,y,z], view angles (pitch: + = down; yaw: degrees,
    counter-clockwise from +X) and current map."""
    return json.dumps(_source_game().state(ctrl))


@tool
def source_look(yaw: float = 0.0, pitch: float = 0.0, absolute: bool = False,
                duration: float = 0.0) -> str:
    """Turn the view. Relative (default): +yaw turns LEFT, -yaw turns right, +pitch looks
    down, by exact degrees. absolute=true: face the given yaw/pitch as reported by
    source_status."""
    src = _source_game()
    if absolute:
        return "Now: " + json.dumps(src.aim(ctrl, yaw, pitch))
    dx, dy = src.turn(ctrl, yaw, pitch, duration)
    return f"Turned yaw {yaw:+.1f} deg, pitch {pitch:+.1f} deg (mouse {dx}, {dy})"


@tool
def source_panorama(pitch: float = 0.0) -> list:
    """Look around in one call: a 2x2 grid of the views AHEAD | RIGHT / LEFT | BEHIND,
    each labelled with its absolute yaw, at the given pitch. Ends facing ahead again."""
    grid, st = _source_game().panorama(ctrl, pitch)
    return [json.dumps(st), Image(data=encode_jpeg(grid), format="jpeg")]


@tool
def source_goto(x: float, y: float, tolerance: float = 12.0, timeout: float = 20.0) -> str:
    """Walk to map coordinates (x, y) from source_status, steering with live getpos
    feedback. Returns the final status with result "arrived", "blocked" (something in
    the way) or "timeout". Turns smoothly, so it is safe while carrying an object."""
    return json.dumps(_source_game().goto(ctrl, x, y, tolerance, timeout))


@tool
def source_pause(paused: bool = True) -> str:
    """Freeze (true) or resume (false) the game world without opening a menu."""
    return _source_game().run(ctrl, "setpause" if paused else "unpause") or ("paused" if paused else "resumed")


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
