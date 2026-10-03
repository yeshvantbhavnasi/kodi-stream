# Local SQLite store: catalogue index, watch history, My List and saved suggestions.
import json
import os
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


def recent(limit=30):
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM watched ORDER BY played_at DESC LIMIT ?', (limit,)).fetchall()
    return [_entry(r) for r in rows]


def continue_watching(limit=20):
    """Titles stopped part-way: past the first minute and not yet watched to the end."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM watched WHERE position > 60 AND duration > 0 AND done = 0 AND key NOT LIKE 'live:%' "
            'ORDER BY played_at DESC LIMIT ?', (limit,)).fetchall()
    return [_entry(r) for r in rows]


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
