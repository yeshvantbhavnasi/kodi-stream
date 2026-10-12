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
    GET  /         a plain-text page: every box and profile's current picks, the reasons, and what the model was told
    GET  /batches  the same as JSON; GET /history the past batches, newest first

Settings come from the environment: PORT (8770), BIND (addresses to listen on, comma separated; 127.0.0.1 by
default, add the machine's Tailscale address so the other boxes on the tailnet can reach it), OLLAMA_URL
(http://127.0.0.1:11434), OLLAMA_MODEL (gemma3:4b) and STATE (the SQLite file the batches are kept in). With Serve
enabled on the tailnet, `tailscale serve --bg 8770` publishes it as https://<machine>.<tailnet>.ts.net instead.

Storage: one SQLite file. `batches` holds the current batch per box, profile and kind (the only thing a box ever
asks for), and `history` keeps the last 48 batches per key so the page can show how the picks moved; a batch
identical to the previous one is not written again.
"""
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8770'))
BIND = [h.strip() for h in os.environ.get('BIND', '127.0.0.1').split(',') if h.strip()]
OLLAMA = os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
MODEL = os.environ.get('OLLAMA_MODEL', 'gemma3:4b')
STATE = os.environ.get('STATE', os.path.expanduser('~/Library/Application Support/stream-recs/batches.db'))
KEEP = 7 * 24 * 3600  # a remembered search answer is kept this long
HISTORY = 48          # past batches kept per box, profile and kind

LOCK = threading.Lock()        # guards BATCHES and BUILDING
MODEL_LOCK = threading.Lock()  # one question to the model at a time
BATCHES = {}                   # key -> {'digest', 'picks', 'at', 'model', 'took'}
BUILDING = set()               # keys being rebuilt right now


def log(msg):
    sys.stdout.write('{0} {1}\n'.format(time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    sys.stdout.flush()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS batches (key TEXT PRIMARY KEY, profile TEXT, kind TEXT, digest TEXT, at REAL, model TEXT,
    took REAL, count INTEGER, system TEXT, intro TEXT, candidates TEXT, picks TEXT);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, at REAL, model TEXT, took REAL, picks TEXT);
CREATE INDEX IF NOT EXISTS history_key ON history (key, at);
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
             [(c.get('id'), c.get('name'), c.get('text')) for c in req.get('candidates', [])]]
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def build(req):
    """Ask the model and return [{'name', 'reason'}] (names, not positional ids, so a remembered answer stays valid)."""
    candidates = req.get('candidates', [])
    count = int(req.get('count', 15))
    names = {c['id']: c['name'] for c in candidates}
    listing = '\n'.join('{0}: {1}'.format(c['id'], c['text']) for c in candidates)
    prompt = ('{0}\n\nCandidates:\n{1}\n\nPick the {2} best candidates, best first. Reply with JSON only, shaped as '
              '{{"picks": [{{"id": "c0", "reason": "why, for the viewer, in at most ten words"}}]}}. Use only ids from the list.'
              ).format(req.get('intro', ''), listing, count)
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
            'system': req.get('system', '')[:2000], 'intro': req.get('intro', '')[:12000],
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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/health'):
            with LOCK:
                n = len(BATCHES)
            return self._send(200, {'ok': True, 'model': MODEL, 'ollama': ollama_up(), 'batches': n})
        if self.path.startswith('/batches'):
            with LOCK:
                return self._send(200, BATCHES)
        if self.path.startswith('/history'):
            with connect() as conn:
                rows = conn.execute('SELECT key, at, model, took, picks FROM history ORDER BY at DESC LIMIT 500').fetchall()
            return self._send(200, [{'key': r['key'], 'at': r['at'], 'model': r['model'], 'took': r['took'], 'picks': json.loads(r['picks'])} for r in rows])
        if self.path == '/' or self.path.startswith('/?') or self.path.startswith('/index'):
            body = page().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if not self.path.startswith('/pick'):
            return self._send(404, {'error': 'not found'})
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

    def log_message(self, fmt, *args):
        pass  # the build log above is enough


if __name__ == '__main__':
    load()
    log('serving on {0} port {1}, model {2} at {3}, {4} batches remembered'.format(', '.join(BIND), PORT, MODEL, OLLAMA, len(BATCHES)))
    servers = [ThreadingHTTPServer((host, PORT), Handler) for host in BIND]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()
