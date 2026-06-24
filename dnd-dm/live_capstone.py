"""Live capstone: Opus 4.8 drives a zoned fight using ALL the new combat tools —
zones + movement, hands-off attacks, spellcasting, and a mid-fight rules lookup.
"""

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
for line in (ROOT / ".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from engine import dm, state

DIM, BOLD, CYAN, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[0m"


def on_tool(name, inp, out):
    print(f"{DIM}    · {name} {dict(inp) if inp else ''} → {out.splitlines()[0]}{RESET}")


def main():
    conn = state.connect(":memory:")
    cid = state.compile_campaign(conn, str(ROOT / "campaign" / "example_campaign.yaml"))
    state.add_pc(conn, cid, "Bram", {
        "class": "fighter", "level": 1, "ac": 16,
        "abilities": {"str": 16, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
        "proficient_saves": ["str", "con"],
        "attacks": [{"name": "Longsword", "ability": "str", "die": "1d8", "proficient": True}]}, 12,
        player="human", location="the-lakeshore")
    state.add_pc(conn, cid, "Wisp", {
        "class": "wizard", "level": 3, "ac": 12,
        "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 11, "cha": 10},
        "proficient_saves": ["int", "wis"], "spellcasting_ability": "int",
        "attacks": [{"name": "Dagger", "ability": "dex", "die": "1d4", "proficient": True}]}, 18,
        player="AI", location="the-lakeshore")
    state.advance_scene(conn, cid, "lakeshore-dusk")
    state.set_flag(conn, cid, "tide", "low")

    client = dm.make_client()
    history, turn = [], 1
    turns = [
        "We're out on the mudflat at low tide when a bog wretch claws its way up onto the spire stair "
        "above us — maybe twenty feet off, out of sword reach. Start the fight: the wretch is up on the "
        "stair, Bram and Wisp are down on the mudflat.",
        "Wisp hurls a Fire Bolt up at the wretch from the mudflat. Bram scrambles up toward the stair to "
        "get into reach.",
        "Before Bram swings — quick question, since the wretch is up on that higher stair, does its "
        "position give it any benefit against us? Then Bram attacks it.",
    ]

    for player in turns:
        print(f"\n{BOLD}{'═'*78}\nPLAYER: {player}{RESET}\n")
        text, history = dm.run_turn(conn, cid, client, history, player, turn=turn, effort="medium", on_tool=on_tool)
        print(f"\n{CYAN}{text}{RESET}")
        turn += 1

    from engine.tools import execute
    print(f"\n{BOLD}{'─'*78}\nfinal board{RESET}")
    print(execute(conn, cid, "encounter_status", {}))


if __name__ == "__main__":
    main()
