"""Downloads: movies and episodes saved on the device to watch without a connection.

The home screen adds an entry once it has the title's real video address (Sasta TV only hands that to Kodi's player,
see default.download); Stream's service, which runs for as long as Kodi does, then fetches the file. Each download
is one small JSON record beside its video in the profile folder, so the screen and the service never write the same
file at the same moment: the screen creates, cancels and deletes records, the service fills in progress.
"""
import hashlib
import json
import os
import shutil
import time

import xbmcvfs

DIR = xbmcvfs.translatePath('special://profile/addon_data/script.stream/downloads')
CHUNK = 1 << 20
SAVE_EVERY = 3  # seconds between progress updates while a file is coming in
VIDEO_EXT = ('.mp4', '.mkv', '.m4v', '.mov', '.avi', '.webm')

# states: queued -> downloading -> done; failed (with a reason) or expired (the address ran out: fetch a new one)
ACTIVE = ('queued', 'downloading')


def _ensure():
    os.makedirs(DIR, exist_ok=True)


def ident(key):
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def _record_path(did):
    return os.path.join(DIR, did + '.json')


def load(did):
    try:
        with open(_record_path(did), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(entry):
    _ensure()
    tmp = _record_path(entry['id']) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(entry, f)
    os.replace(tmp, _record_path(entry['id']))


def all_entries():
    if not os.path.isdir(DIR):
        return []
    out = []
    for name in os.listdir(DIR):
        if name.endswith('.json'):
            entry = load(name[:-5])
            if entry:
                out.append(entry)
    return sorted(out, key=lambda e: e.get('added', 0), reverse=True)


def find(key):
    return load(ident(key)) if key else None


def video_path(entry):
    return os.path.join(DIR, entry['file'])


def thumb_path(entry):
    path = os.path.join(DIR, entry['id'] + '.jpg')
    return path if os.path.exists(path) else ''


def extension(url):
    path = url.split('?', 1)[0].lower()
    return next((ext for ext in VIDEO_EXT if path.endswith(ext)), '.mp4')


def add(key, url, headers, item, parent, group, series, label, plot='', meta=''):
    """Queue a title (or give an expired or failed one a fresh address). Returns the record."""
    did = ident(key)
    entry = load(did) or {'id': did, 'key': key, 'added': time.time(), 'bytes': 0, 'size': 0}
    entry.update(url=url, headers=headers, item=item, parent=parent, group=group or '', series=series or '',
                 label=label, plot=plot or entry.get('plot', ''), meta=meta or entry.get('meta', ''),
                 file=did + extension(url), state='queued', reason='', art=item.get('img', ''))
    save(entry)
    return entry


def cancel(entry):
    """Stop a download (the service notices the record is gone) and delete everything it saved."""
    for path in (_record_path(entry['id']), video_path(entry), video_path(entry) + '.part',
                 os.path.join(DIR, entry['id'] + '.jpg')):
        try:
            os.remove(path)
        except OSError:
            pass


def size_text(n):
    if n >= 1 << 30:
        return '{0:.1f} GB'.format(n / float(1 << 30))
    return '{0} MB'.format(max(1, int(n / float(1 << 20))))


def status(entry):
    """One line for the tile: progress, a wait, or what went wrong."""
    state = entry.get('state')
    if state == 'done':
        return size_text(entry.get('size') or entry.get('bytes') or 0)
    if state == 'queued':
        return 'Waiting to download'
    if state == 'downloading':
        got, total = entry.get('bytes', 0), entry.get('size', 0)
        if total:
            return '{0}% · {1} of {2}'.format(int(100 * got / total), size_text(got), size_text(total))
        return 'Downloading · ' + size_text(got)
    if state == 'expired':
        return 'Paused: press and hold to continue'
    return 'Failed: ' + (entry.get('reason') or 'press and hold to try again')


def percent(entry):
    total = entry.get('size', 0)
    return int(100 * entry.get('bytes', 0) / total) if total else 0


# ---------- the service side ----------

def next_job():
    jobs = [e for e in all_entries() if e.get('state') in ACTIVE]
    return min(jobs, key=lambda e: e.get('added', 0)) if jobs else None


def _fetch_art(entry, requests):
    target = os.path.join(DIR, entry['id'] + '.jpg')
    if os.path.exists(target) or not entry.get('art', '').startswith('http'):
        return
    try:
        r = requests.get(entry['art'], timeout=20)
        if r.ok:
            with open(target, 'wb') as f:
                f.write(r.content)
    except Exception:
        pass


def _update(entry, **changes):
    """Write progress unless the record was cancelled meanwhile. False once it is gone."""
    current = load(entry['id'])
    if current is None or current.get('url') != entry.get('url'):
        return False  # deleted, or given a new address by the screen: drop this attempt
    current.update(changes)
    save(current)
    entry.update(changes)
    return True


def work(entry, monitor, log):
    """Download one record until it is finished, cancelled, out of date, or Kodi is closing."""
    import requests  # script.module.requests; imported here so the screen does not pay for it

    _fetch_art(entry, requests)
    part = video_path(entry) + '.part'
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = dict(entry.get('headers') or {})
    if have:
        headers['Range'] = 'bytes={0}-'.format(have)
    try:
        r = requests.get(entry['url'], headers=headers, stream=True, timeout=30)
    except Exception as e:
        log('download {0}: no connection ({1})'.format(entry['label'], e))
        _update(entry, state='downloading', reason='')
        return 'retry'
    with r:
        if r.status_code in (401, 403, 410):
            _update(entry, state='expired', reason='the link expired')
            return 'expired'
        if r.status_code == 416 and have:
            os.replace(part, video_path(entry))  # already complete
            _update(entry, state='done', bytes=have, size=have)
            return 'done'
        if r.status_code not in (200, 206):
            _update(entry, state='failed', reason='the server answered {0}'.format(r.status_code))
            return 'failed'
        if r.status_code == 200:
            have = 0  # the server ignored the range: start over
        total = have + int(r.headers.get('Content-Length') or 0)
        if total:
            free = shutil.disk_usage(DIR).free
            if total - have > free - (200 << 20):
                _update(entry, state='failed', reason='not enough space ({0} needed)'.format(size_text(total - have)))
                return 'failed'
        if not _update(entry, state='downloading', size=total, bytes=have, reason=''):
            return 'cancelled'
        last = time.time()
        with open(part, 'ab' if have else 'wb') as f:
            try:
                for chunk in r.iter_content(CHUNK):
                    if monitor.abortRequested():
                        return 'stopped'
                    f.write(chunk)
                    have += len(chunk)
                    if time.time() - last >= SAVE_EVERY:
                        last = time.time()
                        if not _update(entry, bytes=have):
                            break
            except Exception as e:
                log('download {0}: interrupted at {1} ({2})'.format(entry['label'], size_text(have), e))
                _update(entry, bytes=have)
                return 'retry'
    if load(entry['id']) is None:
        try:
            os.remove(part)
        except OSError:
            pass
        return 'cancelled'
    if total and have < total:
        _update(entry, bytes=have)
        return 'retry'
    os.replace(part, video_path(entry))
    _update(entry, state='done', bytes=have, size=have)
    log('download {0}: finished, {1}'.format(entry['label'], size_text(have)))
    return 'done'
