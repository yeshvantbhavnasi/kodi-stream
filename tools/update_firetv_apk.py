#!/usr/bin/env python3
"""Update ~/stream-tv/Stream-21.3.apk with the latest Stream splash + auto-updater intro service,
re-align, re-sign with ~/stream-tv/stream-release.jks, and install -r onto Fire TV."""
import glob
import os
import shutil
import subprocess
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAM_TV = os.path.expanduser('~/stream-tv')
APK_PATH = os.path.join(STREAM_TV, 'Stream-21.3.apk')
BACKUP_APK = os.path.join(STREAM_TV, 'Stream-21.3.apk.orig')
KEYSTORE = os.path.join(STREAM_TV, 'stream-release.jks')
PW_FILE = os.path.join(STREAM_TV, 'keystore-password.txt')
SPLASH_PNG = os.path.join(ROOT, 'skin.stream', 'resources', 'splash.png')
MAGICK = shutil.which('magick') or '/opt/homebrew/bin/magick'

BUILD_TOOLS = sorted(glob.glob(os.path.expanduser('~/Library/Android/sdk/build-tools/*')))[-1]
ZIPALIGN = os.path.join(BUILD_TOOLS, 'zipalign')
APKSIGNER = os.path.join(BUILD_TOOLS, 'apksigner')

INTRO_SERVICE_PY = r'''import hashlib
import json
import os
import shutil
import zipfile

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

MIGRATE = '/storage/emulated/0/Download/StreamMigrate'
DOWNLOADS = '/storage/emulated/0/Download'
LOGS = '/storage/emulated/0/Download/StreamLogs'
HOME_WINDOW = xbmcgui.Window(10000)
ADDON_PATH = xbmcaddon.Addon().getAddonInfo('path')

HOME_WINDOW.setProperty('stream.intro', 'playing')


def _write_advanced_settings():
    try:
        adv = xbmcvfs.translatePath('special://userdata/advancedsettings.xml')
        if os.path.exists(adv):
            with open(adv, 'r', encoding='utf-8') as f:
                if 'imageres' in f.read():
                    return
        os.makedirs(os.path.dirname(adv), exist_ok=True)
        with open(adv, 'w', encoding='utf-8') as f:
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
        xbmc.log('[stream.intro] wrote RAM-optimized advancedsettings.xml', xbmc.LOGINFO)
    except Exception as e:
        xbmc.log('[stream.intro] advancedsettings skipped: %r' % (e,), xbmc.LOGWARNING)


def _install_staged_zips():
    """Install any staged script.stream / skin.stream zips from /sdcard/Download directly into addons/."""
    addons_dir = xbmcvfs.translatePath('special://home/addons/')
    marker_dir = xbmcvfs.translatePath('special://home/userdata/')
    installed_any = False
    for prefix in ('skin.stream-', 'script.stream-'):
        try:
            candidates = sorted(
                f for f in os.listdir(DOWNLOADS)
                if f.startswith(prefix) and f.endswith('.zip')
            )
        except OSError:
            candidates = []
        if not candidates:
            continue
        zip_name = candidates[-1]
        zip_path = os.path.join(DOWNLOADS, zip_name)
        try:
            sig = '%s:%d' % (zip_name, os.path.getsize(zip_path))
            marker = os.path.join(marker_dir, '.installed_' + prefix.rstrip('-'))
            if os.path.exists(marker) and open(marker, 'r', encoding='utf-8').read().strip() == sig:
                continue
            addon_id = prefix.rstrip('-')
            target_dir = os.path.join(addons_dir, addon_id)
            tmp_root = os.path.join(addons_dir, '.tmp_' + addon_id)
            shutil.rmtree(tmp_root, ignore_errors=True)
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmp_root)
            extracted = os.path.join(tmp_root, addon_id)
            if os.path.isdir(extracted):
                if os.path.isdir(target_dir):
                    shutil.rmtree(target_dir, ignore_errors=True)
                os.replace(extracted, target_dir)
                shutil.rmtree(tmp_root, ignore_errors=True)
                with open(marker, 'w', encoding='utf-8') as f:
                    f.write(sig)
                installed_any = True
                xbmc.log('[stream.intro] installed %s into %s' % (zip_name, target_dir), xbmc.LOGINFO)
        except Exception as e:
            xbmc.log('[stream.intro] staged zip %s failed: %r' % (zip_name, e), xbmc.LOGWARNING)
    if installed_any:
        xbmc.executebuiltin('UpdateLocalAddons')
        xbmc.sleep(1200)
    return installed_any


def _activate_stream_skin():
    try:
        xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 1,
            'method': 'Addons.SetAddonEnabled',
            'params': {'addonid': 'skin.stream', 'enabled': True},
        }))
        cur = json.loads(xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 2,
            'method': 'Settings.GetSettingValue',
            'params': {'setting': 'lookandfeel.skin'},
        }))).get('result', {}).get('value')
        if cur != 'skin.stream':
            xbmc.executeJSONRPC(json.dumps({
                'jsonrpc': '2.0', 'id': 3,
                'method': 'Settings.SetSettingValue',
                'params': {'setting': 'lookandfeel.skin', 'value': 'skin.stream'},
            }))
            for _ in range(20):
                if xbmc.getCondVisibility('Window.IsActive(yesnodialog)'):
                    xbmc.executebuiltin('SendClick(11)')
                    xbmc.sleep(300)
                    break
                xbmc.sleep(150)
            xbmc.log('[stream.intro] activated skin.stream (was %s)' % cur, xbmc.LOGINFO)
    except Exception as e:
        xbmc.log('[stream.intro] skin activation skipped: %r' % (e,), xbmc.LOGWARNING)


def export_logs():
    os.makedirs(LOGS, exist_ok=True)
    for src in ('special://temp/kodi.log', 'special://temp/kodi.old.log',
                'special://profile/addon_data/script.stream/stream.log'):
        path = xbmcvfs.translatePath(src)
        if os.path.exists(path):
            shutil.copyfile(path, os.path.join(LOGS, os.path.basename(path)))


def wait(monitor, condition, seconds):
    for _ in range(int(seconds * 20)):
        if condition() or monitor.waitForAbort(0.05):
            return


def run():
    monitor = xbmc.Monitor()
    try:
        wait(monitor, lambda: xbmc.getCondVisibility('Window.IsVisible(home)'), 30)
        _write_advanced_settings()
        _install_staged_zips()
        _activate_stream_skin()
        # Wait for any skin confirmation modal to clear
        wait(monitor, lambda: not xbmc.getCondVisibility('Window.IsActive(yesnodialog)'), 5)
        if not HOME_WINDOW.getProperty('script.stream.running'):
            xbmc.executebuiltin('RunScript(special://home/addons/script.stream/default.py)')
            wait(monitor, lambda: HOME_WINDOW.getProperty('script.stream.running')
                 and xbmcgui.getCurrentWindowId() >= 13000, 30)
    finally:
        HOME_WINDOW.setProperty('stream.ready', '1')


try:
    export_logs()
except OSError:
    pass
run()
try:
    export_logs()
except OSError:
    pass
'''


def main():
    if not os.path.exists(BACKUP_APK):
        shutil.copy2(APK_PATH, BACKUP_APK)
        print('Backed up original APK to', BACKUP_APK)

    with tempfile.TemporaryDirectory() as tmp:
        splash_jpg = os.path.join(tmp, 'splash.jpg')
        subprocess.run([MAGICK, SPLASH_PNG, '-quality', '92', splash_jpg], check=True)
        splash_bytes = open(splash_jpg, 'rb').read()

        unsigned_apk = os.path.join(tmp, 'unsigned.apk')
        aligned_apk = os.path.join(tmp, 'aligned.apk')

        # Repack APK preserving STORED entries (critical for Android resources.arsc & native .so libs)
        with zipfile.ZipFile(BACKUP_APK, 'r') as zin, zipfile.ZipFile(unsigned_apk, 'w') as zout:
            for item in zin.infolist():
                if item.filename.startswith('META-INF/'):
                    continue
                if item.filename == 'assets/media/splash.jpg':
                    zi = zipfile.ZipInfo(item.filename)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(zi, splash_bytes)
                elif item.filename == 'assets/addons/service.stream.intro/service.py':
                    zi = zipfile.ZipInfo(item.filename)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(zi, INTRO_SERVICE_PY.encode('utf-8'))
                else:
                    data = zin.read(item.filename)
                    zi = zipfile.ZipInfo(item.filename)
                    zi.compress_type = item.compress_type
                    zi.external_attr = item.external_attr
                    zout.writestr(zi, data)

        subprocess.run([ZIPALIGN, '-f', '-p', '4', unsigned_apk, aligned_apk], check=True)
        pw = open(PW_FILE, 'r', encoding='utf-8').read().strip()
        env = os.environ.copy()
        jdk_bin = '/opt/homebrew/opt/openjdk/bin'
        if os.path.isdir(jdk_bin):
            env['PATH'] = jdk_bin + ':' + env.get('PATH', '')
            env['JAVA_HOME'] = '/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home'
        subprocess.run([
            APKSIGNER, 'sign',
            '--ks', KEYSTORE,
            '--ks-key-alias', 'stream',
            '--ks-pass', 'pass:' + pw,
            '--key-pass', 'pass:' + pw,
            '--out', APK_PATH,
            aligned_apk,
        ], env=env, check=True)
        print('Signed updated APK at', APK_PATH)


if __name__ == '__main__':
    main()
