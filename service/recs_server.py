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
'''
JSON_COLUMNS = ('candidates', 'picks')


def connect():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    conn = sqlite3.connect(STATE)
    conn.row_factory = sqlite3.Row
    return conn


def load():
    """Read every current batch into memory; batches saved by the JSON-file version are taken over once."""
    global BATCHES
    with connect() as conn:
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
    needs = current is None or time.time() - current['at'] > 20 * 3600 or latest > current['at'] or bool(missing)
    return {'generated': time.time(), 'list': current, 'needs_update': needs,
            'reason': ('no list posted yet' if current is None else 'the list is a day old' if time.time() - current['at'] > 20 * 3600
                       else 'new activity since the list was posted' if latest > current['at']
                       else 'some titles have no related list yet' if missing else 'nothing changed since the list was posted'),
            'related_wanted': wanted[:30], 'related_missing': missing[:30],
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
        q = self.query()
        embed = q.get('embed') == '1'
        if self.path.startswith('/login'):
            return self._redirect('/' + ('?embed=1' if embed else '')) if not TOKEN or self.signed_in() else self._html(200, login_page(embed=embed))
        if self.path.startswith('/logout'):
            return self._redirect('/login' + ('?embed=1' if embed else ''), [self.cookie('', 0)])
        if self.path.startswith('/context'):
            return self._send(200, context_for_bot()) if self.pin_ok() else None
        if self.path.startswith('/rows') and self.query().get('pin'):
            return self._send(200, {'rows': shown_for(self.query().get('profile', ''))}) if self.pin_ok() else None
        is_page = self.path == '/' or self.path.startswith(('/?', '/text', '/index'))
        if not self.allowed(is_page):
            return
        carry = '&'.join(p for p in ('embed=1' if embed else '', 's=' + q['s'] if q.get('s') else '') if p)
        if is_page and not self.path.startswith('/text'):
            # The Kindle tab is told the sign-in, so it can open signed in next time even without the cookie.
            return self._html(200, web_page(q.get('device', ''), carry, handoff(SESSION) if embed and SESSION else ''))
        if self.path.startswith('/top10'):
            return self._send(200, TOP10 or {'at': 0, 'sources': {}, 'titles': []})
        if self.path.startswith('/batches'):
            with LOCK:
                return self._send(200, BATCHES)
        if self.path.startswith('/feedback'):
            return self._send(200, all_feedback())
        if self.path.startswith('/lists'):
            return self._send(200, {'lists': lists_for(q.get('profile', '')), 'related': related_all()})
        if self.path.startswith('/related'):
            return self._send(200, {'related': related_all()})
        if self.path.startswith('/rows'):
            return self._send(200, {'rows': shown_for(q.get('profile', ''))})
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


if __name__ == '__main__':
    load()
    log('serving on {0} port {1}, model {2} at {3}, {4} batches remembered'.format(', '.join(BIND), PORT, MODEL, OLLAMA, len(BATCHES)))
    threading.Thread(target=refresh_top10, daemon=True).start()
    servers = [ThreadingHTTPServer((host, PORT), Handler) for host in BIND]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()
