# Runs once when Kodi starts. If "Open Stream when Kodi starts" is on, it opens Stream as soon as Kodi's
# own home screen is showing, so starting Kodi goes straight to Stream.
import xbmc
import xbmcaddon

if xbmcaddon.Addon('script.stream').getSetting('autostart') != 'false':
    monitor = xbmc.Monitor()
    for _ in range(60):
        if xbmc.getCondVisibility('Window.IsVisible(home)'):
            break
        if monitor.waitForAbort(0.5):
            break
    if not monitor.abortRequested():
        xbmc.executebuiltin('RunScript(special://home/addons/script.stream/default.py)')
