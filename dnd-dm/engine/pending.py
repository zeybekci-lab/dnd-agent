"""Pending player rolls — the bridge between a tool that needs a human's dice and the
web flow that collects them.

A tool (e.g. attack) runs deep inside the DM loop with only `conn`/`cid` in hand; it has
no reference to the Room or session. When a *player-controlled* PC must roll, the tool
parks a roll REQUEST here, keyed by the connection object (unique per in-memory game), and
returns a "stop and let them roll" message. The web layer reads the request, shows the dice
menu, and on submission resolves it deterministically in engine.combat.

Shape of a request dict:
    {"character", "die": "d20", "count": 1, "purpose": str, "step": "tohit"|"damage",
     "ctx": {...combat resolution context...}, "crit": bool}
"""

from __future__ import annotations

_PENDING: dict[int, dict] = {}   # id(conn) -> request, while a roll is awaited
_ENABLED: set[int] = set()       # games where PCs roll their own dice (multiplayer tables)


def enable(conn) -> None:
    """Turn on player-rolled dice for this game (the multiplayer flow that has a dice menu)."""
    _ENABLED.add(id(conn))


def is_enabled(conn) -> bool:
    return id(conn) in _ENABLED


_TURN_DONE: set[int] = set()     # games where the DM has signalled the acting PC's turn is used up


def consume_turn(conn) -> None:
    """The DM signals (via the end_turn tool) that the acting PC has spent their action."""
    _TURN_DONE.add(id(conn))


def pop_turn_consumed(conn) -> bool:
    """Read-and-clear: did the DM end the turn during the beat just run?"""
    if id(conn) in _TURN_DONE:
        _TURN_DONE.discard(id(conn))
        return True
    return False


def clear_turn(conn) -> None:
    _TURN_DONE.discard(id(conn))


_HELD: set[int] = set()          # the DM is awaiting the current player's choice before the turn ends


def hold(conn) -> None:
    """The DM signals (via hold_turn) that it asked the acting player a question — keep their turn."""
    _HELD.add(id(conn))


def pop_held(conn) -> bool:
    if id(conn) in _HELD:
        _HELD.discard(id(conn))
        return True
    return False


def clear_hold(conn) -> None:
    _HELD.discard(id(conn))


def set_request(conn, request: dict) -> None:
    _PENDING[id(conn)] = request


def get_request(conn) -> dict | None:
    return _PENDING.get(id(conn))


def clear(conn) -> None:
    _PENDING.pop(id(conn), None)
