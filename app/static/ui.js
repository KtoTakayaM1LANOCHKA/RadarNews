/* NewsRadar: общие помощники интерфейса — тема, уведомления, API, радар, карта, навигация «назад». */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const store = {
  get(key) { try { return localStorage.getItem(key) } catch { return null } },
  set(key, value) { try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value) } catch {} }
};
const LABELS = {
  kind: {tip: 'Новость', confirm: 'Подтверждение', refute: 'Опровержение'},
  sub: {pending: 'На рассмотрении', accepted: 'Принято', rejected: 'Отклонено'},
  action: {reject: 'Отклонено', publish: 'Опубликовано', revise: 'Создана задача', order_fulfilled: 'Задача выполнена'}
};
const MOSCOW = {lat: 55.7519, lon: 37.6176};

function isTelegramLaunch() {
  const params = new URLSearchParams(location.hash.slice(1));
  return !!window.Telegram?.WebApp?.initData || params.has('tgWebAppData') || params.has('tgWebAppPlatform');
}
let telegramLoading;
function loadTelegram() {
  if (window.Telegram?.WebApp) return Promise.resolve(window.Telegram.WebApp);
  if (!isTelegramLaunch()) return Promise.resolve(null);
  if (telegramLoading) return telegramLoading;
  telegramLoading = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://telegram.org/js/telegram-web-app.js'; script.async = true;
    const finish = error => {
      clearTimeout(timer); script.onload = script.onerror = null;
      if (error) { script.remove(); reject(error) }
      else if (window.Telegram?.WebApp) resolve(window.Telegram.WebApp);
      else reject(new Error('Telegram не передал данные приложения. Откройте его снова.'));
    };
    const timer = setTimeout(() => finish(new Error('Telegram отвечает слишком долго. Повторите подключение.')), 6000);
    script.onload = () => finish();
    script.onerror = () => finish(new Error('Не удалось подключиться к Telegram. Повторите попытку.'));
    document.head.append(script);
  }).catch(error => { telegramLoading = null; throw error });
  return telegramLoading;
}
async function telegramSession() {
  const tg = await loadTelegram();
  if (!tg?.initData) {
    if (isTelegramLaunch()) throw new Error('Откройте приложение заново через кнопку бота.');
    return null;
  }
  // Remove Telegram's native loading screen as soon as our shell is ready.
  tg.ready(); tg.expand();
  return makeApi(() => '')('/api/auth/telegram', {method: 'POST', timeoutMs: 10000, body: JSON.stringify({init_data: tg.initData})});
}
async function restoreSession(api, token) {
  const telegram = await telegramSession();
  if (telegram) return telegram; // The signed login already returns the user.
  if (!token) return null;
  try { return {token, user: await api('/api/auth/me', {timeoutMs: 10000})} }
  catch (error) { if (error.status === 401) return null; throw error }
}
function showBoot(message = 'Открываем NewsRadar…', failed = false) {
  $('boot').classList.remove('hidden'); $('bootMessage').textContent = message;
  $('bootRetry').classList.toggle('hidden', !failed);
  $('bootRetry').onclick = () => location.reload();
}
function hideBoot() { $('boot').classList.add('hidden') }

// ---------- Тема: «как в системе» по умолчанию, выбор хранится в браузере ----------
const systemDark = matchMedia('(prefers-color-scheme: dark)');
function applyTheme() {
  const mode = store.get('nr_theme') || 'system';
  document.documentElement.dataset.theme = mode === 'system' ? (systemDark.matches ? 'dark' : 'light') : mode;
  document.querySelectorAll('[data-theme-set]').forEach(b => b.classList.toggle('on', b.dataset.themeSet === mode));
}
function themeSeg(withLabels) {
  return '<span class="seg" role="group" aria-label="Тема оформления">' +
    [['system', '◐', 'Как в системе'], ['light', '☀', 'Светлая'], ['dark', '☾', 'Тёмная']]
      .map(([mode, icon, label]) => `<button type="button" data-theme-set="${mode}" title="${label}" aria-label="${label}">${icon}${withLabels ? ' ' + label : ''}</button>`).join('') + '</span>';
}
systemDark.addEventListener('change', applyTheme);
applyTheme();

// ---------- Навигация: стрелка «назад» возвращает на предыдущий экран ----------
const router = {
  depth: 0, goingBack: false,
  back(fallback) { if (this.depth > 0) { this.goingBack = true; history.back() } else location.hash = fallback || '' }
};
addEventListener('hashchange', () => { router.depth += router.goingBack ? -1 : 1; router.goingBack = false });

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.theme-slot').forEach(slot => slot.innerHTML = themeSeg());
  document.querySelectorAll('[data-dismiss]').forEach(x => { if (x.parentElement.id && store.get('nr_hide_' + x.parentElement.id)) x.parentElement.classList.add('hidden') });
  applyTheme();
});
document.addEventListener('click', e => {
  const theme = e.target.closest('[data-theme-set]');
  if (theme) { store.set('nr_theme', theme.dataset.themeSet); applyTheme() }
  const dismiss = e.target.closest('[data-dismiss]');   // крестик: скрыть блок и запомнить это
  if (dismiss) { const box = dismiss.parentElement; box.classList.add('hidden'); if (box.id) store.set('nr_hide_' + box.id, '1') }
  const back = e.target.closest('[data-back]');
  if (back) router.back(back.dataset.back);
});

function toast(message, bad = false) {
  const t = $('toast');
  t.textContent = message; t.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.className = 'toast', 4300);
}

function makeApi(getToken) {
  return async (url, options = {}) => {
    const {timeoutMs = 30000, signal, ...request} = options;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, {once: true});
    const timer = setTimeout(abort, timeoutMs);
    const headers = {...(request.body instanceof FormData ? {} : {'Content-Type': 'application/json'}), ...(getToken() ? {'X-Session': getToken()} : {}), ...(request.headers || {})};
    try {
      const response = await fetch(url, {...request, signal: controller.signal, headers});
      let data;
      try { data = await response.json() }
      catch (error) { if (controller.signal.aborted) throw error; data = {detail: 'Ошибка сервера'} }
      if (!response.ok) {
        let message = data.detail || 'Ошибка запроса';
        if (Array.isArray(message)) message = message.map(x => x.msg).join('; ');
        throw Object.assign(new Error(message), {status: response.status});
      }
      return data;
    } catch (error) {
      if (controller.signal.aborted) throw new Error('Сервер отвечает слишком долго. Повторите попытку.');
      throw error;
    } finally {
      clearTimeout(timer); signal?.removeEventListener('abort', abort);
    }
  };
}

// ---------- Радар: луч вращается, красные точки вспыхивают, когда он проходит над ними ----------
function radar(el, blips = 6) {
  let html = '<i class="sweep"></i>';
  for (let i = 0; i < blips; i++) {
    const angle = (i * 137.5 + 40) % 360, radius = 14 + (i * 29) % 30, rad = angle * Math.PI / 180;
    html += `<b style="left:${(50 + radius * Math.sin(rad)).toFixed(1)}%;top:${(50 - radius * Math.cos(rad)).toFixed(1)}%;animation-delay:${(angle / 360 * 4).toFixed(2)}s"></b>`;
  }
  el.classList.add('radar'); el.setAttribute('aria-hidden', 'true'); el.innerHTML = html;
}

// ---------- Карта: Leaflet + тайлы OpenStreetMap, ключ API не нужен ----------
const LEAFLET = '/static/vendor/leaflet-1.9.4/leaflet';
let leafletLoading;
function loadLeaflet() {
  return leafletLoading ||= new Promise((resolve, reject) => {
    const css = Object.assign(document.createElement('link'), {rel: 'stylesheet', href: LEAFLET + '.css', integrity: 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=', crossOrigin: ''});
    const js = Object.assign(document.createElement('script'), {src: LEAFLET + '.js', integrity: 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=', crossOrigin: '', async: true});
    const timer = setTimeout(() => reject(new Error('превышено время загрузки')), 6000);
    js.onload = () => { clearTimeout(timer); resolve() };
    js.onerror = () => { clearTimeout(timer); reject(new Error('не удалось загрузить библиотеку карты')) };
    document.head.append(css, js);
  });
}
async function createMap(el, center = MOSCOW, zoom = 12) {
  await loadLeaflet();
  const map = L.map(el, {center: [center.lat, center.lon], zoom, attributionControl: false});
  const wrap = map.getContainer().closest('.map-wrap') || map.getContainer();
  if (!wrap.nextElementSibling?.classList.contains('map-source-credit')) {
    const credit = document.createElement('div');
    credit.className = 'map-source-credit';
    credit.style.cssText = 'text-align:right;font-size:12px;line-height:1.5;margin:4px 2px 8px;color:var(--muted)';
    credit.innerHTML = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
    wrap.after(credit);
  }
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>'}).addTo(map);
  map.pins = L.layerGroup().addTo(map);
  return map;
}
const PIN_ICON = {event: '●', order: '?', ad: '★', review: '…', rejected: '✕'};
const eventKind = e => e.promoted ? 'ad' : e.status === 'published' ? 'event' : e.status;
const pinSize = e => Math.min(46, Math.max(20, 18 + Math.log2(1 + e.popularity) * 2.7 + (e.promoted ? 13 : 0)));
function drawPins(map, events, onClick, selectedId) {
  map.pins.clearLayers();
  events.filter(e => e.location_known !== 0).forEach(e => {
    const kind = eventKind(e), size = pinSize(e);
    const icon = L.divIcon({className: 'pin-wrap', iconSize: [size, size], iconAnchor: [size / 2, size / 2],
      html: `<span class="pin ${kind}${e.id === selectedId ? ' selected' : ''}" style="font-size:${size * .5}px">${PIN_ICON[kind] || '●'}</span>`});
    L.marker([e.latitude, e.longitude], {icon, title: e.title, alt: e.title, riseOnHover: true}).on('click', () => onClick(e)).addTo(map.pins);
  });
}
function showMe(map, pos) {   // синяя точка «я здесь» и центрирование карты
  map.me?.remove();
  map.me = L.marker([pos.lat, pos.lon], {interactive: false, keyboard: false, icon: L.divIcon({className: 'pin-wrap', iconSize: [18, 18], iconAnchor: [9, 9], html: '<span class="me-dot"></span>'})}).addTo(map);
  map.setView([pos.lat, pos.lon], 12, {animate: true});
}
function locateMe(onFound, onFail) {   // браузер сам спрашивает разрешение на геолокацию
  if (!navigator.geolocation) return onFail('Геолокация недоступна в этом браузере');
  navigator.geolocation.getCurrentPosition(p => onFound({lat: p.coords.latitude, lon: p.coords.longitude}),
    () => onFail('Нет доступа к местоположению. Разрешите его в настройках браузера.'), {timeout: 12000, maximumAge: 300000});
}
