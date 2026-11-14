"""The world server: streams the real world to the game over a local socket.

Start it with the game ("Play OpenWorld.cmd" does) or by hand:   python server.py [--port 47800]

One game connects on 127.0.0.1. It searches for a place, opens a session there, and then asks for blocks of
chunks as the player drives; the server downloads the tiles, generates the chunks and sends them back. Nothing
here is ever waited on by a game frame: the game asks, keeps running, and applies a chunk when it arrives.

Wire format (little endian). Every message, both directions:

    u32 length of the rest   u16 type   u32 request id   payload

A string is u16 byte length + UTF-8. Replies carry the request id of the message they answer.

    game -> server                                       server -> game
    1 HELLO     u32 protocol                             101 HELLO_OK   u32 protocol, string server
    2 SEARCH    string query                             102 PLACES     u16 n, n * (f64 lat, f64 lon, string name, string kind)
    3 OPEN      f64 lat, f64 lon, string name            103 OPENED     see _open()
                [u16 window, u8 layers]  (protocol 2: the window the player chose, odd 3..25, and the layers to
                generate, 1 buildings | 2 roads | 4 trees; the base map for that window is written if missing)
    4 CHUNKS    i32 cx0, i32 cy0, u16 nx, u16 ny         104 CHUNK      i32 cx, i32 cy, u32 raw size, zlib(payload)   (one per chunk)
                                                         105 DONE       (after the last chunk of the request)
    5 POSITION  f64 x, f64 y, f32 heading                -               (the server prefetches tiles ahead)
    6 DESTROYED u64 object id                            -               (remembered: the object stays gone)
    7 CLOSE                                              -
    8 POPULATION u16 n, n * (f64 x, f64 y)               106 POPULATION u16 n, n * (f64 x, f64 y, i32 row, i32 col,
                (protocol 3: world units of the grid)                   u8 status, f32 residents)
                                                         (the 1 km GHS-POP cell of each point: status 0 known,
                                                         1 no data, 2 pending - ask again later)
                                                         199 ERROR      string message

Protocol 3: OPEN's layers may carry 8 = the zombie run (its base map is the "Z" variant, worldmap.py).
"""
import argparse
import os
import queue
import socket
import struct
import sys
import threading
import time
import traceback
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chunkgen                                         # noqa: E402
import worldmap                                         # noqa: E402
from world import World                                 # noqa: E402

PROTOCOL = 3
LAYER_BUILDINGS, LAYER_ROADS, LAYER_TREES, LAYER_ZOMBIES = 1, 2, 4, 8
M_HELLO, M_SEARCH, M_OPEN, M_CHUNKS, M_POSITION, M_DESTROYED, M_CLOSE, M_POPULATION = 1, 2, 3, 4, 5, 6, 7, 8
R_HELLO_OK, R_PLACES, R_OPENED, R_CHUNK, R_DONE, R_POPULATION, R_ERROR = 101, 102, 103, 104, 105, 106, 199


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def pstr(s):
    b = s.encode('utf-8')[:65000]
    return struct.pack('<H', len(b)) + b


def rstr(buf, p):
    n = struct.unpack_from('<H', buf, p)[0]
    return buf[p + 2:p + 2 + n].decode('utf-8', 'replace'), p + 2 + n


class DestroyedStore:
    """Object ids the player destroyed in THIS run (one OPEN): every run starts with the world whole again. Kept in
    memory only - a chunk asked for again during the run still comes with its ruins. A file left by an older server
    (state/<grid>/destroyed.bin, which kept them for ever) is removed."""

    def __init__(self, cache, grid_key):
        self.ids = set()
        self.lock = threading.Lock()
        try:
            os.remove(cache.path('state', grid_key, 'destroyed.bin'))
        except OSError:
            pass

    def add(self, oid):
        with self.lock:
            self.ids.add(oid)


class Connection:
    def __init__(self, server, sock, addr):
        self.server, self.world, self.sock, self.addr = server, server.world, sock, addr
        self.send_lock = threading.Lock()
        self.session = None
        self.destroyed = None
        # chunk requests: (cx, cy) -> (generation, rid). The worker takes the one nearest the game's focus first, so
        # after a fast camera flight the chunks under the camera do not wait behind the ones it has flown past.
        self.pending = {}
        self.pending_cv = threading.Condition()
        self.focus = None              # (cx, cy) in chunks, from M_POSITION
        self.alive = True
        self.generation = 0            # bumped by OPEN: chunk requests of an older session are dropped

    # ---- sending
    def send(self, mtype, rid, payload=b''):
        frame = struct.pack('<IHI', 6 + len(payload), mtype, rid) + payload
        with self.send_lock:
            try:
                self.sock.sendall(frame)
            except OSError:
                self.alive = False

    def error(self, rid, text):
        log('error ->', text)
        self.send(R_ERROR, rid, pstr(text))

    # ---- receiving
    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            part = self.sock.recv(n - len(buf))
            if not part:
                raise ConnectionError('closed')
            buf += part
        return bytes(buf)

    def run(self):
        log(f'game connected from {self.addr[0]}:{self.addr[1]}')
        worker = threading.Thread(target=self._work, daemon=True, name='chunks')
        worker.start()
        try:
            while self.alive:
                length = struct.unpack('<I', self._recv(4))[0]
                if length < 6 or length > 1 << 20:
                    raise ConnectionError('bad frame')
                body = self._recv(length)
                mtype, rid = struct.unpack_from('<HI', body, 0)
                self._handle(mtype, rid, body[6:])
        except (ConnectionError, OSError):
            pass
        finally:
            self.alive = False
            with self.pending_cv:
                self.pending_cv.notify_all()
            try: self.sock.close()
            except OSError: pass
            log('game disconnected')

    def _handle(self, mtype, rid, p):
        if mtype == M_HELLO:
            self.send(R_HELLO_OK, rid, struct.pack('<I', PROTOCOL) + pstr('Zero Hour Earth world server'))
        elif mtype == M_SEARCH:
            query, _ = rstr(p, 0)
            threading.Thread(target=self._search, args=(rid, query), daemon=True).start()
        elif mtype == M_OPEN:
            lat, lon = struct.unpack_from('<dd', p, 0)
            name, q = rstr(p, 16)
            window, layers = self.world.cfg['window_chunks'], LAYER_BUILDINGS | LAYER_ROADS | LAYER_TREES
            if len(p) >= q + 3:                                  # protocol 2
                window, layers = struct.unpack_from('<HB', p, q)
            self.generation += 1
            with self.pending_cv:
                self.pending.clear()
                self.focus = None
            threading.Thread(target=self._open, args=(rid, lat, lon, name, self.generation, window, layers),
                             daemon=True).start()
        elif mtype == M_CHUNKS:
            cx0, cy0, nx, ny = struct.unpack_from('<iiHH', p, 0)
            if nx < 1 or ny < 1 or nx * ny > 400:
                self.error(rid, 'bad chunk request')
                return
            with self.pending_cv:
                for j in range(ny):
                    for i in range(nx):
                        self.pending[(cx0 + i, cy0 + j)] = (self.generation, rid)
                self.pending_cv.notify()
        elif mtype == M_POSITION:
            x, y, heading = struct.unpack_from('<ddf', p, 0)
            s = self.session
            if s is not None:
                with self.pending_cv:
                    self.focus = (x / s.grid.chunk_units - 0.5, y / s.grid.chunk_units - 0.5)
                # look two windows ahead in the direction of travel
                import math
                reach = s.grid.chunk_units * max(6, getattr(self, 'window', 7) // 2 + 2)
                s.prefetch(x + math.cos(heading) * reach * 0.6, y + math.sin(heading) * reach * 0.6, reach)
        elif mtype == M_DESTROYED:
            if self.destroyed is not None:
                self.destroyed.add(struct.unpack_from('<Q', p, 0)[0])
        elif mtype == M_POPULATION:
            s = self.session
            n = struct.unpack_from('<H', p, 0)[0]
            out = [struct.pack('<H', n)]
            for i in range(n):
                x, y = struct.unpack_from('<dd', p, 2 + 16 * i)
                row, col, status, value = s.population(x, y) if s is not None else (0, 0, 2, 0.0)
                out.append(struct.pack('<ddiiBf', x, y, row, col, status, value))
            self.send(R_POPULATION, rid, b''.join(out))
        elif mtype == M_CLOSE:
            self.generation += 1
            self.session = None
            with self.pending_cv:
                self.pending.clear()

    def _search(self, rid, query):
        try:
            places = self.world.search(query)
        except Exception as ex:
            self.error(rid, str(ex) or 'The place search failed.')
            return
        out = [struct.pack('<H', len(places))]
        for lat, lon, name, kind in places:
            out.append(struct.pack('<dd', lat, lon) + pstr(name) + pstr(kind))
        log(f'search "{query}": {len(places)} place(s)')
        self.send(R_PLACES, rid, b''.join(out))

    def _open(self, rid, lat, lon, name, generation, window, layers):
        t = time.perf_counter()
        if not (3 <= window <= 25) or window % 2 == 0:
            self.error(rid, f'A {window} x {window} window is not possible: pick an odd size from 3 to 25.')
            return
        try:
            # the game loads the base map of exactly this window: write it now if this size is new
            made = worldmap.ensure(self.world.root, window, self.world.cfg['chunk_cells'], log=log,
                                   zombie=bool(layers & LAYER_ZOMBIES))
            if made:
                log(f'base map for a {window} x {window} window written: {made}')
        except Exception as ex:
            traceback.print_exc()
            self.error(rid, f'The base map for a {window} x {window} window could not be written: {ex}')
            return
        try:
            s = self.world.open(lat, lon, dict(buildings=bool(layers & LAYER_BUILDINGS), trees=bool(layers & LAYER_TREES)))
        except Exception as ex:
            traceback.print_exc()
            self.error(rid, f'This place could not be opened: {ex}')
            return
        if generation != self.generation:
            return
        self.session = s
        self.window = window
        self.destroyed = DestroyedStore(self.world.cache, s.grid.key)
        g = s.grid
        with self.pending_cv:                                    # the first window: from the spawn outwards
            self.focus = (float(s.spawn_chunk[0]), float(s.spawn_chunk[1]))
        # start downloading the first window right away, before the game asks for it
        s.prefetch(s.spawn[0], s.spawn[1], g.chunk_units * (window // 2 + 2))
        if layers & LAYER_ZOMBIES:
            s.population(s.spawn[0], s.spawn[1])                 # the zombie run: its population tile, now
        out = [struct.pack('<ddfHHii', g.lat0, g.lon0, g.mpc, g.n, window, s.spawn_chunk[0], s.spawn_chunk[1]),
               struct.pack('<ddfff', s.spawn[0], s.spawn[1], s.spawn_z, s.spawn[2], chunkgen.ZBASE),
               struct.pack('<H', len(chunkgen.GROUND_CLASSES))]
        out += [pstr(c) for c in chunkgen.GROUND_CLASSES]
        out.append(pstr(name))
        log(f'opened "{name}" at {lat:.5f},{lon:.5f} in {time.perf_counter() - t:.2f} s: window {window}, layers {layers}, '
            f'anchor {g.lat0},{g.lon0}, '
            f'spawn chunk {s.spawn_chunk}, elevation {s.spawn_elev:.0f} m, {self.world.dem.source["name"]} z{s.gen.demz}')
        self.send(R_OPENED, rid, b''.join(out))

    def _take(self):
        """The pending chunk nearest the focus and the pending chunks next to it in its row (up to 4 on each side,
        generated in one pass), or None when the connection is gone."""
        with self.pending_cv:
            while self.alive and not self.pending:
                self.pending_cv.wait()
            if not self.alive:
                return None
            f = self.focus
            if f is None:
                key = min(self.pending)                      # nothing known yet: any order will do
            else:
                key = min(self.pending, key=lambda k: (k[0] - f[0]) ** 2 + (k[1] - f[1]) ** 2)
            generation, rid = self.pending[key]
            lo = hi = key[0]
            while lo - 1 >= key[0] - 4 and self.pending.get((lo - 1, key[1]), (None,))[0] == generation:
                lo -= 1
            while hi + 1 <= key[0] + 4 and self.pending.get((hi + 1, key[1]), (None,))[0] == generation:
                hi += 1
            for cx in range(lo, hi + 1):
                del self.pending[(cx, key[1])]
            return generation, rid, lo, key[1], hi - lo + 1, 1

    # ---- chunk generation, nearest the focus first
    def _work(self):
        while True:
            job = self._take()
            if job is None:
                return
            generation, rid, cx0, cy0, nx, ny = job
            s = self.session
            if s is None or generation != self.generation or not self.alive:
                continue
            t = time.perf_counter()
            try:
                chunks = s.chunks(cx0, cy0, nx, ny)
            except Exception as ex:
                traceback.print_exc()
                self.error(rid, f'chunks {cx0},{cy0} {nx}x{ny} failed: {ex}')
                continue
            sent = 0
            gone = self.destroyed.ids if self.destroyed is not None else ()
            for (cx, cy), payload in sorted(chunks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                if gone:
                    payload = chunkgen.mark_destroyed(payload, gone)
                packed = zlib.compress(payload, 1)
                sent += len(packed)
                self.send(R_CHUNK, rid, struct.pack('<iiI', cx, cy, len(payload)) + packed)
            self.send(R_DONE, rid)
            log(f'chunks {cx0},{cy0} {nx}x{ny}: {1000 * (time.perf_counter() - t):.0f} ms, {sent / 1024:.0f} KB '
                f'(generated {s.stats["generated"]}, cached {s.stats["from_cache"]}; net {self.world.net.stats()})')


class Server:
    def __init__(self, root, port):
        self.world = World(root, log)
        self.port = port or self.world.cfg['port']

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(('127.0.0.1', self.port))
        except OSError as ex:
            log(f'port {self.port} is in use ({ex}) - is another world server already running?')
            return 1
        srv.listen(2)
        log(f'world server listening on 127.0.0.1:{self.port}; cache {self.world.cache.root}')
        while True:
            sock, addr = srv.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=Connection(self, sock, addr).run, daemon=True, name='game').start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=0)
    a = ap.parse_args()
    root = os.environ.get('ZHE_WORLD_ROOT') or os.path.dirname(HERE)
    return Server(root.rstrip('\\/'), a.port).serve()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
