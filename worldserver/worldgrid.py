"""The world grid: where real places land in game units, and how the world is cut into chunks.

Game units as in the map generator at 1:1: one terrain cell = 10 units = 2.5 m, so 1 unit = 0.25 m.

    world x east, world y north, in units, measured from the grid anchor (a lat/lon)
    vertex (gi, gj)  = world (gi * 10, gj * 10)          heights live on vertices
    cell   (gi, gj)  = the square between vertices (gi, gj) and (gi + 1, gj + 1)
    chunk  (cx, cy)  = vertices and cells gi in [cx * N, cx * N + N), gj likewise   (N = 64 -> 160 m)

Every vertex and every cell belongs to exactly one chunk, so neighbouring chunks never disagree about a shared
edge: there is no shared edge.

The anchor is the spawn place snapped to a coarse lat/lon lattice. Two sessions that start in the same town get
the same anchor, therefore the same chunk coordinates, and reuse each other's generated chunks and saved state.
"""
import math

import numpy as np

CELL_UNITS = 10.0
ANCHOR_STEP_DEG = 0.05          # about 5.5 km north-south


def snap_anchor(lat, lon):
    k = 1.0 / ANCHOR_STEP_DEG
    return round(round(lat * k) / k, 6), round(round(lon * k) / k, 6)


class WorldGrid:
    def __init__(self, lat0, lon0, metres_per_cell=2.5, chunk_cells=64):
        self.lat0, self.lon0 = float(lat0), float(lon0)
        self.mpc = float(metres_per_cell)
        self.upm = CELL_UNITS / self.mpc                     # game units per metre
        self.n = int(chunk_cells)
        self.chunk_units = self.n * CELL_UNITS
        phi = math.radians(self.lat0)
        # metres per degree on the WGS84 ellipsoid at the anchor latitude
        self.ky = 111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
        self.kx = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
        self.key = f'{self.lat0:+09.4f}{self.lon0:+010.4f}_m{self.mpc:g}_n{self.n}'

    # ---- projection (numpy friendly)
    def to_units(self, lat, lon):
        return ((np.asarray(lon, float) - self.lon0) * (self.kx * self.upm),
                (np.asarray(lat, float) - self.lat0) * (self.ky * self.upm))

    def from_units(self, x, y):
        return (self.lat0 + np.asarray(y, float) / (self.ky * self.upm),
                self.lon0 + np.asarray(x, float) / (self.kx * self.upm))

    # ---- chunks
    def chunk_of(self, x, y):
        return int(math.floor(x / self.chunk_units)), int(math.floor(y / self.chunk_units))

    def chunk_origin(self, cx, cy):
        return cx * self.chunk_units, cy * self.chunk_units

    def chunk_bbox_latlon(self, cx, cy, margin_units=0.0):
        """(south, west, north, east) of a chunk plus a margin."""
        x0, y0 = self.chunk_origin(cx, cy)
        s, w = self.from_units(x0 - margin_units, y0 - margin_units)
        n, e = self.from_units(x0 + self.chunk_units + margin_units, y0 + self.chunk_units + margin_units)
        return float(s), float(w), float(n), float(e)
