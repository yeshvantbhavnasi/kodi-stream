"""Live TV EPG ("Now Playing" & "Up Next") background worker for Stream backend.

Downloads public Indian XMLTV EPG feeds once every 2 hours on the Mac mini, parses current & upcoming
programmes, and serves a compact normalized JSON lookup map so low-RAM TVs do zero XML parsing.
"""
import datetime
import gzip
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

FEEDS = [
    'https://epgshare01.online/epgshare01/epg_ripper_IN1.xml.gz',
]
REFRESH_SECONDS = 2 * 3600
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

_NOISE = re.compile(
    r'\b(4k|uhd|fhd|hd|sd|usa|us|uk|ca|canada|india|in|backup|test|vip|hevc|h265|50fps|60fps|plus|\+|tv)\b',
    re.I,
)
_NON_ALNUM = re.compile(r'[^a-z0-9]+')

_lock = threading.Lock()
_channels = {}       # cid -> (display_name, [norm_keys])
_programmes = {}     # cid -> sorted list of (start_ts, stop_ts, title, desc)
_state = {
    'ok': False,
    'channels': 0,
    'programmes': 0,
    'last_run': 0.0,
    'last_duration_ms': 0,
    'last_error': '',
}


def norm_channel(name):
    """Normalize Sasta TV & XMLTV channel names so 'GEMINI MUSIC 4K' matches 'Gemini Music HD'."""
    s = (name or '').lower().strip()
    s = re.sub(r'\(.*?\)', ' ', s)
    s = _NOISE.sub(' ', s)
    return _NON_ALNUM.sub('', s)


def _parse_xmltv_time(raw):
    """Parse '20261125003000 +0530' into UTC epoch seconds."""
    if not raw or len(raw) < 14:
        return 0.0
    try:
        dt = datetime.datetime.strptime(raw[:14], '%Y%m%d%H%M%S')
        tz_part = raw[14:].strip()
        if len(tz_part) >= 5 and tz_part[0] in ('+', '-'):
            sign = 1 if tz_part[0] == '+' else -1
            oh, om = int(tz_part[1:3]), int(tz_part[3:5])
            offset = datetime.timezone(datetime.timedelta(hours=sign * oh, minutes=sign * om))
            dt = dt.replace(tzinfo=offset)
        else:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def refresh_once():
    started = time.time()
    new_chans = {}
    new_progs = {}
    err = ''
    try:
        now = time.time()
        window_start = now - 3600
        window_end = now + 12 * 3600
        for url in FEEDS:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = gzip.decompress(r.read())
            root = ET.fromstring(raw)
            for c in root.findall('channel'):
                cid = c.attrib.get('id', '')
                if not cid:
                    continue
                names = [el.text.strip() for el in c.findall('display-name') if el.text and el.text.strip()]
                display = names[0] if names else cid
                keys = set()
                for n in names + [cid.split('.')[0]]:
                    nk = norm_channel(n)
                    if len(nk) >= 2:
                        keys.add(nk)
                    raw_k = _NON_ALNUM.sub('', n.lower())
                    if len(raw_k) >= 2:
                        keys.add(raw_k)
                new_chans[cid] = (display, list(keys))
            for p in root.findall('programme'):
                cid = p.attrib.get('channel', '')
                if cid not in new_chans:
                    continue
                st = _parse_xmltv_time(p.attrib.get('start', ''))
                en = _parse_xmltv_time(p.attrib.get('stop', ''))
                if en < window_start or st > window_end:
                    continue
                title = (p.findtext('title') or '').strip()
                if not title:
                    continue
                desc = (p.findtext('desc') or '').strip()[:220]
                new_progs.setdefault(cid, []).append((st, en, title, desc))
        for cid in new_progs:
            new_progs[cid].sort(key=lambda x: x[0])
    except Exception as e:
        err = str(e)

    total_p = sum(len(v) for v in new_progs.values())
    with _lock:
        if new_chans and new_progs:
            _channels.clear()
            _channels.update(new_chans)
            _programmes.clear()
            _programmes.update(new_progs)
            _state['ok'] = True
            _state['channels'] = len(new_chans)
            _state['programmes'] = total_p
        _state['last_run'] = time.time()
        _state['last_duration_ms'] = int((time.time() - started) * 1000)
        _state['last_error'] = err


def now_playing():
    """Return compact map {norm_channel_key: {now, next, desc, pct}} for all currently airing shows."""
    now = time.time()
    out = {}
    with _lock:
        for cid, (display, keys) in _channels.items():
            plist = _programmes.get(cid)
            if not plist:
                continue
            cur = nxt = None
            for i, (st, en, title, desc) in enumerate(plist):
                if st <= now < en:
                    cur = (st, en, title, desc)
                    if i + 1 < len(plist):
                        nxt = plist[i + 1][2]
                    break
            if not cur:
                continue
            st, en, title, desc = cur
            pct = max(1, min(99, int(100 * (now - st) / max(1.0, en - st))))
            entry = {'now': title, 'pct': pct}
            if nxt:
                entry['next'] = nxt
            if desc:
                entry['desc'] = desc
            for k in keys:
                if k not in out:
                    out[k] = entry
    return out


def status():
    with _lock:
        return dict(_state)


def _loop():
    while True:
        refresh_once()
        time.sleep(REFRESH_SECONDS)


def start():
    threading.Thread(target=_loop, daemon=True).start()
