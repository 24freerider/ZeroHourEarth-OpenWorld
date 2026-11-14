@echo off
rem Zero Hour Earth - Open World prototype.
rem Runs the engine built in engine\ from the game\ folder, with its own settings and saves in game\userdata,
rem so the installed GeneralsX and the Steam game are never touched.
setlocal
set "ROOT=%~dp0"
set "CNC_GENERALS_ZH_PATH=C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour\"
set "GX_USERDATA_DIR=%ROOT%game\userdata"
set "ZHE_WORLD_ROOT=%ROOT%"

if not exist "%ROOT%game\generalszh.exe" (
  echo The game is not staged yet. Run tools\build-engine.ps1 and tools\stage-game.ps1 first.
  pause
  exit /b 1
)

rem The world server streams terrain, roads and buildings to the game over a local socket.
if exist "%ROOT%worldserver\.venv\Scripts\pythonw.exe" (
  start "ZH Earth world server" /min "%ROOT%worldserver\.venv\Scripts\python.exe" "%ROOT%worldserver\server.py"
)

cd /d "%ROOT%game"
start "" generalszh.exe %*
