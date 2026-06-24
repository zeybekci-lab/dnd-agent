"""Offline proof of the combat layer (no API key).

Party (Bram, Mira) vs two bog wretches. Exercises initiative, per-instance HP,
**auto-resolved PC weapon attacks** (no numbers passed — pulled from the sheet),
statblock monster attacks, a creature dropping, **condition-driven advantage and
auto-crits**, round advancement, and end-of-fight HP sync — all through the same
tool dispatch the model uses.
"""

from engine import state, tools
from engine.dice import Dice

SEP = "─" * 78
BRAM = {"class": "fighter", "level": 1, "ac": 16,
        "abilities": {"str": 16, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
        "proficient_skills": ["athletics"], "proficient_saves": ["str", "con"],
        "attacks": [{"name": "Longsword", "ability": "str", "die": "1d8", "proficient": True}]}
MIRA = {"class": "rogue", "level": 1, "ac": 14,
        "abilities": {"str": 9, "dex": 17, "con": 12, "int": 13, "wis": 11, "cha": 14},
        "proficient_skills": ["stealth"], "proficient_saves": ["dex", "int"],
        "attacks": [{"name": "Shortsword", "ability": "dex", "die": "1d6", "proficient": True}]}


def call(conn, cid, name, inp, d, t):
    out = tools.execute(conn, cid, name, inp, dice=d, turn=t)
    print(f"\n▶ {name}({', '.join(f'{k}={v}' for k, v in inp.items())})\n{out}")
    return out


def main():
    conn = state.connect(":memory:")
    cid = state.compile_campaign(conn, "campaign/example_campaign.yaml")
    state.add_pc(conn, cid, "Bram", BRAM, 12, player="human", location="the-lakeshore")
    state.add_pc(conn, cid, "Mira", MIRA, 9, player="AI", location="the-lakeshore")
    d = Dice(seed=5)

    print(f"{SEP}\nget_state now surfaces each PC's AC + computed attacks (#1)\n{SEP}")
    call(conn, cid, "get_state", {}, d, 8)

    print(f"\n{SEP}\nSTART — two bog wretches\n{SEP}")
    call(conn, cid, "start_encounter", {"enemies": [{"monster": "bog-wretch", "count": 2}]}, d, 8)

    print(f"\n{SEP}\nPC attacks auto-resolve from the sheet — no bonus/damage passed (#1)\n{SEP}")
    call(conn, cid, "attack", {"attacker": "Bram", "target": "Bog Wretch 1"}, d, 8)
    call(conn, cid, "attack", {"attacker": "Mira", "target": "Bog Wretch 1"}, d, 8)
    call(conn, cid, "attack", {"attacker": "Bram", "target": "Bog Wretch 1"}, d, 8)

    print(f"\n{SEP}\nconditions auto-apply advantage / auto-crit (#3)\n{SEP}")
    call(conn, cid, "set_condition", {"combatant": "Bog Wretch 2", "condition": "prone"}, d, 8)
    call(conn, cid, "attack", {"attacker": "Bram", "target": "Bog Wretch 2"}, d, 8)        # melee vs prone → advantage
    call(conn, cid, "set_condition", {"combatant": "Bog Wretch 2", "condition": "paralyzed"}, d, 8)
    call(conn, cid, "attack", {"attacker": "Mira", "target": "Bog Wretch 2"}, d, 8)        # paralyzed → advantage + auto-crit

    print(f"\n{SEP}\nmonster strikes back (statblock), then advance the round\n{SEP}")
    call(conn, cid, "attack", {"attacker": "Bog Wretch 2", "target": "Bram"}, d, 8)
    for _ in range(4):
        call(conn, cid, "next_turn", {}, d, 8)
    call(conn, cid, "encounter_status", {}, d, 8)

    print(f"\n{SEP}\nEND — HP syncs back to canon\n{SEP}")
    call(conn, cid, "end_encounter", {}, d, 8)


if __name__ == "__main__":
    main()
