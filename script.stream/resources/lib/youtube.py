# Reads the YouTube add-on's lists through Kodi, so YouTube videos can sit in Stream's rows and play through that
# add-on (which fetches the video streams directly, so no advertisements are served). Nothing here talks to YouTube.
import json
import re
from urllib.parse import unquote

import xbmc
import xbmcaddon
import xbmcgui

PLUGIN = 'plugin://plugin.video.youtube/'
ADDON_ID = 'plugin.video.youtube'

# The lists on the YouTube tab, in order: (title, path, needs the viewer's own API key). Related Videos works without
# one; asking the add-on for the others without a key makes it show a "key.requirement" dialog, so those lists are
# only requested once a key is entered (YouTube add-on > Settings > API).
LISTS = [
    ('Recommendations', 'special/recommendations', True),
    ('My Subscriptions', 'special/my_subscriptions', True),
    ('History', 'special/playlist/HL/', True),
    ('Related Videos', 'special/related_videos', False),
    ('Watch Later', 'special/playlist/WL/', True),
    ('Subscribed Channels', 'subscriptions/list/', True),
    ('Trending', 'special/popular_right_now', True),
    ('Live broadcast', 'special/live', True),
]
KEY_HELP = ('Subscriptions, History, Recommendations and Trending need your own (free) Google API key in the YouTube '
            'add-on: Settings > YouTube: sign in and keys > Add your API key.')
# Google no longer gives the Kodi add-on a shared key, so an account's own lists are only reachable with a key the
# viewer creates. Shown on screen; the add-on's web page lets the values be pasted from a phone or computer.
KEY_STEPS = (
    'Signing in alone gives Related Videos and ad-free playback. Your subscriptions, history and recommendations '
    'come through the YouTube Data API, which Google only allows with your own free API key. One-time setup, about '
    'five minutes on a computer:\n\n'
    '1. Open console.cloud.google.com, sign in with your Google account and create a project (any name).\n'
    '2. APIs & Services > Library: search "YouTube Data API v3" and enable it.\n'
    '3. APIs & Services > OAuth consent screen (Google Auth Platform): user type External, fill in the app name '
    'and your email, then choose PUBLISH APP so the status is "In production". In "Testing" status Google signs '
    'you out again after 7 days.\n'
    '4. APIs & Services > Credentials > Create credentials: first an API key, then an OAuth client ID of type '
    '"TVs and Limited Input devices". Note the API key, client ID and client secret.\n'
    '5. Press OK here, open the address shown on a phone or computer on the same network, paste the three values '
    'and save.\n'
    '6. Back here, choose Sign in and enter the code at google.com/device. Google warns that the app is not '
    'verified; choose Advanced / Continue, it is your own project.'
)
TAGS = re.compile(r'\[/?(B|I|COLOR[^\]]*|UPPERCASE|LOWERCASE|CR)\]', re.I)
SKIP = ('sign in', 'sign out', 'switch user', 'setup wizard', 'go back...', 'search')


def installed():
    return xbmc.getCondVisibility('System.HasAddon({0})'.format(ADDON_ID))


def has_key():
    """True once the viewer's own API key, client ID and client secret are in the YouTube add-on's settings."""
    if not installed():
        return False
    try:
        addon = xbmcaddon.Addon(ADDON_ID)
        return all(addon.getSetting(name).strip() for name in ('youtube.api.key', 'youtube.api.id', 'youtube.api.secret'))
    except Exception:
        return False


def _tell_service(**data):
    """A message to the YouTube add-on's background service (the same channel its own scripts use)."""
    xbmc.executeJSONRPC(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'JSONRPC.NotifyAll',
                                    'params': {'sender': ADDON_ID, 'message': 'service_ipc', 'data': data}}))


def key_page():
    """Switch on the add-on's API-key web page, wake its web server and return the page's address on this network."""
    addon = xbmcaddon.Addon(ADDON_ID)
    if not addon.getSettingBool('youtube.api.config.page'):
        addon.setSettingBool('youtube.api.config.page', True)
        _tell_service(target='check_settings', state='process')
    _tell_service(target='server_wakeup', force=True)
    port = addon.getSetting('kodion.http.port').strip() or '50152'
    return 'http://{0}:{1}/youtube/api'.format(xbmc.getIPAddress(), port)


def sign_in():
    xbmc.executebuiltin('RunPlugin({0}sign/in/)'.format(PLUGIN))


def add_key():
    """Walk the viewer through creating a Google API key and pasting it into the add-on from another device."""
    dialog = xbmcgui.Dialog()
    dialog.textviewer('YouTube: add your API key', KEY_STEPS)
    link = key_page()
    xbmc.sleep(1500)  # the web server takes a moment to come up
    while True:
        if not dialog.yesno('YouTube: add your API key',
                            'On a phone or computer on this network open\n[B]{0}[/B]\n'
                            'paste the API key, client ID and client secret, and press Save.'.format(link),
                            yeslabel='Saved, sign in', nolabel='Later'):
            return
        if has_key():
            sign_in()
            return
        if not dialog.yesno('YouTube: add your API key', 'The add-on has no key yet. Check the page saved all three values.',
                            yeslabel='Show address again', nolabel='Later'):
            return


def lists():
    """The lists worth asking for right now."""
    key = has_key()
    return [(title, path) for title, path, needs_key in LISTS if key or not needs_key]


def is_youtube(url):
    return (url or '').startswith(PLUGIN)


def url(path):
    return PLUGIN + path


def video_id(link):
    m = re.search(r'[?&]video_id=([\w-]+)', link or '')
    return m.group(1) if m else None


def clean(label):
    return TAGS.sub('', label or '').strip()


def _image(value):
    if not value:
        return None
    m = re.match(r'^image://(.+?)/?$', value)
    link = unquote(m.group(1)) if m else value
    return link if re.match(r'^https?://', link) else None


def classify(entry):
    """An item dict for a listing entry, or None for actions and anything Stream cannot show."""
    name = clean(entry.get('title') or entry.get('label'))
    link = entry.get('file') or ''
    if not name or not is_youtube(link) or name.lower() in SKIP:
        return None
    item = {'name': name, 'url': link, 'yt': 1}
    if entry.get('filetype') == 'directory':
        item['t'] = 'next' if 'next page' in name.lower() else 'folder'
    elif '/play/' in link:
        item['t'] = 'play'
    else:
        return None
    art = entry.get('art') or {}
    img = _image(entry.get('thumbnail')) or _image(art.get('thumb')) or _image(art.get('poster'))
    if img:
        item['img'] = img
    plot = clean(entry.get('plot'))
    if plot:
        item['plot'] = plot
    return item


def listing(rpc, link):
    result = rpc('Files.GetDirectory', {'directory': link, 'media': 'video', 'properties': ['title', 'thumbnail', 'art', 'plot']})
    return [i for i in (classify(e) for e in result.get('files') or []) if i]


def open_settings():
    """The YouTube add-on's own settings, where the API key is entered and the sign-in lives."""
    if not installed():
        xbmcgui.Dialog().ok('Stream', 'Install "YouTube" from Add-ons > Install from repository > Video add-ons first.')
        return
    state = 'API key entered' if has_key() else 'no API key yet'
    choice = xbmcgui.Dialog().select('YouTube  -  ' + state,
                                     ['Sign in to YouTube', 'Add your API key (paste it from a phone or computer)',
                                      'Sign out', 'YouTube add-on settings (quality, API)'])
    if choice == 0:
        sign_in()
    elif choice == 1:
        add_key()
    elif choice == 2:
        xbmc.executebuiltin('RunPlugin({0}sign/out/)'.format(PLUGIN))
    elif choice == 3:
        xbmcaddon.Addon(ADDON_ID).openSettings()
