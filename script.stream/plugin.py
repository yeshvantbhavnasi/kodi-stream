# Entry used when Stream is opened from Video add-ons: hand over to the full-screen interface.
import sys

import xbmc
import xbmcplugin

xbmcplugin.endOfDirectory(int(sys.argv[1]), succeeded=False)
xbmc.executebuiltin('RunScript(special://home/addons/script.stream/default.py)')
