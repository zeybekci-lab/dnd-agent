#!/usr/bin/env python3
"""Play the campaign.  `python play.py`

Type actions at the prompt. The dim `·` lines show the tools/dice firing behind
the prose; in combat the initiative board prints after each turn. Slash commands:

  /status        the current scene, party, flags
  /rules <q>     look up a 5e rule (e.g. /rules grappling)
  /recap         recent events from memory
  /board         the combat status (when fighting)
  /help          this list
  /quit          leave (the game is saved to data/game.db)
"""

from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

from engine import dm, state, tools  # after env so the SDK sees the key

DIM, BOLD, CYAN, YELLOW, GREEN, RESET = (
    "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[32m", "\033[0m")

PARTY = [
    ("Mala", {"class": "paladin", "level": 1, "ac": 16, "speed": 30, "pronouns": "he/him",
              "abilities": {"str": 17, "dex": 12, "con": 14, "int": 11, "wis": 11, "cha": 16},
              "proficient_skills": ["athletics", "history", "insight", "persuasion", "religion"],
              "proficient_saves": ["wis", "cha"],
              "attacks": [{"name": "Greatsword", "ability": "str", "die": "2d6", "proficient": True},
                          {"name": "Javelin", "ability": "str", "die": "1d6", "proficient": True}],
              "hit_dice": {"die": "d10", "count": 1},
              "features": [
                  {"name": "Divine Sense", "uses": 4, "recharge": "long", "use": "action",
                   "text": "Detect celestials, fiends, and undead within 60 ft (not behind total cover) until the end of your next turn."},
                  {"name": "Lay on Hands", "use": "pool of 5 HP · 1/long rest",
                   "text": "A pool of 5 hit points of healing. As an action, touch a creature to restore HP from the pool, or spend 5 to cure a disease or neutralize a poison."},
                  {"name": "Stone's Endurance", "uses": 1, "recharge": "short", "use": "reaction",
                   "text": "When you take damage, reduce it by 1d12 + 2 (Constitution). Once per short rest."},
                  {"name": "Powerful Build", "use": "passive",
                   "text": "Count as one size larger for carrying capacity and push/drag/lift."},
                  {"name": "Mountain Born", "use": "passive",
                   "text": "Resistance to cold damage; no penalties from high altitude."}]}, 12, "human"),
    ("Mira", {"class": "rogue", "level": 1, "ac": 14, "pronouns": "she/her",
              "abilities": {"str": 9, "dex": 17, "con": 12, "int": 13, "wis": 11, "cha": 14},
              "proficient_skills": ["stealth", "perception", "persuasion", "acrobatics", "investigation"],
              "expertise_skills": ["stealth", "perception"], "proficient_saves": ["dex", "int"],
              "attacks": [{"name": "Shortsword", "ability": "dex", "die": "1d6", "proficient": True},
                          {"name": "Shortbow", "ability": "dex", "die": "1d6", "proficient": True}],
              "hit_dice": {"die": "d8", "count": 1},
              "features": [
                  {"name": "Sneak Attack (1d6)", "use": "once per turn",
                   "text": "+1d6 damage on an attack made with advantage, or when an ally is within 5 ft of the target (finesse or ranged weapon)."},
                  {"name": "Expertise", "use": "passive", "text": "Double proficiency on Stealth and Perception."},
                  {"name": "Thieves' Cant", "use": "passive", "text": "A secret rogue language of signs and slang."}]}, 9, "AI"),
]


def bootstrap(db_path: str):
    conn = state.connect(db_path)
    row = conn.execute("SELECT id FROM campaign LIMIT 1").fetchone()
    if row:
        return conn, row["id"]
    cid = state.compile_campaign(conn, str(ROOT / "campaign" / "example_campaign.yaml"))
    for name, sheet, hp, player in PARTY:
        state.add_pc(conn, cid, name, sheet, hp, player=player, location="mirefen-village")
    return conn, cid


def on_tool(name, inp, out):
    print(f"{DIM}  · {name}: {out.splitlines()[0] if out else ''}{RESET}")


def handle_command(conn, cid, line: str) -> bool:
    """Return True if the line was a slash command (and handled)."""
    if not line.startswith("/"):
        return False
    cmd, _, arg = line[1:].partition(" ")
    cmd = cmd.lower()
    if cmd in ("quit", "exit", "q"):
        raise EOFError
    if cmd == "help":
        print(YELLOW + __doc__.split("Slash commands:")[1].strip() + RESET)
    elif cmd == "status":
        print(tools.execute(conn, cid, "get_state", {}))
    elif cmd == "board":
        print(tools.execute(conn, cid, "encounter_status", {}))
    elif cmd == "recap":
        print(tools.execute(conn, cid, "recall", {}))
    elif cmd == "rules":
        print(tools.execute(conn, cid, "consult_rules", {"query": arg or "combat"}))
    else:
        print(f"{YELLOW}unknown command /{cmd} — try /help{RESET}")
    return True


def main():
    db = ROOT / "data" / "game.db"
    db.parent.mkdir(exist_ok=True)
    conn, cid = bootstrap(str(db))
    client = dm.make_client()
    history, turn = [], 1

    camp = conn.execute("SELECT title FROM campaign WHERE id=?", (cid,)).fetchone()
    print(f"{BOLD}{camp['title']}{RESET}  {DIM}(effort=medium; /help for commands){RESET}\n")
    print(tools.execute(conn, cid, "get_state", {}) + "\n")

    while True:
        try:
            line = input(f"{BOLD}> {RESET}").strip()
        except EOFError:
            break
        if not line:
            continue
        try:
            if handle_command(conn, cid, line):
                continue
        except EOFError:
            break
        try:
            text, history = dm.run_turn(conn, cid, client, history, line,
                                        turn=turn, effort="medium", on_tool=on_tool)
        except Exception as e:
            print(f"{DIM}[error: {type(e).__name__}: {e}]{RESET}")
            continue
        print(f"\n{CYAN}{text}{RESET}\n")
        if state.current_session(conn, cid)["in_combat"]:
            print(GREEN + tools.execute(conn, cid, "encounter_status", {}) + RESET + "\n")
        turn += 1

    conn.close()
    print(f"\n{DIM}Saved to {db}.{RESET}")


if __name__ == "__main__":
    main()
