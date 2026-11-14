# Workflow: exact commands (PowerShell 5.1, from the project root)

## Prerequisites (developer machine)
- Visual Studio 2022 with C++, CMake and Ninja (`tools\build-engine.ps1` sets up the `win64` preset and vcpkg
  under `tools\vcpkg`).
- Generals Vanguard installed (DXVK dlls, `Data\` and `GeneralsXPatch\` come from it), and Zero Hour (Steam).
- Python 3.10+:
  `py -3 -m venv worldserver\.venv`, then `worldserver\.venv\Scripts\python.exe -m pip install -r worldserver\requirements.txt`.
- git: the one from GitHub Desktop works:
  `$git = (Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop\app-*\resources\app\git\cmd\git.exe" | Sort-Object FullName -Desc | Select -First 1).FullName`

## Build → stage → (with consent) run
```
powershell -ExecutionPolicy Bypass -File tools\build-engine.ps1 *> logs\build.txt
powershell -ExecutionPolicy Bypass -File tools\stage-game.ps1 -SkipPatch       # fails while generalszh.exe runs
"Play OpenWorld.cmd"                                                          # the owner plays; do not start it unasked
```
- `-SkipPatch` skips copying the 1.2 GB `GeneralsXPatch`. Leave it out after a Vanguard update.
- **Automated test**, **only with the owner's consent** (it moves the real mouse):
  `tools\run-test.ps1 -Server -Place "Poltar" [-Window 9] [-FreeCam] [-Seconds N] [-Shot logs\x.png -ShotAt "a b"]`
- Logs:
  - `game\userdata\generals-stderr.log` (lines `[WORLD]`, `[GXROADS]`, `nuke crater at …`);
  - `logs\run-err.txt` and `logs\server-out.txt` (from run-test);
  - crashes in `game\userdata\CrashLogs` and `ReleaseCrashInfo.txt` (the pdb is in `game\`).

## Server-side changes
- Edit `worldserver\*.py`, then **bump `GEN_VERSION`** in `chunkgen.py`.
- A new ground class or a changed base map:
  `worldserver\.venv\Scripts\python.exe tools\dev\rebuildmaps.py .` (rebuilds `gamedata\Maps\WorldExploration*`
  and copies them to `game\Maps`).
- The server starts with `Play OpenWorld.cmd`, or by hand: `worldserver\.venv\Scripts\python.exe worldserver\server.py`.
  Restart it after a change.

## Reading game data
- `worldserver\.venv\Scripts\python.exe tools\dev\bigblock.py game\GeneralsXPatch "Object NeutronMissile"`
  prints INI blocks from .big archives.
- `tools\dev\bigobjs.py <dir> <regex>`: object names. `tools\dev\wndbuttons.py <dir>`: ControlBar slot positions.
  `tools\dev\terrainlum.py`: terrain textures by brightness.

## Commit (engine only)
```
& $git -C engine add -- <files by name>
& $git -C engine commit -q -F <message file>
```
Branch: `openworld-074`. Do not use `git add -A`, and do not push unless the owner asks.

## Package for players
```
powershell -ExecutionPolicy Bypass -File tools\build-installer.ps1          # → dist\ZeroHourEarth-OpenWorld-<ver>.zip
```
The player unpacks the zip and runs `Install.cmd`. The installer:
- finds Vanguard and Zero Hour;
- copies Vanguard's runtime and data, the mod's exe and maps, the world server and the generator;
- sets up the Python venv;
- creates shortcuts.

Running `Install.cmd` again repairs or updates the install. `Uninstall.ps1` removes it.

## Port to a new Vanguard release
See [../port/PORTING.md](../port/PORTING.md): `tools\port-to-upstream.ps1 -Tag v0.9.xx [-Merge]`.
