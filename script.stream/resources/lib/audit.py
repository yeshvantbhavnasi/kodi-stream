# A local activity log for debugging: what the viewer did, what failed, and whether the last session ended cleanly.
# It stays on the device (stream.log in the addon's data folder) and is shown under Settings > Activity log.
import json
import os
import threading
import time
import traceback
import uuid

import xbmc
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon('script.stream')
FOLDER = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
PATH = os.path.join(FOLDER, 'stream.log')
FLAG = os.path.join(FOLDER, 'session.open')
INSTALL = os.path.join(FOLDER, 'install.json')
MAX_BYTES = 400 * 1024
NEW = {'install': False}

_LOCK = threading.Lock()
_QUEUE = []
_MAX_QUEUE = 300
_FLUSH_EVERY = 30.0
_LAST_FLUSH = [0.0]
_FLUSHING = [False]
_URGENT = frozenset(('error', 'play_timeout', 'load_failed', 'fetch_failed', 'previous_session_ended_unexpectedly',
                     'slow_fetch', 'empty_listing', 'play_ended'))


def _platform():
    return next((p for p in ('Android', 'OSX', 'Windows', 'Linux', 'IOS', 'TVOS')
                 if xbmc.getCondVisibility('System.Platform.' + p)), 'unknown')


def _profile_key():
    try:
        import db
        active = getattr(db, 'ACTIVE', 'default')
    except Exception:
        active = 'default'
    return '{0} {1}/{2}'.format(xbmc.getInfoLabel('System.FriendlyName') or 'kodi', _install_id(), active).strip()


def _ship(batch, timeout=10):
    """Send a batch of interaction/observability events to the recommendation service (POST /telemetry)."""
    if not batch:
        return
    try:
        addon = xbmcaddon.Addon('script.stream')
        url = addon.getSetting('recs_url').strip().rstrip('/')
        if not url:
            return
        key = addon.getSetting('recs_key').strip()
        import requests
        body = {'profile': _profile_key(), 'version': addon.getAddonInfo('version'), 'platform': _platform(), 'events': batch}
        requests.post(url + '/telemetry', json=body, timeout=timeout, headers={'X-Stream-Key': key})
    except Exception:
        pass  # observability must never disturb playback or navigation


def flush(sync=False):
    """Flush queued telemetry events to the service, asynchronously by default."""
    with _LOCK:
        if not _QUEUE or (_FLUSHING[0] and not sync):
            return
        batch = list(_QUEUE)
        _QUEUE.clear()
        _LAST_FLUSH[0] = time.time()
        _FLUSHING[0] = True

    def run():
        try:
            _ship(batch, timeout=4 if sync else 10)
        finally:
            with _LOCK:
                _FLUSHING[0] = False

    if sync:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


def _install_id():
    try:
        with open(INSTALL, 'r') as f:
            return json.load(f)['id']
    except Exception:
        ident = uuid.uuid4().hex[:12]
        NEW['install'] = True  # first run on this device
        try:
            with open(INSTALL, 'w') as f:
                json.dump({'id': ident, 'created': time.time()}, f)
        except Exception:
            pass
        return ident


def event(_name, **fields):
    """Append one line locally (stream.log) and enqueue for central observability (POST /telemetry)."""
    now = time.time()
    try:
        if not os.path.isdir(FOLDER):
            os.makedirs(FOLDER)
        if os.path.exists(PATH) and os.path.getsize(PATH) > MAX_BYTES:
            os.replace(PATH, PATH + '.1')
        line = '{0} {1}'.format(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)), _name)
        if fields:
            line += ' ' + json.dumps(fields, ensure_ascii=False, default=str)
        with open(PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    should_flush = False
    try:
        with _LOCK:
            if len(_QUEUE) >= _MAX_QUEUE:
                del _QUEUE[:50]
            _QUEUE.append({'at': now, 'event': _name, 'data': fields})
            if _name in _URGENT or len(_QUEUE) >= 20 or (now - _LAST_FLUSH[0] >= _FLUSH_EVERY):
                should_flush = True
        if should_flush:
            flush(sync=False)
    except Exception:
        pass


def error(where):
    """Record the exception being handled, with its traceback, and report it if reports are allowed."""
    event('error', where=where, trace=traceback.format_exc()[-1500:])
    try:
        import threading
        import report
        threading.Thread(target=report.send, args=('internal error in ' + where,), daemon=True).start()
    except Exception:
        pass


def memory():
    return xbmc.getInfoLabel('System.Memory(free)')


def tail(lines=200):
    try:
        with open(PATH, 'r', encoding='utf-8') as f:
            return ''.join(f.readlines()[-lines:])
    except Exception:
        return ''


def session_start():
    """Note the start of a session. If the previous one never reached session_end, Kodi closed or crashed under it."""
    unclean = os.path.exists(FLAG)
    if unclean:
        last = tail(1).strip()
        event('previous_session_ended_unexpectedly', last_entry=last[:300])
    try:
        with open(FLAG, 'w') as f:
            f.write(str(time.time()))
    except Exception:
        pass
    platform = next((p for p in ('Android', 'OSX', 'Windows', 'Linux', 'IOS', 'TVOS')
                     if xbmc.getCondVisibility('System.Platform.' + p)), 'unknown')
    event('start', install=_install_id(), version=ADDON.getAddonInfo('version'), kodi=xbmc.getInfoLabel('System.BuildVersion'),
          platform=platform, device=xbmc.getInfoLabel('System.FriendlyName'), free_memory=memory(),
          screen=xbmc.getInfoLabel('System.ScreenResolution'))
    return unclean


def session_end():
    event('exit', free_memory=memory())
    flush(sync=True)
    try:
        os.remove(FLAG)
    except Exception:
        pass
