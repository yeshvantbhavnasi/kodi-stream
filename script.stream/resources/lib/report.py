# Sends problem reports and install notices to the maintainer by email, through a small relay
# (see relay/lambda_function.py in the repository). The relay holds no secrets and can only deliver
# to the maintainer's address. Nothing is sent automatically once the viewer switches reports off.
import os
import time

import xbmc
import xbmcaddon

import audit

ENDPOINT = 'https://lwiv6bxxeahejwbqmiu6a6ewde0thtrv.lambda-url.us-east-1.on.aws/'
MIN_GAP = 3600      # automatic problem reports: at most one an hour per device
MAX_CHARS = 9000
STAMP = os.path.join(audit.FOLDER, 'report.sent')


def automatic():
    """True unless the viewer has switched automatic reports off."""
    return xbmcaddon.Addon('script.stream').getSetting('report_enabled').strip() != 'false'


def _recently_sent():
    try:
        return time.time() - os.path.getmtime(STAMP) < MIN_GAP
    except OSError:
        return False


def send(reason, note='', manual=False, with_log=True, limited=True):
    """Email a report. Returns (sent, message for the viewer). Automatic sends respect the off switch and the hourly limit."""
    if not manual and (not automatic() or (limited and _recently_sent())):
        return False, 'Skipped.'
    addon = xbmcaddon.Addon('script.stream')
    platform = next((p for p in ('Android', 'OSX', 'Windows', 'Linux', 'IOS', 'TVOS')
                     if xbmc.getCondVisibility('System.Platform.' + p)), 'unknown')
    payload = {
        'app': 'stream', 'reason': reason, 'note': note, 'install': audit._install_id(),
        'version': addon.getAddonInfo('version'), 'kodi': xbmc.getInfoLabel('System.BuildVersion'),
        'platform': platform, 'device': xbmc.getInfoLabel('System.FriendlyName'), 'free_memory': audit.memory(),
        'log': audit.tail(160)[-MAX_CHARS:] if with_log else '',
    }
    try:
        import requests
        r = requests.post(ENDPOINT, json=payload, timeout=20)
        ok = r.status_code == 200 and r.json().get('success', False)
    except Exception as e:
        audit.event('report_failed', reason=reason, error=str(e))
        return False, 'Could not send the report. Check the internet connection.'
    if not ok:
        audit.event('report_failed', reason=reason, status=r.status_code)
        return False, 'The report could not be delivered.'
    if limited:
        try:
            with open(STAMP, 'w') as f:
                f.write(str(time.time()))
        except Exception:
            pass
    audit.event('report_sent', reason=reason)
    return True, 'Report sent. Thank you.'
