/* NewsRadar: редакторская панель — обзор, новости, задачи для пользователей, подтверждения, карта, аналитика. */
let token = store.get('nr_editor_token') || '', viaApp = false;   // viaApp: вошли сессией администратора из приложения
let data = null, rules = null, view = 'overview', current = null, period = 'month', query = '', mapSelected = null, fitted = false;
const api = makeApi(() => token);
const maps = {};
const mapFilter = {types: {review: true, order: true, event: true, rejected: true, ad: true}, topic: ''};
const MAP_TYPES = [['review', '… На проверке'], ['order', '? Задачи'], ['event', '● Опубликовано'], ['rejected', '✕ Отклонено'], ['ad', '★ Реклама']];
const STATUS = {review: 'На проверке', order: 'Ждут подтверждений', published: 'Опубликовано', rejected: 'Отклонено'};
const VIEWS = {
  overview: ['Панель редактора', 'Сводка по редакции'], events: ['Новости', 'Редакторская очередь'], event: ['Новость', 'Решение редактора'],
  orders: ['Задачи для проверки пользователей', 'Запросы подтверждений'], verification: ['Подтверждение', 'Ответы пользователей'],
  sources: ['Источники', 'Поиск по открытым ресурсам'], map: ['Карта', 'География событий'],
  analytics: ['Аналитика', 'Показатели редакции'], users: ['Пользователи', 'Репутация и активность']
};
const wide = () => innerWidth >= 960;
const when = iso => esc(String(iso).slice(0, 16).replace('T', ' '));
const matches = e => !query || `${e.title} ${e.summary} ${e.topic} ${e.status_label}`.toLocaleLowerCase('ru').includes(query);
const metric = (label, note, value, feature) => `<div class="metric ${feature ? 'feature' : ''}"><span>${label}</span><b>${value}</b><span>${note}</span></div>`;
const count = status => data.events.filter(e => e.status === status).length;
const ensureMap = (id, zoom) => maps[id] ||= createMap($(id), MOSCOW, zoom);
const fitAll = (map, list) => { const located = list.filter(e => e.location_known !== 0); if (located.length) map.fitBounds(L.latLngBounds(located.map(e => [e.latitude, e.longitude])).pad(.25), {maxZoom: 14}) };

// ---------- Вход: пароль редактора или сессия администратора из приложения ----------
async function start() {
  radar($('loginRadar'), 7); radar($('logoRadar'), 2); radar($('promoRadar'), 4);
  showBoot();
  api('/api/config', {timeoutMs: 10000}).then(d => { rules = d.rewards; if ($('imp')) rewardHint() }).catch(err => toast(err.message, true));
  let telegram;
  try { telegram = await telegramSession() } catch (err) { showBoot(err.message, true); return }
  if (telegram) {
    store.set('nr_user_session', telegram.token);
    if (['editor', 'admin'].includes(telegram.user.role)) { token = telegram.token; viaApp = true; return enter() }
    token = ''; hideBoot(); $('login').classList.remove('hidden'); return;
  }
  for (const candidate of new Set([store.get('nr_editor_token'), store.get('nr_user_session')])) {
    if (!candidate) continue;
    token = candidate;
    try {
      if (['editor', 'admin'].includes((await api('/api/auth/me', {timeoutMs: 10000})).role)) { viaApp = candidate !== store.get('nr_editor_token'); return enter() }
    } catch (err) { if (err.status !== 401 && err.status !== 403) { showBoot(err.message, true); return } }
  }
  token = ''; hideBoot(); $('login').classList.remove('hidden');
}
function enter() {
  hideBoot();
  $('login').classList.add('hidden'); $('workspace').classList.remove('hidden');
  document.body.classList.toggle('nav-open', wide() && store.get('nr_nav') !== '0');
  route(); refresh();
}
function logout() {
  store.set('nr_editor_token', null);
  if (viaApp) location.href = '/'; else location.reload();
}
$('loginForm').onsubmit = async e => {
  e.preventDefault();
  try {
    token = (await api('/api/editor/login', {method: 'POST', body: JSON.stringify({password: $('password').value})})).token;
    store.set('nr_editor_token', token); enter();
  } catch (err) { toast(err.message, true) }
};
$('logout').onclick = logout;

// ---------- Меню: три полоски скрывают и показывают все боковые кнопки ----------
function setNav(open) { document.body.classList.toggle('nav-open', open); if (wide()) store.set('nr_nav', open ? '1' : '0') }
$('menuBtn').onclick = () => setNav(!document.body.classList.contains('nav-open'));
$('sideClose').onclick = $('sideScrim').onclick = () => setNav(false);

// ---------- Разделы: адрес #overview, #events, #event/12 … ----------
function route() {
  const [name, id] = (location.hash.slice(1) || 'overview').split('/');
  view = VIEWS[name] ? name : 'overview'; current = +id || null;
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('hidden', v.id !== 'view-' + view));
  document.querySelectorAll('#menu [data-view]').forEach(a => a.classList.toggle('active', a.dataset.view === (view === 'event' ? 'events' : view)));
  [$('viewTitle').textContent, $('viewSub').textContent] = VIEWS[view];
  if (!wide()) setNav(false);
  scrollTo({top: 0}); render();
}
addEventListener('hashchange', route);
async function refresh() {
  try { data = await api('/api/editor/events'); render() }
  catch (err) { toast(err.message, true); if (err.status === 401 || err.status === 403) logout() }
}
function render() {
  if (data) ({overview: renderOverview, events: renderEvents, event: renderEvent, orders: renderOrders, verification: renderSubs, map: renderMap, analytics: renderAnalytics, users: renderUsers, sources: renderSources}[view] || (() => {}))();
}
$('refreshBtn').onclick = () => refresh().then(() => toast('Данные обновлены'));
$('search').oninput = e => { query = e.target.value.trim().toLocaleLowerCase('ru'); render() };

// ---------- Обзор ----------
function renderOverview() {
  const events = data.events, exclusive = events.filter(e => e.exclusive_score >= 75).length;
  $('ovMetrics').innerHTML = metric('Потенциальные эксклюзивы', 'Эксклюзивность от 75', exclusive, true) + metric('Задачи для пользователей', 'Ждут подтверждений', count('order'))
    + metric('На проверке', 'Ждут решения редактора', count('review')) + metric('Опубликовано', 'За всё время', count('published'));
  const days = {day: 1, week: 7, month: 30, year: 365}[period], bars = {day: 6, week: 7, month: 6, year: 12}[period], step = days / bars * 864e5, now = Date.now();
  const bins = Array.from({length: bars}, (_, i) => { const end = now - (bars - 1 - i) * step; return events.filter(e => { const t = Date.parse(e.created_at); return t >= end - step && t < end }).length });
  const max = Math.max(1, ...bins), unit = {day: 'ч', week: 'д', month: '', year: 'м'}[period];
  $('ovChart').innerHTML = bins.map((n, i) => `<div><i style="height:${Math.max(4, n / max * 150)}px" title="${n} событий"></i>${period === 'day' ? (i + 1) * 4 : i + 1}${unit}</div>`).join('');
  $('ovQueue').innerHTML = [['Нужна проверка', 'review'], ['Открытые задачи', 'order'], ['Опубликовано', 'published'], ['Отклонено', 'rejected']]
    .map(([label, s]) => `<div class="line"><span><span class="tag ${s}">●</span> ${label}</span><b>${count(s)}</b></div>`).join('');
  const done = count('published') + count('rejected'), percent = events.length ? Math.round(done / events.length * 100) : 0;
  $('ovProgress').innerHTML = `<div class="donut" style="--p:${percent}%"><b>${percent}%</b></div><div class="grow">${[['Опубликовано', count('published')], ['В работе', count('review') + count('order')], ['Отклонено', count('rejected')]]
    .map(([label, n]) => `<div class="row between" style="padding:8px 0;border-bottom:1px solid var(--line)"><span class="muted">${label}</span><b>${n}</b></div>`).join('')}</div>`;
  $('ovRows').innerHTML = events.filter(matches).slice(0, 12).map(e => `<tr data-open="${e.id}" tabindex="0"><td><b>${esc(e.title)}</b></td><td>${esc(e.topic)}</td><td><span class="tag ${esc(e.status)}">${esc(e.status_label)}</span></td><td>${e.importance}/100</td><td>${e.exclusive_score}/100</td><td>→</td></tr>`).join('')
    || '<tr><td colspan="6" class="muted">Ничего не найдено</td></tr>';
  ensureMap('miniMap', 11).then(map => { map.invalidateSize(); drawPins(map, events, e => location.hash = 'event/' + e.id) })
    .catch(() => $('miniMap').innerHTML = '<div class="empty">Карта недоступна</div>');
}

// ---------- Новости: список → событие на весь экран, выход стрелкой ----------
function renderEvents() {
  $('eventCards').innerHTML = data.events.filter(matches).map(e => `<button class="item" data-open="${e.id}">
      <span class="row between"><span class="tag ${esc(e.status)}">${esc(e.status_label)}</span><small>#${e.id} →</small></span>
      <b>${esc(e.title)}</b><small>${esc(e.topic)} · значимость ${e.importance} · эксклюзив ${e.exclusive_score} · ответов ${e.submission_count}</small></button>`).join('')
    || '<div class="empty">Новостей не найдено</div>';
}
function rewardHint() {
  if (!rules) { $('rewardHint').textContent = 'Загружаем тарифы вознаграждений…'; return }
  const points = 10 + Math.floor((45 * (+$('imp').value + +$('exc').value) + 50) / 100), tier = rules.tiers.find(t => points >= t.from);
  $('impValue').textContent = $('imp').value; $('excValue').textContent = $('exc').value;
  $('rewardHint').textContent = `Автору за достоверную новость: +${points} баллов и ${tier.money} ₽ (тип «${tier.name}»). За недостоверную: ${rules.false_news} баллов без денег. Начисление — на следующий день.`;
}
function subCard(s) {
  return `<div class="story" style="box-shadow:none">
    <div class="row between wrap"><span class="tag ${esc(s.status)}">${LABELS.sub[s.status] || esc(s.status)}</span><span class="muted tiny">${LABELS.kind[s.kind] || esc(s.kind)} · ${esc(s.provider)} · ${when(s.submitted_at)}</span></div>
    <h3>${esc(s.event_title)}</h3><p>${esc(s.text)}</p>
    <span class="muted tiny">Файлов: ${s.media_count}${s.contact_phone ? ` · контакт: ${esc(s.contact_phone)}` : ''}</span>
    ${s.status === 'pending' && s.kind !== 'tip' ? `<div class="row wrap" style="margin-top:10px"><button class="btn green" data-review="${s.id}" data-accept="1">✓ Принять</button><button class="btn danger" data-review="${s.id}" data-accept="0">✕ Отклонить</button></div>` : ''}</div>`;
}
function renderEvent() {
  const e = data.events.find(x => x.id === current);
  if (!e) return location.replace('#events');
  const draft = data.drafts.find(d => d.event_id === e.id), history = data.history.filter(h => h.event_id === e.id), subs = data.submissions.filter(s => s.event_id === e.id);
  $('eventBody').innerHTML = `<div class="card">
      <div class="row between wrap"><span class="tag ${esc(e.status)}">${esc(e.status_label)}</span><span class="muted tiny">Событие #${e.id} · ${esc(e.topic)} · ответов: ${e.submission_count}</span></div>
      <h1 style="margin:12px 0 8px">${esc(e.title)}</h1><p class="lead">${esc(e.summary)}</p>
      ${/^https?:\/\//i.test(e.source_url || '') ? `<p><a href="${esc(e.source_url)}" target="_blank" rel="noopener noreferrer">Источник ↗</a></p>` : ''}
      ${draft ? `<details><summary>Черновик v${draft.version}</summary><p class="muted" style="white-space:pre-wrap;margin-top:8px">${esc(draft.content)}</p></details>` : ''}
      ${e.order ? `<div class="notice warn" style="margin-top:12px"><b>Открытая задача:</b> ${esc(e.order.question)}</div>` : ''}
      <button id="analyzeBtn" class="btn ghost" style="margin:12px 0">◈ Анализ доказательств (демо-правила)</button><div id="analysis"></div>
      ${e.status === 'published' ? '<div class="notice">Новость опубликована. Публикация окончательна — отменить её нельзя.</div>' : `
      <div class="grid2"><div class="field"><label for="imp">Значимость: <b id="impValue"></b>/100</label><input id="imp" type="range" min="0" max="100" value="${e.importance}"></div>
        <div class="field"><label for="exc">Эксклюзивность: <b id="excValue"></b>/100</label><input id="exc" type="range" min="0" max="100" value="${e.exclusive_score}"></div></div>
      <p id="rewardHint" class="muted tiny"></p>
      ${e.location_known === 0 ? '<p class="notice">Место новости из интернета не установлено. Для показа на карте укажите проверенные координаты.</p><div class="grid2"><div class="field"><label for="eventLat">Широта</label><input id="eventLat" type="number" min="-90" max="90" step="any"></div><div class="field"><label for="eventLon">Долгота</label><input id="eventLon" type="number" min="-180" max="180" step="any"></div></div>' : ''}
      <div class="field"><label for="note">Комментарий редактора</label><textarea id="note" placeholder="Причина решения. Для публикации неподтверждённой новости после независимой проверки укажите «проверено редактором»."></textarea></div>
      <div class="field"><label for="question">Что должны подтвердить пользователи? (для задачи)</label><textarea id="question" placeholder="Например: нужна ссылка на официальное расписание или документ"></textarea></div>
      <div class="row wrap"><button class="btn green" data-decide="publish">✓ Опубликовать</button>
        <button class="btn danger" data-decide="reject" ${e.status === 'rejected' ? 'disabled title="Уже отклонено"' : ''}>✕ Отклонить</button>
        <button class="btn primary" data-decide="revise" ${e.status === 'order' ? 'disabled title="Задача уже создана"' : ''}>＋ Создать задачу</button></div>
      <p class="muted tiny" style="margin:10px 0 0">Публикацию отменить нельзя. Отклонённую новость можно опубликовать позже.</p>`}
    </div>
    <div class="stack">
      <div class="card"><h2>Ответы пользователей</h2>${subs.map(subCard).join('') || '<p class="muted">Ответов пока нет</p>'}</div>
      <div class="card"><h2>История решений</h2><div class="timeline">${history.map(h => `<div><b>${LABELS.action[h.action] || esc(h.action)}</b> · ${when(h.created_at)}${h.note ? `<br>${esc(h.note)}` : ''}</div>`).join('') || 'Решений пока нет'}</div></div>
    </div>`;
  if ($('imp')) { $('imp').oninput = $('exc').oninput = rewardHint; rewardHint() }
  $('analyzeBtn').onclick = async () => {
    try {
      const d = await api(`/api/events/${e.id}/analyze`, {method: 'POST'});
      $('analysis').innerHTML = `<div class="notice" style="margin-bottom:12px"><b>Демо-проверка:</b> ${esc(d.summary)}<ul>${d.flags.map(f => `<li>${esc(f)}</li>`).join('')}</ul></div>`;
    } catch (err) { toast(err.message, true) }
  };
}
async function decide(action) {
  if (action === 'publish' && !confirm('Опубликовать новость? Это действие нельзя отменить.')) return;
  const buttons = [...document.querySelectorAll('[data-decide]')], wasDisabled = buttons.map(b => b.disabled);
  buttons.forEach(b => b.disabled = true);
  try {
    const coordinates = $('eventLat') && ($('eventLat').value || $('eventLon').value) ? {latitude: $('eventLat').value === '' ? null : +$('eventLat').value, longitude: $('eventLon').value === '' ? null : +$('eventLon').value} : {};
    await api(`/api/editor/events/${current}/decision`, {method: 'POST', body: JSON.stringify({action, note: $('note').value, question: $('question').value, importance: +$('imp').value, exclusive_score: +$('exc').value, ...coordinates})});
    toast({reject: 'Новость отклонена', publish: 'Новость опубликована', revise: 'Задача для пользователей создана'}[action]);
    await refresh();
  } catch (err) { toast(err.message, true); buttons.forEach((b, i) => b.disabled = wasDisabled[i]) }
}

// ---------- Задачи для проверки пользователей и подтверждения ----------
function renderOrders() {
  $('orderCards').innerHTML = data.events.filter(e => e.status === 'order' && e.order && matches(e)).map(e => `<div class="item">
      <span class="row between"><span class="tag order">Задача #${e.order.id}</span><small>Ответов: ${e.submission_count}</small></span>
      <b>${esc(e.title)}</b><small>${esc(e.order.question)}</small><a class="btn ghost" href="#event/${e.id}">Открыть новость →</a></div>`).join('')
    || '<div class="empty">Открытых задач нет. Создайте задачу в карточке новости.</div>';
}
function renderSubs() {
  $('subList').innerHTML = data.submissions.map(subCard).join('') || '<div class="empty">Ответов пользователей пока нет</div>';
}
async function review(id, accepted) {
  try {
    const d = await api(`/api/editor/submissions/${id}/review`, {method: 'POST', body: JSON.stringify({accepted})});
    toast('Ответ рассмотрен' + (d.awarded_credits ? ` · пользователю завтра придёт ${d.awarded_credits > 0 ? '+' : ''}${d.awarded_credits} баллов` : ''));
    refresh();
  } catch (err) { toast(err.message, true) }
}

// ---------- Карта: OpenStreetMap, фильтры по статусу и тематике ----------
function renderMap() {
  const topics = [...new Set(data.events.map(e => e.topic))].sort();
  if (!topics.includes(mapFilter.topic)) mapFilter.topic = '';
  $('emapTypes').innerHTML = MAP_TYPES.map(([k, label]) => `<button class="chip ${mapFilter.types[k] ? 'on' : ''}" data-mtype="${k}" aria-pressed="${mapFilter.types[k]}">${label}</button>`).join('');
  $('emapTopics').innerHTML = ['', ...topics].map(t => `<button class="chip ${mapFilter.topic === t ? 'on' : ''}" data-mtopic="${esc(t)}">${t ? esc(t) : 'Все темы'}</button>`).join('');
  const shown = data.events.filter(e => mapFilter.types[eventKind(e)] && (!mapFilter.topic || e.topic === mapFilter.topic) && matches(e));
  $('emapCount').textContent = `На карте: ${shown.length} из ${data.events.length} событий`;
  const picked = shown.find(e => e.id === mapSelected);
  $('emapCard').classList.toggle('hidden', !picked);
  if (picked) $('emapCard').innerHTML = `<button class="x" data-close-card aria-label="Закрыть карточку">✕</button>
    <div class="row between wrap"><span class="tag ${esc(picked.status)}">${esc(picked.status_label)}</span><span class="muted tiny">${esc(picked.topic)} · ответов: ${picked.submission_count}</span></div>
    <h3 style="margin:8px 0 4px">${esc(picked.title)}</h3><p class="muted">${esc(picked.summary)}</p><a class="btn primary" href="#event/${picked.id}">Открыть событие →</a>`;
  ensureMap('emap', 11).then(map => {
    map.invalidateSize(); drawPins(map, shown, e => { mapSelected = e.id; renderMap() }, mapSelected);
    if (!fitted && shown.length) { fitted = true; fitAll(map, shown) }
  }).catch(err => $('emapStatus').textContent = `Карта недоступна (${err.message})`);
}
$('emapLoc').onclick = () => locateMe(pos => ensureMap('emap').then(map => showMe(map, pos)), message => toast(message, true));
$('emapFit').onclick = () => ensureMap('emap').then(map => fitAll(map, data.events));

// ---------- Аналитика и пользователи ----------
function renderAnalytics() {
  const events = data.events;
  $('anMetrics').innerHTML = metric('Всего событий', 'Все статусы', events.length, true) + metric('Ответы пользователей', 'Новости, подтверждения, опровержения', data.submissions.length)
    + metric('В работе', 'Проверка и задачи', count('review') + count('order')) + metric('Потенциальные эксклюзивы', 'Эксклюзивность от 75', events.filter(e => e.exclusive_score >= 75).length);
  $('anBars').innerHTML = Object.entries(STATUS).map(([s, label]) => `<div class="row" style="margin:12px 0"><span style="width:150px;flex:none">${label}</span><div class="track"><i style="width:${events.length ? count(s) / events.length * 100 : 0}%"></i></div><b>${count(s)}</b></div>`).join('');
}
function renderUsers() {
  const subs = data.submissions;
  $('usMetrics').innerHTML = metric('Сообщений', 'Всего', subs.length, true) + metric('На рассмотрении', 'Ждут решения', subs.filter(s => s.status === 'pending').length)
    + metric('Принято', 'Одобрено редакцией', subs.filter(s => s.status === 'accepted').length) + metric('Опровержений', 'Все статусы', subs.filter(s => s.kind === 'refute').length);
  $('usRows').innerHTML = data.users.map(u => `<tr><td><b>${esc(u.nickname)}</b></td><td>${u.credits > 0 ? '+' : ''}${u.credits}</td><td>${u.wallet} ₽</td><td>${u.submissions}</td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">Пользователей пока нет</td></tr>';
}

// ---------- Общие обработчики нажатий ----------
document.addEventListener('click', e => {
  const pick = sel => e.target.closest(sel);
  const open = pick('[data-open]'), decision = pick('[data-decide]'), rev = pick('[data-review]'), per = pick('[data-period]'), mtype = pick('[data-mtype]'), mtopic = pick('[data-mtopic]');
  if (open) location.hash = 'event/' + open.dataset.open;
  if (decision) decide(decision.dataset.decide);
  if (rev) review(rev.dataset.review, rev.dataset.accept === '1');
  if (per) { period = per.dataset.period; document.querySelectorAll('[data-period]').forEach(b => b.classList.toggle('on', b === per)); renderOverview() }
  if (mtype) { mapFilter.types[mtype.dataset.mtype] = !mapFilter.types[mtype.dataset.mtype]; renderMap() }
  if (mtopic) { mapFilter.topic = mtopic.dataset.mtopic; renderMap() }
  if (pick('[data-close-card]')) { mapSelected = null; renderMap() }
});
addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.matches('tr[data-open]')) location.hash = 'event/' + e.target.dataset.open });
$('searchForm').onsubmit = async e => {
  e.preventDefault();
  try {
    const d = await api('/api/discovery/search', {method: 'POST', body: JSON.stringify({query: $('searchQuery').value})});
    searchHits = d.items;
    $('searchResults').innerHTML = d.items.map((x, i) => `<div class="story" style="box-shadow:none"><b>${esc(x.title)}</b><p>${esc(x.description)}</p><div class="row wrap"><a target="_blank" rel="noopener noreferrer" href="${esc(/^https?:\/\//i.test(x.url) ? x.url : '#')}">Открыть источник ↗</a><button class="btn ghost" data-import-hit="${i}">В очередь редактора</button></div></div>`).join('') || '<div class="empty">Ничего не найдено</div>';
  } catch (err) { toast(err.message, true) }
};

start();

let sources = [], searchHits = [];
async function renderSources() {
  try {
    const d = await api('/api/editor/sources');
    sources = d.items;
    $('sourceList').innerHTML = (d.worker_enabled ? '' : '<p class="notice">Фоновый сбор отключён. Для отдельного воркера запустите python -m app.bots.telegram_bot.</p>') + sources.map(s => `<article class="story" style="box-shadow:none"><div class="row between wrap"><b>${esc(s.name)}</b><span class="tag">${s.enabled ? 'Включён' : 'Пауза'}</span></div><p style="overflow-wrap:anywhere">${esc(s.kind === 'search' ? s.query : s.url)}</p><p class="muted tiny">Каждые ${s.interval_minutes} мин · импортировано: ${s.imported_count} · проверка: ${s.last_checked ? when(s.last_checked) : 'ещё не было'}</p>${s.last_error ? `<p class="notice warn">${esc(s.last_error)}</p>` : ''}<div class="row wrap"><button class="btn ghost" data-edit-source="${s.id}">Изменить</button><button class="btn ghost" data-toggle-source="${s.id}">${s.enabled ? 'Приостановить' : 'Включить'}</button></div></article>`).join('');
  } catch (err) { toast(err.message, true) }
}
function sourcePayload(s) {
  return {name: s.name, kind: s.kind, query: s.query, url: s.url, topic: s.topic, latitude: s.latitude, longitude: s.longitude, interval_minutes: s.interval_minutes, enabled: !!s.enabled};
}
$('sourceForm').onsubmit = async e => {
  e.preventDefault();
  const id = $('sourceId').value, previous = sources.find(s => s.id === +id);
  const payload = {...(previous ? sourcePayload(previous) : {}), name: $('sourceName').value, kind: $('sourceKind').value, query: $('sourceQuery').value, url: $('sourceUrl').value, topic: $('sourceTopic').value, interval_minutes: +$('sourceInterval').value, enabled: $('sourceEnabled').checked};
  try {
    await api('/api/editor/sources' + (id ? '/' + id : ''), {method: id ? 'PUT' : 'POST', body: JSON.stringify(payload)});
    $('sourceForm').reset(); $('sourceId').value = ''; $('sourceFormTitle').textContent = 'Добавить источник';
    toast('Источник сохранён'); renderSources();
  } catch (err) { toast(err.message, true) }
};
$('sourceCancel').onclick = () => { $('sourceForm').reset(); $('sourceId').value = ''; $('sourceFormTitle').textContent = 'Добавить источник' };
$('collectSources').onclick = async () => {
  $('collectSources').disabled = true;
  try {
    const d = await api('/api/editor/sources/collect', {method: 'POST'});
    toast(d.busy ? 'Сбор уже выполняется' : `Добавлено: ${d.imported}${d.errors.length ? '. Ошибок источников: ' + d.errors.length : ''}`, !!d.errors.length);
    await refresh();
  } catch (err) { toast(err.message, true) }
  finally { $('collectSources').disabled = false; renderSources() }
};
document.addEventListener('click', async e => {
  const edit = e.target.closest('[data-edit-source]'), toggle = e.target.closest('[data-toggle-source]'), hit = e.target.closest('[data-import-hit]');
  if (edit) {
    const s = sources.find(s => s.id === +edit.dataset.editSource);
    $('sourceId').value = s.id; $('sourceName').value = s.name; $('sourceKind').value = s.kind; $('sourceQuery').value = s.query; $('sourceUrl').value = s.url; $('sourceTopic').value = s.topic; $('sourceInterval').value = s.interval_minutes; $('sourceEnabled').checked = !!s.enabled;
    $('sourceFormTitle').textContent = 'Изменить источник'; $('sourceForm').scrollIntoView({behavior: 'smooth'});
  }
  if (toggle) {
    const s = sources.find(s => s.id === +toggle.dataset.toggleSource);
    try { await api('/api/editor/sources/' + s.id, {method: 'PUT', body: JSON.stringify({...sourcePayload(s), enabled: !s.enabled})}); renderSources() } catch (err) { toast(err.message, true) }
  }
  if (hit) {
    const x = searchHits[+hit.dataset.importHit];
    hit.disabled = true;
    try {
      const d = await api('/api/discovery/import', {method: 'POST', body: JSON.stringify({title: x.title, summary: x.description.slice(0, 1200), source_url: x.url})});
      toast(d.imported ? 'Новость добавлена в очередь' : 'Найден возможный дубль'); await refresh();
    } catch (err) { toast(err.message, true); hit.disabled = false }
  }
});
