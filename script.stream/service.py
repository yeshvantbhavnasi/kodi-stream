# Runs for as long as Kodi does. If "Open Stream when Kodi starts" is on, it opens Stream as soon as Kodi's
# own home screen is showing, so starting Kodi goes straight to Stream. When Kodi shuts down while Stream is
# open, it closes Stream's session record so the next start does not take the shutdown for a crash.
import os
import sys

import xbmc
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon('script.stream')
sys.path.insert(0, os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo('path')), 'resources', 'lib'))
import audit  # noqa: E402

monitor = xbmc.Monitor()
if ADDON.getSetting('autostart') != 'false':
    for _ in range(60):
        if xbmc.getCondVisibility('Window.IsVisible(home)'):
            break
        if monitor.waitForAbort(0.5):
            break
    if not monitor.abortRequested():
        xbmc.executebuiltin('RunScript(special://home/addons/script.stream/default.py)')

monitor.waitForAbort()
if os.path.exists(audit.FLAG):
    audit.event('kodi_quit')
    audit.session_end()
