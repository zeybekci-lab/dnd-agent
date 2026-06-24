"""The transactional tool layer — what the DM model drives the world through.

Design borrowed from LoreKit: tools are coarse and atomic (one call does the
whole transaction), and they return a human-readable string — or "ERROR: ..."
on failure, which goes back to the model so it adapts rather than the harness
silently smoothing over it. The model NEVER mutates state or rolls dice itself;
it only calls these.
"""

from __future__ import annotations

import json

from engine import combat, compendium, pending, rules, srd, state
from engine.dice import Dice, ability_check

# ─────────────────────────────── tool schemas (sent to Claude) ───────────────────────────────
TOOLS = [
    {
        "name": "get_state",
        "description": "Read the current authoritative situation: active scene (boxed text + your private DM notes), NPCs present, party HP, active quests, and world flags. Call this at the start of a turn to ground yourself — never narrate facts you have not confirmed here.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recall",
        "description": "Search episodic memory for past events. Use before referencing anything that happened earlier so you don't contradict the record. Results are ranked by importance and recency.",
        "input_schema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "keyword to match in event summaries"},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "npc/pc/item slugs involved"},
            "location": {"type": "string", "description": "location slug"},
        }},
    },
    {
        "name": "consult_rules",
        "description": "Look up the actual 5e rule for a mechanic — a condition's exact effect, how cover / advantage / death saves / resting / grappling / concentration work — instead of relying on memory. Returns the most relevant rule entries from the SRD.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "the rule or mechanic to look up, e.g. 'how does cover work'"},
        }, "required": ["query"]},
    },
    {
        "name": "lookup",
        "description": "Look up a campaign entry you haven't loaded — a scene/area, NPC, monster, location, or item — by slug or name. Use it to stay consistent or set up foreshadowing across the adventure (e.g. check who an NPC is before they appear, or what a not-yet-visited area holds). Returns DM-only notes; honor any 'do not reveal' guidance.",
        "input_schema": {"type": "object", "properties": {
            "target": {"type": "string", "description": "a scene slug/title, NPC, monster, location, or item name"},
        }, "required": ["target"]},
    },
    {
        "name": "compendium",
        "description": "Look up the exact 5e rules for a SPELL, MONSTER, magic ITEM, EQUIPMENT, class FEATURE, or CONDITION from the SRD reference. Use this for precise mechanics (a spell's dice/save/range, an item's effect, a monster's statblock) instead of recalling them from memory.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "what to look up, e.g. 'fireball' or 'potion of healing'"},
            "kind": {"type": "string", "enum": ["spell", "monster", "item", "equipment", "feature", "condition"],
                     "description": "optional filter to one category"},
        }, "required": ["query"]},
    },
    {
        "name": "roll_check",
        "description": "Make an ability check or saving throw for a party member against a DC. The modifier is computed from their sheet — you only choose the check and DC. Returns success/failure and margin.",
        "input_schema": {"type": "object", "properties": {
            "actor": {"type": "string", "description": "PC name"},
            "check": {"type": "string", "description": "a skill (e.g. 'stealth'), an ability ('str'), a save ('dex_save'), or 'initiative'"},
            "dc": {"type": "integer"},
            "advantage": {"type": "boolean"},
            "disadvantage": {"type": "boolean"},
        }, "required": ["actor", "check", "dc"]},
    },
    {
        "name": "start_encounter",
        "description": "Begin a combat encounter. Spawns the party plus monster instances (each tracks its own HP), rolls initiative for everyone, and returns the turn order. Enemies reference monster slugs from the campaign. Optionally pass `zones` (ordered theater-of-mind areas; adjacent = neighbours) and `placements` to position combatants — melee then requires sharing a zone.",
        "input_schema": {"type": "object", "properties": {
            "enemies": {"type": "array", "items": {"type": "object", "properties": {
                "monster": {"type": "string", "description": "monster slug"},
                "count": {"type": "integer", "description": "how many (default 1)"},
            }, "required": ["monster"]}},
            "zones": {"type": "array", "items": {"type": "string"},
                      "description": "optional ordered zone names, e.g. ['mudflat','spire stair','chapel mouth']"},
            "placements": {"type": "object", "description": "optional {combatant_name: zone} (defaults to the first zone)"},
        }, "required": ["enemies"]},
    },
    {
        "name": "encounter_status",
        "description": "Show the current fight: round, initiative order, whose turn it is (▶), every combatant's HP, and conditions. Call it to see who's up and who's hurt.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "attack",
        "description": "Resolve one attack between combatants: rolls to-hit vs the target's AC (nat 20 crits), rolls and APPLIES damage on a hit, tracks HP/downing. Attacker stats auto-resolve — monster from its statblock, PC from its sheet weapons — so usually just pass attacker + target (optionally `weapon` to pick which one). Condition advantage/disadvantage and auto-crits (vs paralyzed/unconscious) are applied for you. Set ranged=true for ranged attacks.",
        "input_schema": {"type": "object", "properties": {
            "attacker": {"type": "string"}, "target": {"type": "string"},
            "weapon": {"type": "string", "description": "which of the attacker's attacks, by name; defaults to the first"},
            "attack_bonus": {"type": "integer", "description": "override the auto-resolved to-hit bonus"},
            "damage": {"type": "string", "description": "override damage dice, e.g. '1d8+3'"},
            "bonus_damage": {"type": "array", "description": "class-feature damage riders this hit is entitled to "
                             "(e.g. Sneak Attack, Divine Smite, Hunter's Mark) — the player rolls each as its own "
                             "die after the weapon damage. Only include what the character legitimately has this turn.",
                             "items": {"type": "object", "properties": {
                                 "name": {"type": "string"}, "dice": {"type": "string", "description": "e.g. '1d6', '2d8'"}}}},
            "advantage": {"type": "boolean"}, "disadvantage": {"type": "boolean"}, "ranged": {"type": "boolean"},
        }, "required": ["attacker", "target"]},
    },
    {
        "name": "cast_spell",
        "description": "Cast a damaging spell in combat. mode='attack' (spell attack roll vs the target's AC) or mode='save' (target rolls a saving throw vs your spell save DC; half damage on a success unless half_on_save=false). The caster's DC and attack bonus come from their sheet. Narrate utility/non-damage spells yourself; use this for the dice and HP.",
        "input_schema": {"type": "object", "properties": {
            "caster": {"type": "string"}, "target": {"type": "string"},
            "mode": {"type": "string", "enum": ["attack", "save"]},
            "name": {"type": "string", "description": "spell name, for flavor"},
            "damage": {"type": "string", "description": "damage dice, e.g. '3d6' or '1d10'"},
            "save_ability": {"type": "string", "enum": ["str", "dex", "con", "int", "wis", "cha"],
                             "description": "required for mode=save"},
            "half_on_save": {"type": "boolean", "description": "default true: half damage on a successful save"},
            "ranged": {"type": "boolean"},
            "slot_level": {"type": "integer", "description": "spell slot level to spend (0 = cantrip, no slot). Errors if the caster has none left."},
        }, "required": ["caster", "target", "mode"]},
    },
    {
        "name": "next_turn",
        "description": "Advance initiative to the next combatant. Automatically starts a new round when everyone has acted. Returns whose turn it now is.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "roll_dice",
        "description": "Roll arbitrary dice and get the result from the referee (never invent dice results "
                       "yourself). Use for any roll without a dedicated tool — a reaction's dice (e.g. Stone's "
                       "Endurance 1d12+2), a feature, an environmental/random roll. For a player character's "
                       "ATTACK or skill check, use attack/roll_check instead so the player rolls their own.",
        "input_schema": {"type": "object", "properties": {
            "notation": {"type": "string", "description": "dice notation, e.g. '1d12+2', '2d6', '1d100'"},
            "reason": {"type": "string", "description": "what this roll is for (flavor/log)"},
        }, "required": ["notation"]},
    },
    {
        "name": "end_turn",
        "description": "Optional: explicitly end the acting player's turn. A player's turn passes "
                       "automatically once your beat finishes, so you rarely need this — use it only to be "
                       "explicit. To KEEP a player's turn (because you asked them a question), use hold_turn.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "hold_turn",
        "description": "Call this when your beat ends by asking the CURRENT player a question they must "
                       "answer before their turn resolves — how much of a resource to spend (e.g. how many "
                       "Lay on Hands points), which of several targets, a yes/no choice. It keeps the turn "
                       "with that player; the engine will NOT advance until they reply and you resolve it. "
                       "Without it, finishing your beat passes the turn to the next combatant.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lay_on_hands",
        "description": "A paladin spends points from their Lay on Hands pool to restore a creature's HP "
                       "(or 5 points to cure a disease/poison). The pool is tracked and refreshes on a long "
                       "rest. Pass the number of points; it errors if the pool is too low. When a player says "
                       "'I heal X', ask how much (hold_turn), then call this with their answer.",
        "input_schema": {"type": "object", "properties": {
            "healer": {"type": "string"}, "target": {"type": "string"}, "amount": {"type": "integer"},
        }, "required": ["healer", "target", "amount"]},
    },
    {
        "name": "move",
        "description": "Move a combatant to an adjacent zone during a zoned encounter. Melee attacks require attacker and target to share a zone; this is how you close distance or reposition.",
        "input_schema": {"type": "object", "properties": {
            "combatant": {"type": "string"}, "zone": {"type": "string"},
        }, "required": ["combatant", "zone"]},
    },
    {
        "name": "set_condition",
        "description": "Apply or remove a 5e condition (prone, restrained, poisoned, frightened, etc.) on a combatant.",
        "input_schema": {"type": "object", "properties": {
            "combatant": {"type": "string"}, "condition": {"type": "string"},
            "active": {"type": "boolean", "description": "true to apply, false to remove (default true)"},
        }, "required": ["combatant", "condition"]},
    },
    {
        "name": "end_encounter",
        "description": "End the current fight when it's resolved. Party HP is already saved to the canon; combatant instances are cleared.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_damage",
        "description": "Apply damage (positive) or healing (negative) to a party member — or, during an encounter, any combatant by name. Use this for environmental/trap/spell damage outside the attack flow. HP is clamped to [0, max]. Returns new HP.",
        "input_schema": {"type": "object", "properties": {
            "target": {"type": "string"}, "amount": {"type": "integer", "description": "positive = damage, negative = healing"},
        }, "required": ["target", "amount"]},
    },
    {
        "name": "set_flag",
        "description": "Set a world-state flag (e.g. tide='low', gate_locked=false). Use for any persistent change to the world the canon must remember.",
        "input_schema": {"type": "object", "properties": {
            "key": {"type": "string"}, "value": {"description": "any JSON value"},
        }, "required": ["key", "value"]},
    },
    {
        "name": "advance_scene",
        "description": "Move the party to a new scene (use the slugs from the current scene's transitions). Marks the old scene cleared and the new one active. Returns the new scene's boxed text and DM notes.",
        "input_schema": {"type": "object", "properties": {"scene_slug": {"type": "string"}}, "required": ["scene_slug"]},
    },
    {
        "name": "update_npc",
        "description": "Update an NPC: shift disposition (-100..100), set alive/dead, or write arbitrary mutable state. Use when an interaction changes how an NPC feels or what is true about them.",
        "input_schema": {"type": "object", "properties": {
            "slug": {"type": "string"}, "disposition_delta": {"type": "integer"},
            "alive": {"type": "boolean"}, "state": {"type": "object"},
        }, "required": ["slug"]},
    },
    {
        "name": "log_event",
        "description": "Record a meaningful beat to episodic memory the moment it happens (a discovery, a decision, a death, a promise). Recall is only as good as capture — log liberally. Set importance 0..1.",
        "input_schema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["dialogue", "combat", "decision", "discovery", "state_change"]},
            "summary": {"type": "string", "description": "one line, e.g. 'Party agreed to find Elsy's son for 50gp'"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "location": {"type": "string"},
            "importance": {"type": "number", "description": "0=trivial, 1=campaign-defining"},
        }, "required": ["kind", "summary"]},
    },
    {
        "name": "award_gold",
        "description": "Give ONE character gold (positive) or have them spend it (negative). Gold is "
                       "per-character — name who actually collected or is paying. Use when they loot coins, "
                       "earn a reward, or pay for something.",
        "input_schema": {"type": "object", "properties": {
            "recipient": {"type": "string", "description": "the character who collects or spends the gold"},
            "amount": {"type": "integer", "description": "gp to add (negative to spend)"},
        }, "required": ["recipient", "amount"]},
    },
    {
        "name": "give_item",
        "description": "Put an item into a character's pack (loot, reward, gift). Creates the item if it's new. Use whenever the party picks something up.",
        "input_schema": {"type": "object", "properties": {
            "recipient": {"type": "string", "description": "the character who takes it"},
            "name": {"type": "string"},
            "quantity": {"type": "integer"},
            "description": {"type": "string"},
            "properties": {"type": "object", "description": "optional, e.g. {\"heal\": \"2d4+2\"} for a healing potion"},
        }, "required": ["recipient", "name"]},
    },
    {
        "name": "recruit_companion",
        "description": "Register an NPC who joins / allies with / is freed by / is recruited by the party "
                       "(a freed prisoner like Sildar, a won-over goblin, a hireling). They persist across "
                       "scenes, appear in the party panel, and are remembered along with anything promised "
                       "to them. Set stat_block (a monster slug) to let them fight; set hp/max_hp to track "
                       "their health (e.g. a prisoner found at 1 HP). Call again to update them.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "note": {"type": "string", "description": "who they are and anything owed/promised"},
            "hp": {"type": "integer"}, "max_hp": {"type": "integer"},
            "stat_block": {"type": "string", "description": "monster slug for combat stats (e.g. 'sildar-hallwinter', 'goblin'); omit if they won't fight"},
        }, "required": ["name"]},
    },
    {
        "name": "release_companion",
        "description": "An NPC companion leaves the party — they part ways, stay behind, or die.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "companion_joins_combat",
        "description": "Bring an allied companion into the CURRENT fight on the party's side (uses their "
                       "stat_block). They roll initiative and act against the enemies on their turn. Combat only.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "use_item",
        "description": "A character uses/consumes an item from their pack (e.g. drink a potion). Applies its effect when known (a 'heal' property restores HP) and removes one from the pack.",
        "input_schema": {"type": "object", "properties": {
            "character": {"type": "string"}, "item": {"type": "string"},
        }, "required": ["character", "item"]},
    },
    {
        "name": "use_feature",
        "description": "A character spends a limited-use class feature (e.g. Second Wind, Action Surge). Decrements its counter and applies a known effect (a 'heal' effect restores HP). Errors if already spent — it recharges on a rest.",
        "input_schema": {"type": "object", "properties": {
            "character": {"type": "string"}, "feature": {"type": "string"},
        }, "required": ["character", "feature"]},
    },
    {
        "name": "concentrate",
        "description": "Mark a character's concentration when they cast a spell that requires it (e.g. "
                       "Hold Person, Hunter's Mark, Bless). Pass the spell name to start it, or no spell "
                       "(empty) to end it. Casting a new concentration spell automatically drops the old "
                       "one. While concentrating, taking damage forces a CON save (handled automatically "
                       "when a monster hits them); it also drops if they fall unconscious.",
        "input_schema": {"type": "object", "properties": {
            "character": {"type": "string"},
            "spell": {"type": "string", "description": "the spell being concentrated on; empty/omitted to end concentration"},
        }, "required": ["character"]},
    },
    {
        "name": "spend_hit_dice",
        "description": "On a SHORT REST, a character spends hit dice to heal — each die rolls (its hit "
                       "die + CON modifier) and restores that much HP, up to their max. Call this when a "
                       "player chooses to spend hit dice; the player rolls them. Hit dice refresh (half) "
                       "on a long rest. Don't use mid-combat.",
        "input_schema": {"type": "object", "properties": {
            "character": {"type": "string"},
            "count": {"type": "integer", "description": "how many hit dice to spend (capped at remaining)"},
        }, "required": ["character", "count"]},
    },
    {
        "name": "rest",
        "description": "The party (or one character) takes a rest. A long rest heals to full and refreshes spell slots and ALL limited features; a short rest refreshes short-rest features. Use when they rest in the fiction — never mid-combat, and rests cost in-world time.",
        "input_schema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["short", "long"]},
            "character": {"type": "string", "description": "optional — defaults to the whole party"},
        }, "required": ["kind"]},
    },
]


# ─────────────────────────────── dispatch ───────────────────────────────
def execute(conn, campaign_id: int, name: str, inp: dict, *, dice: Dice | None = None, turn: int | None = None) -> str:
    """Run one tool call against the canon. Returns a string for the model —
    `ERROR: ...` on any failure so the model can recover instead of inventing."""
    d = dice or Dice()
    try:
        return _DISPATCH[name](conn, campaign_id, inp, d, turn)
    except KeyError as e:
        if name not in _DISPATCH:
            return f"ERROR: unknown tool '{name}'"
        return f"ERROR: not found — {e}"
    except Exception as e:  # surfaced to the model, never smoothed over
        return f"ERROR: {type(e).__name__}: {e}"


def _t_get_state(conn, cid, inp, d, turn):
    sess = state.current_session(conn, cid)
    scene = state.get_scene(conn, cid, sess["current_scene"]) if sess and sess["current_scene"] else None
    lines = []
    if scene:
        lines.append(f"SCENE: {scene['title']} ({scene['slug']}) — status {scene['status']}")
        if scene["read_aloud"]:
            lines.append(f"  boxed text: {scene['read_aloud']}")
        if scene["dm_notes"]:
            lines.append(f"  DM notes (private): {scene['dm_notes']}")
        if scene["transitions"]:
            lines.append(f"  transitions: {scene['transitions']}")
        loc = scene["location_slug"]
        present = state.npcs_at(conn, cid, loc) if loc else []
        if present:
            statted = {r["slug"] for r in conn.execute(
                "SELECT slug FROM monster WHERE campaign_id=?", (cid,))}
            def _npc_tag(n):
                tag = f"{n['name']} ({n['slug']}, disp {n['disposition']}"
                if n["slug"] in statted:
                    tag += ", has stat block — start_encounter/lookup by slug if combat begins"
                return tag + ")"
            lines.append("NPCs present: " + ", ".join(_npc_tag(n) for n in present))
    party = state.party(conn, cid)
    if party:
        lines.append("PARTY:")
        for p in party:
            sheet = json.loads(p["sheet"])
            der = rules.derive(sheet)
            atks = "; ".join(f"{o['name']} {o['bonus']:+d} ({o['damage']})" for o in rules.attack_options(sheet))
            spell = (f", spells: save DC {der.spell_save_dc} / atk {der.spell_attack:+d}"
                     if der.spell_save_dc is not None else "")
            feats = sheet.get("features", [])
            feat_str = (" — features: " + ", ".join(f["name"] for f in feats)) if feats else ""
            pron = sheet.get("pronouns")
            name_disp = f"{p['name']} ({pron})" if pron else p["name"]
            armor = sheet.get("armor", "")
            heavy = armor.lower() in {"chain mail", "ring mail", "splint", "splint armor",
                                      "plate", "plate armor", "chain mail armor"}
            armor_note = f" — wearing {armor} (heavy: disadvantage on Stealth)" if heavy else ""
            conc = state.get_flag(conn, cid, f"conc:{p['name']}")
            conc_note = f" — concentrating on {conc}" if conc else ""
            cls = (sheet.get("class") or "").title()
            class_lvl = f", {cls} {der.level}" if cls else f", level {der.level}"
            lines.append(f"  {name_disp}{class_lvl} — {p['current_hp']}/{p['max_hp']}hp, AC {der.ac}, "
                         f"passive Perception {der.passive_perception}, PB +{der.proficiency} — "
                         f"attacks: {atks}{spell}{feat_str}{armor_note}{conc_note}")
            res, mx = state.get_resources(conn, cid, p), state.init_resources(sheet)
            rparts = [f"{fn} {rem}/{mx['features'].get(fn, rem)}" for fn, rem in res.get("features", {}).items()]
            rparts += [f"L{lvl} slots {rem}/{mx['spell_slots'].get(lvl, rem)}" for lvl, rem in sorted(res.get("spell_slots", {}).items())]
            if mx.get("hit_dice"):
                rparts.append(f"HD {res.get('hit_dice', mx['hit_dice'])}/{mx['hit_dice']}")
            if mx.get("lay_on_hands"):
                rparts.append(f"Lay on Hands {res.get('lay_on_hands', mx['lay_on_hands'])}/{mx['lay_on_hands']} HP")
            if rparts:
                lines.append("      resources: " + ", ".join(rparts))
            notable = []
            for r in conn.execute(
                "SELECT i.name, i.properties, inv.quantity FROM inventory inv JOIN item i "
                "ON i.campaign_id=? AND i.slug=inv.item_slug "
                "WHERE inv.owner_type='pc' AND inv.owner_id=? AND inv.quantity>0", (cid, p["id"])).fetchall():
                ip = json.loads(r["properties"]) if r["properties"] else {}
                if any(ip.get(k) for k in ("value_gp", "magical", "plot", "unique", "heal")):
                    notable.append(r["name"] + (f" x{r['quantity']}" if r["quantity"] > 1 else ""))
            if notable:    # so the DM never re-awards loot the party already holds
                lines.append("      carrying (notable): " + ", ".join(notable))
    else:
        lines.append("PARTY: (none)")
    quests = state.active_quests(conn, cid)
    if quests:
        lines.append("ACTIVE QUESTS: " + "; ".join(q["title"] for q in quests))
    comps = state.list_companions(conn, cid)
    if comps:
        lines.append("TRAVELING WITH / ALLIED TO THE PARTY (remember these NPCs and honor any promises):")
        for c in comps:
            hp = f" [{c['hp']}/{c['max_hp']} HP]" if c["hp"] is not None and c["max_hp"] else ""
            sb = f" (can fight: {c['stat_block']})" if c["stat_block"] else ""
            lines.append(f"  {c['name']}{hp}{sb} — {c['note'] or ''}")
    flags = conn.execute("SELECT key, value FROM flag WHERE campaign_id=?", (cid,)).fetchall()
    other = [f for f in flags if not f["key"].startswith("companion:")]   # legacy companion: flags now live in the table
    if other:
        lines.append("FLAGS: " + ", ".join(f"{f['key']}={f['value']}" for f in other))
    return "\n".join(lines) if lines else "(no active scene)"


def _t_recall(conn, cid, inp, d, turn):
    hits = state.recall(conn, cid, text=inp.get("text"), entities=inp.get("entities"),
                        location=inp.get("location"), current_turn=turn)
    if not hits:
        return "No matching memories."
    return "\n".join(f"- [{h['kind']}] {h['summary']}" for h in hits)


def _t_consult_rules(conn, cid, inp, d, turn):
    hits = srd.search(inp["query"], k=3)
    if not hits:
        return "No matching rule found in the SRD reference."
    return "\n\n".join(f"## {h['heading']}\n{h['body']}" for h in hits)


def _t_lookup(conn, cid, inp, d, turn):
    q = inp["target"].strip().lower()
    like = f"%{q}%"
    out = []
    for s in conn.execute("SELECT * FROM scene WHERE campaign_id=? AND (lower(slug)=? OR lower(title) LIKE ?)", (cid, q, like)):
        line = f"SCENE {s['title']} ({s['slug']}): {s['dm_notes'] or s['read_aloud'] or ''}"
        if s["transitions"]:
            line += f"  | leads to: {s['transitions']}"
        out.append(line)
    for n in conn.execute("SELECT * FROM npc WHERE campaign_id=? AND (lower(slug)=? OR lower(name) LIKE ?)", (cid, q, like)):
        parts = [f"NPC {n['name']} ({n['slug']}) — {n['role']}"]
        if n["persona"]:
            parts.append(f"persona: {n['persona']}")
        if n["knowledge"]:
            parts.append(f"knows: {n['knowledge']}")
        if n["secrets"]:
            parts.append(f"secret (DM-only): {n['secrets']}")
        out.append(" | ".join(parts))
    for loc in conn.execute("SELECT * FROM location WHERE campaign_id=? AND (lower(slug)=? OR lower(name) LIKE ?)", (cid, q, like)):
        out.append(f"LOCATION {loc['name']} ({loc['slug']}): {loc['description'] or ''}")
    for m in conn.execute("SELECT * FROM monster WHERE campaign_id=? AND (lower(slug)=? OR lower(name) LIKE ?)", (cid, q, like)):
        out.append(f"MONSTER {m['name']} ({m['slug']}): {m['statblock']}")
    for it in conn.execute("SELECT * FROM item WHERE campaign_id=? AND (lower(slug)=? OR lower(name) LIKE ?)", (cid, q, like)):
        out.append(f"ITEM {it['name']} ({it['slug']}): {it['description'] or ''}")
    if not out:
        return f"No campaign entry matching '{inp['target']}'. Try a scene slug/title, NPC, monster, location, or item."
    return "\n\n".join(out[:6])


def _t_compendium(conn, cid, inp, d, turn):
    hits = compendium.search(inp["query"], kind=inp.get("kind"), k=3)
    if not hits:
        return "Nothing in the SRD compendium matches that."
    return "\n\n".join(f"[{h['kind']}] {h['summary']}" for h in hits)


def _t_roll_check(conn, cid, inp, d, turn):
    pc = state.resolve_pc(conn, cid, inp["actor"])
    if pc is None:
        raise KeyError(f"PC '{inp['actor']}'")
    check = inp["check"]
    is_save = "save" in check.lower()
    if pending.is_enabled(conn) and not is_save:
        # The player rolls their own skill/ability check on the dice menu (like attacks). Saves are
        # auto-rolled below — they often fire during a monster's turn, where there's no clean opening
        # to pause for a player roll.
        derived = rules.derive(json.loads(pc["sheet"]))
        mod = rules.check_modifier(derived, check)
        adv, dis = bool(inp.get("advantage")), bool(inp.get("disadvantage"))
        if adv and dis:
            adv = dis = False
        pending.set_request(conn, {
            "character": pc["name"], "die": "d20", "count": 2 if (adv or dis) else 1, "step": "check",
            "purpose": f"{check} check (DC {inp['dc']})",
            "ctx": {"kind": "check", "character": pc["name"], "check": check, "dc": int(inp["dc"]),
                    "mod": int(mod), "advantage": adv, "disadvantage": dis}})
        return (f"ROLL REQUEST: {pc['name']} must roll a d20 for a {check} check. Tell them to roll — do "
                f"NOT decide or narrate the result, reveal the DC, or take any other action. End here and wait.")
    derived = rules.derive(json.loads(pc["sheet"]))
    mod = rules.check_modifier(derived, check)
    res = ability_check(d, mod, int(inp["dc"]),
                        advantage=inp.get("advantage", False), disadvantage=inp.get("disadvantage", False))
    verdict = "SUCCESS" if res.success else "FAILURE"
    extra = " (natural 20!)" if res.critical else " (natural 1!)" if res.fumble else ""
    return f"{pc['name']} {check} vs DC {inp['dc']}: {res.roll.detail} -> {verdict} by {abs(res.margin)}{extra}"


def _t_start_encounter(conn, cid, inp, d, turn):
    return combat.start_encounter(conn, cid, inp["enemies"], dice=d,
                                  zones=inp.get("zones"), placements=inp.get("placements"))


def _t_move(conn, cid, inp, d, turn):
    return combat.move(conn, cid, inp["combatant"], inp["zone"])


def _t_cast_spell(conn, cid, inp, d, turn):
    lvl = int(inp.get("slot_level", 0))
    if lvl > 0 and state.resolve_pc(conn, cid, inp["caster"]) is not None:
        state.spend_slot(conn, cid, inp["caster"], lvl)  # raises -> ERROR surfaced if none left
    out = combat.cast_spell(conn, cid, inp["caster"], inp["target"], dice=d, mode=inp["mode"],
                            damage=inp.get("damage"), save_ability=inp.get("save_ability"),
                            half_on_save=inp.get("half_on_save", True),
                            name=inp.get("name", "the spell"), ranged=inp.get("ranged", True))
    if state.resolve_pc(conn, cid, inp["caster"]) is not None:
        pending.consume_turn(conn)        # a PC casting a spell uses their action → their turn passes
    return out + (f"  (level-{lvl} slot)" if lvl > 0 else "")


def _t_use_feature(conn, cid, inp, d, turn):
    pc, left = state.spend_feature(conn, cid, inp["character"], inp["feature"])
    fdef = next((f for f in json.loads(pc["sheet"]).get("features", [])
                 if f["name"].lower() == inp["feature"].lower()), {})
    if (fdef.get("use") or "").lower() != "reaction":   # a reaction doesn't spend the actor's turn
        pending.consume_turn(conn)
    out = f"{pc['name']} uses {fdef.get('name', inp['feature'])} ({left} left)."
    heal = (fdef.get("effect") or {}).get("heal")
    if heal:
        roll = d.roll(str(heal))
        sess = state.current_session(conn, cid)
        if sess and sess["in_combat"] and combat._resolve(conn, sess["id"], pc["name"]) is not None:
            after = combat.change_hp(conn, cid, pc["name"], roll.total)
        else:
            after = state.change_hp(conn, cid, pc["name"], roll.total)
        out += f" Heals {roll.detail} → {after['name']} {after['current_hp']}/{after['max_hp']}hp."
    return out


def _t_spend_hit_dice(conn, cid, inp, d, turn):
    pc = state.resolve_pc(conn, cid, inp["character"])
    if pc is None:
        raise KeyError(f"PC '{inp['character']}'")
    res = state.get_resources(conn, cid, pc)
    avail = int(res.get("hit_dice", 0))
    if avail <= 0:
        return f"{pc['name']} has no hit dice left — they refresh (half) on a long rest."
    count = max(1, min(int(inp.get("count", 1)), avail))
    sheet = json.loads(pc["sheet"])
    die = (sheet.get("hit_dice") or {}).get("die", "d8")
    con = rules.derive(sheet).ability_mods.get("con", 0)
    if pending.is_enabled(conn):
        pending.set_request(conn, {"character": pc["name"], "die": die, "count": count, "step": "hit_dice",
                                   "purpose": f"hit dice to heal ({count}{die} + {con:+d} each)",
                                   "ctx": {"kind": "hit_dice", "character": pc["name"], "count": count, "con": con}})
        return (f"ROLL REQUEST: {pc['name']} spends {count} hit dice — roll {count}{die}. Tell them to roll; "
                f"do NOT decide the healing or the totals. End here and wait.")
    # auto (single-player legacy)
    roll = d.roll(f"{count}{die}")
    heal = max(0, roll.total + con * count)
    state.spend_hit_dice(conn, cid, pc["name"], count)
    after = state.change_hp(conn, cid, pc["name"], heal)
    return f"{pc['name']} spends {count} hit dice: {roll.detail} + {con*count} CON = {heal} healed → {after['current_hp']}/{after['max_hp']}hp."


def _t_hold_turn(conn, cid, inp, d, turn):
    pending.hold(conn)
    return "Holding the turn for the player's choice — they reply, then you resolve it."


def _t_lay_on_hands(conn, cid, inp, d, turn):
    healer = state.resolve_pc(conn, cid, inp["healer"])
    if healer is None:
        raise KeyError(f"PC '{inp['healer']}'")
    res = state.get_resources(conn, cid, healer)
    pool = int(res.get("lay_on_hands", 0))
    if pool <= 0:
        return f"{healer['name']}'s Lay on Hands pool is EMPTY — nothing to channel (it refreshes on a long rest)."
    amt = int(inp["amount"])
    if amt > pool:
        return f"Only {pool} point(s) left in {healer['name']}'s Lay on Hands pool — can't spend {amt}."
    res["lay_on_hands"] = pool - amt
    state._save_resources(conn, healer["id"], res)
    pending.consume_turn(conn)            # Lay on Hands is an action → the paladin's turn passes
    sess = state.current_session(conn, cid)
    in_combat = bool(sess["in_combat"]) if sess else False
    after = (combat.change_hp(conn, cid, inp["target"], amt) if in_combat
             else state.change_hp(conn, cid, inp["target"], amt))
    return (f"{healer['name']} channels {amt} Lay on Hands into {inp['target']} → "
            f"{after['current_hp']}/{after['max_hp']}hp. Pool: {pool - amt} left.")


def _t_recruit_companion(conn, cid, inp, d, turn):
    state.recruit_companion(conn, cid, inp["name"], note=inp.get("note"),
                            hp=inp.get("hp"), max_hp=inp.get("max_hp"), stat_block=inp.get("stat_block"))
    extra = f" — {inp['note']}" if inp.get("note") else ""
    return f"{inp['name']} now travels with the party{extra}. (Shown in the party panel; remembered each turn.)"


def _t_release_companion(conn, cid, inp, d, turn):
    ok = state.release_companion(conn, cid, inp["name"])
    return f"{inp['name']} is no longer with the party." if ok else f"No companion named '{inp['name']}'."


def _t_companion_joins_combat(conn, cid, inp, d, turn):
    return combat.add_companion_to_combat(conn, cid, inp["name"], dice=d)


def _t_concentrate(conn, cid, inp, d, turn):
    pc = state.resolve_pc(conn, cid, inp["character"])
    if pc is None:
        raise KeyError(f"PC '{inp['character']}'")
    spell = (inp.get("spell") or "").strip()
    state.set_flag(conn, cid, f"conc:{pc['name']}", spell or None)
    return (f"{pc['name']} is now concentrating on {spell}." if spell
            else f"{pc['name']} is no longer concentrating.")


def _t_rest(conn, cid, inp, d, turn):
    kind = inp.get("kind", "long")
    if inp.get("character"):
        state.rest(conn, cid, inp["character"], kind)
        who = inp["character"]
    else:
        who = ", ".join(state.rest_party(conn, cid, kind))
    extra = (" HP restored to full; spell slots and all features refreshed." if kind == "long"
             else " Short-rest features refreshed.")
    return f"{kind.capitalize()} rest — {who}.{extra}"


def _t_encounter_status(conn, cid, inp, d, turn):
    return combat.status(conn, cid)


def _t_attack(conn, cid, inp, d, turn):
    sess = state.current_session(conn, cid)
    a = combat._resolve(conn, sess["id"], inp["attacker"]) if sess and sess["in_combat"] else None
    if a is not None and a["ref_type"] == "pc" and pending.is_enabled(conn):
        # A player character rolls their own dice. Park a roll request and stop the turn;
        # the web layer collects the d20 (then damage) and engine.combat resolves it.
        ctx = combat.begin_pc_attack(conn, cid, inp["attacker"], inp["target"],
                                     weapon=inp.get("weapon"), advantage=inp.get("advantage", False),
                                     disadvantage=inp.get("disadvantage", False), ranged=inp.get("ranged", False),
                                     bonus_damage=inp.get("bonus_damage"))
        req = combat.pc_tohit_request(ctx)
        pending.set_request(conn, {"character": ctx["attacker"], "die": req["die"], "count": req["count"],
                                   "step": "tohit", "purpose": f"to hit {ctx['target']}", "ctx": ctx})
        adv = (" with advantage" if ctx["advantage"] else
               " with disadvantage" if ctx["disadvantage"] else "")
        ammo = f" ({ctx['ammo_note']})" if ctx.get("ammo_note") else ""
        return (f"ROLL REQUEST: {ctx['attacker']} must roll {req['count']}d20{adv} to hit "
                f"{ctx['target']}, then damage.{ammo} Tell them to roll — do NOT resolve it, narrate the "
                f"outcome, or take any further action. End your turn here and wait for the dice.")
    if a is not None and a["ref_type"] == "monster" and pending.is_enabled(conn):
        # Turn-locked table: a monster may only act on ITS OWN turn. This stops the DM resolving
        # several goblins at once or playing ahead — the engine reaches each one in initiative order.
        cur = combat.current_turn_name(conn, cid)
        if cur and a["name"].lower() != cur.lower():
            return (f"ERROR: it's {cur}'s turn, not {a['name']}'s. Resolve ONLY the current combatant's "
                    f"single action — the engine will reach {a['name']} on its own turn.")
    return combat.attack(conn, cid, inp["attacker"], inp["target"], dice=d,
                         attack_bonus=inp.get("attack_bonus"), damage=inp.get("damage"),
                         weapon=inp.get("weapon"), advantage=inp.get("advantage", False),
                         disadvantage=inp.get("disadvantage", False), ranged=inp.get("ranged", False))


def _t_next_turn(conn, cid, inp, d, turn):
    if pending.is_enabled(conn):
        # At a player-rolled table the ENGINE drives initiative (it advances after each resolved
        # turn and plays the monsters one at a time). The DM calling next_turn here would double-
        # advance and scramble the order, so it's a no-op.
        return ("Turn order is automatic at this table — you do NOT advance it. Just resolve the "
                "current combatant's single action and stop; the engine moves to the next turn.")
    return combat.next_turn(conn, cid)


def _t_end_turn(conn, cid, inp, d, turn):
    pending.consume_turn(conn)
    return "Turn ended — the engine will advance to the next combatant."


def _t_roll_dice(conn, cid, inp, d, turn):
    r = d.roll(inp["notation"])
    reason = f" ({inp['reason']})" if inp.get("reason") else ""
    return f"Rolled {r.detail}{reason}"


def _t_set_condition(conn, cid, inp, d, turn):
    return combat.set_condition(conn, cid, inp["combatant"], inp["condition"], inp.get("active", True))


def _t_end_encounter(conn, cid, inp, d, turn):
    return combat.end_encounter(conn, cid)


def _t_apply_damage(conn, cid, inp, d, turn):
    amount = int(inp["amount"])
    sess = state.current_session(conn, cid)
    if sess and sess["in_combat"] and combat._resolve(conn, sess["id"], inp["target"]) is not None:
        c = combat.change_hp(conn, cid, inp["target"], -amount)
        down = " — DOWN" if c["current_hp"] == 0 else ""
        return f"{c['name']}: now {c['current_hp']}/{c['max_hp']} hp{down}"
    pc = state.change_hp(conn, cid, inp["target"], -amount)
    note = " — UNCONSCIOUS (0 hp)" if pc["current_hp"] == 0 else ""
    verb = "takes" if amount >= 0 else "heals"
    return f"{pc['name']} {verb} {abs(amount)}: now {pc['current_hp']}/{pc['max_hp']} hp{note}"


def _t_set_flag(conn, cid, inp, d, turn):
    state.set_flag(conn, cid, inp["key"], inp["value"])
    return f"flag {inp['key']} = {json.dumps(inp['value'])}"


def _t_advance_scene(conn, cid, inp, d, turn):
    scene = state.advance_scene(conn, cid, inp["scene_slug"])
    out = [f"Now in scene: {scene['title']} ({scene['slug']})"]
    if scene["read_aloud"]:
        out.append(f"boxed text: {scene['read_aloud']}")
    if scene["dm_notes"]:
        out.append(f"DM notes (private): {scene['dm_notes']}")
    return "\n".join(out)


def _t_update_npc(conn, cid, inp, d, turn):
    npc = state.update_npc(conn, cid, inp["slug"], disposition_delta=int(inp.get("disposition_delta", 0)),
                           alive=inp.get("alive"), state=inp.get("state"))
    return f"{npc['name']}: disposition {npc['disposition']}, alive={bool(npc['alive'])}"


def _t_log_event(conn, cid, inp, d, turn):
    eid = state.log_event(conn, cid, kind=inp["kind"], summary=inp["summary"], turn=turn,
                          entities=inp.get("entities"), location=inp.get("location"),
                          importance=float(inp.get("importance", 0.5)))
    return f"logged event #{eid}: {inp['summary']}"


def _t_award_gold(conn, cid, inp, d, turn):
    amt = int(inp["amount"])
    pc, new = state.add_pc_gold(conn, cid, inp["recipient"], amt)
    return f"{pc['name']} {'gains' if amt >= 0 else 'spends'} {abs(amt)} gp — now {new} gp."


def _t_give_item(conn, cid, inp, d, turn):
    q = int(inp.get("quantity", 1))
    # Anti-duplication: a unique/plot/magic item exists exactly once. If anyone in the party already
    # carries this item, don't hand out a second — the model is almost certainly re-looting the same
    # thing (e.g. taking a chest's contents twice). Stackable goods (potions, coins) are unaffected.
    slug = state._slug(inp["name"])
    row = conn.execute("SELECT properties FROM item WHERE campaign_id=? AND slug=?", (cid, slug)).fetchone()
    props = (json.loads(row["properties"]) if row and row["properties"] else {}) or (inp.get("properties") or {})
    is_unique = bool(props.get("unique") or props.get("plot"))
    if is_unique:
        held = conn.execute(
            "SELECT 1 FROM inventory inv JOIN pc ON pc.id=inv.owner_id "
            "WHERE inv.owner_type='pc' AND pc.campaign_id=? AND inv.item_slug=? AND inv.quantity>0",
            (cid, slug)).fetchone()
        if held:
            return (f"NOT GIVEN: {inp['name']} is a unique item the party already carries — refusing to "
                    f"duplicate it. (If this is a different object, give it a distinct name.)")
    pc = state.grant_item(conn, cid, inp["recipient"], inp["name"], q,
                          description=inp.get("description"), properties=inp.get("properties"))
    return f"{pc['name']} receives {inp['name']}" + (f" x{q}" if q > 1 else "") + "."


def _t_use_item(conn, cid, inp, d, turn):
    name, props = state.consume_item(conn, cid, inp["character"], inp["item"])
    pending.consume_turn(conn)            # using an item in combat is an action → the turn passes
    out = f"{inp['character']} uses {name}."
    if props.get("heal"):
        roll = d.roll(str(props["heal"]))
        sess = state.current_session(conn, cid)
        if sess and sess["in_combat"] and combat._resolve(conn, sess["id"], inp["character"]) is not None:
            after = combat.change_hp(conn, cid, inp["character"], roll.total)
        else:
            after = state.change_hp(conn, cid, inp["character"], roll.total)
        out += f" Heals {roll.detail} → {after['name']} {after['current_hp']}/{after['max_hp']}hp."
    return out


_DISPATCH = {
    "get_state": _t_get_state, "recall": _t_recall, "consult_rules": _t_consult_rules,
    "lookup": _t_lookup, "compendium": _t_compendium, "roll_check": _t_roll_check,
    "apply_damage": _t_apply_damage, "set_flag": _t_set_flag,
    "advance_scene": _t_advance_scene, "update_npc": _t_update_npc, "log_event": _t_log_event,
    "award_gold": _t_award_gold, "give_item": _t_give_item, "use_item": _t_use_item,
    "use_feature": _t_use_feature, "rest": _t_rest,
    "spend_hit_dice": _t_spend_hit_dice, "concentrate": _t_concentrate,
    "recruit_companion": _t_recruit_companion, "release_companion": _t_release_companion,
    "companion_joins_combat": _t_companion_joins_combat,
    "hold_turn": _t_hold_turn, "lay_on_hands": _t_lay_on_hands,
    "start_encounter": _t_start_encounter, "encounter_status": _t_encounter_status,
    "attack": _t_attack, "cast_spell": _t_cast_spell, "next_turn": _t_next_turn,
    "end_turn": _t_end_turn, "roll_dice": _t_roll_dice, "move": _t_move,
    "set_condition": _t_set_condition, "end_encounter": _t_end_encounter,
}
