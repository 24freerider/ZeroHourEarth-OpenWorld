"""Print INI blocks from the game's .big archives: bigblock.py <dir> <regex on 'Kind Name' header line> [maxlines]"""
import struct, sys, re, glob, os
hdr = re.compile(sys.argv[2], re.I)
maxl = int(sys.argv[3]) if len(sys.argv) > 3 else 80
for big in sorted(glob.glob(os.path.join(sys.argv[1], '*.big'))):
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
        lines = data[off:off + size].decode('latin1', 'replace').splitlines()
        for i, ln in enumerate(lines):
            if re.match(r'^[A-Za-z]', ln) and hdr.search(ln.split(';')[0].strip()):
                print(f'==== {os.path.basename(big)}:{name}:{i + 1}')
                for k in range(i, min(len(lines), i + maxl)):
                    print(lines[k].rstrip())
                    if k > i and re.match(r'^(Object|Weapon|CommandButton|SpecialPower|ObjectCreationList|FXList|CommandSet|Upgrade|Locomotor|ChildObject|ObjectReskin)\s+[^=\s]+\s*(;.*)?$', lines[k + 1] if k + 1 < len(lines) else 'Object x'):
                        break
