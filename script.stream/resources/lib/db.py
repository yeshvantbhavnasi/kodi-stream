# Local SQLite store: catalogue index, watch history, My List and saved suggestions.
import json
import os
import re
import sqlite3
import time

import xbmcaddon
import xbmcvfs

PROFILE = xbmcvfs.translatePath(xbmcaddon.Addon('script.stream').getAddonInfo('profile'))
PATH = os.path.join(PROFILE, 'stream.db')
PROFILES_PATH = os.path.join(PROFILE, 'profiles.json')
KIDS = False        # True while a kids profile is active: only kids titles are shown anywhere
ACTIVE = 'default'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS titles (
    url TEXT PRIMARY KEY, name TEXT, kind TEXT, grp TEXT, plot TEXT,
    item TEXT, parent TEXT, first_seen REAL, last_seen REAL);
CREATE INDEX IF NOT EXISTS titles_name ON titles(name);
CREATE TABLE IF NOT EXISTS history (
    name TEXT PRIMARY KEY, item TEXT, parent TEXT, grp TEXT,
    played_at REAL, plays INTEGER DEFAULT 1, position REAL DEFAULT 0, duration REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS watched (
    key TEXT PRIMARY KEY, name TEXT, label TEXT, series TEXT, item TEXT, parent TEXT, grp TEXT,
    played_at REAL, plays INTEGER DEFAULT 1, position REAL DEFAULT 0, duration REAL DEFAULT 0, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS mylist (
    name TEXT PRIMARY KEY, item TEXT, parent TEXT, grp TEXT, added_at REAL);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch REAL, rank INTEGER, name TEXT, reason TEXT,
    source TEXT, item TEXT, parent TEXT, grp TEXT, dismissed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, data TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS ratings (
    name TEXT PRIMARY KEY, value INTEGER, item TEXT, parent TEXT, grp TEXT, rated_at REAL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, action TEXT, name TEXT, grp TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, name TEXT, img TEXT, added_at REAL);
'''


def _connect():
    if not os.path.isdir(PROFILE):
        os.makedirs(PROFILE)
    conn = sqlite3.connect(PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- viewer profiles: each has its own database file ----------

def list_profiles():
    try:
        with open(PROFILES_PATH, 'r', encoding='utf-8') as f:
            profiles = json.load(f)
    except Exception:
        profiles = []
    return profiles or [{'id': 'default', 'name': 'Me', 'kids': False, 'pin': ''}]


def save_profiles(profiles):
    if not os.path.isdir(PROFILE):
        os.makedirs(PROFILE)
    with open(PROFILES_PATH, 'w', encoding='utf-8') as f:
        json.dump(profiles, f)


def last_profile():
    """The profile used last time, or None on a first run or if it has since been removed."""
    try:
        with open(os.path.join(PROFILE, 'last_profile.json'), 'r') as f:
            wanted = json.load(f)['id']
    except Exception:
        return None
    return next((p for p in list_profiles() if p['id'] == wanted), None)


def remember_profile(profile):
    try:
        with open(os.path.join(PROFILE, 'last_profile.json'), 'w') as f:
            json.dump({'id': profile['id']}, f)
    except Exception:
        pass


def use_profile(profile):
    """Switch every later read and write to this profile's own history, lists, ratings and suggestions."""
    global PATH, KIDS, ACTIVE
    ACTIVE = profile['id']
    KIDS = bool(profile.get('kids'))
    name = 'stream.db' if ACTIVE == 'default' else 'stream-{0}.db'.format(ACTIVE)
    PATH = os.path.join(PROFILE, name)
    init()


def play_key(item):
    """Stable identity for something playable. Live links carry expiring signatures, so those go by name."""
    url = item.get('url', '')
    if 'vendor1play.php' in url or '.m3u8' in url:
        return 'live:' + item['name']
    if url.startswith('plugin://plugin.video.youtube/'):
        video = re.search(r'[?&]video_id=([\w-]+)', url)
        return 'yt:' + (video.group(1) if video else item['name'])
    return url.split('?')[0]


def init():
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # One-time move from the older name-keyed history table.
        old = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        if old and not conn.execute("SELECT COUNT(*) FROM watched").fetchone()[0]:
            for r in conn.execute('SELECT * FROM history').fetchall():
                item = json.loads(r['item'])
                conn.execute('INSERT OR REPLACE INTO watched (key, name, label, series, item, parent, grp, played_at, plays, position, duration, done) '
                             'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                             (play_key(item), r['name'], r['name'], '', r['item'], r['parent'], r['grp'], r['played_at'], r['plays'],
                              r['position'], r['duration'], 1 if r['duration'] and r['position'] >= r['duration'] * 0.9 else 0))
            conn.execute('DELETE FROM history')


def _entry(row):
    entry = {'item': json.loads(row['item']), 'parent': row['parent'], 'group': row['grp']}
    keys = row.keys()
    if 'label' in keys and row['label']:
        entry['label'] = row['label']
    if 'series' in keys and row['series']:
        entry['series'] = row['series']
    if 'position' in keys:
        entry['note'] = _note(row)
        if row['duration'] and row['position'] > 30 and not row['done']:
            entry['progress'] = max(1, int(100 * row['position'] / row['duration']))
    return entry


def meta(key, default=None):
    with _connect() as conn:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def set_meta(key, value):
    with _connect() as conn:
        conn.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))


# ---------- catalogue index ----------

def index_titles(parent, group, kind, items):
    now = time.time()
    if meta('index_born') is None:
        set_meta('index_born', now)  # when this index started, so "new" titles are the ones seen after that
    rows = [(i['url'], i['name'], kind, group, i.get('plot', ''), json.dumps(i), parent, now, now) for i in items]
    with _connect() as conn:
        conn.executemany(
            'INSERT INTO titles VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET '
            'name=excluded.name, plot=excluded.plot, item=excluded.item, parent=excluded.parent, last_seen=excluded.last_seen',
            rows)


def candidates(limit=120, groups=None):
    """Indexed titles the viewer has not played, newest first, preferring the given languages."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM titles WHERE kind {0} 'Kids' AND name NOT IN (SELECT name FROM watched) ".format('=' if KIDS else '!=') +
            "AND name NOT IN (SELECT series FROM watched) AND name NOT IN (SELECT name FROM ratings) "
            'AND name NOT IN (SELECT name FROM suggestions WHERE dismissed=1) '
            'ORDER BY first_seen DESC, rowid ASC LIMIT 2000').fetchall()
    preferred = [g.lower() for g in (groups or [])]
    rows.sort(key=lambda r: preferred.index((r['grp'] or '').lower()) if (r['grp'] or '').lower() in preferred else len(preferred))
    return [dict(_entry(r), kind=r['kind']) for r in rows[:limit]]


def find_titles(text, limit=60):
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM titles WHERE name LIKE ? OR plot LIKE ? LIMIT ?',
                            ('%' + text + '%', '%' + text + '%', limit)).fetchall()
    return [_entry(r) for r in rows]


def find_names(text, limit=40):
    """Indexed titles whose name contains the text, names that start with it first."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM titles WHERE name LIKE ? ORDER BY CASE WHEN name LIKE ? THEN 0 ELSE 1 END, first_seen DESC LIMIT ?",
                            ('%' + text + '%', text + '%', limit)).fetchall()
    return [_entry(r) for r in rows]


def title_count():
    with _connect() as conn:
        return conn.execute('SELECT COUNT(*) FROM titles').fetchone()[0]


# ---------- history ----------

def add_history(item, parent, group, series=None, label=None):
    with _connect() as conn:
        conn.execute(
            'INSERT INTO watched (key, name, label, series, item, parent, grp, played_at) VALUES (?,?,?,?,?,?,?,?) '
            'ON CONFLICT(key) DO UPDATE SET item=excluded.item, parent=excluded.parent, grp=excluded.grp, '
            'label=excluded.label, series=excluded.series, played_at=excluded.played_at, plays=plays+1',
            (play_key(item), item['name'], label or item['name'], series or '', json.dumps(item), parent, group, time.time()))


def update_progress(key, position, duration):
    """Save where playback is. A title counts as watched once 90% of it has played."""
    done = 1 if duration > 0 and position >= duration * 0.9 else 0
    with _connect() as conn:
        conn.execute('UPDATE watched SET position=?, duration=?, done=MAX(done, ?) WHERE key=?', (position, duration, done, key))


def progress(key):
    with _connect() as conn:
        row = conn.execute('SELECT position, duration, done FROM watched WHERE key=?', (key,)).fetchone()
    return (row['position'], row['duration'], bool(row['done'])) if row else (0, 0, False)


def progress_for(keys):
    """{key: (position, duration, done)} for the given keys, used to mark episodes in a listing."""
    if not keys:
        return {}
    with _connect() as conn:
        rows = conn.execute('SELECT key, position, duration, done FROM watched WHERE key IN ({0})'.format(','.join('?' * len(keys))),
                            list(keys)).fetchall()
    return {r['key']: (r['position'], r['duration'], bool(r['done'])) for r in rows}


def last_played(keys):
    """(key, finished) for the most recently played of the given keys, or None if none has been played."""
    if not keys:
        return None
    with _connect() as conn:
        row = conn.execute('SELECT key, done FROM watched WHERE key IN ({0}) ORDER BY played_at DESC LIMIT 1'.format(
            ','.join('?' * len(keys))), list(keys)).fetchone()
    return (row['key'], bool(row['done'])) if row else None


def _note(row):
    """A short status for history rows: watched, or how far in."""
    if row['done']:
        return 'Watched'
    if row['duration'] and row['position'] > 30:
        seconds = int(row['position'])
        hours, minutes = seconds // 3600, (seconds % 3600) // 60
        stopped = '{0}:{1:02d}:{2:02d}'.format(hours, minutes, seconds % 60) if hours else '{0}:{1:02d}'.format(minutes, seconds % 60)
        return 'Stopped at {0} of {1} min'.format(stopped, int(row['duration'] // 60))
    return ''


def recent(limit=30):
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM watched ORDER BY played_at DESC LIMIT ?', (limit,)).fetchall()
    return [_entry(r) for r in rows]


def continue_watching(limit=20):
    """Titles stopped part-way: past the first 30 seconds and not yet watched to the end."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM watched WHERE position > 30 AND duration > 0 AND done = 0 AND key NOT LIKE 'live:%' "
            'ORDER BY played_at DESC LIMIT ?', (limit,)).fetchall()
    return [_entry(r) for r in rows]


def in_history(key):
    with _connect() as conn:
        return conn.execute('SELECT 1 FROM watched WHERE key=?', (key,)).fetchone() is not None


def clear_progress(key):
    """Take a title off Continue Watching: it stays in History and plays from the beginning next time."""
    with _connect() as conn:
        conn.execute('UPDATE watched SET position=0 WHERE key=?', (key,))


def forget(key):
    """Remove a title from History (and so from Continue Watching)."""
    with _connect() as conn:
        conn.execute('DELETE FROM watched WHERE key=?', (key,))


def history_count():
    with _connect() as conn:
        return conn.execute('SELECT COALESCE(SUM(plays), 0) FROM watched').fetchone()[0]


def clear_history():
    with _connect() as conn:
        conn.execute('DELETE FROM watched')


# ---------- My List ----------

def in_mylist(name):
    with _connect() as conn:
        return conn.execute('SELECT 1 FROM mylist WHERE name=?', (name,)).fetchone() is not None


def toggle_mylist(item, parent, group):
    with _connect() as conn:
        if conn.execute('SELECT 1 FROM mylist WHERE name=?', (item['name'],)).fetchone():
            conn.execute('DELETE FROM mylist WHERE name=?', (item['name'],))
            return False
        conn.execute('INSERT INTO mylist VALUES (?,?,?,?,?)', (item['name'], json.dumps(item), parent, group, time.time()))
        return True


def mylist():
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM mylist ORDER BY added_at DESC').fetchall()
    return [_entry(r) for r in rows]


# ---------- YouTube channels followed in Stream (kept here, so no Google account or API key is needed) ----------

def channels():
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM channels ORDER BY added_at DESC').fetchall()
    return [{'id': r['id'], 'name': r['name'], 'img': r['img'] or ''} for r in rows]


def is_subscribed(channel_id):
    with _connect() as conn:
        return conn.execute('SELECT 1 FROM channels WHERE id=?', (channel_id,)).fetchone() is not None


def toggle_channel(channel_id, name, img=''):
    """Follow a channel, or stop following it. Returns True when it is now followed."""
    with _connect() as conn:
        if conn.execute('SELECT 1 FROM channels WHERE id=?', (channel_id,)).fetchone():
            conn.execute('DELETE FROM channels WHERE id=?', (channel_id,))
            return False
        conn.execute('INSERT INTO channels VALUES (?,?,?,?)', (channel_id, name, img, time.time()))
        return True


def channels_version():
    """Changes whenever the followed channels change, so cached subscription listings are rebuilt."""
    with _connect() as conn:
        row = conn.execute('SELECT COUNT(*), COALESCE(MAX(added_at), 0) FROM channels').fetchone()
    return '{0}-{1}'.format(row[0], int(row[1]))


def recent_videos(limit=3):
    """The YouTube videos played most recently on this profile: [(video_id, name)]."""
    with _connect() as conn:
        rows = conn.execute("SELECT key, name FROM watched WHERE key LIKE 'yt:%' ORDER BY played_at DESC LIMIT ?", (limit,)).fetchall()
    return [(r['key'][3:], r['name']) for r in rows]


def recent_titles(limit=2):
    """The movies and shows played most recently on this profile (no channels or videos): [name].
    An episode counts as its show."""
    return [name for name, _, _ in recent_watched(limit)]


def recent_watched(limit=4):
    """[(title, is_show, language)] for the movies and shows played most recently, episodes folded into their show."""
    with _connect() as conn:
        rows = conn.execute("SELECT name, series, grp FROM watched WHERE key NOT LIKE 'yt:%' AND key NOT LIKE 'live:%' "
                            "ORDER BY played_at DESC LIMIT 40").fetchall()
    out, seen = [], set()
    for r in rows:
        title = r['series'] or r['name']
        if title in seen:
            continue
        seen.add(title)
        out.append((title, bool(r['series']), r['grp'] or ''))
        if len(out) >= limit:
            break
    return out


# ---------- suggestions ----------

def save_suggestions(picks, source):
    """picks: [{'entry': {item, parent, group}, 'reason': str}] in rank order. Every batch is kept."""
    batch = time.time()
    rows = [(batch, n, p['entry']['item']['name'], p.get('reason', ''), source, json.dumps(p['entry']['item']),
             p['entry']['parent'], p['entry'].get('group')) for n, p in enumerate(picks)]
    with _connect() as conn:
        conn.executemany('INSERT INTO suggestions (batch, rank, name, reason, source, item, parent, grp) '
                         'VALUES (?,?,?,?,?,?,?,?)', rows)
        # Keep the five most recent batches; dismissed titles stay hidden through their own flag in those rows.
        conn.execute('DELETE FROM suggestions WHERE batch NOT IN (SELECT DISTINCT batch FROM suggestions ORDER BY batch DESC LIMIT 5)')
    return batch


def suggestions():
    """The latest batch, minus anything dismissed or already played."""
    with _connect() as conn:
        rows = conn.execute(
            'SELECT * FROM suggestions WHERE batch=(SELECT MAX(batch) FROM suggestions) AND dismissed=0 '
            'AND name NOT IN (SELECT name FROM watched) AND name NOT IN (SELECT series FROM watched) ORDER BY rank').fetchall()
    return [dict(_entry(r), reason=r['reason'], source=r['source']) for r in rows]


def dismiss(name):
    with _connect() as conn:
        conn.execute('UPDATE suggestions SET dismissed=1 WHERE name=?', (name,))


def clear_suggestions():
    with _connect() as conn:
        conn.execute('DELETE FROM suggestions')


# ---------- interaction log (the sequence the recommender reads) ----------

def log_event(action, name, group=None, kind=None, detail=''):
    """action: played | stopped | finished | listed | unlisted | dismissed | searched"""
    with _connect() as conn:
        conn.execute('INSERT INTO events (at, action, name, grp, kind, detail) VALUES (?,?,?,?,?,?)',
                     (time.time(), action, name, group, kind, detail))


def events(limit=80):
    """Most recent interactions, oldest first."""
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------- likes and dislikes ----------

def rate(item, parent, group, value):
    """value: 1 like, -1 dislike, 0 clears the rating."""
    with _connect() as conn:
        if value == 0:
            conn.execute('DELETE FROM ratings WHERE name=?', (item['name'],))
        else:
            conn.execute('INSERT OR REPLACE INTO ratings VALUES (?,?,?,?,?,?)',
                         (item['name'], value, json.dumps(item), parent, group, time.time()))


def rating(name):
    with _connect() as conn:
        row = conn.execute('SELECT value FROM ratings WHERE name=?', (name,)).fetchone()
    return row['value'] if row else 0


def rated(value):
    """Titles the viewer liked (1) or disliked (-1), newest first."""
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM ratings WHERE value=? ORDER BY rated_at DESC', (value,)).fetchall()
    return [_entry(r) for r in rows]


def titles_in(groups, limit=40):
    """A spread of indexed titles from the given languages, for the first-run picker."""
    wanted = [g.lower() for g in groups]
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM titles WHERE kind != 'Kids' ORDER BY first_seen DESC, rowid ASC").fetchall()
    buckets = {}
    for r in rows:
        key = (r['grp'] or '').lower()
        if not wanted or key in wanted:
            buckets.setdefault((key, r['kind']), []).append(r)
    out = []
    depth = 0
    while len(out) < limit and any(len(b) > depth for b in buckets.values()):
        out += [b[depth] for b in buckets.values() if len(b) > depth]
        depth += 1
    return [_entry(r) for r in out[:limit]]


# ---------- language preference ----------

LANGUAGES = ['Hindi', 'Telugu', 'Tamil', 'Malayalam', 'Kannada', 'Marathi', 'Gujarati', 'Punjabi', 'Bengali', 'Urdu', 'English']
# Catalogue sections use different names for the same language, and file dubbed films separately.
ALIASES = {'hindi': ['hindi', 'south dubbed', 'english dubbed'], 'bengali': ['bengali', 'bangla'], 'urdu': ['urdu', 'pakistani']}


def chosen_languages():
    return [g.lower() for g in (meta('profile') or {}).get('languages', [])]


def lang_match(name, chosen=None):
    """True when a section or title language is one the viewer chose (or when nothing was chosen)."""
    chosen = chosen_languages() if chosen is None else chosen
    if not chosen:
        return True
    name = (name or '').lower().strip()
    for lang in chosen:
        for alias in ALIASES.get(lang, [lang]):
            if name == alias or (alias != 'english' and alias in name):
                return True
    return False


def only_chosen(entries, name_of):
    """Keep entries in the chosen languages; if that leaves nothing, show everything rather than an empty screen."""
    chosen = chosen_languages()
    kept = [e for e in entries if lang_match(name_of(e), chosen)]
    return kept or list(entries)


# ---------- cached ratings, posters, reviews and trailers ----------

def get_metadata(name):
    """(data, age in seconds) or None when the title has never been looked up."""
    with _connect() as conn:
        row = conn.execute('SELECT data, fetched_at FROM metadata WHERE name=?', (name,)).fetchone()
    return (json.loads(row['data']), time.time() - row['fetched_at']) if row else None


def put_metadata(name, data):
    with _connect() as conn:
        conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?,?)', (name, json.dumps(data), time.time()))


# ---------- discovery: genres, what is new, what the household plays most ----------

def _like(text):
    return '%' + text.replace('%', '').replace('_', '') + '%'


def by_genre(kinds, tmdb_genres, words, limit=400):
    """Indexed titles of the given kinds whose looked-up TMDb genres include one of tmdb_genres, or whose description
    contains one of the words. Newest first; the caller orders by language."""
    conds, params = [], []
    for g in tmdb_genres:
        conds.append('name IN (SELECT name FROM metadata WHERE data LIKE ?)')
        params.append('%"' + g + '"%')
    for w in words:
        conds.append('lower(plot) LIKE ?')
        params.append(_like(w.lower()))
    if not conds or not kinds:
        return []
    sql = 'SELECT * FROM titles WHERE kind IN ({0}) AND ({1}) ORDER BY first_seen DESC, rowid ASC LIMIT ?'.format(
        ','.join('?' * len(kinds)), ' OR '.join(conds))
    with _connect() as conn:
        rows = conn.execute(sql, list(kinds) + params + [limit]).fetchall()
    return [dict(_entry(r), kind=r['kind']) for r in rows]


def find_by_title(title, kinds=None, limit=3):
    """Indexed titles whose name starts with the given title (ignoring the year and quality words after it)."""
    sql = 'SELECT * FROM titles WHERE lower(name) LIKE ?'
    params = [title.lower().replace('%', '').replace('_', '') + '%']
    if kinds:
        sql += ' AND kind IN ({0})'.format(','.join('?' * len(kinds)))
        params += list(kinds)
    with _connect() as conn:
        rows = conn.execute(sql + ' ORDER BY first_seen DESC LIMIT ?', params + [limit]).fetchall()
    return [dict(_entry(r), kind=r['kind']) for r in rows]


def find_show(name):
    """The indexed folder of a show, by its exact name, or None."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM titles WHERE name=? AND kind IN ('Web Series', 'TV Shows', 'English TV') "
                           'ORDER BY last_seen DESC LIMIT 1', (name,)).fetchone()
    return dict(_entry(row), kind=row['kind']) if row else None


def new_titles(days=7, limit=200):
    """Titles that appeared in the catalogue in the last few days (not counting what the index held at the start)."""
    born = meta('index_born') or 0
    if not born or time.time() - born < 2 * 24 * 3600:
        return []
    since = max(time.time() - days * 24 * 3600, born + 24 * 3600)
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM titles WHERE first_seen > ? AND kind IN ('Movies', 'Web Series', 'TV Shows') "
                            'ORDER BY first_seen DESC LIMIT ?', (since, limit)).fetchall()
    return [dict(_entry(r), kind=r['kind']) for r in rows]


def profile_db_paths():
    """Every profile's database file on this device."""
    try:
        names = sorted(os.listdir(PROFILE))
    except Exception:
        return []
    return [os.path.join(PROFILE, n) for n in names if n == 'stream.db' or (n.startswith('stream-') and n.endswith('.db'))]


def top_titles(days=30, limit=10):
    """What the whole household played most in the last month, across every profile:
    [{'title', 'plays', 'series', 'entry'}] best first. Episodes count towards their show."""
    since = time.time() - days * 24 * 3600
    counts = {}
    for path in profile_db_paths():
        try:
            conn = sqlite3.connect(path, timeout=15)
            conn.row_factory = sqlite3.Row
            with conn:
                rows = conn.execute("SELECT * FROM watched WHERE played_at > ? AND key NOT LIKE 'live:%' AND key NOT LIKE 'yt:%'",
                                    (since,)).fetchall()
            conn.close()
        except Exception:
            continue
        for r in rows:
            title = r['series'] or r['name']
            c = counts.setdefault(title, {'title': title, 'plays': 0, 'series': bool(r['series']), 'row': r, 'at': 0})
            c['plays'] += max(1, r['plays'] or 1)
            if r['played_at'] > c['at']:
                c['at'], c['row'] = r['played_at'], r
    ranked = sorted(counts.values(), key=lambda c: (-c['plays'], -c['at']))[:limit]
    for c in ranked:
        c['entry'] = _entry(c['row'])
        del c['row']
    return ranked
