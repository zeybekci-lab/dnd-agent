"""The cruncher — pure, dependency-free D&D 5e rules math.

Rules-as-data in the spirit of LoreKit's system packs, but focused on 5e: the
constants live in DATA tables below, the functions compute derived stats. No
database, no network, no AI, no I/O. Dicts in, values out — trivially testable.

Two-phase use (mirrors LoreKit): call `derive(sheet)` once to compute all the
derived stats, then checks/attacks read those cached values.
"""

from __future__ import annotations

from dataclasses import dataclass

# ─────────────────────────────── 5e data ───────────────────────────────
ABILITIES = ("str", "dex", "con", "int", "wis", "cha")

# skill -> governing ability
SKILLS: dict[str, str] = {
    "athletics": "str",
    "acrobatics": "dex", "sleight_of_hand": "dex", "stealth": "dex",
    "arcana": "int", "history": "int", "investigation": "int", "nature": "int", "religion": "int",
    "animal_handling": "wis", "insight": "wis", "medicine": "wis", "perception": "wis", "survival": "wis",
    "deception": "cha", "intimidation": "cha", "performance": "cha", "persuasion": "cha",
}

CONDITIONS = (
    "blinded", "charmed", "deafened", "frightened", "grappled", "incapacitated",
    "invisible", "paralyzed", "petrified", "poisoned", "prone", "restrained",
    "stunned", "unconscious",
)

# Challenge Rating -> XP (subset used for encounter math)
CR_XP: dict[str, int] = {
    "0": 10, "1/8": 25, "1/4": 50, "1/2": 100, "1": 200, "2": 450, "3": 700,
    "4": 1100, "5": 1800, "6": 2300, "7": 2900, "8": 3900, "9": 5000, "10": 5900,
}

# DC guidance (5e DMG)
DC_BANDS = {"very_easy": 5, "easy": 10, "medium": 15, "hard": 20, "very_hard": 25, "nearly_impossible": 30}


# ─────────────────────────────── core math ───────────────────────────────
def ability_mod(score: int) -> int:
    """5e ability modifier: floor((score - 10) / 2). Python // floors correctly."""
    return (int(score) - 10) // 2


def proficiency_bonus(level: int) -> int:
    """+2 at levels 1-4, +3 at 5-8, +4 at 9-12, +5 at 13-16, +6 at 17-20."""
    return 2 + (max(1, min(20, int(level))) - 1) // 4


# ─────────────────────────────── the character sheet ───────────────────────────────
# A sheet is the JSON dict stored in pc.sheet / monster context. Expected shape:
#   {"class": "fighter", "level": 1,
#    "abilities": {"str":16,"dex":13,"con":14,"int":8,"wis":12,"cha":10},
#    "proficient_skills": ["athletics","intimidation"],
#    "proficient_saves": ["str","con"],
#    "ac": 16, "spellcasting_ability": "int" | null,
#    "expertise_skills": ["stealth"]}   # optional, doubles proficiency


@dataclass
class Derived:
    """Everything computed from a sheet — the cached layer checks read from."""

    level: int
    proficiency: int
    ability_mods: dict[str, int]
    skill_mods: dict[str, int]
    save_mods: dict[str, int]
    passive_perception: int
    initiative: int
    ac: int
    spell_save_dc: int | None
    spell_attack: int | None


def derive(sheet: dict) -> Derived:
    """Compute all derived stats from a raw sheet (the 'recalculate' step)."""
    level = int(sheet.get("level", 1))
    prof = proficiency_bonus(level)
    abilities = {a: int(sheet.get("abilities", {}).get(a, 10)) for a in ABILITIES}
    mods = {a: ability_mod(s) for a, s in abilities.items()}

    prof_skills = set(sheet.get("proficient_skills", []))
    expertise = set(sheet.get("expertise_skills", []))
    skill_mods = {}
    for skill, abil in SKILLS.items():
        m = mods[abil]
        if skill in prof_skills:
            m += prof * (2 if skill in expertise else 1)
        skill_mods[skill] = m

    prof_saves = set(sheet.get("proficient_saves", []))
    save_mods = {a: mods[a] + (prof if a in prof_saves else 0) for a in ABILITIES}

    sca = sheet.get("spellcasting_ability")
    spell_dc = 8 + prof + mods[sca] if sca in ABILITIES else None
    spell_atk = prof + mods[sca] if sca in ABILITIES else None

    return Derived(
        level=level, proficiency=prof, ability_mods=mods, skill_mods=skill_mods,
        save_mods=save_mods,
        passive_perception=10 + skill_mods["perception"],
        initiative=mods["dex"],
        ac=int(sheet.get("ac", 10 + mods["dex"])),
        spell_save_dc=spell_dc, spell_attack=spell_atk,
    )


def check_modifier(derived: Derived, kind: str) -> int:
    """Resolve the modifier for a named check: a skill, an ability ('str'...),
    a save ('str_save'...), or 'initiative'. Raises on unknown kinds."""
    kind = kind.lower().strip()
    if kind == "initiative":
        return derived.initiative
    if kind in derived.skill_mods:
        return derived.skill_mods[kind]
    if kind.endswith("_save") and kind[:-5] in ABILITIES:
        return derived.save_mods[kind[:-5]]
    if kind in derived.ability_mods:
        return derived.ability_mods[kind]
    raise ValueError(f"unknown check '{kind}' (expected a skill, ability, '<ability>_save', or 'initiative')")


# ─────────────────────────────── weapon attacks ───────────────────────────────
# Optional sheet field describing a PC's weapons:
#   "attacks": [{"name": "Longsword", "ability": "str", "die": "1d8", "proficient": true}, ...]
def attack_options(sheet: dict) -> list[dict]:
    """Compute each weapon's to-hit bonus and damage string from the sheet, so the
    DM never has to be told (or invent) a PC's attack math. Falls back to an
    unarmed strike if the sheet lists no weapons."""
    level = int(sheet.get("level", 1))
    prof = proficiency_bonus(level)
    mods = {a: ability_mod(int(sheet.get("abilities", {}).get(a, 10))) for a in ABILITIES}
    out = []
    for w in sheet.get("attacks", []):
        ab = w.get("ability", "str")
        mod = mods.get(ab, 0)
        bonus = mod + (prof if w.get("proficient", True) else 0) + int(w.get("bonus", 0))
        dmg_flat = mod + int(w.get("damage_bonus", 0))  # fighting styles, magic weapons, etc.
        die = w["die"]
        out.append({"name": w.get("name", "attack"), "bonus": bonus,
                    "damage": die if dmg_flat == 0 else f"{die}{dmg_flat:+d}", "ability": ab})
    if not out:
        sm = mods["str"]
        out.append({"name": "unarmed strike", "bonus": prof + sm,
                    "damage": str(max(1, 1 + sm)), "ability": "str"})
    return out


# ─────────────────────────────── modifier stacking ───────────────────────────────
# 5e is mostly "sum everything", with a few non-stacking cases (e.g. cover,
# the same named bonus from the same source). We group by `kind` and take the
# best within a kind, summing across kinds — the LoreKit-style generic resolver,
# trimmed to what 5e actually needs. Untyped modifiers all stack.
def stack_modifiers(mods: list[tuple[int, str | None]]) -> int:
    """mods: list of (value, kind). Same-kind take max; untyped (None) all sum."""
    by_kind: dict[str | None, list[int]] = {}
    for value, kind in mods:
        by_kind.setdefault(kind, []).append(value)
    total = 0
    for kind, values in by_kind.items():
        total += sum(values) if kind is None else max(values)
    return total


if __name__ == "__main__":
    bram = {
        "class": "fighter", "level": 5,
        "abilities": {"str": 16, "dex": 13, "con": 14, "int": 8, "wis": 12, "cha": 10},
        "proficient_skills": ["athletics", "intimidation"],
        "proficient_saves": ["str", "con"], "ac": 18,
    }
    d = derive(bram)
    assert d.proficiency == 3, d.proficiency
    assert d.ability_mods["str"] == 3
    assert d.skill_mods["athletics"] == 3 + 3, d.skill_mods["athletics"]   # str mod + prof
    assert d.save_mods["con"] == 2 + 3                                      # con mod + prof
    assert d.passive_perception == 10 + 1                                   # 10 + wis mod
    assert check_modifier(d, "athletics") == 6
    assert check_modifier(d, "con_save") == 5
    assert stack_modifiers([(2, "cover"), (5, "cover"), (3, None), (1, None)]) == 5 + 4
    print("rules.py self-test passed:", d)
