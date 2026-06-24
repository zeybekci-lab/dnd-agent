'use client';

import React, { useEffect, useRef, useState } from 'react';
import CombatBoard from '@/components/CombatBoard';
import PartyPanel from '@/components/PartyPanel';
import CharacterSheet from '@/components/CharacterSheet';
import DiceMenu, { PendingRoll } from '@/components/DiceMenu';
import { apiBase } from '@/lib/api';

const api = (path: string, opts?: RequestInit) =>
    fetch(`${apiBase()}/api/play${path}`, { headers: { 'Content-Type': 'application/json' }, ...opts });

type Entry = { seq: number; kind: string; who: string | null; text: string };

export default function TablePage() {
    const [phase, setPhase] = useState<'lobby' | 'pick' | 'play'>('lobby');
    const [roomId, setRoomId] = useState('');
    const [codeInput, setCodeInput] = useState('');
    const [roster, setRoster] = useState<{ name: string; claimed: boolean }[]>([]);
    const [myChar, setMyChar] = useState('');
    const [savedRooms, setSavedRooms] = useState<{ id: string; title: string; claimed: string[]; turns: number }[]>([]);
    const [characters, setCharacters] = useState<{ name: string; klass: string; level: number }[]>([]);

    const [entries, setEntries] = useState<Entry[]>([]);
    const [mode, setMode] = useState<'explore' | 'combat'>('explore');
    const [whoseTurn, setWhoseTurn] = useState<string | null>(null);
    const [ready, setReady] = useState<string[]>([]);
    const [active, setActive] = useState<string[]>([]);
    const [party, setParty] = useState<any>(null);
    const [encounter, setEncounter] = useState<any>(null);
    const [busy, setBusy] = useState(false);
    const [pendingRoll, setPendingRoll] = useState<PendingRoll | null>(null);
    const [reactionsQueued, setReactionsQueued] = useState(0);
    const [reacting, setReacting] = useState(false);
    const [reactionText, setReactionText] = useState('');
    const [checkpoints, setCheckpoints] = useState(0);
    const [rewindList, setRewindList] = useState<{ i: number; label: string }[] | null>(null);

    const [input, setInput] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [sheetOpen, setSheetOpen] = useState(false);
    const [error, setError] = useState('');
    const since = useRef(0);
    const logRef = useRef<HTMLDivElement>(null);

    // ── lobby actions ──
    const createTable = async () => {
        const r = await api('/room/create', { method: 'POST' }).then((x) => x.json());
        setRoomId(r.room_id);
        setRoster(r.characters);
        setPhase('pick');
    };
    const goToTable = async (code: string) => {
        const res = await api(`/room/${code}/roster`);
        if (!res.ok) { setError('No table with that code.'); return; }
        const d = await res.json();
        setRoomId(code);
        setRoster(d.characters);   // the full playable library + claim status (not the party, which now includes NPC allies)
        setPhase('pick');
    };
    const enterCode = async () => { const c = codeInput.trim().toUpperCase(); if (c) await goToTable(c); };
    const doDelete = async (id: string) => {
        if (!window.confirm(`Delete table ${id}? This permanently erases its save — no undo.`)) return;
        const res = await api(`/room/${id}`, { method: 'DELETE' });
        if (res.ok) setSavedRooms((prev) => prev.filter((r) => r.id !== id));
        else setError('Could not delete that table.');
    };

    // Load saved tables + the solo character options whenever we're in the lobby.
    useEffect(() => {
        if (phase !== 'lobby') return;
        api('/rooms').then((r) => r.json()).then((d) => setSavedRooms(d.rooms || [])).catch(() => {});
        api('/characters').then((r) => r.json()).then((d) => setCharacters(d.characters || [])).catch(() => {});
    }, [phase]);

    const soloStart = async (name: string) => {
        const res = await api('/room/create_solo', { method: 'POST', body: JSON.stringify({ character: name }) });
        if (!res.ok) { setError('Could not start the solo game.'); return; }
        const r = await res.json();
        setRoomId(r.room_id); setMyChar(r.character); setPhase('play');
    };
    const pick = async (name: string) => {
        const res = await api(`/room/${roomId}/join`, { method: 'POST', body: JSON.stringify({ character: name }) });
        if (!res.ok) { setError('Could not claim that character.'); return; }
        setMyChar(name);
        setPhase('play');
    };

    // ── polling ──
    useEffect(() => {
        if (phase !== 'play') return;
        let alive = true;
        const poll = async () => {
            try {
                const st = await api(`/room/${roomId}/state?since=${since.current}`).then((x) => x.json());
                if (!alive) return;
                if (st.entries.length) {
                    since.current = Math.max(since.current, st.seq, ...st.entries.map((e: Entry) => e.seq));
                    // Dedupe by seq — overlapping polls (e.g. React StrictMode's double effects) must
                    // never append the same entry twice, or React hits duplicate keys.
                    setEntries((prev) => {
                        const seen = new Set(prev.map((x) => x.seq));
                        const fresh = st.entries.filter((e: Entry) => !seen.has(e.seq));
                        return fresh.length ? [...prev, ...fresh] : prev;
                    });
                }
                setMode(st.mode); setWhoseTurn(st.whose_turn); setReady(st.ready);
                setActive(st.active); setParty(st.party); setEncounter(st.encounter); setBusy(st.busy);
                setPendingRoll(st.pending_roll ?? null);
                setReactionsQueued(st.reactions_queued ?? 0);
                setCheckpoints(st.checkpoints ?? 0);
            } catch { /* transient */ }
        };
        poll();
        const id = setInterval(poll, 1500);
        return () => { alive = false; clearInterval(id); };
    }, [phase, roomId]);

    useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [entries]);

    // ── play actions ──
    const send = async (path: string, body: any) => {
        setSubmitting(true); setError('');
        try {
            const res = await api(`/room/${roomId}/${path}`, { method: 'POST', body: JSON.stringify(body) });
            if (!res.ok) setError((await res.json()).detail || 'Action failed.');
        } catch { setError('Could not reach the table.'); }
        setSubmitting(false);
    };
    const doReady = async () => { await send('ready', { character: myChar, text: input, ready: true }); setInput(''); };
    const unready = async () => { await send('ready', { character: myChar, text: '', ready: false }); };
    const doAct = async () => { const t = input.trim() || '(I hold — take no action this turn.)'; setInput(''); await send('act', { character: myChar, text: t }); };
    const doAdvance = async () => { await send('advance', {}); };
    const doRoll = (die: string, values: number[]) => { send('roll', { character: myChar, die, values }); };
    const doReact = async () => {
        const t = reactionText.trim(); if (!t) return;
        setReactionText(''); setReacting(false);
        await send('reaction', { character: myChar, text: t });
    };
    const resetLog = () => { since.current = 0; setEntries([]); };   // re-pull the (rolled-back) transcript
    const doRedo = async () => { await send('redo', {}); resetLog(); };
    const openRewind = async () => {
        const d = await api(`/room/${roomId}/checkpoints`).then((r) => r.json());
        setRewindList(d.checkpoints || []);
    };
    const doRewind = async (i: number) => { setRewindList(null); await send('rewind', { to: i }); resetLog(); };

    const iAmReady = ready.includes(myChar);
    const myTurn = whoseTurn === myChar;
    const turnIsPlayer = whoseTurn != null && active.includes(whoseTurn);

    // ── lobby / pick screens ──
    if (phase !== 'play') {
        return (
            <div className="min-h-screen bg-gray-900 text-gray-100 flex flex-col items-center justify-center gap-6 p-6">
                <h1 className="text-3xl font-bold text-purple-400 tracking-wider">A.R.C.A.N.A. — Shared Table</h1>
                {error && <div className="text-red-400 text-sm">{error}</div>}
                {phase === 'lobby' ? (
                    <div className="flex flex-col gap-4 w-80">
                        <button onClick={createTable} className="bg-purple-600 hover:bg-purple-500 rounded-lg py-3 font-semibold">
                            Create a New Table
                        </button>
                        <div className="flex gap-2">
                            <input value={codeInput} onChange={(e) => setCodeInput(e.target.value)} placeholder="Table code"
                                className="flex-grow bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 uppercase" />
                            <button onClick={enterCode} className="bg-gray-700 hover:bg-gray-600 rounded-lg px-4">Join</button>
                        </div>

                        {characters.length > 0 && (
                            <div className="mt-2">
                                <div className="text-xs text-gray-500 uppercase tracking-widest mb-2">Play solo — one character</div>
                                <div className="flex gap-2">
                                    {characters.map((c) => (
                                        <button key={c.name} onClick={() => soloStart(c.name)}
                                            className="flex-grow bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg px-3 py-2 text-left">
                                            <div className="text-sm text-gray-200">{c.name}</div>
                                            <div className="text-xs text-gray-500 capitalize">L{c.level} {c.klass}</div>
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}

                        {savedRooms.length > 0 && (
                            <div className="mt-2">
                                <div className="text-xs text-gray-500 uppercase tracking-widest mb-2">Resume a table</div>
                                <div className="flex flex-col gap-2">
                                    {savedRooms.map((r) => (
                                        <div key={r.id} className="flex items-stretch gap-2">
                                            <button onClick={() => goToTable(r.id)}
                                                className="flex-grow text-left bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-lg px-3 py-2">
                                                <div className="text-sm text-gray-200">{r.title} <span className="font-mono text-purple-300">{r.id}</span></div>
                                                <div className="text-xs text-gray-500">{r.claimed.length ? `playing: ${r.claimed.join(', ')}` : 'no one yet'}</div>
                                            </button>
                                            <button onClick={() => doDelete(r.id)} title="Delete this table"
                                                className="px-3 bg-gray-800 hover:bg-red-900/40 border border-gray-700 hover:border-red-800 rounded-lg text-gray-500 hover:text-red-300">✕</button>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                ) : (
                    <div className="flex flex-col items-center gap-3">
                        <div className="text-gray-400">Table <span className="font-mono text-purple-300 text-lg">{roomId}</span> — pick your character:</div>
                        <div className="flex flex-wrap gap-3 justify-center max-w-lg">
                            {roster.map((c) => (
                                <button key={c.name} onClick={() => pick(c.name)}
                                    className="px-5 py-3 rounded-lg border border-gray-700 bg-gray-800 hover:bg-gray-700">
                                    {c.name}{c.claimed ? <span className="text-xs text-gray-500 ml-1">· in use (take over)</span> : ''}
                                </button>
                            ))}
                        </div>
                        <div className="text-xs text-gray-500">Pick whoever you're playing — "in use" just means someone held that seat before; tap to take it. Share the code <span className="font-mono">{roomId}</span> with other players.</div>
                    </div>
                )}
            </div>
        );
    }

    // ── play screen ──
    return (
        // h-screen (not min-h-screen) locks the height to the viewport, so the story log scrolls on
        // its own while the right rail (Combat + Party/HP) and the input bar stay pinned in view.
        <div className="h-screen bg-gray-900 text-gray-100 flex flex-col font-sans overflow-hidden">
            <header className="p-4 border-b border-gray-700 bg-gray-800 flex justify-between items-center">
                <h1 className="text-xl font-bold tracking-wider text-purple-400">A.R.C.A.N.A.</h1>
                <div className="flex items-center gap-4">
                    <button onClick={() => setSheetOpen(true)}
                        className="text-sm bg-gray-700 hover:bg-gray-600 px-3 py-1.5 rounded">Character Sheet</button>
                    <div className="text-sm text-gray-400">
                        Table <span className="font-mono text-purple-300">{roomId}</span> · you are <span className="text-purple-300 font-semibold">{myChar}</span>
                    </div>
                </div>
            </header>

            <div className="flex flex-1 overflow-hidden">
                <main className="flex-1 flex flex-col border-r border-gray-800">
                    <div ref={logRef} className="flex-1 overflow-y-auto p-6 space-y-3">
                        {entries.map((e) => (
                            <div key={e.seq}>
                                {e.kind === 'dm' ? (
                                    <p className="text-lg leading-relaxed text-cyan-100 whitespace-pre-wrap">{e.text}</p>
                                ) : e.kind === 'player' ? (
                                    <p className="text-sm text-amber-300"><span className="font-semibold">{e.who}:</span> {e.text}</p>
                                ) : (
                                    <p className="text-xs italic text-gray-500">{e.text}</p>
                                )}
                            </div>
                        ))}
                        {busy && <p className="text-sm text-purple-300 flex items-center gap-2">
                            <span className="inline-block w-4 h-4 border-2 border-purple-400 border-t-transparent rounded-full animate-spin" />
                            The Dungeon Master is resolving the turn…</p>}
                    </div>

                    <div className="p-4 border-t border-gray-800">
                        {error && <div className="mb-2 text-sm text-red-300 bg-red-900/30 border border-red-800 rounded px-3 py-2">{error}</div>}

                        {/* Reactions — always available, even mid-beat. Queued and resolved retroactively. */}
                        <div className="mb-2 flex items-center gap-2">
                            {!reacting ? (
                                <button onClick={() => setReacting(true)}
                                    className="text-xs bg-amber-700/30 hover:bg-amber-600/40 border border-amber-700/60 text-amber-200 px-3 py-1 rounded">
                                    ⚡ React
                                </button>
                            ) : (
                                <>
                                    <input value={reactionText} onChange={(e) => setReactionText(e.target.value)} autoFocus
                                        placeholder="React to what just happened — Stone's Endurance, Shield, Counterspell…"
                                        onKeyDown={(e) => { if (e.key === 'Enter') doReact(); if (e.key === 'Escape') { setReacting(false); setReactionText(''); } }}
                                        className="flex-grow bg-gray-800 border border-amber-700/60 rounded px-3 py-1.5 text-sm focus:ring-2 focus:ring-amber-500" />
                                    <button onClick={doReact} className="bg-amber-600 hover:bg-amber-500 text-white px-3 py-1.5 rounded text-sm font-semibold">Send</button>
                                    <button onClick={() => { setReacting(false); setReactionText(''); }} className="text-gray-400 hover:text-white px-2">✕</button>
                                </>
                            )}
                            {reactionsQueued > 0 && <span className="text-xs text-amber-400">⚡ {reactionsQueued} queued — resolving…</span>}

                            {/* Rewind / redo — fix it when the DM fumbles. */}
                            {checkpoints > 0 && !reacting && (
                                <div className="ml-auto flex items-center gap-2">
                                    <button onClick={doRedo} disabled={busy} title="Re-do the last beat (a fresh take on the same moment)"
                                        className="text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 px-3 py-1 rounded disabled:opacity-50">↶ Redo</button>
                                    <button onClick={() => (rewindList ? setRewindList(null) : openRewind())} disabled={busy}
                                        className="text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 px-3 py-1 rounded disabled:opacity-50">⟲ Rewind</button>
                                </div>
                            )}
                        </div>

                        {rewindList && (
                            <div className="mb-2 rounded-lg border border-gray-700 bg-gray-900/80 p-2 max-h-48 overflow-y-auto">
                                <div className="text-xs text-gray-500 mb-1 px-1">Rewind the table to an earlier moment (everyone reverts):</div>
                                {rewindList.length === 0 && <div className="text-xs text-gray-600 px-1">No earlier moments yet.</div>}
                                {[...rewindList].reverse().map((c) => (
                                    <button key={c.i} onClick={() => doRewind(c.i)}
                                        className="block w-full text-left text-xs text-gray-300 hover:bg-gray-800 rounded px-2 py-1 truncate">
                                        ⟲ {c.label}
                                    </button>
                                ))}
                            </div>
                        )}

                        {mode === 'explore' ? (
                            <>
                                <div className="mb-2 text-xs text-gray-500">
                                    Ready: {active.map((c) => `${c}${ready.includes(c) ? ' ✓' : ' …'}`).join('  ·  ') || '—'}
                                    {iAmReady && <span className="text-emerald-400"> — waiting for the party…</span>}
                                </div>
                                <div className="flex gap-2">
                                    <input value={input} onChange={(e) => setInput(e.target.value)} disabled={busy || iAmReady}
                                        placeholder={iAmReady ? 'You are ready — others are deciding…' : 'Describe your action — or tap ✓ with nothing to hold…'}
                                        onKeyDown={(e) => { if (e.key === 'Enter' && !iAmReady) doReady(); }}
                                        className="flex-grow bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 focus:ring-2 focus:ring-purple-500 disabled:opacity-50" />
                                    {iAmReady ? (
                                        <button onClick={unready} disabled={busy} className="bg-gray-700 hover:bg-gray-600 px-5 rounded-lg">Edit</button>
                                    ) : (
                                        <button onClick={doReady} disabled={busy || submitting}
                                            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-6 rounded-lg font-semibold text-xl" title="Ready (tap with an empty box to hold/pass)">✓</button>
                                    )}
                                </div>
                            </>
                        ) : pendingRoll ? (
                            pendingRoll.character === myChar ? (
                                <DiceMenu key={`${pendingRoll.purpose}|${pendingRoll.die}`}
                                    pending={pendingRoll} onSubmit={doRoll} disabled={busy || submitting} />
                            ) : (
                                <div className="text-sm text-gray-400 flex items-center gap-2">
                                    🎲 Waiting for <span className="text-purple-300 font-semibold">{pendingRoll.character}</span> to roll {pendingRoll.die}…
                                </div>
                            )
                        ) : (
                            <>
                                <div className="mb-2 text-xs text-gray-400">
                                    {myTurn ? <span className="text-emerald-400 font-semibold">Your turn.</span>
                                        : <span>Combat — waiting for <span className="text-purple-300">{whoseTurn}</span>…</span>}
                                </div>
                                <div className="flex gap-2">
                                    <input value={input} onChange={(e) => setInput(e.target.value)} disabled={!myTurn || busy}
                                        placeholder={myTurn ? 'What does your character do?' : `It's ${whoseTurn}'s turn`}
                                        onKeyDown={(e) => { if (e.key === 'Enter' && myTurn) doAct(); }}
                                        className="flex-grow bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 focus:ring-2 focus:ring-purple-500 disabled:opacity-50" />
                                    {myTurn ? (
                                        <button onClick={doAct} disabled={busy || submitting}
                                            className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 px-6 rounded-lg font-semibold">Act</button>
                                    ) : !turnIsPlayer && !busy ? (
                                        <button onClick={doAdvance} className="bg-gray-700 hover:bg-gray-600 px-4 rounded-lg text-sm" title="Let the DM play out the monster/NPC turn">▶ DM</button>
                                    ) : null}
                                </div>
                            </>
                        )}
                    </div>
                </main>

                <aside className="w-[380px] flex-shrink-0 bg-gray-950 p-4 overflow-y-auto flex flex-col gap-6 border-l border-gray-800">
                    <CombatBoard encounter={encounter} />
                    <PartyPanel party={party} you={myChar} />
                </aside>
            </div>
            {sheetOpen && <CharacterSheet roomId={roomId} character={myChar} onClose={() => setSheetOpen(false)} />}
        </div>
    );
}
