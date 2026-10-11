# Reads the Sasta TV addon's folders through Kodi and hands back plain item dicts.
import base64
import json
import os
import re
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

import xbmc
import xbmcaddon
import xbmcvfs

import db
import youtube as yt

ADDON = xbmcaddon.Addon('script.stream')
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
SASTA = 'plugin://plugin.video.sastatv/'

# Only catalogue folders are ever opened. Addon actions (settings, cache clearing, log upload)
# live on other plugin modes and are never called from here.
CATALOGUE = re.compile(r'^https?://sastatv\.com/secured/php/[\w/]+\.php(\?|$)', re.I)
GENERIC_IMG = re.compile(r'sastatv\.com/images/(logo/|Sasta)', re.I)
FRESH = 30 * 60
MAX_STALE = 12 * 3600

S3 = 'https://sastatv.com/secured/php/fetchXclusiveS3.php?bkt='
URLS = {
    'movies': S3 + 'ZGVzaW1vdmllcw==',
    'web_series': S3 + 'ZGVzaXdlYnNlcmllcw==',
    'tv_shows': S3 + 'ZGVzaXR2c2hvd3M=',
    'english_movies': 'http://sastatv.com/secured/php/fetchXclusivePlus.php?host=movies&order=recent',
    'english_tv': 'http://sastatv.com/secured/php/fetchXclusiveS3.php?bkt=cGx1cy12b2Q=&order=recent',
    'kids': 'http://sastatv.com/secured/php/fetchXclusiveS3.php?bkt=cGx1cy1raWRz',
    'live': 'https://sastatv.com/secured/php/fetchXML.php?id=live-root2-beta',
    'fresh': 'https://sastatv.com/secured/php/fetchXML.php?id=newlyadded',
    'sports': 'https://sastatv.com/secured/php/fetchXML.php?id=live-grp-sports',
}
BUCKETS = {'desimovies': 'Movies', 'desiwebseries': 'Web Series', 'desitvshows': 'TV Shows',
           'plus-vod': 'English TV', 'plus-kids': 'Kids'}

_lock = threading.Lock()
_cache = {}
_cache_path = os.path.join(PROFILE, 'cache.json')
_state_path = os.path.join(PROFILE, 'state.json')


def log(msg):
    xbmc.log('STREAM: {0}'.format(msg), xbmc.LOGINFO)


def _load(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    try:
        if not os.path.isdir(PROFILE):
            os.makedirs(PROFILE)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        log('could not save {0}: {1}'.format(path, e))


MAX_CACHED = 60          # listings kept on disk
_cache.update({k: v for k, v in _load(_cache_path, {}).items() if time.time() - v.get('t', 0) < 12 * 3600})
_last_save = [0.0]


def _save_cache(force=False):
    """Write the listing cache, at most every 20 seconds, keeping only the most recent listings. Call with _lock held."""
    if not force and time.time() - _last_save[0] < 20:
        return
    _last_save[0] = time.time()
    if len(_cache) > MAX_CACHED:
        for key in sorted(_cache, key=lambda k: _cache[k]['t'])[:len(_cache) - MAX_CACHED]:
            del _cache[key]
    try:
        if not os.path.isdir(PROFILE):
            os.makedirs(PROFILE)
        tmp = _cache_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_cache, f)
        os.replace(tmp, _cache_path)
    except Exception as e:
        log('could not save cache: {0}'.format(e))


def flush():
    with _lock:
        _save_cache(force=True)
db.init()
state = {}


def load_state():
    """Per-profile preferences (how often each language is played, last live language)."""
    state.clear()
    saved = db.meta('state')
    if saved is None and db.ACTIVE == 'default':
        saved = _load(_state_path, {})  # carried over from versions that kept this in a file
        saved.pop('recent', None)
    state.update(saved or {})
    state.setdefault('affinity', {})


def save_state():
    db.set_meta('state', state)


load_state()


# Each listing or play request starts a fresh copy of the Sasta TV addon inside Kodi. Many at once can exhaust
# the memory of a TV stick, so they take turns: at most two run at any moment.
_plugin_slots = threading.Semaphore(2)


def rpc(method, params=None):
    request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
    if method == 'Files.GetDirectory':
        with _plugin_slots:
            reply = json.loads(xbmc.executeJSONRPC(request))
    else:
        reply = json.loads(xbmc.executeJSONRPC(request))
    if 'error' in reply:
        raise RuntimeError(reply['error'].get('message', 'Kodi error'))
    return reply.get('result')


def is_folder_url(url):
    return bool(CATALOGUE.match(url)) and 'sastatv.php' not in url.lower()


def clean(label):
    label = re.sub(r'\[/?(B|I|COLOR[^\]]*|UPPERCASE|LOWERCASE|CAPITALIZE|LIGHT)\]', '', label or '', flags=re.I)
    return label.replace('[CR]', ' ').strip()


def trim_decor(name):
    return re.sub(r'^[\s*\-:\[\]<>]+|[\s*\-:\[\]<>]+$', '', name)


def title_case(name):
    return ' '.join(w.capitalize() for w in name.split(' '))


def _image(value):
    if not value:
        return None
    m = re.match(r'^image://(.+?)/?$', value)
    url = unquote(m.group(1)) if m else value
    return url if re.match(r'^https?://', url) else None


def is_live(item):
    return bool(re.search(r'vendor1play\.php|\.m3u8', item.get('url', ''), re.I))


def classify(entry, index):
    """t is one of: folder, play, next, search, filter, head."""
    url = entry.get('file') or ''
    name = clean(entry.get('title') or entry.get('label'))
    art = entry.get('art') or {}
    if not name:
        return None
    item = {'t': 'play', 'name': name, 'url': url, 'i': index + 1}
    img = _image(entry.get('thumbnail')) or _image(art.get('thumb')) or _image(art.get('poster'))
    if img and not GENERIC_IMG.search(img):
        item['img'] = img
    plot = clean(entry.get('plot'))
    if plot:
        item['plot'] = plot
    if url.startswith('plugin://') or not re.match(r'^https?://', url):
        return None
    if CATALOGUE.match(url):
        if not is_folder_url(url) or url.endswith('query='):
            return None
        if url.endswith('search='):
            item['t'] = 'search'
        elif 'NEXT PAGE' in name.upper():
            item['t'] = 'next'
        elif re.search(r'[?&](filter|xmode)=year', url):
            item['t'] = 'filter'
            item['name'] = trim_decor(name)
        else:
            item['t'] = 'folder'
        return item
    if re.search(r'cricbuzz\.com|espncricinfo\.com', url, re.I):
        return None
    return item


def plugin_folder(url):
    return '{0}?mode=10&name=x&url={1}'.format(SASTA, quote(url, safe=''))


def _fetch(url):
    if yt.is_youtube(url):
        items = yt.listing(rpc, url)
    else:
        result = rpc('Files.GetDirectory', {'directory': plugin_folder(url), 'media': 'video',
                                            'properties': ['title', 'thumbnail', 'art', 'plot']})
        items = [classify(e, n) for n, e in enumerate(result.get('files') or [])]
        items = [i for i in items if i]
    if not re.search(r'search=.', url):
        with _lock:
            _cache[url] = {'t': time.time(), 'items': items}
            _save_cache()
        index(url, items)
    return items


def section_of(url):
    """(kind, language) for a catalogue folder, or (None, None) when it is not a title listing."""
    params = parse_qs(urlparse(url).query)
    bucket = _b64(params.get('bkt', [''])[0])
    if bucket in BUCKETS:
        return BUCKETS[bucket], _b64(params.get('lang', [''])[0]) or ('English' if bucket.startswith('plus') else '')
    if 'fetchXclusivePlus.php' in url and params.get('host') == ['movies']:
        return 'Movies', 'English'
    return None, None


def index(url, items):
    """Record titles from a listing in the local database so recommendations and search can use them."""
    kind, lang = section_of(url)
    if not kind or 'sdirurl=' in url:
        return
    titles = [i for i in items if (i['t'] == 'play' and not is_live(i)) or (i['t'] == 'folder' and i.get('img'))]
    if titles:
        try:
            db.index_titles(url, lang, kind, titles)
        except Exception as e:
            log('index failed: {0}'.format(e))


def warm_index():
    """Once a day, make sure the first page of every main section is in the local database."""
    if time.time() - db.meta('indexed_at', 0) < 24 * 3600:
        return
    if db.KIDS:
        try:
            index(URLS['kids'], get_dir(URLS['kids']))
        except Exception as e:
            log('warm failed: {0}'.format(e))
        db.set_meta('indexed_at', time.time())
        return
    for root in (URLS['movies'], URLS['web_series']):
        try:
            for folder in [f for f in get_dir(root) if f['t'] == 'folder']:
                index(folder['url'], get_dir(folder['url']))
        except Exception as e:
            log('warm failed: {0}'.format(e))
    for url in (URLS['english_movies'], URLS['english_tv'], URLS['kids']):
        try:
            index(url, get_dir(url))
        except Exception as e:
            log('warm failed: {0}'.format(e))
    db.set_meta('indexed_at', time.time())


def top_groups():
    """Languages in order of preference: the ones chosen at setup first, then by how often they are played."""
    chosen = [g.lower() for g in (db.meta('profile') or {}).get('languages', [])]
    played = [g for g, _ in sorted(state['affinity'].items(), key=lambda p: -p[1]) if g not in chosen]
    return chosen + played


_refresh_queue = []
_refresh_worker = [None]


def _refresh_loop():
    while True:
        with _lock:
            if not _refresh_queue:
                _refresh_worker[0] = None
                return
            url = _refresh_queue.pop(0)
        try:
            _fetch(url)
        except Exception as e:
            log('refresh failed: {0}'.format(e))


def _refresh_later(url):
    """Queue a stale listing for one background worker; duplicates are dropped."""
    with _lock:
        if url in _refresh_queue:
            return
        _refresh_queue.append(url)
        if _refresh_worker[0] is None:
            _refresh_worker[0] = threading.Thread(target=_refresh_loop, daemon=True)
            _refresh_worker[0].start()


def get_dir(url, fresh=False):
    """Serve from cache when possible; refresh in the background once the copy is older than FRESH."""
    with _lock:
        hit = _cache.get(url)
    age = time.time() - hit['t'] if hit else None
    if hit is None or fresh or age > MAX_STALE:
        return _fetch(url)
    if age > FRESH:
        _refresh_later(url)
    return hit['items']


def showable(items):
    return [i for i in items if i['t'] in ('play', 'folder')]


def _b64(value):
    try:
        return base64.b64decode(value or '').decode('utf-8')
    except Exception:
        return ''


def _source_language(base):
    params = parse_qs(urlparse(base).query)
    return _b64(params.get('lang', [''])[0]) or 'english'


def search(query):
    """Run the query against the sections in the viewer's languages; widen to every section only if that finds nothing."""
    with _lock:
        sources = []
        for entry in _cache.values():
            for item in entry['items']:
                if item['t'] == 'search' and item['url'] not in sources:
                    sources.append(item['url'])
    if db.KIDS:
        return _search_sources(query, [s for s in sources if 'cGx1cy1raWRz' in s])  # the kids section only
    chosen = [s for s in sources if db.lang_match(_source_language(s))]
    if chosen and len(chosen) < len(sources):
        hits = _search_sources(query, chosen)
        if hits:
            return hits
        sources = [s for s in sources if s not in chosen]
    return _search_sources(query, sources)


def _search_sources(query, sources):
    results = [None] * len(sources)

    def run(n, base):
        url = base + quote(query, safe='')
        try:
            items = showable(_fetch(url))
        except Exception:
            return
        if not items:
            return
        params = parse_qs(urlparse(base).query)
        bucket = _b64(params.get('bkt', [''])[0])
        lang = _b64(params.get('lang', [''])[0])
        title = ' · '.join(x for x in (BUCKETS.get(bucket, bucket), lang) if x) or 'English Movies'
        results[n] = (title, url, items)

    threads = [threading.Thread(target=run, args=(n, base), daemon=True) for n, base in enumerate(sources)]
    for group in range(0, len(threads), 2):
        batch = threads[group:group + 2]
        for t in batch:
            t.start()
        for t in batch:
            t.join()
    return [r for r in results if r]


def rank_results(query, groups):
    """Flatten search groups into one list: closest title matches first, then the viewer's languages, then newest."""
    q = query.lower().strip()
    words = [w for w in re.findall(r'\w+', q) if len(w) > 2]
    preferred = top_groups()
    ranked, seen = [], set()
    for title, parent, items in groups:
        lang = title.split(' · ')[-1]
        for item in items:
            name = item['name'].lower()
            text = name + ' ' + item.get('plot', '').lower()
            if words and not any(w in text for w in words):
                continue  # some sections ignore the query and return their whole first page
            if (name, lang) in seen:
                continue
            seen.add((name, lang))
            base = re.sub(r'\s*\(\d{4}\).*$', '', name)
            if base == q:
                score = 0
            elif name.startswith(q):
                score = 1
            elif re.search(r'\b' + re.escape(q), name):
                score = 2
            elif q in name:
                score = 3
            else:
                score = 4
            year = re.search(r'\((\d{4})\)', name)
            lang_rank = preferred.index(lang.lower()) if lang.lower() in preferred else len(preferred)
            ranked.append((score, lang_rank, -int(year.group(1)) if year else 0, len(ranked),
                           {'item': item, 'parent': parent, 'group': lang, 'meta': title}))
    ranked.sort(key=lambda r: r[:4])
    return [r[4] for r in ranked]


def play_url(parent, item):
    """The Sasta TV addon addresses a playable entry by mode; build the link for the kinds we know."""
    url = item['url']
    ref = None
    if re.match(r'^https?://sastatv\.com/vendor1play\.php', url, re.I):
        mode = 110
    elif 'nriflix.com/' in url.lower():
        mode, ref = 256, 'xclusiveS3'
    elif re.search(r'\.m3u8(\?|$)', url, re.I):
        mode, ref = 255, 'xclusiveStream'
    else:
        return None
    parts = [('index', item.get('i', 1)), ('mode', mode), ('name', item['name']), ('origurl', parent)]
    if ref:
        parts += [('ref', ref), ('shareurl', 'None')]
    if item.get('img'):
        parts.append(('thumb', item['img']))
    parts.append(('url', url))
    return SASTA + '?' + '&'.join('{0}={1}'.format(k, quote(str(v), safe='')) for k, v in parts)


def play(parent, item, group=None, series=None, label=None):
    target = item['url'] if yt.is_youtube(item.get('url')) else play_url(parent, item)
    if not target:
        return False
    db.add_history(item, parent, group, series, label)
    if group and not yt.is_youtube(target):   # YouTube is not a language, so it does not shape the language order
        key = group.lower()
        state['affinity'][key] = state['affinity'].get(key, 0) + 1
    save_state()

    # Opening the link as a folder is what a click inside the Sasta TV addon does; it then starts the player.
    def run():
        try:
            if yt.is_youtube(target):
                rpc('Player.Open', {'item': {'file': target}})
            else:
                rpc('Files.GetDirectory', {'directory': target})
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
    return True


def by_affinity(folders):
    """Preferred languages come first; ties keep the provider's order."""
    order = top_groups()

    def rank(name):
        return order.index(name.lower()) if name.lower() in order else len(order)
    ranked = sorted(enumerate(folders), key=lambda p: (rank(p[1]['name']), p[0]))
    return [f for _, f in ranked]


def natural_key(name):
    """Sort key that orders 'Episode 2' before 'Episode 10'."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', name)]
