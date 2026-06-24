"""Combat — initiative, combatant instances, turn order, attacks, conditions.

Theater-of-mind for v1 (no grid/zones). Builds on the cruncher (PC stats), the
dice (referee), and the canon (monster statblocks). Monster *instances* each
track their own HP in the `combatant` table, so three goblins die independently.
The model drives all of this only through the combat tools in tools.py; every
roll, hit, and HP change happens here, never in the model.
"""

from __future__ import annotations

import json

from engine import rules, state
from engine.dice import Dice, ability_check, attack_roll

DOWN_TAGS = ("dead", "unconscious")

# Condition → attack-roll effects (5e, the combat-relevant subset).
TARGET_GRANTS_ADV = {"restrained", "stunned", "paralyzed", "unconscious", "blinded"}
ATTACKER_DISADV = {"poisoned", "restrained", "frightened", "blinded", "prone"}
AUTO_CRIT_TARGET = {"paralyzed", "unconscious"}  # a hit from within melee is an automatic crit


# ─────────────────────────────── lookups ───────────────────────────────
def _statblock(conn, monster_id: int) -> dict:
    row = conn.execute("SELECT statblock FROM monster WHERE id=?", (monster_id,)).fetchone()
    return json.loads(row["statblock"]) if row else {}


def _resolve(conn, session_id: int, name: str):
    return conn.execute(
        "SELECT * FROM combatant WHERE session_id=? AND lower(name)=lower(?)", (session_id, name)
    ).fetchone()


def _ac(conn, comb) -> int:
    if comb["ref_type"] == "pc":
        pc = conn.execute("SELECT sheet FROM pc WHERE id=?", (comb["ref_id"],)).fetchone()
        return rules.derive(json.loads(pc["sheet"])).ac
    return int(_statblock(conn, comb["ref_id"]).get("ac", 10))


def _save_mod(conn, comb, ability: str) -> int:
    if comb["ref_type"] == "pc":
        sheet = json.loads(conn.execute("SELECT sheet FROM pc WHERE id=?", (comb["ref_id"],)).fetchone()["sheet"])
        return rules.derive(sheet).save_mods.get(ability, 0)
    sb = _statblock(conn, comb["ref_id"])
    if ability in sb.get("saves", {}):
        return int(sb["saves"][ability])
    abil = sb.get("abilities", {})
    return rules.ability_mod(int(abil[ability])) if ability in abil else 0


def _caster_stats(conn, comb):
    """(spell_save_dc, spell_attack) for a combatant, or (None, None) if no caster."""
    if comb["ref_type"] == "pc":
        der = rules.derive(json.loads(conn.execute("SELECT sheet FROM pc WHERE id=?", (comb["ref_id"],)).fetchone()["sheet"]))
        return der.spell_save_dc, der.spell_attack
    sb = _statblock(conn, comb["ref_id"])
    return sb.get("spell_dc"), sb.get("spell_attack")


def _conds(comb) -> list[str]:
    return list(json.loads(comb["conditions"] or "[]"))


def get_round(conn, campaign_id: int) -> int:
    return state.current_session(conn, campaign_id)["combat_round"]


# ─────────────────────────────── lifecycle ───────────────────────────────
def start_encounter(conn, campaign_id: int, enemies: list[dict], *, dice: Dice,
                    zones: list[str] | None = None, placements: dict[str, str] | None = None) -> str:
    """Spawn combatants (party + monster instances), roll initiative, begin.
    enemies: [{"monster": "<slug>", "count": N}, ...] referencing campaign monsters.
    zones: optional ordered list of theater-of-mind zones (adjacent = neighbours in
    the list); defaults to one shared zone. placements: {combatant_name: zone}."""
    sess = state.current_session(conn, campaign_id)
    sid = sess["id"]
    conn.execute("DELETE FROM combatant WHERE session_id=?", (sid,))
    zone_list = [z for z in (zones or []) if z] or ["the area"]
    placements = placements or {}
    default_zone = zone_list[0]
    ins = ("INSERT INTO combatant (session_id, name, side, ref_type, ref_id, initiative,"
           " current_hp, max_hp, conditions, zone) VALUES (?,?,?,?,?,?,?,?,'[]',?)")

    for pc in state.party(conn, campaign_id):
        d = rules.derive(json.loads(pc["sheet"]))
        init = dice.d20(d.initiative).total
        conn.execute(ins, (sid, pc["name"], "party", "pc", pc["id"], init, pc["current_hp"],
                           pc["max_hp"], placements.get(pc["name"], default_zone)))

    for e in enemies:
        slug = e["monster"]
        m = conn.execute("SELECT * FROM monster WHERE campaign_id=? AND slug=?", (campaign_id, slug)).fetchone()
        if m is None:
            raise KeyError(f"monster '{slug}'")
        sb = json.loads(m["statblock"])
        count = int(e.get("count", 1))
        hp = int(sb.get("hp", 1))
        for i in range(count):
            name = m["name"] if count == 1 else f"{m['name']} {i + 1}"
            init = dice.d20(int(sb.get("dex_mod", 0))).total
            conn.execute(ins, (sid, name, "enemy", "monster", m["id"], init, hp, hp,
                               placements.get(name, default_zone)))

    # Allied companions auto-join on the party's side if they can fight (have a stat block) and are alive.
    for comp in state.list_companions(conn, campaign_id):
        if not comp["stat_block"]:
            continue
        cm = conn.execute("SELECT * FROM monster WHERE campaign_id=? AND slug=?", (campaign_id, comp["stat_block"])).fetchone()
        if cm is None:
            continue
        csb = json.loads(cm["statblock"])
        cmax = int(comp["max_hp"] or csb.get("hp", 1))
        ccur = int(comp["hp"]) if comp["hp"] is not None else cmax
        if ccur <= 0:
            continue
        cinit = dice.d20(int(csb.get("dex_mod", 0))).total
        conn.execute(ins, (sid, comp["name"], "party", "monster", cm["id"], cinit, ccur, cmax,
                           placements.get(comp["name"], default_zone)))

    conn.execute("UPDATE game_session SET in_combat=1, combat_round=1, combat_zones=? WHERE id=?",
                 (json.dumps(zone_list), sid))
    conn.commit()
    return status(conn, campaign_id)


def move(conn, campaign_id: int, name: str, target_zone: str) -> str:
    """Move a combatant to an adjacent zone (neighbours in the encounter's zone
    list). One move per turn is assumed; budget isn't tracked in v1."""
    sess = state.current_session(conn, campaign_id)
    if not sess["in_combat"]:
        raise ValueError("no active encounter")
    zone_list = json.loads(sess["combat_zones"]) if sess["combat_zones"] else ["the area"]
    if target_zone not in zone_list:
        raise ValueError(f"no zone '{target_zone}' (zones: {', '.join(zone_list)})")
    comb = _resolve(conn, sess["id"], name)
    if comb is None:
        raise KeyError(f"combatant '{name}'")
    if comb["zone"] and comb["zone"] in zone_list and abs(zone_list.index(comb["zone"]) - zone_list.index(target_zone)) > 1:
        raise ValueError(f"'{target_zone}' is not adjacent to '{comb['zone']}' — move one zone at a time")
    conn.execute("UPDATE combatant SET zone=? WHERE id=?", (target_zone, comb["id"]))
    conn.commit()
    return f"{comb['name']} moves to {target_zone}."


def companion_side(conn, campaign_id: int, name: str):
    """The 'party' / 'enemy' side of a combatant (used to play allies vs monsters correctly)."""
    sess = state.current_session(conn, campaign_id)
    if not sess:
        return None
    r = conn.execute("SELECT side FROM combatant WHERE session_id=? AND name=?", (sess["id"], name)).fetchone()
    return r["side"] if r else None


def add_companion_to_combat(conn, campaign_id: int, name: str, *, dice: Dice) -> str:
    """Bring an allied companion into the current fight on the party's side, using its stat block
    (Sildar, a goblin ally, …). Mechanically it's a stat-block instance with side='party'."""
    sess = state.current_session(conn, campaign_id)
    if not sess or not sess["in_combat"]:
        raise ValueError("not in combat — start_encounter first")
    sid = sess["id"]
    comp = state.get_companion(conn, campaign_id, name)
    if comp is None:
        raise KeyError(f"no companion '{name}'")
    if not comp["stat_block"]:
        raise ValueError(f"{name} has no stat_block — set one via recruit_companion to let them fight")
    if conn.execute("SELECT 1 FROM combatant WHERE session_id=? AND name=?", (sid, name)).fetchone():
        return status(conn, campaign_id)
    m = conn.execute("SELECT * FROM monster WHERE campaign_id=? AND slug=?", (campaign_id, comp["stat_block"])).fetchone()
    if m is None:
        raise KeyError(f"stat block '{comp['stat_block']}' not found")
    sb = json.loads(m["statblock"])
    max_hp = int(comp["max_hp"] or sb.get("hp", 1))
    cur_hp = int(comp["hp"]) if comp["hp"] is not None else max_hp
    init = dice.d20(int(sb.get("dex_mod", 0))).total
    zones = json.loads(sess["combat_zones"]) if sess["combat_zones"] else ["the area"]
    conn.execute("INSERT INTO combatant (session_id, name, side, ref_type, ref_id, initiative,"
                 " current_hp, max_hp, conditions, zone) VALUES (?,?,?,?,?,?,?,?,'[]',?)",
                 (sid, name, "party", "monster", m["id"], init, cur_hp, max_hp, zones[0]))
    conn.commit()
    return status(conn, campaign_id)


def end_encounter(conn, campaign_id: int) -> str:
    """Resolve the fight: PC HP is already synced to the canon; clear instances."""
    sess = state.current_session(conn, campaign_id)
    # Carry surviving allied companions' HP back to their persistent record before clearing combat.
    for c in conn.execute("SELECT name, current_hp FROM combatant WHERE session_id=? AND side='party' AND ref_type!='pc'",
                          (sess["id"],)).fetchall():
        if state.get_companion(conn, campaign_id, c["name"]):
            state.set_companion_hp(conn, campaign_id, c["name"], c["current_hp"])
    survivors = conn.execute(
        "SELECT name, side, current_hp FROM combatant WHERE session_id=? AND current_hp>0", (sess["id"],)
    ).fetchall()
    line = "Encounter ended. Standing: " + (", ".join(f"{s['name']} ({s['side']})" for s in survivors) or "none")
    conn.execute("DELETE FROM combatant WHERE session_id=?", (sess["id"],))
    conn.execute("UPDATE game_session SET in_combat=0, combat_round=0 WHERE id=?", (sess["id"],))
    conn.commit()
    return line


# ─────────────────────────────── turn order ───────────────────────────────
def _ordered(conn, sid: int):
    return conn.execute(
        "SELECT * FROM combatant WHERE session_id=? ORDER BY initiative DESC, id ASC", (sid,)
    ).fetchall()


def _death_saves(comb) -> dict:
    try:
        return json.loads(comb["death_saves"]) if comb["death_saves"] else {"s": 0, "f": 0}
    except (KeyError, TypeError, ValueError):
        return {"s": 0, "f": 0}


def is_dying(comb) -> bool:
    """A PC at 0 HP who is still making death saves (down, not stable, not dead)."""
    conds = set(_conds(comb))
    return comb["ref_type"] == "pc" and comb["current_hp"] <= 0 \
        and "dead" not in conds and "stable" not in conds


def current_turn(conn, campaign_id: int):
    sid = state.current_session(conn, campaign_id)["id"]
    for r in _ordered(conn, sid):
        if r["has_acted"]:
            continue
        if r["current_hp"] > 0 or is_dying(r):   # the conscious — and a dying PC gets a death-save turn
            return r
    return None


def next_turn(conn, campaign_id: int) -> str:
    sess = state.current_session(conn, campaign_id)
    sid = sess["id"]
    cur = current_turn(conn, campaign_id)
    if cur is not None:
        conn.execute("UPDATE combatant SET has_acted=1 WHERE id=?", (cur["id"],))
    nxt = current_turn(conn, campaign_id)
    rnd = sess["combat_round"]
    if nxt is None:  # everyone alive has acted — new round
        conn.execute("UPDATE combatant SET has_acted=0 WHERE session_id=? AND current_hp>0", (sid,))
        rnd += 1
        conn.execute("UPDATE game_session SET combat_round=? WHERE id=?", (rnd, sid))
        conn.commit()
        nxt = current_turn(conn, campaign_id)
    conn.commit()
    return f"Round {rnd}: {nxt['name']}'s turn." if nxt else f"Round {rnd}: no one able to act."


# ─────────────────────────────── HP / conditions ───────────────────────────────
def change_hp(conn, campaign_id: int, name: str, delta: int):
    """delta < 0 is damage, > 0 is healing. Clamps, syncs PC canon, sets/clears
    down conditions. Returns the refreshed combatant row."""
    sid = state.current_session(conn, campaign_id)["id"]
    comb = _resolve(conn, sid, name)
    if comb is None:
        raise KeyError(f"combatant '{name}'")
    was = comb["current_hp"]
    new_hp = max(0, min(comb["max_hp"], comb["current_hp"] + delta))
    conn.execute("UPDATE combatant SET current_hp=? WHERE id=?", (new_hp, comb["id"]))
    if comb["ref_type"] == "pc":
        conn.execute("UPDATE pc SET current_hp=? WHERE id=?", (new_hp, comb["ref_id"]))

    conds = [c for c in _conds(comb) if c not in DOWN_TAGS and c != "stable"]
    if new_hp == 0:
        if comb["ref_type"] == "monster":
            conds.append("dead")
            conn.execute("UPDATE combatant SET has_acted=1 WHERE id=?", (comb["id"],))
        else:
            conds.append("unconscious")   # a PC at 0 is dying — NOT marked acted; it gets death-save turns
            state.set_flag(conn, campaign_id, f"conc:{comb['name']}", None)   # falling breaks concentration
            ds = _death_saves(comb)
            if was > 0:                    # just dropped — fresh death saves
                ds = {"s": 0, "f": 0}
            elif delta < 0:                # took damage while already down → one death-save failure
                ds["f"] += 1
            if ds["f"] >= 3:               # three failures → dead
                conds = [c for c in conds if c != "unconscious"] + ["dead"]
                conn.execute("UPDATE combatant SET has_acted=1 WHERE id=?", (comb["id"],))
            conn.execute("UPDATE combatant SET death_saves=? WHERE id=?", (json.dumps(ds), comb["id"]))
    elif comb["ref_type"] == "pc" and was <= 0 and new_hp > 0:
        conn.execute("UPDATE combatant SET death_saves=? WHERE id=?", (json.dumps({"s": 0, "f": 0}), comb["id"]))
    conn.execute("UPDATE combatant SET conditions=? WHERE id=?", (json.dumps(sorted(set(conds))), comb["id"]))
    conn.commit()
    return _resolve(conn, sid, name)


def is_pc_dying(conn, campaign_id: int, name: str) -> bool:
    sid = state.current_session(conn, campaign_id)["id"]
    comb = _resolve(conn, sid, name)
    return comb is not None and is_dying(comb)


def resolve_death_save(conn, campaign_id: int, name: str, d20: int) -> str:
    """Resolve a downed PC's death saving throw (player-rolled d20, no modifier).
    10+ = success, <10 = failure; nat 20 = up at 1 HP; nat 1 = two failures;
    3 successes → stable; 3 failures → dead."""
    sid = state.current_session(conn, campaign_id)["id"]
    comb = _resolve(conn, sid, name)
    if comb is None:
        raise KeyError(f"combatant '{name}'")
    if d20 == 20:
        change_hp(conn, campaign_id, name, 1)   # heals to 1, clears unconscious, resets the tally
        return f"{comb['name']} death save: natural 20 — gasps back to consciousness at 1 HP!"
    ds = _death_saves(comb)
    if d20 == 1:
        ds["f"] += 2
        kind = "failure (nat 1 — counts double)"
    elif d20 >= 10:
        ds["s"] += 1
        kind = "success"
    else:
        ds["f"] += 1
        kind = "failure"
    conds = set(_conds(comb))
    note = ""
    if ds["s"] >= 3:
        conds = (conds | {"unconscious", "stable"})
        note = " — STABILIZED (stops rolling, still down)"
        conn.execute("UPDATE combatant SET conditions=? WHERE id=?", (json.dumps(sorted(conds)), comb["id"]))
    elif ds["f"] >= 3:
        conds = (conds - {"unconscious"}) | {"dead"}
        note = " — DIES"
        conn.execute("UPDATE combatant SET conditions=?, has_acted=1 WHERE id=?",
                     (json.dumps(sorted(conds)), comb["id"]))
    conn.execute("UPDATE combatant SET death_saves=? WHERE id=?", (json.dumps(ds), comb["id"]))
    conn.commit()
    return f"{comb['name']} death save: d20 ({d20}) → {kind} [{ds['s']} successes / {ds['f']} failures]{note}"


def set_condition(conn, campaign_id: int, name: str, condition: str, active: bool = True) -> str:
    sid = state.current_session(conn, campaign_id)["id"]
    comb = _resolve(conn, sid, name)
    if comb is None:
        raise KeyError(f"combatant '{name}'")
    valid = set(rules.CONDITIONS) | set(DOWN_TAGS)
    if condition not in valid:
        raise ValueError(f"unknown condition '{condition}'")
    conds = set(_conds(comb))
    conds.add(condition) if active else conds.discard(condition)
    conn.execute("UPDATE combatant SET conditions=? WHERE id=?", (json.dumps(sorted(conds)), comb["id"]))
    conn.commit()
    return f"{comb['name']}: {', '.join(sorted(conds)) or 'no conditions'}"


# ─────────────────────────────── the attack ───────────────────────────────
def _resolve_weapon(conn, a, attack_bonus, damage, weapon):
    """Fill in attack_bonus/damage from the attacker if not supplied — monster
    from its statblock, PC from its sheet's weapons. Symmetric so the model can
    just say `attack(Bram, Wretch)` and the right numbers are used."""
    if attack_bonus is not None and damage is not None:
        return attack_bonus, damage
    if a["ref_type"] == "monster":
        atks = _statblock(conn, a["ref_id"]).get("attacks", [])
        if not atks:
            raise ValueError(f"{a['name']} has no attacks in its statblock")
        pick = next((x for x in atks if weapon and x.get("name", "").lower() == weapon.lower()), atks[0])
        return (attack_bonus if attack_bonus is not None else pick["bonus"],
                damage if damage is not None else pick["damage"])
    sheet = json.loads(conn.execute("SELECT sheet FROM pc WHERE id=?", (a["ref_id"],)).fetchone()["sheet"])
    opts = rules.attack_options(sheet)
    pick = next((o for o in opts if weapon and o["name"].lower() == weapon.lower()), opts[0])
    return (attack_bonus if attack_bonus is not None else pick["bonus"],
            damage if damage is not None else pick["damage"])


def _condition_mods(a, t, ranged: bool):
    """Derive advantage/disadvantage and auto-crit from the combatants' conditions."""
    a_conds, t_conds = set(_conds(a)), set(_conds(t))
    adv = dis = False
    notes = []
    grant = t_conds & TARGET_GRANTS_ADV
    if grant:
        adv = True
        notes.append("adv: target " + "/".join(sorted(grant)))
    if "prone" in t_conds:
        if ranged:
            dis = True
            notes.append("disadv: prone target at range")
        else:
            adv = True
            notes.append("adv: prone target in melee")
    hinder = a_conds & ATTACKER_DISADV
    if hinder:
        dis = True
        notes.append("disadv: attacker " + "/".join(sorted(hinder)))
    return adv, dis, bool(t_conds & AUTO_CRIT_TARGET), notes


def attack(conn, campaign_id: int, attacker: str, target: str, *, dice: Dice,
           attack_bonus: int | None = None, damage: str | None = None, weapon: str | None = None,
           advantage: bool = False, disadvantage: bool = False, ranged: bool = False) -> str:
    """Resolve one attack: roll to-hit vs AC, roll and apply damage on a hit, and
    report. Attacker stats auto-resolve from statblock (monster) or sheet (PC).
    Condition advantage/disadvantage and auto-crits are applied automatically;
    explicit advantage/disadvantage stacks with them (and cancels per 5e)."""
    sess = state.current_session(conn, campaign_id)
    if not sess["in_combat"]:
        raise ValueError("no active encounter — call start_encounter first")
    a = _resolve(conn, sess["id"], attacker)
    t = _resolve(conn, sess["id"], target)
    if a is None:
        raise KeyError(f"attacker '{attacker}'")
    if t is None:
        raise KeyError(f"target '{target}'")
    if t["current_hp"] <= 0:
        raise ValueError(f"{t['name']} is already down")
    if not ranged and a["zone"] and t["zone"] and a["zone"] != t["zone"]:
        raise ValueError(f"{t['name']} is in '{t['zone']}', not melee range of {a['name']} in "
                         f"'{a['zone']}' — move adjacent or make a ranged attack (ranged=true)")

    attack_bonus, damage = _resolve_weapon(conn, a, attack_bonus, damage, weapon)

    c_adv, c_dis, auto_crit, notes = _condition_mods(a, t, ranged)
    adv, dis = advantage or c_adv, disadvantage or c_dis
    target_ac = _ac(conn, t)
    atk = attack_roll(dice, int(attack_bonus), target_ac, advantage=adv, disadvantage=dis)
    tag = f" [{', '.join(notes)}]" if notes else ""
    head = f"{a['name']} → {t['name']}: {atk.attack_roll.detail} vs AC {target_ac}{tag}"
    if not atk.hit:
        return f"{head} → MISS"

    is_crit = atk.critical or auto_crit
    dmg = dice.roll(damage)
    if is_crit:
        crit = dice.roll(damage)
        bonus_dice = crit.total - crit.modifier  # double the dice, not the flat mod
        total = dmg.total + bonus_dice
        why = " (nat 20)" if atk.critical else " (helpless target)"
        dmg_str = f"{dmg.detail} + crit dice {bonus_dice} = {total}{why}"
    else:
        total = dmg.total
        dmg_str = dmg.detail  # already ends in "= total"
    after = change_hp(conn, campaign_id, t["name"], -total)
    down = " — DOWN" if after["current_hp"] == 0 else ""
    verb = "CRITS" if is_crit else "hits"
    conc = _concentration_check(conn, campaign_id, t, total, dice) if after["current_hp"] > 0 else ""
    return (f"{head} → {verb}. Damage {dmg_str}. "
            f"{after['name']} {after['current_hp']}/{after['max_hp']}hp{down}{conc}")


def _concentration_check(conn, campaign_id: int, target, damage: int, dice) -> str:
    """If a damaged PC is concentrating, force a CON save (DC = max(10, half damage)); a failure
    breaks concentration. Returns a note to append to the attack line (empty if N/A)."""
    if target["ref_type"] != "pc":
        return ""
    spell = state.get_flag(conn, campaign_id, f"conc:{target['name']}")
    if not spell:
        return ""
    dc = max(10, int(damage) // 2)
    tsheet = json.loads(conn.execute("SELECT sheet FROM pc WHERE id=?", (target["ref_id"],)).fetchone()["sheet"])
    chk = ability_check(dice, rules.derive(tsheet).save_mods.get("con", 0), dc)
    if chk.success:
        return f" {target['name']} holds concentration on {spell} (CON save {chk.roll.total} vs DC {dc})."
    state.set_flag(conn, campaign_id, f"conc:{target['name']}", None)
    return f" {target['name']} LOSES concentration on {spell} (CON save {chk.roll.total} vs DC {dc})."


# ─────────────────────────── player-rolled attacks (dice menu) ───────────────────────────
# The DM declares a PC's attack; the engine sets up a roll REQUEST and the human supplies
# the raw d20 / damage dice from the dice menu. The verdict (vs AC, crit, damage applied)
# is computed HERE from the faces the player rolled — never by the model or the player.
import re as _re


def _parse_damage(notation: str) -> tuple[int, int, int]:
    """'2d6+3' -> (count=2, sides=6, flat=+3). One die term + a flat modifier."""
    notation = (notation or "").replace(" ", "")
    md = _re.search(r"(\d*)d(\d+)", notation, _re.I)
    count = int(md.group(1) or 1) if md else 1
    sides = int(md.group(2)) if md else 0
    flat = sum(int(m.group(0)) for m in _re.finditer(r"[+-]\d+(?!\d*d)", notation))
    return count, sides, flat


def begin_pc_attack(conn, campaign_id: int, attacker: str, target: str, *, weapon=None,
                    advantage=False, disadvantage=False, ranged=False, bonus_damage=None) -> dict:
    """Validate a PC's attack and return a resolution context — WITHOUT rolling. Reuses the
    same weapon/condition/range logic as attack(); the player supplies the dice next. `damage_parts`
    is the ordered list of damage rolls the player will make on a hit — the weapon, then any
    class-feature riders (Sneak Attack, Divine Smite, …) the DM passed in bonus_damage."""
    sess = state.current_session(conn, campaign_id)
    if not sess["in_combat"]:
        raise ValueError("no active encounter — call start_encounter first")
    a, t = _resolve(conn, sess["id"], attacker), _resolve(conn, sess["id"], target)
    if a is None:
        raise KeyError(f"attacker '{attacker}'")
    if t is None:
        raise KeyError(f"target '{target}'")
    if t["current_hp"] <= 0:
        raise ValueError(f"{t['name']} is already down")
    if not ranged and a["zone"] and t["zone"] and a["zone"] != t["zone"]:
        raise ValueError(f"{t['name']} is in '{t['zone']}', not melee range of {a['name']} — "
                         f"move adjacent or attack ranged (ranged=true)")
    bonus, damage = _resolve_weapon(conn, a, None, None, weapon)
    c_adv, c_dis, auto_crit, notes = _condition_mods(a, t, ranged)
    adv, dis = bool(advantage or c_adv), bool(disadvantage or c_dis)
    if adv and dis:
        adv = dis = False
    # A ranged shot spends a piece of ammunition (arrows/bolts) from the attacker's pack. Thrown
    # weapons (javelins) aren't flagged ammunition, so they're not consumed (they're recoverable).
    ammo_note = ""
    if ranged:
        for r in conn.execute(
            "SELECT i.name, i.properties, inv.quantity FROM inventory inv JOIN item i "
            "ON i.campaign_id=? AND i.slug=inv.item_slug "
            "WHERE inv.owner_type='pc' AND inv.owner_id=? AND inv.quantity>0", (campaign_id, a["ref_id"])).fetchall():
            props = json.loads(r["properties"]) if r["properties"] else {}
            if props.get("ammunition"):
                state.consume_item(conn, campaign_id, a["name"], r["name"])
                ammo_note = f"{r['name']}: {r['quantity'] - 1} left"
                break
    parts = [{"name": weapon or "weapon", "notation": damage}]
    for b in (bonus_damage or []):
        notation = b.get("dice") or b.get("notation")
        if notation:
            parts.append({"name": b.get("name", "bonus"), "notation": notation})
    return {"kind": "attack", "attacker": a["name"], "target": t["name"], "weapon": weapon or "",
            "bonus": int(bonus), "damage": damage, "damage_parts": parts, "target_ac": _ac(conn, t),
            "advantage": adv, "disadvantage": dis, "auto_crit": bool(auto_crit), "notes": notes,
            "ammo_note": ammo_note}


def pc_tohit_request(ctx: dict) -> dict:
    """The dice the player must roll to hit: a d20, or two for (dis)advantage."""
    n = 2 if (ctx.get("advantage") or ctx.get("disadvantage")) else 1
    return {"die": "d20", "count": n}


def resolve_pc_tohit(ctx: dict, faces: list[int]) -> dict:
    """faces = the raw d20(s) the player rolled. Returns the hit/crit verdict + a detail line."""
    if ctx.get("advantage") or ctx.get("disadvantage"):
        chosen = max(faces) if ctx["advantage"] else min(faces)
        kind = "adv" if ctx["advantage"] else "dis"
        base = f"d20 {kind} ({', '.join(map(str, faces))})→{chosen}"
    else:
        chosen = faces[0]
        base = f"d20 ({chosen})"
    bonus, ac = ctx["bonus"], ctx["target_ac"]
    total = chosen + bonus
    crit = (chosen == 20) or ctx.get("auto_crit", False)
    hit = (chosen == 20) or (chosen != 1 and total >= ac)
    sign = "+" if bonus >= 0 else "-"
    detail = f"{ctx['attacker']} → {ctx['target']}: {base} {sign} {abs(bonus)} = {total} vs AC {ac}"
    if ctx.get("notes"):
        detail += f" [{', '.join(ctx['notes'])}]"
    detail += " → " + ("CRIT!" if crit else "HIT" if hit else "MISS")
    return {"hit": hit, "crit": crit, "chosen": chosen, "total": total, "detail": detail}


def pc_damage_request(notation: str, crit: bool) -> dict:
    """The dice to roll for one damage part — double the dice (not the flat mod) on a crit."""
    count, sides, _ = _parse_damage(notation)
    return {"die": f"d{sides}", "count": count * (2 if crit else 1)}


def resolve_damage_part(notation: str, faces: list[int]) -> tuple[int, str]:
    """One damage part from its rolled faces: returns (total, human detail)."""
    _, sides, flat = _parse_damage(notation)
    total = sum(faces) + flat
    body = f"{len(faces)}d{sides} ({', '.join(map(str, faces))})"
    if flat:
        body += f" {'+' if flat >= 0 else '-'} {abs(flat)}"
    return total, body


def apply_pc_damage(conn, campaign_id: int, target: str, total: int) -> dict:
    """Apply a fully-rolled attack's total damage to the target. Returns the refreshed row."""
    return change_hp(conn, campaign_id, target, -total)


def current_turn_name(conn, campaign_id: int):
    cur = current_turn(conn, campaign_id)
    return cur["name"] if cur else None


def cast_spell(conn, campaign_id: int, caster: str, target: str, *, dice: Dice, mode: str,
               damage: str | None = None, save_ability: str | None = None,
               half_on_save: bool = True, name: str = "the spell", ranged: bool = True) -> str:
    """Cast a damaging spell. mode='attack' → spell attack roll vs AC (crits, condition
    advantage). mode='save' → target rolls a save vs the caster's spell DC; half damage
    on a success unless half_on_save is false. Caster DC/attack come from the sheet."""
    sess = state.current_session(conn, campaign_id)
    if not sess["in_combat"]:
        raise ValueError("no active encounter — call start_encounter first")
    a = _resolve(conn, sess["id"], caster)
    t = _resolve(conn, sess["id"], target)
    if a is None:
        raise KeyError(f"caster '{caster}'")
    if t is None:
        raise KeyError(f"target '{target}'")
    if t["current_hp"] <= 0:
        raise ValueError(f"{t['name']} is already down")
    dc, atk_bonus = _caster_stats(conn, a)

    if mode == "attack":
        if atk_bonus is None:
            raise ValueError(f"{a['name']} has no spell attack bonus (set spellcasting_ability on the sheet)")
        c_adv, c_dis, auto_crit, notes = _condition_mods(a, t, ranged)
        atk = attack_roll(dice, int(atk_bonus), _ac(conn, t), advantage=c_adv, disadvantage=c_dis)
        tag = f" [{', '.join(notes)}]" if notes else ""
        head = f"{a['name']} casts {name} at {t['name']}: {atk.attack_roll.detail} vs AC {_ac(conn, t)}{tag}"
        if not atk.hit:
            return f"{head} → MISS"
        if not damage:
            return f"{head} → HIT"
        dmg = dice.roll(damage)
        is_crit = atk.critical or auto_crit
        if is_crit:
            crit = dice.roll(damage)
            total = dmg.total + (crit.total - crit.modifier)
            dstr = f"{dmg.detail} + crit dice {crit.total - crit.modifier} = {total}"
        else:
            total, dstr = dmg.total, dmg.detail
        after = change_hp(conn, campaign_id, t["name"], -total)
        down = " — DOWN" if after["current_hp"] == 0 else ""
        return (f"{head} → {'CRIT' if is_crit else 'HIT'}. Damage {dstr}. "
                f"{after['name']} {after['current_hp']}/{after['max_hp']}hp{down}")

    if mode == "save":
        if dc is None:
            raise ValueError(f"{a['name']} has no spell save DC (set spellcasting_ability on the sheet)")
        if save_ability not in rules.ABILITIES:
            raise ValueError("save_ability must be one of " + ", ".join(rules.ABILITIES))
        smod = _save_mod(conn, t, save_ability)
        save = dice.d20(smod)
        saved = save.total >= dc
        head = f"{a['name']} casts {name}; {t['name']} {save_ability} save {save.detail} vs DC {dc} → {'SAVES' if saved else 'FAILS'}"
        if not damage:
            return head
        dmg = dice.roll(damage)
        taken = (dmg.total // 2 if half_on_save else 0) if saved else dmg.total
        after = change_hp(conn, campaign_id, t["name"], -taken) if taken else t
        halved = " (halved)" if saved and half_on_save else ""
        down = " — DOWN" if after["current_hp"] == 0 else ""
        return (f"{head}. Damage {dmg.detail}{halved} → takes {taken}. "
                f"{after['name']} {after['current_hp']}/{after['max_hp']}hp{down}")

    raise ValueError("mode must be 'attack' or 'save'")


# ─────────────────────────────── status ───────────────────────────────
def status(conn, campaign_id: int) -> str:
    sess = state.current_session(conn, campaign_id)
    sid = sess["id"]
    rows = _ordered(conn, sid)
    if not rows:
        return "No active encounter."
    zones = json.loads(sess["combat_zones"]) if sess["combat_zones"] else []
    cur = current_turn(conn, campaign_id)
    cur_id = cur["id"] if cur else None
    lines = [f"COMBAT — round {get_round(conn, campaign_id)} (initiative order):"]
    if len(zones) > 1:
        lines.append("  zones (adjacent = neighbours): " + " — ".join(zones))
    for r in rows:
        mark = "▶" if r["id"] == cur_id else " "
        conds = _conds(r)
        ctag = f" [{', '.join(conds)}]" if conds else ""
        ztag = f" @{r['zone']}" if r["zone"] and len(zones) > 1 else ""
        down = "" if r["current_hp"] > 0 else " (down)"
        lines.append(f"  {mark} {r['initiative']:>2} {r['name']} — {r['current_hp']}/{r['max_hp']}hp"
                     f" [{r['side']}]{ztag}{ctag}{down}")
    enemies_up = any(r["side"] == "enemy" and r["current_hp"] > 0 for r in rows)
    party_up = any(r["side"] == "party" and r["current_hp"] > 0 for r in rows)
    if not enemies_up:
        lines.append("  → all enemies are down. Call end_encounter.")
    elif not party_up:
        lines.append("  → the party is down.")
    return "\n".join(lines)
