# Doppler — backend (KNMI radar)

Python/FastAPI service die KNMI radar + nowcast HDF5 ophaalt, verwerkt naar
PNG-overlays met dBZ-kleuren, cellen detecteert, en serveert aan de Doppler-app.

## Wat zit erin
- `server.py` — FastAPI server + endpoints
- `radar.py` — KNMI HDF5 lezen, reprojecteren, kleuren, celdetectie
- `requirements.txt` — Python dependencies
- `Procfile` / `railway.json` — Railway deploy-config
- `runtime.txt` — Python-versie

## Endpoints
| Endpoint | Doel |
|---|---|
| `GET /healthz` | status (frames, cellen, of key aanwezig) |
| `GET /api/frames` | lijst frames (verleden + nowcast) met tijd + bounds |
| `GET /api/radar/{id}.png` | gerenderde radar-overlay |
| `GET /api/cells` | gedetecteerde stormcellen |

---

## STAP 1 — KNMI API-key
**Al geregeld!** De backend gebruikt standaard de officiële anonieme KNMI-key
(geldig t/m 1 augustus 2027, publiek gepubliceerd door KNMI zelf). Je hoeft
niks aan te vragen of te mailen.

Optioneel later: wil je je eigen rate limit + quota (niet gedeeld met andere
anonieme gebruikers), vraag dan een geregistreerde key aan via het KNMI
Developer Portal en zet die als `KNMI_API_KEY` env-variabele op Railway.
De backend gebruikt dan automatisch die i.p.v. de anonieme.

## STAP 2 — Backend op Railway
1. Zet deze map in een GitHub-repo (bijv. `doppler-backend`).
2. Railway → New Project → Deploy from GitHub repo → kies de repo.
3. Railway → Variables → voeg toe:
   `KNMI_API_KEY = <jouw key>`
4. Railway bouwt automatisch (NIXPACKS leest requirements.txt).
5. Settings → Networking → Generate Domain → je krijgt een URL:
   `https://<iets>.up.railway.app`
6. Test: open `https://<iets>.up.railway.app/healthz`
   → `{"ok":true,"has_key":true,...}` en na ~1 min `"frames">0`.

## STAP 3 — Frontend (GitHub Pages)
1. Repo `doppler`, bestand `index.html` (de Doppler-app).
2. In de app-code staat bovenaan:
   `const BACKEND = "";`
   Vul daar je Railway-URL in:
   `const BACKEND = "https://<iets>.up.railway.app";`
3. Settings → Pages → Deploy from branch → main → /root.
4. App komt op `https://<jouwnaam>.github.io/doppler/`.

Zolang `BACKEND` leeg is, gebruikt de app automatisch RainViewer (werkt
meteen, lagere resolutie). Zodra je de Railway-URL invult, schakelt 'ie
over op KNMI hoge-resolutie radar + echte nowcast + celdetectie.

## Lokaal testen (optioneel)
```
pip install -r requirements.txt
export KNMI_API_KEY=<jouw key>
uvicorn server:app --reload
# open http://localhost:8000/healthz
```
