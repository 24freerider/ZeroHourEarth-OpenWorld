import os, re, sys
sys.path.insert(0, r'C:\Users\yamak\Desktop\ZeroHourEarth-OpenWorld\generator')
sys.path.insert(0, r'C:\Users\yamak\Desktop\ZeroHourEarth-OpenWorld\worldserver')
import catalog
from world import load_config
cfg = load_config(r'C:\Users\yamak\Desktop\ZeroHourEarth-OpenWorld')
for d in cfg['game_dirs']:
    if not os.path.isdir(d): continue
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith('.big'): continue
        try: data, files = catalog.read_big(os.path.join(d, fn))
        except Exception: continue
        for name, (off, sz) in files.items():
            if 'commandmap' in name:
                t = data[off:off + sz].decode('latin1')
                for m in re.finditer(r'^CommandMap\s+(\w+)(.*?)^End', t, re.M | re.S):
                    if re.search(r'Key\s*=\s*KEY_(UP|DOWN|LEFT|RIGHT)\b', m.group(2)):
                        print(fn, name, m.group(1), ' '.join(m.group(2).split()))
