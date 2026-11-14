"""Chunk generator: turns elevation tiles and map data into game chunks.

A block of chunks is generated in one pass over a region = block + margin. Every step is a local operation with
a bounded reach (see MARGIN), and everything random is a hash of world coordinates, so a chunk comes out the same
no matter which block it was generated in. That is what makes the seams invisible: neighbouring chunks are cut
from one continuous world, never generated towards each other.

Heights are true scale: metres * units-per-metre, as float32. Nothing is squeezed into 0..255; the engine stores
them in 16 bits (see docs/DESIGN.md).

Chunk payload (little endian), all x / y relative to the chunk's south-west corner, in world units:

    'GXCK' u16 version u16 flags  i32 cx i32 cy  u16 n u16 0
    u32 count, strings (u16 length + utf-8)                     object templates and road types
    f32 heights[n*n]                                            row by row, south to north
    u8  ground[n*n]                                             ground class per cell (GROUND_CLASSES)
    u32 count, roads:   u16 type u8 rank u8 flags f32 width u32 points, points * (f32 lx ly lz rx ry rz s)
    u32 count, objects: u16 template u8 kind u8 flags f32 x y z angle scale u64 id
    u32 count, water:   f32 z u8 flags u8[3] u32 points, points * (f32 x y)
"""
import math
import os
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'generator'))
import geo                                              # noqa: E402
import gconst                                           # noqa: E402
import worlddata                                        # noqa: E402

from worldgrid import CELL_UNITS

VERSION = 1
MARGIN = 72                 # cells of context around a block; must exceed the reach of every step below (a bridge's
                            # approach embankment: up to ~600 units)
PAD_REACH = 30              # a building levels the ground at most this many cells from its centre
ZBASE = 4000.0              # world z of the session's reference elevation: room below for valleys and seas
GEN_VERSION = 23  # bump when the output for the same input changes (invalidates cached chunks)

# ground classes, in the order of the texture palette of the base map (worldmap.py writes the same list)
GROUND_CLASSES = ['GrassMediumType1', 'GrassMediumType11', 'DirtMediumType1', 'SandMediumType1', 'RocksMediumType1',
                  'CliffMediumType1', 'AsphaltType1', 'ConcreteType1', 'CobblestoneType1', 'WoodType1',
                  'GrassMediumType38a', 'Cobble01',
                  'SandType1',             # past the template's palette: worldmap.py adds it (fields: light brown soil)
                  'DirtMediumType2',       # likewise: never in a chunk - the client's nuke crater (ash, the middle)
                  'GrassMediumType23brown']  # likewise: the crater's outer zone (scorched grass)
G_GRASS, G_FOREST, G_DIRT, G_SAND, G_ROCK, G_CLIFF, G_ASPHALT, G_CONCRETE, G_COBBLE, G_WOOD, G_FIELD, G_COBBLE2, \
    G_FARM, G_BURNT, G_SCORCHED = range(15)

LAND_GROUND = {
    'forest': G_FOREST, 'wood': G_FOREST,
    'farmland': G_FARM, 'farmyard': G_DIRT, 'orchard': G_FIELD, 'vineyard': G_FIELD, 'allotments': G_FIELD,
    'greenhouse_horticulture': G_FIELD, 'plant_nursery': G_FIELD,
    'industrial': G_CONCRETE, 'commercial': G_CONCRETE, 'retail': G_CONCRETE, 'garages': G_CONCRETE,
    'railway': G_DIRT, 'quarry': G_ROCK, 'landfill': G_DIRT, 'brownfield': G_DIRT, 'greenfield': G_DIRT,
    'construction': G_DIRT, 'parking': G_ASPHALT, 'bicycle_parking': G_ASPHALT, 'square': G_COBBLE,
    'sand': G_SAND, 'beach': G_SAND, 'dune': G_SAND, 'bare_rock': G_ROCK, 'scree': G_ROCK, 'shingle': G_ROCK,
}
# later entries are painted over earlier ones
LAND_ORDER = ['farmland', 'orchard', 'vineyard', 'allotments', 'greenhouse_horticulture', 'plant_nursery',
              'greenfield', 'brownfield', 'landfill', 'construction', 'forest', 'wood', 'sand', 'beach', 'dune',
              'bare_rock', 'scree', 'shingle', 'quarry', 'railway', 'farmyard', 'industrial', 'commercial',
              'retail', 'garages', 'parking', 'bicycle_parking', 'square']
TREE_LAND = {'forest': 1.0, 'wood': 1.0, 'orchard': 0.55, 'park': 0.28, 'cemetery': 0.22, 'grave_yard': 0.22,
             'garden': 0.3, 'scrub': 0.5, 'heath': 0.25, 'village_green': 0.14, 'recreation_ground': 0.08,
             # town green: yards and gardens between the houses, verges, allotments; lone trees on meadows
             'residential': 0.13, 'allotments': 0.15, 'grass': 0.07, 'meadow': 0.025, 'playground': 0.15}
PROP_LAND = ('cemetery', 'grave_yard', 'playground', 'park', 'parking')     # land kinds that get props
LAND_CONTEXT = {'industrial': 'industrial', 'railway': 'industrial', 'quarry': 'industrial',
                'commercial': 'shop', 'retail': 'shop', 'farmyard': 'farm', 'garages': 'shed',
                'allotments': 'shed', 'cemetery': 'civic', 'school': 'civic', 'university': 'civic',
                'college': 'civic', 'hospital': 'hospital', 'residential': 'res'}

# the ground under a road, by gconst.ROAD_SURFACE: paved roads write asphalt, unpaved ones keep dirt
ROAD_GROUND = {'asphalt': G_ASPHALT, 'conc': G_CONCRETE, 'cobble': G_COBBLE, 'dirt': G_DIRT}

# the Roads.ini bridge for a road type where World Exploration differs from the generator's maps (gconst.BRIDGE): a
# paved footpath or a pedestrian street crosses on a concrete footbridge, a dirt path on a wooden one; the game sizes
# either to the real deck's width
STREAM_BRIDGE = {'Sidewalk': 'ConcreteOneLane', 'Sidewalk2': 'ConcreteOneLane', 'Sidewalk3': 'ConcreteOneLane',
                 'DirtPath1': 'WoodenSectional'}

KIND_BUILDING, KIND_TREE, KIND_PROP = 0, 1, 2
ROAD_FLAG_BRIDGE = 1
WATER_FLAG_RIVER = 1
WATER_FLAG_FORD = 2         # drawn, but units cross it (a brook): not water for the game's logic

# footprints (x, y) in world units for when the game's INI archives cannot be read
FALLBACK_MODELS = {'GermanTownHouse1': (66, 52), 'GermanTownHouse2': (60, 50), 'FarmHouse': (70, 60), 'Chalet': (60, 50),
                   'AsianApartment01': (140, 70), 'SupplyWarehouse': (160, 110), 'BarnShed': (50, 40),
                   'Shack': (30, 26), 'GermanTownHall': (110, 90)}

# props of the living town (the map generator's g_nature uses the same objects); a set whose objects the game lacks
# is left out
PROP_SETS = {'graves': ['Tombstones01', 'Tombs'], 'crypt': ['Crypt01'], 'play': ['PlaygroundSet', 'Swing'],
             'bench': ['ParkBench'], 'fountain': ['Fountain1', 'Fountain2'], 'statue': ['StatueAngel'],
             'cars': ['CarSedan01', 'CarCompact', 'CarEuroVan1', 'CarEuroVan2', 'CarBug1', 'CarBug2', 'CarBug3',
                      'CarSwatchEuro', 'CarLuxury01', 'CarSport01', 'CarSUV01'],
             'vans': ['TruckWork', 'CarEuroVan1', 'CarEuroVan2'],
             'goal': ['SoccerStadiumGoal'], 'rr_gate': ['CheckPointCrossingGuard'], 'rr_gantry': ['RRCrossingGate'],
             'rr_sign': ['RRSign'],
             'wagons': ['TrainCoalOrnament', 'TrainTankOrnament'],
             'lamps': ['StreetLamp', 'StreetLamp02'], 'bins': ['TrashCan01', 'TrashCanA', 'TrashCanB'],
             'bikes': ['BikeRack']}
PROP_KERB_ROADS = {'residential', 'living_street', 'tertiary', 'secondary', 'street_marked', 'street_unmarked',
                   'unclassified', 'service'}
LAMP_ROADS = PROP_KERB_ROADS - {'service'}
# the width the game draws a road type at (Roads.ini RoadWidth x RoadWidthInTexture), units
DRAWN_WIDTH = {'TwoLane': 31.5, 'TwoLaneDarkDotted': 31.5, 'Cobblestone': 27.0, 'Sidewalk': 7.2, 'Sidewalk2': 14.4,
               'Sidewalk3': 19.8, 'Sidewalk4': 25.2, 'DirtRoad2': 31.3, 'DirtRoadTracks': 49.4, 'DirtPath1': 9.5,
               'GravelRoad': 31.3, 'TrainTrack': 33.2}
# streets that get pavements in town, and the town for that
SIDEWALK_STREETS = {'primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'street_marked',
                    'street_unmarked', 'street_concrete', 'street_cobble'}
SIDEWALK_LAND = ('residential', 'retail', 'commercial', 'school', 'university', 'college', 'hospital')
# land use of a town: no field there, whatever the satellites call a garden
TOWN_LAND = ('residential', 'retail', 'commercial', 'industrial', 'school', 'university', 'college', 'hospital',
             'cemetery', 'grave_yard', 'allotments', 'garages', 'railway', 'parking', 'park', 'playground')
PARK_HIDDEN = {'service', 'living_street', 'pedestrian', 'street_unmarked', 'track_paved', 'footway', 'path_paved'}
PITCH_HIDDEN = {'path', 'footway', 'path_paved', 'track', 'track_rutted', 'track_gravel'}   # not drawn on a pitch
# a house's front fence, by share: (template, length of one piece in units)
FENCES = [('PicketFence01', 32.0, 40), ('Hedge1', 18.0, 35), ('WoodWall01', 30.0, 15), ('BrickWall01', 30.0, 10)]

# The game's own road markings (Roads.ini; the road buffer draws a type at its INI width, whatever the payload says).
# ParkingLines is a row of bays (measured on TRParkingLines): a divider every PARK_BAY units, the first PARK_FIRST after
# the line's start, the back line PARK_BACK left of the line, the open end of the bays PARK_OPEN right of it.
PARK_TYPE, PARK_WIDTH = 'ParkingLines', 33.25
PARK_BAY, PARK_FIRST, PARK_BACK, PARK_OPEN = 17.54, 0.76, 15.1, 14.1
LINE_TYPE, LINE_WIDTH = 'SingleWhiteLine', 18.0           # a white line ~0.5 m wide in the middle of its ribbon
MARK_RANK = 250
WAGON_PITCH = 58.0                                      # a 14 m wagon and its couplings
RAIL_HALF = 7.2
GATE_ROADS = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'street_marked',
              'street_unmarked', 'living_street', 'service', 'track_paved', 'street_concrete', 'street_cobble'}
SIGN_ROADS = GATE_ROADS | {'track_gravel', 'track', 'track_rutted'}
# fences of grounds: land kind -> (model, piece length, the model starts at its pivot (True) or is centred on it, the
# biggest ground fenced in hectares (0 = any)); ChainLinkFence01 runs from its pivot along +x, the walls are centred
_CHAIN = ('ChainLinkFence01', 28.0, True, 0)
FENCE_LAND = {'school': _CHAIN, 'kindergarten': _CHAIN, 'university': _CHAIN, 'college': _CHAIN, 'hospital': _CHAIN,
              'sports_centre': _CHAIN, 'stadium': _CHAIN, 'allotments': _CHAIN, 'prison': ('ConcreteWall01', 30.0, False, 0),
              'wastewater_plant': _CHAIN, 'water_works': _CHAIN, 'industrial': ('ChainLinkFence01', 28.0, True, 25),
              'cemetery': ('BrickWall01', 30.0, False, 0), 'grave_yard': ('BrickWall01', 30.0, False, 0)}
# petrol stations of every region, picked by size (fallback footprints in units, measured on their W3D models)
GAS_STATIONS = [('StanGasStation', (66, 33)), ('GasStation', (64, 63)), ('GasStation03', (95, 63)),
                ('AmericanGasStation', (101, 88))]
GANTRY_ROADS = {'motorway', 'trunk', 'primary', 'secondary'}     # the big signal gantry: main roads of a big city


# ------------------------------------------------------------------------------------------------- helpers
def hash2(ix, iy, salt=0):
    """Deterministic 32-bit hash of integer world coordinates (numpy arrays or ints)."""
    if not isinstance(ix, np.ndarray):
        m = (1 << 64) - 1                                  # the same arithmetic as the int64 arrays below, by hand
        h = ((int(ix) * 73856093) ^ (int(iy) * 19349663) ^ (salt * 83492791)) & m
        h = ((h ^ (h >> 13)) * 1274126177) & m
        return (h ^ (h >> 16)) & 0xFFFFFFFF
    with np.errstate(over='ignore'):
        h = (ix.astype(np.int64) * 73856093) ^ (np.asarray(iy, np.int64) * 19349663) ^ (salt * 83492791)
        h = (h ^ (h >> 13)) * 1274126177
        h = h ^ (h >> 16)
    return (h & 0xFFFFFFFF).astype(np.int64)


def bilinear(field, fx, fy):
    """field[row = y, col = x] sampled at fractional indices (clamped at the border)."""
    h, w = field.shape
    fx = np.clip(fx, 0, w - 1.001)
    fy = np.clip(fy, 0, h - 1.001)
    ix, iy = fx.astype(np.int64), fy.astype(np.int64)
    ax, ay = fx - ix, fy - iy
    return (field[iy, ix] * (1 - ax) * (1 - ay) + field[iy, ix + 1] * ax * (1 - ay) +
            field[iy + 1, ix] * (1 - ax) * ay + field[iy + 1, ix + 1] * ax * ay)


def value_noise(gi, gj, period, salt):
    """Smooth noise in -0.5..0.5 over integer world cells: hashed lattice every `period` cells, smoothstep-blended."""
    x, y = gi / period, gj / period
    i0, j0 = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = smoothstep(x - i0), smoothstep(y - j0)

    def h(i, j):
        return hash2(i, j, salt) / 4294967295.0 - 0.5
    a, b = h(i0, j0), h(i0 + 1, j0)
    c, d = h(i0, j0 + 1), h(i0 + 1, j0 + 1)
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def _length(p):
    p = np.asarray(p, float)
    return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) > 1 else 0.0


def _densify(p, step):
    """The polyline with a point at least every `step` units (its own points kept)."""
    p = np.asarray(p, float)
    out = [p[0]]
    for a, b in zip(p[:-1], p[1:]):
        k = max(1, int(math.ceil(math.hypot(*(b - a)) / step)))
        out += [a + (b - a) * (t / k) for t in range(1, k + 1)]
    return np.asarray(out)


def _project(line, q):
    """For each point of q: (distance to the polyline `line`, signed offset to its left, the segment's direction)."""
    a, b = line[:-1], line[1:]
    d = b - a
    l2 = np.maximum((d ** 2).sum(1), 1e-9)
    t = np.clip(((q[:, None, 0] - a[None, :, 0]) * d[None, :, 0] + (q[:, None, 1] - a[None, :, 1]) * d[None, :, 1]) / l2[None, :], 0, 1)
    px = a[None, :, 0] + t * d[None, :, 0]
    py = a[None, :, 1] + t * d[None, :, 1]
    dist = np.hypot(q[:, None, 0] - px, q[:, None, 1] - py)
    k = np.argmin(dist, axis=1)
    rows = np.arange(len(q))
    dk = d[k] / np.sqrt(l2[k])[:, None]
    side = dk[:, 0] * (q[:, 1] - py[rows, k]) - dk[:, 1] * (q[:, 0] - px[rows, k])
    return dist[rows, k], side, dk


def _side_by_side(p, q, gap):
    """Two runs that are one structure: parallel, within `gap` units of each other along 70 % of the shorter."""
    if _length(p) < _length(q):
        p, q = q, p
    if len(q) < 2:
        return False
    dist, _, dk = _project(p, q)
    dq = np.diff(q, axis=0)
    dq = np.vstack([dq, dq[-1:]])
    dq /= np.maximum(np.hypot(dq[:, 0], dq[:, 1]), 1e-9)[:, None]
    parallel = np.abs((dq * dk).sum(1)) > 0.9
    return float(((dist < gap) & parallel).mean()) >= 0.7


def _mean_offset(line, q):
    """How far left of `line` the run q lies, on average (its middle part)."""
    _, side, _ = _project(line, q)
    n = len(side)
    return float(np.median(side[n // 5: max(n - n // 5, n // 5 + 1)]))


def _offset_line(line, off):
    """The polyline moved `off` units to its left."""
    if abs(off) < 1e-6:
        return line
    d = np.diff(line, axis=0)
    n = np.stack([-d[:, 1], d[:, 0]], 1) / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    vn = np.empty_like(line)
    vn[0], vn[-1] = n[0], n[-1]
    vn[1:-1] = n[:-1] + n[1:]
    vn /= np.maximum(np.hypot(vn[:, 0], vn[:, 1]), 1e-9)[:, None]
    return line + vn * off


def _crossings(p, q):
    """Points where polyline p crosses polyline q, not at (or near) a vertex both share - a junction is not a crossing."""
    out = []
    for a, b in zip(p[:-1], p[1:]):
        rx, ry = b[0] - a[0], b[1] - a[1]
        lr = math.hypot(rx, ry)
        if lr < 1e-6:
            continue
        for c, d in zip(q[:-1], q[1:]):
            sx, sy = d[0] - c[0], d[1] - c[1]
            ls = math.hypot(sx, sy)
            den = rx * sy - ry * sx
            if ls < 1e-6 or abs(den) < 0.3 * lr * ls:
                continue
            t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / den
            u = ((c[0] - a[0]) * ry - (c[1] - a[1]) * rx) / den
            if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
                continue
            X = (a[0] + t * rx, a[1] + t * ry)
            near = lambda e, f: min(math.hypot(X[0] - e[0], X[1] - e[1]), math.hypot(X[0] - f[0], X[1] - f[1])) < 4.0
            if near(a, b) and near(c, d):
                continue
            out.append(X)
    return out


def _frame(a):
    """An area's own axes: (cx, cy, L, W, angle) of its outline's smallest rectangle, L >= W, the angle in 5 degrees."""
    mr = geo.min_rect([tuple(p) for p in a.rings[0].tolist()])
    if mr is None:
        return None
    cx, cy, L, W, th = mr
    if W > L:
        L, W, th = W, L, th + math.pi / 2
    return cx, cy, L, W, round(th / math.radians(5)) * math.radians(5)


def _at(p, s_cum, s):
    """Point and unit direction of polyline p at arc length s."""
    k = int(np.clip(np.searchsorted(s_cum, s) - 1, 0, len(p) - 2))
    d = p[k + 1] - p[k]
    ln = max(float(np.hypot(*d)), 1e-9)
    t = (s - s_cum[k]) / ln
    return p[k] + d * t, d / ln


def _straight_runs(p, tol=math.radians(6.0), min_len=0.0):
    """The polyline cut into nearly straight runs: [(start point, end point, arc length at the start)]; a run turns
    at most `tol` off its first piece."""
    p = np.asarray(p, float)
    seg = np.hypot(*np.diff(p, axis=0).T)
    s_cum = np.concatenate([[0.0], np.cumsum(seg)])
    out, i = [], 0
    while i < len(p) - 1:
        d0 = p[i + 1] - p[i]
        a0 = math.atan2(d0[1], d0[0])
        j = i + 1
        while j < len(p) - 1:
            d = p[j + 1] - p[j]
            if abs((math.atan2(d[1], d[0]) - a0 + math.pi) % math.tau - math.pi) > tol:
                break
            j += 1
        if s_cum[j] - s_cum[i] >= min_len:
            out.append((p[i], p[j], float(s_cum[i])))
        i = j
    return out


def _block_rects(blocks):
    """Cover the True cells of a small boolean grid with few rectangles (greedy: a row run, grown down while the
    rows under it are True across it). -> [(x0, y0, x1, y1)] inclusive."""
    b = blocks.copy()
    h, w = b.shape
    out = []
    for y in range(h):
        x = 0
        while x < w:
            if not b[y, x]:
                x += 1
                continue
            x1 = x
            while x1 + 1 < w and b[y, x1 + 1]:
                x1 += 1
            y1 = y
            while y1 + 1 < h and b[y1 + 1, x:x1 + 1].all():
                y1 += 1
            b[y:y1 + 1, x:x1 + 1] = False
            out.append((x, y, x1, y1))
            x = x1 + 1
    return out


def smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


class _Block:
    """Sections of a block of flats mapped one by one, joined (see _Region._merge_sections): it stands in for a
    worlddata.Building."""

    def __init__(self, ring, height_m, area, dense):
        self.ring, self.height_m, self.area, self.dense, self.block = ring, height_m, area, dense, True
        self.bbox = (ring[:, 0].min(), ring[:, 1].min(), ring[:, 0].max(), ring[:, 1].max())
        self.cx, self.cy = float(ring[:, 0].mean()), float(ring[:, 1].mean())


class Models:
    """Building models of the installed game with their footprints (read from the INI archives)."""

    def __init__(self, sizes=None, objects=None):
        self.sizes = dict(sizes or FALLBACK_MODELS)
        self.objects = set(objects) if objects else None

    @staticmethod
    def load(game_dirs, log=print):
        try:
            import catalog
            bigs = []
            for d in game_dirs:
                if not d or not os.path.isdir(d):
                    continue
                for fn in sorted(os.listdir(d)):
                    if fn.lower().endswith('.big') and 'ini' in fn.lower():
                        bigs.append(os.path.join(d, fn))
            cat = catalog.Catalog.load(bigs)
            if cat.buildings:
                log(f'game data: {len(cat.buildings)} building models, {len(cat.objects)} objects from {len(bigs)} archives')
                return Models({k: (v[0], v[1]) for k, v in cat.buildings.items()}, cat.objects)
        except Exception as ex:
            log(f'game data could not be read ({ex!r}); using the built-in model list')
        return Models()

    def has(self, name):
        return name in self.sizes and (self.objects is None or name in self.objects)

    def has_object(self, name):
        return self.objects is None or name in self.objects


# ------------------------------------------------------------------------------------------------- generator
class ChunkGenerator:
    def __init__(self, grid, dem, data, models, elev0_m, options=None, log=print):
        self.grid, self.dem, self.data, self.models, self.log = grid, dem, data, models, log
        self.elev0 = float(elev0_m)
        self.opt = dict(tree_spacing=34.0, trees=True, buildings=True, smooth=1)
        self.opt.update(options or {})
        self.demz = dem.zoom_for(grid.lat0, grid.mpc)
        self.style = gconst.region_style(grid.lat0, grid.lon0)
        st = gconst.STYLES[self.style]
        self.style_models = {cls: [m for m in names if models.has(m)] for cls, names in st.items()}
        # the church model (a town hall with a tower: the game has no European church) only where there is a real
        # church - a town hall, a school or a hospital must not look like one
        church = set(st.get('church', ()))
        for cls in self.style_models:
            if cls != 'church':
                self.style_models[cls] = [m for m in self.style_models[cls] if m not in church]
        # a dense big city: every region's models of a class (and the city blocks for the ordinary buildings), never
        # a church model outside a church
        churches = {m for s in gconst.STYLES.values() for m in s.get('church', ())}
        self.dense_models = {}
        for cls in {c for s in gconst.STYLES.values() for c in s}:
            names = [m for s in gconst.STYLES.values() for m in s.get(cls, ())]
            if cls in ('house', 'shop', 'civic', 'pub'):
                names += gconst.DENSE_MID
            if cls != 'church':
                names = [m for m in names if m not in churches]
            self.dense_models[cls] = [m for m in dict.fromkeys(names) if models.has(m)]
        self.props = {k: [m for m in names if models.has_object(m)] for k, names in PROP_SETS.items()}
        self.trees_broad = [t for t in gconst.BROADLEAF if models.has_object(t)] or ['TreeOak01']
        self.trees_conifer = [t for t in gconst.CONIFER if models.has_object(t)] or self.trees_broad
        self.bushes = [t for t in gconst.BUSHES if models.has_object(t)] or self.trees_broad

    # ---- world z of an elevation in metres
    def z_of(self, metres):
        return (metres - self.elev0) * self.grid.upm + ZBASE

    # ---------------------------------------------------------------------------------------------- a block
    def generate(self, cx0, cy0, nx=1, ny=1):
        """-> {(cx, cy): payload bytes} for the block of nx * ny chunks whose south-west chunk is (cx0, cy0)."""
        g, n = self.grid, self.grid.n
        gi0, gj0 = cx0 * n - MARGIN, cy0 * n - MARGIN          # first vertex of the region
        rw, rh = nx * n + 2 * MARGIN + 1, ny * n + 2 * MARGIN + 1
        ctx = _Region(self, gi0, gj0, rw, rh)
        ctx.build()
        out = {}
        for j in range(ny):
            for i in range(nx):
                out[(cx0 + i, cy0 + j)] = ctx.payload(cx0 + i, cy0 + j)
        return out


class _Region:
    def __init__(self, gen, gi0, gj0, rw, rh):
        self.gen, self.grid = gen, gen.grid
        self.gi0, self.gj0, self.rw, self.rh = gi0, gj0, rw, rh
        self.x0, self.y0 = gi0 * CELL_UNITS, gj0 * CELL_UNITS
        self.x1, self.y1 = (gi0 + rw - 1) * CELL_UNITS, (gj0 + rh - 1) * CELL_UNITS

    # world units -> fractional vertex index of the region
    def fx(self, x): return np.asarray(x, float) / CELL_UNITS - self.gi0
    def fy(self, y): return np.asarray(y, float) / CELL_UNITS - self.gj0

    def raster(self, rings_list, vertices):
        """Mask (rh, rw) of the vertices (or of the cells) covered by polygons given as lists of rings."""
        img = Image.new('L', (self.rw, self.rh), 0)
        d = ImageDraw.Draw(img)
        s = 0.5 if vertices else 0.0
        for rings in rings_list:
            for k, r in enumerate(rings):
                if len(r) < 3:
                    continue
                pts = list(zip((self.fx(r[:, 0]) + s).tolist(), (self.fy(r[:, 1]) + s).tolist()))
                d.polygon(pts, fill=1 if k == 0 else 0)
        return np.asarray(img, bool)

    # ------------------------------------------------------------------------------------------ the pipeline
    def build(self):
        gen, g = self.gen, self.grid
        vx = (self.gi0 + np.arange(self.rw)) * CELL_UNITS
        vy = (self.gj0 + np.arange(self.rh)) * CELL_UNITS
        X, Y = np.meshgrid(vx, vy)
        lat, lon = g.from_units(X, Y)
        E = np.maximum(gen.dem.sample(lat, lon, gen.demz).astype(np.float64), -430.0)
        self.E0 = geo.blur(E, int(gen.opt['smooth'])) if gen.opt['smooth'] else E      # the ground as it is
        self.Esm = geo.blur(E, 3)                                                      # what yards level to
        self.Eroad = geo.blur(self.Esm, 4)                     # what roads level to: smoother, no waves along them
        self.H = self.E0.copy()                                                        # metres
        self.data = gen.data.gather(self.x0, self.y0, self.x1, self.y1)
        self.complete = self.data.complete
        # the real land cover of every cell (ESA WorldCover), or None while it is being fetched (incomplete chunks)
        self.wc = None
        lc = getattr(gen.data, 'landcover', None)
        if lc is not None:
            self.wc = lc.sample(lat, lon)
            if self.wc is None:
                self.complete = False

        self._buildings_prepare()
        self._pads()
        # football pitches: mown grass with a crisp edge; a path the map runs across one is not drawn on it
        # (the field: the rectangle its lines are drawn on, as the map generator paints it)
        rects = []
        for a in self.data.pitches:
            f = _frame(a)
            if f is not None and f[3] >= 12 * self.grid.upm:
                cx, cy, L, W, th = f
                c, s = math.cos(th), math.sin(th)
                rects.append([np.array([(cx + u * c - v * s, cy + u * s + v * c)
                                        for u, v in ((-L / 2, -W / 2), (L / 2, -W / 2), (L / 2, W / 2), (-L / 2, W / 2))])])
        self.pitch_mask = self.raster(rects, vertices=False) if rects else None
        self._sidewalks()
        self._roads()
        self._water()
        self._bridges()
        self._ground()
        self._markings()

    # ---- buildings: footprint rectangles and the level pads under them
    def _buildings_prepare(self):
        self.brects = []
        seen = {}
        for b in sorted(self.data.buildings, key=lambda b: (round(b.cx, 2), round(b.cy, 2))):
            if b.area < 6 * self.grid.upm ** 2:                      # under 6 m2: a kiosk, a shrine, noise
                continue
            key = (int(b.cx // 16), int(b.cy // 16))
            dup = False
            for kx in (key[0] - 1, key[0], key[0] + 1):
                for ky in (key[1] - 1, key[1], key[1] + 1):
                    for o in seen.get((kx, ky), ()):
                        if math.hypot(o.cx - b.cx, o.cy - b.cy) < 0.35 * math.sqrt(min(o.area, b.area)):
                            dup = True
            if dup:
                continue
            seen.setdefault(key, []).append(b)
            ring = [tuple(p) for p in b.ring.tolist()]
            mr = geo.min_rect(ring)
            if mr is None:
                continue
            self.brects.append((b, mr))
        self.brects = self._merge_sections(self.brects)
        # where the blocks of flats stand (a tower block next to them is one too: _estate_tower)
        self.flats_pts = [(b.cx, b.cy, b) for b, mr in self.brects
                          if getattr(b, 'block', False) or id(b) in self.panel_ids or self._flats_shape(b, mr[2], mr[3])]

    def _merge_sections(self, brects):
        """A block of flats is often mapped one entrance (section) at a time: sections that stand wall to wall in one
        line, 9-17 m deep, become one block (a panelak) - instead of a row of little houses."""
        upm = self.grid.upm
        self.panel_ids = set()                  # sections of a stepped block: each one a block of flats of its own
        cand = [i for i, (b, (cx, cy, L, W, th)) in enumerate(brects)
                if 8.5 <= min(L, W) / upm <= 17.5 and 90 <= b.area / upm ** 2 <= 900]
        if len(cand) < 2:
            return brects
        par = {i: i for i in cand}

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        def corners(mr, grow):
            cx, cy, L, W, th = mr
            c, s = math.cos(th), math.sin(th)
            return [(cx + u * c - v * s, cy + u * s + v * c)
                    for u, v in ((-L / 2 - grow, -W / 2 - grow), (L / 2 + grow, -W / 2 - grow),
                                 (L / 2 + grow, W / 2 + grow), (-L / 2 - grow, W / 2 + grow))]

        def overlap(m1, m2, grow):
            """Separating axes: do the two rectangles (grown by `grow`) overlap?"""
            P1, P2 = corners(m1, grow), corners(m2, grow)
            for th in (m1[4], m1[4] + math.pi / 2, m2[4], m2[4] + math.pi / 2):
                ax, ay = math.cos(th), math.sin(th)
                a = [x * ax + y * ay for x, y in P1]
                b = [x * ax + y * ay for x, y in P2]
                if max(a) < min(b) or max(b) < min(a):
                    return False
            return True
        grid = {}
        for i in cand:
            cx, cy = brects[i][1][:2]
            grid.setdefault((int(cx // 200), int(cy // 200)), []).append(i)
        for i in cand:
            m1 = brects[i][1]
            gx, gy = int(m1[0] // 200), int(m1[1] // 200)
            for kx in (gx - 1, gx, gx + 1):
                for ky in (gy - 1, gy, gy + 1):
                    for j in grid.get((kx, ky), ()):
                        if j <= i:
                            continue
                        m2 = brects[j][1]
                        dth = abs((m1[4] - m2[4] + math.pi / 4) % (math.pi / 2) - math.pi / 4)
                        if dth < math.radians(8) and overlap(m1, m2, 1.5 * upm):
                            par[find(i)] = find(j)
        groups = {}
        for i in cand:
            groups.setdefault(find(i), []).append(i)
        drop, extra = set(), []
        for g in groups.values():
            if len(g) < 2:
                continue
            g.sort()
            pts = [tuple(p) for i in g for p in brects[i][0].ring.tolist()]
            mr = geo.min_rect(pts)
            if mr is None:
                continue
            cx, cy, L, W, th = mr
            if W > L:
                L, W, th = W, L, th + math.pi / 2
            area = sum(brects[i][0].area for i in g)
            if not (9.0 <= W / upm <= 17.5 and L / upm >= 25.0 and area / (L * W) >= 0.7):
                # not one straight block: a stepped one (sections set off from each other) when there are 3+ of
                # them or they make 400 m2 - each section then stands as its own piece of a block of flats
                depth = sum(min(brects[i][1][2], brects[i][1][3]) for i in g) / len(g) / upm
                if (len(g) >= 3 or area / upm ** 2 >= 400) and depth >= 10.0:     # (terraced houses: shallower)
                    self.panel_ids.update(id(brects[i][0]) for i in g)
                continue
            drop.update(g)
            extra.append((_Block(np.array(corners((cx, cy, L, W, th), 0.0)), max(brects[i][0].height_m for i in g),
                                 area, any(getattr(brects[i][0], 'dense', False) for i in g)), (cx, cy, L, W, th)))
        if not extra:
            return brects
        return [br for i, br in enumerate(brects) if i not in drop] + extra

    def _pads(self):
        """Level the ground under every building to the smoothed height at its centre."""
        self.pad_w = np.zeros((self.rh, self.rw), np.float32)
        H, gen = self.H, self.gen
        for b, (cx, cy, L, W, th) in self.brects:
            fcx, fcy = float(self.fx(cx)), float(self.fy(cy))
            if not (6 <= fcx <= self.rw - 7 and 6 <= fcy <= self.rh - 7):
                continue                                              # centre too close to the region edge
            target = float(bilinear(self.Esm, np.array([fcx]), np.array([fcy]))[0])
            hl = min(L / 2 / CELL_UNITS + 0.6, PAD_REACH - 3)
            hw = min(W / 2 / CELL_UNITS + 0.6, PAD_REACH - 3)
            r = int(math.ceil(math.hypot(hl, hw) + 3))
            i0, i1 = max(int(fcx) - r, 0), min(int(fcx) + r + 2, self.rw)
            j0, j1 = max(int(fcy) - r, 0), min(int(fcy) + r + 2, self.rh)
            ii, jj = np.meshgrid(np.arange(i0, i1) - fcx, np.arange(j0, j1) - fcy)
            c, s = math.cos(th), math.sin(th)
            u, v = ii * c + jj * s, -ii * s + jj * c
            d = np.maximum(np.maximum(np.abs(u) - hl, np.abs(v) - hw), 0.0)       # cells outside the rectangle
            w = smoothstep(1.0 - d / 2.5).astype(np.float32)
            sub = H[j0:j1, i0:i1]
            sub += (target - sub) * w
            pw = self.pad_w[j0:j1, i0:i1]
            np.maximum(pw, w, out=pw)

    # ---- pavements the map gives only as a tag of the street (sidewalk=both): drawn along town streets
    def _sidewalks(self):
        """A pavement on both sides of a town street, just past the edge the game draws the street at, where the map
        has no footpath of its own within 10 m; it stops at junctions (another road's ribbon) and at buildings."""
        upm = self.grid.upm
        town_rings = [a.rings for a in self.data.land if a.kind in SIDEWALK_LAND]
        if not town_rings:
            return
        town = self.raster(town_rings, vertices=False)
        img_r = Image.new('L', (self.rw, self.rh), 0)
        img_f = Image.new('L', (self.rw, self.rh), 0)
        dr, df = ImageDraw.Draw(img_r), ImageDraw.Draw(img_f)
        for r in self.data.roads:
            pts = list(zip(self.fx(r.pts[:, 0]).tolist(), self.fy(r.pts[:, 1]).tolist()))
            if len(pts) < 2:
                continue
            if r.kind in ('footway', 'path_paved', 'pedestrian', 'path'):
                df.line(pts, fill=1, width=max(1, int(round(2 * 10.0 * upm / CELL_UNITS))))
            elif not r.bridge:
                w = max(r.width, DRAWN_WIDTH.get(r.rtype, r.width))      # (its own pavement lies past this)
                dr.line(pts, fill=1, width=max(1, int(round(w / CELL_UNITS))))
        blocked = np.asarray(img_r, bool) | np.asarray(img_f, bool) | (self.pad_w > 0.5)
        made = []
        for r in self.data.roads:
            if r.kind not in SIDEWALK_STREETS or r.bridge:
                continue
            p = _densify(r.pts, CELL_UNITS)
            if len(p) < 3:
                continue
            off = max(r.width, DRAWN_WIDTH.get(r.rtype, r.width)) / 2 + DRAWN_WIDTH['Sidewalk'] / 2 + 2.0
            for side in (1, -1):
                q = _offset_line(p, side * off)
                ci = np.floor(self.fx(q[:, 0])).astype(np.int64)
                cj = np.floor(self.fy(q[:, 1])).astype(np.int64)
                inside = (ci >= 0) & (ci < self.rw) & (cj >= 0) & (cj < self.rh)
                keep = np.zeros(len(q), bool)
                keep[inside] = town[cj[inside], ci[inside]] & ~blocked[cj[inside], ci[inside]]
                k = 0
                while k < len(q):
                    if not keep[k]:
                        k += 1
                        continue
                    k1 = k
                    while k1 + 1 < len(q) and keep[k1 + 1]:
                        k1 += 1
                    piece = q[k:k1 + 1]
                    if len(piece) >= 2 and _length(piece) >= 8.0 * upm:
                        made.append(worlddata.Road('footway', piece, False, upm))
                    k = k1 + 1
        self.data.roads = list(self.data.roads) + made

    # ---- roads: a level bed across the road, following the smoothed terrain along it
    def _roads(self):
        rw, rh = self.rw, self.rh
        weight = np.zeros((rh, rw), np.float32)
        target = np.zeros((rh, rw), np.float32)
        surface = np.full((rh, rw), 255, np.uint8)                    # ground class of the road under this cell
        self.roads = sorted(self.data.roads, key=lambda r: (-r.rank, r.bbox))
        for r in self.roads:
            if r.bridge:
                continue
            px, py = self.fx(r.pts[:, 0]), self.fy(r.pts[:, 1])
            # the game draws a road at its Roads.ini width (often wider than the real one): the level bed reaches a
            # full cell past the drawn edge, so the ribbon never drapes over a cell that is half bed, half slope
            # (the "bumps" along a road on a hillside); its own surface is painted under the drawn ribbon
            drawn = max(r.width, DRAWN_WIDTH.get(r.rtype, r.width)) / 2 / CELL_UNITS
            half = r.width / 2 / CELL_UNITS
            inner = drawn + 1.0
            paint = drawn + 0.3
            shoulder = max(2.0, half * 1.3) if r.kind not in ('path', 'footway', 'path_paved') else 1.2
            reach = inner + shoulder
            i0 = max(int(math.floor(px.min() - reach)), 0); i1 = min(int(math.ceil(px.max() + reach)) + 1, rw)
            j0 = max(int(math.floor(py.min() - reach)), 0); j1 = min(int(math.ceil(py.max() + reach)) + 1, rh)
            if i0 >= i1 or j0 >= j1:
                continue
            ax, ay, bx, by = px[:-1], py[:-1], px[1:], py[1:]
            dx, dy = bx - ax, by - ay
            ln2 = np.maximum(dx * dx + dy * dy, 1e-9)
            ground = ROAD_GROUND.get(gconst.ROAD_SURFACE.get(r.rtype, 'dirt'), G_DIRT)
            # rows in slabs, so the (segments x rows x cols) arrays stay small
            step = max(1, int(400000 / max(1, len(ax) * (i1 - i0))))
            for ja in range(j0, j1, step):
                jb = min(ja + step, j1)
                gi = np.arange(i0, i1, dtype=np.float64)[None, None, :]
                gj = np.arange(ja, jb, dtype=np.float64)[None, :, None]
                t = np.clip(((gi - ax[:, None, None]) * dx[:, None, None] +
                             (gj - ay[:, None, None]) * dy[:, None, None]) / ln2[:, None, None], 0, 1)
                qx = ax[:, None, None] + t * dx[:, None, None]
                qy = ay[:, None, None] + t * dy[:, None, None]
                d2 = (gi - qx) ** 2 + (gj - qy) ** 2
                k = np.argmin(d2, axis=0)
                d = np.sqrt(np.take_along_axis(d2, k[None], 0)[0])
                near = d < reach
                if not near.any():
                    continue
                qxn = np.take_along_axis(qx, k[None], 0)[0]
                qyn = np.take_along_axis(qy, k[None], 0)[0]
                w = smoothstep(1.0 - (d - inner) / shoulder).astype(np.float32)
                sub_w = weight[ja:jb, i0:i1]
                better = near & (w > sub_w)
                if r.kind in PITCH_HIDDEN and self.pitch_mask is not None:
                    better &= ~self.pitch_mask[ja:jb, i0:i1]
                if not better.any():
                    continue
                tg = bilinear(self.Eroad, qxn[better], qyn[better]).astype(np.float32)
                sub_w[better] = w[better]
                target[ja:jb, i0:i1][better] = tg
                surface[ja:jb, i0:i1][better & (d < paint)] = ground
        self.H += (target - self.H) * weight
        self.road_w, self.road_surface = weight, surface
        self.road_core = surface != 255                                # under a drawn ribbon (water stops here)

    # ---- water: lakes and river areas, each chunk's part at its own level
    def _water(self):
        """The water surface of lakes and rivers, as the elevation data measures it, and a bed under it.

        Over water a DEM measures the surface, so a chunk's level is a low percentile of the ground under its water
        and the 32 cells around it (a river falls a little from chunk to chunk; a pond above the river is a second
        level). The surface goes to the game as a few rectangles per chunk (8 x 8 cell blocks): the ground decides
        the shore - the bed is carved below the level, dry ground inside a rectangle kept above it."""
        self.water_mask = np.zeros((self.rh, self.rw), bool)
        self.water_areas = [a for a in self.data.water]
        if self.water_areas:
            self.water_mask = self.raster([a.rings for a in self.water_areas], vertices=True)
        # streams: a shallow channel along the line
        self.stream_mask = np.zeros((self.rh, self.rw), bool)
        if self.data.waterlines:
            img = Image.new('L', (self.rw, self.rh), 0)
            d = ImageDraw.Draw(img)
            for wl in self.data.waterlines:
                if wl.kind not in ('river', 'canal', 'stream'):
                    continue                                   # ditches and drains: not water you see from above
                # at least two cells (5 m) wide: narrower water vanishes between the terrain's vertices
                wm = max({'river': 14.0, 'canal': 9.0}.get(wl.kind, 5.0) * self.grid.upm / CELL_UNITS, 2.0)
                pts = list(zip((self.fx(wl.pts[:, 0]) + 0.5).tolist(), (self.fy(wl.pts[:, 1]) + 0.5).tolist()))
                d.line(pts, fill=1, width=max(1, int(round(wm))))
            self.stream_mask = np.asarray(img, bool) & ~self.water_mask
        wet = self.water_mask | self.stream_mask
        self.wet = wet
        self.water_rects = {}                                  # (cx, cy) -> [(level m, i0, j0, i1, j1)] region vertices
        self.water_quads = {}                                  # (cx, cy) -> [(level m, 4 world points)] brooks
        if not wet.any():
            return
        n, rw, rh = self.grid.n, self.rw, self.rh
        self._streams()
        wm = self.water_mask
        if not wm.any():
            return
        level = np.full((rh, rw), np.nan)
        for ccy in range(self.gj0 // n, (self.gj0 + rh - 1) // n + 1):
            for ccx in range(self.gi0 // n, (self.gi0 + rw - 1) // n + 1):
                a0, b0 = ccx * n - self.gi0, ccy * n - self.gj0           # the chunk's first vertex in the region
                i0, i1, j0, j1 = max(a0, 0), min(a0 + n, rw), max(b0, 0), min(b0 + n, rh)
                if i0 >= i1 or j0 >= j1:
                    continue
                cm = wm[j0:j1, i0:i1]
                if not cm.any():
                    continue
                wi0, wi1, wj0, wj1 = max(i0 - 32, 0), min(i1 + 32, rw), max(j0 - 32, 0), min(j1 + 32, rh)
                rest = np.sort(self.E0[wj0:wj1, wi0:wi1][wm[wj0:wj1, wi0:wi1]])
                levels = []
                while rest.size and len(levels) < 3:
                    lv = float(np.percentile(rest, 20)) - 0.1
                    levels.append(lv)
                    rest = rest[rest > lv + 2.0]
                e = self.E0[j0:j1, i0:i1]
                grp = np.zeros(e.shape, np.int32)
                for gi, lv in enumerate(levels):                   # ascending: the highest level a cell reaches
                    grp[e >= lv - 1.0] = gi
                lv_cells = np.asarray(levels)[grp]
                sub = level[j0:j1, i0:i1]
                sub[cm] = lv_cells[cm]
                for gi, lv in enumerate(levels):
                    m = cm & (grp == gi)
                    if not m.any():
                        continue
                    blocks = np.zeros((n // 8, n // 8), bool)
                    jj, ii = np.nonzero(m)
                    blocks[(jj + j0 - b0) // 8, (ii + i0 - a0) // 8] = True
                    for bx0, by0, bx1, by1 in _block_rects(blocks):
                        # vertices of the blocks plus the first vertex of the next block: no gap at a chunk edge
                        self.water_rects.setdefault((ccx, ccy), []).append(
                            (lv, a0 + bx0 * 8, b0 + by0 * 8, a0 + (bx1 + 1) * 8, b0 + (by1 + 1) * 8, 0))
        # the bed: a bowl below the level, deeper the farther from the bank (a big river 4-5 m, a pond's edge 0.4 m)
        deep = geo.blur(wm.astype(np.float64), 6)
        bed = level - (0.4 + 4.0 * np.clip(deep * 1.6 - 0.3, 0, 1))
        cells = wm & ~self.road_core & ~np.isnan(level)
        self.H[cells] = np.minimum(self.H[cells], bed[cells])
        # the banks: dry ground under a rectangle stays above its water
        dry = ~(wm | self.stream_mask) | self.road_core
        for rects in self.water_rects.values():
            for lv, i0, j0, i1, j1, flags in rects:
                if flags & WATER_FLAG_FORD:
                    continue                                   # the streams' banks were made by _streams
                ci0, ci1, cj0, cj1 = max(i0, 0), min(i1 + 1, rw), max(j0, 0), min(j1 + 1, rh)
                if ci0 >= ci1 or cj0 >= cj1:
                    continue
                sub, d = self.H[cj0:cj1, ci0:ci1], dry[cj0:cj1, ci0:ci1]
                sub[d] = np.maximum(sub[d], lv + 0.35)

    def _streams(self):
        """Brooks, streams and rivers drawn as lines: water that steps down the valley in 4 x 4 cell blocks (10 m).

        A block's level is a low percentile of the ground under its water (rounded to a world unit, so neighbours at
        the same level merge into one rectangle), its bed 0.6 m below that, the dry ground around it kept above it.
        They are fordable: the game draws them but its units cross them (WATER_FLAG_FORD); a road over one keeps its
        level (a culvert)."""
        sm = self.stream_mask
        if not sm.any():
            return
        n, rw, rh, B = self.grid.n, self.rw, self.rh, 4
        wet = sm & ~self.road_core
        jj, ii = np.nonzero(wet)
        if not len(jj):
            return
        upm = self.grid.upm
        bi, bj = (self.gi0 + ii) // B, (self.gj0 + jj) // B             # blocks on the world's grid
        keys = (bj.astype(np.int64) << 32) + (bi.astype(np.int64) & 0xFFFFFFFF)
        order = np.argsort(keys, kind='stable')
        keys, ii, jj = keys[order], ii[order], jj[order]
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        ends = np.r_[starts[1:], len(keys)]
        e = self.E0[jj, ii]
        level = np.full((rh, rw), np.nan)
        blocks = {}                                                     # (bi, bj) -> level m (on the unit grid)
        for s, t in zip(starts, ends):
            lv = float(np.percentile(e[s:t], 25)) - 0.1
            lv = round(lv * upm) / upm
            level[jj[s:t], ii[s:t]] = lv
            blocks[((self.gi0 + int(ii[s])) // B, (self.gj0 + int(jj[s])) // B)] = lv
        cells = ~np.isnan(level)
        self.H[cells] = np.minimum(self.H[cells], level[cells] - 0.6)
        # per chunk and level: the blocks into rectangles
        per = {}
        for (bx, by), lv in blocks.items():
            ccx, ccy = (bx * B) // n, (by * B) // n
            per.setdefault((ccx, ccy, lv), []).append(((bx * B) % n // B, (by * B) % n // B))
        nb = n // B
        dry = ~(self.water_mask | sm) | self.road_core
        for (ccx, ccy, lv), cells_b in per.items():
            grid_b = np.zeros((nb, nb), bool)
            for x, y in cells_b:
                grid_b[y, x] = True
            a0, b0 = ccx * n - self.gi0, ccy * n - self.gj0
            for bx0, by0, bx1, by1 in _block_rects(grid_b):
                # the banks: dry ground around the blocks stays above their water
                r = (a0 + bx0 * B - 1, b0 + by0 * B - 1, a0 + (bx1 + 1) * B + 1, b0 + (by1 + 1) * B + 1)
                ci0, ci1, cj0, cj1 = max(r[0], 0), min(r[2] + 1, rw), max(r[1], 0), min(r[3] + 1, rh)
                if ci0 < ci1 and cj0 < cj1:
                    sub, d = self.H[cj0:cj1, ci0:ci1], dry[cj0:cj1, ci0:ci1]
                    sub[d] = np.maximum(sub[d], lv + 0.3)
        # the surface: a strip of quads down each stream's own line (10 m each, the corners shared with the next one,
        # a cell wider than the channel on each side - the banks hide that), each at its block's level
        for wl in self.data.waterlines:
            if wl.kind not in ('river', 'canal', 'stream'):
                continue
            half = max({'river': 14.0, 'canal': 9.0}.get(wl.kind, 5.0) * upm, 2 * CELL_UNITS) / 2 + CELL_UNITS
            p = _densify(wl.pts, 40.0)
            if len(p) < 2:
                continue
            d = np.diff(p, axis=0)
            ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
            nrm = np.stack([-d[:, 1] / ln, d[:, 0] / ln], 1)
            vn = np.empty_like(p)
            vn[0], vn[-1] = nrm[0], nrm[-1]
            vn[1:-1] = nrm[:-1] + nrm[1:]
            vn /= np.maximum(np.hypot(vn[:, 0], vn[:, 1]), 1e-9)[:, None]
            cosh = np.ones(len(p))
            cosh[1:-1] = np.clip((vn[1:-1] * nrm[:-1]).sum(1), 0.5, 1.0)
            L, R = p + vn * (half / cosh)[:, None], p - vn * (half / cosh)[:, None]
            prev_lv = None
            for k in range(len(p) - 1):
                mx, my = (p[k] + p[k + 1]) / 2
                ci, cj = int(math.floor(mx / CELL_UNITS)), int(math.floor(my / CELL_UNITS))
                lv = None
                for dj in (0, -1, 1):                                  # the block under it, or a neighbour's
                    for di in (0, -1, 1):
                        lv = blocks.get(((ci + di * B) // B, (cj + dj * B) // B))
                        if lv is not None:
                            break
                    if lv is not None:
                        break
                if lv is None:
                    lv = prev_lv
                if lv is None:
                    continue                                           # under a road (a culvert): no surface
                prev_lv = lv
                q = [tuple(L[k]), tuple(L[k + 1]), tuple(R[k + 1]), tuple(R[k])]
                self.water_quads.setdefault((int(mx // (n * CELL_UNITS)), int(my // (n * CELL_UNITS))), []).append((lv, q))

    # ---- bridges: decks as built, at the height what they cross asks for, with embankments up to them
    BRIDGE_CLEAR_ROAD = 6.5      # m between a road under a bridge and the deck (5.5 m clearance + the deck itself)
    BRIDGE_CLEAR_RAIL = 7.5      # m over a railway (overhead line)
    BRIDGE_CLEAR_RIVER = 8.0     # m over a big river or lake (shipping)
    BRIDGE_CLEAR_BROOK = 2.0     # m over a brook
    RAMP_GRADE = 0.06            # rise per run of an approach embankment
    RAMP_FALL = 28.0             # units the side of an embankment takes to reach the ground

    def _bridges(self):
        """Every bridge of the region as one deck: OSM splits a bridge into ways (at every junction, at each tile
        edge, one per carriageway); they are joined end to end into runs, and runs that lie on the same bridge outline
        (OSM man_made=bridge) of a straight bridge become ONE deck down the middle of the outline, as wide as it is.
        Otherwise a run keeps its own line and its road's width (a footbridge stays a narrow footbridge).

        The deck is as high as what it crosses asks for - a road under it plus clearance, a railway, a river plus
        shipping clearance, a brook, the ground of the valley it spans - and never below the ground at its ends.
        Where the deck is above the ground at an end, the road leading to it is raised on an embankment at
        RAMP_GRADE, its sides falling to the ground; the road it crosses keeps its own level in a corridor."""
        self.bridge_specs = []
        self.bridge_ways = set()                               # id() of the roads drawn as a bridge deck instead
        self.embank = np.zeros((self.rh, self.rw), bool)       # raised for an approach: grass, not rock
        self.deck_mask = np.zeros((self.rh, self.rw), bool)    # under a bridge deck: no building stands there
        self.culvert_ways = set()                              # id() of bridge ways drawn as ordinary roads
        self.stream_cut = np.zeros((self.rh, self.rw), bool)   # a brook under a road (a culvert): no water drawn
        roads = [r for r in self.roads if r.bridge]
        if not roads:
            return
        upm = self.grid.upm

        def key(p):
            return (int(round(p[0] / 4.0)), int(round(p[1] / 4.0)))

        # 1. runs: ways joined end to end (straightest continuation first)
        ends = {}
        for k, r in enumerate(roads):
            ends.setdefault(key(r.pts[0]), []).append((k, 0))
            ends.setdefault(key(r.pts[-1]), []).append((k, 1))
        used, runs = set(), []
        for k0 in range(len(roads)):
            if k0 in used:
                continue
            used.add(k0)
            pts = [tuple(p) for p in roads[k0].pts.tolist()]
            members = [roads[k0]]
            for front in (False, True):
                while True:
                    tip, prev = (pts[0], pts[1]) if front else (pts[-1], pts[-2])
                    best = None
                    for k, e in ends.get(key(tip), ()):
                        if k in used:
                            continue
                        q = roads[k].pts.tolist()
                        q = q if e == 0 else q[::-1]
                        d1 = (tip[0] - prev[0], tip[1] - prev[1])
                        d2 = (q[1][0] - q[0][0], q[1][1] - q[0][1])
                        n1, n2 = math.hypot(*d1) or 1.0, math.hypot(*d2) or 1.0
                        cs = (d1[0] * d2[0] + d1[1] * d2[1]) / (n1 * n2)
                        if cs > 0.3 and (best is None or cs > best[0]):
                            best = (cs, k, q)
                    if best is None:
                        break
                    _, k, q = best
                    used.add(k)
                    members.append(roads[k])
                    q = [tuple(p) for p in q[1:]]
                    pts = (q[::-1] + pts) if front else (pts + q)
            runs.append((np.asarray(pts, float), members))

        # 2. outlines: runs on one straight bridge outline become one deck down its middle
        outlines = []
        for a in self.data.bridges:
            ring = [tuple(p) for p in a.rings[0].tolist()]
            mr = geo.min_rect(ring)
            if mr is None:
                continue
            area = abs(geo.poly_area(ring))
            outlines.append((a, ring, mr, area))
        # runs side by side on one structure (two carriageways, the footways along them): one deck, as wide as
        # all of them from the outer edge of one to the outer edge of the other, down their middle
        parent = list(range(len(runs)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        dense = [_densify(p, 20.0) for p, _ in runs]
        boxes = [(d[:, 0].min(), d[:, 1].min(), d[:, 0].max(), d[:, 1].max()) for d in dense]
        for i in range(len(runs)):
            for j in range(i + 1, len(runs)):
                if find(i) == find(j):
                    continue
                bi, bj = boxes[i], boxes[j]
                if bi[0] > bj[2] + 60 or bj[0] > bi[2] + 60 or bi[1] > bj[3] + 60 or bj[1] > bi[3] + 60:
                    continue
                rail_i = any(m.kind in ('rail', 'rail_side') for m in runs[i][1])
                rail_j = any(m.kind in ('rail', 'rail_side') for m in runs[j][1])
                if rail_i != rail_j:
                    continue                                           # a railway bridge is a structure of its own
                if _side_by_side(dense[i], dense[j], 60.0):             # within 15 m along most of the shorter
                    parent[find(j)] = find(i)
        clusters = {}
        for i in range(len(runs)):
            clusters.setdefault(find(i), []).append(i)
        decks = []                                             # (pts, members, width units)
        for idx in clusters.values():
            base = max(idx, key=lambda i: _length(runs[i][0]))
            line = dense[base]
            members = [m for i in idx for m in runs[i][1]]
            lo = hi = 0.0
            for i in idx:
                hw = max(m.width for m in runs[i][1]) / 2.0
                off = _mean_offset(line, dense[i]) if i != base else 0.0
                lo, hi = min(lo, off - hw), max(hi, off + hw)
            if len(idx) == 1:
                lo, hi = -max(m.width for m in members) / 2.0, max(m.width for m in members) / 2.0
            decks.append((_offset_line(line, (lo + hi) / 2.0), members, hi - lo))
        # an outline wider than the roads on it (the footways were not mapped as ways): the outline's width
        for k, (pts, members, width) in enumerate(decks):
            mid = pts[len(pts) // 2]
            for a, ring, (cx, cy, L, W, th), area in outlines:
                if a.bbox[0] <= mid[0] <= a.bbox[2] and a.bbox[1] <= mid[1] <= a.bbox[3] and geo.point_in_poly(mid[0], mid[1], ring):
                    if L > 1.5 * W and area > 0.6 * L * W and W > width:
                        decks[k] = (pts, members, min(float(W), width * 3.0))
                    break

        # 3. heights
        lowroads = [r for r in self.roads if not r.bridge]
        wet = self.water_mask | self.stream_mask
        for pts, members, width in decks:
            seg = np.hypot(*np.diff(pts, axis=0).T)
            L = float(seg.sum())
            if L < 8.0:
                continue
            s_cum = np.concatenate([[0.0], np.cumsum(seg)])
            ss = np.linspace(0.0, L, max(2, int(L / 5.0) + 1))
            xs = np.interp(ss, s_cum, pts[:, 0])
            ys = np.interp(ss, s_cum, pts[:, 1])
            fi, fj = self.fx(xs), self.fy(ys)
            ci = np.clip(np.rint(fi).astype(np.int64), 0, self.rw - 1)
            cj = np.clip(np.rint(fj).astype(np.int64), 0, self.rh - 1)
            ground = bilinear(self.H, fi, fj)
            za, zb = float(ground[0]), float(ground[-1])
            # what each point of the deck must clear: the ground under it, water, the roads it crosses
            need = ground + 1.0
            need[0], need[-1] = za, zb
            under = wet[cj, ci]
            if under.any():
                level = float(np.min(self.E0[cj[under], ci[under]]))
                big = self.water_mask[cj, ci].sum() * 5.0 / upm > 30.0      # more than 30 m of lake or river
                clear = self.BRIDGE_CLEAR_RIVER if big else self.BRIDGE_CLEAR_BROOK
                need[under] = np.maximum(need[under], level + clear)
            crossings = []
            bx0, bx1, by0, by1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
            for r in lowroads:
                if r.bbox[2] < bx0 or r.bbox[0] > bx1 or r.bbox[3] < by0 or r.bbox[1] > by1:
                    continue
                for X in _crossings(pts, r.pts):
                    k = int(np.argmin((xs - X[0]) ** 2 + (ys - X[1]) ** 2))
                    if ss[k] < 60.0 or ss[k] > L - 60.0:
                        continue                                       # the road the bridge starts from
                    gz = float(bilinear(self.H, self.fx(np.array([X[0]])), self.fy(np.array([X[1]])))[0])
                    clear = self.BRIDGE_CLEAR_RAIL if r.kind in ('rail', 'rail_side') else self.BRIDGE_CLEAR_ROAD
                    need[k] = max(need[k], gz + clear)
                    crossings.append((X, r))
            need = np.minimum(need, ground + 25.0)                         # a data error, not a 25 m viaduct
            big = bool(under.any()) and self.water_mask[cj, ci].sum() * 5.0 / upm > 30.0
            if not crossings and not big and L < 100.0 and all(m.kind not in ('rail', 'rail_side') for m in members):
                # a short bridge over a brook or a ditch: in reality a culvert or a slab level with the road - the road
                # goes on over it, on the ground, which is filled in under it
                self.culvert_ways.update(id(m) for m in members)
                img = Image.new('L', (self.rw, self.rh), 0)
                ImageDraw.Draw(img).line(list(zip((fi + 0.5).tolist(), (fj + 0.5).tolist())), fill=1,
                                         width=max(2, int(round(width / CELL_UNITS)) + 2))
                m = np.asarray(img, bool)
                fill = np.full((self.rh, self.rw), -1e9)
                fill[cj, ci] = za + (zb - za) * ss / L
                fill = np.maximum.reduce([np.roll(np.roll(fill, dj, 0), di, 1) for dj in (-2, -1, 0, 1, 2) for di in (-2, -1, 0, 1, 2)])
                self.H[m] = np.maximum(self.H[m], fill[m])
                self.stream_cut[m] = True
                continue
            overpass = bool(crossings) and not big
            # the deck: the line between its ends, lifted over every need and falling from it at most 4 %
            grade = 0.04 * (ss[1] - ss[0]) / upm
            dz = np.maximum(za + (zb - za) * ss / L, need)
            for i in range(1, len(dz)):
                dz[i] = max(dz[i], dz[i - 1] - grade)
            for i in range(len(dz) - 2, -1, -1):
                dz[i] = max(dz[i], dz[i + 1] - grade)
            ends_z = (float(dz[0]), float(dz[-1]))
            main = min(members, key=lambda m: m.rank)              # the most important road on it (rank 0 = rail)
            tmpl = None
            if overpass and main.kind not in ('rail', 'rail_side') and main.rtype not in STREAM_BRIDGE:
                tmpl = 'EuropeanBridgeTwoLane'     # over roads and railways a girder deck on piers, not stone arches
            self.bridge_specs.append(dict(pts=np.stack([xs, ys], 1), z=dz, width=width, rtype=main.rtype,
                                          rank=main.rank, tmpl=tmpl))
            img = Image.new('L', (self.rw, self.rh), 0)
            ImageDraw.Draw(img).line(list(zip((fi + 0.5).tolist(), (fj + 0.5).tolist())), fill=1,
                                     width=max(1, int(round(width / CELL_UNITS))))
            self.deck_mask |= np.asarray(img, bool)
            for m in members:
                self.bridge_ways.add(id(m))
            # 4. the approaches
            for end, outward in ((0, -1), (len(xs) - 1, 1)):
                rise = ends_z[0 if end == 0 else 1] - float(ground[end])
                if rise < 0.3:
                    continue
                E = (float(xs[end]), float(ys[end]))
                back = (float(xs[end - outward]), float(ys[end - outward])) if len(xs) > 1 else E
                self._embankment(E, back, ends_z[0 if end == 0 else 1], rise, width, crossings, lowroads)

    def _embankment(self, E, back, deck_z, rise, width, crossings, lowroads):
        """Raise the road leading away from a bridge end E (the deck is at deck_z, `rise` m above the ground) on an
        embankment falling at RAMP_GRADE; its sides fall to the ground over RAMP_FALL units."""
        upm = self.grid.upm
        reach = rise * upm / self.RAMP_GRADE                           # units of road to come down
        # the road(s) continuing from E, straightest first
        d0 = (E[0] - back[0], E[1] - back[1])
        n0 = math.hypot(*d0) or 1.0
        chain, pt, prev_dir, got, seen = [E], E, (d0[0] / n0, d0[1] / n0), 0.0, set()
        for _ in range(8):
            if got >= reach:
                break
            best = None
            for r in lowroads:
                if id(r) in seen or r.bbox[0] > pt[0] + 6 or r.bbox[2] < pt[0] - 6 or r.bbox[1] > pt[1] + 6 or r.bbox[3] < pt[1] - 6:
                    continue
                for q in (r.pts, r.pts[::-1]):
                    if math.hypot(q[0][0] - pt[0], q[0][1] - pt[1]) > 6.0 or len(q) < 2:
                        continue
                    d = (q[1][0] - q[0][0], q[1][1] - q[0][1])
                    n = math.hypot(*d) or 1.0
                    cs = (d[0] * prev_dir[0] + d[1] * prev_dir[1]) / n
                    if cs > 0.3 and (best is None or cs > best[0]):
                        best = (cs, r, q)
            if best is None:
                break
            _, r, q = best
            seen.add(id(r))
            for p in q[1:]:
                p = (float(p[0]), float(p[1]))
                got += math.hypot(p[0] - pt[0], p[1] - pt[1])
                chain.append(p)
                pt = p
                if got >= reach:
                    break
            if len(chain) >= 2:
                dd = (chain[-1][0] - chain[-2][0], chain[-1][1] - chain[-2][1])
                nn = math.hypot(*dd) or 1.0
                prev_dir = (dd[0] / nn, dd[1] / nn)
        if len(chain) < 2:                                             # no road found: straight on
            chain.append((E[0] + prev_dir[0] * reach, E[1] + prev_dir[1] * reach))
        chain = np.asarray(chain, float)
        hw = width / 2.0 + 6.0
        fall = max(self.RAMP_FALL, rise * upm * 1.8)                  # sides about 1 : 1.8, grassed
        i0 = max(int(math.floor(self.fx(chain[:, 0].min() - hw - fall))) - 1, 0)
        i1 = min(int(math.ceil(self.fx(chain[:, 0].max() + hw + fall))) + 2, self.rw)
        j0 = max(int(math.floor(self.fy(chain[:, 1].min() - hw - fall))) - 1, 0)
        j1 = min(int(math.ceil(self.fy(chain[:, 1].max() + hw + fall))) + 2, self.rh)
        if i0 >= i1 or j0 >= j1:
            return
        gx = (self.gi0 + np.arange(i0, i1))[None, :] * CELL_UNITS + np.zeros((j1 - j0, 1))
        gy = (self.gj0 + np.arange(j0, j1))[:, None] * CELL_UNITS + np.zeros((1, i1 - i0))
        dmin = np.full(gx.shape, 1e18)
        sc = np.zeros(gx.shape)
        cum = 0.0
        for a, b in zip(chain[:-1], chain[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            l2 = dx * dx + dy * dy
            if l2 < 1e-9:
                continue
            t = np.clip(((gx - a[0]) * dx + (gy - a[1]) * dy) / l2, 0, 1)
            dd = np.hypot(gx - (a[0] + t * dx), gy - (a[1] + t * dy))
            better = dd < dmin
            dmin = np.where(better, dd, dmin)
            sc = np.where(better, cum + t * math.sqrt(l2), sc)
            cum += math.sqrt(l2)
        # behind the abutment (under the deck) stays as it is
        o = chain[1] - chain[0]
        o /= (np.hypot(*o) or 1.0)
        front = ((gx - E[0]) * o[0] + (gy - E[1]) * o[1]) >= -2.0
        T = deck_z - self.RAMP_GRADE * sc / upm
        wc = np.clip(1.0 - np.maximum(0.0, dmin - hw) / fall, 0.0, 1.0) * front
        sub = self.H[j0:j1, i0:i1]
        lift = np.maximum(0.0, T - sub) * wc
        # the road the bridge crosses keeps its level in a corridor near the crossing
        for X, r in crossings:
            near = np.hypot(gx - X[0], gy - X[1]) < 400.0
            if not near.any():
                continue
            dr = np.full(gx.shape, 1e18)
            for a, b in zip(r.pts[:-1], r.pts[1:]):
                dx, dy = b[0] - a[0], b[1] - a[1]
                l2 = dx * dx + dy * dy
                if l2 < 1e-9:
                    continue
                t = np.clip(((gx - a[0]) * dx + (gy - a[1]) * dy) / l2, 0, 1)
                dr = np.minimum(dr, np.hypot(gx - (a[0] + t * dx), gy - (a[1] + t * dy)))
            keep = np.clip((dr - (r.width / 2.0 + 10.0)) / 20.0, 0.0, 1.0)
            lift = np.where(near, lift * keep, lift)
        lift[self.water_mask[j0:j1, i0:i1] | self.stream_mask[j0:j1, i0:i1]] = 0.0
        sub += lift
        self.embank[j0:j1, i0:i1] |= lift > 0.05

    # ---- ground class per cell
    def _ground(self):
        rw, rh = self.rw, self.rh
        cls = np.full((rh, rw), G_GRASS, np.uint8)
        by_kind = {}
        for a in self.data.land:
            by_kind.setdefault(a.kind, []).append(a.rings)
        self.tree_density = np.zeros((rh, rw), np.float32)
        self.context = {}
        for kind in LAND_ORDER:
            if kind in by_kind:
                m = self.raster(by_kind[kind], vertices=False)
                cls[m] = LAND_GROUND[kind]
        # natural edges: the land cover is read a few cells off along a smooth noise (a field's or a wood's edge
        # wanders instead of following the cell grid); world-hashed, so the same in every block
        GI = (self.gi0 + np.arange(rw))[None, :].repeat(rh, 0)
        GJ = (self.gj0 + np.arange(rh))[:, None].repeat(rw, 1)
        ox = 2.6 * (value_noise(GI, GJ, 9.0, 21) + 0.45 * value_noise(GI, GJ, 3.5, 22))
        oy = 2.6 * (value_noise(GI, GJ, 9.0, 23) + 0.45 * value_noise(GI, GJ, 3.5, 24))
        si = np.clip(np.rint(np.arange(rw)[None, :] + ox), 0, rw - 1).astype(np.int64)
        sj = np.clip(np.rint(np.arange(rh)[:, None] + oy), 0, rh - 1).astype(np.int64)
        cls = cls[sj, si]
        for kind, dens in TREE_LAND.items():
            if kind in by_kind:
                m = self.raster(by_kind[kind], vertices=False)
                self.tree_density[m] = np.maximum(self.tree_density[m], dens)
        self.tree_density = self.tree_density[sj, si]           # the trees keep to the same wandering edge
        self._land_cover(cls, by_kind, sj, si)
        # a field drawn over a village: grass within 30 m of every house (yards and gardens), on the same wandering
        # edge (the map often has a village inside one farmland polygon)
        if (cls == G_FARM).any() and self.brects:
            r_cells = int(round(30.0 * self.grid.upm / CELL_UNITS))
            near = geo.box_mean((self.pad_w > 0.5).astype(np.float32), r_cells) > 1e-4
            near = near[sj, si]
            cls[(cls == G_FARM) & near] = G_GRASS
        # props: the areas that get them, and their cells (no tree on a car park)
        self.prop_areas = {k: [a for a in self.data.land if a.kind == k] for k in PROP_LAND if k in by_kind}
        self.prop_masks = {k: self.raster(by_kind[k], vertices=False) for k in self.prop_areas}
        if 'parking' in self.prop_masks:
            self.tree_density[self.prop_masks['parking']] = 0.0
        # football pitches: mown grass with a crisp edge, no tree on them
        if self.pitch_mask is not None:
            cls[self.pitch_mask] = G_FIELD                  # GrassMediumType38a, the generator's pitch turf
            self.tree_density[self.pitch_mask] = 0.0
        if 'parking' in self.prop_masks:
            cls[self.prop_masks['parking']] = G_ASPHALT     # a car park: asphalt to its very edge (no wandering edge)
        # town squares and pedestrian areas (Overpass): paving to their edge, a tree here and there
        if self.data.squares:
            sq = self.raster([a.rings for a in self.data.squares], vertices=False)
            cls[sq] = G_COBBLE
            np.minimum(self.tree_density, np.where(sq, 0.05, 1.0).astype(np.float32), out=self.tree_density)
        for kind, ctx in LAND_CONTEXT.items():
            if kind in by_kind:
                self.context.setdefault(ctx, np.zeros((rh, rw), bool))
                self.context[ctx] |= self.raster(by_kind[kind], vertices=False)
        # slopes: rock where it is steep (from the final heights, in metres per metre)
        gy, gx = np.gradient(self.H, self.grid.mpc)
        slope = np.hypot(gx, gy)
        cls[(slope > 0.75) & ~self.embank] = G_ROCK
        cls[(slope > 1.1) & ~self.embank] = G_CLIFF
        # beds of lakes and streams; under every road its own surface (asphalt, concrete, cobbles or dirt)
        cls[self.wet & (cls != G_CLIFF)] = G_SAND
        road = (self.road_surface != 255) & ~self.wet
        cls[road] = self.road_surface[road]
        self.cls = cls
        self.slope = slope

    # ---- painted lines (car park bays, football pitches) and the props that go with them, for the whole region
    def _markings(self):
        """self.marks = [(road type, ribbon width, Nx2 world line)], each owned by the chunk that holds the middle of
        its bounding box (a line is not cut at chunk edges: the bays' dividers must stay in step with the line's start);
        self.spots = [(prop set, x, y, angle, salt, gap, road_ok)], each placed by the chunk it falls in."""
        self.marks, self.spots = [], []
        self._park_marks()
        self._pitch_marks()
        self._rail_props()
        self._site_fences()
        self._fuel_points()

    def _site_fences(self):
        """A fence round a school, a hospital, a works, a cemetery (a wall) ...: pieces end to end along the outline,
        a gap where a road or a path goes in and where a building stands on the boundary."""
        upm = self.grid.upm
        for a in self.data.land:
            spec = FENCE_LAND.get(a.kind)
            if spec is None or a.own is None:
                continue
            tmpl, plen, from_end, max_ha = spec
            if not self.gen.models.has_object(tmpl):
                continue
            ring = a.rings[0]
            if max_ha and (a.bbox[2] - a.bbox[0]) * (a.bbox[3] - a.bbox[1]) / (upm * upm) > max_ha * 1e4:
                continue                                            # a whole industrial estate: not one fence
            n = len(ring)
            for i in range(n):
                if not a.own[i]:
                    continue
                p, q = ring[i], ring[(i + 1) % n]
                d = q - p
                ln = float(np.hypot(*d))
                if ln < plen * 0.5:
                    continue
                u = d / ln
                k = max(1, int(round(ln / plen)))
                step = ln / k
                ang = math.atan2(u[1], u[0])
                for j in range(k):
                    mx, my = p + u * (j + 0.5) * step               # the middle of the piece
                    ci, cj = int(math.floor(float(self.fx(mx)))), int(math.floor(float(self.fy(my))))
                    if not (0 <= ci < self.rw and 0 <= cj < self.rh):
                        continue
                    if self.road_surface[cj, ci] != 255 or self.pad_w[cj, ci] > 0.98 or self.wet[cj, ci]:
                        continue                                    # a gate, a building on the line, water
                    x, y = (p + u * j * step) if from_end else (mx, my)
                    self.spots.append((tmpl, float(x), float(y), ang, 0, 4.0, True))

    def _bay_ok(self, x, y, c, s, others):
        """A bay of the row at x / y (direction c, s; the bay's middle on the row's line) fits: on the car park, off
        buildings and water, and clear of any other aisle."""
        upm = self.grid.upm
        for du in (-PARK_BAY / 2 + 1, 0.0, PARK_BAY / 2 - 1):
            # the back corners a little in: the car park's raster is one cell (2.5 m) coarse at its edge, and a row
            # along the kerb has its back line right on the edge
            for dv in (-PARK_OPEN + 1, PARK_BACK - 7):
                px, py = x + c * du - s * dv, y + s * du + c * dv
                if not self._in_mask('parking', px, py):
                    return False
                ci, cj = int(math.floor(float(self.fx(px)))), int(math.floor(float(self.fy(py))))
                if self.pad_w[cj, ci] > 0.3 or self.wet[cj, ci] or self.deck_mask[cj, ci]:
                    return False
        if others:
            q = np.array([[x - s * (PARK_BACK - PARK_OPEN) / 2, y + c * (PARK_BACK - PARK_OPEN) / 2]])
            for line, half in others:
                if _project(line, q)[0][0] < half + (PARK_BACK + PARK_OPEN) / 2 + 1.0:
                    return False
        return True

    def _park_marks(self):
        """Car parks as they are built: rows of bays on both sides of every aisle (the map's parking aisles; a car park
        without one gets aisles on its long axis, world-anchored), the white bay lines of the game's ParkingLines, and
        about seven cars in ten bays, all nose-in to the back line, a few reversed in."""
        if 'parking' not in self.prop_masks:
            return
        aisles = [(r.pts, r.width / 2) for r in self.roads if r.kind == 'parking_aisle' and not r.bridge]
        half = 3.0 * self.grid.upm                                 # a 6 m aisle
        # bays taken so far, as 2-unit cells of their footprint: rows of different aisles (at an angle, at a bend)
        # must not overlap; a bay checks a slightly smaller footprint than it marks, so neighbours in a row still fit
        occ = set()

        def cells(x, y, c, s, du_max, v0, v1):
            for du in np.arange(-du_max, du_max + 0.01, 1.5):
                for dv in np.arange(v0, v1 + 0.01, 1.5):
                    yield (int(math.floor((x + c * du - s * dv) / 2.0)), int(math.floor((y + s * du + c * dv) / 2.0)))

        def near_aisles(P):
            return [(L, h) for L, h in aisles if L is not P and
                    L[:, 0].min() - 80 <= P[:, 0].max() and L[:, 0].max() + 80 >= P[:, 0].min() and
                    L[:, 1].min() - 80 <= P[:, 1].max() and L[:, 1].max() + 80 >= P[:, 1].min()]

        def row(A, B, others):
            """One straight row of bays on the line A -> B (its back line on the left): the runs of bays that fit, each
            run a ParkingLines strip, and cars in about seven bays in ten."""
            d = B - A
            ln = float(np.hypot(*d))
            if ln < 2 * PARK_BAY:
                return
            c, s = d / ln
            nb = int((ln - 2.0) // PARK_BAY)
            ok = []
            for k in range(nb):
                t = 1.0 + (k + 0.5) * PARK_BAY
                x, y = A[0] + c * t, A[1] + s * t
                free = not any(k_ in occ for k_ in cells(x, y, c, s, 6.5, -10.0, 11.0))
                good = free and self._bay_ok(x, y, c, s, others)
                ok.append(good)
                if good:
                    occ.update(cells(x, y, c, s, 8.0, -13.0, 14.0))
            k = 0
            while k < nb:
                if not ok[k]:
                    k += 1
                    continue
                k1 = k
                while k1 + 1 < nb and ok[k1 + 1]:
                    k1 += 1
                if k1 > k:                                          # two bays at least
                    t0 = 1.0 + k * PARK_BAY - PARK_FIRST
                    t1 = 1.0 + (k1 + 1) * PARK_BAY + 0.8
                    self.marks.append((PARK_TYPE, PARK_WIDTH, np.array([A + d / ln * t0, A + d / ln * t1])))
                    # asphalt under the whole row, its lines included
                    nrm = np.array([-s, c])
                    poly = np.array([A + d / ln * t0 - nrm * (PARK_OPEN + 4), A + d / ln * t1 - nrm * (PARK_OPEN + 4),
                                     A + d / ln * t1 + nrm * (PARK_BACK + 4), A + d / ln * t0 + nrm * (PARK_BACK + 4)])
                    paved = self.raster([[poly]], vertices=False)
                    self.cls[paved & ~self.wet] = G_ASPHALT
                    for j in range(k, k1 + 1):
                        t = 1.0 + (j + 0.5) * PARK_BAY
                        x, y = A[0] + c * t, A[1] + s * t
                        h = hash2(int(round(x)), int(round(y)), 44)
                        if (h & 0xFF) >= 180:                       # ~70 % taken
                            continue
                        # nose-in to the back line, one in five reversed in; the car's middle 1.1 left of the line
                        # (its nose ~3 units off the back line), a hair of jitter
                        v = 1.1 + ((h >> 8) & 0xF) / 15.0 - 0.5
                        ang = math.atan2(s, c) + math.pi / 2 + (math.pi if ((h >> 12) % 5) == 0 else 0.0)
                        ang += (((h >> 16) & 0xFF) / 255.0 - 0.5) * math.radians(5)
                        self.spots.append(('cars', x - s * v, y + c * v, ang, 7, 8.0, True))
                k = k1 + 1

        def both_sides(line, h):
            others = near_aisles(line)
            for side in (1, -1):
                r = _offset_line(line, side * (h + PARK_OPEN))
                if side < 0:
                    r = r[::-1]                                     # the back line is always left of the row
                for A, B, _ in _straight_runs(r, min_len=2 * PARK_BAY):
                    row(A, B, others)

        # 1. the map's aisles: a row on each side
        for line, h in aisles:
            both_sides(line, h)
        for a in self.prop_areas['parking']:
            ring = a.rings[0]
            n = len(ring)
            own = a.own if a.own is not None else np.ones(n, bool)
            edges = []
            for i in range(n):
                if not own[i]:
                    continue
                p, q = ring[i], ring[(i + 1) % n]
                d = q - p
                ln = float(np.hypot(*d))
                if ln < 2 * PARK_BAY + 4:
                    continue
                u = d / ln
                m = np.array([-u[1], u[0]])                         # the inward side: where the car park is
                mid = (p + q) / 2
                if not self._in_mask('parking', *(mid + m * 20.0)):
                    m = -m
                    if not self._in_mask('parking', *(mid + m * 20.0)):
                        continue
                edges.append((ln, p, q, u, m))
            edges.sort(key=lambda e: -e[0])
            bb = a.bbox
            has_aisle = any(L[:, 0].min() <= bb[2] and L[:, 0].max() >= bb[0] and L[:, 1].min() <= bb[3] and
                            L[:, 1].max() >= bb[1] and
                            any(self._in_mask('parking', float(px), float(py)) for px, py in L[::max(1, len(L) // 6)])
                            for L, _ in aisles)
            # 2. along the kerb: a row on every straight edge of the car park, its back line on the kerb (parallel to
            # the pavement), the bays opening inwards
            for ln, p, q, u, m in edges:
                off = PARK_BACK + 2.0
                A, B = p + m * off, q + m * off
                r_dir = np.array([-m[1], m[0]])                     # the row's direction: the back line (left) outside
                if float(r_dir @ (B - A)) < 0:
                    A, B = B, A
                row(A, B, near_aisles(np.array([A, B])))
            # 3. no aisle mapped: aisles parallel to the longest edge, with rows back to back between them
            if not has_aisle and edges:
                ln, p, q, u, m = edges[0]
                b = a.bbox
                corners = [np.array([x, y]) for x in (b[0], b[2]) for y in (b[1], b[3])]
                ts = [float((cc - p) @ u) for cc in corners]
                vs = [float((cc - p) @ m) for cc in corners]
                t0, t1 = min(ts) - PARK_BAY, max(ts) + PARK_BAY
                period = 2 * (half + PARK_OPEN + PARK_BACK)        # aisle, two rows back to back, next aisle
                v = 2.0 + PARK_BACK + PARK_OPEN + half              # the first aisle: in front of the kerb row
                while v < max(vs):
                    both_sides(np.array([p + u * t0 + m * v, p + u * t1 + m * v]), half)
                    v += period

    def _pitch_marks(self):
        """A football pitch: its lines in white (touch and goal lines, the halfway line and centre circle, both penalty
        and goal areas, sized down on a small pitch) and a goal in the middle of each goal line, its mouth to the
        field. Lines never share an end (the client would join them into a road junction)."""
        upm, e = self.grid.upm, 1.5
        for a in self.data.pitches:
            f = _frame(a)
            if f is None or f[3] < 12 * upm:
                continue
            cx, cy, L, W, th = f
            c, s = math.cos(th), math.sin(th)

            def P(u, v):
                return (cx + u * c - v * s, cy + u * s + v * c)

            def line(*pts):
                self.marks.append((LINE_TYPE, LINE_WIDTH, np.array([P(u, v) for u, v in pts])))
            hl, hw = L / 2, W / 2
            pd, pw = min(16.5 * upm, L * 0.16), min(40.3 * upm, W * 0.6)
            gd, gw = min(5.5 * upm, L * 0.055), min(18.3 * upm, W * 0.3)
            r = min(9.15 * upm, W * 0.15)
            for v in (-hw, hw):
                line((-hl + e, v), (hl - e, v))                    # touch lines
            for u in (-hl, hl):
                line((u, -hw + e), (u, hw - e))                    # goal lines
            line((0.0, -hw + e), (0.0, hw - e))                    # halfway line
            for sg in (-1, 1):
                for d_, w_ in ((pd, pw), (gd, gw)):
                    for v in (-w_ / 2, w_ / 2):
                        line((sg * (hl - e), v), (sg * (hl - d_ + e), v))
                    line((sg * (hl - d_), -w_ / 2 + e), (sg * (hl - d_), w_ / 2 - e))
                # the goal: 6 units behind the line (its net), the mouth (the model's +x) to the middle
                gx, gy = P(sg * (hl + 6.0), 0.0)
                self.spots.append(('goal', gx, gy, th + (math.pi if sg > 0 else 0.0), 12, 20.0, False))
            n = 32
            circle = [(r * math.cos(t), r * math.sin(t)) for t in np.linspace(0.0, math.tau, n + 1)[:-1]]
            circle.append((r * math.cos(math.tau - 2.0 / r), r * math.sin(math.tau - 2.0 / r)))
            line(*circle)

    def _rail_props(self):
        """Level crossings (a gate on the right of each approach of a paved road, a crossbuck beside it; a track gets
        the crossbuck only) and goods wagons standing on about half of the sidings."""
        self.rail_crossings = []
        rails = [r for r in self.roads if r.kind in ('rail', 'rail_side') and not r.bridge]
        if not rails:
            return
        for q in self.roads:
            if q.kind not in SIGN_ROADS or q.bridge:
                continue
            xs = []
            for r in rails:
                if q.bbox[0] <= r.bbox[2] and q.bbox[2] >= r.bbox[0] and q.bbox[1] <= r.bbox[3] and q.bbox[3] >= r.bbox[1]:
                    xs += _crossings(q.pts, r.pts)
            # the tracks one road crosses side by side are one level crossing: its gates stand outside the outer ones
            groups = []
            for X in sorted(xs):
                for g in groups:
                    if any((X[0] - Y[0]) ** 2 + (X[1] - Y[1]) ** 2 < 60.0 ** 2 for Y in g):
                        g.append(X)
                        break
                else:
                    groups.append([X])
            for g in groups:
                self.rail_crossings += g
                X = (sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g))
                _, _, dk = _project(q.pts, np.array([X]))
                dx, dy = float(dk[0, 0]), float(dk[0, 1])
                ext = max(abs((p[0] - X[0]) * dx + (p[1] - X[1]) * dy) for p in g)    # half the tracks' spread
                gantry = q.kind in GANTRY_ROADS and self.gen.data.is_dense(float(X[0]), float(X[1]))
                if True:
                    RAIL_OUT = RAIL_HALF + ext
                    for sg in (-1, 1):
                        tx, ty = -sg * dx, -sg * dy                 # travelling towards the crossing
                        nx, ny = ty, -tx                            # its right
                        # both models lie along their own y: the boom's arm reaches out along -y from its post, the
                        # gantry's beam spans y; model y = the road's right, so both reach across the road
                        ang = math.atan2(-nx, ny)
                        if q.kind in GATE_ROADS and gantry:
                            gx, gy = X[0] + sg * dx * (RAIL_OUT + 14.0), X[1] + sg * dy * (RAIL_OUT + 14.0)
                            self.spots.append(('rr_gantry', gx, gy, ang, 13, 6.0, True))
                        elif q.kind in GATE_ROADS:
                            off = q.width / 2 + 2.0                 # the post just off the right edge
                            gx = X[0] + sg * dx * (RAIL_OUT + 12.0) + nx * off
                            gy = X[1] + sg * dy * (RAIL_OUT + 12.0) + ny * off
                            self.spots.append(('rr_gate', gx, gy, ang, 13, 6.0, True))
                        else:                                       # a farm track: the crossbuck only
                            off = q.width / 2 + 6.0
                            sx = X[0] + sg * dx * (RAIL_OUT + 12.0) + nx * off
                            sy = X[1] + sg * dy * (RAIL_OUT + 12.0) + ny * off
                            self.spots.append(('rr_sign', sx, sy, math.atan2(-ty, -tx), 14, 6.0, True))
        for r in rails:
            if r.kind != 'rail_side':
                continue
            p = r.pts
            s_cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
            total = float(s_cum[-1])
            h = hash2(int(round(float(p[0, 0]))), int(round(float(p[0, 1]))), 45)
            if total < 200.0 or (h & 1):
                continue
            nw = min(14, int(0.7 * total / WAGON_PITCH))
            t0 = 0.15 * total
            kind = (h >> 1) % 3                                     # coal, tank or a mixed rake
            for i in range(nw):
                (x, y), d = _at(p, s_cum, t0 + (i + 0.5) * WAGON_PITCH)
                name = 'TrainCoalOrnament' if kind == 0 or (kind == 2 and (h >> (3 + i)) & 1) else 'TrainTankOrnament'
                self.spots.append((name, float(x), float(y), math.atan2(d[1], d[0]), 15, 20.0, True))

    def _land_cover(self, cls, by_kind, sj, si):
        """The map's land use checked against what the satellites see (ESA WorldCover, 10 m): a field polygon only
        stays a field where there is cropland (a village drawn inside one is green between its houses), cropland the
        map lacks becomes a field outside the town, bare ground is earth, and trees and shrubs stand where they are
        (gardens, copses, hedgerows) - read on the same wandering edge as the land use."""
        if self.wc is None:
            return
        wc = self.wc[sj, si]
        town = np.zeros(cls.shape, bool)
        for kind in TOWN_LAND:
            if kind in by_kind:
                town |= self.raster(by_kind[kind], vertices=False)
        known = wc != 0
        crops = wc == 40
        cls[(cls == G_FARM) & known & ~crops] = G_GRASS
        cls[(cls == G_GRASS) & crops & ~town] = G_FARM
        cls[(cls == G_GRASS) & (wc == 60)] = G_DIRT
        # trees and shrubs where they really are; in town a garden's trees, not a wood
        open_ground = (cls == G_GRASS) | (cls == G_FARM) | (cls == G_DIRT)
        td = self.tree_density
        tree = (wc == 10) & open_ground
        np.maximum(td, np.where(tree, np.where(town, 0.3, 0.62), 0.0).astype(np.float32), out=td)
        shrub = (wc == 20) & open_ground & (td < 0.45)
        td[shrub] = 0.5                                     # the scrub band of _chunk_trees: mostly bushes

    # ------------------------------------------------------------------------------------------ one chunk
    def payload(self, cx, cy):
        gen, g, n = self.gen, self.grid, self.grid.n
        i0, j0 = cx * n - self.gi0, cy * n - self.gj0
        ox, oy = g.chunk_origin(cx, cy)
        strings, index = [], {}

        def sid(s):
            if s not in index:
                index[s] = len(strings)
                strings.append(s)
            return index[s]

        heights = gen.z_of(self.H[j0:j0 + n, i0:i0 + n]).astype('<f4')
        ground = self.cls[j0:j0 + n, i0:i0 + n].astype(np.uint8)

        roads = self._chunk_roads(ox, oy, n, sid)
        objects = []
        if gen.opt['buildings']:
            objects += self._chunk_buildings(ox, oy, n, sid)
            objects += self._chunk_props(ox, oy, n, sid)
        if gen.opt['trees']:
            objects += self._chunk_trees(cx, cy, ox, oy, n, sid)
        water = self._chunk_water(cx, cy, ox, oy, n)

        out = [struct.pack('<4sHHiiHH', b'GXCK', VERSION, 0 if self.complete else 1, cx, cy, n, 0),
               struct.pack('<I', len(strings))]
        for s in strings:
            b = s.encode('utf-8')
            out.append(struct.pack('<H', len(b)) + b)
        out.append(heights.tobytes())
        out.append(ground.tobytes())
        out.append(struct.pack('<I', len(roads)))
        for type_id, rank, flags, width, pts in roads:
            out.append(struct.pack('<HBBfI', type_id, rank, flags, width, len(pts)))
            out.append(np.asarray(pts, '<f4').tobytes())
        out.append(struct.pack('<I', len(objects)))
        for tid, kind, flags, x, y, z, ang, scale, oid in objects:
            out.append(struct.pack('<HBBfffffQ', tid, kind, flags, x, y, z, ang, scale, oid))
        out.append(struct.pack('<I', len(water)))
        for z, flags, pts in water:
            out.append(struct.pack('<fB3xI', z, flags, len(pts)))
            out.append(np.asarray(pts, '<f4').tobytes())
        return b''.join(out)

    def _z_at(self, x, y):
        """World z of the finished ground at world x / y (arrays)."""
        return self.gen.z_of(bilinear(self.H, self.fx(x), self.fy(y)))

    def _off_pitch(self, P, mask=None):
        """The pieces of polyline P off the football pitches (or off another cell mask): its own points, and a cut
        point where it enters or leaves one (sampled every 5 units). Away from a pitch the points are exactly P's, so
        a chunk comes out the same whether its region holds the pitch or not."""
        mask = self.pitch_mask if mask is None else mask

        def inside(pt):
            ci, cj = int(math.floor(float(self.fx(pt[0])))), int(math.floor(float(self.fy(pt[1]))))
            return 0 <= ci < self.rw and 0 <= cj < self.rh and bool(mask[cj, ci])
        pieces, cur = [], []
        prev_in = inside(P[0])
        if not prev_in:
            cur.append(P[0])
        for a, b in zip(P[:-1], P[1:]):
            k = max(1, int(math.ceil(math.hypot(*(b - a)) / 5.0)))
            for t in range(1, k + 1):
                pt = a + (b - a) * (t / k)
                now_in = inside(pt)
                if now_in and not prev_in:
                    if t > 1:
                        cur.append(a + (b - a) * ((t - 1) / k))       # the last point off the pitch
                    pieces.append(cur)
                    cur = []
                elif not now_in and prev_in:
                    cur = [pt]                                         # the first point off it again
                elif not now_in and t == k:
                    cur.append(b)
                prev_in = now_in
        if cur:
            pieces.append(cur)
        return [np.asarray(pc) for pc in pieces if len(pc) >= 2]

    # ---- road ribbons, cut exactly at the chunk edge
    def _chunk_roads(self, ox, oy, n, sid):
        size = n * CELL_UNITS
        out = []
        for r in self.roads:                                   # minor first: major roads are drawn on top
            if r.bridge and id(r) not in getattr(self, 'culvert_ways', ()):
                continue                                       # the decks are made by _bridges (below)
            if r.kind == 'parking_aisle':
                continue                                       # the car park's asphalt; its bays are the markings
            if not (r.bbox[0] <= ox + size and r.bbox[2] >= ox and r.bbox[1] <= oy + size and r.bbox[3] >= oy):
                continue
            pieces = [r.pts]
            if r.kind in PITCH_HIDDEN and self.pitch_mask is not None:
                pieces = self._off_pitch(r.pts)
            pm = self.prop_masks.get('parking')
            if r.kind in PARK_HIDDEN and pm is not None:
                # a service road on a car park is its asphalt: only the bay lines are drawn there
                pieces = [q for pc in pieces for q in self._off_pitch(pc, pm)]
            runs = [run for pc in pieces for run in geo.clip_polyline((pc - (ox, oy)).tolist(), size, size)]
            for run in runs:
                run = np.asarray(run, float)
                if len(run) < 2:
                    continue
                seg = np.hypot(*np.diff(run, axis=0).T)
                if seg.sum() < 1.0:
                    continue
                # a point at least every cell, so the ribbon follows the ground
                dense = [run[0]]
                for a, b, ln in zip(run[:-1], run[1:], seg):
                    k = max(1, int(math.ceil(ln / CELL_UNITS)))
                    dense += [a + (b - a) * (t / k) for t in range(1, k + 1)]
                p = np.asarray(dense)
                d = np.diff(p, axis=0)
                ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
                nrm = np.stack([-d[:, 1] / ln, d[:, 0] / ln], axis=1)
                vn = np.empty_like(p)
                vn[0], vn[-1] = nrm[0], nrm[-1]
                vn[1:-1] = nrm[:-1] + nrm[1:]
                vl = np.maximum(np.hypot(vn[:, 0], vn[:, 1]), 1e-9)
                vn /= vl[:, None]
                # mitre: keep the width through a bend, but never spike on a hairpin
                cosh = np.ones(len(p))
                cosh[1:-1] = np.clip((vn[1:-1] * nrm[:-1]).sum(1), 0.5, 1.0)
                hw = (r.width / 2) / cosh
                left, right = p + vn * hw[:, None], p - vn * hw[:, None]
                s = np.concatenate([[0.0], np.cumsum(ln)])
                if r.bridge:
                    # the deck: a straight line between the ground at the bridge's two ends (on the banks), whichever
                    # chunk this piece of it is in; the type is the Roads.ini bridge the game builds for this road
                    a, b = r.pts[0], r.pts[-1]
                    za, zb = (float(v) for v in self._z_at(np.array([a[0], b[0]]), np.array([a[1], b[1]])))
                    ab = b - a
                    t = np.clip(((p[:, 0] + ox - a[0]) * ab[0] + (p[:, 1] + oy - a[1]) * ab[1]) / max(float(ab @ ab), 1e-9), 0, 1)
                    lz = rz = za + (zb - za) * t
                    type_id = sid(gconst.BRIDGE.get(r.rtype, 'EuropeanBridgeAsphalt'))
                else:
                    lz = self._z_at(left[:, 0] + ox, left[:, 1] + oy)
                    rz = self._z_at(right[:, 0] + ox, right[:, 1] + oy)
                    type_id = sid(r.rtype)
                rec = np.stack([left[:, 0], left[:, 1], lz, right[:, 0], right[:, 1], rz, s], axis=1)
                out.append((type_id, min(r.rank, 255), ROAD_FLAG_BRIDGE if r.bridge else 0, float(r.width), rec))
        # bridge decks: their own line (the middle of a bridge outline, or the road), height and true width
        for b in getattr(self, 'bridge_specs', ()):
            P = b['pts']
            if not (P[:, 0].min() <= ox + size and P[:, 0].max() >= ox and P[:, 1].min() <= oy + size and P[:, 1].max() >= oy):
                continue
            Z = self.gen.z_of(b['z'])
            type_id = sid(b.get('tmpl') or STREAM_BRIDGE.get(b['rtype']) or gconst.BRIDGE.get(b['rtype'], 'EuropeanBridgeAsphalt'))
            for run in geo.clip_polyline((P - (ox, oy)).tolist(), size, size):
                run = np.asarray(run, float)
                if len(run) < 2 or np.hypot(*(run[-1] - run[0])) < 1.0:
                    continue
                # each point's deck height: that of the nearest sample of the deck line
                d2 = ((run[:, None, 0] + ox - P[None, :, 0]) ** 2 + (run[:, None, 1] + oy - P[None, :, 1]) ** 2)
                z = Z[np.argmin(d2, axis=1)]
                d = np.diff(run, axis=0)
                ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
                nrm = np.stack([-d[:, 1] / ln, d[:, 0] / ln], axis=1)
                vn = np.empty_like(run)
                vn[0], vn[-1] = nrm[0], nrm[-1]
                vn[1:-1] = nrm[:-1] + nrm[1:]
                vn /= np.maximum(np.hypot(vn[:, 0], vn[:, 1]), 1e-9)[:, None]
                hw = b['width'] / 2
                left, right = run + vn * hw, run - vn * hw
                s = np.concatenate([[0.0], np.cumsum(ln)])
                rec = np.stack([left[:, 0], left[:, 1], z, right[:, 0], right[:, 1], z, s], axis=1)
                out.append((type_id, min(b['rank'], 255), ROAD_FLAG_BRIDGE, float(b['width']), rec))
        # painted lines: whole, in the chunk that holds the middle of their bounding box (the road buffer drapes them on
        # the ground; only their points matter, so they are not densified - no point for another line to snap to)
        for rtype, width, P in getattr(self, 'marks', ()):
            mx = (P[:, 0].min() + P[:, 0].max()) / 2
            my = (P[:, 1].min() + P[:, 1].max()) / 2
            if not (ox <= mx < ox + size and oy <= my < oy + size):
                continue
            p = P - (ox, oy)
            d = np.diff(p, axis=0)
            ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
            nrm = np.stack([-d[:, 1] / ln, d[:, 0] / ln], axis=1)
            vn = np.empty_like(p)
            vn[0], vn[-1] = nrm[0], nrm[-1]
            vn[1:-1] = nrm[:-1] + nrm[1:]
            vn /= np.maximum(np.hypot(vn[:, 0], vn[:, 1]), 1e-9)[:, None]
            left, right = p + vn * width / 2, p - vn * width / 2
            lz = self._z_at(left[:, 0] + ox, left[:, 1] + oy)
            rz = self._z_at(right[:, 0] + ox, right[:, 1] + oy)
            s = np.concatenate([[0.0], np.cumsum(ln)])
            rec = np.stack([left[:, 0], left[:, 1], lz, right[:, 0], right[:, 1], rz, s], axis=1)
            out.append((sid(rtype), MARK_RANK, 0, float(width), rec))
        return out

    # ---- buildings whose centre is in this chunk
    def _classify(self, b, L, W):
        gen, upm = self.gen, self.grid.upm
        area_m2 = b.area / upm ** 2
        if getattr(b, 'block', False) or id(b) in getattr(self, 'panel_ids', ()):
            return 'apt'                                    # sections of a block of flats
        flats = self._flats_shape(b, L, W) or self._estate_tower(b, L, W)
        for kind, px, py in self.data.pois:
            if b.bbox[0] <= px <= b.bbox[2] and b.bbox[1] <= py <= b.bbox[3] and \
                    geo.point_in_poly(px, py, b.ring.tolist()):
                if flats and kind not in ('civic', 'church', 'hospital', 'fuel'):
                    return 'apt'                            # a pub or a shop on the ground floor of a block of flats
                return kind                                 # ('fuel': a petrol station, see _gas_station)
        ci, cj = int(self.fx(b.cx)), int(self.fy(b.cy))
        ctx = None
        if 0 <= ci < self.rw and 0 <= cj < self.rh:
            for name, m in self.context.items():
                if m[cj, ci]:
                    ctx = name
                    if name != 'res':
                        break
        if area_m2 < 28:
            return 'shed'
        if ctx == 'industrial':
            return 'industrial' if area_m2 > 120 else 'shed'
        if ctx == 'farm':
            return 'farm' if area_m2 < 400 else 'farmhall'
        if ctx == 'shop':
            return 'shop' if area_m2 > 250 else 'house'
        if ctx == 'shed':
            return 'shed'
        if ctx in ('civic', 'hospital') and area_m2 > 200:
            return ctx
        # a block of flats (the map generator's rule): 4+ floors, or a big deep footprint
        if flats and ctx != 'farm':
            return 'apt'
        if area_m2 > 900:
            return 'industrial'
        if area_m2 < 55 and ctx != 'res':
            return 'shed'
        return 'house'

    def _apt_pieces(self, cx, cy, L, W, th):
        """A block of flats, everywhere the same square-cut system buildings (the map generator's _apt_row): a short
        deep block (a tower, up to 22 m) is one CivilianHighrise02; a longer one is a row of sections touching end to
        end, one per entrance (~12.5 m): an odd count of one-entrance AsiaApartment02, an even count as half as many
        two-entrance AsianApartmentSingle01. -> [(name, x, y, angle)] or None."""
        upm, models = self.grid.upm, self.gen.models
        Lm, Wm = L / upm, W / upm
        if Lm <= 22 and Wm >= 9 and L / max(W, 1e-3) < 1.8 and models.has('CivilianHighrise02'):
            name, n = 'CivilianHighrise02', 1
        else:
            ent = max(1, int(round(Lm / 12.5)))
            name, n = ('AsiaApartment02', ent) if ent % 2 else ('AsianApartmentSingle01', ent // 2)
            if not models.has(name):
                name, n = 'AsiaApartment02', ent
            if not models.has(name):
                return None
        mx, my = models.sizes[name]
        ml = max(mx, my)
        mang = th if mx >= my else th + math.pi / 2
        c, s = math.cos(th), math.sin(th)
        step = ml * 0.9                     # a little overlap: the models have empty margins
        return [(name, cx + c * (i - (n - 1) / 2) * step, cy + s * (i - (n - 1) / 2) * step, mang) for i in range(n)]

    def _gas_station(self, cx, cy, L, W, th, seed):
        """A petrol station: the game's station that best fits the room (the mapped shop and its forecourt, about
        three times the shop), whatever the country; its long side along the street. -> [(name, x, y, angle)]"""
        models = self.gen.models
        target = max(L * W * 3.0, 1800.0)
        best = None
        for k, (name, fb) in enumerate(GAS_STATIONS):
            if not models.has_object(name):
                continue
            mx, my = models.sizes.get(name, fb)
            sc = abs(math.log(mx * my / target)) + ((seed >> (k * 3)) & 7) * 0.03
            if best is None or sc < best[0]:
                best = (sc, name, mx, my)
        if best is None:
            return None
        _, name, mx, my = best
        front = self._front(cx, cy, L, W, th)
        if front is not None:
            ux, uy, half = front[2], front[3], front[4]
            along = math.atan2(ux, -uy)                             # the street's direction
            back = max(0.0, min(mx, my) / 2 - half)                 # a station deeper than the shop: off the street
            cx, cy = cx - ux * back, cy - uy * back
        else:
            along = th
        return [(name, cx, cy, along if mx >= my else along + math.pi / 2)]

    def _fuel_points(self):
        """A petrol station mapped as a point only (no building): the medium station beside the nearest road."""
        upm = self.grid.upm
        names = [n for n, _ in GAS_STATIONS[:3] if self.gen.models.has_object(n)]
        if not names:
            return
        for kind, px, py in self.data.pois:
            if kind != 'fuel':
                continue
            if any(b.bbox[0] <= px <= b.bbox[2] and b.bbox[1] <= py <= b.bbox[3] and
                   geo.point_in_poly(px, py, b.ring.tolist()) for b, _ in self.brects):
                continue                                            # in a building: _gas_station builds it
            best = None
            for r in self.roads:
                if r.kind not in GATE_ROADS or r.bridge or not (r.bbox[0] - 200 <= px <= r.bbox[2] + 200 and
                                                               r.bbox[1] - 200 <= py <= r.bbox[3] + 200):
                    continue
                dist, side, dk = _project(r.pts, np.array([[px, py]]))
                if dist[0] < 200 and (best is None or dist[0] < best[0]):
                    best = (float(dist[0]), float(side[0]), dk[0], r)
            if best is None:
                continue
            dist, side, dk, r = best
            name = names[hash2(int(px), int(py), 47) % len(names)]
            mx, my = dict(GAS_STATIONS)[name]
            mx, my = self.gen.models.sizes.get(name, (mx, my))
            nx, ny = -dk[1], dk[0]                                  # left of the road
            sg = 1.0 if side >= 0 else -1.0
            want = r.width / 2 + min(mx, my) / 2 + 6.0              # beside the road, never on it
            foot_x, foot_y = px - nx * side, py - ny * side
            x, y = foot_x + nx * sg * max(want, abs(side)), foot_y + ny * sg * max(want, abs(side))
            along = math.atan2(dk[1], dk[0])
            self.spots.append((name, float(x), float(y), along if mx >= my else along + math.pi / 2, 1, 60.0, True))

    def _flats_shape(self, b, L, W):
        """The shape of a block of flats: 4+ floors, or a big deep footprint (the map generator's rule), or long and
        deep (a house is seldom 24 m long)."""
        upm = self.grid.upm
        Lm, Wm, area_m2 = max(L, W) / upm, min(L, W) / upm, b.area / upm ** 2
        return (b.height_m >= 11.5 or (b.height_m == 0 and area_m2 > 260 and Wm > 9) or
                ((b.height_m == 0 or b.height_m >= 8) and Lm >= 24 and Wm >= 9))

    def _estate_tower(self, b, L, W):
        """A tower block of a housing estate (a 'bodovka', ~19 x 12 m, often mapped without its floors): a short deep
        footprint of 180 m2 and more, with another block of flats within 150 m - a villa on its own stays a house."""
        upm = self.grid.upm
        Lm, Wm, area_m2 = max(L, W) / upm, min(L, W) / upm, b.area / upm ** 2
        if not (area_m2 >= 180 and Wm >= 10.5 and Lm <= 22 and (b.height_m == 0 or b.height_m >= 8)):
            return False
        r2 = (150.0 * upm) ** 2
        return any((x - b.cx) ** 2 + (y - b.cy) ** 2 < r2 for x, y, o in getattr(self, 'flats_pts', ()) if o is not b)

    def _pick_model(self, cls, L, W, seed, dense=False):
        """Best fitting model of a class for an L x W footprint: (name, mx, my) or None. In a dense big city the pool
        is every region's models of the class and the city blocks, so a street is not one model repeated."""
        gen = self.gen
        best = None
        for c in [cls] + gconst.FALLBACK.get(cls, []) + (['house', 'shed'] if cls not in ('house', 'shed') else []):
            pool = gen.dense_models.get(c, []) if dense else gen.style_models.get(c, [])
            for k, name in enumerate(pool):
                mx, my = gen.models.sizes[name]
                ml, mw = max(mx, my), min(mx, my)
                loose = 1.35 if c == 'church' else 1.0               # a church: its own model even a size up
                if ml > (L * 1.45 + 6) * loose or mw > (W * 1.6 + 6) * loose:
                    continue                                         # never a model far bigger than the plot
                score = abs(math.log(ml / L)) + abs(math.log(mw / W)) + \
                    ((seed >> (k % 16)) & 7) * (0.07 if dense else 0.02)  # a big city: more variety than fit
                if best is None or score < best[0]:
                    best = (score, name, mx, my)
            if best is not None:
                break
        return None if best is None else best[1:]

    def _front(self, cx, cy, L, W, th):
        """The side of a building that faces its street: (cx, cy, outward unit normal x / y, distance from the
        centre to that side, the side's length), or None when no road is near any side."""
        c, s = math.cos(th), math.sin(th)
        best = None
        for ux, uy, half, along in ((c, s, L / 2, W), (-c, -s, L / 2, W), (-s, c, W / 2, L), (s, -c, W / 2, L)):
            w = 0.0
            for dd in (half + 20.0, half + 32.0):
                ci, cj = int(math.floor(float(self.fx(cx + ux * dd)))), int(math.floor(float(self.fy(cy + uy * dd))))
                if 0 <= ci < self.rw and 0 <= cj < self.rh:
                    w = max(w, float(self.road_w[cj, ci]))
            if w > 0.05 and (best is None or w > best[0]):
                best = (w, (cx, cy, ux, uy, half, along))
        return None if best is None else best[1]

    def _plot_line(self, cx, cy, ux, uy, half):
        """Where a front fence stands: walking out from the wall, the last point before the paving of the street (or
        of its pavement) that is off the house's own pad; None when there is no room between the two."""
        start = end = None
        for dd in np.arange(half + 3.0, half + 44.0, 2.0):
            x, y = cx + ux * dd, cy + uy * dd
            ci, cj = int(math.floor(float(self.fx(x)))), int(math.floor(float(self.fy(y))))
            if not (0 <= ci < self.rw and 0 <= cj < self.rh):
                return None
            if self.road_surface[cj, ci] != 255:
                break
            if start is None and self.pad_w[cj, ci] < 0.999:              # out of the house's level pad
                start = dd
            if start is not None:
                end = dd
        if start is None or end is None:
            return None
        return float(max(start, end - 1.0))

    def _chunk_buildings(self, ox, oy, n, sid):
        size = n * CELL_UNITS
        out = []
        g = self.grid
        for b, (cx, cy, L, W, th) in self.brects:
            if not (ox <= b.cx < ox + size and oy <= b.cy < oy + size):
                continue
            bj, bi = int(round(float(self.fy(b.cy)))), int(round(float(self.fx(b.cx))))
            if self.water_mask[bj, bi]:
                continue                                              # a houseboat, a pontoon: not a house in the river
            if getattr(self, 'deck_mask', None) is not None and self.deck_mask[bj, bi]:
                continue                                              # mapped on (or under) a bridge deck
            lat, lon = g.from_units(b.cx, b.cy)
            base_id = ((int(round(float(lat) * 1e5)) & 0xFFFFFFF) << 28) | (int(round(float(lon) * 1e5)) & 0xFFFFFFF)
            seed = hash2(int(round(float(lat) * 1e5)), int(round(float(lon) * 1e5)), 7)
            cls = self._classify(b, L, W)
            special = self._apt_pieces(cx, cy, L, W, th) if cls == 'apt' else \
                self._gas_station(cx, cy, L, W, th, seed) if cls == 'fuel' else None
            if special:
                for k, (name, x, y, ang) in enumerate(special):
                    z = float(self._z_at(np.array([x]), np.array([y]))[0])
                    oid = (base_id << 8) | (k & 0xFF)
                    out.append((sid(name), KIND_BUILDING, 0, x - ox, y - oy, z, ang % math.tau, 1.0,
                                oid & 0xFFFFFFFFFFFFFFFF))
                continue
            if cls == 'fuel':
                cls = 'shop'
            pick = self._pick_model(cls, L, W, seed, dense=getattr(b, 'dense', False))
            if pick is None:
                continue
            name, mx, my = pick
            ml, mw = max(mx, my), min(mx, my)
            # a statue in front of a church; a car beside one house in four, a van at one shop in six
            spots = self.__dict__.setdefault('_yard_spots', {}).setdefault((ox, oy), [])
            c0, s0 = math.cos(th), math.sin(th)
            if cls == 'church':
                d = W / 2 + 16.0
                spots.append((cx - s0 * d, cy + c0 * d, th, 'church'))
            elif (cls == 'house' and (seed >> 3) % 4 == 0) or (cls == 'shop' and (seed >> 3) % 6 == 0):
                side = 1 if (seed >> 6) & 1 else -1
                d = L / 2 + 1.6 * g.upm + 5.0
                spots.append((cx + c0 * d * side, cy + s0 * d * side, th + math.pi / 2 + (math.pi if (seed >> 7) & 1 else 0), cls))
            front = self._front(cx, cy, L, W, th) if cls in ('house', 'shop') else None
            if front is not None:
                fx_, fy_, ux, uy, half, along = front                # outward normal, distance, side length
                dist = self._plot_line(cx, cy, ux, uy, half) if cls == 'house' and (seed >> 9) % 4 != 0 else None
                if dist is not None:
                    # a front fence on the plot's street line: pieces end to end parallel to the street side, a gap
                    # for the gate in the middle
                    pick, acc = (seed >> 11) % 100, 0
                    for tmpl, plen, share in FENCES:
                        acc += share
                        if pick < acc:
                            break
                    span = along + 12.0
                    npc = max(1, int(round(span / plen + 0.4)))
                    for k in range(npc):
                        if npc >= 3 and k == npc // 2:
                            continue
                        t = (k - (npc - 1) / 2) * plen
                        px, py = cx + ux * dist - uy * t, cy + uy * dist + ux * t
                        spots.append((px, py, math.atan2(ux, -uy), 'fence', tmpl))
                elif cls == 'shop':
                    dist = half + 6.0
                    t = along / 3 * (1 if (seed >> 9) & 1 else -1)
                    spots.append((cx + ux * dist - uy * t, cy + uy * dist + ux * t, math.atan2(uy, ux), 'bins'))
                    if (seed >> 10) % 3 == 0:
                        spots.append((cx + ux * dist + uy * t, cy + uy * dist - ux * t, math.atan2(ux, -uy), 'bikes'))
            # a footprint much longer / wider than the model becomes a row (or a grid) of models
            nl = max(1, min(8, int(round(L / ml)))) if L > ml * 1.6 else 1
            nw = max(1, min(4, int(round(W / mw)))) if W > mw * 1.7 else 1
            ang = th if mx >= my else th + math.pi / 2
            if seed & 1:
                ang += math.pi
            c, s = math.cos(th), math.sin(th)
            for a in range(nl):
                for bb in range(nw):
                    u = (a - (nl - 1) / 2) * (L / nl)
                    v = (bb - (nw - 1) / 2) * (W / nw)
                    x, y = cx + u * c - v * s, cy + u * s + v * c
                    z = float(self._z_at(np.array([x]), np.array([y]))[0])
                    oid = (base_id << 8) | ((a * 4 + bb) & 0xFF)
                    out.append((sid(name), KIND_BUILDING, 0, x - ox, y - oy, z, ang % math.tau, 1.0,
                                oid & 0xFFFFFFFFFFFFFFFF))
        return out

    # ---- props of the living town: graves, playgrounds, benches, cars on car parks, at the kerb and in yards
    def _prop_free(self, x, y, road_ok=False, pad_max=0.6, road_max=0.05):
        """A prop may stand at world x / y: dry, level enough, off buildings (and off roads unless road_ok; a fence
        may stand on a road's verge, road_max). Right beside its own building (pad_max near 1) only the footprint of
        a building is off limits."""
        ci, cj = int(math.floor(float(self.fx(x)))), int(math.floor(float(self.fy(y))))
        if not (0 <= ci < self.rw and 0 <= cj < self.rh):
            return False
        if self.wet[cj, ci] or self.slope[cj, ci] > 0.35 or self.pad_w[cj, ci] > pad_max or self.deck_mask[cj, ci]:
            return False
        return road_ok or self.road_w[cj, ci] < road_max

    def _in_mask(self, kind, x, y):
        m = self.prop_masks.get(kind)
        ci, cj = int(math.floor(float(self.fx(x)))), int(math.floor(float(self.fy(y))))
        return m is not None and 0 <= ci < self.rw and 0 <= cj < self.rh and bool(m[cj, ci])

    def _chunk_props(self, ox, oy, n, sid):
        gen, upm = self.gen, self.grid.upm
        P = gen.props
        size = n * CELL_UNITS
        out, taken = [], {}

        def add(names, x, y, ang, salt, gap=10.0, road_ok=False, pad_max=0.6, road_max=0.05):
            """One prop from `names` (picked by a hash of where it stands), if it is in this chunk and has room."""
            if not names or not (ox <= x < ox + size and oy <= y < oy + size) or \
                    not self._prop_free(x, y, road_ok, pad_max, road_max):
                return False
            key = (int(x // 16), int(y // 16))
            for kx in (key[0] - 1, key[0], key[0] + 1):            # two pieces of one area: one prop, not two
                for ky in (key[1] - 1, key[1], key[1] + 1):
                    for px, py in taken.get((kx, ky), ()):
                        if (px - x) ** 2 + (py - y) ** 2 < gap * gap:
                            return False
            taken.setdefault(key, []).append((x, y))
            ix, iy = int(round(x)), int(round(y))
            h = hash2(ix, iy, 40 + salt)
            z = float(self._z_at(np.array([x]), np.array([y]))[0])
            oid = (3 << 60) | ((salt & 0xF) << 52) | ((ix & 0x3FFFFFF) << 26) | (iy & 0x3FFFFFF)
            out.append((sid(names[h % len(names)]), KIND_PROP, 0, x - ox, y - oy, z, ang % math.tau, 1.0, oid))
            return True

        def frame(a):
            """An area's own axes: (cx, cy, L, W, angle) of its outline's smallest rectangle, the angle in 5 degrees."""
            mr = geo.min_rect([tuple(p) for p in a.rings[0].tolist()])
            if mr is None:
                return None
            cx, cy, L, W, th = mr
            return cx, cy, L, W, round(th / math.radians(5)) * math.radians(5)

        def lattice(a, th, du, dv, salt):
            """Points of a lattice on the area's axes, anchored to the world (the same for every piece of the area),
            that fall in this chunk."""
            c, s = math.cos(th), math.sin(th)
            b = a.bbox
            if b[0] > ox + size or b[2] < ox or b[1] > oy + size or b[3] < oy:
                return []
            xs = [max(b[0], ox), min(b[2], ox + size)]
            ys = [max(b[1], oy), min(b[3], oy + size)]
            us = [x * c + y * s for x in xs for y in ys]
            vs = [-x * s + y * c for x in xs for y in ys]
            pts = []
            for iu in range(int(math.floor(min(us) / du)), int(math.ceil(max(us) / du)) + 1):
                for iv in range(int(math.floor(min(vs) / dv)), int(math.ceil(max(vs) / dv)) + 1):
                    u, v = iu * du, iv * dv
                    pts.append((u * c - v * s, u * s + v * c, iu, iv))
            return pts

        # what the region laid out (car park bays, pitches, level crossings, wagons): placed where they fall
        for name, x, y, ang, salt, gap, road_ok in getattr(self, 'spots', ()):
            if ox <= x < ox + size and oy <= y < oy + size:
                names = P.get(name)
                if names is None:
                    names = [name] if gen.models.has_object(name) else []
                add(names, x, y, ang, salt, gap=gap, road_ok=road_ok)
        areas = getattr(self, 'prop_areas', {})
        # cemeteries: rows of graves on the cemetery's axes, a crypt in the middle of a big one
        for kind in ('cemetery', 'grave_yard'):
            for a in areas.get(kind, ()):
                f = frame(a)
                if f is None:
                    continue
                cx, cy, L, W, th = f
                if L * W > 1500 * upm * upm and self._in_mask(kind, cx, cy):
                    add(P['crypt'], cx, cy, th, 1, gap=40.0)
                for x, y, iu, iv in lattice(a, th, 30.0, 36.0, 2):
                    if self._in_mask(kind, x, y) and (hash2(iu, iv, 41) & 7) != 0:      # a few empty plots
                        add(P['graves'], x, y, th, 2, gap=14.0)
        # playgrounds: the climbing frame in the middle, a swing beside it
        for a in areas.get('playground', ()):
            f = frame(a)
            if f is None or f[2] * f[3] < 80 * upm * upm:
                continue
            cx, cy, L, W, th = f
            if self._in_mask('playground', cx, cy):
                add(P['play'][:1], cx, cy, th, 3, gap=30.0)
                if L > 60:
                    add(P['play'][1:], cx + math.cos(th) * 30, cy + math.sin(th) * 30, th, 4, gap=16.0)
        # parks: benches around the middle, a fountain in a big one
        for a in areas.get('park', ()):
            f = frame(a)
            if f is None:
                continue
            cx, cy, L, W, th = f
            if L * W < 1500 * upm * upm or not self._in_mask('park', cx, cy):
                continue
            if L * W > 6000 * upm * upm:
                add(P['fountain'], cx, cy, 0.0, 5, gap=30.0)
            for k in range(4):
                t = th + k * math.pi / 2
                bx, by = cx + math.cos(t) * 45, cy + math.sin(t) * 45
                if self._in_mask('park', bx, by):
                    add(P['bench'], bx, by, t + math.pi / 2, 6)
        # (car parks: laid out with their bay lines by _park_marks, placed above)
        town = None
        for ctx in ('res', 'shop', 'civic'):
            m = self.context.get(ctx)
            if m is not None:
                town = m if town is None else (town | m)
        # street lamps: town streets, one every 35 m, sides taking turns, one model along a street
        if town is not None and P['lamps']:
            for r in self.roads:
                if r.kind not in LAMP_ROADS or r.bridge:
                    continue
                if not (r.bbox[0] <= ox + size + 40 and r.bbox[2] >= ox - 40 and r.bbox[1] <= oy + size + 40 and
                        r.bbox[3] >= oy - 40):
                    continue
                p = r.pts
                s_cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
                total = float(s_cum[-1])
                if total < 100.0:
                    continue
                hr = hash2(int(round(float(p[0, 0]))) // 8, int(round(float(p[0, 1]))) // 8, 46)
                names = [P['lamps'][hr % len(P['lamps'])]]
                step = 35.0 * upm
                for k in range(int((total - 60.0) // step) + 1):
                    (x0, y0), d = _at(p, s_cum, 30.0 + k * step)
                    side = 1 if (k + hr) & 1 else -1
                    # behind the pavement (its ribbon ends ~7 units past the carriageway's drawn edge)
                    off = max(r.width, DRAWN_WIDTH.get(r.rtype, r.width)) / 2 + DRAWN_WIDTH['Sidewalk'] + 4.0
                    x, y = x0 - d[1] * off * side, y0 + d[0] * off * side
                    ci, cj = int(self.fx(x)), int(self.fy(y))
                    if not (0 <= ci < self.rw and 0 <= cj < self.rh) or not town[cj, ci]:
                        continue
                    add(names, float(x), float(y), math.atan2(-d[0] * side, d[1] * side), 11, gap=30.0,
                        road_ok=True)
        # at the kerb: town streets, a car every so often on either side, along the street
        if town is not None and P['cars']:
            for r in self.roads:
                if r.kind not in PROP_KERB_ROADS or r.bridge:
                    continue
                if not (r.bbox[0] <= ox + size and r.bbox[2] >= ox and r.bbox[1] <= oy + size and r.bbox[3] >= oy):
                    continue
                p = _densify(r.pts, 22.0)
                if len(p) < 3 or _length(p) < 120.0:
                    continue
                s_cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
                total = s_cum[-1]
                for k in range(len(p) - 1):
                    if s_cum[k] < 40.0 or s_cum[k] > total - 40.0:
                        continue                                   # not in a junction
                    a_, b_ = p[k], p[k + 1]
                    d = b_ - a_
                    ln = math.hypot(*d)
                    if ln < 1e-6:
                        continue
                    ux, uy = d / ln
                    mx, my = (a_ + b_) / 2
                    h = hash2(int(round(mx)) // 20, int(round(my)) // 20, 43)
                    if (h & 0xFF) >= 34:                           # ~13 %
                        continue
                    side = 1 if (h >> 9) & 1 else -1
                    # at the kerb: on the carriageway's edge (the game draws it this wide), off the pavement
                    off = max(r.width, DRAWN_WIDTH.get(r.rtype, r.width)) / 2 - 4.5
                    x, y = mx - uy * off * side, my + ux * off * side
                    ci, cj = int(self.fx(x)), int(self.fy(y))
                    if not (0 <= ci < self.rw and 0 <= cj < self.rh) or not town[cj, ci]:
                        continue
                    if any((x - X[0]) ** 2 + (y - X[1]) ** 2 < 60.0 ** 2 for X in getattr(self, 'rail_crossings', ())):
                        continue                                   # never parked on a level crossing
                    ang = math.atan2(uy, ux) + (0.0 if side > 0 else math.pi)
                    add(P['cars'], x, y, ang, 8, gap=22.0, road_ok=True)
        # yards and churches (from the buildings of this chunk)
        for spot in getattr(self, '_yard_spots', {}).get((ox, oy), ()):
            x, y, ang, kind = spot[:4]
            if kind == 'church':
                add(P['statue'], x, y, ang, 9, gap=20.0, pad_max=0.97)
            elif kind == 'fence':
                ci, cj = int(math.floor(float(self.fx(x)))), int(math.floor(float(self.fy(y))))
                if gen.models.has_object(spot[4]) and 0 <= ci < self.rw and 0 <= cj < self.rh and \
                        self.road_surface[cj, ci] == 255:                       # on the verge, never on the paving
                    add([spot[4]], x, y, ang, 0, gap=6.0, pad_max=0.97, road_ok=True)
            elif kind in ('bins', 'bikes'):
                add(P[kind], x, y, ang, 10, gap=8.0, pad_max=0.97, road_max=0.9)
            else:
                add(P['vans'] if kind == 'shop' else P['cars'], x, y, ang, 10, gap=20.0, pad_max=0.97)
        return out

    # ---- trees on a world-wide jittered lattice
    def _chunk_trees(self, cx, cy, ox, oy, n, sid):
        gen = self.gen
        sp = float(gen.opt['tree_spacing'])
        size = n * CELL_UNITS
        a0, a1 = int(math.floor(ox / sp)), int(math.ceil((ox + size) / sp))
        b0, b1 = int(math.floor(oy / sp)), int(math.ceil((oy + size) / sp))
        A, B = np.meshgrid(np.arange(a0, a1), np.arange(b0, b1))
        A, B = A.ravel(), B.ravel()
        h1, h2, h3 = hash2(A, B, 1), hash2(A, B, 2), hash2(A, B, 3)
        x = (A + (h1 & 0xFFFF) / 65536.0) * sp
        y = (B + (h2 & 0xFFFF) / 65536.0) * sp
        inside = (x >= ox) & (x < ox + size) & (y >= oy) & (y < oy + size)
        ci = np.clip(self.fx(x).astype(np.int64), 0, self.rw - 1)
        cj = np.clip(self.fy(y).astype(np.int64), 0, self.rh - 1)
        dens = self.tree_density[cj, ci]
        keep = inside & (((h3 >> 8) & 0xFFFF) / 65536.0 < dens)
        keep &= (self.road_w[cj, ci] < 0.05) & (self.pad_w[cj, ci] < 0.05) & ~self.wet[cj, ci] & (self.slope[cj, ci] < 0.9)
        keep &= ~self.deck_mask[cj, ci] if getattr(self, 'deck_mask', None) is not None else True
        street = self._street_trees(ox, oy, size, sid)
        if not keep.any():
            return street
        x, y, h3, dens = x[keep], y[keep], h3[keep], dens[keep]
        z = self._z_at(x, y)
        elev = bilinear(self.H, self.fx(x), self.fy(y))
        # more conifers with altitude (Europe); bushes where the cover is scrub
        conifer_share = np.clip((elev - 450.0) / 700.0, 0.08, 0.92) if gen.style == 'eu' else np.full(len(x), 0.1)
        out = []
        for k in range(len(x)):
            hv = int(h3[k])
            r = ((hv >> 3) & 0xFF) / 255.0
            if 0.45 < dens[k] < 0.56 and (hv & 3):                       # scrub: mostly bushes
                names = gen.bushes
            elif dens[k] < 0.16 and (hv & 1):                            # gardens and town green: half are shrubs
                names = gen.bushes
            else:
                names = gen.trees_conifer if r < conifer_share[k] else gen.trees_broad
            name = names[(hv >> 12) % len(names)]
            scale = 0.85 + ((hv >> 20) & 0xFF) / 255.0 * 0.5
            ang = ((hv >> 4) & 0xFFF) / 4096.0 * math.tau
            oid = (int(round(x[k])) & 0xFFFFFFF) << 28 | (int(round(y[k])) & 0xFFFFFFF)
            out.append((sid(name), KIND_TREE, 0, float(x[k] - ox), float(y[k] - oy), float(z[k]), ang, scale,
                        (1 << 63) | oid))
        return out + street

    STREET_TREE_KINDS = {'residential', 'living_street', 'tertiary', 'secondary', 'primary', 'pedestrian',
                         'street_marked', 'street_unmarked', 'unclassified'}

    def _street_trees(self, ox, oy, size, sid):
        """Avenues: town streets lined with trees every ~12 m on both sides, where the street runs through housing,
        shops or civic ground and there is room (no building, no other road, no water, no bridge)."""
        gen = self.gen
        names = [t for t in gconst.STREET_TREES if t in gen.trees_broad] or gen.trees_broad
        town = None
        for ctx in ('res', 'shop', 'civic'):
            m = self.context.get(ctx)
            if m is not None:
                town = m if town is None else (town | m)
        if town is None:
            return []
        out = []
        step = 48.0
        for r in self.roads:
            if r.kind not in self.STREET_TREE_KINDS or r.bridge:
                continue
            if not (r.bbox[0] <= ox + size + 40 and r.bbox[2] >= ox - 40 and r.bbox[1] <= oy + size + 40 and r.bbox[3] >= oy - 40):
                continue
            p = _densify(r.pts, step)
            if len(p) < 2:
                continue
            d = np.diff(p, axis=0)
            ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
            nrm = np.stack([-d[:, 1] / ln, d[:, 0] / ln], 1)
            mid = (p[:-1] + p[1:]) / 2
            off = r.width / 2 + 14.0
            for side in (1.0, -1.0):
                q = mid + nrm * off * side
                inside = (q[:, 0] >= ox) & (q[:, 0] < ox + size) & (q[:, 1] >= oy) & (q[:, 1] < oy + size)
                if not inside.any():
                    continue
                q = q[inside]
                ci = np.clip(np.rint(self.fx(q[:, 0])).astype(np.int64), 0, self.rw - 1)
                cj = np.clip(np.rint(self.fy(q[:, 1])).astype(np.int64), 0, self.rh - 1)
                ix, iy = np.rint(q[:, 0]).astype(np.int64), np.rint(q[:, 1]).astype(np.int64)
                h = hash2(ix // 8, iy // 8, 31)
                ok = town[cj, ci] & (self.road_w[cj, ci] < 0.05) & (self.pad_w[cj, ci] < 0.05) & ~self.wet[cj, ci]
                ok &= ((h >> 5) & 0xFF) < 150                         # gaps: driveways, crossings, missing trees
                if getattr(self, 'deck_mask', None) is not None:
                    ok &= ~self.deck_mask[cj, ci]
                if not ok.any():
                    continue
                q, h = q[ok], h[ok]
                z = self._z_at(q[:, 0], q[:, 1])
                for k in range(len(q)):
                    hv = int(h[k])
                    name = names[(hv >> 13) % len(names)]
                    oid = (2 << 60) | ((int(round(q[k][0])) & 0x3FFFFFF) << 26) | (int(round(q[k][1])) & 0x3FFFFFF)
                    out.append((sid(name), KIND_TREE, 0, float(q[k][0] - ox), float(q[k][1] - oy), float(z[k]),
                                ((hv >> 3) & 0x3FF) / 1024.0 * math.tau, 0.8 + ((hv >> 20) & 0x3F) / 63.0 * 0.3,
                                (1 << 63) | oid))
        return out

    # ---- water surfaces of this chunk: its rectangles (_water), chunk-relative
    def _chunk_water(self, cx, cy, ox, oy, n):
        out = []
        for lv, i0, j0, i1, j1, flags in self.water_rects.get((cx, cy), ()):
            x0, x1 = (self.gi0 + i0) * CELL_UNITS - ox, (self.gi0 + i1) * CELL_UNITS - ox
            y0, y1 = (self.gj0 + j0) * CELL_UNITS - oy, (self.gj0 + j1) * CELL_UNITS - oy
            out.append((float(self.gen.z_of(lv)), flags, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))
        for lv, q in getattr(self, 'water_quads', {}).get((cx, cy), ()):
            mx, my = sum(p[0] for p in q) / 4, sum(p[1] for p in q) / 4
            ci = min(max(int(round(float(self.fx(mx)))), 0), self.rw - 1)
            cj = min(max(int(round(float(self.fy(my)))), 0), self.rh - 1)
            if self.road_core[cj, ci] or (getattr(self, 'stream_cut', None) is not None and self.stream_cut[cj, ci]):
                continue                                              # under a road: the brook runs in a culvert
            out.append((float(self.gen.z_of(lv)), WATER_FLAG_FORD, [(x - ox, y - oy) for x, y in q]))
        return out


# ------------------------------------------------------------------------------------------------- reading
OBJECT_FLAG_DESTROYED = 1


def mark_destroyed(payload, ids):
    """The payload with OBJECT_FLAG_DESTROYED set on every object whose id is in ids (the player destroyed it)."""
    n = struct.unpack_from('<H', payload, 16)[0]
    p = 20
    count = struct.unpack_from('<I', payload, p)[0]; p += 4
    for _ in range(count):
        p += 2 + struct.unpack_from('<H', payload, p)[0]
    p += 5 * n * n
    count = struct.unpack_from('<I', payload, p)[0]; p += 4
    for _ in range(count):
        p += 12 + 28 * struct.unpack_from('<I', payload, p + 8)[0]
    count = struct.unpack_from('<I', payload, p)[0]; p += 4
    out = None
    for k in range(count):
        q = p + 32 * k
        if struct.unpack_from('<Q', payload, q + 24)[0] in ids:
            if out is None:
                out = bytearray(payload)
            out[q + 3] |= OBJECT_FLAG_DESTROYED
    return payload if out is None else bytes(out)


def parse(payload):
    """Decode a chunk payload (tests, the preview tool and the state filter use this)."""
    magic, version, flags, cx, cy, n, _ = struct.unpack_from('<4sHHiiHH', payload, 0)
    if magic != b'GXCK':
        raise ValueError('not a chunk payload')
    p = 20
    count = struct.unpack_from('<I', payload, p)[0]; p += 4
    strings = []
    for _ in range(count):
        ln = struct.unpack_from('<H', payload, p)[0]; p += 2
        strings.append(payload[p:p + ln].decode('utf-8')); p += ln
    heights = np.frombuffer(payload, '<f4', n * n, p).reshape(n, n); p += 4 * n * n
    ground = np.frombuffer(payload, np.uint8, n * n, p).reshape(n, n); p += n * n
    count = struct.unpack_from('<I', payload, p)[0]; p += 4
    roads = []
    for _ in range(count):
        tid, rank, rflags, width, npts = struct.unpack_from('<HBBfI', payload, p); p += 12
        pts = np.frombuffer(payload, '<f4', npts * 7, p).reshape(npts, 7); p += 28 * npts
        roads.append(dict(type=strings[tid], rank=rank, flags=rflags, width=width, pts=pts))
    count = struct.unpack_from('<I', payload, p)[0]; p += 4
    objects = []
    for _ in range(count):
        tid, kind, oflags, x, y, z, ang, scale, oid = struct.unpack_from('<HBBfffffQ', payload, p); p += 32
        objects.append(dict(template=strings[tid], kind=kind, flags=oflags, x=x, y=y, z=z, angle=ang, scale=scale, id=oid))
    count = struct.unpack_from('<I', payload, p)[0]; p += 4
    water = []
    for _ in range(count):
        z, wflags, npts = struct.unpack_from('<fB3xI', payload, p); p += 12
        pts = np.frombuffer(payload, '<f4', npts * 2, p).reshape(npts, 2); p += 8 * npts
        water.append(dict(z=z, flags=wflags, pts=pts))
    return dict(version=version, flags=flags, cx=cx, cy=cy, n=n, strings=strings, heights=heights, ground=ground,
                roads=roads, objects=objects, water=water, size=p)
