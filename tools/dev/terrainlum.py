"""Mean brightness and colour of every terrain texture: terrainlum.py <terrain.txt> <out.png> <dir with .big>..."""
import struct, sys, glob, os, io, re
from PIL import Image

names = {}
cur = None
for ln in open(sys.argv[1], encoding='utf-8-sig'):
    m = re.match(r'^Terrain\s+(\S+)', ln)
    if m:
        cur = m.group(1)
        continue
    m = re.match(r'^\s*Texture\s*=\s*(\S+)', ln)
    if m and cur:
        names.setdefault(m.group(1).lower(), set()).add(cur)

files = {}
for d in sys.argv[3:]:
    for big in glob.glob(os.path.join(d, '*.big')):
        with open(big, 'rb') as f:
            data = f.read()
        if data[:4] not in (b'BIGF', b'BIG4'):
            continue
        count, = struct.unpack('>I', data[8:12])
        p = 16
        for _ in range(count):
            off, size = struct.unpack('>II', data[p:p + 8]); p += 8
            e = data.index(b'\0', p); name = data[p:e].decode('latin1'); p = e + 1
            base = os.path.basename(name.replace('\\', '/')).lower()
            if base in names:
                files[base] = data[off:off + size]

rows = []
for tex, blob in files.items():
    try:
        im = Image.open(io.BytesIO(blob)).convert('RGB').resize((32, 32))
    except Exception:
        continue
    px = list(im.getdata())
    r = sum(c[0] for c in px) / len(px); g = sum(c[1] for c in px) / len(px); b = sum(c[2] for c in px) / len(px)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    rows.append((lum, tex, r, g, b, sorted(names[tex]), im))
rows.sort()
for lum, tex, r, g, b, terr, _ in rows[:25]:
    print(f'{lum:6.1f}  rgb {r:5.1f} {g:5.1f} {b:5.1f}  {tex:22s} {" ".join(terr)}')
# a contact sheet of the darkest 20, labelled by index
sheet = Image.new('RGB', (20 * 66, 66), (255, 255, 255))
for i, row in enumerate(rows[:20]):
    sheet.paste(row[6].resize((64, 64)), (i * 66 + 1, 1))
sheet.save(sys.argv[2])
