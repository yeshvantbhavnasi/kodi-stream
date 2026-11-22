# A complete local copy of the catalogue's titles in the viewer's languages (Telugu when none were chosen), so search
# answers instantly from this device instead of asking Sasta TV section by section.
#
# A background worker walks every page of Movies, Web Series and TV Shows (channels and their shows included) for
# each language, one listing at a time, and writes every title into library.db in the profile folder. The work list
# is saved after every page, so closing Stream or Kodi only pauses it; the next start carries on where it stopped.
# Once a language is complete, its sections are re-read every few hours from the first page until a page brings
# nothing new, which picks up new releases without walking the whole catalogue again.
import base64
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import xbmc

import catalogue as cat
import db

PATH = os.path.join(cat.PROFILE, 'library.db')
REFRESH_EVERY = 3600         # check for new titles in every finished section every hour
FULL_REFRESH = 24 * 3600     # walk every page of Movies/Shows once a day so deep pages and updates stay fresh
FRESH_PAGES = 8              # at most this many pages per hourly refresh, stopping early once a page brings nothing new
ENGLISH_PAGES = 10           # cap full crawl of huge English sections to the newest ~1000 titles each
PAUSE = 1.5                  # seconds between listings, so the screen's own requests are never kept waiting
SECTIONS = (('movies', 'Movies'), ('web_series', 'Web Series'), ('tv_shows', 'TV Shows'))
ENGLISH_SECTIONS = (('english_movies', 'Movies'), ('english_tv', 'English TV'))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS library (
    url TEXT PRIMARY KEY, name TEXT, norm TEXT, squeezed TEXT, kind TEXT, grp TEXT, item TEXT, parent TEXT,
    first_seen REAL, last_seen REAL);
CREATE INDEX IF NOT EXISTS library_grp ON library(grp);
CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, kind TEXT, grp TEXT, queue TEXT, done_at REAL, pages INTEGER, full_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, items TEXT, fetched_at REAL);
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
        cols = [r['name'] for r in conn.execute('PRAGMA table_info(jobs)').fetchall()]
        if 'full_at' not in cols:
            conn.execute('ALTER TABLE jobs ADD COLUMN full_at REAL DEFAULT 0')


def normalize(text):
    """Lower case, punctuation as spaces: 'Pushpa 2: The Rule (2024)' -> 'pushpa 2 the rule 2024'."""
    return ' '.join(re.findall(r'\w+', (text or '').lower()))


def squeeze(text):
    """Letters and digits only, so 'Pushpa2' finds 'Pushpa 2' and 'Ala Vaikunthapurramuloo' finds 'AlaVaikunthapurramuloo'."""
    return re.sub(r'\W+', '', (text or '').lower())


def languages():
    """The languages to keep complete: Telugu always first, Hindi secondary."""
    chosen = [g for g in db.chosen_languages() if g != 'english']
    return db.ordered_languages(chosen or ['telugu'])


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


def put_page(url, items):
    """Persist a full directory listing in library.db so page loads answer straight from the local index."""
    if not url or not items:
        return
    try:
        init()
        with _connect() as conn:
            conn.execute('INSERT OR REPLACE INTO pages VALUES (?,?,?)', (url, json.dumps(items), time.time()))
    except Exception as e:
        cat.log('library: put_page failed: {0}'.format(e))


def get_page(url):
    """(items, fetched_at) from the local page index, or None when not yet indexed."""
    if not url or not os.path.exists(PATH):
        return None
    try:
        with _connect() as conn:
            row = conn.execute('SELECT items, fetched_at FROM pages WHERE url=?', (url,)).fetchone()
        if row:
            return json.loads(row['items']), row['fetched_at']
    except Exception:
        pass
    return None


def _list(url):
    """One listing straight from the provider, without touching the screen's listing cache."""
    result = cat.rpc('Files.GetDirectory', {'directory': cat.plugin_folder(url), 'media': 'video',
                                            'properties': ['title', 'thumbnail', 'art', 'plot']})
    items = [cat.classify(e, n) for n, e in enumerate(result.get('files') or [])]
    return [i for i in items if i]


# ---------- the crawler ----------

def _job_key(kind, group):
    return '{0}|{1}'.format(kind, group.lower())


def _schedule_job(conn, key, kind, group, start_url):
    """Ensure a job exists and schedule either a daily full traversal or an hourly incremental refresh."""
    now = time.time()
    row = conn.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
    if row is None:
        conn.execute('INSERT INTO jobs (key, kind, grp, queue, done_at, pages, full_at) VALUES (?,?,?,?,?,?,?)',
                     (key, kind, group, json.dumps([[start_url, 'full', 0]]), 0, 0, 0))
        return
    if json.loads(row['queue']):
        return
    full_at = row['full_at'] or 0
    has_page = conn.execute('SELECT 1 FROM pages WHERE url=?', (start_url,)).fetchone() is not None
    if not full_at or now - full_at > FULL_REFRESH or not has_page:
        conn.execute('UPDATE jobs SET queue=? WHERE key=?', (json.dumps([[start_url, 'full', 0]]), key))
    elif now - (row['done_at'] or 0) > REFRESH_EVERY:
        conn.execute('UPDATE jobs SET queue=? WHERE key=?', (json.dumps([[start_url, 'fresh', 0]]), key))


def _plan():
    """Make sure there is a job for every section in every chosen language (Telugu first, Hindi secondary);
    returns the job keys in order."""
    keys = []
    roots = {}
    for section, kind in SECTIONS:
        try:
            folders = [f for f in cat.get_dir(cat.URLS[section]) if f['t'] == 'folder']
            roots[kind] = cat.by_affinity(folders)
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
                    if key in keys:
                        continue
                    keys.append(key)
                    _schedule_job(conn, key, kind, cat.title_case(folder['name']), folder['url'])
        if db.lang_match('english'):
            for section, kind in ENGLISH_SECTIONS:
                key = _job_key(kind, 'english')
                keys.append(key)
                _schedule_job(conn, key, kind, 'English', cat.URLS[section])
        # Keep Kids page 1 and Bigg Boss Telugu Season 10 warm in pages so Home & Movies open instantaneously.
        if conn.execute('SELECT 1 FROM pages WHERE url=?', (cat.URLS['kids'],)).fetchone() is None:
            try:
                cat.get_dir(cat.URLS['kids'])
            except Exception:
                pass
        if db.lang_match('telugu'):
            bb_url = ('http://sastatv.com/secured/php/fetchXclusiveS3.php?bkt=ZGVzaXR2c2hvd3M='
                      '&order=recent&sdirurl=VGVsdWd1L1NUQVIlMjBNQUEvL0JpZ2clMjBCb3NzLy9TZWFzb24lMjAxMC8='
                      '&thumb=aHR0cDovL3NtYXJ0NGsubWUvc3RhbGtlcl9wb3J0YWwvc2NyZWVuc2hvdHMvMjIzNi8yMjM1NjEuanBlZw==')
            bb_row = conn.execute('SELECT fetched_at FROM pages WHERE url=?', (bb_url,)).fetchone()
            if bb_row is None or time.time() - (bb_row['fetched_at'] or 0) > REFRESH_EVERY:
                try:
                    cat.get_dir(bb_url, fresh=True)
                except Exception:
                    pass
    return keys


def _step(key):
    """Read one listing of a job. Returns (did_work, new_titles_count)."""
    with _connect() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
    queue = json.loads(row['queue']) if row else []
    if not queue:
        return False, 0
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
    now = time.time()
    with _connect() as conn:
        if items:
            conn.execute('INSERT OR REPLACE INTO pages VALUES (?,?,?)', (url, json.dumps(items), now))
        new = _store(conn, url, kind, group, titles) if titles else 0
        rest = queue[1:]
        full_limit = ENGLISH_PAGES if group.lower() == 'english' else 0
        if nxt and ((mode == 'full' and (not full_limit or page + 1 < full_limit)) or (new and page + 1 < FRESH_PAGES)):
            rest.insert(0, [nxt, mode, page + 1])
        known = {q[0] for q in rest}
        rest += [m for m in more if m[0] not in known and (mode == 'full' or page == 0)]
        done_full = now if (not rest and mode == 'full') else (row['full_at'] or 0)
        conn.execute('UPDATE jobs SET queue=?, pages=pages+1, done_at=?, full_at=? WHERE key=?',
                     (json.dumps(rest), now if not rest else row['done_at'], done_full, key))
    if items:
        cat.warm_memory(url, items, now)
    if titles and (page == 0 or new > 0):
        try:
            db.index_titles(url, group, kind, titles)
        except Exception:
            pass
    return True, new


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
            started_pass = time.time()
            worked, added, pages_read = False, 0, 0
            for key in keys:
                failures = 0
                while not _stop.is_set():
                    while should_pause() and not _stop.is_set():
                        if monitor.waitForAbort(5):
                            return
                    try:
                        did, new = _step(key)
                        if not did:
                            break
                        worked, added, pages_read, failures = True, added + new, pages_read + 1, 0
                    except Exception as e:
                        failures += 1
                        cat.log('library: {0} failed: {1}'.format(key, e))
                        if failures >= 3:
                            break  # try this section again on the next pass
                    if monitor.waitForAbort(PAUSE):
                        return
            STATUS['page'] = ''
            if worked:
                try:
                    import audit
                    audit.event('library_refresh', pages=pages_read, added=added, ms=int((time.time() - started_pass) * 1000))
                except Exception:
                    pass
            if added:
                cat.log('library: {0} new titles indexed'.format(added))
                try:
                    import recs
                    recs.mark_dirty()
                    recs.share_catalogue()
                except Exception as e:
                    cat.log('library: post-refresh sync skipped: {0}'.format(e))
            if monitor.waitForAbort(60 if worked else 10 * 60):
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
    """Forget the saved titles and pages and walk everything again from the first page."""
    init()
    with _connect() as conn:
        conn.execute('DELETE FROM library')
        conn.execute('DELETE FROM jobs')
        conn.execute('DELETE FROM pages')


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


def _year_of(name, metadata=None):
    """4-digit release year from '(YYYY)' in the title name, a trailing/standalone year <= next year, or TMDb metadata."""
    max_year = time.localtime().tm_year + 1
    m = re.search(r'\(((?:19|20)\d{2})\)', name or '')
    if m and int(m.group(1)) <= max_year:
        return m.group(1)
    for y in reversed(re.findall(r'\b((?:19|20)\d{2})\b', name or '')):
        if 1920 <= int(y) <= max_year:
            return y
    if metadata:
        y = str((metadata.get(name) or {}).get('year') or '')
        if re.match(r'^(19|20)\d{2}$', y) and 1920 <= int(y) <= max_year:
            return y
    return ''


def _clean_letter(name):
    """Leading A-Z bucket for alphabetical browsing ('0-9' for numbers/symbols)."""
    text = re.sub(r'^[\W_]+', '', (name or '').strip())
    if not text:
        return '0-9'
    ch = text[0].upper()
    return ch if 'A' <= ch <= 'Z' else '0-9'


def channel_of(parent):
    """TV show channel / group name decoded from a Sasta TV sdirurl (e.g. 'Star Maa', 'ETV Telugu'), or ''."""
    if not parent or 'sdirurl=' not in parent:
        return ''
    try:
        raw = parse_qs(urlparse(parent).query).get('sdirurl', [''])[0]
        path = unquote(base64.b64decode(raw).decode('utf-8')).replace('//', '/').strip('/')
    except Exception:
        return ''
    parts = [p.strip() for p in path.split('/') if p.strip()]
    if len(parts) < 2:
        return ''
    ch = parts[1]
    return cat.title_case(ch)


def section_counts():
    """{(kind, grp_lower): count} from library.db."""
    if not os.path.exists(PATH):
        return {}
    try:
        with _connect() as conn:
            return {(r['kind'], (r['grp'] or '').lower()): r['n']
                    for r in conn.execute('SELECT kind, grp, COUNT(*) AS n FROM library GROUP BY kind, grp')}
    except Exception:
        return {}


def _channel_counts():
    """{(grp_lower, channel_lower): count} for indexed TV Shows."""
    if not os.path.exists(PATH):
        return {}
    counts = {}
    try:
        with _connect() as conn:
            for r in conn.execute("SELECT grp, parent FROM library WHERE kind='TV Shows'"):
                ch = channel_of(r['parent'])
                if ch:
                    key = ((r['grp'] or '').lower(), ch.lower())
                    counts[key] = counts.get(key, 0) + 1
    except Exception:
        pass
    return counts


def movie_sections():
    """Browsable movie sections ordered Telugu first, Hindi secondary: [{'label', 'title', 'url', 'group', 'kind', 'count'}]."""
    counts = section_counts()
    order = cat.top_groups()
    out = []
    try:
        folders = [f for f in cat.get_dir(cat.URLS['movies']) if f['t'] == 'folder']
        folders = db.only_chosen(folders, lambda f: f['name'])
    except Exception:
        folders = []
    items = []
    for f in folders:
        lang = cat.title_case(f['name'])
        n = counts.get(('Movies', lang.lower()), 0)
        items.append({'title': lang + ' Movies', 'url': f['url'], 'group': lang, 'kind': 'Movies', 'count': n})
    if db.lang_match('english'):
        n = counts.get(('Movies', 'english'), 0)
        items.append({'title': 'English Movies', 'url': cat.URLS['english_movies'], 'group': 'English', 'kind': 'Movies', 'count': n})
    items.sort(key=lambda x: (db.language_rank(x['group'], order), x['title'].lower()))
    items.append({'title': 'Kids Movies & Shows', 'url': cat.URLS['kids'], 'group': 'Kids', 'kind': 'Kids', 'count': 0})
    for it in items:
        it['label'] = '{0}  ({1:,})'.format(it['title'], it['count']) if it['count'] else it['title']
        out.append(it)
    return out


def show_groups():
    """Browsable show sections and TV show channel groups, Telugu first and Hindi secondary:
    [{'label', 'title', 'url', 'group', 'kind', 'channel', 'count'}]."""
    counts = section_counts()
    ch_counts = _channel_counts()
    order = cat.top_groups()
    try:
        ws_folders = db.only_chosen([f for f in cat.get_dir(cat.URLS['web_series']) if f['t'] == 'folder'], lambda f: f['name'])
    except Exception:
        ws_folders = []
    try:
        tv_folders = db.only_chosen([f for f in cat.get_dir(cat.URLS['tv_shows']) if f['t'] == 'folder'], lambda f: f['name'])
    except Exception:
        tv_folders = []
    ws_by_lang = {f['name'].lower(): f for f in ws_folders}
    tv_by_lang = {f['name'].lower(): f for f in tv_folders}
    langs = list(dict.fromkeys(list(ws_by_lang.keys()) + list(tv_by_lang.keys())))
    langs.sort(key=lambda l: (db.language_rank(l, order), l))
    out = []
    for lang_key in langs:
        lang = cat.title_case(lang_key)
        if lang_key in ws_by_lang:
            f = ws_by_lang[lang_key]
            n = counts.get(('Web Series', lang_key), 0)
            title = lang + ' Web Series'
            out.append({'label': '{0}  ({1:,})'.format(title, n) if n else title, 'title': title,
                        'url': f['url'], 'group': lang, 'kind': 'Web Series', 'channel': '', 'count': n})
        if lang_key in tv_by_lang:
            f = tv_by_lang[lang_key]
            n = counts.get(('TV Shows', lang_key), 0)
            title = lang + ' TV Shows'
            out.append({'label': '{0} · All Channels{1}'.format(title, '  ({0:,})'.format(n) if n else ''),
                        'title': title, 'url': f['url'], 'group': lang, 'kind': 'TV Shows', 'channel': '', 'count': n})
            try:
                channels = [c for c in cat.get_dir(f['url']) if c['t'] == 'folder']
            except Exception:
                channels = []
            ch_rows = []
            for ch in channels:
                ch_name = cat.title_case(ch['name'])
                cn = ch_counts.get((lang_key, ch_name.lower()), 0)
                ch_rows.append((cn, ch_name, ch))
            ch_rows.sort(key=lambda p: (-p[0], p[1].lower()))
            for cn, ch_name, ch in ch_rows:
                ctitle = '{0} · {1}'.format(lang, ch_name)
                out.append({'label': '    › {0}{1}'.format(ctitle, '  ({0:,})'.format(cn) if cn else ''),
                            'title': ctitle, 'url': ch['url'], 'group': lang, 'kind': 'TV Shows',
                            'channel': ch_name, 'count': cn, 'img': ch.get('img') or ''})
    if db.lang_match('english'):
        n = counts.get(('English TV', 'english'), 0)
        title = 'English TV Shows'
        out.append({'label': '{0}  ({1:,})'.format(title, n) if n else title, 'title': title,
                    'url': cat.URLS['english_tv'], 'group': 'English', 'kind': 'English TV', 'channel': '', 'count': n})
    return out


def matching_groups(query, kinds=None):
    """Browsable section or TV-channel folder entries whose name matches `query` (e.g. 'star maa', 'zee', 'telugu')."""
    q = normalize(query)
    if not q or len(q) < 2:
        return []
    words = q.split()
    flat = squeeze(query)
    pool = []
    if not kinds or any(k in kinds for k in ('Web Series', 'TV Shows', 'English TV')):
        pool += show_groups()
    if not kinds or 'Movies' in kinds:
        pool += movie_sections()
    out, seen = [], set()
    for g in pool:
        if kinds and g.get('kind') not in kinds:
            continue
        hay = normalize('{0} {1} {2}'.format(g.get('title', ''), g.get('channel', ''), g.get('kind', '')))
        if not (all(w in hay for w in words) or flat in squeeze(hay)):
            continue
        if g['url'] in seen:
            continue
        seen.add(g['url'])
        n_lbl = '{0:,} title{1}'.format(g['count'], '' if g['count'] == 1 else 's')
        item = {'t': 'folder', 'name': g['title'], 'url': g['url'],
                'plot': 'Browse all {0} in {1}'.format(n_lbl, g['title']) if g.get('count') else 'Browse ' + g['title']}
        if g.get('img'):
            item['img'] = g['img']
        meta_note = '{0} · {1}'.format(g['kind'], n_lbl) if g.get('count') else g['kind']
        out.append({'item': item, 'parent': g['url'], 'group': g['group'], 'kind': g['kind'],
                    'channel': g.get('channel', ''), 'meta': meta_note})
    return out


def page_search_sources():
    """All Sasta TV per-section search URLs recorded in library.db's pages table."""
    if not os.path.exists(PATH):
        return []
    sources = []
    try:
        with _connect() as conn:
            for (raw,) in conn.execute("SELECT items FROM pages WHERE items LIKE '%\"search\"%'"):
                for item in json.loads(raw):
                    if item.get('t') == 'search' and item.get('url') and item['url'] not in sources:
                        sources.append(item['url'])
    except Exception:
        pass
    return sources


def search(query, kinds=None, limit=150):
    """Titles whose name, year, or TV channel/group holds every word of the query, best matches first:
    [{'item', 'parent', 'group', 'meta', 'kind', 'channel'}]. Telugu first, Hindi secondary."""
    q = normalize(query)
    if not q:
        return []
    words = q.split()
    flat = squeeze(query)
    conds = ' AND '.join(['norm LIKE ?'] * len(words))
    params = ['%' + w + '%' for w in words]
    sql = 'SELECT * FROM library WHERE (({0}) OR squeezed LIKE ?)'.format(conds)
    params.append('%' + flat + '%')
    if kinds:
        sql += ' AND kind IN ({0})'.format(','.join('?' * len(kinds)))
        params += list(kinds)
    try:
        with _connect() as conn:
            rows = conn.execute(sql + ' LIMIT 2000', params).fetchall()
            # Also match TV Show channel/group names (e.g. 'Star Maa', 'Zee Telugu', 'ETV', 'Colors').
            ch_rows = []
            if not kinds or 'TV Shows' in kinds:
                for r in conn.execute("SELECT * FROM library WHERE kind='TV Shows'"):
                    ch = channel_of(r['parent'])
                    if not ch:
                        continue
                    ch_norm = normalize('{0} {1}'.format(r['grp'] or '', ch))
                    if all(w in ch_norm for w in words) or flat in squeeze(ch_norm):
                        ch_rows.append(r)
    except Exception as e:
        cat.log('library search failed: {0}'.format(e))
        return []
    preferred = cat.top_groups()
    out_rows = list(rows)
    seen_urls = {r['url'] for r in out_rows}
    for r in ch_rows:
        if r['url'] not in seen_urls:
            seen_urls.add(r['url'])
            out_rows.append(r)
    # If the query is a 4-digit year, also include shows/movies whose year came from TMDb metadata.
    if re.match(r'^(19|20)\d{2}$', q):
        for e in by_year(q, kinds, limit=limit):
            if e['item']['url'] not in seen_urls:
                seen_urls.add(e['item']['url'])
                out_rows.append({'url': e['item']['url'], 'norm': normalize(e['item']['name']),
                                 'squeezed': squeeze(e['item']['name']), 'grp': e['group'],
                                 'kind': e.get('kind') or '', 'item': json.dumps(e['item']),
                                 'parent': e['parent'], 'first_seen': e.get('first_seen') or 0})

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
        elif all(w in norm for w in words):
            s = 4
        else:
            s = 5  # matched by TV channel / group name
        year = re.search(r'\b((?:19|20)\d\d)\b', norm)
        return (s, db.language_rank(r['grp'], preferred), -int(year.group(1)) if year else 0, -r['first_seen'])

    out_rows = sorted(out_rows, key=score)
    out_rows = db.only_chosen(out_rows, lambda r: r['grp'])
    out, seen = [], set()
    for r in out_rows:
        if (r['norm'], r['grp']) in seen:
            continue
        seen.add((r['norm'], r['grp']))
        ch = channel_of(r['parent']) if r['kind'] == 'TV Shows' else ''
        meta_note = ' · '.join(p for p in (r['kind'], r['grp'], ch) if p)
        out.append({'item': json.loads(r['item']), 'parent': r['parent'], 'group': r['grp'], 'kind': r['kind'],
                    'channel': ch, 'meta': meta_note})
        if len(out) >= limit:
            break
    return out


def all_for_kinds(kinds):
    """Every indexed title of the given kinds across library.db and stream.db, deduplicated, chosen languages only."""
    seen, entries = set(), []
    if os.path.exists(PATH):
        try:
            with _connect() as conn:
                if kinds:
                    rows = conn.execute('SELECT * FROM library WHERE kind IN ({0}) ORDER BY first_seen DESC, rowid ASC'.format(
                        ','.join('?' * len(kinds))), list(kinds)).fetchall()
                else:
                    rows = conn.execute('SELECT * FROM library ORDER BY first_seen DESC, rowid ASC').fetchall()
            for r in rows:
                key = (r['norm'], (r['grp'] or '').lower())
                if key in seen:
                    continue
                seen.add(key)
                entries.append({'item': json.loads(r['item']), 'parent': r['parent'], 'group': r['grp'],
                                'kind': r['kind'], 'first_seen': r['first_seen']})
        except Exception as e:
            cat.log('library all_for_kinds failed: {0}'.format(e))
    for e in db.all_titles():
        if kinds and e.get('kind') not in kinds:
            continue
        key = (normalize(e['item']['name']), (e.get('group') or '').lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append(e)
    return db.only_chosen(entries, lambda e: e.get('group'))


def years_for(kinds):
    """[(year_label, count)] for Movies or Shows in the local index, newest year first ('Older' for < 2000)."""
    metadata = db.metadata_all()
    counts = {}
    older = 0
    for e in all_for_kinds(kinds):
        y = _year_of(e['item']['name'], metadata)
        if not y:
            continue
        if int(y) < 2000:
            older += 1
        else:
            counts[y] = counts.get(y, 0) + 1
    out = sorted(counts.items(), key=lambda kv: -int(kv[0]))
    if older:
        out.append(('Older', older))
    return out


def by_year(year, kinds=None, limit=800):
    """Indexed Movies or Shows from `year` ('2026', '2025', ... or 'Older'), Telugu first, Hindi secondary."""
    metadata = db.metadata_all()
    preferred = cat.top_groups()
    found = []
    for e in all_for_kinds(kinds):
        y = _year_of(e['item']['name'], metadata)
        if not y:
            continue
        if (year == 'Older' and int(y) < 2000) or y == str(year):
            found.append(e)
    found.sort(key=lambda e: (db.language_rank(e.get('group'), preferred), -(e.get('first_seen') or 0), e['item']['name'].lower()))
    return found[:limit]


def letters_for(kinds):
    """[('ALL', total), ('A', n), ('B', n), ...] for alphabetical browsing of Movies or Shows."""
    counts = {}
    total = 0
    for e in all_for_kinds(kinds):
        ch = _clean_letter(e['item']['name'])
        counts[ch] = counts.get(ch, 0) + 1
        total += 1
    if not total:
        return []
    letters = sorted([k for k in counts if k != '0-9']) + (['0-9'] if '0-9' in counts else [])
    return [('ALL', total)] + [(ch, counts[ch]) for ch in letters]


def by_letter(letter, kinds=None, limit=1000):
    """Indexed Movies or Shows starting with `letter` (or all when 'ALL'), alphabetically per language, Telugu first."""
    preferred = cat.top_groups()
    found = []
    for e in all_for_kinds(kinds):
        if letter == 'ALL' or _clean_letter(e['item']['name']) == letter:
            found.append(e)
    found.sort(key=lambda e: (db.language_rank(e.get('group'), preferred), normalize(e['item']['name'])))
    return found[:limit]


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
