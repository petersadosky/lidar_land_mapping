#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "rasterio>=1.4", "laspy[lazrs]>=2.5"]
# ///
"""Build the viewer's binaries from the raw Jackson County tiles.

    expanded_layers_dem/*.tif   ->  site/models/terrain.bin   (HGT1 heightfield)
    expanded_layers_las/*.las   ->  site/models/points.bin    (PCL2 point cloud)

    ./build.py                  # both, into site/models/
    ./build.py --only terrain   # just the mesh (fast; skips the 26 GB LiDAR read)
    ./build.py --out /tmp/x     # write somewhere else, e.g. to diff against the live files

Defaults reproduce exactly what is in site/models today: a 2200x1400 grid at 12.5 ft
posting and 2,875,000 points over the 77-tile footprint. Both outputs are
deterministic -- same tiles and same --seed give byte-identical files.

deploy.sh makes the .gz companions the page actually fetches, so there is no
gzip step here -- run ./deploy.sh after this.

Dependencies: `uv run build.py` reads the header above, or install
numpy/rasterio/laspy into a venv yourself.

Both formats are little-endian, and both exist because they decode into typed
arrays with no parsing: index.html hands the buffer straight to Three.js.

    HGT1  magic[4] W:u32 H:u32 L:f64 B:f64 R:f64 T:f64 zmin:f64 zmax:f64
          h:u16[W*H]        row 0 = north edge, elevation = zmin + h/65535*(zmax-zmin)

    PCL2  magic[4] n:u32 ox:f32 oy:f32 oz:f32 scale:f32
          q:i16[n*3]        interleaved easting/northing/elevation offsets from
          cls:u8[n]         the origin, in `scale` units; cls is the ASPRS code
"""

import argparse
import glob
import os
import struct
import sys

import numpy as np

# The viewer draws one point layer per group, so the sampler budgets per group
# too: the vegetation classes compete for a single quota and keep their natural
# 3/4/5 proportions, while the sparser classes get a floor that keeps them
# legible. Group keys match the `g` field of CLASS in index.html.
#
# Sized for the 77-tile / 17.3 sq mi footprint, which works out to ~13 ft mean
# point spacing. Widen the area again and these want scaling with it -- use
# --budget-scale rather than editing here, so the ratios between groups hold.
GROUPS = {
    5:  {"classes": (3, 4, 5), "budget": 2_000_000, "name": "vegetation"},
    9:  {"classes": (9,),      "budget":   375_000, "name": "water"},
    6:  {"classes": (6,),      "budget":   250_000, "name": "buildings"},
    11: {"classes": (11,),     "budget":   250_000, "name": "roads"},
}

# Quantisation headroom for the point cloud. The widest axis is mapped onto
# +/-Q counts of a shared i16 scale; 32000 leaves ~2% of the i16 range for
# points that overhang the DEM bounding box (tree crowns near the edge, and
# elevations below the terrain's zmin, which is the vertical origin).
Q_SPAN = 32000


def log(msg):
    print(msg, flush=True)


# ---- terrain ---------------------------------------------------------------

def load_mosaic(dem_dir):
    """Stitch the DEM tiles into one north-up float64 array plus its bounds."""
    import rasterio

    paths = sorted(glob.glob(os.path.join(dem_dir, "*.tif")))
    if not paths:
        sys.exit(f"no .tif tiles in {dem_dir}")

    tiles = []
    for p in paths:
        with rasterio.open(p) as d:
            if d.count != 1:
                sys.exit(f"{p}: expected 1 band, got {d.count}")
            tiles.append((p, d.bounds, d.width, d.height, d.res, d.nodata))

    shapes = {(t[2], t[3]) for t in tiles}
    res = {t[4] for t in tiles}
    if len(shapes) != 1 or len(res) != 1:
        sys.exit(f"tiles are not uniform: sizes {shapes}, resolutions {res}")
    (tw, th), = shapes

    L = min(t[1].left for t in tiles)
    B = min(t[1].bottom for t in tiles)
    R = max(t[1].right for t in tiles)
    T = max(t[1].top for t in tiles)
    step_x = tiles[0][1].right - tiles[0][1].left
    step_y = tiles[0][1].top - tiles[0][1].bottom

    ncol, nrow = round((R - L) / step_x), round((T - B) / step_y)
    if ncol * nrow != len(tiles):
        sys.exit(f"{len(tiles)} tiles do not fill a {ncol}x{nrow} grid -- gaps or overlaps")

    mosaic = np.empty((nrow * th, ncol * tw), np.float32)
    seen = np.zeros((nrow, ncol), bool)
    for p, bnds, _, _, _, nodata in tiles:
        c, r = round((bnds.left - L) / step_x), round((T - bnds.top) / step_y)
        if not (0 <= c < ncol and 0 <= r < nrow) or seen[r, c]:
            sys.exit(f"{p}: tile is off-grid or duplicates slot ({r},{c})")
        with rasterio.open(p) as d:
            a = d.read(1)
        if nodata is not None:
            a = np.where(a == nodata, np.nan, a)
        mosaic[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = a
        seen[r, c] = True

    log(f"    {len(tiles)} tiles -> {mosaic.shape[1]}x{mosaic.shape[0]} cells "
        f"@ {(R - L) / mosaic.shape[1]:.3f} ft")
    return mosaic, (L, B, R, T)


def choose_block(shape, max_dim):
    """Smallest integer decimation that divides both axes and fits max_dim."""
    h, w = shape
    for f in range(1, min(h, w) + 1):
        if h % f == 0 and w % f == 0 and max(h // f, w // f) <= max_dim:
            return f
    sys.exit(f"no integer block factor takes {w}x{h} under {max_dim}; pass --block")


def build_terrain(dem_dir, out_path, max_dim, block):
    log("==> terrain")
    mosaic, (L, B, R, T) = load_mosaic(dem_dir)

    f = block or choose_block(mosaic.shape, max_dim)
    if mosaic.shape[0] % f or mosaic.shape[1] % f:
        sys.exit(f"block {f} does not divide {mosaic.shape[1]}x{mosaic.shape[0]}")
    H, W = mosaic.shape[0] // f, mosaic.shape[1] // f

    # Block *mean*, not decimation: at a 5x reduction, sampling one cell in 25
    # aliases the ridgelines badly (73 ft of error against the averaged grid).
    # float64 accumulation -- a float32 sum over 25 cells loses enough precision
    # to shift roughly 0.5% of the codes by one quantisation step.
    blocks = mosaic.reshape(H, f, W, f).transpose(0, 2, 1, 3).reshape(H, W, f * f)
    holes = int(np.isnan(mosaic).sum())
    if holes:
        log(f"    {holes:,} nodata cells -> averaging around them")
        with np.errstate(invalid="ignore"):
            grid = np.nanmean(blocks, axis=2, dtype=np.float64)
        empty = np.isnan(grid)
        if empty.any():
            grid[empty] = np.nanmean(grid)
            log(f"    {int(empty.sum()):,} output cells had no data at all -> filled with the mean")
    else:
        grid = blocks.mean(axis=2, dtype=np.float64)

    zmin, zmax = float(grid.min()), float(grid.max())
    # Round rather than truncate: truncation biases every cell half a step low.
    h = np.rint((grid - zmin) / (zmax - zmin) * 65535).astype(np.uint16)

    with open(out_path, "wb") as fh:
        fh.write(b"HGT1" + struct.pack("<II", W, H))
        fh.write(struct.pack("<6d", L, B, R, T, zmin, zmax))
        fh.write(h.tobytes())

    log(f"    block {f} -> {W}x{H} @ {(R - L) / (W - 1):.3f} ft posting")
    log(f"    elevation {zmin:.2f}..{zmax:.2f} ft ({zmax - zmin:.0f} ft relief), "
        f"step {(zmax - zmin) / 65535:.4f} ft")
    log(f"    {out_path}  {os.path.getsize(out_path) / 1e6:.1f} MB")
    return (L, B, R, T), zmin


# ---- points ----------------------------------------------------------------

class Reservoir:
    """A uniform sample of `budget` points drawn in a single streaming pass.

    Every point gets a random key and we keep the `budget` smallest keys seen so
    far. That is a sample without replacement, same as textbook reservoir
    sampling, but it stays vectorised -- Algorithm R needs a Python-level loop
    per point, which is hopeless at 770M points.
    """

    def __init__(self, budget, rng):
        self.budget, self.rng, self.seen = budget, rng, 0
        self.keys = np.empty(0, np.float64)
        self.q = np.empty((0, 3), np.int16)
        self.cls = np.empty(0, np.uint8)

    def offer(self, q, cls):
        self.seen += len(q)
        keys = self.rng.random(len(q))
        if len(self.keys):
            keys = np.concatenate((self.keys, keys))
            q = np.concatenate((self.q, q))
            cls = np.concatenate((self.cls, cls))
        if len(keys) > self.budget:
            keep = np.argpartition(keys, self.budget)[:self.budget]
            keys, q, cls = keys[keep], q[keep], cls[keep]
        self.keys, self.q, self.cls = keys, q, cls

    def finish(self):
        """Sorted by key so the output depends only on which points were drawn."""
        order = np.argsort(self.keys, kind="stable")
        return self.q[order], self.cls[order]


def build_points(las_glob, out_path, bounds, zmin, seed, chunk, budget_scale):
    import laspy

    log("==> points")
    L, B, R, T = bounds
    paths = sorted(glob.glob(las_glob))
    if not paths:
        sys.exit(f"no LiDAR tiles matching {las_glob}")

    scale = np.float32((R - L) / Q_SPAN)
    ox, oy, oz = np.float32(L), np.float32(B), np.float32(zmin)
    wanted = {c: g for g, spec in GROUPS.items() for c in spec["classes"]}
    lut = np.zeros(256, np.uint8)          # class code -> group, 0 = discard
    for c, g in wanted.items():
        lut[c] = g

    # One RNG stream per group keeps a group's sample independent of how many
    # points the other groups happened to see first.
    pools = {g: Reservoir(round(spec["budget"] * budget_scale), np.random.default_rng(seed + g))
             for g, spec in GROUPS.items()}
    total = clipped = 0

    for i, p in enumerate(paths, 1):
        with laspy.open(p) as fh:
            hdr = fh.header
            sx, sy, sz = hdr.scales
            hx, hy, hz = hdr.offsets
            for ch in fh.chunk_iterator(chunk):
                total += len(ch)
                grp = lut[np.asarray(ch.classification)]
                keep = grp != 0
                if not keep.any():
                    continue
                grp = grp[keep]
                east = np.asarray(ch.X)[keep] * sx + hx
                north = np.asarray(ch.Y)[keep] * sy + hy
                elev = np.asarray(ch.Z)[keep] * sz + hz

                inside = (east >= L) & (east <= R) & (north >= B) & (north <= T)
                if not inside.all():
                    grp, east, north, elev = grp[inside], east[inside], north[inside], elev[inside]

                qf = np.empty((len(east), 3), np.float64)
                np.rint((east - ox) / scale, out=qf[:, 0])
                np.rint((north - oy) / scale, out=qf[:, 1])
                np.rint((elev - oz) / scale, out=qf[:, 2])
                out_of_range = (qf < -32768) | (qf > 32767)
                if out_of_range.any():
                    clipped += int(out_of_range.any(axis=1).sum())
                    np.clip(qf, -32768, 32767, out=qf)
                q = qf.astype(np.int16)

                cls = np.asarray(ch.classification)[keep]
                if not inside.all():
                    cls = cls[inside]
                for g, pool in pools.items():
                    m = grp == g
                    if m.any():
                        pool.offer(q[m], cls[m])
        log(f"    [{i:2d}/{len(paths)}] {os.path.basename(p)}  {total:,} points read")

    if clipped:
        log(f"    !! {clipped:,} points fell outside the i16 range and were clamped")

    parts = [pool.finish() for _, pool in sorted(pools.items())]
    q = np.concatenate([p[0] for p in parts])
    cls = np.concatenate([p[1] for p in parts])
    n = len(q)

    with open(out_path, "wb") as fh:
        fh.write(b"PCL2" + struct.pack("<I", n))
        fh.write(struct.pack("<4f", ox, oy, oz, scale))
        fh.write(q.tobytes())
        fh.write(cls.tobytes())

    for g, pool in sorted(pools.items()):
        log(f"    {GROUPS[g]['name']:<11s} {len(pool.cls):>9,} of {pool.seen:>12,} "
            f"({100 * len(pool.cls) / max(pool.seen, 1):.2f}% kept)")
    log(f"    origin {ox:.1f},{oy:.1f},{oz:.2f} ft  scale {scale} ft/count")
    log(f"    {n:,} points from {total:,}  ->  {out_path}  "
        f"{os.path.getsize(out_path) / 1e6:.1f} MB")


# ---- cli -------------------------------------------------------------------

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dem-dir", default=os.path.join(here, "expanded_layers_dem"))
    ap.add_argument("--las-glob", default=os.path.join(here, "expanded_layers_las", "*.las"),
                    help="quote it to keep the shell from expanding it")
    ap.add_argument("--out", default=os.path.join(here, "site", "models"))
    ap.add_argument("--only", choices=("terrain", "points", "both"), default="both")
    ap.add_argument("--max-dim", type=int, default=2200,
                    help="cap on the terrain grid's long side. 2200 gives 12.5 ft posting "
                         "across the 77-tile footprint (default: 2200)")
    ap.add_argument("--block", type=int, default=None,
                    help="force the DEM decimation factor instead of deriving it")
    ap.add_argument("--seed", type=int, default=0, help="point sampler seed (default: 0)")
    ap.add_argument("--budget-scale", type=float, default=1.0,
                    help="multiply every group budget, to hold point density roughly "
                         "constant when the tile footprint changes (default: 1.0)")
    ap.add_argument("--chunk", type=int, default=5_000_000, help="LiDAR points per read")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    terrain_path = os.path.join(args.out, "terrain.bin")
    points_path = os.path.join(args.out, "points.bin")

    if args.only in ("terrain", "both"):
        bounds, zmin = build_terrain(args.dem_dir, terrain_path, args.max_dim, args.block)
    else:
        # The point cloud is positioned against the terrain's own origin, so
        # rebuilding it alone means reading those five numbers back out.
        with open(terrain_path, "rb") as fh:
            head = fh.read(60)
        if head[:4] != b"HGT1":
            sys.exit(f"{terrain_path} is not an HGT1 file -- build the terrain first")
        bounds = struct.unpack_from("<4d", head, 12)
        zmin = struct.unpack_from("<d", head, 44)[0]

    if args.only in ("points", "both"):
        build_points(args.las_glob, points_path, bounds, zmin, args.seed, args.chunk,
                     args.budget_scale)

    log("==> done. run ./deploy.sh to gzip and publish.")


if __name__ == "__main__":
    main()
