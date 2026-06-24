"""FastAPI adapter — serves the CodingHarpers/DndAgent (A.R.C.A.N.A.) Next.js
frontend over OUR engine. We don't use their backend at all; we just reproduce
the four endpoints its UI calls and map them onto engine.dm + engine.state.

    POST /api/play/start_session            -> Scene
    POST /api/play/step {session_id, text}  -> {scene, action_log, player_stats}
    GET  /api/play/stats/{session_id}       -> {hp_current, hp_max, gold, power, speed}
    GET  /api/play/inventory/{session_id}   -> [{id, name, type, properties}]
    POST /api/play/buy                       -> {success, message}   (stub)

Run:  cd ~/dnd-dm && .venv/bin/uvicorn web.adapter:app --port 8000
Then: cd frontend && npm install && npm run dev   (UI at http://localhost:3000/play)
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import uuid

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
    if _line.strip() and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from engine import combat, dm, pending, rules, state  # after .env so the SDK sees the key
from web import persist, rooms

CAMPAIGN = os.environ.get("DND_CAMPAIGN", str(ROOT / "campaign" / "phandelver.yaml"))
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
                   "text": "Resistance to cold damage; no penalties from high altitude."}],
              "armor": "Chain Mail",  # heavy → disadvantage on Stealth
              "coins": {"gp": 20, "sp": 15, "cp": 52},
              "equipment": [
                  {"name": "Chain Mail", "qty": 1, "equipped": True,
                   "desc": "Heavy armor. AC 16. Disadvantage on Stealth; needs Strength 13.",
                   "props": {"slot": "armor", "armor": "heavy", "base_ac": 16, "stealth_disadvantage": True, "str_req": 13}},
                  {"name": "Shield", "qty": 1,
                   "desc": "+2 AC while wielded in one hand (not usable with a two-handed greatsword).",
                   "props": {"slot": "shield", "ac_bonus": 2}},
                  {"name": "Greatsword", "qty": 1, "equipped": True,
                   "desc": "Martial melee weapon. 2d6 slashing. Heavy, two-handed.",
                   "props": {"weapon": True, "damage": "2d6", "type": "slashing", "tags": ["martial", "heavy", "two-handed"]}},
                  {"name": "Javelin", "qty": 6,
                   "desc": "Simple weapon. 1d6 piercing. Thrown, range 30/120.",
                   "props": {"weapon": True, "damage": "1d6", "type": "piercing", "thrown": "30/120"}},
                  {"name": "Holy Symbol (Emblem)", "qty": 1,
                   "desc": "A paladin's spellcasting focus.", "props": {"spellcasting_focus": True}},
                  {"name": "Backpack", "qty": 1},
                  {"name": "Bedroll", "qty": 1},
                  {"name": "Mess Kit", "qty": 1},
                  {"name": "Tinderbox", "qty": 1, "desc": "Lights a torch/lantern as an action."},
                  {"name": "Torch", "qty": 10,
                   "desc": "Sheds 20 ft bright + 20 ft dim light for 1 hour; 1 fire damage as an improvised weapon.",
                   "props": {"light_bright_ft": 20, "light_dim_ft": 20, "burn": "1 hour"}},
                  {"name": "Rations (1 day)", "qty": 10},
                  {"name": "Waterskin", "qty": 1},
                  {"name": "Rope, Hempen (50 ft)", "qty": 2, "props": {"length_ft": 50, "burst_dc": 17, "hp": 2}},
                  {"name": "Clothes, Common", "qty": 1},
                  {"name": "Map", "qty": 1}]}, 12, "human"),
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
                  {"name": "Thieves' Cant", "use": "passive", "text": "A secret rogue language of signs and slang."}],
              "armor": "Leather Armor",  # light → no stealth penalty
              "coins": {"gp": 15},
              "equipment": [
                  {"name": "Leather Armor", "qty": 1, "equipped": True,
                   "desc": "Light armor. AC 11 + Dex. No Stealth penalty.",
                   "props": {"slot": "armor", "armor": "light", "base_ac": 11, "stealth_disadvantage": False}},
                  {"name": "Shortsword", "qty": 2, "equipped": True,
                   "desc": "Martial melee weapon. 1d6 piercing. Finesse, light.",
                   "props": {"weapon": True, "damage": "1d6", "type": "piercing", "tags": ["finesse", "light"]}},
                  {"name": "Shortbow", "qty": 1, "equipped": True,
                   "desc": "Simple ranged weapon. 1d6 piercing. Range 80/320; needs ammunition.",
                   "props": {"weapon": True, "damage": "1d6", "type": "piercing", "range": "80/320"}},
                  {"name": "Arrows", "qty": 20, "props": {"ammunition": True}},
                  {"name": "Thieves' Tools", "qty": 1,
                   "desc": "Proficiency lets you pick locks and disarm traps (Dex check)."},
                  {"name": "Backpack", "qty": 1},
                  {"name": "Bedroll", "qty": 1},
                  {"name": "Tinderbox", "qty": 1, "desc": "Lights a torch/lantern as an action."},
                  {"name": "Torch", "qty": 10,
                   "desc": "Sheds 20 ft bright + 20 ft dim light for 1 hour.",
                   "props": {"light_bright_ft": 20, "light_dim_ft": 20, "burn": "1 hour"}},
                  {"name": "Rations (1 day)", "qty": 5},
                  {"name": "Waterskin", "qty": 1},
                  {"name": "Rope, Hempen (50 ft)", "qty": 1, "props": {"length_ft": 50, "burst_dc": 17, "hp": 2}},
                  {"name": "Clothes, Common", "qty": 1}]}, 9, "AI"),
]

# ── character library ──────────────────────────────────────────────────────────────────────
# Beyond the two built-ins above, any character saved as data/characters/<name>.json is loaded
# here and becomes playable (solo picker + multiplayer claim). Each file is:
#   {"name": str, "hp": int, "player": "human"|"AI", "sheet": {<engine sheet>}}
# Drop a new file in (or have one imported from a D&D Beyond PDF) and it shows up — no code change.
CHARACTERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "characters"


def _load_character_library() -> int:
    if not CHARACTERS_DIR.exists():
        return 0
    have = {p[0].lower() for p in PARTY}
    n = 0
    for f in sorted(CHARACTERS_DIR.glob("*.json")):
        try:
            c = json.loads(f.read_text())
            if c["name"].lower() in have:
                continue
            PARTY.append((c["name"], c["sheet"], int(c["hp"]), c.get("player", "human")))
            have.add(c["name"].lower())
            n += 1
        except Exception as e:
            print(f"[characters] failed to load {f.name}: {e}", flush=True)
    if n:
        print(f"[characters] loaded {n} from the library", flush=True)
    return n


_load_character_library()

# Opus 4.8 for the best DM judgment/pacing. Effort 'low' keeps Opus turns as snappy
# as they get (fewer tool calls, less deliberation) while still out-DMing Sonnet.
# Raise DND_WEB_EFFORT=medium/high for more finesse (slower); drop to
# DND_WEB_MODEL=claude-sonnet-4-6 for cheaper/faster turns.
WEB_MODEL = os.environ.get("DND_WEB_MODEL", "claude-opus-4-8")
WEB_EFFORT = os.environ.get("DND_WEB_EFFORT", "low")
# Cheaper model for MECHANICAL beats — monster turns, narrating a resolved roll. These don't
# need Opus's judgment, so they run on Sonnet with thinking OFF. The player-facing creative
# turns stay on WEB_MODEL. (Set DND_WEB_MECH_MODEL=claude-opus-4-8 to put it all back on Opus.)
WEB_MECH_MODEL = os.environ.get("DND_WEB_MECH_MODEL", "claude-sonnet-4-6")

SESSIONS: dict[str, dict] = {}
_client = None


def client():
    global _client
    if _client is None:
        _client = dm.make_client()
    return _client


def _session(sid: str) -> dict:
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return s


def _human_pc(conn, cid):
    return conn.execute(
        "SELECT * FROM pc WHERE campaign_id=? AND active=1 ORDER BY (player='AI'), id LIMIT 1", (cid,)
    ).fetchone()


# Heavy armor imposes disadvantage on Stealth checks (5e armor table).
HEAVY_ARMOR = {"chain mail", "ring mail", "splint", "splint armor", "plate", "plate armor", "chain mail armor"}


def _pc_has_items(conn, cid, pc) -> bool:
    return conn.execute(
        "SELECT 1 FROM inventory WHERE owner_type='pc' AND owner_id=? LIMIT 1", (pc["id"],)
    ).fetchone() is not None


def _grant_starting_gear(conn, cid) -> int:
    """Stock each default PC's pack from their sheet's `equipment`. Per-character starting gold is
    seeded automatically from each sheet's `coins.gp` when the PC is created (init_resources), so
    there's no party pool to set here. Idempotent: a PC who already carries anything is skipped.
    Returns the number of PCs that were stocked.
    """
    stocked = 0
    for name, sheet, _hp, _player in PARTY:
        pc = state.resolve_pc(conn, cid, name)
        if pc is None or _pc_has_items(conn, cid, pc):
            continue
        for it in sheet.get("equipment", []):
            state.grant_item(conn, cid, name, it["name"], it.get("qty", 1),
                             description=it.get("desc"), properties=it.get("props"),
                             equipped=it.get("equipped", False))
        stocked += 1
    return stocked


# ─────────────────────────────── mappers (engine → their shapes) ───────────────────────────────
def scene_payload(conn, cid, narrative, sid) -> dict:
    sess = state.current_session(conn, cid)
    scene = state.get_scene(conn, cid, sess["current_scene"]) if sess and sess["current_scene"] else None
    actions = []
    if scene and scene["transitions"]:
        actions = [k.replace("_", " ").title() for k in json.loads(scene["transitions"]).keys()]
    return {
        "scene_id": scene["slug"] if scene else None,
        "title": scene["title"] if scene else "Adventure",
        "narrative_text": narrative,
        "available_actions": actions,
        "metadata": {"session_id": sid},
    }


def stats_payload(conn, cid) -> dict:
    pc = _human_pc(conn, cid)
    if pc is None:
        return {"hp_current": 0, "hp_max": 0, "gold": 0, "power": 0, "speed": 30}
    sheet = json.loads(pc["sheet"])
    der = rules.derive(sheet)
    power = max((o["bonus"] for o in rules.attack_options(sheet)), default=der.proficiency)
    gold = state.get_resources(conn, cid, pc).get("gold", 0)
    return {"hp_current": pc["current_hp"], "hp_max": pc["max_hp"], "gold": int(gold),
            "power": power, "speed": int(sheet.get("speed", 30))}


def inventory_payload(conn, cid) -> list[dict]:
    pc = _human_pc(conn, cid)
    if pc is None:
        return []
    rows = conn.execute(
        "SELECT i.slug, i.name, i.properties FROM inventory inv "
        "JOIN item i ON i.campaign_id=? AND i.slug=inv.item_slug "
        "WHERE inv.owner_type='pc' AND inv.owner_id=?", (cid, pc["id"]),
    ).fetchall()
    return [{"id": r["slug"], "name": r["name"], "type": "item",
             "properties": json.loads(r["properties"]) if r["properties"] else {}} for r in rows]


def encounter_payload(conn, cid) -> dict:
    """The full combat board — initiative order, per-instance HP, conditions, zones."""
    sess = state.current_session(conn, cid)
    rows = conn.execute(
        "SELECT * FROM combatant WHERE session_id=? ORDER BY initiative DESC, id ASC", (sess["id"],)
    ).fetchall()
    cur = combat.current_turn(conn, cid)
    cur_id = cur["id"] if cur else None
    return {
        "in_combat": bool(sess["in_combat"]),
        "round": sess["combat_round"],
        "zones": json.loads(sess["combat_zones"]) if sess["combat_zones"] else [],
        "combatants": [{
            "name": r["name"], "side": r["side"],
            "current_hp": r["current_hp"], "max_hp": r["max_hp"],
            "conditions": json.loads(r["conditions"] or "[]"),
            "zone": r["zone"], "initiative": r["initiative"],
            "is_current": r["id"] == cur_id, "down": r["current_hp"] <= 0,
        } for r in rows],
    }


def party_payload(conn, cid) -> dict:
    """Every party member with the stats the panel shows (multi-PC, unlike StatsPanel)."""
    members = []
    for p in state.party(conn, cid):
        sheet = json.loads(p["sheet"])
        der = rules.derive(sheet)
        members.append({
            "name": p["name"], "player": p["player"], "is_npc": False,
            "current_hp": p["current_hp"], "max_hp": p["max_hp"],
            "ac": der.ac, "level": der.level,
            "attacks": rules.attack_options(sheet),
            "spell_save_dc": der.spell_save_dc, "spell_attack": der.spell_attack,
        })
    for c in state.list_companions(conn, cid):   # allied NPCs travel in the panel too, marked as NPCs
        members.append({
            "name": c["name"], "player": "NPC", "is_npc": True,
            "current_hp": c["hp"], "max_hp": c["max_hp"],
            "ac": None, "level": None, "attacks": [],
            "spell_save_dc": None, "spell_attack": None,
            "note": c["note"], "can_fight": bool(c["stat_block"]),
        })
    return {"members": members}


def character_sheet(conn, cid, character: str) -> dict:
    """The full computed character sheet — everything the cruncher derives from the PC."""
    pc = state.resolve_pc(conn, cid, character)
    if pc is None:
        raise KeyError(character)
    sheet = json.loads(pc["sheet"])
    der = rules.derive(sheet)
    abil = sheet.get("abilities", {})
    prof_saves = set(sheet.get("proficient_saves", []))
    prof_sk = set(sheet.get("proficient_skills", []))
    exp = set(sheet.get("expertise_skills", []))
    sess = state.current_session(conn, cid)
    conds = []
    if sess["in_combat"]:
        crow = conn.execute("SELECT conditions FROM combatant WHERE session_id=? AND lower(name)=lower(?)",
                            (sess["id"], pc["name"])).fetchone()
        if crow and crow["conditions"]:
            conds = json.loads(crow["conditions"])
    inv = conn.execute(
        "SELECT i.name, inv.quantity, inv.equipped FROM inventory inv JOIN item i "
        "ON i.campaign_id=? AND i.slug=inv.item_slug WHERE inv.owner_type='pc' AND inv.owner_id=?",
        (cid, pc["id"])).fetchall()
    # Heavy armor (worn) → disadvantage on Stealth. Fall back to the sheet's declared armor.
    worn_heavy = any(r["equipped"] and r["name"].lower() in HEAVY_ARMOR for r in inv)
    heavy = worn_heavy or sheet.get("armor", "").lower() in HEAVY_ARMOR
    res, mx = state.get_resources(conn, cid, pc), state.init_resources(sheet)
    resources = {
        "features": {fn: {"remaining": res["features"].get(fn, 0), "max": mx["features"][fn]} for fn in mx["features"]},
        "spell_slots": {lvl: {"remaining": res["spell_slots"].get(lvl, 0), "max": mx["spell_slots"][lvl]} for lvl in mx["spell_slots"]},
        "hit_dice": {"remaining": res.get("hit_dice", mx["hit_dice"]), "max": mx["hit_dice"],
                     "die": (sheet.get("hit_dice") or {}).get("die", "")},
    }
    return {
        "name": pc["name"], "klass": sheet.get("class", "adventurer"), "level": der.level,
        "pronouns": sheet.get("pronouns", ""),
        "hp_current": pc["current_hp"], "hp_max": pc["max_hp"], "ac": der.ac,
        "speed": int(sheet.get("speed", 30)), "proficiency": der.proficiency,
        "initiative": der.initiative, "passive_perception": der.passive_perception,
        "abilities": {a: {"score": int(abil.get(a, 10)), "mod": der.ability_mods[a]} for a in rules.ABILITIES},
        "saves": {a: {"mod": der.save_mods[a], "proficient": a in prof_saves} for a in rules.ABILITIES},
        "skills": {sk: {"ability": ab, "mod": der.skill_mods[sk], "proficient": sk in prof_sk,
                        "expertise": sk in exp, "disadvantage": sk == "stealth" and heavy}
                   for sk, ab in rules.SKILLS.items()},
        "stealth_disadvantage": heavy,
        "attacks": rules.attack_options(sheet),
        "features": sheet.get("features", []),
        "resources": resources,
        "spell_save_dc": der.spell_save_dc, "spell_attack": der.spell_attack,
        "inventory": [{"name": r["name"], "qty": r["quantity"], "equipped": bool(r["equipped"])} for r in inv],
        "gold": int(res.get("gold", 0)),   # per-character now
        "conditions": conds,
    }


def action_log(calls: list[tuple]) -> dict | None:
    """Collapse a turn's tool calls into the single combat/action entry CombatLog wants."""
    combat = [c for c in calls if c[0] in ("attack", "cast_spell")]
    pick = combat[-1] if combat else next((c for c in reversed(calls) if c[0] == "roll_check"), None)
    if pick is None:
        return None
    result = pick[2]
    is_miss = "MISS" in result or "FAILS" in result
    log: dict = {"success": not is_miss, "hit": not is_miss, "message": result}
    if (m := re.search(r"d20[^=]*=\s*(\d+)", result)):
        log["roll"] = int(m.group(1))
    if (m := re.search(r"takes (\d+)", result)) or (m := re.search(r"Damage.*?=\s*(\d+)", result)):
        log["damage"] = int(m.group(1))
    hps = re.findall(r"(\d+)/\d+hp", result)
    if hps:
        log["target_hp"] = int(hps[-1])
    return log


# ─────────────────────────────── routes ───────────────────────────────
router = APIRouter()


@router.post("/backfill-gear")
def backfill_gear():
    """Stock starting gear + party gold into every live session and room that predates the
    equipment feature. Idempotent — a PC who already carries items is skipped."""
    sessions = sum(_grant_starting_gear(s["conn"], s["cid"]) for s in SESSIONS.values())
    table_rooms = sum(_grant_starting_gear(r.conn, r.cid) for r in rooms.ROOMS.values())
    return {"sessions_pcs_stocked": sessions, "rooms_pcs_stocked": table_rooms,
            "live_sessions": len(SESSIONS), "live_rooms": len(rooms.ROOMS)}


class PlayerInput(BaseModel):
    session_id: str
    text: str


class BuyRequest(BaseModel):
    session_id: str
    item_id: str


class JoinBody(BaseModel):
    character: str


class ReadyBody(BaseModel):
    character: str
    text: str = ""
    ready: bool = True


class ActBody(BaseModel):
    character: str
    text: str


class RollBody(BaseModel):
    character: str
    die: str            # "d20", "d6", …
    values: list[int]   # the face(s) the device rolled


class ReactionBody(BaseModel):
    character: str
    text: str           # what the player wants to do in reaction to the last thing


class RewindBody(BaseModel):
    to: int             # checkpoint index to roll back to


@router.post("/start_session")
def start_session(body: dict | None = None):
    sid = uuid.uuid4().hex
    conn = state.connect(persist.session_db_path(sid))   # file-backed so the game survives a restart
    cid = state.compile_campaign(conn, CAMPAIGN)
    if not state.party(conn, cid):  # campaign defines no PCs — fall back to the default duo
        for name, sheet, hp, player in PARTY:
            state.add_pc(conn, cid, name, sheet, hp, player=player)
        _grant_starting_gear(conn, cid)
    SESSIONS[sid] = {"conn": conn, "cid": cid, "history": [], "turn": 1}
    sess = state.current_session(conn, cid)
    scene = state.get_scene(conn, cid, sess["current_scene"]) if sess["current_scene"] else None
    narrative = (scene["read_aloud"] if scene and scene["read_aloud"]
                 else "Your adventure begins. What do you do?")
    payload = scene_payload(conn, cid, narrative, sid)
    SESSIONS[sid]["last_scene"] = payload
    persist.save_session(sid, SESSIONS[sid])
    return payload


@router.post("/step")
def step(inp: PlayerInput):
    s = _session(inp.session_id)
    calls: list[tuple] = []
    text, s["history"] = dm.run_turn(
        s["conn"], s["cid"], client(), s["history"], inp.text, model=WEB_MODEL,
        turn=s["turn"], effort=WEB_EFFORT, on_tool=lambda n, i, o: calls.append((n, i, o)),
    )
    s["turn"] += 1
    scene = scene_payload(s["conn"], s["cid"], text, inp.session_id)
    s["last_scene"] = scene
    persist.save_session(inp.session_id, s)
    return {
        "scene": scene,
        "action_log": action_log(calls),
        "player_stats": stats_payload(s["conn"], s["cid"]),
    }


@router.get("/stats/{session_id}")
def get_stats(session_id: str):
    s = _session(session_id)
    return stats_payload(s["conn"], s["cid"])


@router.get("/inventory/{session_id}")
def get_inventory(session_id: str):
    s = _session(session_id)
    return inventory_payload(s["conn"], s["cid"])


@router.get("/encounter/{session_id}")
def get_encounter(session_id: str):
    s = _session(session_id)
    return encounter_payload(s["conn"], s["cid"])


@router.get("/party/{session_id}")
def get_party(session_id: str):
    s = _session(session_id)
    return party_payload(s["conn"], s["cid"])


# ─────────────────────────────── multiplayer rooms ───────────────────────────────
def _run_for(room):
    """Bind the DM loop to a room: returns run(player_input, mechanical=False) -> (narration, log).
    mechanical=True routes the beat to the cheaper model with thinking off (monster turns, narration)."""
    def run(player_input, mechanical=False):
        calls = []
        text, room.history = dm.run_turn(
            room.conn, room.cid, client(), room.history, player_input,
            model=(WEB_MECH_MODEL if mechanical else WEB_MODEL),
            turn=room.turn, effort=WEB_EFFORT, think=not mechanical,
            on_tool=lambda n, i, o: calls.append((n, i, o)))
        room.turn += 1
        return text, action_log(calls)
    return run


def _get_room(rid):
    try:
        return rooms.get(rid)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such room")


def _library_roster(room) -> list[dict]:
    """The full playable roster (the character library) with claim status. The pick screen lists
    these; a character enters the campaign only when someone claims it."""
    return [{"name": n, "klass": s.get("class", "adventurer"), "level": s.get("level", 1),
             "claimed": bool(room.claimed.get(n))} for n, s, _hp, _pl in PARTY]


def _add_library_pc(conn, cid, name: str) -> bool:
    """Add a library character to the campaign on first claim, stocking their gear. Returns False
    if the name isn't a known playable character."""
    if state.resolve_pc(conn, cid, name):
        return True
    entry = next((p for p in PARTY if p[0].lower() == name.lower()), None)
    if entry is None:
        return False
    pname, sheet, hp, player = entry
    state.add_pc(conn, cid, pname, sheet, hp, player=player)
    for it in sheet.get("equipment", []):
        state.grant_item(conn, cid, pname, it["name"], it.get("qty", 1),
                         description=it.get("desc"), properties=it.get("props"), equipped=it.get("equipped", False))
    return True


@router.post("/room/create")
def room_create():
    rid = uuid.uuid4().hex[:6].upper()
    conn = state.connect(persist.db_path(rid))   # file-backed so the table survives a restart
    cid = state.compile_campaign(conn, CAMPAIGN)
    # NB: no characters are added here — a character only joins the campaign when a player CLAIMS it
    # (see room_join), so the party is exactly the selected cast, not the whole library.
    pending.enable(conn)  # PCs roll their own dice at the table (dice menu)
    room = rooms.create_room(conn, cid, rid=rid)
    persist.save_room(room)
    return {"room_id": room.id, "characters": _library_roster(room)}


@router.get("/characters")
def list_characters():
    """The default party's characters, for the solo picker (and the lobby)."""
    return {"characters": [{"name": n, "klass": s.get("class", "adventurer"),
                            "level": s.get("level", 1)} for n, s, _hp, _pl in PARTY]}


class SoloBody(BaseModel):
    character: str


@router.post("/room/create_solo")
def room_create_solo(body: SoloBody):
    """A single-player game: a one-PC room, auto-claimed. Reuses the whole table engine — dice
    menu, engine-owned turns, reactions, persistence — for one character alone."""
    entry = next((p for p in PARTY if p[0].lower() == body.character.lower()), None)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"no character '{body.character}'")
    name, sheet, hp, _player = entry
    rid = uuid.uuid4().hex[:6].upper()
    conn = state.connect(persist.db_path(rid))
    cid = state.compile_campaign(conn, CAMPAIGN)
    if not state.party(conn, cid):
        state.add_pc(conn, cid, name, sheet, hp, player="human")  # the lone PC is human-rolled
        _grant_starting_gear(conn, cid)
    pending.enable(conn)
    room = rooms.create_room(conn, cid, rid=rid)
    rooms.claim(room, name)                       # auto-claim — no pick screen needed
    persist.save_room(room)
    return {"room_id": room.id, "character": name}


@router.get("/room/{rid}/roster")
def room_roster(rid: str):
    """The pick-screen roster: the whole playable library with who's already claimed."""
    return {"characters": _library_roster(_get_room(rid))}


@router.post("/room/{rid}/join")
def room_join(rid: str, body: JoinBody):
    room = _get_room(rid)
    with room.lock:  # claim + roster read the shared connection
        if not _add_library_pc(room.conn, room.cid, body.character):   # add the PC to the campaign on first claim
            raise HTTPException(status_code=400, detail=f"no character '{body.character}'")
        try:
            rooms.claim(room, body.character)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        roster = _library_roster(room)
    persist.save_room(room)
    return {"ok": True, "room_id": room.id, "character": body.character, "characters": roster}


@router.post("/room/{rid}/ready")
def room_ready(rid: str, body: ReadyBody):
    room = _get_room(rid)
    fired = False
    # Hold the lock across every DB read (in_combat/all_ready) and the shared-state mutation
    # (stage_ready) so they can't race a running turn on the single-threaded connection, nor
    # collide with run_round clearing ready/staged. A deliberate click can wait out a turn.
    with room.lock:
        if rooms.in_combat(room):
            raise HTTPException(status_code=409, detail="in combat — act on your turn instead of the ready-check")
        rooms.stage_ready(room, body.character, body.text, ready=body.ready)
        if rooms.all_ready(room) and not room.busy:
            room.busy = True
            try:
                rooms.run_round(room, _run_for(room))
                fired = True
            finally:
                room.busy = False
    persist.save_room(room)
    return {"ok": True, "fired": fired}


@router.post("/room/{rid}/act")
def room_act(rid: str, body: ActBody):
    room = _get_room(rid)
    with room.lock:
        turn = rooms.whose_turn(room)
        if turn is None:
            raise HTTPException(status_code=409, detail="not in combat")
        if turn != body.character:
            raise HTTPException(status_code=409, detail=f"It's {turn}'s turn, not {body.character}'s.")
        room.busy = True
        try:
            rooms.act(room, body.character, body.text, _run_for(room))
        finally:
            room.busy = False
    persist.save_room(room)
    return {"ok": True}


@router.post("/room/{rid}/roll")
def room_roll(rid: str, body: RollBody):
    room = _get_room(rid)
    with room.lock:
        room.busy = True
        try:
            result = rooms.submit_roll(room, body.character, body.die, body.values, _run_for(room))
        except PermissionError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            room.busy = False
    persist.save_room(room)
    return {"ok": True, **result}


@router.post("/room/{rid}/reaction")
def room_reaction(rid: str, body: ReactionBody):
    room = _get_room(rid)
    rooms.queue_reaction(room, body.character, body.text)
    # Drain now if the table's idle; if a beat is mid-flight, it drains when that beat ends.
    if room.lock.acquire(blocking=False):
        try:
            room.busy = True
            processed = rooms.drain_reactions(room, _run_for(room))
        finally:
            room.busy = False
            room.lock.release()
        persist.save_room(room)
        return {"ok": True, "processed": processed}
    persist.save_room(room)   # at least persist the queued reaction
    return {"ok": True, "queued": True}


@router.post("/room/{rid}/advance")
def room_advance(rid: str):
    room = _get_room(rid)
    with room.lock:
        if not rooms.in_combat(room):
            raise HTTPException(status_code=409, detail="not in combat")
        room.busy = True
        try:
            rooms.advance(room, _run_for(room))
        finally:
            room.busy = False
    persist.save_room(room)
    return {"ok": True}


@router.get("/room/{rid}/checkpoints")
def room_checkpoints(rid: str):
    """The rewindable moments, newest last, labelled by what was happening."""
    return {"checkpoints": rooms.list_checkpoints(_get_room(rid))}


@router.post("/room/{rid}/redo")
def room_redo(rid: str):
    """Redo the last beat: roll back to just before the last action and re-run it (a fresh take)."""
    room = _get_room(rid)
    with room.lock:
        if not room.checkpoints:
            raise HTTPException(status_code=400, detail="nothing to redo yet")
        room.busy = True
        try:
            rooms.restore_checkpoint(room, len(room.checkpoints) - 1, _run_for(room), do_replay=True)
        finally:
            room.busy = False
    persist.save_room(room)
    return {"ok": True}


@router.post("/room/{rid}/rewind")
def room_rewind(rid: str, body: RewindBody):
    """Rewind to a chosen earlier moment (no replay — the players re-decide from there)."""
    room = _get_room(rid)
    with room.lock:
        if not (0 <= body.to < len(room.checkpoints)):
            raise HTTPException(status_code=400, detail="no such checkpoint")
        room.busy = True
        try:
            rooms.restore_checkpoint(room, body.to, _run_for(room), do_replay=False)
        finally:
            room.busy = False
    persist.save_room(room)
    return {"ok": True}


@router.get("/room/{rid}/state")
def room_state(rid: str, since: int = 0):
    room = _get_room(rid)
    # The room's SQLite connection is single-threaded and a running DM turn holds room.lock
    # while it reads/writes the DB across many calls. A poll must NOT touch that connection
    # concurrently (sqlite raises InterfaceError). So try the lock without blocking: when idle
    # we read fresh world state and cache it; when a turn is mid-flight we serve the pure-Python
    # fields plus the last cached snapshot, and the client keeps its spinner (busy=True) until
    # the next idle poll picks up the result. This never blocks and never races the turn.
    if room.lock.acquire(blocking=False):
        try:
            payload = rooms.state_since(room, since)
            payload["scene"] = scene_payload(room.conn, room.cid, "", room.id)
            payload["party"] = party_payload(room.conn, room.cid)
            payload["encounter"] = encounter_payload(room.conn, room.cid)
            room.snapshot = {k: payload[k] for k in (*rooms.DB_FIELDS, "scene", "party", "encounter")}
        finally:
            room.lock.release()
    else:
        payload = rooms.state_meta(room, since)
        payload.update(room.snapshot or {k: None for k in (*rooms.DB_FIELDS, "scene", "party", "encounter")})
        payload.setdefault("mode", "explore")
    return payload


@router.get("/room/{rid}/sheet/{character}")
def room_sheet(rid: str, character: str):
    room = _get_room(rid)
    # Reads the shared connection — take the lock so it can't race a running turn.
    with room.lock:
        try:
            return character_sheet(room.conn, room.cid, character)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such character")


@router.get("/sessions")
def list_sessions():
    """Saved single-player games, for the /play resume list."""
    return {"sessions": persist.list_sessions(SESSIONS)}


@router.get("/session/{session_id}")
def resume_session(session_id: str):
    """Resume a single-player game: return the scene the player left off on."""
    s = _session(session_id)
    return s.get("last_scene") or scene_payload(s["conn"], s["cid"], "You return to the adventure.", session_id)


@router.delete("/session/{session_id}")
def delete_session(session_id: str):
    if not persist.delete_session(SESSIONS, session_id):
        raise HTTPException(status_code=404, detail="no such session")
    return {"ok": True}


@router.get("/rooms")
def list_rooms():
    """Saved tables, for the lobby's resume list."""
    return {"rooms": persist.list_saved(rooms)}


@router.delete("/room/{rid}")
def room_delete(rid: str):
    """Permanently delete a saved table (its DB + sidecar). Refuses if a turn is mid-flight."""
    room = rooms.ROOMS.get(rid)
    if room is not None:
        if not room.lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="table is busy — try again in a moment")
        try:
            existed = persist.delete_room(rooms, rid)
        finally:
            room.lock.release()
    else:
        existed = persist.delete_room(rooms, rid)   # only files on disk (not loaded)
    if not existed:
        raise HTTPException(status_code=404, detail="no such table")
    return {"ok": True}


@router.post("/buy")
def buy(_: BuyRequest):
    # No shop economy in this build; surface a clean message rather than 500.
    return {"success": False, "message": "This build has no shop yet."}


app = FastAPI(title="dnd-dm web adapter")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router, prefix="/api/play")

# Reload any saved tables + single-player games on boot so a week-later resume just works.
try:
    _r = persist.load_rooms(rooms)
    _s = persist.load_sessions(SESSIONS)
    if _r or _s:
        print(f"[persist] resumed {_r} table(s), {_s} single-player game(s)", flush=True)
except Exception as _e:  # never let a bad sidecar block startup
    print(f"[persist] reload failed: {_e}", flush=True)


@app.get("/")
def root():
    return {"ok": True, "play": "POST /api/play/start_session", "sessions": len(SESSIONS)}
