# Campaign format

A campaign is a single YAML file. On load it is **compiled into the canonical
SQLite world-state** (`db/schema.sql`). After that, the database is the source
of truth — the YAML is just the seed.

**This is the one thing I need from you.** Hand me a campaign in this shape (or
a file I convert into it, or say "generate one" and I'll author an original
starter). I will **not** reproduce a commercial module's text from memory — if
you want to run a published adventure, give me the file you own and I'll parse
it.

Every section is optional except `meta`. You can author a 3-room dungeon or a
40-session sandbox with the same schema.

```yaml
meta:
  slug: sunken-bell          # unique id
  title: The Sunken Bell of Mirefen
  ruleset: 5e-srd-5.1
  starting_scene: village-arrival
  level_range: [1, 3]
  tone: "grim folk-horror, dry humor"   # steers the DM's voice
  safety: { lines: ["..."], veils: ["..."] }   # session-zero boundaries

locations:
  - slug: mirefen-village
    name: Mirefen
    region: The Reedlands
    description: "A fog-bound fishing village..."   # DM-facing
    read_aloud: "Peat smoke and the smell of low tide..."  # boxed text

npcs:
  - slug: elsy-marsh
    name: Elsy Marsh
    role: innkeeper
    location: mirefen-village
    disposition: 20
    persona: "Blunt, grieving, speaks in short sentences. Wants her son found."
    knowledge: "Saw lights under the lake three nights ago."
    secrets: "Her son rang the bell on purpose."

monsters:
  - slug: bog-wretch
    name: Bog Wretch
    statblock: { ac: 12, hp: 18, speed: 30, attacks: [{name: claw, bonus: 4, damage: "1d6+2"}], saves: {dex: 1} }

items:
  - slug: tarnished-handbell
    name: Tarnished Handbell
    description: "Rings with no sound a living ear can hear."
    properties: { weight: 1, magical: true }

pcs:                          # pre-gens; or leave empty and create at session start
  - name: Bram
    player: AI                # 'AI' = model-run companion; a name = human
    max_hp: 11
    sheet:                    # field names the cruncher reads
      class: fighter
      level: 1
      abilities: {str: 16, dex: 13, con: 14, int: 8, wis: 12, cha: 10}
      ac: 16
      proficient_skills: [athletics, intimidation]
      proficient_saves: [str, con]
      attacks:                # to-hit bonus + damage are computed from these
        - { name: Longsword, ability: str, die: "1d8", proficient: true }

quests:
  - slug: find-the-boy
    title: Find Elsy's Son
    status: active
    steps:
      - { text: "Learn what happened at the lake", done: false }
      - { text: "Descend to the sunken chapel", done: false }

flags:
  gate_locked: true
  duke_knows: false

factions:
  - { faction: The Reed Cult, standing: -30 }

scenes:
  - slug: village-arrival
    title: Arrival in Mirefen
    location: mirefen-village
    read_aloud: "The road gives way to a causeway of black planks..."
    dm_notes: "Goal: hook the party with Elsy's plea. Don't reveal the cult yet."
    triggers:
      - when: "party asks about the lights"
        then: "Elsy reveals she saw lights; offers 50gp to investigate"
    transitions:
      go_to_lake: lakeshore-dusk
      stay_inn: inn-night
```

## Field notes

- **`read_aloud`** is boxed text the DM reads on first entry. **`dm_notes`** are
  private objectives/secrets — the model uses them to steer but never reads them
  aloud.
- **`triggers`** are conditional beats (`when` → `then`). The model evaluates
  them against player intent; the harness logs the fire.
- **`transitions`** map a player choice to the next scene slug. This is the
  campaign's graph — it's how the agent knows where a prewritten story can go.
- **`persona` / `knowledge` / `secrets`** on an NPC are what make it stay in
  character and not leak what it shouldn't.
- Anything the model invents mid-play (a throwaway NPC, a named tavern) gets
  **written back** to the canon tables, so it persists into later sessions
  instead of drifting.
