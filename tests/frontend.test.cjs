const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ui = fs.readFileSync(path.join(__dirname, '../app/static/ui.js'), 'utf8');
const app = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function environment({hash = '', fetch, fastTimers = false} = {}) {
  const elements = new Map(), appended = [], storage = new Map();
  function el(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {id, textContent: '', innerHTML: '', style: {}, value: '', classList: {
        add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
        toggle(x, force) { const on = force === undefined ? !classes.has(x) : force; on ? classes.add(x) : classes.delete(x); return on }
      }, setAttribute() {}, remove() { this.removed = true }});
    }
    return elements.get(id);
  }
  const context = vm.createContext({
    window: {}, location: {hash, search: '', reload() {}, replace() {}},
    localStorage: {getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k)},
    document: {getElementById: el, querySelectorAll: () => [], addEventListener() {}, head: {append: (...nodes) => appended.push(...nodes)}, createElement: () => ({remove() { this.removed = true }})},
    matchMedia: () => ({matches: false, addEventListener() {}}),
    addEventListener() {}, scrollTo() {}, requestAnimationFrame() {},
    navigator: {}, URLSearchParams, FormData, AbortController,
    fetch: fetch || (() => { throw Error('Unexpected request') }),
    setTimeout: fastTimers ? (cb,ms) => setTimeout(cb,Math.min(ms,15)) : setTimeout, clearTimeout,
  });
  context.document.documentElement = {dataset: {}};
  vm.runInContext(ui, context);
  return {context, elements, appended, storage, el, run: source => vm.runInContext(source, context)};
}
function telegram(env, calls = []) {
  env.context.window.Telegram = {WebApp: {initData: 'signed-data', ready: () => calls.push('ready'), expand() {}}};
}

test('regular browser does not download Telegram SDK', async () => {
  const e = environment();
  assert.equal(await e.run('loadTelegram()'), null);
  assert.equal(e.appended.length, 0);
});

test('slow Telegram SDK has a bounded wait and can be retried', async () => {
  const e = environment({hash: '#tgWebAppData=signed', fastTimers: true});
  await assert.rejects(e.run('loadTelegram()'), /слишком долго/);
  assert.equal(e.appended[0].removed, true);
  const again = e.run('loadTelegram()');
  telegram(e); e.appended[1].onload();
  assert.equal(await again, e.context.window.Telegram.WebApp);
});

test('signed Telegram login calls ready before API and skips auth/me', async () => {
  const calls = [];
  const e = environment({fetch: async url => {
    calls.push(url);
    return {ok: true, json: async () => ({token: 'session', user: {id: 1, role: 'user'}})};
  }});
  telegram(e, calls);
  const result = await e.run("restoreSession(makeApi(() => 'old'), 'old')");
  assert.equal(result.user.id, 1);
  assert.deepEqual(calls, ['ready', '/api/auth/telegram']);
});

test('expired browser session returns login, network error stays retryable', async () => {
  const e = environment({fetch: async () => ({ok: false, status: 401, json: async () => ({detail: 'Expired'})})});
  assert.equal(await e.run("restoreSession(makeApi(() => 'old'), 'old')"), null);
  e.context.fetch = async () => { throw Error('Offline') };
  await assert.rejects(e.run("restoreSession(makeApi(() => 'old'), 'old')"), /Offline/);
});

test('API timeout also aborts a stalled response body', async () => {
  const e = environment({fastTimers: true, fetch: async (_url, request) => ({ok: true, json: () => new Promise((_, reject) => request.signal.addEventListener('abort', () => reject(Error('aborted'))))})});
  await assert.rejects(e.run("makeApi(() => '')('/api/config', {timeoutMs: 5})"), /слишком долго/);
});

test('Leaflet is downloaded from the app once for concurrent maps', async () => {
  const e = environment();
  const first = e.run('loadLeaflet()'), second = e.run('loadLeaflet()');
  assert.equal(first, second);
  assert.equal(e.appended.length, 2);
  assert.ok(e.appended.every(x => (x.href || x.src).startsWith('/static/vendor/leaflet-1.9.4/')));
  e.appended[1].onload();
  await first;
});

test('app becomes usable while configuration and map are still pending', async () => {
  const calls = [];
  let resolveConfig;
  const e = environment({hash: '#tgWebAppData=signed', fetch: async url => {
    calls.push(url);
    if (url === '/api/config') return {ok: true, json: () => new Promise(resolve => resolveConfig = resolve)};
    if (url === '/api/auth/telegram') return {ok: true, json: async () => ({token: 'session', user: {id: 1, role: 'user', nickname: 'User'}})};
    if (url.startsWith('/api/events')) return {ok: true, json: async () => ({items: []})};
    throw Error('Unexpected request ' + url);
  }});
  telegram(e);
  e.context.createMap = () => new Promise(() => {});
  e.context.drawPins = () => {};
  e.run(app);
  await tick(); await tick();
  assert.equal(e.el('boot').classList.contains('hidden'), true);
  assert.equal(e.el('app').classList.contains('hidden'), false);
  assert.equal(calls.filter(x => x === '/api/auth/telegram').length, 1);
  assert.equal(calls.includes('/api/auth/me'), false);
  assert.ok(calls.some(x => x.startsWith('/api/events')));
  resolveConfig({rewards: {min_withdraw: 100}});
  await tick();
});

test('failed SDK shows retry instead of a permanently blank app', async () => {
  const e = environment({hash: '#tgWebAppPlatform=tdesktop', fetch: async () => ({ok: true, json: async () => ({rewards: {}})})});
  e.run(app);
  e.appended[0].onerror();
  await tick(); await tick();
  assert.equal(e.el('boot').classList.contains('hidden'), false);
  assert.equal(e.el('bootRetry').classList.contains('hidden'), false);
  assert.match(e.el('bootMessage').textContent, /Telegram/);
});
