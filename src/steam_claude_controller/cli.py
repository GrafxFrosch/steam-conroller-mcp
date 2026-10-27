"""Command-line twin of the MCP tools, for debugging without an MCP client.

    scc games
    scc --appid 400 source-launch --map testchmb_a_00
    scc --appid 400 shot shots/now.jpg
    scc --appid 400 act '[{"key":"w","hold":1.0}]'
    scc --appid 400 console "getpos"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import steam
from .controller import Controller, Step
from .source_engine import SourceGame


def main() -> None:
    p = argparse.ArgumentParser(prog="scc")
    p.add_argument("--appid", type=int, help="target game (default: Steam's running app)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("games")
    sl = sub.add_parser("source-launch")
    sl.add_argument("--map")
    sl.add_argument("--size", default="1280x720")
    sub.add_parser("shot").add_argument("out", type=Path)
    a = sub.add_parser("act")
    a.add_argument("steps", help="JSON list of steps")
    a.add_argument("--shot", type=Path)
    sub.add_parser("console").add_argument("command")
    sub.add_parser("state")
    lk = sub.add_parser("look")
    lk.add_argument("yaw", type=float)
    lk.add_argument("pitch", type=float)
    lk.add_argument("--abs", action="store_true")
    sub.add_parser("pause").add_argument("state", choices=["on", "off"])
    args = p.parse_args()

    if args.cmd == "games":
        for g in steam.installed_games():
            tag = " [source]" if SourceGame.is_source(g.install_dir) else ""
            ac = f" [anti-cheat: {g.anticheat}]" if g.anticheat else ""
            print(f"{g.appid:>8}  {g.name}{tag}{ac}")
        return

    ctrl = Controller()
    if args.cmd == "source-launch":
        game = steam.find_game(args.appid)
        src = SourceGame(game.install_dir)
        src.write_bridge()
        w, h = (int(v) for v in args.size.split("x"))
        steam.launch(args.appid, src.launch_args(w, h, args.map))
        ctrl.attach(args.appid, timeout=120)
        print("attached:", ctrl.describe())
        return

    ctrl.attach(args.appid)
    src = SourceGame(ctrl.game.install_dir) if ctrl.game and SourceGame.is_source(ctrl.game.install_dir) else None
    if args.cmd == "shot":
        print(ctrl.save_screenshot(args.out))
    elif args.cmd == "act":
        steps = [Step(**s) for s in json.loads(args.steps)]
        print("; ".join(ctrl.run_steps(steps)))
        if args.shot:
            print(ctrl.save_screenshot(args.shot))
    elif args.cmd == "console":
        print(src.run(ctrl, args.command))
    elif args.cmd == "state":
        print(json.dumps(src.state(ctrl)))
    elif args.cmd == "look":
        print(json.dumps(src.aim(ctrl, args.yaw, args.pitch)) if args.abs
              else src.turn(ctrl, args.yaw, args.pitch))
    elif args.cmd == "pause":
        print(src.run(ctrl, "setpause" if args.state == "on" else "unpause"))


if __name__ == "__main__":
    main()
