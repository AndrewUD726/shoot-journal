#!/usr/bin/env python
"""Разбор съёмки по журналу «Журнал съёмки».

  python sort_shoot.py --cam ПАПКА_ВИДЕО [--cam ЕЩЁ] --sound ПАПКА_ЗВУКА \
                       --journal journal_....zip --out ПАПКА_ОТЧЁТА

Что делает:
  1. читает метаданные видео (ffprobe: creation_time, длительность, таймкод);
  2. находит сдвиг часов «камера → телефон»: по QR с экрана «Часы» (в кадре),
     иначе автоматически по рисунку интервалов между дублями, иначе --offset;
  3. раскладывает клипы по рилсам и дублям журнала;
  4. привязывает записи рекордера к клипам кросс-корреляцией огибающей звука;
  5. (--transcribe) ищет в расшифровке слейт «рилс N дубль M» для клипов без отметки
     и расшифровывает голосовые заметки;
  6. пишет report.html, edit.json, rename_plan.csv (и раскладывает файлы: --layout).

Нужно: Python 3.10+, numpy, ffmpeg/ffprobe в PATH; для QR — opencv-python-headless.
Исходники только читаются.
"""
import argparse, csv, datetime as dt, hashlib, html, json, os, re, shutil, subprocess, sys, zipfile
from pathlib import Path
import numpy as np
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except Exception:
        pass

VIDEO_EXT = {'.mp4', '.mov', '.mxf', '.mts', '.braw', '.avi', '.m4v'}
AUDIO_EXT = {'.m4a', '.wav', '.mp3', '.aac', '.flac', '.webm', '.ogg'}
RATING_RU = {'good': 'хороший', 'backup': 'запасной', 'bad': 'брак', None: 'без оценки'}
ENV_HZ = 100


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- метаданные
def parse_iso(s):
    try:
        return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def tc_to_sec(tc, fps):
    m = re.match(r'(\d+):(\d+):(\d+)[:;.](\d+)', tc or '')
    if not m:
        return None
    h, mi, s, f = map(int, m.groups())
    return h * 3600 + mi * 60 + s + (f / fps if fps else 0)


def probe(path):
    r = run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', str(path)])
    try:
        j = json.loads(r.stdout.decode('utf-8', 'replace'))
    except Exception:
        return None
    fmt = j.get('format', {})
    tags = {k.lower(): v for k, v in (fmt.get('tags') or {}).items()}
    info = {'path': str(path), 'name': Path(path).name, 'duration': float(fmt.get('duration') or 0),
            'creation': parse_iso(tags.get('creation_time', '')), 'brand': tags.get('major_brand', '').strip(),
            'fps': None, 'width': None, 'height': None, 'tc': None, 'has_audio': False, 'rotation': 0}
    tc = tags.get('timecode')
    for s in j.get('streams', []):
        st = {k.lower(): v for k, v in (s.get('tags') or {}).items()}
        tc = tc or st.get('timecode')
        if s.get('codec_type') == 'video' and info['fps'] is None:
            try:
                a, b = s.get('r_frame_rate', '0/1').split('/')
                info['fps'] = float(a) / float(b) if float(b) else None
            except Exception:
                pass
            info['width'], info['height'] = s.get('width'), s.get('height')
            for sd in s.get('side_data_list', []) or []:
                if 'rotation' in sd:
                    info['rotation'] = int(sd['rotation'])
        if s.get('codec_type') == 'audio':
            info['has_audio'] = True
    info['tc'] = tc
    return info


def scan_dir(d, exts):
    out = []
    for p in sorted(Path(d).rglob('*')):
        if p.is_file() and p.suffix.lower() in exts:
            out.append(p)
    return out


def refine_starts(clips):
    """Старт клипа в «часах камеры». creation_time точен до 1 с; если таймкод идёт
    синхронно с часами (free run), берём его дробную часть — точность до кадра."""
    deltas = []
    for c in clips:
        c['start'] = c['creation']
        c['start_src'] = 'creation_time'
        ts = tc_to_sec(c['tc'], c['fps'])
        if ts is not None and c['creation'] is not None:
            sod = c['creation'] % 86400
            d = (sod - ts + 43200) % 86400 - 43200
            c['_tcd'] = d
            deltas.append(d)
    if len(deltas) >= 3:
        med = float(np.median(deltas))
        for c in clips:
            d = c.get('_tcd')
            if d is not None and abs(d - med) <= 1.5:
                c['start'] = c['creation'] - d + med
                c['start_src'] = 'timecode'
    for c in clips:
        c.pop('_tcd', None)


# ---------------------------------------------------------------- журнал
def load_journal(path, out):
    path = Path(path)
    media = {}
    if path.suffix.lower() == '.zip':
        mdir = out / 'journal_media'
        with zipfile.ZipFile(path) as z:
            j = json.loads(z.read('journal.json').decode('utf-8'))
            for n in z.namelist():
                if n.startswith('media/') and not n.endswith('/'):
                    mdir.mkdir(parents=True, exist_ok=True)
                    dst = mdir / Path(n).name
                    dst.write_bytes(z.read(n))
                    media[n] = dst
    else:
        j = json.loads(path.read_text(encoding='utf-8'))
        for t in j.get('takes', []):
            for n in (t.get('notes') or []) + ([t['audio']] if t.get('audio') else []):
                f = n.get('file')
                if f and (path.parent / f).exists():
                    media[f] = path.parent / f
    j['_media'] = media
    j['takes'] = sorted(j.get('takes', []), key=lambda t: t['t'])
    return j


# ---------------------------------------------------------------- QR-калибровка
def qr_offset(clip, seconds=8.0, width=1280):
    """Ищет в первых секундах клипа QR с временем телефона (мс). Возвращает (offset, n_кадров) или None.
    offset: время_телефона = время_камеры + offset."""
    try:
        import cv2
    except ImportError:
        return None
    if not clip.get('width') or not clip.get('fps') or clip.get('start') is None:
        return None
    w = min(width, clip['width']); w -= w % 2
    h = int(round(clip['height'] * w / clip['width'])); h -= h % 2
    if abs(clip.get('rotation') or 0) in (90, 270):
        w, h = h, w
    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-t', str(seconds), '-i', clip['path'], '-an',
                          '-vf', f'scale={w}:{h}', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    det = cv2.QRCodeDetector()
    samples, i, size = [], 0, w * h
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        img = np.frombuffer(buf, np.uint8).reshape(h, w)
        try:
            val, _, _ = det.detectAndDecode(img)
        except Exception:
            val = ''
        if re.fullmatch(r'1[5-9]\d{11}|2\d{12}', val or ''):
            samples.append((clip['start'] + i / clip['fps'], int(val) / 1000.0))
        i += 1
    p.stdout.close(); p.wait()
    if len(samples) < 2:
        return None
    # QR держит значение 0,2 с: кадр с кодом v снят в телефонное время [v, v+0.2)
    d = np.array([v - c for c, v in samples])
    lo, hi = d.max(), d.min() + 0.2
    off = (lo + hi) / 2 if hi >= lo else float(np.median(d)) + 0.1
    return float(off), len(samples)


# ---------------------------------------------------------------- авто-сдвиг по интервалам
def auto_offset(clips, takes, tol=4.0):
    cs = np.array(sorted(c['start'] for c in clips if c['start'] is not None))
    ts = np.array([t['t'] / 1000.0 for t in takes])
    if len(cs) == 0 or len(ts) == 0:
        return None
    cands = (ts[:, None] - cs[None, :]).ravel()
    best = (0, None, None)
    scores = []
    for off in cands:
        d = np.abs((ts[:, None] - off) - cs[None, :]).min(axis=1)
        n = int((d <= tol).sum())
        scores.append((n, off))
        if n > best[0] or (n == best[0] and best[1] is not None and d[d <= tol].sum() < best[2]):
            best = (n, off, d[d <= tol].sum())
    n, off, _ = best
    if off is None:
        return None
    # уточнение: медиана остатков по совпавшим
    res = []
    for t in ts:
        k = np.abs((t - off) - cs).argmin()
        r = (t - off) - cs[k]
        if abs(r) <= tol:
            res.append(r)
    off += float(np.median(res)) if res else 0.0
    second = max((s for s, o in scores if abs(o - off) > 3 * tol), default=0)
    return off, n, len(ts), second


# ---------------------------------------------------------------- звук: огибающие и кросс-корреляция
def cache_key(path):
    st = os.stat(path)
    return hashlib.md5(f'{path}|{st.st_size}|{int(st.st_mtime)}'.encode()).hexdigest()[:16]


def envelope(path, cache):
    f = cache / f'env_{cache_key(path)}.npy'
    if f.exists():
        return np.load(f)
    r = run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'])
    x = np.frombuffer(r.stdout, np.int16).astype(np.float32)
    hop = 8000 // ENV_HZ
    n = len(x) // hop
    if n < 3 * ENV_HZ:
        e = np.zeros(0, np.float32)
    else:
        e = np.log1p(np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1)))
        k = 2 * ENV_HZ  # убираем медленный тренд (скользящее среднее 2 с)
        cs = np.cumsum(np.concatenate([[0], e]))
        ma = (cs[k:] - cs[:-k]) / k
        ma = np.concatenate([np.full(k // 2, ma[0]), ma, np.full(len(e) - len(ma) - k // 2, ma[-1])])
        e = (e - ma).astype(np.float32)
    np.save(f, e)
    return e


def xcorr(a, b, around=None, win=1.5):
    """a — запись рекордера, b — звук камеры. Возвращает (score, lag_sec): время_записи = время_камеры + lag.
    around — искать пик только рядом с ожидаемым лагом (±win с)."""
    if len(a) < ENV_HZ or len(b) < ENV_HZ:
        return 0.0, 0.0
    a = (a - a.mean()) / (a.std() + 1e-9)
    b = (b - b.mean()) / (b.std() + 1e-9)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    cc = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    if around is None:
        k = int(np.argmax(cc))
    else:
        ks = np.arange(int((around - win) * ENV_HZ), int((around + win) * ENV_HZ) + 1) % n
        k = int(ks[np.argmax(cc[ks])])
    lag = k if k <= len(a) else k - n  # лаги: −len(b)…+len(a)
    return float(cc[k] / min(len(a), len(b))), lag / ENV_HZ


def match_audio(recs, clips, cache, min_score=0.3):
    """Запись → клип-«якорь» по лучшей корреляции. Остальные клипы той же камеры, попавшие
    по времени внутрь записи, привязываются по часам камеры (лаг уточняется рядом с ожидаемым).
    Возвращает ({clip_path: {...}}, {rec_path: [(score, lag, clip_path)…]})."""
    log(f'Огибающие звука: {len(clips)} клипов, {len(recs)} записей…')
    cenv = {c['path']: envelope(c['path'], cache) for c in clips if c['has_audio']}
    by_path = {c['path']: c for c in clips}
    table, best = {}, {}

    def put(cp, r, s, lag, how):
        cur = best.get(cp)
        if cur is None or s > cur['score']:
            best[cp] = {'rec': r['path'], 'score': round(s, 3), 'lag': round(lag, 2), 'weak': s < 0.6, 'how': how}

    for r in recs:
        ra = envelope(r['path'], cache)
        row = []
        for cp, ce in cenv.items():
            s, lag = xcorr(ra, ce)
            row.append((s, lag, cp))
        row.sort(reverse=True)
        table[r['path']] = row
        # якорь: лучший клип; короткие клипы (< 10 с) дают случайные пики — им нужен запас
        anchor = None
        for i, (s, lag, cp) in enumerate(row[:5]):
            c = by_path[cp]
            nxt = max((x[0] for x in row[i + 1:i + 2]), default=0.0)
            need = 0.8 if min(c['duration'], r['duration']) < 10 else 0.5
            if s >= need or (i == 0 and s >= min_score and s >= 1.5 * max(nxt, 1e-6) and c['duration'] >= 10):
                anchor = (s, lag, c); break
        if not anchor:
            continue
        s, lag, ac = anchor
        put(ac['path'], r, s, lag, 'xcorr')
        if ac['start'] is None:
            continue
        rec_start = ac['start'] - lag  # начало записи в часах камеры
        for c in clips:
            if c is ac or c['dir'] != ac['dir'] or c['start'] is None or c['path'] not in cenv:
                continue
            exp = c['start'] - rec_start
            if -1.0 <= exp and exp + min(c['duration'], 3.0) <= r['duration']:
                s2, lag2 = xcorr(ra, cenv[c['path']], around=exp)
                put(c['path'], r, s2, lag2 if s2 >= min_score else exp, 'по часам камеры')
    return best, table


# ---------------------------------------------------------------- расшифровка и слейт
NUM = [('одиннадц', 11), ('двенадц', 12), ('тринадц', 13), ('четырнадц', 14), ('пятнадц', 15), ('шестнадц', 16),
       ('семнадц', 17), ('восемнадц', 18), ('девятнадц', 19), ('двадц', 20), ('тридц', 30), ('сорок', 40),
       ('пятьдес', 50), ('перв', 1), ('один', 1), ('одн', 1), ('втор', 2), ('два', 2), ('две', 2), ('трет', 3),
       ('три', 3), ('четв', 4), ('четыр', 4), ('пят', 5), ('шест', 6), ('седьм', 7), ('сем', 7), ('восьм', 8),
       ('восем', 8), ('девят', 9), ('десят', 10)]
REEL_W = ('рилс', 'рилз', 'рил', 'ролик', 'reel', 'сцена')
TAKE_W = ('дубл', 'тейк', 'take')


def word_num(w):
    w = re.sub(r'[^\w]', '', w.lower())
    if w.isdigit():
        return int(w)
    for p, v in NUM:
        if w.startswith(p):
            return v
    return None


def read_num(words, i):
    """Число, начиная с words[i] (в т. ч. «двадцать один»). → (value, next_index) или (None, i)."""
    if i >= len(words):
        return None, i
    v = word_num(words[i]['text'])
    if v is None:
        return None, i
    if v in (20, 30, 40, 50) and i + 1 < len(words):
        u = word_num(words[i + 1]['text'])
        if u is not None and 1 <= u <= 9:
            return v + u, i + 2
    return v, i + 1


def find_slates(words):
    """Ищет «рилс N … дубль M». words: [{text,start,end}]. → [{reel,take,t,end}]"""
    out = []
    norm = [re.sub(r'[^\w]', '', w['text'].lower()) for w in words]
    for i, w in enumerate(norm):
        if not w.startswith(REEL_W):
            continue
        reel, j = None, i + 1
        for j0 in range(i + 1, min(i + 3, len(words))):
            reel, j = read_num(words, j0)
            if reel is not None:
                break
        if reel is None:
            continue
        for k in range(j, min(j + 4, len(words))):
            if norm[k].startswith(TAKE_W):
                take = None
                for k0 in range(k + 1, min(k + 3, len(words))):
                    take, kend = read_num(words, k0)
                    if take is not None:
                        break
                if take is not None and words[kend - 1]['end'] - words[i]['start'] <= 8:
                    out.append({'reel': reel, 'take': take, 't': words[i]['start'], 'end': words[kend - 1]['end']})
                break
    return out


def transcribe(path, cache, enabled, tdir=None):
    """Слова с таймингами. Сначала ищет готовый JSON (tdir/<имя>.json), потом кэш, потом запускает hyperframes."""
    stem = Path(path).stem
    if tdir and (Path(tdir) / f'{stem}.json').exists():
        return json.loads((Path(tdir) / f'{stem}.json').read_text(encoding='utf-8'))
    f = cache / f'tr_{cache_key(path)}.json'
    if f.exists():
        return json.loads(f.read_text(encoding='utf-8'))
    if not enabled:
        return None
    wav = cache / f'wav_{cache_key(path)}.wav'
    run(['ffmpeg', '-v', 'error', '-y', '-i', str(path), '-vn', '-ac', '1', '-ar', '16000', str(wav)])
    tmp = cache / '_tr'
    tmp.mkdir(exist_ok=True)
    (tmp / 'transcript.json').unlink(missing_ok=True)
    log(f'  расшифровка {Path(path).name}…')
    subprocess.run(f'npx -y hyperframes@0.8.153 transcribe "{wav}" --engine parakeet -l ru --json -d "{tmp}"',
                   shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wav.unlink(missing_ok=True)
    res = tmp / 'transcript.json'
    if not res.exists():
        return None
    words = json.loads(res.read_text(encoding='utf-8'))
    f.write_text(json.dumps(words, ensure_ascii=False), encoding='utf-8')
    return words


# ---------------------------------------------------------------- раскладка
def assign(clips, takes, offsets, tol):
    """Клип → дубль. Возвращает список неразмеченных клипов."""
    for t in takes:
        t['_clips'] = []
    loose = []
    for c in clips:
        off = offsets.get(c['dir'])
        if off is None or c['start'] is None:
            loose.append(c); continue
        c['phone_start'] = c['start'] + off
        best, bd = None, None
        for t in takes:
            d = c['phone_start'] - t['t'] / 1000.0
            if bd is None or abs(d) < abs(bd):
                best, bd = t, d
        if best is not None and abs(bd) <= tol:
            c['match'] = {'by': 'journal', 'delta': round(bd, 2)}
            best['_clips'].append(c); continue
        inside = None
        for i, t in enumerate(takes):
            t0 = t['t'] / 1000.0
            t1 = (t['tEnd'] / 1000.0) if t.get('tEnd') else (takes[i + 1]['t'] / 1000.0 if i + 1 < len(takes) else t0 + 600)
            if t0 <= c['phone_start'] <= t1:
                inside = t
        if inside is not None:
            c['match'] = {'by': 'journal-inside', 'delta': round(c['phone_start'] - inside['t'] / 1000.0, 2)}
            inside['_clips'].append(c)
        else:
            loose.append(c)
    return loose


def thumb(clip, out):
    tdir = out / 'thumbs'
    tdir.mkdir(exist_ok=True)
    f = tdir / (Path(clip['name']).stem + '.jpg')
    if not f.exists():
        ss = min(1.0, clip['duration'] / 2)
        run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{ss:.2f}', '-i', clip['path'], '-frames:v', '1',
             '-vf', 'scale=320:-2', '-q:v', '5', str(f)])
    return f'thumbs/{f.name}' if f.exists() else ''


def safe(s):
    return re.sub(r'[\\/:*?"<>|]+', '_', s).strip()[:60]


def fmt_time(ts, tzmin):
    if ts is None:
        return '—'
    return (dt.datetime.fromtimestamp(ts, dt.timezone.utc) + dt.timedelta(minutes=tzmin)).strftime('%H:%M:%S')


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace('+00:00', 'Z') if ts is not None else None


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description='Разбор съёмки по журналу')
    ap.add_argument('--cam', action='append', required=True, help='папка с видео (можно несколько — по одной на камеру)')
    ap.add_argument('--sound', action='append', default=[], help='папка с записями рекордера')
    ap.add_argument('--journal', help='journal_*.zip или journal.json из приложения')
    ap.add_argument('--out', required=True)
    ap.add_argument('--offset', type=float, help='сдвиг вручную, с: время_телефона = время_камеры + offset')
    ap.add_argument('--no-qr', action='store_true', help='не искать QR в кадре')
    ap.add_argument('--qr-seconds', type=float, default=8.0)
    ap.add_argument('--tol', type=float, default=8.0, help='допуск «нажатие ↔ старт клипа», с')
    ap.add_argument('--transcribe', action='store_true', help='расшифровать звук (слейты, голосовые заметки)')
    ap.add_argument('--transcripts', help='папка с готовыми расшифровками <имя файла>.json')
    ap.add_argument('--layout', choices=['none', 'copy', 'hardlink'], default='none', help='разложить файлы в out/by_reel')
    ap.add_argument('--no-thumbs', action='store_true')
    a = ap.parse_args()

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    cache = out / '_cache'; cache.mkdir(exist_ok=True)
    warnings = []

    # --- видео
    clips = []
    for d in a.cam:
        files = scan_dir(d, VIDEO_EXT)
        log(f'{d}: {len(files)} видео, читаю метаданные…')
        group = []
        for p in files:
            info = probe(p)
            if info:
                info['dir'] = str(d); group.append(info)
        refine_starts(group)
        clips += group
    clips.sort(key=lambda c: (c['start'] or 0, c['name']))
    recs = []
    for d in a.sound:
        for p in scan_dir(d, AUDIO_EXT):
            info = probe(p)
            if info:
                recs.append(info)

    # --- журнал
    J = load_journal(a.journal, out) if a.journal else {'reels': [], 'takes': [], 'shoot': {'name': 'без журнала'}, '_media': {}, 'tzOffsetMin': 180}
    tz = J.get('tzOffsetMin', 180)
    takes = J['takes']
    reels = {r['id']: dict(r) for r in J.get('reels', [])}
    # звук, записанный приложением, тоже участвует в сопоставлении
    for t in takes:
        au = t.get('audio')
        if au and J['_media'].get(au.get('file')):
            info = probe(J['_media'][au['file']])
            if info:
                info['app_take'] = t['id']; info['t0'] = au.get('t0'); recs.append(info)

    # --- сдвиг часов
    offsets, offinfo = {}, {}
    for d in a.cam:
        group = [c for c in clips if c['dir'] == str(d)]
        if a.offset is not None:
            offsets[str(d)] = a.offset; offinfo[str(d)] = {'method': 'вручную', 'offset': a.offset}; continue
        got = None
        if not a.no_qr and takes:
            log(f'{d}: ищу QR-калибровку в начале клипов…')
            for c in group:
                r = qr_offset(c, a.qr_seconds)
                if r:
                    got = (r[0], f'QR в {c["name"]} ({r[1]} кадров)'); c['calib'] = True; break
        if got:
            offsets[str(d)] = got[0]; offinfo[str(d)] = {'method': got[1], 'offset': got[0]}
        elif takes:
            r = auto_offset(group, takes, tol=min(a.tol, 4.0))
            if r:
                off, n, tot, second = r
                offsets[str(d)] = off
                offinfo[str(d)] = {'method': f'по интервалам: совпало {n} из {tot} дублей (следующий вариант — {second})', 'offset': off}
                if n < max(3, tot * 0.5) or second >= n * 0.8:
                    warnings.append(f'{d}: сдвиг часов найден ненадёжно ({n} из {tot}). Сними экран «Часы» или задай --offset.')
        if str(d) not in offsets and takes:
            warnings.append(f'{d}: сдвиг часов не найден.')
    for d, o in offinfo.items():
        log(f'  сдвиг {d}: {o["offset"]:.2f} с — {o["method"]}')

    loose = assign(clips, takes, offsets, a.tol) if takes else list(clips)

    # --- звук ↔ видео
    amatch, atable = ({}, {})
    if recs:
        amatch, atable = match_audio(recs, clips, cache)
    rec_by_path = {r['path']: r for r in recs}
    used_recs = set()
    for c in clips:
        m = amatch.get(c['path'])
        if m:
            c['audio'] = [{'file': Path(m['rec']).name, 'path': m['rec'], 'lag': m['lag'], 'score': m['score'], 'weak': m['weak'], 'how': m['how']}]
            used_recs.add(m['rec'])

    # --- слейты (запасной способ) и заметки
    want_tr = a.transcribe or a.transcripts
    slate_reels = {}
    if want_tr:
        for c in list(loose):
            if c.get('calib'):
                continue
            srcs = [(c['path'], 0.0)] + [(x['path'], x['lag']) for x in c.get('audio', [])]
            for src, lag in srcs:
                words = transcribe(src, cache, a.transcribe, a.transcripts)
                sl = find_slates(words) if words else []
                if sl:
                    s = sl[0]
                    c['match'] = {'by': 'slate', 'reel': s['reel'], 'take': s['take'], 'slate_t': round(s['t'] - lag, 2), 'src': Path(src).name}
                    slate_reels.setdefault(s['reel'], []).append(c)
                    loose.remove(c)
                    break
        for t in takes:
            for n in t.get('notes') or []:
                if n.get('kind') == 'audio' and J['_media'].get(n.get('file')):
                    words = transcribe(J['_media'][n['file']], cache, a.transcribe, None)
                    if words:
                        n['transcript'] = ' '.join(w['text'] for w in words)

    # --- сборка результата
    by_n = {r['n']: r for r in reels.values()}
    for r in reels.values():
        r['takes'] = []; r['extra'] = []
    for t in takes:
        r = reels.get(t['reelId'])
        if r is None:
            r = reels.setdefault(t['reelId'], {'id': t['reelId'], 'n': t.get('reel') or 0, 'title': '(рилс удалён)', 'takes': [], 'extra': []})
        r['takes'].append(t)
    for n, cl in slate_reels.items():
        r = by_n.get(n)
        if r is None:
            r = {'id': f'slate{n}', 'n': n, 'title': f'Рилс {n} (по слейту)', 'takes': [], 'extra': []}
            reels[r['id']] = r; by_n[n] = r
        r['extra'] += cl
    # клипы без отметки: предложить рилс по соседству во времени
    for c in loose:
        if c.get('phone_start') is None or not takes:
            continue
        prev = [t for t in takes if t['t'] / 1000.0 <= c['phone_start']]
        if prev and c['phone_start'] - prev[-1]['t'] / 1000.0 < 900:
            c['near'] = {'reel': reels[prev[-1]['reelId']]['n'], 'take': prev[-1]['n']}
    no_clip = [t for t in takes if not t['_clips']]
    if no_clip:
        warnings.append(f'Дублей без видео: {len(no_clip)} (нажал «Дубль», а клипа с таким временем нет).')

    def clip_json(c):
        return {'file': c['name'], 'path': c['path'], 'duration': round(c['duration'], 2), 'fps': c['fps'],
                'size': [c['width'], c['height']], 'rotation': c['rotation'], 'timecode': c['tc'],
                'start_cam': iso(c['start']), 'start_phone': iso(c.get('phone_start')),
                'match': c.get('match'), 'near': c.get('near'), 'audio': c.get('audio', [])}

    rl = sorted(reels.values(), key=lambda r: r['n'])
    edit = {'format': 'shoot-journal-edit', 'version': 1, 'shoot': J.get('shoot'), 'generated': iso(dt.datetime.now().timestamp()),
            'note': 'audio.lag: время_записи = время_клипа + lag (с)', 'offsets': offinfo, 'warnings': warnings,
            'reels': [{'n': r['n'], 'title': r.get('title', ''), 'cast': r.get('cast', ''), 'text': r.get('text', ''),
                       'takes': [{'n': t['n'], 'rating': t.get('rating'), 't': iso(t['t'] / 1000.0),
                                  'duration': round((t['tEnd'] - t['t']) / 1000.0, 1) if t.get('tEnd') else None,
                                  'clips': [clip_json(c) for c in t['_clips']],
                                  'notes': [{k: v for k, v in n.items() if k in ('kind', 'text', 'transcript', 'file', 't')} for n in t.get('notes') or []]}
                                 for t in r['takes']],
                       'extra_clips': [clip_json(c) for c in r['extra']]} for r in rl],
            'unassigned': {'clips': [clip_json(c) for c in loose],
                           'audio': [{'file': r['name'], 'path': r['path'], 'duration': round(r['duration'], 2),
                                      'best_guess': [{'clip': Path(cp).name, 'score': round(s, 3), 'lag': round(l, 2)} for s, l, cp in atable.get(r['path'], [])[:2]]}
                                     for r in recs if r['path'] not in used_recs]}}
    (out / 'edit.json').write_text(json.dumps(edit, ensure_ascii=False, indent=1), encoding='utf-8')

    # --- план имён и раскладка
    plan = []
    for r in rl:
        folder = f'{r["n"]:02d}_{safe(r.get("title", ""))}'
        for t in r['takes']:
            tag = {'good': 'OK', 'backup': 'ZAP', 'bad': 'BRAK'}.get(t.get('rating'), 'NA')
            for c in t['_clips']:
                plan.append((c['path'], folder, f'R{r["n"]:02d}_T{t["n"]:02d}_{tag}__{c["name"]}', r['n'], t['n'], t.get('rating') or ''))
                for au in c.get('audio', []):
                    plan.append((au['path'], folder, f'R{r["n"]:02d}_T{t["n"]:02d}_{tag}__{au["file"]}', r['n'], t['n'], t.get('rating') or ''))
        for c in r['extra']:
            plan.append((c['path'], folder, f'R{r["n"]:02d}_slate__{c["name"]}', r['n'], '', ''))
    seen = set(); plan = [p for p in plan if not ((p[0], p[1]) in seen or seen.add((p[0], p[1])))]
    with open(out / 'rename_plan.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, delimiter=';'); w.writerow(['source', 'folder', 'new_name', 'reel', 'take', 'rating']); w.writerows(plan)
    if a.layout != 'none':
        for src, folder, name, *_ in plan:
            dst = out / 'by_reel' / folder / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                continue
            try:
                os.link(src, dst) if a.layout == 'hardlink' else shutil.copy2(src, dst)
            except OSError as e:
                warnings.append(f'Не удалось разложить {Path(src).name}: {e}'); break

    # --- HTML
    E = html.escape
    def clip_html(c):
        th = '' if a.no_thumbs else thumb(c, out)
        m = c.get('match') or {}
        how = {'journal': f'Δ {m.get("delta", 0):+.1f} с к нажатию', 'journal-inside': f'внутри дубля, +{m.get("delta", 0):.0f} с',
               'slate': f'по слейту «рилс {m.get("reel")} дубль {m.get("take")}» на {m.get("slate_t")} с'}.get(m.get('by'), '')
        au = ''.join(f'<div class="au{" weak" if x["weak"] else ""}">🎙 {E(x["file"])} · lag {x["lag"]:+.2f} с · {x["score"]:.2f}{" · слабо" if x["weak"] else ""}{" · по часам камеры" if x.get("how") != "xcorr" else ""}</div>' for x in c.get('audio', []))
        near = f'<div class="how">рядом: рилс {c["near"]["reel"]}, после дубля {c["near"]["take"]}</div>' if c.get('near') else ''
        img = f'<img src="{E(th)}" loading="lazy">' if th else ''
        return (f'<div class="clip">{img}<div><b>{E(c["name"])}</b> · {c["duration"]:.0f} с'
                f'{" · " + str(round(c["fps"])) + "p" if c["fps"] else ""}'
                f'<div class="how">{fmt_time(c.get("phone_start"), tz)} {how}</div>{near}{au}</div></div>')

    def notes_html(t):
        h = ''
        for n in t.get('notes') or []:
            f = J['_media'].get(n.get('file'))
            rel = f'journal_media/{Path(f).name}' if f else ''
            if n['kind'] == 'text':
                h += f'<div class="note">✎ {E(n.get("text", ""))}</div>'
            elif n['kind'] == 'audio':
                h += f'<div class="note">🎙 {E(n.get("transcript", "(голосовая заметка)"))}' + (f'<br><audio controls preload="none" src="{E(rel)}"></audio>' if rel else '') + '</div>'
            elif n['kind'] == 'photo' and rel:
                h += f'<a href="{E(rel)}"><img class="ph" src="{E(rel)}" loading="lazy"></a>'
        return h

    H = [f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Отчёт: {E(J.get("shoot", {}).get("name", ""))}</title>
<style>body{{background:#111;color:#eee;font:15px/1.4 system-ui,sans-serif;margin:24px auto;max-width:1100px;padding:0 16px}}
h1{{font-size:24px}}h2{{font-size:19px;margin:28px 0 6px;border-top:1px solid #333;padding-top:18px}}
.sub,.how{{color:#999;font-size:13px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #2a2a2a;padding:8px;vertical-align:top;text-align:left}}
.r{{display:inline-block;border-radius:99px;padding:1px 10px;color:#000;font-size:13px}}.good{{background:#66bb6a}}.backup{{background:#f9a825}}.bad{{background:#888}}.None{{background:#444;color:#ddd}}
.clip{{display:flex;gap:10px;margin-bottom:8px}}.clip img{{width:140px;border-radius:6px;align-self:flex-start}}.au{{color:#9cf;font-size:13px}}.weak{{color:#fc6}}
.note{{font-size:14px;margin:2px 0}}.ph{{height:90px;border-radius:6px;margin:2px}}.warn{{background:#4a3200;border-radius:8px;padding:8px 12px;margin:6px 0}}
details{{margin:4px 0;color:#bbb}}audio{{height:28px;margin-top:4px}}</style>
<h1>{E(J.get("shoot", {}).get("name", "Съёмка"))}</h1>
<div class="sub">клипов {len(clips)} · записей звука {len(recs)} · рилсов {len(rl)} · дублей {len(takes)} · без отметки {len(loose)}</div>''']
    for d, o in offinfo.items():
        H.append(f'<div class="sub">Часы {E(Path(d).name)}: телефон = камера {o["offset"]:+.2f} с ({E(o["method"])})</div>')
    for w in warnings:
        H.append(f'<div class="warn">⚠ {E(w)}</div>')
    for r in rl:
        good = sum(1 for t in r['takes'] if t.get('rating') == 'good')
        H.append(f'<h2>{r["n"]}. {E(r.get("title", ""))}</h2><div class="sub">{E(r.get("cast", "") or "")} · дублей {len(r["takes"])}, хороших {good}</div>')
        if r.get('text'):
            H.append(f'<details><summary>Текст</summary><div style="white-space:pre-wrap">{E(r["text"])}</div></details>')
        if r['takes']:
            H.append('<table><tr><th>Дубль</th><th>Время</th><th>Оценка</th><th>Видео и звук</th><th>Заметки</th></tr>')
            for t in r['takes']:
                cl = ''.join(clip_html(c) for c in t['_clips']) or '<span class="sub">нет клипа</span>'
                dur = f' · {(t["tEnd"] - t["t"]) / 1000:.0f} с' if t.get('tEnd') else ''
                H.append(f'<tr><td><b>{t["n"]}</b></td><td>{fmt_time(t["t"] / 1000, tz)}{dur}</td>'
                         f'<td><span class="r {t.get("rating")}">{RATING_RU.get(t.get("rating"))}</span></td><td>{cl}</td><td>{notes_html(t)}</td></tr>')
            H.append('</table>')
        if r['extra']:
            H.append('<div class="sub">Найдено по слейту:</div>' + ''.join(clip_html(c) for c in r['extra']))
    if loose:
        H.append(f'<h2>Без отметки ({len(loose)})</h2><div class="sub">Клипы, к которым не нашлось нажатия «Дубль» и слейта: перебивки, пробы, калибровка.</div>')
        H.append(''.join(clip_html(c) for c in loose))
    un = edit['unassigned']['audio']
    if un:
        H.append(f'<h2>Звук без пары ({len(un)})</h2>')
        for x in un:
            g = ', '.join(f'{b["clip"]} ({b["score"]:.2f})' for b in x['best_guess'])
            H.append(f'<div>🎙 {E(x["file"])} · {x["duration"]:.0f} с <span class="sub">ближайшие: {E(g)}</span></div>')
    (out / 'report.html').write_text('\n'.join(H), encoding='utf-8')

    log(f'\nГотово: {out / "report.html"}')
    log(f'  по дублям: {sum(len(t["_clips"]) for t in takes)} клипов · по слейту: {sum(len(v) for v in slate_reels.values())} · без отметки: {len(loose)}')
    log(f'  звук привязан к {len(amatch)} клипам; записей без пары: {len(un)}')
    for w in warnings:
        log('  ⚠ ' + w)


if __name__ == '__main__':
    main()
