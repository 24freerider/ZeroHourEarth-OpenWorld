"""Print ButtonCommandNN screen rectangles from ControlBar.wnd in the .big archives: wndbuttons.py <dir>..."""
import struct, sys, re, glob, os
for d in sys.argv[1:]:
    for big in sorted(glob.glob(os.path.join(d, '*.big'))):
        with open(big, 'rb') as f:
            data = f.read()
        if data[:4] not in (b'BIGF', b'BIG4'):
            continue
        count, = struct.unpack('>I', data[8:12])
        p = 16
        for _ in range(count):
            off, size = struct.unpack('>II', data[p:p + 8]); p += 8
            e = data.index(b'\0', p); name = data[p:e].decode('latin1'); p = e + 1
            if not name.lower().endswith('controlbar.wnd'):
                continue
            text = data[off:off + size].decode('latin1', 'replace')
            hits = []
            for m in re.finditer(r'NAME\s*=\s*"ControlBar\.wnd:ButtonCommand(\d+)"', text):
                before = text[max(0, m.start() - 600):m.start()]
                sc = re.findall(r'UPPERLEFT:\s*(\d+)\s+(\d+)', before)
                if sc:
                    hits.append((int(m.group(1)), sc[-1]))
            print(os.path.basename(big), name, sorted(hits))
