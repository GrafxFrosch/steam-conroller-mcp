# steam-claude-controller

An MCP server that lets Claude **see and play Steam games** on Windows: list and launch
your Steam library, capture the game window, send timed keyboard/mouse input, and, for
Source engine games like Portal, read exact game state and aim to the degree.

Tested end to end on Portal (AppID 400): launched into `testchmb_a_00`, read
position/angles, turned to exact headings, and walked out of the Relaxation Vault through
the portal.

## Why not "the Steam Input API"?

Steam Input (`ISteamInput`) is the API a game uses **to read** controllers. Steam maps the
physical pad to the game's actions (Portal ships `steam_input/action_manifest_portal.vdf`).
No outside program can use it to press buttons in a game, and Steamworks only exists
inside the game process. So the generic layer works one level lower:

| Layer | What it does | Works for |
|---|---|---|
| `steam.py` | Reads `libraryfolders.vdf` / `appmanifest_*.acf`, launches via `steam.exe -applaunch` | every Steam game |
| `win32.py` / `controller.py` | Window capture (`PrintWindow`, screen fallback) + `SendInput` (VK + scancode, raw mouse deltas) | any game that accepts keyboard/mouse |
| `source_engine.py` | Console bridge, `getpos` state, exact aim, pause | Source 1 games (Portal, HL2, ...) |

Engine adapters give Claude *senses* beyond pixels. Adding a game engine means adding
one module like `source_engine.py`.

## The Source console bridge

Portal 1 (Source 2013) has no remote console (`-netconport` doesn't exist there), so:

1. `source_launch` writes `portal/cfg/claude_bridge.cfg` and launches with
   `-condebug -windowed -noborder -w 1280 -h 720 +exec claude_bridge`.
   The bridge binds **INS** to `exec claude_cmd`.
2. To run a command, it writes `claude_cmd.cfg` (command + `echo <nonce>`), taps INS, and
   reads `portal/console.log` up to the nonce.
3. If the bind isn't active (e.g. a menu has focus), it opens the console with `` ` ``,
   types the exec line, and closes it again.

Aiming is exact because `degrees = mouse_counts × sensitivity × m_yaw` with raw input. In
testing, a requested +90° came back as 90.04°.

## Tools

| Tool | Purpose |
|---|---|
| `steam_list_games` | Installed games, Source detection, anti-cheat flag |
| `steam_launch` | Launch any game (with args) and attach to its window |
| `game_attach` | Attach by appid / title / Steam's running app |
| `game_screenshot` | JPEG of the game's client area |
| `game_act` | Timed input sequence (`key`, `down`/`up`, `move`, `click`+`at`, `text`, `scroll`, `wait`), then a screenshot. `freeze_after` pauses Source games while Claude thinks |
| `game_release_all` | Release anything held |
| `source_launch` | Launch a Source game with the bridge, optionally into a map |
| `source_console` | Run console commands, get their output |
| `source_status` | Position, pitch/yaw, map |
| `source_look` | Relative turn by degrees, or absolute heading |
| `source_goto` | Walk to map coordinates with live `getpos` feedback; reports arrived/blocked |
| `source_panorama` | AHEAD/RIGHT/LEFT/BEHIND views in one labelled image |
| `source_pause` | `setpause` / `unpause` |

## Setup

Requires Windows and Steam.

### Claude Desktop (one-click bundle)

Download `steam-controller-<version>.mcpb` from the
[latest release](https://github.com/GrafxFrosch/steam-conroller-mcp/releases/latest) and
open it (or drag it into **Settings → Extensions**). The bundle uses the uv runtime, so
Claude Desktop provides Python and installs the dependencies itself.

### Claude Code (from source)

Requires [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/GrafxFrosch/steam-conroller-mcp.git
cd steam-conroller-mcp
uv sync
```

Claude Code sessions opened in the clone pick up the server from `.mcp.json` (approve it
when prompted). To use it from any folder, register it at user scope with the `claude`
CLI, replacing the path with your clone:

```bash
claude mcp add steam-controller --scope user -- uv run --directory "C:\path\to\steam-conroller-mcp" steam-claude-controller
```

Then ask, for example: *"Launch Portal into testchmb_a_00 and get out of the vault."*

### Building the bundle

```bash
npx @anthropic-ai/mcpb@2.1.2 validate manifest.json
npx @anthropic-ai/mcpb@2.1.2 pack . dist/steam-controller-0.1.0.mcpb
```

`.mcpbignore` keeps `.venv`, `.git` and screenshots out of the bundle.

### CLI for debugging

`uv run scc ...` mirrors the tools without an MCP client:

```bash
uv run scc games
uv run scc --appid 400 source-launch --map testchmb_a_00
uv run scc --appid 400 state
uv run scc --appid 400 look 90 0
uv run scc --appid 400 act "[{\"key\":\"w\",\"hold\":1.0}]" --shot shots/after.jpg
```

## Safety and side effects

- **Focus guard:** input is only sent while the game is the foreground window. If you
  click away mid-sequence, it releases all keys and stops, so nothing types into your apps.
  While Claude plays, the game holds keyboard focus.
- **Anti-cheat refusal:** games with EasyAntiCheat/BattlEye/etc. folders are refused.
  VAC can't be detected from files, so **don't point this at online multiplayer**:
  synthetic input there can get the account banned.
- **Portal config:** the bridge sets `con_enable 1`, `m_rawinput 1`, `m_customaccel 0`,
  `m_filter 0` and binds INS. These persist in `portal/cfg/config.cfg`. `console.log`
  grows while `-condebug` is on. Delete `claude_bridge.cfg` / `claude_cmd.cfg` to remove
  the bridge files.

## Known limits / next steps

- Source games are captured with the engine's own `jpeg` command. After a level change,
  Portal's D3D9 window sometimes stops presenting frames (the window shows a frozen or
  black image) while the game keeps rendering, so window capture can't be trusted there.
  Other games use `PrintWindow`, falling back to a screen grab.
- Walking into a portal off-center pins you against the wall beside it. Center on it
  first (`source_status` + `source_look absolute`).
- Possible additions: a virtual gamepad via ViGEm (so Steam Input sees a real controller),
  a BepInEx bridge for Unity games (KSP, Polytopia), and Windows Graphics Capture for
  occluded D3D11/12 windows.

## License

GPL-3.0, see [LICENSE](LICENSE).
