# Browsertest

Draait `index.html` in een echte Chromium op iPhone-formaat (390×844, touch,
Safari-UA) en meet of het afspelen klopt. Alle externe bronnen worden lokaal
nagebootst, dus de test werkt zonder internet en zonder KNMI-quota te verbruiken.

```
cd test
npm install
npx playwright install chromium   # niet nodig als Chromium er al staat
npm test
```

Uitvoer: `report.json` plus screenshots `01-start.png` t/m `05-drive.png`.

## Wat er nagebootst wordt

| Bron | Nabootsing |
|---|---|
| KNMI WMS (radar + nowcast) | PNG op de gevraagde afmeting, ~550 ms vertraging |
| Open-Meteo | CAPE / shear / lifted index |
| Blitzortung websocket | een inslag per 400 ms |
| NASA GIBS, CARTO, cdnjs | lokale stand-ins (Leaflet uit `node_modules`) |

Elk verzoek naar een niet-nagebootste host wordt geblokkeerd en verschijnt in
`report.json` onder `unmocked` — zo valt meteen op als er een bron bijkomt.

## Wat er gemeten wordt

- `playback.framesShown` tegenover het aantal keer dat het beeld écht wisselde
- `playback.swapLatencyMs` — hoe lang na een frame het beeld op het scherm staat
- `playback.wmsTileRequests` en `uniqueTimes` — of frames onnodig opnieuw worden gehaald
- `afterPlayback.scrubRefetch` — scrubben hoort 0 requests te kosten
- `afterPlayback.panRefetch` — pannen hoort er precies één per frame te kosten
- `state.imgTilesInDom` — hoort 2 te zijn; hoger betekent dat de radarlagen
  weer groeien, wat eerder de Safari-crash veroorzaakte

## Varianten

```
npm run test:nocors    # KNMI zonder CORS-headers: moet terugvallen op <img>
npm run test:traag     # 1200 ms per beeld: moet bufferen i.p.v. desynchroniseren
npm run test:badlayer  # KNMI stuurt XML-fout met status 200: moet dat melden
npm run test:lowcape   # nauwelijks CAPE: moet zeggen dat er niets te tonen is
npm run test:backend   # eigen radarservice: één beeld per frame, geen KNMI WMS
npm run test:backenddown  # backend plat: moet terugvallen op de KNMI WMS
```

`test:backend` opent de app met `?backend=…` tegen een nagebootste Railway-
service, en controleert ook dat die keuze een herlaadbeurt zonder parameter
overleeft.

De GPS wordt altijd nagebootst met een positie die blijft doortikken. Dat
kan niet via `setGeolocation`: die vuurt `watchPosition` in Chromium niet
opnieuw af, waardoor juist de herhaalde update — waar de app op stukliep —
nooit getest werd.

## Beperking

De nagebootste radarbeelden zijn synthetisch. De test bewijst dat de app-logica
klopt, niet dat de KNMI-laagnamen en -stijlen nog geldig zijn — dat blijft iets
om tegen de echte server te controleren.
