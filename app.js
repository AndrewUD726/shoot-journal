'use strict';
// Журнал съёмки — PWA. Всё хранится в IndexedDB, работает без сети.
const APP_VERSION = '1.0.0';

// ---------- IndexedDB ----------
const DB = (() => {
  let dbp;
  function open() {
    if (dbp) return dbp;
    dbp = new Promise((res, rej) => {
      const r = indexedDB.open('shoot-journal', 1);
      r.onupgradeneeded = () => {
        const d = r.result;
        d.createObjectStore('kv');
        d.createObjectStore('reels', { keyPath: 'id' });
        d.createObjectStore('takes', { keyPath: 'id' });
        d.createObjectStore('media');
      };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
    return dbp;
  }
  async function tx(store, mode, fn) {
    const d = await open();
    return new Promise((res, rej) => {
      const t = d.transaction(store, mode), s = t.objectStore(store);
      let out; const r = fn(s);
      if (r) r.onsuccess = () => { out = r.result; };
      t.oncomplete = () => res(out);
      t.onerror = () => rej(t.error);
      t.onabort = () => rej(t.error);
    });
  }
  return {
    get: (st, k) => tx(st, 'readonly', s => s.get(k)),
    all: st => tx(st, 'readonly', s => s.getAll()),
    put: (st, v, k) => tx(st, 'readwrite', s => (k === undefined ? s.put(v) : s.put(v, k))),
    del: (st, k) => tx(st, 'readwrite', s => s.delete(k)),
    clear: st => tx(st, 'readwrite', s => s.clear()),
  };
})();

// ---------- состояние ----------
const S = {
  shoot: null, reels: [], takes: [], settings: { appAudio: false },
  events: [], undo: [],
  view: 'reels', reelId: null, liveTakeId: null, editId: null,
  noteRec: null, takeRec: null, stream: null, wake: null,
};
const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const $ = sel => document.querySelector(sel);
const fmtT = ms => { const d = new Date(ms); return d.toLocaleTimeString('ru-RU', { hour12: false }); };
const fmtDur = s => { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const RATING = { good: 'Хороший', backup: 'Запасной', bad: 'Брак' };

async function load() {
  S.shoot = await DB.get('kv', 'shoot');
  if (!S.shoot) { S.shoot = { id: uid(), name: 'Съёмка ' + new Date().toLocaleDateString('ru-RU'), created: Date.now() }; await DB.put('kv', S.shoot, 'shoot'); }
  S.settings = Object.assign(S.settings, await DB.get('kv', 'settings') || {});
  S.events = await DB.get('kv', 'events') || [];
  S.undo = await DB.get('kv', 'undo') || [];
  S.reels = (await DB.all('reels')).sort((a, b) => a.n - b.n);
  S.takes = (await DB.all('takes')).sort((a, b) => a.t - b.t);
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
}
const saveReel = r => DB.put('reels', r);
const saveTake = t => DB.put('takes', t);
const saveUndo = () => DB.put('kv', S.undo.slice(-50), 'undo');
const logEvent = (kind, extra) => { S.events.push(Object.assign({ kind, t: Date.now() }, extra || {})); DB.put('kv', S.events, 'events'); };
const reelTakes = id => S.takes.filter(t => t.reelId === id);
const reelById = id => S.reels.find(r => r.id === id);
const takeById = id => S.takes.find(t => t.id === id);

// ---------- UI-помощники ----------
let toastTimer;
function toast(msg) {
  let el = $('.toast');
  if (!el) { el = document.createElement('div'); el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = msg; el.style.display = 'block';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.style.display = 'none'; }, 2200);
}
function flash() { const f = document.createElement('div'); f.className = 'flash'; document.body.appendChild(f); setTimeout(() => f.remove(), 300); }
async function wakeOn() { try { if ('wakeLock' in navigator && !S.wake) { S.wake = await navigator.wakeLock.request('screen'); S.wake.addEventListener('release', () => { S.wake = null; }); } } catch (e) { /* нет поддержки */ } }
function wakeOff() { if (S.wake) { S.wake.release().catch(() => {}); S.wake = null; } }
function go(view, extra) { Object.assign(S, extra || {}, { view }); render(); window.scrollTo(0, 0); }
function pills(takes) {
  const c = { good: 0, backup: 0, bad: 0, none: 0 };
  takes.forEach(t => { c[t.rating || 'none']++; });
  let h = '';
  if (c.good) h += `<span class="pill p-good">${c.good} хор</span>`;
  if (c.backup) h += `<span class="pill p-backup">${c.backup} зап</span>`;
  if (c.bad) h += `<span class="pill p-bad">${c.bad} брак</span>`;
  if (c.none) h += `<span class="pill p-none">${c.none} ?</span>`;
  return h || '<span class="sub">нет дублей</span>';
}

// ---------- экраны ----------
function vReels() {
  const items = S.reels.map(r => `
    <div class="reel" data-act="open" data-id="${r.id}">
      <div class="num">${r.n}</div>
      <div class="grow"><div class="t">${esc(r.title) || '<span class="sub">без названия</span>'}</div>
        <div class="m">${r.cast ? esc(r.cast) + ' · ' : ''}${pills(reelTakes(r.id))}</div></div>
      <button class="edit" data-act="edit" data-id="${r.id}" aria-label="Изменить">✎</button>
    </div>`).join('');
  return `
    <div class="top"><div class="grow"><h1>${esc(S.shoot.name)}</h1>
      <div class="sub">${S.reels.length} рилсов · ${S.takes.length} дублей</div></div>
      <button class="ghost" data-act="settings">Ещё</button></div>
    <div class="list">${items}
      <button class="add" data-act="new">+ Рилс</button>
      <button class="add" data-act="bulk">+ Списком (диктовка)</button>
    </div>
    <div class="bar"><button data-act="clock">Часы</button><button data-act="export">Экспорт</button><button data-act="undo">Отменить</button></div>`;
}

function vEdit() {
  const r = reelById(S.editId) || { title: '', text: '', cast: '' };
  return `
    <div class="top"><button class="ghost back" data-act="home">‹ Назад</button><div class="grow"></div>
      ${S.editId ? '<button class="ghost danger" data-act="delreel">Удалить</button>' : ''}</div>
    <h1>${S.editId ? 'Рилс ' + r.n : 'Новый рилс'}</h1>
    <label>Название</label><input type="text" id="f-title" value="${esc(r.title)}" placeholder="Не покупайте ребёнку обычное">
    <label>Кто в кадре</label><input type="text" id="f-cast" value="${esc(r.cast)}" placeholder="Вика">
    <label>Текст / сценарий</label><textarea id="f-text" placeholder="Можно надиктовать: микрофон на клавиатуре">${esc(r.text)}</textarea>
    <div class="bar" style="grid-template-columns:1fr"><button style="background:var(--accent);color:#000" data-act="savereel">Сохранить</button></div>`;
}

function vBulk() {
  return `
    <div class="top"><button class="ghost back" data-act="home">‹ Назад</button></div>
    <h1>Рилсы списком</h1>
    <textarea id="f-bulk" style="min-height:45vh" placeholder="Каждая строка — новый рилс.\nНазвание / кто в кадре\n\nНе покупайте обычное / Вика\nЧёрная пятница / Вика и Саша"></textarea>
    <div class="hint">Строка = рилс. После «/» — кто в кадре. При диктовке скажи «новая строка».</div>
    <div class="bar" style="grid-template-columns:1fr"><button style="background:var(--accent);color:#000" data-act="savebulk">Добавить</button></div>`;
}

function vReel() {
  const r = reelById(S.reelId); if (!r) return vReels();
  const takes = reelTakes(r.id);
  const live = S.liveTakeId && takeById(S.liveTakeId);
  if (live) return vLive(r, live);
  const last = takes[takes.length - 1];
  const lastBox = last ? `
    <div class="last"><div class="row"><div class="grow">Дубль ${last.n} · ${fmtT(last.t)}${last.tEnd ? ' · ' + fmtDur((last.tEnd - last.t) / 1000) : ''}</div>
      <span class="pill p-${last.rating || 'none'}">${last.rating ? RATING[last.rating] : 'без оценки'}</span></div>
      <div class="rate-mini">
        <button class="r-good ${last.rating === 'good' ? 'sel' : ''}" data-act="rate" data-id="${last.id}" data-r="good">Хороший</button>
        <button class="r-backup ${last.rating === 'backup' ? 'sel' : ''}" data-act="rate" data-id="${last.id}" data-r="backup">Запасной</button>
        <button class="r-bad ${last.rating === 'bad' ? 'sel' : ''}" data-act="rate" data-id="${last.id}" data-r="bad">Брак</button></div>
      ${notesLine(last)}
    </div>` : '';
  const idx = S.reels.indexOf(r);
  return `
    <div class="top"><button class="ghost back" data-act="home">‹ Рилсы</button><div class="grow"></div>
      <div class="sub">${pills(takes)}</div></div>
    <h1>${r.n}. ${esc(r.title)}</h1>
    <div class="sub">${r.cast ? esc(r.cast) + ' · ' : ''}следующий — дубль ${takes.length + 1}${S.settings.appAudio ? ' · звук пишет приложение' : ''}</div>
    ${lastBox}
    <div class="big-take"><button id="take-btn" data-act="take">Дубль</button></div>
    <div class="tools">
      <button data-act="prev" ${idx <= 0 ? 'style="opacity:.35"' : ''}>‹ Рилс</button>
      <button data-act="undo">Отменить</button>
      <button data-act="next" ${idx >= S.reels.length - 1 ? 'style="opacity:.35"' : ''}>Рилс ›</button>
    </div>
    ${last ? `<div class="tools" style="margin-top:8px"><button data-act="note" data-id="${last.id}">${S.noteRec ? 'Стоп заметки' : 'Голос'}</button><button data-act="photo" data-id="${last.id}">Фото</button><button data-act="text" data-id="${last.id}">Текст</button></div>` : ''}`;
}

function notesLine(t) {
  if (!t.notes || !t.notes.length) return '';
  const a = t.notes.filter(n => n.kind === 'audio').length, p = t.notes.filter(n => n.kind === 'photo').length;
  const txt = t.notes.filter(n => n.kind === 'text').map(n => esc(n.text)).join(' · ');
  return `<div class="notes">${a ? '🎙 ' + a + ' ' : ''}${p ? '📷 ' + p + ' ' : ''}${txt}</div>`;
}

function vLive(r, t) {
  return `
    <div class="top"><div class="grow"><div class="sub">${r.n}. ${esc(r.title)}</div>
      <div class="row"><div class="grow" style="font-size:20px">Дубль ${t.n}</div><div class="timer" id="timer">0:00</div></div></div></div>
    <div class="rate">
      <button class="r-good" data-act="rate" data-id="${t.id}" data-r="good">Хороший</button>
      <button class="r-backup" data-act="rate" data-id="${t.id}" data-r="backup">Запасной</button>
      <button class="r-bad" data-act="rate" data-id="${t.id}" data-r="bad">Брак</button>
    </div>
    <button data-act="retake" style="width:100%;margin-bottom:8px;background:#3a0d0d">Сразу следующий дубль (оценю потом)</button>
    <div class="tools">
      <button data-act="note" data-id="${t.id}" class="${S.noteRec ? 'rec-on' : ''}">${S.noteRec ? 'Стоп' : 'Голос'}</button>
      <button data-act="photo" data-id="${t.id}">Фото</button>
      <button data-act="undo">Отменить</button>
    </div>
    ${S.takeRec ? '<div class="hint" style="text-align:center">● идёт запись звука в приложении</div>' : ''}
    ${notesLine(t)}`;
}

function vSettings() {
  return `
    <div class="top"><button class="ghost back" data-act="home">‹ Назад</button></div>
    <h1>Настройки</h1>
    <label>Название съёмки</label><input type="text" id="f-shoot" value="${esc(S.shoot.name)}">
    <div class="sect">Звук</div>
    <label class="switch"><span>Звук дубля пишет приложение<br><span class="sub">Вместо Диктофона: запись стартует по «Дубль», стоп — по оценке. Экран не гасить.</span></span>
      <input type="checkbox" id="f-appaudio" ${S.settings.appAudio ? 'checked' : ''}></label>
    <div class="hint">Важно: голосовая заметка или запись в приложении прерывает запись Диктофона. Если звук пишет Диктофон — заметки делай после остановки.</div>
    <div class="sect">Данные</div>
    <div class="list">
      <button data-act="export">Экспорт журнала (zip)</button>
      <button data-act="exportjson">Экспорт только JSON</button>
      <button data-act="import">Импорт рилсов из JSON</button>
      <button data-act="newshoot" class="danger">Новая съёмка (очистить дубли)</button>
    </div>
    <div class="hint" style="margin-top:16px">Версия ${APP_VERSION}. Данные хранятся только на этом телефоне, пока не сделан экспорт.</div>
    <input type="file" id="f-import" accept=".json,application/json" style="display:none">`;
}

function vClock() {
  return `<div class="clock" data-act="closeclock">
    <div class="digits" id="clk">--:--:--.<span class="ms">---</span></div>
    <canvas id="qr" width="580" height="580"></canvas>
    <div class="sub">Сними экран 3–5 с каждой камерой (и с включённым Диктофоном — хлопни рядом).<br>Тап — закрыть.</div></div>`;
}

let timerInt, clockRAF;
function render() {
  clearInterval(timerInt); cancelAnimationFrame(clockRAF);
  const v = { reels: vReels, edit: vEdit, bulk: vBulk, reel: vReel, settings: vSettings, clock: vClock }[S.view] || vReels;
  $('#app').innerHTML = v();
  if (S.view === 'reel' && S.liveTakeId) {
    const t = takeById(S.liveTakeId);
    const tick = () => { const el = $('#timer'); if (el && t) el.textContent = fmtDur((Date.now() - t.t) / 1000); };
    tick(); timerInt = setInterval(tick, 250);
  }
  if (S.view === 'clock') startClock();
  if (S.view === 'reel' || S.view === 'clock') wakeOn(); else wakeOff();
}

// ---------- часы для калибровки ----------
function startClock() {
  const cv = $('#qr'), dg = $('#clk'); let lastQ = 0;
  const loop = () => {
    const now = Date.now();
    const d = new Date(now);
    const hms = d.toLocaleTimeString('ru-RU', { hour12: false });
    dg.innerHTML = `${hms}.<span class="ms">${String(now % 1000).padStart(3, '0')}</span>`;
    // QR меняется 5 раз в секунду и несёт момент своей отрисовки (мс с 1970).
    // Скрипт ловит кадры смены значения → точность до кадра камеры.
    const q = Math.floor(now / 200);
    if (q !== lastQ) { lastQ = q; QR.draw(cv, String(now)); }
    clockRAF = requestAnimationFrame(loop);
  };
  loop();
}

// ---------- действия ----------
async function startTake() {
  const r = reelById(S.reelId); if (!r) return;
  const t0 = Date.now();
  const t = { id: uid(), reelId: r.id, n: reelTakes(r.id).length + 1, t: t0, tEnd: null, rating: null, notes: [], audio: null, tz: -new Date().getTimezoneOffset() };
  S.takes.push(t); S.liveTakeId = t.id;
  S.undo.push({ type: 'take', id: t.id }); saveUndo();
  flash(); render();
  await saveTake(t);
  if (S.settings.appAudio) startTakeAudio(t);
}

async function rate(id, r) {
  const t = takeById(id); if (!t) return;
  S.undo.push({ type: 'rate', id, prev: t.rating, prevEnd: t.tEnd }); saveUndo();
  t.rating = r;
  if (S.liveTakeId === id) { t.tEnd = Date.now(); S.liveTakeId = null; await stopTakeAudio(t); }
  await saveTake(t);
  toast(`Дубль ${t.n}: ${RATING[r]}`); render();
}

async function undo() {
  const a = S.undo.pop(); saveUndo();
  if (!a) { toast('Нечего отменять'); return; }
  const t = takeById(a.id);
  if (a.type === 'take' && t) {
    if (S.liveTakeId === t.id) { S.liveTakeId = null; await stopTakeAudio(t, true); }
    for (const n of t.notes || []) if (n.blob !== false) await DB.del('media', n.id).catch(() => {});
    if (t.audio) await DB.del('media', t.audio.id).catch(() => {});
    S.takes = S.takes.filter(x => x.id !== t.id); await DB.del('takes', t.id);
    toast(`Отменён дубль ${t.n}`);
  } else if (a.type === 'rate' && t) {
    t.rating = a.prev; t.tEnd = a.prevEnd; await saveTake(t); toast(`Оценка дубля ${t.n} отменена`);
  } else if (a.type === 'note' && t) {
    t.notes = t.notes.filter(n => n.id !== a.noteId); await DB.del('media', a.noteId).catch(() => {}); await saveTake(t); toast('Заметка удалена');
  }
  render();
}

async function addNote(t, note, blob) {
  if (blob) await DB.put('media', blob, note.id);
  t.notes = t.notes || []; t.notes.push(note); await saveTake(t);
  S.undo.push({ type: 'note', id: t.id, noteId: note.id }); saveUndo();
}

async function getMic() {
  if (S.stream && S.stream.active) return S.stream;
  S.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
  return S.stream;
}
function pickMime() {
  for (const m of ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm']) if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  return '';
}
async function toggleNote(id) {
  const t = takeById(id); if (!t) return;
  if (S.noteRec) { S.noteRec.stop(); return; }
  if (S.takeRec) { toast('Сейчас пишется звук дубля'); return; }
  try {
    const st = await getMic(), mime = pickMime();
    const rec = new MediaRecorder(st, mime ? { mimeType: mime } : undefined), chunks = [];
    const t0 = Date.now();
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      S.noteRec = null;
      const blob = new Blob(chunks, { type: rec.mimeType || mime || 'audio/mp4' });
      await addNote(t, { id: uid(), kind: 'audio', t: t0, dur: (Date.now() - t0) / 1000, mime: blob.type, size: blob.size }, blob);
      releaseMic(); toast('Заметка сохранена'); render();
    };
    rec.start(); S.noteRec = rec; render();
  } catch (e) { toast('Нет доступа к микрофону'); }
}
function releaseMic() { if (!S.takeRec && !S.noteRec && S.stream) { S.stream.getTracks().forEach(x => x.stop()); S.stream = null; } }

async function startTakeAudio(t) {
  try {
    const st = await getMic(), mime = pickMime();
    const rec = new MediaRecorder(st, mime ? { mimeType: mime } : undefined), chunks = [];
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstart = () => { rec._t0 = Date.now(); };
    rec._chunks = chunks; rec._take = t.id;
    rec.start(1000); S.takeRec = rec; render();
  } catch (e) { toast('Звук не пишется: нет микрофона'); }
}
function stopTakeAudio(t, discard) {
  const rec = S.takeRec;
  if (!rec || rec._take !== t.id) return Promise.resolve();
  return new Promise(res => {
    rec.onstop = async () => {
      S.takeRec = null;
      if (!discard) {
        const blob = new Blob(rec._chunks, { type: rec.mimeType || 'audio/mp4' });
        const id = uid();
        await DB.put('media', blob, id);
        t.audio = { id, t0: rec._t0 || t.t, t1: Date.now(), mime: blob.type, size: blob.size };
        await saveTake(t);
      }
      releaseMic(); res();
    };
    rec.stop();
  });
}

function pickPhoto(id) {
  const inp = document.createElement('input');
  inp.type = 'file'; inp.accept = 'image/*'; inp.setAttribute('capture', 'environment');
  inp.onchange = async () => {
    const f = inp.files[0]; if (!f) return;
    const t = takeById(id); if (!t) return;
    let blob = f;
    try { // уменьшаем до 1600 px, чтобы журнал был лёгким
      const bmp = await createImageBitmap(f); const k = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
      const c = document.createElement('canvas'); c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
      c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
      blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.85)) || f;
    } catch (e) { /* оставить как есть */ }
    await addNote(t, { id: uid(), kind: 'photo', t: Date.now(), mime: blob.type || 'image/jpeg', size: blob.size }, blob);
    toast('Фото сохранено'); render();
  };
  inp.click();
}

async function textNote(id) {
  const t = takeById(id); if (!t) return;
  const s = prompt(`Заметка к дублю ${t.n}`, ''); if (!s) return;
  await addNote(t, { id: uid(), kind: 'text', t: Date.now(), text: s }, null);
  render();
}

function extOf(mime) { return /jpeg/.test(mime) ? 'jpg' : /png/.test(mime) ? 'png' : /heic/.test(mime) ? 'heic' : /webm/.test(mime) ? 'webm' : /mp4|m4a|aac/.test(mime) ? 'm4a' : 'bin'; }
function journalJSON() {
  const reels = S.reels.map(r => ({ id: r.id, n: r.n, title: r.title, cast: r.cast, text: r.text }));
  const takes = S.takes.map(t => ({
    id: t.id, reelId: t.reelId, reel: (reelById(t.reelId) || {}).n, n: t.n, t: t.t, tEnd: t.tEnd,
    tISO: new Date(t.t).toISOString(), rating: t.rating, tz: t.tz,
    notes: (t.notes || []).map(n => Object.assign({}, n, n.kind === 'text' ? {} : { file: `media/${n.id}.${extOf(n.mime)}` })),
    audio: t.audio ? Object.assign({}, t.audio, { file: `media/${t.audio.id}.${extOf(t.audio.mime)}` }) : null,
  }));
  return { format: 'shoot-journal', version: 1, app: APP_VERSION, exportedAt: Date.now(), tzOffsetMin: -new Date().getTimezoneOffset(),
    shoot: S.shoot, reels, takes, events: S.events };
}
function fileStamp() { const d = new Date(), p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`; }
async function shareOrDownload(blob, name) {
  const file = new File([blob], name, { type: blob.type });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    try { await navigator.share({ files: [file], title: name }); return; } catch (e) { if (e.name === 'AbortError') return; }
  }
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 3000);
}
async function exportZip() {
  toast('Собираю журнал…');
  const j = journalJSON(), files = [{ name: 'journal.json', data: new TextEncoder().encode(JSON.stringify(j, null, 1)) }];
  for (const t of j.takes) {
    for (const n of t.notes) if (n.file) { const b = await DB.get('media', n.id); if (b) files.push({ name: n.file, data: new Uint8Array(await b.arrayBuffer()) }); }
    if (t.audio) { const b = await DB.get('media', t.audio.id); if (b) files.push({ name: t.audio.file, data: new Uint8Array(await b.arrayBuffer()) }); }
  }
  await shareOrDownload(Zip.build(files), `journal_${fileStamp()}.zip`);
  logEvent('export');
}
async function exportJSON() {
  await shareOrDownload(new Blob([JSON.stringify(journalJSON(), null, 1)], { type: 'application/json' }), `journal_${fileStamp()}.json`);
}
async function importJSON(file) {
  try {
    const j = JSON.parse(await file.text());
    const list = Array.isArray(j) ? j : j.reels || [];
    let added = 0;
    for (const x of list) {
      const title = typeof x === 'string' ? x : x.title;
      if (!title) continue;
      const r = { id: uid(), n: S.reels.length + 1, title, cast: x.cast || '', text: x.text || '', created: Date.now() };
      S.reels.push(r); await saveReel(r); added++;
    }
    toast(`Добавлено рилсов: ${added}`); go('reels');
  } catch (e) { toast('Не получилось прочитать JSON'); }
}
async function renumber() { S.reels.forEach((r, i) => { r.n = i + 1; }); for (const r of S.reels) await saveReel(r); }

// ---------- обработчик ----------
document.addEventListener('click', async e => {
  const el = e.target.closest('[data-act]'); if (!el) return;
  const act = el.dataset.act, id = el.dataset.id;
  if (act === 'take' || act === 'retake') return; // ловим на pointerdown (быстрее)
  if (act === 'edit') e.stopPropagation();
  switch (act) {
    case 'home': if (S.view === 'settings') { const n = $('#f-shoot').value.trim(); if (n && n !== S.shoot.name) { S.shoot.name = n; await DB.put('kv', S.shoot, 'shoot'); } } go('reels'); break;
    case 'open': go('reel', { reelId: id }); break;
    case 'edit': go('edit', { editId: id }); break;
    case 'new': go('edit', { editId: null }); break;
    case 'bulk': go('bulk'); break;
    case 'settings': go('settings'); break;
    case 'clock': logEvent('clock-open'); go('clock', { prevView: S.view }); break;
    case 'closeclock': logEvent('clock-close'); go(S.prevView || 'reels'); break;
    case 'savereel': {
      const title = $('#f-title').value.trim(), cast = $('#f-cast').value.trim(), text = $('#f-text').value.trim();
      let r = reelById(S.editId);
      if (!r) { r = { id: uid(), n: S.reels.length + 1, created: Date.now() }; S.reels.push(r); }
      Object.assign(r, { title, cast, text }); await saveReel(r); go('reels'); break;
    }
    case 'delreel': {
      const r = reelById(S.editId);
      if (reelTakes(r.id).length && !confirm('У рилса есть дубли. Удалить вместе с ними?')) break;
      if (!reelTakes(r.id).length && !confirm('Удалить рилс?')) break;
      for (const t of reelTakes(r.id)) await DB.del('takes', t.id);
      S.takes = S.takes.filter(t => t.reelId !== r.id);
      S.reels = S.reels.filter(x => x.id !== r.id); await DB.del('reels', r.id); await renumber(); go('reels'); break;
    }
    case 'savebulk': {
      const lines = $('#f-bulk').value.split(/\n+/).map(s => s.trim()).filter(Boolean);
      for (const l of lines) {
        const [title, cast] = l.split(/\s*\/\s*/);
        const r = { id: uid(), n: S.reels.length + 1, title: title.replace(/[.,;]+$/, ''), cast: (cast || '').replace(/[.,;]+$/, ''), text: '', created: Date.now() };
        S.reels.push(r); await saveReel(r);
      }
      toast(`Добавлено: ${lines.length}`); go('reels'); break;
    }
    case 'rate': await rate(id, el.dataset.r); break;
    case 'undo': await undo(); break;
    case 'prev': case 'next': {
      if (S.liveTakeId) { toast('Сначала оцени дубль'); break; }
      const i = S.reels.findIndex(r => r.id === S.reelId) + (act === 'next' ? 1 : -1);
      if (S.reels[i]) go('reel', { reelId: S.reels[i].id }); break;
    }
    case 'note': await toggleNote(id); break;
    case 'photo': pickPhoto(id); break;
    case 'text': await textNote(id); break;
    case 'export': await exportZip(); break;
    case 'exportjson': await exportJSON(); break;
    case 'import': $('#f-import').click(); break;
    case 'newshoot': {
      if (!confirm('Сделал экспорт? Дубли и заметки этой съёмки будут удалены, рилсы останутся.')) break;
      await DB.clear('takes'); await DB.clear('media'); S.takes = []; S.undo = []; saveUndo(); S.events = []; DB.put('kv', [], 'events');
      S.shoot = { id: uid(), name: 'Съёмка ' + new Date().toLocaleDateString('ru-RU'), created: Date.now() }; await DB.put('kv', S.shoot, 'shoot');
      go('reels'); break;
    }
  }
});
document.addEventListener('pointerdown', async e => {
  const el = e.target.closest('[data-act="take"],[data-act="retake"]'); if (!el) return;
  e.preventDefault();
  if (el.dataset.act === 'retake') {
    const t = takeById(S.liveTakeId);
    if (t) { t.tEnd = Date.now(); S.liveTakeId = null; await stopTakeAudio(t); await saveTake(t); }
  }
  startTake();
});
document.addEventListener('change', async e => {
  if (e.target.id === 'f-appaudio') { S.settings.appAudio = e.target.checked; await DB.put('kv', S.settings, 'settings'); }
  if (e.target.id === 'f-import' && e.target.files[0]) importJSON(e.target.files[0]);
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && (S.view === 'reel' || S.view === 'clock')) wakeOn(); });

(async () => {
  await load();
  // если приложение закрылось посреди дубля — вернуть его на экран оценки
  const open = S.takes.filter(t => !t.rating && !t.tEnd && Date.now() - t.t < 30 * 60e3).pop();
  if (open) { S.liveTakeId = open.id; S.reelId = open.reelId; S.view = 'reel'; }
  render();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
})();
