"""Rebuild every World Exploration base map in gamedata/Maps (a new ground class) and copy it to game/Maps."""
import os, re, shutil, sys
root = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.join(root, 'worldserver'))
import worldmap  # noqa: E402

maps = os.path.join(root, 'gamedata', 'Maps')
for name in sorted(os.listdir(maps)):
    m = re.fullmatch(r'WorldExploration(\d*)(Z?)', name)
    if not m:
        continue
    window = int(m.group(1)) if m.group(1) else 7
    zombie = m.group(2) == 'Z'
    src = worldmap.build(root, window, log=lambda s: None, zombie=zombie)
    dst = worldmap.map_path(root, window, 'game', zombie)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    if zombie:
        shutil.copyfile(os.path.join(os.path.dirname(src), 'map.ini'), os.path.join(os.path.dirname(dst), 'map.ini'))
    print(name, 'rebuilt and copied')
