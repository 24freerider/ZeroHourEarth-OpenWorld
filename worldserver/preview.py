"""Offline check of the world server: generate the chunks around a place and draw them as one picture.

    python preview.py "Banska Bystrica"                 7 x 7 chunks around the spawn point -> preview.png
    python preview.py 48.7363,19.1462 --chunks 9 --out bb.png
    python preview.py "Poltar" --seams                  also proves that chunks do not depend on how they are batched

The picture is stitched from the separate chunk payloads exactly as the game receives them: hill shading from the
heights, ground classes, road ribbons, building footprints, water, trees. A visible line on a chunk border would
be a seam.
"""
import argparse
import math
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chunkgen                                         # noqa: E402
from world import World                                 # noqa: E402

GROUND_RGB = np.array([(104, 140, 72), (58, 98, 52), (134, 110, 78), (206, 190, 140), (128, 126, 120), (96, 92, 88),
                       (70, 70, 74), (150, 150, 146), (140, 128, 116), (120, 90, 60), (168, 170, 88), (136, 124, 112),
                       (176, 124, 82)],
                      np.float32)
ROAD_RGB = {'TwoLaneDarkDotted': (48, 48, 52), 'TwoLane': (62, 62, 66), 'Cobblestone': (120, 108, 98),
            'Sidewalk4': (150, 150, 150), 'GravelRoad': (160, 150, 130), 'DirtRoad2': (140, 112, 78),
            'DirtRoadTracks': (128, 104, 72), 'DirtPath1': (150, 124, 90), 'Sidewalk3': (170, 166, 160),
            'Sidewalk2': (176, 172, 166), 'Sidewalk': (184, 180, 174), 'TrainTrack': (90, 70, 60)}


def render(session, chunks, cx0, cy0, nx, ny, px_per_cell=3):
    n = session.grid.n
    H = np.zeros((ny * n, nx * n), np.float32)
    G = np.zeros((ny * n, nx * n), np.uint8)
    parsed = {}
    for (cx, cy), payload in chunks.items():
        c = chunkgen.parse(payload)
        parsed[(cx, cy)] = c
        i, j = (cx - cx0) * n, (cy - cy0) * n
        H[j:j + n, i:i + n] = c['heights']
        G[j:j + n, i:i + n] = c['ground']
    # hill shading (sun from the north-west), heights are world units: 10 units per cell
    gy, gx = np.gradient(H, 10.0)
    shade = np.clip(0.80 + 0.9 * (-gx * 0.6 + gy * 0.6), 0.35, 1.35)
    rgb = GROUND_RGB[G] * shade[..., None]
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)[::-1])           # north up
    img = img.resize((img.width * px_per_cell, img.height * px_per_cell), Image.BILINEAR)
    d = ImageDraw.Draw(img, 'RGBA')
    k = px_per_cell / 10.0
    full_h = ny * n * 10.0

    def P(cx, cy, x, y):
        return ((cx - cx0) * n * 10.0 + x) * k, (full_h - ((cy - cy0) * n * 10.0 + y)) * k

    counts = dict(roads=0, buildings=0, trees=0, water=0)
    for (cx, cy), c in parsed.items():
        for w in c['water']:
            d.polygon([P(cx, cy, x, y) for x, y in w['pts']], fill=(70, 120, 190, 200))
            counts['water'] += 1
    marks = []
    for (cx, cy), c in parsed.items():
        for r in c['roads']:
            p = r['pts']
            if r['type'] in (chunkgen.PARK_TYPE, chunkgen.LINE_TYPE):
                marks.append((cx, cy, r))
                continue
            col = ROAD_RGB.get(r['type'], (60, 60, 60))
            for a, b in zip(p[:-1], p[1:]):
                d.polygon([P(cx, cy, a[0], a[1]), P(cx, cy, b[0], b[1]), P(cx, cy, b[3], b[4]), P(cx, cy, a[3], a[4])],
                          fill=col + (255,))
            counts['roads'] += 1
    # painted lines as the game draws them: a ParkingLines row = its back line and a divider every bay
    for cx, cy, r in marks:
        p = r['pts']
        mid = [((a[0] + a[3]) / 2, (a[1] + a[4]) / 2) for a in p]
        if r['type'] == chunkgen.LINE_TYPE:
            d.line([P(cx, cy, x, y) for x, y in mid], fill=(255, 255, 255, 255), width=1)
            counts['lines'] = counts.get('lines', 0) + 1
            continue
        (ax, ay), (bx, by) = mid[0], mid[-1]
        ln = math.hypot(bx - ax, by - ay)
        ux, uy = (bx - ax) / ln, (by - ay) / ln
        nx, ny = -uy, ux
        back = lambda t: (ax + ux * t + nx * chunkgen.PARK_BACK, ay + uy * t + ny * chunkgen.PARK_BACK)
        d.line([P(cx, cy, *back(0)), P(cx, cy, *back(ln))], fill=(255, 255, 255, 255), width=1)
        t = chunkgen.PARK_FIRST
        while t <= ln:
            e = (ax + ux * t - nx * chunkgen.PARK_OPEN, ay + uy * t - ny * chunkgen.PARK_OPEN)
            d.line([P(cx, cy, *back(t)), P(cx, cy, *e)], fill=(255, 255, 255, 255), width=1)
            t += chunkgen.PARK_BAY
        counts['bay_rows'] = counts.get('bay_rows', 0) + 1
    models = session.world.models.sizes
    for (cx, cy), c in parsed.items():
        for o in c['objects']:
            if o['kind'] == chunkgen.KIND_TREE:
                x, y = P(cx, cy, o['x'], o['y'])
                r = 1.6 * o['scale'] * px_per_cell / 3
                d.ellipse([x - r, y - r, x + r, y + r], fill=(28, 70, 34, 230))
                counts['trees'] += 1
            elif o['kind'] == chunkgen.KIND_PROP:
                t = o['template']
                if t.startswith(('Car', 'Truck', 'Train')):
                    # a car as its footprint (22 x 8, a wagon 56 x 20), so overlaps show
                    mx, my = (56, 20) if t.startswith('Train') else (22, 8)
                    ca, sa = math.cos(o['angle']), math.sin(o['angle'])
                    corners = [(-mx / 2, -my / 2), (mx / 2, -my / 2), (mx / 2, my / 2), (-mx / 2, my / 2)]
                    d.polygon([P(cx, cy, o['x'] + u * ca - v * sa, o['y'] + u * sa + v * ca) for u, v in corners],
                              fill=(40, 120, 230, 255) if t.startswith('Car') else (120, 60, 160, 255))
                else:
                    x, y = P(cx, cy, o['x'], o['y'])
                    r = 1.4 * px_per_cell / 3
                    d.rectangle([x - r, y - r, x + r, y + r], fill=(240, 220, 60, 255))
                counts['props'] = counts.get('props', 0) + 1
                kinds = counts.setdefault('by_template', {})
                kinds[t] = kinds.get(t, 0) + 1
            else:
                mx, my = models.get(o['template'], (40, 40))
                ca, sa = math.cos(o['angle']), math.sin(o['angle'])
                corners = [(-mx / 2, -my / 2), (mx / 2, -my / 2), (mx / 2, my / 2), (-mx / 2, my / 2)]
                d.polygon([P(cx, cy, o['x'] + u * ca - v * sa, o['y'] + u * sa + v * ca) for u, v in corners],
                          fill=(196, 92, 70, 255), outline=(60, 20, 16, 255))
                counts['buildings'] += 1
    return img, H, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('place')
    ap.add_argument('--chunks', type=int, default=7)
    ap.add_argument('--out', default='preview.png')
    ap.add_argument('--grid', action='store_true', help='draw the chunk borders')
    ap.add_argument('--seams', action='store_true', help='regenerate chunk by chunk and compare')
    ap.add_argument('--fresh', action='store_true', help='ignore cached chunks')
    a = ap.parse_args()

    root = os.path.dirname(HERE)
    world = World(root)
    if ',' in a.place and a.place.replace(',', '').replace('.', '').replace('-', '').replace(' ', '').isdigit():
        lat, lon = (float(v) for v in a.place.split(','))
        name = a.place
    else:
        lat, lon, name, _ = world.search(a.place)[0]
    t = time.perf_counter()
    s = world.open(lat, lon)
    print('place :', name)
    print('session:', s.describe())
    print(f'opened in {time.perf_counter() - t:.2f} s')
    if a.fresh:
        s._cached = lambda cx, cy: None
    nx = ny = a.chunks
    cx0, cy0 = s.spawn_chunk[0] - nx // 2, s.spawn_chunk[1] - ny // 2

    t = time.perf_counter()
    chunks = s.chunks(cx0, cy0, nx, ny)
    dt = time.perf_counter() - t
    size = sum(len(p) for p in chunks.values())
    print(f'{len(chunks)} chunks in {dt:.2f} s ({1000 * dt / len(chunks):.0f} ms each), {size / 1024:.0f} KB raw, '
          f'generated {s.stats["generated"]}, from cache {s.stats["from_cache"]}; network {world.net.stats()}')

    img, H, counts = render(s, chunks, cx0, cy0, nx, ny)
    if a.grid:
        d = ImageDraw.Draw(img)
        step = img.width / nx
        for i in range(1, nx):
            d.line([(i * step, 0), (i * step, img.height)], fill=(255, 255, 0), width=1)
            d.line([(0, i * step), (img.width, i * step)], fill=(255, 255, 0), width=1)
    # the spawn point
    d = ImageDraw.Draw(img)
    k = 3 / 10.0
    sx = (s.spawn[0] - cx0 * s.grid.chunk_units) * k
    sy = img.height - (s.spawn[1] - cy0 * s.grid.chunk_units) * k
    d.ellipse([sx - 6, sy - 6, sx + 6, sy + 6], outline=(255, 255, 255), width=2)
    d.line([(sx, sy), (sx + 16 * math.cos(s.spawn[2]), sy - 16 * math.sin(s.spawn[2]))], fill=(255, 255, 255), width=2)
    img.save(a.out)
    upm = s.grid.upm
    print(f'picture: {a.out} {img.size}; relief {(H.max() - H.min()) / upm:.1f} m '
          f'(world z {H.min():.0f} .. {H.max():.0f}); {counts}')

    if a.seams:
        # the same chunks generated one by one, and in 2 x 2 blocks, must be byte-identical to the big block
        s._cached = lambda cx, cy: None
        s.world.cache.write = lambda *args, **kw: None
        ref = s.gen.generate(cx0, cy0, nx, ny)
        bad = 0
        mid = [(cx0 + nx // 2 + i, cy0 + ny // 2 + j) for j in (-1, 0, 1) for i in (-1, 0, 1)]
        for key in mid:
            single = s.gen.generate(key[0], key[1], 1, 1)[key]
            if single != ref[key]:
                bad += 1
                a_, b_ = chunkgen.parse(single), chunkgen.parse(ref[key])
                dh = float(np.abs(a_['heights'] - b_['heights']).max())
                print(f'  chunk {key}: differs (max height difference {dh:.4f} units, '
                      f'objects {len(a_["objects"])}/{len(b_["objects"])}, roads {len(a_["roads"])}/{len(b_["roads"])})')
        blocks = s.gen.generate(mid[0][0], mid[0][1], 2, 2)
        for key, p in blocks.items():
            if p != ref[key]:
                bad += 1
                print(f'  chunk {key}: differs between a 2 x 2 block and the full block')
        print('seam check:', 'OK - chunks are identical however they are batched' if not bad else f'{bad} MISMATCHES')
        return 1 if bad else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
