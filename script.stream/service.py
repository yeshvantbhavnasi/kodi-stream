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


def _ensure_stream_skin():
    """Enable and activate skin.stream automatically when installed so the native OSD and zero-Kodi UI apply."""
    try:
        raw = xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 1,
            'method': 'Addons.GetAddonDetails',
            'params': {'addonid': 'skin.stream', 'properties': ['enabled']},
        }))
        res = json.loads(raw).get('result', {}).get('addon')
        if not res:
            return
        if not res.get('enabled'):
            xbmc.executeJSONRPC(json.dumps({
                'jsonrpc': '2.0', 'id': 2,
                'method': 'Addons.SetAddonEnabled',
                'params': {'addonid': 'skin.stream', 'enabled': True},
            }))
        cur = json.loads(xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 3,
            'method': 'Settings.GetSettingValue',
            'params': {'setting': 'lookandfeel.skin'},
        }))).get('result', {}).get('value')
        if cur != 'skin.stream':
            xbmc.executeJSONRPC(json.dumps({
                'jsonrpc': '2.0', 'id': 4,
                'method': 'Settings.SetSettingValue',
                'params': {'setting': 'lookandfeel.skin', 'value': 'skin.stream'},
            }))
            xbmc.log('[script.stream] switched active skin from %s to skin.stream' % cur, xbmc.LOGINFO)
            # Auto-accept Kodi's "Would you like to keep this change?" confirmation dialog
            for _ in range(20):
                if xbmc.getCondVisibility('Window.IsActive(yesnodialog)'):
                    xbmc.executebuiltin('SendClick(11)')
                    xbmc.sleep(300)
                    break
                xbmc.sleep(150)
    except Exception as e:
        xbmc.log('[script.stream] skin check skipped: %s' % e, xbmc.LOGINFO)


def _tune_advanced_settings():
    """Write RAM-saving texture & dirty-region settings to userdata/advancedsettings.xml once per device."""
    try:
        adv_path = xbmcvfs.translatePath('special://userdata/advancedsettings.xml')
        if os.path.exists(adv_path):
            with open(adv_path, 'r', encoding='utf-8') as f:
                if 'imageres' in f.read():
                    return
        os.makedirs(os.path.dirname(adv_path), exist_ok=True)
        with open(adv_path, 'w', encoding='utf-8') as f:
            f.write(
                '<advancedsettings>\n'
                '  <splash>false</splash>\n'
                '  <imageres>540</imageres>\n'
                '  <fanartres>720</fanartres>\n'
                '  <gui>\n'
                '    <algorithmdirtyregions>3</algorithmdirtyregions>\n'
                '  </gui>\n'
                '</advancedsettings>\n'
            )
        xbmc.log('[script.stream] wrote RAM-optimized advancedsettings.xml', xbmc.LOGINFO)
    except Exception as e:
        xbmc.log('[script.stream] advancedsettings write skipped: %s' % e, xbmc.LOGINFO)


def _apply_staged_yt_keys():
    """Apply locally staged YouTube API keys from /sdcard/Download/yt_keys.json (never committed to git) and delete the file."""
    staged = '/storage/emulated/0/Download/yt_keys.json'
    if not os.path.exists(staged):
        return
    try:
        data = json.load(open(staged, 'r', encoding='utf-8'))
        yt = xbmcaddon.Addon('plugin.video.youtube')
        if data.get('api_key'):
            yt.setSetting('youtube.api.key', data['api_key'])
        if data.get('client_id'):
            yt.setSetting('youtube.api.id', data['client_id'])
        if data.get('client_secret'):
            yt.setSetting('youtube.api.secret', data['client_secret'])
        yt_profile = xbmcvfs.translatePath(yt.getAddonInfo('profile'))
        ak_path = os.path.join(yt_profile, 'api_keys.json')
        ak = json.load(open(ak_path, 'r', encoding='utf-8')) if os.path.exists(ak_path) else {'keys': {'developer': {}, 'user': {}}}
        user_keys = ak.setdefault('keys', {}).setdefault('user', {})
        for k in ('api_key', 'client_id', 'client_secret'):
            if data.get(k):
                user_keys[k] = data[k]
        os.makedirs(yt_profile, exist_ok=True)
        json.dump(ak, open(ak_path, 'w', encoding='utf-8'), indent=4)
        os.remove(staged)
        xbmc.log('[script.stream] applied staged YouTube API keys and cleaned up', xbmc.LOGINFO)
    except Exception as e:
        xbmc.log('[script.stream] staged YouTube keys skipped: %s' % e, xbmc.LOGINFO)


_tune_advanced_settings()
_tune_kodi_cache()
_ensure_stream_skin()
_apply_staged_yt_keys()

monitor = xbmc.Monitor()

# Launch local native stream-core engine (Go binary) if bundled/installed on this device.
_core_proc = None
try:
    import subprocess
    _candidates = [
        os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo('path')), 'bin', 'stream-core'),
        os.path.expanduser('~/.local/bin/stream-core'),
        '/Users/yeshvant/dev/kodi-stream/core/stream-core',
    ]
    _core_bin = next((p for p in _candidates if os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if _core_bin:
        _profile_dir = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
        os.makedirs(_profile_dir, exist_ok=True)
        _env = os.environ.copy()
        # Bind on LAN on full-RAM hosts (Mac mini) so low-RAM TVs can use this Go core hub with zero TV RAM!
        _env.setdefault('STREAM_CORE_ADDR', '127.0.0.1:8775' if _total_ram_mb() <= 2048 else '0.0.0.0:8775')
        _env.setdefault('STREAM_CORE_DATA', _profile_dir)
        if _total_ram_mb() <= 2048:
            _env.setdefault('GOMEMLIMIT', '40MiB')
        _core_proc = subprocess.Popen(
            [_core_bin],
            env=_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        xbmc.log('[script.stream] started native stream-core (%s pid=%d)' % (_core_bin, _core_proc.pid), xbmc.LOGINFO)
except Exception as _e:
    xbmc.log('[script.stream] stream-core autostart skipped: %s' % _e, xbmc.LOGINFO)

if ADDON.getSetting('autostart') != 'false':
    # skin.stream's Home.xml launches script.stream directly on load (setting stream.launching).
    # In the Stream Android app, service.stream.intro sets stream.intro. Only launch from here
    # if neither has already started Stream (e.g. when running under another skin).
    intro = 'Window(Home).Property(stream.intro)'
    launching = 'Window(Home).Property(stream.launching)'
    running = 'Window(Home).Property(script.stream.running)'
    for _ in range(40):
        if xbmc.getCondVisibility('Window.IsVisible(home) | !String.IsEmpty(%s) | !String.IsEmpty(%s) | !String.IsEmpty(%s)' % (intro, launching, running)):
            break
        if monitor.waitForAbort(0.25):
            break
    # Wait until no modal dialog (such as skin confirmation) is blocking window activation.
    for _ in range(30):
        if not xbmc.getCondVisibility('Window.IsActive(yesnodialog)'):
            break
        if monitor.waitForAbort(0.2):
            break
    if not monitor.abortRequested() and xbmc.getCondVisibility('String.IsEmpty(%s) + String.IsEmpty(%s) + String.IsEmpty(%s)' % (intro, launching, running)):
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
if _core_proc is not None:
    try:
        _core_proc.terminate()
        _core_proc.wait(timeout=3)
    except Exception:
        try:
            _core_proc.kill()
        except Exception:
            pass
if os.path.exists(audit.FLAG):
    audit.event('kodi_quit')
    audit.session_end()
