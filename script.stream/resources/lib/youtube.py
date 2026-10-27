# Reads the YouTube add-on's lists through Kodi, so YouTube videos can sit in Stream's rows and play through that
# add-on (which fetches the video streams directly, so no advertisements are served). Nothing here talks to YouTube.
import html
import json
import re
import threading
import time
from urllib.parse import parse_qs, quote, unquote

import xbmc
import xbmcaddon
import xbmcgui

import db

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
# Stream's own YouTube listings, served under the add-on's namespace so they cache and page like any other folder.
VIRTUAL = PLUGIN + 'stream/'
# Channels that are always on the YouTube tab (and on a kids profile's home), whether or not anyone follows them:
# (row title, channel id). Their newest uploads come from the channel's public feed, so no key or sign-in is needed.
PINNED = [('Ms Rachel', 'UCG2CL6EUjG8TVT1Tpl9nJdg'), ('Ms Apple', 'UCneD8oYNapJ7AfhTGfiuWvg')]
# What a toddler who is learning to talk watches: rows of Ms Rachel and Ms Apple videos by what they teach. Each is a
# YouTube search (no key needed) kept to those two channels, so the rows fill with their videos first.
TODDLER_ROWS = [
    ('First Words with Ms Rachel', 'Ms Rachel first words toddler'),
    ('Learn to Talk', 'Ms Rachel Ms Apple learn to talk speech practice'),
    ('Songs to Sing Along', 'Ms Rachel songs for littles sing along'),
    ('Animal Sounds', 'Ms Rachel animal sounds toddler'),
    ('Baby Sign Language', 'Ms Rachel baby sign language'),
    ('Colours, Shapes and Numbers', 'Ms Rachel learn colors shapes numbers toddler'),
    ('Ms Apple: Learn and Play', 'Ms Apple toddler learning toy play'),
]
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36')
_channel_cache = {}
TAGS = re.compile(r'\[/?(B|I|COLOR[^\]]*|UPPERCASE|LOWERCASE|CR)\]', re.I)
# Emoji and other pictographs: Kodi's fonts have no glyphs for them, and a title that starts with one stops Kodi's
# label rendering ("[B]" is all that is shown), so they are dropped from names.
EMOJI = re.compile('[\U00010000-\U0010FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+')
SKIP = ('sign in', 'sign out', 'switch user', 'setup wizard', 'go back...', 'search')


def installed():
    return xbmc.getCondVisibility('System.HasAddon({0})'.format(ADDON_ID))


def ensure_installed(purpose='play YouTube videos'):
    """True when Kodi's YouTube add-on is there. Otherwise offer to install it from Kodi's own repository and return False,
    so nothing else in Stream has to deal with a missing add-on."""
    if installed():
        return True
    if xbmcgui.Dialog().yesno('Stream', 'Kodi\'s YouTube add-on is needed to {0}.\nInstall it now? Everything else in Stream works without it.'.format(purpose),
                              yeslabel='Install', nolabel='Not now'):
        xbmc.executebuiltin('InstallAddon({0})'.format(ADDON_ID))
    return False


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
    return re.sub(r'\s{2,}', ' ', EMOJI.sub('', TAGS.sub('', label or ''))).strip()


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
    if entry.get('studio'):
        item['by'] = clean(entry['studio'])  # the YouTube add-on files the channel name here
    try:
        if int(entry.get('duration') or 0) > 0:
            item['duration'] = int(entry['duration'])
    except (TypeError, ValueError):
        pass
    return item


def listing(rpc, link):
    if link.startswith(VIRTUAL):
        return virtual(link[len(VIRTUAL):], rpc)
    if not installed():
        return []  # a saved row from before the add-on was removed: shown as empty rather than failing
    result = rpc('Files.GetDirectory', {'directory': link, 'media': 'video',
                                        'properties': ['title', 'thumbnail', 'art', 'plot', 'studio', 'duration']})
    return [i for i in (classify(e) for e in result.get('files') or []) if i]


# ---------- without an API key: YouTube's public pages and feeds ----------

def _get(link, timeout=12):
    import requests
    r = requests.get(link, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError('HTTP {0} for {1}'.format(r.status_code, link))
    return r.text


def _video(video_id, name, by='', when=''):
    return {'t': 'play', 'name': name, 'url': play_url(video_id), 'yt': 1, 'by': by, 'when': when,
            'img': 'https://i.ytimg.com/vi/{0}/hqdefault.jpg'.format(video_id)}


def play_url(video_id):
    return '{0}play/?video_id={1}'.format(PLUGIN, video_id)


def _unescape(text):
    return html.unescape(json.loads('"' + text + '"')) if '\\' in text else html.unescape(text)


def search(query, limit=24):
    """Videos matching a search, read from the results page (YouTube's search API needs a key)."""
    page = _get('https://www.youtube.com/results?search_query=' + quote(query))
    out, seen = [], set()
    pattern = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})"(.{0,4000}?)"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)+)"')
    for m in pattern.finditer(page):
        video_id, body, title = m.group(1), m.group(2), m.group(3)
        if video_id in seen:
            continue
        seen.add(video_id)
        owner = re.search(r'"ownerText":\{"runs":\[\{"text":"((?:[^"\\]|\\.)+)"', page[m.end():m.end() + 3000])
        out.append(_video(video_id, clean(_unescape(title)), _unescape(owner.group(1)) if owner else ''))
        if len(out) >= limit:
            break
    return out


def trailer(title, year='', show=False):
    """The video id of a trailer for a movie or show, found by searching YouTube."""
    query = ' '.join(p for p in (title, year, 'official trailer') if p)
    try:
        found = search(query, 10)
    except Exception as e:
        xbmc.log('STREAM YT: trailer search failed: {0}'.format(e), xbmc.LOGINFO)
        return None
    words = [w for w in re.findall(r'\w+', title.lower()) if len(w) > 2]
    for item in found:
        name = item['name'].lower()
        if re.search(r'trailer|teaser', name) and (not words or any(w in name for w in words)):
            return video_id(item['url'])
    return video_id(found[0]['url']) if found else None


def channel_of(video):
    """(channel id, channel name) for a video, read from its watch page."""
    if video in _channel_cache:
        return _channel_cache[video]
    page = _get('https://www.youtube.com/watch?v=' + video)
    cid = re.search(r'"channelId":"(UC[\w-]+)"', page)
    name = re.search(r'"ownerChannelName":"((?:[^"\\]|\\.)+)"', page) or re.search(r'"author":"((?:[^"\\]|\\.)+)"', page)
    if not cid:
        return None
    found = (cid.group(1), _unescape(name.group(1)) if name else cid.group(1))
    _channel_cache[video] = found
    return found


def feed(channel_id):
    """The latest uploads of a channel from its public feed: newest first."""
    xml = _get('https://www.youtube.com/feeds/videos.xml?channel_id=' + channel_id)
    by = re.search(r'<name>([^<]*)</name>', xml)
    by = html.unescape(by.group(1)) if by else ''
    out = []
    for entry in re.findall(r'<entry>(.*?)</entry>', xml, re.S):
        vid = re.search(r'<yt:videoId>([\w-]+)</yt:videoId>', entry)
        title = re.search(r'<title>([^<]*)</title>', entry)
        when = re.search(r'<published>([^<]*)</published>', entry)
        if vid and title:
            out.append(_video(vid.group(1), clean(html.unescape(title.group(1))), by, when.group(1) if when else ''))
    return out


def related_url(video):
    """The add-on's list of videos related to one video (works without a key)."""
    return '{0}special/related_videos/?video_id={1}'.format(PLUGIN, video)


def uploads_url(channel_id):
    """The add-on's listing of everything a channel uploaded (its UU playlist works without a key)."""
    return '{0}playlist/UU{1}/'.format(PLUGIN, channel_id[2:])


def uploads(rpc, channel_id, by=''):
    """A channel's newest uploads through the add-on's playlist listing (no key needed): the stand-in for YouTube's
    public feeds, which have stopped answering."""
    out = []
    for item in listing(rpc, uploads_url(channel_id)):
        if item['t'] == 'play':
            item.setdefault('by', by)
            item.setdefault('when', '')
            out.append(item)
    return out


def subscriptions(channels, limit=48, rpc=None):
    """New videos across the followed channels, newest first. Feeds are read in parallel; a channel whose feed fails
    is read through the add-on's uploads playlist when an rpc is given, otherwise skipped."""
    results = {}

    def fetch(channel):
        try:
            results[channel['id']] = feed(channel['id'])
        except Exception as e:
            xbmc.log('STREAM YT: feed failed for {0}: {1}'.format(channel['name'], e), xbmc.LOGINFO)
            if rpc is not None:
                try:
                    results[channel['id']] = uploads(rpc, channel['id'], channel['name'])
                except Exception as e2:
                    xbmc.log('STREAM YT: uploads failed for {0}: {1}'.format(channel['name'], e2), xbmc.LOGINFO)
    threads = [threading.Thread(target=fetch, args=(c,), daemon=True) for c in channels]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    videos = [v for c in channels for v in results.get(c['id'], [])]
    videos.sort(key=lambda v: v['when'], reverse=True)
    return videos[:limit]


def virtual(path, rpc=None):
    """Stream's own listings: stream/subs/ (new from followed channels), stream/search/?q=... and stream/channel/?id=..."""
    kind, _, query = path.partition('?')
    params = parse_qs(query)
    if kind.startswith('subs'):
        return subscriptions(db.channels(), rpc=rpc)
    if kind.startswith('search'):
        return search(params.get('q', [''])[0])
    if kind.startswith('channel'):
        channel_id = params.get('id', [''])[0]
        try:
            return sorted(feed(channel_id), key=lambda v: v['when'], reverse=True)
        except Exception as e:
            xbmc.log('STREAM YT: feed failed for {0}: {1}'.format(channel_id, e), xbmc.LOGINFO)
            return uploads(rpc, channel_id) if rpc is not None else []
    return []


def channel_url(channel_id):
    """A listing of a channel's newest uploads from its public feed."""
    return '{0}channel/?id={1}'.format(VIRTUAL, channel_id)


def subs_url():
    return '{0}subs/?v={1}'.format(VIRTUAL, db.channels_version())


def search_url(query):
    return '{0}search/?q={1}'.format(VIRTUAL, quote(query))


def search_entry(query):
    """A folder entry that opens a YouTube search for the query, for the end of Stream's own search results."""
    return {'item': {'t': 'folder', 'name': 'Search YouTube for \u201c{0}\u201d'.format(query), 'url': search_url(query), 'yt': 1,
                     'plot': 'Videos on YouTube matching \u201c{0}\u201d, played through Kodi\'s YouTube add-on.'.format(query)},
            'parent': VIRTUAL, 'group': 'YouTube', 'meta': 'YouTube'}


def when_text(stamp):
    """'Today', 'Yesterday', '3 days ago' or a date for a feed timestamp."""
    try:
        published = time.mktime(time.strptime(stamp[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone
    except Exception:
        return ''
    days = int((time.time() - published) // 86400)
    if days <= 0:
        return 'Today'
    if days == 1:
        return 'Yesterday'
    if days < 30:
        return '{0} days ago'.format(days)
    return time.strftime('%d %b %Y', time.localtime(published))


def open_settings():
    """The YouTube add-on's own settings, where the API key is entered and the sign-in lives."""
    if not ensure_installed('sign in and show your YouTube lists'):
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
