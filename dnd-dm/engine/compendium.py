"""The compendium — a global, read-only 5e reference the DM queries for exact
mechanics (spells, monsters, magic items, equipment, class features, conditions).

Sourced from the SRD 5.1 (CC-BY-4.0, © Wizards of the Coast) via the open
5e-bits/5e-database dataset — see compendium/srd/NOTICE.md. This is shared
reference data, NOT per-game canon: it loads once per process and every session
searches the same index, so the model looks rules up instead of recalling them.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections import Counter

DIR = pathlib.Path(__file__).resolve().parent.parent / "compendium" / "srd"
FILES = {
    "spell": "5e-SRD-Spells.json",
    "monster": "5e-SRD-Monsters.json",
    "item": "5e-SRD-Magic-Items.json",
    "equipment": "5e-SRD-Equipment.json",
    "feature": "5e-SRD-Features.json",
    "condition": "5e-SRD-Conditions.json",
}
_WORD = re.compile(r"[a-z0-9]+")
_entries: list[dict] | None = None


def _toks(s: str) -> list[str]:
    return _WORD.findall(s.lower())


def _desc(raw: dict, limit: int = 600) -> str:
    d = raw.get("desc")
    if isinstance(d, list):
        d = " ".join(str(x) for x in d)
    d = (d or "").strip()
    return d[:limit] + "…" if len(d) > limit else d


# ─────────────────────────────── per-kind formatters ───────────────────────────────
def _fmt_spell(r: dict) -> str:
    lvl = "cantrip" if r.get("level") == 0 else f"level {r.get('level')}"
    parts = [f"{r['name']} — {lvl} {r.get('school', {}).get('name', '')}".strip()]
    meta = []
    for key, label in [("casting_time", "cast"), ("range", "range"), ("duration", "duration")]:
        if r.get(key):
            meta.append(f"{label} {r[key]}")
    if r.get("components"):
        meta.append("components " + "/".join(r["components"]))
    if r.get("concentration"):
        meta.append("concentration")
    if meta:
        parts.append("; ".join(meta))
    if r.get("dc"):
        parts.append(f"save: {r['dc'].get('dc_type', {}).get('name', '')}")
    parts.append(_desc(r))
    if r.get("higher_level"):
        parts.append("Higher levels: " + " ".join(r["higher_level"])[:280])
    return "\n".join(p for p in parts if p)


def _fmt_monster(r: dict) -> str:
    ac = r.get("armor_class")
    ac = ac[0].get("value") if isinstance(ac, list) and ac else ac
    speed = ", ".join(f"{k} {v}" for k, v in (r.get("speed") or {}).items())
    abil = tuple(r.get(a, 10) for a in
                 ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"))
    parts = [
        f"{r['name']} — {r.get('size', '')} {r.get('type', '')}, CR {r.get('challenge_rating')} ({r.get('xp')} XP)",
        f"AC {ac}, HP {r.get('hit_points')} ({r.get('hit_dice')}), speed {speed}",
        "STR %s DEX %s CON %s INT %s WIS %s CHA %s" % abil,
    ]
    for a in (r.get("special_abilities") or [])[:3]:
        parts.append(f"• {a.get('name')}: {str(a.get('desc', ''))[:150]}")
    for a in (r.get("actions") or [])[:4]:
        parts.append(f"⚔ {a.get('name')}: {str(a.get('desc', ''))[:150]}")
    return "\n".join(parts)


def _fmt_item(r: dict) -> str:
    head = f"{r['name']} — {r.get('equipment_category', {}).get('name', '')}, {r.get('rarity', {}).get('name', '')}"
    return head + "\n" + _desc(r, 700)


def _fmt_equipment(r: dict) -> str:
    bits = [r["name"], r.get("equipment_category", {}).get("name", "")]
    if r.get("cost"):
        bits.append(f"{r['cost'].get('quantity')} {r['cost'].get('unit')}")
    if (dmg := r.get("damage")):
        bits.append(f"{dmg.get('damage_dice')} {dmg.get('damage_type', {}).get('name', '')}")
    if r.get("armor_class"):
        bits.append(f"AC {r['armor_class'].get('base')}")
    if r.get("weight"):
        bits.append(f"{r['weight']} lb")
    head = " · ".join(str(b) for b in bits if b)
    body = _desc(r)
    return head + ("\n" + body if body else "")


def _fmt_feature(r: dict) -> str:
    sub = (r.get("subclass") or {}).get("name", "")
    lvl = r.get("level")
    head = (f"{r['name']} — {r.get('class', {}).get('name', '')}"
            f"{' (' + sub + ')' if sub else ''}{' L' + str(lvl) if lvl else ''}")
    return head + "\n" + _desc(r)


def _fmt_condition(r: dict) -> str:
    return f"{r['name']}\n{_desc(r, 800)}"


_FMT = {"spell": _fmt_spell, "monster": _fmt_monster, "item": _fmt_item,
        "equipment": _fmt_equipment, "feature": _fmt_feature, "condition": _fmt_condition}


# ─────────────────────────────── load + search ───────────────────────────────
def _load() -> list[dict]:
    global _entries
    if _entries is None:
        _entries = []
        for kind, fn in FILES.items():
            fp = DIR / fn
            if not fp.exists():
                continue
            for r in json.loads(fp.read_text()):
                name = r.get("name", "")
                _entries.append({
                    "kind": kind, "name": name,
                    "summary": _FMT[kind](r),
                    "tokens": Counter(_toks(name + " " + _desc(r, 1000))),
                    "htoks": set(_toks(name)),
                })
    return _entries


def search(query: str, kind: str | None = None, k: int = 4) -> list[dict]:
    """Top-k SRD entries for a query (name matches weighted heavily). Optional kind filter."""
    qt = set(_toks(query))
    if not qt:
        return []
    ql = query.strip().lower()
    scored = []
    for e in _load():
        if kind and e["kind"] != kind:
            continue
        score = sum(e["tokens"][t] for t in qt) + 5 * sum(1 for t in qt if t in e["htoks"])
        if not score:
            continue
        ename = e["name"].lower()
        if ename == ql:               # exact name match wins
            score += 50
        elif ename.startswith(ql):
            score += 12
        if qt <= e["htoks"]:          # every query word appears in the name
            score += 6
        scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:k]]


def stats() -> dict:
    counts: Counter = Counter(e["kind"] for e in _load())
    return dict(counts)


if __name__ == "__main__":
    print("loaded:", stats())
    for q in ["fireball", "potion of healing", "goblin", "rage", "poisoned condition"]:
        top = search(q, k=1)
        print(f"{q!r:28} -> {top[0]['kind'] + ': ' + top[0]['name'] if top else '(none)'}")
