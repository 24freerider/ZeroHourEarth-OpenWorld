"""Talks to a running world server the way the game does: search, open, fetch the first window, drive a little.

    python testclient.py "Poltar"            (start server.py first)
"""
import socket
import struct
import sys
import time
import zlib

import chunkgen


def pstr(s):
    b = s.encode('utf-8')
    return struct.pack('<H', len(b)) + b


def rstr(buf, p):
    n = struct.unpack_from('<H', buf, p)[0]
    return buf[p + 2:p + 2 + n].decode('utf-8'), p + 2 + n


class Client:
    def __init__(self, port=47800):
        self.s = socket.create_connection(('127.0.0.1', port), timeout=60)
        self.rid = 0

    def send(self, mtype, payload=b''):
        self.rid += 1
        self.s.sendall(struct.pack('<IHI', 6 + len(payload), mtype, self.rid) + payload)
        return self.rid

    def _read(self, n):
        buf = b''
        while len(buf) < n:
            part = self.s.recv(n - len(buf))
            if not part:
                raise ConnectionError('server closed the connection')
            buf += part
        return buf

    def recv(self):
        length = struct.unpack('<I', self._read(4))[0]
        body = self._read(length)
        mtype, rid = struct.unpack_from('<HI', body, 0)
        if mtype == 199:
            raise RuntimeError('server error: ' + rstr(body, 6)[0])
        return mtype, rid, body[6:]


WINDOW = 7


def main():
    global WINDOW
    args = sys.argv[1:]
    if '--window' in args:                                   # python testclient.py "Poltar" --window 9
        i = args.index('--window')
        WINDOW = int(args[i + 1])
        del args[i:i + 2]
    query = args[0] if args else 'Poltar'
    c = Client()
    c.send(1, struct.pack('<I', 2))
    mtype, _, p = c.recv()
    print('hello:', mtype, struct.unpack_from('<I', p, 0)[0], rstr(p, 4)[0])

    t = time.perf_counter()
    c.send(2, pstr(query))
    _, _, p = c.recv()
    n = struct.unpack_from('<H', p, 0)[0]
    q = 2
    places = []
    for _ in range(n):
        lat, lon = struct.unpack_from('<dd', p, q); q += 16
        name, q = rstr(p, q)
        kind, q = rstr(p, q)
        places.append((lat, lon, name, kind))
    print(f'search "{query}": {n} places in {time.perf_counter() - t:.2f} s')
    for pl in places[:5]:
        print('   %.5f %.5f  [%s] %s' % (pl[0], pl[1], pl[3], pl[2][:90]))
    if not places:
        return 1

    t = time.perf_counter()
    # protocol 2: the window and the layers (1 buildings | 2 roads | 4 trees)
    c.send(3, struct.pack('<dd', places[0][0], places[0][1]) + pstr(places[0][2]) + struct.pack('<HB', WINDOW, 7))
    _, _, p = c.recv()
    lat0, lon0, mpc, cells, window, scx, scy = struct.unpack_from('<ddfHHii', p, 0)
    sx, sy, sz, heading, zbase = struct.unpack_from('<ddfff', p, 32)
    q = 32 + 28
    ncls = struct.unpack_from('<H', p, q)[0]; q += 2
    classes = []
    for _ in range(ncls):
        s, q = rstr(p, q)
        classes.append(s)
    print(f'opened in {time.perf_counter() - t:.2f} s: anchor {lat0},{lon0} {mpc} m/cell, chunk {cells} cells, '
          f'window {window}, spawn chunk ({scx},{scy}) at ({sx:.0f},{sy:.0f},{sz:.0f}) heading {heading:.2f}, '
          f'{ncls} ground classes')

    def fetch(cx0, cy0, nx, ny):
        t0 = time.perf_counter()
        rid = c.send(4, struct.pack('<iiHH', cx0, cy0, nx, ny))
        got, raw, packed = [], 0, 0
        while True:
            mtype, r, p = c.recv()
            if r != rid:
                continue
            if mtype == 105:
                break
            cx, cy, size = struct.unpack_from('<iiI', p, 0)
            data = zlib.decompress(p[12:])
            assert len(data) == size
            got.append(chunkgen.parse(data))
            raw += size; packed += len(p) - 12
        ms = 1000 * (time.perf_counter() - t0)
        objs = sum(len(g['objects']) for g in got)
        roads = sum(len(g['roads']) for g in got)
        print(f'   block ({cx0},{cy0}) {nx}x{ny}: {len(got)} chunks, {ms:.0f} ms, {packed / 1024:.0f} KB on the wire '
              f'({raw / 1024:.0f} KB raw), {objs} objects, {roads} road pieces')
        return ms

    half = window // 2
    print('first window:')
    fetch(scx - half, scy - half, window, window)
    print('driving east, one column of chunks per step:')
    worst = 0
    for step in range(1, 9):
        c.send(5, struct.pack('<ddf', sx + step * cells * 10.0, sy, 0.0))
        worst = max(worst, fetch(scx + half + step, scy - half, 1, window))
    print(f'worst column: {worst:.0f} ms  (at 15 m/s a column is needed every {cells * mpc / 15:.1f} s)')
    c.send(7)
    return 0


if __name__ == '__main__':
    sys.exit(main())
