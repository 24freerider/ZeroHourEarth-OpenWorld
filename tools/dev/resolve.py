"""Resolve diff3 conflict hunks. usage: resolve.py FILE SIDES   (SIDES: e.g. "ttb" -> hunk1 theirs, hunk2 theirs, hunk3 both(ours then theirs))
o = ours, t = theirs, b = ours then theirs, B = theirs then ours, s = show (leave as is)"""
import sys
path, sides = sys.argv[1], sys.argv[2]
lines = open(path, encoding='latin-1', newline='').read().split('\n')
out, i, h = [], 0, 0
while i < len(lines):
    l = lines[i]
    if l.startswith('<<<<<<< '):
        o, b, t, part = [], [], [], 'o'
        start = i
        i += 1
        while not lines[i].startswith('>>>>>>> '):
            s = lines[i]
            if s.startswith('||||||| '): part = 'b'
            elif s == '=======' or s == '=======\r': part = 't'
            else: {'o': o, 'b': b, 't': t}[part].append(s)
            i += 1
        c = sides[h] if h < len(sides) else 's'
        h += 1
        if c == 'o': out += o
        elif c == 't': out += t
        elif c == 'b': out += o + t
        elif c == 'B': out += t + o
        else: out += lines[start:i + 1]
        i += 1
        continue
    out.append(l)
    i += 1
open(path, 'w', encoding='latin-1', newline='').write('\n'.join(out))
print(path, 'hunks', h)
