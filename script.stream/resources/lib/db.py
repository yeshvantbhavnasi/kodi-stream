# Local SQLite store: catalogue index, watch history, My List and saved suggestions.
import json
import os
import sqlite3
import time

import xbmcaddon
import xbmcvfs

PROFILE = xbmcvfs.translatePath(xbmcaddon.Addon('script.stream').getAddonInfo('profile'))
PATH = os.path.join(PROFILE, 'stream.db')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS titles (
    url TEXT PRIMARY KEY, name TEXT, kind TEXT, grp TEXT, plot TEXT,
    item TEXT, parent TEXT, first_seen REAL, last_seen REAL);
CREATE INDEX IF NOT EXISTS titles_name ON titles(name);
CREATE TABLE IF NOT EXISTS history (
    name TEXT PRIMARY KEY, item TEXT, parent TEXT, grp TEXT,
    played_at REAL, plays INTEGER DEFAULT 1, position REAL DEFAULT 0, duration REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mylist (
    name TEXT PRIMARY KEY, item TEXT, parent TEXT, grp TEXT, added_at REAL);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch REAL, rank INTEGER, name TEXT, reason TEXT,
    source TEXT, item TEXT, parent TEXT, grp TEXT, dismissed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, action TEXT, name TEXT, grp TEXT, kind TEXT, detail TEXT);
'''


def _connect():
    if not os.path.isdir(PROFILE):
        os.makedirs(PROFILE)
    conn = sqlite3.connect(PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _entry(row):
    return {'item': json.loads(row['item']), 'parent': row['parent'], 'group': row['grp']}


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
            "SELECT * FROM titles WHERE kind != 'Kids' AND name NOT IN (SELECT name FROM history) "
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


def title_count():
    with _connect() as conn:
        return conn.execute('SELECT COUNT(*) FROM titles').fetchone()[0]


# ---------- history ----------

def add_history(item, parent, group):
    with _connect() as conn:
        conn.execute(
            'INSERT INTO history (name, item, parent, grp, played_at) VALUES (?,?,?,?,?) '
            'ON CONFLICT(name) DO UPDATE SET item=excluded.item, parent=excluded.parent, grp=excluded.grp, '
            'played_at=excluded.played_at, plays=plays+1',
            (item['name'], json.dumps(item), parent, group, time.time()))


def update_progress(name, position, duration):
    with _connect() as conn:
        conn.execute('UPDATE history SET position=?, duration=? WHERE name=?', (position, duration, name))


def progress(name):
    with _connect() as conn:
        row = conn.execute('SELECT position, duration FROM history WHERE name=?', (name,)).fetchone()
    return (row['position'], row['duration']) if row else (0, 0)


def recent(limit=30):
    with _connect() as conn:
        rows = conn.execute('SELECT * FROM history ORDER BY played_at DESC LIMIT ?', (limit,)).fetchall()
    return [_entry(r) for r in rows]


def continue_watching(limit=20):
    """Titles stopped part-way: past the first minute and before the last 5%."""
    with _connect() as conn:
        rows = conn.execute(
            'SELECT * FROM history WHERE position > 60 AND duration > 0 AND position < duration * 0.95 '
            'ORDER BY played_at DESC LIMIT ?', (limit,)).fetchall()
    return [_entry(r) for r in rows]


def history_count():
    with _connect() as conn:
        return conn.execute('SELECT COALESCE(SUM(plays), 0) FROM history').fetchone()[0]


def clear_history():
    with _connect() as conn:
        conn.execute('DELETE FROM history')


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
    return batch


def suggestions():
    """The latest batch, minus anything dismissed or already played."""
    with _connect() as conn:
        rows = conn.execute(
            'SELECT * FROM suggestions WHERE batch=(SELECT MAX(batch) FROM suggestions) AND dismissed=0 '
            'AND name NOT IN (SELECT name FROM history) ORDER BY rank').fetchall()
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
