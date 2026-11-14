# Zero Hour Earth – Open World

An open world for **Command & Conquer Generals Zero Hour**, running on the
[Generals Vanguard](https://github.com/wh1ter0se69/GeneralsVanguard) engine (formerly GeneralsX).

Pick any place on Earth in **WORLD EXPLORATION** in the main menu. The game streams it around you while you drive:
real terrain, roads, bridges, rivers, buildings, trees and town props, from elevation data and OpenStreetMap. The
world scrolls, so the map never ends. There is also a **zombie survival** mode: a Humvee with a shop, a horde
coming out of the houses, and a nuke that leaves a real crater.

## Install (players)

You need:
- Zero Hour (Steam, EA app or CD);
- **Generals Vanguard**, installed with its own installer;
- **Python 3.10+**;
- about 2.5 GB of free space.

1. Download `ZeroHourEarth-OpenWorld-….zip` from [Releases](../../releases) and unpack it.
2. Double-click **`Install.cmd`**. It finds Vanguard and Zero Hour, then creates the folder
   **`Zero Hour Earth - Open World`** on your desktop: a full copy of Vanguard with the mod on top. Your Vanguard,
   the game and your profile are not changed, and a Vanguard update cannot break the mod.
3. Start **`Play OpenWorld.cmd`** in that folder, or the desktop shortcut. Then choose WORLD EXPLORATION.

Uninstall: run `Uninstall.ps1` in the folder, or delete the folder.

## Develop

The engine is a fork of Generals Vanguard, linked here as the `engine` submodule; the mod branch is
`openworld-074` (Vanguard v0.9.74).

```
git clone --recurse-submodules <this repository>
```

Start with **[HANDOFF/README.md](HANDOFF/README.md)**. It covers the architecture, every engine touchpoint, the
build and test workflow, known issues, and how to port the mod to the next Vanguard release
([port/PORTING.md](port/PORTING.md), `tools\port-to-upstream.ps1`).

The base maps (`gamedata/Maps/WorldExploration*`) are generated and not in git. Build them with
`worldserver\.venv\Scripts\python.exe worldserver\worldmap.py`, or let the world server write them on first use.

## Data and credits

- Terrain and map data: OpenStreetMap contributors (ODbL), VersaTiles, public DEM sources and GHS-POP; all are
  downloaded at play time.
- No game files are included. Zero Hour is EA's; the engine is Generals Vanguard and its contributors (GPL-3.0).
