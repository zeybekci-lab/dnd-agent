'use client';

import React, { useState } from 'react';

const DICE = [4, 6, 8, 10, 12, 20, 100];

export type PendingRoll = { character: string; die: string; count: number; purpose: string };

// The player physically rolls their own dice. The die rolls on THIS device; the engine does
// the math. Tapping the wrong die rolls it but asks for the right one — it isn't submitted.
export default function DiceMenu({ pending, onSubmit, disabled }: {
    pending: PendingRoll;
    onSubmit: (die: string, values: number[]) => void;
    disabled?: boolean;
}) {
    const [rolling, setRolling] = useState(false);
    const [shown, setShown] = useState<{ die: string; values: number[] } | null>(null);
    const [note, setNote] = useState('');

    const expected = pending.die;                 // e.g. "d20"
    const want = pending.count > 1 ? `${pending.count}× ${expected}` : expected;
    const rollOne = (sides: number) => Math.floor(Math.random() * sides) + 1;

    const tap = (sides: number) => {
        if (rolling || disabled) return;
        const die = `d${sides}`;
        const right = die === expected;
        const values = Array.from({ length: right ? pending.count : 1 }, () => rollOne(sides));
        setNote(''); setRolling(true); setShown({ die, values });
        window.setTimeout(() => {
            setRolling(false);
            if (right) {
                onSubmit(die, values);            // engine resolves it
            } else {
                setNote(`That's a ${die} (${values.join(', ')}). You need a ${expected} — roll again.`);
                setShown(null);
            }
        }, 650);
    };

    const total = shown ? shown.values.reduce((a, b) => a + b, 0) : 0;

    return (
        <div className="rounded-lg border border-purple-700/60 bg-purple-950/20 p-3">
            <div className="mb-2 text-sm text-purple-200">
                🎲 <span className="font-semibold">{pending.character}</span> — roll{' '}
                <span className="font-mono text-purple-100">{want}</span>{' '}
                <span className="text-purple-400">{pending.purpose}</span>
            </div>
            <div className="flex flex-wrap gap-2">
                {DICE.map((s) => {
                    const isExpected = `d${s}` === expected;
                    return (
                        <button key={s} onClick={() => tap(s)} disabled={disabled || rolling}
                            className={`px-3 py-2 rounded-lg font-mono text-sm border transition disabled:opacity-50 ${
                                isExpected
                                    ? 'border-purple-400 bg-purple-700/40 text-purple-100 ring-2 ring-purple-500/60 hover:bg-purple-600/50'
                                    : 'border-gray-700 bg-gray-800 text-gray-400 hover:bg-gray-700'}`}>
                            d{s}
                        </button>
                    );
                })}
            </div>
            {shown && (
                <div className={`mt-3 text-center font-mono ${rolling ? 'animate-pulse text-purple-300' : 'text-purple-100'}`}>
                    <span className="text-2xl font-bold">{shown.values.join('  ')}</span>
                    {shown.values.length > 1 && <span className="text-gray-400 text-sm"> = {total}</span>}
                    <span className="text-gray-500 text-xs"> ({shown.die})</span>
                </div>
            )}
            {note && <div className="mt-2 text-sm text-amber-300">{note}</div>}
        </div>
    );
}
