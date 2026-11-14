# Engine touchpoints

The mod lives mostly in **new files**. Everything else is a small hook in an existing engine file. Most hooks carry
the comment `GeneralsX @feature ZeroHourEarth` (sometimes `@bugfix ZeroHourEarth`), so this finds them all:

```
git -C engine grep -n "ZeroHourEarth"
git -C engine diff --stat v0.9.74 openworld-074        # every changed file against the Vanguard release
```

World Exploration code is Zero Hour only (`#if RTS_ZEROHOUR` where a file is shared with Generals).

## New files (never conflict on a port)

| File | Role |
|---|---|
| `Core/GameEngineDevice/Source/W3DDevice/GameClient/GXWorldStream.cpp` | The whole client side: window, chunks, shifts, objects, roads, water, trees, bridges, zombies, arrow driving, craters, HUD. |
| `GeneralsMD/Code/GameEngine/Include/GameClient/GXWorldStream.h` | `TheWorldStream`, `GXWorldSettings`: what the menu sets. |
| `Core/GameEngineDevice/.../GXWorldStreamNet.h/.cpp` | Socket, UTF-8, zlib inflate, raw keys (`GXKEY_*`). Kept away from engine headers (winsock order). |
| `GeneralsMD/.../GUICallbacks/Menus/GXWorldExploreMenu.cpp` | The WORLD EXPLORATION menu, built in code with CodeGui: search, sliders, zombie options. |
| `Core/GameEngineDevice/CMakeLists.txt`, `GeneralsMD/Code/GameEngine/CMakeLists.txt` | Lines that add the files above. |

## Hooks in engine files

| File | What the mod adds | Why |
|---|---|---|
| **Height map** | | |
| `WorldHeightMap.h/.cpp` | `HeightSample` = float samples; `readHeightHeader` / `readHeightSamples` (both version 5 layouts, k folded in); `recomputeSampleRange`, `getMin/MaxSample`; the stream API `streamScroll`, `streamWriteHeights`, `streamWriteGround`, `streamBlendGround`, `streamRecomputeCliffs`, `streamFindTextureClass`, `streamTextureClassOfCell` | True-scale terrain (more than 159 units of relief, no 0.625 steps), and writing chunks into the live map. |
| `Core/GameEngine/Source/GameClient/MapUtil.cpp` | The map list reads both version 5 layouts. | 0.9.74's list refused the mod's maps. |
| `BaseHeightMap.h/.cpp`, `HeightMap.cpp`, `W3DTerrainBackground.cpp`, `W3DTerrainVisual.cpp` | Float samples; partial terrain refresh after a stream write; `streamWaterChanged` (soft shore); `streamSetBridges`. | Render the changing terrain. |
| `W3DTerrainLogic.h/.cpp`, `TerrainLogic.h/.cpp` | Logic-side refresh and water hooks. | |
| `W3DShadowMap.cpp`, `W3DVolumetricShadow.cpp` | Float sample bounds. | |
| **Roads, bridges, trees, water** | | |
| `W3DRoadBuffer.h/.cpp` | `loadStreamRoads`, `translateStreamRoads`, `clearStreamRoads`, `buildStreamRoads`, `addStreamSegments`, `roadVertexCap` / `roadIndexCap` | The streamed roads go through Vanguard's own build (`buildRoadSegments`). The draw buffers may reach the 16-bit ceiling while streaming. |
| `W3DBridgeBuffer.h/.cpp` | Bridge width (`addBridge(..., width)`). | Real bridge widths. |
| `W3DTreeBuffer.h/.cpp` | `streamClearTrees` (keeps the types), higher limits. | |
| `Water/W3DWater.cpp`, `PolygonTrigger.h/.cpp` | `g_gxStreamWaterLookup`, `g_gxStreamWaterFirst`: the stream's own water chain. | Objects keep pointers to triggers, so the mod never touches the map's list. |
| **Camera, input, view** | | |
| `View.h`, `W3DView.h/.cpp` | `setStreamLimit` / `applyStreamLimit` (camera brake at an unloaded edge). A camera lock counts as scripted movement only if `!m_isUserControlled \|\| isDoingScriptedCamera()` (the wheel zooms on a locked Humvee). The locked unit is hidden only by a real view filter. | |
| `LookAtXlat.cpp` | With the camera locked, the arrows do not scroll. | They drive. |
| `CommandXlat.cpp` | Order handling for the followed vehicle. | |
| `W3DInGameUI.cpp` | The overlay / HUD draw call. | |
| **Logic** | | |
| `AIPathfind.h/.cpp` | `rebuildAfterWorldShift`, `shiftTerrainCache`, `invalidateTerrainCache`, `releaseBridgeLayer`. | An incremental pathfinder for a moving map. |
| `AIUpdate.h` | `gxWakeUp()`. | A sleeping AI does not turn the turret. |
| `PartitionManager.h/.cpp` | `gxShiftShroud`, `gxReapplyShroud`. | The explored area moves with the world. |
| `Radar.h/.cpp`, `W3DRadar.cpp` | `setStreamView`: the radar scrolls with the camera and has no border. | |
| `GameLogic.cpp` | `TheWorldStream->onGameEnded()` before teardown. | |
| `NeutronMissileSlowDeathUpdate.h/.cpp` | `g_gxNukeDetonated(groundPos, blastRadius)`, at the first damaging blast. | Nuke crater. |
| **UI** | | |
| `CodeGui.h/.cpp` | `CodeGuiButtonLike(tmpl, tmplName, idName, x, y, w, h, label)`: a button that looks exactly like an authored one. Overloads Vanguard's own `CodeGuiButtonLike(tmpl, parent, ...)`. | The WORLD EXPLORATION entry in the stock main menu column, with no .wnd change. |
| `MainMenu.cpp` | Creates that button and opens `GXWorldExploreMenu`. | |
| `W3DModelDraw.cpp` | `[TURRETDRAW]` diagnostic log, once a second. | **Diagnostic only**: may be removed. |

## Hook style, and keeping it

New code goes in the GX files. An engine file gets only a call, a function pointer (like `g_gxNukeDetonated` or
`g_gxStreamWaterLookup`) or a small API with the `stream`/`gx` prefix. This keeps a port to a new Vanguard release
down to a handful of conflicts (11 files of 51 for 0.9.72 → 0.9.74).
