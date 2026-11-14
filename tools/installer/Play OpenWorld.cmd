@echo off
rem Zero Hour Earth - Open World (installed). Runs the mod's engine from game\, with its own settings and saves
rem in game\userdata, so Vanguard and the Steam game are never touched. Paths come from zhe-paths.cmd (Install.ps1).
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%zhe-paths.cmd" call "%ROOT%zhe-paths.cmd"
if not defined CNC_GENERALS_ZH_PATH set "CNC_GENERALS_ZH_PATH=C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour\"
set "GX_USERDATA_DIR=%ROOT%game\userdata"
set "ZHE_WORLD_ROOT=%ROOT%"

if not exist "%ROOT%game\generalszh.exe" (
  echo The game is not installed here. Run Install.cmd.
  pause
  exit /b 1
)

rem The world server streams terrain, roads and buildings to the game over a local socket.
if exist "%ROOT%worldserver\.venv\Scripts\python.exe" (
  start "ZH Earth world server" /min "%ROOT%worldserver\.venv\Scripts\python.exe" "%ROOT%worldserver\server.py"
) else (
  echo World server not set up ^(Python missing^) - World Exploration will not work. Run Install.cmd after installing Python.
  timeout /t 5 >nul
)

cd /d "%ROOT%game"
start "" generalszh.exe %*
