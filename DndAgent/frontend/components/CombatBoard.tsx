import React from 'react';

interface Combatant {
    name: string;
    side: 'party' | 'enemy';
    current_hp: number;
    max_hp: number;
    conditions: string[];
    zone: string | null;
    initiative: number;
    is_current: boolean;
    down: boolean;
}

interface Encounter {
    in_combat: boolean;
    round: number;
    zones: string[];
    combatants: Combatant[];
}

export default function CombatBoard({ encounter }: { encounter: Encounter | null }) {
    if (!encounter || !encounter.in_combat) return null;
    const zoned = encounter.zones.length > 1;

    return (
        <div className="p-4 rounded-lg bg-gray-900 border border-red-800/60 space-y-3">
            <div className="flex justify-between items-center border-b border-gray-700 pb-2">
                <h3 className="text-xl font-bold text-red-400 uppercase tracking-widest">Combat</h3>
                <span className="text-sm text-gray-400 font-mono">Round {encounter.round}</span>
            </div>

            {zoned && (
                <div className="text-xs text-gray-500">Zones: {encounter.zones.join('  →  ')}</div>
            )}

            <div className="space-y-2">
                {encounter.combatants.map((c, i) => {
                    const pct = Math.max(0, Math.min(100, (c.current_hp / c.max_hp) * 100));
                    const isParty = c.side === 'party';
                    return (
                        <div
                            key={i}
                            className={`p-2 rounded border ${c.is_current ? 'border-yellow-500 bg-yellow-500/5' : 'border-gray-700 bg-gray-800/50'} ${c.down ? 'opacity-40' : ''}`}
                        >
                            <div className="flex justify-between items-center text-sm mb-1">
                                <span className={`font-semibold ${isParty ? 'text-emerald-300' : 'text-red-300'} ${c.down ? 'line-through' : ''}`}>
                                    {c.is_current && <span className="text-yellow-400">▶ </span>}
                                    {c.name}
                                    {zoned && c.zone && <span className="text-gray-500 font-normal"> @{c.zone}</span>}
                                </span>
                                <span className="font-mono text-gray-400">{Math.max(0, c.current_hp)}/{c.max_hp}</span>
                            </div>
                            <div className="w-full bg-gray-800 rounded-full h-1.5 overflow-hidden">
                                <div
                                    className={`h-full transition-all duration-500 ${isParty ? 'bg-emerald-500' : 'bg-red-600'}`}
                                    style={{ width: `${pct}%` }}
                                />
                            </div>
                            {c.conditions.length > 0 && (
                                <div className="flex flex-wrap gap-1 mt-1">
                                    {c.conditions.map((cond, j) => (
                                        <span
                                            key={j}
                                            className="text-[10px] uppercase tracking-wide bg-purple-900/50 text-purple-300 border border-purple-800/50 px-1.5 py-0.5 rounded"
                                        >
                                            {cond}
                                        </span>
                                    ))}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
