# Zero Hour Earth – Open World: handoff (start here)

This folder is for whoever continues the mod next — another AI assistant or a person. Read it in this order:

1. **This file**: what the project is, where everything lives, and the rules.
2. [ARCHITECTURE.md](ARCHITECTURE.md): how the game and the world server work together. Protocol, chunk payload, subsystems.
3. [ENGINE_TOUCHPOINTS.md](ENGINE_TOUCHPOINTS.md): every engine file the mod changes, and why.
4. [../port/PORTING.md](../port/PORTING.md): moving the mod onto a new Generals Vanguard release.
5. [KNOWN_ISSUES.md](KNOWN_ISSUES.md): open bugs, untested work and the next planned work.
6. [WORKFLOW.md](WORKFLOW.md): build, stage, test, package and git, with the exact commands.
7. [history/session_dropoff.md](history/session_dropoff.md): the full development log in Slovak. Detailed; look things up there.

[PROMPT.md](PROMPT.md) is a ready-made first message for a new AI session.

## What it is

A mod for **Command & Conquer Generals Zero Hour** running on **Generals Vanguard**, the open-source engine formerly
called GeneralsX (github.com/wh1ter0se69/GeneralsVanguard). In the main menu, **WORLD EXPLORATION** lets the player
pick any real place on Earth. The game then streams that place around the player: real terrain from DEM, plus roads,
bridges, rivers, buildings, trees and props from OpenStreetMap vector tiles. The world scrolls while the player drives
(a "sliding window"), so the map never ends.

Modes:
- **RTS / free camera**: explore with a dozer or other units.
- **Humvee**: drive one vehicle, with arrow-key driving like a car game. A **zombie survival** run can be added, with
  a horde from houses, money per kill, a shop on the Humvee, and a nuke that leaves a real crater.

## Layout (project root `C:\Users\yamak\Desktop\ZeroHourEarth-OpenWorld`)

| Folder | What | In git? |
|---|---|---|
| `engine\` | The Generals Vanguard source with the mod merged in. Current branch **`openworld-074`** (on Vanguard v0.9.74); `openworld` is the same mod on v0.9.72. | **yes** (the only git repo) |
| `worldserver\` | Python world server: downloads DEM and OSM tiles, generates chunks, serves them on `127.0.0.1:47800`. Has a `.venv`. | no |
| `generator\` | The author's older static map generator. The server imports `geo.py`, `gconst.py` and `catalog.py` from it. | no |
| `gamedata\` | The mod's loose game data: `Maps\WorldExploration{5,7,9,11,13,25}` and the zombie variants `...Z`, each with a `map.ini`. Copied over `game\`. | no |
| `game\` | The staged, runnable game (exe + Vanguard's Data and GeneralsXPatch + gamedata). Built by `tools\stage-game.ps1`. | no |
| `tools\` | `build-engine.ps1`, `stage-game.ps1`, `run-test.ps1`, `build-installer.ps1`, `port-to-upstream.ps1`, `installer\`, and `dev\` (helper scripts: INI from .big files, map rebuild, conflict resolver). | no |
| `port\` | `PORTING.md`, the patch series of the mod (`patches\`), and one full diff against v0.9.72. | no |
| `dist\` | The built installer packages (zip). | no |
| `FEATURE\` | Design notes and the working session log (`session_dropoff.md`). | no |
| `logs\`, `cache\` | Build and run logs; the server's disk cache (tiles, chunks). Disposable. | no |

## Rules (from the project owner; keep them)

1. **Communicate in Slovak.**
2. **Start the game only with the owner's consent.** The owner plays in another window, and the test harness clicks
   with the real mouse (`-ClickAt`). Never launch `generalszh.exe` or `run-test.ps1` unasked.
3. **After every change to what the server outputs, bump `GEN_VERSION` in `worldserver\chunkgen.py`.** Otherwise
   chunks cached on disk under the old version are served again.
4. **Commit only `engine\`**, onto the mod branch (now `openworld-074`). Use the git from GitHub Desktop, with the
   message passed as a file: `git commit -q -F <file>`. Stage files by name, not with `git add -A`.
5. **Before writing code, analyse the files involved and propose an exact plan.** Wait for approval when the change
   is not trivial.
6. The owner's e-mail address and other personal data never go to external services.

## Facts that bite

- The game reads the **Community Patch** data (`game\GeneralsXPatch\...\500_900_CommunityPatch_CoreINI.big`), not
  the retail INI. Take weapon and object values from there (`tools\dev\bigblock.py`).
- **map.ini**: module lines (`Draw`, `Body`, `Behavior`) must sit inside `AddModule ... End`, even in a brand-new
  `Object`. Otherwise the map fails to load, and the error message shows only the first line of the block.
  `ReplaceModule` needs a new, unique tag.
- **Ground classes**: `GROUND_CLASSES` in `chunkgen.py` must stay in the order of the base map's texture palette.
  A new class means rebuilding every base map (`tools\dev\rebuildmaps.py`, which also copies them to `game\Maps`).
- **Height maps**: the mod stores terrain heights as floats (`HeightSample`). HeightMapData **version 5 has two
  layouts**: Vanguard's own k multiplier and the mod's float or 16-bit samples, told apart by the first Int. See
  ENGINE_TOUCHPOINTS.md.
- **Scale**: 1 m = 4 world units. One cell = 10 units = 2.5 m. One chunk = 64 cells = 160 m.
- The command bar is filled column by column: odd slots on top, even slots below.
- PowerShell 5.1: no `&&`, no ternary operator. Avoid `2>&1` on native executables.
