"""
Controleert de georeferentie van de PNG-overlays.

De app plaatst de PNG met L.imageOverlay op een lat/lon-rechthoek. Leaflet rekt
dat beeld lineair uit in *geprojecteerde* ruimte (web-mercator), niet lineair in
breedtegraad. Bouwt de backend zijn doelraster met gelijke stappen in
breedtegraad, dan staat elke rij op een andere plek dan waar Leaflet 'm tekent.
Over Nederland loopt dat op tot kilometers.

Draaien:  python3 test_reproject.py
"""
import numpy as np

import radar

# KNMI NL25-composiet, ruwweg
SOUTH, NORTH = 49.362, 55.974
WEST, EAST = 0.0, 10.856
H = 1000

R_MERC = 6378137.0


def merc_y(lat_deg):
    lat = np.radians(np.asarray(lat_deg, dtype=float))
    return R_MERC * np.log(np.tan(np.pi / 4 + lat / 2))


def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(np.asarray(y, dtype=float) / R_MERC)) - np.pi / 2)


def leaflet_row_latitudes(h=H):
    """De breedtegraad waar Leaflet rij i van het beeld daadwerkelijk tekent."""
    y = np.linspace(merc_y(NORTH), merc_y(SOUTH), h)
    return inv_merc_y(y)


def km_per_degree_lat():
    return 111.32


def test_row_alignment():
    """De rijen van het doelraster moeten liggen waar Leaflet ze tekent."""
    drawn = leaflet_row_latitudes()

    # zo bouwde de backend het raster vóór de correctie
    lat_linear = np.linspace(NORTH, SOUTH, H)
    err_old = np.abs(lat_linear - drawn) * km_per_degree_lat()

    # zoals radar.py het nu bouwt
    actual = radar.target_latitudes(NORTH, SOUTH, H)
    err_new = np.abs(actual - drawn) * km_per_degree_lat()

    print(f"lineair in breedtegraad : max {err_old.max():6.2f} km, "
          f"gemiddeld {err_old.mean():5.2f} km")
    print(f"lineair in mercator     : max {err_new.max():6.4f} km, "
          f"gemiddeld {err_new.mean():5.4f} km")

    assert err_old.max() > 3.0, "verwachtte een meetbare fout in de oude aanpak"
    assert err_new.max() < 0.01, f"rijen staan nog steeds scheef: {err_new.max()} km"
    return err_old.max(), err_new.max()


def test_known_point_lands_right():
    """
    Legt een echo op een bekende positie in het bronraster en controleert dat
    die in de PNG op de rij staat waar Leaflet die breedtegraad tekent.
    """
    rows = cols = 400
    dbz = np.full((rows, cols), np.nan, dtype=np.float32)

    proj4 = radar.KNMI_PROJ4
    corners = np.array([WEST, SOUTH, WEST, NORTH, EAST, NORTH, EAST, SOUTH], dtype=float)

    # doelpunt: Utrecht
    target_lat, target_lon = 52.09, 5.11

    from pyproj import Transformer
    to_proj = Transformer.from_crs("EPSG:4326", proj4, always_xy=True)
    cxs, cys = to_proj.transform(corners[0::2], corners[1::2])
    px0, px1 = min(cxs), max(cxs)
    py0, py1 = min(cys), max(cys)

    tx, ty = to_proj.transform(target_lon, target_lat)
    col = int(round((tx - px0) / (px1 - px0) * (cols - 1)))
    row = int(round((py1 - ty) / (py1 - py0) * (rows - 1)))
    dbz[row - 1:row + 2, col - 1:col + 2] = 60.0   # felle echo van 3x3 bronpixels

    png, bounds = radar.reproject_to_png(dbz, proj4, corners, out_w=H, out_h=H)
    south, west, north, east = bounds

    import io
    from PIL import Image
    img = np.array(Image.open(io.BytesIO(png)))
    ys, xs = np.where(img[:, :, 3] > 0)
    assert len(ys) > 0, "echo helemaal niet teruggevonden in de PNG"

    hit_row = ys.mean()
    drawn = leaflet_row_latitudes(H)
    lat_where_drawn = drawn[int(round(hit_row))]
    err_km = abs(lat_where_drawn - target_lat) * km_per_degree_lat()

    lon_at_hit = west + xs.mean() / (H - 1) * (east - west)
    err_lon_km = abs(lon_at_hit - target_lon) * 111.32 * np.cos(np.radians(target_lat))

    print(f"echo op {target_lat:.2f}N {target_lon:.2f}E -> "
          f"afwijking noord-zuid {err_km:.2f} km, oost-west {err_lon_km:.2f} km")
    assert err_km < 2.0, f"echo staat {err_km:.2f} km verkeerd in noord-zuid"
    assert err_lon_km < 2.0, f"echo staat {err_lon_km:.2f} km verkeerd in oost-west"


def _synthetic_knmi_hdf5():
    """Bouwt een bestand in de vorm die KNMI levert, met twee echo's erin."""
    import io
    import h5py

    rows = cols = 300
    pv = np.full((rows, cols), 255, dtype=np.uint8)   # 255 = geen data
    # dBZ = PV * 0.5 - 32, dus PV 174 ~ 55 dBZ en PV 150 ~ 43 dBZ
    pv[100:112, 120:132] = 174
    pv[200:206, 60:66] = 150

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        g = f.create_group("image1")
        g.create_dataset("image_data", data=pv)
        cal = g.create_group("calibration")
        cal.attrs["calibration_formulas"] = np.bytes_("GEO = 0.500000*PV + -32.000000")
        geo = f.create_group("geographic")
        geo.attrs["geo_number_columns"] = cols
        geo.attrs["geo_number_rows"] = rows
        geo.attrs["geo_product_corners"] = np.array(
            [WEST, SOUTH, WEST, NORTH, EAST, NORTH, EAST, SOUTH], dtype=float)
        mp = geo.create_group("map_projection")
        mp.attrs["projection_proj4_params"] = np.bytes_(radar.KNMI_PROJ4)
        ov = f.create_group("overview")
        ov.attrs["product_datetime_start"] = np.bytes_("27-JUN-2026;17:20:00.000")
    return buf.getvalue()


def test_pipeline_end_to_end():
    """Hele keten: HDF5 lezen -> PNG renderen -> cellen detecteren."""
    raw = _synthetic_knmi_hdf5()

    dbz, proj4, corners, cols, rows, ts = radar.read_knmi_hdf5(raw)
    assert np.nanmax(dbz) > 50, f"calibratie klopt niet, hoogste dBZ is {np.nanmax(dbz)}"
    assert np.isnan(dbz).any(), "255 hoort als 'geen data' te worden gelezen"
    assert ts == "27-JUN-2026;17:20:00.000"

    png, bounds = radar.reproject_to_png(dbz, proj4, corners, out_w=600, out_h=600)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "geen geldige PNG"
    south, west, north, east = bounds
    assert south < north and west < east

    import io
    from PIL import Image
    img = np.array(Image.open(io.BytesIO(png)))
    assert img.shape == (600, 600, 4)
    painted = (img[:, :, 3] > 0).sum()
    assert painted > 0, "niets ingekleurd"
    assert painted < 600 * 600 * 0.5, "veel te veel ingekleurd; nodata lekt door"

    cells = radar.detect_cells(dbz, corners, threshold=45)
    assert len(cells) == 1, f"verwachtte één cel boven 45 dBZ, kreeg er {len(cells)}"
    c = cells[0]
    assert SOUTH < c["lat"] < NORTH and WEST < c["lon"] < EAST
    assert 54 < c["max_dbz"] < 56, c["max_dbz"]

    print(f"keten OK: {painted} pixels ingekleurd, "
          f"cel op {c['lat']:.2f}N {c['lon']:.2f}E met {c['max_dbz']} dBZ")


if __name__ == "__main__":
    test_row_alignment()
    test_known_point_lands_right()
    test_pipeline_end_to_end()
    print("\nalles goed")
