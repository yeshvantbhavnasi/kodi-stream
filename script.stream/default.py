import os
import re
import sys
import threading
import time
from urllib.parse import parse_qsl

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon('script.stream')
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
sys.path.insert(0, os.path.join(ADDON_PATH, 'resources', 'lib'))

import ai  # noqa: E402
import audit  # noqa: E402
import catalogue as cat  # noqa: E402
import db  # noqa: E402
import discover  # noqa: E402
import downloads as dl  # noqa: E402
import episodes  # noqa: E402
import library  # noqa: E402
import meta  # noqa: E402
import recs  # noqa: E402
import report  # noqa: E402
import youtube as yt  # noqa: E402

ROWS = 18
ROW_LIMIT = 40  # tiles per row before See all; a TV stick holds rows of forty posters comfortably
TABS_ID = 100
CLOSE_ID = 110
SEARCH_ID = 120
GEAR_ID = 130
PROFILE_ID = 140
TOP_BAR = (TABS_ID, CLOSE_ID, SEARCH_ID, GEAR_ID, PROFILE_ID)
FIRST_ROW = 200
BROWSE_FIRST = 400  # header 'Browse all ›' pill buttons on each row (400..417)
PANEL = 50
EPISODE_LIST = 51   # the same screen shows episodes as a text list instead of poster tiles
ACTION_LEFT, ACTION_RIGHT = 1, 2
ACTION_BACK = (9, 10, 92)  # parent dir, previous menu, nav back
ACTION_CONTEXT = 117
ACTION_INFO = 11  # the Info key on a remote plays the trailer of the highlighted title (also in the hold-OK menu)
ACTION_DELETE = 80  # the Delete / Clear key takes a part-watched title off Continue Watching
ACTION_DOWN = 4
FILTER_ID = 160  # the genre strip under the description panel on Movies and Shows
BACK_ID = 170  # the Back button on inner screens of the touch layout
# Touch screens: Kodi turns a tap into a mouse click, a long press into a right click (the context menu) and a
# one-finger swipe into these gestures; a two-finger swipe left is Back.
ACTION_MOUSE = range(100, 108)
ACTION_RIGHT_CLICK = 101
SWIPE_UP = range(531, 541)
SWIPE_DOWN = range(541, 551)
IDLE_AFTER = 60  # seconds without a key press before the home screen starts showing off recommended titles
NEXT_IN = 10  # seconds the next episode waits before it starts by itself
NEXT_AT = 60  # seconds before an episode ends (its end titles, mostly) that the Next episode button appears
START_TIMEOUT = 45  # seconds to wait for the Sasta TV addon to deliver a stream
CANCEL_GRACE = 1.5  # seconds after a play request during which a cancel is taken as a repeated OK press

TABS = [('home', 'Home'), ('movies', 'Movies'), ('shows', 'Shows'), ('live', 'Live TV'), ('sports', 'Sports'),
        ('youtube', 'YouTube'), ('mylist', 'My Stuff')]
KIDS_TABS = [('home', 'Home'), ('kids', 'Movies and Shows'), ('live', 'Kids TV'), ('mylist', 'My Stuff')]
CURRENT = {'profile': None}  # the viewer profile in use


def touch_mode():
    """Phones and tablets get the touch layout. Setting: 0 Auto (iPhone and iPad), 1 On, 2 Off."""
    choice = ADDON.getSetting('touch') or '0'
    if choice != '0':
        return choice == '1'
    return xbmc.getCondVisibility('System.Platform.IOS + !System.Platform.TVOS')


TOUCH = touch_mode()


def skin(name):
    return 'script-stream-{0}{1}.xml'.format(name, '-touch' if TOUCH and name in ('home', 'grid', 'title') else '')


def touch_words(hint):
    """The remote's wording of a hint, said for a finger: a title plays on a second tap (the first shows it off)."""
    if not TOUCH:
        return hint
    for word in ('Play', 'Resume', 'Open'):
        hint = hint.replace('OK  ' + word, 'Tap again  ' + word)
    return hint.replace('OK  Choose', 'Tap  Choose').replace('Hold OK', 'Press and hold')


# Sports the viewer can follow, and the words that identify each in the catalogue's section names.
SPORTS = {
    'Cricket': ['cricket'],
    'Football (soccer)': ['epl', 'uefa', 'soccer', 'mls', 'fifa', 'bein', 'dazn'],
    'Formula 1 and racing': ['formula', 'race'],
    'Tennis': ['tennis'],
    'Golf': ['golf'],
    'Basketball': ['nba', 'ncaa'],
    'American football': ['nfl', 'ncaa'],
    'Baseball': ['mlb'],
    'Ice hockey': ['nhl', 'hockey', 'ohl'],
}
EVENT_WORDS = ('tour', 'series', ' cup', 'trophy', ' vs ', 'final', 'league', '20')

# Release-quality words that appear in titles. Cinema recordings are called out so nobody is surprised by the picture.
QUALITY = re.compile(r'\s*[\[(]?\b(PreDVD|Pre-DVD|DVDScr|HDCAM|HDTS|CAM|TS|DVDRip|HDRip|WEBRip|WEB-DL|BluRay|4K|UHD)\b[\])]?', re.I)
CINEMA_COPY = ('predvd', 'pre-dvd', 'dvdscr', 'hdcam', 'hdts', 'cam', 'ts')


def quality_of(name):
    """(clean title, tag, note) - tag is the release quality found in the title, if any."""
    found = QUALITY.search(name)
    if not found:
        return name, '', ''
    tag = found.group(1)
    note = 'Cinema recording, lower picture and sound quality' if tag.lower() in CINEMA_COPY else tag + ' copy'
    return QUALITY.sub('', name).strip() or name, tag.upper() if len(tag) <= 4 else tag, note


# Tracks a stream that was requested but is not yet full screen, so Back can still cancel it.
PLAY = {'since': 0, 'cancel': False, 'stopper': False, 'ended': ''}
# True while a click is being handled. Kodi can deliver a second queued key press in the middle of a handler
# that is waiting; without this a double OK press starts the same stream twice.
BUSY = {'on': False}


def watch_visibility(window):
    """Close a screen of ours that Kodi has put another window on top of (for example after the remote's Home key).
    Otherwise it would stay open unseen, holding memory, and block Stream from being opened again."""
    monitor = xbmc.Monitor()
    away = 0
    while not window.closed and not monitor.waitForAbort(2):
        current = xbmcgui.getCurrentWindowId()
        # Addon windows are numbered from 13000; 12005 and 12006 are full-screen video and music.
        if current >= 13000 or current in (12005, 12006):
            away = 0
            continue
        away += 1
        if away >= 3:
            audit.event('left_behind', kodi_window=current)
            window.close()
            return


def exclusive(handler):
    """Run a window callback only if no other callback of ours is in progress and the window is still open."""
    def wrapped(self, *args):
        if BUSY['on'] or getattr(self, 'closed', False):
            return
        BUSY['on'] = True
        try:
            handler(self, *args)
        except Exception:
            audit.error(handler.__name__)
        finally:
            BUSY['on'] = False
    return wrapped

# The open home screen, so it can refresh its Continue Watching row when something finishes playing.
HOME = {'window': None}

# Set when the viewer chooses "Home" inside a nested screen; every open grid closes on seeing it.
NAV = {'home': False}
OPEN = {'grids': 0}  # how many inner screens are open above the home screen


def clock(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return '{0}:{1:02d}:{2:02d}'.format(h, m, s) if h else '{0}:{1:02d}'.format(m, s)


def shorten(text, limit=32):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def notify(text, ms=3000):
    xbmcgui.Dialog().notification('Stream', text, xbmcgui.NOTIFICATION_INFO, ms)


def list_item(item, parent, group=None, meta=None, saved=False, suggestion=False, label=None, series=None, progress=0):
    video = yt.is_youtube(item.get('url'))
    # Channel names such as '... 4K' and YouTube titles are not release tags.
    titled = item['t'] == 'play' and not cat.is_live(item) and not video
    clean, tag, note = quality_of(item['name']) if titled else (yt.clean(item['name']) if video else item['name'], '', '')
    li = xbmcgui.ListItem(label=label or clean)
    li.setProperty('tag', tag)
    if item.get('img') and not cat.GENERIC_IMG.search(item['img']):
        li.setArt({'thumb': item['img']})  # a placeholder image is left out so a real poster can be looked up
    if item.get('duration'):
        li.setProperty('length', clock(item['duration']))
    if progress:
        li.setProperty('progress', str(min(99, max(1, int(progress)))))
    li.setProperty('name', item['name'])
    li.setProperty('plot', item.get('plot', ''))
    if meta is None:
        meta = (item.get('by') or 'YouTube') if video and item['t'] == 'play' else cat.title_case(group) if group and item['t'] == 'play' else ''
        if video and item.get('when'):
            meta += ' · ' + yt.when_text(item['when'])
    if note:
        meta = (meta + ' · ' if meta else '') + note
    li.setProperty('meta', meta)
    if item['t'] == 'play' and cat.is_live(item):
        li.setProperty('logo', '1')
        li.setProperty('card', '1')  # channels are drawn as square cards rather than posters
    elif video and item['t'] == 'play':
        li.setProperty('wide', '1')  # YouTube thumbnails are 16:9, so videos get a wide tile instead of a poster
    li.setProperty('t', item['t'])
    category_folder = item['t'] == 'folder' and (label == 'See all ›' or cat.is_category(item.get('url', '')))
    hint = ('OK  Resume' if progress else 'OK  Play') if item['t'] == 'play' else 'OK  Open'
    if video and item['t'] == 'play':
        hint += '     Hold OK  Subscribe to channel, My List'
    elif (titled or (item['t'] == 'folder' and not category_folder)) and not series and cat.section_of(parent or '')[0]:
        hint += '     Hold OK  My List, Like'   # a movie or show from the catalogue
        li.setProperty('trailer', 'find')  # has a trailer (hold OK or Info); TMDb's own pick replaces this once looked up
    elif not category_folder and item['t'] in ('play', 'folder'):
        hint += '     Hold OK  More options'
    li.setProperty('hint', touch_words(hint))
    li.setProperty('url', item.get('url', ''))
    li.setProperty('i', str(item.get('i', 1)))
    li.setProperty('parent', parent or '')
    li.setProperty('group', group or '')
    li.setProperty('series', series or '')
    if item.get('season'):
        li.setProperty('season', item['season'])  # saved episodes remember their season, for the S/E tag and Up Next
    li.setProperty('saved', '1' if saved else '')
    li.setProperty('suggestion', '1' if suggestion else '')
    return li


def see_all_tile(title, url, group, note='Browse all · pages, years, search'):
    """The 'See all ›' tile at the end of a browseable row."""
    li = list_item({'t': 'folder', 'name': title, 'url': url}, url, group, meta=note, label='See all ›')
    li.setProperty('trailer', '')
    return li


def entry_item(entry, **kwargs):
    # A saved label is only needed for episodes ("Show · Episode 5"); other titles use their cleaned name.
    kwargs.setdefault('label', entry.get('label') if entry.get('series') else None)
    kwargs.setdefault('series', entry.get('series'))
    kwargs.setdefault('progress', entry.get('progress', 0))
    li = list_item(entry['item'], entry['parent'], entry.get('group'), saved=True, **kwargs)
    if entry.get('progress'):
        # A part-watched title: the Delete key, or hold OK, takes it off Continue Watching.
        li.setProperty('removable', '1')
        li.setProperty('hint', li.getProperty('hint').replace('Hold OK  ', 'Hold OK  Remove, ').replace('Press and hold  ', 'Press and hold  Remove, '))
    return li


def episode_tag(item):
    """'S2 · E5' for an episode, from its season folder ("Season 2") or its name ("Episode 5. Show S02 1080",
    "S02E05", "Ep 5"). Whichever part cannot be found is left out; '' when neither is known."""
    name = item.get('name') or ''
    season = re.search(r'(\d+)', item.get('season') or '') or re.search(r'\bS(?:eason)?\s*0*(\d{1,2})(?=\b|E\d)', name, re.I)
    episode = (re.search(r'\bS\d{1,2}\s*E0*(\d{1,3})\b', name, re.I)
               or re.search(r'\b(?:Episode|Ep|E)\.?\s*0*(\d{1,3})\b', name, re.I)
               or re.match(r'\s*0*(\d{1,3})\b', name))
    parts = ['S' + str(int(season.group(1)))] if season else []
    if episode:
        parts.append('E' + str(int(episode.group(1))))
    return ' · '.join(parts)


def watched_item(entry):
    """A tile for Continue Watching and Recently Played. An episode is shown as its show (one tile per show, see
    db.recent(fold=True)), tagged with the season and episode it is on; OK carries on with that episode."""
    series = entry.get('series')
    if not series:
        return entry_item(entry)
    tag = episode_tag(entry['item'])
    note = 'Resume ' + tag if entry.get('progress') else tag
    li = entry_item(entry, label=quality_of(series)[0] or series, meta=note or cat.title_case(entry.get('group') or ''))
    li.setProperty('folded', '1')
    return li


def remove_from_continue_watching(li):
    """Clear a highlighted part-watched title's position. True when something was removed."""
    if not li or not li.getProperty('removable'):
        return False
    db.clear_progress(db.play_key(item_of(li)))
    notify('Removed from Continue Watching', 2500)
    return True


def tile(label, kind, **props):
    li = xbmcgui.ListItem(label=label)
    li.setProperty('t', kind)
    li.setProperty('card', '1')
    li.setProperty('hint', touch_words('OK  Choose'))
    for key, value in props.items():
        li.setProperty(key, value)
    return li


def item_of(li):
    item = {'t': li.getProperty('t'), 'name': li.getProperty('name') or li.getLabel(), 'url': li.getProperty('url'),
            'i': int(li.getProperty('i') or 1)}
    if li.getArt('thumb'):
        item['img'] = li.getArt('thumb')
    if li.getProperty('plot'):
        item['plot'] = li.getProperty('plot')
    return item


# ---------- playback ----------

def follow(key, name, group, kind, live):
    """While a title plays: save the position every 5 seconds, and stop the stream when the viewer backs out of the video."""
    player, monitor = xbmc.Player(), xbmc.Monitor()
    position = duration = 0
    seen_fullscreen, away, tick = False, 0, 0
    PLAY['ended'] = ''
    for _ in range(40):
        if player.isPlayingVideo() or PLAY['cancel'] or monitor.waitForAbort(0.5):
            break
    while player.isPlayingVideo():
        if PLAY['cancel']:
            PLAY['ended'] = 'user'
            player.stop()
            break
        if xbmc.getCondVisibility('Window.IsActive(fullscreenvideo)'):
            seen_fullscreen, away = True, 0
            PLAY['since'] = 0  # on screen now: Back is handled by the rule below
        else:
            # Not on screen: either Back was pressed on the video, or it never came to the front
            # (Back pressed in the first instant). Either way, end the stream so browsing continues in silence.
            away += 1
            if away >= (2 if seen_fullscreen else 6):
                PLAY['ended'] = 'user'
                player.stop()
                break
        if not live and tick % 5 == 0:
            try:
                position, duration = player.getTime(), player.getTotalTime()
                db.update_progress(key, position, duration)
            except Exception:
                break
        tick += 1
        if monitor.waitForAbort(1):
            return
    if not PLAY['ended']:
        # Leaving fullscreenvideo as playback stops is a normal user Stop; only flag 'broken' if playback died
        # unexpectedly while fullscreenvideo was still active (or never reached 30s of a long video).
        still_full = xbmc.getCondVisibility('Window.IsActive(fullscreenvideo)')
        if duration <= 0 or position >= duration * 0.85:
            PLAY['ended'] = 'natural'
        elif still_full or (duration > 180 and position < 30):
            PLAY['ended'] = 'broken'
        else:
            PLAY['ended'] = 'stopped'
    audit.event('play_ended', title=name, position=int(position), duration=int(duration), ended=PLAY['ended'])
    home = HOME['window']
    if home is not None and not home.closed and home.tab == 'home' and not live:
        home.load_tab('home')  # so Continue Watching and Recently Played show what was just played
    if not live and duration > 0:
        percent = int(100 * position / duration)
        db.log_event('finished' if percent >= 90 else 'stopped', name, group, kind, 'watched {0}%'.format(percent))


def stop_if_playing_behind():
    """Back on a menu while a stream is starting or playing behind it: stop the stream instead of navigating."""
    player = xbmc.Player()
    if xbmc.getCondVisibility('Window.IsActive(fullscreenvideo)'):
        return False
    starting = PLAY['since'] and time.time() - PLAY['since'] < 30
    if not (starting or player.isPlayingVideo()):
        return False
    PLAY['cancel'] = True
    PLAY['since'] = 0
    if player.isPlaying():
        player.stop()
    threading.Thread(target=stop_late_arrival, daemon=True).start()
    notify('Stopped')
    return True


def stop_late_arrival():
    """After a cancel, stop the stream if it still arrives. Only one of these runs at a time."""
    if PLAY['stopper']:
        return
    PLAY['stopper'] = True
    player, monitor = xbmc.Player(), xbmc.Monitor()
    try:
        quiet = 0
        for _ in range(40):
            if not PLAY['cancel']:
                return  # something else was started on purpose
            if player.isPlaying():
                player.stop()
                quiet = 0
            else:
                quiet += 1
                if quiet >= 12:
                    return  # nothing has shown up for 12 seconds
            if monitor.waitForAbort(1):
                return
    finally:
        PLAY['stopper'] = False
        PLAY['cancel'] = False


def start(li, restart=False):
    item = item_of(li)
    parent, group = li.getProperty('parent'), li.getProperty('group') or None
    series = li.getProperty('series') or None
    label = '{0} · {1}'.format(series, item['name']) if series else item['name']
    live = cat.is_live(item)
    # Saved entries (history, My List, suggestions) can hold an expired link; take the current one.
    if li.getProperty('saved') and live:
        try:
            item = next((i for i in cat.get_dir(parent, fresh=True) if i['name'] == item['name'] and i['t'] == 'play'), item)
        except Exception:
            pass
    if yt.is_youtube(item.get('url')) and not yt.ensure_installed('play this video'):
        return
    key = db.play_key(item)
    position, duration, done = db.progress(key)
    # A part-watched title carries on from where it stopped. Hold OK offers "Play from the beginning".
    resume = position if (not live and not restart and not done and position > 30 and duration > 0) else 0
    player, monitor = xbmc.Player(), xbmc.Monitor()
    if player.isPlaying():
        player.stop()
        monitor.waitForAbort(1)
    if series and li.getProperty('season'):
        item['season'] = li.getProperty('season')  # kept with the history entry, so its tile can say S2 · E5
        db.set_meta('last:' + series, {'season': li.getProperty('season'), 'episode': item['name']})
    if not cat.play(parent, item, group, series, label):
        xbmcgui.Dialog().ok('Stream', 'This title can only be opened from the Sasta TV addon itself.')
        return
    PLAY['since'], PLAY['cancel'] = time.time(), False
    kind = 'Live TV' if live else 'YouTube' if yt.is_youtube(item.get('url')) else cat.section_of(parent)[0]
    db.log_event('played', label, group, kind)
    audit.event('play_request', title=label, kind=kind, free_memory=audit.memory())

    # Wait for the stream with a dialog the Back button can cancel.
    text = '{0} {1}…\nPress Back to cancel.'.format(
        'Resuming' if resume else 'Starting', label + (' from ' + clock(resume) if resume else ''))
    progress = xbmcgui.DialogProgress()
    progress.create('Stream', text)
    started = cancelled = False
    grace = time.time() + CANCEL_GRACE
    for step in range(START_TIMEOUT * 2):
        if progress.iscanceled():
            if time.time() < grace:
                # The OK press that chose the title can repeat (a held key on a TV remote) and land on the
                # dialog's Cancel button. A cancel this early is not meant, so the dialog is simply put back.
                progress.close()
                progress = xbmcgui.DialogProgress()
                progress.create('Stream', text)
                audit.event('play_cancel_ignored', title=label)
            else:
                cancelled = True
                break
        if player.isPlayingVideo() and xbmc.getCondVisibility('Player.HasVideo'):
            started = True
            break
        progress.update(min(95, int(100 * step / (START_TIMEOUT * 2))))
        if monitor.waitForAbort(0.5):
            break
    progress.close()
    if cancelled or progress.iscanceled():
        PLAY['cancel'], PLAY['since'] = True, 0
        if player.isPlaying():
            player.stop()
        threading.Thread(target=stop_late_arrival, daemon=True).start()
        audit.event('play_cancelled', title=label)
        notify('Cancelled')
        return
    if not started:
        PLAY['cancel'], PLAY['since'] = True, 0
        threading.Thread(target=stop_late_arrival, daemon=True).start()
        audit.event('play_timeout', title=label, seconds=START_TIMEOUT)
        notify('This stream did not start. Try another title or channel.', 5000)
        return
    if resume > 30:
        try:
            player.seekTime(resume)
        except Exception:
            pass
    audit.event('play_started', title=label, kind=kind, startup_ms=int((time.time() - PLAY['since']) * 1000) if PLAY['since'] else 0)
    threading.Thread(target=follow, args=(key, label, group, kind, live), daemon=True).start()


def next_episode(li):
    """The episode after this one in its season, as a tile, or None when it is the last or not part of a show."""
    series, parent = li.getProperty('series'), li.getProperty('parent')
    if not series or not parent:
        return None
    try:
        raw = cat.get_dir(parent)
    except Exception:
        return None
    nxt_page = next((i['url'] for i in raw if i['t'] == 'next'), None)
    items = [i for i in cat.showable(raw) if i['t'] == 'play']
    if not nxt_page:
        items = sorted(items, key=lambda i: cat.natural_key(i['name']))
    keys = [db.play_key(i) for i in items]
    key = db.play_key(item_of(li))
    if key not in keys:
        return None
    idx = keys.index(key) + 1
    if idx < len(items):
        following, target_parent = items[idx], parent
    elif nxt_page and nxt_page != parent:
        try:
            more = [i for i in cat.showable(cat.get_dir(nxt_page)) if i['t'] == 'play']
        except Exception:
            return None
        if not more:
            return None
        following, target_parent = more[0], nxt_page
    else:
        return None
    out = list_item(following, target_parent, li.getProperty('group') or None, series=series)
    if li.getProperty('season'):
        out.setProperty('season', li.getProperty('season'))
    return out


def countdown(nxt):
    """Show what plays next and count down. Returns False when the viewer presses Back or Cancel to stay."""
    dialog, monitor = xbmcgui.DialogProgress(), xbmc.Monitor()
    name = nxt.getLabel()
    text = 'Up next: {0}\nStarts in {1} seconds. Press Back to stay here.'
    dialog.create('Stream', text.format(name, NEXT_IN))
    steps = NEXT_IN * 4
    try:
        for n in range(steps):
            if dialog.iscanceled():
                return False
            dialog.update(int(100 * n / steps), text.format(name, NEXT_IN - n // 4))
            if monitor.waitForAbort(0.25):
                return False
    finally:
        dialog.close()
    return True


class NextUp(xbmcgui.WindowXMLDialog):
    """The Next episode button over the video; choice is 'next', 'credits' (Watch credits or Back), or None."""

    def onClick(self, control_id):
        self.choice = 'next' if control_id == 301 else 'credits'
        self.close()

    def onAction(self, action):
        if action.getId() in ACTION_BACK:
            self.choice = 'credits'
            self.close()


def offer_next(nxt):
    """In an episode's last minute, show Next episode over the video with its countdown filling the button.
    True to skip to it now (OK pressed, or the countdown ran out); False to let this episode play out."""
    player, monitor = xbmc.Player(), xbmc.Monitor()
    dialog = NextUp(skin('next'), ADDON_PATH, 'Default', '1080i')
    dialog.choice = None
    dialog.setProperty('next', nxt.getLabel())
    dialog.setProperty('button', 'Next episode')
    dialog.show()
    steps = NEXT_IN * 4
    try:
        for n in range(steps + 1):
            if dialog.choice or not player.isPlayingVideo():
                break
            dialog.setProperty('button', 'Next episode in {0}'.format(NEXT_IN - n // 4) if n < steps else 'Next episode')
            try:
                fill = dialog.getControl(302)
                width = int(320 * n / steps)
                fill.setWidth(max(width, 64))
                fill.setVisible(width >= 64)
            except (RuntimeError, TypeError):
                pass  # the overlay is not drawn yet
            if monitor.waitForAbort(0.25):
                return False
    finally:
        dialog.close()
    return dialog.choice != 'credits' and player.isPlayingVideo()


def time_left(player):
    """Seconds left in the playing video, or None while unknown (and for clips too short to have end titles)."""
    try:
        total = player.getTotalTime()
        return total - player.getTime() if total > 300 else None
    except Exception:
        return None


def autoplay_after(li, window=None):
    """Follow an episode to its end, and keep going episode after episode until the viewer stops or the season
    ends. In the last minute a Next episode button skips the end titles; when the episode ends on its own
    instead (not by Back), a countdown plays the next one."""
    player, monitor = xbmc.Player(), xbmc.Monitor()
    while True:
        if monitor.waitForAbort(3):
            return
        nxt, looked, skip = None, False, False
        while player.isPlayingVideo() and not (window is not None and window.closed):
            left = time_left(player) if not looked else None
            if left is not None and 0 < left <= NEXT_AT:
                looked, nxt = True, next_episode(li)
                if nxt is not None:
                    audit.event('next_button', title=nxt.getLabel(), left=int(left))
                    skip = offer_next(nxt)
                    if skip:
                        break
            if monitor.waitForAbort(1):
                return
        if window is not None and window.closed:
            return
        if skip:
            try:
                duration = player.getTotalTime()
                db.update_progress(db.play_key(item_of(li)), duration, duration)  # skipping the end titles counts as watched
            except Exception:
                pass
            db.log_event('skipped credits', li.getProperty('name'), li.getProperty('group') or None, 'Autoplay')
        else:
            if monitor.waitForAbort(1.5) or (window is not None and window.closed):
                return  # a moment for follow() to record how the stream ended
            if PLAY['ended'] != 'natural':
                return
            nxt = nxt if looked else next_episode(li)
            if nxt is None:
                return
            audit.event('autoplay_offer', title=nxt.getLabel())
            if not countdown(nxt):
                db.log_event('stayed', nxt.getProperty('name'), li.getProperty('group') or None, 'Autoplay')
                return
        BUSY['on'] = True
        try:
            start(nxt)
        finally:
            BUSY['on'] = False
        if not player.isPlaying():
            return
        li = nxt


def is_title(li):
    """True for movies and shows from the catalogue (not channels, episodes, tiles or 'See all')."""
    return (li.getProperty('t') in ('play', 'folder') and not li.getProperty('logo') and not li.getProperty('series')
            and not li.getProperty('show') and li.getLabel() != 'See all ›' and bool(cat.section_of(li.getProperty('parent'))[0])
            and not (li.getProperty('t') == 'folder' and cat.is_category(li.getProperty('url'))))


def apply_details(li, data):
    """Put a looked-up rating, and a poster or description when the catalogue had none, onto a tile."""
    if not data:
        return
    rating = meta.rating_text(data)
    genres = ', '.join(data.get('genres', [])[:2])
    if not li.getProperty('tagged'):
        # Rating, length and genres lead the description line: "★ 7.4 · 2h 13m · Action, Thriller · Telugu".
        li.setProperty('tagged', '1')
        li.setProperty('rating', rating)
        li.setProperty('genres', genres)
        line = ' · '.join(x for x in (rating, meta.length_text(data), genres, li.getProperty('meta')) if x)
        li.setProperty('meta', line)
    if not li.getArt('thumb') and data.get('poster'):
        li.setArt({'thumb': data['poster']})
    if data.get('backdrop') and not li.getArt('fanart'):
        li.setArt({'fanart': data['backdrop']})  # the billboard behind the highlighted title
    plot = li.getProperty('plot')
    # The catalogue's descriptions often arrive with their punctuation stripped; TMDb's reads better.
    if data.get('overview') and (not plot or '.' not in plot):
        li.setProperty('plot', data['overview'])
    people = meta.people_text(data)
    if people:
        li.setProperty('cast', people)
    if data.get('trailer'):
        li.setProperty('trailer', data['trailer'])


def enrich(control, still_wanted, limit, lock, start=0):
    """Look up ratings for up to `limit` tiles of a row or grid starting at `start`, one at a time, while that screen
    is still showing. The lookup itself runs unlocked; reading and changing a tile happens under the screen's lock."""
    if not meta.enabled():
        return
    try:
        for n in range(start, start + limit):
            with lock:
                if not still_wanted() or n >= control.size():
                    return
                li = control.getListItem(n)
                wanted = is_title(li) and not li.getProperty('tagged')
                name, show = li.getProperty('name'), li.getProperty('t') == 'folder'
                lang = li.getProperty('group')
            if not wanted:
                continue
            data = meta.lookup(name, show, lang=lang)
            with lock:
                if not still_wanted() or n >= control.size():
                    return
                li = control.getListItem(n)
                if li.getProperty('name') == name:
                    apply_details(li, data)
    except Exception as e:
        cat.log('enrich stopped: {0}'.format(e))


def play_trailer(video_id, title):
    if not yt.ensure_installed('play trailers'):
        return
    PLAY['since'], PLAY['cancel'] = time.time(), False
    xbmc.Player().play('plugin://plugin.video.youtube/play/?video_id=' + video_id)
    threading.Thread(target=follow, args=(None, title + ' (trailer)', None, 'Trailer', True), daemon=True).start()


def watch_trailer(li):
    """Play the trailer of a movie or show: TMDb's pick when available, otherwise the best match from a YouTube search."""
    name = li.getProperty('show') or li.getProperty('name') or li.getLabel()  # a season plays its show's trailer
    show = li.getProperty('t') == 'folder'
    busy = xbmcgui.DialogProgressBG()
    busy.create('Stream', 'Finding the trailer for ' + name + '…')
    key, title = '', name
    known = li.getProperty('trailer')
    if known and known != 'find':
        key, title = known, meta.split_title(name)[0] or name  # TMDb's pick, already looked up for this tile
    try:
        if not key and meta.enabled():
            data = meta.lookup(name, show, lang=li.getProperty('group'))
            if data:
                key, title = data.get('trailer') or '', data.get('title') or name
                apply_details(li, data)
        if not key:
            key = db.meta('trailer:' + name) or ''
        if not key:
            short, year = meta.split_title(name)
            title = short or name
            key = yt.trailer(short, year, show) or ''
            if key:
                db.set_meta('trailer:' + name, key)
    except Exception as e:
        cat.log('trailer lookup failed: {0}'.format(e))
    finally:
        busy.close()
    if not key:
        notify('No trailer found for ' + name)
        return
    db.log_event('trailer', name, li.getProperty('group') or None, 'Trailer')
    play_trailer(key, title)


def details(li):
    """Rating, overview, reviews and trailer for a title."""
    name = li.getProperty('name') or li.getLabel()
    if not meta.enabled():
        xbmcgui.Dialog().ok('Stream', 'Ratings, reviews and trailers are switched off because no TMDb key is available.')
        return
    busy = xbmcgui.DialogProgressBG()
    busy.create('Stream', 'Looking up ' + name + '…')
    try:
        data = meta.lookup(name, li.getProperty('t') == 'folder', lang=li.getProperty('group'))
    finally:
        busy.close()
    if not data:
        notify('No details found for this title')
        return
    apply_details(li, data)
    facts = [meta.rating_text(data) + (' (IMDb)' if data.get('imdb_rating') else '')]
    facts.append(meta.length_text(data))
    facts.append(', '.join(data.get('genres', [])[:3]))
    heading = '{0}  ·  {1}'.format(data.get('title') or name, '  ·  '.join(f for f in facts if f))
    reviews = data.get('reviews') or []
    while True:
        options = [('overview', 'Read the overview')]
        if data.get('trailer'):
            options.append(('trailer', 'Play trailer'))
        if reviews:
            options.append(('reviews', 'Read reviews ({0})'.format(len(reviews))))
        choice = xbmcgui.Dialog().select(heading, [label for _, label in options])
        if choice < 0:
            return
        action = options[choice][0]
        if action == 'overview':
            text = data.get('overview') or 'No overview available.'
            if meta.people_text(data):
                text += '\n\n' + meta.people_text(data)
            xbmcgui.Dialog().textviewer(data.get('title') or name, text)
        elif action == 'reviews':
            text = '\n\n'.join('[B]{0}[/B]\n{1}'.format(r['author'] or 'Review', r['text']) for r in reviews)
            xbmcgui.Dialog().textviewer('Reviews · ' + (data.get('title') or name), text)
        else:
            play_trailer(data['trailer'], data.get('title') or name)
            return


def activate(li, trail=(), category=False):
    """Click handling shared by the home rows and the grid. Returns True when the viewer asked for Home."""
    kind = li.getProperty('t')
    if kind == 'folder':
        name = li.getProperty('name') or li.getLabel()
        url = li.getProperty('url')
        # Category/channel/year folders list titles or shows rather than being a show, so they never start a show trail.
        is_cat = category or li.getLabel() == 'See all ›' or cat.is_category(url)
        inside_show = False if is_cat else (bool(trail) or 'sdirurl=' in url)
        return open_grid(name, url=url, group=li.getProperty('group') or None,
                         trail=tuple(trail) + (name,) if inside_show else (),
                         plot=li.getProperty('plot') if inside_show else '')
    if kind == 'play':
        start(li)
    return False


def context_menu(li, in_grid):
    """Long-press / menu-key actions. Returns 'home', 'changed' or None."""
    kind = li.getProperty('t')
    if kind == 'download':
        return download_menu(li)
    if kind not in ('play', 'folder') or li.getLabel() == 'See all ›':
        return 'home' if in_grid and xbmcgui.Dialog().contextmenu(['Go to Home']) == 0 else None
    item, parent, group = item_of(li), li.getProperty('parent'), li.getProperty('group') or None
    if kind == 'folder' and cat.is_category(item.get('url', '')):
        options = [('open', 'Open')]
        if not in_grid and li.getProperty('row_url'):
            options.append(('browse_row', 'See all in ' + (li.getProperty('row_title') or 'this section')))
        if in_grid:
            options.append(('home', 'Go to Home'))
        choice = xbmcgui.Dialog().contextmenu([label for _, label in options])
        if choice < 0:
            return None
        picked = options[choice][0]
        if picked == 'browse_row':
            return 'home' if open_grid(li.getProperty('row_title') or group or 'Browse', url=li.getProperty('row_url'), group=group) else None
        return 'home' if picked == 'home' or activate(li) else None
    section = cat.section_of(parent)[0]
    channel = li.getProperty('channel')
    if channel:
        # A followed channel's tile on the YouTube tab.
        options = [('open', 'Open'), ('unsubscribe', 'Unsubscribe from ' + item['name'])]
        if in_grid:
            options.append(('home', 'Go to Home'))
        choice = xbmcgui.Dialog().contextmenu([label for _, label in options])
        if choice < 0:
            return None
        if options[choice][0] == 'unsubscribe':
            db.toggle_channel(channel, item['name'])
            notify('Unsubscribed from ' + item['name'], 2500)
            return 'changed'
        return 'home' if options[choice][0] == 'home' or activate(li) else None
    video = kind == 'play' and yt.is_youtube(item.get('url'))
    listed = db.in_mylist(item['name'])
    rating = db.rating(item['name'])
    options = [('open', 'Play' if kind == 'play' else 'Open'),
               ('list', 'Remove from My List' if listed else 'Add to My List'),
               ('like', 'Remove like' if rating > 0 else 'Like'),
               ('dislike', 'Remove dislike' if rating < 0 else 'Dislike')]
    if is_title(li):
        options.insert(1, ('trailer', 'Watch trailer'))
        options.insert(2, ('info', 'Rating, reviews and trailer'))
    if video:
        options.insert(1, ('subscribe', 'Subscribe to channel'))
    key = db.play_key(item) if kind == 'play' else None
    position, _, done = db.progress(key) if key else (0, 0, False)
    if key and not cat.is_live(item) and position > 30:
        options[0] = ('open', 'Resume')
        options.insert(1, ('restart', 'Play from the beginning'))
        if not done:
            options.append(('clear', 'Remove from Continue Watching'))
    if key and li.getProperty('saved') and db.in_history(key):
        options.append(('forget', 'Remove from History'))
    if li.getProperty('suggestion'):
        options.append(('dismiss', 'Not interested'))
    if can_download(li):
        saved = dl.find(key)
        options.insert(1, ('download', 'Downloaded ✓' if saved and saved.get('state') == 'done'
                           else 'Downloading…' if saved and saved.get('state') in dl.ACTIVE else 'Download'))
    if not in_grid and li.getProperty('row_url'):
        options.append(('browse_row', 'See all in ' + (li.getProperty('row_title') or 'this section')))
    if in_grid:
        options.append(('home', 'Go to Home'))
    choice = xbmcgui.Dialog().contextmenu([label for _, label in options])
    if choice < 0:
        return None
    action = options[choice][0]
    if action == 'browse_row':
        return 'home' if open_grid(li.getProperty('row_title') or group or 'Browse', url=li.getProperty('row_url'), group=group) else None
    if action == 'open':
        return 'home' if (open_title(li) if kind == 'folder' and is_title(li) else activate(li)) else None
    if action == 'restart':
        start(li, restart=True)
        return None
    if action == 'download':
        saved = dl.find(key)
        if saved and saved.get('state') in ('done',) + dl.ACTIVE:
            notify('It is on the Downloads tab')
            return None
        download(li)
        return 'changed'
    if action == 'info':
        details(li)
        return None
    if action == 'trailer':
        watch_trailer(li)
        return None
    if action == 'subscribe':
        busy = xbmcgui.DialogProgressBG()
        busy.create('Stream', 'Finding the channel…')
        try:
            found = yt.channel_of(yt.video_id(item['url']))
        except Exception as e:
            cat.log('channel lookup failed: {0}'.format(e))
            found = None
        finally:
            busy.close()
        if not found:
            notify('Could not find the channel of this video')
            return None
        channel_id, channel_name = found
        if db.is_subscribed(channel_id):
            if xbmcgui.Dialog().yesno('Stream', 'You already follow {0}. Unsubscribe?'.format(channel_name)):
                db.toggle_channel(channel_id, channel_name)
                notify('Unsubscribed from ' + channel_name, 2500)
                return 'changed'
            return None
        db.toggle_channel(channel_id, channel_name, item.get('img', ''))
        db.log_event('subscribed', channel_name, 'YouTube', 'channel')
        notify('Subscribed to {0}. New videos appear on the YouTube tab.'.format(channel_name), 4000)
        return 'changed'
    if action == 'list':
        added = db.toggle_mylist(item, parent, group)
        db.log_event('listed' if added else 'unlisted', item['name'], group, section)
        notify('Added to My List' if added else 'Removed from My List', 2500)
        return 'changed'
    if action in ('like', 'dislike'):
        wanted = 1 if action == 'like' else -1
        value = 0 if rating == wanted else wanted
        db.rate(item, parent, group, value)
        db.set_meta('recs_dirty', True)
        recs.mark_dirty()
        if value:
            db.log_event('liked' if value > 0 else 'disliked', item['name'], group, section)
        notify({1: 'Liked', -1: 'Disliked', 0: 'Rating removed'}[value], 2500)
        return 'changed'
    if action == 'dismiss':
        db.dismiss(item['name'])
        db.log_event('dismissed', item['name'], group, section)
        recs.mark_dirty()
        return 'changed'
    if action == 'clear':
        db.clear_progress(key)
        notify('Removed from Continue Watching', 2500)
        return 'changed'
    if action == 'forget':
        if li.getProperty('folded'):
            db.forget_show(li.getProperty('series'), group)  # the tile stands for the whole show
        else:
            db.forget(key)
        notify('Removed from History', 2500)
        return 'changed'
    return 'home'


# ---------- downloads (touch screens) ----------

def can_download(li):
    """Movies and episodes from the catalogue; not live channels or YouTube videos."""
    if not TOUCH or li.getProperty('t') != 'play':
        return False
    item = item_of(li)
    return not cat.is_live(item) and not yt.is_youtube(item.get('url')) and bool(cat.play_url(li.getProperty('parent'), item))


def capture(li):
    """The title's real video address. Sasta TV only gives it to Kodi's player, so the title is started with the
    sound off and stopped as soon as it plays. Returns (url, headers) or None."""
    item, parent = item_of(li), li.getProperty('parent')
    target = cat.play_url(parent, item)
    if not target:
        return None
    player, monitor = xbmc.Player(), xbmc.Monitor()
    if player.isPlaying():
        player.stop()
        monitor.waitForAbort(1)
    muted = xbmc.getCondVisibility('Player.Muted')
    if not muted:
        xbmc.executebuiltin('Mute')
    PLAY['cancel'] = True  # nothing should treat this short playback as watching
    progress = xbmcgui.DialogProgress()
    progress.create('Stream', 'Getting {0} ready to download…'.format(li.getLabel()))
    threading.Thread(target=lambda: cat.rpc('Files.GetDirectory', {'directory': target}), daemon=True).start()
    found = None
    try:
        for step in range(START_TIMEOUT * 2):
            if progress.iscanceled() or monitor.waitForAbort(0.5):
                break
            progress.update(min(95, int(100 * step / (START_TIMEOUT * 2))))
            if player.isPlaying():
                try:
                    found = player.getPlayingFile()
                except Exception:
                    found = None
                if found:
                    break
    finally:
        progress.close()
        if player.isPlaying():
            player.stop()
        threading.Thread(target=stop_late_arrival, daemon=True).start()
        monitor.waitForAbort(0.5)
        if not muted and xbmc.getCondVisibility('Player.Muted'):
            xbmc.executebuiltin('Mute')
    if not found or not found.lower().startswith('http'):
        audit.event('download_capture_failed', title=li.getLabel(), got=(found or '')[:40])
        return None
    url, _, extra = found.partition('|')
    headers = dict(parse_qsl(extra)) if extra else {}  # Kodi's 'url|Header=value&...' form
    return url, headers


def download(li):
    """Queue a title for the service to fetch."""
    item = item_of(li)
    key = db.play_key(item)
    existing = dl.find(key)
    if existing and existing.get('state') == 'done':
        notify('Already downloaded. It is on the Downloads tab.')
        return False
    got = capture(li)
    if not got:
        xbmcgui.Dialog().ok('Stream', 'This title could not be prepared for download. Try again, or try another title.')
        return False
    series = li.getProperty('series') or ''
    label = '{0} · {1}'.format(series, item['name']) if series else li.getLabel()
    dl.add(key, got[0], got[1], item, li.getProperty('parent'), li.getProperty('group'), series, label,
           plot=li.getProperty('plot'), meta=li.getProperty('meta'))
    audit.event('download_queued', title=label)
    db.log_event('downloaded', label, li.getProperty('group') or None, cat.section_of(li.getProperty('parent'))[0])
    notify('Downloading {0}. It continues while Stream or Kodi is open.'.format(label), 5000)
    return True


def download_tile(entry):
    """A tile on the Downloads tab: plays the saved file, or shows how far its download has got."""
    item = dict(entry['item'])
    thumb = dl.thumb_path(entry)
    if thumb:
        item['img'] = thumb  # saved beside the video, so the poster shows without a connection
    position, duration, done = db.progress(entry['key'])
    watched = int(100 * position / duration) if duration and position > 30 and not done else 0
    finished = entry.get('state') == 'done'
    li = list_item(item, entry['parent'], entry.get('group') or None, meta=dl.status(entry),
                   label=entry['label'], progress=watched if finished else dl.percent(entry))
    li.setProperty('t', 'download')
    li.setProperty('did', entry['id'])
    li.setProperty('trailer', '')
    li.setProperty('plot', entry.get('plot') or item.get('plot', ''))
    if finished:
        li.setProperty('hint', ('Tap  Resume' if watched else 'Tap  Play') + '     Press and hold  Delete')
    else:
        li.setProperty('hint', 'Press and hold  Cancel or continue')
    return li


def play_download(li, restart=False):
    entry = dl.load(li.getProperty('did'))
    if not entry or entry.get('state') != 'done' or not os.path.exists(dl.video_path(entry)):
        notify('Still downloading' if entry and entry.get('state') != 'done' else 'This download is missing')
        return
    key, label = entry['key'], entry['label']
    position, duration, done = db.progress(key)
    resume = position if (not restart and not done and position > 30) else 0
    item = xbmcgui.ListItem(label=label)
    item.setArt({'thumb': dl.thumb_path(entry)})
    db.add_history(entry['item'], entry['parent'], entry.get('group') or None, entry.get('series') or None, label)
    player, monitor = xbmc.Player(), xbmc.Monitor()
    PLAY['cancel'], PLAY['since'] = False, time.time()
    player.play(dl.video_path(entry), item)
    for _ in range(40):
        if player.isPlayingVideo() or monitor.waitForAbort(0.25):
            break
    if resume:
        try:
            player.seekTime(resume)
        except Exception:
            pass
    kind = cat.section_of(entry['parent'])[0]
    db.log_event('played', label, entry.get('group') or None, kind, 'downloaded copy')
    threading.Thread(target=follow, args=(key, label, entry.get('group') or None, kind, False), daemon=True).start()


def download_menu(li):
    """Long press on a Downloads tile. Returns 'changed' when the tab should be redrawn."""
    entry = dl.load(li.getProperty('did'))
    if not entry:
        return 'changed'
    if entry.get('state') == 'done':
        position, _, done = db.progress(entry['key'])
        options = [('play', 'Resume' if position > 30 and not done else 'Play')]
        if position > 30:
            options.append(('restart', 'Play from the beginning'))
        options.append(('delete', 'Delete download'))
    else:
        options = []
        if entry.get('state') in ('expired', 'failed'):
            options.append(('again', 'Continue download'))
        options.append(('delete', 'Cancel download'))
    choice = xbmcgui.Dialog().contextmenu([label for _, label in options])
    if choice < 0:
        return None
    action = options[choice][0]
    if action in ('play', 'restart'):
        play_download(li, restart=action == 'restart')
        return None
    if action == 'again':
        source = list_item(entry['item'], entry['parent'], entry.get('group') or None, series=entry.get('series') or None)
        got = capture(source)
        if not got:
            xbmcgui.Dialog().ok('Stream', 'The title could not be reached. Check the connection and try again.')
            return None
        dl.add(entry['key'], got[0], got[1], entry['item'], entry['parent'], entry.get('group'), entry.get('series'), entry['label'])
        notify('Continuing ' + entry['label'])
        return 'changed'
    if xbmcgui.Dialog().yesno('Stream', '{0} {1}?'.format('Delete' if entry.get('state') == 'done' else 'Cancel the download of', entry['label'])):
        dl.cancel(entry)
        notify('Removed ' + entry['label'], 2500)
        return 'changed'
    return None


def online(timeout=3):
    import socket
    try:
        socket.create_connection(('sastatv.com', 443), timeout).close()
        return True
    except OSError:
        return False


# ---------- first-run setup ----------

def onboarding(first_run):
    """Ask for languages, genres, sports and a few liked titles. Returns True when something was saved."""
    dialog = xbmcgui.Dialog()
    profile = db.meta('profile') or {}
    if first_run:
        dialog.ok('Welcome to Stream', 'Five quick questions set up your home screen and recommendations.\n'
                                       'You can change the answers later under Settings.\n\n'
                                       'Stream tells its developer when it is first installed, and emails a problem report with its '
                                       'activity log if it crashes. This can be switched off under Settings.')

    def ask(title, options, saved_key):
        preset = [n for n, o in enumerate(options) if o in profile.get(saved_key, [])]
        picked = dialog.multiselect(title, options, preselect=preset)
        return None if picked is None else [options[n] for n in picked]

    languages = ask('1 of 5 · Which languages do you watch?', db.LANGUAGES, 'languages')
    if languages is None:
        if first_run:
            db.set_meta('profile', {'languages': [], 'genres': [], 'sports': [], 'people': []})
        return first_run
    if len(languages) > 1:
        # The rows follow this order: the main language first, then the rest as listed.
        previous = profile.get('languages', [])
        start = languages.index(previous[0]) if previous and previous[0] in languages else 0
        first = dialog.select('Which language should come first?', languages, preselect=start)
        if first > 0:
            languages.insert(0, languages.pop(first))
        if len(languages) > 2:
            rest = languages[1:]
            second = dialog.select('And second?', rest)
            if second > 0:
                rest.insert(0, rest.pop(second))
            languages = languages[:1] + rest
    genres = ask('2 of 5 · What do you like to watch?', list(ai.GENRES), 'genres')
    sports = ask('3 of 5 · Which sports do you follow?', list(SPORTS), 'sports')
    db.set_meta('profile', {'languages': languages,
                            'genres': profile.get('genres', []) if genres is None else genres,
                            'sports': profile.get('sports', []) if sports is None else sports,
                            'people': profile.get('people', [])})
    db.set_meta('recs_dirty', True)
    recs.mark_dirty()

    # Load the chosen languages so there are real titles to pick from.
    busy = xbmcgui.DialogProgressBG()
    busy.create('Stream', 'Loading titles in your languages…')
    try:
        for root in (cat.URLS['movies'], cat.URLS['web_series']):
            folders = [f for f in cat.get_dir(root) if f['t'] == 'folder']
            for folder in db.only_chosen(folders, lambda f: f['name']):
                cat.index(folder['url'], cat.get_dir(folder['url']))
        if db.lang_match('english'):
            for url in (cat.URLS['english_movies'], cat.URLS['english_tv']):
                cat.index(url, cat.get_dir(url))
    except Exception as e:
        cat.log('setup load failed: {0}'.format(e))
    finally:
        busy.close()
    # Favourite actors and directors: the names seen most in the looked-up titles of their languages, plus any typed.
    people = ask_people(dialog, profile.get('people', []), languages)
    if people is not None:
        current = db.meta('profile') or {}
        current['people'] = people
        db.set_meta('profile', current)
    choices = db.only_chosen(db.titles_in([], 400), lambda e: e.get('group'))[:40]
    if choices:
        labels = ['{0}  [COLOR FF888888]{1}[/COLOR]'.format(e['item']['name'], cat.title_case(e.get('group') or '')) for e in choices]
        picked = dialog.multiselect('5 of 5 · Pick any you like (optional)', labels)
        for n in picked or []:
            entry = choices[n]
            db.rate(entry['item'], entry['parent'], entry.get('group'), 1)
            db.log_event('liked', entry['item']['name'], entry.get('group'), cat.section_of(entry['parent'])[0])
    return True


def ask_people(dialog, current, languages):
    """'4 of 5': a multiselect of the actors and directors seen most in the chosen languages (with the ones already
    chosen ticked), and a 'Type a name' entry for anyone else. None when the question was skipped."""
    TYPE = 'Type a name…'
    options = list(current)
    options += [n for n in db.top_people(languages, 30) if n not in options]
    options.append(TYPE)
    preset = [n for n, o in enumerate(options) if o in current]
    picked = dialog.multiselect('4 of 5 · Favourite actors or directors (optional)', options, preselect=preset)
    if picked is None:
        return None
    people = [options[n] for n in picked if options[n] != TYPE]
    if any(options[n] == TYPE for n in picked):
        typed = dialog.input('Actors or directors, separated by commas')
        people += [t.strip() for t in (typed or '').split(',') if t.strip() and t.strip() not in people]
    db.set_meta('recs_dirty', True)
    recs.mark_dirty()
    return people[:20]


def reset_everything():
    """Erase every profile and everything Stream has saved on this device, as if it had just been installed.
    The add-on settings (keys) are kept; the activity log is kept so the reset itself can be traced."""
    import shutil
    keep = ('settings.xml', 'stream.log', 'session.open')
    for name in sorted(os.listdir(db.PROFILE)):
        if name in keep:
            continue
        path = os.path.join(db.PROFILE, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as e:
            cat.log('reset could not remove {0}: {1}'.format(name, e))
    cat._cache.clear()
    cat.state.clear()
    cat.state['affinity'] = {}
    db.use_profile({'id': 'default', 'name': 'Me', 'kids': False, 'pin': ''})
    audit.event('reset')


def add_profile():
    dialog = xbmcgui.Dialog()
    name = dialog.input('Name for the new profile')
    if not name:
        return None
    kids = dialog.yesno('Stream', 'Is this a kids profile?\nKids profiles only show children\'s titles and channels.')
    pin = dialog.input('PIN needed to leave this kids profile (optional)', type=xbmcgui.INPUT_NUMERIC) if kids else ''
    profiles = db.list_profiles()
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'profile'
    pid, n = base, 2
    while any(p['id'] == pid for p in profiles) or pid == 'default':
        pid, n = '{0}-{1}'.format(base, n), n + 1
    profile = {'id': pid, 'name': name, 'kids': bool(kids), 'pin': pin or ''}
    db.save_profiles(profiles + [profile])
    return profile


def choose_profile(force=False):
    """Ask who is watching. With a single profile and no reason to ask, that profile is used straight away."""
    profiles = db.list_profiles()
    if len(profiles) == 1 and not force:
        return profiles[0]
    labels = [p['name'] + ('  [COLOR FF888888]Kids[/COLOR]' if p.get('kids') else '') for p in profiles]
    choice = xbmcgui.Dialog().select("Who's watching?", labels + ['+ Add a profile'])
    if choice < 0:
        return None
    if choice == len(profiles):
        return add_profile() or choose_profile(True)
    return profiles[choice]


def sport_match(name):
    """True when a sports section belongs to a sport the viewer follows (or when none were chosen)."""
    chosen = (db.meta('profile') or {}).get('sports', [])
    if not chosen:
        return True
    name = name.lower()
    return any(word in name for sport in chosen for word in SPORTS.get(sport, []))


# ---------- screens ----------

def first_tap(window, control, li):
    """Touch: True when this tap only highlights an entry rather than acting on it. Movies and shows open their own
    page on the first tap, so nothing is held back any more; kept for layouts that still want a two-tap flow."""
    if not TOUCH or not is_title(li) or True:
        return False
    key = (control.getId(), control.getSelectedPosition(), li.getLabel())
    if window.armed == key:
        window.armed = None
        return False
    window.armed = key
    return True

# ---------- a title's own page ----------

PLAY_B, TRAILER_B, LIST_B, LIKE_B, DISLIKE_B = 301, 302, 303, 304, 305
SEASONS_ID, EPISODES_ID, ALIKE_ID, LANGS_ID = 310, 320, 330, 340
SEASON_LIKE = re.compile(
    r'\b(season|series|part|vol|volume|chapter|specials|episodes|'
    r'january|february|march|april|may|june|july|august|september|october|november|december)\b|\d', re.I)


def open_title(li, trail=()):
    """Show a movie's or show's page. Returns True when the viewer chose Home, so callers can close too."""
    audit.event('open', screen=li.getProperty('name') or li.getLabel())
    win = TitlePage(skin('title'), ADDON_PATH, 'Default', '1080i')
    win.setup(li)
    was_busy, BUSY['on'] = BUSY['on'], False
    OPEN['grids'] += 1
    try:
        win.doModal()
    finally:
        BUSY['on'] = was_busy
        OPEN['grids'] -= 1
    win.finish()
    fallback = win.fallback
    del win
    if fallback:
        return activate(li, (), category=True)  # not a show after all (a channel or category): open it as a listing
    return NAV['home']


def episode_label(name, number, info):
    """'3. Kishkindha Bound' when the episode's name is known; the provider's 'Episode 3' otherwise."""
    return episodes.describe(number, info, quality_of(name)[0])


class TitlePage(xbmcgui.WindowXML):
    def setup(self, li):
        self.li = li
        self.name = li.getProperty('name') or li.getLabel()
        self.show = li.getProperty('t') == 'folder'
        self.url, self.parent, self.group = li.getProperty('url'), li.getProperty('parent'), li.getProperty('group') or None
        self.data = {}
        self.seasons = []            # [(label, url)] for a show
        self.season = None           # the label of the season whose episodes are on screen
        self.episode_items = []      # the ListItems of that season, in order
        self.next_episodes_url = None
        self.episode_urls = set()
        self.seen_episode_keys = set()
        self.known_episodes = {}
        self.loading_more = False
        self.versions = []           # other language copies of this film, Telugu first when one exists
        self.version_index = 0
        self.version_token = 0       # bumped when the viewer switches language, so a slower load is dropped
        self.fallback = False        # set when the folder turns out to list shows rather than seasons
        self.ready = self.closed = False
        self.loading = 0
        self.workers = []
        self.ui_lock = threading.Lock()

    def spawn(self, target, *args):
        self.workers = [t for t in self.workers if t.is_alive()]
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.workers.append(thread)
        thread.start()

    def finish(self):
        self.closed = True
        for thread in self.workers:
            thread.join(3)

    def close(self):
        if not self.closed:
            self.closed = True
            xbmcgui.WindowXML.close(self)

    def onInit(self):
        if self.ready:
            return
        self.ready = True
        self.setProperty('kids', '1' if db.KIDS else '')
        self.setProperty('show', '1' if self.show else '')
        self.setProperty('section', 'Episodes' if self.show else 'More like this')
        self.describe()
        self.setProperty('status', 'Loading…' if self.show else '')
        self.spawn(self.load)
        self.spawn(watch_visibility, self)

    def describe(self):
        """Title, facts, synopsis, cast and artwork from what the tile already knows, then from TMDb."""
        li = self.li
        clean, tag, note = quality_of(self.name) if not self.show else (self.name, '', '')
        self.setProperty('title', self.data.get('title') or clean)
        facts = [meta.rating_text(self.data), self.data.get('year', ''), meta.length_text(self.data),
                 ', '.join(self.data.get('genres', [])[:3]), cat.title_case(self.group or ''), meta.provider_text(self.data), note]
        if not self.data:
            facts = [li.getProperty('meta')]
        self.setProperty('meta', ' · '.join(f for f in facts if f))
        plot = li.getProperty('plot')
        if self.data.get('overview') and (not plot or '.' not in plot):
            plot = self.data['overview']
        self.setProperty('plot', plot or '')
        self.setProperty('cast', meta.people_text(self.data) or li.getProperty('cast'))
        self.setProperty('fanart', self.data.get('backdrop') or li.getArt('fanart') or '')
        self.setProperty('poster', li.getArt('thumb') or self.data.get('poster') or '')
        self.labels()

    def labels(self):
        """The buttons' wording: Resume or Play (and which episode), My List and Like states."""
        rating = db.rating(self.name)
        self.setProperty('list_label', '✓  My List' if db.in_mylist(self.name) else '+  My List')
        self.setProperty('like_label', '✓  Liked' if rating > 0 else 'Like')
        self.setProperty('dislike_label', '✓  Disliked' if rating < 0 else 'Dislike')
        if self.show:
            self.setProperty('play_label', 'Play')
        else:
            position, duration, done = db.progress(db.play_key(item_of(self.li)))
            self.setProperty('play_label', 'Resume' if position > 30 and duration > 0 and not done else 'Play')

    # ---- loading ----

    def load(self):
        try:
            self.show_languages()
            token = self.version_token
            self.data = meta.lookup(self.name, self.show, lang=self.group or '') or {}
            if self.closed or token != self.version_token:
                return
            apply_details(self.li, self.data)
            self.describe()
            if self.show:
                self.load_seasons()
            else:
                self.load_alike()
        except Exception:
            audit.error('title.load')
            self.setProperty('status', '')

    def show_languages(self):
        """Chips for each language this film is listed in. Telugu is selected when that copy exists; otherwise
        the copy the viewer opened stays selected."""
        try:
            current = {'item': item_of(self.li), 'parent': self.parent, 'group': self.group or ''}
            versions = discover.language_versions(self.name, self.show, current)
        except Exception:
            audit.error('title.languages')
            versions = []
        self.versions = versions
        if len(versions) < 2:
            return
        labels = [cat.title_case(entry.get('group') or 'Other') for entry in versions]
        telugu = next((n for n, entry in enumerate(versions) if db.lang_match(entry.get('group') or '', ['telugu'])), None)
        opened = next((n for n, entry in enumerate(versions) if (entry['item'].get('url') or '') == (self.url or '')
                       or ((entry.get('group') or '').lower() == (self.group or '').lower() and entry['item']['name'] == self.name)), 0)
        # Telugu when that copy exists; otherwise the copy this page was opened on.
        selected = telugu if telugu is not None else opened
        self.setProperty('langs', '1')
        with self.ui_lock:
            if self.closed:
                return
            chips = self.getControl(LANGS_ID)
            chips.reset()
            chips.addItems([xbmcgui.ListItem(label=label) for label in labels])
            chips.selectItem(selected)
        self.setProperty('language', labels[selected])
        if selected != opened:
            self.apply_version(selected)
        else:
            self.version_index = selected
        self.describe()

    def apply_version(self, index):
        """Point the page at one language copy, without loading its details."""
        entry = self.versions[index]
        item = entry['item']
        self.version_index = index
        self.name = item['name']
        self.group = entry.get('group') or None
        self.parent = entry.get('parent') or ''
        self.url = item.get('url') or ''
        self.show = item.get('t') == 'folder'
        self.li = list_item(item, self.parent, self.group)
        self.setProperty('show', '1' if self.show else '')
        self.setProperty('section', 'Episodes' if self.show else 'More like this')
        self.setProperty('language', cat.title_case(self.group or 'Other'))

    def use_version(self, index):
        """Switch to another language copy and reload its details, seasons or similar titles."""
        if self.closed or not (0 <= index < len(self.versions)) or index == self.version_index:
            return
        self.version_token += 1
        token = self.version_token
        self.apply_version(index)
        self.data = {}
        self.describe()
        self.setProperty('status', 'Loading…' if self.show else '')
        try:
            data = meta.lookup(self.name, self.show, lang=self.group or '') or {}
        except Exception:
            audit.error('title.version')
            data = {}
        if self.closed or token != self.version_token:
            return
        self.data = data
        apply_details(self.li, self.data)
        self.describe()
        self.labels()
        if self.show:
            self.load_seasons()
        else:
            self.seasons = []
            self.episode_items = []
            self.load_alike()

    def load_alike(self):
        token = self.version_token
        alike = discover.similar_entries(self.name, False, self.group or '', 24)
        if self.closed or token != self.version_token:
            return
        items = [entry_item(e, meta=e.get('reason') or cat.title_case(e.get('group') or '')) for e in alike]
        with self.ui_lock:
            if self.closed or token != self.version_token:
                return
            self.getControl(ALIKE_ID).reset()
            if items:
                self.getControl(ALIKE_ID).addItems(items)
            else:
                self.setProperty('section', '')
        if items:
            enrich(self.getControl(ALIKE_ID), lambda: not self.closed, 12, self.ui_lock)

    def load_seasons(self):
        token = self.version_token
        try:
            raw = cat.get_dir(self.url)
        except Exception as e:
            self.setProperty('status', 'Could not load: {0}'.format(e))
            return
        if self.closed or token != self.version_token:
            return
        shown = cat.showable(raw)
        folders = [f for f in shown if f['t'] == 'folder']
        plays = [i for i in shown if i['t'] == 'play']
        # Follow pagination at the season level if a show has many year/season folders.
        seen_urls = {self.url}
        nxt = next((i['url'] for i in raw if i['t'] == 'next'), None)
        if folders and not plays:
            for _ in range(3):
                if not nxt or nxt in seen_urls or self.closed:
                    break
                seen_urls.add(nxt)
                try:
                    more = cat.get_dir(nxt)
                except Exception:
                    break
                folders += [f for f in cat.showable(more) if f['t'] == 'folder' and f['url'] not in {x['url'] for x in folders}]
                nxt = next((i['url'] for i in more if i['t'] == 'next'), None)
        if self.closed:
            return
        if folders and not plays and sum(1 for f in folders if SEASON_LIKE.search(f['name'])) < (len(folders) + 1) // 2:
            # Sub-folders with names like shows, not seasons: this is a channel or category, so show it as a listing.
            self.fallback = True
            self.close()
            return
        if not folders:
            # A show without season folders: its episodes sit directly under it.
            self.seasons = [('Episodes', self.url)]
        else:
            nums = [episodes.season_number(f['name']) for f in folders]
            year_folders = bool(nums) and all(1900 <= n <= 2100 for n in nums) and not any(
                re.search(r'\b(season|series|part|vol|chapter)\b', f['name'], re.I) for f in folders)
            # Ongoing multi-season shows whose early seasons were retired (e.g. Season 8, 9, 10) lead with the newest season.
            positive = [n for n in nums if 0 < n < 1900]
            ongoing_high = bool(positive) and min(positive) > 1 and len(folders) > 1
            folders.sort(key=lambda f: episodes.season_number(f['name']), reverse=(year_folders or ongoing_high))
            self.seasons = [(cat.title_case(f['name']), f['url']) for f in folders]
            if plays:
                self.seasons.insert(0, ('Episodes', self.url))
        last = (db.meta('last:' + self.name) or {}).get('season')
        chosen = next((n for n, (label, _) in enumerate(self.seasons) if label.lower() == (last or '').lower()), 0)
        with self.ui_lock:
            if self.closed or token != self.version_token:
                return
            chips = self.getControl(SEASONS_ID)
            chips.reset()
            chips.addItems([xbmcgui.ListItem(label=label) for label, _ in self.seasons])
            chips.selectItem(chosen)
        self.show_season(chosen, auto_fallback=True)

    def show_season(self, index, auto_fallback=False):
        """Put one season's episodes on screen, named from TMDb or Wikipedia when they know the show."""
        label, url = self.seasons[index]
        version = self.version_token
        self.season = label
        self.setProperty('season', label)
        self.loading += 1
        token = self.loading
        self.next_episodes_url = None
        self.episode_urls = set()
        self.seen_episode_keys = set()
        self.known_episodes = {}
        self.loading_more = False
        self.setProperty('status', 'Loading {0}…'.format(label))
        self.load_episodes_page(url, label, token, version=version, first_page=True)
        # If the initial season is empty on the provider (e.g. an archived season), automatically jump to a non-empty one.
        if (auto_fallback and not self.episode_items and len(self.seasons) > 1
                and not self.closed and token == self.loading and version == self.version_token):
            for alt in range(len(self.seasons)):
                if alt == index:
                    continue
                alt_label, alt_url = self.seasons[alt]
                self.season = alt_label
                self.setProperty('season', alt_label)
                self.next_episodes_url = None
                self.episode_urls = set()
                self.seen_episode_keys = set()
                self.known_episodes = {}
                self.setProperty('status', 'Loading {0}…'.format(alt_label))
                self.load_episodes_page(alt_url, alt_label, token, version=version, first_page=True)
                if self.closed or token != self.loading or version != self.version_token:
                    return
                if self.episode_items:
                    with self.ui_lock:
                        if not self.closed and token == self.loading and version == self.version_token:
                            self.getControl(SEASONS_ID).selectItem(alt)
                    return

    def load_episodes_page(self, url, label, token, version=None, first_page=False):
        """Load one page of episodes (or nested sub-folders) for the active season."""
        if version is None:
            version = self.version_token
        if not url or url in self.episode_urls:
            return
        self.episode_urls.add(url)
        if not first_page:
            self.setProperty('status', 'Loading more episodes…')
        try:
            raw = cat.get_dir(url)
        except Exception as e:
            if first_page:
                self.setProperty('status', 'Could not load: {0}'.format(e))
            else:
                self.episode_urls.discard(url)
                self.next_episodes_url = url  # allow retrying on the next scroll
                self.setProperty('status', '')
            return
        if self.closed or token != self.loading or version != self.version_token:
            return
        nxt = next((i['url'] for i in raw if i['t'] == 'next'), None)
        self.next_episodes_url = nxt if nxt and nxt not in self.episode_urls else None
        shown = cat.showable(raw)
        found = [i for i in shown if i['t'] == 'play']
        if first_page and not found:
            # Nested folders inside a season (for example Show -> 2026 -> October): open them as a grid under this show.
            subfolders = [i for i in shown if i['t'] == 'folder']
            if subfolders:
                items = []
                for n, sub in enumerate(subfolders):
                    li = list_item(sub, url, self.group or self.name, meta='Open folder', label=cat.title_case(sub['name']))
                    li.setProperty('t', 'subfolder')
                    li.setProperty('number', str(n + 1))
                    li.setProperty('season', label)
                    li.setProperty('plot', sub.get('plot') or self.getProperty('plot'))
                    if not li.getArt('thumb') and self.getProperty('fanart'):
                        li.setArt({'thumb': self.getProperty('fanart')})
                    items.append(li)
                with self.ui_lock:
                    if self.closed or token != self.loading or version != self.version_token:
                        return
                    row = self.getControl(EPISODES_ID)
                    row.reset()
                    row.addItems(items)
                    row.selectItem(0)
                    self.episode_items = items
                self.setProperty('status', '')
                self.setProperty('play_label', 'Open')
                return
        # Deduplicate across pages; flip to reverse (newest first) when a season has more than 10 episodes.
        deduped = []
        for i in found:
            key = db.play_key(i)
            if key not in self.seen_episode_keys:
                self.seen_episode_keys.add(key)
                deduped.append(i)
        found = deduped
        reversed_order = False
        if first_page and not self.next_episodes_url:
            reversed_order = len(found) > 10
            found.sort(key=lambda i: cat.natural_key(i['name']), reverse=reversed_order)
        if first_page and found:
            try:
                self.known_episodes = episodes.names(self.name, label, self.group or '', self.data)
            except Exception as e:
                cat.log('episode names failed: {0}'.format(e))
                self.known_episodes = {}
        if self.closed or token != self.loading or version != self.version_token:
            return
        known = self.known_episodes
        marks = db.progress_for([db.play_key(i) for i in found])
        last = db.last_played([db.play_key(i) for i in found]) if first_page else None
        base_index = 0 if first_page else len(self.episode_items)
        items, upnext = [], None
        for n, i in enumerate(found):
            number = episodes.episode_number(i['name']) or (base_index + n + 1)
            info = known.get(number) or {}
            position, duration, done = marks.get(db.play_key(i), (0, 0, False))
            percent = int(100 * position / duration) if (duration and position > 30 and not done) else 0
            li = list_item(i, url, self.group or self.name, meta='', label=episode_label(i['name'], number, info),
                           series=self.name, progress=max(1, percent) if percent else 0)
            li.setProperty('number', str(number))
            li.setProperty('season', label)
            li.setProperty('done', '1' if done else '')
            li.setProperty('plot', info.get('plot') or (i.get('plot') if '.' in (i.get('plot') or '') else '') or self.getProperty('plot'))
            if info.get('runtime'):
                li.setProperty('length', '{0}m'.format(int(info['runtime'])))
            note = 'Watched' if done else ('Resume from ' + clock(position) if position > 30 else '')
            li.setProperty('meta', ' · '.join(x for x in (li.getProperty('length'), note) if x))
            if info.get('still'):
                li.setArt({'thumb': info['still']})
            elif not li.getArt('thumb') and self.getProperty('fanart'):
                li.setArt({'thumb': self.getProperty('fanart')})
            if first_page and last and db.play_key(i) == last[0]:
                if not last[1]:
                    upnext = n
                else:
                    upnext = max(0, n - 1) if reversed_order else min(n + 1, len(found) - 1)
            items.append(li)
        if first_page and upnext is None:
            upnext = next((n for n, i in enumerate(found) if not marks.get(db.play_key(i), (0, 0, False))[2]), 0)
        with self.ui_lock:
            if self.closed or token != self.loading or version != self.version_token:
                return
            row = self.getControl(EPISODES_ID)
            if first_page:
                row.reset()
                row.addItems(items)
                if items:
                    row.selectItem(upnext)
                self.episode_items = items
            elif items:
                position = row.getSelectedPosition()
                row.addItems(items)
                if position >= 0:
                    row.selectItem(position)
                self.episode_items += items
        if not self.episode_items:
            self.setProperty('status', 'No episodes found')
        elif self.next_episodes_url:
            self.setProperty('status', '{0}+ episodes · scroll right for more'.format(len(self.episode_items)))
        else:
            self.setProperty('status', '')
        if first_page and items:
            self.setProperty('play_label', '{0} {1}'.format('Resume' if items[upnext].getProperty('progress') else 'Play',
                                                           'E{0}'.format(items[upnext].getProperty('number'))))

    def load_more_episodes(self, token):
        if self.loading_more or not self.next_episodes_url or self.closed or token != self.loading:
            return
        self.loading_more = True
        url, self.next_episodes_url = self.next_episodes_url, None
        try:
            self.load_episodes_page(url, self.season, token, first_page=False)
        finally:
            self.loading_more = False

    def refresh(self):
        """After playback: progress marks, the Play button and the episode row are brought up to date."""
        if self.closed:
            return
        self.labels()
        if self.show and self.seasons:
            index = next((n for n, (label, _) in enumerate(self.seasons) if label == self.season), 0)
            self.show_season(index)

    # ---- actions ----

    def open_episode_item(self, li):
        if li.getProperty('t') == 'subfolder':
            sub_name = li.getProperty('name') or li.getLabel()
            trail = (self.name, self.season, sub_name) if self.season and self.season != 'Episodes' else (self.name, sub_name)
            if open_grid(sub_name, url=li.getProperty('url'), group=self.group, trail=trail, plot=self.getProperty('plot')):
                self.close()
            return
        self.play(li)

    def play(self, li):
        start(li)
        if xbmc.Player().isPlaying():
            self.spawn(self.after_playback, li)

    def after_playback(self, li):
        if li.getProperty('series'):
            autoplay_after(li, self)  # returns once the viewer stops watching, or the season runs out
        else:
            follow_until_done(self)
        self.refresh()

    def play_pressed(self):
        if self.show:
            if not self.episode_items:
                return
            row = self.getControl(EPISODES_ID)
            self.open_episode_item(self.episode_items[max(0, row.getSelectedPosition())])
        else:
            self.play(self.li)

    @exclusive
    def onClick(self, control_id):
        if control_id == BACK_ID:
            if not stop_if_playing_behind():
                self.close()
        elif control_id == PLAY_B:
            self.play_pressed()
        elif control_id == TRAILER_B:
            watch_trailer(self.li)
        elif control_id == LIST_B:
            item, parent, group = item_of(self.li), self.parent, self.group
            added = db.toggle_mylist(item, parent, group)
            db.log_event('listed' if added else 'unlisted', item['name'], group, cat.section_of(parent)[0])
            notify(('Added to' if added else 'Removed from') + ' My List', 2000)
            self.labels()
        elif control_id in (LIKE_B, DISLIKE_B):
            item, parent, group = item_of(self.li), self.parent, self.group
            value = 1 if control_id == LIKE_B else -1
            if db.rating(self.name) == value:
                value = 0
            db.rate(item, parent, group, value)
            db.log_event('liked' if value > 0 else 'disliked' if value < 0 else 'unrated', item['name'], group, cat.section_of(parent)[0])
            db.set_meta('recs_dirty', True)
            recs.mark_dirty()
            self.labels()
        elif control_id == LANGS_ID:
            index = self.getControl(LANGS_ID).getSelectedPosition()
            if 0 <= index < len(self.versions) and index != self.version_index:
                audit.event('lang_switch', title=self.name[:80], to_lang=self.versions[index].get('group') or '')
                self.spawn(self.use_version, index)
            else:
                self.setFocusId(SEASONS_ID if self.show else ALIKE_ID)
        elif control_id == SEASONS_ID:
            index = self.getControl(SEASONS_ID).getSelectedPosition()
            if 0 <= index < len(self.seasons) and self.seasons[index][0] != self.season:
                self.spawn(self.show_season, index)
            else:
                self.setFocusId(EPISODES_ID)
        elif control_id == EPISODES_ID:
            row = self.getControl(EPISODES_ID)
            index = row.getSelectedPosition()
            if 0 <= index < len(self.episode_items):
                self.open_episode_item(self.episode_items[index])
        elif control_id == ALIKE_ID:
            li = self.getControl(ALIKE_ID).getSelectedItem()
            if li is not None and is_title(li):
                if open_title(li):
                    self.close()

    def onAction(self, action):
        if self.closed or BUSY['on']:
            return
        code = action.getId()
        if code in ACTION_BACK:
            if not stop_if_playing_behind():
                self.close()
            return
        focus = self.getFocusId()
        if focus == LANGS_ID and code in (ACTION_LEFT, ACTION_RIGHT):
            # Moving along the language chips switches to that copy without needing OK.
            index = self.getControl(LANGS_ID).getSelectedPosition()
            if 0 <= index < len(self.versions) and index != self.version_index:
                self.spawn(self.use_version, index)
        elif focus == SEASONS_ID and code in (ACTION_LEFT, ACTION_RIGHT):
            # Moving along the season chips shows that season's episodes without needing OK.
            index = self.getControl(SEASONS_ID).getSelectedPosition()
            if 0 <= index < len(self.seasons) and self.seasons[index][0] != self.season:
                self.spawn(self.show_season, index)
        elif code == ACTION_CONTEXT or (TOUCH and code == ACTION_RIGHT_CLICK):
            BUSY['on'] = True
            try:
                target = self.li
                if focus == ALIKE_ID:
                    target = self.getControl(ALIKE_ID).getSelectedItem() or self.li
                elif focus == EPISODES_ID and self.episode_items:
                    target = self.episode_items[max(0, self.getControl(EPISODES_ID).getSelectedPosition())]
                result = context_menu(target, True)
            finally:
                BUSY['on'] = False
            if result == 'home':
                NAV['home'] = True
                self.close()
            elif result == 'changed' or target is self.li:
                self.labels()
        elif code == ACTION_INFO:
            BUSY['on'] = True
            try:
                watch_trailer(self.li)
            finally:
                BUSY['on'] = False
        elif focus == EPISODES_ID and self.next_episodes_url and not self.loading_more:
            row = self.getControl(EPISODES_ID)
            if row.getSelectedPosition() >= len(self.episode_items) - 6:
                self.spawn(self.load_more_episodes, self.loading)


def follow_until_done(window):
    """Wait while a movie plays from a title page, so the page can refresh its Resume button afterwards."""
    player, monitor = xbmc.Player(), xbmc.Monitor()
    while player.isPlayingVideo() and not window.closed:
        if monitor.waitForAbort(1):
            return


def open_grid(title, url=None, entries=None, group=None, trail=(), plot=''):
    """Show a full-screen grid. Returns True when the viewer chose Home, so callers can close too."""
    audit.event('open', screen=title)
    win = Grid(skin('grid'), ADDON_PATH, 'Default', '1080i')
    win.setup(title, url, entries, group, trail, plot)
    # The click that opened this screen is still "in progress" underneath; let the new screen take key presses.
    was_busy, BUSY['on'] = BUSY['on'], False
    OPEN['grids'] += 1
    try:
        win.doModal()
    finally:
        BUSY['on'] = was_busy
        OPEN['grids'] -= 1
    win.finish()
    del win
    return NAV['home']


class Grid(xbmcgui.WindowXML):
    def setup(self, title, url, entries, group, trail, plot=''):
        self.title, self.url, self.entries, self.group, self.trail = title, url, entries, group, tuple(trail)
        self.plot = plot  # the show's own description, shown for its seasons and for episodes without one
        self.next_url = None
        self.seen_urls = set()
        self.seen_keys = set()
        self.loading = False
        self.ready = False
        self.closed = False
        self.workers = []
        self.ui_lock = threading.Lock()
        self.armed = None  # touch: the title shown off by a first tap; a second tap on it plays

    def spawn(self, target, *args):
        self.workers = [t for t in self.workers if t.is_alive()]
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.workers.append(thread)
        thread.start()

    def pager(self):
        """Touch: a finger scrolls the tiles without moving the highlight, so the next page is fetched when the
        last screenful comes into view rather than when the highlight nears the end."""
        monitor = xbmc.Monitor()
        while not self.closed and not monitor.waitForAbort(1):
            if not self.next_url or self.loading or self.view not in (PANEL, EPISODE_LIST):
                continue
            try:
                pages = int(xbmc.getInfoLabel('Container({0}).NumPages'.format(self.view)) or 0)
                page = int(xbmc.getInfoLabel('Container({0}).CurrentPage'.format(self.view)) or 0)
            except ValueError:
                continue
            if pages and page >= pages - 1:
                url, self.next_url = self.next_url, None
                self.load(url)

    def finish(self):
        """Mark the window closed and give its background work a moment to stop before the window is dropped."""
        self.closed = True
        for thread in self.workers:
            thread.join(3)

    def home_tile(self):
        return list_item({'t': 'home', 'name': '⌂ Home'}, '', meta='Back to the home screen')

    def onInit(self):
        if self.ready:
            return
        self.ready = True
        self.setProperty('kids', '1' if db.KIDS else '')
        self.setProperty('title', ' · '.join(self.trail) if self.trail else self.title)
        self.panel = self.getControl(PANEL)
        self.view = PANEL
        self.episodes = False
        if self.entries is not None:
            items = [self.home_tile()]
            items += [entry_item(e, meta=e.get('meta'), suggestion=bool(e.get('suggestion'))) for e in self.entries]
            self.panel.addItems(items)
            self.setProperty('status', '{0} titles'.format(len(self.entries)) if self.entries else 'Nothing here')
            self.spawn(watch_visibility, self)
            self.setFocusId(self.view)
            if self.entries:
                self.panel.selectItem(1)
                self.spawn(enrich, self.panel, lambda: not self.closed, 24, self.ui_lock, 1)
            return
        self.setProperty('status', 'Loading…')
        self.spawn(self.load, self.url)
        self.spawn(watch_visibility, self)
        if TOUCH:
            self.spawn(self.pager)

    def title_items(self, url, titles):
        """Build tiles for a listing. Inside a show, episodes are put in order and marked with their progress."""
        series = self.trail[0] if self.trail else None
        episodes = bool(series) and bool(titles) and all(i['t'] == 'play' for i in titles)
        self.episodes = episodes
        reversed_order = False
        if episodes and not self.next_url:
            reversed_order = len(titles) > 10
            titles = sorted(titles, key=lambda i: cat.natural_key(i['name']), reverse=reversed_order)
        marks = db.progress_for([db.play_key(i) for i in titles]) if episodes else {}
        out, upnext = [], None
        # Catch up from where the viewer left off: the last episode played if it is unfinished, otherwise the one after it.
        last_played = db.last_played([db.play_key(i) for i in titles]) if episodes else None
        for n, i in enumerate(titles):
            if last_played and db.play_key(i) == last_played[0]:
                if not last_played[1]:
                    upnext = n
                else:
                    upnext = max(0, n - 1) if reversed_order else min(n + 1, len(titles) - 1)
        for n, i in enumerate(titles):
            position, duration, done = marks.get(db.play_key(i), (0, 0, False))
            note = None
            if done:
                note = '✓ Watched'
            elif position > 30:
                note = 'Resume from ' + clock(position)
            if episodes and not done and upnext is None:
                upnext = n
            label = ('✓ ' + i['name']) if done else None
            if series and self.plot and (i['t'] == 'folder' or not i.get('plot')):
                i = dict(i, plot=self.plot)  # seasons carry the show's description, not the provider's filler text
            percent = int(100 * position / duration) if (duration and position > 30 and not done) else 0
            out.append(list_item(i, url, self.group or self.title, meta=note, label=label, series=series if episodes else None,
                                 progress=max(1, percent) if percent or (position > 30 and not done) else 0))
            if episodes and len(self.trail) > 1:
                out[-1].setProperty('season', self.trail[1])
        if not episodes and series and len(self.trail) == 1:
            # A show's page: its seasons show the show's own rating, cast and trailer, and land on the season watched last.
            season = (db.meta('last:' + series) or {}).get('season')
            data = meta.lookup(series, True, fetch=False, lang=self.group or '')
            for n, i in enumerate(titles):
                if season and i['name'] == season:
                    upnext = n
                if i['t'] == 'folder':
                    out[n].setProperty('show', series)
                    out[n].setProperty('trailer', 'find')
                    apply_details(out[n], data)
        return out, upnext

    def load(self, url):
        try:
            self.load_page(url)
        except Exception:
            audit.error('grid.load')
            self.loading = False

    def load_page(self, url):
        if not url or url in self.seen_urls:
            return
        self.loading = True
        self.seen_urls.add(url)
        first_page = self.panel.size() == 0
        started = time.time()
        if not first_page:
            self.setProperty('status', 'Loading more…')
        try:
            items = cat.get_dir(url)
        except Exception as e:
            audit.event('load_failed', screen=self.title, page=len(self.seen_urls), ms=int((time.time() - started) * 1000), reason=str(e)[:200])
            if self.closed:
                return
            self.seen_urls.discard(url)
            if first_page:
                self.setProperty('status', 'Could not load: {0}'.format(e))
                if self.panel.size() == 0:
                    self.panel.addItems([self.home_tile()])
                    self.setFocusId(self.view)
            else:
                self.next_url = url  # allow retrying on the next move
                self.setProperty('status', '')
            self.loading = False
            return
        if self.closed:
            return
        nxt = [i for i in items if i['t'] == 'next']
        self.next_url = nxt[0]['url'] if nxt and nxt[0]['url'] not in self.seen_urls else None
        shown = []
        if first_page:
            shown.append(self.home_tile())
            for extra in [i for i in items if i['t'] in ('search', 'filter')]:
                label = 'Search in ' + self.title if extra['t'] == 'search' else cat.title_case(extra['name'])
                shown.append(list_item(dict(extra, name=label, plot=''), url, self.group))
        raw_titles = []
        for i in cat.showable(items):
            key = (i['t'], i.get('url') or i['name'])
            if key not in self.seen_keys:
                self.seen_keys.add(key)
                raw_titles.append(i)
        titles, upnext = self.title_items(url, raw_titles)
        if first_page and self.episodes:
            # Episodes read better as a list of names than as a wall of identical posters.
            self.setProperty('mode', 'list')
            self.panel = self.getControl(EPISODE_LIST)
            self.view = EPISODE_LIST
        first_title = len(shown)
        shown += titles
        with self.ui_lock:
            if self.closed:
                return
            before_size = self.panel.size()
            if first_page and titles and all(t.getProperty('wide') for t in titles):
                self.setProperty('wide', '1')  # a listing of nothing but videos is drawn with wide tiles
            position = self.panel.getSelectedPosition()
            self.panel.addItems(shown)
            if not first_page and position >= 0:
                self.panel.selectItem(position)
        has_entries = bool(titles) or len(shown) > 1
        self.setProperty('status', '' if has_entries or not first_page else 'Nothing here')
        took_ms = int((time.time() - started) * 1000)
        audit.event('grid_loaded', screen=self.title, page=len(self.seen_urls), items=len(titles),
                    ms=took_ms, source=cat.LAST_SOURCE.get(url, 'unknown'))
        if first_page and not has_entries:
            audit.event('empty_listing', screen=self.title, url=url[:160])
        if first_page:
            self.setFocusId(self.view)
            if titles:
                # Land on the next episode to watch, or on the first title.
                self.select_when_ready(first_title + (upnext or 0))
                if upnext:
                    self.setProperty('status', 'Up next: ' + titles[upnext].getProperty('name'))
            elif len(shown) > 1:
                # A filter-only page (such as Filter by Year): land on the first choice rather than Home.
                self.select_when_ready(1)
        self.loading = False
        if titles:
            enrich(self.panel, lambda: not self.closed and not self.loading, 24, self.ui_lock, start=before_size)

    def select_when_ready(self, index):
        """Highlight an entry. A list that has only just been shown ignores the request for a frame or two, so retry."""
        for _ in range(12):
            if self.closed:
                return
            try:
                self.panel.selectItem(index)
                if self.panel.getSelectedPosition() == index and self.getFocusId() == self.view:
                    return
                self.setFocusId(self.view)
            except Exception:
                pass
            time.sleep(0.1)

    def refresh_marks(self):
        """After playback, redraw an episode list so watched ticks and the next episode are current."""
        if self.closed or not self.trail or self.entries is not None or self.next_url:
            return
        try:
            items = cat.get_dir(self.url)
        except Exception:
            return
        titles, upnext = self.title_items(self.url, cat.showable(items))
        if self.closed or not titles or not all(t.getProperty('t') == 'play' for t in titles):
            return
        with self.ui_lock:
            if self.closed:
                return
            self.panel.reset()
            self.panel.addItems([self.home_tile()] + titles)
        self.select_when_ready(1 + (upnext or 0))
        if upnext:
            self.setProperty('status', 'Up next: ' + titles[upnext].getProperty('name'))

    def close(self):
        if not self.closed:
            self.closed = True
            xbmcgui.WindowXML.close(self)

    def leave_if_home(self, go_home):
        if go_home:
            self.close()

    @exclusive
    def onClick(self, control_id):
        if control_id == BACK_ID:
            if not stop_if_playing_behind():
                self.close()
            return
        if control_id not in (PANEL, EPISODE_LIST) or self.loading:
            return
        li = self.panel.getSelectedItem()
        if li is None or first_tap(self, self.panel, li):
            return
        kind = li.getProperty('t')
        if kind == 'home':
            NAV['home'] = True
            self.close()
        elif kind == 'search':
            query = xbmcgui.Dialog().input('Search in ' + self.title)
            if query:
                db.log_event('searched', query, self.group)
                self.leave_if_home(open_grid('“{0}” in {1}'.format(query, self.title),
                                             url=li.getProperty('url') + cat.quote(query, safe=''), group=self.group))
        elif kind == 'filter':
            self.leave_if_home(open_grid(li.getLabel(), url=li.getProperty('url'), group=self.group))
        elif is_title(li):
            self.leave_if_home(open_title(li))
        elif kind == 'play':
            start(li)
            if xbmc.Player().isPlaying():
                self.spawn(self.after_playback, li)
        else:
            self.leave_if_home(activate(li, self.trail))

    def after_playback(self, li):
        autoplay_after(li, self)  # returns once the viewer stops watching, or the season runs out
        if self.closed:
            return
        self.loading = True
        try:
            self.refresh_marks()
        finally:
            self.loading = False

    def onAction(self, action):
        if self.closed or BUSY['on']:
            return
        if action.getId() in ACTION_BACK:
            if not stop_if_playing_behind():
                self.close()
            return
        if not self.ready or self.loading:
            return
        if action.getId() == ACTION_CONTEXT or (TOUCH and action.getId() == ACTION_RIGHT_CLICK):
            li = self.panel.getSelectedItem()
            BUSY['on'] = True
            try:
                result = context_menu(li, True) if li else None
            finally:
                BUSY['on'] = False
            if result == 'home':
                NAV['home'] = True
                self.close()
            return
        if action.getId() == ACTION_DELETE:
            li = self.panel.getSelectedItem()
            if remove_from_continue_watching(li):
                keep = [self.panel.getListItem(n) for n in range(self.panel.size()) if n != self.panel.getSelectedPosition()]
                self.panel.reset()
                self.panel.addItems(keep)
            return
        if action.getId() == ACTION_INFO:
            li = self.panel.getSelectedItem()
            if li and (is_title(li) or li.getProperty('show')):
                BUSY['on'] = True
                try:
                    watch_trailer(li)
                finally:
                    BUSY['on'] = False
            return
        # Fetch the next page shortly before the end of the list is reached.
        if self.next_url and self.panel.getSelectedPosition() >= self.panel.size() - 14:
            url, self.next_url = self.next_url, None
            self.spawn(self.load, url)


class Home(xbmcgui.WindowXML):
    def onInit(self):
        if getattr(self, 'ready', False):
            return
        self.ready = True
        audit.event('window_init')
        self.token = 0
        self.touched = False
        self.switch = False
        self.closed = False
        self.workers = []
        self.tab = 'home'
        self.ui_lock = threading.Lock()   # held for every change to the row controls
        self.pending = ('home', 0)
        self.settling = False
        self.filter = None        # genre chosen on the Movies or Shows strip
        self.year_filter = None   # release year chosen via By Year on Movies or Shows
        self.letter_filter = None # A-Z letter chosen via A-Z on Movies or Shows
        self.search_query = None  # active grouped search query on Movies, Shows, or global search view
        self.row_browse = {}      # slot -> ('dir', title, url, group) or ('entries', title, group, entries)
        self.more_entries = {}    # row title -> (group, full entries list)
        self.strip_for = None     # (tab, year_filter, letter_filter, search_query) the strip chips were built for
        self.armed = None  # touch: the title shown off by a first tap
        HOME['window'] = self
        self.setProperty('kids', '1' if db.KIDS else '')
        tabs = []
        names = list(KIDS_TABS if db.KIDS else TABS)
        if not db.KIDS and not yt.installed():
            names = [t for t in names if t[0] != 'youtube']
        if TOUCH:
            # A phone's top bar has less room: My Stuff moves to Settings, and Downloads takes its place.
            names = [t for t in names if t[0] not in ('mylist', 'history')] + [('downloads', 'Downloads')]
        for key, label in names:
            li = xbmcgui.ListItem(label=label)
            li.setProperty('key', key)
            tabs.append(li)
        self.getControl(TABS_ID).addItems(tabs)
        if TOUCH and any(e.get('state') == 'done' for e in dl.all_entries()) and not online():
            # No connection: open where something can still be watched.
            self.getControl(TABS_ID).selectItem([n for n, _ in names].index('downloads'))
            self.load_tab('downloads')
            notify('No connection. Showing your downloads.', 4000)
        else:
            self.load_tab('home')
        if TOUCH:
            self.spawn(self.download_progress)
        self.spawn(self.background)
        self.spawn(self.keeper)
        library.start(lambda: BUSY['on'] or self.closed or xbmc.Player().isPlaying())
        self.spawn(self.billboard)
        self.spawn(watch_visibility, self)

    def keeper(self):
        """While Stream is open: every hour, re-read the catalogue's first pages, refresh videos and metadata, and
        share the catalogue; every minute check whether a pending service recommendation batch is ready. Home's rows
        are redrawn straight away when nobody is using the remote; otherwise the next visit to Home shows them."""
        monitor = xbmc.Monitor()
        last_hourly = time.time()  # background() already runs the initial pass on startup
        while not self.closed and not monitor.waitForAbort(60):
            if BUSY['on'] or xbmc.Player().isPlaying():
                continue
            try:
                now = time.time()
                if now - last_hourly >= ai.REFRESH_AFTER:
                    last_hourly = now
                    cat.warm_index(ai.REFRESH_AFTER)
                    meta.warm(120)
                    gathered = recs.refresh_videos(ai.REFRESH_AFTER)
                    if recs.resolve_lists() or gathered:
                        recs.mark_dirty()
                    recs.share_catalogue()
                made = ai.recommend(cat.top_groups(), pool=recs.pool_for_model())
                if made:
                    recs.mark_dirty()
                    notify('Recommendations updated' + ai.engine_note().replace('  \u00b7  ', ' \u00b7 '), 4000)
                if not recs.stale():
                    continue
                recs.rows(force=True)
                idle = xbmc.getGlobalIdleTime() >= IDLE_AFTER and not OPEN['grids'] and xbmcgui.getCurrentWindowDialogId() == 9999
                if self.tab == 'home' and idle and not self.closed:
                    self.load_tab('home')
            except Exception as e:
                cat.log('hourly refresh failed: {0}'.format(e))

    def background(self):
        """Keep the local index and the suggestions fresh without holding up the screen."""
        try:
            cat.warm_index()
            meta.warm(40)
            gathered = recs.refresh_videos()
            gathered = recs.resolve_lists() or gathered  # both run; either makes the rows stale
            recs.share_catalogue()
            # Show the new rows straight away only if the viewer has not started navigating; otherwise they appear next time Home loads.
            if ai.recommend(cat.top_groups(), pool=recs.pool_for_model()) or gathered:
                recs.rows(force=True)
                if not self.touched and not self.closed and self.tab == 'home':
                    self.load_tab('home')
        except Exception as e:
            cat.log('background refresh failed: {0}'.format(e))
            audit.error('home.background')

    def spawn(self, target, *args):
        self.workers = [t for t in getattr(self, 'workers', []) if t.is_alive()]
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.workers.append(thread)
        thread.start()

    def finish(self):
        """Mark the window closed and give its background work a moment to stop before the window is dropped."""
        self.closed = True
        library.stop()
        for thread in getattr(self, 'workers', []):
            thread.join(4)

    def close(self):
        if not getattr(self, 'closed', False):
            self.closed = True
            xbmcgui.WindowXML.close(self)

    def go_home(self, focus=True):
        NAV['home'] = False
        self.getControl(TABS_ID).selectItem(0)
        self.load_tab('home', focus)

    def grouped_rows(self, heading, entries):
        """Entries grouped into one row per language (Telugu first, Hindi/dubbed secondary), with header Browse all & See all ›."""
        rows, by_lang = [], {}
        self.more_entries = {}
        for e in entries:
            by_lang.setdefault(cat.title_case(e.get('group') or 'Other'), []).append(e)
        order = cat.top_groups()
        for lang in sorted(by_lang, key=lambda l: db.language_rank(l, order)):
            found = by_lang[lang]
            title = '{0} · {1}'.format(heading, lang)
            meta_entries = [dict(e, meta=e.get('meta') or lang) for e in found]
            tiles = [entry_item(e, meta=e.get('meta') or lang) for e in found[:ROW_LIMIT]]
            if len(found) > 8:
                self.more_entries[title] = (lang, meta_entries)
            if len(found) > ROW_LIMIT:
                more = list_item({'t': 'more', 'name': title, 'url': ''}, '', lang,
                                 meta='See all {0} titles'.format(len(found)), label='See all ›')
                more.setProperty('trailer', '')
                more.setProperty('more_key', title)
                tiles.append(more)
            rows.append(('items', title, tiles))
        return rows

    def grouped_search_rows(self, query, kinds=None):
        """Search results grouped into rows by section/group (Telugu first, Hindi secondary), plus matching TV channels/groups."""
        started = time.time()
        self.more_entries = {}
        rows = []
        order = cat.top_groups()
        kind_order = {'Movies': 0, 'Web Series': 1, 'TV Shows': 2, 'English TV': 3, 'Kids': 4}
        q = (query or '').strip()
        if not q:
            return rows

        # 1. Matching browsable groups / TV channels (e.g. 'Star Maa', 'Zee Telugu', 'Colors', 'Web Series').
        groups = [] if db.KIDS else library.matching_groups(q, kinds=kinds)
        if groups:
            gtitle = 'Groups & Channels · “{0}”'.format(q)
            gtiles = [entry_item(g, meta=g.get('meta')) for g in groups[:ROW_LIMIT]]
            if len(groups) > 8:
                self.more_entries[gtitle] = ('', groups)
            rows.append(('items', gtitle, gtiles))

        # 2. Local library + stream.db hits (scoped to `kinds` when on Movies or Shows).
        saved = [] if db.KIDS else library.search(q, kinds=kinds, limit=200)
        local = [dict(hit, meta=cat.title_case(hit.get('group') or '')) for hit in db.find_names(q, 50)
                 if not kinds or not hit.get('kind') or hit.get('kind') in kinds]
        local = db.only_chosen(local, lambda e: e.get('group')) if local else []
        entries = list(saved)
        seen_names = {(e['item']['name'].lower(), (e.get('group') or '').lower()) for e in entries}
        for e in local:
            key = (e['item']['name'].lower(), (e.get('group') or '').lower())
            if key not in seen_names:
                seen_names.add(key)
                entries.append(e)
        if not entries and not groups:
            # Fall back to live Sasta TV search when local index has nothing for this query.
            live = cat.rank_results(q, cat.search(q))
            if kinds:
                allowed = set(kinds)
                live = [e for e in live if cat.section_of(e.get('parent') or '')[0] in allowed or not cat.section_of(e.get('parent') or '')[0]]
            for e in live:
                key = (e['item']['name'].lower(), (e.get('group') or '').lower())
                if key not in seen_names:
                    seen_names.add(key)
                    entries.append(e)

        # 3. On global descriptive queries (3+ words), put AI best matches up front.
        if kinds is None and len(q.split()) >= 3:
            smart, ai_seen = [], set()
            try:
                for pick in ai.smart_search(q):
                    name = pick['entry']['item']['name']
                    if name not in ai_seen:
                        ai_seen.add(name)
                        smart.append(dict(pick['entry'], meta='Best match · ' + (pick['reason'] or 'chosen by AI')))
            except Exception:
                smart = []
            if smart:
                stitle = 'Best Matches · “{0}”'.format(q)
                rows.append(('items', stitle, [entry_item(e, meta=e.get('meta')) for e in smart[:ROW_LIMIT]]))
                if len(smart) > 8:
                    self.more_entries[stitle] = ('', smart)

        # 4. Bucket title hits into clean (Language, Kind) rows, Telugu first and Hindi secondary.
        buckets = {}
        for e in entries:
            lang = cat.title_case(e.get('group') or 'Other')
            kind = e.get('kind') or cat.section_of(e.get('parent') or '')[0] or ('Shows' if kinds == discover.SHOW_KINDS else 'Movies' if kinds == discover.MOVIE_KINDS else 'Titles')
            buckets.setdefault((lang, kind), []).append(e)
        for (lang, kind) in sorted(buckets, key=lambda lk: (db.language_rank(lk[0], order), kind_order.get(lk[1], 9), lk[1])):
            found = buckets[(lang, kind)]
            if kind == 'English TV':
                label_group = 'English TV Shows'
            elif lang.lower() in kind.lower():
                label_group = kind
            else:
                label_group = '{0} {1}'.format(lang, kind)
            title = '“{0}” · {1}'.format(q, label_group)
            tiles = [entry_item(e, meta=e.get('meta') or label_group) for e in found[:ROW_LIMIT]]
            if len(found) > 8:
                self.more_entries[title] = (lang, [dict(e, meta=e.get('meta') or label_group) for e in found])
            if len(found) > ROW_LIMIT:
                more = list_item({'t': 'more', 'name': title, 'url': ''}, '', lang,
                                 meta='See all {0} titles'.format(len(found)), label='See all ›')
                more.setProperty('trailer', '')
                more.setProperty('more_key', title)
                tiles.append(more)
            rows.append(('items', title, tiles))

        # 5. Related titles on global search.
        if kinds is None:
            have = {e['item']['name'] for e in entries}
            related = []
            for word in [w for w in re.findall(r'\w+', q) if len(w) > 3][:4]:
                for hit in db.only_chosen(db.find_titles(word, 30), lambda e: e.get('group')):
                    if hit['item']['name'] not in have:
                        have.add(hit['item']['name'])
                        related.append(dict(hit, meta='Related · ' + cat.title_case(hit.get('group') or '')))
            if related:
                rtitle = 'Related · “{0}”'.format(q)
                rows.append(('items', rtitle, [entry_item(e, meta=e.get('meta')) for e in related[:ROW_LIMIT]]))
                if len(related) > 8:
                    self.more_entries[rtitle] = ('', related)

        # 6. Direct Sasta TV per-section search tiles (scoped to `kinds`) + YouTube on global search.
        more_search = [cat.search_tile(q, base) for base in cat.search_sources(chosen_only=True, kinds=kinds)]
        if kinds is None and yt.installed():
            more_search.append(yt.search_entry(q))
        if more_search:
            mtitle = 'More Search · Sasta TV' + (' & YouTube' if kinds is None and yt.installed() else '')
            rows.append(('items', mtitle, [entry_item(e, meta=e.get('meta')) for e in more_search[:ROW_LIMIT]]))

        audit.event('search', tab=self.tab, query=q[:80], local_hits=len(saved), group_hits=len(groups),
                    rows=len(rows), ms=int((time.time() - started) * 1000))
        return rows

    def filtered_rows(self, kinds):
        """The chosen genre as one row per language, Telugu first and Hindi secondary."""
        entries = discover.genre_entries(self.filter, kinds, 400)
        return self.grouped_rows(self.filter, entries)

    def show_filters(self, key):
        """Fill the browse/filter strip on Movies, Shows, and Search; hide it elsewhere. Called with the UI lock held."""
        strip = self.getControl(FILTER_ID)
        active = ('__find__' if self.search_query else '__year__' if self.year_filter
                  else '__az__' if self.letter_filter else (self.filter or ''))
        self.setProperty('filter', active)
        if key not in ('movies', 'shows', 'search') or db.KIDS:
            strip.reset()
            self.strip_for = None
            self.setProperty('filters', '')
            return
        stamp = (key, self.year_filter, self.letter_filter, self.search_query)
        if self.strip_for == stamp:
            return  # same tab reloading after a pick: keep the chips, and the highlight on them
        pos = strip.getSelectedPosition() if self.strip_for and self.strip_for[0] == key else 0
        strip.reset()
        if key == 'search':
            chips = [
                tile('← Back Home', 'filter', genre='__home__'),
                tile('New Search', 'filter', genre='__find__'),
                tile('Browse Movies', 'filter', genre='__browse_movies__'),
                tile('Browse Show Groups', 'filter', genre='__browse_shows__'),
            ]
        else:
            kinds = discover.MOVIE_KINDS if key == 'movies' else discover.SHOW_KINDS
            browse_label = 'Browse Sections' if key == 'movies' else 'Browse Groups'
            year_label = 'Year: ' + self.year_filter if self.year_filter else 'By Year'
            az_label = ('A–Z: ' + self.letter_filter if self.letter_filter != 'ALL' else 'A–Z: All') if self.letter_filter else 'A–Z'
            find_label = 'Search: ' + shorten(self.search_query, 10) if self.search_query else 'Find Title'
            chips = [
                tile('All', 'filter', genre=''),
                tile(browse_label, 'filter', genre='__browse__'),
                tile(year_label, 'filter', genre='__year__'),
                tile(az_label, 'filter', genre='__az__'),
                tile(find_label, 'filter', genre='__find__'),
            ]
            for genre, count in discover.genre_tiles(kinds):
                chips.append(tile(genre.replace(' & Mythology', ''), 'filter', genre=genre))
        strip.addItems(chips)
        if 0 <= pos < len(chips):
            strip.selectItem(pos)
        self.strip_for = stamp
        self.setProperty('filters', '1')

    def discovery_rows(self):
        """What is new, what the household plays most, and titles like the ones watched last."""
        rows = []
        try:
            fresh = discover.new_entries()
            if len(fresh) >= 4:
                rows.append(('items', 'New This Week', [entry_item(e, meta=cat.title_case(e.get('group') or '')) for e in fresh]))
            # A Top 10 per language, with movies and shows apart: 'Top 10 Telugu Movies', 'Top 10 Telugu Shows', ...
            for lang, kind, chart in discover.india_top_rows():
                rows.append(('items', 'Top 10 {0} {1}'.format(lang, kind), [entry_item(e, meta=e['meta']) for e in chart]))
            top = discover.top_entries()
            if len(top) >= 3:
                rows.append(('items', 'Top 10 in Your Home', [entry_item(e, meta=e['meta']) for e in top]))
            for name, show, lang in db.recent_watched(4):
                if sum(1 for r in rows if r[1].startswith('More like')) >= 2:
                    break
                alike = discover.similar_entries(name, show, lang)
                if len(alike) >= 4:
                    rows.append(('items', 'More like ' + shorten(meta.split_title(name)[0] or name),
                                 [entry_item(e, meta=cat.title_case(e.get('group') or '')) for e in alike]))
        except Exception as e:
            cat.log('discovery rows failed: {0}'.format(e))
        return rows

    def order_language_rows(self, rows):
        """Order ('dir', title, url, group, only) rows with Telugu always first and Hindi/dubbed secondary."""
        order = cat.top_groups()
        return [r for _, r in sorted(enumerate(rows), key=lambda p: (db.language_rank(p[1][3], order), p[0]))]

    def language_rows(self, root, title, limit):
        """One row per sub-folder of root, limited to the chosen languages when any of them exist there."""
        folders = [f for f in cat.get_dir(root) if f['t'] == 'folder']
        folders = cat.by_affinity(db.only_chosen(folders, lambda f: f['name']))
        return [('dir', title.format(cat.title_case(f['name'])), f['url'], f['name'], None) for f in folders[:limit]]

    def sports_rows(self, limit):
        """Live events first, then one row per sports section, limited to the sports the viewer follows."""
        root = cat.URLS['sports']
        items = cat.get_dir(root)
        rows = []
        events = [i for i in items if i['t'] == 'play']
        folders = [i for i in items if i['t'] == 'folder']
        known = [w for words in SPORTS.values() for w in words]
        event_folders = [f for f in folders if not any(w in f['name'].lower() for w in known)
                         and any(w in f['name'].lower() for w in EVENT_WORDS)]
        if events or event_folders:
            rows.append(('items', 'Live Events', [list_item(i, root, 'Sports') for i in events + event_folders]))
        sections = [f for f in folders if f not in event_folders]
        kept = [f for f in sections if sport_match(f['name'])] or sections
        rows += [('dir', cat.title_case(f['name']), f['url'], 'Sports', None) for f in kept[:limit]]
        return rows

    def settings_tiles(self):
        tiles = []
        if TOUCH:
            tiles = [tile('My List', 'setting', action='open_mylist', meta='{0} titles'.format(len(db.mylist())),
                          plot='Titles you saved with Add to My List. Press and hold any title to add it.'),
                     tile('History', 'setting', action='open_history', meta='{0} played'.format(db.history_count()),
                          plot='Everything played on this profile, newest first, with unfinished titles at the top.')]
        return tiles + self.option_tiles()

    def option_tiles(self):
        last = db.meta('recs', {})
        engine = ai.engine_name(last.get('source')) or ('your languages and likes' if last.get('source') == 'local' else 'not run yet')
        profile = db.meta('profile') or {}
        chosen = ', '.join(profile.get('languages', [])) or 'all languages'
        return [
            tile('Tastes', 'setting', action='profile', meta='Now: ' + chosen,
                 plot='Change the languages, genres and sports you chose at setup, and pick more titles you like.'),
            tile('Ratings', 'setting', action='ratings',
                 plot='See everything you have marked with Like or Dislike. Hold OK on any title to rate it.'),
            tile('Options', 'setting', action='keys',
                 plot='Choose whether Stream opens when Kodi starts and whether problem reports are sent, and enter keys for AI recommendations and ratings. Stream works without any keys.'),
            tile('YouTube', 'setting', action='youtube',
                 meta='Add-on ' + ('installed' if yt.installed() else 'not installed'),
                 plot='Sign in to YouTube and add your own Google API key (pasted from a phone or computer). '
                      'Related Videos work without a key; your Subscriptions, History, Recommendations and Trending need it.'),
            tile('Refresh', 'setting', action='refresh', meta='Last built with: ' + engine,
                 plot='Rebuild the Top Picks and the other recommendation rows now.'),
            tile('Library', 'setting', action='library', meta=self.library_note(),
                 plot='Local copy of titles in your languages for instant search and list matching.'),
            tile('Reset', 'setting', action='clear_recs', plot='Remove the saved recommendations.'),
            tile('Forget', 'setting', action='clear_history',
                 plot='Forget everything played, including resume points and watched episodes.'),
            tile('Log', 'setting', action='log',
                 plot='What Stream did recently, including errors and whether the last session ended unexpectedly. Useful when reporting a problem.'),
            tile('Report', 'setting', action='send_log',
                 plot='Email the latest activity log so a problem can be investigated. It includes what was opened and played. '
                      'A report is also sent automatically after a crash unless that is switched off under Options.'),
            tile('Profiles', 'setting', action='switch', meta='Now: ' + (CURRENT['profile'] or {}).get('name', ''),
                 plot='Stream opens with the profile used last. Each profile has its own history, My List, likes and recommendations. A kids profile shows only children\'s titles.'),
            tile('Erase', 'setting', action='reset', meta='Erases every profile',
                 plot='Delete all profiles with their watch history, resume points, My List, likes, recommendations and setup answers, '
                      'and the cached catalogue, then run the first-time setup again as on a new installation. API keys entered under Options are kept.'),
            tile('PIN', 'setting', action='pin',
                 meta='Locked' if any(p.get('kids') and p.get('pin') for p in db.list_profiles()) else 'No PIN set',
                 plot='Set or change the PIN that is asked for when someone tries to leave a kids profile, switch profile or close Stream from it. '
                      'Without a PIN a child holding the remote can wander into everything else.'),
        ]

    def download_rows(self):
        entries = dl.all_entries()
        if db.KIDS:
            entries = [e for e in entries if cat.section_of(e['parent'])[0] == 'Kids' or (e.get('group') or '').lower() == 'kids']
        going = [download_tile(e) for e in entries if e.get('state') != 'done']
        saved = [download_tile(e) for e in entries if e.get('state') == 'done']
        return ([('items', 'Downloading', going)] if going else []) + ([('items', 'Downloaded', saved)] if saved else [])

    def download_progress(self):
        """While the Downloads tab shows: keep the tiles' progress current, and redraw when a download finishes."""
        monitor = xbmc.Monitor()
        while not self.closed and not monitor.waitForAbort(2):
            if self.tab != 'downloads' or BUSY['on']:
                continue
            finished = False
            with self.ui_lock:
                if self.closed or self.tab != 'downloads':
                    continue
                for n in range(ROWS):
                    if self.getProperty('row{0}.title'.format(n)) != 'Downloading':
                        continue
                    control = self.getControl(FIRST_ROW + n)
                    for i in range(control.size()):
                        li = control.getListItem(i)
                        entry = dl.load(li.getProperty('did'))
                        if entry is None or entry.get('state') == 'done':
                            finished = True
                            continue
                        li.setProperty('meta', dl.status(entry))
                        li.setProperty('progress', str(min(99, max(1, dl.percent(entry)))) if dl.percent(entry) else '')
            if finished and not self.closed:
                self.load_tab('downloads', focus=False)

    def kids_live_rows(self):
        root = [f for f in cat.get_dir(cat.URLS['live']) if f['t'] == 'folder' and f['name'].lower() == 'kids']
        if not root:
            return []
        folders = [f for f in cat.get_dir(root[0]['url']) if f['t'] == 'folder']
        return [('dir', cat.title_case(f['name']) + ' Channels', f['url'], 'Kids', None) for f in folders[:ROWS - 2]]

    def kids_specs(self, key):
        """Rows for a kids profile: children's titles and channels only."""
        titles = ('dir', 'Movies and Shows', cat.URLS['kids'], 'Kids', None)
        if key == 'home':
            rows = []
            for title, entries in (('Continue Watching', db.continue_watching(fold=True)), ('Recently Played', db.recent(30, fold=True))):
                if entries:
                    rows.append(('items', title, [watched_item(e) for e in entries]))
            picks = db.suggestions()
            if picks:
                rows.insert(1 if rows else 0, ('items', 'Recommended for You' + ai.engine_note(),
                                               [entry_item(p, meta=p['reason'], suggestion=True) for p in picks]))
            # A toddler learning to talk: Ms Rachel and Ms Apple first, then their videos by what they teach.
            learning = [('dir', title, yt.uploads_url(channel_id), 'YouTube', None) for title, channel_id in yt.PINNED]
            learning += [('dir', title, yt.search_url(query), 'YouTube', None) for title, query in yt.TODDLER_ROWS]
            return rows + learning + [titles] + self.kids_live_rows()
        if key == 'kids':
            return [titles]
        if key == 'live':
            return self.kids_live_rows()
        if key == 'mylist':
            saved = db.mylist()
            rows = [('items', 'My List', [entry_item(e) for e in saved])] if saved else []
            return rows + self.history_rows()
        if key == 'settings':
            return [('items', 'Settings', [tile('Switch profile', 'setting', action='switch',
                                                 meta='Now: ' + (CURRENT['profile'] or {}).get('name', ''),
                                                 plot='Choose who is watching.')])]
        return []

    def history_rows(self):
        """Everything played on this profile: unfinished titles first, then the full history, newest first."""
        rows = []
        unfinished = db.continue_watching(40)
        if unfinished:
            rows.append(('items', 'Continue Watching', [entry_item(e, meta=e.get('note')) for e in unfinished]))
        played = db.recent(120)
        for title, entries in (('Watched', [e for e in played if not cat.is_live(e['item'])]),
                               ('Channels Watched', [e for e in played if cat.is_live(e['item'])])):
            if entries:
                rows.append(('items', title, [entry_item(e, meta=e.get('note')) for e in entries[:60]]))
        return rows

    def youtube_rows(self):
        """Subscriptions kept in Stream, rows that follow what was watched here, then the add-on's own lists."""
        rows = [('dir', title, yt.uploads_url(channel_id), 'YouTube', None) for title, channel_id in yt.PINNED]
        followed = db.channels()
        if followed:
            rows.append(('dir', 'New from Your Subscriptions', yt.subs_url(), 'YouTube', None))
        for video, name in db.recent_videos(2):
            rows.append(('dir', 'Because you watched ' + shorten(name), yt.url('special/related_videos/?video_id=' + video), 'YouTube', None))
        for name in db.recent_titles(1):
            title, year = meta.split_title(name)
            rows.append(('dir', 'Videos about ' + title, yt.search_url(' '.join(p for p in (title, year) if p)), 'YouTube', None))
        rows += [('dir', title, yt.url(path), 'YouTube', None) for title, path in yt.lists()]
        if followed:
            tiles = []
            for channel in followed:
                url = yt.uploads_url(channel['id'])
                li = list_item({'t': 'folder', 'name': channel['name'], 'url': url, 'img': channel['img'], 'yt': 1}, url, 'YouTube',
                               meta='Channel')
                li.setProperty('wide', '1')
                li.setProperty('channel', channel['id'])
                li.setProperty('hint', touch_words('OK  Open     Hold OK  Unsubscribe'))
                tiles.append(li)
            rows.append(('items', 'Your Subscriptions', tiles))
        else:
            rows.append(('items', 'Subscriptions', [tile('Follow channels in Stream', 'setting', action='subscribe_help',
                                                         meta='No Google account needed',
                                                         plot='Hold OK on any YouTube video and choose Subscribe to channel. '
                                                              'New videos from the channels you follow then appear here.')]))
        if not yt.has_key():
            rows.append(('items', 'More from YouTube', [tile('Use your YouTube account\'s own lists', 'setting', action='youtube',
                                                              meta='Needs a Google API key',
                                                              plot=yt.KEY_HELP)]))
        return rows

    def specs(self, key):
        """Rows for a tab: ('items', title, [ListItem]) or ('dir', title, url, group, only)."""
        if key == 'downloads':
            return self.download_rows()
        if key == 'history':
            return self.history_rows()
        if db.KIDS:
            return self.kids_specs(key)
        u = cat.URLS
        english = db.lang_match('english')
        english_movies = [('dir', 'English Movies', u['english_movies'], 'English', None)] if english else []
        english_tv = [('dir', 'English TV Shows', u['english_tv'], 'English', None)] if english else []
        if key == 'home':
            rows = []
            resume = db.continue_watching(fold=True)
            if resume:
                rows.append(('items', 'Continue Watching', [watched_item(e) for e in resume]))
            for title, entries in recs.rows(fetch=True):  # every visit to Home asks the service for new lists and rebuilds
                tiles = []
                for e in entries:
                    li = entry_item(e, meta=e.get('meta'), suggestion=bool(e.get('suggestion')))
                    if e.get('number'):
                        li.setProperty('number', str(e['number']))  # posted picks are numbered on their posters
                    if e.get('card'):
                        li.setProperty('card', '1')  # a match: its team badge on a square card, like a channel
                        li.setProperty('logo', '1')
                    tiles.append(li)
                rows.append(('items', title, tiles))
            saved = db.mylist()
            if saved:
                rows.append(('items', 'My List', [entry_item(e) for e in saved]))
            return rows
        if key == 'search':
            return self.grouped_search_rows(self.search_query, None)
        if key == 'movies':
            if self.search_query:
                return self.grouped_search_rows(self.search_query, discover.MOVIE_KINDS)
            if self.year_filter:
                return self.grouped_rows(self.year_filter, library.by_year(self.year_filter, discover.MOVIE_KINDS))
            if self.letter_filter:
                label = 'Movies A–Z' if self.letter_filter == 'ALL' else 'Movies · ' + self.letter_filter
                return self.grouped_rows(label, library.by_letter(self.letter_filter, discover.MOVIE_KINDS))
            if self.filter:
                return self.filtered_rows(discover.MOVIE_KINDS)
            movie_rows = self.order_language_rows(self.language_rows(u['movies'], '{0} Movies', ROWS - 2) + english_movies)
            return (movie_rows + [('dir', 'Kids Movies & Shows', u['kids'], 'Kids', None)])[:ROWS]
        if key == 'shows':
            if self.search_query:
                return self.grouped_search_rows(self.search_query, discover.SHOW_KINDS)
            if self.year_filter:
                return self.grouped_rows(self.year_filter, library.by_year(self.year_filter, discover.SHOW_KINDS))
            if self.letter_filter:
                label = 'Shows A–Z' if self.letter_filter == 'ALL' else 'Shows · ' + self.letter_filter
                return self.grouped_rows(label, library.by_letter(self.letter_filter, discover.SHOW_KINDS))
            if self.filter:
                return self.filtered_rows(discover.SHOW_KINDS)
            series = self.order_language_rows(self.language_rows(u['web_series'], '{0} Web Series', 14) + english_tv)
            tv = self.order_language_rows(self.language_rows(u['tv_shows'], '{0} TV Shows', max(4, ROWS - len(series))))
            return (series + tv)[:ROWS]
        if key == 'sports':
            return self.sports_rows(ROWS - 1)
        if key == 'kids':
            return [('dir', 'Kids', u['kids'], 'Kids', None)]
        if key == 'youtube':
            if not yt.installed():
                return [('items', 'YouTube', [tile('Install the YouTube add-on', 'setting', action='youtube',
                                                    plot='Stream shows YouTube through Kodi\'s YouTube add-on. Install it from '
                                                         'Add-ons > Install from repository > Video add-ons, then come back here.')])]
            return self.youtube_rows()
        if key == 'mylist':
            saved = db.mylist()
            rows = [('items', 'My List', [entry_item(e) for e in saved])] if saved else []
            return rows + self.history_rows()
        if key == 'settings':
            return [('items', 'Settings', self.settings_tiles())]
        if key == 'live':
            langs = [f for f in cat.get_dir(u['live']) if f['t'] == 'folder' and f['name'].lower() != 'sports']
            # Kids channels stay available whatever languages were chosen; sports has its own tab.
            chosen = db.chosen_languages()
            if chosen:
                kept = [l for l in langs if db.lang_match(l['name'], chosen) or l['name'].lower() == 'kids']
                if any(db.lang_match(l['name'], chosen) for l in kept):
                    langs = kept
            langs = cat.by_affinity(langs)  # the viewer's first language leads here too
            saved = cat.state.get('live_lang')
            current = next((l for l in langs if l['name'] == saved), langs[0] if langs else None)
            tiles = [tile(cat.title_case(lang['name']), 'lang', lang=lang['name']) for lang in langs]
            out = [('items', 'Language', tiles)]
            if current:
                folders = [f for f in cat.get_dir(current['url']) if f['t'] == 'folder']
                out += [('dir', '{0} · {1}'.format(cat.title_case(current['name']), cat.title_case(f['name'])), f['url'], f['name'], None)
                        for f in folders[:ROWS - 1]]
            return out
        return []

    def load_tab(self, key, focus=True):
        """Show a section. The rows themselves are only ever touched by one loader at a time (see fill)."""
        if self.closed:
            return
        audit.event('tab', name=key, free_memory=audit.memory())
        if key != self.tab:
            self.filter = self.year_filter = self.letter_filter = None  # filters belong to the tab they were chosen on
            if key != 'search':
                self.search_query = None
        self.tab = key
        self.token += 1
        self.setProperty('status', 'Loading…')
        self.spawn(self.fill, key, self.token, focus)

    def focus_rows(self, token):
        """Move focus into the first row. The row only becomes focusable a frame after its title is set, so retry."""
        for _ in range(15):
            if token != self.token or self.closed:
                return
            try:
                self.setFocusId(FIRST_ROW)
                time.sleep(0.1)
                if self.getFocusId() == FIRST_ROW:
                    return
            except Exception:
                time.sleep(0.1)  # nothing focusable yet

    def fill(self, key, token, focus):
        """Load a section's rows. Listings are fetched without the lock; every change to the row controls is made
        while holding self.ui_lock and only if this is still the newest request. On a slow device a newer request
        often arrives while an older one is still loading, and two loaders changing the same list crashed Kodi."""
        def current():
            return token == self.token and not self.closed

        slot = 0
        started = time.time()
        try:
            specs = self.specs(key)[:ROWS]
            with self.ui_lock:
                if not current():
                    return
                self.row_browse = {}
                for n in range(ROWS):
                    self.setProperty('row{0}.title'.format(n), '')
                    self.setProperty('row{0}.wide'.format(n), '')
                    self.setProperty('row{0}.top10'.format(n), '')
                    self.setProperty('row{0}.browse'.format(n), '')
                    self.getControl(FIRST_ROW + n).reset()
                self.show_filters(key)
            for row in specs:
                if not current():
                    return
                browse_label = ''
                browse_target = None
                if row[0] == 'items':
                    title, items = row[1], row[2]
                    if title.startswith('Top 10'):
                        for rank, li in enumerate(items, 1):
                            li.setProperty('rank', str(rank))
                    if title in getattr(self, 'more_entries', {}):
                        group, found_all = self.more_entries[title]
                        browse_label = 'All {0:,} ›'.format(len(found_all))
                        browse_target = ('entries', title, group, found_all)
                else:
                    _, title, url, group, only = row
                    try:
                        raw = cat.get_dir(url)
                    except Exception as e:
                        cat.log('row failed: {0}'.format(e))
                        continue
                    has_more = any(i['t'] in ('next', 'filter', 'search') for i in raw)
                    found = [i for i in raw if i['t'] == only] if only else cat.showable(raw)
                    if not found:
                        continue
                    items = [list_item(i, url, group) for i in found[:ROW_LIMIT]]
                    show_see_all = len(found) > ROW_LIMIT or has_more or any(i['t'] == 'folder' for i in found)
                    if show_see_all:
                        for li in items:
                            li.setProperty('row_title', title)
                            li.setProperty('row_url', url)
                        more = see_all_tile(title, url, group)
                        if items and all(i.getProperty('wide') for i in items):
                            more.setProperty('wide', '1')
                        items.append(more)
                        browse_label = 'Browse all ›'
                        browse_target = ('dir', title, url, group)
                with self.ui_lock:
                    if not current():
                        return
                    self.getControl(FIRST_ROW + slot).addItems(items)
                    self.setProperty('row{0}.title'.format(slot), title)
                    self.setProperty('row{0}.browse'.format(slot), browse_label)
                    if browse_target is not None:
                        self.row_browse[slot] = browse_target
                    # A row of videos is drawn with wide tiles (the skin can only switch layouts per row, not per tile).
                    # Wide tiles only when every tile is a video; a mixed row keeps posters, with videos letterboxed inside.
                    self.setProperty('row{0}.wide'.format(slot), '1' if items and all(i.getProperty('wide') for i in items) else '')
                    # The Top 10 row draws a big numeral beside each poster.
                    self.setProperty('row{0}.top10'.format(slot), '1' if title.startswith('Top 10') else '')
                    if slot == 0:
                        self.setProperty('status', '')
                if slot == 0 and focus:
                    # Only take focus when the viewer opened the tab, never while they are moving along the tabs.
                    self.focus_rows(token)
                slot += 1
        except Exception as e:
            cat.log('tab failed: {0}'.format(e))
            audit.error('home.fill ' + key)
        if slot == 0:
            with self.ui_lock:
                if current():
                    empty = {'mylist': 'My List is empty. Hold OK on any title (or press the menu key) and choose Add to My List.',
                             'movies': 'Nothing found matching this filter yet.',
                             'shows': 'Nothing found matching this filter yet.',
                             'search': 'No matches found. Try a shorter title, a year, or a channel name.',
                             'history': 'Nothing has been played on this profile yet.',
                             'downloads': 'Nothing downloaded yet. Press and hold a movie or an episode and choose Download '
                                          'to keep it on this device and watch it without a connection.',
                             'youtube': yt.KEY_HELP}
                    self.setProperty('status', empty.get(key, 'Nothing to show. Check that the Sasta TV addon opens and is signed in.'))
            if key not in ('mylist', 'history', 'downloads'):
                audit.event('empty_listing', screen='tab:' + key, filter=self.filter or self.search_query or '')
            return
        audit.event('tab_loaded', tab=key, rows=slot, ms=int((time.time() - started) * 1000), filter=self.filter or self.search_query or '')
        # Ratings arrive after the rows are on screen, so browsing never waits for them.
        for n in range(slot):
            if not current():
                return
            enrich(self.getControl(FIRST_ROW + n), current, 12, self.ui_lock)

    def settle_on_tab(self):
        """Wait until the highlight has rested on a tab for a moment, then show it. Sweeping across the tab bar
        therefore loads one section, not every section passed on the way."""
        monitor = xbmc.Monitor()
        try:
            while not self.closed:
                key, since = self.pending
                if time.time() - since >= 0.4:
                    if key != self.tab:
                        self.load_tab(key, focus=False)
                    return
                if monitor.waitForAbort(0.1):
                    return
        finally:
            self.settling = False

    def search(self):
        if self.tab == 'youtube':
            query = xbmcgui.Dialog().input('Search YouTube')
            if query:
                db.log_event('searched', query, 'YouTube')
                if open_grid('YouTube: \u201c{0}\u201d'.format(query), url=yt.search_url(query), group='YouTube'):
                    self.go_home()
            return
        if db.KIDS:
            query = xbmcgui.Dialog().input('Search kids movies and shows')
            if not query:
                return
            db.log_event('searched', query, 'Kids')
            hits = cat.rank_results(query, cat.search(query))
            if open_grid('Results for “{0}”'.format(query), entries=hits, group='Kids'):
                self.go_home()
            return
        if self.tab == 'shows':
            prompt = 'Search shows, web series, or channels'
            target_tab = 'shows'
        elif self.tab == 'movies':
            prompt = 'Search movies by title or year'
            target_tab = 'movies'
        else:
            prompt = 'Search movies, shows, or channels'
            target_tab = 'search'
        query = xbmcgui.Dialog().input(prompt, defaultt=self.search_query or '')
        if not query:
            return
        db.log_event('searched', query, None, target_tab)
        self.search_query = query.strip()
        self.filter = self.year_filter = self.letter_filter = None
        self.load_tab(target_tab, focus=True)

    def browse_picker(self, mode):
        """Open a direct picker of Movie Sections or Show Groups & TV Channels (no far-right See all scrolling needed)."""
        if mode == 'movies':
            items = library.movie_sections()
            heading = 'Browse Movie Sections'
        else:
            items = library.show_groups()
            heading = 'Browse Show Groups & Channels'
        if not items:
            notify('No sections available yet')
            return
        picked = xbmcgui.Dialog().select(heading, [it['label'] for it in items])
        if picked < 0:
            return
        chosen = items[picked]
        audit.event('browse_group', mode=mode, title=chosen['title'])
        if open_grid(chosen['title'], url=chosen['url'], group=chosen['group']):
            self.go_home()

    def refresh_recommendations(self):
        busy = xbmcgui.DialogProgressBG()
        busy.create('Stream', 'Building recommendations…')
        try:
            cat.warm_index()
            meta.warm(40)
            made = ai.recommend(cat.top_groups(), force=True, pool=recs.pool_for_model())
            recs.rows(force=True)
        except Exception as e:
            made = False
            cat.log('recommend failed: {0}'.format(e))
        finally:
            busy.close()
        return made

    def run_setting(self, action):
        if action in ('open_mylist', 'open_history'):
            self.load_tab(action[5:])  # shown like a tab; Back returns to Home
            return
        if action == 'send_log':
            if xbmcgui.Dialog().yesno('Stream', 'Send the latest activity log to the developer?\nIt includes what was opened and played on this profile.'):
                note = xbmcgui.Dialog().input('What went wrong? (optional)')
                sent, message = report.send('sent by viewer', note, manual=True)
                xbmcgui.Dialog().ok('Stream', message)
        elif action == 'log':
            xbmcgui.Dialog().textviewer('Stream activity log', audit.tail(250) or 'The log is empty.')
        elif action == 'switch':
            self.switch = True
            self.close()
        elif action == 'profile':
            if onboarding(False):
                self.refresh_recommendations()
                self.go_home()
        elif action == 'ratings':
            entries = [dict(e, meta='Liked') for e in db.rated(1)] + [dict(e, meta='Disliked') for e in db.rated(-1)]
            if open_grid('Liked and disliked titles', entries=entries):
                self.go_home()
        elif action == 'keys':
            xbmcaddon.Addon('script.stream').openSettings()
        elif action == 'pin':
            self.set_kids_pin()
        elif action == 'reset':
            dialog = xbmcgui.Dialog()
            if dialog.yesno('Start fresh', 'Erase every profile on this device, with all watch history, My List, likes, '
                                           'recommendations and setup answers?\nThis cannot be undone.', nolabel='Keep everything', yeslabel='Erase'):
                if dialog.yesno('Start fresh', 'Last check: erase everything and run the first-time setup again?',
                                nolabel='Keep everything', yeslabel='Erase everything'):
                    reset_everything()
                    notify('Everything erased. Setting up from the start…', 4000)
                    self.reset = True
                    self.close()
        elif action == 'youtube':
            yt.open_settings()
        elif action == 'subscribe_help':
            xbmcgui.Dialog().ok('Stream', 'Hold OK on any YouTube video and choose Subscribe to channel.\n'
                                          'New videos from the channels you follow then appear on the YouTube tab and on Home. '
                                          'No Google account or key is needed.')
        elif action == 'refresh':
            made = self.refresh_recommendations()
            source = db.meta('recs', {}).get('source', 'local')
            xbmcgui.Dialog().ok('Stream', 'Recommendations updated ({0}).'.format(
                ai.engine_name(source) or 'using your languages and likes; no model is set up or it did not respond')
                if made else 'Could not build recommendations. The catalogue index is empty or Kodi could not reach Sasta TV.')
            self.go_home()
        elif action == 'clear_recs':
            db.clear_suggestions()
            db.set_meta('recs', {})
            notify('Recommendations cleared')
        elif action == 'clear_history' and xbmcgui.Dialog().yesno('Stream', 'Clear all watch history and resume points?'):
            db.clear_history()
            notify('Watch history cleared')
        elif action == 'library':
            count, done, total, now = library.status()
            text = '{0:,} titles saved on this device.[CR]{1} of {2} sections fully read.'.format(count, done, total)
            if now:
                text += '[CR]Reading now: ' + now
            if xbmcgui.Dialog().yesno('Stream', text + '[CR][CR]Rebuild the library from the first page?', nolabel='Close', yeslabel='Rebuild'):
                library.rebuild()
                notify('Library will be rebuilt in the background')

    def library_note(self):
        count, done, total, _ = library.status()
        if not total:
            return 'Building…'
        return '{0:,} titles{1}'.format(count, '' if done == total else ' · building')

    def set_kids_pin(self):
        dialog = xbmcgui.Dialog()
        profiles = db.list_profiles()
        kids = [p for p in profiles if p.get('kids')]
        if not kids:
            if dialog.yesno('Stream', 'There is no kids profile yet. Add one now?'):
                if add_profile():
                    notify('Profile added')
            return
        which = kids[0] if len(kids) == 1 else None
        if which is None:
            choice = dialog.select('Which kids profile?', [p['name'] for p in kids])
            if choice < 0:
                return
            which = kids[choice]
        pin = dialog.input('PIN for leaving {0} (leave empty to remove the PIN)'.format(which['name']), type=xbmcgui.INPUT_NUMERIC)
        for p in profiles:
            if p['id'] == which['id']:
                p['pin'] = pin or ''
        db.save_profiles(profiles)
        if CURRENT['profile'] and CURRENT['profile']['id'] == which['id']:
            CURRENT['profile']['pin'] = pin or ''
        notify('PIN set for ' + which['name'] if pin else 'PIN removed from ' + which['name'])
        self.load_tab('settings')

    def may_leave(self):
        """A kids profile with a PIN asks for it before Stream closes or switches profile."""
        profile = CURRENT['profile'] or {}
        if not (profile.get('kids') and profile.get('pin')):
            return True
        BUSY['on'] = True
        try:
            entered = xbmcgui.Dialog().input('PIN to leave ' + profile['name'], type=xbmcgui.INPUT_NUMERIC)
        finally:
            BUSY['on'] = False
        if entered == profile['pin']:
            return True
        if entered:
            notify('Wrong PIN')
        return False

    def billboard(self):
        """After a minute without a key press, walk the highlight along the recommended titles so the description
        panel shows them off, one every few seconds, until someone picks up the remote. Not on a touch screen, where
        it would pull the rows out from under a finger."""
        if TOUCH:
            return
        monitor = xbmc.Monitor()
        while not self.closed and not monitor.waitForAbort(6):
            if (self.tab != 'home' or BUSY['on'] or OPEN['grids'] or xbmc.getGlobalIdleTime() < IDLE_AFTER
                    or xbmc.Player().isPlaying() or xbmcgui.getCurrentWindowDialogId() != 9999):
                continue
            row = next((FIRST_ROW + n for n in range(ROWS) if self.getProperty('row{0}.title'.format(n)).startswith('Top Picks')), None)
            if row is None:
                row = next((FIRST_ROW + n for n in range(ROWS) if self.getProperty('row{0}.title'.format(n)).endswith(('Movies', 'Web Series'))), None)
            if row is None:
                continue
            with self.ui_lock:
                if self.closed or xbmc.getGlobalIdleTime() < IDLE_AFTER:
                    continue
                try:
                    control = self.getControl(row)
                    if not control.size():
                        continue
                    if self.getFocusId() != row:
                        self.setFocusId(row)
                    control.selectItem((control.getSelectedPosition() + 1) % control.size())
                except Exception:
                    continue

    def step_rows(self, step, focus):
        """Touch: a swipe up moves to the next row, a swipe down to the one before (from the first row, the tabs)."""
        rows = [FIRST_ROW + n for n in range(ROWS) if self.getProperty('row{0}.title'.format(n))]
        if not rows:
            return
        if focus in rows:
            at = rows.index(focus) + step
        else:
            at = 0 if step > 0 else -1
        try:
            self.setFocusId(TABS_ID if at < 0 else rows[min(at, len(rows) - 1)])
        except Exception:
            pass

    def selected_tab(self):
        li = self.getControl(TABS_ID).getSelectedItem()
        return li.getProperty('key') if li else None

    def play_trailer_of(self, control_id):
        li = self.getControl(control_id).getSelectedItem()
        if li and is_title(li):
            BUSY['on'] = True
            try:
                watch_trailer(li)
            finally:
                BUSY['on'] = False

    def onClick(self, control_id):
        if control_id == CLOSE_ID:
            if self.may_leave():
                self.close()
        elif control_id == SEARCH_ID:
            self.search()
        elif control_id == GEAR_ID:
            self.load_tab('settings')
        elif control_id == FILTER_ID:
            li = self.getControl(FILTER_ID).getSelectedItem()
            if li is not None:
                choice = li.getProperty('genre') or ''
                kinds = discover.MOVIE_KINDS if self.tab == 'movies' else discover.SHOW_KINDS
                section_name = 'Movies' if self.tab == 'movies' else 'Shows'
                if choice == '__home__':
                    self.go_home()
                elif choice == '__browse__':
                    self.browse_picker(self.tab)
                elif choice == '__browse_movies__':
                    self.browse_picker('movies')
                elif choice == '__browse_shows__':
                    self.browse_picker('shows')
                elif choice == '__year__':
                    years = library.years_for(kinds)
                    if not years:
                        notify('No years indexed yet — still building library')
                        return
                    labels = ['{0}  ({1:,})'.format(y, n) for y, n in years]
                    picked = xbmcgui.Dialog().select('Browse {0} by Year'.format(section_name), labels)
                    if picked < 0:
                        return
                    self.year_filter = years[picked][0]
                    self.filter = self.letter_filter = self.search_query = None
                    audit.event('filter_year', tab=self.tab, year=self.year_filter)
                    self.load_tab(self.tab, focus=False)
                elif choice == '__az__':
                    letters = library.letters_for(kinds)
                    if not letters:
                        notify('No titles indexed yet — still building library')
                        return
                    labels = ['{0}  ({1:,})'.format('All A–Z' if ch == 'ALL' else ch, n) for ch, n in letters]
                    picked = xbmcgui.Dialog().select('Browse {0} A–Z'.format(section_name), labels)
                    if picked < 0:
                        return
                    self.letter_filter = letters[picked][0]
                    self.filter = self.year_filter = self.search_query = None
                    audit.event('filter_az', tab=self.tab, letter=self.letter_filter)
                    self.load_tab(self.tab, focus=False)
                elif choice == '__find__':
                    self.search()
                else:
                    self.filter = choice or None
                    self.year_filter = self.letter_filter = self.search_query = None
                    audit.event('filter', tab=self.tab, genre=self.filter or 'All')
                    if self.filter:
                        db.log_event('browsed', self.filter, None, self.tab)
                    self.load_tab(self.tab, focus=False)  # the highlight stays on the strip; Down goes to the rows
        elif BROWSE_FIRST <= control_id < BROWSE_FIRST + ROWS:
            target = getattr(self, 'row_browse', {}).get(control_id - BROWSE_FIRST)
            if target:
                mode = target[0]
                audit.event('browse_all', tab=self.tab, row=target[1], via='header')
                if mode == 'dir':
                    _, title, url, group = target
                    if open_grid(title, url=url, group=group):
                        self.go_home()
                else:
                    _, title, group, found = target
                    if open_grid(title, entries=found, group=group):
                        self.go_home()
                NAV['home'] = False
        elif control_id == PROFILE_ID:
            if self.may_leave():
                self.switch = True
                self.close()
        elif control_id == TABS_ID:
            key = self.selected_tab()
            self.pending = (key, 0)
            if key == self.tab and self.getProperty('row0.title'):
                self.focus_rows(self.token)  # already showing: OK just moves into the rows
            else:
                self.load_tab(key)
        elif FIRST_ROW <= control_id < FIRST_ROW + ROWS:
            control = self.getControl(control_id)
            li = control.getSelectedItem()
            if li is None or first_tap(self, control, li):
                return
            kind = li.getProperty('t')
            audit.event('click', tab=self.tab, kind=kind, label=(li.getLabel() or '')[:80])
            if kind == 'download':
                play_download(li)
                return
            if kind == 'lang':
                cat.state['live_lang'] = li.getProperty('lang')
                cat.save_state()
                self.load_tab('live')
            elif kind == 'more':
                more_key = li.getProperty('more_key')
                if more_key in getattr(self, 'more_entries', {}):
                    group, found = self.more_entries[more_key]
                    if open_grid(more_key, entries=found, group=group):
                        self.go_home()
            elif kind == 'setting':
                self.run_setting(li.getProperty('action'))
            elif is_title(li):
                if open_title(li):
                    self.go_home()
            elif activate(li):
                self.go_home()
            elif kind == 'play' and li.getProperty('series') and xbmc.Player().isPlaying():
                self.spawn(autoplay_after, li, self)  # an episode from Continue Watching carries on to the next
            NAV['home'] = False  # a "go Home" request has been dealt with once control is back here

    def onAction(self, action):
        if getattr(self, 'closed', False) or BUSY['on']:
            return
        try:
            focus = self.getFocusId()
        except Exception:
            focus = 0  # nothing has focus yet
        code = action.getId()
        self.touched = True
        if TOUCH:
            if code == ACTION_RIGHT_CLICK:
                code = ACTION_CONTEXT  # a long press
            elif code in SWIPE_UP or code in SWIPE_DOWN:
                self.step_rows(1 if code in SWIPE_UP else -1, focus)
                return
            elif code in ACTION_MOUSE:
                return  # taps arrive as clicks; nothing else to do
        if focus == 0 and code not in ACTION_BACK:
            # Nothing is highlighted (this can happen when Stream opens by itself as Kodi starts): any key press
            # puts the highlight on the first row, or on the top bar if the rows are still loading.
            try:
                self.setFocusId(FIRST_ROW if self.getProperty('row0.title') else TABS_ID)
            except Exception:
                pass
            return
        if code in ACTION_BACK:
            # Back climbs one level at a time: row/browse -> filter/clear -> tabs -> Home tab -> exit.
            if stop_if_playing_behind():
                return
            if focus == FILTER_ID and self.tab in ('movies', 'shows') and (self.filter or self.year_filter or self.letter_filter or self.search_query):
                self.filter = self.year_filter = self.letter_filter = self.search_query = None
                self.load_tab(self.tab, focus=False)
                return
            if focus not in TOP_BAR:
                self.setFocusId(TABS_ID)
            elif self.tab != 'home':
                self.go_home(focus=False)
            else:
                BUSY['on'] = True
                try:
                    leave = xbmcgui.Dialog().yesno('Stream', 'Exit Stream?')
                finally:
                    BUSY['on'] = False
                if leave and self.may_leave():
                    self.close()
        elif code == ACTION_CONTEXT and FIRST_ROW <= focus < FIRST_ROW + ROWS:
            li = self.getControl(focus).getSelectedItem()
            if li and li.getProperty('t') in ('play', 'folder', 'download'):
                BUSY['on'] = True
                try:
                    result = context_menu(li, False)
                finally:
                    BUSY['on'] = False
                if result == 'home':
                    self.go_home()
                elif result == 'changed' and self.tab in ('home', 'mylist', 'youtube', 'history', 'downloads'):
                    self.load_tab(self.tab)
        elif code == ACTION_INFO and FIRST_ROW <= focus < FIRST_ROW + ROWS:
            self.play_trailer_of(focus)
        elif code == ACTION_DELETE and FIRST_ROW <= focus < FIRST_ROW + ROWS:
            if remove_from_continue_watching(self.getControl(focus).getSelectedItem()):
                self.load_tab(self.tab)
        elif focus == TABS_ID and code in (ACTION_LEFT, ACTION_RIGHT):
            # Moving along the tabs shows the tab the highlight comes to rest on; focus stays on the tabs.
            key = self.selected_tab()
            if key:
                self.pending = (key, time.time())
                if not self.settling:
                    self.settling = True
                    self.spawn(self.settle_on_tab)


def run():
    """One session: pick a profile, show the home screen, repeat while the viewer switches profile."""
    if audit.session_start():
        # The last session never closed: Kodi was killed under it (common on Android when the TV is switched off)
        # or crashed. Email the log if reports are allowed; the viewer can do nothing about it, so no notice is shown.
        threading.Thread(target=report.send, args=('session ended unexpectedly',), daemon=True).start()
    if audit.NEW['install']:
        # First run on this device: let the maintainer know there is a new installation (no activity log attached).
        threading.Thread(target=report.send, args=('new install',), kwargs={'with_log': False, 'limited': False}, daemon=True).start()
    current = None
    # Open with the profile used last time; the picker only appears when the viewer asks to switch.
    remembered = db.last_profile()
    while True:
        if current is None and remembered:
            profile, remembered = remembered, None
            current = CURRENT['profile'] = profile
            audit.event('profile', kids=bool(profile.get('kids')), remembered=True)
            db.use_profile(profile)
            cat.load_state()
            window = Home(skin('home'), ADDON_PATH, 'Default', '1080i')
            xbmcgui.Window(10000).setProperty('script.stream.running', str(time.time()))
            audit.event('window_open')
            try:
                window.doModal()
            finally:
                xbmcgui.Window(10000).clearProperty('script.stream.running')
            audit.event('window_closed')
            again = getattr(window, 'switch', False)
            fresh = getattr(window, 'reset', False)
            window.finish()
            del window
            if fresh:
                current, remembered = None, None  # everything was erased: pick up as a new installation
                continue
            if not again:
                break
            continue
        # Leaving a kids profile can be protected by a PIN.
        if current and current.get('kids') and current.get('pin'):
            if xbmcgui.Dialog().input('PIN to leave ' + current['name'], type=xbmcgui.INPUT_NUMERIC) != current['pin']:
                notify('Wrong PIN')
                profile = current
            else:
                profile = choose_profile(True) or current
        else:
            profile = choose_profile(force=current is not None)
            if profile is None:
                if current is None:
                    break
                profile = current
        current = CURRENT['profile'] = profile
        db.remember_profile(profile)
        audit.event('profile', kids=bool(profile.get('kids')))
        db.use_profile(profile)
        cat.load_state()
        if not profile.get('kids') and db.meta('profile') is None:
            onboarding(True)
        window = Home(skin('home'), ADDON_PATH, 'Default', '1080i')
        xbmcgui.Window(10000).setProperty('script.stream.running', str(time.time()))
        audit.event('window_open')
        try:
            window.doModal()
        finally:
            # Only an open home screen blocks a second launch; a slow shutdown must not.
            xbmcgui.Window(10000).clearProperty('script.stream.running')
        audit.event('window_closed')
        again = getattr(window, 'switch', False)
        fresh = getattr(window, 'reset', False)
        window.finish()
        audit.event('workers_stopped')
        del window
        if fresh:
            current, remembered = None, None
            continue
        if not again:
            break
    cat.flush()
    audit.session_end()


if __name__ == '__main__':
    home = xbmcgui.Window(10000)
    opened = home.getProperty('script.stream.running')
    try:
        cat.rpc('Addons.GetAddonDetails', {'addonid': 'plugin.video.sastatv'})
        sasta = True
    except Exception:
        sasta = False
    if sys.argv[1:2] == ['youtube']:
        yt.open_settings()  # RunScript(script.stream,youtube): the YouTube sign-in and API key helper on its own
    elif not sasta:
        xbmcgui.Dialog().ok('Stream', 'Install the Sasta TV addon and sign in to it first.')
    elif opened and time.time() - float(opened) < 12 * 3600:
        # A copy is already open (or still closing down): starting a second would double the memory in use.
        notify('Stream is already open')
    else:
        try:
            run()
        except Exception:
            audit.error('main')
