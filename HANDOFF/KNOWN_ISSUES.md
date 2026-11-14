# Known issues, untested work, next steps (as of 14. 11. 2026)

## Untested in the game (built, not yet played)

- **Vanguard 0.9.74 port** (`openworld-074`, merge 26c5ec4). It builds; the game has not been run on it yet. Check:
  - the base maps load and show in the map list (version 5 layout detection);
  - roads appear after shifts and do not vanish in a big town (the new road buffer path; look for `[GXROADS] stream`
    lines in the log);
  - the main menu WORLD EXPLORATION button is there.
- **Crater, three zones** (0067a7f, GEN 23): ash, burnt soil and scorched grass with ragged edges, and scorch patches
  around the rim.
- **Fresh world every run**: the server keeps destroyed ids in memory per OPEN only.
- **Ruins stay within a run** (34a003b); roads and bridges are cleared in the crater; the wheel zooms after
  Ctrl+F9 and back.
- Whether the nuke missile really launches from the Humvee.

## Open bugs

- **The Humvee turret looks frozen while arrow-driving.** The logic aims it (`aiAttackObject` on the nearest enemy,
  `gxWakeUp`), but it does not turn on screen.
  - Diagnostics:
    - `[TURRET]`, logic side, in `driveWithKeys`. Broken: it sits in an every-10-frames block and tests
      `frame % 30`, so it rarely fires. Give it its own counter.
    - `[TURRETDRAW]`, draw side, in `W3DModelDraw.cpp`.
  - Suspect: 47f06c6 draws the camera-locked unit in the normal scene pass instead of `renderSpecificDrawables`.
    In 164bf14 the turret turned, but the Humvee had no shadow.
- The Humvee's shadow: status unclear after 47f06c6.

## Planned: phase 2 (approved by the owner, not started)

1. **Minimap dots** in the player's colour for groups of the player's own units, including units parked outside the
   window (`m_parkedUnits`). Units outside the shown area get a dot clamped to the edge. Hook from GXWorldStream into
   `W3DRadar::draw`.
2. **Traffic**: up to 40 cars on the window's road network (`aiFollowPath` over short pieces) and up to 30
   pedestrians (`MogadishuMale/FemaleCivilian`) on sidewalks. Traffic is never parked: it vanishes outside the window.
3. **A moving train**: kinematic, along `rail`, about 70 km/h. In stream mode `RailroadBehavior` must be bypassed;
   otherwise it snaps to the player's start waypoint.

## Zombie mode, delivery 3 (not started)

Verify the shop; bounty (`Player::setCashBounty` 10 %); HUD polish; a highscore table.

## Ideas parked by the owner

- ESA WorldCover land cover: implemented in `sources.WorldCover`, switched off because it was too slow
  (`Session` → `self.data.landcover`).
- A mega nuke plus a NUKE CONFIG panel: built in 8c0c093, **reverted** (d595c19) at the owner's request. Kept in
  history, if wanted again.
