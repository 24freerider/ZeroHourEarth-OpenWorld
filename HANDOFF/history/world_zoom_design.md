# World Zoom: plynulý prechod od terénu pod kamerou po celú planétu

**Stav:** konceptuálny návrh (bez implementácie v engine). **Dátum:** 13. 11. 2026.
**Kontext:** Zero Hour Earth – Open World (GeneralsX, vetva `openworld`, commity `39872bb`, `315fb83`).

## 1. Cieľ

Hráč stojí v RTS pohľade nad mestom (bunka 2,5 m, budovy, cesty a mosty) a začne kamerou oddiaľovať. Pohľad sa má plynulo, bez načítavacej obrazovky a bez viditeľného „skoku“, dostať cez okres, krajinu a kontinent až na celú zemeguľu. Potom sa dá priblížiť na ľubovoľné iné miesto a skončiť opäť v hrateľnom detaile.

Čo návrh **nemení**: hrateľná vrstva ostáva dnešné streamované okno (heightmapa N×N chunkov, objekty, pathfinder). Všetko ostatné je „pozadie“, ktoré sa iba zobrazuje.

## 2. Východiská a tvrdé obmedzenia enginu

| Obmedzenie | Dôsledok pre návrh |
|---|---|
| Logika, objekty aj pathfinder pracujú v 32-bit `float` súradniciach jednej mapy (desiatky km by stratili presnosť). | Hrateľné okno ostáva malé. Vzdialený svet sa rieši **plávajúcim počiatkom** (už existuje: `shiftWorld`) a vlastnými súradnicami vrstiev. |
| Heightmapa má pevnú veľkosť a pevný krok `MAP_XY_FACTOR` = 10 jednotiek. | Iná mierka sa nedá dosiahnuť „zväčšením mapy“. Vzdialené vrstvy sú **samostatné render objekty** s vlastnou mriežkou. |
| Indexy vertex bufferov sú 16-bitové (≤ 65 535 vrcholov na buffer). | Vzdialený terén je rozdelený na dlaždice s vlastnými buffermi (napr. 33×33 alebo 65×65 vrcholov). |
| Hĺbkový buffer (24 bit) a jedna near/far rovina nepokryjú 1 m až 10 000 km. | Kreslenie je **rozdelené podľa hĺbky** (viac passov s vlastnými near/far rovinami, od vzdialených po blízke). Pri obežnej dráhe sa použije logaritmická hĺbka alebo reverse-Z. |
| Simulácia je deterministická a viazaná na logický snímok. | Zoom je **čisto klientsky**: nemení simuláciu, len to, čo sa kreslí a čo sa sťahuje. |
| Dnešný server: chunky 160 m (z15 výšky, z14 vektory), disková cache. | Server dostane **pyramídu dlaždíc** pre všetky úrovne a nový typ správy. Chunky ostávajú pre hrateľné okno. |

## 3. Úrovne detailu (LOD pyramída)

Svet sa zobrazuje ako **vnorené prstence okolo bodu pod kamerou** (geometry clipmaps, Losasso & Hoppe 2004). Každá úroveň má 2× až 4× hrubší krok ako predchádzajúca a pokrýva väčšie územie. Bližšie úrovne sa kreslia „cez“ vzdialenejšie.

| Úroveň | Krok mriežky | Dosah (polomer) | Obsah | Zdroj |
|---|---|---|---|---|
| **L0 – hrateľné okno** | 2,5 m | 0,4–1 km (5×5 … 13×13 chunkov) | presný terén, textúry podľa tried, cesty, mosty, budovy, jednotky, pathfinder | dnešné chunky (z15 výšky, z14 vektory) |
| **L1 – blízke okolie** | 10 m | ~5 km | výškové pole, satelitná alebo klasifikovaná textúra, hlavné cesty ako textúra, impostory budov (kvádre) | terrarium z13–z14, vektory z12–z13 |
| **L2 – kraj** | 40 m | ~20 km | výškové pole a textúra | z11–z12 |
| **L3 – región** | 160 m | ~80 km | výškové pole, textúra, rieky a pobrežia | z9–z10 |
| **L4 – krajina/kontinent** | 1–5 km | ~1 000 km | výškové pole, farebná mozaika, hranice a pobrežia (vektor) | z5–z8, Natural Earth |
| **L5 – planéta** | dlaždice kocky premietnutej na guľu | celý svet | elipsoid WGS84 s výškami z4, mozaika, atmosféra | z0–z4 |

Prechod medzi úrovňami riadi **chyba v pixeloch na obrazovke**: úroveň sa zobrazí, keď by hrubšia úroveň mala chybu nad 1–2 px. Výsledkom je, že pri danej výške kamery sa kreslí 3–5 úrovní naraz.

## 4. Súradnicové systémy

1. **Okno (game units, float):** dnešný stav, počiatok v juhozápadnom rohu okna, posúva sa `shiftWorld`.
2. **Grid sveta (double):** kotva (`anchor lat/lon`) + `worldgrid.py` (lokálne ekvidistantné premietnutie, metre na stupeň pri kotve). Vhodné do ~50 km od kotvy.
3. **ECEF (double):** geocentrické karteziánske súradnice pre L3–L5 a pre zakrivenie Zeme.
4. **Relatívne ku kamere (float, vo shaderi):** každá dlaždica nesie svoj stred v double a jej vrcholy sú relatívne voči nemu. Pred odoslaním do GPU sa odčíta pozícia kamery v double („camera-relative rendering“). Float sa tak nikdy nepoužije na absolútne súradnice planéty.

**Zakrivenie:** do ~20 km sa používa rovinná aproximácia s poklesom `h = d² / (2R)` (pri 10 km je to 7,8 m, viditeľné na horizonte). Od L3 nahor sa dlaždice počítajú priamo v ECEF.

**Opätovné ukotvenie (re-anchor):** keď sa ťažisko pohľadu vzdiali od kotvy o viac ako ~50 km (alebo pri priblížení na nové miesto), server otvorí novú session s novou kotvou. Okno L0 sa postaví nanovo v pozadí (pozri kap. 7). Kotvy sú zarovnané na mriežku 0,05°, takže cache sa dá znovu použiť.

## 5. Kreslenie

Nový render objekt `GXFarTerrain` (v `Core/GameEngineDevice`), ktorý kreslí L1–L5. Kreslí sa **pred** heightmapou L0:

1. **Pass „vesmír/atmosféra“**: obloha, hviezdy pri orbite, rozptyl atmosféry (jednoduchý Rayleigh/Mie gradient podľa výšky kamery). Nahrádza dnešný skybox nad určitou výškou.
2. **Pass „vzdialené prstence“** s vlastnou near/far rovinou (napr. 500 m až 10 000 km a logaritmická hĺbka). Prstence L5 → L1 sa kreslia od najhrubšieho. Hĺbka sa potom vymaže.
3. **Pass „hra“**: dnešná scéna L0 (terén, cesty, objekty) s pôvodnými rovinami.

**Švy medzi úrovňami:**
- **Morphing vrcholov** v pásme pri okraji prstenca (vrchol plynulo prejde na výšku hrubšej úrovne), takže medzi úrovňami nevznikajú trhliny ani „pop“.
- **Sukne (skirts):** zvislé lemy dlaždíc zakryjú zostávajúce mikro-trhliny.
- **Okraj L0 ↔ L1:** L1 má v oblasti okna dieru (stencil alebo vyrezané dlaždice). Okraj okna sa výškovo zlieva s L1 v pásme 1–2 buniek. Farba L1 sa kalibruje na priemerné farby tried povrchu okna (to isté, čo dnes robí ambientný horizont v `skyBelowHorizonLand`).

**Opar a hmla:** výškovo závislá exponenciálna hmla vo farbe oblohy zjednotí vzdialené úrovne a skryje rozdiel v kvalite textúr. Dnešný „ambientný horizont“ je jej prvý krok.

**Objekty mimo L0:** budovy a stromy L1 ako inštancované kvádre/impostory z vektorových dát (bez logiky). Jednotky iných hráčov (multiplayer) ako ikony/markery.

## 6. Dáta a server

### 6.1 Pyramída dlaždíc
- **Výšky:** Mapterhorn / AWS terrarium z0–z15 (dnes sa používa z15). Pre L1–L5 stačia z4–z14.
- **Textúry:** satelitná mozaika (Sentinel-2 cloudless alebo ekvivalent s vhodnou licenciou). Pre začiatok alebo offline variant **syntetická textúra** z tried povrchu (`chunkgen` už triedy počíta: les, pole, mesto, voda), ktorá je štýlovo konzistentná s hrou.
- **Vektory:** shortbread z5–z14 (pobrežia, rieky, hlavné cesty, hranice), Natural Earth pre z0–z4.

### 6.2 Protokol (rozšírenie dnešného `server.py`, protokol 3)

| Správa | Smer | Obsah |
|---|---|---|
| `M_TILES` | hra → server | zoznam `(level, x, y)` + priorita (chyba v px, vzdialenosť) |
| `R_TILE` | server → hra | `level, x, y`, výšky (`int16` alebo `float16` relatívne k minimu dlaždice), textúra (DXT1/BC1 alebo PNG→DXT na klientovi), bbox v ECEF |
| `M_VIEW` | hra → server | pozícia kamery (lat, lon, výška), smer a rýchlosť zoomu (pre predikciu) |
| `M_REANCHOR` | hra → server | nová kotva pre L0 (otvorí novú session v pozadí) |

Dlaždice sa generujú a cachujú na disku rovnako ako dnešné chunky (`cache/levels/<level>/<x>_<y>.gxt`).

### 6.3 Priority a predsťahovanie
- **Fronta podľa chyby v pixeloch** × viditeľnosť × čas od požiadavky. Najprv sa načítajú hrubé úrovne, aby nikde nebola diera; jemné sa dopĺňajú.
- **Predikcia:** pri oddiaľovaní sa sťahujú hrubšie úrovne o 1–2 dopredu, pri približovaní jemnejšie úrovne okolo bodu, kam smeruje stred zoomu.
- Rozpočty: max. N dlaždíc a M MB nahratých na GPU za snímok (napr. 4 dlaždice a 2 MB), zvyšok v ďalších snímkoch.

## 7. Prechody kamery (stavový automat)

| Stav | Výška kamery | Čo beží |
|---|---|---|
| **RTS** | < ~1,5 km | dnešný stav: L0 + L1 na pozadí, plná simulácia, Free Cam (Ctrl+F9) |
| **Regionálny** | 1,5–50 km | L0 ostáva načítané a simulácia beží, no L0 je už len pár pixelov. UI hry sa skryje a zobrazí sa navigačné UI (mierka, súradnice, vyhľadávanie). |
| **Kontinentálny / orbitálny** | > 50 km | L0 sa uspí (simulácia beží alebo je pozastavená podľa režimu, objekty sa nekreslia), kreslí sa L3–L5 a atmosféra. Kamera sa točí okolo zemegule. |
| **Pristátie** | klesanie k bodu X | 1. Hra predpovie cieľ (bod pod stredom obrazovky, keď rýchlosť klesania prekročí prah). 2. Server otvorí session s kotvou pri X (`M_REANCHOR`) a v pozadí sa stiahne a pripraví okno L0. 3. Keď je pripravené a kamera klesne pod ~2 km, okno sa „prepne“: re-anchor, nové L0, `onMapLoaded` bez obrazovky (dnešný mechanizmus aplikácie chunkov, pathfinder z cache). |

Prechody sú riadené výškou s hysterézou. Žiadny z nich nečaká na sieť: ak dáta nie sú, ukáže sa hrubšia úroveň (vždy niečo je, L5 je vždy v pamäti).

**Kamera:** pri oddiaľovaní sa sklon postupne mení z RTS 37,5° na pohľad kolmo dole (nadir) a pri orbite na „orbit around globe“. Rýchlosť zoomu je úmerná výške (logaritmický zoom), takže z ulice na kontinent sa dá prejsť za 2–3 sekundy kolieskom.

## 8. Simulácia a multiplayer

- Simulácia ostáva **iba v L0**. Zoom je klientsky a nedeterministický, nič z L1–L5 nesmie ovplyvniť logiku (rovnaké pravidlo ako dnes pre skybox a radar).
- **Multiplayer:** každý hráč má vlastné okno L0 (alebo zdieľané pri kooperácii). Server udržiava stav sveta (zničené budovy už dnes, neskôr jednotky mimo okna ako „abstraktné“ entity). Vzdialení hráči sa zobrazujú ako markery v L1–L5.
- **Re-anchor v multiplayeri** vyžaduje synchronizovaný bod (všetci klienti s rovnakým L0 musia posunúť počiatok v tom istom logickom snímku). Je to rovnaký problém ako dnešný `shiftWorld` a treba ho vyriešiť skôr.

## 9. Výkon a pamäť (cieľové rozpočty)

| Položka | Rozpočet |
|---|---|
| Vzdialené prstence (L1–L5) | ≤ 2 ms GPU, ≤ 300 draw callov (inštancovanie dlaždíc rovnakého tvaru) |
| Dlaždica | 33×33 vrcholov (2 178 B výšok `int16`) + textúra 256² BC1 (32 KB) |
| Pamäť GPU | ~1 500 dlaždíc ≈ 60 MB |
| Upload na snímok | ≤ 2 MB / ≤ 4 dlaždice |
| Klientska CPU práca | dekódovanie a príprava dlaždíc na sieťovom vlákne (ako dnes `parseChunk`/`prepareChunkLines`) |

## 10. Etapy

1. **Ambientný horizont a hmla** (hotové čiastočne: `skyBelowHorizonLand`). Doplniť výškovú hmlu.
2. **L1 prstenec** (10 m, ~5 km) z výšok, ktoré server už sťahuje (nižší zoom), so syntetickou textúrou z tried povrchu. Dá najväčší vizuálny zisk v RTS a Free Cam pohľade a odstráni koniec sveta.
3. **Pyramída L2–L4** a protokol `M_TILES`, kreslenie podľa hĺbky, morphing.
4. **L5 glóbus a atmosféra**, ECEF, orbitálna kamera.
5. **Re-anchor a pristátie** (asynchrónna príprava nového L0, prepnutie bez obrazovky).
6. **Multiplayer:** synchronizovaný posun počiatku a stav sveta na serveri.

## 11. Riziká a otvorené otázky

- **Licencie dát** (satelitné snímky): pre verejnú distribúciu treba zdroj s voľnou licenciou alebo syntetickú textúru.
- **Presnosť hĺbky** na DX8/DXVK: overiť dostupnosť formátu hĺbky a reverse-Z. Inak zostáva viacpassové rozdelenie hĺbky.
- **Výkon starého renderera** (fixed-function a málo shaderov): morphing a camera-relative výpočty možno budú musieť bežať na CPU pri tvorbe dlaždice.
- **Pamäť 32-bit procesu** (ak build ostane 32-bit): rozpočet GPU a CPU pamäte musí byť prísny.
- **Plynulosť re-anchoru:** pathfinder a footprinty objektov sú dnes najdrahšia časť posunu (~15–30 ms). Pri pristátí sa okno stavia celé, preto ho treba stavať v rozložení na viac snímok alebo ešte pred prepnutím.
