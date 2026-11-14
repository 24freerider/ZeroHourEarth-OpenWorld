# Architecture

```
 ┌───────────────────────── generalszh.exe (engine\, C++) ─────────────────────────┐
 │ GXWorldExploreMenu.cpp   main menu → place search, window size, modes, zombie    │
 │        │                                                                         │
 │ GXWorldStream.cpp  (TheWorldStream; ~3800 lines, the heart of the mod)           │
 │   sliding window of N×N chunks over the planet; network thread; applies chunks   │
 │   into the height map, textures, roads, bridges, water, trees, objects; shifts   │
 │   the world; parks units outside; zombies; arrow driving; nuke craters           │
 │        │  GXWorldStreamNet.cpp (winsock, zlib, raw keys)                         │
 └────────┼─────────────────────────────────────────────────────────────────────────┘
          │ TCP 127.0.0.1:47800, protocol 3
 ┌────────┴──────────── worldserver\server.py (Python 3.10+) ───────────────────────┐
 │ world.py (session, caches) ─ sources.py (DEM, vector tiles, Overpass, GHS-POP)   │
 │ worlddata.py (decode tiles) ─ chunkgen.py (heights, ground, roads, water,         │
 │ bridges, objects, trees → GXCK payload) ─ worldmap.py (writes the base maps)     │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

## The sliding window (client)

- **Base map**: `Maps\WorldExploration<N>[Z]\` is a flat N×N-chunk map that the server writes once
  (`worldmap.py`, palette = `GROUND_CLASSES`). The game loads it as a normal skirmish map, and then
  `GXWorldStream::onMapLoaded` takes over.
- **Chunk** = 64×64 cells. The window is N×N chunks, N odd from 3 to 25; the default is 25 (4 km).
  - From `kBigWindow` (17) up, the camera moves freely inside the window. The world shifts only when the camera
    comes within `kEdgeMargin` chunks of the edge, and then by enough chunks to centre it again.
  - The incoming strip is preloaded first. If it is not ready, the camera brakes (`View::setStreamLimit`).
- **`shiftWorld`** moves everything by whole chunks in one frame:
  - height map content (`WorldHeightMap::streamScroll`), every object, camera, water polygons, trees, bridges,
    roads (`translateStreamRoads`), radar, shroud (`gxShiftShroud`), pathfinder (`rebuildAfterWorldShift`),
    move goals and crater scorch marks.
- **Applying a chunk** (`applyTerrain`):
  - heights → `streamWriteHeights`; ground classes → `streamWriteGround` + `streamBlendGround` (auto-blend);
    cliffs; water; craters first (`craterChunk`).
  - Roads, bridges, trees and objects are rebuilt for the window: `rebuildRoads`, `rebuildBridges`, `rebuildTrees`,
    `populate` / `createQueued`.
- **Memory**:
  - `m_chunks`: in the window and around it.
  - `m_parked`: an LRU of 600 chunks, for the way back.
  - Units outside the window are parked (type, owner, health, veterancy, world position) and restored
    (`parkUnitsOutside` / `restoreParkedUnits`).
  - Buildings exist only within `kObjectRing` chunks of the camera.
- **World coordinates**: the client keeps an absolute origin, so `m_craters`, parked units and destroyed ids
  survive shifts.

## Protocol 3 (see the docstring at the top of `server.py`)

Every message: `u32 length, u16 type, u32 request id, payload`, little endian. A string is a u16 length + UTF-8.

| game → server | server → game |
|---|---|
| 1 HELLO u32 protocol | 101 HELLO_OK |
| 2 SEARCH string | 102 PLACES |
| 3 OPEN lat, lon, name, u16 window, u8 layers (1 buildings, 2 roads, 4 trees, 8 zombie run) | 103 OPENED |
| 4 CHUNKS cx0, cy0, nx, ny (≤ 400 per request) | 104 CHUNK cx, cy, raw size, zlib(GXCK) … 105 DONE |
| 5 POSITION x, y, heading (the server prefetches ahead) | – |
| 6 DESTROYED u64 object id (remembered only for this OPEN: every run starts with a whole world) | – |
| 7 CLOSE | – |
| 8 POPULATION points | 106 POPULATION (GHS-POP 1 km cells; not used by the zombies any more) |
| | 199 ERROR string |

**GXCK chunk payload v1** (one per chunk):
- strings; heights f32 (world z); ground u8 (an index into `GROUND_CLASSES`);
- roads: `u16 type, u8 rank, u8 flags (1 = bridge), f32 width, points × (lx ly lz rx ry rz s)`;
- objects; water: `f32 z, u8 flags (2 = ford), 3 pad, u32 n, n × (x y)`.

A header flag 1 marks an *incomplete* chunk (an Overpass query still pending). The client asks for it again every
20 s and replaces it.

**Cache rule**: chunks are cached on disk under `cache\chunks\<grid>\v<GEN_VERSION>_…`. Bump `GEN_VERSION` whenever
the output for the same input changes.

## Server pipeline (`chunkgen.py`, `MARGIN = 72` cells of context)

1. `_pads`
2. `_roads` (road beds)
3. `_water` (level per chunk from the DEM)
4. `_streams` (quads along the line)
5. `_bridges`: deck profile by what the bridge crosses, embankments, culverts
6. `_ground`: classes; edges moved by value noise
7. trees `_chunk_trees` / `_street_trees`; props `_chunk_props`: parking rows, pitches, rail crossings, lamps, fences,
   flats

Data sources (`sources.py`), all cached under `cache\`:
- DEM tiles; VersaTiles / Shortbread vector tiles; Overpass (pitches, squares);
- GHS-POP; ESA WorldCover (implemented, **switched off** because it was too slow).

## Subsystems in GXWorldStream.cpp (search for these names)

| Subsystem | Entry points | Notes |
|---|---|---|
| Water | `rebuildWater`, `g_gxStreamWaterLookup` | Its own `PolygonTrigger` chain (`g_gxStreamWaterFirst`). A ford is drawn but ignored by the logic. |
| Roads | `rebuildRoads` → `W3DRoadBuffer::loadStreamRoads` | Points prepared on the network thread (welder, RDP). Clipped at craters. |
| Bridges | `rebuildBridges` | Splits into straight spans and builds the 12 nearest (pathfinder layer limit). Width via `W3DBridge::setWidth`. |
| Trees | `rebuildTrees` | `W3DTreeBuffer` only, no game objects. OFF / OPTIMIZED / UNLIMITED (32k). |
| Zombies | `zombieStart`, `zombieUpdate`, `zombieSpawn`, `drawZombieHud` | 3–8 per house (hash of the house id), never refilled. 500 start money, 2 per kill. Only in `…Z` maps. |
| Arrow driving | `driveWithKeys` | Direct car control from the Humvee's locomotor. The turret attacks the nearest enemy (`aiAttackObject`). **The turret still looks frozen** (KNOWN_ISSUES). |
| Darkness / fog | `drawOverlay` veil; `revealMapForPlayer`; `gxShiftShroud` / `gxReapplyShroud` | |
| Nuke crater | `nukeDetonated` → `addCrater` → `craterChunk`, `addCraterScorches`, `redrawCraterScorches`, `insideCrater` | Hook `g_gxNukeDetonated` in `NeutronMissileSlowDeathUpdate.cpp`. Radius 0.48 × blast, depth 0.25 R, cos² bowl, rim to 1.3 R. Three ragged ground zones: ash, burnt soil, scorched grass. Roads, bridges, props and trees are cleared to 1.15 R. |
| Ruins | `makeRuin` (from `createQueued`) | A destroyed building comes back as a ruin (POST_COLLAPSE or RUBBLE, effectively dead). |

## Zombie data (`worldserver\zombie\`)

`humvee_shop.ini` and `zombie_tiers.ini` are merged into the `map.ini` of every `…Z` map (`worldmap.py --zombie`,
`ensure()` rebuilds a map when they are newer).
- The Humvee gets `OCLSpecialPower ModuleTag_EARTH_Nuke` (`SuperweaponNeutronMissile`, `StartsReady = Yes`), with the
  button `EARTH_Command_Nuke` in slot 10.
- The nuke is 3× the patch values: reach 630, Blast6 10500/900, scorch 960, `ReloadTime = 0`, no public timer.
