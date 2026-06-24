import React, { useEffect, useState } from 'react';
import { apiBase } from '@/lib/api';

const fmt = (n: number) => (n >= 0 ? `+${n}` : `${n}`);
const pips = (r: number, m: number) => (m <= 6 ? '●'.repeat(r) + '○'.repeat(Math.max(0, m - r)) : `${r}/${m}`);

const ABILITY_NAMES: Record<string, string> = {
    str: 'STR', dex: 'DEX', con: 'CON', int: 'INT', wis: 'WIS', cha: 'CHA',
};

// Standard 5e action economy — a quick reference so players know their options.
const ACTIONS = [
    ['Attack', 'Make a weapon or unarmed attack (more if you have Extra Attack).'],
    ['Cast a Spell', 'Cast a spell with a casting time of 1 action.'],
    ['Dash', 'Gain extra movement equal to your speed.'],
    ['Disengage', "Your movement doesn't provoke opportunity attacks."],
    ['Dodge', 'Attackers have disadvantage; you have advantage on Dex saves.'],
    ['Help', 'Give an ally advantage on a check or an attack.'],
    ['Hide', 'Make a Stealth check to become hidden.'],
    ['Ready', 'Prepare an action to trigger on a condition you set.'],
    ['Search', 'Look for something (Perception or Investigation).'],
    ['Use an Object', 'Interact with a second object or feature.'],
];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
    return (
        <div>
            <h3 className="text-xs font-bold text-purple-300 uppercase tracking-widest mb-2 border-b border-gray-700 pb-1">{title}</h3>
            {children}
        </div>
    );
}

export default function CharacterSheet({ roomId, character, onClose }: { roomId: string; character: string; onClose: () => void }) {
    const [s, setS] = useState<any>(null);
    const [err, setErr] = useState('');

    useEffect(() => {
        fetch(`${apiBase()}/api/play/room/${roomId}/sheet/${character}`)
            .then((r) => (r.ok ? r.json() : Promise.reject()))
            .then(setS)
            .catch(() => setErr('Could not load the sheet.'));
    }, [roomId, character]);

    return (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4" onClick={onClose}>
            <div className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-3xl max-h-[90vh] overflow-y-auto p-6"
                onClick={(e) => e.stopPropagation()}>
                {!s ? (
                    <div className="text-gray-400">{err || 'Loading sheet…'}</div>
                ) : (
                    <>
                        <div className="flex justify-between items-start mb-4">
                            <div>
                                <h2 className="text-2xl font-bold text-gray-100">{s.name}
                                    {s.pronouns && <span className="text-sm text-gray-500 font-normal ml-2">({s.pronouns})</span>}</h2>
                                <div className="text-sm text-gray-400 capitalize">Level {s.level} {s.klass}</div>
                            </div>
                            <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl leading-none">×</button>
                        </div>

                        {/* vitals */}
                        <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mb-5">
                            {[['AC', s.ac], ['HP', `${s.hp_current}/${s.hp_max}`], ['Speed', `${s.speed}ft`],
                              ['Init', fmt(s.initiative)], ['Passive', s.passive_perception], ['Prof', fmt(s.proficiency)]].map(([k, v]) => (
                                <div key={k as string} className="bg-gray-800 border border-gray-700 rounded p-2 text-center">
                                    <div className="text-[10px] text-gray-500 uppercase">{k}</div>
                                    <div className="text-lg font-mono text-gray-100">{v}</div>
                                </div>
                            ))}
                        </div>

                        {s.conditions?.length > 0 && (
                            <div className="mb-4 flex flex-wrap gap-1">
                                {s.conditions.map((c: string) => (
                                    <span key={c} className="text-xs uppercase bg-purple-900/50 text-purple-300 border border-purple-800/50 px-2 py-0.5 rounded">{c}</span>
                                ))}
                            </div>
                        )}

                        <div className="grid md:grid-cols-2 gap-6">
                            <div className="space-y-5">
                                <Section title="Abilities">
                                    <div className="grid grid-cols-3 gap-2">
                                        {Object.entries(s.abilities).map(([a, v]: [string, any]) => (
                                            <div key={a} className="bg-gray-800 border border-gray-700 rounded p-2 text-center">
                                                <div className="text-[10px] text-gray-500">{ABILITY_NAMES[a]}</div>
                                                <div className="text-xl font-mono text-gray-100">{fmt(v.mod)}</div>
                                                <div className="text-[10px] text-gray-500">{v.score}</div>
                                            </div>
                                        ))}
                                    </div>
                                </Section>

                                <Section title="Saving Throws">
                                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                                        {Object.entries(s.saves).map(([a, v]: [string, any]) => (
                                            <div key={a} className="flex justify-between">
                                                <span className={v.proficient ? 'text-gray-200' : 'text-gray-500'}>
                                                    {v.proficient ? '● ' : '○ '}{ABILITY_NAMES[a]}
                                                </span>
                                                <span className="font-mono text-gray-300">{fmt(v.mod)}</span>
                                            </div>
                                        ))}
                                    </div>
                                </Section>

                                <Section title="Attacks">
                                    <div className="space-y-1 text-sm">
                                        {s.attacks.map((a: any, i: number) => (
                                            <div key={i} className="flex justify-between">
                                                <span className="text-gray-200">{a.name}</span>
                                                <span className="font-mono text-gray-400">{fmt(a.bonus)} · {a.damage}</span>
                                            </div>
                                        ))}
                                        {s.spell_save_dc != null && (
                                            <div className="flex justify-between text-blue-300 pt-1 border-t border-gray-800 mt-1">
                                                <span>Spells</span><span className="font-mono">save DC {s.spell_save_dc} · atk {fmt(s.spell_attack)}</span>
                                            </div>
                                        )}
                                    </div>
                                </Section>

                                {s.resources && (Object.keys(s.resources.spell_slots).length > 0 ||
                                    Object.keys(s.resources.features).length > 0 || s.resources.hit_dice.max > 0) && (
                                    <Section title="Resources">
                                        <div className="space-y-1 text-sm">
                                            {Object.entries(s.resources.spell_slots).map(([lvl, v]: [string, any]) => (
                                                <div key={lvl} className="flex justify-between">
                                                    <span className="text-gray-300">Level {lvl} slots</span>
                                                    <span className="font-mono text-blue-300">{pips(v.remaining, v.max)}</span>
                                                </div>
                                            ))}
                                            {Object.entries(s.resources.features).map(([fn, v]: [string, any]) => (
                                                <div key={fn} className="flex justify-between">
                                                    <span className="text-gray-300">{fn}</span>
                                                    <span className="font-mono text-emerald-300">{pips(v.remaining, v.max)}</span>
                                                </div>
                                            ))}
                                            {s.resources.hit_dice.max > 0 && (
                                                <div className="flex justify-between">
                                                    <span className="text-gray-300">Hit Dice {s.resources.hit_dice.die}</span>
                                                    <span className="font-mono text-gray-300">{s.resources.hit_dice.remaining}/{s.resources.hit_dice.max}</span>
                                                </div>
                                            )}
                                        </div>
                                    </Section>
                                )}

                                <Section title="Pack">
                                    <div className="text-sm text-gray-300 space-y-0.5">
                                        {s.inventory.length === 0
                                            ? <div className="text-gray-500">Empty.</div>
                                            : s.inventory.map((it: any, i: number) => (
                                                <div key={i}>{it.name}{it.qty > 1 ? ` ×${it.qty}` : ''}{it.equipped ? ' (equipped)' : ''}</div>
                                            ))}
                                        <div className="text-yellow-400 pt-1">{s.gold} gp</div>
                                    </div>
                                </Section>
                            </div>

                            <div className="space-y-5">
                                {s.features?.length > 0 && (
                                    <Section title="Class Features">
                                        <div className="space-y-2 text-sm">
                                            {s.features.map((f: any, i: number) => (
                                                <div key={i}>
                                                    <div className="text-gray-200 font-semibold">{f.name}
                                                        {f.use && <span className="text-xs text-gray-500 font-normal"> · {f.use}</span>}</div>
                                                    {f.text && <div className="text-xs text-gray-400">{f.text}</div>}
                                                </div>
                                            ))}
                                        </div>
                                    </Section>
                                )}
                                <Section title="Skills">
                                    <div className="grid grid-cols-1 gap-y-0.5 text-sm">
                                        {Object.entries(s.skills).map(([sk, v]: [string, any]) => (
                                            <div key={sk} className="flex justify-between">
                                                <span className={v.proficient ? 'text-gray-200' : 'text-gray-500'}>
                                                    {v.expertise ? '◆ ' : v.proficient ? '● ' : '○ '}
                                                    {sk.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())}
                                                    <span className="text-gray-600 text-xs"> ({ABILITY_NAMES[v.ability].toLowerCase()})</span>
                                                    {v.disadvantage && <span className="text-amber-500 text-xs ml-1" title="Disadvantage from heavy armor">⚠ disadv.</span>}
                                                </span>
                                                <span className="font-mono text-gray-300">{fmt(v.mod)}</span>
                                            </div>
                                        ))}
                                    </div>
                                </Section>

                                <Section title="Actions on Your Turn">
                                    <div className="text-xs text-gray-400 space-y-1">
                                        {ACTIONS.map(([name, desc]) => (
                                            <div key={name}><span className="text-gray-200">{name}</span> — {desc}</div>
                                        ))}
                                        <div className="pt-1 text-gray-500">
                                            <span className="text-gray-300">Bonus action:</span> only if a feature or spell grants one (e.g. a rogue's Cunning Action, two-weapon fighting, certain spells).<br />
                                            <span className="text-gray-300">Reaction:</span> one per round — an opportunity attack or a readied trigger.<br />
                                            <span className="text-gray-300">Move:</span> up to your speed ({s.speed} ft), split around your action.
                                        </div>
                                    </div>
                                </Section>
                            </div>
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}
