"""Writes the base maps of World Exploration, one per window size:

    gamedata/Maps/WorldExploration/WorldExploration.map            the 7 x 7 window (its old name)
    gamedata/Maps/WorldExploration<N>/WorldExploration<N>.map      every other odd N x N window

The game loads one small map and then replaces its ground with what the world server streams. It holds only what
streaming cannot bring: the size of the window (window_chunks * chunk_cells vertices a side, no border), the palette
of ground textures (in the order of chunkgen.GROUND_CLASSES), lighting, and the players - one human (the explorer)
and the civilians who own the buildings.

Its HeightMapData chunk is version 5 (float samples), which only the open-world engine reads.

    python worldmap.py                 the presets (5, 7, 9, 11, 13); tools/stage-game.ps1 copies gamedata/ into the game
    python worldmap.py --window 15     one size
The world server writes a missing size itself when the game asks for it (ensure()), into gamedata/ and game/.
"""
import argparse
import os
import shutil
import struct
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'generator'))
sys.path.insert(0, HERE)
import mapio                                            # noqa: E402
import chunkgen                                         # noqa: E402
from world import load_config                           # noqa: E402

MAP_NAME = 'WorldExploration'
HEIGHT_FORMAT_F32 = 2
PRESETS = (5, 7, 9, 11, 13, 25)
_lock = threading.Lock()


def map_name(window, zombie=False):
    """The zombie run has its own variant (<name><N>Z): the Zombies side, and a map.ini - the Humvee shop and the
    zombie types (worldserver/zombie/) - that changes nothing outside this map."""
    if zombie:
        return f'{MAP_NAME}{window}Z'
    return MAP_NAME if window == 7 else f'{MAP_NAME}{window}'


def map_path(root, window, base='gamedata', zombie=False):
    name = map_name(window, zombie)
    return os.path.join(root, base, 'Maps', name, name + '.map')


ZOMBIE_INI = ('humvee_shop.ini', 'zombie_tiers.ini')


def zombie_map_ini():
    """The zombie variant's map.ini: the Humvee shop and the zombie types (Zero Hour Earth v0.54.1, zombie/LICENSE.txt)."""
    parts = []
    for fn in ZOMBIE_INI:
        with open(os.path.join(HERE, 'zombie', fn), encoding='utf-8') as f:
            parts.append(f.read().replace('\r\n', '\n'))
    return '\n\n'.join(parts).replace('\n', '\r\n').encode('latin1', 'replace')


def height_chunk_v5(w, h, border, z):
    """HeightMapData version 5: width, height, border, boundaries, sample count, format, scale, offset, samples."""
    n = w * h
    out = struct.pack('<III', w, h, border)
    out += struct.pack('<I', 1) + struct.pack('<II', w - 2 * border, h - 2 * border)
    out += struct.pack('<I', n)
    out += struct.pack('<Iff', HEIGHT_FORMAT_F32, 1.0, 0.0)
    out += struct.pack(f'<{n}f', *([float(z)] * n))
    return out


def key(m, name, t):
    return (m.sid(name) << 8) | t


def sides_chunk(m, zombie=False):
    """SidesList: the neutral side, the explorer (human, USA) and the civilians, one team each; the zombie run also
    the Zombies (a computer player, GLA, red), enemies of the explorer both ways."""
    def side(name, display, faction, human, color=None, enemies=''):
        d = [(key(m, 'playerName', 3), name), (key(m, 'playerIsHuman', 0), 1 if human else 0),
             (key(m, 'playerDisplayName', 4), display.encode('utf-16le')), (key(m, 'playerFaction', 3), faction),
             (key(m, 'playerEnemies', 3), enemies), (key(m, 'playerAllies', 3), '')]
        if color is not None:
            d.append((key(m, 'playerColor', 1), color))
        return d

    sides = [side('', 'Neutral', '', False),
             side('Explorer', 'Explorer', 'FactionAmerica', True, -16776961 + 0x0000FF00 * 0,
                  enemies='Zombies' if zombie else ''),
             side('PlyrCivilian', 'Civilians', 'FactionCivilian', False)]
    teams = [[(key(m, 'teamName', 3), 'team'), (key(m, 'teamOwner', 3), ''), (key(m, 'teamIsSingleton', 0), 1)],
             [(key(m, 'teamName', 3), 'teamExplorer'), (key(m, 'teamOwner', 3), 'Explorer'), (key(m, 'teamIsSingleton', 0), 1)],
             [(key(m, 'teamName', 3), 'teamPlyrCivilian'), (key(m, 'teamOwner', 3), 'PlyrCivilian'), (key(m, 'teamIsSingleton', 0), 1)]]
    if zombie:
        sides.append(side('Zombies', 'Zombies', 'FactionGLA', False, -65536, enemies='Explorer'))	# 0xFFFF0000: red
        teams.append([(key(m, 'teamName', 3), 'teamZombies'), (key(m, 'teamOwner', 3), 'Zombies'),
                      (key(m, 'teamIsSingleton', 0), 1)])
    out = [struct.pack('<I', len(sides))]
    for d in sides:
        out.append(mapio.write_dict(d) + struct.pack('<I', 0))            # no build list
    out.append(struct.pack('<I', len(teams)))
    for d in teams:
        out.append(mapio.write_dict(d))
    # one empty script list per side
    lists = b''.join(mapio._chunk(m, 'ScriptList', 1, b'') for _ in sides)
    out.append(mapio._chunk(m, 'PlayerScriptsList', 1, lists))
    return b''.join(out)


def build(root, window=None, chunk_cells=None, log=print, zombie=False):
    """Write the base map of a window x window window into gamedata/ (zombie: the zombie run's variant, with its
    map.ini); returns its path."""
    cfg = load_config(root)
    window = int(window or cfg['window_chunks'])
    if window < 3 or window % 2 == 0:
        raise ValueError(f'the window must be odd and at least 3, not {window}')
    size = window * int(chunk_cells or cfg['chunk_cells'])
    m = mapio.MapFile.read(os.path.join(HERE, '..', 'generator', 'template.map'))
    tail = mapio.BlendTail.parse(m.blend.tail, m.height.w, m.height.h)
    names = [c[4] for c in tail.classes]
    if names != chunkgen.GROUND_CLASSES[:len(names)]:
        raise SystemExit(f'template.map palette {names} does not start chunkgen.GROUND_CLASSES')
    # the classes past the template's palette (the map generator does the same): 128 px textures, 2 x 2 tiles
    for extra in chunkgen.GROUND_CLASSES[len(names):]:
        nxt = max(c[0] + c[1] for c in tail.classes)
        tail.classes.append((nxt, 4, 2, 0, extra))
    tail.num_bitmap_tiles = max(c[0] + c[1] for c in tail.classes)
    names = [c[4] for c in tail.classes]

    # ground: every cell grass (class 0), no blends, no cliffs
    xs = [x for _ in range(size) for x in range(size)]
    ys = [y for y in range(size) for _ in range(size)]
    cls0 = tail.classes[0]
    tiles = [int(mapio.tile_index(cls0, x, y)) for x, y in zip(xs, ys)]
    zeros = [0] * (size * size)
    tail.cliff_state = bytes(((size + 7) // 8) * size)
    tail.blends, tail.cliffs = [], []
    m.blend = mapio.BlendTile(size, size, tiles, list(zeros), list(zeros), list(zeros), tail.dump())
    # version 7+ of BlendTileData stores the flip state before the tail; the template is version 8
    m.height = None                                    # written by hand below (version 5)

    # objects: only the camera start, in the middle of the window
    proto = next(o for o in m.objects if o.name == '*Waypoints/Waypoint')
    centre = size * 10.0 / 2
    props = []
    for k, v in proto.props:
        nm = next(n for n, i in m.strings if i == k >> 8)
        if nm in ('waypointPathLabel1', 'waypointPathBiDirectional'):
            continue
        if nm == 'waypointName' or nm == 'objectName' or nm == 'uniqueID':
            v = 'InitialCameraPosition'
        if nm == 'waypointID':
            v = 1
        props.append((k, v))
    m.objects = [mapio.MapObject(proto.ver, centre, centre, 0.0, 0.0, proto.flags, proto.name, props)]

    sides = sides_chunk(m, zombie)
    for c in m.chunks:
        if c[0] == 'HeightMapData':
            c[1] = 5
            c[2] = height_chunk_v5(size, size, 0, chunkgen.ZBASE)
        elif c[0] == 'SidesList':
            c[2] = sides
    path = map_path(root, window, zombie=zombie)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    m.write(path)
    if zombie:
        with open(os.path.join(os.path.dirname(path), 'map.ini'), 'wb') as f:
            f.write(zombie_map_ini())
    log(f'{path}: {window} x {window} chunks, {size} x {size} vertices, ground z {chunkgen.ZBASE:.0f}, '
        f'{len(names)} ground textures, {os.path.getsize(path) / 1024:.0f} KB')
    return path


def ensure(root, window, chunk_cells=None, log=print, zombie=False):
    """The base map of this window exists in gamedata/ and in the staged game/ folder: write / copy what is missing.
    The zombie variant's map.ini is written again whenever worldserver/zombie/ is newer. Returns a description of
    what was done, '' when both were there."""
    with _lock:
        done = []
        src = map_path(root, window, zombie=zombie)
        ini = os.path.join(os.path.dirname(src), 'map.ini')
        stale = zombie and os.path.isfile(src) and (not os.path.isfile(ini) or os.path.getmtime(ini) < max(
            os.path.getmtime(os.path.join(HERE, 'zombie', fn)) for fn in ZOMBIE_INI))
        if not os.path.isfile(src) or stale:
            build(root, window, chunk_cells, log, zombie)
            done.append('generated')
        game = os.path.join(root, 'game')
        dst = map_path(root, window, 'game', zombie)
        if os.path.isdir(game) and (not os.path.isfile(dst) or 'generated' in done):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            if zombie:
                shutil.copyfile(os.path.join(os.path.dirname(src), 'map.ini'), os.path.join(os.path.dirname(dst), 'map.ini'))
            done.append(f'copied to {dst}')
        return ', '.join(done)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--window', type=int, default=0, help='one odd window size (default: the presets)')
    ap.add_argument('--zombie', action='store_true', help='the zombie run\'s variant')
    a = ap.parse_args()
    root = os.path.normpath(os.path.join(HERE, '..'))
    for w in ([a.window] if a.window else PRESETS):
        build(root, w, zombie=a.zombie)
