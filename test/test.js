// Drive Doppler in a real Chromium, measure playback smoothness + lightning.
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const { install } = require('./mocks');

const APP = 'file://' + path.join(__dirname, '..', 'index.html');
const OUT = __dirname;

// Injected before app code: expose swap + frame events on window.__ev
const PROBE = `
window.__ev = [];
window.__t0 = Date.now();
addEventListener('DOMContentLoaded', () => {
  // 1. wrap showFrame (classic-script function declaration => on window)
  const iv = setInterval(() => {
    if (typeof window.showFrame !== 'function') return;
    clearInterval(iv);
    const orig = window.showFrame;
    window.showFrame = function (i) {
      window.__ev.push({ k: 'showFrame', i, t: Date.now() });
      return orig.apply(this, arguments);
    };
  }, 20);
  // 2. watch the radar pane for the buffer swap (opacity flip)
  const paneWatch = setInterval(() => {
    const pane = document.querySelector('.leaflet-radar-pane');
    if (!pane) return;
    clearInterval(paneWatch);
    new MutationObserver(muts => {
      for (const m of muts) {
        const el = m.target;
        if (!el.classList) continue;
        if (!el.classList.contains('leaflet-layer') &&
            !el.classList.contains('leaflet-image-layer')) continue;
        const op = el.style.opacity;
        window.__ev.push({ k: 'opacity', op: op === '' ? null : +op, t: Date.now(),
                           id: el.__dopId || (el.__dopId = Math.random().toString(36).slice(2, 6)) });
      }
    }).observe(pane, { attributes: true, attributeFilter: ['style'], subtree: true });
  }, 20);
});
`;

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
  });
  const page = await ctx.newPage();
  const netlog = [];
  const consoleLog = [];
  const pageErrors = [];
  page.on('console', m => consoleLog.push(`[${m.type()}] ${m.text()}`));
  page.on('pageerror', e => pageErrors.push(String(e)));

  // one full-viewport GetMap from KNMI is realistically ~400-900ms
  await install(page, {
    log: netlog,
    tileLatency: +(process.env.LAT || 550),
    noCors: process.env.NOCORS === '1',
  });
  await page.addInitScript(PROBE);

  await page.goto(APP, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3500);
  await page.screenshot({ path: path.join(OUT, '01-start.png') });

  if (pageErrors.length) {
    console.log('PAGE ERRORS:\n' + pageErrors.join('\n'));
  }
  const loaderGone = await page.evaluate(() =>
    document.getElementById('loader').classList.contains('gone'));
  console.log('loader dismissed:', loaderGone);
  if (!loaderGone) {
    console.log('console:', consoleLog.slice(-15).join('\n'));
    await browser.close();
    process.exit(1);
  }

  // ---- lightning check -----------------------------------------------------
  const bolt = await page.evaluate(() => {
    const badge = document.querySelector('[data-l=bolt] .badge');
    return { badge: badge ? badge.textContent : null };
  });

  // ---- playback ------------------------------------------------------------
  await page.evaluate(() => { window.__ev.length = 0; window.__playStart = Date.now(); });
  await page.click('#playBtn');
  await page.waitForTimeout(14000);
  await page.screenshot({ path: path.join(OUT, '02-playing.png') });
  await page.click('#playBtn'); // pause

  const ev = await page.evaluate(() => window.__ev);

  // ---- open the threat sheet + a layer explainer ---------------------------
  await page.click('#codebanner');
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(OUT, '03-sheet.png') });
  await page.click('#codebanner');

  await page.click('[data-l=cells]');
  await page.click('[data-l=cape]');
  await page.waitForTimeout(1800);
  await page.screenshot({ path: path.join(OUT, '04-layers.png') });

  await page.click('#driveBtn');
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(OUT, '05-drive.png') });
  await page.click('#driveBtn');

  // ---- pan: buffer must invalidate and refetch exactly once per frame ------
  const beforePan = netlog.filter(n => n.kind === 'wms').length;
  await page.mouse.move(200, 400);
  await page.mouse.down();
  await page.mouse.move(330, 560, { steps: 14 });
  await page.mouse.up();
  await page.waitForTimeout(6000);
  const afterPan = netlog.filter(n => n.kind === 'wms').length;

  // ---- scrub through the whole timeline ------------------------------------
  const beforeScrub = netlog.filter(n => n.kind === 'wms').length;
  await page.evaluate(async () => {
    const s = document.getElementById('slider');
    for (let i = 0; i <= +s.max; i++) {
      s.value = i; s.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise(r => setTimeout(r, 90));
    }
  });
  await page.waitForTimeout(1200);
  const afterScrub = netlog.filter(n => n.kind === 'wms').length;
  const blobs = await page.evaluate(() =>
    ({ overlaySrcs: [...document.querySelectorAll('.leaflet-radar-pane img')].map(i => i.src.slice(0, 5)) }));

  const state = await page.evaluate(() => ({
    clk: document.getElementById('clk').textContent,
    status: document.getElementById('sttx').textContent,
    code: document.getElementById('cbBadge').textContent,
    sub: document.getElementById('cbSub').textContent,
    cape: document.getElementById('vCape').textContent,
    shear: document.getElementById('vShear').textContent,
    radarLayersInPane: document.querySelectorAll('.leaflet-radar-pane .leaflet-layer').length,
    imgTilesInDom: document.querySelectorAll('.leaflet-radar-pane img').length,
    totalImgs: document.querySelectorAll('img').length,
  }));

  // ---- analysis ------------------------------------------------------------
  const shows = ev.filter(e => e.k === 'showFrame');
  const swaps = ev.filter(e => e.k === 'opacity' && e.op > 0);
  const gaps = [];
  for (let i = 1; i < shows.length; i++) gaps.push(shows[i].t - shows[i - 1].t);

  // how long after each showFrame did a buffer actually become visible?
  const lat = [];
  for (const s of shows) {
    const sw = swaps.find(x => x.t >= s.t);
    if (sw) lat.push(sw.t - s.t);
  }
  const stats = a => a.length ? {
    n: a.length,
    min: Math.min(...a),
    med: a.slice().sort((x, y) => x - y)[Math.floor(a.length / 2)],
    max: Math.max(...a),
    avg: Math.round(a.reduce((p, c) => p + c, 0) / a.length),
  } : null;

  const wms = netlog.filter(n => n.kind === 'wms');
  const report = {
    pageErrors,
    consoleTail: consoleLog.slice(-12),
    unmocked: [...new Set(netlog.filter(n => n.kind === 'unmocked').map(n => n.url))].slice(0, 10),
    lightning: bolt,
    wsOpened: netlog.filter(n => n.kind === 'ws-open').length,
    state,
    playback: {
      framesShown: shows.length,
      frameGapMs: stats(gaps),
      swapLatencyMs: stats(lat),
      fallbackSwaps: lat.filter(v => v >= 880).length,
      wmsTileRequests: wms.length,
      uniqueTimes: [...new Set(wms.map(w => w.t))].length,
      tilesPerFrame: shows.length ? +(wms.length / shows.length).toFixed(1) : 0,
    },
    afterPlayback: {
      panRefetch: afterPan - beforePan,        // verwacht: 1 per frame
      scrubRefetch: afterScrub - beforeScrub,  // verwacht: 0
      overlaySrcScheme: blobs.overlaySrcs,     // verwacht: blob:
    },
  };
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));

  await browser.close();
  process.exit(0);
})();
