#!/usr/bin/env python3
"""Stream's recommendation service: a small HTTP server for a Mac (or any machine) in the home that asks a local
model (Gemma through Ollama) to pick titles for each Kodi box. It keeps the last answer for every profile and
rebuilds it in the background, so a box gets its "Recommended for You" row at once and never waits for the model.
Searches are answered on the spot and remembered, so a repeated query is instant. Standard library only.

    POST /pick    {"profile": "living-room/default", "kind": "recommend" | "search", "system": "...", "intro": "...",
                   "count": 15, "candidates": [{"id": "c0", "name": "Sardar 2 (2026)", "text": "..."}]}
               -> {"picks": [{"name": "Sardar 2 (2026)", "reason": "..."}], "model": "gemma3:4b",
                   "fresh": true | false, "took": 12.3, "building": true | false}
    GET  /health  -> {"ok": true, "model": "gemma3:4b", "ollama": true, "batches": 3}
    GET  /top10   -> India's streaming charts merged into one ranking (see charts.py), rebuilt every 12 hours:
                   {"at": 1791855969, "sources": {"netflix": 20, ...}, "titles": [{"title", "kind", "lang", "year",
                   "tmdb", "score", "ranks": {"netflix": 1, "tmdb": 5}}]}
    GET  /         a web page with one tab per box: each profile's current picks with the reasons, and recent searches
                   (?device=<box> picks the tab; plain markup that the Kindle's browser draws well)
    GET  /text     a plain-text page: every box and profile's current picks, the reasons, and what the model was told
    GET  /batches  the same as JSON; GET /history the past batches, newest first
    POST /feedback profile, name, verdict (good | bad | clear) from the web page's buttons; GET /feedback lists them
    POST /lists   a list of titles from another bot or person, shown on the boxes as a row of its own:
                   {"pin": "...", "name": "weekend", "title": "Weekend Picks", "by": "Claude", "profile": "",
                    "items": [{"title": "Sardar 2", "year": "2026", "kind": "movie", "lang": "te", "reason": "..."}]}
                   The pin is POST_PIN (or ?pin= / X-Stream-Pin); profile "" means every box, otherwise one box's
                   profile key as /batches shows it. Posting a name again replaces that list; DELETE /lists?name=
                   removes one. GET /lists?profile=<key> is what a box asks for: {"lists": [...]} newest first.
                   A list holds at most 20 items; the boxes show the posted titles as their Top Picks row.
    POST /rows    from a box: every row it is showing with the titles, kept per profile; GET /rows?profile= (key, or
                   ?pin=) returns them, and /context carries them per profile as "showing".
    POST /related {"pin", "title": "Romanchakam", "by", "items": [...]} titles like one title, from a bot: they lead
                   More like this on that title's page and its Because you watched row. GET /related lists them all;
                   /context names the titles that want one (related_wanted / related_missing).
    POST /catalogue from a box: the titles its index holds, newest first per language and kind. GET /titles
                   (key, or ?pin=) with lang, kind, days and limit lists them across boxes; /context carries the
                   newest per language as "catalogue".
    GET  /cricket  live and upcoming cricket matches from ESPN's public feed (see cricket.py), refreshed every 30
                   minutes: {"at", "matches": [{"name", "abbr", "series", "start", "state", "status", "india", "major"}]}
    GET  /context  (pin) what a posting bot needs: the current list, whether anything changed since it was posted
                   (needs_update), and per box profile what the household told us and watched (as the box sent it),
                   the catalogue titles it offered (names to use verbatim) and the web page's good/bad verdicts.
    GET  /login    a sign-in form for the pages above when TOKEN is set; the key is checked once and a browser keeps a
                   cookie (a hash of the key, never the key itself); GET /logout forgets it

Settings come from the environment: PORT (8770), BIND (addresses to listen on, comma separated; 127.0.0.1 by
default, add the machine's Tailscale address so the other boxes on the tailnet can reach it), OLLAMA_URL
(http://127.0.0.1:11434), OLLAMA_MODEL (gemma3:4b), STATE (the SQLite file the batches are kept in), EMBED_ORIGINS
(origins allowed to show the web page in a frame and be told its sign-in, e.g. the Kindle home screen) and TOKEN (when
set, every request except /health must carry it, as an X-Stream-Key header or ?key=; needed once the service is
reachable from outside the home). With Serve enabled on the tailnet, `tailscale serve --bg 8770` publishes it as
https://<machine>.<tailnet>.ts.net; `tailscale funnel --bg 8770` (Funnel enabled) makes that address public, so TV
boxes that are not on the tailnet can reach it too.

Storage: one SQLite file. `batches` holds the current batch per box, profile and kind (the only thing a box ever
asks for), and `history` keeps the last 48 batches per key so the page can show how the picks moved; a batch
identical to the previous one is not written again.
"""
import hashlib
import hmac
import html
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import charts
import cricket

PORT = int(os.environ.get('PORT', '8770'))
BIND = [h.strip() for h in os.environ.get('BIND', '127.0.0.1').split(',') if h.strip()]
OLLAMA = os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
MODEL = os.environ.get('OLLAMA_MODEL', 'gemma3:4b')
STATE = os.environ.get('STATE', os.path.expanduser('~/Library/Application Support/stream-recs/batches.db'))
TOKEN = os.environ.get('TOKEN', '').strip()
POST_PIN = os.environ.get('POST_PIN', '').strip() or TOKEN  # what a bot must send to post a list
EMBED_ORIGINS = [o.strip().rstrip('/') for o in os.environ.get('EMBED_ORIGINS', '').split(',') if o.strip()]  # pages that may frame /
SESSION = hashlib.sha256(('stream-recs-web:' + TOKEN).encode()).hexdigest() if TOKEN else ''  # the browser cookie
KEEP = 7 * 24 * 3600  # a remembered search answer is kept this long
HISTORY = 48          # past batches kept per box, profile and kind

LOCK = threading.Lock()        # guards BATCHES and BUILDING
MODEL_LOCK = threading.Lock()  # one question to the model at a time
BATCHES = {}                   # key -> {'digest', 'picks', 'at', 'model', 'took'}
BUILDING = set()               # keys being rebuilt right now
TOP10 = {}                     # the merged charts, see charts.build()
TOP10_FILE = os.path.join(os.path.dirname(STATE), 'top10.json')
TOP10_EVERY = 12 * 3600
CRICKET = {}                   # live and upcoming matches, see cricket.build()
CRICKET_FILE = os.path.join(os.path.dirname(STATE), 'cricket.json')
CRICKET_EVERY = 30 * 60


def log(msg):
    sys.stdout.write('{0} {1}\n'.format(time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    sys.stdout.flush()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS batches (key TEXT PRIMARY KEY, profile TEXT, kind TEXT, digest TEXT, at REAL, model TEXT,
    took REAL, count INTEGER, system TEXT, intro TEXT, candidates TEXT, picks TEXT);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, at REAL, model TEXT, took REAL, picks TEXT);
CREATE INDEX IF NOT EXISTS history_key ON history (key, at);
CREATE TABLE IF NOT EXISTS feedback (profile TEXT, name TEXT, verdict TEXT, at REAL, PRIMARY KEY (profile, name));
CREATE TABLE IF NOT EXISTS lists (name TEXT PRIMARY KEY, title TEXT, by TEXT, profile TEXT, at REAL, items TEXT, exclude TEXT);
CREATE TABLE IF NOT EXISTS shown (profile TEXT PRIMARY KEY, at REAL, rows TEXT, recent TEXT);
CREATE TABLE IF NOT EXISTS related (key TEXT PRIMARY KEY, title TEXT, by TEXT, at REAL, items TEXT);
CREATE TABLE IF NOT EXISTS catalogue (profile TEXT PRIMARY KEY, at REAL, titles TEXT);
CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, profile TEXT, version TEXT,
    platform TEXT, event TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS telemetry_at ON telemetry (at);
CREATE INDEX IF NOT EXISTS telemetry_profile ON telemetry (profile, at);
CREATE TABLE IF NOT EXISTS page_cache (url TEXT PRIMARY KEY, at REAL, items TEXT);
CREATE TABLE IF NOT EXISTS meta_cache (key TEXT PRIMARY KEY, at REAL, data TEXT);
'''
JSON_COLUMNS = ('candidates', 'picks')


def connect():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    conn = sqlite3.connect(STATE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def load():
    """Read every current batch into memory; batches saved by the JSON-file version are taken over once."""
    global BATCHES
    with connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.executescript(SCHEMA)
        if 'exclude' not in [r['name'] for r in conn.execute('PRAGMA table_info(lists)').fetchall()]:
            conn.execute('ALTER TABLE lists ADD COLUMN exclude TEXT')
        if 'recent' not in [r['name'] for r in conn.execute('PRAGMA table_info(shown)').fetchall()]:
            conn.execute('ALTER TABLE shown ADD COLUMN recent TEXT')
        rows = conn.execute('SELECT * FROM batches').fetchall()
    BATCHES = {}
    for r in rows:
        entry = {k: r[k] for k in r.keys() if k != 'key'}
        for col in JSON_COLUMNS:
            entry[col] = json.loads(entry[col] or '[]')
        BATCHES[r['key']] = entry
    old = STATE.rsplit('.', 1)[0] + '.json'
    if not BATCHES and os.path.exists(old):
        try:
            with open(old) as f:
                BATCHES = json.load(f)
            for key in BATCHES:
                save(key)
            os.rename(old, old + '.imported')
            log('took over {0} batches from {1}'.format(len(BATCHES), old))
        except Exception as e:
            log('could not import {0}: {1}'.format(old, e))


def save(key):
    """Write one batch (call with LOCK held). The history row is skipped when the picks did not change."""
    b = BATCHES[key]
    columns = ('profile', 'kind', 'digest', 'at', 'model', 'took', 'count', 'system', 'intro', 'candidates', 'picks')
    values = [json.dumps(b.get(c)) if c in JSON_COLUMNS else b.get(c) for c in columns]
    with connect() as conn:
        conn.execute('INSERT OR REPLACE INTO batches (key, {0}) VALUES (?{1})'.format(', '.join(columns), ', ?' * len(columns)), [key] + values)
        last = conn.execute('SELECT picks FROM history WHERE key=? ORDER BY at DESC LIMIT 1', (key,)).fetchone()
        if not last or last['picks'] != json.dumps(b.get('picks')):
            conn.execute('INSERT INTO history (key, at, model, took, picks) VALUES (?,?,?,?,?)',
                         (key, b.get('at'), b.get('model'), b.get('took'), json.dumps(b.get('picks'))))
            conn.execute('DELETE FROM history WHERE key=? AND id NOT IN (SELECT id FROM history WHERE key=? ORDER BY at DESC LIMIT ?)',
                         (key, key, HISTORY))


def forget(key):
    with connect() as conn:
        conn.execute('DELETE FROM batches WHERE key=?', (key,))
        conn.execute('DELETE FROM history WHERE key=?', (key,))


def runs(key):
    with connect() as conn:
        return conn.execute('SELECT COUNT(*) FROM history WHERE key=?', (key,)).fetchone()[0]


def feedback_for(profile):
    """{title: 'good' | 'bad'}: what the household said about earlier picks for a box's profile, on the web page."""
    with connect() as conn:
        return {r['name']: r['verdict'] for r in conn.execute('SELECT name, verdict FROM feedback WHERE profile=?', (profile,))}


def all_feedback():
    with connect() as conn:
        return [dict(r) for r in conn.execute('SELECT profile, name, verdict, at FROM feedback ORDER BY at DESC')]


def rate(profile, name, verdict):
    """Remember a verdict ('good' or 'bad'); anything else clears it. The next batch for that profile is rebuilt
    with it (the rating is part of the request digest)."""
    with connect() as conn:
        if verdict in ('good', 'bad'):
            conn.execute('INSERT OR REPLACE INTO feedback (profile, name, verdict, at) VALUES (?,?,?,?)', (profile, name, verdict, time.time()))
        else:
            conn.execute('DELETE FROM feedback WHERE profile=? AND name=?', (profile, name))


# ---------- lists posted by other bots and people ----------

LIST_ITEMS = 20  # a list is the day's picks: twenty at a time


def save_list(name, title, by, profile, items, exclude=()):
    """Keep (or replace) a posted list. Items are trimmed to title, year, kind, lang and reason; exclude is a list of
    titles the boxes must leave out of every row."""
    excluded = [str(t)[:200].strip() for t in list(exclude or [])[:LIST_ITEMS * 2] if str(t).strip()]
    clean = []
    for item in items[:LIST_ITEMS]:
        if not isinstance(item, dict) or not str(item.get('title') or '').strip():
            continue
        clean.append({'title': str(item.get('title'))[:200].strip(), 'year': str(item.get('year') or '')[:4],
                      'kind': str(item.get('kind') or '')[:10].lower(), 'lang': str(item.get('lang') or '')[:20].lower(),
                      'reason': str(item.get('reason') or '')[:200].strip()})
    with connect() as conn:
        conn.execute('INSERT OR REPLACE INTO lists (name, title, by, profile, at, items, exclude) VALUES (?,?,?,?,?,?,?)',
                     (name, title, by, profile, time.time(), json.dumps(clean), json.dumps(excluded)))
    return len(clean)


def plain(text):
    """'nayyinavelli' from 'Nayyi Navelli (2026) PreDVD': the key related lists are filed under."""
    import re
    text = re.sub(r'\(\d{4}\).*$', '', text or '')
    text = re.sub(r'\b(PreDVD|Pre-DVD|DVDScr|HDCAM|HDTS|DVDRip|HDRip|WEBRip|WEB-DL|BluRay|4K|UHD)\b', '', text, flags=re.I)
    return re.sub(r'[^a-z0-9]', '', text.lower())


def save_related(title, by, items):
    """Keep (or replace) the titles a bot says are like one title."""
    clean = []
    for item in list(items or [])[:LIST_ITEMS]:
        if not isinstance(item, dict) or not str(item.get('title') or '').strip():
            continue
        clean.append({'title': str(item.get('title'))[:200].strip(), 'year': str(item.get('year') or '')[:4],
                      'kind': str(item.get('kind') or '')[:10].lower(), 'lang': str(item.get('lang') or '')[:20].lower(),
                      'reason': str(item.get('reason') or '')[:200].strip()})
    with connect() as conn:
        conn.execute('INSERT OR REPLACE INTO related (key, title, by, at, items) VALUES (?,?,?,?,?)',
                     (plain(title), title[:200], by, time.time(), json.dumps(clean)))
    return len(clean)


def related_all():
    """{plain title: {'title', 'by', 'at', 'items'}} for every related list posted."""
    with connect() as conn:
        rows = conn.execute('SELECT * FROM related ORDER BY at DESC').fetchall()
    return {r['key']: {'title': r['title'], 'by': r['by'], 'at': r['at'], 'items': json.loads(r['items'] or '[]')} for r in rows}


def save_catalogue(profile, titles):
    """The titles a box's catalogue index holds (newest first per language and kind), as the box sent them."""
    clean = []
    for t in list(titles or [])[:5000]:
        if not isinstance(t, dict) or not t.get('name'):
            continue
        clean.append({'name': str(t['name'])[:200], 'kind': str(t.get('kind') or '')[:20], 'lang': str(t.get('lang') or '')[:30],
                      'year': str(t.get('year') or '')[:4], 'added': str(t.get('added') or '')[:10],
                      'rating': t.get('rating') or 0, 'genres': [str(g)[:30] for g in list(t.get('genres') or [])[:4]],
                      'plot': str(t.get('plot') or '')[:160]})
    with connect() as conn:
        conn.execute('INSERT OR REPLACE INTO catalogue (profile, at, titles) VALUES (?,?,?)', (profile, time.time(), json.dumps(clean)))
    return len(clean)


LANG_NAMES = {'te': 'telugu', 'hi': 'hindi', 'en': 'english', 'ta': 'tamil', 'ml': 'malayalam', 'kn': 'kannada', 'mr': 'marathi',
              'gu': 'gujarati', 'pa': 'punjabi', 'bn': 'bengali'}


def catalogue_titles(lang='', kind='', days=0, limit=200, query=''):
    """Titles across every box's index, newest first, one entry per title and language. lang is a code or a name,
    kind 'movie' or 'show', days limits to arrivals in the last N days, query filters by title substring."""
    with connect() as conn:
        rows = conn.execute('SELECT titles FROM catalogue').fetchall()
    want_lang = LANG_NAMES.get((lang or '').lower(), (lang or '').lower())
    want_kind = {'movie': 'movie', 'movies': 'movie', 'show': 'show', 'shows': 'show', 'tv': 'show'}.get((kind or '').lower(), '')
    want_q = plain(query) if query else ''
    since = time.strftime('%Y-%m-%d', time.localtime(time.time() - days * 86400)) if days else ''
    seen, out = set(), []
    for r in rows:
        for t in json.loads(r['titles'] or '[]'):
            p = plain(t['name'])
            key = (p, (t.get('lang') or '').lower())
            if key in seen:
                continue
            if want_lang and not (t.get('lang') or '').lower().startswith(want_lang):
                continue
            if want_kind and t.get('kind') != want_kind:
                continue
            if since and (t.get('added') or '') < since:
                continue
            if want_q and want_q not in p and (query or '').lower() not in (t['name'] or '').lower():
                continue
            seen.add(key)
            out.append(t)
    out.sort(key=lambda t: (t.get('added') or '', t.get('year') or ''), reverse=True)
    return out[:limit]


def save_shown(profile, rows, recent=()):
    """What a box is showing right now: every row with its titles, as the box built them, and the titles the
    household played last (so a bot knows which titles want a related list)."""
    clean = []
    for row in list(rows or [])[:40]:
        if not isinstance(row, dict) or not row.get('title'):
            continue
        items = [{'name': str(i.get('name') or '')[:200], 'reason': str(i.get('reason') or '')[:200],
                  'kind': str(i.get('kind') or '')[:20], 'lang': str(i.get('lang') or '')[:30]}
                 for i in list(row.get('items') or [])[:60] if isinstance(i, dict) and i.get('name')]
        clean.append({'title': str(row['title'])[:80], 'items': items})
    played = [str(t)[:200] for t in list(recent or [])[:12] if str(t).strip()]
    with connect() as conn:
        conn.execute('INSERT OR REPLACE INTO shown (profile, at, rows, recent) VALUES (?,?,?,?)', (profile, time.time(), json.dumps(clean), json.dumps(played)))
    return len(clean)


def shown_for(profile=''):
    """{profile: {'at', 'rows'}} for every box (or one)."""
    with connect() as conn:
        rows = conn.execute('SELECT * FROM shown' + (' WHERE profile=?' if profile else ''), (profile,) if profile else ()).fetchall()
    return {r['profile']: {'at': r['at'], 'rows': json.loads(r['rows'] or '[]'), 'recent': json.loads(r['recent'] or '[]')} for r in rows}


# ---------- observability: interactions, latency and hiccups across boxes ----------

TELEMETRY_KEEP = 30 * 24 * 3600
TELEMETRY_MAX = 50000


def save_telemetry(profile, version, platform, events):
    """Store interaction and hiccup events from a Kodi box; prune old rows so the database stays compact."""
    now = time.time()
    rows = []
    for e in list(events or [])[:300]:
        if not isinstance(e, dict) or not e.get('event'):
            continue
        at = float(e.get('at') or now)
        data = e.get('data') if isinstance(e.get('data'), dict) else {}
        rows.append((at, profile[:200], str(version or '')[:20], str(platform or '')[:20],
                     str(e['event'])[:60], json.dumps(data, ensure_ascii=False)[:4000]))
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany('INSERT INTO telemetry (at, profile, version, platform, event, data) VALUES (?,?,?,?,?,?)', rows)
        conn.execute('DELETE FROM telemetry WHERE at < ?', (now - TELEMETRY_KEEP,))
        conn.execute('DELETE FROM telemetry WHERE id NOT IN (SELECT id FROM telemetry ORDER BY id DESC LIMIT ?)', (TELEMETRY_MAX,))
    return len(rows)


def query_telemetry(profile='', event='', hours=24, limit=200):
    """Recent interaction events across boxes (newest first)."""
    since = time.time() - max(1, hours) * 3600
    sql = 'SELECT * FROM telemetry WHERE at >= ?'
    params = [since]
    if profile:
        sql += ' AND profile LIKE ?'
        params.append('%' + profile + '%')
    if event:
        sql += ' AND event = ?'
        params.append(event)
    sql += ' ORDER BY at DESC, id DESC LIMIT ?'
    params.append(min(max(1, limit), 1000))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{'at': r['at'], 'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['at'])),
             'profile': r['profile'], 'version': r['version'], 'platform': r['platform'],
             'event': r['event'], 'data': json.loads(r['data'] or '{}')} for r in rows]


def hiccup_summary(hours=24):
    """Observability rollup: per-box activity, index hit rates, latency averages, and every hiccup in the window."""
    events = query_telemetry(hours=hours, limit=1000)
    devices = {}
    counts = {}
    hiccups = []
    latencies = {}
    sources = {}
    for e in events:
        prof = e['profile']
        d = devices.setdefault(prof, {'version': e['version'], 'platform': e['platform'], 'events': 0, 'last_seen': e['time'], 'hiccups': 0})
        d['events'] += 1
        ev = e['event']
        counts[ev] = counts.get(ev, 0) + 1
        data = e['data']
        ms = data.get('ms') or data.get('startup_ms')
        if isinstance(ms, (int, float)) and ms > 0:
            latencies.setdefault(ev, []).append(ms)
        if ev == 'grid_loaded' and data.get('source'):
            src = data['source']
            sources[src] = sources.get(src, 0) + 1
        is_hiccup = (ev in ('error', 'previous_session_ended_unexpectedly', 'play_timeout', 'load_failed',
                            'fetch_failed', 'slow_fetch', 'empty_listing')
                     or (ev == 'play_ended' and data.get('ended') == 'broken')
                     or (ev in ('tab_loaded', 'grid_loaded', 'search') and isinstance(ms, (int, float)) and ms >= 3000))
        if is_hiccup:
            d['hiccups'] += 1
            if len(hiccups) < 50:
                hiccups.append(e)
    avg_ms = {k: {'avg_ms': int(sum(v) / len(v)), 'max_ms': int(max(v)), 'count': len(v)} for k, v in latencies.items()}
    return {'hours': hours, 'total_events': len(events), 'devices': devices, 'event_counts': counts,
            'grid_sources': sources, 'latency': avg_ms, 'hiccup_count': sum(d['hiccups'] for d in devices.values()),
            'recent_hiccups': hiccups}


def delete_list(name):
    with connect() as conn:
        conn.execute('DELETE FROM lists WHERE name=?', (name,))


def lists_for(profile=''):
    """Lists posted for everyone plus those for one box's profile, newest first."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM lists WHERE profile='' OR profile=? ORDER BY at DESC", (profile or '',)).fetchall()
    return [{'name': r['name'], 'title': r['title'], 'by': r['by'], 'profile': r['profile'], 'at': r['at'],
             'items': json.loads(r['items'] or '[]'), 'exclude': json.loads(r['exclude'] or '[]')} for r in rows]


def context_for_bot():
    """Everything a posting bot needs, built from what the boxes sent and the web page's verdicts."""
    with LOCK:
        batches = [(key, dict(b)) for key, b in BATCHES.items() if (b.get('kind') or 'recommend') == 'recommend']
    posted = lists_for()
    current = posted[0] if posted else None
    latest = 0
    profiles = []
    shown = shown_for()
    for key, b in sorted(batches, key=lambda kv: -kv[1].get('at', 0)):
        profile = b.get('profile') or key.split('|')[0]
        intro = (b.get('intro') or '').strip()
        profiles.append({'profile': profile, 'at': b.get('at', 0), 'about': intro[:12000],
                         'candidates': list(b.get('candidates') or [])[:100], 'feedback': feedback_for(profile),
                         'showing': shown.get(profile, {}).get('rows', []), 'showing_at': shown.get(profile, {}).get('at', 0)})
        latest = max(latest, b.get('at', 0))
    for profile, s in shown.items():
        if not any(p['profile'] == profile for p in profiles):
            profiles.append({'profile': profile, 'at': s['at'], 'about': '', 'candidates': [], 'feedback': feedback_for(profile),
                             'showing': s['rows'], 'showing_at': s['at']})
    with connect() as conn:
        row = conn.execute('SELECT MAX(at) AS at FROM feedback').fetchone()
    latest = max(latest, (row['at'] if row and row['at'] else 0))
    # Titles that want a 'titles like this' list: what the boxes played last and the posted top picks.
    related = related_all()
    wanted, seen = [], set()
    for s in shown.values():
        for title in s.get('recent') or []:
            if plain(title) not in seen:
                seen.add(plain(title))
                wanted.append(title)
    for item in (current or {}).get('items') or []:
        if plain(item['title']) not in seen:
            seen.add(plain(item['title']))
            wanted.append(item['title'])
    missing = [t for t in wanted if plain(t) not in related]
    # The catalogue, compact: per language the newest movies and shows, so a bot picks titles that exist.
    catalogue = {}
    for code, name in (('te', 'Telugu'), ('hi', 'Hindi'), ('en', 'English'), ('ta', 'Tamil'), ('ml', 'Malayalam')):
        movies = catalogue_titles(code, 'movie', 0, 60)
        shows = catalogue_titles(code, 'show', 0, 30)
        if movies or shows:
            catalogue[name] = {'movies': [{'name': t['name'], 'added': t.get('added', ''), 'rating': t.get('rating') or 0, 'genres': t.get('genres') or []} for t in movies],
                               'shows': [{'name': t['name'], 'added': t.get('added', ''), 'rating': t.get('rating') or 0, 'genres': t.get('genres') or []} for t in shows]}
    needs = current is None or time.time() - current['at'] > 20 * 3600 or latest > current['at'] or bool(missing)
    return {'generated': time.time(), 'list': current, 'needs_update': needs,
            'reason': ('no list posted yet' if current is None else 'the list is a day old' if time.time() - current['at'] > 20 * 3600
                       else 'new activity since the list was posted' if latest > current['at']
                       else 'some titles have no related list yet' if missing else 'nothing changed since the list was posted'),
            'related_wanted': wanted[:30], 'related_missing': missing[:30], 'catalogue': catalogue,
            'cricket': [{'match': m['name'], 'abbr': m['abbr'], 'series': m['series'], 'start': m['start'],
                         'start_text': time.strftime('%a %d %b %H:%M UTC', time.gmtime(m['start'])), 'state': m['state'], 'status': m['status'],
                         'india': m['india'], 'major': m['major']} for m in cricket.soon(CRICKET, 48, 15)],
            'catalogue_note': 'newest first per language; GET /titles?pin=…&lang=te&kind=movie&days=30&limit=200 for more',
            'related_posted': [{'title': r['title'], 'by': r['by'], 'at': r['at'], 'count': len(r['items'])} for r in related.values()],
            'profiles': profiles}


def guidance(req):
    """What to tell the model about rated picks, and which candidates to leave out."""
    rated = feedback_for(req.get('profile', 'default'))
    good = [n for n, v in rated.items() if v == 'good']
    bad = [n for n, v in rated.items() if v == 'bad']
    text = ''
    if good:
        text += ' They marked these earlier picks as good: ' + '; '.join(sorted(good)[:25]) + '. Favour titles like these.'
    if bad:
        text += ' They marked these as not for them: ' + '; '.join(sorted(bad)[:25]) + '. Never pick those, and avoid anything much like them.'
    return ('\n\nThe viewer rated earlier picks on a screen at home.' + text) if text else '', set(bad)


def ask_model(system, prompt, max_tokens=4000):
    body = {'model': MODEL, 'stream': False, 'format': 'json', 'keep_alive': '30m',
            'options': {'temperature': 0.2, 'num_ctx': 12288, 'num_predict': max_tokens},
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]}
    req = urllib.request.Request(OLLAMA + '/api/chat', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=600) as r:
        reply = json.load(r)
    return (reply.get('message') or {}).get('content', '')


def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA + '/api/tags', timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def digest_of(req):
    parts = [req.get('system', ''), req.get('intro', ''), int(req.get('count', 15)),
             [(c.get('id'), c.get('name'), c.get('text')) for c in req.get('candidates', [])],
             sorted(feedback_for(req.get('profile', 'default')).items())]
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def build(req):
    """Ask the model and return [{'name', 'reason'}] (names, not positional ids, so a remembered answer stays valid)."""
    candidates = req.get('candidates', [])
    count = int(req.get('count', 15))
    advice, unwanted = guidance(req)
    if unwanted and req.get('kind', 'recommend') != 'search':
        candidates = [c for c in candidates if c.get('name') not in unwanted] or candidates
    names = {c['id']: c['name'] for c in candidates}
    listing = '\n'.join('{0}: {1}'.format(c['id'], c['text']) for c in candidates)
    prompt = ('{0}{3}\n\nCandidates:\n{1}\n\nPick the {2} best candidates, best first. Reply with JSON only, shaped as '
              '{{"picks": [{{"id": "c0", "reason": "why, for the viewer, in at most ten words"}}]}}. Use only ids from the list.'
              ).format(req.get('intro', ''), listing, count, advice)
    with MODEL_LOCK:
        started = time.time()
        text = ask_model(req.get('system', ''), prompt)
        took = time.time() - started
    start, end = text.find('{'), text.rfind('}')
    picks = json.loads(text[start:end + 1]).get('picks', []) if start >= 0 else []
    out = [{'name': names[p['id']], 'reason': str(p.get('reason', ''))[:160]} for p in picks if p.get('id') in names]
    return out[:count], took


def entry_for(req, digest, picks, took):
    """A stored batch: the picks plus what the model was told, so the page can explain them."""
    return {'digest': digest, 'picks': picks, 'at': time.time(), 'model': MODEL, 'took': round(took, 1),
            'kind': req.get('kind', 'recommend'), 'profile': req.get('profile', 'default'), 'count': int(req.get('count', 15)),
            'system': req.get('system', '')[:2000], 'intro': (req.get('intro', '') + guidance(req)[0])[:12000],
            'candidates': [c.get('name', '') for c in req.get('candidates', [])][:100]}


def rebuild(key, req, digest):
    try:
        picks, took = build(req)
        with LOCK:
            BATCHES[key] = entry_for(req, digest, picks, took)
            save(key)
        log('{0}: {1} picks in {2:.1f}s, for {3}'.format(key, len(picks), took, req.get('from', '?')))
    except Exception as e:
        log('{0}: build failed: {1}'.format(key, e))
    finally:
        with LOCK:
            BUILDING.discard(key)


def pick(req):
    kind = req.get('kind', 'recommend')
    key = '{0}|{1}'.format(req.get('profile', 'default'), kind)
    if kind == 'search':
        key += '|' + hashlib.sha1(req.get('intro', '').encode()).hexdigest()[:12]
    digest = digest_of(req)
    with LOCK:
        have = BATCHES.get(key)
        if have and (have['digest'] == digest or (kind == 'search' and time.time() - have['at'] < KEEP)):
            log('{0}: served as is, for {1}'.format(key, req.get('from', '?')))
            if 'intro' not in have:  # a batch saved by an earlier version: keep its picks, add the explanation
                have.update({k: v for k, v in entry_for(req, digest, have['picks'], have.get('took', 0)).items() if k not in ('at', 'picks', 'took')})
                save(key)
            return dict(have, fresh=True, building=False)
        serve_old = have if kind == 'recommend' else None
        if serve_old and key in BUILDING:
            return dict(serve_old, fresh=False, building=True)
        if serve_old:
            BUILDING.add(key)
    if serve_old:
        # The box gets the last batch now; the model works on the new one for next time.
        threading.Thread(target=rebuild, args=(key, req, digest), daemon=True).start()
        return dict(serve_old, fresh=False, building=True)
    picks, took = build(req)
    entry = entry_for(req, digest, picks, took)
    with LOCK:
        BATCHES[key] = entry
        save(key)
        # Remembered searches are trimmed so the store stays small.
        for k in [k for k, v in BATCHES.items() if '|search|' in k and time.time() - v['at'] > KEEP]:
            del BATCHES[k]
            forget(k)
    log('{0}: {1} picks in {2:.1f}s (asked to wait), for {3}'.format(key, len(picks), took, req.get('from', '?')))
    return dict(entry, fresh=True, building=False)


def page():
    """A plain-text report: every box and profile's current picks with the model's reasons, and what it was told."""
    with LOCK:
        items = sorted(BATCHES.items(), key=lambda kv: -kv[1].get('at', 0))
    lines = ['STREAM RECOMMENDATIONS',
             'Model {0} through Ollama at {1}: {2}. {3} batches remembered.'.format(MODEL, OLLAMA, 'answering' if ollama_up() else 'NOT answering', len(items)),
             '']
    for posted in lists_for():
        lines.append('=' * 78)
        lines.append('List "{0}" by {1} for {2}, posted {3}, {4} titles'.format(
            posted['title'] or posted['name'], posted['by'] or '?', posted['profile'] or 'every box',
            time.strftime('%a %d %b %H:%M', time.localtime(posted['at'])), len(posted['items'])))
        for n, item in enumerate(posted['items']):
            lines.append('{0:>3}. {1} ({2}) {3} {4}'.format(n + 1, item['title'], item['year'] or '?', item['kind'], item['lang']).rstrip())
            if item['reason']:
                lines.append('     ' + item['reason'])
        if posted.get('exclude'):
            lines.append('     Excluded everywhere: ' + '; '.join(posted['exclude']))
        lines.append('')
    live = cricket.soon(CRICKET, 48, 20)
    if live:
        lines.append('=' * 78)
        lines.append('Cricket: live and in the next two days')
        for m in live:
            lines.append('  {0}  {1}  {2}  {3}'.format('LIVE ' if m['state'] == 'in' else time.strftime('%a %H:%M', time.localtime(m['start'])),
                                                     m['abbr'], m['short'], m['status']).rstrip())
        lines.append('')
    for code, name in (('te', 'Telugu'), ('hi', 'Hindi'), ('en', 'English')):
        fresh = catalogue_titles(code, 'movie', 14, 30)
        if fresh:
            lines.append('=' * 78)
            lines.append('New {0} movies in the catalogue (last two weeks)'.format(name))
            for t in fresh:
                lines.append('  {0}  added {1}{2}'.format(t['name'], t.get('added', ''), ('  ' + ', '.join(t['genres'])) if t.get('genres') else ''))
            lines.append('')
    for profile, s in sorted(shown_for().items(), key=lambda kv: -kv[1]['at']):
        lines.append('=' * 78)
        lines.append('{0}  |  Showing now ({1} rows, built {2})'.format(profile, len(s['rows']), time.strftime('%a %d %b %H:%M', time.localtime(s['at']))))
        for row in s['rows']:
            lines.append('  ' + row['title'] + ': ' + '; '.join(i['name'] for i in row['items'][:40]))
        lines.append('')
    for key, b in items:
        kind = b.get('kind') or ('search' if '|search|' in key else 'recommend')
        heading = ('Search: ' + (b.get('intro') or '').replace('The viewer asked for: ', '')[:120] if kind == 'search'
                   else 'Recommended for You' + (' (' + kind.split('-', 1)[1].title() + ' only)' if '-' in kind else ''))
        lines.append('=' * 78)
        lines.append('{0}  |  {1}'.format(b.get('profile') or key.split('|')[0], heading))
        lines.append('built {0}, {1}s, {2}, {3} picks from {4} candidates, {5} batches kept'.format(
            time.strftime('%a %d %b %H:%M', time.localtime(b.get('at', 0))), b.get('took', 0), b.get('model') or MODEL,
            len(b.get('picks') or []), len(b.get('candidates') or []) or '?', runs(key)))
        lines.append('')
        for n, p in enumerate(b.get('picks') or []):
            lines.append('{0:>3}. {1}'.format(n + 1, p.get('name', '')))
            lines.append('     {0}'.format(p.get('reason') or 'no reason given'))
        rated = feedback_for(b.get('profile') or key.split('|')[0])
        if rated:
            lines.append('')
            lines.append('Rated on the web page: good: {0}; not for them: {1}'.format(
                ', '.join(sorted(n for n, v in rated.items() if v == 'good')) or '-', ', '.join(sorted(n for n, v in rated.items() if v == 'bad')) or '-'))
        lines.append('')
        lines.append('What the model was told:')
        told = ((b.get('system') or '') + '\n\n' + (b.get('intro') or '')).strip() or 'Not recorded (built before this was kept; it fills in on the next request).'
        lines += ['    ' + l for l in told.splitlines()]
        lines.append('')
        lines.append('Candidates offered: ' + (', '.join(b.get('candidates') or []) or 'not recorded'))
        lines.append('')
    if not items:
        lines.append('No batches yet. Open Stream on a TV and the first one appears here.')
    return '\n'.join(lines) + '\n'


STYLE = '''<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body { margin: 0; padding: 14px 18px 40px; font: 17px/1.4 Helvetica, Arial, sans-serif; color: #000; background: #fff; }
h1 { font-size: 22px; margin: 0 0 10px; } h2 { font-size: 18px; margin: 22px 0 4px; border-bottom: 2px solid #000; padding-bottom: 4px; }
.tabs a { display: inline-block; padding: 9px 14px; margin: 0 6px 6px 0; border: 2px solid #000; border-radius: 10px;
  color: #000; text-decoration: none; font-weight: bold; } .tabs a.on { background: #000; color: #fff; }
.meta { font-size: 14px; color: #444; } ol { padding-left: 30px; margin: 6px 0; } li { margin: 5px 0; }
.why { display: block; font-size: 14px; color: #444; } .top { float: right; font-size: 14px; } .top a { color: #000; }
input, button { font-size: 18px; padding: 10px; border: 2px solid #000; border-radius: 10px; }
button { background: #000; color: #fff; font-weight: bold; } .err { font-weight: bold; margin: 8px 0; }
.rate { display: inline-block; margin-left: 8px; vertical-align: middle; } .rate form { display: inline; }
.rate button { font-size: 13px; padding: 3px 9px; margin: 0 2px; background: #fff; color: #000; border: 1px solid #000; border-radius: 8px; font-weight: normal; }
.rate button.on { background: #000; color: #fff; font-weight: bold; } .rated { font-size: 14px; }
</style>'''


def html_page(title, body, script=''):
    return ('<!doctype html><html><head><meta charset="utf-8"><title>{0}</title>{1}</head><body>{2}{3}</body></html>'
            .format(html.escape(title), STYLE, body, script)).encode()


def device_of(batch, key):
    """The box a batch belongs to and the profile on it: a box sends its profile as '<box>/<profile>'."""
    profile = batch.get('profile') or key.split('|')[0]
    box, _, name = profile.partition('/')
    return box or 'default', name or 'default'


def rate_buttons(profile, name, verdict, device, carry, undo_only=False):
    """Good / Not for me buttons for one title (plain forms, so the Kindle's browser can use them). Pressing the
    button that is already on clears the rating."""
    action = '/feedback?device={0}{1}'.format(quote(device), '&' + carry if carry else '')
    hidden = '<input type="hidden" name="profile" value="{0}"><input type="hidden" name="name" value="{1}">'.format(html.escape(profile, True), html.escape(name, True))
    if undo_only:
        return ' <span class="rate"><form method="post" action="{0}">{1}<button name="verdict" value="clear">Undo</button></form></span>'.format(action, hidden)
    def button(value, label):
        on = verdict == value
        return '<button name="verdict" value="{0}"{2}>{1}</button>'.format('clear' if on else value, label, ' class="on"' if on else '')
    return ' <span class="rate"><form method="post" action="{0}">{1}{2}{3}</form></span>'.format(action, hidden, button('good', 'Good'), button('bad', 'Not for me'))


def web_page(device, carry, script=''):
    """One tab per box; under it each profile's Recommended row with the reasons, then that box's recent searches.
    carry is the query string kept on every link (the Kindle tab's sign-in and embed flag)."""
    with LOCK:
        items = sorted(BATCHES.items(), key=lambda kv: -kv[1].get('at', 0))
    boxes = {}
    for key, b in items:
        box, profile = device_of(b, key)
        boxes.setdefault(box, []).append((key, profile, b))
    names = sorted(boxes, key=lambda n: -max(b.get('at', 0) for _, _, b in boxes[n]))  # most recently active first
    device = device if device in boxes else (names[0] if names else '')
    amp = '&' + carry if carry else ''
    out = ['<div class="top"><a href="/text?{0}">Full report</a> &middot; <a href="/logout?{0}">Sign out</a></div>'.format(carry) if TOKEN
           else '<div class="top"><a href="/text?{0}">Full report</a></div>'.format(carry),
           '<h1>Stream recommendations</h1>',
           '<div class="meta">{0} &middot; Ollama {1}</div>'.format(html.escape(MODEL), 'answering' if ollama_up() else 'NOT answering')]
    if not names:
        out.append('<p>No batches yet. Open Stream on a TV and its picks appear here.</p>')
        return html_page('Stream recommendations', '\n'.join(out), script)
    out.append('<div class="tabs" style="margin-top:12px">' + ''.join(
        '<a href="/?device={0}{1}"{2}>{3}</a>'.format(quote(n), amp, ' class="on"' if n == device else '', html.escape(n))
        for n in names) + '</div>')
    rows = boxes[device]
    first_profile = next((b.get('profile') or key.split('|')[0] for key, _, b in rows), device + '/default')
    # The list a bot posted leads every TV's Top Picks: shown first, numbered, with its reasons and the rating buttons.
    for posted in lists_for(first_profile):
        rated = feedback_for(first_profile)
        out.append('<h2>{0} &middot; posted by {1}</h2>'.format(html.escape(posted['title'] or posted['name']), html.escape(posted['by'] or '?')))
        out.append('<div class="meta">posted {0} &middot; {1} titles &middot; for {2} &middot; leads Top Picks on the TV</div>'.format(
            time.strftime('%a %d %b, %H:%M', time.localtime(posted['at'])), len(posted['items']), html.escape(posted['profile'] or 'every box')))
        out.append('<ol>' + ''.join('<li><b>{0}</b> <span class="meta">{1}</span>{3}<span class="why">{2}</span></li>'.format(
            html.escape(i['title']), html.escape(' '.join(x for x in (i['year'], i['kind'], i['lang']) if x)), html.escape(i['reason']),
            rate_buttons(first_profile, i['title'], rated.get(i['title']), device, carry)) for i in posted['items']) + '</ol>')
        if posted.get('exclude'):
            out.append('<p class="meta">Excluded from every row: {0}</p>'.format(html.escape('; '.join(posted['exclude']))))
    related = related_all()
    if related:
        out.append('<h2>Titles like &hellip; &middot; posted</h2><ul class="rows">' + ''.join('<li><b>{0}</b> <span class="meta">by {1}</span>: {2}</li>'.format(
            html.escape(r['title']), html.escape(r['by'] or '?'), html.escape(' · '.join(i['title'] for i in r['items']))) for r in related.values()) + '</ul>')
    # Every row each profile on this box is showing right now, as the box built it.
    for profile_key, shown in sorted(shown_for().items(), key=lambda kv: -kv[1]['at']):
        if profile_key.partition('/')[0] != device:
            continue
        out.append('<h2>{0} &middot; Showing now</h2>'.format(html.escape((profile_key.partition('/')[2] or 'default').title())))
        out.append('<div class="meta">{0} rows, built {1}</div>'.format(len(shown['rows']), time.strftime('%a %d %b, %H:%M', time.localtime(shown['at']))))
        out.append('<ul class="rows">' + ''.join('<li><b>{0}</b>: {1}</li>'.format(
            html.escape(row['title']), html.escape(' · '.join(i['name'] for i in row['items'][:40]))) for row in shown['rows']) + '</ul>')
    for key, profile, b in rows:
        kind = b.get('kind') or ('search' if '|search|' in key else 'recommend')
        if kind == 'search':
            continue
        label = 'Picks by the model' + (' (' + kind.split('-', 1)[1].title() + ' only)' if '-' in kind else '')
        out.append('<h2>{0} &middot; {1}</h2>'.format(html.escape(profile.title()), label))
        out.append('<div class="meta">built {0} &middot; {1}s &middot; {2} picks from {3} candidates</div>'.format(
            time.strftime('%a %d %b, %H:%M', time.localtime(b.get('at', 0))), b.get('took', 0),
            len(b.get('picks') or []), len(b.get('candidates') or []) or '?'))
        rated = feedback_for(b.get('profile') or key.split('|')[0])
        out.append('<ol>' + ''.join('<li><b>{0}</b>{2}<span class="why">{1}</span></li>'.format(
            html.escape(p.get('name', '')), html.escape(p.get('reason') or ''),
            rate_buttons(b.get('profile') or key.split('|')[0], p.get('name', ''), rated.get(p.get('name', '')), device, carry))
            for p in b.get('picks') or []) + '</ol>')
    rated_rows = [r for r in all_feedback() if r['profile'].partition('/')[0] == device]
    if rated_rows:
        out.append('<h2>Rated</h2><p class="meta">Good picks steer the next batch towards titles like them; Not for me keeps a title out and steers away from it. The next hourly batch uses the ratings.</p><ol class="rated">')
        for r in rated_rows[:60]:
            out.append('<li><b>{0}</b> &middot; {1} &middot; {2}{3}</li>'.format(
                html.escape(r['name']), 'good' if r['verdict'] == 'good' else 'not for me', html.escape(r['profile'].partition('/')[2] or 'default'),
                rate_buttons(r['profile'], r['name'], r['verdict'], device, carry, undo_only=True)))
        out.append('</ol>')
    searches = [(profile, b) for key, profile, b in rows if (b.get('kind') or '') == 'search' or '|search|' in key]
    if searches:
        out.append('<h2>Recent searches</h2><ol>')
        for profile, b in searches[:15]:
            asked = (b.get('intro') or '').replace('The viewer asked for: ', '').strip()[:100] or '?'
            out.append('<li><b>&ldquo;{0}&rdquo;</b> <span class="meta">{1}, {2}</span><span class="why">{3}</span></li>'.format(
                html.escape(asked), html.escape(profile), time.strftime('%a %H:%M', time.localtime(b.get('at', 0))),
                html.escape(', '.join(p.get('name', '') for p in (b.get('picks') or [])[:6]))))
        out.append('</ol>')
    return html_page('Stream recommendations', '\n'.join(out), script)


def handoff(session):
    """Tell the Kindle tab (an allowed framing page) its sign-in; an empty one makes it forget the old one."""
    return ''.join('<script>try { parent.postMessage("recs-session:%s", "%s"); } catch (e) {}</script>' % (session, o)
                   for o in EMBED_ORIGINS)


def login_page(error='', embed=False):
    return html_page('Sign in', '<h1>Stream recommendations</h1><form method="post" action="/login">'
                     '<p>Enter the service key to see each TV\'s picks.</p>{0}'
                     '<input type="password" name="key" autofocus style="width:60%"> <button>Sign in</button>'
                     '<input type="hidden" name="embed" value="{1}"></form>'.format(
                         '<div class="err">{0}</div>'.format(html.escape(error)) if error else '', '1' if embed else ''),
                     handoff('') if embed else '')


def refresh_cricket():
    """Rebuild the cricket fixtures every CRICKET_EVERY seconds; the last good list is kept on disk."""
    global CRICKET
    try:
        with open(CRICKET_FILE) as f:
            CRICKET = json.load(f)
    except (OSError, ValueError):
        pass
    while True:
        if time.time() - CRICKET.get('at', 0) >= CRICKET_EVERY - 30:
            try:
                built = cricket.build(log)
                if built['matches'] or not CRICKET:
                    CRICKET = built
                    os.makedirs(os.path.dirname(CRICKET_FILE), exist_ok=True)
                    with open(CRICKET_FILE, 'w') as f:
                        json.dump(CRICKET, f)
            except Exception as e:
                log('cricket: build failed: {0}'.format(e))
        time.sleep(120)


def refresh_top10():
    """Rebuild the merged charts now and then every TOP10_EVERY seconds; the last good list is kept on disk."""
    global TOP10
    try:
        with open(TOP10_FILE) as f:
            TOP10 = json.load(f)
    except (OSError, ValueError):
        pass
    while True:
        if time.time() - TOP10.get('at', 0) >= TOP10_EVERY - 60:
            try:
                built = charts.build(log)
                if built['titles']:
                    TOP10 = built
                    os.makedirs(os.path.dirname(TOP10_FILE), exist_ok=True)
                    with open(TOP10_FILE, 'w') as f:
                        json.dump(TOP10, f)
            except Exception as e:
                log('top10: build failed: {0}'.format(e))
        time.sleep(600)


VOICE_LOCK = threading.Lock()
VOICE_SESSIONS = {}  # code -> {'created': ts, 'text': '', 'final': False, 'seq': 0}
VOICE_TTL = 600
VOICE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _prune_voice():
    cutoff = time.time() - VOICE_TTL
    for k in [k for k, v in VOICE_SESSIONS.items() if v['created'] < cutoff]:
        VOICE_SESSIONS.pop(k, None)


def create_voice_session():
    with VOICE_LOCK:
        _prune_voice()
        for _ in range(20):
            code = ''.join(VOICE_ALPHABET[b % len(VOICE_ALPHABET)] for b in os.urandom(6))
            if code not in VOICE_SESSIONS:
                VOICE_SESSIONS[code] = {'created': time.time(), 'text': '', 'final': False, 'seq': 0}
                return code
    raise RuntimeError('voice session pool full')


def _clean_voice_code(raw):
    return ''.join(c for c in str(raw or '').upper() if c.isalnum())[:8]


def voice_page(code=''):
    safe_code = html.escape(_clean_voice_code(code))
    return ('''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Stream Voice Search</title>
<style>
  :root { --bg:#0e0e12; --card:#18181f; --border:rgba(255,255,255,.12); --accent:#e50914; --text:#f5f5f7; --muted:#9a9aa6; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { margin:0; min-height:100dvh; background:radial-gradient(circle at top,#1c1c26 0%,var(--bg) 65%); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Roboto,sans-serif; display:flex; align-items:center; justify-content:center; padding:20px; }
  .wrap { width:100%; max-width:440px; background:var(--card); border:1px solid var(--border); border-radius:26px; padding:28px 22px; box-shadow:0 24px 60px rgba(0,0,0,.55); text-align:center; }
  .brand { font-size:13px; letter-spacing:.18em; text-transform:uppercase; color:var(--muted); margin-bottom:6px; }
  h1 { margin:0 0 8px; font-size:24px; font-weight:700; }
  .sub { color:var(--muted); font-size:14px; margin:0 0 22px; }
  .langs { display:flex; gap:8px; justify-content:center; flex-wrap:wrap; margin-bottom:24px; }
  .lang { border:1px solid var(--border); background:rgba(255,255,255,.06); color:var(--text); border-radius:999px; padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; }
  .lang.on { background:#fff; color:#141414; border-color:#fff; }
  .mic { width:124px; height:124px; border-radius:50%; border:none; background:linear-gradient(145deg,#ff2a36,var(--accent)); color:#fff;
         display:inline-flex; align-items:center; justify-content:center; cursor:pointer; box-shadow:0 12px 34px rgba(229,9,20,.45); transition:transform .15s,box-shadow .2s; }
  .mic:active { transform:scale(.95); }
  .mic.live { animation:pulse 1.3s infinite; }
  @keyframes pulse { 0% { box-shadow:0 0 0 0 rgba(229,9,20,.65); } 70% { box-shadow:0 0 0 26px rgba(229,9,20,0); } 100% { box-shadow:0 0 0 0 rgba(229,9,20,0); } }
  .status { margin-top:16px; min-height:22px; font-size:14px; color:var(--muted); }
  .transcript { margin:16px 0; min-height:52px; padding:14px 16px; border-radius:16px; background:rgba(255,255,255,.05); border:1px solid var(--border); font-size:18px; font-weight:600; word-break:break-word; }
  .row { display:flex; gap:8px; margin-top:14px; }
  input[type=text] { flex:1; background:rgba(255,255,255,.07); border:1px solid var(--border); border-radius:14px; padding:13px 15px; color:#fff; font-size:16px; outline:none; }
  input[type=text]:focus { border-color:#fff; }
  .go { background:#fff; color:#141414; border:none; border-radius:14px; padding:0 18px; font-weight:700; font-size:15px; cursor:pointer; }
  .code-row { margin-top:14px; font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">Stream TV</div>
  <h1>Voice Search</h1>
  <p class="sub">Tap the mic and speak — results appear live on your TV</p>
  <div class="langs">
    <button class="lang on" data-lang="en-IN">English</button>
    <button class="lang" data-lang="te-IN">Telugu</button>
    <button class="lang" data-lang="hi-IN">Hindi</button>
  </div>
  <button id="mic" class="mic" aria-label="Start voice search">
    <svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <rect x="9" y="2" width="6" height="12" rx="3"></rect>
      <path d="M5 10v2a7 7 0 0 0 14 0v-2"></path>
      <line x1="12" y1="19" x2="12" y2="22"></line>
      <line x1="8" y1="22" x2="16" y2="22"></line>
    </svg>
  </button>
  <div id="status" class="status">Ready</div>
  <div id="transcript" class="transcript" style="color:var(--muted)">Say a movie, show, actor, or channel…</div>
  <form id="form" class="row">
    <input id="box" type="text" placeholder="Or type / dictate here…" autocomplete="off" autocorrect="off">
    <button class="go" type="submit">Send</button>
  </form>
  <div class="code-row">TV Code: <strong id="codeLabel">''' + (safe_code or '—') + '''</strong></div>
</div>
<script>
const code = "''' + safe_code + '''";
let lang = "en-IN";
let rec = null, listening = false, lastSent = "";
const mic = document.getElementById("mic"), st = document.getElementById("status"),
      tr = document.getElementById("transcript"), box = document.getElementById("box");
document.querySelectorAll(".lang").forEach(b => b.onclick = () => {
  document.querySelectorAll(".lang").forEach(x => x.classList.remove("on"));
  b.classList.add("on"); lang = b.dataset.lang;
});
async function push(text, isFinal) {
  text = (text || "").trim();
  if (!code || (!text && !isFinal)) return;
  if (!isFinal && text === lastSent) return;
  lastSent = text;
  tr.style.color = "#fff"; tr.textContent = text; box.value = text;
  try {
    const r = await fetch("/voice/send", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({code, text, final: !!isFinal})
    });
    if (r.ok) st.textContent = isFinal ? "Sent to TV!" : "Listening…";
    else st.textContent = "Session expired — press Voice on TV again";
  } catch (e) { st.textContent = "Connection error"; }
}
document.getElementById("form").onsubmit = e => { e.preventDefault(); push(box.value, true); };
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!SR) {
  st.textContent = "Use keyboard mic below to dictate";
  mic.onclick = () => box.focus();
} else {
  mic.onclick = () => {
    if (listening && rec) { rec.stop(); return; }
    rec = new SR(); rec.lang = lang; rec.interimResults = true; rec.continuous = false;
    rec.onstart = () => { listening = true; mic.classList.add("live"); st.textContent = "Listening…"; };
    rec.onresult = ev => {
      let txt = "", fin = false;
      for (let i = 0; i < ev.results.length; i++) {
        txt += ev.results[i][0].transcript;
        if (ev.results[i].isFinal) fin = true;
      }
      push(txt, fin);
    };
    rec.onerror = ev => { st.textContent = ev.error === "not-allowed" ? "Microphone permission denied" : ("Mic error: " + ev.error); };
    rec.onend = () => { listening = false; mic.classList.remove("live"); if (st.textContent === "Listening…") st.textContent = "Tap mic to speak again"; };
    rec.start();
  };
}
</script>
</body>
</html>''').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, body, headers=()):
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "frame-ancestors 'self' " + ' '.join(EMBED_ORIGINS))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where, headers=()):
        self.send_response(303)
        self.send_header('Location', where)
        self.send_header('Content-Length', '0')
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()

    def query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def signed_in(self):
        """A browser's sign-in: the cookie, or ?s= for the Kindle tab (its frame may not keep a cookie)."""
        cookies = dict(c.strip().split('=', 1) for c in self.headers.get('Cookie', '').split(';') if '=' in c)
        given = cookies.get('stream_recs', '') or self.query().get('s', '')
        return bool(given) and hmac.compare_digest(given, SESSION)

    def allowed(self, page=False):
        """True when no token is set, or the request carries it (header, ?key=, or a browser sign-in). A page asked
        for without it is sent to the sign-in form; the API answers 401."""
        if not TOKEN:
            return True
        given = self.headers.get('X-Stream-Key', '') or self.query().get('key', '')
        if (given and hmac.compare_digest(given, TOKEN)) or self.signed_in():
            return True
        if page:
            self._redirect('/login' + ('?embed=1' if self.query().get('embed') else ''))
        else:
            self._send(401, {'error': 'key required'})
        return False

    def pin_ok(self, body=None):
        """True when the request carries the posting pin (header, ?pin= or in the body), or the service key."""
        if not POST_PIN:
            return True
        given = self.headers.get('X-Stream-Pin', '') or self.query().get('pin', '') or str((body or {}).get('pin') or '')
        if given and hmac.compare_digest(given, POST_PIN):
            return True
        if TOKEN and self.allowed():
            return True
        self._send(401, {'error': 'pin required'})
        return False

    def cookie(self, value, age):
        # Through the tunnel the page is https; Secure + SameSite=None lets the Kindle's framed tab keep the cookie.
        https = self.headers.get('X-Forwarded-Proto', '') == 'https'
        return ('Set-Cookie', 'stream_recs={0}; Path=/; Max-Age={1}; HttpOnly; {2}'.format(
            value, age, 'Secure; SameSite=None' if https else 'SameSite=Lax'))

    def do_GET(self):
        if self.path.startswith('/health'):
            with LOCK:
                n = len(BATCHES)
            return self._send(200, {'ok': True, 'model': MODEL, 'ollama': ollama_up(), 'batches': n})
        if self.path.startswith('/voice/poll'):
            if not self.allowed():
                return
            code = _clean_voice_code(self.query().get('code'))
            with VOICE_LOCK:
                _prune_voice()
                sess = VOICE_SESSIONS.get(code)
                if not sess:
                    return self._send(404, {'error': 'session expired'})
                return self._send(200, {'ok': True, 'text': sess['text'], 'final': sess['final'], 'seq': sess['seq']})
        if self.path == '/voice' or self.path.startswith('/voice/'):
            tail = self.path.split('?', 1)[0].rstrip('/')
            code = tail.split('/')[-1] if tail.startswith('/voice/') else self.query().get('code', '')
            return self._html(200, voice_page(code))
        q = self.query()
        embed = q.get('embed') == '1'
        if self.path.startswith('/login'):
            return self._redirect('/' + ('?embed=1' if embed else '')) if not TOKEN or self.signed_in() else self._html(200, login_page(embed=embed))
        if self.path.startswith('/logout'):
            return self._redirect('/login' + ('?embed=1' if embed else ''), [self.cookie('', 0)])
        if self.path.startswith('/context'):
            return self._send(200, context_for_bot()) if self.pin_ok() else None
        if self.path.startswith('/cricket') and self.query().get('pin'):
            return self._send(200, CRICKET or {'at': 0, 'matches': []}) if self.pin_ok() else None
        if self.path.startswith('/rows') and self.query().get('pin'):
            return self._send(200, {'rows': shown_for(self.query().get('profile', ''))}) if self.pin_ok() else None
        if self.path.startswith('/telemetry') and self.query().get('pin'):
            if not self.pin_ok():
                return
            try:
                hours, limit = int(q.get('hours') or 24), int(q.get('limit') or 200)
            except ValueError:
                hours, limit = 24, 200
            return self._send(200, {'events': query_telemetry(q.get('profile', ''), q.get('event', ''), hours, limit)})
        if self.path.startswith('/hiccups') and self.query().get('pin'):
            if not self.pin_ok():
                return
            try:
                hours = int(q.get('hours') or 24)
            except ValueError:
                hours = 24
            return self._send(200, hiccup_summary(hours))
        if self.path.startswith('/titles') and self.query().get('pin'):
            if not self.pin_ok():
                return
            try:
                days, limit = int(q.get('days') or 0), int(q.get('limit') or 200)
            except ValueError:
                days, limit = 0, 200
            return self._send(200, {'titles': catalogue_titles(q.get('lang', ''), q.get('kind', ''), days, min(limit, 5000), q.get('q', ''))})
        is_page = self.path == '/' or self.path.startswith(('/?', '/text', '/index'))
        if not self.allowed(is_page):
            return
        carry = '&'.join(p for p in ('embed=1' if embed else '', 's=' + q['s'] if q.get('s') else '') if p)
        if is_page and not self.path.startswith('/text'):
            # The Kindle tab is told the sign-in, so it can open signed in next time even without the cookie.
            return self._html(200, web_page(q.get('device', ''), carry, handoff(SESSION) if embed and SESSION else ''))
        if self.path.startswith('/top10'):
            return self._send(200, TOP10 or {'at': 0, 'sources': {}, 'titles': []})
        if self.path.startswith('/cricket'):
            return self._send(200, CRICKET or {'at': 0, 'matches': []})
        if self.path.startswith('/batches'):
            with LOCK:
                return self._send(200, BATCHES)
        if self.path.startswith('/feedback'):
            return self._send(200, all_feedback())
        if self.path.startswith('/lists'):
            return self._send(200, {'lists': lists_for(q.get('profile', '')), 'related': related_all()})
        if self.path.startswith('/related'):
            return self._send(200, {'related': related_all()})
        if self.path.startswith('/titles'):
            try:
                days, limit = int(q.get('days') or 0), int(q.get('limit') or 200)
            except ValueError:
                days, limit = 0, 200
            return self._send(200, {'titles': catalogue_titles(q.get('lang', ''), q.get('kind', ''), days, min(limit, 5000), q.get('q', ''))})
        if self.path.startswith('/rows'):
            return self._send(200, {'rows': shown_for(q.get('profile', ''))})
        if self.path.startswith('/telemetry'):
            try:
                hours, limit = int(q.get('hours') or 24), int(q.get('limit') or 200)
            except ValueError:
                hours, limit = 24, 200
            return self._send(200, {'events': query_telemetry(q.get('profile', ''), q.get('event', ''), hours, limit)})
        if self.path.startswith('/hiccups'):
            try:
                hours = int(q.get('hours') or 24)
            except ValueError:
                hours = 24
            return self._send(200, hiccup_summary(hours))
        if self.path.startswith('/cache/page'):
            url = q.get('url', '').strip()
            if not url:
                return self._send(400, {'error': 'url needed'})
            return self._send(200, cache_get_page(url))
        if self.path.startswith('/cache/meta'):
            name = q.get('name', '').strip()
            if not name:
                return self._send(400, {'error': 'name needed'})
            return self._send(200, cache_get_meta(name))
        if self.path.startswith('/cache/stats'):
            return self._send(200, cache_stats())
        if self.path.startswith('/api/endpoints'):
            return self._send(200, api_endpoints_directory())
        if self.path.startswith('/api/status'):
            return self._send(200, api_overall_status())
        if self.path.startswith('/api/index/status'):
            return self._send(200, api_index_status())
        if self.path.startswith('/api/search/status'):
            return self._send(200, api_search_status())
        if self.path.startswith('/api/search'):
            try:
                limit = min(int(q.get('limit') or 60), 500)
            except ValueError:
                limit = 60
            return self._send(200, api_search(q.get('q', ''), q.get('lang', ''), q.get('kind', ''), limit))
        if self.path.startswith('/api/recs/status'):
            return self._send(200, api_recs_status())
        if self.path.startswith('/api/listings/status'):
            return self._send(200, api_listings_status())
        if self.path.startswith('/api/listings/page'):
            url = q.get('url', '').strip()
            if not url:
                return self._send(400, {'error': 'url needed'})
            return self._send(200, cache_get_page(url))
        if self.path.startswith('/api/listings'):
            try:
                limit = min(int(q.get('limit') or 200), 1000)
            except ValueError:
                limit = 200
            return self._send(200, api_listings_index(q.get('q', ''), limit))
        if self.path.startswith('/history'):
            with connect() as conn:
                rows = conn.execute('SELECT key, at, model, took, picks FROM history ORDER BY at DESC LIMIT 500').fetchall()
            return self._send(200, [{'key': r['key'], 'at': r['at'], 'model': r['model'], 'took': r['took'], 'picks': json.loads(r['picks'])} for r in rows])
        if self.path.startswith('/text') and embed:
            back = '/?' + carry if carry else '/'
            return self._html(200, html_page('Full report', '<div class="top"><a href="{0}">Back to picks</a></div><h1>Full report</h1><pre style="white-space:pre-wrap;font-size:14px">{1}</pre>'.format(back, html.escape(page()))))
        if self.path.startswith('/text'):
            body = page().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.path.startswith('/api/index/refresh'):
            if not self.allowed():
                return
            started = trigger_index_refresh(trigger='api')
            return self._send(200, {'ok': True, 'started': started, 'status': api_index_status()})
        if self.path.startswith('/voice/session'):
            if not self.allowed():
                return
            code = create_voice_session()
            proto = self.headers.get('X-Forwarded-Proto') or 'https'
            host = self.headers.get('Host') or 'recs.senapati.si'
            return self._send(200, {'ok': True, 'code': code, 'url': '{0}://{1}/voice/{2}'.format(proto, host, code)})
        if self.path.startswith('/voice/send'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 4096)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            code = _clean_voice_code(body.get('code'))
            text = str(body.get('text') or '').strip()[:120]
            final = bool(body.get('final'))
            with VOICE_LOCK:
                _prune_voice()
                sess = VOICE_SESSIONS.get(code)
                if not sess:
                    return self._send(404, {'error': 'session expired'})
                sess['text'] = text
                sess['final'] = final
                sess['seq'] += 1
                sess['created'] = time.time()
            return self._send(200, {'ok': True})
        if self.path.startswith('/login'):
            length = int(self.headers.get('Content-Length') or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(length, 4096)).decode('utf-8', 'replace')).items()}
            embed = form.get('embed') == '1'
            if not TOKEN:
                return self._redirect('/')
            if not hmac.compare_digest(form.get('key', '').strip(), TOKEN):
                log('web sign-in refused, from {0}'.format(self.headers.get('Cf-Connecting-Ip') or self.client_address[0]))
                return self._html(401, login_page('That key is not right.', embed))
            # A year-long cookie; the Kindle tab also gets the sign-in in its address, in case its frame drops cookies.
            return self._redirect('/?embed=1&s=' + SESSION if embed else '/', [self.cookie(SESSION, 365 * 24 * 3600)])
        if self.path.startswith('/feedback'):
            if not self.allowed(page=True):
                return
            length = int(self.headers.get('Content-Length') or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(length, 4096)).decode('utf-8', 'replace')).items()}
            profile, name, verdict = form.get('profile', '').strip()[:200], form.get('name', '').strip()[:200], form.get('verdict', '')
            if profile and name:
                rate(profile, name, verdict)
                log('{0}: "{1}" rated {2}, from {3}'.format(profile, name, verdict, self.headers.get('Cf-Connecting-Ip') or self.client_address[0]))
            q = self.query()
            carry = '&'.join(p for p in ('embed=1' if q.get('embed') == '1' else '', 's=' + q['s'] if q.get('s') else '') if p)
            return self._redirect('/?device={0}{1}'.format(quote(q.get('device', '')), '&' + carry if carry else ''))
        if self.path.startswith('/lists'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 200000)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            if not self.pin_ok(body):
                return
            name = str(body.get('name') or '').strip()[:60]
            items = body.get('items')
            if not name or not isinstance(items, list):
                return self._send(400, {'error': 'name and items needed'})
            kept = save_list(name, str(body.get('title') or '').strip()[:80], str(body.get('by') or '').strip()[:60],
                             str(body.get('profile') or '').strip()[:200], items, body.get('exclude') or [])
            log('list "{0}" by {1}: {2} titles, from {3}'.format(name, body.get('by') or '?', kept, self.headers.get('Cf-Connecting-Ip') or self.client_address[0]))
            return self._send(200, {'ok': True, 'name': name, 'kept': kept})
        if self.path.startswith('/rows'):
            if not self.allowed():
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 400000)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            profile = str(body.get('profile') or '').strip()[:200]
            if not profile:
                return self._send(400, {'error': 'profile needed'})
            kept = save_shown(profile, body.get('rows') or [], body.get('recent') or [])
            return self._send(200, {'ok': True, 'kept': kept})
        if self.path.startswith('/catalogue'):
            if not self.allowed():
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 4000000)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            profile = str(body.get('profile') or '').strip()[:200]
            if not profile:
                return self._send(400, {'error': 'profile needed'})
            kept = save_catalogue(profile, body.get('titles') or [])
            log('catalogue from {0}: {1} titles'.format(profile, kept))
            return self._send(200, {'ok': True, 'kept': kept})
        if self.path.startswith('/cache/page'):
            if not self.allowed():
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 2000000)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            url = str(body.get('url') or '').strip()
            items = body.get('items')
            if not url or not isinstance(items, list):
                return self._send(400, {'error': 'url and items needed'})
            cache_put_page(url, items, body.get('at'))
            return self._send(200, {'ok': True, 'count': len(items)})
        if self.path.startswith('/cache/meta'):
            if not self.allowed():
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 200000)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            name = str(body.get('name') or '').strip()
            data = body.get('data')
            if not name or not isinstance(data, dict):
                return self._send(400, {'error': 'name and data needed'})
            cache_put_meta(name, data, body.get('at'))
            return self._send(200, {'ok': True})
        if self.path.startswith('/telemetry'):
            if not self.allowed():
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 500000)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            profile = str(body.get('profile') or '').strip()[:200]
            if not profile:
                return self._send(400, {'error': 'profile needed'})
            kept = save_telemetry(profile, body.get('version', ''), body.get('platform', ''), body.get('events') or [])
            return self._send(200, {'ok': True, 'kept': kept})
        if self.path.startswith('/related'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(length, 200000)) or b'{}')
            except ValueError:
                return self._send(400, {'error': 'bad json'})
            if not self.pin_ok(body):
                return
            title = str(body.get('title') or '').strip()
            items = body.get('items')
            if not title or not isinstance(items, list):
                return self._send(400, {'error': 'title and items needed'})
            kept = save_related(title, str(body.get('by') or '').strip()[:60], items)
            log('related to "{0}" by {1}: {2} titles'.format(title, body.get('by') or '?', kept))
            return self._send(200, {'ok': True, 'title': title, 'kept': kept})
        if not self.path.startswith('/pick'):
            return self._send(404, {'error': 'not found'})
        if not self.allowed():
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(length) or b'{}')
            if not req.get('candidates'):
                return self._send(400, {'error': 'no candidates'})
            req['from'] = self.client_address[0]
            result = pick(req)
            self._send(200, {'picks': result['picks'], 'model': result.get('model', MODEL), 'fresh': result['fresh'],
                             'building': result['building'], 'took': result.get('took', 0), 'at': result.get('at', 0)})
        except Exception as e:
            log('request failed: {0}'.format(e))
            self._send(500, {'error': str(e)})

    def do_DELETE(self):
        if self.path.startswith('/related'):
            if not self.pin_ok():
                return
            title = self.query().get('title', '').strip()
            if not title:
                return self._send(400, {'error': 'title needed'})
            with connect() as conn:
                conn.execute('DELETE FROM related WHERE key=?', (plain(title),))
            return self._send(200, {'ok': True})
        if not self.path.startswith('/lists'):
            return self._send(404, {'error': 'not found'})
        if not self.pin_ok():
            return
        name = self.query().get('name', '').strip()
        if not name:
            return self._send(400, {'error': 'name needed'})
        delete_list(name)
        log('list "{0}" removed'.format(name))
        self._send(200, {'ok': True})

    def log_message(self, fmt, *args):
        pass  # the build log above is enough


def cache_get_page(url):
    with connect() as conn:
        row = conn.execute('SELECT at, items FROM page_cache WHERE url=?', (url,)).fetchone()
    if not row:
        return {'hit': False}
    try:
        return {'hit': True, 'at': row['at'], 'items': json.loads(row['items'])}
    except Exception:
        return {'hit': False}


def cache_put_page(url, items, fetched_at=None):
    now = float(fetched_at) if fetched_at else time.time()
    blob = json.dumps(items, separators=(',', ':'))
    with connect() as conn:
        conn.execute('INSERT OR REPLACE INTO page_cache (url, at, items) VALUES (?, ?, ?)', (url, now, blob))


def cache_get_meta(name):
    key = name.strip().lower()
    with connect() as conn:
        row = conn.execute('SELECT at, data FROM meta_cache WHERE key=?', (key,)).fetchone()
    if not row:
        return {'hit': False}
    try:
        return {'hit': True, 'at': row['at'], 'data': json.loads(row['data'])}
    except Exception:
        return {'hit': False}


def cache_put_meta(name, data, fetched_at=None):
    key = name.strip().lower()
    if not key:
        return
    now = float(fetched_at) if fetched_at else time.time()
    blob = json.dumps(data, separators=(',', ':'))
    with connect() as conn:
        conn.execute('INSERT OR REPLACE INTO meta_cache (key, at, data) VALUES (?, ?, ?)', (key, now, blob))


def cache_stats():
    with connect() as conn:
        pages = conn.execute('SELECT COUNT(*) FROM page_cache').fetchone()[0]
        meta_n = conn.execute('SELECT COUNT(*) FROM meta_cache').fetchone()[0]
    return {'pages': pages, 'meta': meta_n}


INDEX_REFRESH_INTERVAL = int(os.environ.get('INDEX_REFRESH_SECONDS') or 3600)
STREAM_CORE_URL = os.environ.get('STREAM_CORE_URL') or 'http://127.0.0.1:8775'
_index_lock = threading.Lock()
INDEX_STATE = {
    'running': False,
    'interval_seconds': INDEX_REFRESH_INTERVAL,
    'last_run': 0.0,
    'next_run': 0.0,
    'last_trigger': '',
    'last_duration_ms': 0,
    'pages_added': 0,
    'meta_added': 0,
    'titles_synced': 0,
    'core_synced': 0,
    'runs': 0,
    'last_error': '',
}


def _stream_core_stats():
    try:
        req = urllib.request.Request(STREAM_CORE_URL + '/stats', headers={'User-Agent': 'StreamRecs/2.0'})
        with urllib.request.urlopen(req, timeout=0.6) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None


def _sync_titles_to_core():
    """Push indexed catalogue titles to Go stream-core for sub-millisecond ranked search."""
    try:
        titles = catalogue_titles('', '', 0, 5000, '')
        if not titles:
            return 0
        payload = json.dumps({
            'titles': [
                {
                    'name': t.get('name') or '',
                    'group': t.get('lang') or '',
                    'url': t.get('url') or '',
                    'parent': t.get('parent') or '',
                    't': t.get('kind') or '',
                }
                for t in titles if t.get('name')
            ]
        }).encode('utf-8')
        req = urllib.request.Request(
            STREAM_CORE_URL + '/index/sync',
            data=payload,
            headers={'Content-Type': 'application/json', 'User-Agent': 'StreamRecs/2.0'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=3.0) as r:
            res = json.loads(r.read().decode('utf-8'))
            return int(res.get('indexed') or 0)
    except Exception:
        return 0


def run_index_refresh(trigger='schedule'):
    """Mirror local Mac Kodi's library.db pages, titles & stream.db metadata into the backend hub and Go core."""
    with _index_lock:
        if INDEX_STATE['running']:
            return False
        INDEX_STATE['running'] = True
        INDEX_STATE['last_trigger'] = trigger
        INDEX_STATE['last_error'] = ''

    started = time.time()
    base = os.path.expanduser('~/Library/Application Support/Kodi/userdata/addon_data/script.stream')
    pages_added = meta_added = titles_synced = core_synced = 0
    err_msg = ''
    try:
        lib_db = os.path.join(base, 'library.db')
        if os.path.exists(lib_db):
            src = sqlite3.connect('file:' + lib_db + '?mode=ro', uri=True, timeout=5)
            rows = src.execute('SELECT url, items, fetched_at FROM pages WHERE items IS NOT NULL').fetchall()
            try:
                t_rows = src.execute('SELECT name, grp, kind, first_seen FROM library WHERE name IS NOT NULL').fetchall()
            except Exception:
                t_rows = []
            src.close()
            if rows:
                with connect() as conn:
                    for url, items, at in rows:
                        cur = conn.execute('SELECT at FROM page_cache WHERE url=?', (url,)).fetchone()
                        if cur is None or (at or 0) > (cur['at'] or 0):
                            conn.execute('INSERT OR REPLACE INTO page_cache (url, at, items) VALUES (?, ?, ?)', (url, at or time.time(), items))
                            pages_added += 1
            if t_rows:
                with connect() as conn:
                    for name, lang, kind, first_seen in t_rows:
                        key = (name or '').strip().lower()
                        if not key:
                            continue
                        cur = conn.execute('SELECT first_seen FROM catalogue WHERE key=?', (key,)).fetchone()
                        if cur is None:
                            conn.execute(
                                'INSERT OR REPLACE INTO catalogue (key, name, lang, kind, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)',
                                (key, name, lang or '', kind or '', first_seen or time.time(), time.time()),
                            )
                            titles_synced += 1
        str_db = os.path.join(base, 'stream.db')
        if os.path.exists(str_db):
            src = sqlite3.connect('file:' + str_db + '?mode=ro', uri=True, timeout=5)
            rows = src.execute('SELECT name, data, fetched_at FROM metadata WHERE data IS NOT NULL AND data != "{}"').fetchall()
            src.close()
            if rows:
                with connect() as conn:
                    for name, data, at in rows:
                        key = (name or '').strip().lower()
                        if not key:
                            continue
                        cur = conn.execute('SELECT at FROM meta_cache WHERE key=?', (key,)).fetchone()
                        if cur is None or (at or 0) > (cur['at'] or 0):
                            conn.execute('INSERT OR REPLACE INTO meta_cache (key, at, data) VALUES (?, ?, ?)', (key, at or time.time(), data))
                            meta_added += 1
        core_synced = _sync_titles_to_core()
        if pages_added or meta_added or titles_synced or core_synced:
            log('index refresh ({0}): +{1} pages, +{2} metadata, +{3} titles, core={4}'.format(
                trigger, pages_added, meta_added, titles_synced, core_synced))
    except Exception as e:
        err_msg = str(e)
        log('index refresh ({0}) note: {1}'.format(trigger, e))
    finally:
        now = time.time()
        with _index_lock:
            INDEX_STATE['running'] = False
            INDEX_STATE['last_run'] = now
            INDEX_STATE['next_run'] = now + INDEX_REFRESH_INTERVAL
            INDEX_STATE['last_duration_ms'] = int((now - started) * 1000)
            INDEX_STATE['pages_added'] = pages_added
            INDEX_STATE['meta_added'] = meta_added
            INDEX_STATE['titles_synced'] = titles_synced
            INDEX_STATE['core_synced'] = core_synced
            INDEX_STATE['runs'] += 1
            INDEX_STATE['last_error'] = err_msg
    return True


def trigger_index_refresh(trigger='api'):
    with _index_lock:
        if INDEX_STATE['running']:
            return False
    threading.Thread(target=run_index_refresh, args=(trigger,), daemon=True).start()
    return True


def index_scheduler():
    """Run index refresh once every hour on schedule."""
    while True:
        run_index_refresh(trigger='schedule')
        time.sleep(INDEX_REFRESH_INTERVAL)


def api_index_status():
    stats = cache_stats()
    with connect() as conn:
        cat_count = conn.execute('SELECT COUNT(*) FROM catalogue').fetchone()[0]
    with _index_lock:
        state = dict(INDEX_STATE)
    state['counts'] = {'pages': stats['pages'], 'meta': stats['meta'], 'titles': cat_count}
    return state


def api_search_status():
    with connect() as conn:
        cat_count = conn.execute('SELECT COUNT(*) FROM catalogue').fetchone()[0]
    core = _stream_core_stats()
    return {
        'ok': True,
        'backend_titles': cat_count,
        'stream_core_online': bool(core),
        'stream_core_indexed': (core or {}).get('engine', {}).get('indexed_titles', 0),
    }


def api_search(query, lang='', kind='', limit=60):
    q = (query or '').strip()
    if not q:
        return {'query': '', 'results': [], 'total': 0, 'source': 'backend'}
    titles = catalogue_titles(lang, kind, 0, limit, q)
    return {'query': q, 'results': titles, 'total': len(titles), 'source': 'backend'}


def api_recs_status():
    with LOCK:
        profiles = [
            {
                'key': k,
                'model': b.get('model', ''),
                'at': b.get('at', 0),
                'picks': len(b.get('picks') or []),
            }
            for k, b in BATCHES.items()
        ]
    return {
        'ok': True,
        'model': MODEL,
        'ollama': OLLAMA,
        'batches': len(profiles),
        'profiles': profiles,
        'top10_at': TOP10.get('at', 0) if isinstance(TOP10, dict) else 0,
        'cricket_at': CRICKET.get('at', 0) if isinstance(CRICKET, dict) else 0,
    }


def api_listings_status():
    now = time.time()
    buckets = {}
    freshest, oldest, total = 0.0, 0.0, 0
    with connect() as conn:
        rows = conn.execute('SELECT url, at FROM page_cache').fetchall()
    for r in rows:
        total += 1
        at = r['at'] or 0.0
        if at > freshest:
            freshest = at
        if oldest == 0.0 or (at > 0 and at < oldest):
            oldest = at
        u = (r['url'] or '').lower()
        if 'desimovies' in u or 'host=movies' in u:
            b = 'movies'
        elif 'desiwebseries' in u:
            b = 'web_series'
        elif 'desitvshows' in u or 'cGx1cy12b2Q'.lower() in u:
            b = 'tv_shows'
        elif 'live' in u or 'sports' in u:
            b = 'live_sports'
        elif 'kids' in u or 'cGx1cy1raWRz'.lower() in u:
            b = 'kids'
        elif 'youtube' in u:
            b = 'youtube'
        else:
            b = 'other'
        buckets[b] = buckets.get(b, 0) + 1
    return {
        'ok': True,
        'total_pages': total,
        'buckets': buckets,
        'freshest_age_s': int(now - freshest) if freshest else None,
        'oldest_age_s': int(now - oldest) if oldest else None,
    }


def api_listings_index(query='', limit=200):
    now = time.time()
    ql = (query or '').strip().lower()
    out = []
    with connect() as conn:
        rows = conn.execute('SELECT url, at, items FROM page_cache ORDER BY at DESC').fetchall()
    for r in rows:
        url = r['url'] or ''
        if ql and ql not in url.lower():
            continue
        try:
            items = json.loads(r['items'] or '[]')
            cnt = len(items) if isinstance(items, list) else 0
        except Exception:
            cnt = 0
        out.append({'url': url, 'items': cnt, 'at': r['at'] or 0, 'age_s': int(now - (r['at'] or now))})
        if len(out) >= limit:
            break
    return {'ok': True, 'total': len(out), 'listings': out}


def api_overall_status():
    return {
        'ok': True,
        'index': api_index_status(),
        'search': api_search_status(),
        'recs': api_recs_status(),
        'listings': api_listings_status(),
        'stream_core': _stream_core_stats(),
    }


def api_endpoints_directory():
    return {
        'ok': True,
        'schedule': {'index_refresh_seconds': INDEX_REFRESH_INTERVAL},
        'endpoints': [
            {'method': 'GET', 'path': '/api/endpoints', 'desc': 'List all backend API endpoints'},
            {'method': 'GET', 'path': '/api/status', 'desc': 'Unified status across index, search, recs, listings, and stream-core'},
            {'method': 'GET', 'path': '/api/index/status', 'desc': 'Hourly index schedule, last run telemetry, and indexed counts'},
            {'method': 'POST', 'path': '/api/index/refresh', 'desc': 'Trigger an immediate background index & cache hub refresh'},
            {'method': 'GET', 'path': '/api/search?q=&lang=&kind=&limit=', 'desc': 'Search indexed catalogue titles'},
            {'method': 'GET', 'path': '/api/search/status', 'desc': 'Search index and Go stream-core status'},
            {'method': 'GET', 'path': '/api/recs/status', 'desc': 'Recommendations engine, model, batches, Top 10, and cricket status'},
            {'method': 'GET', 'path': '/api/listings', 'desc': 'List cached VOD/Live/YouTube listing URLs with item counts and age'},
            {'method': 'GET', 'path': '/api/listings/status', 'desc': 'Summary of cached listings by bucket and freshness'},
            {'method': 'GET', 'path': '/api/listings/page?url=', 'desc': 'Fetch cached items for a specific listing URL'},
            {'method': 'GET', 'path': '/titles?q=&lang=&kind=&days=&limit=', 'desc': 'Raw catalogue titles endpoint'},
            {'method': 'GET', 'path': '/top10', 'desc': 'India Top 10 streaming charts'},
            {'method': 'GET', 'path': '/cricket', 'desc': 'Live and upcoming cricket fixtures'},
            {'method': 'GET', 'path': '/rows?profile=', 'desc': 'Current Home rows per profile'},
            {'method': 'GET', 'path': '/batches', 'desc': 'Latest recommendation batches'},
            {'method': 'GET', 'path': '/cache/stats', 'desc': 'Shared page & metadata cache counts'},
            {'method': 'GET', 'path': '/cache/page?url=', 'desc': 'Fetch cached listing page'},
            {'method': 'GET', 'path': '/cache/meta?name=', 'desc': 'Fetch cached TMDb metadata'},
            {'method': 'GET', 'path': '/telemetry?hours=24', 'desc': 'Box interaction telemetry'},
            {'method': 'GET', 'path': '/hiccups?hours=24', 'desc': 'Playback/network hiccup summary'},
            {'method': 'GET', 'path': '/health', 'desc': 'Service health probe'},
        ],
    }


if __name__ == '__main__':
    load()
    servers, bound = [], []
    for host in BIND:
        try:
            servers.append(ThreadingHTTPServer((host, PORT), Handler))
            bound.append(host)
        except OSError as e:
            log('could not bind {0}:{1}: {2}'.format(host, PORT, e))
    if not servers:
        raise SystemExit('no bind address available from BIND={0}'.format(BIND))
    log('serving on {0} port {1}, model {2} at {3}, {4} batches remembered, hourly index refresh={5}s'.format(
        ', '.join(bound), PORT, MODEL, OLLAMA, len(BATCHES), INDEX_REFRESH_INTERVAL))
    threading.Thread(target=refresh_top10, daemon=True).start()
    threading.Thread(target=refresh_cricket, daemon=True).start()
    threading.Thread(target=index_scheduler, daemon=True).start()
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()
