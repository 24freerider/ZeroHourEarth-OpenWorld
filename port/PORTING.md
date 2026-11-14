# Porting the mod to a new Generals Vanguard release

Generals Vanguard (formerly GeneralsX) publishes each release as a tag on `origin`
(github.com/wh1ter0se69/GeneralsVanguard): `v0.9.72`, `v0.9.73`, `v0.9.74`, … The mod is a branch on top of one
release: `openworld` (v0.9.72), `openworld-074` (v0.9.74). To move it to the next release:

```
powershell -ExecutionPolicy Bypass -File tools\port-to-upstream.ps1 -Tag v0.9.75           # check: lists conflicts
powershell -ExecutionPolicy Bypass -File tools\port-to-upstream.ps1 -Tag v0.9.75 -Merge    # branch openworld-075 + merge
```

Then:
1. Resolve the conflicts. The markers are diff3 style: `ours` = the new Vanguard, `base` = the old release,
   `theirs` = the mod. `tools\dev\resolve.py FILE SIDES` resolves the hunks of a file in order
   (`o` ours, `t` theirs, `b` both, `B` both with theirs first, `s` leave the hunk as it is).
2. Build: `powershell -ExecutionPolicy Bypass -File tools\build-engine.ps1 *> logs\build.txt`. Search the log for
   `error`, and for `warning C4244` in the terrain files: float heights narrowed to bytes are a real bug.
3. With the owner's consent, stage and test: `tools\stage-game.ps1 -SkipPatch`, then World Exploration. Check
   terrain, roads, a shift, a zombie run and a nuke.
4. Commit in `engine\` with a message file that says how each conflict was resolved.
5. Package: `tools\build-installer.ps1` → `dist\*.zip`.

## Hot spots (where conflicts came before)

| Area | 0.9.72 → 0.9.74: what happened, and how it was resolved |
|---|---|
| **HeightMapData version 5** (`WorldHeightMap.h/.cpp`, `MapUtil.cpp`) | Vanguard added its own version 5: a height multiplier k (1..8) **before** dataSize, byte samples, and `getHeightScale()` = MAP_HEIGHT_SCALE·k used everywhere. The mod's version 5 is dataSize, then format, scale, offset and u16/f32 samples. `readHeightHeader` tells them apart by the first Int (k ≤ 8 < any dataSize). `readHeightSamples` folds k into the floats, and then `m_heightScaleK = 1` and `m_heightScale = MAP_HEIGHT_SCALE`, so Vanguard's code works unchanged. **If Vanguard adds a version 6, check that the mod's maps (version 5, mod layout) are still read.** |
| **W3DRoadBuffer** | Vanguard rewrote segment storage: a per-map `sizeSegmentArray`, a rebuild when full, per-type `sizeBuffers`, and `addMapObject(road, slot, counts)` with the `m_build` scratch. The mod dropped its own `reserveRoads` / `resizeBuffers` / `finishLoadRoads`. The streamed roads now enter through `addMapObjects` → `addStreamSegments`, and `buildStreamRoads` doubles the array until it is not full. Caps come from `roadVertexCap` / `roadIndexCap`. |
| **CodeGui** | Both sides added `CodeGuiButtonLike` with different parameters: both are kept as overloads. |
| W3DRadar, BaseHeightMap, HeightMap, PolygonTrigger, GameLogic | Small: both sides were kept. |
| The rest (39 files) | Merged automatically. |

## Data that may change with a release

- **Community Patch INI** (`GeneralsXPatch\`): the zombie `map.ini` (`worldserver\zombie\*.ini`) overrides patch
  objects such as `NeutronMissile` `ModuleTag_06` and `SuperweaponNeutronMissile`. If a release renames a module
  tag, the map fails to load. Check with `tools\dev\bigblock.py game\GeneralsXPatch "Object NeutronMissile"`.
- **Main menu .wnd**: the WORLD EXPLORATION button copies the look of an authored button (`MainMenu.cpp`). If that
  button is renamed, the entry disappears.
- **Install folder**: from 0.9.74 a new install goes to `%LOCALAPPDATA%\Programs\GeneralsVanguard`, and an old one
  stays in `...\GeneralsX`. `stage-game.ps1 -Installed <dir>` and the installer handle both.

## Files here

- `patches\*.patch`: the mod as a patch series on v0.9.72 (`git format-patch v0.9.72..openworld`). For reading, or
  for `git am` onto a tree without the history.
- `zhe-openworld-v0.9.72.diff`: the same as one diff.
