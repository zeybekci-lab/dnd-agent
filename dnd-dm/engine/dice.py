"""The referee. All randomness and rules-math lives here — never in the LLM.

The DM model decides *what* roll to make and *why*; this module decides the
*outcome*. That separation is what stops a player from talking the model into a
natural 20, and stops the model from hallucinating damage numbers.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

# Standard dice notation: 2d6+3, 1d20, d8, 4d6-1, 3d8+2d4+1
_TOKEN = re.compile(r"([+-]?)(\d*)d(\d+)|([+-]?\d+)", re.IGNORECASE)


@dataclass
class Roll:
    """The full audit trail of a single roll — show this to the player verbatim."""

    notation: str
    dice: list[int] = field(default_factory=list)  # individual die faces, in order
    modifier: int = 0
    total: int = 0
    detail: str = ""  # human-readable breakdown, e.g. "2d6 (4, 5) + 3 = 12"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.detail


class Dice:
    """Seedable RNG so sessions are reproducible / testable.

    Pass a fixed seed in tests; in play, leave it None for OS entropy.
    """

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def _roll_die(self, sides: int) -> int:
        return self._rng.randint(1, sides)

    def roll(self, notation: str) -> Roll:
        """Roll arbitrary dice notation: '2d6+3', '1d20', 'd8-1', '3d8+2d4+1'."""
        notation = notation.replace(" ", "")
        faces: list[int] = []
        modifier = 0
        parts: list[str] = []

        for m in _TOKEN.finditer(notation):
            sign, count, sides, flat = m.group(1), m.group(2), m.group(3), m.group(4)
            if flat is not None:
                modifier += int(flat)
                parts.append(f"{'+' if int(flat) >= 0 else '-'} {abs(int(flat))}")
                continue
            n = int(count) if count else 1
            s = int(sides)
            rolled = [self._roll_die(s) for _ in range(n)]
            if sign == "-":
                faces.extend(-r for r in rolled)
                parts.append(f"- {n}d{s} ({', '.join(map(str, rolled))})")
            else:
                faces.extend(rolled)
                lead = "" if not parts else "+ "
                parts.append(f"{lead}{n}d{s} ({', '.join(map(str, rolled))})")

        total = sum(faces) + modifier
        detail = " ".join(parts).lstrip("+ ").strip() + f" = {total}"
        return Roll(notation=notation, dice=faces, modifier=modifier, total=total, detail=detail)

    def d20(self, modifier: int = 0, *, advantage: bool = False, disadvantage: bool = False) -> Roll:
        """A d20 check with optional (dis)advantage. Adv and disadv cancel out."""
        if advantage and disadvantage:
            advantage = disadvantage = False
        a = self._roll_die(20)
        if advantage or disadvantage:
            b = self._roll_die(20)
            chosen = max(a, b) if advantage else min(a, b)
            kind = "adv" if advantage else "dis"
            faces = [chosen]
            base = f"d20 {kind} ({a}, {b}) -> {chosen}"
        else:
            chosen = a
            faces = [a]
            base = f"d20 ({a})"
        total = chosen + modifier
        mod_str = f" {'+' if modifier >= 0 else '-'} {abs(modifier)}" if modifier else ""
        return Roll("1d20", dice=faces, modifier=modifier, total=total,
                    detail=f"{base}{mod_str} = {total}")


@dataclass
class CheckResult:
    roll: Roll
    dc: int
    success: bool
    margin: int          # total - dc; negative = how badly it failed
    natural: int         # the raw d20 face (before modifiers)
    critical: bool       # nat 20
    fumble: bool         # nat 1


def ability_check(dice: Dice, modifier: int, dc: int, *,
                  advantage: bool = False, disadvantage: bool = False) -> CheckResult:
    """A check or saving throw against a DC. Nat 1/20 are flagged but, per 5e RAW,
    only auto-succeed/fail on attack rolls and death saves — the DM model decides
    whether to honor them narratively for skill checks."""
    r = dice.d20(modifier, advantage=advantage, disadvantage=disadvantage)
    return CheckResult(
        roll=r, dc=dc, success=r.total >= dc, margin=r.total - dc,
        natural=abs(r.dice[0]), critical=abs(r.dice[0]) == 20, fumble=abs(r.dice[0]) == 1,
    )


@dataclass
class AttackResult:
    attack_roll: Roll
    ac: int
    hit: bool
    critical: bool       # nat 20 -> auto-hit, double damage dice
    fumble: bool         # nat 1 -> auto-miss
    natural: int


def attack_roll(dice: Dice, attack_bonus: int, target_ac: int, *,
                advantage: bool = False, disadvantage: bool = False) -> AttackResult:
    """5e attack: nat 20 always hits (and crits), nat 1 always misses."""
    r = dice.d20(attack_bonus, advantage=advantage, disadvantage=disadvantage)
    nat = abs(r.dice[0])
    crit, fumble = nat == 20, nat == 1
    hit = crit or (not fumble and r.total >= target_ac)
    return AttackResult(attack_roll=r, ac=target_ac, hit=hit, critical=crit,
                        fumble=fumble, natural=nat)


if __name__ == "__main__":
    d = Dice(seed=42)
    print(d.roll("2d6+3"))
    print(d.d20(5, advantage=True))
    print(ability_check(d, modifier=3, dc=15))
    print(attack_roll(d, attack_bonus=6, target_ac=14))
