"""Lightweight client bridge to the native Go `stream-core` daemon (127.0.0.1:8775).

Provides:
- Fast local HTTP/2 stream prefetching & Range/HLS proxy URLs (so FFmpeg starts in <300ms)
- Resolved stream URL caching (so resuming/re-playing skips spawning plugin.video.sastatv)
- Sub-millisecond in-memory ranked search & concurrent YouTube Atom feed fetching
Gracefully falls back to no-ops if stream-core is not running (e.g. on iOS).
"""
import json
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASES = ('http://127.0.0.1:8775', 'http://192.168.1.175:8775')
BASE = BASES[0]
_alive = {'ok': False, 'base': BASES[0], 'checked': 0.0}
_lock = threading.Lock()


def available(force=False):
    global BASE
    now = time.time()
    with _lock:
        if not force and now - _alive['checked'] < 15.0:
            return _alive['ok']
    ok, chosen = False, BASES[0]
    for candidate in BASES:
        try:
            with urlopen(candidate + '/health', timeout=0.35) as r:
                if r.status == 200:
                    ok, chosen = True, candidate
                    break
        except Exception:
            pass
    with _lock:
        _alive['ok'] = ok
        _alive['base'] = chosen
        _alive['checked'] = now
        BASE = chosen
    return ok


def _post_json(path, payload, timeout=1.5):
    data = json.dumps(payload).encode('utf-8')
    req = Request(BASE + path, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _get_json(path, timeout=1.5):
    with urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def get_resolved(key):
    """Return cached local proxy URL for a previously resolved Sasta TV item, or None."""
    if not key or not available():
        return None
    try:
        res = _get_json('/resolve-cache?' + urlencode({'key': key}), timeout=0.5)
        if res.get('found') and res.get('proxy_url'):
            return res['proxy_url']
    except Exception:
        pass
    return None


def cache_resolved(key, media_url, headers=None, ttl_seconds=1200, prefetch=True):
    """Store a resolved media URL (e.g. captured from Kodi Player after Sasta TV resolves it)."""
    if not key or not media_url or not media_url.startswith('http') or not available():
        return None
    try:
        res = _post_json('/resolve-cache', {
            'key': key,
            'url': media_url,
            'headers': headers or {},
            'ttl_seconds': ttl_seconds,
            'prefetch': prefetch,
        }, timeout=1.0)
        return res.get('proxy_url')
    except Exception:
        return None


def prefetch_url(media_url, headers=None):
    """Warm the Go proxy's 2 MB VOD head buffer or first 3 HLS segments in the background."""
    if not media_url or not media_url.startswith('http') or not available():
        return
    def _run():
        try:
            _post_json('/prefetch', {'url': media_url, 'headers': headers or {}}, timeout=1.0)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def sync_index(titles):
    """Push catalogue titles to stream-core's in-memory search index asynchronously."""
    if not titles or not available():
        return
    def _run():
        try:
            _post_json('/index/sync', {'titles': titles}, timeout=3.0)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def fast_search(query, langs=None):
    """Query stream-core's sub-millisecond in-memory ranked search index."""
    if not query or not available():
        return None
    try:
        params = {'q': query}
        if langs:
            params['langs'] = ','.join(langs)
        res = _get_json('/search?' + urlencode(params), timeout=0.6)
        return res.get('results')
    except Exception:
        return None


def youtube_feeds(channel_ids):
    """Fetch multiple YouTube channel Atom feeds concurrently via Go goroutines."""
    if not channel_ids or not available():
        return None
    try:
        res = _post_json('/youtube/feeds', {'channels': list(channel_ids)}, timeout=4.0)
        return res.get('feeds')
    except Exception:
        return None
