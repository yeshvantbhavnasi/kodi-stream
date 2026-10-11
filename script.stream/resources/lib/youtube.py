# Reads the YouTube add-on's lists through Kodi, so YouTube videos can sit in Stream's rows and play through that
# add-on (which fetches the video streams directly, so no advertisements are served). Nothing here talks to YouTube.
import re
from urllib.parse import unquote

import xbmc
import xbmcgui

PLUGIN = 'plugin://plugin.video.youtube/'
ADDON_ID = 'plugin.video.youtube'

# The lists on the YouTube tab, in order: (title, path, needs the viewer's own API key). Related Videos works without
# one; asking the add-on for the others without a key makes it show a "key.requirement" dialog, so those lists are
# only requested once a key is entered (YouTube add-on > Settings > API).
LISTS = [
    ('Recommendations', 'special/recommendations', True),
    ('Related Videos', 'special/related_videos', False),
    ('My Subscriptions', 'special/my_subscriptions', True),
    ('Trending', 'special/popular_right_now', True),
    ('Live broadcast', 'special/live', True),
    ('Watch Later', 'special/playlist/WL/', True),
    ('Subscribed Channels', 'subscriptions/list/', True),
    ('History', 'special/playlist/HL/', True),
]
KEY_HELP = ('Recommendations, Subscriptions and Trending need the YouTube add-on signed in with your own API key: '
            'Settings > YouTube: sign in and keys.')
TAGS = re.compile(r'\[/?(B|I|COLOR[^\]]*|UPPERCASE|LOWERCASE|CR)\]', re.I)
SKIP = ('sign in', 'sign out', 'switch user', 'setup wizard', 'go back...', 'search')


def installed():
    return xbmc.getCondVisibility('System.HasAddon({0})'.format(ADDON_ID))


def has_key():
    """True once the viewer's own API key, client ID and client secret are in the YouTube add-on's settings."""
    if not installed():
        return False
    try:
        import xbmcaddon
        addon = xbmcaddon.Addon(ADDON_ID)
        return all(addon.getSetting(name).strip() for name in ('youtube.api.key', 'youtube.api.id', 'youtube.api.secret'))
    except Exception:
        return False


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
    choice = xbmcgui.Dialog().select('YouTube', ['Sign in to YouTube', 'Sign out', 'YouTube add-on settings (API key, quality)'])
    if choice == 0:
        xbmc.executebuiltin('RunPlugin({0}sign/in/)'.format(PLUGIN))
    elif choice == 1:
        xbmc.executebuiltin('RunPlugin({0}sign/out/)'.format(PLUGIN))
    elif choice == 2:
        import xbmcaddon
        xbmcaddon.Addon(ADDON_ID).openSettings()
