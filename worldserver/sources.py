"""Real-time data sources of the world server.

Everything is a slippy-map tile fetched over HTTPS from a CDN, stored on disk forever and kept decoded in a small
memory cache, so the world can be streamed while driving:

  elevation   Mapterhorn (terrarium WebP, 512 px, national LiDAR + Copernicus GLO-30)  ->  AWS Terrain Tiles fallback
  map data    VersaTiles OSM tiles in the Shortbread schema (roads with surface, single buildings with height,
              land use, water, POIs), zoom 14
  geocoding   Nominatim (only when the player searches for a place)

Overpass, which the map generator uses, answers in seconds to minutes and is rate limited; tiles answer in tens of
milliseconds and never need a query.
"""
import concurrent.futures as cf
import io
import json
import math
import os
import tempfile
import threading
import time
from collections import OrderedDict

import numpy as np

UA = {'User-Agent': 'ZeroHourEarth-OpenWorld/0.1 (C&C Generals Zero Hour open-world prototype)'}

DEM_SOURCES = (
    dict(name='mapterhorn', url='https://tiles.mapterhorn.com/{z}/{x}/{y}.webp', size=512, minz=0, maxz=17, ext='webp'),
    dict(name='aws', url='https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png', size=256, minz=0,
         maxz=15, ext='png'),
)
VECTOR_SOURCE = dict(name='versatiles', url='https://tiles.versatiles.org/tiles/osm/{z}/{x}/{y}', z=14, ext='pbf')
NOMINATIM = 'https://nominatim.openstreetmap.org/search'


# --------------------------------------------------------------------------------------------- small utilities
class LRU:
    """Thread-safe least-recently-used map."""

    def __init__(self, capacity):
        self.capacity, self._d, self._lock = capacity, OrderedDict(), threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.capacity:
                self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)


class DiskCache:
    def __init__(self, root):
        self.root = root

    def path(self, *parts):
        return os.path.join(self.root, *[str(p) for p in parts])

    def read(self, *parts):
        try:
            with open(self.path(*parts), 'rb') as f:
                return f.read()
        except OSError:
            return None

    def exists(self, *parts):
        return os.path.isfile(self.path(*parts))

    def write(self, data, *parts):
        """Atomic: a reader never sees half a file, two writers of the same tile do not corrupt it."""
        p = self.path(*parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix='.part')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            for attempt in range(5):
                try:
                    os.replace(tmp, p)
                    tmp = None
                    break
                except PermissionError:
                    time.sleep(0.02 * (attempt + 1))
        finally:
            if tmp is not None:
                try: os.unlink(tmp)
                except OSError: pass

    def size_bytes(self):
        total = 0
        for d, _, files in os.walk(self.root):
            for fn in files:
                try: total += os.path.getsize(os.path.join(d, fn))
                except OSError: pass
        return total


class Net:
    """HTTP with one keep-alive session per thread and running statistics."""

    def __init__(self):
        self._local = threading.local()
        self.lock = threading.Lock()
        self.requests = self.bytes = self.errors = 0
        self.seconds = 0.0
        self.offline = False

    def _session(self):
        s = getattr(self._local, 's', None)
        if s is None:
            import requests
            s = requests.Session()
            s.headers.update(UA)
            adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
            s.mount('https://', adapter)
            self._local.s = s
        return s

    def get(self, url, timeout=20, params=None):
        """-> (status, bytes). status 0 = no answer at all."""
        if self.offline:
            return 0, b''
        t = time.perf_counter()
        try:
            r = self._session().get(url, timeout=timeout, params=params)
            data = r.content
            with self.lock:
                self.requests += 1
                self.bytes += len(data)
                self.seconds += time.perf_counter() - t
            return r.status_code, data
        except Exception:
            with self.lock:
                self.errors += 1
            return 0, b''

    def stats(self):
        with self.lock:
            avg = 1000 * self.seconds / self.requests if self.requests else 0
            return dict(requests=self.requests, megabytes=round(self.bytes / 1048576, 2), errors=self.errors,
                        avg_ms=round(avg))


class Inflight:
    """Makes concurrent requests for the same key share one piece of work."""

    def __init__(self):
        self._lock, self._jobs = threading.Lock(), {}

    def run(self, key, fn):
        with self._lock:
            fut = self._jobs.get(key)
            owner = fut is None
            if owner:
                fut = self._jobs[key] = cf.Future()
        if not owner:
            return fut.result()
        try:
            result = fn()
            fut.set_result(result)
            return result
        except BaseException as ex:
            fut.set_exception(ex)
            raise
        finally:
            with self._lock:
                self._jobs.pop(key, None)


# --------------------------------------------------------------------------------------------- web mercator
def lonlat_to_merc(lon, lat):
    """-> x, y in 0..1 (y down), numpy friendly."""
    lat = np.clip(lat, -85.0511, 85.0511)
    x = (np.asarray(lon, float) + 180.0) / 360.0
    r = np.radians(lat)
    y = (1.0 - np.log(np.tan(r) + 1.0 / np.cos(r)) / math.pi) / 2.0
    return x, y


def merc_to_lonlat(x, y):
    lon = np.asarray(x, float) * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * np.asarray(y, float)))))
    return lon, lat


# --------------------------------------------------------------------------------------------- elevation
class Dem:
    """Elevation in metres at any lat/lon, from terrarium tiles.

    A sample depends only on its position: tiles missing at the working zoom are rebuilt from their parent, so two
    chunks that share an edge always read the same value there."""

    def __init__(self, cache, net, pool, log=print):
        self.cache, self.net, self.pool, self.log = cache, net, pool, log
        self.mem = LRU(160)
        self.inflight = Inflight()
        self.source = DEM_SOURCES[0]
        self._checked = False
        self.tiles_fetched = 0

    # -- source choice: the first one that answers
    def _pick_source(self):
        if self._checked:
            return
        self._checked = True
        for src in DEM_SOURCES:
            status, data = self.net.get(src['url'].format(z=3, x=4, y=2), timeout=10)
            if status == 200 and data:
                self.source = src
                self.log(f'elevation source: {src["name"]} ({src["size"]} px tiles)')
                return
        self.log('elevation source: none reachable, using the disk cache only')

    def zoom_for(self, lat, metres_per_cell):
        """Pixels about 0.6 cell wide: finer than the terrain grid, so sampling does not alias."""
        self._pick_source()
        want = max(metres_per_cell * 0.6, 0.25)
        res0 = 40075016.686 * math.cos(math.radians(lat)) / self.source['size']
        z = int(math.floor(math.log2(res0 / want) + 0.5))
        return max(8, min(self.source['maxz'], z))

    def _decode(self, raw):
        from PIL import Image
        a = np.asarray(Image.open(io.BytesIO(raw)).convert('RGB'), np.float32)
        return a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0

    def _raw_tile(self, z, x, y):
        """Native tile as an array, or None when the source has no tile there."""
        src = self.source
        n = 1 << z
        x %= n
        if y < 0 or y >= n:
            return None
        raw = self.cache.read('dem', src['name'], z, x, f'{y}.{src["ext"]}')
        if raw is None:
            if self.cache.exists('dem', src['name'], z, x, f'{y}.none'):
                return None
            status = 0
            for attempt in range(3):
                status, raw = self.net.get(src['url'].format(z=z, x=x, y=y))
                if status in (200, 204, 404):
                    break
                time.sleep(0.3 * (attempt + 1))
            if status in (204, 404) or (status == 200 and not raw):
                self.cache.write(b'', 'dem', src['name'], z, x, f'{y}.none')
                return None
            if status != 200:
                return None                       # offline: treated like a missing tile, but not remembered
            self.cache.write(raw, 'dem', src['name'], z, x, f'{y}.{src["ext"]}')
            self.tiles_fetched += 1
        try:
            a = self._decode(raw)
        except Exception:
            return None
        return a if a.shape == (src['size'], src['size']) else None

    def tile(self, z, x, y):
        """Tile content at zoom z, rebuilt from coarser zooms where the source stops."""
        key = (self.source['name'], z, x, y)
        a = self.mem.get(key)
        if a is not None:
            return a

        def load():
            a = self._raw_tile(z, x, y)
            if a is None:
                if z <= 5:
                    a = np.zeros((self.source['size'],) * 2, np.float32)
                else:
                    a = self._from_parent(z, x, y)
            self.mem.put(key, a)
            return a
        return self.inflight.run(key, load)

    def _from_parent(self, z, x, y):
        """One quadrant of the parent, doubled with a smooth (cubic B-spline like) kernel."""
        s = self.source['size']
        p = self.tile(z - 1, x >> 1, y >> 1)
        h = s // 2
        ox, oy = (x & 1) * h, (y & 1) * h
        pad = np.pad(p, 1, mode='edge')
        q = pad[oy:oy + h + 2, ox:ox + h + 2]
        # separable 2x upsampling with weights (1/4, 3/4): the classic smooth doubling
        a = np.empty((h + 2, s), np.float32)
        a[:, 0::2] = 0.25 * q[:, :-2] + 0.75 * q[:, 1:-1]
        a[:, 1::2] = 0.75 * q[:, 1:-1] + 0.25 * q[:, 2:]
        out = np.empty((s, s), np.float32)
        out[0::2, :] = 0.25 * a[:-2, :] + 0.75 * a[1:-1, :]
        out[1::2, :] = 0.75 * a[1:-1, :] + 0.25 * a[2:, :]
        return out

    def sample(self, lat, lon, z):
        """Bilinear elevation (m) at arrays of lat / lon."""
        s = self.source['size']
        mx, my = lonlat_to_merc(lon, lat)
        scale = (1 << z) * s
        fx = mx * scale - 0.5                      # pixel centres
        fy = my * scale - 0.5
        x0, y0 = int(math.floor(fx.min())), int(math.floor(fy.min()))
        x1, y1 = int(math.floor(fx.max())) + 1, int(math.floor(fy.max())) + 1
        tx0, ty0, tx1, ty1 = x0 // s, y0 // s, x1 // s, y1 // s
        jobs = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
        if len(jobs) > 400:
            raise ValueError('elevation request covers too many tiles')
        tiles = dict(zip(jobs, self.pool.map(lambda t: self.tile(z, t[0], t[1]), jobs))) if len(jobs) > 1 else \
            {jobs[0]: self.tile(z, *jobs[0])}
        mosaic = np.empty(((ty1 - ty0 + 1) * s, (tx1 - tx0 + 1) * s), np.float32)
        for (tx, ty), a in tiles.items():
            mosaic[(ty - ty0) * s:(ty - ty0 + 1) * s, (tx - tx0) * s:(tx - tx0 + 1) * s] = a
        gx, gy = fx - tx0 * s, fy - ty0 * s
        ix = np.clip(np.floor(gx).astype(np.int64), 0, mosaic.shape[1] - 2)
        iy = np.clip(np.floor(gy).astype(np.int64), 0, mosaic.shape[0] - 2)
        ax, ay = (gx - ix).astype(np.float32), (gy - iy).astype(np.float32)
        return (mosaic[iy, ix] * (1 - ax) * (1 - ay) + mosaic[iy, ix + 1] * ax * (1 - ay) +
                mosaic[iy + 1, ix] * (1 - ax) * ay + mosaic[iy + 1, ix + 1] * ax * ay)

    def prefetch(self, lat_s, lon_w, lat_n, lon_e, z):
        s = self.source['size']
        (mx0, mx1), (my0, my1) = lonlat_to_merc(np.array([lon_w, lon_e]), np.array([lat_n, lat_s]))
        n = 1 << z
        for ty in range(int(my0 * n), int(my1 * n) + 1):
            for tx in range(int(mx0 * n), int(mx1 * n) + 1):
                if self.mem.get((self.source['name'], z, tx, ty)) is None:
                    self.pool.submit(self.tile, z, tx, ty)


# --------------------------------------------------------------------------------------------- vector tiles
class VectorTiles:
    """Raw vector tiles (bytes), disk cached. Decoding and projecting is done by the session (worlddata.py)."""

    def __init__(self, cache, net, log=print):
        self.cache, self.net, self.log = cache, net, log
        self.source = VECTOR_SOURCE
        self.z = VECTOR_SOURCE['z']
        self.inflight = Inflight()
        self.tiles_fetched = 0

    def raw(self, x, y):
        """Decompressed tile bytes; b'' = the tile is empty (open sea); None = could not be fetched right now."""
        src, z = self.source, self.z

        def load():
            data = self.cache.read('vec', src['name'], z, x, f'{y}.{src["ext"]}')
            if data is None:
                status = 0
                for attempt in range(3):
                    status, data = self.net.get(src['url'].format(z=z, x=x, y=y))
                    if status in (200, 204, 404):
                        break
                    time.sleep(0.4 * (attempt + 1))
                if status in (204, 404):
                    data = b''
                elif status != 200:
                    return None
                self.cache.write(data, 'vec', src['name'], z, x, f'{y}.{src["ext"]}')
                self.tiles_fetched += 1
            if data[:2] == b'\x1f\x8b':
                import gzip
                data = gzip.decompress(data)
            return data
        return self.inflight.run((x, y), load)


# --------------------------------------------------------------------------------------------- OSM extras
OVERPASS = ('https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter',
            'https://overpass.private.coffee/api/interpreter')


class OsmExtras:
    """What the Shortbread tiles leave out (football pitches), asked from Overpass once per vector tile and kept on
    disk for good, like the tiles: the same tile always gives the same pitches, so chunks stay seamless.

    Never waits: Overpass answers in seconds to minutes (or not at all), so a tile not on disk yet is queued for one
    background thread (one request at a time, Overpass is rate limited) and reported as not known; a chunk generated
    meanwhile is marked incomplete and not cached, and comes out with its pitches once they are on disk. A tile that
    could not be asked is tried again after RETRY seconds."""
    RETRY = 300.0
    QUERIES = {
        'pitch': ('[out:json][timeout:25];(way["leisure"="pitch"]["sport"~"soccer"]({s},{w},{n},{e});'
                  'relation["leisure"="pitch"]["sport"~"soccer"]({s},{w},{n},{e}););out geom;'),
        # town squares and pedestrian areas: paved ground (the tiles carry only some of them)
        'square': ('[out:json][timeout:25];(way["place"="square"]({s},{w},{n},{e});'
                   'relation["place"="square"]({s},{w},{n},{e});'
                   'way["highway"="pedestrian"]["area"="yes"]({s},{w},{n},{e});'
                   'way["area:highway"~"^(pedestrian|footway)$"]({s},{w},{n},{e});'
                   'way["amenity"="marketplace"]({s},{w},{n},{e}););out geom;'),
    }

    def __init__(self, cache, net, log=print):
        self.cache, self.net, self.log = cache, net, log
        self.cv = threading.Condition()
        self.queue, self.queued, self.failed = [], set(), {}
        self.fetched = self.errors = 0
        self.thread = None

    @staticmethod
    def _key(what, z, x, y):
        return (what, z, x, f'{y}.json')

    def pitches(self, z, x, y):
        """[(lon, lat) ring, ...] of the soccer pitches in tile z/x/y; None = not known yet (asked in the background)."""
        return self.areas('pitch', z, x, y)

    def squares(self, z, x, y):
        """[(lon, lat) ring, ...] of the squares and pedestrian areas in tile z/x/y; None = not known yet."""
        return self.areas('square', z, x, y)

    def areas(self, what, z, x, y):
        raw = self.cache.read('osm', *self._key(what, z, x, y))
        job = (what, z, x, y)
        if raw is None:
            with self.cv:
                t = self.failed.get(job)
                if job not in self.queued and (t is None or time.time() - t >= self.RETRY):
                    self.queued.add(job)
                    self.queue.append(job)
                    if self.thread is None:
                        self.thread = threading.Thread(target=self._run, name='overpass', daemon=True)
                        self.thread.start()
                    self.cv.notify()
            return None
        try:
            js = json.loads(raw.decode('utf-8'))
        except ValueError:
            return []
        rings = []
        for el in js.get('elements', ()):
            geoms = [el.get('geometry')] if el.get('type') == 'way' else \
                    [m.get('geometry') for m in el.get('members', ()) if m.get('role') == 'outer']
            for g in geoms:
                if g and len(g) >= 4:
                    rings.append([(float(p['lon']), float(p['lat'])) for p in g])
        return rings

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue.pop(0)
            what, z, x, y = job
            n = float(1 << z)
            w_, n_ = merc_to_lonlat(x / n, y / n)
            e_, s_ = merc_to_lonlat((x + 1) / n, (y + 1) / n)
            q = self.QUERIES[what].format(s=f'{float(s_):.6f}', w=f'{float(w_):.6f}', n=f'{float(n_):.6f}',
                                          e=f'{float(e_):.6f}')
            raw = None
            for url in OVERPASS:
                status, data = self.net.get(url, timeout=40, params={'data': q})
                if status == 200 and data[:1] == b'{':
                    raw = data
                    break
                time.sleep(1.0)
            if raw is not None:
                self.cache.write(raw, 'osm', *self._key(what, z, x, y))
                self.fetched += 1
            with self.cv:
                self.queued.discard(job)
                if raw is None:
                    self.failed[job] = time.time()
                    self.errors += 1
            time.sleep(1.0)                                     # a second between requests


# --------------------------------------------------------------------------------------------- land cover
class WorldCover:
    """ESA WorldCover 2021 (10 m land cover from Sentinel-1/2, CC BY 4.0): what the ground really is where the map
    data is coarse - a field polygon drawn over a whole village, gardens, copses, bare ground.

    The data are 3 x 3 degree cloud-optimised GeoTIFFs on AWS (tiles of 1024 x 1024 pixels, deflate); only the pieces
    a region needs are read (HTTP ranges) and kept on disk for good. Like OsmExtras it never waits: a piece not on
    disk yet is fetched in the background and the region is reported as not known (its chunks come out incomplete).
    Classes: 10 trees, 20 shrubs, 30 grass, 40 cropland, 50 built up, 60 bare, 70 snow, 80 water, 90 wetland,
    95 mangroves, 100 moss; 0 = no data (open sea)."""
    URL = 'https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{name}_Map.tif'
    RETRY = 300.0
    TREES, SHRUBS, GRASS, CROPS, BUILT, BARE = 10, 20, 30, 40, 50, 60

    def __init__(self, cache, net, log=print):
        self.cache, self.net, self.log = cache, net, log
        self.cv = threading.Condition()
        self.queue, self.queued, self.failed = [], set(), {}
        self.headers, self.arrays = {}, LRU(48)
        self.thread = None
        self.fetched = self.errors = 0

    @staticmethod
    def _name(lat3, lon3):
        return f"{'N' if lat3 >= 0 else 'S'}{abs(lat3):02d}{'E' if lon3 >= 0 else 'W'}{abs(lon3):03d}"

    def _header(self, name):
        """(tile size, tiles across, offsets, byte counts) of a file, or False (no file: sea), or None (not known)."""
        h = self.headers.get(name)
        if h is not None:
            return h
        raw = self.cache.read('worldcover', name, 'header.bin')
        if raw is None:
            if self.cache.exists('worldcover', name, 'none'):
                self.headers[name] = False
                return False
            self._ask((name, 'header'))
            return None
        import struct
        off = struct.unpack_from('<I', raw, 4)[0]
        n = struct.unpack_from('<H', raw, off)[0]
        tags = {}
        for i in range(n):
            tag, typ, cnt, val = struct.unpack_from('<HHII', raw, off + 2 + 12 * i)
            if typ == 3 and cnt == 1:
                val = struct.unpack_from('<H', raw, off + 2 + 12 * i + 8)[0]
            tags[tag] = (typ, cnt, val)
        width, tile = tags[256][2], tags[322][2]
        across = (width + tile - 1) // tile
        cnt = tags[324][1]
        offsets = struct.unpack_from(f'<{cnt}I', raw, tags[324][2])
        counts = struct.unpack_from(f'<{cnt}I', raw, tags[325][2])
        h = (tile, across, offsets, counts)
        self.headers[name] = h
        return h

    def _ask(self, job):
        with self.cv:
            t = self.failed.get(job)
            if job in self.queued or (t is not None and time.time() - t < self.RETRY):
                return
            self.queued.add(job)
            self.queue.append(job)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='worldcover', daemon=True)
                self.thread.start()
            self.cv.notify()

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue.pop(0)
            name, what = job
            ok = False
            url = self.URL.format(name=name)
            if what == 'header':
                status, data = self._range(url, 0, 65535)
                if status in (403, 404):
                    self.cache.write(b'', 'worldcover', name, 'none')
                    ok = True
                elif status in (200, 206) and data[:4] == b'II*\x00':
                    self.cache.write(data, 'worldcover', name, 'header.bin')
                    ok = True
            else:
                h = self._header(name)
                if h:
                    k = what
                    status, data = self._range(url, h[2][k], h[2][k] + h[3][k] - 1)
                    if status in (200, 206) and len(data) == h[3][k]:
                        self.cache.write(data, 'worldcover', name, f'{k}.z')
                        self.fetched += 1
                        ok = True
            with self.cv:
                self.queued.discard(job)
                if not ok:
                    self.failed[job] = time.time()
                    self.errors += 1

    def _range(self, url, a, b):
        if self.net.offline:
            return 0, b''
        try:
            r = self.net._session().get(url, headers={'Range': f'bytes={a}-{b}'}, timeout=40)
            return r.status_code, r.content
        except Exception:
            return 0, b''

    def _array(self, name, k):
        key = (name, k)
        a = self.arrays.get(key)
        if a is not None:
            return a
        raw = self.cache.read('worldcover', name, f'{k}.z')
        if raw is None:
            self._ask((name, k))
            return None
        import zlib
        h = self.headers[name]
        a = np.frombuffer(zlib.decompress(raw), np.uint8).reshape(h[0], h[0])
        self.arrays.put(key, a)
        return a

    def sample(self, lat, lon):
        """Classes at lat / lon (arrays of one shape), or None while a piece is still being fetched."""
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        out = np.zeros(lat.shape, np.uint8)
        lat3 = (np.floor(lat / 3.0) * 3).astype(np.int64)
        lon3 = (np.floor(lon / 3.0) * 3).astype(np.int64)
        missing = False
        for la, lo in set(zip(lat3.ravel().tolist(), lon3.ravel().tolist())):
            name = self._name(la, lo)
            h = self._header(name)
            if h is None:
                missing = True
                continue
            if h is False:
                continue                                    # no file: open sea, no data
            tile, across = h[0], h[1]
            sel = (lat3 == la) & (lon3 == lo)
            col = np.clip(((lon[sel] - lo) * 12000.0).astype(np.int64), 0, across * tile - 1)
            row = np.clip(((la + 3 - lat[sel]) * 12000.0).astype(np.int64), 0, across * tile - 1)
            k = (row // tile) * across + col // tile
            vals = np.zeros(len(k), np.uint8)
            for kk in np.unique(k).tolist():
                a = self._array(name, kk)
                if a is None:
                    missing = True
                    continue
                m = k == kk
                vals[m] = a[row[m] % tile, col[m] % tile]
            out[sel] = vals
        return None if missing else out


# --------------------------------------------------------------------------------------------- population
def tiff_lzw_decode(data):
    """TIFF LZW (MSB-first codes of 9..12 bits, 256 = clear, 257 = end, the code width grows one code early)."""
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b'', b'']
    width, prev = 9, None
    bits = nbits = 0
    pos, n = 0, len(data)
    while True:
        while nbits < width:
            if pos >= n:
                return bytes(out)
            bits = (bits << 8) | data[pos]
            pos += 1
            nbits += 8
        nbits -= width
        code = (bits >> nbits) & ((1 << width) - 1)
        bits &= (1 << nbits) - 1
        if code == 256:
            table = table[:258]
            width, prev = 9, None
            continue
        if code == 257:
            return bytes(out)
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        if len(table) + 1 >= (1 << width) and width < 12:
            width += 1


def mollweide(lat, lon, radius=6378137.0):
    """ESRI:54009 World Mollweide (PROJ: the sphere of the WGS 84 semi-major axis), metres."""
    phi = np.radians(np.asarray(lat, float))
    lam = np.radians(np.asarray(lon, float))
    theta = phi.copy()
    target = np.pi * np.sin(phi)
    for _ in range(30):                                     # 2 theta + sin 2 theta = pi sin phi (Newton)
        f = 2 * theta + np.sin(2 * theta) - target
        d = 2 + 2 * np.cos(2 * theta)
        step = np.where(np.abs(d) > 1e-12, f / np.where(np.abs(d) > 1e-12, d, 1.0), 0.0)
        theta = theta - step
        if np.all(np.abs(step) < 1e-12):
            break
    x = radius * 2 * np.sqrt(2) / np.pi * lam * np.cos(theta)
    y = radius * np.sqrt(2) * np.sin(theta)
    return x, y


class Population:
    """Residents per 1 km cell: GHS-POP R2023A, epoch 2025 (European Commission, JRC / Copernicus GHSL; open data,
    CC BY 4.0), World Mollweide (ESRI:54009) at 1000 m. A cell is a stable geographic id: (row, col) of the global
    grid, row 0 at y = 9,000,000 m, col 0 at x = -18,041,000 m. The data come as zipped GeoTIFF tiles of 1000 x 1000
    cells (1000 km a side; R1 C1 the north-west one): a tile is downloaded once in the background (~5 MB), decoded
    (LZW, floating point predictor, float64) and kept as a float32 array. Like the other sources it never waits:
    a cell whose tile is not on disk yet is reported pending."""
    URL = ('https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/'
           'GHS_POP_E2025_GLOBE_R2023A_54009_1000/V1-0/tiles/GHS_POP_E2025_GLOBE_R2023A_54009_1000_V1_0_R{r}_C{c}.zip')
    SOURCE = 'GHS-POP R2023A, 2025 (JRC / Copernicus)'
    X0, Y0, CELL, TILE = -18041000.0, 9000000.0, 1000.0, 1000
    KNOWN, MISSING, PENDING = 0, 1, 2
    RETRY = 300.0

    def __init__(self, cache, net, log=print):
        self.cache, self.net, self.log = cache, net, log
        self.cv = threading.Condition()
        self.queue, self.queued, self.failed = [], set(), {}
        self.arrays = {}
        self.thread = None
        self.fetched = self.errors = 0

    def cell(self, lat, lon):
        x, y = mollweide(lat, lon)
        return int(np.floor((self.Y0 - float(y)) / self.CELL)), int(np.floor((float(x) - self.X0) / self.CELL))

    def _tile(self, r, c):
        """The tile's array, False (no tile: open sea), or None (being fetched)."""
        key = (r, c)
        a = self.arrays.get(key)
        if a is not None:
            return a
        name = f'R{r}_C{c}'
        if self.cache.exists('ghspop', f'{name}.none'):
            self.arrays[key] = False
            return False
        raw = self.cache.read('ghspop', f'{name}.f32')
        if raw is None:
            self._ask(key)
            return None
        a = np.frombuffer(raw, np.float32).reshape(self.TILE, self.TILE)
        self.arrays[key] = a
        return a

    def population(self, row, col):
        """(status, residents) of cell (row, col): KNOWN with the count (0 is a real 0), MISSING (no data there),
        or PENDING (its tile is on the way)."""
        r, c = row // self.TILE + 1, col // self.TILE + 1
        a = self._tile(r, c)
        if a is None:
            return self.PENDING, 0.0
        if a is False:
            return self.MISSING, 0.0
        v = float(a[row % self.TILE, col % self.TILE])
        if not np.isfinite(v) or v < 0:
            return self.MISSING, 0.0
        return self.KNOWN, v

    def _ask(self, key):
        with self.cv:
            t = self.failed.get(key)
            if key in self.queued or (t is not None and time.time() - t < self.RETRY):
                return
            self.queued.add(key)
            self.queue.append(key)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='ghspop', daemon=True)
                self.thread.start()
            self.cv.notify()

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                key = self.queue.pop(0)
            r, c = key
            name = f'R{r}_C{c}'
            ok = False
            try:
                status, data = self.net.get(self.URL.format(r=r, c=c), timeout=120)
                if status == 404:
                    self.cache.write(b'', 'ghspop', f'{name}.none')
                    ok = True
                elif status == 200 and data[:2] == b'PK':
                    a = self._decode(data)
                    self.cache.write(a.astype(np.float32).tobytes(), 'ghspop', f'{name}.f32')
                    self.fetched += 1
                    ok = True
                    self.log(f'population tile {name}: {float(a[a > 0].sum()):.0f} residents')
            except Exception as ex:
                self.log(f'population tile {name} failed: {ex}')
            with self.cv:
                self.queued.discard(key)
                if not ok:
                    self.failed[key] = time.time()
                    self.errors += 1

    @classmethod
    def _decode(cls, zipped):
        """The tile's 1000 x 1000 float64 values out of the zip's GeoTIFF (BigTIFF, tiled, LZW, predictor 3)."""
        import io
        import struct
        import zipfile
        with zipfile.ZipFile(io.BytesIO(zipped)) as z:
            tif = next(n for n in z.namelist() if n.lower().endswith('.tif'))
            raw = z.read(tif)
        bo = '<' if raw[:2] == b'II' else '>'
        big = struct.unpack_from(bo + 'H', raw, 2)[0] == 43
        if big:
            off = struct.unpack_from(bo + 'Q', raw, 8)[0]
            count, base, esize = struct.unpack_from(bo + 'Q', raw, off)[0], off + 8, 20
        else:
            off = struct.unpack_from(bo + 'I', raw, 4)[0]
            count, base, esize = struct.unpack_from(bo + 'H', raw, off)[0], off + 2, 12
        sizes = {1: 1, 3: 2, 4: 4, 12: 8, 16: 8}
        codes = {1: 'B', 3: 'H', 4: 'I', 12: 'd', 16: 'Q'}
        tags = {}
        for i in range(count):
            e = base + esize * i
            if big:
                tag, typ, cnt = struct.unpack_from(bo + 'HHQ', raw, e)
                vpos, inline = e + 12, 8
            else:
                tag, typ, cnt = struct.unpack_from(bo + 'HHI', raw, e)
                vpos, inline = e + 8, 4
            if typ not in codes:
                continue
            pos = vpos if sizes[typ] * cnt <= inline else \
                struct.unpack_from(bo + ('Q' if big else 'I'), raw, vpos)[0]
            tags[tag] = struct.unpack_from(bo + f'{cnt}{codes[typ]}', raw, pos)
        width, height = tags[256][0], tags[257][0]
        tw, th = tags[322][0], tags[323][0]
        bps = tags[258][0] // 8
        comp, pred = tags[259][0], tags.get(317, (1,))[0]
        fmt = {(3, 8): 'f8', (3, 4): 'f4'}.get((tags.get(339, (1,))[0], bps))
        if fmt is None or comp not in (1, 5):
            raise ValueError(f'unexpected GeoTIFF: compression {comp}, {bps} byte samples')
        offs, cnts = tags[324], tags[325]
        out = np.empty((height, width), np.float64)
        across = (width + tw - 1) // tw
        for k in range(len(offs)):
            data = raw[offs[k]:offs[k] + cnts[k]]
            if comp == 5:
                data = tiff_lzw_decode(data)
            b = np.frombuffer(data, np.uint8)[:tw * th * bps].reshape(th, tw * bps)
            if pred == 3:
                # floating point predictor: bytes differenced along the row, then laid out as byte planes (most
                # significant first)
                b = np.cumsum(b, axis=1, dtype=np.uint8)
                v = b.reshape(th, bps, tw).transpose(0, 2, 1).copy().view('>' + fmt).reshape(th, tw)
            else:
                v = b.copy().view(bo + fmt).reshape(th, tw)
            ty, tx = (k // across) * th, (k % across) * tw
            hh, ww = min(th, height - ty), min(tw, width - tx)
            out[ty:ty + hh, tx:tx + ww] = v[:hh, :ww]
        return out


# --------------------------------------------------------------------------------------------- geocoding
def geocode(net, cache, query, limit=8):
    """Candidates for a place name: [(lat, lon, display_name, kind)]. One request per search, cached."""
    import hashlib
    key = hashlib.sha1(query.strip().lower().encode('utf-8')).hexdigest() + '.json'
    raw = cache.read('geocode', key)
    if raw is None:
        status, raw = net.get(NOMINATIM, params={'q': query, 'format': 'jsonv2', 'limit': limit, 'dedupe': 1,
                                                 'accept-language': 'en,sk'}, timeout=20)
        if status != 200:
            raise RuntimeError('The place search did not answer (no internet connection?).')
        cache.write(raw, 'geocode', key)
    out = []
    for j in json.loads(raw.decode('utf-8')):
        try:
            out.append((float(j['lat']), float(j['lon']), j.get('display_name', query),
                        j.get('addresstype') or j.get('type') or ''))
        except (KeyError, ValueError):
            pass
    return out
