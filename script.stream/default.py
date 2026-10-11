import os
import re
import sys
import threading
import time

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
import meta  # noqa: E402
import report  # noqa: E402

ROWS = 14
ROW_LIMIT = 24  # tiles per row; kept modest so low-memory TV sticks are not asked to hold hundreds of posters
TABS_ID = 100
CLOSE_ID = 110
SEARCH_ID = 120
GEAR_ID = 130
PROFILE_ID = 140
TOP_BAR = (TABS_ID, CLOSE_ID, SEARCH_ID, GEAR_ID, PROFILE_ID)
FIRST_ROW = 200
PANEL = 50
EPISODE_LIST = 51   # the same screen shows episodes as a text list instead of poster tiles
ACTION_LEFT, ACTION_RIGHT = 1, 2
ACTION_BACK = (9, 10, 92)  # parent dir, previous menu, nav back
ACTION_CONTEXT = 117
START_TIMEOUT = 45  # seconds to wait for the Sasta TV addon to deliver a stream
CANCEL_GRACE = 1.5  # seconds after a play request during which a cancel is taken as a repeated OK press

TABS = [('home', 'Home'), ('movies', 'Movies'), ('shows', 'Shows'), ('live', 'Live TV'), ('sports', 'Sports'),
        ('kids', 'Kids'), ('mylist', 'My List'), ('history', 'History')]
KIDS_TABS = [('home', 'Home'), ('kids', 'Movies and Shows'), ('live', 'Kids TV'), ('mylist', 'My List'), ('history', 'History')]
CURRENT = {'profile': None}  # the viewer profile in use

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
PLAY = {'since': 0, 'cancel': False, 'stopper': False}
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


def clock(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return '{0}:{1:02d}:{2:02d}'.format(h, m, s) if h else '{0}:{1:02d}'.format(m, s)


def notify(text, ms=3000):
    xbmcgui.Dialog().notification('Stream', text, xbmcgui.NOTIFICATION_INFO, ms)


def list_item(item, parent, group=None, meta=None, saved=False, suggestion=False, label=None, series=None):
    titled = item['t'] == 'play' and not cat.is_live(item)  # channel names such as '... 4K' are not release tags
    clean, tag, note = quality_of(item['name']) if titled else (item['name'], '', '')
    li = xbmcgui.ListItem(label=label or clean)
    li.setProperty('tag', tag)
    if item.get('img'):
        li.setArt({'thumb': item['img']})
    li.setProperty('name', item['name'])
    li.setProperty('plot', item.get('plot', ''))
    if meta is None:
        meta = cat.title_case(group) if group and item['t'] == 'play' else ''
    if note:
        meta = (meta + ' · ' if meta else '') + note
    li.setProperty('meta', meta)
    if item['t'] == 'play' and cat.is_live(item):
        li.setProperty('logo', '1')
        li.setProperty('card', '1')  # channels are drawn as square cards rather than posters
    li.setProperty('t', item['t'])
    li.setProperty('hint', ('OK  Play' if item['t'] == 'play' else 'OK  Open') + '     Hold OK  More options')
    li.setProperty('url', item.get('url', ''))
    li.setProperty('i', str(item.get('i', 1)))
    li.setProperty('parent', parent or '')
    li.setProperty('group', group or '')
    li.setProperty('series', series or '')
    li.setProperty('saved', '1' if saved else '')
    li.setProperty('suggestion', '1' if suggestion else '')
    return li


def entry_item(entry, **kwargs):
    # A saved label is only needed for episodes ("Show · Episode 5"); other titles use their cleaned name.
    kwargs.setdefault('label', entry.get('label') if entry.get('series') else None)
    kwargs.setdefault('series', entry.get('series'))
    return list_item(entry['item'], entry['parent'], entry.get('group'), saved=True, **kwargs)


def tile(label, kind, **props):
    li = xbmcgui.ListItem(label=label)
    li.setProperty('t', kind)
    li.setProperty('card', '1')
    li.setProperty('hint', 'OK  Choose')
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
    for _ in range(40):
        if player.isPlayingVideo() or PLAY['cancel'] or monitor.waitForAbort(0.5):
            break
    while player.isPlayingVideo():
        if PLAY['cancel']:
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
    audit.event('play_ended', title=name, position=int(position), duration=int(duration))
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
    key = db.play_key(item)
    position, duration, done = db.progress(key)
    # A part-watched title carries on from where it stopped. Hold OK offers "Play from the beginning".
    resume = position if (not live and not restart and not done and position > 30 and duration > 0) else 0
    player, monitor = xbmc.Player(), xbmc.Monitor()
    if player.isPlaying():
        player.stop()
        monitor.waitForAbort(1)
    if series and li.getProperty('season'):
        db.set_meta('last:' + series, {'season': li.getProperty('season'), 'episode': item['name']})
    if not cat.play(parent, item, group, series, label):
        xbmcgui.Dialog().ok('Stream', 'This title can only be opened from the Sasta TV addon itself.')
        return
    PLAY['since'], PLAY['cancel'] = time.time(), False
    kind = 'Live TV' if live else cat.section_of(parent)[0]
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
    audit.event('play_started', title=label)
    threading.Thread(target=follow, args=(key, label, group, kind, live), daemon=True).start()


def is_title(li):
    """True for movies and shows from the catalogue (not channels, episodes, tiles or 'See all')."""
    return (li.getProperty('t') in ('play', 'folder') and not li.getProperty('logo') and not li.getProperty('series')
            and li.getLabel() != 'See all ›' and bool(cat.section_of(li.getProperty('parent'))[0]))


def apply_details(li, data):
    """Put a looked-up rating, and a poster or description when the catalogue had none, onto a tile."""
    if not data:
        return
    rating = meta.rating_text(data)
    genres = ', '.join(data.get('genres', [])[:2])
    if not li.getProperty('tagged'):
        # Rating and genres lead the description line: "IMDb 7.4 · Action, Thriller · Telugu".
        li.setProperty('tagged', '1')
        li.setProperty('rating', rating)
        li.setProperty('genres', genres)
        line = ' · '.join(x for x in (rating, genres, li.getProperty('meta')) if x)
        li.setProperty('meta', line)
    if not li.getArt('thumb') and data.get('poster'):
        li.setArt({'thumb': data['poster']})
    if not li.getProperty('plot') and data.get('overview'):
        li.setProperty('plot', data['overview'])


def enrich(control, still_wanted, limit, lock):
    """Look up ratings for the first tiles of a row or grid, one at a time, while that screen is still showing.
    The lookup itself runs unlocked; reading and changing a tile happens under the screen's lock."""
    if not meta.enabled():
        return
    try:
        for n in range(limit):
            with lock:
                if not still_wanted() or n >= control.size():
                    return
                li = control.getListItem(n)
                wanted = is_title(li) and not li.getProperty('tagged')
                name, show = li.getProperty('name'), li.getProperty('t') == 'folder'
            if not wanted:
                continue
            data = meta.lookup(name, show)
            with lock:
                if not still_wanted() or n >= control.size():
                    return
                li = control.getListItem(n)
                if li.getProperty('name') == name:
                    apply_details(li, data)
    except Exception as e:
        cat.log('enrich stopped: {0}'.format(e))


def play_trailer(video_id, title):
    if not xbmc.getCondVisibility('System.HasAddon(plugin.video.youtube)'):
        xbmcgui.Dialog().ok('Stream', 'Trailers play through Kodi\'s YouTube add-on.\n'
                                      'Install "YouTube" from Add-ons > Install from repository > Video add-ons, then try again.')
        return
    PLAY['since'], PLAY['cancel'] = time.time(), False
    xbmc.Player().play('plugin://plugin.video.youtube/play/?video_id=' + video_id)
    threading.Thread(target=follow, args=(None, title + ' (trailer)', None, 'Trailer', True), daemon=True).start()


def details(li):
    """Rating, overview, reviews and trailer for a title."""
    name = li.getProperty('name') or li.getLabel()
    if not meta.enabled():
        xbmcgui.Dialog().ok('Stream', 'Ratings, reviews and trailers are switched off because no TMDb key is available.')
        return
    busy = xbmcgui.DialogProgressBG()
    busy.create('Stream', 'Looking up ' + name + '…')
    try:
        data = meta.lookup(name, li.getProperty('t') == 'folder')
    finally:
        busy.close()
    if not data:
        notify('No details found for this title')
        return
    apply_details(li, data)
    facts = [meta.rating_text(data) + (' (IMDb)' if data.get('imdb_rating') else '')]
    if data.get('runtime'):
        facts.append('{0} min'.format(data['runtime']))
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
            xbmcgui.Dialog().textviewer(data.get('title') or name, data.get('overview') or 'No overview available.')
        elif action == 'reviews':
            text = '\n\n'.join('[B]{0}[/B]\n{1}'.format(r['author'] or 'Review', r['text']) for r in reviews)
            xbmcgui.Dialog().textviewer('Reviews · ' + (data.get('title') or name), text)
        else:
            play_trailer(data['trailer'], data.get('title') or name)
            return


def activate(li, trail=()):
    """Click handling shared by the home rows and the grid. Returns True when the viewer asked for Home."""
    kind = li.getProperty('t')
    if kind == 'folder':
        name = li.getProperty('name') or li.getLabel()
        url = li.getProperty('url')
        # Opening a show starts a trail (show, season) so its episodes can be remembered under the show's name.
        inside_show = bool(trail) or 'sdirurl=' in url
        return open_grid(name, url=url, group=li.getProperty('group') or None, trail=tuple(trail) + (name,) if inside_show else ())
    if kind == 'play':
        start(li)
    return False


def context_menu(li, in_grid):
    """Long-press / menu-key actions. Returns 'home', 'changed' or None."""
    kind = li.getProperty('t')
    if kind not in ('play', 'folder') or li.getLabel() == 'See all ›':
        return 'home' if in_grid and xbmcgui.Dialog().contextmenu(['Go to Home']) == 0 else None
    item, parent, group = item_of(li), li.getProperty('parent'), li.getProperty('group') or None
    section = cat.section_of(parent)[0]
    listed = db.in_mylist(item['name'])
    rating = db.rating(item['name'])
    options = [('open', 'Play' if kind == 'play' else 'Open'),
               ('list', 'Remove from My List' if listed else 'Add to My List'),
               ('like', 'Remove like' if rating > 0 else 'Like'),
               ('dislike', 'Remove dislike' if rating < 0 else 'Dislike')]
    if is_title(li):
        options.insert(1, ('info', 'Rating, reviews and trailer'))
    if kind == 'play' and not cat.is_live(item) and db.progress(db.play_key(item))[0] > 30:
        options[0] = ('open', 'Resume')
        options.insert(1, ('restart', 'Play from the beginning'))
    if li.getProperty('suggestion'):
        options.append(('dismiss', 'Not interested'))
    if in_grid:
        options.append(('home', 'Go to Home'))
    choice = xbmcgui.Dialog().contextmenu([label for _, label in options])
    if choice < 0:
        return None
    action = options[choice][0]
    if action == 'open':
        return 'home' if activate(li) else None
    if action == 'restart':
        start(li, restart=True)
        return None
    if action == 'info':
        details(li)
        return None
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
        if value:
            db.log_event('liked' if value > 0 else 'disliked', item['name'], group, section)
        notify({1: 'Liked', -1: 'Disliked', 0: 'Rating removed'}[value], 2500)
        return 'changed'
    if action == 'dismiss':
        db.dismiss(item['name'])
        db.log_event('dismissed', item['name'], group, section)
        return 'changed'
    return 'home'


# ---------- first-run setup ----------

def onboarding(first_run):
    """Ask for languages, genres, sports and a few liked titles. Returns True when something was saved."""
    dialog = xbmcgui.Dialog()
    profile = db.meta('profile') or {}
    if first_run:
        dialog.ok('Welcome to Stream', 'Four quick questions set up your home screen and recommendations.\n'
                                       'You can change the answers later under Settings.\n\n'
                                       'Stream tells its developer when it is first installed, and emails a problem report with its '
                                       'activity log if it crashes. This can be switched off under Settings.')

    def ask(title, options, saved_key):
        preset = [n for n, o in enumerate(options) if o in profile.get(saved_key, [])]
        picked = dialog.multiselect(title, options, preselect=preset)
        return None if picked is None else [options[n] for n in picked]

    languages = ask('1 of 4 · Which languages do you watch?', db.LANGUAGES, 'languages')
    if languages is None:
        if first_run:
            db.set_meta('profile', {'languages': [], 'genres': [], 'sports': []})
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
    genres = ask('2 of 4 · What do you like to watch?', list(ai.GENRES), 'genres')
    sports = ask('3 of 4 · Which sports do you follow?', list(SPORTS), 'sports')
    db.set_meta('profile', {'languages': languages,
                            'genres': profile.get('genres', []) if genres is None else genres,
                            'sports': profile.get('sports', []) if sports is None else sports})
    db.set_meta('recs_dirty', True)

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
    choices = db.only_chosen(db.titles_in([], 400), lambda e: e.get('group'))[:40]
    if choices:
        labels = ['{0}  [COLOR FF888888]{1}[/COLOR]'.format(e['item']['name'], cat.title_case(e.get('group') or '')) for e in choices]
        picked = dialog.multiselect('4 of 4 · Pick any you like (optional)', labels)
        for n in picked or []:
            entry = choices[n]
            db.rate(entry['item'], entry['parent'], entry.get('group'), 1)
            db.log_event('liked', entry['item']['name'], entry.get('group'), cat.section_of(entry['parent'])[0])
    return True


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

def open_grid(title, url=None, entries=None, group=None, trail=()):
    """Show a full-screen grid. Returns True when the viewer chose Home, so callers can close too."""
    audit.event('open', screen=title)
    win = Grid('script-stream-grid.xml', ADDON_PATH, 'Default', '1080i')
    win.setup(title, url, entries, group, trail)
    # The click that opened this screen is still "in progress" underneath; let the new screen take key presses.
    was_busy, BUSY['on'] = BUSY['on'], False
    try:
        win.doModal()
    finally:
        BUSY['on'] = was_busy
    win.finish()
    del win
    return NAV['home']


class Grid(xbmcgui.WindowXML):
    def setup(self, title, url, entries, group, trail):
        self.title, self.url, self.entries, self.group, self.trail = title, url, entries, group, tuple(trail)
        self.next_url = None
        self.loading = False
        self.ready = False
        self.closed = False
        self.workers = []
        self.ui_lock = threading.Lock()

    def spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        self.workers.append(thread)
        thread.start()

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
            return
        self.setProperty('status', 'Loading…')
        self.spawn(self.load, self.url)
        self.spawn(watch_visibility, self)

    def title_items(self, url, titles):
        """Build tiles for a listing. Inside a show, episodes are put in order and marked with their progress."""
        series = self.trail[0] if self.trail else None
        episodes = bool(series) and bool(titles) and all(i['t'] == 'play' for i in titles)
        self.episodes = episodes
        if episodes and not self.next_url:
            titles = sorted(titles, key=lambda i: cat.natural_key(i['name']))
        marks = db.progress_for([db.play_key(i) for i in titles]) if episodes else {}
        out, upnext = [], None
        # Catch up from where the viewer left off: the last episode played if it is unfinished, otherwise the one after it.
        last_played = db.last_played([db.play_key(i) for i in titles]) if episodes else None
        for n, i in enumerate(titles):
            if last_played and db.play_key(i) == last_played[0]:
                upnext = n if not last_played[1] else min(n + 1, len(titles) - 1)
        for n, i in enumerate(titles):
            position, duration, done = marks.get(db.play_key(i), (0, 0, False))
            meta = None
            if done:
                meta = '✓ Watched'
            elif position > 30:
                meta = 'Resume from ' + clock(position)
            if episodes and not done and upnext is None:
                upnext = n
            label = ('✓ ' + i['name']) if done else None
            out.append(list_item(i, url, self.group or self.title, meta=meta, label=label, series=series if episodes else None))
            if episodes and len(self.trail) > 1:
                out[-1].setProperty('season', self.trail[1])
        if not episodes and series and len(self.trail) == 1:
            # A show's page: land on the season that was watched last.
            season = (db.meta('last:' + series) or {}).get('season')
            for n, i in enumerate(titles):
                if season and i['name'] == season:
                    upnext = n
        return out, upnext

    def load(self, url):
        try:
            self.load_page(url)
        except Exception:
            audit.error('grid.load')

    def load_page(self, url):
        self.loading = True
        try:
            items = cat.get_dir(url)
        except Exception as e:
            audit.event('load_failed', screen=self.title, reason=str(e))
            if self.closed:
                return
            self.setProperty('status', 'Could not load: {0}'.format(e))
            if self.panel.size() == 0:
                self.panel.addItems([self.home_tile()])
                self.setFocusId(self.view)
            self.loading = False
            return
        if self.closed:
            return
        nxt = [i for i in items if i['t'] == 'next']
        self.next_url = nxt[0]['url'] if nxt else None
        first_page = self.panel.size() == 0
        shown = []
        if first_page:
            shown.append(self.home_tile())
            for extra in [i for i in items if i['t'] in ('search', 'filter')]:
                label = 'Search in ' + self.title if extra['t'] == 'search' else cat.title_case(extra['name'])
                shown.append(list_item(dict(extra, name=label, plot=''), url, self.group))
        titles, upnext = self.title_items(url, cat.showable(items))
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
            position = self.panel.getSelectedPosition()
            self.panel.addItems(shown)
            if not first_page and position >= 0:
                self.panel.selectItem(position)
        self.setProperty('status', '' if titles or not first_page else 'Nothing here')
        if first_page:
            self.setFocusId(self.view)
            if titles:
                # Land on the next episode to watch, or on the first title.
                self.select_when_ready(first_title + (upnext or 0))
                if upnext:
                    self.setProperty('status', 'Up next: ' + titles[upnext].getProperty('name'))
        self.loading = False
        if first_page:
            enrich(self.panel, lambda: not self.closed and not self.loading, 24, self.ui_lock)

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
        if control_id not in (PANEL, EPISODE_LIST) or self.loading:
            return
        li = self.panel.getSelectedItem()
        if li is None:
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
        elif kind == 'play':
            start(li)
            if xbmc.Player().isPlaying():
                self.spawn(self.after_playback)
        else:
            self.leave_if_home(activate(li, self.trail))

    def after_playback(self):
        player, monitor = xbmc.Player(), xbmc.Monitor()
        if monitor.waitForAbort(3):
            return
        while player.isPlayingVideo() and not self.closed:
            if monitor.waitForAbort(1):
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
        if action.getId() == ACTION_CONTEXT:
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
        HOME['window'] = self
        self.setProperty('kids', '1' if db.KIDS else '')
        tabs = []
        for key, label in (KIDS_TABS if db.KIDS else TABS):
            li = xbmcgui.ListItem(label=label)
            li.setProperty('key', key)
            tabs.append(li)
        self.getControl(TABS_ID).addItems(tabs)
        self.load_tab('home')
        self.spawn(self.background)
        self.spawn(watch_visibility, self)

    def background(self):
        """Keep the local index and the suggestions fresh without holding up the screen."""
        try:
            cat.warm_index()
            # Show the new row straight away only if the viewer has not started navigating; otherwise it appears next time Home loads.
            if ai.recommend(cat.top_groups()) and not self.touched and not self.closed and self.tab == 'home':
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
        last = db.meta('recs', {})
        engine = {'llm+jev': 'Claude + Jev', 'llm': 'Claude', 'jev': 'Jev', 'local': 'your languages and likes'}.get(last.get('source'), 'not run yet')
        profile = db.meta('profile') or {}
        chosen = ', '.join(profile.get('languages', [])) or 'all languages'
        return [
            tile('My languages, sports and likes', 'setting', action='profile', meta='Now: ' + chosen,
                 plot='Change the languages, genres and sports you chose at setup, and pick more titles you like.'),
            tile('Liked and disliked titles', 'setting', action='ratings',
                 plot='See everything you have marked with Like or Dislike. Hold OK on any title to rate it.'),
            tile('Options and keys', 'setting', action='keys',
                 plot='Choose whether Stream opens when Kodi starts and whether problem reports are sent, and enter keys for AI recommendations and ratings. Stream works without any keys.'),
            tile('Refresh recommendations', 'setting', action='refresh', meta='Last built with: ' + engine,
                 plot='Build a new Recommended for You row now.'),
            tile('Clear recommendations', 'setting', action='clear_recs', plot='Remove the saved recommendations.'),
            tile('Clear watch history', 'setting', action='clear_history',
                 plot='Forget everything played, including resume points and watched episodes.'),
            tile('Activity log', 'setting', action='log',
                 plot='What Stream did recently, including errors and whether the last session ended unexpectedly. Useful when reporting a problem.'),
            tile('Send log to the developer', 'setting', action='send_log',
                 plot='Email the latest activity log so a problem can be investigated. It includes what was opened and played. '
                      'A report is also sent automatically after a crash unless that is switched off under Keys and options.'),
            tile('Switch or add profile', 'setting', action='switch', meta='Now: ' + (CURRENT['profile'] or {}).get('name', ''),
                 plot='Stream opens with the profile used last. Each profile has its own history, My List, likes and recommendations. A kids profile shows only children\'s titles.'),
        ]

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
            for title, entries in (('Continue Watching', db.continue_watching()), ('Recently Played', db.recent(30))):
                if entries:
                    rows.append(('items', title, [entry_item(e) for e in entries]))
            picks = db.suggestions()
            if picks:
                rows.insert(1 if rows else 0, ('items', 'Recommended for You',
                                               [entry_item(p, meta=p['reason'], suggestion=True) for p in picks]))
            return rows + [titles] + self.kids_live_rows()
        if key == 'kids':
            return [titles]
        if key == 'live':
            return self.kids_live_rows()
        if key == 'mylist':
            saved = db.mylist()
            return [('items', 'My List', [entry_item(e) for e in saved])] if saved else []
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

    def specs(self, key):
        """Rows for a tab: ('items', title, [ListItem]) or ('dir', title, url, group, only)."""
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
            resume = db.continue_watching()
            if resume:
                rows.append(('items', 'Continue Watching', [entry_item(e) for e in resume]))
            picks = db.suggestions()
            if picks:
                rows.append(('items', 'Recommended for You',
                             [entry_item(p, meta=p['reason'] or cat.title_case(p.get('group') or ''), suggestion=True) for p in picks]))
            recent = db.recent(30)
            if recent:
                rows.append(('items', 'Recently Played', [entry_item(e) for e in recent]))
            liked = db.rated(1)
            if liked:
                rows.append(('items', 'Liked by You', [entry_item(e) for e in liked]))
            rows += self.sports_rows(0)
            rows += self.language_rows(u['movies'], '{0} Movies', 4)
            rows += self.language_rows(u['web_series'], '{0} Web Series', 2)
            rows += english_movies
            if english:
                rows.append(('dir', 'Kids', u['kids'], 'Kids', None))
            return rows
        if key == 'movies':
            return self.language_rows(u['movies'], '{0} Movies', ROWS - 1) + english_movies
        if key == 'shows':
            return (self.language_rows(u['web_series'], '{0} Web Series', 9) + english_tv
                    + self.language_rows(u['tv_shows'], '{0} TV Shows', 4))
        if key == 'sports':
            return self.sports_rows(ROWS - 1)
        if key == 'kids':
            return [('dir', 'Kids', u['kids'], 'Kids', None)]
        if key == 'mylist':
            saved = db.mylist()
            return [('items', 'My List', [entry_item(e) for e in saved])] if saved else []
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
        try:
            specs = self.specs(key)[:ROWS]
            with self.ui_lock:
                if not current():
                    return
                for n in range(ROWS):
                    self.setProperty('row{0}.title'.format(n), '')
                    self.getControl(FIRST_ROW + n).reset()
            for row in specs:
                if not current():
                    return
                if row[0] == 'items':
                    title, items = row[1], row[2]
                else:
                    _, title, url, group, only = row
                    try:
                        found = cat.get_dir(url)
                    except Exception as e:
                        cat.log('row failed: {0}'.format(e))
                        continue
                    found = [i for i in found if i['t'] == only] if only else cat.showable(found)
                    if not found:
                        continue
                    items = [list_item(i, url, group) for i in found[:ROW_LIMIT]]
                    if len(found) > ROW_LIMIT or any(i['t'] == 'folder' for i in found):
                        items.append(list_item({'t': 'folder', 'name': title, 'url': url}, url, group, label='See all ›'))
                with self.ui_lock:
                    if not current():
                        return
                    self.getControl(FIRST_ROW + slot).addItems(items)
                    self.setProperty('row{0}.title'.format(slot), title)
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
                     'history': 'Nothing has been played on this profile yet.'}
                    self.setProperty('status', empty.get(key, 'Nothing to show. Check that the Sasta TV addon opens and is signed in.'))
            return
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
        query = xbmcgui.Dialog().input('Search movies and shows')
        if not query:
            return
        db.log_event('searched', query)
        busy = xbmcgui.DialogProgressBG()
        busy.create('Stream', 'Searching every section…')
        try:
            # Titles already in the local index appear without waiting for the provider.
            local = [dict(hit, meta=cat.title_case(hit.get('group') or '')) for hit in db.find_names(query, 40)]
            local = db.only_chosen(local, lambda e: e.get('group')) if local else []
            found = cat.rank_results(query, cat.search(query))
            names = {e['item']['name'] for e in found}
            entries = found + [e for e in local if e['item']['name'] not in names]
            # A descriptive request ("feel-good family comedy") goes to the AI as well as the title search.
            if len(query.split()) >= 3:
                busy.update(60, 'Stream', 'Asking the AI for best matches…')
                seen = set()
                smart = []
                for pick in ai.smart_search(query):
                    name = pick['entry']['item']['name']
                    if name not in seen:
                        seen.add(name)
                        smart.append(dict(pick['entry'], meta='Best match · ' + (pick['reason'] or 'chosen by AI')))
                entries = smart + [e for e in entries if e['item']['name'] not in seen]
            # Related titles from the local index: the query's words found in other names or descriptions.
            have = {e['item']['name'] for e in entries}
            related = []
            for word in [w for w in re.findall(r'\w+', query) if len(w) > 3][:4]:
                for hit in db.only_chosen(db.find_titles(word, 30), lambda e: e.get('group')):
                    if hit['item']['name'] not in have:
                        have.add(hit['item']['name'])
                        related.append(dict(hit, meta='Related · ' + cat.title_case(hit.get('group') or '')))
            entries += related[:30]
            if not entries:
                entries = [dict(p, meta='No match · you might like this') for p in db.suggestions()]
        finally:
            busy.close()
        if open_grid('Results for “{0}”'.format(query), entries=entries):
            self.go_home()

    def refresh_recommendations(self):
        busy = xbmcgui.DialogProgressBG()
        busy.create('Stream', 'Building recommendations…')
        try:
            cat.warm_index()
            made = ai.recommend(cat.top_groups(), force=True)
        except Exception as e:
            made = False
            cat.log('recommend failed: {0}'.format(e))
        finally:
            busy.close()
        return made

    def run_setting(self, action):
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
        elif action == 'refresh':
            made = self.refresh_recommendations()
            source = db.meta('recs', {}).get('source', 'local')
            xbmcgui.Dialog().ok('Stream', 'Recommendations updated ({0}).'.format(
                {'llm+jev': 'Claude + Jev', 'llm': 'Claude', 'jev': 'Jev'}.get(source, 'using your languages and likes; no AI key is set or it did not respond'))
                if made else 'Could not build recommendations. The catalogue index is empty or Kodi could not reach Sasta TV.')
            self.go_home()
        elif action == 'clear_recs':
            db.clear_suggestions()
            db.set_meta('recs', {})
            notify('Recommendations cleared')
        elif action == 'clear_history' and xbmcgui.Dialog().yesno('Stream', 'Clear all watch history and resume points?'):
            db.clear_history()
            notify('Watch history cleared')

    def selected_tab(self):
        li = self.getControl(TABS_ID).getSelectedItem()
        return li.getProperty('key') if li else None

    @exclusive
    def onClick(self, control_id):
        if control_id == CLOSE_ID:
            self.close()
        elif control_id == SEARCH_ID:
            self.search()
        elif control_id == GEAR_ID:
            self.load_tab('settings')
        elif control_id == PROFILE_ID:
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
            li = self.getControl(control_id).getSelectedItem()
            if li is None:
                return
            kind = li.getProperty('t')
            if kind == 'lang':
                cat.state['live_lang'] = li.getProperty('lang')
                cat.save_state()
                self.load_tab('live')
            elif kind == 'setting':
                self.run_setting(li.getProperty('action'))
            elif activate(li):
                self.go_home()
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
        if focus == 0 and code not in ACTION_BACK:
            # Nothing is highlighted (this can happen when Stream opens by itself as Kodi starts): any key press
            # puts the highlight on the first row, or on the top bar if the rows are still loading.
            try:
                self.setFocusId(FIRST_ROW if self.getProperty('row0.title') else TABS_ID)
            except Exception:
                pass
            return
        if code in ACTION_BACK:
            # Back climbs one level at a time: rows -> tabs -> Home tab -> exit.
            if stop_if_playing_behind():
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
                if leave:
                    self.close()
        elif code == ACTION_CONTEXT and FIRST_ROW <= focus < FIRST_ROW + ROWS:
            li = self.getControl(focus).getSelectedItem()
            if li and li.getProperty('t') in ('play', 'folder'):
                BUSY['on'] = True
                try:
                    result = context_menu(li, False)
                finally:
                    BUSY['on'] = False
                if result == 'home':
                    self.go_home()
                elif result == 'changed' and self.tab in ('home', 'mylist'):
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
            window = Home('script-stream-home.xml', ADDON_PATH, 'Default', '1080i')
            xbmcgui.Window(10000).setProperty('script.stream.running', str(time.time()))
            audit.event('window_open')
            try:
                window.doModal()
            finally:
                xbmcgui.Window(10000).clearProperty('script.stream.running')
            audit.event('window_closed')
            again = getattr(window, 'switch', False)
            window.finish()
            del window
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
        window = Home('script-stream-home.xml', ADDON_PATH, 'Default', '1080i')
        xbmcgui.Window(10000).setProperty('script.stream.running', str(time.time()))
        audit.event('window_open')
        try:
            window.doModal()
        finally:
            # Only an open home screen blocks a second launch; a slow shutdown must not.
            xbmcgui.Window(10000).clearProperty('script.stream.running')
        audit.event('window_closed')
        again = getattr(window, 'switch', False)
        window.finish()
        audit.event('workers_stopped')
        del window
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
    if not sasta:
        xbmcgui.Dialog().ok('Stream', 'Install the Sasta TV addon and sign in to it first.')
    elif opened and time.time() - float(opened) < 12 * 3600:
        # A copy is already open (or still closing down): starting a second would double the memory in use.
        notify('Stream is already open')
    else:
        try:
            run()
        except Exception:
            audit.error('main')
