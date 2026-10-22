'use strict';
/* Focus Pulse — дашборд обратной связи фокус-группы. Данные: /api/state; действия админа — POST /api/*. */

const S = {
  data: null, view: 'Обзор', detail: null, detailId: null, task: null, taskId: null,
  inboxState: 'На проверке', inboxChat: '', auditQuery: '', polling: null, lastLoad: 0,
};
const F = {
  from: '', to: '', drill: '', platform: '', section: '', operation: '', type: '', version: '', priority: '',
  status: '', owner: '', chat: '', exec: '', query: '', initiator: '', order: 'newest', group: 'platform',
};
const VIEWS = [
  ['◫', 'Обзор'], ['☷', 'Все обращения'], ['⚑', 'Баги'], ['✧', 'Пожелания и UX'], ['?', 'Вопросы'],
  ['◈', 'На утверждение'], ['▤', 'Источники'], ['⌁', 'Фильтрация чата'], ['↻', 'Журнал анализа'], ['⚙', 'Настройки'],
];
const RESOLVED = ['Устранено', 'Закрыто', 'Реализовано'];
const TERMINAL = ['Устранено', 'Закрыто', 'Реализовано', 'Отвечено', 'Отклонено'];
const PRIORITY_RANK = { 'Критичный': 0, 'Высокий': 1, 'Средний': 2, 'Низкий': 3 };
const MSG_STATES = ['На проверке', 'В очереди', 'Отфильтровано', 'Контекст', 'Учтено'];
const PALETTE = ['#286546', '#90ae79', '#d7b769', '#638fa5', '#9c88ad', '#cc886c', '#668378', '#a4ac57', '#7f9b9d', '#c5a5b3', '#8c8980', '#b5835a'];

// ---------------------------------------------------------------- утилиты
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = esc;
const $ = id => document.getElementById(id);
const day = iso => (iso || '').slice(0, 10);
const showDate = d => d ? d.slice(0, 10).split('-').reverse().join('.') : 'Не задан';
const showTime = iso => (iso || '').slice(11, 16);
const showDT = iso => iso ? `${showDate(iso)} ${showTime(iso)}` : '—';
const today = () => day(S.data?.meta?.now) || new Date().toISOString().slice(0, 10);
const isAdmin = () => !!S.data?.me?.admin;
const opts = (values, current, labels) => values.map((v, n) => `<option value="${attr(v)}" ${v === current ? 'selected' : ''}>${esc(labels ? labels[n] : v)}</option>`).join('');
const chatTitle = id => (S.data.chats.find(c => c.chat_id === id) || {}).title || id;
const issueById = id => S.data.issues.find(i => i.issue_id === id);
function dateShift(date, n) { const d = new Date(date + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
function plural(n, one, few, many) { n = Math.abs(n) % 100; const n1 = n % 10; if (n > 10 && n < 20) return many; if (n1 > 1 && n1 < 5) return few; if (n1 === 1) return one; return many; }
function toast(text) { const e = $('toast'); e.textContent = text; e.style.display = 'block'; clearTimeout(window.__tt); window.__tt = setTimeout(() => e.style.display = 'none', 3800); }

function storedActor() { try { return localStorage.getItem('fp_actor') || ''; } catch { return ''; } }
S.actor = storedActor();
async function api(method, path, body) {
  if (method !== 'GET' && S.data?.me?.link && !S.actor && !path.startsWith('/api/auth') && path !== '/api/logout') {
    openNameModal();
    const err = new Error('Представьтесь — имя попадёт в журнал изменений'); err.errors = [err.message]; throw err;
  }
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch', 'X-Actor': encodeURIComponent(S.actor || '') },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const errors = data?.errors || [(res.status === 401 ? 'Войдите как администратор' : `Ошибка ${res.status}`)];
    const err = new Error(errors.join('. '));
    err.errors = errors; err.status = res.status;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------- загрузка
async function load(quiet = false) {
  try {
    S.data = await api('GET', '/api/state');
    S.lastLoad = Date.now();
    if (!F.from) quickPeriod('week', false);
    render();
    if (S.data.meta.running) schedulePoll(3000);
  } catch (e) {
    if (!quiet) $('app').innerHTML = `<div class="empty">Не удалось загрузить данные: ${esc(e.message)}</div>`;
  }
}
function schedulePoll(ms) { clearTimeout(S.polling); S.polling = setTimeout(() => load(true), ms); }
setInterval(() => {
  const busy = document.querySelector('.drawerback.open') || !$('taskmodal').hidden || document.activeElement?.matches('input,textarea,select');
  if (!busy && Date.now() - S.lastLoad > 60000) load(true);
}, 15000);

// ---------------------------------------------------------------- навигация и рамка
function go(v) { S.view = v; F.drill = ''; F.exec = ''; window.scrollTo(0, 0); render(); }

function renderNav() {
  const review = S.data.issues.filter(i => i.review).length;
  const pending = S.data.messages.filter(m => m.state === 'На проверке').length;
  $('nav').innerHTML = VIEWS.map(([icon, v]) => `<button class="${S.view === v ? 'active' : ''}" onclick="go('${v}')"><span class="navicon">${icon}</span>${v}${v === 'На утверждение' && review ? `<span class="count">${review}</span>` : ''}${v === 'Фильтрация чата' && pending ? `<span class="count">${pending}</span>` : ''}</button>`).join('');
}

function initials(name) { return (name || '?').split(/\s+/).filter(w => /^[\p{L}\d]/u.test(w)).slice(0, 2).map(w => w[0]).join('').toUpperCase(); }

function renderFrame() {
  const m = S.data.meta, me = S.data.me;
  $('sidebottom').innerHTML = `<div class="small" style="margin-bottom:16px"><span class="dot">●</span> ${m.demo ? 'Демо-режим' : 'Рабочий режим'} · ${m.classifier === 'claude' ? 'AI: Claude' : 'AI: правила'}</div>${me.link
    ? `<span class="avatar">${esc(initials(S.actor || 'Г'))}</span><b>${esc(S.actor || 'Гость')}</b><p class="small">Редактирование по ссылке · <button class="linkbtn" onclick="openNameModal()">${S.actor ? 'Сменить имя' : 'Представиться'}</button></p>`
    : me.admin
    ? `<span class="avatar">${esc(initials(me.name))}</span><b>${esc(me.name)}</b><p class="small">Администратор · <button class="linkbtn" onclick="logout()">Выйти</button></p>`
    : `<span class="avatar">👁</span><b>Наблюдатель</b><p class="small">Только просмотр · <button class="linkbtn" onclick="openLogin()">Войти</button></p>`}`;
  $('topright').innerHTML = `<span class="demo">${m.demo ? 'ДЕМО' : 'FOCUS PULSE'}</span><span class="small">${showDate(m.now)}</span><span class="avatar">${me.admin ? esc(initials(me.name)) : '👁'}</span>`;
}

const SUBTITLES = {
  'Обзор': 'От сообщений в чатах — к понятным решениям по продукту.',
  'На утверждение': 'Новые обращения от AI. Проверьте тип, раздел и срок, затем примите или отклоните.',
  'Источники': 'Подключённые чаты Telegram. Каждый сигнал хранит ссылку на исходное сообщение.',
  'Фильтрация чата': 'Отделяем обращения от разговоров, сохраняя контекст. Ничего не удаляется.',
  'Журнал анализа': 'Запуски анализа и все решения администраторов: кто, когда, что изменил.',
  'Настройки': 'Анализ, фильтрация, справочник операций, релизы и интеграции.',
};

function render() {
  if (!S.data) return;
  renderNav(); renderFrame();
  const v = S.view, m = S.data.meta;
  const title = v === 'Обзор' ? 'Пульс обратной связи' : v;
  const actions = `<div class="actions"><button onclick="refresh()">↻ Обновить</button>${isAdmin() ? `<button class="primary" onclick="runAnalysis()" ${m.running ? 'disabled' : ''}>${m.running ? '◌ Анализируем…' : '✧ Запустить анализ'}</button>` : ''}</div>`;
  let body;
  if (v === 'Источники') body = sourcesPage();
  else if (v === 'Фильтрация чата') body = inboxPage();
  else if (v === 'Журнал анализа') body = logPage();
  else if (v === 'Настройки') body = settingsPage();
  else body = registryPage();
  const lr = m.last_run;
  const next = m.next_run_at && m.auto ? ` · следующий ${showTime(m.next_run_at)}` : '';
  $('app').innerHTML = `<div class="heading"><div><h1>${esc(title)}</h1><p>${SUBTITLES[v] || 'Обратная связь фокус-группы мобильного приложения.'}</p></div>${actions}</div>${body}<div class="bottomnote"><span>${m.demo ? 'Демо-данные из выгрузки прототипа · ' : ''}Данные: Google Sheets${m.pending_writes ? ` · несохранённых изменений: ${m.pending_writes}` : ''}</span><span id="sync">Последний анализ: ${lr ? showDT(lr.finished_at) : 'ещё не было'} · ${m.auto ? `каждые ${m.interval_min} мин` : 'по кнопке'}${next} · в очереди ${m.queued}</span></div>`;
}

async function refresh() { await load(); toast('Данные обновлены'); }

async function runAnalysis() {
  try {
    const r = await api('POST', '/api/run', {});
    toast(r.started ? 'Анализ запущен' : 'Анализ уже идёт');
    S.data.meta.running = true; render(); schedulePoll(2500);
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- вход
function openLogin() {
  const m = S.data.meta;
  $('logincontent').innerHTML = `<button class="close" onclick="closeModal('loginmodal')" aria-label="Закрыть">✕</button><span class="ailabel">ВХОД АДМИНИСТРАТОРА</span><h2>Войти через Telegram</h2>
  <ol class="loginsteps"><li>Откройте бота${m.bot_username ? ` <a href="https://t.me/${attr(m.bot_username)}" target="_blank" rel="noopener">@${esc(m.bot_username)}</a>` : ''}.</li><li>Отправьте команду <code>/login</code>.</li><li>Откройте присланную ссылку — она действует 10 минут.</li></ol>
  <p class="small">Вход доступен только Telegram-аккаунтам из списка администраторов.</p>
  ${m.login_widget ? '<div id="tgwidget" style="margin-top:14px"></div>' : ''}
  ${m.demo ? '<p><a class="primary buttonlink" href="/auth/demo">Войти как демо-администратор</a></p>' : ''}`;
  $('loginmodal').hidden = false;
  if (m.login_widget) {
    const s = document.createElement('script');
    s.async = true; s.src = 'https://telegram.org/js/telegram-widget.js?22';
    s.setAttribute('data-telegram-login', m.bot_username); s.setAttribute('data-size', 'large');
    s.setAttribute('data-onauth', 'onTelegramAuth(user)'); s.setAttribute('data-request-access', 'write');
    $('tgwidget').append(s);
  }
}
async function onTelegramAuth(user) {
  try { await api('POST', '/api/auth/telegram', user); closeModal('loginmodal'); await load(); toast('Вы вошли как администратор'); }
  catch (e) { toast(e.message); }
}
async function logout() { await api('POST', '/api/logout', {}); await load(); toast('Вы вышли'); }
function closeModal(id) { $(id).hidden = true; }
function openNameModal() {
  $('logincontent').innerHTML = `<button class="close" onclick="closeModal('loginmodal')" aria-label="Закрыть">✕</button><span class="ailabel">КТО ВНОСИТ ИЗМЕНЕНИЯ</span><h2>Представьтесь</h2><p class="small">Имя сохранится в этом браузере и будет записываться в журнал изменений рядом с каждым решением.</p><label class="field">Имя и роль<input id="actorName" maxlength="60" value="${attr(S.actor)}" placeholder="Например: Игорь, PM"></label><button class="primary" onclick="saveActor()">Сохранить</button>`;
  $('loginmodal').hidden = false; setTimeout(() => $('actorName')?.focus(), 50);
}
function saveActor() {
  const v = $('actorName').value.trim();
  if (!v) { toast('Введите имя'); return; }
  S.actor = v; try { localStorage.setItem('fp_actor', v); } catch { /* только на эту сессию */ }
  closeModal('loginmodal'); render(); toast('Имя сохранено. Повторите действие');
}

// ---------------------------------------------------------------- выборка
function viewItems() {
  return S.data.issues.filter(i => !i.merged_into).filter(i => {
    switch (S.view) {
      case 'Баги': return i.type === 'Баг';
      case 'Пожелания и UX': return i.type === 'Пожелание' || i.type === 'UX';
      case 'Вопросы': return i.type === 'Вопрос';
      case 'На утверждение': return i.review;
      default: return true;
    }
  });
}
function textOf(i) { return `${i.issue_id} ${i.title} ${i.section} ${i.operation} ${i.summary} ${i.first_text}`.toLowerCase(); }
function authorsOf(i) { return i.reporters.map(r => `${r.name} @${r.username} ${r.author_id}`).join(' ').toLowerCase(); }
function matchesExec(i) {
  switch (F.exec) {
    case 'open': return !TERMINAL.includes(i.status);
    case 'done': return RESOLVED.includes(i.status);
    case 'late': return i.overdue;
    case 'related': return i.related.length > 0;
    case 'unverified': return i.screen_unverified;
    default: return true;
  }
}
function scoped(ignoreDay = false, ignoreGroupFilter = '') {
  const reviewView = S.view === 'На утверждение';
  return viewItems().filter(i => {
    const d = day(i.created_at);
    if (!reviewView && (d < F.from || d > F.to)) return false;
    if (!ignoreDay && F.drill && d !== F.drill) return false;
    if (F.platform && ignoreGroupFilter !== 'platform' && i.platform !== F.platform) return false;
    if (F.section && ignoreGroupFilter !== 'section' && i.section !== F.section) return false;
    if (F.operation && i.operation !== F.operation) return false;
    if (F.type && i.type !== F.type) return false;
    if (F.version && ignoreGroupFilter !== 'version' && (i.app_version || 'Не указана') !== F.version) return false;
    if (F.priority && i.priority !== F.priority) return false;
    if (F.status && (F.status === 'На утверждении' ? !i.review : (i.status !== F.status || i.review))) return false;
    if (F.owner && i.owner !== F.owner) return false;
    if (F.chat && !i.chats.includes(F.chat)) return false;
    if (!matchesExec(i)) return false;
    if (F.query && !textOf(i).includes(F.query.toLowerCase())) return false;
    if (F.initiator && !authorsOf(i).includes(F.initiator.toLowerCase())) return false;
    return true;
  });
}
function filtered() {
  const arr = scoped();
  if (S.view === 'На утверждение') return arr.sort((a, b) => a.confidence - b.confidence || b.created_at.localeCompare(a.created_at));
  const by = {
    newest: (a, b) => b.created_at.localeCompare(a.created_at),
    oldest: (a, b) => a.created_at.localeCompare(b.created_at),
    priority: (a, b) => (PRIORITY_RANK[a.priority] ?? 9) - (PRIORITY_RANK[b.priority] ?? 9) || b.created_at.localeCompare(a.created_at),
    participants: (a, b) => b.participant_count - a.participant_count || b.msg_count - a.msg_count,
  }[F.order];
  return arr.sort(by);
}

// ---------------------------------------------------------------- период и фильтры
function quickPeriod(k, rerender = true) {
  const t = today();
  F.to = t;
  F.from = k === 'today' ? t : k === 'month' ? dateShift(t, -29) : dateShift(t, -6);
  if (k === 'all') {
    const ds = S.data.issues.map(i => day(i.created_at)).filter(Boolean).sort();
    F.from = ds[0] || t; F.to = ds.at(-1) > t ? ds.at(-1) : t;
  }
  F.drill = '';
  if (rerender) render();
}
function applyPeriod() {
  const a = $('periodFrom').value, b = $('periodTo').value, e = $('periodError');
  if (!a || !b) { e.textContent = 'Укажите начало и конец периода. Для одного дня выберите одинаковые даты.'; return; }
  if (a > b) { e.textContent = 'Начало периода не может быть позже окончания.'; return; }
  if ((new Date(b) - new Date(a)) / 86400000 > 366) { e.textContent = 'Выберите период не более 366 дней.'; return; }
  F.from = a; F.to = b; F.drill = ''; render();
}
function movePeriod(dir) { const n = Math.round((new Date(F.to) - new Date(F.from)) / 86400000) + 1; F.from = dateShift(F.from, n * dir); F.to = dateShift(F.to, n * dir); F.drill = ''; render(); }
function setF(key, value) { F[key] = value; if (key !== 'order') F.drill = ''; if (key === 'section') F.operation = ''; render(); }
function resetFilters() { Object.assign(F, { drill: '', platform: '', section: '', operation: '', type: '', version: '', priority: '', status: '', owner: '', chat: '', exec: '', query: '', initiator: '', order: 'newest' }); quickPeriod('week'); }

function uniq(values) { return [...new Set(values.filter(Boolean))]; }
function versionsList() {
  const vs = uniq(S.data.issues.map(i => i.app_version || 'Не указана'));
  return vs.sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
}

function periodPanel() {
  const o = S.data.options;
  const reviewView = S.view === 'На утверждение';
  const ops = uniq(S.data.operations.filter(x => !F.section || x.section === F.section).filter(x => !x.merged_into).map(x => x.operation)).sort();
  const statuses = uniq([...(F.type ? o.statuses[F.type] : Object.values(o.statuses).flat())]);
  const sel = (key, label, values, all, labels) => `<label>${label}<select aria-label="${label}" onchange="setF('${key}',this.value)"><option value="">${all}</option>${opts(values, F[key], labels)}</select></label>`;
  return `<section class="periodpanel" aria-label="Период и фильтры">
    ${reviewView ? '<div class="periodnote" style="margin:0 0 10px">Очередь утверждения показывает все новые обращения независимо от периода · сначала с низкой уверенностью AI</div>' : `<div class="periodrow"><label>Дата с<input id="periodFrom" type="date" value="${F.from}"></label><label>Дата по<input id="periodTo" type="date" value="${F.to}"></label><button class="primary" onclick="applyPeriod()">Показать период</button><button onclick="movePeriod(-1)" aria-label="Предыдущий период">←</button><button onclick="movePeriod(1)" aria-label="Следующий период">→</button><span class="periodnote">По дате выявления · включая обе границы</span></div>
    <div class="quickdates"><button onclick="quickPeriod('today')">Сегодня</button><button onclick="quickPeriod('week')">7 дней</button><button onclick="quickPeriod('month')">30 дней</button><button onclick="quickPeriod('all')">Все даты</button><button onclick="resetFilters()">Сбросить фильтры</button></div>
    <div id="periodError" class="perioderror" role="alert"></div>`}
    <div class="periodrow analytic-filters">
      ${sel('platform', 'Платформа', o.platforms, 'Все платформы')}
      ${sel('section', 'Раздел', o.sections, 'Все разделы')}
      ${sel('operation', 'Операция', ops, 'Все операции')}
      ${['Баги', 'Вопросы'].includes(S.view) ? '' : sel('type', 'Тип', S.view === 'Пожелания и UX' ? ['Пожелание', 'UX'] : o.types, 'Все типы')}
      ${sel('version', 'Версия', versionsList(), 'Все версии')}
      ${sel('priority', 'Приоритет', o.priorities, 'Все приоритеты')}
      ${reviewView ? '' : sel('status', 'Статус', ['На утверждении', ...statuses], 'Все статусы')}
      ${sel('owner', 'Ответственный', uniq([...o.owners, ...S.data.issues.map(i => i.owner)]), 'Все')}
      ${sel('chat', 'Чат', S.data.chats.map(c => c.chat_id), 'Все чаты', S.data.chats.map(c => c.title))}
    </div></section>`;
}

function scopeStrip(a) {
  const n = (f) => a.filter(f).length;
  const b = (k, label) => `<button class="${F.exec === k ? 'active' : ''}" onclick="setF('exec', F.exec==='${k}'?'':'${k}')">${label}</button>`;
  return `<div class="v2-strip" data-scope-summary><b>Исполнение</b>${b('late', `${n(i => i.overdue)} просрочено`)}${b('open', `${n(i => !TERMINAL.includes(i.status))} открыто`)}${b('done', `${n(i => RESOLVED.includes(i.status))} устранено / реализовано`)}${b('related', `${n(i => i.related.length)} со связанной историей`)}${n(i => i.screen_unverified) ? b('unverified', `${n(i => i.screen_unverified)} со скрином «не проверен»`) : ''}</div>`;
}

function metrics(a) {
  const cards = [
    ['Обращений в выборке', a.length, `${a.reduce((s, i) => s + i.msg_count, 0)} сообщений · ${new Set(a.flatMap(i => i.reporters.map(r => r.author_id))).size} участников`, "go('Все обращения')"],
    ['Баги', a.filter(i => i.type === 'Баг').length, (n => `${n} ${plural(n, 'критичный открытый', 'критичных открытых', 'критичных открытых')}`)(a.filter(i => i.type === 'Баг' && i.priority === 'Критичный' && !TERMINAL.includes(i.status)).length), "go('Баги')"],
    ['Пожелания и UX', a.filter(i => ['Пожелание', 'UX'].includes(i.type)).length, `Вопросов: ${a.filter(i => i.type === 'Вопрос').length}`, "go('Пожелания и UX')"],
    ['На утверждении', S.data.issues.filter(i => i.review).length, 'Ждут решения администратора', "go('На утверждение')"],
  ];
  return `<div class="metrics">${cards.map(([t, n, s, act], k) => `<div class="metric ${k === 3 && n ? 'alert' : ''}" onclick="${act}"><span class="small">${t} ↗</span><div class="num">${n}</div><div class="foot">${esc(s)}</div></div>`).join('')}</div>`;
}

// ---------------------------------------------------------------- график
function colorFor(key, all) { return PALETTE[Math.max(0, all.indexOf(key)) % PALETTE.length]; }
function chart() {
  const data = scoped(true, F.group);
  const days = []; for (let d = F.from; d <= F.to && days.length < 400; d = dateShift(d, 1)) days.push(d);
  const field = F.group === 'platform' ? (i => i.platform || 'Не указана') : F.group === 'section' ? (i => i.section) : (i => i.app_version || 'Не указана');
  const all = F.group === 'platform' ? S.data.options.platforms : F.group === 'section' ? S.data.options.sections : versionsList();
  const series = all.filter(s => data.some(i => field(i) === s));
  const bins = days.map(d => ({ date: d, total: data.filter(i => day(i.created_at) === d).length, counts: series.map(s => data.filter(i => day(i.created_at) === d && field(i) === s).length) }));
  const max = Math.max(1, ...bins.map(b => b.total));
  const active = F.group === 'platform' ? F.platform : F.group === 'section' ? F.section : F.version;
  S.series = series;
  return `<section class="panel" style="margin:20px 0"><div class="panelhead"><div><h2>Обращения по дням</h2><p class="chartsummary"><strong>${data.length}</strong> ${plural(data.length, 'обращение', 'обращения', 'обращений')} · ${showDate(F.from)} — ${showDate(F.to)}${active ? ' · ' + esc(active) : ''}</p></div><label class="small">Разбивка <select aria-label="Разбивка графика" onchange="setF('group',this.value)">${opts(['platform', 'section', 'version'], F.group, ['По платформам', 'По разделам', 'По версиям'])}</select></label></div>
  <div class="chartscroll"><div class="dailychart" style="min-width:${Math.max(360, days.length * 51)}px">${bins.map(b => `<button class="daybutton ${F.drill === b.date ? 'picked' : ''}" aria-label="${showDate(b.date)}: ${b.total}" aria-pressed="${F.drill === b.date}" onclick="selectDay('${b.date}')" title="${showDate(b.date)} · ${series.map((s, j) => s + ': ' + b.counts[j]).join('; ') || 'Нет обращений'}"><span class="daytotal">${b.total}</span><span class="daystack">${b.counts.map((n, j) => n ? `<span class="segment" style="height:${n / max * 165}px;background:${colorFor(series[j], all)}"></span>` : '').join('')}</span><span class="daydate">${showDate(b.date).slice(0, 5)}</span></button>`).join('')}</div></div>
  <div class="chartlegend">${series.map((s, k) => `<button class="${active === s ? 'active' : ''}" onclick="filterSeries(${k})"><i style="background:${colorFor(s, all)}"></i>${esc(s)} · ${data.filter(i => field(i) === s).length}</button>`).join('')}</div>
  <p class="chart-hint">Уникальные обращения по дате выявления, а не сообщения. Нажмите столбец — обращения за день; легенду — фильтр по ${F.group === 'platform' ? 'платформе' : F.group === 'section' ? 'разделу' : 'версии'}.${!data.length ? ' <b>За период и фильтры обращений нет.</b>' : ''}</p></section>`;
}
function selectDay(d) { F.drill = F.drill === d ? '' : d; render(); $('registry')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
function filterSeries(k) { const s = S.series[k]; const key = F.group === 'platform' ? 'platform' : F.group === 'section' ? 'section' : 'version'; F[key] = F[key] === s ? '' : s; F.drill = ''; render(); }

// ---------------------------------------------------------------- реестр
function badge(t) { return `<span class="badge ${t === 'Баг' ? 'bug' : t === 'UX' ? 'ux' : t === 'Вопрос' ? 'question' : 'wish'}">${esc(t)}</span>`; }
function statusBadge(i) { return `<span class="badge ${i.review ? 'review' : RESOLVED.includes(i.status) || i.status === 'Отвечено' ? 'approved' : i.status === 'Отклонено' ? 'rejected' : ''}">${i.review ? 'На утверждении' : esc(i.status)}</span>`; }
function sources(i) { return i.chats.map(c => `<span class="source">${esc(chatTitle(c))}</span>`).join(' ') || '<span class="small">—</span>'; }
function deadline(i) {
  if (i.type === 'Вопрос') return `<div class="deadline">${i.status === 'Отвечено' ? '<span class="dot">✓ Отвечено</span>' : 'Без срока'}</div>`;
  if (i.type !== 'Баг') return `<div class="deadline">${i.fix_version ? 'Релиз ' + esc(i.fix_version) : 'Релиз не задан'}${RESOLVED.includes(i.status) ? `<br><span class="dot">✓ Реализовано</span><br>${showDate(i.resolved_at)}` : ''}</div>`;
  return `<div class="deadline ${i.overdue ? 'overdue' : ''}">${i.due ? showDate(i.due) : 'Срок не задан'}${i.overdue ? '<br>● Просрочено' : ''}${RESOLVED.includes(i.status) ? `<br><span class="dot">✓ Устранено</span><br>${showDate(i.resolved_at)}${i.fix_version ? ' · ' + esc(i.fix_version) : ''}` : ''}</div>`;
}
function taskLabel(i) { return i.type === 'Баг' ? 'Задача на устранение' : i.type === 'Вопрос' ? '' : 'Задача на реализацию'; }

function rows() {
  const list = filtered();
  if (!list.length) return '<tr><td colspan="7" class="empty">Нет обращений по выбранным фильтрам.</td></tr>';
  return list.map(i => {
    const r = i.reporters[0];
    return `<tr data-id="${attr(i.issue_id)}" tabindex="0" onclick="openDetail('${attr(i.issue_id)}')" onkeydown="if(event.key==='Enter'&&event.target===this)openDetail('${attr(i.issue_id)}')">
    <td class="authorcell"><b>${esc(r?.name || '—')}</b><small>${r?.username ? '@' + esc(r.username) : 'Username не указан'}</small>${i.reporters.length > 1 ? `<small>+ ${i.reporters.length - 1} ${plural(i.reporters.length - 1, 'участник', 'участника', 'участников')}</small>` : ''}</td>
    <td class="datecell">${showDate(i.created_at)} ${showTime(i.created_at)}<br><span class="badge platformbadge">${esc(i.platform || 'Не указана')}</span></td>
    <td class="titlecell"><span class="id">${esc(i.issue_id)} · ${esc(i.section)}${i.operation ? ' / ' + esc(i.operation) : ''}</span><b>${esc(i.title)}</b><span class="small">${esc(i.app_version || 'версия не указана')} · ${esc(i.owner)} · ${i.participant_count} участн. / ${i.msg_count} сообщ.</span>${i.related.length ? `<div class="relatednotice">↶ ${i.related.map(r => `${esc(r.relation)}: ${esc(r.issue_id)}`).join(', ')}</div>` : ''}${S.view === 'На утверждение' ? `<div class="small" style="margin-top:4px">Уверенность AI: ${Math.round(i.confidence * 100)}%</div>` : ''}</td>
    <td>${badge(i.type)}<div style="margin-top:7px">${statusBadge(i)}</div><div class="small ${i.priority === 'Критичный' ? 'critical' : ''}" style="margin-top:6px">${i.priority === 'Критичный' ? '● ' : ''}${esc(i.priority)}</div></td>
    <td>${sources(i)}</td>
    <td>${deadline(i)}</td>
    <td>${i.type !== 'Вопрос' ? `<button class="taskbutton" onclick="event.stopPropagation();openTask('${attr(i.issue_id)}')">▤ ${taskLabel(i)}</button>` : '<span class="small">В Jira не уходит</span>'}${i.jira_key ? `<div class="small" style="margin-top:6px">${esc(i.jira_key)}</div>` : ''}</td></tr>`;
  }).join('');
}

function registry(home = false) {
  const list = filtered();
  return `<section class="panel" id="registry"><div class="panelhead"><h2>${home ? 'Обращения и исполнение' : S.view === 'На утверждение' ? 'Очередь утверждения' : 'Реестр обращений'}</h2><button onclick="exportCSV()">↓ CSV</button></div>
  ${F.drill ? `<div class="drillstrip"><span>Выбрано: <b>${showDate(F.drill)}</b> · ${esc(F.section || 'все разделы')} · ${esc(F.platform || 'все платформы')}</span><button onclick="F.drill='';render()">Вернуться к периоду ×</button></div>` : ''}
  <div class="authorfilter"><label for="initiatorSearch">Инициатор / участник</label><input id="initiatorSearch" value="${attr(F.initiator)}" placeholder="Имя, @username или Telegram ID" oninput="F.initiator=this.value;renderRows()"></div>
  <div class="toolbar"><input aria-label="Поиск" placeholder="⌕ Найти обращение, раздел, операцию или ID" value="${attr(F.query)}" oninput="F.query=this.value;renderRows()">
  ${S.view === 'На утверждение' ? '' : `<select aria-label="Сортировка" onchange="setF('order',this.value)">${opts(['newest', 'oldest', 'priority', 'participants'], F.order, ['Сначала новые', 'Сначала старые', 'По приоритету', 'По числу участников'])}</select>`}</div>
  <div class="tablewrap"><table><thead><tr><th>Инициатор</th><th>Выявлено</th><th>Обращение</th><th>Тип / статус</th><th>Чат-источник</th><th>Срок и исполнение</th><th>Задача для Jira</th></tr></thead><tbody id="rows">${rows()}</tbody></table></div>
  <div class="small" id="resultcount" style="padding-top:15px">${list.length} ${plural(list.length, 'обращение', 'обращения', 'обращений')} · нажмите строку для деталей</div></section>`;
}
function renderRows() { $('rows').innerHTML = rows(); const n = filtered().length; $('resultcount').textContent = `${n} ${plural(n, 'обращение', 'обращения', 'обращений')} · нажмите строку для деталей`; }

function registryPage() {
  const a = scoped();
  const reviewView = S.view === 'На утверждение';
  const reviewCount = S.data.issues.filter(i => i.review).length;
  return `${periodPanel()}${reviewView ? '' : scopeStrip(a)}${S.view === 'Обзор' ? metrics(a) : ''}${S.view === 'Обзор' && reviewCount ? `<div class="banner" style="margin-top:20px"><div><b>◈ ${reviewCount} ${plural(reviewCount, 'обращение ждёт', 'обращения ждут', 'обращений ждут')} решения</b><span style="margin-left:12px;color:#788064">Проверьте классификацию AI и срок</span></div><button onclick="go('На утверждение')">Проверить →</button></div>` : ''}${reviewView ? '' : chart()}${registry(S.view === 'Обзор')}`;
}

function exportCSV() {
  const head = ['ID', 'Дата выявления', 'Инициатор', 'Все участники', 'Платформа', 'Устройство', 'Версия', 'Раздел', 'Операция', 'Обращение', 'Тип', 'Приоритет', 'Статус', 'Ответственный', 'Срок', 'Дата устранения', 'Версия исправления', 'Результат', 'Чаты', 'Участников', 'Сообщений', 'Связанные', 'Jira'];
  const data = [head, ...filtered().map(i => [i.issue_id, showDT(i.created_at), i.reporters[0]?.name || '', i.reporters.map(r => r.name + (r.username ? ' @' + r.username : '')).join('; '), i.platform, i.device, i.app_version, i.section, i.operation, i.title, i.type, i.priority, i.review ? 'На утверждении' : i.status, i.owner, i.due, i.resolved_at, i.fix_version, i.resolution, i.chats.map(chatTitle).join('; '), i.participant_count, i.msg_count, i.related.map(r => `${r.relation}: ${r.issue_id}`).join('; '), i.jira_key])];
  const csv = '﻿' + data.map(r => r.map(v => '"' + String(v ?? '').replace(/^[=+@\-\t\r]/, "'$&").replace(/"/g, '""') + '"').join(',')).join('\r\n');
  download(csv, `focus-pulse-${F.drill || F.from}-${F.drill || F.to}.csv`, 'text/csv;charset=utf-8');
  toast('Реестр выгружен с учётом фильтров');
}
function download(text, name, type) { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); }

// ---------------------------------------------------------------- карточка
async function openDetail(id) {
  try {
    S.detail = await api('GET', `/api/issues/${encodeURIComponent(id)}`);
    S.detailId = id;
    renderDetail();
    $('modal').classList.add('open');
    $('detail').scrollTop = 0;
    document.querySelector('#detail .close')?.focus();
  } catch (e) { toast(e.message); }
}
function closeDetail() { $('modal').classList.remove('open'); S.detail = null; }

function screenThumbs(d) {
  const out = [];
  for (const m of d.messages) {
    for (const s of m.screens) {
      if (s.status === 'ok') out.push(`<button class="attachment-thumb" onclick="viewImage('${attr(m.msg_id)}',${s.idx})"><img loading="lazy" src="/api/image/${encodeURIComponent(m.msg_id)}/${s.idx}" alt="Скриншот к ${attr(d.issue.issue_id)}"><small>⤢ Обезличенный скрин${s.masked ? ' · скрыто: ' + esc(s.masked) : ''}<br>${esc(m.author)} · ${showTime(m.sent_at)}</small></button>`);
      else out.push(`<div class="screen-ph"><b>${s.status === 'pending' ? 'Скрин обрабатывается' : 'Скрин не проверен'}</b><p>${esc(s.reason || '')}</p>${m.link ? `<a href="${attr(m.link)}" target="_blank" rel="noopener">Открыть оригинал в чате ↗</a>` : ''}</div>`);
    }
  }
  return out.length ? `<div class="attachment-grid">${out.join('')}</div>` : '<p class="small">Скриншотов нет.</p>';
}
function viewImage(msgId, idx) {
  $('imagecontent').innerHTML = `<button class="close" onclick="closeModal('imagemodal')" aria-label="Закрыть изображение">✕</button><img src="/api/image/${encodeURIComponent(msgId)}/${idx}" alt="Обезличенный скриншот"><div class="small">Обезличенный скриншот: персональные и платёжные данные закрашены до сохранения и анализа.</div>`;
  $('imagemodal').hidden = false;
}

function authorPanel(d) {
  const i = d.issue;
  if (!i.reporters.length) return '';
  return `<section class="authorpanel"><h3>Инициатор и участники</h3>${i.reporters.map(r => `<div class="authorline"><span class="avatar">${esc(initials(r.name))}</span><div class="authordetails"><b>${esc(r.name)}</b> ${r.role === 'initiator' ? '<span class="badge approved">Инициатор</span>' : `<span class="badge">${r.state === 'Контекст' ? 'Уточнение' : 'Повторный сигнал'}</span>`}<div>${r.username ? '@' + esc(r.username) : 'Username не указан'}${r.messages > 1 ? ` · ${r.messages} сообщ.` : ''}</div><div class="authormeta">ID ${esc(r.author_id)} · ${esc(r.chat)}<br>${showDT(r.sent_at)}</div>${r.link ? `<a class="authorsource" href="${attr(r.link)}" target="_blank" rel="noopener">Исходное сообщение ↗</a>` : ''}</div></div>`).join('')}<div class="authorfoot">Ссылка открывает сообщение в Telegram у участников чата.</div></section>`;
}

function messagesBlock(d) {
  return d.messages.map(m => `<div class="quote"><span class="small">${esc(m.author)} · ${showDT(m.sent_at)} · ${esc(m.chat)}${m.edited ? ' · изменено' : ''}</span><p>${esc(m.text || (m.kind === 'photo' ? '(скриншот без подписи)' : '(без текста)'))}</p><span class="small">${m.state === 'Контекст' ? 'Уточнение' : m.reason.startsWith('Старая версия') ? esc(m.reason) : 'Сигнал'}${m.app_version ? ' · версия ' + esc(m.app_version) : ''}${m.mask_summary ? ` · <span class="maskchip" title="Скрыто при обезличивании">скрыто: ${esc(m.mask_summary)}</span>` : ''}${m.link ? ` · <a href="${attr(m.link)}" target="_blank" rel="noopener">в чате ↗</a>` : ''}</span></div>`).join('') || '<div class="quote small">Сообщений нет.</div>';
}

function relatedHtml(d) {
  const rel = d.issue.related;
  if (!rel.length) return '';
  return `<div class="relatedbox"><b>↶ Связанные обращения</b><p class="small">Закрытые обращения не переоткрываются: повтор на версии ≥ исправления — регрессия, на более старой — сигнал «старая версия».</p>${rel.map(r => `<div class="relatedrow"><div><button class="relatedtitle" onclick="openDetail('${attr(r.issue_id)}')">${esc(r.issue_id)} · ${esc(r.title)}</button><p>${esc(r.status)} · выявлено ${showDate(r.created_at)}${r.resolved_at ? ' · устранено ' + showDate(r.resolved_at) : ''}</p>${r.fix_version ? `<p>Версия исправления: <b>${esc(r.fix_version)}</b></p>` : ''}${r.resolution ? `<p>${esc(r.resolution)}</p>` : ''}</div><span class="badge ${r.relation === 'Регрессия' ? 'bug' : r.relation === 'Объединено' ? '' : 'review'}">${esc(r.relation)}${r.direction === 'in' ? ' ←' : ''}</span></div>`).join('')}</div>`;
}

function aiBlock(i) {
  const changed = [];
  if (i.type_ai && i.type_ai !== i.type) changed.push(`тип: AI «${i.type_ai}» → «${i.type}»`);
  if (i.priority_ai && i.priority_ai !== i.priority) changed.push(`приоритет: AI «${i.priority_ai}» → «${i.priority}»`);
  const steps = (i.steps || '').split('\n').filter(Boolean);
  return `<div class="quote"><span class="ailabel">✧ АНАЛИЗ AI${i.confidence ? ` · уверенность ${Math.round(i.confidence * 100)}%` : ''}</span><p>${esc(i.reason_ai || 'Обращение создано администратором.')}</p>
  ${i.summary ? `<p><b>Суть.</b> ${esc(i.summary)}</p>` : ''}${steps.length ? `<p><b>Шаги.</b></p><ol class="steps">${steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}${i.actual ? `<p><b>Факт.</b> ${esc(i.actual)}</p>` : ''}${i.expected ? `<p><b>Ожидание.</b> ${esc(i.expected)}</p>` : ''}${i.error_text ? `<p><b>Текст ошибки.</b> «${esc(i.error_text)}»</p>` : ''}
  ${changed.length ? `<div class="reason">Администратор изменил ${changed.join('; ')}.</div>` : i.review ? '<div class="reason">Проверьте тип, раздел и платформу. Нехватка данных — повод для статуса «Уточнение», а не для отклонения.</div>' : ''}</div>`;
}

function field(label, html, wide = false) { return `<label class="field${wide ? ' wide' : ''}">${label}${html}</label>`; }

function detailForm(d) {
  const i = d.issue, o = S.data.options, admin = isAdmin();
  const dis = admin ? '' : 'disabled';
  const ops = uniq(S.data.operations.filter(x => x.section === i.section && !x.merged_into).map(x => x.operation));
  if (i.operation && !ops.includes(i.operation)) ops.unshift(i.operation);
  const statuses = o.statuses[i.type] || [];
  const isBug = i.type === 'Баг';
  const due = i.due || (admin && i.review && isBug ? d.suggested_due : '');
  return `<div class="formgrid">
    ${field('Тип обращения', `<select id="dtype" ${dis} onchange="onTypeChange()">${opts(o.types, i.type)}</select>`)}
    ${field('Приоритет', `<select id="dpriority" ${dis} onchange="onPriorityChange()">${opts(o.priorities, i.priority)}</select>`)}
    ${field('Статус', `<select id="dstatus" ${dis} onchange="syncResolution()">${opts(statuses, i.status)}</select>`)}
    ${field('Ответственный', `<select id="downer" ${dis}>${opts(uniq([...o.owners, i.owner]), i.owner)}</select>`)}
    ${field('Раздел', `<select id="dsection" ${dis} onchange="onSectionChange()">${opts(o.sections, i.section)}</select>`)}
    ${field('Операция', `<select id="doperation" ${dis}>${opts(ops.length ? ops : ['Не определена'], i.operation || 'Не определена')}</select>`)}
    ${field('Платформа', `<select id="dplatform" ${dis}>${opts(o.platforms, i.platform)}</select>`)}
    ${field('Версия приложения', `<input id="dversion" ${dis} value="${attr(i.app_version)}" placeholder="4.2.0">`)}
    ${field('Устройство', `<input id="ddevice" ${dis} value="${attr(i.device)}" placeholder="Samsung A54">`)}
    ${field(isBug ? 'Срок устранения' : 'Целевой релиз', isBug ? `<input id="ddue" type="date" ${dis} value="${attr(due)}">` : `<input id="dfixplan" ${dis} value="${attr(i.fix_version)}" placeholder="4.3.0">`)}
  </div>
  ${isBug ? `<div class="sugg small" id="duehint">${d.suggested_due ? `По приоритету: ${showDate(d.suggested_due)}${admin ? ` <button class="linkbtn" onclick="$('ddue').value='${d.suggested_due}'">подставить</button>` : ''}` : i.priority === 'Низкий' ? 'Низкий приоритет — без срока' : 'Средний приоритет — срок по дате ближайшего релиза (заведите релиз в настройках)'}${i.due_set_by ? ` · срок поставил: ${esc(i.due_set_by)}` : ''}</div>${deadline(i)}` : ''}
  ${field('Заголовок', `<input id="dtitle" ${dis} value="${attr(i.title)}" maxlength="120">`, true)}
  <div class="resolved-controls" id="resolvedbox" ${RESOLVED.includes(i.status) ? '' : 'hidden'}>
    <div class="formgrid">${field('Дата устранения', `<input type="date" id="dresolvedAt" ${dis} value="${attr(i.resolved_at)}">`)}${field('Версия исправления', `<input id="dfix" ${dis} value="${attr(i.fix_version)}" placeholder="4.2.1">`)}</div>
    ${field('Результат / что исправлено', `<textarea id="dresolution" rows="2" ${dis} placeholder="Решение и результат проверки">${esc(i.resolution)}</textarea>`, true)}
  </div>
  <div id="rejectbox" ${i.status === 'Отклонено' ? '' : 'hidden'}>${field('Причина отклонения', `<input id="dreject" ${dis} list="rejectlist" value="${attr(i.reject_reason)}"><datalist id="rejectlist">${o.reject_reasons.map(r => `<option value="${attr(r)}">`).join('')}</datalist>`, true)}</div>
  <div id="answerbox" ${i.status === 'Отвечено' ? '' : 'hidden'}>${field('Ответ участникам', `<textarea id="danswer" rows="2" ${dis}>${esc(i.resolution)}</textarea>`, true)}</div>
  ${field('Комментарий администратора', `<textarea id="dcomment" rows="2" ${dis} placeholder="Основание решения">${esc(i.comment)}</textarea>`, true)}
  <div id="detailerror" class="validation" role="alert"></div>
  ${i.accepted_by ? `<p class="small">Принял: ${esc(i.accepted_by)} · ${showDT(i.accepted_at)}</p>` : ''}
  ${i.type !== 'Вопрос' ? `<button class="taskbutton" onclick="openTask('${attr(i.issue_id)}')">▤ ${taskLabel(i)}</button><p class="small">Текст для ручного переноса в Jira${i.jira_key ? ' · ' + esc(i.jira_key) : ''}.</p>` : ''}`;
}

function renderDetail() {
  const d = S.detail, i = d.issue, admin = isAdmin();
  const hist = d.audit.map(a => `<div class="history"><b>${showDT(a.ts)}</b> · ${esc(a.actor)} · ${esc(a.field)}${a.old || a.new ? `: ${esc(a.old || '—')} → ${esc(a.new || '—')}` : ''}</div>`).join('');
  $('detail').innerHTML = `<button class="close" onclick="closeDetail()" aria-label="Закрыть карточку">✕</button>
  <span class="small">${esc(i.issue_id)} · ${esc(i.section)}${i.operation ? ' / ' + esc(i.operation) : ''} · версия ${esc(i.app_version || 'не указана')}</span>
  <h2>${esc(i.title)}</h2>
  <p class="small">Выявлено ${showDT(i.created_at)} · Платформа: <b>${esc(i.platform)}</b>${i.device ? ' · ' + esc(i.device) : ''} · ${i.participant_count} ${plural(i.participant_count, 'участник', 'участника', 'участников')} · ${i.msg_count} ${plural(i.msg_count, 'сообщение', 'сообщения', 'сообщений')}</p>
  ${badge(i.type)} ${statusBadge(i)} <span class="badge ${i.priority === 'Критичный' ? 'bug' : ''}">${esc(i.priority)}</span>${i.merged_into ? ` <span class="badge">Объединено с ${esc(i.merged_into)}</span>` : ''}
  <div style="margin-top:14px">${sources(i)}</div>
  <div class="detailcols"><div>${authorPanel(d)}<h3 class="subhead">Сообщения</h3>${messagesBlock(d)}<h3 class="subhead">Скриншоты</h3>${screenThumbs(d)}${relatedHtml(d)}</div>
  <div>${aiBlock(i)}${detailForm(d)}${hist ? `<h3 class="subhead">История изменений</h3>${hist}` : ''}</div></div>
  ${admin && !i.merged_into ? `<div class="detailfooter">${i.review ? `<button class="primary" onclick="saveDetail(true)">Принять и распределить</button><button onclick="saveDetail(false)">Сохранить без принятия</button><button onclick="quickReject()">Отклонить…</button>` : '<button class="primary" onclick="saveDetail(false)">Сохранить</button>'}${!TERMINAL.includes(i.status) && d.merge_targets.length ? '<button onclick="showMerge()">Объединить…</button>' : ''}</div><div id="mergebox"></div>` : admin ? '' : '<p class="small" style="margin-top:20px">Изменять обращения могут только администраторы. <button class="linkbtn" onclick="openLogin()">Войти</button></p>'}`;
}

function onTypeChange() {
  const t = $('dtype').value, cur = $('dstatus').value, list = S.data.options.statuses[t] || [];
  $('dstatus').innerHTML = opts(list, list.includes(cur) ? cur : 'Новое');
  syncResolution();
}
function onSectionChange() {
  const sec = $('dsection').value;
  const ops = uniq(S.data.operations.filter(x => x.section === sec && !x.merged_into).map(x => x.operation));
  $('doperation').innerHTML = opts(ops.length ? ops : ['Не определена'], ops[0] || 'Не определена');
}
function onPriorityChange() { if ($('duehint')) $('duehint').textContent = 'Приоритет изменён — срок по умолчанию пересчитается после сохранения.'; }
function syncResolution() {
  const st = $('dstatus').value;
  $('resolvedbox').hidden = !RESOLVED.includes(st);
  $('rejectbox').hidden = st !== 'Отклонено';
  $('answerbox').hidden = st !== 'Отвечено';
  if (RESOLVED.includes(st) && !$('dresolvedAt').value) $('dresolvedAt').value = today();
}
function readForm() {
  const type = $('dtype').value, status = $('dstatus').value;
  const f = {
    type, priority: $('dpriority').value, status, owner: $('downer').value, section: $('dsection').value,
    operation: $('doperation').value, platform: $('dplatform').value, app_version: $('dversion').value.trim(),
    device: $('ddevice').value.trim(), title: $('dtitle').value.trim(), comment: $('dcomment').value,
    reject_reason: $('dreject').value.trim(),
  };
  if ($('ddue')) f.due = $('ddue').value;
  if (RESOLVED.includes(status)) { f.resolved_at = $('dresolvedAt').value; f.fix_version = $('dfix').value.trim(); f.resolution = $('dresolution').value; }
  else if (status === 'Отвечено') { f.resolution = $('danswer').value; }
  else if ($('dfixplan')) { f.fix_version = $('dfixplan').value.trim(); }
  return f;
}
async function saveDetail(accept) {
  const box = $('detailerror'); box.textContent = '';
  try {
    const r = await api('POST', `/api/issues/${encodeURIComponent(S.detailId)}/save`, { fields: readForm(), accept });
    S.detail = r; renderDetail(); await load(true);
    toast(accept ? 'Обращение принято и распределено' : 'Изменения сохранены');
  } catch (e) { box.innerHTML = (e.errors || [e.message]).map(esc).join('<br>'); box.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
}
function quickReject() {
  $('dstatus').value = 'Отклонено'; syncResolution(); $('dreject').focus();
  $('detailerror').textContent = 'Укажите причину и нажмите «Сохранить без принятия».';
}
function showMerge() {
  const d = S.detail;
  $('mergebox').innerHTML = `<div class="quote"><b>Объединить с открытым обращением</b><p class="small">Эта карточка станет частью выбранной. Сообщения, авторы и чаты сохранятся, счётчики пересчитаются. Закрытые обращения остаются историей.</p><select id="mergeid" style="width:100%">${d.merge_targets.map(t => `<option value="${attr(t.issue_id)}">${esc(t.issue_id)} · ${esc(t.title)}</option>`).join('')}</select><button style="margin-top:12px" onclick="doMerge()">Подтвердить объединение</button></div>`;
  $('mergebox').scrollIntoView({ behavior: 'smooth' });
}
async function doMerge() {
  try {
    const r = await api('POST', `/api/issues/${encodeURIComponent(S.detailId)}/merge`, { target: $('mergeid').value });
    S.detail = r; S.detailId = r.issue.issue_id; renderDetail(); await load(true); toast('Обращения объединены');
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- задача Jira
async function openTask(id) {
  try {
    S.task = await api('GET', `/api/issues/${encodeURIComponent(id)}/jira`); S.taskId = id;
    const t = S.task, admin = isAdmin();
    $('taskcontent').innerHTML = `<button class="close" onclick="closeModal('taskmodal')" aria-label="Закрыть">✕</button><span class="ailabel">${esc(id)} · РУЧНОЙ ПЕРЕНОС В JIRA</span><h2>${t.type === 'Баг' ? 'Задача на устранение' : 'Задача на реализацию'}</h2>
    <div class="taskhint">Текст собран по шаблону из полей карточки. Скопируйте его в Jira и приложите скриншоты из карточки вручную.${t.stale ? '<br><b>Карточка изменилась после правки текста. Сформируйте заново, чтобы включить новые данные.</b>' : ''}</div>
    <textarea class="tasktext" id="tasktext" aria-label="Текст задачи для Jira" ${admin ? '' : 'readonly'}>${esc(t.text)}</textarea>
    <div class="mini-actions" style="margin-top:15px"><button class="primary" onclick="copyTask()">Копировать текст</button><button onclick="download($('tasktext').value,'${attr(id)}-Jira.txt','text/plain;charset=utf-8')">Скачать .txt</button>${admin ? '<button onclick="saveTask(false)">Сохранить правки</button><button onclick="saveTask(true)">Сформировать заново</button>' : ''}</div>
    ${admin ? `<label class="field">Ключ задачи Jira после переноса<input id="jiraKey" value="${attr(t.jira_key)}" placeholder="MOB-123"></label><button onclick="saveJiraKey()">Сохранить ключ</button>` : (t.jira_key ? `<p>Задача в Jira: <b>${esc(t.jira_key)}</b></p>` : '')}
    <p class="small" id="copyhint"></p>`;
    $('taskmodal').hidden = false;
  } catch (e) { toast(e.message); }
}
async function copyTask() {
  const t = $('tasktext');
  try { await navigator.clipboard.writeText(t.value); $('copyhint').textContent = 'Скопировано. Вставьте текст в Jira.'; toast('Текст задачи скопирован'); }
  catch { t.focus(); t.select(); $('copyhint').textContent = 'Копирование недоступно — текст выделен, нажмите Ctrl+C (⌘C).'; }
}
async function saveTask(regenerate) {
  try { await api('POST', `/api/issues/${encodeURIComponent(S.taskId)}/jira`, { jira_text: $('tasktext').value, regenerate }); await openTask(S.taskId); toast(regenerate ? 'Текст сформирован заново' : 'Правки сохранены'); }
  catch (e) { toast(e.message); }
}
async function saveJiraKey() {
  try { await api('POST', `/api/issues/${encodeURIComponent(S.taskId)}/jira`, { jira_key: $('jiraKey').value }); await load(true); toast('Ключ Jira сохранён'); }
  catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- фильтрация чата
function inboxPage() {
  const msgs = S.data.messages.filter(m => !S.inboxChat || m.chat_id === S.inboxChat);
  const counts = Object.fromEntries(MSG_STATES.map(s => [s, msgs.filter(m => m.state === s).length]));
  const list = msgs.filter(m => S.inboxState === 'Все' || m.state === S.inboxState).slice(0, 300);
  const open = S.data.issues.filter(i => !i.merged_into && !TERMINAL.includes(i.status));
  const admin = isAdmin();
  return `<div class="banner"><span><b>Ничего не удаляется.</b> Приветствия, реакции и организационные вопросы исключаются из реестра правилами без AI. Уточнения сохраняются в карточке, неясные сообщения проверяет администратор.</span></div>
  <section class="panel"><div class="panelhead"><h2>Сообщения из чатов</h2><select aria-label="Чат" onchange="S.inboxChat=this.value;render()"><option value="">Все чаты</option>${opts(S.data.chats.map(c => c.chat_id), S.inboxChat, S.data.chats.map(c => c.title))}</select></div>
  <div class="chipbar">${['Все', ...MSG_STATES].map(s => `<button class="${S.inboxState === s ? 'active' : ''}" onclick="S.inboxState='${s}';render()">${s}<span class="pillcount">${s === 'Все' ? msgs.length : counts[s]}</span></button>`).join('')}</div>
  ${list.map(m => `<div class="incoming"><div><span class="source">${esc(m.chat)}</span><span class="small"> · ${showDT(m.sent_at)}</span><div class="authormeta">Автор: ${esc(m.author)} · ID ${esc(m.author_id)}${m.app_version ? ' · версия ' + esc(m.app_version) : ''}</div><p>${esc(m.text || (m.kind === 'voice' ? '🎤 Голосовое сообщение' : m.kind === 'photo' ? '(скриншот)' : '(без текста)'))}</p>${m.screens.length ? `<div class="small">${m.screens.map(s => s.status === 'ok' ? `<button class="linkbtn" onclick="viewImage('${attr(m.msg_id)}',${s.idx})">🖼 скрин</button>` : `🖼 ${s.status === 'pending' ? 'обрабатывается' : 'не проверен'}`).join(' · ')}</div>` : ''}${m.mask_summary ? `<span class="maskchip">скрыто: ${esc(m.mask_summary)}</span>` : ''}${m.link ? ` <a class="small" href="${attr(m.link)}" target="_blank" rel="noopener">в чате ↗</a>` : ''}${m.issue_id ? `<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">${[m.issue_id, ...(m.also || [])].map(id => `<button onclick="openDetail('${attr(id)}')">Открыть ${esc(id)} →</button>`).join('')}</div>` : ''}</div>
  <div><span class="badge ${m.state === 'На проверке' ? 'review' : m.state === 'Учтено' ? 'approved' : ''}">${esc(m.state)}</span>${m.by ? `<span class="small"> · ${esc(m.by)}</span>` : ''}<p class="reasonline">${esc(m.reason)}</p>${admin ? inboxActions(m, open) : ''}</div></div>`).join('') || '<div class="empty">В этой категории нет сообщений.</div>'}
  ${list.length === 300 ? '<p class="small">Показаны последние 300 сообщений.</p>' : ''}</section>`;
}
function inboxActions(m, open) {
  const id = attr(m.msg_id);
  const pick = `<select id="pick-${id}"><option value="">Выберите обращение</option>${open.map(i => `<option value="${attr(i.issue_id)}">${esc(i.issue_id)} · ${esc(i.title)}</option>`).join('')}</select>`;
  if (m.state === 'Отфильтровано') return `<div class="mini-actions"><button onclick="msgAction('${id}','reanalyze')">Вернуть в анализ</button><button onclick="msgAction('${id}','review')">На проверку</button></div>`;
  if (m.state === 'На проверке') return `<div class="mini-actions"><button onclick="msgAction('${id}','create')">Создать обращение</button><button onclick="msgAction('${id}','reanalyze')">Повторить анализ</button></div><label class="field">Добавить к обращению${pick}</label><div class="mini-actions"><button onclick="msgAction('${id}','context')">Как уточнение</button><button onclick="msgAction('${id}','attach')">Как сигнал</button></div><label class="field">Исключить с причиной<input id="why-${id}" placeholder="Например: обсуждение не по приложению"></label><button onclick="msgAction('${id}','exclude')">Исключить</button>`;
  if (m.state === 'Учтено' || m.state === 'Контекст') return `<div class="mini-actions"><button onclick="msgAction('${id}','review')">Отвязать и проверить</button></div>`;
  return '';
}
async function msgAction(msgId, action) {
  const body = { action };
  if (['context', 'attach'].includes(action)) { body.issue_id = $('pick-' + msgId)?.value || ''; if (!body.issue_id) { toast('Выберите обращение'); return; } }
  if (action === 'exclude') { body.reason = $('why-' + msgId)?.value || ''; if (!body.reason.trim()) { toast('Укажите причину'); return; } }
  try {
    const r = await api('POST', `/api/messages/${encodeURIComponent(msgId)}/action`, body);
    await load(true);
    toast({ reanalyze: 'Сообщение вернётся в анализ при следующем запуске', review: 'Сообщение на проверке', exclude: 'Сообщение исключено', context: 'Добавлено как уточнение', attach: 'Добавлено как сигнал', create: 'Обращение создано' }[action]);
    if (action === 'create' && r.issue_id) openDetail(r.issue_id);
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- источники
function sourcesPage() {
  const admin = isAdmin();
  if (!S.data.chats.length) return `<div class="banner"><span>Чатов пока нет. Администратор добавляет бота в действующий чат фокус-группы — чат подключается автоматически.</span></div>`;
  return `<div class="banner"><span>Чтобы подключить чат, администратор добавляет в него бота — чат подключается сам. Если бота добавил кто-то другой, отправьте в чате <code>/connect</code>. У бота должен быть отключён privacy mode в BotFather (или бот — админ чата). Участникам ничего делать не нужно: платформу, телефон и версию бот берёт из их сообщений и скриншотов.</span></div>
  <div class="grid3">${S.data.chats.map(c => {
    const n = S.data.issues.filter(i => i.chats.includes(c.chat_id) && !i.merged_into).length;
    return `<section class="panel chatcard"><span class="chaticon">↗</span><h2>${esc(c.title)}</h2><span class="badge ${c.active === '1' ? 'approved' : ''}">${c.active === '1' ? 'Собираем сообщения' : 'Отключён'}</span><p>Telegram · ${c.participants} ${plural(c.participants, 'автор', 'автора', 'авторов')} · ${c.messages} сообщ.<br>Последнее сообщение: ${c.last_message_at ? showDT(c.last_message_at) : '—'}<br>Платформа по умолчанию: ${esc(c.default_platform || 'не задана')}</p>
    ${admin ? `<label class="field">Платформа по умолчанию<select onchange="chatUpdate('${attr(c.chat_id)}',{default_platform:this.value})">${opts(['', 'iOS', 'Android'], c.default_platform, ['Не задана', 'iOS', 'Android'])}</select></label><button onclick="chatUpdate('${attr(c.chat_id)}',{active:${c.active === '1' ? 'false' : 'true'}})">${c.active === '1' ? 'Отключить сбор' : 'Включить сбор'}</button>` : ''}
    <hr style="border:0;border-top:1px solid var(--line)"><p><b style="color:var(--ink);font-size:24px">${n}</b> ${plural(n, 'обращение', 'обращения', 'обращений')}</p><button onclick="S.view='Все обращения';F.chat='${attr(c.chat_id)}';quickPeriod('all')">Посмотреть обращения →</button></section>`;
  }).join('')}</div>`;
}
async function chatUpdate(id, changes) { try { await api('POST', `/api/chats/${encodeURIComponent(id)}`, changes); await load(true); toast('Чат обновлён'); } catch (e) { toast(e.message); } }

// ---------------------------------------------------------------- журнал
function logPage() {
  const q = S.auditQuery.toLowerCase();
  const audit = S.data.audit.filter(a => !q || `${a.actor} ${a.object_id} ${a.field} ${a.old} ${a.new}`.toLowerCase().includes(q)).slice(0, 300);
  return `<section class="panel"><div class="panelhead"><h2>Запуски анализа</h2><span class="small">Вызовов Claude сегодня: ${S.data.meta.calls_today} из ${S.data.meta.daily_limit}</span></div>
  <div class="tablewrap"><table><thead><tr><th>Начало</th><th>Запуск</th><th>Сообщений</th><th>Эпизодов</th><th>Новых</th><th>Сигналов</th><th>Отфильтровано</th><th>На проверке</th><th>Ошибок</th></tr></thead><tbody>${S.data.runs.map(r => `<tr><td>${showDT(r.started_at)}</td><td>${esc({ schedule: 'по расписанию', dashboard: 'кнопка на дашборде', telegram: 'команда /run' }[r.trigger] || r.trigger)}</td><td>${esc(r.messages)}</td><td>${esc(r.episodes)}</td><td>${esc(r.new_issues)}</td><td>${esc(r.signals)}</td><td>${esc(r.filtered)}</td><td>${esc(r.review)}</td><td class="${+r.errors ? 'critical' : ''}">${esc(r.errors)}</td></tr>`).join('') || '<tr><td colspan="9" class="empty">Запусков ещё не было.</td></tr>'}</tbody></table></div></section>
  <section class="panel" style="margin-top:20px"><div class="panelhead"><h2>Журнал изменений</h2><input style="min-width:260px" placeholder="⌕ Кто, FP-номер, поле" value="${attr(S.auditQuery)}" oninput="S.auditQuery=this.value;renderAudit()"></div><div class="tablewrap"><table><thead><tr><th>Дата и время</th><th>Кто</th><th>Объект</th><th>Поле</th><th>Было</th><th>Стало</th></tr></thead><tbody id="auditrows">${auditRows(audit)}</tbody></table></div></section>`;
}
function auditRows(list) { return list.map(a => `<tr><td>${showDT(a.ts)}</td><td>${esc(a.actor)}</td><td>${/^FP-/.test(a.object_id) ? `<button class="linkbtn" onclick="openDetail('${attr(a.object_id)}')">${esc(a.object_id)}</button>` : esc(a.object_id)}</td><td>${esc(a.field)}</td><td class="titlecell">${esc(a.old)}</td><td class="titlecell">${esc(a.new)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">Записей нет.</td></tr>'; }
function renderAudit() { const q = S.auditQuery.toLowerCase(); $('auditrows').innerHTML = auditRows(S.data.audit.filter(a => !q || `${a.actor} ${a.object_id} ${a.field} ${a.old} ${a.new}`.toLowerCase().includes(q)).slice(0, 300)); }

// ---------------------------------------------------------------- настройки
function toggle(key, label, hint) {
  const on = S.data.settings[key] === '1';
  return `<div class="settingrow"><div><b>${label}</b><p>${hint}</p></div>${isAdmin() ? `<label class="checklabel"><input type="checkbox" ${on ? 'checked' : ''} onchange="saveSetting('${key}',this.checked)" aria-label="${label}"> ${on ? 'Включено' : 'Выключено'}</label>` : `<span class="badge ${on ? 'approved' : ''}">${on ? 'Включено' : 'Выключено'}</span>`}</div>`;
}
function settingsPage() {
  const m = S.data.meta, admin = isAdmin(), o = S.data.options;
  const opsRows = [...S.data.operations].sort((a, b) => (a.status === 'Предложена' ? 0 : 1) - (b.status === 'Предложена' ? 0 : 1) || a.section.localeCompare(b.section) || a.operation.localeCompare(b.operation));
  const h = m.health || {};
  S.opsRows = opsRows;
  return `<section class="panel settings"><h2>Анализ и фильтрация</h2>
  ${toggle('auto_analysis', `Автоанализ каждые ${m.interval_min} минут`, `Эпизод разбирается, когда в нём нет новых сообщений ${m.pause_min} минут. Кнопка «Запустить анализ» разбирает всё сразу.`)}
  ${toggle('filter_greetings', 'Фильтровать приветствия', 'Исключаются из реестра без AI, сохраняются в «Фильтрации чата».')}
  ${toggle('filter_organization', 'Фильтровать организационную переписку', 'Созвоны, встречи, время — без признаков проблемы.')}
  ${toggle('filter_reactions', 'Фильтровать короткие реакции', '«Спасибо», «+», эмодзи, стикеры.')}
  <p class="small">Сообщения со скриншотами и с признаками проблемы никогда не отсекаются правилами. Решения администратора повторный анализ не меняет.</p></section>

  <section class="panel settings" style="margin-top:20px"><h2>Справочник операций</h2><p class="small">Новые операции AI предлагает со статусом «Предложена». Утвердите, объедините с существующей или отклоните.</p>
  <div class="tablewrap"><table><thead><tr><th>Раздел</th><th>Операция</th><th>Статус</th><th>Обращений</th>${admin ? '<th>Действия</th>' : ''}</tr></thead><tbody>${opsRows.map((op, k) => {
    const n = S.data.issues.filter(i => i.section === op.section && i.operation === op.operation).length;
    const same = S.data.operations.filter(x => x.section === op.section && x.operation !== op.operation && !x.merged_into && x.status === 'Утверждена');
    return `<tr><td>${esc(op.section)}</td><td class="titlecell"><b>${esc(op.operation)}</b>${op.merged_into ? `<span class="small">→ ${esc(op.merged_into)}</span>` : ''}</td><td><span class="badge ${op.status === 'Предложена' ? 'review' : op.status === 'Утверждена' ? 'approved' : ''}">${esc(op.status)}</span></td><td>${n}</td>${admin ? `<td>${op.status === 'Предложена' ? `<div class="mini-actions"><button onclick="opAction('approve',${k})">Утвердить</button><button onclick="opAction('reject',${k})">Отклонить</button>${same.length ? `<select id="mt-${k}" aria-label="Объединить с">${opts(same.map(x => x.operation))}</select><button onclick="opMerge(${k})">Объединить</button>` : ''}</div>` : ''}</td>` : ''}</tr>`;
  }).join('')}</tbody></table></div>
  ${admin ? `<div class="periodrow" style="margin-top:14px"><label>Раздел<select id="newopSection">${opts(o.sections)}</select></label><label>Операция<input id="newopName" placeholder="Например: Оплата штрафов"></label><button onclick="opAdd()">Добавить операцию</button></div>` : ''}</section>

  <section class="panel settings" style="margin-top:20px"><h2>Релизы</h2><p class="small">Нужны для срока «до ближайшего релиза» и для логики регрессий. Версию участника бот берёт из его сообщений; можно также написать в личку тем, кто запускал бота.</p>
  ${S.data.releases.map(r => `<div class="settingrow"><div><b>${esc(r.version)}</b> · ${esc(r.platform)}<p>Выход: ${showDate(r.released_at)} · завёл ${esc(r.created_by)}</p></div></div>`).join('') || '<p class="small">Релизов пока нет.</p>'}
  ${admin ? `<div class="periodrow" style="margin-top:14px"><label>Версия<input id="relVersion" placeholder="4.2.1"></label><label>Платформа<select id="relPlatform">${opts(['iOS и Android', 'iOS', 'Android'])}</select></label><label>Дата выхода<input id="relDate" type="date" value="${today()}"></label><label class="checklabel"><input type="checkbox" id="relNotify"> Написать в личку участникам, которые запускали бота</label><button onclick="addRelease()">Завести релиз</button></div>` : ''}</section>

  <section class="panel settings" style="margin-top:20px"><h2>Интеграции и доступ</h2>
  <div class="settingrow"><div><b>Google Sheets</b><p>Хранилище всех данных. Проверка раз в час${m.health_checked_at ? ', последняя ' + showDT(m.health_checked_at) : ''}.</p></div><span class="badge ${h.sheets === 'ok' ? 'approved' : 'review'}">${esc(h.sheets || '—')}</span></div>
  <div class="settingrow"><div><b>Claude</b><p>Классификация обезличенных эпизодов. Сегодня ${m.calls_today} из ${m.daily_limit} вызовов.</p></div><span class="badge ${h.claude === 'ok' ? 'approved' : 'review'}">${esc(h.claude || '—')}</span></div>
  <div class="settingrow"><div><b>Telegram</b><p>${m.bot_username ? 'Бот @' + esc(m.bot_username) : 'Бот не подключён'} · участников в чатах: ${S.data.participants}</p></div><span class="badge ${h.telegram === 'ok' ? 'approved' : 'review'}">${esc(h.telegram || '—')}</span></div>
  <div class="settingrow"><div><b>Администраторы</b><p>${S.data.admins.map(a => esc(a.name || a.telegram_id) + ' · ' + esc(a.telegram_id)).join('<br>') || 'Не заданы'}</p></div></div>
  ${admin ? '<div class="settingrow"><div><b>Перечитать таблицу</b><p>Если правили данные прямо в Google Sheets.</p></div><button onclick="reloadSheets()">Перечитать</button></div>' : ''}</section>`;
}
async function saveSetting(key, value) { try { await api('POST', '/api/settings', { [key]: value }); await load(true); toast('Настройка сохранена'); } catch (e) { toast(e.message); } }
async function opRequest(body) { try { await api('POST', '/api/operations', body); await load(true); toast('Справочник обновлён'); } catch (e) { toast(e.message); } }
function opAction(action, k, extra = {}) { const op = S.opsRows[k]; opRequest({ action, section: op.section, operation: op.operation, ...extra }); }
function opMerge(k) { opAction('merge', k, { target: $('mt-' + k)?.value || '' }); }
function opAdd() { opRequest({ action: 'add', section: $('newopSection').value, operation: $('newopName').value.trim() }); }
async function addRelease() {
  try {
    const r = await api('POST', '/api/releases', { version: $('relVersion').value.trim(), platform: $('relPlatform').value, released_at: $('relDate').value, notify: $('relNotify').checked });
    await load(true); toast(r.notified ? `Релиз заведён, сообщения отправлены: ${r.notified}` : 'Релиз заведён');
  } catch (e) { toast(e.message); }
}
async function reloadSheets() { try { await api('POST', '/api/reload', {}); await load(true); toast('Данные перечитаны из таблицы'); } catch (e) { toast(e.message); } }

// ---------------------------------------------------------------- события
$('modal').addEventListener('click', e => { if (e.target === $('modal')) closeDetail(); });
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('imagemodal').hidden) closeModal('imagemodal');
  else if (!$('taskmodal').hidden) closeModal('taskmodal');
  else if (!$('loginmodal').hidden) closeModal('loginmodal');
  else closeDetail();
});
load();
