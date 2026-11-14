/* NewsRadar: пользовательское приложение — вход, карта, лента задач, отправка новости, личный кабинет. */
const state = {
  token: store.get('nr_user_session') || '', user: null, rules: null,
  center: MOSCOW, myLoc: null, radius: 10, events: [], selected: null,
  types: {event: true, order: true, ad: true}, topic: '', map: null, started: false
};
const api = makeApi(() => state.token);
const TABS = ['map', 'send', 'feed', 'profile'];
const TYPES = [['event', '● Новости'], ['order', '? Нужно подтвердить'], ['ad', '★ Реклама']];
const GAUGE_MAX = 300;   // баллов до края шкалы репутации
const signed = n => (n > 0 ? '+' : '') + n;

// ---------- Вход и регистрация сразу при открытии ----------
function setToken(token) { state.token = token; store.set('nr_user_session', token || null) }
async function start() {
  radar($('authRadar'), 7); radar($('logoRadar'), 2); radar($('sendRadar'), 8);
  state.rules = (await api('/api/config').catch(() => ({}))).rewards;
  if (state.token) {
    try { state.user = await api('/api/auth/me') } catch { setToken('') }
  }
  if (state.user && state.user.role !== 'editor') enter(); else showAuth();
}
function showAuth() {
  state.user = null;
  $('app').classList.add('hidden'); $('tabbar').classList.add('hidden'); $('auth').classList.remove('hidden');
  $('codeForm').classList.add('hidden'); $('phoneForm').classList.remove('hidden');
}
function enter() {
  $('auth').classList.add('hidden'); $('app').classList.remove('hidden'); $('tabbar').classList.remove('hidden');
  paintAvatar(); route();
  if (!state.started) { state.started = true; initMap().then(() => locate(true)) }
}
$('phoneForm').onsubmit = async e => {
  e.preventDefault();
  try {
    const d = await api('/api/auth/request-code', {method: 'POST', body: JSON.stringify({phone: $('authPhone').value})});
    $('phoneForm').classList.add('hidden'); $('codeForm').classList.remove('hidden');
    $('authCode').value = d.demo_code || '';
    $('codeHint').textContent = d.demo_code ? `Демо-режим: SMS не отправляется, код ${d.demo_code} подставлен автоматически.` : 'Код отправлен на указанный номер.';
    $('authCode').focus();
  } catch (err) { toast(err.message, true) }
};
$('codeBack').onclick = () => { $('codeForm').classList.add('hidden'); $('phoneForm').classList.remove('hidden') };
$('codeForm').onsubmit = async e => {
  e.preventDefault();
  try {
    const d = await api('/api/auth/verify-code', {method: 'POST', body: JSON.stringify({phone: $('authPhone').value, code: $('authCode').value})});
    setToken(d.token); state.user = d.user; enter();
    toast(`Добро пожаловать, ${d.user.nickname}!`);
  } catch (err) { toast(err.message, true) }
};
function avatarHtml(u) { return u.avatar ? `<img src="${esc(u.avatar)}" alt="">` : esc((u.nickname || '?').trim()[0].toUpperCase()) }
function paintAvatar() { $('profileBtn').innerHTML = avatarHtml(state.user) }

// ---------- Разделы: адрес #map / #send / #feed / #profile, работает кнопка «назад» ----------
function route() {
  if (!state.user) return;
  const tab = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'map';
  TABS.forEach(t => $('page-' + t).classList.toggle('hidden', t !== tab));
  document.querySelectorAll('#tabbar button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  closeModal(); scrollTo({top: 0});
  if (tab === 'map') { setTimeout(() => state.map?.invalidateSize(), 60); loadEvents() }
  if (tab === 'feed') loadEvents();
  if (tab === 'profile') renderProfile();
}
addEventListener('hashchange', route);
document.querySelectorAll('#tabbar button').forEach(b => b.onclick = () => location.hash = b.dataset.tab);
$('profileBtn').onclick = () => location.hash = 'profile';

function openModal(title, html) { $('modalTitle').textContent = title; $('modalBody').innerHTML = html; $('modal').classList.remove('hidden') }
function closeModal() { $('modal').classList.add('hidden'); $('modalBody').innerHTML = '' }
$('modalClose').onclick = closeModal;
$('modal').onclick = e => { if (e.target === $('modal')) closeModal() };
addEventListener('keydown', e => { if (e.key === 'Escape') { closeModal(); choose(null) } });

// ---------- Карта, геолокация и фильтры ----------
async function initMap() {
  try {
    state.map = await createMap($('map'), state.center);
    $('mapStatus').textContent = 'Нажмите на значок, чтобы открыть событие';
  } catch (err) {
    $('map').parentElement.classList.add('hidden');
    $('mapStatus').textContent = `Карта недоступна (${err.message}) — события показаны списком`;
  }
  draw();
}
function locate(auto) {   // запрос местоположения: автоматически при входе и по кнопке «Где я»
  locateMe(pos => {
    state.myLoc = state.center = pos;
    if (state.map) showMe(state.map, pos);
    loadEvents();
  }, message => toast(auto ? 'Местоположение не получено — показана Москва' : message, !auto));
}
$('locBtn').onclick = () => locate(false);
$('demoBtn').onclick = () => { state.center = MOSCOW; state.map?.setView([MOSCOW.lat, MOSCOW.lon], 12, {animate: true}); loadEvents() };
$('radius').onchange = e => { state.radius = +e.target.value; loadEvents() };
$('feedRefresh').onclick = loadEvents;

async function loadEvents() {
  try {
    state.events = (await api(`/api/events?lat=${state.center.lat}&lon=${state.center.lon}&radius=${state.radius}`)).items;
    draw();
  } catch (err) { toast(err.message, true) }
}
function draw() {
  const topics = [...new Set(state.events.map(e => e.topic))].sort();
  if (!topics.includes(state.topic)) state.topic = '';
  $('typeChips').innerHTML = TYPES.map(([k, label]) => `<button class="chip ${state.types[k] ? 'on' : ''}" data-type="${k}" aria-pressed="${state.types[k]}">${label}</button>`).join('');
  $('topicChips').innerHTML = ['', ...topics].map(t => `<button class="chip ${state.topic === t ? 'on' : ''}" data-topic="${esc(t)}">${t ? esc(t) : 'Все темы'}</button>`).join('');
  const shown = state.events.filter(e => state.types[eventKind(e)] && (!state.topic || e.topic === state.topic));
  $('mapCount').textContent = shown.length ? `На карте: ${shown.length} из ${state.events.length} событий в радиусе ${state.radius} км`
    : 'Здесь событий нет. Увеличьте радиус, измените фильтры или нажмите «↺ Москва» для демо.';
  if (state.map) drawPins(state.map, shown, choose, state.selected);
  else $('mapList').innerHTML = shown.map(e => `<article class="story">${eventCard(e)}</article>`).join('');
  const picked = shown.find(e => e.id === state.selected);
  if (state.selected && !picked) choose(null); else if (picked) $('mapCard').innerHTML = closeX + eventCard(picked);
  const tasks = state.events.filter(e => e.status === 'order' && e.order);
  $('feedList').innerHTML = tasks.map(e => `<article class="story">${eventCard(e)}</article>`).join('')
    || '<div class="empty">Рядом нет задач на подтверждение. Увеличьте радиус на карте.</div>';
}
const closeX = '<button class="x" data-close-card aria-label="Закрыть карточку">✕</button>';
function eventCard(e) {
  const task = e.status === 'order' && e.order;
  return `<div class="row between wrap"><span class="tag ${e.promoted ? 'ad' : esc(e.status)}">${e.promoted ? 'Реклама' : esc(e.status_label)}</span><span class="muted tiny">${esc(e.topic)}${e.distance_km != null ? ` · ${e.distance_km} км` : ''}</span></div>
    <h3 style="margin-top:8px">${esc(e.title)}</h3><p>${esc(e.summary)}</p>
    ${task ? `<div class="notice" style="margin-bottom:10px"><b>Что нужно уточнить:</b> ${esc(e.order.question)}</div>` : ''}
    <div class="row wrap"><span class="pill">◉ Интерес: ${e.popularity}</span><span class="pill">✦ Эксклюзив: ${e.exclusive_score}/100</span>${task ? `<span class="pill">+${e.order.reward_points} баллов за принятый ответ</span>` : ''}</div>
    ${task ? `<div class="row wrap" style="margin-top:12px"><button class="btn green" data-ev="${e.id}" data-kind="confirm">✓ Подтвердить</button><button class="btn danger" data-ev="${e.id}" data-kind="refute">✕ Опровергнуть</button></div>` : ''}`;
}
function choose(e) {
  state.selected = e ? e.id : null;
  $('mapCard').classList.toggle('hidden', !e);
  draw();
}
document.addEventListener('click', e => {
  const type = e.target.closest('[data-type]'), topic = e.target.closest('[data-topic]'), ev = e.target.closest('[data-ev]');
  if (type) { state.types[type.dataset.type] = !state.types[type.dataset.type]; draw() }
  if (topic) { state.topic = topic.dataset.topic; draw() }
  if (e.target.closest('[data-close-card]')) choose(null);
  if (ev) openEvidence(state.events.find(x => x.id === +ev.dataset.ev), ev.dataset.kind);
});

// ---------- Подтверждение или опровержение по задаче ----------
function openEvidence(event, kind) {
  const confirm = kind === 'confirm';
  openModal(confirm ? 'Подтвердить событие' : 'Опровергнуть событие', `<p class="muted">${esc(event.title)}</p>
    <form id="evidenceForm">
      <div class="field"><label for="evText">${confirm ? 'Что вы можете подтвердить?' : 'Что неверно и почему?'}</label><textarea id="evText" minlength="8" maxlength="3000" required placeholder="Факты, время и источники"></textarea></div>
      <div class="field"><label for="evFiles">Фото или видео (необязательно)</label><input type="file" id="evFiles" accept="image/jpeg,image/png,image/webp,video/mp4,video/webm" multiple></div>
      <label class="check"><input type="checkbox" id="evContact"> Редактор может связаться со мной</label>
      <button class="btn primary full">Отправить редактору →</button>
    </form>`);
  $('evidenceForm').onsubmit = async e => {
    e.preventDefault();
    try {
      const d = await api('/api/submissions', {method: 'POST', body: JSON.stringify({event_id: event.id, kind, text: $('evText').value, contact_opt_in: $('evContact').checked})});
      await sendFiles(d.id, [...$('evFiles').files]);
      closeModal(); toast('Отправлено. Баллы придут на следующий день после проверки.'); loadEvents();
    } catch (err) { toast(err.message, true) }
  };
}
async function sendFiles(submissionId, files) {
  if (files.length > 5) throw new Error('Не более 5 файлов');
  for (const file of files) { const fd = new FormData(); fd.append('file', file); await api(`/api/submissions/${submissionId}/media`, {method: 'POST', body: fd}) }
}

// ---------- Прислать новость ----------
$('tipForm').onsubmit = async e => {
  e.preventDefault();
  const withGeo = $('tipGeo').checked;
  if (withGeo && !state.myLoc) return toast('Сначала разрешите геолокацию кнопкой «⌖ Где я» на карте', true);
  try {
    const d = await api('/api/submissions', {method: 'POST', body: JSON.stringify({kind: 'tip', title: $('tipTitle').value, text: $('tipText').value, topic: $('tipTopic').value,
      contact_opt_in: $('tipContact').checked, ...(withGeo ? {latitude: state.myLoc.lat, longitude: state.myLoc.lon} : {})})});
    await sendFiles(d.id, [...$('tipFiles').files]);
    $('tipForm').reset(); toast('Новость отправлена редактору'); location.hash = 'profile';
  } catch (err) { toast(err.message, true) }
};

// ---------- Личный кабинет: аватар, ник, репутация, кошелёк ----------
function repLevel(p) { return p < -100 ? 'низкая' : p < -20 ? 'ниже нейтральной' : p <= 20 ? 'нейтральная' : p <= 100 ? 'хорошая' : 'высокая' }
function gauge(points) {   // стрелка: 0 — по центру, плюс — вправо, минус — влево
  return `<svg class="gauge" viewBox="0 0 220 132" role="img" aria-label="Шкала репутации: ${points} баллов">
    <defs><linearGradient id="gaugeColors"><stop offset="0" stop-color="#e52f42"/><stop offset=".5" stop-color="#fdbc02"/><stop offset="1" stop-color="#0e9f6e"/></linearGradient></defs>
    <path d="M20 110A90 90 0 0 1 200 110" fill="none" stroke="url(#gaugeColors)" stroke-width="26" pathLength="100" stroke-dasharray="13.2 1.2667"/>
    <path class="needle" id="needle" d="M110 110L105 104 110 30 115 104Z"/><circle class="hub" cx="110" cy="110" r="9"/>
    <text x="12" y="130">−</text><text x="208" y="130" text-anchor="end">+</text></svg>`;
}
async function renderProfile() {
  let d;
  try { d = await api('/api/my/submissions') } catch (err) { if (err.status === 401) { setToken(''); return showAuth() } return toast(err.message, true) }
  const u = state.user = d.user, r = state.rules;
  paintAvatar();
  $('profileBody').innerHTML = `
    <div class="card profile-top">
      <label class="avatar xl" title="Сменить аватар">${avatarHtml(u)}<span class="edit">✎</span><input type="file" id="avatarFile" accept="image/jpeg,image/png,image/webp" hidden></label>
      <div class="grow" style="min-width:200px">
        <form id="nickForm" class="row wrap"><input id="nick" style="flex:1 1 150px" value="${esc(u.nickname)}" minlength="2" maxlength="32" required aria-label="Ник"><button class="btn primary">Сохранить</button></form>
        <p class="muted tiny" style="margin:8px 0 0">${esc(u.phone)} ${u.role === 'admin' ? '<span class="tag order">Админ</span>' : ''}</p>
      </div>
    </div>
    <div class="card row between wrap"><div class="row wrap"><b>Тема</b>${themeSeg(true)}</div>
      <div class="row wrap">${u.role === 'admin' ? '<a class="btn ghost" href="/editor">Редакция ↗</a>' : ''}<button id="logoutBtn" class="btn ghost">⇥ Выйти</button></div></div>
    <div class="grid2">
      <div class="card center"><h2>Репутация</h2>${gauge(u.credits)}
        <p style="margin:4px 0"><b>${signed(u.credits)} баллов</b> · ${repLevel(u.credits)}</p>
        <p class="muted tiny" style="margin:0">${u.pending_points ? `Завтра придёт: ${signed(u.pending_points)} баллов` : 'Достоверные новости двигают стрелку вправо, недостоверные — влево'}</p></div>
      <div class="card"><h2>Кошелёк</h2><div class="money">${u.wallet} ₽</div>
        <p class="muted tiny" style="margin:6px 0 12px">${u.pending_money ? `Завтра придёт: +${u.pending_money} ₽. ` : ''}Вывод от ${r.min_withdraw} ₽.</p>
        <button id="withdrawBtn" class="btn primary full" ${u.wallet < r.min_withdraw ? 'disabled' : ''}>Вывести ${u.wallet >= r.min_withdraw ? u.wallet + ' ₽' : ''} →</button>
        <p class="muted tiny" style="margin:8px 0 0">Демо: реальные выплаты не подключены.</p></div>
    </div>
    <details class="card"><summary>Как начисляются баллы и вознаграждения</summary>
      <div class="table-scroll"><table><thead><tr><th>За что</th><th>Баллы</th><th>Деньги</th></tr></thead><tbody>
        ${[...r.tiers].reverse().map((t, i, all) => `<tr><td>Достоверная новость · ${esc(t.name)}</td><td>${t.from || 10}–${all[i + 1] ? all[i + 1].from - 1 : 100}</td><td>${t.money} ₽</td></tr>`).join('')}
        <tr><td>Принятое подтверждение или опровержение по задаче</td><td>${signed(r.confirm)}</td><td>—</td></tr>
        <tr><td>Отклонённое подтверждение</td><td>${r.confirm_rejected}</td><td>—</td></tr>
        <tr><td>Недостоверная новость</td><td>${r.false_news}</td><td>—</td></tr></tbody></table></div>
      <p class="muted tiny" style="margin:10px 0 0">Баллы за новость = 10 + 0,45 × значимость + 0,45 × эксклюзивность (оценивает редактор, 0–100). Баллы и деньги приходят на следующий день после решения редактора.</p></details>
    <div class="card"><h2>Мои сообщения</h2>${d.items.map(s => `<div class="story" style="box-shadow:none">
        <div class="row between wrap"><span class="tag ${esc(s.status)}">${LABELS.sub[s.status] || esc(s.status)}</span><span class="muted tiny">${LABELS.kind[s.kind] || esc(s.kind)} · ${esc(s.submitted_at.slice(0, 10))}</span></div>
        <h3>${esc(s.title)}</h3><p>${esc(s.text)}</p>
        ${s.points || s.money ? `<span class="pill">${signed(s.points)} баллов${s.money ? ` · +${s.money} ₽` : ''}${s.reward_pending ? ' · придёт завтра' : ''}</span>` : ''}</div>`).join('') || '<div class="empty">Пока нет сообщений. Нажмите «＋ Прислать новость».</div>'}</div>`;
  applyTheme();
  requestAnimationFrame(() => $('needle').style.transform = `rotate(${Math.max(-1, Math.min(1, u.credits / GAUGE_MAX)) * 90}deg)`);
  $('nickForm').onsubmit = async e => {
    e.preventDefault();
    try { await api('/api/my/profile', {method: 'PATCH', body: JSON.stringify({nickname: $('nick').value})}); toast('Ник сохранён'); renderProfile() } catch (err) { toast(err.message, true) }
  };
  $('avatarFile').onchange = async e => {
    if (!e.target.files[0]) return;
    const fd = new FormData(); fd.append('file', e.target.files[0]);
    try { await api('/api/my/avatar', {method: 'POST', body: fd}); toast('Аватар обновлён'); renderProfile() } catch (err) { toast(err.message, true) }
  };
  $('withdrawBtn').onclick = async () => {
    try { toast((await api('/api/my/withdraw', {method: 'POST'})).message); renderProfile() } catch (err) { toast(err.message, true) }
  };
  $('logoutBtn').onclick = () => { setToken(''); showAuth(); toast('Вы вышли из аккаунта') };
}

start();
