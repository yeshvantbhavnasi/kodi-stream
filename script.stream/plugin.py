# Entry used when Stream is opened from Video add-ons: hand over to the full-screen interface.
# plugin://script.stream/?youtube opens only the YouTube sign-in and API key helper.
import sys

import xbmc
import xbmcplugin

xbmcplugin.endOfDirectory(int(sys.argv[1]), succeeded=False)
helper = len(sys.argv) > 2 and 'youtube' in sys.argv[2]
xbmc.executebuiltin('RunScript(special://home/addons/script.stream/default.py{0})'.format(',youtube' if helper else ''))
