"""Crater edge previews from the game's own terrain textures: craterpreview.py <terrain.txt> <out.png> <dir with .big>..."""
import struct, sys, glob, os, io, re, math, random
from PIL import Image, ImageDraw, ImageFont

want = {'GrassMediumType1': None, 'DirtMediumType2': None, 'DirtMediumType1': None, 'GrassMediumType23brown': None,
        'GrassMediumType27brown': None, 'SandType1darker': None, 'CliffLargeType4': None, 'DirtType3': None}
cur = None
for ln in open(sys.argv[1], encoding='utf-8-sig'):
    m = re.match(r'^Terrain\s+(\S+)', ln)
    if m:
        cur = m.group(1); continue
    m = re.match(r'^\s*Texture\s*=\s*(\S+)', ln)
    if m and cur in want and want[cur] is None:
        want[cur] = m.group(1).lower()
files = {}
for d in sys.argv[3:]:
    for big in glob.glob(os.path.join(d, '*.big')):
        data = open(big, 'rb').read()
        if data[:4] not in (b'BIGF', b'BIG4'):
            continue
        count, = struct.unpack('>I', data[8:12]); p = 16
        for _ in range(count):
            off, size = struct.unpack('>II', data[p:p + 8]); p += 8
            e = data.index(b'\0', p); name = data[p:e].decode('latin1'); p = e + 1
            base = os.path.basename(name.replace('\\', '/')).lower()
            if base in want.values() and base not in files:
                files[base] = data[off:off + size]
tex = {}
for k, v in want.items():
    if v in files:
        tex[k] = Image.open(io.BytesIO(files[v])).convert('RGB').resize((96, 96))

S = 360
def tiled(name):
    im = Image.new('RGB', (S, S))
    for y in range(0, S, 96):
        for x in range(0, S, 96):
            im.paste(tex[name], (x, y))
    return im

def noise(seed):
    rnd = random.Random(seed)
    ph = [rnd.uniform(0, 6.283) for _ in range(6)]
    return lambda a: 0.10 * math.sin(3 * a + ph[0]) + 0.07 * math.sin(7 * a + ph[1]) + 0.05 * math.sin(13 * a + ph[2])

def render(zones, ragged, dither, scorch):
    """zones: [(radius share, texture)] inside out; ragged: edge noise; dither: patchy band width; scorch: dark overlay"""
    layers = [tiled(t) for _, t in zones]
    base = tiled('GrassMediumType1')
    out = base.load()
    lp = [l.load() for l in layers]
    R = S * 0.33
    n = noise(7)
    rnd = random.Random(3)
    for y in range(S):
        for x in range(S):
            dx, dy = x - S / 2, y - S / 2
            r = math.hypot(dx, dy) / R
            a = math.atan2(dy, dx)
            if ragged:
                r /= 1.0 + n(a)
            for i, (share, _) in enumerate(zones):
                edge = share
                if dither:
                    # cells (8 px) burnt with a chance falling off across the band
                    cx, cy = x // 8, y // 8
                    h = random.Random(cx * 7919 + cy * 104729 + i).random()
                    if r < edge - dither / 2 or (r < edge + dither / 2 and h < (edge + dither / 2 - r) / dither):
                        out[x, y] = lp[i][x, y]
                        break
                elif r < edge:
                    out[x, y] = lp[i][x, y]
                    break
            if scorch:
                k = max(0.0, 1.0 - r / 1.35) ** 1.5 * scorch
                c = out[x, y]
                out[x, y] = tuple(int(v * (1 - k)) for v in c)
    return base

variants = [
    ('A dnes: ostrý kruh', [(1.18, 'DirtMediumType2')], False, 0, 0.55),
    ('B rozstrapkaný okraj', [(1.18, 'DirtMediumType2')], True, 0.25, 0.55),
    ('C tri zóny + šum', [(0.65, 'DirtMediumType2'), (0.95, 'DirtMediumType1'), (1.3, 'GrassMediumType23brown')], True, 0.2, 0.5),
    ('D popol + spálená tráva', [(0.8, 'DirtMediumType2'), (1.3, 'GrassMediumType27brown')], True, 0.3, 0.6),
]
sheet = Image.new('RGB', (len(variants) * (S + 10) + 10, S + 40), (30, 30, 30))
dr = ImageDraw.Draw(sheet)
try:
    font = ImageFont.truetype('arial.ttf', 18)
except OSError:
    font = ImageFont.load_default()
for i, (label, zones, ragged, dither, scorch) in enumerate(variants):
    missing = [t for _, t in zones if t not in tex]
    if missing:
        print('missing', missing); continue
    sheet.paste(render(zones, ragged, dither, scorch), (10 + i * (S + 10), 30))
    dr.text((10 + i * (S + 10), 6), label, fill=(240, 240, 240), font=font)
sheet.save(sys.argv[2])
print('ok', sorted(tex))
