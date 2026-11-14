import struct, sys, re, glob, os
pat = re.compile(r'^\s*Object\s+(\w+)', re.M)
want = re.compile(sys.argv[2], re.I)
for big in glob.glob(os.path.join(sys.argv[1], '*.big')):
    with open(big, 'rb') as f:
        data = f.read()
    if data[:4] not in (b'BIGF', b'BIG4'):
        continue
    count, = struct.unpack('>I', data[8:12])
    p = 16
    for _ in range(count):
        off, size = struct.unpack('>II', data[p:p + 8]); p += 8
        e = data.index(b'\0', p); name = data[p:e].decode('latin1'); p = e + 1
        if not name.lower().endswith('.ini'):
            continue
        text = data[off:off + size].decode('latin1', 'replace')
        for m in pat.finditer(text):
            if want.search(m.group(1)):
                # the object's KindOf and locomotor lines
                block = text[m.start():text.find('\nEnd', m.start()) if False else m.start() + 6000]
                kind = re.search(r'KindOf\s*=\s*([^\n;]*)', block)
                loco = re.search(r'Locomotor\s*=\s*SET_NORMAL\s+(\w+)', block)
                rail = 'Railroad' in block[:4000]
                print(f"{os.path.basename(big)}:{name}: {m.group(1)} | loco={loco.group(1) if loco else '-'} rail={rail} | {kind.group(1).strip()[:90] if kind else ''}")
