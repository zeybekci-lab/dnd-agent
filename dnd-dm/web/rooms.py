"""Multiplayer 'table' rooms — shared world, one device per character.

Two interaction modes, matching D&D's own rhythm:

  • OUT OF COMBAT — ready-check batching. Each player stages an action and taps
    "ready" (the Red Tick). When every active claimed player is ready, the whole
    batch resolves as ONE beat ("Bram does X, Mira does Y"). No one specifies who
    is talking — each page is bound to a character.

  • IN COMBAT — initiative turn-lock. Only the character whose turn it is may act;
    the DM also plays out monsters and AI companions until the next player's turn.

One DM turn runs at a time per room (a lock), so the shared world never has two
concurrent writers. The model call is injected as a `run(player_input)` callable
so this module stays engine-only and unit-testable without the API.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field

from engine import combat, pending, rules, state
from web import persist

# Backstop regex: the DM asked for a skill check in prose ("roll a Perception check", "make an
# Investigation check") — used to auto-park the dice prompt if it forgot to call roll_check.
_SKILL_WORDS = "|".join(sorted((k.replace("_", " ") for k in rules.SKILLS), key=len, reverse=True))
_CHECK_RE = re.compile(rf"\b(?:roll|make)\b[^.\n]{{0,40}}\b({_SKILL_WORDS})\b\s+check\b", re.I)


@dataclass
class Room:
    id: str
    conn: object
    cid: int
    history: list = field(default_factory=list)
    turn: int = 1
    claimed: dict = field(default_factory=dict)   # character name -> True
    staged: dict = field(default_factory=dict)    # character -> staged action text (explore)
    ready: set = field(default_factory=set)        # characters marked ready (explore)
    transcript: list = field(default_factory=list)  # [{seq, kind, who, text, action_log}]
    seq: int = 0
    busy: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    snapshot: dict = field(default_factory=dict)   # last good DB-derived sync fields (served while a turn runs)
    reactions: list = field(default_factory=list)  # queued out-of-turn reactions [{character, text}]
    checkpoints: list = field(default_factory=list)  # rewind ring: snapshots taken before each action
    ckpt_n: int = 0                                   # monotonic counter for snapshot filenames

    def add(self, kind: str, text: str, who: str | None = None, action_log=None) -> None:
        # Drop empty DM beats (a turn that produced no narration — e.g. a surprise-round monster
        # doing nothing) and back-to-back identical ones (a model stutter), so neither blank lines
        # nor repeats reach the screen or burn a seq.
        if kind == "dm":
            if not (text or "").strip():
                return
            if self.transcript and self.transcript[-1]["kind"] == "dm" \
                    and (self.transcript[-1]["text"] or "").strip() == text.strip():
                return
        self.seq += 1
        self.transcript.append({"seq": self.seq, "kind": kind, "who": who,
                                "text": text, "action_log": action_log})


ROOMS: dict[str, Room] = {}


# ─────────────────────────────── lifecycle ───────────────────────────────
def create_room(conn, cid, rid: str | None = None) -> Room:
    rid = rid or uuid.uuid4().hex[:6].upper()  # short join code
    room = Room(id=rid, conn=conn, cid=cid)
    sess = state.current_session(conn, cid)
    scene = state.get_scene(conn, cid, sess["current_scene"]) if sess["current_scene"] else None
    room.add("dm", scene["read_aloud"] if scene and scene["read_aloud"] else "Your adventure begins.")
    ROOMS[rid] = room
    return room


def get(rid: str) -> Room:
    r = ROOMS.get(rid.upper())
    if r is None:
        raise KeyError(f"no room '{rid}'")
    return r


# ─────────────────────────────── rewind / redo (checkpoints) ───────────────────────────────
MAX_CHECKPOINTS = 15


def capture_checkpoint(room: Room, replay: dict) -> None:
    """Snapshot the room BEFORE an action runs, so we can roll back to this moment. Cheap: a SQLite
    online-backup of the (tiny) DB plus copies of the transcript marker + DM history. `replay` records
    the action (so Redo can re-run it). A ring buffer keeps the last MAX_CHECKPOINTS."""
    n = room.ckpt_n = room.ckpt_n + 1
    path = persist.ckpt_dir(room.id) / f"{n}.db"
    dest = sqlite3.connect(str(path))
    with dest:
        room.conn.backup(dest)
    dest.close()
    last_dm = next((e["text"] for e in reversed(room.transcript) if e["kind"] == "dm"), "the adventure's start")
    label = last_dm.strip().replace("\n", " ")
    room.checkpoints.append({
        "seq": room.seq, "turn": room.turn, "claimed": dict(room.claimed),
        "history": list(room.history), "db": str(path), "replay": replay,
        "label": (label[:80] + "…") if len(label) > 80 else label})
    while len(room.checkpoints) > MAX_CHECKPOINTS:
        old = room.checkpoints.pop(0)
        pathlib.Path(old["db"]).unlink(missing_ok=True)


def restore_checkpoint(room: Room, idx: int, run, do_replay: bool) -> None:
    """Roll the room back to checkpoint `idx`: restore the DB (in place, same connection), the
    transcript (truncated to that moment), the DM history, and clear transient combat state. Discards
    that checkpoint and every later one (the abandoned timeline). If do_replay, re-run the action that
    followed (Redo = a fresh take on the same moment); otherwise just leave the players at that moment."""
    ck = room.checkpoints[idx]
    src = sqlite3.connect(ck["db"])
    src.backup(room.conn)          # overwrite the live DB content in place — same conn, so pending stays valid
    src.close()
    room.conn.commit()
    room.history = list(ck["history"])
    room.transcript = [e for e in room.transcript if e["seq"] <= ck["seq"]]
    room.seq, room.turn, room.claimed = ck["seq"], ck["turn"], dict(ck["claimed"])
    room.reactions = []
    pending.clear(room.conn)
    pending.clear_turn(room.conn)
    for old in room.checkpoints[idx:]:
        pathlib.Path(old["db"]).unlink(missing_ok=True)
    room.checkpoints = room.checkpoints[:idx]
    if do_replay:
        r = ck["replay"]
        if r["kind"] == "act":
            act(room, r["character"], r["text"], run)
        elif r["kind"] == "run_round":
            room.staged = dict(r.get("staged", {}))
            room.ready = set(room.staged)
            run_round(room, run)


def list_checkpoints(room: Room) -> list[dict]:
    return [{"i": i, "label": c["label"]} for i, c in enumerate(room.checkpoints)]


# ─────────────────────────────── characters ───────────────────────────────
def roster(room: Room) -> list[dict]:
    return [{"name": p["name"], "player": p["player"], "claimed": bool(room.claimed.get(p["name"]))}
            for p in state.party(room.conn, room.cid)]


def claim(room: Room, character: str) -> None:
    names = {p["name"] for p in state.party(room.conn, room.cid)}
    if character not in names:
        raise ValueError(f"no character '{character}' at this table")
    room.claimed[character] = True


def _claimed_active(room: Room) -> list[str]:
    """Claimed characters still standing (used for ready-checks and turn gating)."""
    party = {p["name"]: p for p in state.party(room.conn, room.cid)}
    return [c for c in room.claimed if c in party and party[c]["current_hp"] > 0]


# ─────────────────────────────── mode ───────────────────────────────
def in_combat(room: Room) -> bool:
    return bool(state.current_session(room.conn, room.cid)["in_combat"])


def whose_turn(room: Room) -> str | None:
    if not in_combat(room):
        return None
    cur = combat.current_turn(room.conn, room.cid)
    return cur["name"] if cur else None


# ─────────────────────────────── out of combat: ready-check batch ───────────────────────────────
def stage_ready(room: Room, character: str, text: str, ready: bool = True) -> None:
    text = text.strip()
    if text:
        room.staged[character] = text
    else:
        room.staged.pop(character, None)  # readying with no text = passing / holding
    if ready:
        room.ready.add(character)
    else:
        room.ready.discard(character)


def all_ready(room: Room) -> bool:
    # Out of combat the fiction governs HP — a downed-but-alive PC (stabilized, captured, recovering)
    # can still take actions, so the ready-check counts every seated player. Using _claimed_active here
    # would deadlock a solo game whose only PC is at 0 HP. (Combat turn-gating still uses _claimed_active.)
    active = set(room.claimed)
    return bool(active) and active.issubset(room.ready)


def run_round(room: Room, run) -> str:
    """Assemble every staged action into one batch and resolve it as a single beat."""
    capture_checkpoint(room, {"kind": "run_round", "staged": dict(room.staged)})
    order = [p["name"] for p in state.party(room.conn, room.cid) if p["name"] in room.staged]
    for name in order:
        room.add("player", room.staged[name], who=name)
    batch = "\n".join(f"{name}: {room.staged[name]}" for name in order) or \
        "(The party takes no deliberate action this turn — let the scene breathe or the world react.)"
    text, alog = run(f"{_player_frame(room)}\n\n{batch}")
    room.add("dm", text, action_log=alog)
    room.staged.clear()
    room.ready.clear()
    _backstop_check(room)                 # if the DM asked for a check in prose, give the player the dice menu
    _pump_monsters(room, run)             # if that beat started combat on a monster's turn, play it through
    drain_reactions(room, run)
    return text


# ─────────────────────────────── in combat: turn-locked act ───────────────────────────────
def _player_frame(room: Room) -> str:
    players = ", ".join(sorted(room.claimed)) or "the players"
    return (f"[{players} are controlled by human players who have written their own actions. Narrate ONLY the "
            f"world's reaction and what NPCs and monsters do and say. Do NOT invent any action, line of dialogue, "
            f"thought, or decision for {players} beyond exactly what they wrote — never put words in their mouths "
            f"or move them. If one of them did nothing, let the world proceed without acting for them. If an outcome "
            f"is uncertain, CALL the roll_check tool (it gives the player a dice prompt) — do NOT just write 'roll a "
            f"check' in your prose; the words alone do nothing. Then ask the players what they do.]")


def _combat_hint(room: Room) -> str:
    players = ", ".join(_claimed_active(room)) or "the players"
    return (f"(Multiplayer combat, turn-locked. Players control: {players}, and they roll their own dice. "
            f"ONE action per turn. The turn passes ONLY when you actually RESOLVE the player's action with a "
            f"tool. If instead you ask a question, ask them to clarify a target, reject an invalid action, or "
            f"they just look around, the turn STAYS with them — they haven't acted, so do NOT advance; just "
            f"answer and wait for their real action. Narrate the lead-in to THIS character's action, call the "
            f"matching tool, then STOP — don't play any other combatant; the engine runs the monsters. "
            f"• Any ATTACK — a weapon OR an attack-roll spell/cantrip like Eldritch Blast, Fire Bolt, a "
            f"ranged spell attack: call the attack tool (it returns a ROLL REQUEST so the PLAYER rolls it). "
            f"NEVER narrate an attack's hit, miss, or damage yourself — that skips their dice. "
            f"• Any OTHER action: resolve it with its tool — cast_spell, use_feature, lay_on_hands, use_item "
            f"(those pass the turn automatically). For Dash/Dodge/Disengage/Help (no specific tool), resolve it "
            f"and call end_turn. "
            f"If they try TWO actions in one turn, resolve only the first and tell them the rest waits. "
            f"NEVER announce whose turn it is, say 'your turn' / 'you're up', address a player to prompt their "
            f"turn, or state round numbers — the interface shows initiative and turn order. You ONLY narrate "
            f"what happens in the fiction, then stop.)")


def act(room: Room, character: str, text: str, run) -> str:
    if pending.get_request(room.conn):
        raise PermissionError("a dice roll is pending — roll it before acting")
    turn_name = whose_turn(room)
    if turn_name is None:
        raise PermissionError("not in combat — use the ready-check instead")
    if turn_name != character:
        raise PermissionError(f"It's {turn_name}'s turn, not {character}'s.")
    capture_checkpoint(room, {"kind": "act", "character": character, "text": text})
    pending.clear_turn(room.conn)
    room.add("player", text, who=character)
    out, alog = run(f"{character}: {text}\n\n{_player_frame(room)} {_combat_hint(room)}")
    room.add("dm", out, action_log=alog)
    # act() only runs when it's already this player's turn in active combat, so their action ENDS
    # their turn. If it parked an attack roll, the turn passes when the dice resolve (submit_roll);
    # otherwise it passes now — even if the DM resolved the action purely in narration (e.g. an
    # Eldritch Blast it didn't route through the attack tool). This stops a player from acting again
    # and again because the turn never advanced.
    if pending.get_request(room.conn):
        pending.clear_turn(room.conn)
        return out                       # awaiting dice — hold reactions until the action resolves
    _backstop_check(room)                 # DM asked for a check in prose? give the player the dice menu
    if pending.get_request(room.conn):
        return out                       # the backstop parked a check roll — wait for it
    if pending.pop_held(room.conn):       # DM explicitly held the turn (awaiting a choice) → keep it
        pending.clear_turn(room.conn)
        return out
    if pending.pop_turn_consumed(room.conn):
        _advance_and_pump(room, run)      # the player RESOLVED an action (a tool fired) → turn passes
    # else: nothing was resolved — a question, a clarification, an invalid/ambiguous target, a free
    # look. The action wasn't used, so the turn STAYS with this player (don't advance).
    drain_reactions(room, run)
    return out


def submit_roll(room: Room, character: str, die: str, values: list[int], run) -> dict:
    """Resolve a player's physical dice roll. Validates the die, applies the result via the
    engine, chains the damage roll on a hit, and — once the action is fully resolved — advances
    the turn and plays the monsters. Raises ValueError for a wrong/ill-formed roll."""
    req = pending.get_request(room.conn)
    if req is None:
        raise ValueError("no roll is awaited right now")
    if character != req["character"]:
        raise PermissionError(f"{req['character']} is rolling, not {character}")
    if die != req["die"]:
        raise ValueError(f"wrong die — you need a {req['die']} ({req['purpose']})")
    if len(values) != req["count"] or any(not (1 <= v <= int(die[1:])) for v in values):
        raise ValueError(f"roll {req['count']}× {req['die']}")

    ctx = req["ctx"]
    if req["step"] == "hit_dice":
        rolled = sum(values)
        heal = max(0, rolled + ctx["con"] * ctx["count"])
        state.spend_hit_dice(room.conn, room.cid, character, ctx["count"])
        after = state.change_hp(room.conn, room.cid, character, heal)
        con_total = ctx["con"] * ctx["count"]
        detail = (f"{character} spends {ctx['count']} hit dice: {len(values)}{req['die']} "
                  f"({', '.join(map(str, values))}){f' + {con_total}' if con_total else ''} = {heal} healed "
                  f"→ {after['current_hp']}/{after['max_hp']}hp")
        room.add("roll", detail, who=character)
        pending.clear(room.conn)
        return {"resolved": True}

    if req["step"] == "death_save":
        detail = combat.resolve_death_save(room.conn, room.cid, character, values[0])
        room.add("roll", detail, who=character)
        pending.clear(room.conn)
        out, alog = run(f"[{character}'s death saving throw resolved: {detail}. Narrate this brief, "
                        f"tense beat — they're unconscious and bleeding out (or just stabilized / slipped "
                        f"away / gasped back). Do NOT roll, reveal the tally numbers, or advance combat.] "
                        f"{_player_frame(room)}", mechanical=True)
        room.add("dm", out, action_log=alog)
        _advance_and_pump(room, run)
        return {"resolved": True}

    if req["step"] == "check":
        # a player skill/ability check: d20 + modifier vs DC, then narrate the outcome (no turn change)
        if ctx.get("advantage") or ctx.get("disadvantage"):
            chosen = max(values) if ctx["advantage"] else min(values)
            base = f"d20 {'adv' if ctx['advantage'] else 'dis'} ({', '.join(map(str, values))})→{chosen}"
        else:
            chosen = values[0]
            base = f"d20 ({chosen})"
        mod, dc = ctx["mod"], ctx["dc"]
        total = chosen + mod
        ok = total >= dc
        sign = "+" if mod >= 0 else "-"
        detail = (f"{character} — {ctx['check']}: {base} {sign} {abs(mod)} = {total} vs DC {dc} "
                  f"→ {'SUCCESS' if ok else 'FAILURE'}")
        room.add("roll", detail, who=character)
        pending.clear(room.conn)
        out, alog = run(f"[{character}'s {ctx['check']} check is resolved: {detail}. Narrate the OUTCOME in "
                        f"the fiction — what this {'success' if ok else 'failure'} reveals or achieves. Do NOT "
                        f"roll again, reveal the DC or your notes, or advance combat.] {_player_frame(room)}",
                        mechanical=True)
        room.add("dm", out, action_log=alog)
        if pending.get_request(room.conn):
            pending.clear(room.conn)
        return {"resolved": True}

    if req["step"] == "tohit":
        v = combat.resolve_pc_tohit(ctx, values)
        room.add("roll", v["detail"], who=character)
        if v["hit"]:
            _request_damage_part(room, character, ctx, v["crit"], parts=ctx["damage_parts"], idx=0, rolled=[])
            return {"resolved": False, "awaiting": "damage"}
        pending.clear(room.conn)            # a miss ends the action
        _after_player_action(room, run, f"{character}'s attack on {ctx['target']} MISSED — {v['detail']}")
        return {"resolved": True}

    # a damage part — accumulate, chain to the next part, or apply the grand total on the last
    parts, idx, crit = req["parts"], req["part_index"], req.get("crit", False)
    total, body = combat.resolve_damage_part(parts[idx]["notation"], values)
    rolled = req.get("rolled", []) + [{"name": parts[idx]["name"], "total": total, "body": body}]
    if idx + 1 < len(parts):
        _request_damage_part(room, character, ctx, crit, parts=parts, idx=idx + 1, rolled=rolled)
        return {"resolved": False, "awaiting": "damage"}
    grand = sum(r["total"] for r in rolled)
    after = combat.apply_pc_damage(room.conn, room.cid, ctx["target"], grand)
    breakdown = " + ".join(f"{r['name']} {r['body']}" for r in rolled)
    down = " — DOWN" if after["current_hp"] == 0 else ""
    detail = (f"{character} → {ctx['target']}: {grand} damage{' CRIT' if crit else ''} "
              f"({breakdown}). {after['name']} {after['current_hp']}/{after['max_hp']}hp{down}")
    room.add("roll", detail, who=character)
    pending.clear(room.conn)
    _after_player_action(room, run, f"{character} HIT {ctx['target']} — {detail}")
    return {"resolved": True}


def _backstop_check(room: Room) -> None:
    """If the DM asked for a skill check in its narration but didn't actually call roll_check (so no
    dice menu appeared), park the roll ourselves at a default DC. Belt-and-suspenders for the soft
    'invoke the tool, don't just narrate it' rule — guarantees the player gets the dice prompt."""
    if pending.get_request(room.conn):
        return
    last = next((e["text"] for e in reversed(room.transcript) if e["kind"] == "dm"), "") or ""
    m = _CHECK_RE.search(last)
    if not m:
        return
    skill = m.group(1).lower().replace(" ", "_")
    humans = list(room.claimed)
    named = [h for h in humans if h.lower() in last.lower()]
    actor = named[0] if named else (humans[0] if len(humans) == 1 else None)
    if not actor:
        return
    from engine import tools
    tools.execute(room.conn, room.cid, "roll_check", {"actor": actor, "check": skill, "dc": 13})


def queue_reaction(room: Room, character: str, text: str) -> None:
    """A player pressed React (possibly mid-beat). Park it; it drains at the next free moment."""
    room.reactions.append({"character": character, "text": (text or "").strip()})


def drain_reactions(room: Room, run) -> int:
    """Process every queued reaction, retroactively. Each is adjudicated against the MOST RECENT
    event: the DM checks the character's sheet/resources, then either applies it (spending the
    reaction + resource and revising the last outcome with tools) or refuses. Caller holds the lock.
    Returns how many were processed."""
    n = 0
    while room.reactions:
        r = room.reactions.pop(0)
        who = r["character"]
        room.add("player", f"⚡ {who} reacts: {r['text']}", who=who)
        out, alog = run(
            f"[REACTION — out of turn, from {who}: \"{r['text']}\". This is retroactive: it may only "
            f"affect the MOST RECENT thing that just happened, nothing earlier. Check {who}'s sheet and "
            f"resources (get_state): do they actually have a reaction/feature/spell that does this, is it "
            f"available (uses left, a free reaction this round), and does it legitimately apply to that last "
            f"event? If YES — spend it (use_feature / cast_spell / a spell slot), use roll_dice for any "
            f"reaction dice, adjust the outcome with the tools (e.g. restore HP that a damage-reduction "
            f"prevents, or recompute a hit a +AC reaction now causes to miss), and narrate the revised "
            f"result. If NO — briefly tell {who} why it can't be done and change nothing. Do not advance "
            f"the turn or act for anyone else.] {_player_frame(room)}")
        room.add("dm", out, action_log=alog)
        if pending.get_request(room.conn):   # a reaction shouldn't park a PC dice-menu roll in v1
            pending.clear(room.conn)
        pending.clear_turn(room.conn)
        n += 1
    return n


def _request_damage_part(room: Room, character: str, ctx: dict, crit: bool, *, parts, idx, rolled) -> None:
    """Park the dice request for damage part `idx` (weapon, then each feature rider)."""
    dreq = combat.pc_damage_request(parts[idx]["notation"], crit)
    pending.set_request(room.conn, {
        "character": character, "die": dreq["die"], "count": dreq["count"], "step": "damage",
        "purpose": f"{parts[idx]['name']} on {ctx['target']}", "ctx": ctx, "crit": crit,
        "parts": parts, "part_index": idx, "rolled": rolled})


def _after_player_action(room: Room, run, outcome: str) -> None:
    """A PC's roll just resolved. First let the DM NARRATE the result — this is what puts the
    outcome into the DM's conversation history, so it never re-asks for a roll it already got.
    Then advance and play the monsters."""
    if not in_combat(room):
        return
    out, alog = run(f"[The dice are resolved: {outcome}. Narrate ONLY this result in the fiction — "
                    f"briefly, vividly. The roll is DONE: do NOT roll, call attack, or ask anyone to "
                    f"roll. Do NOT narrate or resolve ANY monster or other character taking an action — "
                    f"they act next, one at a time, on their own turns. Do NOT announce the next turn, "
                    f"prompt a player, or say whose turn it is. Just describe what just happened, then "
                    f"stop.] {_player_frame(room)}", mechanical=True)
    room.add("dm", out, action_log=alog)
    if pending.get_request(room.conn):     # safety: the narration shouldn't park a new roll
        pending.clear(room.conn)
    pending.clear_turn(room.conn)
    _advance_and_pump(room, run)
    drain_reactions(room, run)


def _advance_and_pump(room: Room, run) -> None:
    """The acting PC's turn is done: mark it, then play the monsters until the next human is up."""
    if not in_combat(room):
        return
    combat.next_turn(room.conn, room.cid)
    _pump_monsters(room, run)


def _pump_monsters(room: Room, run) -> None:
    """Play monster/AI turns one at a time (each its own beat) until a live human player is up.
    Safe to call anytime: no-ops when it's already a human's turn, so it also unsticks combat that
    STARTED on a monster's turn (the monster won initiative) — there's no player action to trigger
    advancement otherwise, which is what wedged the table."""
    if not in_combat(room):
        return
    humans = set(room.claimed) or {p["name"] for p in state.party(room.conn, room.cid)}
    for _ in range(40):
        if not in_combat(room):
            return
        cur = combat.current_turn_name(room.conn, room.cid)
        if cur is None:
            return
        if cur in humans:
            # If it's a downed player's turn, they don't act — they roll a death save (dice menu).
            if combat.is_pc_dying(room.conn, room.cid, cur) and not pending.get_request(room.conn):
                pending.set_request(room.conn, {"character": cur, "die": "d20", "count": 1,
                    "step": "death_save", "purpose": "death saving throw",
                    "ctx": {"kind": "death_save", "character": cur}})
            return                          # a human player's turn (or their death save) — stop

        ally = combat.companion_side(room.conn, room.cid, cur) == "party"
        role = (f"{cur} is a PARTY ALLY — have it act to help the party, attacking the ENEMIES (never a "
                f"player or another ally). " if ally
                else f"{cur} is an enemy. ")
        out, alog = run(f"[It is {cur}'s turn in initiative. {role}Resolve ONLY {cur}'s single action with the "
                        f"tools (its attack rolls automatically), and narrate ONLY what {cur} does — say "
                        f"nothing about any other character acting; they get their own turns next. "
                        f"Then STOP. Do not announce whose turn is next, do not say 'your turn', do not "
                        f"prompt a player.] {_player_frame(room)}", mechanical=True)
        room.add("dm", out, action_log=alog)
        if pending.get_request(room.conn):  # safety: a monster turn shouldn't park a PC roll
            pending.clear(room.conn)
        pending.clear_turn(room.conn)
        drain_reactions(room, run)          # resolve reactions to this monster's action first
        combat.next_turn(room.conn, room.cid)
        print(f"[TURN] {room.id}: played {cur} -> next is {combat.current_turn_name(room.conn, room.cid)}", flush=True)


def advance(room: Room, run) -> str:
    """The ▶ DM button: play out monster/AI turns until the next player's turn. Uses the engine
    pump (deterministic one-at-a-time advancement), not a freeform beat — so it can't desync the
    pointer or accidentally end combat the way the old version did."""
    _pump_monsters(room, run)
    drain_reactions(room, run)
    return ""


# ─────────────────────────────── sync ───────────────────────────────
def state_meta(room: Room, since: int = 0) -> dict:
    """The pure-Python sync fields — no DB access. Safe to read while a DM turn holds the
    lock and is using the shared (single-threaded) SQLite connection on another thread."""
    req = pending.get_request(room.conn)
    return {
        "room_id": room.id,
        "seq": room.seq,
        "entries": [e for e in room.transcript if e["seq"] > since],
        "claimed": sorted(room.claimed),
        "ready": sorted(room.ready),
        "staged": sorted(room.staged),
        "busy": room.busy,
        "pending_roll": ({"character": req["character"], "die": req["die"], "count": req["count"],
                          "purpose": req["purpose"]} if req else None),
        "reactions_queued": len(room.reactions),
        "checkpoints": len(room.checkpoints),
    }


# The DB-derived sync fields (these read the shared connection — caller must hold room.lock).
DB_FIELDS = ("mode", "whose_turn", "active")


def state_since(room: Room, since: int = 0) -> dict:
    """Full sync payload. Reads the shared SQLite connection (mode/whose_turn/active), so the
    caller MUST hold room.lock — two threads touching one sqlite connection at once raises
    InterfaceError. The adapter caches these fields to serve polls that arrive mid-turn."""
    p = state_meta(room, since)
    p["mode"] = "combat" if in_combat(room) else "explore"
    p["whose_turn"] = whose_turn(room)
    p["active"] = _claimed_active(room)
    return p
