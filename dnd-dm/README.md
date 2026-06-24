# dnd-dm — an AI Dungeon Master that runs a prewritten 5e campaign

> The LLM is the improv actor. The database is the canon. The dice engine is the
> referee. Every "AI DM" that feels like it's hallucinating let the actor also be
> the canon and the referee. This one doesn't.

## The problem this solves

| Failure mode | How it's prevented |
|---|---|
| **Forgetting** (campaigns are dozens of hours) | Canon lives in SQLite (`db/schema.sql`), not the context window. The model reads/writes it through tools. Episodic `event_log` + `session_summary` give recall of past beats. |
| **Hallucination** (inventing NPCs, contradicting itself) | Retrieve-before-narrate: relevant canon is injected each turn. The model fills prose *around* facts, it doesn't generate them. |
| **Bad math / cheating** (HP, loot, "I rolled a 25") | All randomness and rules-math run in `engine/dice.py`. The model requests a roll; code decides the outcome. Players can't argue with the DB or the dice. |
| **Referencing past details** | Disciplined *write* at every beat → structured recall by entity/location/tag. |
| **Rules adjudication** | 5e SRD 5.1 (CC-licensed) as a retrieved corpus, not the model's fuzzy memory. |
| **Continuity conflicts** | A validation pass reconciles narration against canon before it commits. |

## The turn loop

```
player input
   │
   ▼
RETRIEVE   current canon (scene, NPCs present, party state) + relevant memories + rules
   │
   ▼
NARRATE    Claude (DM brain) narrates + decides intent → emits tool calls
   │
   ▼
ADJUDICATE tools run authoritatively: dice, combat math, state mutations
   │
   ▼
VALIDATE   reconcile against canon (did it contradict a flag/location?)
   │
   ▼
WRITE      persist new facts + log the beat to episodic memory
```

## Decisions (locked — veto any)

- **Language / store:** Python 3.12, SQLite for canon. No server, runs local.
- **DM brain:** `claude-opus-4-8`, adaptive thinking, effort `high`. **Manual**
  agentic tool-use loop (not the auto tool-runner) so dice and state writes are
  gated and validated before they commit. `claude-haiku-4-5` for cheap
  mechanical sub-tasks (scene-close summarization, memory extraction).
  → Sonnet 4.6 is the cost lever if you want it (~half the price); same code.
- **Prompt caching:** rules SRD + campaign canon + system prompt sit in the
  cached prefix; per-turn state, dice results, and player input go *after* the
  last cache breakpoint. Tool set is frozen for the session (changing it nukes
  the cache). After turn 1 the big static context bills at ~0.1×.
- **Interface:** CLI/TUI first (terminal-native). State is modeled for N PCs;
  default play is 1 human + optional AI-run companions. Discord adapter later.
- **Ruleset:** D&D 5e, SRD 5.1 (CC-BY-4.0) — I ingest it; nothing needed from you.

## Prior art (surveyed 2026-06-16) — what we validated and borrowed

Every serious project converges on our core split (LLM narrates, code adjudicates,
DB holds canon). Concrete refinements adopted from them:

- **Rules as *data*, not just retrieved prose** (from LoreKit). Split the rules
  layer in two: a pure, AI-free **cruncher** (5e stat math, derived stats,
  conditions, action economy as structured formulas the engine *computes*) +
  SRD *prose* via RAG for edge-case rulings/flavor. `engine/dice.py` is the seed
  of the cruncher; it stays dependency-free and unit-tested.
- **Transactional tools, not primitives** (LoreKit). One `resolve_attack` call
  does roll → damage → conditions → state write, instead of three round trips.
  Coarse, atomic, consistent.
- **Stop on tool failure — never smooth over it narratively** (LoreKit). If a
  state mutation is illegal, the DM halts and surfaces it; it does not invent
  prose to cover a failed write. This is the spine of the VALIDATE step.
- **Extraction is the utility model's job** (dnd-llm-game). Haiku turns the DM's
  narration into structured state writes (entities touched, flags changed) — the
  WRITE step — and Opus stays on narration/adjudication.
- **Per-turn loop cap** (~20 iterations, from ai-gamemaster's continuation
  depth) so combat tool-chains can't run away.
- **Theater-of-mind / zone positioning** for v1 combat — no grid; less for the
  model to track.
- **Tool-call log** for deterministic replay/debugging of a session.

Flagged as optional (more model calls / scope — not adopted yet): **NPCs as
isolated sub-agents** (keeps an NPC from leaking what the GM knows) and **vector
episodic memory** (ChromaDB/LanceDB — the common pick; our `event_log.embedding`
column is the hook). Our distinctive bet vs. most of these (which are
improvisational sandboxes): **faithfully running a *prewritten* module** via the
scene graph (`scene.triggers` / `scene.transitions`).

## Layout

```
db/schema.sql                    the canon (world state) + episodic memory tables
campaign/SCHEMA.md               the campaign import format  ← what you hand me
campaign/example_campaign.yaml   a complete original starter adventure
engine/rules.py                  the cruncher — pure 5e math (rules as data + functions)
engine/dice.py                   the referee — dice, checks, attacks
engine/state.py                  the canon layer — YAML→SQLite compiler + reads/writes + recall
engine/combat.py                 the combat layer — initiative, per-instance HP, turns, conditions, zones
engine/srd.py                    rules retrieval — keyword search over the SRD corpus
engine/tools.py                  the 16 transactional tools the model drives the world through
engine/dm.py                     the DM loop — cached prompt, manual tool-use loop, recap
play.py                          interactive CLI (needs a key)
demo_offline.py                  end-to-end exploration proof, no API key
combat_demo.py                   end-to-end combat proof, no API key
rules/srd_5.1.md                 curated 5e rules corpus (consult_rules searches it)
```

## Run it

```bash
# setup (once)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# put your key in .env (gitignored):  ANTHROPIC_API_KEY=sk-ant-...

# offline proofs — no key needed
.venv/bin/python demo_offline.py        # exploration substrate
.venv/bin/python combat_demo.py         # combat layer

# play in the terminal (needs key)
.venv/bin/python play.py

# play in the browser (needs key + Node) — the A.R.C.A.N.A. Next.js UI over our engine:
.venv/bin/uvicorn web.adapter:app --port 8000      # 1) our API adapter (speaks the UI's contract)
#   2) in a clone of github.com/CodingHarpers/DndAgent:
#      cd frontend && npm install && npm run dev    # UI at http://localhost:3000/play
```

The web UI is **adapter-only**: `web/adapter.py` reproduces the four endpoints that
repo's frontend calls (`start_session`, `step`, `stats`, `inventory`) over our
engine — none of its backend (LangGraph/Neo4j) is used. Local use only.

## Status (2026-06-16)

**Live and verified.** The exploration substrate runs end-to-end
(`demo_offline.py`), a real turn has been DMed by Opus 4.8 through `play.py`, and
the **combat layer is built and verified** (`combat_demo.py`): initiative,
per-instance monster HP, PC and statblock-driven monster attacks, crits, downing,
conditions, round advancement, and HP sync back to canon. **16 tools**; the gated
manual tool-use loop with prompt caching is the spine.

Combat is now fully featured: PC attacks **auto-resolve from the sheet** (surfaced
in `get_state`), **conditions auto-apply** advantage/disadvantage and auto-crits,
the DM can **`consult_rules`** against the SRD corpus, and **zone positioning**
gates melee vs. ranged with adjacency-limited movement.

Remaining: a richer TUI, deeper SRD coverage (more chunks / the full document),
spellcasting modeling, and (optional) vector episodic memory.

## What's needed to run it

1. **A campaign** — your own, a file you legally own that I convert, or "generate
   one" (I'll author an original). See `campaign/SCHEMA.md`.
2. **`ANTHROPIC_API_KEY`** in the environment.
