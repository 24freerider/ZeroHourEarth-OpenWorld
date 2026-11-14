# Zero Hour Earth – Open World: Session Dropoff (13. 11. 2026)

Komunikácia: **po slovensky**. Pracovný priečinok: `C:\Users\yamak\Desktop\ZeroHourEarth-OpenWorld`.

## 0. Vanguard 0.9.74, inštalátor, podklady (14. 11. 2026)
- **Port:** vetva `openworld-074` = `v0.9.74` + merge `openworld` (commit `26c5ec4`). Build prešiel, v hre zatiaľ netestované. Vetva `openworld` ostáva na 0.9.72 (z nej je `game\`, kým sa nespustí stage).
  - Konflikt formátu výšok: 0.9.74 má vlastnú HeightMapData v5 (násobiteľ k). `readHeightHeader` rozlíši obe rozloženia podľa prvého Int, k sa zloží do float vzoriek, potom k = 1. Mapy netreba prestavať. `MapUtil.cpp` číta obe.
  - Cesty: používa sa mechanizmus 0.9.74 (`sizeSegmentArray`, `sizeBuffers`). Streamované cesty idú cez `addMapObjects` → `addStreamSegments`, limit 16 bit cez `roadVertexCap`. `reserveRoads`/`resizeBuffers` sú preč.
- **Ďalšie verzie:** `tools\port-to-upstream.ps1 -Tag v0.9.xx [-Merge]`, návod `port\PORTING.md`.
- **Inštalátor:** `tools\build-installer.ps1` → `dist\ZeroHourEarth-OpenWorld-<ver>.zip` (Install.cmd / Install.ps1 / Uninstall.ps1 v `tools\installer\`).
- **Podklady pre inú AI:** `HANDOFF\` (README, ARCHITECTURE, ENGINE_TOUCHPOINTS, KNOWN_ISSUES, WORKFLOW, PROMPT), `CLAUDE.md` a `AGENTS.md` v koreni, pomocné skripty v `tools\dev\`.

## 1. Architektúra

| Časť | Kde | Čo robí |
|---|---|---|
| Engine | `engine\` (GeneralsX, klon GeneralsVanguard v0.9.72, vetva **`openworld`**, git) | Hra C&C Generals Zero Hour. Kód World Exploration je pod `#if RTS_ZEROHOUR`. MSVC 2022, preset `win64`, cieľ len ZH. |
| World server | `worldserver\` (Python, venv `worldserver\.venv`) | Generuje chunky z DEM a OSM vektorových dlaždíc (VersaTiles / Shortbread), TCP `127.0.0.1:47800`, protokol 2. **Nie je v gite.** |
| Generátor máp | `generator\` | Pôvodný generátor statických máp používateľa. Server z neho berie `geo.py` a `gconst.py`. Má aj logiku nadjazdov (`g_roads.py build_overpasses`). |
| Nástroje | `tools\` | `build-engine.ps1`, `stage-game.ps1`, `run-test.ps1` |
| Herné dáta | `gamedata\Maps\WorldExploration{5,7,9,11,13}` | Základné mapy pre veľkosti okna. Stage ich kopíruje do `game\`. |
| Dokumenty | `FEATURE\` | `world_zoom_design.md` (návrh zoomu L0–L5), tento dropoff |
| Cache | `cache\chunks\<grid>\v<GEN_VERSION>_...`, `cache\vec`, `cache\dem` | Diskové cache servera |

### Streamovanie (klient)
- **`engine\Core\GameEngineDevice\Source\W3DDevice\GameClient\GXWorldStream.cpp`** je hlavný súbor (~2600 riadkov).
  - Okno N×N chunkov (chunk = 64 buniek, bunka = 10 jednotiek = 2,5 m; 4 jednotky = 1 m).
  - Stredom je bod, kam sa pozerá kamera (Free Cam / RTS). Humvee je len testovací režim.
  - `shiftWorld` posúva terén, objekty, kameru, vodu, stromy a mosty o chunk.
- Pamäť chunkov:
  - `m_chunks` sú chunky v okne a okolí.
  - `m_parked` je LRU cache v RAM (600 chunkov) pre návrat.
  - `m_requested` drží čas požiadavky a po 8 s sa požiadavka zopakuje.
- Pri nenačítanom okraji sa okno neposunie (`stripReady`) a kamera spomalí a zastaví (`View::setStreamLimit`, `W3DView::applyStreamLimit`).
- Jednotky za oknom sa odložia (typ, hráč, zdravie, veteránstvo, poloha vo svete) a vrátia na svoje miesto (`parkUnitsOutside` / `restoreParkedUnits`).
- Pri posune sa posunie cieľ pohybu všetkým pohybujúcim sa jednotkám.
- **Voda:**
  - `rebuildWater` vytvára `PolygonTrigger`-e vo vlastnom reťazci `g_gxStreamWaterFirst`, nie v zozname mapy (objekty si držia pointery na triggery).
  - `g_gxStreamWaterLookup(x, y, forLogic)` hľadá vodu po chunkoch. Potoky majú `WATER_FLAG_FORD`: kreslia sa, ale logika ich ignoruje (brodenie).
  - Mäkký breh: `BaseHeightMap::streamWaterChanged`. Kreslenie: `W3DWater::renderWater` prejde aj reťazec streamu.
- **Stromy:**
  - `rebuildTrees` ich kreslí cez `W3DTreeBuffer`, bez herných objektov.
  - `MAX_TREES` 32000, `MAX_BUFFERS` 12, `streamClearTrees` zachová typy.
  - Režimy: OFF / OPTIMIZED (do 2 chunkov všetky, ďalej 1/3, max 12k) / UNLIMITED (32k).
- **Textúry:** `WorldHeightMap::streamBlendGround` je auto-blend ako vo World Builderi. Priority sú v `m_texturePriority`.
- **Mosty:**
  - `rebuildBridges` spojí kúsky mosta, delí ich na rovné polia podľa zlomov aj výšky a postaví 12 najbližších (limit vrstiev pathfindera).
  - Šírka ide cez `W3DBridge::setWidth` / `W3DBridgeBuffer::addBridge(..., width)`.
- **Cesty:** `rebuildRoads` → `W3DRoadBuffer` stream API (`loadStreamRoads`, `translateStreamRoads`). Body ciest sa pripravujú na sieťovom vlákne (`prepareChunkLines`: welder, RDP).
- **Radar:** `Radar::setStreamView`, posúva sa s kamerou.
- **Pathfinder:** `AIPathfind.cpp` má `rebuildAfterWorldShift` a cache terénu (`shiftTerrainCache`, `invalidateTerrainCache`, `releaseBridgeLayer`).
- **Menu:** `engine\GeneralsMD\...\Menus\GXWorldExploreMenu.cpp`
  - grid 5–13, režim RTS/Free Cam vs. Humvee
  - checkboxy budov a ciest, tlačidlo `TREES: OFF/OPTIMIZED/UNLIMITED`, overlay, wireframe
- **Klávesy v hre:** Ctrl+F9 Free Cam, Ctrl+F10 debug overlay, PgUp/PgDn sklon kamery.
- Predvolený spawn je **dozer** (`AmericaVehicleDozer`), vybraný aj cez `MSG_CREATE_SELECTED_GROUP`. Bez toho nešli príkazy pohybu.

### Server
- `server.py`: požiadavky na chunky obsluhuje od najbližšieho ku kamere (`pending` + `focus` z `M_POSITION`).
- `world.py`: session a cache chunkov.
- `worldmap.py`: zapisuje základné mapy.
- `worlddata.py`: dekódovanie dlaždíc. Vrstvy streets, buildings, water, land, sites, pois a **bridges** (obrysy mostov).
- `worlddata._smooth_roads`: zaoblenie zákrut ciest (Bézier, polomer podľa triedy `BEND_RADIUS_M`, krok ≤ 8°, križovatky a konce pripnuté). Klient `simplifyRun` delí aj pri odchýlke smeru > 10°, takže všetky spoje sú pod prahom ~27° W3DRoadBuffer (miter namiesto oblúkových dielcov).
- Rekvizity `KIND_PROP` (`_chunk_props`): hroby a krypta (cintorín), ihriská, lavičky a fontány (park), autá na parkoviskách (rady), pri obrubníku v zástavbe a na dvoroch, socha anjela pred kostolom. Klient ich stavia ako budovy. `GermanTownHall` je len pri kostole (POI `place_of_worship`).
- **Fáza 1 realizmu (GEN 18, len server, engine bez zmeny, v hre zatiaľ netestované):**
  - **Parkoviská.** Uličky `parking_aisle` sa už nezahadzujú: majú lôžko a asfalt, ale nemajú pás. Pozdĺž nich sú rady miest s bielymi čiarami pôvodného typu hry `ParkingLines` (namerané z `TRParkingLines`: priečka každých 17,54 j., zadná čiara 15,1 j. vľavo od smeru čiary). Parkovisko bez uličky dostane virtuálne uličky na dlhej osi. Miesta sa neprekrývajú (obsadenosť po 2 j.), obsadených je asi 70 % a autá stoja čelom k zadnej čiare.
  - **Futbalové ihriská.** `sources.OsmExtras` posiela Overpass dopyt na jednu z14 dlaždicu v pozadí (nikdy neblokuje) a ukladá ho do `cache\osm\pitch`. Kým dopyt nepríde, chunky sú neúplné a neukladajú sa. Ihrisko má čiary `SingleWhiteLine`, bránky `SoccerStadiumGoal` (otvor = +x modelu, neoverené) a trávu. Chodníky cez ihrisko sa nekreslia (`_off_pitch`).
  - **Železnica.** Na priecestiach `RRCrossingGate` vpravo pri každom príjazde a `RRSign`; pri poľných cestách iba značka. Na polovici vedľajších koľají stoja vagóny `TrainCoalOrnament` / `TrainTankOrnament`. Autá neparkujú do 15 m od priecestia.
  - **Obytné štvrte.** Lampy každých 35 m, striedavo po stranách ulice. Ploty alebo živé ploty pred 3/4 domov na hranici pozemku (`_front`, `_plot_line`), s medzerou na bránku. Koše a stojany na bicykle pri obchodoch.
  - Čiary značenia sa nerežú na okraji chunku (patria chunku so stredom ich obrysu), majú iba svoje body a ich konce sa nedotýkajú, pretože klientov welder spája body do 1 j.
  - Neoverené natočenie modelov: rameno závory a lampy smerujú k ceste po osi +x, otvor bránky je na +x.
- **GEN 19 (feedback po teste, v hre zatiaľ netestované):**
  - Ihrisko: obdĺžnik s textúrou `G_FIELD` (`GrassMediumType38a`, ako v generátore), čiary a bránky ostali. Overpass dopyty sa posielajú hneď po otvorení miesta (±2 km) aj pri prednačítaní.
  - Engine `82e6100`: neúplný chunk (príznak 1 v hlavičke) si klient každých 20 s vyžiada znova a úplný ho nahradí (`retryIncomplete`, `replaceChunk`). Neúplné chunky sa neodkladajú do pamäte pre návrat.
  - Priecestia: rampa `CheckPointCrossingGuard` (rameno po −y modelu, stĺpik pri pravom okraji, za krajnou koľajou). Portál `RRCrossingGate` (priečnik po y) len na hlavných cestách v hustom meste. Značka `RRSign` len na poľných cestách. Koľaje jedného priecestia sa zlučujú.
  - Oplotenie areálov (`FENCE_LAND`): pletivo `ChainLinkFence01` (pivot na konci, +x) okolo škôl, nemocníc, priemyslu do 25 ha, športovísk a záhradkárskych osád; `BrickWall01` okolo cintorínov. Plot ide len po vlastných úsekoch obrysu dlaždice (`Area.own`), s medzerou pri ceste a budove.
  - Paneláky: `_merge_sections` spája sekcie hĺbky 9–17,5 m, ktoré stoja stena pri stene, do bloku. Panelák = plocha > 260 m² a hĺbka > 9 m. `_apt_pieces`: bodovka do 22 m = `CivilianHighrise02`, dlhší blok = sekcie podľa počtu vchodov (12,5 m), nepárny počet `AsiaApartment02`, párny `AsianApartmentSingle01`.
  - Benzínky: trieda `fuel`, model podľa plochy (`GAS_STATIONS`, nezávisle od krajiny), otočený pozdĺž ulice. Bod benzínky bez budovy (`_fuel_points`) dostane stanicu vedľa cesty.
  - Husté mesto: dlaždica z14 s aspoň 900 budovami/km² a 28 % zastavanej plochy (`TileData.dense`). Model sa vyberá zo všetkých štýlov a z `DENSE_MID`, s väčšou náhodnosťou.
  - Parkoviská: rad pri každej rovnej hrane parkoviska so zadnou čiarou pri obrubníku, súbežne s chodníkom. Bez mapovanej uličky sa pridajú vnútorné uličky súbežné s najdlhšou hranou a rady chrbtom k sebe.
- **GEN 20 (druhý feedback, v hre zatiaľ netestované):**
  - **Polia** (`farmland`) majú novú triedu `G_FARM` = `SandType1` (svetlohnedá pôda). `GROUND_CLASSES` má 13 tried; trieda nad paletu šablóny sa doplní vo `worldmap.build` (128 px = 2×2 dlaždice, ako generátor). Základné mapy sú prestavané v `gamedata\` a skopírované do `game\Maps`. Pri ďalšej triede treba mapy znova prestavať (`python worldmap.py`) a skopírovať.
  - **Parkoviská:** maska parkoviska má ostrý `G_ASPHALT` (bez okraja posúvaného šumom). Pod každý rad miest sa namaľuje asfalt. Cesty `PARK_HIDDEN` (service a podobné) sa na parkovisku nekreslia (`_off_pitch` s maskou).
  - **Paneláky:** tvar paneláka (`flats`) má prednosť pred bodom krčmy alebo obchodu v budove (149 m blok s krčmou bol radom domov). Platí aj pravidlo: dĺžka ≥ 24 m a hĺbka ≥ 9 m = panelák. Posunuté sekcie (≥ 3 alebo ≥ 400 m², hĺbka ≥ 10 m) sú paneláky po jednom (`panel_ids`).
  - **Hladké cesty:** lôžko cesty siaha o políčko za šírku, ktorou hra pás kreslí (`DRAWN_WIDTH`, z `Roads.ini`). Výška cesty sa berie z `Eroad` (`Esm` ešte 4× vyhladené). Voda sa vynecháva len pod vykresleným pásom (`road_core`), nie pod celým lôžkom.
  - Pozor, nájdená chyba: `tree_density[None] = 0` vynuluje celé pole. Nulovanie pitch masky musí byť pod `if self.pitch_mask is not None`.
- **GEN 21 (tretí feedback, v hre zatiaľ netestované):**
  - **Polia okolo domov:** pole (`G_FARM`) do 30 m od každej budovy je tráva. Obec Zelené je v OSM vnútri jedného polygónu poľa.
  - `sources.WorldCover` (ESA WorldCover 10 m, COG na AWS, čítanie po častiach a cache v `cache\worldcover`) je hotový a otestovaný, ale **vypnutý**: používateľovi to trvalo dlho. Zapína sa v `Session` cez `self.data.landcover = world.landcover`, pravidlá sú v `_land_cover`.
  - **Námestia a pešie zóny:** druhý dopyt na Overpass (`OsmExtras.squares`: `place=square`, pešie plochy, `area:highway`, trhoviská). Dostanú `G_COBBLE` s ostrým okrajom a len málo stromov.
  - **Bodovky:** `_estate_tower` = plocha aspoň 180 m², hĺbka aspoň 10,5 m, dĺžka do 22 m a do 150 m stojí iný panelák (`flats_pts`).
  - **Chodníky:** `_sidewalks` pridá k mestským uliciam (`SIDEWALK_STREETS`, obytné a podobné plochy) chodník na oboch stranách za vykresleným okrajom, s medzerou pri iných cestách, budovách a zmapovaných chodníkoch (do 10 m). Schody sú `footway`. Autá pri obrubníku stoja na okraji vozovky, lampy za chodníkom.
  - **Trávnaté poľné cesty** (`track_rutted`) sa nekreslia.
- **Veľké okno 25×25 (4 km), engine commit `5f56da8`, v hre zatiaľ netestované.** Je to predvolená veľkosť, v menu je za 13. Konštanty sú v `GXWorldStream.cpp` pod `THE BIG WINDOW`. Platí od `kBigWindow` = 17.
  - Kamera sa po okne pohybuje voľne. Svet sa posunie, až keď je kamera do `kEdgeMargin` = 5 chunkov od okraja, a to o toľko chunkov, aby bola zas v strede (pri okne 25 je to 8). Bez stmavnutia obrazovky.
  - Pás, ktorý posun prinesie, sa začne sťahovať už 3 chunky pred bodom posunu. Kým nie je celý v pamäti, kamera sa zastaví 2 chunky pred okrajom.
  - Budovy a rekvizity sú v hre len do `kObjectRing` = 5 chunkov od kamery a odoberajú sa za `kObjectKeep` = 6 (`updateObjectRing`). Cesty sa kreslia do `kRoadRing` = 7. Stromy a mosty sa radia od kamery, nie od stredu okna.
  - Hra štartuje, keď je v pamäti `kStartRing` = 4 chunkov okolo spawnu (`coreReady`). Zvyšok okna dochádza počas hry, najviac `kLatePerFrame` = 6 chunkov za snímku, najbližšie ku kamere prvé. Pathfinder sa prestaví najviac raz za `kPathDelay` = 0,5 s.
  - Radar ukazuje asi 12 chunkov okolo kamery.
  - Humvee (commit `9a72019`):
    - Stredom je vozidlo, ale len kým je naň kamera zamknutá (`followingVehicle`). Keď hráč kameru pustí (free cam, posun), okno ide s kamerou a brzda kamery platí.
    - Ctrl+F9 zapne free cam a vozidlo pustí, vypnutie vráti kameru na vozidlo. Pri posune sa kamera znova zamkne len vtedy, ak bola zamknutá.
    - Pás sa sťahuje 6 chunkov pred bodom posunu. Brzda vozidla je odstránená.
    - Odložená jednotka sa vráti vybraná, ak bola vybraná, a Humvee ostáva testovacím vozidlom (`ParkedUnit.selected/vehicle`).
  - `kEdgeMargin` = 3: od stredu je to 10 chunkov (1,6 km) bez posunu, potom posun o 10.
  - Hlásenie používateľa: Humvee občas nemá health bar a správa sa ako nevybraný. Príčina zatiaľ nie je známa, v kóde sa nenašla.
    - Commit `5a75b7f` pridal `checkVehicleSelection`. Raz za sekundu porovná výber na obrazovke a v simulácii. Ak sa dvakrát po sebe líšia, vyberie Humvee v oboch (`MSG_CREATE_SELECTED_GROUP_NO_SOUND`).
    - Pri zásahu zapíše do logu `the test vehicle was selected ... only`. Log vzniká len pri spustení cez `run-test.ps1`.
  - Server: okno 3–25, prvé chunky od spawnu von, `M_CHUNKS` po riadkoch (server prijme najviac 400 v jednej požiadavke). Základná mapa `WorldExploration25` má 1600×1600 a 30 MB.
- **Zombie mód.** Zadanie je v prílohách používateľa (`INTEGRACIA.md`, `continuous_rules.py`, ZHE INI). Plán má 3 dodávky.
  - **Dodávka 1, hotová (commit `9861a73`, server, v hre zatiaľ netestované):**
    - **Menu:**
      - `ZOMBIE MODE` (len Humvee).
      - `WORLD: REVEALED / GREY / BLACK`.
      - Posuvník `World darkness` 0–100 %.
      - Posuvník `Zombie limit` 50–3000 (predvolene 300).
      - Env premenné `ZHE_ZOMBIES`, `ZHE_WORLDVIEW`, `ZHE_DARKNESS`, `ZHE_ZOMBIECAP`.
    - **Stmavenie** (`applyWorldLook` / `restoreLighting`): pri 100 % násobí svetlo terénu a objektov ×0,25 a trochu pridá modrú. Volá `TheDisplay->setTimeOfDay`.
    - **Hmla:** sivý režim = `revealMapForPlayer` (všetko vo fog). Čierny režim = shroud hry. Pri posune `PartitionManager::gxShiftShroud` posunie preskúmané bunky a `gxReapplyShroud` beží o 2 a 20 snímok neskôr.
    - **Protokol 3:**
      - `OPEN` layers bit 8 = zombie beh, ktorý používa mapu `WorldExploration<N>Z`.
      - `M_POPULATION` 8 / `R_POPULATION` 106: body vo svetových jednotkách, odpoveď obsahuje riadok, stĺpec, stav (0 známe, 1 bez údajov, 2 čaká sa) a počet obyvateľov.
    - **Populácia** (`sources.Population`):
      - GHS-POP R2023A, epocha 2025, Mollweide 1 km.
      - Dlaždice R/C po 1000 km sú zip s BigTIFF (LZW, prediktor 3, float64, nodata −200). Dekódujú sa za ~1 s a ukladajú do `cache\ghspop\R*_C*.f32`.
      - Bunka je (row, col): row 0 pri y = 9 000 000, col 0 pri x = −18 041 000, R = 6 378 137.
      - Overené: Poltár ~5 600 v okolí 5×5 km, Lučenec ~29 000, Banská Bystrica ~62 000.
    - **Zombie mapa:**
      - `worldmap.py --zombie` pridá stranu `Zombies` (GLA, červená, nepriateľ Explorera) a `map.ini` = `worldserver/zombie/humvee_shop.ini` + `zombie_tiers.ini`.
      - Všetky odkazy aj `RemoveModule` tagy sú overené proti herným INI aj s patchom.
      - `ensure()` mapu prestaví, keď je `zombie/` novší.
  - **Dodávka 2, hotová (commit `43885d9`, v hre zatiaľ netestované).** Kód je v `GXWorldStream.cpp` v sekcii `ZOMBIES`: `zombieStart`, `zombieUpdate`, `zombieSpawn`, `drawZombieHud`.
    - **Zmena po feedbacku:** limit neurčuje populácia, ale domy. Každý dom má 3–8 zombíkov podľa hashu svojho world id a za celý beh sa neobnoví. Populácia GHS-POP ostáva na serveri, ale nepoužíva sa.
    - **Peniaze:** štart 500, za obyčajného zombíka 2, ostatné typy podľa build cost voči nemu. Zabitie sa uzná, ak posledný zásah patril hráčovi.
    - **Spawn:**
      - Dom 40–120 m od Humvee, stena odvrátená od Humvee, mimo obrazovky prednostne.
      - Bunka pathfindera musí byť `CELL_CLEAR` aj s výbehom; Bomb Truck potrebuje šírku cesty.
      - Výbeh 20 m, potom `aiHunt` každé 3 s. Dom má medzi zombíkmi 4 s pauzu.
    - **Záznamy:** `ParkedUnit.zombie` odkazuje na záznam zombíka. Zombík odložený 3 km od Humvee dlhšie než 60 s sa odstráni a dom ho dostane späť.
    - **HUD:** fáza, čas, zabitia, zarobené, živí / limit, zostáva v domoch do 1 km. Zničený Humvee ukončí beh.
  - **Commit `90264bd`:**
    - **Posun v boji:**
      - A: zombíci po posune hneď `aiHunt`, útočiace jednotky si útok nechajú.
      - B: krok `kZombieShiftStep` = 6 a žiadny posun, keď sú do 250 m viac než 3 zombíci, okrem kamery tesne pri okraji.
      - C: horda za okrajom sa presunie chunk dovnútra okna.
    - **Šípky:**
      - Voľba `keyDriving` v menu.
      - Príkaz na bod 100 m vpred sa dáva len pri zmene, takže veža sa otáča.
      - Šípka dole cúva ručným posúvaním.
      - `LookAtXlat`: so zamknutou kamerou šípky neposúvajú kameru.
    - **Stmavenie:** je to tmavomodrý závoj cez 3D obraz v `drawOverlay` (do 70 %). Úprava svetla v `applyWorldLook` je vypnutá, lebo spôsobovala čierne budovy a chyby pri posune.
  - **Jazda šípkami a zamknutá kamera (commity `4d1a5ce` až `40cb41e`):**
    - Jazda šípkami je priame riadenie auta snímku po snímke (`driveWithKeys`). Rýchlosť je z lokomotora Humvee, cúvanie polovičné. Svahy (CELL_CLIFF) jazdu nebrzdia. Nitro bolo odstránené.
    - **Hlavná chyba (`47f06c6`):** `W3DView` pri zapnutom filtri skrýval jednotku so zamknutou kamerou a predvolený filter (`FT_VIEW_DEFAULT`) je zapnutý stále. Humvee sa preto odznačoval, nemal health bar, nedal sa kliknúť a nemal obchod. Teraz ho skrýva len skutočný efekt. Overené testom: Humvee je vybraný a obchod funguje.
    - **Veža:** cieľ je najbližší nepriateľ v dostrele (`setTurretTargetObject`) a drží sa, kým žije. AI sa budí cez `AIUpdateInterface::gxWakeUp`, lebo pri nečinnosti spí a veža by sa neotáčala. V hre zatiaľ netestované, lebo vstrihnutie kláves je zakázané.
    - Free cam ostáva zamknutá na Humvee. Zoom kolieskom po prepnutí kamery treba ešte overiť.
  - **Veža pri jazde šípkami: stále vizuálne stojí** (`4354e74` = `aiAttackObject`, netestované ako oprava). V `28c19c8` je diagnostika: `[TURRET]` (logika) a `[TURRETDRAW]` (kreslenie, len jednotka so zamknutou kamerou) raz za sekundu. Prvý log mal len `[TURRETDRAW]` z rámca 0; `[TURRET]` nevzniklo, lebo blok veže beží každých 10 rámcov a podmienka `frame % 30` sa s ním netrafí (opraviť na vlastný počítadlo). Podozrenie: `47f06c6` (Humvee sa kreslí bežnou scénou, nie `renderSpecificDrawables`); v `164bf14` veža išla, ale chýbal tieň.
  - **Atómovka (commit `28c19c8`, v hre zatiaľ netestované):**
    - `humvee_shop.ini`: Humvee má `OCLSpecialPower ModuleTag_EARTH_Nuke` (`SuperweaponNeutronMissile`, `StartsReady = Yes`), tlačidlo `EARTH_Command_Nuke` na slote 10 prvej strany (namiesto jedného Exit). Raketa letí z Humvee (`FireWeapon` OCL, štart z kosti zbrane Humvee - neoverené).
    - 2× silnejšia: `SpecialPower SuperweaponNeutronMissile` (kurzor 420) a `ReplaceModule ModuleTag_06` na `NeutronMissile`: polomery 2×, Blast6 7000/600, scorch 640. Hodnoty sú z Community Patchu (`500_900_CommunityPatch_CoreINI.big`), nie z retail INI.
    - Kráter: `g_gxNukeDetonated` v `NeutronMissileSlowDeathUpdate.cpp` (prvá ničivá vlna) → `GXWorldStream::addCrater`. Polomer 0,48 × dosah (≈200 j.), hĺbka ¼ polomeru, profil cos², val do 1,3 R, `RocksMediumType1` vnútri, 4 vrstvy scorch (SCORCH_1–4). `m_craters` vo svetových súradniciach, `Chunk::cratersDone`, `craterChunk` v `applyTerrain`. Po posune `redrawCraterScorches` (zmaže všetky scorch a nakreslí krátery znova). Stromy v kráteri sa nekreslia.
    - Zombie mapy 7/11/13/25 Z sú prestavané (`worldmap.ensure`).
  - **Pravidlo map.ini:** moduly (Draw, Body, Behavior) musia byť vnútri `AddModule ... End`, aj v úplne novom objekte, inak `ThingTemplate::parseModuleName` hodí výnimku a hra spadne pri načítaní mapy („Error parsing INI file ... Line: 'Object ...'“ – hlásenie ukazuje len začiatok bloku). Prvý test Mega nuke spadol presne na tomto, opravené v `humvee_shop.ini`.
  - **Tri zóny krátera a nový svet v každom kole (commit `0067a7f`, GEN 23, v hre zatiaľ netestované):**
    - Variant C z návrhov (`logs\crater-navrhy.png`): popol `DirtMediumType2` do 0,65 R, spálená pôda `DirtMediumType1` do 0,95 R, spálená tráva `GrassMediumType23brown` (nová trieda servera `G_SCORCHED`) do 1,3 R. Okraje: uhlový šum krátera (`craterPhases`, 3/7/13 vĺn, ±22 %) a náhodné bunky v páse 0,2 R. Bunka si nechá „spálenejšiu“ triedu. Siaha do 1,65 R (`kCraterGroundReach`).
    - Spáleniny: dve v strede (0,85 a 0,55 R) a 9 škvŕn po obvode (0,7–1,15 R), nie čierny kruh.
    - `server.py` `DestroyedStore`: zničené objekty len v pamäti počas jedného OPEN, starý `state\<grid>\destroyed.bin` sa zmaže. Každé kolo začína s celým svetom, počas kola ruiny ostávajú.
  - **Kráter vyčistí zem, ruiny ostanú, zoom na zámku (commit `34a003b`, GEN 22, v hre zatiaľ netestované):**
    - Server: `GROUND_CLASSES` + `DirtMediumType2` (`G_BURNT`, žiadny chunk ju nepoužíva; najtmavšia sivohnedá pôda zo 561 textúr, meraná podľa jasu). `GEN_VERSION` = 22. Všetky základné mapy (bežné aj Z) sú prestavané a skopírované do `game\Maps`.
    - Kráter: spálená zem do 1,18 R (bez triedy na serveri: skala). `kCraterClear` = 1,15 R: cesty sa orežú na kružnici (`rebuildRoads`, `addSegment`), most, ktorý zasahuje do krátera, sa nepostaví (`rebuildBridges`), rekvizity sa zničia natrvalo (M_DESTROYED), stromy sa nekreslia.
    - Ruiny: zničená budova (OBJECT_FLAG_DESTROYED) sa pri `populate` znova vytvorí ako ruina (`makeRuin`: POST_COLLAPSE ak má StructureCollapseUpdate, inak RUBBLE, `setEffectivelyDead`, bez pathfindera). M_DESTROYED sa pre ňu znova neposiela.
    - Zoom: `W3DView::updateView` rátal každý zámok kamery ako skriptovaný pohyb (výška sa každú snímku prepísala aktuálnou, koliesko nemalo účinok). Teraz len keď `!m_isUserControlled || isDoingScriptedCamera()`.
  - **VRÁTENÉ (commit `d595c19`):** Mega nuke aj NUKE CONFIG nefungovali (prvý test spadol na map.ini, druhý „nejde to“: v logu je len config, žiadny výstrel ani kráter). Používateľ chcel návrat na `28c19c8`. Engine je teraz zhodný s `28c19c8`. `humvee_shop.ini`: len nuke, **3× hra** (dosah 630, Blast6 10500/900, scorch 960, kurzor 630, polomery 180/270/360/450/540) a **bez timera** (`ReloadTime = 0`, `PublicTimer = No`; `getPercentReady` pri pripravenej supersile nedelí). Kráter = 0,48 × 630 ≈ 302 j. (≈75 m), hĺbka ¼ (≈19 m). Slot 9 je opäť Exit. Popis nižšie platí len historicky.
  - **Mega nuke a NUKE CONFIG (commit `8c0c093`, vrátený):**
    - `humvee_shop.ini`: `EARTH_SuperweaponMegaNuke` (Enum `SUPW_SPECIAL_NEUTRON_MISSILE`), `EARTH_SUPERWEAPON_MegaNuke` OCL, `EARTH_MegaNukeWeapon`, plný objekt `EARTH_MegaNukeMissile` (kópia patchovej rakety, 4× hodnoty). Tlačidlo `EARTH_Command_MegaNuke` na slote 9. Lišta je po stĺpcoch: nepárne sloty hore, párne dole (`ControlBar.wnd`), 9 je nad 10. Ikona a text sú ako pri nuke.
    - Menu: tlačidlo NUKE CONFIG (dolný rad, v strede) otvorí panel s dvoma stĺpcami (NUKE, MEGA NUKE). Každý stĺpec: poškodenie 1000–50000, dosah 20–400 m, kráter 0–250 m (0 = bez krátera), hĺbka 1–80 m, nabíjanie 1–30 min, „Ready at the start“. Tlačidlá DEFAULTS a BACK. Ukladá sa do `<user data>\WorldExplorationNuke.ini` (riadky `nukeN.key = value`).
    - `GXNukeConfig` / `GXWorldSettings::nuke[2]`, predvolené hodnoty sú v `gxDefaultNuke` (nuke 7000 / 105 m / 50 m / 12 m / 6 min, mega 14000 / 210 m / 100 m / 25 m / 10 min).
    - `applyNukeConfig` (pred spawnom Humvee, len keď existuje strana Zombies a `NeutronMissile` má override z map.ini) prepíše `NeutronMissileSlowDeathBehaviorModuleData` (profil výbuchu hry × dosah/210, Blast6 = poškodenie, min = 300/3500, scorch 320 × mierka) a cez `SpecialPowerTemplate::gxSetReloadAndCursor` aj nabíjanie a kurzor. `nukeReadiness` nastaví `setReadyFrame` na moduloch Humvee. Kráter berie veľkosť z configu podľa šablóny rakety (`g_gxNukeDetonated(missile, pos, radius)`). 1 m = 4 jednotky.
    - Nástroj na čítanie INI z .big: skript `bigblock.py` (scratchpad session, nie v repozitári) - parsuje BIGF, vypíše bloky podľa regexu hlavičky.
  - **Dodávka 3:** obchod, odmena (`Player::setCashBounty` 10 %), HUD a highscore.
  - **Potvrdené rozhodnutia:**
    - Bunka 1 km² má rozpočet `ceil(obyvatelia / 10)` zombíkov za celý beh a neobnovuje sa. Zombík odstránený bez zabitia sa do rozpočtu vráti.
    - Globálny strop živých z menu.
    - Zombie obsah je len vo variante `Z`.
    - Režim sveta a stmavenie platia pre všetky režimy.
- **`chunkgen.py`** (`GEN_VERSION = 21`, `MARGIN = 72`). Postup:
  1. `_pads`
  2. `_roads` (lôžko cesty)
  3. `_water`: hladina z DEM po chunkoch, obdĺžniky 8×8 a koryto
  4. `_streams`: bloky 4×4 pre koryto a breh, plocha ako **štvoruholníky pozdĺž línie toku**
  5. `_bridges`: spájanie úsekov, zlúčenie súbežných pásov (do 15 m, železnica zvlášť), šírka z obrysu, profil mostovky podľa toho, čo križuje (cesta +6,5 m, železnica +7,5 m, rieka +8 m, potok +2 m, max. sklon 4 %), násypy `_embankment` (6 %), krátke mosty nad potokom sú priepusty (`culvert_ways`), nad cestami `EuropeanBridgeTwoLane`, `deck_mask`
  6. `_ground`: textúry, posun okrajov plôch šumom (`value_noise`), `embank` nie je skala
- Stromy: `_chunk_trees` (mriežka 34 jednotiek, `TREE_LAND`, kroviny) a `_street_trees` (stromoradia).
- Payload `GXCK` v1: strings, heights f32, ground u8, roads (`u16 type, u8 rank, u8 flags(1=bridge), f32 width, points*(lx ly lz rx ry rz s)`), objects, water (`f32 z, u8 flags(2=ford), 3 bajty pad, u32 points, points*(x y)`).
- Po každej zmene výstupu treba zvýšiť `GEN_VERSION`, inak sa použije stará cache.

## 2. Pravidlá a postupy
- Build: `powershell -ExecutionPolicy Bypass -File tools\build-engine.ps1 *> logs\build.txt`
- Stage: `tools\stage-game.ps1 -SkipPatch`
- Test:
  ```
  tools\run-test.ps1 -Server -Place "Poltar" [-Window 9] [-FreeCam] [-Pan deg -PanSpeed u/s -PanBack s] [-Overlay] [-Seconds N] [-Shot logs\x.png -ShotAt "a b"] [-ClickAt "18:1000,450"]
  ```
- Env premenné: `ZHE_TRACE`, `ZHE_LOOKBRIDGE`, `ZHE_CAMHEIGHT`, `ZHE_CAMPITCH`, `ZHE_TREES`, `ZHE_PATHTEST`, `ZHE_CLICKTRACE`, `GX_TREES`.
- Logy: `logs\run-err.txt` (riadky `[WORLD]`), `logs\server-out.txt`. Pády: `game\userdata\CrashLogs` (symbolizácia cez `dbghelp` z PowerShellu, pdb je v `game\`).
- **Testy hry spúšťať len so súhlasom používateľa.** Hrá v druhom okne a `-ClickAt` hýbe skutočnou myšou. Pred testom ukončiť starý `generalszh` (aplikácia je jedna inštancia).
- Git: `(Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop" -Recurse -Filter git.exe | ? { $_.FullName -like '*\cmd\git.exe' } | Sort FullName -Desc | Select -First 1).FullName`. Commit správa cez `-F <súbor>`. Commity sú len v `engine\` na `openworld`.
- Repozitár pre celý projekt (worldserver, tools, gamedata, FEATURE) sa založí až po vyladení.
- Prostredie: PowerShell 5.1 (bez `&&`, bez `Join-String`).
- Windows makro `far`: nepoužívať ho ako názov premennej.

## 3. Čo funguje (posledný commit `82e6100`: neúplné chunky sa vyžiadajú znova; GEN 19 čaká na test v hre)
- Streamovanie podľa kamery, cache v RAM, brzda na okraji, prioritné načítanie chunkov.
- Cesty, mosty so skutočnou šírkou a výškou a násypy, radar, horizont, overlay.
- Voda: Dunaj a jazerá blokujú jednotky. Potoky sa kreslia ako štvoruholníky a dajú sa prebrodiť.
- Mäkké prechody textúr, stromy (32k), mestská zeleň (prvá verzia), dozer sa dá ovládať.
- Jednotky ostávajú na svojom mieste aj ďaleko od kamery.

## 4. To-Do (feedback používateľa, začať tu)
**Fáza 2 (engine, schválená v pláne, ešte nezačatá):**
- **Minimapa.** Bod vo farbe hráča pre skupiny vlastných jednotiek, aj zaparkovaných mimo okna (`m_parkedUnits`). Mimo zobrazenej časti sa bod prichytí na okraj. Hook z `GXWorldStream` do `W3DRadar::draw`.
- **Premávka.** Autá jazdia po sieti ciest okna cez `aiFollowPath` po krátkych úsekoch, najviac 40 áut. Chodci sú `MogadishuMale/FemaleCivilian` na chodníkoch, najviac 30. Premávka sa neparkuje, mimo okna zanikne.
- **Idúci vlak.** Kinematicky po trati `rail`, rýchlosťou asi 70 km/h. `RailroadBehavior` treba v stream režime vypnúť, inak sa prichytí k waypointu štartu hráča.

Staršie body:
1. **Hladké cesty.** Cesty majú „zuby“ (lomené segmenty).
   - Pravdepodobné príčiny: kvantovanie MVT (z14, extent 4096), RDP s `eps` 5–10 v `prepareChunkLines` a rovné úseky v `W3DRoadBuffer`.
   - Riešenie: vyhladiť os cesty na serveri (Chaikin / Catmull-Rom), menší RDP pre zákruty, prípadne krivky v `W3DRoadBuffer`.
2. **Podjazdy a tunely.** Cesta pod mostom má prechádzať plynulo. Tunely sa teraz zahadzujú (`worlddata.py`, `p.get('tunnel')`). Treba portály a cestu „pod terénom“ (napr. skryť ribbon a dať portálové objekty).
3. **Hladké potoky.** Štvoruholníky s rôznou hladinou robia schody.
   - Riešenie: jeden súvislý pás so sklonom. Engine má rieky v `PolygonTrigger` (`isRiver`, `W3DWater::drawRiverWater`), kde body zrejme nesú z. Využiť to (aj úpravou jadra) a koryto vymodelovať hladko pozdĺž línie, nie po blokoch 4×4.
4. **Mesto je prázdne.**
   - Viac stromov.
   - Ihriská (`leisure=playground`, `pitch`), cintoríny (hroby, kaplnky, ploty).
   - Lepšie parkoviská (povrch, rozmiestnené autá).
   - Spawn áut podľa reality: parkoviská, ulice, dvory. Vrstva `sites` / `land` má druhy, `pois` a `street_polygons`.
5. **Townhall model (vyzerá ako kostol) len pri skutočnom kostole.** Pozri `chunkgen._classify` / `_pick_model` (POI `place_of_worship` / budova `church`).
