/* Step-capture harness for the ARS click-by-click manual.
   Per step: navigate → act (fill/click/scroll — never destructive) →
   inject red outline + numbered badge on the target → screenshot (cropped). */
const puppeteer = require('puppeteer-core')
const fs = require('fs')
const path = require('path')

const BASE = 'http://localhost:3000'
const API = 'http://127.0.0.1:8000'
const OUT = 'D:/ARS_PROD/ars_prod/frontend/public/docs/guide'
const VW = { width: 1600, height: 1000 }

/* highlight/action target: string = CSS selector, or {sel, text} = first
   matching selector whose textContent includes text. */
const M = []
const S = (module, step, name, route, opts = {}) => M.push({ module, step, name, route, ...opts })

/* ── Getting started ─────────────────────────────────────────────── */
S('start', 1, 'login',            '/login', { noAuth: true, highlight: 'form, input[type="password"]', clipPad: 320 })
S('start', 2, 'dashboard',        '/ars-dashboard', { wait: 6000 })
S('start', 3, 'sidebar',          '/ars-dashboard', { wait: 3000, highlight: 'nav[class*="flex-1"]', clipPad: 200,
  pre: [{ clickSel: '[data-nav-row="header"][data-section="Listing & Alloc"]' }, { wait: 400 }] })
S('start', 4, 'pipeline-home',    '/data-prep/listing', { wait: 7000, highlight: { sel: 'h1', text: 'Listing Generation' }, clipPad: 500 })

/* ── 1 · MSA Stock ───────────────────────────────────────────────── */
S('msa', 1, 'open',     '/msa', { wait: 5000 })
S('msa', 2, 'sources',  '/msa', { wait: 5000, highlight: { sel: 'div,section', text: 'MSA' }, clip: 'viewport' })
S('msa', 3, 'run',      '/msa', { wait: 5000, highlight: { sel: 'button', text: 'Calculate MSA' }, clipPad: 420 })
S('msa', 4, 'outputs',  '/msa', { wait: 5000, clip: 'viewport', scrollY: 500 })

/* ── 2 · Grid Builder ────────────────────────────────────────────── */
S('grid', 1, 'open',      '/data-prep/store-stock', { wait: 6000 })
S('grid', 2, 'majcat',    '/data-prep/store-stock', { wait: 6000, highlight: { sel: 'input,select', text: '' }, clipPad: 420 })
S('grid', 3, 'tunables',  '/data-prep/store-stock', { wait: 6000, scrollY: 350, clip: 'viewport' })
S('grid', 4, 'build',     '/data-prep/store-stock', { wait: 6000, highlight: { sel: 'button', text: 'Build' }, clipPad: 420 })
S('grid', 5, 'results',   '/data-prep/store-stock', { wait: 6000, scrollY: 700, clip: 'viewport' })
S('grid', 6, 'gap-banner','/data-prep/listing', { wait: 8000, inject: 'gapBanner',
  highlight: '#__cap_banner', clipPad: 160 })

/* ── 3 · Merge Rules ─────────────────────────────────────────────── */
S('merge', 1, 'open',    '/data-prep/merge-rules', { wait: 5000 })
S('merge', 2, 'rows',    '/data-prep/merge-rules', { wait: 5000, highlight: 'table, [class*="table"]', clipPad: 120 })
S('merge', 3, 'mapping', '/data-prep/merge-rules', { wait: 5000, highlight: { sel: 'button', text: 'New rule' }, clipPad: 420 })
S('merge', 4, 'save',    '/data-prep/merge-rules', { wait: 5000, highlight: { sel: 'button', text: 'Refresh derived' }, clipPad: 420 })

/* ── 4 · Listing & Allocation (the cockpit) ──────────────────────── */
const L = '/data-prep/listing'
S('listing', 1,  'open',        L, { wait: 8000 })
S('listing', 2,  'opt-types',   L, { wait: 7000, highlight: 'input[name="alloc_ot_filter"]', hlContainer: true, clipPad: 320 })
S('listing', 3,  'alloc-mode',  L, { wait: 7000, highlight: 'input[name="allocation_mode"]', hlContainer: true, clipPad: 320 })
S('listing', 4,  'order-workers', L, { wait: 7000, highlight: 'input[name="exec_order"]', hlContainer: true, clipPad: 320 })
S('listing', 5,  'store-search', L, { wait: 7000,
  pre: [{ fill: ['input[placeholder^="Search store"]', 'patna'] }, { wait: 600 }],
  highlight: 'input[placeholder^="Search store"]', clipPad: 380 })
S('listing', 6,  'hub-bulk',    L, { wait: 7000,
  pre: [{ fill: ['input[placeholder^="Search store"]', 'db03'] }, { wait: 600 }],
  highlight: 'div[title*="stores of HUB DB03"]', clipPad: 360 })
S('listing', 7,  'majcat-search', L, { wait: 7000,
  pre: [{ fill: ['input[placeholder^="Search MAJ_CAT"]', 'ladies'] }, { wait: 600 }],
  highlight: 'div[title*="of DIV LADIES"]', clipPad: 360 })
S('listing', 8,  'stock-excess', L, { wait: 7000, pre: [{ scrollTo: { sel: 'div', text: 'STOCK & EXCESS' } }, { wait: 400 }],
  highlight: { sel: 'div', text: 'STOCK & EXCESS' }, clipPad: 260 })
S('listing', 9,  'store-ranking', L, { wait: 7000, pre: [{ scrollTo: { sel: 'div', text: 'STORE RANKING' } }, { wait: 400 }],
  highlight: { sel: 'div', text: 'STORE RANKING' }, clipPad: 260 })
S('listing', 10, 'run-pool-hold', L, { wait: 7000, pre: [{ scrollTo: { sel: 'div', text: 'RUN POOL' } }, { wait: 400 }],
  highlight: { sel: 'div', text: 'RUN POOL' }, clipPad: 280 })
S('listing', 11, 'caps-growth', L, { wait: 7000, pre: [{ scrollTo: { sel: 'div', text: 'CAPS & GROWTH' } }, { wait: 400 }],
  highlight: { sel: 'div', text: 'CAPS & GROWTH' }, clipPad: 280 })
S('listing', 12, 'sizing-season', L, { wait: 7000, pre: [{ scrollTo: { sel: 'div', text: 'SIZING' } }, { wait: 400 }],
  highlight: { sel: 'div', text: 'SIZING' }, clipPad: 280 })
S('listing', 13, 'run-scope',   L, { wait: 7000, pre: [{ scrollTo: { sel: 'div', text: 'RUN MODE' } }, { wait: 400 }],
  highlight: { sel: 'div', text: 'RUN MODE' }, clipPad: 300 })
S('listing', 14, 'key-numbers', L, { wait: 7000,
  highlight: { sel: 'button', text: 'Key Numbers' }, hlContainer: true, clipPad: 340 })
S('listing', 15, 'generate',    L, { wait: 7000, highlight: { sel: 'button', text: 'Generate' }, clipPad: 420 })
S('listing', 16, 'preview',     L, { wait: 7000, pre: [{ scrollTo: { sel: 'button', text: 'Fetch' } }, { wait: 400 }],
  highlight: { sel: 'button', text: 'Fetch' }, clipPad: 380 })

/* ── 5 · Review ──────────────────────────────────────────────────── */
S('review', 1, 'parked-bar',   L, { wait: 8000, inject: 'parkedBar',
  highlight: '#__cap_parked', clipPad: 200 })
S('review', 2, 'view-logs',    L, { wait: 8000, highlight: { sel: 'button', text: 'View Logs' }, clipPad: 420 })
S('review', 3, 'session-logs', '/data-prep/listing/logs', { wait: 6000 })
S('review', 4, 'alloc-review', '/alc-review', { wait: 7000 })
S('review', 5, 'filters',      '/alc-review', { wait: 7000, highlight: 'input[type="date"], select, input', clipPad: 380 })
S('review', 6, 'drill',        '/alc-review', { wait: 7000, scrollY: 600, clip: 'viewport' })

/* ── 6 · Hold ────────────────────────────────────────────────────── */
S('hold', 1, 'open',   '/reports/hold', { wait: 7000 })
S('hold', 2, 'kpi',    '/reports/hold', { wait: 7000, highlight: { sel: 'div', text: 'Hold' }, clipPad: 300 })
S('hold', 3, 'by-rdc', '/reports/hold', { wait: 7000, scrollY: 500, clip: 'viewport' })

/* ── 7 · Pending Allocation ──────────────────────────────────────── */
S('pendalc', 1, 'overview', '/pend-alc/overview',     { wait: 7000 })
S('pendalc', 2, 'ageing',   '/pend-alc/overview',     { wait: 7000, scrollY: 450, clip: 'viewport' })
S('pendalc', 3, 'do-entry', '/pend-alc/do-entry',     { wait: 7000 })
S('pendalc', 4, 'reco',     '/pend-alc/reco',         { wait: 7000 })
S('pendalc', 5, 'adhoc',    '/pend-alc/adhoc-close',  { wait: 7000 })
S('pendalc', 6, 'ops-log',  '/pend-alc/operations',   { wait: 7000 })

/* ── Data Dictionary ─────────────────────────────────────────────── */
S('dictionary', 1, 'open',   '/data-dictionary', { wait: 5000 })
S('dictionary', 2, 'search', '/data-dictionary', { wait: 5000,
  pre: [{ fill: ['input[placeholder*="Search column"]', 'FNL_Q'] }, { wait: 500 }],
  highlight: 'input[placeholder*="Search column"]', clipPad: 380 })

/* ── runner ──────────────────────────────────────────────────────── */
const findInPage = ({ sel, text }) => {
  const els = [...document.querySelectorAll(sel)]
  if (!text) return els.find(e => e.offsetWidth > 0) || null
  // smallest visible element containing the text (most specific)
  const hits = els.filter(e => e.offsetWidth > 0 && (e.textContent || '').includes(text))
  hits.sort((a, b) => a.textContent.length - b.textContent.length)
  return hits[0] || null
}

async function resolveTarget(page, target, useContainer) {
  return page.evaluate(({ target, useContainer, findSrc }) => {
    const find = eval('(' + findSrc + ')')
    let el = typeof target === 'string'
      ? document.querySelector(target)
      : find(target)
    if (el && useContainer) el = el.closest('div') || el
    if (!el) return null
    el.setAttribute('data-cap-target', '1')
    const r = el.getBoundingClientRect()
    return { x: r.left, y: r.top, w: r.width, h: r.height }
  }, { target, useContainer: !!useContainer, findSrc: findInPage.toString() })
}

async function annotate(page, num) {
  await page.evaluate((num) => {
    const el = document.querySelector('[data-cap-target]')
    if (!el) return
    const r = el.getBoundingClientRect()
    const o = document.createElement('div')
    o.id = '__cap_anno'
    o.style.cssText = `position:fixed;left:${r.left - 6}px;top:${r.top - 6}px;width:${r.width + 12}px;height:${r.height + 12}px;border:3px solid #dc2626;border-radius:10px;z-index:2147483647;pointer-events:none;box-shadow:0 0 0 4000px rgba(15,23,42,0.06)`
    const b = document.createElement('div')
    b.style.cssText = 'position:absolute;left:-16px;top:-16px;width:30px;height:30px;background:#dc2626;color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font:800 15px system-ui;box-shadow:0 2px 8px rgba(0,0,0,.35)'
    b.textContent = num
    o.appendChild(b)
    document.body.appendChild(o)
  }, num)
}

async function runActions(page, actions) {
  for (const a of actions || []) {
    if (a.wait) await new Promise(r => setTimeout(r, a.wait))
    else if (a.fill) {
      const [sel, value] = a.fill
      await page.evaluate(({ sel, value }) => {
        const inp = document.querySelector(sel)
        if (!inp) return
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
        setter.call(inp, value)
        inp.dispatchEvent(new Event('input', { bubbles: true }))
        inp.focus()
      }, { sel, value })
    }
    else if (a.clickSel) await page.evaluate(sel => document.querySelector(sel)?.click(), a.clickSel)
    else if (a.clickText) {
      const [sel, text] = a.clickText
      await page.evaluate(({ sel, text, findSrc }) => {
        const find = eval('(' + findSrc + ')')
        find({ sel, text })?.click()
      }, { sel, text, findSrc: findInPage.toString() })
    }
    else if (a.scrollTo) {
      await page.evaluate(({ target, findSrc }) => {
        const find = eval('(' + findSrc + ')')
        const el = typeof target === 'string' ? document.querySelector(target) : find(target)
        el?.scrollIntoView({ block: 'center' })
      }, { target: a.scrollTo, findSrc: findInPage.toString() })
    }
  }
}

async function main() {
  const login = await fetch(`${API}/api/v1/auth/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'superadmin', password: 'Admin@12345' }),
  }).then(r => r.json())
  if (!login.access_token) throw new Error('login failed')

  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
    args: ['--no-sandbox', '--disable-gpu', `--window-size=${VW.width},${VW.height}`],
    defaultViewport: VW,
  })
  const page = await browser.newPage()

  // seed auth (except for noAuth steps, handled with a separate page)
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' })
  await page.evaluate((t) => {
    localStorage.setItem('access_token', t.access_token)
    if (t.refresh_token) localStorage.setItem('refresh_token', t.refresh_token)
    localStorage.setItem('login_time', new Date().toISOString())
    localStorage.setItem('ars_sidebar_collapsed', '0')
  }, login)

  const warnings = []
  let lastRoute = null
  for (const s of M.sort((a, b) => a.module.localeCompare(b.module) === 0 ? a.step - b.step : 0)) {
    const dir = path.join(OUT, s.module)
    fs.mkdirSync(dir, { recursive: true })
    const file = path.join(dir, `step-${String(s.step).padStart(2, '0')}-${s.name}.png`)
    try {
      let p = page
      if (s.noAuth) {
        const ctx = await browser.createBrowserContext()
        p = await ctx.newPage()
        await p.setViewport(VW)
        await p.goto(BASE + s.route, { waitUntil: 'networkidle2', timeout: 40000 }).catch(() => {})
        await new Promise(r => setTimeout(r, 2500))
      } else {
        if (lastRoute !== s.route) {
          await p.goto(BASE + s.route, { waitUntil: 'networkidle2', timeout: 45000 }).catch(() => {})
          lastRoute = s.route
        } else {
          await p.reload({ waitUntil: 'networkidle2', timeout: 45000 }).catch(() => {})
        }
        await new Promise(r => setTimeout(r, s.wait || 4000))
      }
      if (s.scrollY) await p.evaluate(y => { document.querySelector('main')?.scrollTo(0, y); window.scrollTo(0, y) }, s.scrollY)
      await runActions(p, s.pre)

      /* Replicas for conditional UI that isn't live right now — markup
         mirrors the real components in ListingPage.jsx. */
      if (s.inject === 'gapBanner') {
        await p.evaluate(() => {
          const anchor = [...document.querySelectorAll('h1')].find(h => h.textContent.includes('Listing Generation'))
            ?.closest('div[style*="border"]') || document.querySelector('main .animate-fade-in > div > div')
          const b = document.createElement('div')
          b.id = '__cap_banner'
          b.style.cssText = 'background:#fef3c7;border:1px solid #f59e0b;border-left:4px solid #d97706;border-radius:8px;padding:10px 14px;margin:10px 0;display:flex;align-items:center;gap:10px;font-family:inherit'
          b.innerHTML = '<span style="color:#b45309;font-size:18px">&#9888;</span>'
            + '<div style="flex:1;font-size:12px;color:#78350f"><b style="color:#92400e">1 MAJ_CAT missing from ARS_GRID_HIERARCHY</b>'
            + '<span style="margin-left:6px">— these will be skipped during listing. Run the relevant grids in Grid Builder to populate them.</span>'
            + '<span style="margin-left:6px;color:#a16207;font-weight:600">(901/451 covered)</span></div>'
            + '<button style="height:26px;padding:0 10px;font-size:11px;font-weight:700;background:#fff;color:#92400e;border:1px solid #fcd34d;border-radius:5px">Details</button>'
            + '<button style="height:26px;padding:0 10px;font-size:11px;font-weight:700;background:#d97706;color:#fff;border:none;border-radius:5px">Grid Builder &#8594;</button>'
            + '<button style="height:26px;padding:0 10px;font-size:10px;font-weight:700;background:#fff;color:#92400e;border:1px solid #fcd34d;border-radius:5px">Generate Anyway</button>'
          anchor?.parentElement?.insertBefore(b, anchor)
        })
      }
      if (s.inject === 'parkedBar') {
        await p.evaluate(() => {
          const sections = [...document.querySelectorAll('button')].filter(x => x.textContent.trim().startsWith('Key Numbers'))
          const anchor = sections[0]?.closest('div')?.parentElement
          const b = document.createElement('div')
          b.id = '__cap_parked'
          b.style.cssText = 'background:#fff;border:1px solid #e2e8f0;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,0.04);padding:8px 12px;margin:10px 0;display:flex;justify-content:space-between;align-items:center;font-family:inherit'
          b.innerHTML = '<span style="font-size:11px;font-weight:700;color:#0f172a;display:flex;align-items:center;gap:6px">'
            + '<span style="color:#f59e0b">&#128337;</span> Parked Runs '
            + '<span style="font-size:9px;font-weight:700;color:#f59e0b;background:#f59e0b15;padding:2px 8px;border-radius:4px;border:1px solid #f59e0b40">1</span>'
            + '<span style="font-weight:400;color:#94a3b8;font-size:10px">awaiting review &#8212; Approve to move 5 tables to history, Reject to discard</span></span>'
            + '<span style="display:flex;align-items:center;gap:8px">'
            + '<button style="height:24px;padding:0 12px;font-size:10px;font-weight:700;background:#059669;color:#fff;border:none;border-radius:5px">Approve</button>'
            + '<button style="height:24px;padding:0 12px;font-size:10px;font-weight:700;background:#dc2626;color:#fff;border:none;border-radius:5px">Reject</button>'
            + '<span style="color:#94a3b8">&#9660;</span></span>'
          if (anchor?.nextSibling) anchor.parentElement.insertBefore(b, anchor.nextSibling)
          else document.querySelector('main')?.prepend(b)
        })
      }

      let rect = null
      await p.evaluate(() => document.querySelector('[data-cap-target]')?.removeAttribute('data-cap-target'))
      if (s.highlight) {
        rect = await resolveTarget(p, s.highlight, s.hlContainer)
        if (rect) await annotate(p, s.step)
        else warnings.push(`${s.module}/${s.name}: highlight target not found`)
      }

      let clip
      if (rect && s.clip !== 'viewport') {
        const pad = s.clipPad || 240
        const x = Math.max(0, rect.x - pad)
        const y = Math.max(0, rect.y - Math.min(pad, 160))
        clip = {
          x, y,
          width: Math.min(VW.width - x, rect.w + pad * 2),
          height: Math.min(VW.height - y, rect.h + Math.min(pad, 160) * 2),
        }
        if (clip.width < 700) { clip.x = Math.max(0, clip.x - (700 - clip.width) / 2); clip.width = Math.min(VW.width - clip.x, 700) }
        if (clip.height < 260) { clip.y = Math.max(0, clip.y - (260 - clip.height) / 2); clip.height = Math.min(VW.height - clip.y, 260) }
      }
      await p.screenshot({ path: file, clip })
      await p.evaluate(() => document.getElementById('__cap_anno')?.remove())
      console.log(`ok  ${s.module}/step-${s.step} ${s.name}${rect ? '' : s.highlight ? '  [no-highlight]' : ''}`)
      if (s.noAuth) await p.browserContext().close()
    } catch (e) {
      warnings.push(`${s.module}/${s.name}: FAILED ${e.message.slice(0, 80)}`)
      console.log(`ERR ${s.module}/${s.name}: ${e.message.slice(0, 80)}`)
    }
  }
  await browser.close()
  console.log('\nwarnings:'); warnings.forEach(w => console.log(' -', w))
  console.log(`done: ${M.length} steps`)
}

main().catch(e => { console.error('FATAL', e); process.exit(1) })
