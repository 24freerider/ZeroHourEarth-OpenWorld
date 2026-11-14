"""Mapbox Vector Tile decoder (pure Python): layers -> features with geometry and properties.

Only what the world server needs: every geometry type, every value type, and a layer filter so labels and
addresses are skipped without being parsed.

    decode(data, layers={'streets', 'buildings'}) ->
        {layer: (extent, [(gtype, parts, props), ...])}

gtype 1 = points   parts = [[(x, y), ...]]
gtype 2 = lines    parts = [[(x, y), ...], ...]            one list per line
gtype 3 = polygons parts = [[exterior, hole, ...], ...]    one list of rings per polygon
Coordinates are tile units (0 .. extent, y down); features may reach slightly outside (the tile buffer).
"""
import struct


def _varint(b, i):
    shift = result = 0
    while True:
        c = b[i]
        i += 1
        result |= (c & 0x7F) << shift
        if not c & 0x80:
            return result, i
        shift += 7


def _fields(b):
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
            yield field, v
        elif wire == 2:
            ln, i = _varint(b, i)
            yield field, b[i:i + ln]
            i += ln
        elif wire == 1:
            yield field, b[i:i + 8]
            i += 8
        elif wire == 5:
            yield field, b[i:i + 4]
            i += 4
        else:
            raise ValueError('unsupported protobuf wire type %d' % wire)


def _packed(b):
    out, i, n = [], 0, len(b)
    while i < n:
        c = b[i]
        i += 1
        if c < 0x80:
            out.append(c)
            continue
        result, shift = c & 0x7F, 7
        while True:
            c = b[i]
            i += 1
            result |= (c & 0x7F) << shift
            if c < 0x80:
                break
            shift += 7
        out.append(result)
    return out


def _value(b):
    for field, v in _fields(b):
        if field == 1: return v.decode('utf-8', 'replace')
        if field == 2: return struct.unpack('<f', v)[0]
        if field == 3: return struct.unpack('<d', v)[0]
        if field in (4, 5): return v
        if field == 6: return (v >> 1) ^ -(v & 1)
        if field == 7: return bool(v)
    return None


def _ring_area2(ring):
    s = 0
    x0, y0 = ring[-1]
    for x1, y1 in ring:
        s += x0 * y1 - x1 * y0
        x0, y0 = x1, y1
    return s


def _geometry(gtype, cmds):
    """Decode the command stream into paths."""
    paths, cur = [], []
    x = y = 0
    i, n = 0, len(cmds)
    while i < n:
        c = cmds[i]
        i += 1
        cid, cnt = c & 7, c >> 3
        if cid == 1:                                   # MoveTo
            for _ in range(cnt):
                dx, dy = cmds[i], cmds[i + 1]
                i += 2
                x += (dx >> 1) ^ -(dx & 1)
                y += (dy >> 1) ^ -(dy & 1)
                if gtype == 1:
                    cur.append((x, y))
                else:
                    if cur: paths.append(cur)
                    cur = [(x, y)]
        elif cid == 2:                                 # LineTo
            for _ in range(cnt):
                dx, dy = cmds[i], cmds[i + 1]
                i += 2
                x += (dx >> 1) ^ -(dx & 1)
                y += (dy >> 1) ^ -(dy & 1)
                cur.append((x, y))
        elif cid == 7:                                 # ClosePath
            if cur: paths.append(cur)
            cur = []
    if cur: paths.append(cur)
    if gtype != 3:
        return [p for p in paths if (len(p) >= 2 or gtype == 1)]
    polys = []
    for ring in paths:
        if len(ring) < 3:
            continue
        if _ring_area2(ring) > 0 or not polys:         # exterior ring (positive area in y-down tile space)
            polys.append([ring])
        else:
            polys[-1].append(ring)
    return polys


def decode(data, layers=None):
    out = {}
    for field, layer in _fields(data):
        if field != 3:
            continue
        name, extent, keys, values, feats = '', 4096, [], [], []
        skip = False
        for lf, v in _fields(layer):
            if lf == 1:
                name = v.decode('utf-8', 'replace')
                if layers is not None and name not in layers:
                    skip = True
                    break
            elif lf == 2: feats.append(v)
            elif lf == 3: keys.append(v.decode('utf-8', 'replace'))
            elif lf == 4: values.append(v)
            elif lf == 5: extent = v
        if skip:
            continue
        decoded = [None] * len(values)
        features = []
        for ft in feats:
            gtype, geom, tags = 0, b'', b''
            for ff, v in _fields(ft):
                if ff == 2: tags = v
                elif ff == 3: gtype = v
                elif ff == 4: geom = v
            if gtype not in (1, 2, 3):
                continue
            props = {}
            t = _packed(tags)
            for k in range(0, len(t) - 1, 2):
                vi = t[k + 1]
                if decoded[vi] is None:
                    decoded[vi] = (_value(values[vi]),)
                props[keys[t[k]]] = decoded[vi][0]
            parts = _geometry(gtype, _packed(geom))
            if parts:
                features.append((gtype, parts, props))
        out[name] = (extent, features)
    return out
