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
import catalogue as cat  # noqa: E402
import db  # noqa: E402

ROWS = 14
ROW_LIMIT = 40
TABS_ID = 100
CLOSE_ID = 110
SEARCH_ID = 120
GEAR_ID = 130
TOP_BAR = (TABS_ID, CLOSE_ID, SEARCH_ID, GEAR_ID)
FIRST_ROW = 200
PANEL = 50
ACTION_LEFT, ACTION_RIGHT = 1, 2
ACTION_BACK = (9, 10, 92)  # parent dir, previous menu, nav back
ACTION_CONTEXT = 117
START_TIMEOUT = 45  # seconds to wait for the Sasta TV addon to deliver a stream

TABS = [('home', 'Home'), ('movies', 'Movies'), ('shows', 'Shows'), ('live', 'Live TV'), ('sports', 'Sports'),
        ('kids', 'Kids'), ('mylist', 'My List')]

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
PLAY = {'since': 0, 'cancel': False}

# Set when the viewer chooses "Home" inside a nested screen; every open grid closes on seeing it.
NAV = {'home': False}


def clock(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return '{0}:{1:02d}:{2:02d}'.format(h, m, s) if h else '{0}:{1:02d}'.format(m, s)


def notify(text, ms=3000):
    xbmcgui.Dialog().notification('Stream', text, xbmcgui.NOTIFICATION_INFO, ms)


def list_item(item, parent, group=None, meta=None, saved=False, suggestion=False, label=None, series=None):
    clean, tag, note = quality_of(item['name']) if item['t'] == 'play' else (item['name'], '', '')
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
    li.setProperty('t', item['t'])
    li.setProperty('url', item.get('url', ''))
    li.setProperty('i', str(item.get('i', 1)))
    li.setProperty('parent', parent or '')
    li.setProperty('group', group or '')
    li.setProperty('series', series or '')
    li.setProperty('saved', '1' if saved else '')
    li.setProperty('suggestion', '1' if suggestion else '')
    return li


def entry_item(entry, **kwargs):
    kwargs.setdefault('label', entry.get('label'))
    kwargs.setdefault('series', entry.get('series'))
    return list_item(entry['item'], entry['parent'], entry.get('group'), saved=True, **kwargs)


def tile(label, kind, **props):
    li = xbmcgui.ListItem(label=label)
    li.setProperty('t', kind)
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
    """After a cancel, keep stopping the stream for a while: a stop sent while the player is still opening is
    ignored, and the Sasta TV addon may deliver the stream a moment later."""
    player, monitor = xbmc.Player(), xbmc.Monitor()
    for _ in range(80):
        if not PLAY['cancel']:
            return  # something else was started on purpose
        if player.isPlaying():
            player.stop()
        if monitor.waitForAbort(0.5):
            return


def start(li):
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
    resume = 0
    position, duration, done = db.progress(key)
    if not live and not done and position > 60 and duration > 0:
        choice = xbmcgui.Dialog().contextmenu(['Resume from ' + clock(position), 'Start from the beginning'])
        if choice < 0:
            return
        resume = position if choice == 0 else 0
    player, monitor = xbmc.Player(), xbmc.Monitor()
    if player.isPlaying():
        player.stop()
        monitor.waitForAbort(1)
    if not cat.play(parent, item, group, series, label):
        xbmcgui.Dialog().ok('Stream', 'This title can only be opened from the Sasta TV addon itself.')
        return
    PLAY['since'], PLAY['cancel'] = time.time(), False
    kind = 'Live TV' if live else cat.section_of(parent)[0]
    db.log_event('played', label, group, kind)

    # Wait for the stream with a dialog the Back button can cancel.
    progress = xbmcgui.DialogProgress()
    progress.create('Stream', 'Starting {0}…\nPress Back to cancel.'.format(label))
    started = cancelled = False
    for step in range(START_TIMEOUT * 2):
        if progress.iscanceled():
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
        notify('Cancelled')
        return
    if not started:
        PLAY['cancel'], PLAY['since'] = True, 0
        threading.Thread(target=stop_late_arrival, daemon=True).start()
        notify('This stream did not start. Try another title or channel.', 5000)
        return
    if resume > 60:
        try:
            player.seekTime(resume)
        except Exception:
            pass
    threading.Thread(target=follow, args=(key, label, group, kind, live), daemon=True).start()


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
                                       'You can change the answers later under Settings.')

    def ask(title, options, saved_key):
        preset = [n for n, o in enumerate(options) if o in profile.get(saved_key, [])]
        picked = dialog.multiselect(title, options, preselect=preset)
        return None if picked is None else [options[n] for n in picked]

    languages = ask('1 of 4 · Which languages do you watch?', db.LANGUAGES, 'languages')
    if languages is None:
        if first_run:
            db.set_meta('profile', {'languages': [], 'genres': [], 'sports': []})
        return first_run
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
    win = Grid('script-stream-grid.xml', ADDON_PATH, 'Default', '1080i')
    win.setup(title, url, entries, group, trail)
    win.doModal()
    del win
    return NAV['home']


class Grid(xbmcgui.WindowXML):
    def setup(self, title, url, entries, group, trail):
        self.title, self.url, self.entries, self.group, self.trail = title, url, entries, group, tuple(trail)
        self.next_url = None
        self.loading = False
        self.ready = False

    def home_tile(self):
        return list_item({'t': 'home', 'name': '⌂ Home'}, '', meta='Back to the home screen')

    def onInit(self):
        if self.ready:
            return
        self.ready = True
        self.setProperty('title', ' · '.join(self.trail) if self.trail else self.title)
        self.panel = self.getControl(PANEL)
        if self.entries is not None:
            items = [self.home_tile()]
            items += [entry_item(e, meta=e.get('meta'), suggestion=bool(e.get('suggestion'))) for e in self.entries]
            self.panel.addItems(items)
            self.setProperty('status', '{0} titles'.format(len(self.entries)) if self.entries else 'Nothing here')
            self.setFocusId(PANEL)
            if self.entries:
                self.panel.selectItem(1)
            return
        self.setProperty('status', 'Loading…')
        threading.Thread(target=self.load, args=(self.url,), daemon=True).start()

    def title_items(self, url, titles):
        """Build tiles for a listing. Inside a show, episodes are put in order and marked with their progress."""
        series = self.trail[0] if self.trail else None
        episodes = bool(series) and bool(titles) and all(i['t'] == 'play' for i in titles)
        if episodes and not self.next_url:
            titles = sorted(titles, key=lambda i: cat.natural_key(i['name']))
        marks = db.progress_for([db.play_key(i) for i in titles]) if episodes else {}
        out, upnext = [], None
        for n, i in enumerate(titles):
            position, duration, done = marks.get(db.play_key(i), (0, 0, False))
            meta = None
            if done:
                meta = '✓ Watched'
            elif position > 60:
                meta = 'Resume from ' + clock(position)
            if episodes and not done and upnext is None:
                upnext = n
            label = ('✓ ' + i['name']) if done else None
            out.append(list_item(i, url, self.group or self.title, meta=meta, label=label, series=series if episodes else None))
        return out, upnext

    def load(self, url):
        self.loading = True
        try:
            items = cat.get_dir(url)
        except Exception as e:
            self.setProperty('status', 'Could not load: {0}'.format(e))
            if self.panel.size() == 0:
                self.panel.addItems([self.home_tile()])
                self.setFocusId(PANEL)
            self.loading = False
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
        first_title = len(shown)
        shown += titles
        self.panel.addItems(shown)
        self.setProperty('status', '' if titles or not first_page else 'Nothing here')
        if first_page:
            self.setFocusId(PANEL)
            if titles:
                # Land on the next episode to watch, or on the first title.
                self.panel.selectItem(first_title + (upnext or 0))
                if upnext:
                    self.setProperty('status', 'Up next: ' + titles[upnext].getProperty('name'))
        self.loading = False

    def refresh_marks(self):
        """After playback, redraw an episode list so watched ticks and the next episode are current."""
        if not self.trail or self.entries is not None or self.next_url or self.loading:
            return
        try:
            items = cat.get_dir(self.url)
        except Exception:
            return
        titles, upnext = self.title_items(self.url, cat.showable(items))
        if not titles or not all(t.getProperty('t') == 'play' for t in titles):
            return
        self.panel.reset()
        self.panel.addItems([self.home_tile()] + titles)
        self.panel.selectItem(1 + (upnext or 0))
        if upnext:
            self.setProperty('status', 'Up next: ' + titles[upnext].getProperty('name'))

    def leave_if_home(self, go_home):
        if go_home:
            self.close()

    def onClick(self, control_id):
        if control_id != PANEL:
            return
        li = self.panel.getSelectedItem()
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
            threading.Thread(target=self.after_playback, daemon=True).start()
        else:
            self.leave_if_home(activate(li, self.trail))

    def after_playback(self):
        player, monitor = xbmc.Player(), xbmc.Monitor()
        if monitor.waitForAbort(3):
            return
        while player.isPlayingVideo():
            if monitor.waitForAbort(1):
                return
        self.refresh_marks()

    def onAction(self, action):
        if action.getId() in ACTION_BACK:
            if not stop_if_playing_behind():
                self.close()
            return
        if not self.ready:
            return
        if action.getId() == ACTION_CONTEXT:
            li = self.panel.getSelectedItem()
            if li and context_menu(li, True) == 'home':
                NAV['home'] = True
                self.close()
            return
        # Fetch the next page shortly before the end of the list is reached.
        if self.next_url and not self.loading and self.panel.getSelectedPosition() >= self.panel.size() - 14:
            url, self.next_url = self.next_url, None
            threading.Thread(target=self.load, args=(url,), daemon=True).start()


class Home(xbmcgui.WindowXML):
    def onInit(self):
        if getattr(self, 'ready', False):
            return
        self.ready = True
        self.token = 0
        self.touched = False
        tabs = []
        for key, label in TABS:
            li = xbmcgui.ListItem(label=label)
            li.setProperty('key', key)
            tabs.append(li)
        self.getControl(TABS_ID).addItems(tabs)
        self.load_tab('home')
        threading.Thread(target=self.background, daemon=True).start()

    def background(self):
        """Keep the local index and the suggestions fresh without holding up the screen."""
        try:
            cat.warm_index()
            # Show the new row straight away only if the viewer has not started navigating; otherwise it appears next time Home loads.
            if ai.recommend(cat.top_groups()) and not self.touched and self.tab == 'home':
                self.load_tab('home')
        except Exception as e:
            cat.log('background refresh failed: {0}'.format(e))

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
            tile('AI keys and options', 'setting', action='keys',
                 plot='Enter an Amazon Bedrock key (Claude) and an optional Jev key. Stream works without them.'),
            tile('Refresh recommendations', 'setting', action='refresh', meta='Last built with: ' + engine,
                 plot='Build a new Recommended for You row now.'),
            tile('Clear recommendations', 'setting', action='clear_recs', plot='Remove the saved recommendations.'),
            tile('Clear watch history', 'setting', action='clear_history',
                 plot='Forget everything played, including resume points and watched episodes.'),
        ]

    def specs(self, key):
        """Rows for a tab: ('items', title, [ListItem]) or ('dir', title, url, group, only)."""
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
        self.tab = key
        self.token += 1
        for n in range(ROWS):
            self.setProperty('row{0}.title'.format(n), '')
            self.getControl(FIRST_ROW + n).reset()
        self.setProperty('status', 'Loading…')
        threading.Thread(target=self.fill, args=(key, self.token, focus), daemon=True).start()

    def focus_rows(self, token):
        """Move focus into the first row. The row only becomes focusable a frame after its title is set, so retry."""
        for _ in range(15):
            if token != self.token:
                return
            self.setFocusId(FIRST_ROW)
            xbmc.sleep(100)
            if self.getFocusId() == FIRST_ROW:
                return

    def fill(self, key, token, focus):
        slot = 0
        try:
            for row in self.specs(key)[:ROWS]:
                if token != self.token:
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
                if token != self.token:
                    return
                self.getControl(FIRST_ROW + slot).addItems(items)
                self.setProperty('row{0}.title'.format(slot), title)
                if slot == 0:
                    self.setProperty('status', '')
                    # Only take focus when the viewer opened the tab, never while they are moving along the tabs.
                    if focus:
                        self.focus_rows(token)
                slot += 1
        except Exception as e:
            cat.log('tab failed: {0}'.format(e))
        if slot == 0 and token == self.token:
            empty = {'mylist': 'My List is empty. Hold OK on any title (or press the menu key) and choose Add to My List.'}
            self.setProperty('status', empty.get(key, 'Nothing to show. Check that the Sasta TV addon opens and is signed in.'))

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
        if action == 'profile':
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

    def onClick(self, control_id):
        if control_id == CLOSE_ID:
            self.close()
        elif control_id == SEARCH_ID:
            self.search()
        elif control_id == GEAR_ID:
            self.load_tab('settings')
        elif control_id == TABS_ID:
            key = self.selected_tab()
            if key == self.tab and self.getProperty('row0.title'):
                self.focus_rows(self.token)  # already showing: OK just moves into the rows
            else:
                self.load_tab(key)
        elif FIRST_ROW <= control_id < FIRST_ROW + ROWS:
            li = self.getControl(control_id).getSelectedItem()
            kind = li.getProperty('t')
            if kind == 'lang':
                cat.state['live_lang'] = li.getProperty('lang')
                cat.save_state()
                self.load_tab('live')
            elif kind == 'setting':
                self.run_setting(li.getProperty('action'))
            elif activate(li):
                self.go_home()

    def onAction(self, action):
        focus = self.getFocusId()
        code = action.getId()
        self.touched = True
        if code in ACTION_BACK:
            # Back climbs one level at a time: rows -> tabs -> Home tab -> exit.
            if stop_if_playing_behind():
                return
            if focus not in TOP_BAR:
                self.setFocusId(TABS_ID)
            elif self.tab != 'home':
                self.go_home(focus=False)
            elif xbmcgui.Dialog().yesno('Stream', 'Exit Stream?'):
                self.close()
        elif code == ACTION_CONTEXT and FIRST_ROW <= focus < FIRST_ROW + ROWS:
            li = self.getControl(focus).getSelectedItem()
            if li and li.getProperty('t') in ('play', 'folder'):
                result = context_menu(li, False)
                if result == 'home':
                    self.go_home()
                elif result == 'changed' and self.tab in ('home', 'mylist'):
                    self.load_tab(self.tab)
        elif focus == TABS_ID and code in (ACTION_LEFT, ACTION_RIGHT):
            # Moving along the tabs shows that tab straight away; focus stays on the tabs.
            key = self.selected_tab()
            if key and key != self.tab:
                self.load_tab(key, focus=False)


if __name__ == '__main__':
    try:
        cat.rpc('Addons.GetAddonDetails', {'addonid': 'plugin.video.sastatv'})
    except Exception:
        xbmcgui.Dialog().ok('Stream', 'Install the Sasta TV addon and sign in to it first.')
    else:
        if db.meta('profile') is None:
            onboarding(True)
        window = Home('script-stream-home.xml', ADDON_PATH, 'Default', '1080i')
        window.doModal()
        del window
