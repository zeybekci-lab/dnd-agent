"""The canon layer — compiles a campaign into SQLite and reads/writes world state.

This is the single source of truth the model never holds in its head. A campaign
YAML is *compiled* into the tables once; thereafter the DB is authoritative and
the model touches it only through the tools in tools.py.

Episodic recall borrows LoreKit's NPC-memory scoring: events are ranked by
importance × recency decay, not just "most recent", so an important betrayal
hours ago still surfaces over trivial recent chatter.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import yaml

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
RECENCY_HALF_LIFE_TURNS = 40.0  # an event's recency weight halves every ~40 turns


# ─────────────────────────────── connection ───────────────────────────────
def connect(db_path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False: the web adapter runs sync endpoints in a threadpool,
    # so a session's connection may be touched from different threads. Safe here
    # because play is single-user and turns are sequential (no concurrent writes).
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created by an earlier schema (SQLite
    CREATE TABLE IF NOT EXISTS won't add new columns to an existing table)."""
    def has_col(table, col):
        return col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    adds = [
        ("game_session", "combat_round", "INTEGER NOT NULL DEFAULT 0"),
        ("game_session", "combat_zones", "TEXT"),
        ("combatant", "zone", "TEXT"),
        ("combatant", "death_saves", "TEXT"),
        ("campaign", "overview", "TEXT"),
        ("pc", "resources", "TEXT"),
    ]
    changed = False
    for table, col, decl in adds:
        if not has_col(table, col):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            changed = True
    # Companions: NPCs traveling with / allied to the party (Sildar, a won-over goblin, …). Persist
    # across scenes; can be summoned into combat as friendly combatants.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS companion ("
        " id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, name TEXT NOT NULL,"
        " note TEXT, hp INTEGER, max_hp INTEGER, stat_block TEXT, active INTEGER NOT NULL DEFAULT 1)")
    if changed:
        conn.commit()


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


# ─────────────────────────────── compilation (YAML -> canon) ───────────────────────────────
def compile_campaign(conn: sqlite3.Connection, source: str | dict) -> int:
    """Load a campaign YAML (path or already-parsed dict) into the canon tables.
    Returns the new campaign id. Idempotent per slug is NOT guaranteed — compile
    into a fresh DB."""
    data = source if isinstance(source, dict) else yaml.safe_load(Path(source).read_text())
    meta = data["meta"]

    cur = conn.execute(
        "INSERT INTO campaign (slug, title, ruleset, starting_scene, overview) VALUES (?,?,?,?,?)",
        (meta["slug"], meta["title"], meta.get("ruleset", "5e-srd-5.1"), meta.get("starting_scene"),
         meta.get("overview") or meta.get("arc")),
    )
    cid = cur.lastrowid

    for loc in data.get("locations", []):
        conn.execute(
            "INSERT INTO location (campaign_id, slug, name, description, read_aloud, region) VALUES (?,?,?,?,?,?)",
            (cid, loc["slug"], loc["name"], loc.get("description"), loc.get("read_aloud"), loc.get("region")),
        )
    for n in data.get("npcs", []):
        conn.execute(
            "INSERT INTO npc (campaign_id, slug, name, role, location_slug, persona, knowledge, secrets, disposition, state)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, n["slug"], n["name"], n.get("role"), n.get("location"), n.get("persona"),
             n.get("knowledge"), n.get("secrets"), int(n.get("disposition", 0)), _j(n.get("state"))),
        )
    for m in data.get("monsters", []):
        conn.execute(
            "INSERT INTO monster (campaign_id, slug, name, statblock) VALUES (?,?,?,?)",
            (cid, m["slug"], m["name"], _j(m["statblock"])),
        )
    for it in data.get("items", []):
        conn.execute(
            "INSERT INTO item (campaign_id, slug, name, description, properties) VALUES (?,?,?,?,?)",
            (cid, it["slug"], it["name"], it.get("description"), _j(it.get("properties"))),
        )
    for p in data.get("pcs", []):
        add_pc(conn, cid, p["name"], p.get("sheet", {}), int(p.get("max_hp", 1)),
               player=p.get("player"), location=p.get("location"))
    for q in data.get("quests", []):
        conn.execute(
            "INSERT INTO quest (campaign_id, slug, title, summary, status, steps) VALUES (?,?,?,?,?,?)",
            (cid, q["slug"], q["title"], q.get("summary"), q.get("status", "inactive"), _j(q.get("steps"))),
        )
    for key, value in (data.get("flags") or {}).items():
        conn.execute("INSERT INTO flag (campaign_id, key, value) VALUES (?,?,?)", (cid, key, _j(value)))
    for f in data.get("factions", []):
        conn.execute(
            "INSERT INTO faction_standing (campaign_id, faction, standing) VALUES (?,?,?)",
            (cid, f["faction"], int(f.get("standing", 0))),
        )
    for s in data.get("scenes", []):
        conn.execute(
            "INSERT INTO scene (campaign_id, slug, title, location_slug, read_aloud, dm_notes, triggers, transitions)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (cid, s["slug"], s["title"], s.get("location"), s.get("read_aloud"), s.get("dm_notes"),
             _j(s.get("triggers")), _j(s.get("transitions"))),
        )

    start = meta.get("starting_scene")
    conn.execute(
        "INSERT INTO game_session (campaign_id, number, current_scene) VALUES (?,1,?)", (cid, start)
    )
    if start:
        conn.execute("UPDATE scene SET status='active' WHERE campaign_id=? AND slug=?", (cid, start))
    conn.commit()
    return cid


def add_pc(conn, campaign_id, name, sheet, max_hp, *, player=None, location=None) -> int:
    cur = conn.execute(
        "INSERT INTO pc (campaign_id, name, player, sheet, max_hp, current_hp, location_slug, resources)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (campaign_id, name, player, _j(sheet), max_hp, max_hp, location, _j(init_resources(sheet))),
    )
    conn.commit()
    return cur.lastrowid


# ─────────────────────────────── resources, slots & rests ───────────────────────────────
# The sheet DEFINES maxima:
#   features: [{name, uses, recharge: "short"|"long", effect: {heal: "1d10+1"}, ...}]
#   spell_slots: {"1": 2, "2": 0, ...}   hit_dice: {die: "d10", count: <level>}
# pc.resources holds the CURRENT remaining counts, refreshed by rests.
def _lay_on_hands_pool(sheet: dict) -> int:
    """Paladin Lay on Hands pool = 5 × level, if the character has the feature; else 0."""
    if any((f.get("name") or "").lower().startswith("lay on hands") for f in sheet.get("features", [])):
        return 5 * int(sheet.get("level", 1))
    return 0


def init_resources(sheet: dict) -> dict:
    feats = {f["name"]: int(f["uses"]) for f in sheet.get("features", []) if f.get("uses")}
    slots = {str(k): int(v) for k, v in (sheet.get("spell_slots") or {}).items() if int(v) > 0}
    hd = int((sheet.get("hit_dice") or {}).get("count", sheet.get("level", 1)))
    gold = int((sheet.get("coins") or {}).get("gp", 0))
    res = {"features": feats, "spell_slots": slots, "hit_dice": hd, "gold": gold}
    loh = _lay_on_hands_pool(sheet)
    if loh:
        res["lay_on_hands"] = loh
    return res


def get_resources(conn, campaign_id: int, pc) -> dict:
    sheet = json.loads(pc["sheet"])
    res = json.loads(pc["resources"]) if pc["resources"] else init_resources(sheet)
    res.setdefault("gold", int((sheet.get("coins") or {}).get("gp", 0)))  # backfill older saves
    loh = _lay_on_hands_pool(sheet)
    if loh and "lay_on_hands" not in res:
        res["lay_on_hands"] = loh
    return res


def _save_resources(conn, pc_id: int, res: dict) -> None:
    conn.execute("UPDATE pc SET resources=? WHERE id=?", (_j(res), pc_id))
    conn.commit()


def spend_feature(conn, campaign_id: int, id_or_name, feature: str):
    pc = resolve_pc(conn, campaign_id, id_or_name)
    if pc is None:
        raise KeyError(f"no PC '{id_or_name}'")
    res = get_resources(conn, campaign_id, pc)
    if feature not in res["features"]:
        raise KeyError(f"{pc['name']} has no limited-use feature '{feature}'")
    if res["features"][feature] <= 0:
        raise ValueError(f"{pc['name']}'s {feature} is already used — it recharges on a rest")
    res["features"][feature] -= 1
    _save_resources(conn, pc["id"], res)
    return pc, res["features"][feature]


def spend_slot(conn, campaign_id: int, id_or_name, level: int):
    pc = resolve_pc(conn, campaign_id, id_or_name)
    if pc is None:
        raise KeyError(f"no PC '{id_or_name}'")
    if int(level) <= 0:
        return pc, None  # cantrip — no slot
    res = get_resources(conn, campaign_id, pc)
    lvl = str(level)
    if res["spell_slots"].get(lvl, 0) <= 0:
        raise ValueError(f"{pc['name']} has no level-{lvl} spell slots left")
    res["spell_slots"][lvl] -= 1
    _save_resources(conn, pc["id"], res)
    return pc, res["spell_slots"][lvl]


def rest(conn, campaign_id: int, id_or_name, kind: str = "long"):
    pc = resolve_pc(conn, campaign_id, id_or_name)
    if pc is None:
        raise KeyError(f"no PC '{id_or_name}'")
    sheet = json.loads(pc["sheet"])
    res = get_resources(conn, campaign_id, pc)
    if kind == "short":
        for f in sheet.get("features", []):
            if f.get("uses") and f.get("recharge") == "short":
                res["features"][f["name"]] = int(f["uses"])
    else:  # long rest: full refresh, heal to max, regain half hit dice
        fresh = init_resources(sheet)
        max_hd = fresh["hit_dice"]
        fresh["hit_dice"] = min(max_hd, int(res.get("hit_dice", max_hd)) + max(1, max_hd // 2))
        res = fresh
        conn.execute("UPDATE pc SET current_hp=max_hp, conditions='[]' WHERE id=?", (pc["id"],))
    _save_resources(conn, pc["id"], res)
    return resolve_pc(conn, campaign_id, pc["id"])


def rest_party(conn, campaign_id: int, kind: str = "long") -> list[str]:
    names = []
    for pc in party(conn, campaign_id):
        rest(conn, campaign_id, pc["id"], kind)
        names.append(pc["name"])
    return names


# ─────────────────────────────── reads ───────────────────────────────
def current_session(conn, campaign_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM game_session WHERE campaign_id=? ORDER BY number DESC LIMIT 1", (campaign_id,)
    ).fetchone()


def get_scene(conn, campaign_id: int, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM scene WHERE campaign_id=? AND slug=?", (campaign_id, slug)).fetchone()


def npcs_at(conn, campaign_id: int, location_slug: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM npc WHERE campaign_id=? AND location_slug=? AND alive=1", (campaign_id, location_slug)
    ).fetchall()


def party(conn, campaign_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pc WHERE campaign_id=? AND active=1", (campaign_id,)).fetchall()


def resolve_pc(conn, campaign_id: int, id_or_name: int | str) -> sqlite3.Row | None:
    """Resolve a PC by numeric id or case-insensitive name (LoreKit ergonomic)."""
    if isinstance(id_or_name, int) or str(id_or_name).isdigit():
        return conn.execute("SELECT * FROM pc WHERE campaign_id=? AND id=?", (campaign_id, int(id_or_name))).fetchone()
    return conn.execute(
        "SELECT * FROM pc WHERE campaign_id=? AND lower(name)=lower(?)", (campaign_id, str(id_or_name))
    ).fetchone()


def get_flag(conn, campaign_id: int, key: str) -> Any:
    row = conn.execute("SELECT value FROM flag WHERE campaign_id=? AND key=?", (campaign_id, key)).fetchone()
    return None if row is None else json.loads(row["value"])


def active_quests(conn, campaign_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM quest WHERE campaign_id=? AND status='active'", (campaign_id,)
    ).fetchall()


# ─────────────────────────────── writes / mutations ───────────────────────────────
def set_flag(conn, campaign_id: int, key: str, value: Any) -> None:
    if value is None:                       # None clears the flag (the value column is NOT NULL)
        conn.execute("DELETE FROM flag WHERE campaign_id=? AND key=?", (campaign_id, key))
    else:
        conn.execute(
            "INSERT INTO flag (campaign_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(campaign_id, key) DO UPDATE SET value=excluded.value",
            (campaign_id, key, _j(value)),
        )
    conn.commit()


def change_hp(conn, campaign_id: int, id_or_name, delta: int) -> sqlite3.Row:
    pc = resolve_pc(conn, campaign_id, id_or_name)
    if pc is None:
        raise KeyError(f"no PC '{id_or_name}'")
    new_hp = max(0, min(pc["max_hp"], pc["current_hp"] + int(delta)))
    conn.execute("UPDATE pc SET current_hp=? WHERE id=?", (new_hp, pc["id"]))
    conn.commit()
    return resolve_pc(conn, campaign_id, pc["id"])


# ─────────────────────────────── loot & gold ───────────────────────────────
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "item"


def add_gold(conn, campaign_id: int, delta: int) -> int:
    new = max(0, int(get_flag(conn, campaign_id, "gold") or 0) + int(delta))
    set_flag(conn, campaign_id, "gold", new)
    return new


def recruit_companion(conn, campaign_id: int, name: str, note: str | None = None,
                      hp: int | None = None, max_hp: int | None = None, stat_block: str | None = None):
    """Register (or update) an NPC traveling with / allied to the party. Idempotent by name."""
    row = conn.execute("SELECT id FROM companion WHERE campaign_id=? AND name=?", (campaign_id, name)).fetchone()
    if row:
        sets, vals = [], []
        for col, v in (("note", note), ("hp", hp), ("max_hp", max_hp), ("stat_block", stat_block)):
            if v is not None:
                sets.append(f"{col}=?"); vals.append(v)
        sets.append("active=1")
        conn.execute(f"UPDATE companion SET {', '.join(sets)} WHERE id=?", (*vals, row["id"]))
    else:
        conn.execute("INSERT INTO companion (campaign_id, name, note, hp, max_hp, stat_block, active)"
                     " VALUES (?,?,?,?,?,?,1)", (campaign_id, name, note, hp, max_hp, stat_block))
    conn.commit()


def list_companions(conn, campaign_id: int, active_only: bool = True) -> list:
    q = "SELECT * FROM companion WHERE campaign_id=?" + (" AND active=1" if active_only else "")
    return conn.execute(q, (campaign_id,)).fetchall()


def get_companion(conn, campaign_id: int, name: str):
    return conn.execute("SELECT * FROM companion WHERE campaign_id=? AND name=? AND active=1",
                        (campaign_id, name)).fetchone()


def release_companion(conn, campaign_id: int, name: str) -> bool:
    cur = conn.execute("UPDATE companion SET active=0 WHERE campaign_id=? AND name=?", (campaign_id, name))
    conn.commit()
    return cur.rowcount > 0


def set_companion_hp(conn, campaign_id: int, name: str, hp: int) -> None:
    conn.execute("UPDATE companion SET hp=? WHERE campaign_id=? AND name=?", (max(0, int(hp)), campaign_id, name))
    conn.commit()


def spend_hit_dice(conn, campaign_id: int, id_or_name, n: int):
    """Decrement a PC's remaining hit dice (short-rest healing). Returns (pc, remaining)."""
    pc = resolve_pc(conn, campaign_id, id_or_name)
    if pc is None:
        raise KeyError(f"no PC '{id_or_name}'")
    res = get_resources(conn, campaign_id, pc)
    res["hit_dice"] = max(0, int(res.get("hit_dice", 0)) - int(n))
    _save_resources(conn, pc["id"], res)
    return pc, res["hit_dice"]


def add_pc_gold(conn, campaign_id: int, id_or_name, delta: int):
    """Per-character gold: loot goes to whoever collected it, not a shared party pool."""
    pc = resolve_pc(conn, campaign_id, id_or_name)
    if pc is None:
        raise KeyError(f"no PC '{id_or_name}'")
    res = get_resources(conn, campaign_id, pc)
    res["gold"] = max(0, int(res.get("gold", 0)) + int(delta))
    _save_resources(conn, pc["id"], res)
    return pc, res["gold"]


def grant_item(conn, campaign_id: int, pc_name, name: str, quantity: int = 1,
               description: str | None = None, properties: dict | None = None,
               equipped: bool = False) -> sqlite3.Row:
    """Give an item to a PC's pack, creating the item record if it doesn't exist."""
    pc = resolve_pc(conn, campaign_id, pc_name)
    if pc is None:
        raise KeyError(f"no PC '{pc_name}'")
    slug = _slug(name)
    if conn.execute("SELECT 1 FROM item WHERE campaign_id=? AND slug=?", (campaign_id, slug)).fetchone() is None:
        conn.execute("INSERT INTO item (campaign_id, slug, name, description, properties) VALUES (?,?,?,?,?)",
                     (campaign_id, slug, name, description, _j(properties)))
    inv = conn.execute("SELECT id FROM inventory WHERE owner_type='pc' AND owner_id=? AND item_slug=?",
                       (pc["id"], slug)).fetchone()
    if inv:
        conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?", (int(quantity), inv["id"]))
        if equipped:
            conn.execute("UPDATE inventory SET equipped=1 WHERE id=?", (inv["id"],))
    else:
        conn.execute("INSERT INTO inventory (owner_type, owner_id, item_slug, quantity, equipped) VALUES ('pc',?,?,?,?)",
                     (pc["id"], slug, int(quantity), 1 if equipped else 0))
    conn.commit()
    return pc


def consume_item(conn, campaign_id: int, pc_name, item_name: str) -> tuple[str, dict]:
    """Remove one of an item from a PC's pack. Returns (item name, properties)."""
    pc = resolve_pc(conn, campaign_id, pc_name)
    if pc is None:
        raise KeyError(f"no PC '{pc_name}'")
    inv = conn.execute(
        "SELECT inv.id, inv.quantity, i.name, i.properties FROM inventory inv "
        "JOIN item i ON i.campaign_id=? AND i.slug=inv.item_slug "
        "WHERE inv.owner_type='pc' AND inv.owner_id=? AND (inv.item_slug=? OR lower(i.name) LIKE ?)",
        (campaign_id, pc["id"], _slug(item_name), f"%{item_name.lower()}%")).fetchone()
    if inv is None:
        raise KeyError(f"{pc['name']} has no '{item_name}'")
    if inv["quantity"] > 1:
        conn.execute("UPDATE inventory SET quantity=quantity-1 WHERE id=?", (inv["id"],))
    else:
        conn.execute("DELETE FROM inventory WHERE id=?", (inv["id"],))
    conn.commit()
    return inv["name"], (json.loads(inv["properties"]) if inv["properties"] else {})


def advance_scene(conn, campaign_id: int, scene_slug: str) -> sqlite3.Row:
    sess = current_session(conn, campaign_id)
    if sess["current_scene"]:
        conn.execute("UPDATE scene SET status='cleared' WHERE campaign_id=? AND slug=? AND status='active'",
                     (campaign_id, sess["current_scene"]))
    conn.execute("UPDATE game_session SET current_scene=? WHERE id=?", (scene_slug, sess["id"]))
    conn.execute("UPDATE scene SET status='active' WHERE campaign_id=? AND slug=?", (campaign_id, scene_slug))
    conn.commit()
    scene = get_scene(conn, campaign_id, scene_slug)
    if scene is None:
        raise KeyError(f"no scene '{scene_slug}'")
    return scene


def update_npc(conn, campaign_id: int, slug: str, *, disposition_delta: int = 0,
               alive: bool | None = None, state: dict | None = None) -> sqlite3.Row:
    npc = conn.execute("SELECT * FROM npc WHERE campaign_id=? AND slug=?", (campaign_id, slug)).fetchone()
    if npc is None:
        raise KeyError(f"no NPC '{slug}'")
    new_disp = max(-100, min(100, npc["disposition"] + int(disposition_delta)))
    fields, params = ["disposition=?"], [new_disp]
    if alive is not None:
        fields.append("alive=?"); params.append(1 if alive else 0)
    if state is not None:
        fields.append("state=?"); params.append(_j(state))
    params.append(npc["id"])
    conn.execute(f"UPDATE npc SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    return conn.execute("SELECT * FROM npc WHERE id=?", (npc["id"],)).fetchone()


# ─────────────────────────────── episodic memory ───────────────────────────────
def log_event(conn, campaign_id: int, *, kind: str, summary: str, turn: int | None = None,
              detail: str | None = None, entities: list | None = None, location: str | None = None,
              tags: list | None = None, importance: float = 0.5) -> int:
    sess = current_session(conn, campaign_id)
    tags = list(tags or [])
    tags.append(f"importance:{importance:.2f}")  # stored in tags; schema stays simple
    cur = conn.execute(
        "INSERT INTO event_log (campaign_id, session_id, turn, kind, summary, detail, entities, location_slug, tags)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (campaign_id, sess["id"] if sess else None, turn, kind, summary, detail,
         _j(entities), location, _j(tags)),
    )
    conn.commit()
    return cur.lastrowid


def _importance_of(tags_json: str | None) -> float:
    if not tags_json:
        return 0.5
    for t in json.loads(tags_json):
        if isinstance(t, str) and t.startswith("importance:"):
            try:
                return float(t.split(":", 1)[1])
            except ValueError:
                pass
    return 0.5


def recall(conn, campaign_id: int, *, entities: list | None = None, location: str | None = None,
           tags: list | None = None, text: str | None = None, current_turn: int | None = None,
           limit: int = 6) -> list[dict]:
    """Structured episodic recall. Filters by any provided dimension, then ranks
    by importance × recency decay (LoreKit-style) so important old beats beat
    trivial recent ones. With no filters, returns the top-ranked recent events."""
    rows = conn.execute(
        "SELECT * FROM event_log WHERE campaign_id=? ORDER BY id DESC LIMIT 400", (campaign_id,)
    ).fetchall()
    max_turn = current_turn if current_turn is not None else (rows[0]["turn"] if rows and rows[0]["turn"] else 0)

    scored = []
    for r in rows:
        if location and r["location_slug"] != location:
            continue
        if entities:
            row_ents = set(json.loads(r["entities"]) if r["entities"] else [])
            if not row_ents & set(entities):
                continue
        if tags:
            row_tags = set(json.loads(r["tags"]) if r["tags"] else [])
            if not row_tags & set(tags):
                continue
        if text and text.lower() not in (r["summary"] or "").lower() and text.lower() not in (r["detail"] or "").lower():
            continue
        imp = _importance_of(r["tags"])
        age = max(0, (max_turn or 0) - (r["turn"] or 0))
        recency = math.pow(0.5, age / RECENCY_HALF_LIFE_TURNS)
        scored.append((imp * recency, dict(r)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]
