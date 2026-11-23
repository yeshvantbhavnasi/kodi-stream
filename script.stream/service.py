# Runs for as long as Kodi does. If "Open Stream when Kodi starts" is on, it opens Stream as soon as Kodi's
# own home screen is showing, so starting Kodi goes straight to Stream. When Kodi shuts down while Stream is
# open, it closes Stream's session record so the next start does not take the shutdown for a crash. Meanwhile it
# fetches any downloads queued from the home screen, so they carry on after Stream itself is closed.
import json
import os
import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon('script.stream')
sys.path.insert(0, os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo('path')), 'resources', 'lib'))
import audit  # noqa: E402


def _total_ram_mb():
    try:
        with open('/proc/meminfo', 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    try:
        return (os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')) // (1024 * 1024)
    except Exception:
        return 0


def _tune_kodi_cache():
    """Tune Kodi 21+ built-in filecache settings once per device, scaled to total RAM."""
    profile_dir = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
    marker = os.path.join(profile_dir, 'cache_tuned_v1')
    if os.path.exists(marker):
        return
    ram_mb = _total_ram_mb()
    if ram_mb <= 0 or ram_mb <= 2048:
        mem_mb = 32   # Fire TV Stick 4K (~1.6 GB RAM): 32 MB buffer (~96 MB peak), safe against OOM
    elif ram_mb <= 4096:
        mem_mb = 64
    else:
        mem_mb = 128
    desired = {
        'filecache.buffermode': 4,       # Buffer all network filesystems (HTTP/HTTPS/SMB/NFS)
        'filecache.memorysize': mem_mb,  # MB (valid Kodi 21 option)
        'filecache.readfactor': 0,       # Adaptive fill rate
        'filecache.chunksize': 262144,   # 256 KB chunks for smoother Wi-Fi/VOD throughput
    }
    applied = {}
    for sid, val in desired.items():
        try:
            raw = xbmc.executeJSONRPC(json.dumps({
                'jsonrpc': '2.0', 'id': 1,
                'method': 'Settings.SetSettingValue',
                'params': {'setting': sid, 'value': val},
            }))
            res = json.loads(raw)
            if res.get('result') is True:
                applied[sid] = val
        except Exception:
            pass
    if applied:
        try:
            os.makedirs(profile_dir, exist_ok=True)
            with open(marker, 'w', encoding='utf-8') as f:
                json.dump({'ram_mb': ram_mb, 'applied': applied}, f)
        except Exception:
            pass
        xbmc.log('[script.stream] tuned Kodi filecache (ram=%dMB): %s' % (ram_mb, applied), xbmc.LOGINFO)


_tune_kodi_cache()

monitor = xbmc.Monitor()
if ADDON.getSetting('autostart') != 'false':
    # In the Stream Android app, service.stream.intro sets this property, plays the intro over a cover and
    # opens Stream itself, so Kodi's own home screen is never seen; then there is nothing to do here.
    intro = 'Window(Home).Property(stream.intro)'
    for _ in range(60):
        if xbmc.getCondVisibility('Window.IsVisible(home) | !String.IsEmpty(%s)' % intro):
            break
        if monitor.waitForAbort(0.5):
            break
    if not monitor.abortRequested() and xbmc.getCondVisibility('String.IsEmpty(%s)' % intro):
        xbmc.executebuiltin('RunScript(script.stream)')

import downloads  # noqa: E402

# For as long as Kodi runs: fetch queued downloads one at a time (none are ever queued on a TV, so this only waits).
waits = {'retry': 10, 'expired': 1, 'failed': 1, 'done': 1, 'cancelled': 1}
while not monitor.abortRequested():
    job = downloads.next_job()
    if job is None:
        if monitor.waitForAbort(3):
            break
        continue
    result = downloads.work(job, monitor, lambda text: xbmc.log('[script.stream] ' + text, xbmc.LOGINFO))
    if result == 'done':
        xbmcgui.Dialog().notification('Stream', 'Downloaded: ' + job['label'], ADDON.getAddonInfo('icon'), 4000)
    elif result in ('expired', 'failed'):
        xbmcgui.Dialog().notification('Stream', 'Download paused: ' + job['label'], ADDON.getAddonInfo('icon'), 4000)
    if result == 'stopped' or monitor.waitForAbort(waits.get(result, 3)):
        break
if os.path.exists(audit.FLAG):
    audit.event('kodi_quit')
    audit.session_end()
