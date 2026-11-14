"""World and Session: the pieces of the world server wired together.

    world = World(root)                         caches, network, thread pools, the game's building models
    places = world.search('Banska Bystrica')    [(lat, lon, name, kind)]
    s = world.open(lat, lon)                    a session: grid anchored near the place, spawn point on a road
    s.chunks(cx0, cy0, nx, ny)                  {(cx, cy): payload}, from the chunk cache or freshly generated
"""
import concurrent.futures as cf
import json
import math
import os
import threading
import time
import zlib

import numpy as np

import chunkgen
import sources
import worlddata
import worldgrid

DEFAULT_GAME_DIRS = [
    r'C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour',
]


def load_config(root):
    cfg = dict(port=47800, metres_per_cell=2.5, chunk_cells=64, window_chunks=7, tree_spacing=34.0,
               game_dirs=list(DEFAULT_GAME_DIRS), cache_dir='cache')
    path = os.path.join(root, 'worldserver', 'config.json')
    try:
        with open(path, encoding='utf-8') as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    env = os.environ.get('CNC_GENERALS_ZH_PATH')
    if env and env.rstrip('\\/') not in [d.rstrip('\\/') for d in cfg['game_dirs']]:
        cfg['game_dirs'].insert(0, env.rstrip('\\/'))
    patch = os.path.join(root, 'game', 'GeneralsXPatch')
    if os.path.isdir(patch):
        cfg['game_dirs'].insert(0, patch)          # the community patch overrides the retail INI
    return cfg


class World:
    def __init__(self, root, log=print):
        self.root, self.log = root, log
        self.cfg = load_config(root)
        cache_dir = self.cfg['cache_dir']
        if not os.path.isabs(cache_dir):
            cache_dir = os.path.join(root, cache_dir)
        self.cache = sources.DiskCache(cache_dir)
        self.net = sources.Net()
        self.io_pool = cf.ThreadPoolExecutor(max_workers=16, thread_name_prefix='tile')
        self.dem = sources.Dem(self.cache, self.net, self.io_pool, log)
        self.vector = sources.VectorTiles(self.cache, self.net, log)
        self.extras = sources.OsmExtras(self.cache, self.net, log)
        self.landcover = sources.WorldCover(self.cache, self.net, log)
        self.population = sources.Population(self.cache, self.net, log)
        self.models = chunkgen.Models.load(self.cfg['game_dirs'], log)

    def search(self, query):
        return sources.geocode(self.net, self.cache, query)

    def open(self, lat, lon, options=None):
        return Session(self, lat, lon, options)


class Session:
    def __init__(self, world, lat, lon, options=None):
        self.world, self.log = world, world.log
        cfg = world.cfg
        a_lat, a_lon = worldgrid.snap_anchor(lat, lon)
        self.grid = worldgrid.WorldGrid(a_lat, a_lon, cfg['metres_per_cell'], cfg['chunk_cells'])
        self.data = worlddata.WorldData(self.grid, world.vector, world.io_pool, world.log, extras=world.extras)
        # Overpass answers in seconds to minutes: ask for the pitches around the place at once (~2 km each way), so
        # they are on disk before the chunks that hold them are made
        x, y = self.grid.to_units(lat, lon)
        r = 2000.0 * self.grid.upm
        for k in self.data._tile_range(float(x) - r, float(y) - r, float(x) + r, float(y) + r):
            world.extras.pitches(self.data.z, *k)
            world.extras.squares(self.data.z, *k)
        # ESA WorldCover (sources.WorldCover) is not used for now: its pieces take a while to come, and the
        # player prefers a simple rule (a field turns to grass around the houses, chunkgen._ground)
        self.lock = threading.Lock()
        self.stats = dict(generated=0, from_cache=0, gen_seconds=0.0)

        x, y = self.grid.to_units(lat, lon)
        x, y = float(x), float(y)
        heading = 0.0
        road = self.data.nearest_road(x, y, 400.0 * self.grid.upm)
        if road is not None:
            x, y, heading = road
        self.spawn = (x, y, heading)
        self.spawn_chunk = self.grid.chunk_of(x, y)
        demz = world.dem.zoom_for(self.grid.lat0, self.grid.mpc)
        s_lat, s_lon = self.grid.from_units(np.array([x]), np.array([y]))
        self.spawn_elev = float(world.dem.sample(s_lat, s_lon, demz)[0])
        # one reference elevation per anchor, so cached chunks stay valid for every spawn in the area
        a_elev = float(world.dem.sample(np.array([a_lat]), np.array([a_lon]), demz)[0])
        self.elev0 = round(a_elev / 50.0) * 50.0
        opts = dict(tree_spacing=cfg['tree_spacing'])
        opts.update(options or {})
        self.gen = chunkgen.ChunkGenerator(self.grid, world.dem, self.data, world.models, self.elev0, opts, world.log)
        self.spawn_z = self.gen.z_of(self.spawn_elev)
        layers = ''.join(k[0] for k in ('buildings', 'trees') if not opts.get(k, True))   # '' = all on (old caches)
        self.cache_parts = ('chunks', self.grid.key, f'v{chunkgen.GEN_VERSION}_e{int(self.elev0)}_t{int(opts["tree_spacing"])}'
                            + (f'_no{layers}' if layers else ''))

    # ---- chunks
    def _cached(self, cx, cy):
        raw = self.world.cache.read(*self.cache_parts, f'{cx}_{cy}.gxc')
        if raw is None:
            return None
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return None

    def chunks(self, cx0, cy0, nx=1, ny=1):
        """Payloads of a block of chunks. Cached chunks are read from disk; the rest is generated in one pass."""
        want = [(cx0 + i, cy0 + j) for j in range(ny) for i in range(nx)]
        out = {}
        for key in want:
            p = self._cached(*key)
            if p is not None:
                out[key] = p
        missing = [k for k in want if k not in out]
        self.stats['from_cache'] += len(out)
        if missing:
            xs, ys = [k[0] for k in missing], [k[1] for k in missing]
            bx0, by0 = min(xs), min(ys)
            bnx, bny = max(xs) - bx0 + 1, max(ys) - by0 + 1
            t = time.perf_counter()
            made = self.gen.generate(bx0, by0, bnx, bny)
            self.stats['gen_seconds'] += time.perf_counter() - t
            for key in missing:
                p = made[key]
                out[key] = p
                self.stats['generated'] += 1
                if not (p[6] & 1):                       # incomplete (a tile was unreachable): do not keep
                    self.world.cache.write(zlib.compress(p, 6), *self.cache_parts, f'{key[0]}_{key[1]}.gxc')
        return out

    def prefetch(self, x, y, radius_units):
        """Start downloading the tiles around a position before any chunk there is asked for."""
        s, w = self.grid.from_units(x - radius_units, y - radius_units)
        n, e = self.grid.from_units(x + radius_units, y + radius_units)
        self.world.dem.prefetch(float(s), float(w), float(n), float(e), self.gen.demz)
        self.data.prefetch(x - radius_units, y - radius_units, x + radius_units, y + radius_units)

    def population(self, x, y):
        """(row, col, status, residents) of the 1 km population cell at world units (x, y) (sources.Population)."""
        lat, lon = self.grid.from_units(np.array([float(x)]), np.array([float(y)]))
        pop = self.world.population
        row, col = pop.cell(float(lat[0]), float(lon[0]))
        status, value = pop.population(row, col)
        return row, col, status, value

    def describe(self):
        g = self.grid
        return dict(anchor=(g.lat0, g.lon0), metres_per_cell=g.mpc, chunk_cells=g.n, units_per_metre=g.upm,
                    spawn=self.spawn, spawn_chunk=self.spawn_chunk, spawn_z=self.spawn_z, elev0=self.elev0,
                    spawn_elev=self.spawn_elev, dem_zoom=self.gen.demz, dem_source=self.world.dem.source['name'],
                    style=self.gen.style)
