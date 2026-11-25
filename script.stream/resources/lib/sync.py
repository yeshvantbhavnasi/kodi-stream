"""Cross-device sync for Continue Watching, My List, and Likes via the Mac mini Stream backend."""
import json
import threading
import time
from urllib.request import Request, urlopen

import ai
import db

_lock = threading.Lock()
_last_sync = 0.0


def _endpoint():
    url = (ai.service_url() or '').rstrip('/')
    if not url:
        return None, None
    return url + '/api/sync/state', ai.service_key()


def _export_local():
    watched, mylist, ratings = [], [], []
    with db._connect() as conn:
        for r in conn.execute(
            'SELECT key, name, label, series, item, parent, grp, played_at, plays, position, duration, done '
            'FROM watched ORDER BY played_at DESC LIMIT 120'
        ).fetchall():
            watched.append({
                'key': r['key'], 'name': r['name'], 'label': r['label'], 'series': r['series'],
                'item': r['item'], 'parent': r['parent'], 'grp': r['grp'],
                'last_played': r['played_at'] or 0, 'plays': r['plays'] or 1,
                'position': r['position'] or 0, 'duration': r['duration'] or 0, 'done': r['done'] or 0,
            })
        for r in conn.execute('SELECT name, item, parent, grp, added_at FROM mylist ORDER BY added_at DESC LIMIT 150').fetchall():
            mylist.append({
                'name': r['name'], 'item': r['item'], 'parent': r['parent'], 'grp': r['grp'], 'added_at': r['added_at'] or 0,
            })
        for r in conn.execute('SELECT name, item, parent, grp, value, rated_at FROM ratings ORDER BY rated_at DESC LIMIT 200').fetchall():
            ratings.append({
                'name': r['name'], 'item': r['item'], 'parent': r['parent'], 'grp': r['grp'],
                'value': r['value'] or 0, 'rated_at': r['rated_at'] or 0,
            })
    return watched, mylist, ratings


def _import_remote(data):
    changed = False
    with db._connect() as conn:
        for w in (data.get('watched') or []):
            k = w.get('key')
            if not k or not w.get('item'):
                continue
            cur = conn.execute('SELECT played_at, position FROM watched WHERE key=?', (k,)).fetchone()
            remote_at = w.get('last_played') or 0
            if cur is None or remote_at > (cur['played_at'] or 0) + 1:
                conn.execute(
                    'INSERT OR REPLACE INTO watched (key, name, label, series, item, parent, grp, played_at, plays, position, duration, done) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (k, w.get('name') or '', w.get('label') or '', w.get('series') or '',
                     w['item'], w.get('parent') or '', w.get('grp') or '',
                     remote_at, w.get('plays') or 1, w.get('position') or 0, w.get('duration') or 0, w.get('done') or 0),
                )
                changed = True
        for m in (data.get('mylist') or []):
            name = m.get('name')
            if not name or not m.get('item'):
                continue
            cur = conn.execute('SELECT added_at FROM mylist WHERE LOWER(name)=LOWER(?)', (name,)).fetchone()
            if cur is None:
                conn.execute(
                    'INSERT OR REPLACE INTO mylist (name, item, parent, grp, added_at) VALUES (?,?,?,?,?)',
                    (name, m['item'], m.get('parent') or '', m.get('grp') or '', m.get('added_at') or time.time()),
                )
                changed = True
        for r in (data.get('ratings') or []):
            name = r.get('name')
            if not name or not r.get('item'):
                continue
            cur = conn.execute('SELECT rated_at FROM ratings WHERE LOWER(name)=LOWER(?)', (name,)).fetchone()
            if cur is None or (r.get('rated_at') or 0) > (cur['rated_at'] or 0) + 1:
                conn.execute(
                    'INSERT OR REPLACE INTO ratings (name, item, parent, grp, value, rated_at) VALUES (?,?,?,?,?,?)',
                    (name, r['item'], r.get('parent') or '', r.get('grp') or '', r.get('value') or 0, r.get('rated_at') or time.time()),
                )
                changed = True
    return changed


def sync_now(profile_name=None, force=False):
    global _last_sync
    with _lock:
        if not force and time.time() - _last_sync < 20:
            return False
        _last_sync = time.time()
    url, key = _endpoint()
    if not url:
        return False
    prof = (profile_name or db.ACTIVE or 'default').strip().lower() or 'default'
    watched, mylist, ratings = _export_local()
    payload = json.dumps({
        'profile': prof, 'watched': watched, 'mylist': mylist, 'ratings': ratings,
    }).encode('utf-8')
    hdrs = {'Content-Type': 'application/json', 'User-Agent': 'StreamKodi/2.1'}
    if key:
        hdrs['X-Stream-Key'] = key
    try:
        req = Request(url, data=payload, headers=hdrs, method='POST')
        with urlopen(req, timeout=4.0) as resp:
            merged = json.loads(resp.read().decode('utf-8'))
        if merged.get('ok'):
            return _import_remote(merged)
    except Exception:
        pass
    return False


def sync_async(profile_name=None, force=False, on_change=None):
    def _run():
        changed = sync_now(profile_name=profile_name, force=force)
        if changed and on_change:
            try:
                on_change()
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True).start()
