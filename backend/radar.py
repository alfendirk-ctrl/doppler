"""
radar.py — KNMI HDF5 radarverwerking voor Doppler
Leest KNMI reflectivity/nowcast HDF5, reprojecteert naar web-mercator bounds,
en rendert een transparante PNG met dBZ-kleurschaal.
"""
import io
import numpy as np
import h5py
from PIL import Image
from pyproj import Transformer

# KNMI NL25 polar-stereographic projection (uit HDF5-spec / gdalinfo)
KNMI_PROJ4 = "+proj=stere +lat_0=90 +lon_0=0 +lat_ts=60 +a=6378137 +b=6356752 +x_0=0 +y_0=0"

# dBZ kleurschaal (Doppler huisstijl, licht->zwaar). Stops in dBZ -> RGBA.
DBZ_STOPS = [
    (5,   (168, 213, 255, 130)),
    (15,  (91,  173, 240, 170)),
    (25,  (63,  201, 122, 200)),
    (35,  (195, 232, 74,  220)),
    (45,  (245, 208, 32,  235)),
    (55,  (240, 128, 32,  245)),
    (65,  (226, 59,  59,  255)),
    (75,  (184, 50,  208, 255)),
]

def _build_lut():
    """256-entry RGBA LUT, index = clamp(dBZ,0,80)*256/80."""
    lut = np.zeros((256, 4), dtype=np.uint8)
    xs = [s[0] for s in DBZ_STOPS]
    cols = np.array([s[1] for s in DBZ_STOPS], dtype=float)
    for i in range(256):
        dbz = i / 255.0 * 80.0
        if dbz < xs[0]:
            lut[i] = (0, 0, 0, 0)   # onder drempel = transparant
            continue
        # interpoleer tussen stops
        if dbz >= xs[-1]:
            lut[i] = cols[-1].astype(np.uint8); continue
        for k in range(len(xs) - 1):
            if xs[k] <= dbz < xs[k + 1]:
                t = (dbz - xs[k]) / (xs[k + 1] - xs[k])
                lut[i] = (cols[k] * (1 - t) + cols[k + 1] * t).astype(np.uint8)
                break
    return lut

LUT = _build_lut()

# Web-mercator straal (EPSG:3857)
_R_MERC = 6378137.0


def target_latitudes(north, south, out_h):
    """
    De breedtegraden van de rijen van het doelraster.

    De app plaatst de PNG met L.imageOverlay op een lat/lon-rechthoek, en
    Leaflet rekt dat beeld lineair uit in *geprojecteerde* ruimte. Een raster
    met gelijke stappen in breedtegraad komt daardoor niet overeen met waar
    Leaflet de rijen tekent: over Nederland loopt dat op tot enkele kilometers,
    het grootst in het midden van het domein. Daarom verdelen we gelijkmatig
    over mercator-Y en rekenen we dat terug naar breedtegraad.
    """
    y_north = _R_MERC * np.log(np.tan(np.pi / 4 + np.radians(north) / 2))
    y_south = _R_MERC * np.log(np.tan(np.pi / 4 + np.radians(south) / 2))
    y = np.linspace(y_north, y_south, out_h)
    return np.degrees(2 * np.arctan(np.exp(y / _R_MERC)) - np.pi / 2)


def read_knmi_hdf5(raw_bytes, image_group="image1"):
    """
    Lees een KNMI HDF5 radarbestand.
    Returns: (data float32 in dBZ, proj4 str, (x0,y0,x1,y1) extent in proj-meters, datetime str)
    KNMI slaat reflectiviteit op als 'image_data' met calibratie:
      dBZ = PV * gain + offset  (uit calibration/calibration_formulas of GEO/cal attrs)
    """
    with h5py.File(io.BytesIO(raw_bytes), "r") as f:
        img = f[image_group]
        pv = np.array(img["image_data"]).astype(np.float32)

        # calibratie
        gain, offset = 0.5, -32.0  # KNMI reflectivity default (PV->dBZ)
        cal = img.get("calibration")
        if cal is not None:
            a = cal.attrs
            if "calibration_formulas" in a:
                # formule string "GEO = 0.500000*PV + -32.000000"
                try:
                    s = a["calibration_formulas"]
                    s = s.decode() if isinstance(s, bytes) else s
                    rhs = s.split("=")[1]
                    gain = float(rhs.split("*")[0])
                    offset = float(rhs.split("+")[1])
                except Exception:
                    pass
            else:
                gain = float(a.get("calibration_out_of_image", gain)) if False else gain

        # missing/no-data
        nodata = 255
        geo = f["geographic"]
        ga = geo.attrs
        # grid afmeting
        cols = int(ga.get("geo_number_columns", pv.shape[1]))
        rows = int(ga.get("geo_number_rows", pv.shape[0]))

        # proj4
        mp = geo.get("map_projection")
        proj4 = KNMI_PROJ4
        if mp is not None and "projection_proj4_params" in mp.attrs:
            p = mp.attrs["projection_proj4_params"]
            proj4 = p.decode() if isinstance(p, bytes) else p

        # hoekcoördinaten in lat/lon -> we hebben proj-extent nodig.
        # geo_product_corners = [lon_ll,lat_ll, lon_ul,lat_ul, lon_ur,lat_ur, lon_lr,lat_lr]
        corners = ga.get("geo_product_corners")
        corners = np.array(corners).astype(float) if corners is not None else None

        # datum/tijd
        dt = ""
        try:
            dt = f["overview"].attrs.get("product_datetime_start", b"")
            dt = dt.decode() if isinstance(dt, bytes) else dt
        except Exception:
            pass

        # dBZ
        dbz = pv * gain + offset
        dbz[pv == nodata] = np.nan

        return dbz, proj4, corners, cols, rows, dt


def reproject_to_png(dbz, proj4, corners, out_w=900, out_h=900):
    """
    Reprojecteer KNMI-grid (polar stereo) naar EPSG:4326 lat/lon raster
    en kleur met dBZ LUT. Returns (png_bytes, (south, west, north, east)).
    We samplen het doelraster (regelmatige lat/lon) terug naar bron-pixels
    via inverse transform (nearest neighbour) — snel en goed genoeg.
    """
    rows, cols = dbz.shape

    # corners zijn lon/lat van de vier hoeken (ll, ul, ur, lr)
    lons = corners[0::2]; lats = corners[1::2]
    west, east = float(lons.min()), float(lons.max())
    south, north = float(lats.min()), float(lats.max())

    # bron: proj-coördinaten van het grid. Bouw transform lonlat->proj.
    to_proj = Transformer.from_crs("EPSG:4326", proj4, always_xy=True)

    # bereken proj-extent uit de hoeken
    cxs, cys = to_proj.transform(lons, lats)
    px0, px1 = min(cxs), max(cxs)
    py0, py1 = min(cys), max(cys)

    # Doelraster. In mercator is x lineair in lengtegraad, dus tlon is gewoon
    # regelmatig; de rijen verdelen we gelijkmatig over mercator-Y zodat het
    # beeld past op hoe Leaflet het straks uitrekt.
    tlon = np.linspace(west, east, out_w)
    tlat = target_latitudes(north, south, out_h)  # noord boven
    LON, LAT = np.meshgrid(tlon, tlat)

    # naar proj
    PX, PY = to_proj.transform(LON.ravel(), LAT.ravel())
    PX = PX.reshape(out_h, out_w); PY = PY.reshape(out_h, out_w)

    # proj -> bron pixelindex. KNMI grid: x neemt toe naar rechts, y neemt af naar beneden.
    col_idx = (PX - px0) / (px1 - px0) * (cols - 1)
    row_idx = (py1 - PY) / (py1 - py0) * (rows - 1)

    ci = np.clip(np.round(col_idx).astype(int), 0, cols - 1)
    ri = np.clip(np.round(row_idx).astype(int), 0, rows - 1)
    sampled = dbz[ri, ci]

    # naar LUT-index
    idx = np.clip(sampled / 80.0 * 255.0, 0, 255)
    idx = np.where(np.isnan(sampled), 0, idx).astype(np.uint8)
    rgba = LUT[idx]
    # NaN -> volledig transparant
    rgba[np.isnan(sampled)] = (0, 0, 0, 0)

    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), (south, west, north, east)


def detect_cells(dbz, corners, threshold=45.0, min_size=4):
    """
    Detecteer stormcellen: aaneengesloten gebieden boven dBZ-drempel.
    Returns lijst van {lat, lon, max_dbz, size}.
    Gebruikt scipy label.
    """
    from scipy import ndimage
    rows, cols = dbz.shape
    mask = np.nan_to_num(dbz, nan=-99) >= threshold
    lbl, n = ndimage.label(mask)
    if n == 0:
        return []
    lons = corners[0::2]; lats = corners[1::2]
    west, east = float(lons.min()), float(lons.max())
    south, north = float(lats.min()), float(lats.max())
    cells = []
    for i in range(1, n + 1):
        ys, xs = np.where(lbl == i)
        if len(xs) < min_size:
            continue
        cy, cx = ys.mean(), xs.mean()
        lon = west + cx / (cols - 1) * (east - west)
        lat = north - cy / (rows - 1) * (north - south)
        cells.append({
            "lat": round(float(lat), 4),
            "lon": round(float(lon), 4),
            "max_dbz": round(float(np.nanmax(dbz[ys, xs])), 1),
            "size": int(len(xs)),
        })
    cells.sort(key=lambda c: -c["max_dbz"])
    return cells[:40]
