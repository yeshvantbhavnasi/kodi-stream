# A local activity log for debugging: what the viewer did, what failed, and whether the last session ended cleanly.
# It stays on the device (stream.log in the addon's data folder) and is shown under Settings > Activity log.
import json
import os
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
    """Append one line: time, event name, and any details as JSON. Details may use any key, including "kind"."""
    try:
        if not os.path.isdir(FOLDER):
            os.makedirs(FOLDER)
        if os.path.exists(PATH) and os.path.getsize(PATH) > MAX_BYTES:
            os.replace(PATH, PATH + '.1')
        line = '{0} {1}'.format(time.strftime('%Y-%m-%d %H:%M:%S'), _name)
        if fields:
            line += ' ' + json.dumps(fields, ensure_ascii=False, default=str)
        with open(PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
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
    try:
        os.remove(FLAG)
    except Exception:
        pass
