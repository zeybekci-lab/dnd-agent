"""Durable save/resume for multiplayer tables.

What persists: the game DB (HP, combat, flags, resources, inventory, scene, event_log —
everything mechanical) lives in a per-room SQLite FILE, so it survives a restart for free.
Alongside it we save a small JSON sidecar with the room's transcript, claimed characters,
sequence counter, and queued reactions.

What does NOT persist: the raw LLM conversation history (it holds SDK block objects and is
expensive/fragile to serialize). On resume the DM simply re-grounds the way it always does —
get_state for the current scene/party/combat, recall for recent beats from the event_log.
That's the retrieve-don't-memorize design, so a week-later resume needs no chat replay.
"""

from __future__ import annotations

import json
import pathlib

from engine import pending, state

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOMS_DIR = ROOT / "data" / "rooms"
SESSIONS_DIR = ROOT / "data" / "sessions"


def db_path(rid: str) -> str:
    ROOMS_DIR.mkdir(parents=True, exist_ok=True)   # ensure the dir exists before sqlite opens the file
    return str(ROOMS_DIR / f"{rid}.db")


def ckpt_dir(rid: str) -> pathlib.Path:
    d = ROOMS_DIR / f"{rid}.ckpt"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(rid: str) -> pathlib.Path:
    return ROOMS_DIR / f"{rid}.json"


def save_room(room) -> None:
    """Snapshot the room's Python-side state. The DB has already committed itself."""
    ROOMS_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": room.id, "cid": room.cid, "turn": room.turn,
        "claimed": room.claimed, "seq": room.seq,
        "transcript": room.transcript, "reactions": room.reactions,
    }
    tmp = _meta_path(room.id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta))
    tmp.replace(_meta_path(room.id))   # atomic: never leave a half-written sidecar


def load_rooms(rooms_module) -> int:
    """Reopen every saved table into the in-memory registry. Returns how many loaded."""
    if not ROOMS_DIR.exists():
        return 0
    n = 0
    for f in sorted(ROOMS_DIR.glob("*.json")):
        try:
            meta = json.loads(f.read_text())
            rid = meta["id"]
            if rid in rooms_module.ROOMS:        # already live (e.g. after a hot reload)
                continue
            conn = state.connect(db_path(rid))   # reopens the existing file; schema is IF NOT EXISTS
            pending.enable(conn)
            room = rooms_module.Room(
                id=rid, conn=conn, cid=meta["cid"], turn=meta.get("turn", 1),
                claimed=meta.get("claimed", {}) or {}, seq=meta.get("seq", 0),
                transcript=meta.get("transcript", []) or [],
                reactions=meta.get("reactions", []) or [], history=[])
            rooms_module.ROOMS[rid] = room
            n += 1
        except Exception as e:   # a corrupt sidecar shouldn't take down the server
            print(f"[persist] failed to load {f.name}: {e}", flush=True)
    return n


def delete_room(rooms_module, rid: str) -> bool:
    """Remove a table from the registry and delete its files. Returns True if anything existed."""
    room = rooms_module.ROOMS.pop(rid, None)
    existed = room is not None
    if room is not None:
        try:
            room.conn.close()
        except Exception:
            pass
    for p in (_meta_path(rid), ROOMS_DIR / f"{rid}.db"):
        if p.exists():
            p.unlink()
            existed = True
    return existed


# ─────────────────────────── single-player sessions ───────────────────────────
def session_db_path(sid: str) -> str:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return str(SESSIONS_DIR / f"{sid}.db")


def _session_meta_path(sid: str) -> pathlib.Path:
    return SESSIONS_DIR / f"{sid}.json"


def save_session(sid: str, sess: dict) -> None:
    """Persist a single-player game. `last_scene` is the most recent scene payload so the page
    can render exactly where the player left off on resume; the DB holds all the mechanics."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    meta = {"id": sid, "cid": sess["cid"], "turn": sess.get("turn", 1), "last_scene": sess.get("last_scene")}
    tmp = _session_meta_path(sid).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta))
    tmp.replace(_session_meta_path(sid))


def load_sessions(sessions: dict) -> int:
    if not SESSIONS_DIR.exists():
        return 0
    n = 0
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            meta = json.loads(f.read_text())
            sid = meta["id"]
            if sid in sessions:
                continue
            conn = state.connect(session_db_path(sid))   # single-player: no pending (DM auto-rolls)
            sessions[sid] = {"conn": conn, "cid": meta["cid"], "history": [],
                             "turn": meta.get("turn", 1), "last_scene": meta.get("last_scene")}
            n += 1
        except Exception as e:
            print(f"[persist] failed to load session {f.name}: {e}", flush=True)
    return n


def list_sessions(sessions: dict) -> list[dict]:
    out = []
    for sid, s in sessions.items():
        try:
            camp = s["conn"].execute("SELECT title FROM campaign WHERE id=?", (s["cid"],)).fetchone()
            title = camp["title"] if camp else "Adventure"
        except Exception:
            title = "Adventure"
        out.append({"id": sid, "title": title, "turns": s.get("turn", 1)})
    return out


def delete_session(sessions: dict, sid: str) -> bool:
    s = sessions.pop(sid, None)
    existed = s is not None
    if s is not None:
        try:
            s["conn"].close()
        except Exception:
            pass
    for p in (_session_meta_path(sid), SESSIONS_DIR / f"{sid}.db"):
        if p.exists():
            p.unlink()
            existed = True
    return existed


def list_saved(rooms_module) -> list[dict]:
    """Lightweight summary of every live/saved table for the lobby's resume list."""
    out = []
    for rid, room in rooms_module.ROOMS.items():
        try:
            camp = room.conn.execute("SELECT title FROM campaign WHERE id=?", (room.cid,)).fetchone()
            title = camp["title"] if camp else "Adventure"
        except Exception:
            title = "Adventure"
        out.append({"id": rid, "title": title,
                    "claimed": sorted(room.claimed), "turns": room.turn})
    return out
