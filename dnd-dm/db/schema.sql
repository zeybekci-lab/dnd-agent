-- The canon. This SQLite database is the single source of truth for the game
-- world. The LLM never *holds* this state in its context window — it reads and
-- writes it through tools. Every "what is true right now" question is answered
-- from here, which is what kills hallucination and forgetting.
--
-- Two layers live here:
--   1. CANON   — authoritative current state (party, NPCs, locations, flags...)
--   2. EPISODIC — what has happened (event log + session summaries) for recall
--
-- A campaign YAML file is *compiled* into the canon tables on first load.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────── campaign / session ───────────────────────────────
CREATE TABLE IF NOT EXISTS campaign (
    id              INTEGER PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    ruleset         TEXT NOT NULL DEFAULT '5e-srd-5.1',
    starting_scene  TEXT,                 -- scene slug
    overview        TEXT,                 -- DM-only campaign arc/bible (foreshadow, don't reveal)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS game_session (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaign(id),
    number          INTEGER NOT NULL,     -- session 1, 2, 3...
    current_scene   TEXT,                 -- scene slug the party is in
    in_combat       INTEGER NOT NULL DEFAULT 0,
    combat_round    INTEGER NOT NULL DEFAULT 0,
    combat_zones    TEXT,                 -- JSON ordered list of zone names (positioning)
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT
);

-- ─────────────────────────────── world structure ───────────────────────────────
CREATE TABLE IF NOT EXISTS location (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,                      -- DM-facing notes
    read_aloud  TEXT,                      -- boxed text for first entry
    region      TEXT,
    UNIQUE(campaign_id, slug)
);

-- Scenes are the runnable beats of a prewritten campaign: a keyed encounter,
-- room, or set-piece with triggers and exits. This is what lets the agent
-- "run a module" rather than improvise from nothing.
CREATE TABLE IF NOT EXISTS scene (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaign(id),
    slug            TEXT NOT NULL,
    title           TEXT NOT NULL,
    location_slug   TEXT,
    read_aloud      TEXT,                  -- boxed text
    dm_notes        TEXT,                  -- objectives, secrets, pacing notes
    triggers        TEXT,                  -- JSON: [{when, then}] conditional beats
    transitions     TEXT,                  -- JSON: {choice -> next_scene_slug}
    status          TEXT NOT NULL DEFAULT 'unvisited', -- unvisited|active|cleared
    UNIQUE(campaign_id, slug)
);

-- ─────────────────────────────── actors ───────────────────────────────
CREATE TABLE IF NOT EXISTS npc (
    id            INTEGER PRIMARY KEY,
    campaign_id   INTEGER NOT NULL REFERENCES campaign(id),
    slug          TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT,
    location_slug TEXT,
    persona       TEXT,                    -- voice, mannerisms, goals (for the model)
    knowledge     TEXT,                    -- what this NPC knows / will reveal
    secrets       TEXT,                    -- what they hide
    disposition   INTEGER NOT NULL DEFAULT 0,  -- -100 hostile .. +100 devoted
    alive         INTEGER NOT NULL DEFAULT 1,
    state         TEXT,                    -- JSON: arbitrary mutable facts
    UNIQUE(campaign_id, slug)
);

CREATE TABLE IF NOT EXISTS monster (        -- statblocks; instances tracked in combat
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    statblock   TEXT NOT NULL,             -- JSON: ac, hp, speed, attacks, saves...
    UNIQUE(campaign_id, slug)
);

-- Player characters (the party). Designed for N PCs even if you play solo.
CREATE TABLE IF NOT EXISTS pc (
    id            INTEGER PRIMARY KEY,
    campaign_id   INTEGER NOT NULL REFERENCES campaign(id),
    name          TEXT NOT NULL,
    player        TEXT,                    -- human name / 'AI' for companions
    sheet         TEXT NOT NULL,           -- JSON: class, level, abilities, AC, skills
    max_hp        INTEGER NOT NULL,
    current_hp    INTEGER NOT NULL,
    temp_hp       INTEGER NOT NULL DEFAULT 0,
    conditions    TEXT,                    -- JSON list: ['poisoned', ...]
    resources     TEXT,                    -- JSON: current feature uses / spell slots / hit dice
    location_slug TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS item (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    properties  TEXT,                      -- JSON: damage, weight, attunement...
    UNIQUE(campaign_id, slug)
);

CREATE TABLE IF NOT EXISTS inventory (
    id        INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL,              -- 'pc' | 'npc' | 'location'
    owner_id   INTEGER NOT NULL,
    item_slug  TEXT NOT NULL,
    quantity   INTEGER NOT NULL DEFAULT 1,
    equipped   INTEGER NOT NULL DEFAULT 0
);

-- ─────────────────────────────── quest / flags / factions ───────────────────────────────
CREATE TABLE IF NOT EXISTS quest (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT,
    status      TEXT NOT NULL DEFAULT 'inactive', -- inactive|active|complete|failed
    steps       TEXT,                      -- JSON: ordered objectives w/ done flags
    UNIQUE(campaign_id, slug)
);

-- Generic key/value world flags: "gate_locked"=true, "duke_knows"=false, etc.
CREATE TABLE IF NOT EXISTS flag (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,             -- JSON-encoded scalar
    UNIQUE(campaign_id, key)
);

CREATE TABLE IF NOT EXISTS faction_standing (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    faction     TEXT NOT NULL,
    standing     INTEGER NOT NULL DEFAULT 0,  -- -100 .. +100
    UNIQUE(campaign_id, faction)
);

-- ─────────────────────────────── episodic memory ───────────────────────────────
-- Every meaningful beat is logged here the moment it happens. Recall is only as
-- good as capture — this table is what the agent searches to "remember" things
-- from hours ago. Indexed by entity/location/tags for structured retrieval;
-- an optional embedding column supports fuzzy vector recall later.
CREATE TABLE IF NOT EXISTS event_log (
    id            INTEGER PRIMARY KEY,
    campaign_id   INTEGER NOT NULL REFERENCES campaign(id),
    session_id    INTEGER REFERENCES game_session(id),
    turn          INTEGER,
    kind          TEXT NOT NULL,           -- 'dialogue'|'combat'|'decision'|'discovery'|'state_change'
    summary       TEXT NOT NULL,           -- one-line: "Party betrayed Kael in the mines"
    detail        TEXT,
    entities      TEXT,                    -- JSON list of npc/pc/item slugs involved
    location_slug TEXT,
    tags          TEXT,                    -- JSON list for retrieval
    embedding     BLOB,                    -- optional: vector for semantic recall
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_event_session ON event_log(session_id);
CREATE INDEX IF NOT EXISTS idx_event_location ON event_log(location_slug);

CREATE TABLE IF NOT EXISTS session_summary (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    session_id  INTEGER NOT NULL REFERENCES game_session(id),
    summary     TEXT NOT NULL,             -- "Previously on..." recap of the session
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Combat encounter tracker (initiative, instance HP) — populated only during fights.
CREATE TABLE IF NOT EXISTS combatant (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES game_session(id),
    name         TEXT NOT NULL,
    side         TEXT NOT NULL,            -- 'party' | 'enemy'
    ref_type     TEXT,                     -- 'pc' | 'monster'
    ref_id       INTEGER,
    initiative   INTEGER,
    current_hp   INTEGER,
    max_hp       INTEGER,
    conditions   TEXT,                     -- JSON list
    zone         TEXT,                     -- positioning (theater-of-mind zone)
    has_acted    INTEGER NOT NULL DEFAULT 0
);
