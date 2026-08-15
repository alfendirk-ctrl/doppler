// Mock every external data source Doppler uses, so the app can run in a
// network-isolated browser. Tile latency is simulated to match real KNMI WMS.
const fs = require('fs');
const path = require('path');
const { PNG } = require('pngjs');

const LEAFLET = path.join(__dirname, 'node_modules', 'leaflet', 'dist');

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// --- synthetic radar tile: a moving blob so frames differ visually -----------
const tileCache = new Map();
function radarTile(w, h, phase) {
  const key = `${w}_${h}_${phase}`;
  if (tileCache.has(key)) return tileCache.get(key);
  const png = new PNG({ width: w, height: h });
  const cx = w / 2 + Math.cos(phase / 3) * w * 0.35;
  const cy = h / 2 + Math.sin(phase / 3) * h * 0.25;
  const rad = Math.min(w, h) * 0.42;
  for (let py = 0; py < h; py++) {
    for (let px = 0; px < w; px++) {
      const i = (w * py + px) << 2;
      const d = Math.hypot(px - cx, py - cy);
      const v = Math.max(0, 1 - d / rad);
      // crude dBZ-ish ramp blue -> green -> yellow -> red
      png.data[i] = v > .7 ? 226 : v > .5 ? 245 : v > .3 ? 63 : 90;
      png.data[i + 1] = v > .7 ? 59 : v > .5 ? 208 : v > .3 ? 201 : 173;
      png.data[i + 2] = v > .7 ? 59 : v > .5 ? 24 : v > .3 ? 122 : 240;
      png.data[i + 3] = Math.round(v * 235);
    }
  }
  const buf = PNG.sync.write(png);
  tileCache.set(key, buf);
  return buf;
}

// pale basemap stand-in so the screenshots read like a real map background
const LAND = (() => {
  const png = new PNG({ width: 256, height: 256 });
  for (let py = 0; py < 256; py++) for (let px = 0; px < 256; px++) {
    const i = (256 * py + px) << 2;
    const grid = (px % 64 < 1 || py % 64 < 1) ? 8 : 0;
    png.data[i] = 232 - grid; png.data[i + 1] = 236 - grid; png.data[i + 2] = 240 - grid; png.data[i + 3] = 255;
  }
  return PNG.sync.write(png);
})();

const BLANK = (() => {
  const png = new PNG({ width: 256, height: 256 });
  png.data.fill(0);
  return PNG.sync.write(png);
})();

function meteoBody(url) {
  const u = new URL(url);
  const lats = (u.searchParams.get('latitude') || '52').split(',');
  const n = lats.length;
  const hourly = {
    time: Array.from({ length: 24 }, (_, i) => i),
    cape: Array.from({ length: 24 }, () => 1400),
    lifted_index: Array.from({ length: 24 }, () => -4.2),
    wind_speed_850hPa: Array.from({ length: 24 }, () => 34),
    wind_speed_500hPa: Array.from({ length: 24 }, () => 71),
    wind_direction_850hPa: Array.from({ length: 24 }, () => 210),
    wind_direction_500hPa: Array.from({ length: 24 }, () => 250),
  };
  const one = (i) => ({
    latitude: +lats[i], longitude: 5,
    current: { cape: 900 + i * 130, precipitation: i % 3 ? 0 : 2.4 },
    hourly,
  });
  const out = n > 1 ? Array.from({ length: n }, (_, i) => one(i)) : one(0);
  return JSON.stringify(out);
}

/**
 * @param {import('playwright').Page} page
 * @param {{tileLatency?:number, jitter?:number, log?:Array}} opts
 */
async function install(page, opts = {}) {
  const latency = opts.tileLatency ?? 250;   // per-tile, like real KNMI WMS
  const jitter = opts.jitter ?? 120;
  const log = opts.log || [];

  // NOTE: Playwright matches routes most-recently-added first, so the
  // catch-all must be registered BEFORE the specific handlers.
  await page.route('**://**', route => {
    const u = route.request().url();
    if (u.startsWith('http://localhost') || u.startsWith('file:')) return route.continue();
    log.push({ kind: 'unmocked', url: u, ts: Date.now() });
    return route.abort();
  });

  // Leaflet from local node_modules (cdnjs is unreachable)
  await page.route('**/cdnjs.cloudflare.com/**', async route => {
    const u = route.request().url();
    if (u.endsWith('.css')) {
      return route.fulfill({ contentType: 'text/css', body: fs.readFileSync(path.join(LEAFLET, 'leaflet.css'), 'utf8') });
    }
    return route.fulfill({ contentType: 'application/javascript', body: fs.readFileSync(path.join(LEAFLET, 'leaflet.js'), 'utf8') });
  });

  // basemap + labels: pale land tile, instant
  await page.route('**basemaps.cartocdn.com/**', route => {
    // het zoomniveau uit het tegelpad loggen, zodat een test kan zien
    // of de kaart uit zichzelf terugzoomt
    const m = route.request().url().match(/\/(\d+)\/\d+\/\d+/);
    if (m) log.push({ kind: 'basemap', z: +m[1], ts: Date.now() });
    return route.fulfill({ contentType: 'image/png', body: route.request().url().includes('labels') ? BLANK : LAND });
  });

  // NASA GIBS satellite
  await page.route('**/gibs.earthdata.nasa.gov/**', route =>
    route.fulfill({ contentType: 'image/jpeg', body: BLANK }));

  // KNMI WMS (radar + nowcast) — the thing playback depends on
  await page.route('**/adaguc-server**', async route => {
    const url = route.request().url();
    const u = new URL(url);
    const q = k => u.searchParams.get(k) || u.searchParams.get(k.toUpperCase())
      || u.searchParams.get(k.toLowerCase());
    // NOCORS=1 simulates a KNMI server without CORS headers: fetch() fails,
    // the app must fall back to plain <img> preloading.
    if (opts.noCors && ['fetch', 'xhr'].includes(route.request().resourceType())) {
      log.push({ kind: 'wms-corsblocked', ts: Date.now() });
      return route.abort('failed');
    }
    // BADLAYER=1 simulates ADAGUC answering a bad LAYERS/STYLES with an XML
    // ServiceException at HTTP 200 — the failure mode that looks like "dry".
    if (opts.badLayer) {
      log.push({ kind: 'wms-exception', ts: Date.now() });
      await sleep(latency);
      return route.fulfill({
        status: 200,
        contentType: 'text/xml',
        body: '<?xml version="1.0"?><ServiceExceptionReport><ServiceException code="LayerNotDefined">Layer not defined</ServiceException></ServiceExceptionReport>',
      });
    }
    const t = q('TIME') || 'now';
    const w = Math.min(1200, +q('WIDTH') || 256);
    const h = Math.min(1200, +q('HEIGHT') || 256);
    log.push({ kind: 'wms', t, w, h, ts: Date.now(), url });
    await sleep(latency + Math.random() * jitter);
    return route.fulfill({
      contentType: 'image/png',
      body: radarTile(w, h, Math.abs(hashCode(t)) % 20),
    });
  });

  // Open-Meteo (CAPE / shear / hail / probe)
  await page.route('**/api.open-meteo.com/**', async route => {
    log.push({ kind: 'meteo', ts: Date.now() });
    await sleep(80);
    return route.fulfill({ contentType: 'application/json', body: meteoBody(route.request().url()) });
  });

  // Blitzortung websocket
  await page.routeWebSocket(/blitzortung\.org/, ws => {
    log.push({ kind: 'ws-open', ts: Date.now() });
    let n = 0;
    const iv = setInterval(() => {
      // App tries LZW-decode first, then falls back to raw JSON.parse
      const lat = 51.6 + Math.random() * 1.2;
      const lon = 4.8 + Math.random() * 1.4;
      try { ws.send(JSON.stringify({ time: Date.now() * 1e6, lat, lon, sig: [] })); } catch (_) { clearInterval(iv); }
      n++;
    }, 400);
    ws.onMessage(() => {});
    ws.onClose(() => clearInterval(iv));
  });

  return log;
}

function hashCode(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h |= 0; }
  return h;
}

module.exports = { install };
