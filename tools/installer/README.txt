ZERO HOUR EARTH - OPEN WORLD
============================
An open world for Command & Conquer Generals Zero Hour on the Generals Vanguard (GeneralsX) engine:
real terrain, roads, rivers and buildings streamed around you as you drive, plus a zombie survival mode.

SLOVENSKY
---------
Potrebuješ:
  1. Command & Conquer Generals Zero Hour (Steam, EA app alebo CD).
  2. Generals Vanguard (GeneralsX), nainštalovaný jeho vlastným inštalátorom.
     Najlepšie vo verzii, na ktorej je tento balík postavený (pozri VERSION.txt).
  3. Python 3.10 alebo novší (python.org, alebo: winget install Python.Python.3.12).
     Bez Pythonu hra beží, ale World Exploration nie, lebo ho potrebuje svetový server.
  4. Internet pri prvej inštalácii (pip) a pri hraní (výškové dáta a mapy sveta sa sťahujú a ukladajú do cache).
  5. Asi 2,5 GB voľného miesta.

Inštalácia: rozbaľ zip a dvakrát klikni na Install.cmd.
  Inštalátor sám nájde Vanguard aj Zero Hour. Ak ich nenájde, napíše, aký parameter mu treba dať:
    Install.cmd -VanguardDir "C:\...\GeneralsVanguard" -ZHDir "D:\Steam\...\Command & Conquer Generals - Zero Hour"
  Na ploche vytvorí priečinok "Zero Hour Earth - Open World" (iné miesto: -InstallDir "D:\Hry\ZHE").
  V priečinku je úplná kópia Vanguardu s modom navrch, takže aktualizácia Vanguardu mod nepokazí.
  Pôvodný Vanguard, hra ani tvoj profil v Dokumentoch sa nemenia. Mod má vlastné nastavenia v game\userdata.

Hranie: dvakrát klikni na "Play OpenWorld.cmd" v priečinku modu (alebo na zástupcu na ploche).
  V hlavnom menu potom WORLD EXPLORATION.

Nová verzia modu: rozbaľ nový zip a spusti jeho Install.cmd. Prepíše sa len to, čo sa zmenilo.

Odinštalovanie: spusti Uninstall.ps1 v priečinku modu (alebo priečinok jednoducho zmaž).

ENGLISH
-------
Needs: Zero Hour (Steam / EA app / CD), Generals Vanguard (GeneralsX) installed with its own installer,
Python 3.10+ for the world server, the internet (pip; terrain and map data are downloaded and cached), ~2.5 GB.

Install: unpack, double-click Install.cmd. It finds Vanguard and Zero Hour itself (or pass -VanguardDir / -ZHDir).
It creates "Zero Hour Earth - Open World" on the desktop: a full copy of Vanguard with the mod on top
(elsewhere: -InstallDir "D:\Games\ZHE"). Vanguard, the game and your profile are untouched, and a Vanguard
update does not break the mod.
Play: "Play OpenWorld.cmd" in that folder (or the desktop shortcut), then WORLD EXPLORATION in the main menu.
New mod version: unpack it and run its Install.cmd. Uninstall: Uninstall.ps1 in the folder, or delete the folder.
