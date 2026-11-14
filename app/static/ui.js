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
    const headers = {...(options.body instanceof FormData ? {} : {'Content-Type': 'application/json'}), ...(getToken() ? {'X-Session': getToken()} : {}), ...(options.headers || {})};
    const response = await fetch(url, {...options, headers});
    let data; try { data = await response.json() } catch { data = {detail: 'Ошибка сервера'} }
    if (!response.ok) {
      let message = data.detail || 'Ошибка запроса';
      if (Array.isArray(message)) message = message.map(x => x.msg).join('; ');
      throw Object.assign(new Error(message), {status: response.status});
    }
    return data;
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
const LEAFLET = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet';
let leafletLoading;
function loadLeaflet() {
  return leafletLoading ||= new Promise((resolve, reject) => {
    const css = Object.assign(document.createElement('link'), {rel: 'stylesheet', href: LEAFLET + '.css', integrity: 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=', crossOrigin: ''});
    const js = Object.assign(document.createElement('script'), {src: LEAFLET + '.js', integrity: 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=', crossOrigin: '', async: true});
    js.onload = resolve;
    js.onerror = () => reject(new Error('не удалось загрузить библиотеку карты'));
    setTimeout(() => reject(new Error('превышено время загрузки')), 12000);
    document.head.append(css, js);
  });
}
async function createMap(el, center = MOSCOW, zoom = 12) {
  await loadLeaflet();
  const map = L.map(el, {center: [center.lat, center.lon], zoom});
  map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>'}).addTo(map);
  map.pins = L.layerGroup().addTo(map);
  return map;
}
const PIN_ICON = {event: '●', order: '?', ad: '★', review: '…', rejected: '✕'};
const eventKind = e => e.promoted ? 'ad' : e.status === 'published' ? 'event' : e.status;
const pinSize = e => Math.min(46, Math.max(20, 18 + Math.log2(1 + e.popularity) * 2.7 + (e.promoted ? 13 : 0)));
function drawPins(map, events, onClick, selectedId) {
  map.pins.clearLayers();
  events.forEach(e => {
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
