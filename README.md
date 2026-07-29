# lidar_land_mapping

A browser terrain viewer for Jackson County, North Carolina, built from raw 2025 QL1
LiDAR. It renders a 17.3 sq mi block of the Tuckasegee River valley as a shaded mesh
with the classified point cloud — trees, water, buildings, roads — layered on top.

Everything runs client side in Three.js. There is no tile server and no runtime
dependency on QGIS: `build.py` reduces the raw tiles to two compact binaries that
`index.html` decodes straight into typed arrays.

```
expanded_layers_dem/*.tif ─┐
                           ├─ build.py ─→ site/models/{terrain,points}.bin ─→ index.html
expanded_layers_las/*.las ─┘
```

## Layout

| path | what it is |
| --- | --- |
| `build.py` | the whole data pipeline: DEM + LAS → the two binaries |
| `site/index.html` | the viewer — decoder, scene, camera and UI in one file |
| `site/vendor/` | vendored Three.js and OrbitControls (no CDN; the page is self-contained) |
| `deploy.sh` | push `site/` to the droplet and publish it over Tailscale |

## The data is not in this repo

The 77 LiDAR tiles are 67 GB and the DEM rasters another 308 MB, so both directories
are gitignored, along with the `site/models/` binaries they produce. To rebuild from
scratch you need the source tiles laid out as:

```
expanded_layers_dem/*.tif    77 tiles, 800x800 float32, 3.125 ft cells
expanded_layers_las/*.las    77 tiles, LAS 1.4 point format 6
```

Both sets tile an 11 x 7 grid of 2500 ft squares covering
777500,557500 → 805000,575000 in **EPSG:6543** (NAD83(2011) / North Carolina, US
survey feet), with NAVD88 heights also in US survey feet. They come from the North
Carolina QL1 2025 elevation program as per-tile zip downloads.

## Building

```sh
python3 -m venv .venv
.venv/bin/python -m pip install "numpy>=2" "rasterio>=1.4" "laspy[lazrs]>=2.5"

.venv/bin/python build.py                # both binaries, ~80 s over 2.2B points
.venv/bin/python build.py --only terrain  # ~10 s, skips the LiDAR read entirely
```

`build.py` carries PEP 723 inline metadata, so `uv run build.py` also works without a
manual venv.

Defaults reproduce exactly what is deployed. Both outputs are deterministic: the same
tiles and the same `--seed` give byte-identical files, so a rebuild can be diffed
against what is live.

Useful flags:

| flag | effect |
| --- | --- |
| `--max-dim 1760` | cap on the terrain grid's long side; picks the DEM decimation factor |
| `--budget-scale 2.5` | scales every point budget together, holding density as the area changes |
| `--seed 0` | point sampler seed |
| `--out DIR` | write elsewhere, e.g. to diff against the live files |

### How it reduces the data

**Terrain.** The DEM tiles are mosaicked to 8800 x 5600 cells, then reduced by a 5x5
**block mean** to 1760 x 1120 at 15.6 ft posting. Averaging matters: plain decimation
aliases the ridgelines by up to 73 ft. Heights are quantised to uint16 across the
grid's own min/max, which puts the step at 0.03 ft — far below the data's real
accuracy.

**Points.** 2.2 billion returns are sampled down to 2,875,000 in a single streaming
pass. Each render group gets its own budget, so sparse classes stay legible instead of
being buried by vegetation, and the three vegetation classes share one quota and keep
their natural proportions. Sampling uses vectorised bottom-k selection — every point
draws a random key and the k smallest survive — because textbook reservoir sampling
needs a Python-level loop per point, which is hopeless at this scale.

## Binary formats

Both are little-endian and exist so the viewer can hand buffers to Three.js with no
parsing step.

```
HGT1  magic[4] W:u32 H:u32 L:f64 B:f64 R:f64 T:f64 zmin:f64 zmax:f64
      h:u16[W*H]     row 0 = north edge; elevation = zmin + h/65535*(zmax-zmin)

PCL2  magic[4] n:u32 ox:f32 oy:f32 oz:f32 scale:f32
      q:i16[n*3]     interleaved easting/northing/elevation offsets from the origin,
      cls:u8[n]      in `scale` units; cls is the raw ASPRS class code
```

The point cloud's widest axis is mapped onto ±32000 counts of a shared i16 scale,
leaving ~2% of the range as headroom for tree crowns that overhang the bounding box
and for returns below the terrain's `zmin`.

## Deploying

`deploy.sh` pushes `site/` to a DigitalOcean droplet over Tailscale and serves it on
port **8443**. The port is deliberate — 443 on that host runs Gitea, and Tailscale
Funnel is enabled per *port*, not per path, so funnelling 443 would publish the
private git server as a side effect.

The target host is not in the repo. Create `deploy.env` next to the script (it is
gitignored) or set the variable in your environment:

```sh
echo 'TERRAIN_HOST=root@100.x.y.z' > deploy.env

./deploy.sh            # push and keep it public on :8443 via Funnel
./deploy.sh --private  # push, restricted to the tailnet
```

Public is the default on purpose. `tailscale serve` and `tailscale funnel` both
rewrite a port's config, so a tailnet-only deploy tears down an existing Funnel — a
routine "push the new build" would otherwise quietly pull the site off the internet.
After publishing, the script reads back `AllowFunnel` and exits non-zero if the port
did not land in the requested state, or if 443 ever becomes funnelled.

Destination path and port are constants at the top of the script; point them at your
own box.

## Notes for anyone extending this

- The static file server sends only `Last-Modified` — no `ETag`, no `Cache-Control`.
  Browsers then apply heuristic freshness (~10% of the asset's age) and will serve a
  stale `models/*.bin.gz` for hours after a deploy. The viewer fetches with
  `cache: 'no-cache'` to force revalidation; the server answers `If-Modified-Since`
  with a 304, so an unchanged file costs one round trip and no re-download.
- `resetView()` fits the bounding **box**, not its sphere. The footprint is a thin
  wide plate, and sphere-fitting left roughly half the viewport empty.
- Widen the footprint and two defaults want revisiting together: `--max-dim`, or the
  posting silently coarsens, and `--budget-scale`, or the cloud thins out.

## Data credit

Elevation data from the [NC Department of Public Safety Floodplain Mapping
Program](https://sdd.nc.gov/), QL1 2025 collection. Public domain.
