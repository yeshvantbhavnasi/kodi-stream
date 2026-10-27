# Runs for as long as Kodi does. If "Open Stream when Kodi starts" is on, it opens Stream as soon as Kodi's
# own home screen is showing, so starting Kodi goes straight to Stream. When Kodi shuts down while Stream is
# open, it closes Stream's session record so the next start does not take the shutdown for a crash. Meanwhile it
# fetches any downloads queued from the home screen, so they carry on after Stream itself is closed.
import os
import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon('script.stream')
sys.path.insert(0, os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo('path')), 'resources', 'lib'))
import audit  # noqa: E402

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
