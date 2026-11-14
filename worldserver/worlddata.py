"""Map data of one session: vector tiles decoded once, projected into world units, and handed out by area.

The Shortbread layers map onto what the map generator reads from Overpass:

    streets          -> roads (highway class + surface -> the generator's road kinds), railways
    buildings        -> single footprints with a height
    water_polygons, ocean, water_lines -> lakes, river areas, streams
    land, sites, street_polygons       -> ground cover (forest, farmland, residential, parking, squares ...)
    pois             -> what a building is used for (pub, shop, church, hospital ...)

A feature that crosses a tile edge is in both tiles. Lines and water are cut at the exact tile edge, buildings
and POIs belong to the tile that holds their centre, so nothing is produced twice.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'generator'))
import geo                                              # noqa: E402  (the map generator's geometry helpers)
from online import road_kind, poi_kind                  # noqa: E402

import mvt
from sources import LRU, merc_to_lonlat

LAYERS = {'streets', 'street_polygons', 'buildings', 'water_polygons', 'ocean', 'water_lines', 'land', 'sites', 'pois',
          'bridges'}
RAIL_KINDS = {'rail', 'narrow_gauge'}
SKIP_STREETS = {'bridleway', 'tram', 'light_rail', 'subway', 'monorail', 'funicular', 'runway', 'taxiway',
                'construction', 'proposed', 'raceway', 'platform', 'corridor', 'elevator', 'via_ferrata'}
SKIP_SERVICE = {'drive-through', 'emergency_access'}
WATER_AREA_SKIP = {'swimming_pool', 'glacier'}
# grounds with a fence round them (chunkgen.FENCE_LAND says which fence)
FENCED_LAND = {'school', 'kindergarten', 'university', 'college', 'hospital', 'cemetery', 'grave_yard', 'industrial',
               'sports_centre', 'stadium', 'allotments', 'prison', 'wastewater_plant', 'water_works'}

# game road type per generator road kind, and the width of its ribbon in world units (1 unit = 0.25 m)
ROAD_TYPE = {'rail': 'TrainTrack', 'rail_side': 'TrainTrack', 'motorway': 'TwoLaneDarkDotted',
             'trunk': 'TwoLaneDarkDotted', 'primary': 'TwoLaneDarkDotted', 'secondary': 'TwoLaneDarkDotted',
             'tertiary': 'TwoLane', 'street_marked': 'TwoLane', 'unclassified': 'TwoLane', 'residential': 'TwoLane',
             'street_unmarked': 'TwoLane', 'living_street': 'TwoLane', 'service': 'TwoLane', 'track_paved': 'TwoLane',
             'street_concrete': 'Sidewalk4', 'street_cobble': 'Cobblestone', 'track_gravel': 'GravelRoad',
             'track': 'DirtRoad2', 'track_rutted': 'DirtRoadTracks', 'path': 'DirtPath1',
             'pedestrian': 'Sidewalk3', 'path_paved': 'Sidewalk2', 'footway': 'Sidewalk',
             # a car park's aisle: an asphalt bed only (no ribbon: the car park is asphalt); the bays line it
             'parking_aisle': 'TwoLane'}
# real carriageway widths in metres; the ribbon renderer is not tied to the one width per texture of the game's
# own road system, so a motorway is wider than a driveway
ROAD_WIDTH_M = {'motorway': 11.0, 'trunk': 9.5, 'primary': 8.5, 'secondary': 7.5, 'tertiary': 7.0,
                'street_marked': 6.5, 'unclassified': 6.0, 'residential': 6.0, 'street_unmarked': 5.5,
                'living_street': 5.0, 'service': 4.0, 'track_paved': 3.5, 'street_concrete': 4.5,
                'street_cobble': 5.0, 'track_gravel': 3.5, 'track': 3.2, 'track_rutted': 3.2, 'path': 1.6,
                'pedestrian': 5.0, 'path_paved': 2.2, 'footway': 1.8, 'rail': 3.6, 'rail_side': 3.6,
                'parking_aisle': 6.0}
ROAD_PRIO = ['rail', 'rail_side', 'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified',
             'residential', 'street_marked', 'street_unmarked', 'living_street', 'street_concrete', 'street_cobble',
             'pedestrian', 'service', 'parking_aisle', 'track_paved', 'track_gravel', 'track', 'track_rutted', 'path_paved', 'path',
             'footway']
ROAD_RANK = {k: i for i, k in enumerate(ROAD_PRIO)}
DRIVABLE = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'street_marked',
            'street_unmarked', 'living_street', 'service', 'track_paved', 'street_concrete', 'street_cobble',
            'track_gravel', 'track'}


class Road:
    __slots__ = ('kind', 'rtype', 'width', 'pts', 'bbox', 'bridge', 'rank')

    def __init__(self, kind, pts, bridge, upm):
        self.kind, self.pts, self.bridge = kind, pts, bridge
        self.rtype = ROAD_TYPE[kind]
        self.width = ROAD_WIDTH_M[kind] * upm
        self.rank = ROAD_RANK[kind]
        self.bbox = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())


# radius (m) a road's bends are rounded to: OSM draws a bend as a corner, the game's road renderer turns a corner of
# more than ~27 degrees into a fixed tile curve (or a sharp mitre) - "teeth"; arcs of small steps are drawn smooth
BEND_RADIUS_M = {'rail': 100.0, 'rail_side': 40.0, 'motorway': 60.0, 'trunk': 50.0, 'primary': 40.0,
                 'secondary': 30.0, 'tertiary': 25.0, 'unclassified': 18.0, 'street_marked': 14.0, 'residential': 12.0,
                 'street_unmarked': 12.0, 'living_street': 10.0, 'street_concrete': 10.0, 'street_cobble': 10.0,
                 'pedestrian': 8.0, 'service': 8.0, 'track_paved': 10.0, 'track_gravel': 10.0, 'track': 10.0,
                 'track_rutted': 10.0, 'path_paved': 4.0, 'path': 4.0, 'footway': 4.0, 'parking_aisle': 4.0}
BEND_STEP = math.radians(8.0)          # at most this much turn per point of an arc
BEND_MIN = math.radians(3.0)           # a turn under this stays a vertex


def _round_bends(p, pinned, radius):
    """The polyline p (Nx2) with every bend that is not pinned replaced by a quadratic Bezier arc of about `radius`
    units; an arc takes at most half of each segment next to it, so neighbouring arcs never overlap. The ends and the
    pinned points (junctions) stay exactly where they are."""
    n = len(p)
    if n < 3:
        return p
    out = [p[0]]
    for i in range(1, n - 1):
        v = p[i]
        if pinned[i]:
            out.append(v)
            continue
        d1, d2 = v - p[i - 1], p[i + 1] - v
        l1, l2 = math.hypot(*d1), math.hypot(*d2)
        if l1 < 1e-6 or l2 < 1e-6:
            continue
        cs = max(-1.0, min(1.0, float(d1 @ d2) / (l1 * l2)))
        turn = math.acos(cs)
        if turn < BEND_MIN or turn > math.radians(150.0):
            out.append(v)                                   # straight on, or a hairpin that is a real corner
            continue
        t = min(radius * math.tan(turn / 2.0), 0.5 * l1, 0.5 * l2)
        a, b = v - d1 / l1 * t, v + d2 / l2 * t
        k = max(2, int(math.ceil(turn / BEND_STEP)))
        for s in range(k + 1):
            u = s / k
            out.append((1 - u) * (1 - u) * a + 2 * u * (1 - u) * v + u * u * b)
    out.append(p[-1])
    q = np.asarray(out, float)
    keep = np.r_[True, np.hypot(*np.diff(q, axis=0).T) > 0.05]
    return q[keep]


def _smooth_roads(roads, upm):
    """Round the bends of a tile's roads (not of bridges: their decks are straight spans). A point two roads share
    (a junction) or a road's end stays put, so the roads still meet; a function of the tile alone, so every block of
    the world sees the same lines."""
    if not roads:
        return
    count = {}
    for r in roads:
        for x, y in r.pts.tolist():
            k = (round(x, 1), round(y, 1))
            count[k] = count.get(k, 0) + 1
    for r in roads:
        if r.bridge or len(r.pts) < 3:
            continue
        pinned = [count[(round(x, 1), round(y, 1))] > 1 for x, y in r.pts.tolist()]
        pts = _round_bends(r.pts, pinned, BEND_RADIUS_M.get(r.kind, 10.0) * upm)
        if len(pts) >= 2:
            r.pts = pts
            r.bbox = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())


class Building:
    __slots__ = ('ring', 'height_m', 'bbox', 'cx', 'cy', 'area', 'dense')

    def __init__(self, ring, height_m):
        self.ring, self.height_m = ring, height_m
        self.dense = False
        self.bbox = (ring[:, 0].min(), ring[:, 1].min(), ring[:, 0].max(), ring[:, 1].max())
        x, y = ring[:, 0], ring[:, 1]
        a = float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)) / 2.0
        self.area = abs(a)
        if self.area > 1e-6:
            f = x * np.roll(y, -1) - np.roll(x, -1) * y
            self.cx = float(np.sum((x + np.roll(x, -1)) * f) / (6 * a))
            self.cy = float(np.sum((y + np.roll(y, -1)) * f) / (6 * a))
        else:
            self.cx, self.cy = float(x.mean()), float(y.mean())


class Area:
    __slots__ = ('kind', 'rings', 'bbox', 'own')

    def __init__(self, kind, rings, own=None):
        self.kind, self.rings = kind, rings                # rings[0] = outline, the rest = holes
        o = rings[0]
        self.bbox = (o[:, 0].min(), o[:, 1].min(), o[:, 0].max(), o[:, 1].max())
        # per piece of the outline: does it lie in this tile (True) or in the tile's overlap / on the cut the tile
        # server made (False)? A fence goes only along the own pieces - each piece is then fenced once, by one tile
        self.own = own


class Line:
    __slots__ = ('kind', 'pts', 'bbox')

    def __init__(self, kind, pts):
        self.kind, self.pts = kind, pts
        self.bbox = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())


DENSE_PER_KM2 = 900       # the map generator's dense big city: this many buildings per km2 ...
DENSE_SHARE = 0.28        # ... and this share of the ground built over (Vienna ~40 %, Poltar ~10 %)


class TileData:
    __slots__ = ('roads', 'buildings', 'water', 'waterlines', 'land', 'pois', 'bridges', 'pitches', 'extras_ok', 'ok',
                 'dense', 'squares')

    def __init__(self):
        self.dense = False                                  # a dense big city (the whole tile, ~1.6 km square)
        self.squares = []                                   # squares and pedestrian areas (Overpass), Area
        self.roads, self.buildings, self.water, self.waterlines, self.land, self.pois = [], [], [], [], [], []
        self.bridges = []                                   # deck outlines (OSM man_made=bridge), Area
        self.pitches = []                                   # football pitches (Overpass), Area; owned by the tile
        self.extras_ok = True                               # of their centre - False: not known yet (ask again)
        self.ok = True


def _overlaps(b, x0, y0, x1, y1):
    return b[0] <= x1 and b[2] >= x0 and b[1] <= y1 and b[3] >= y0


class Gathered:
    """Everything that touches one rectangle of the world."""

    def __init__(self):
        self.roads, self.buildings, self.water, self.waterlines, self.land, self.pois = [], [], [], [], [], []
        self.bridges = []
        self.pitches = []
        self.squares = []
        self.complete = True


class WorldData:
    def __init__(self, grid, vector_tiles, pool, log=print, extras=None):
        self.grid, self.vt, self.pool, self.log = grid, vector_tiles, pool, log
        self.extras = extras
        self.landcover = None                           # sources.WorldCover (set by the session)
        self.z = vector_tiles.z
        self.tiles = LRU(40)

    # ---- tile <-> world
    def _tile_range(self, x0, y0, x1, y1):
        lat_a, lon_a = self.grid.from_units(x0, y0)
        lat_b, lon_b = self.grid.from_units(x1, y1)
        n = 1 << self.z

        def txy(lat, lon):
            lat = max(-85.0511, min(85.0511, float(lat)))
            tx = int((float(lon) + 180.0) / 360.0 * n)
            ty = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
            return tx, ty
        ax, ay = txy(lat_b, lon_a)                  # north-west
        bx, by = txy(lat_a, lon_b)                  # south-east
        return [(tx, ty) for tx in range(min(ax, bx), max(ax, bx) + 1) for ty in range(min(ay, by), max(ay, by) + 1)]

    def _project(self, tx, ty, extent, paths):
        """Tile-unit paths -> list of Nx2 world-unit arrays (one numpy pass for the whole tile)."""
        if not paths:
            return []
        lens = [len(p) for p in paths]
        flat = np.array([pt for p in paths for pt in p], dtype=np.float64)
        n = float(1 << self.z)
        lon, lat = merc_to_lonlat((tx + flat[:, 0] / extent) / n, (ty + flat[:, 1] / extent) / n)
        x, y = self.grid.to_units(lat, lon)
        xy = np.stack([x, y], axis=1)
        out, i = [], 0
        for ln in lens:
            out.append(xy[i:i + ln])
            i += ln
        return out

    # ---- decoding one tile
    def _decode(self, tx, ty):
        td = TileData()
        raw = self.vt.raw(tx, ty)
        if raw is None:
            td.ok = False
            return td
        if not raw:
            return td
        try:
            layers = mvt.decode(raw, LAYERS)
        except Exception as ex:                       # a corrupt tile must not stop the world
            self.log(f'vector tile {self.z}/{tx}/{ty} could not be decoded: {ex!r}')
            td.ok = False
            return td
        upm = self.grid.upm
        paths, sink = [], []                          # everything is projected in one go at the end

        def emit(path, fn):
            paths.append(path)
            sink.append(fn)

        # ---------- roads and railways
        extent, feats = layers.get('streets', (4096, []))
        for gtype, parts, p in feats:
            if gtype != 2 or p.get('tunnel'):
                continue
            kind = str(p.get('kind', ''))
            if kind in SKIP_STREETS:
                continue
            service = str(p.get('service') or '')
            if kind in RAIL_KINDS:
                if service == 'crossover':
                    continue
                rk = 'rail_side' if service in ('yard', 'siding', 'spur') else 'rail'
            else:
                if kind == 'service' and service in SKIP_SERVICE:
                    continue
                tags = {'surface': str(p.get('surface') or ''), 'tracktype': str(p.get('tracktype') or '')}
                rk = road_kind(kind + ('_link' if p.get('link') else ''), tags)
                if kind == 'service' and service == 'parking_aisle':
                    rk = 'parking_aisle'
                if kind == 'steps':
                    rk = 'footway'                      # a flight of steps: part of the footpath it joins
                if rk == 'track_rutted':
                    continue                            # a grass track: two faint ruts in a meadow, not a road to draw
                if rk is None or rk not in ROAD_TYPE:
                    continue
            bridge = bool(p.get('bridge'))
            if bridge and rk == 'parking_aisle':
                continue                                # the deck of a parking garage, not a bridge to build
            for line in parts:
                for run in geo.clip_polyline(line, extent, extent):
                    if len(run) >= 2:
                        emit(run, lambda a, rk=rk, bridge=bridge: td.roads.append(Road(rk, a, bridge, upm))
                             if len(a) >= 2 else None)

        # ---------- buildings (owned by the tile that holds the centre of the footprint)
        extent, feats = layers.get('buildings', (4096, []))
        for gtype, parts, p in feats:
            if gtype != 3:
                continue
            try:
                height = float(p.get('height') or 0.0)
            except (TypeError, ValueError):
                height = 0.0
            for rings in parts:
                ring = rings[0]
                if len(ring) < 3:
                    continue
                sx = sum(pt[0] for pt in ring) / len(ring)
                sy = sum(pt[1] for pt in ring) / len(ring)
                if not (0 <= sx < extent and 0 <= sy < extent):
                    continue
                emit(ring, lambda a, height=height: td.buildings.append(Building(a, height)))

        # ---------- water areas, cut at the tile edge
        for layer in ('water_polygons', 'ocean'):
            extent, feats = layers.get(layer, (4096, []))
            for gtype, parts, p in feats:
                if gtype != 3:
                    continue
                kind = 'ocean' if layer == 'ocean' else str(p.get('kind', 'water'))
                if kind in WATER_AREA_SKIP:
                    continue
                for rings in parts:
                    clipped = [geo.clip_poly(r, extent, extent) for r in rings]
                    if len(clipped[0]) < 3:
                        continue
                    group = []
                    holder = dict(kind=kind, rings=group, n=sum(1 for c in clipped if len(c) >= 3))

                    def add(a, holder=holder):
                        holder['rings'].append(a)
                        if len(holder['rings']) == holder['n']:
                            td.water.append(Area(holder['kind'], holder['rings']))
                    for c in clipped:
                        if len(c) >= 3:
                            emit(c, add)

        extent, feats = layers.get('water_lines', (4096, []))
        for gtype, parts, p in feats:
            if gtype != 2 or p.get('tunnel'):
                continue
            kind = str(p.get('kind', 'stream'))
            for line in parts:
                for run in geo.clip_polyline(line, extent, extent):
                    if len(run) >= 2:
                        emit(run, lambda a, kind=kind: td.waterlines.append(Line(kind, a)))

        # ---------- ground cover (only rasterised, so overlap between tiles does not matter)
        for layer in ('land', 'sites', 'street_polygons'):
            extent, feats = layers.get(layer, (4096, []))
            for gtype, parts, p in feats:
                if gtype != 3:
                    continue
                kind = str(p.get('kind', ''))
                if layer == 'street_polygons':
                    kind = 'square'
                for rings in parts:
                    rings = [r for r in rings if len(r) >= 3]
                    if not rings:
                        continue
                    group = []
                    own = None
                    if kind in FENCED_LAND or kind == 'parking':
                        o = np.asarray(rings[0], float)
                        mid = (o + np.roll(o, -1, axis=0)) / 2
                        own = ((mid[:, 0] > 0) & (mid[:, 0] < extent) & (mid[:, 1] > 0) & (mid[:, 1] < extent))
                    holder = dict(kind=kind, rings=group, n=len(rings), own=own)

                    def add(a, holder=holder):
                        holder['rings'].append(a)
                        if len(holder['rings']) == holder['n']:
                            td.land.append(Area(holder['kind'], holder['rings'], holder['own']))
                    for r in rings:
                        emit(r, add)

        # ---------- bridge decks: the outline of a bridge as built (both carriageways, footpaths, its true width)
        extent, feats = layers.get('bridges', (4096, []))
        for gtype, parts, p in feats:
            if gtype != 3:
                continue
            for rings in parts:
                ring = geo.clip_poly(rings[0], extent, extent)
                if len(ring) >= 3:
                    emit(ring, lambda a: td.bridges.append(Area('bridge', [a])))

        # ---------- points of interest
        extent, feats = layers.get('pois', (4096, []))
        for gtype, parts, p in feats:
            if gtype != 1:
                continue
            kind = poi_kind({k: str(v) for k, v in p.items() if isinstance(v, str)})
            if p.get('amenity') == 'fuel':
                kind = 'fuel'
            if kind is None:
                continue
            for pts in parts:
                for pt in pts:
                    if 0 <= pt[0] < extent and 0 <= pt[1] < extent:
                        emit([pt], lambda a, kind=kind: td.pois.append((kind, float(a[0, 0]), float(a[0, 1]))))

        for arr, fn in zip(self._project(tx, ty, 4096, paths), sink):
            fn(arr)
        _smooth_roads(td.roads, upm)
        # how built-up the tile is (its own buildings: each belongs to the tile of its centre)
        n = float(1 << self.z)
        _, lat_c = merc_to_lonlat((tx + 0.5) / n, (ty + 0.5) / n)
        side_m = 40075016.7 * math.cos(math.radians(float(lat_c))) / n
        km2 = side_m * side_m / 1e6
        built = sum(b.area for b in td.buildings) / (upm * upm) / 1e6
        td.dense = len(td.buildings) / km2 >= DENSE_PER_KM2 and built / km2 >= DENSE_SHARE
        for b in td.buildings:
            b.dense = td.dense
        self._extras(tx, ty, td)
        return td

    def is_dense(self, x, y):
        """Is world x / y in a dense big city (its vector tile)?"""
        k = self._tile_range(x, y, x, y)[0]
        return self.tile(*k).dense

    def tile(self, tx, ty):
        td = self.tiles.get((tx, ty))
        if td is None:
            td = self.vt.inflight.run(('decoded', id(self), tx, ty), lambda: self._decode(tx, ty))
            if td.ok:
                self.tiles.put((tx, ty), td)
        if td.ok and not td.extras_ok:
            self._extras(tx, ty, td)                    # Overpass did not answer before: ask again (rate limited)
        return td

    def _extras(self, tx, ty, td):
        """The tile's football pitches (their centre in the tile), in world units."""
        if self.extras is None:
            return
        found = {}
        for what in ('pitch', 'square'):
            rings = self.extras.areas(what, self.z, tx, ty)
            if rings is None:
                td.extras_ok = False
                return
            found[what] = rings
        n = float(1 << self.z)
        lon0, lat0 = (float(v) for v in merc_to_lonlat(tx / n, ty / n))
        lon1, lat1 = (float(v) for v in merc_to_lonlat((tx + 1) / n, (ty + 1) / n))
        for what, rings in found.items():
            out = []
            for ring in rings:
                lon = np.array([p[0] for p in ring])
                lat = np.array([p[1] for p in ring])
                clon, clat = float(lon.mean()), float(lat.mean())
                if not (lon0 <= clon < lon1 and lat1 < clat <= lat0):
                    continue
                x, y = self.grid.to_units(lat, lon)
                out.append(Area(what, [np.stack([np.asarray(x, float), np.asarray(y, float)], axis=1)]))
            if what == 'pitch':
                td.pitches = out
            else:
                td.squares = out
        td.extras_ok = True

    # ---- area queries
    def gather(self, x0, y0, x1, y1):
        g = Gathered()
        keys = self._tile_range(x0, y0, x1, y1)
        tds = list(self.pool.map(lambda k: self.tile(*k), keys)) if len(keys) > 1 else [self.tile(*keys[0])]
        for td in tds:
            if not td.ok or not td.extras_ok:
                g.complete = False
            g.pitches += [a for a in td.pitches if _overlaps(a.bbox, x0, y0, x1, y1)]
            g.squares += [a for a in td.squares if _overlaps(a.bbox, x0, y0, x1, y1)]
            g.roads += [r for r in td.roads if _overlaps(r.bbox, x0, y0, x1, y1)]
            g.buildings += [b for b in td.buildings if _overlaps(b.bbox, x0, y0, x1, y1)]
            g.water += [a for a in td.water if _overlaps(a.bbox, x0, y0, x1, y1)]
            g.waterlines += [w for w in td.waterlines if _overlaps(w.bbox, x0, y0, x1, y1)]
            g.land += [a for a in td.land if _overlaps(a.bbox, x0, y0, x1, y1)]
            g.pois += [p for p in td.pois if x0 <= p[1] <= x1 and y0 <= p[2] <= y1]
            g.bridges += [a for a in td.bridges if _overlaps(a.bbox, x0, y0, x1, y1)]
        return g

    def prefetch(self, x0, y0, x1, y1):
        for k in self._tile_range(x0, y0, x1, y1):
            if self.tiles.get(k) is None:
                self.pool.submit(self.tile, *k)
            if self.extras is not None:
                self.extras.pitches(self.z, *k)         # on disk: a read; otherwise queued for Overpass
                self.extras.squares(self.z, *k)
        self.prefetch_landcover(x0, y0, x1, y1)

    def prefetch_landcover(self, x0, y0, x1, y1):
        """Queue the land cover pieces of a rectangle (sampled every ~100 m: a piece is 10 km wide)."""
        lc = getattr(self, 'landcover', None)
        if lc is None:
            return
        xs = np.linspace(x0, x1, max(2, int((x1 - x0) / 400.0) + 1))
        ys = np.linspace(y0, y1, max(2, int((y1 - y0) / 400.0) + 1))
        X, Y = np.meshgrid(xs, ys)
        lat, lon = self.grid.from_units(X, Y)
        lc.sample(lat, lon)

    def nearest_road(self, x, y, radius):
        """Closest point of a drivable road: (px, py, heading_radians) or None."""
        g = self.gather(x - radius, y - radius, x + radius, y + radius)
        best = None
        for r in g.roads:
            if r.kind not in DRIVABLE or r.bridge:
                continue
            a, b = r.pts[:-1], r.pts[1:]
            d = b - a
            ln2 = np.maximum((d * d).sum(1), 1e-9)
            t = np.clip(((x - a[:, 0]) * d[:, 0] + (y - a[:, 1]) * d[:, 1]) / ln2, 0, 1)
            px, py = a[:, 0] + t * d[:, 0], a[:, 1] + t * d[:, 1]
            dist = np.hypot(px - x, py - y)
            i = int(np.argmin(dist))
            score = float(dist[i]) + r.rank * 6.0        # prefer a real street over a track next to it
            if best is None or score < best[0]:
                best = (score, float(px[i]), float(py[i]), math.atan2(float(d[i, 1]), float(d[i, 0])), float(dist[i]))
        if best is None or best[4] > radius:
            return None
        return best[1], best[2], best[3]
