"""Offline proof: the entire non-LLM substrate, end to end, no API key needed.

Compiles the example campaign into a live SQLite world, then replays the exact
sequence of tool calls the DM model *would* emit during play — proving the
cruncher, canon, mutations, and importance-ranked recall all work. Swap this
scripted caller for engine.dm.run_turn() and a key, and it's a live game.
"""

from engine import state, tools
from engine.dice import Dice

SEP = "─" * 78


def call(conn, cid, name, inp, dice, turn):
    out = tools.execute(conn, cid, name, inp, dice=dice, turn=turn)
    arg = ", ".join(f"{k}={v}" for k, v in inp.items())
    print(f"\n▶ {name}({arg})\n{out}")
    return out


def main():
    conn = state.connect(":memory:")
    cid = state.compile_campaign(conn, "campaign/example_campaign.yaml")
    print(f"Compiled campaign #{cid} into SQLite.")

    # Party created at session start (example campaign ships with pcs: [])
    state.add_pc(conn, cid, "Bram", {
        "class": "fighter", "level": 1, "ac": 16,
        "abilities": {"str": 16, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
        "proficient_skills": ["athletics", "intimidation"], "proficient_saves": ["str", "con"],
    }, max_hp=12, player="human", location="mirefen-village")
    state.add_pc(conn, cid, "Mira", {
        "class": "rogue", "level": 1, "ac": 14,
        "abilities": {"str": 9, "dex": 17, "con": 12, "int": 13, "wis": 11, "cha": 14},
        "proficient_skills": ["stealth", "perception", "persuasion"], "proficient_saves": ["dex", "int"],
    }, max_hp=9, player="AI", location="mirefen-village")

    d = Dice(seed=7)  # seeded so this demo is reproducible

    print(f"\n{SEP}\nTURN 1 — arrival & the hook\n{SEP}")
    call(conn, cid, "get_state", {}, d, 1)
    call(conn, cid, "roll_check", {"actor": "Mira", "check": "insight", "dc": 13}, d, 1)
    call(conn, cid, "update_npc", {"slug": "elsy-marsh", "disposition_delta": 30}, d, 1)
    call(conn, cid, "log_event", {"kind": "decision", "summary": "Party agreed to find Elsy's son Tomas for 50gp",
                                   "entities": ["elsy-marsh"], "location": "mirefen-village", "importance": 0.85}, d, 1)
    call(conn, cid, "log_event", {"kind": "dialogue", "summary": "Mira complimented the inn's stew",
                                   "location": "mirefen-village", "importance": 0.1}, d, 1)

    print(f"\n{SEP}\nTURN 8 — to the lakeshore, low tide, a wretch\n{SEP}")
    call(conn, cid, "set_flag", {"key": "tide", "value": "low"}, d, 8)
    call(conn, cid, "advance_scene", {"scene_slug": "lakeshore-dusk"}, d, 8)
    call(conn, cid, "roll_check", {"actor": "Bram", "check": "athletics", "dc": 12}, d, 8)
    call(conn, cid, "resolve_attack", {"attacker": "Bog Wretch", "target": "Bram",
                                        "attack_bonus": 4, "target_ac": 16, "damage": "1d6+2"}, d, 8)
    call(conn, cid, "apply_damage", {"target": "Bram", "amount": 5}, d, 8)
    call(conn, cid, "log_event", {"kind": "combat", "summary": "A bog wretch ambushed the party at the spire",
                                   "entities": ["bog-wretch"], "location": "the-lakeshore", "importance": 0.7}, d, 8)

    print(f"\n{SEP}\nRECALL — does memory surface the right beats?\n{SEP}")
    call(conn, cid, "recall", {"text": "son"}, d, 9)                       # keyword
    call(conn, cid, "recall", {"entities": ["bog-wretch"]}, d, 9)          # by entity
    call(conn, cid, "recall", {}, d, 9)  # top-ranked overall: important beats beat the trivial stew line

    print(f"\n{SEP}\nFINAL STATE — canon reflects everything that happened\n{SEP}")
    call(conn, cid, "get_state", {}, d, 9)


if __name__ == "__main__":
    main()
