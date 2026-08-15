"""
server.py — Doppler backend (FastAPI)
Haalt KNMI radar + nowcast HDF5, verwerkt naar PNG-overlays met dBZ-kleuren,
detecteert cellen, en serveert alles aan de Doppler-app.

Endpoints:
  GET /api/frames          -> lijst frames (verleden + nowcast) met tijden + bounds
  GET /api/radar/{id}.png  -> de gerenderde PNG-overlay voor een frame
  GET /api/cells           -> gedetecteerde stormcellen (nieuwste frame)
  GET /healthz             -> status

Env:
  KNMI_API_KEY   (verplicht) — Open Data API key van dataplatform.knmi.nl
"""
import os, io, time, threading, datetime as dt
from typing import Dict, List, Optional
import urllib.request, json
from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import radar

KNMI_KEY = os.environ.get("KNMI_API_KEY", "") or "eyJvcmciOiI1ZTU1NGUxOTI3NGE5NjAwMDEyYTNlYjEiLCJpZCI6IjUzYTg1ZDBhMmQ5YzRkYzJiYWNlNzQ4NTQ2Zjk4ODExIiwiaCI6Im11cm11cjEyOCJ9"  # anonieme KNMI-key, geldig t/m 1 aug 2027
BASE = "https://api.dataplatform.knmi.nl/open-data/v1/datasets"
DS_RADAR = ("radar_reflectivity_composites", "2.0")
DS_NOWCAST = ("radar_forecast", "2.0")
REFRESH_SEC = 150         # elke 2.5 min verversen
OUT_SIZE = 1000           # PNG resolutie

app = FastAPI(title="Doppler backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# in-memory cache
_lock = threading.Lock()
_frames: List[dict] = []          # [{id,time,type,bounds}]
_png: Dict[str, bytes] = {}       # id -> png bytes
_cells: List[dict] = []
_last_refresh = 0
_last_error = ""


def _knmi_get(url: str, timeout=25) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": KNMI_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _list_files(ds, version, maxkeys=8) -> List[str]:
    url = f"{BASE}/{ds}/versions/{version}/files?maxKeys={maxkeys}&orderBy=created&sorting=desc"
    data = json.loads(_knmi_get(url))
    return [f["filename"] for f in data.get("files", [])]


def _download_file(ds, version, filename) -> bytes:
    url = f"{BASE}/{ds}/versions/{version}/files/{filename}/url"
    meta = json.loads(_knmi_get(url))
    dl = meta["temporaryDownloadUrl"]
    # tijdelijke URL is presigned (geen auth header nodig)
    with urllib.request.urlopen(dl, timeout=40) as r:
        return r.read()


def _parse_dt(s: str) -> int:
    # "27-JUN-2026;17:20:00.000" -> epoch
    try:
        d = dt.datetime.strptime(s.split(".")[0], "%d-%b-%Y;%H:%M:%S")
        return int(d.replace(tzinfo=dt.timezone.utc).timestamp())
    except Exception:
        return int(time.time())


def refresh():
    """Haal nieuwste radar (verleden) + nowcast, verwerk, vul cache.
    Bij fouten blijft de vorige cache staan (app blijft werken)."""
    global _frames, _png, _cells, _last_refresh, _last_error
    if not KNMI_KEY:
        _last_error = "geen API key"
        return
    new_frames, new_png, new_cells = [], {}, []
    try:
        # --- verleden: laatste 6 reflectivity-frames ---
        past = _list_files(*DS_RADAR, maxkeys=6)
        for fn in reversed(past):   # oud -> nieuw
            try:
                raw = _download_file(*DS_RADAR, fn)
                dbz, proj4, corners, c, r, ts = radar.read_knmi_hdf5(raw)
                png, bounds = radar.reproject_to_png(dbz, proj4, corners, OUT_SIZE, OUT_SIZE)
                fid = "p_" + fn.replace(".h5", "")
                new_png[fid] = png
                new_frames.append({"id": fid, "time": _parse_dt(ts), "type": "past", "bounds": bounds})
                if fn == past[0]:
                    new_cells = radar.detect_cells(dbz, corners, threshold=45)
            except Exception as e:
                print("frame skip", fn, type(e).__name__, e)
                continue
        if not new_frames:
            raise RuntimeError("geen verledenframes verwerkt")
        now_t = new_frames[-1]["time"]

        # --- nowcast: 1 bestand met meerdere image-groepen (+5..+120) ---
        try:
            nc = _list_files(*DS_NOWCAST, maxkeys=1)
            if nc:
                raw = _download_file(*DS_NOWCAST, nc[0])
                import h5py
                with h5py.File(io.BytesIO(raw), "r") as f:
                    groups = sorted([k for k in f.keys() if k.startswith("image")],
                                    key=lambda s: int("".join(ch for ch in s if ch.isdigit()) or 0))
                # elke 2e groep = 10-min stappen, max 6 vooruit (houdt het licht)
                for i, gname in enumerate(groups[1::2][:6]):
                    dbz, proj4, corners, c, r, ts = radar.read_knmi_hdf5(raw, image_group=gname)
                    png, bounds = radar.reproject_to_png(dbz, proj4, corners, OUT_SIZE, OUT_SIZE)
                    fid = f"n_{nc[0].replace('.h5','')}_{i}"
                    new_png[fid] = png
                    new_frames.append({"id": fid, "time": now_t + (i + 1) * 600,
                                       "type": "nowcast", "bounds": bounds})
        except Exception as e:
            print("nowcast overgeslagen:", type(e).__name__, e)

        with _lock:
            _frames = new_frames
            _png = new_png
            _cells = new_cells
            _last_refresh = int(time.time())
            _last_error = ""
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        print("refresh error:", _last_error)


def _loop():
    while True:
        refresh()
        time.sleep(REFRESH_SEC)


@app.on_event("startup")
def _start():
    threading.Thread(target=_loop, daemon=True).start()


@app.get("/healthz")
def healthz():
    age = int(time.time()) - _last_refresh if _last_refresh else None
    return {"ok": len(_frames) > 0, "frames": len(_frames), "cells": len(_cells),
            "last_refresh": _last_refresh, "seconds_ago": age,
            "has_key": bool(KNMI_KEY), "error": _last_error}


@app.get("/api/frames")
def frames():
    with _lock:
        if not _frames:
            raise HTTPException(503, "nog geen data — probeer over even opnieuw")
        return {"frames": _frames, "updated": _last_refresh}


@app.get("/api/radar/{fid}.png")
def radar_png(fid: str):
    with _lock:
        png = _png.get(fid)
    if png is None:
        raise HTTPException(404, "frame niet gevonden")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/cells")
def cells():
    with _lock:
        return {"cells": _cells, "updated": _last_refresh}
