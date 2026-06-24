import React from 'react';

interface Attack {
    name: string;
    bonus: number;
    damage: string;
}

interface Member {
    name: string;
    player: string;
    is_npc?: boolean;
    current_hp: number | null;
    max_hp: number | null;
    ac: number | null;
    level: number | null;
    attacks: Attack[];
    spell_save_dc: number | null;
    spell_attack: number | null;
    note?: string;
    can_fight?: boolean;
}

const fmt = (n: number) => (n >= 0 ? `+${n}` : `${n}`);

export default function PartyPanel({ party, you }: { party: { members: Member[] } | null; you?: string }) {
    if (!party) {
        return <div className="p-4 rounded bg-gray-900 border border-gray-700 text-gray-400">Loading party…</div>;
    }

    return (
        <div className="p-4 rounded-lg bg-gray-900 border border-gray-700 space-y-3">
            <h3 className="text-xl font-bold text-gray-200 uppercase tracking-widest border-b border-gray-700 pb-2">Party</h3>

            {party.members.map((m, i) => {
                const isNpc = !!m.is_npc;
                const hasHp = m.current_hp != null && m.max_hp != null && m.max_hp > 0;
                const pct = hasHp ? Math.max(0, Math.min(100, (m.current_hp! / m.max_hp!) * 100)) : 0;
                const isYou = !isNpc && (you ? m.name === you : m.player !== 'AI');
                const border = isNpc ? 'border-amber-600/60' : isYou ? 'border-purple-500' : 'border-gray-700';
                return (
                    <div key={i} className={`bg-gray-800/50 p-3 rounded border space-y-2 ${border}`}>
                        <div className="flex justify-between items-center gap-2">
                            <span className={`font-semibold ${isNpc ? 'text-amber-300' : 'text-gray-100'}`}>
                                {m.name}
                                {isYou && <span className="text-xs text-purple-400 ml-1">(you)</span>}
                                {isNpc && <span className="text-[10px] uppercase tracking-wider bg-amber-900/50 text-amber-300 border border-amber-700/50 px-1.5 py-0.5 rounded ml-2">NPC ally</span>}
                            </span>
                            {!isNpc && <span className="text-xs text-gray-500 font-mono whitespace-nowrap">L{m.level} · AC {m.ac}</span>}
                            {isNpc && m.can_fight && <span className="text-[10px] text-amber-500/80 whitespace-nowrap" title="Can fight alongside you">⚔ can fight</span>}
                        </div>

                        {hasHp && (
                            <div>
                                <div className="flex justify-between text-xs text-gray-400 mb-0.5">
                                    <span>HP</span>
                                    <span>{Math.max(0, m.current_hp!)}/{m.max_hp}</span>
                                </div>
                                <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
                                    <div className={`h-full transition-all duration-500 ${isNpc ? 'bg-amber-500' : 'bg-emerald-500'}`} style={{ width: `${pct}%` }} />
                                </div>
                            </div>
                        )}

                        {isNpc && m.note && <div className="text-xs text-amber-200/70 italic leading-snug">{m.note}</div>}

                        {!isNpc && (
                            <div className="text-xs text-gray-400 space-y-0.5">
                                {m.attacks.map((a, j) => (
                                    <div key={j}>
                                        <span className="text-gray-200">{a.name}</span> {fmt(a.bonus)} ({a.damage})
                                    </div>
                                ))}
                                {m.spell_save_dc !== null && (
                                    <div className="text-blue-300">Spells: DC {m.spell_save_dc} / atk {fmt(m.spell_attack ?? 0)}</div>
                                )}
                            </div>
                        )}
                    </div>
                );
            })}
        </div>
    );
}
