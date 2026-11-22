# A complete local copy of the catalogue's titles in the viewer's languages (Telugu when none were chosen), so search
# answers instantly from this device instead of asking Sasta TV section by section.
#
# A background worker walks every page of Movies, Web Series and TV Shows (channels and their shows included) for
# each language, one listing at a time, and writes every title into library.db in the profile folder. The work list
# is saved after every page, so closing Stream or Kodi only pauses it; the next start carries on where it stopped.
# Once a language is complete, its sections are re-read every few hours from the first page until a page brings
# nothing new, which picks up new releases without walking the whole catalogue again.
import json
import os
import re
import sqlite3
import threading
import time

import xbmc

import catalogue as cat
import db

PATH = os.path.join(cat.PROFILE, 'library.db')
REFRESH_EVERY = 6 * 3600     # re-check the newest pages of a finished section this often
FRESH_PAGES = 8              # at most this many pages per refresh, stopping early on a page with nothing new
ENGLISH_PAGES = 10           # cap full crawl of huge English sections to the newest ~1000 titles each
PAUSE = 1.5                  # seconds between listings, so the screen's own requests are never kept waiting
SECTIONS = (('movies', 'Movies'), ('web_series', 'Web Series'), ('tv_shows', 'TV Shows'))
ENGLISH_SECTIONS = (('english_movies', 'Movies'), ('english_tv', 'English TV'))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS library (
    url TEXT PRIMARY KEY, name TEXT, norm TEXT, squeezed TEXT, kind TEXT, grp TEXT, item TEXT, parent TEXT,
    first_seen REAL, last_seen REAL);
CREATE INDEX IF NOT EXISTS library_grp ON library(grp);
CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, kind TEXT, grp TEXT, queue TEXT, done_at REAL, pages INTEGER);
'''

_worker = [None]
_stop = threading.Event()
STATUS = {'page': '', 'running': False}


def _connect():
    if not os.path.isdir(cat.PROFILE):
        os.makedirs(cat.PROFILE)
    conn = sqlite3.connect(PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _connect() as conn:
        conn.executescript(SCHEMA)


def normalize(text):
    """Lower case, punctuation as spaces: 'Pushpa 2: The Rule (2024)' -> 'pushpa 2 the rule 2024'."""
    return ' '.join(re.findall(r'\w+', (text or '').lower()))


def squeeze(text):
    """Letters and digits only, so 'Pushpa2' finds 'Pushpa 2' and 'Ala Vaikunthapurramuloo' finds 'AlaVaikunthapurramuloo'."""
    return re.sub(r'\W+', '', (text or '').lower())


def languages():
    """The languages to keep complete: the profile's choice in its own order, Telugu when nothing was chosen."""
    chosen = [g for g in db.chosen_languages() if g != 'english']
    return chosen or ['telugu']


# ---------- storing ----------

def _store(conn, parent, kind, group, items):
    """Insert or refresh titles; returns how many were new."""
    now = time.time()
    urls = [i['url'] for i in items]
    known = set()
    for n in range(0, len(urls), 400):
        chunk = urls[n:n + 400]
        known |= {r[0] for r in conn.execute('SELECT url FROM library WHERE url IN ({0})'.format(','.join('?' * len(chunk))), chunk)}
    conn.executemany(
        'INSERT INTO library VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET name=excluded.name, '
        'norm=excluded.norm, squeezed=excluded.squeezed, item=excluded.item, parent=excluded.parent, last_seen=excluded.last_seen',
        [(i['url'], i['name'], normalize(i['name']), squeeze(i['name']), kind, group, json.dumps(i), parent, now, now)
         for i in items])
    return len(set(urls) - known)


def _list(url):
    """One listing straight from the provider, without touching the screen's listing cache."""
    result = cat.rpc('Files.GetDirectory', {'directory': cat.plugin_folder(url), 'media': 'video',
                                            'properties': ['title', 'thumbnail', 'art', 'plot']})
    items = [cat.classify(e, n) for n, e in enumerate(result.get('files') or [])]
    return [i for i in items if i]


# ---------- the crawler ----------

def _job_key(kind, group):
    return '{0}|{1}'.format(kind, group.lower())


def _plan():
    """Make sure there is a job for every section in every chosen language; returns the job keys in order."""
    keys = []
    roots = {}
    for section, kind in SECTIONS:
        try:
            roots[kind] = [f for f in cat.get_dir(cat.URLS[section]) if f['t'] == 'folder']
        except Exception as e:
            cat.log('library: could not read {0}: {1}'.format(kind, e))
            roots[kind] = []
    with _connect() as conn:
        for lang in languages():
            for _, kind in SECTIONS:
                for folder in roots[kind]:
                    if not db.lang_match(folder['name'], [lang]):
                        continue
                    key = _job_key(kind, folder['name'])
                    keys.append(key)
                    row = conn.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
                    if row is None:
                        conn.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                                     (key, kind, cat.title_case(folder['name']), json.dumps([[folder['url'], 'full', 0]]), 0, 0))
                    elif not json.loads(row['queue']) and time.time() - (row['done_at'] or 0) > REFRESH_EVERY:
                        conn.execute('UPDATE jobs SET queue=? WHERE key=?', (json.dumps([[folder['url'], 'fresh', 0]]), key))
        if db.lang_match('english'):
            for section, kind in ENGLISH_SECTIONS:
                key = _job_key(kind, 'english')
                keys.append(key)
                row = conn.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
                if row is None:
                    conn.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                                 (key, kind, 'English', json.dumps([[cat.URLS[section], 'full', 0]]), 0, 0))
                elif not json.loads(row['queue']) and time.time() - (row['done_at'] or 0) > REFRESH_EVERY:
                    conn.execute('UPDATE jobs SET queue=? WHERE key=?', (json.dumps([[cat.URLS[section], 'fresh', 0]]), key))
    return keys


def _step(key):
    """Read one listing of a job. Returns False once the job has nothing left to do."""
    with _connect() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
    queue = json.loads(row['queue']) if row else []
    if not queue:
        return False
    url, mode, page = queue[0]
    kind, group = row['kind'], row['grp']
    STATUS['page'] = '{0} · {1} · page {2}'.format(group, kind, page + 1)
    items = _list(url)
    titles, more = [], []
    for item in items:
        if item['t'] == 'play' and not cat.is_live(item):
            titles.append(item)
        elif item['t'] == 'folder':
            if kind == 'Movies':
                continue  # collections and years only repeat films the listing pages already have
            if kind == 'TV Shows' and cat.is_category(item['url']):
                more.append([item['url'], mode, 0])  # a channel: its folders are the shows
            else:
                titles.append(item)  # a show or web series; its seasons are read when it is opened
    nxt = next((i['url'] for i in items if i['t'] == 'next'), None)
    with _connect() as conn:
        new = _store(conn, url, kind, group, titles) if titles else 0
        rest = queue[1:]
        full_limit = ENGLISH_PAGES if group.lower() == 'english' else 0
        if nxt and ((mode == 'full' and (not full_limit or page + 1 < full_limit)) or (new and page + 1 < FRESH_PAGES)):
            rest.insert(0, [nxt, mode, page + 1])
        known = {q[0] for q in rest}
        rest += [m for m in more if m[0] not in known and (mode == 'full' or page == 0)]
        conn.execute('UPDATE jobs SET queue=?, pages=pages+1, done_at=? WHERE key=?',
                     (json.dumps(rest), time.time() if not rest else row['done_at'], key))
    return True


def _run(should_pause):
    monitor = xbmc.Monitor()
    STATUS['running'] = True
    try:
        init()
        if monitor.waitForAbort(20):  # let Home finish loading first
            return
        while not _stop.is_set() and not monitor.abortRequested():
            if db.KIDS:
                return
            keys = _plan()
            worked = False
            for key in keys:
                failures = 0
                while not _stop.is_set():
                    while should_pause() and not _stop.is_set():
                        if monitor.waitForAbort(5):
                            return
                    try:
                        if not _step(key):
                            break
                        worked, failures = True, 0
                    except Exception as e:
                        failures += 1
                        cat.log('library: {0} failed: {1}'.format(key, e))
                        if failures >= 3:
                            break  # try this section again on the next pass
                    if monitor.waitForAbort(PAUSE):
                        return
            STATUS['page'] = ''
            if monitor.waitForAbort(60 if worked else 15 * 60):
                return
    finally:
        STATUS['running'] = False


def start(should_pause=lambda: False):
    """Start the background worker once; should_pause() returning True holds it (playing, screen busy)."""
    if _worker[0] and _worker[0].is_alive():
        if not _stop.is_set():
            return
        _worker[0].join(10)  # the last Home closed; let its worker finish the listing it was reading
        if _worker[0].is_alive():
            return
    _stop.clear()
    _worker[0] = threading.Thread(target=_run, args=(should_pause,), daemon=True)
    _worker[0].start()


def stop():
    _stop.set()


def rebuild():
    """Forget the saved titles and walk everything again from the first page."""
    init()
    with _connect() as conn:
        conn.execute('DELETE FROM library')
        conn.execute('DELETE FROM jobs')


# ---------- reading ----------

def status():
    """(titles saved, sections complete, sections known, what is being read now)."""
    try:
        init()
        with _connect() as conn:
            count = conn.execute('SELECT COUNT(*) FROM library').fetchone()[0]
            jobs = conn.execute('SELECT done_at FROM jobs').fetchall()
    except Exception:
        return 0, 0, 0, ''
    return count, sum(1 for j in jobs if j['done_at']), len(jobs), STATUS['page']


def complete(langs=None):
    """True when every known section in the given languages (the chosen ones by default) has been walked once."""
    langs = langs or languages()
    try:
        with _connect() as conn:
            jobs = conn.execute('SELECT grp, done_at FROM jobs').fetchall()
    except Exception:
        return False
    mine = [j for j in jobs if db.lang_match(j['grp'], langs)]
    return bool(mine) and all(j['done_at'] for j in mine)


def search(query, limit=120):
    """Titles whose name holds every word of the query (or the query run together), best matches first:
    [{'item', 'parent', 'group', 'meta', 'kind'}]. Chosen languages only, unless nothing there matches."""
    q = normalize(query)
    if not q:
        return []
    words = q.split()
    flat = squeeze(query)
    conds = ' AND '.join(['norm LIKE ?'] * len(words))
    params = ['%' + w + '%' for w in words]
    try:
        with _connect() as conn:
            rows = conn.execute('SELECT * FROM library WHERE ({0}) OR squeezed LIKE ? LIMIT 2000'.format(conds),
                                params + ['%' + flat + '%']).fetchall()
    except Exception as e:
        cat.log('library search failed: {0}'.format(e))
        return []
    preferred = cat.top_groups()

    def score(r):
        norm = r['norm']
        base = re.sub(r'\s*\b(19|20)\d\d\b.*$', '', norm)
        if base == q or norm == q:
            s = 0
        elif norm.startswith(q) or r['squeezed'].startswith(flat):
            s = 1
        elif re.search(r'\b' + re.escape(q), norm):
            s = 2
        elif q in norm:
            s = 3
        else:
            s = 4
        year = re.search(r'\b((?:19|20)\d\d)\b', norm)
        lang = (r['grp'] or '').lower()
        return (s, preferred.index(lang) if lang in preferred else len(preferred), -int(year.group(1)) if year else 0, -r['first_seen'])

    rows = sorted(rows, key=score)
    rows = db.only_chosen(rows, lambda r: r['grp'])
    out, seen = [], set()
    for r in rows:
        if (r['norm'], r['grp']) in seen:
            continue
        seen.add((r['norm'], r['grp']))
        out.append({'item': json.loads(r['item']), 'parent': r['parent'], 'group': r['grp'], 'kind': r['kind'],
                    'meta': '{0} · {1}'.format(r['kind'], r['grp'])})
        if len(out) >= limit:
            break
    return out


def find_by_title(title, kinds=None, limit=12):
    """Look up titles in the local library whose name starts with `title`, for matching posted lists and language copies."""
    q = normalize(title)
    if not q or not os.path.exists(PATH):
        return []
    sql = 'SELECT * FROM library WHERE norm LIKE ?'
    params = [q + '%']
    if kinds:
        sql += ' AND kind IN ({0})'.format(','.join('?' * len(kinds)))
        params += list(kinds)
    try:
        with _connect() as conn:
            rows = conn.execute(sql + ' ORDER BY first_seen DESC LIMIT ?', params + [limit]).fetchall()
    except Exception:
        return []
    return [{'item': json.loads(r['item']), 'parent': r['parent'], 'group': r['grp'], 'kind': r['kind'],
             'first_seen': r['first_seen']} for r in rows]


def all_recent(per_group=150):
    """Newest library entries per language and kind, for sharing a fuller index with the recommendation service."""
    if not os.path.exists(PATH):
        return []
    try:
        with _connect() as conn:
            rows = conn.execute('SELECT * FROM library ORDER BY first_seen DESC, rowid ASC LIMIT 6000').fetchall()
    except Exception:
        return []
    counts, out = {}, []
    for r in rows:
        key = ((r['grp'] or '').lower(), r['kind'] or '')
        if counts.get(key, 0) >= per_group:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append({'item': json.loads(r['item']), 'parent': r['parent'], 'group': r['grp'], 'kind': r['kind'],
                    'first_seen': r['first_seen']})
    return out
