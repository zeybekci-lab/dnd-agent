"""Live combat: let Opus 4.8 actually drive a fight through the combat tools.

Sets the party at the lakeshore with a wretch rising, then feeds a few scripted
player actions and prints every tool the model calls plus its narration.
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
    print(f"{DIM}    · {name}{(' ' + str(inp)) if inp else ''} → {out.splitlines()[0]}{RESET}")


def main():
    conn = state.connect(":memory:")
    cid = state.compile_campaign(conn, str(ROOT / "campaign" / "example_campaign.yaml"))
    state.add_pc(conn, cid, "Bram", {
        "class": "fighter", "level": 1, "ac": 16,
        "abilities": {"str": 16, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
        "proficient_skills": ["athletics"], "proficient_saves": ["str", "con"]}, 12,
        player="human", location="the-lakeshore")
    state.add_pc(conn, cid, "Mira", {
        "class": "rogue", "level": 1, "ac": 14,
        "abilities": {"str": 9, "dex": 17, "con": 12, "int": 13, "wis": 11, "cha": 14},
        "proficient_skills": ["stealth", "perception"], "proficient_saves": ["dex", "int"]}, 9,
        player="AI", location="the-lakeshore")
    # put the party at the climax scene, low tide (combat is imminent there)
    state.advance_scene(conn, cid, "lakeshore-dusk")
    state.set_flag(conn, cid, "tide", "low")

    client = dm.make_client()
    history, turn = [], 1
    player_turns = [
        "We've waded out to the spire at low tide. As the unheard bell shudders the air, a bog wretch "
        "heaves up out of the mud right in front of us. Bram plants himself between it and Mira — we're "
        "fighting. Start the encounter and set the scene.",
        "Bram hacks at the wretch with his longsword (+5 to hit, 1d8+3 slashing). Mira slips to its flank "
        "and stabs with her shortsword (+5 to hit, 1d6+3 piercing).",
        "No mercy — we finish the wretch off before it can drag either of us under.",
    ]

    for player in player_turns:
        print(f"\n{BOLD}{'═'*78}\nPLAYER: {player}{RESET}\n")
        text, history = dm.run_turn(conn, cid, client, history, player, turn=turn, effort="medium", on_tool=on_tool)
        print(f"\n{CYAN}{text}{RESET}")
        turn += 1

    print(f"\n{BOLD}{'─'*78}\nfinal state{RESET}")
    from engine.tools import execute
    print(execute(conn, cid, "encounter_status", {}))
    print(execute(conn, cid, "get_state", {}).split("FLAGS")[0])


if __name__ == "__main__":
    main()
