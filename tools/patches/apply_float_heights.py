"""One-off source patch: the places that read terrain heights as bytes now read them as HeightSample (float).

Run once from the project root:  python tools/patches/apply_float_heights.py
Each substitution states how many times it must match, so a changed upstream file fails loudly instead of being
half patched. Kept in the tree as the record of what the float-height change touched outside WorldHeightMap.
"""
import os
import re
import sys

ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'engine')
GC = 'Core/GameEngineDevice/Source/W3DDevice/GameClient/'
GI = 'Core/GameEngineDevice/Include/W3DDevice/GameClient/'
ZD = 'GeneralsMD/Code/GameEngineDevice/Source/W3DDevice/'

PATCHES = {
    GI + 'BaseHeightMap.h': [
        (r'typedef UnsignedByte HeightSampleType;', 'typedef HeightSample HeightSampleType;', 1),
        (r'UnsignedByte getClipHeight\(Int x, Int y\) const', 'HeightSample getClipHeight(Int x, Int y) const', 1),
    ],
    GC + 'BaseHeightMap.cpp': [
        # the ray / terrain test starts from a box around the whole terrain: its real range, not 0..255 bytes
        (r'const Real mapMinHeight = MAP_HEIGHT_SCALE \* m_map->getMinHeightValue\(\);',
         'const Real mapMinHeight = MAP_HEIGHT_SCALE * (m_map->getMinSample() - 1.0f);', 1),
        (r'const Real mapMaxHeight = MAP_HEIGHT_SCALE \* m_map->getMaxHeightValue\(\);',
         'const Real mapMaxHeight = MAP_HEIGHT_SCALE * (m_map->getMaxSample() + 1.0f);', 1),
        (r'Int i, j, minHt, maxHt;\s+minHt = m_map->getMaxHeightValue\(\);\s+maxHt = 0;',
         'Int i, j;\n\t\tReal minHt = K_HEIGHT_SAMPLE_CEILING;\n\t\tReal maxHt = -K_HEIGHT_SAMPLE_CEILING;', 1),
        (r'Short cur = getClipHeight\(', 'Real cur = getClipHeight(', 1),
        (r'const UnsignedByte\* data = logicHeightMap->getDataPtr\(\);',
         'const HeightSample* data = logicHeightMap->getDataPtr();', 2),
        (r'UnsignedByte d0, d1, d2, d3, d4, d5, d6, d7, d8, d9, d10, d11;',
         'HeightSample d0, d1, d2, d3, d4, d5, d6, d7, d8, d9, d10, d11;', 1),
        (r'UnsignedByte \*data = logicHeightMap->getDataPtr\(\);',
         'const HeightSample *data = logicHeightMap->getDataPtr();', 1),
        (r'UnsignedByte bytes\[4\] =', 'HeightSample bytes[4] =', 1),
        (r'm_maxHeight=\(pow\(256\.0, sizeof\(HeightSampleType\)\)-1\.0\)\*MAP_HEIGHT_SCALE;', 'm_maxHeight=K_MAX_HEIGHT*MAP_HEIGHT_SCALE;\t// a byte map''s range, until a map is loaded', 1),
        (r'Int i, j, minHt, maxHt;\s+minHt = pMap->getMaxHeightValue\(\);\s+maxHt = 0;',
         'Int i, j;\n\t\t\tReal minHt = K_HEIGHT_SAMPLE_CEILING;\n\t\t\tReal maxHt = -K_HEIGHT_SAMPLE_CEILING;', 1),
        (r'Short cur = pMap->getHeight\(i,j\);', 'Real cur = pMap->getHeight(i,j);', 1),
    ],
    GC + 'HeightMap.cpp': [
        (r'Int height = pMap->getHeight\(x, y\);', 'Real height = pMap->getHeight(x, y);', 1),
        (r'Int i, j, minHt;(\s+Real intersectionZ;\s+)minHt = m_map->getMaxHeightValue\(\);',
         r'Int i, j;\n\t\tReal minHt = K_HEIGHT_SAMPLE_CEILING;\1', 1),
        (r'Short cur = m_map->getDisplayHeight\(i,j\);', 'Real cur = m_map->getDisplayHeight(i,j);', 1),
        (r'const UnsignedByte\* data = m_map->getDataPtr\(\);', 'const HeightSample* data = m_map->getDataPtr();', 1),
    ],
    GC + 'W3DTerrainVisual.cpp': [
        (r'UnsignedByte heightOfOriginal = m_logicHeightMap->getHeight\(',
         'HeightSample heightOfOriginal = m_logicHeightMap->getHeight(', 1),
        (r'Int heightToUse = m_clientHeightMap->getHeight\( x, y \) \+ newSpeed ;',
         'HeightSample heightToUse = m_clientHeightMap->getHeight( x, y ) + newSpeed ;', 1),
        (r'if \( heightToUse > 255 \)\s+heightToUse = 255;', '// (heights are floats: no byte ceiling to clamp to)', 1),
        # save games: version 4 stores the float samples, older saves hold one byte per sample
        (r'XferVersion currentVersion = 3;', 'XferVersion currentVersion = 4;', 1),
        (r'UnsignedByte \*data = m_logicHeightMap->getDataPtr\(\);', 'HeightSample *data = m_logicHeightMap->getDataPtr();', 1),
        (r'xfer->xferUser\(data, len\);',
         'if (version >= 4) {\n\t\t\txfer->xferUser(data, len * (Int)sizeof(HeightSample));\n\t\t} else {\n'
         '\t\t\t// GeneralsX @feature ZeroHourEarth 13/11/2026 a save from before float heights: one byte per sample\n'
         '\t\t\tstd::vector<UnsignedByte> bytes(len > 0 ? len : 0);\n'
         '\t\t\tif (xfer->getXferMode() != XFER_LOAD) {\n'
         '\t\t\t\tfor (Int k = 0; k < len; ++k) bytes[k] = (UnsignedByte)(data[k] < 0.0f ? 0.0f : data[k] > 255.0f ? 255.0f : data[k]);\n'
         '\t\t\t}\n'
         '\t\t\txfer->xferUser(bytes.data(), len);\n'
         '\t\t\tif (xfer->getXferMode() == XFER_LOAD) {\n'
         '\t\t\t\tfor (Int k = 0; k < len; ++k) data[k] = (HeightSample)bytes[k];\n'
         '\t\t\t}\n\t\t}\n'
         '\t\tif (xfer->getXferMode() == XFER_LOAD) m_logicHeightMap->recomputeSampleRange();', 1),
    ],
    ZD + 'GameClient/Shadow/W3DShadowMap.cpp': [
        (r'const UnsignedByte \*d = hm->getDataPtr\(\);', 'const HeightSample *d = hm->getDataPtr();', 1),
        (r'UnsignedByte mn = 255, mx = 0;', 'HeightSample mn = K_HEIGHT_SAMPLE_CEILING, mx = -K_HEIGHT_SAMPLE_CEILING;', 1),
    ],
    ZD + 'GameClient/Shadow/W3DVolumetricShadow.cpp': [
        (r'UnsignedByte \*data=map->getDataPtr\(\);', 'const HeightSample *data=map->getDataPtr();', 1),
    ],
    ZD + 'GameLogic/W3DTerrainLogic.cpp': [
        (r'Int i, j, minHt, maxHt;\s+minHt = terrainHeightMap->getMaxHeightValue\(\);\s+maxHt = 0;',
         'Int i, j;\n\t\tReal minHt = K_HEIGHT_SAMPLE_CEILING;\n\t\tReal maxHt = -K_HEIGHT_SAMPLE_CEILING;', 1),
        (r'Short cur = terrainHeightMap->getHeight\(i,j\);', 'Real cur = terrainHeightMap->getHeight(i,j);', 1),
    ],
    ZD + 'GameClient/W3DRoadBuffer.cpp': [
        # "start the minimum above everything and the maximum below everything", which a byte range no longer is
        (r'Real minHeight=m_map->getMaxHeightValue\(\)\*MAP_HEIGHT_SCALE;',
         'Real minHeight=K_HEIGHT_SAMPLE_CEILING;', 2),
        (r'Real maxHeight = m_map->getMinHeightValue\(\)\*MAP_HEIGHT_SCALE;',
         'Real maxHeight = -K_HEIGHT_SAMPLE_CEILING;', 2),
    ],
}


def main():
    failed = False
    for rel, subs in PATCHES.items():
        path = os.path.normpath(os.path.join(ENGINE, rel))
        with open(path, 'r', encoding='latin-1', newline='') as f:
            text = f.read()
        eol = '\r\n' if '\r\n' in text else '\n'
        orig = text
        for pattern, repl, want in subs:
            repl = repl.replace('\n', eol)
            text, n = re.subn(pattern, repl, text)
            if n != want:
                print(f'{rel}: expected {want} match(es), found {n}: {pattern}')
                failed = True
        if failed:
            continue
        if text != orig:
            with open(path, 'w', encoding='latin-1', newline='') as f:
                f.write(text)
            print(f'patched {rel}')
        else:
            print(f'unchanged {rel}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
