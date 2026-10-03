import os
import sys
import threading

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
FIRST_ROW = 200
PANEL = 50
ACTION_BACK = (9, 10, 92)  # parent dir, previous menu, nav back
ACTION_CONTEXT = 117

TABS = [('home', 'Home'), ('movies', 'Movies'), ('shows', 'Shows'), ('live', 'Live TV'), ('kids', 'Kids'),
        ('mylist', 'My List'), ('search', 'Search'), ('settings', 'Settings')]

# Set when the viewer chooses "Home" inside a nested screen; every open grid closes on seeing it.
NAV = {'home': False}


def clock(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return '{0}:{1:02d}:{2:02d}'.format(h, m, s) if h else '{0}:{1:02d}'.format(m, s)


def list_item(item, parent, group=None, meta=None, saved=False, suggestion=False):
    li = xbmcgui.ListItem(label=item['name'])
    if item.get('img'):
        li.setArt({'thumb': item['img']})
    li.setProperty('plot', item.get('plot', ''))
    if meta is None:
        meta = cat.title_case(group) if group and item['t'] == 'play' else ''
    li.setProperty('meta', meta)
    if item['t'] == 'play' and cat.is_live(item):
        li.setProperty('logo', '1')
    li.setProperty('t', item['t'])
    li.setProperty('url', item.get('url', ''))
    li.setProperty('i', str(item.get('i', 1)))
    li.setProperty('parent', parent or '')
    li.setProperty('group', group or '')
    li.setProperty('saved', '1' if saved else '')
    li.setProperty('suggestion', '1' if suggestion else '')
    return li


def entry_item(entry, **kwargs):
    return list_item(entry['item'], entry['parent'], entry.get('group'), saved=True, **kwargs)


def item_of(li):
    item = {'t': li.getProperty('t'), 'name': li.getLabel(), 'url': li.getProperty('url'),
            'i': int(li.getProperty('i') or 1)}
    if li.getArt('thumb'):
        item['img'] = li.getArt('thumb')
    if li.getProperty('plot'):
        item['plot'] = li.getProperty('plot')
    return item


def watch(name, group, kind, resume):
    """Seek to the resume point once playback starts, then record progress until it stops."""
    player, monitor = xbmc.Player(), xbmc.Monitor()
    if monitor.waitForAbort(1.5):
        return
    for _ in range(60):
        if player.isPlayingVideo():
            break
        if monitor.waitForAbort(0.5):
            return
    else:
        return
    if resume > 60:
        try:
            player.seekTime(resume)
        except Exception:
            pass
    position = duration = 0
    while player.isPlayingVideo():
        try:
            position, duration = player.getTime(), player.getTotalTime()
        except Exception:
            break
        db.update_progress(name, position, duration)
        if monitor.waitForAbort(5):
            return
    if duration > 0:
        percent = int(100 * position / duration)
        db.log_event('finished' if percent >= 90 else 'stopped', name, group, kind, 'watched {0}%'.format(percent))


def start(li):
    item = item_of(li)
    parent, group = li.getProperty('parent'), li.getProperty('group') or None
    live = cat.is_live(item)
    # Saved entries (history, My List, suggestions) can hold an expired link; take the current one.
    if li.getProperty('saved') and live:
        try:
            item = next((i for i in cat.get_dir(parent, fresh=True) if i['name'] == item['name'] and i['t'] == 'play'), item)
        except Exception:
            pass
    resume = 0
    position, duration = db.progress(item['name'])
    if not live and position > 60 and duration > 0 and position < duration * 0.95:
        choice = xbmcgui.Dialog().contextmenu(['Resume from ' + clock(position), 'Start from the beginning'])
        if choice < 0:
            return
        resume = position if choice == 0 else 0
    if not cat.play(parent, item, group):
        xbmcgui.Dialog().ok('Stream', 'This title can only be opened from the Sasta TV addon itself.')
        return
    kind = 'Live TV' if live else cat.section_of(parent)[0]
    db.log_event('played', item['name'], group, kind)
    xbmcgui.Dialog().notification('Stream', 'Starting ' + item['name'], xbmcgui.NOTIFICATION_INFO, 3000)
    if not live:
        threading.Thread(target=watch, args=(item['name'], group, kind, resume), daemon=True).start()


def activate(li):
    """Click handling shared by the home rows and the grid. Returns True when the viewer asked for Home."""
    kind = li.getProperty('t')
    if kind == 'folder':
        return open_grid(li.getLabel(), url=li.getProperty('url'), group=li.getProperty('group') or None)
    if kind == 'play':
        start(li)
    return False


def context_menu(li, in_grid):
    """Long-press / menu-key actions. Returns 'home', 'changed' or None."""
    kind = li.getProperty('t')
    if kind not in ('play', 'folder') or li.getLabel() == 'See all ›':
        return 'home' if in_grid and xbmcgui.Dialog().contextmenu(['Go to Home']) == 0 else None
    item, parent, group = item_of(li), li.getProperty('parent'), li.getProperty('group') or None
    listed = db.in_mylist(item['name'])
    options = [('open', 'Play' if kind == 'play' else 'Open'),
               ('list', 'Remove from My List' if listed else 'Add to My List')]
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
        db.log_event('listed' if added else 'unlisted', item['name'], group, cat.section_of(parent)[0])
        xbmcgui.Dialog().notification('Stream', 'Added to My List' if added else 'Removed from My List', xbmcgui.NOTIFICATION_INFO, 2500)
        return 'changed'
    if action == 'dismiss':
        db.dismiss(item['name'])
        db.log_event('dismissed', item['name'], group, cat.section_of(parent)[0])
        return 'changed'
    return 'home'


def open_grid(title, url=None, entries=None, group=None):
    """Show a full-screen grid. Returns True when the viewer chose Home, so callers can close too."""
    win = Grid('script-stream-grid.xml', ADDON_PATH, 'Default', '1080i')
    win.setup(title, url, entries, group)
    win.doModal()
    del win
    return NAV['home']


class Grid(xbmcgui.WindowXML):
    def setup(self, title, url, entries, group):
        self.title, self.url, self.entries, self.group = title, url, entries, group
        self.next_url = None
        self.loading = False
        self.ready = False

    def home_tile(self):
        return list_item({'t': 'home', 'name': '⌂ Home'}, '', meta='Back to the home screen')

    def onInit(self):
        if self.ready:
            return
        self.ready = True
        self.setProperty('title', self.title)
        self.panel = self.getControl(PANEL)
        if self.entries is not None:
            items = [self.home_tile()]
            items += [entry_item(e, meta=e.get('meta'), suggestion=bool(e.get('suggestion'))) for e in self.entries]
            self.panel.addItems(items)
            self.setProperty('status', '{0} results'.format(len(self.entries)) if self.entries else 'Nothing found')
            self.setFocusId(PANEL)
            if self.entries:
                self.panel.selectItem(1)
            return
        self.setProperty('status', 'Loading…')
        threading.Thread(target=self.load, args=(self.url,), daemon=True).start()

    def load(self, url):
        self.loading = True
        try:
            items = cat.get_dir(url)
        except Exception as e:
            self.setProperty('status', 'Could not load: {0}'.format(e))
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
        titles = cat.showable(items)
        shown += [list_item(i, url, self.group or self.title) for i in titles]
        self.panel.addItems(shown)
        self.setProperty('status', '' if titles or not first_page else 'Nothing here')
        if first_page:
            self.setFocusId(PANEL)
            if titles:
                self.panel.selectItem(len(shown) - len(titles))
        self.loading = False

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
        else:
            self.leave_if_home(activate(li))

    def onAction(self, action):
        if action.getId() in ACTION_BACK:
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
            if ai.recommend(cat.top_groups()) and self.tab == 'home' and self.getFocusId() in (TABS_ID, 0):
                self.load_tab('home')
        except Exception as e:
            cat.log('background refresh failed: {0}'.format(e))

    def go_home(self):
        NAV['home'] = False
        self.getControl(TABS_ID).selectItem(0)
        self.load_tab('home')

    def specs(self, key):
        """Rows for a tab: ('items', title, [ListItem]), ('dir', title, url, group, only) or ('expand', url, format, limit)."""
        u = cat.URLS
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
            return rows + [('dir', 'Live Events', u['fresh'], 'Live Events', 'play'),
                           ('expand', u['movies'], '{0} Movies', 4), ('expand', u['web_series'], '{0} Web Series', 2),
                           ('dir', 'English Movies', u['english_movies'], 'English', None), ('dir', 'Kids', u['kids'], 'Kids', None)]
        if key == 'movies':
            return [('expand', u['movies'], '{0} Movies', ROWS - 1), ('dir', 'English Movies', u['english_movies'], 'English', None)]
        if key == 'shows':
            return [('expand', u['web_series'], '{0} Web Series', 9), ('dir', 'English TV Shows', u['english_tv'], 'English', None),
                    ('expand', u['tv_shows'], '{0} TV Shows', 4)]
        if key == 'kids':
            return [('dir', 'Kids', u['kids'], 'Kids', None)]
        if key == 'mylist':
            saved = db.mylist()
            return [('items', 'My List', [entry_item(e) for e in saved])] if saved else []
        if key == 'live':
            langs = [f for f in cat.get_dir(u['live']) if f['t'] == 'folder']
            chosen = cat.state.get('live_lang')
            current = next((l for l in langs if l['name'] == chosen), langs[0] if langs else None)
            tiles = []
            for lang in langs:
                li = xbmcgui.ListItem(label=cat.title_case(lang['name']))
                li.setProperty('t', 'lang')
                li.setProperty('lang', lang['name'])
                tiles.append(li)
            out = [('items', 'Language', tiles)]
            if current:
                out.append(('expand', current['url'], cat.title_case(current['name']) + ' · {0}', ROWS - 1))
            return out
        return []

    def load_tab(self, key):
        self.tab = key
        self.token += 1
        for n in range(ROWS):
            self.setProperty('row{0}.title'.format(n), '')
            self.getControl(FIRST_ROW + n).reset()
        self.setProperty('status', 'Loading…')
        threading.Thread(target=self.fill, args=(key, self.token), daemon=True).start()

    def fill(self, key, token):
        slot = 0
        try:
            rows = []
            for spec in self.specs(key):
                if spec[0] == 'expand':
                    folders = cat.by_affinity([f for f in cat.get_dir(spec[1]) if f['t'] == 'folder'])
                    rows += [('dir', spec[2].format(cat.title_case(f['name'])), f['url'], f['name'], None) for f in folders[:spec[3]]]
                else:
                    rows.append(spec)
            for row in rows[:ROWS]:
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
                        items.append(list_item({'t': 'folder', 'name': 'See all ›', 'url': url}, url, group))
                if token != self.token:
                    return
                self.getControl(FIRST_ROW + slot).addItems(items)
                self.setProperty('row{0}.title'.format(slot), title)
                if slot == 0:
                    self.setProperty('status', '')
                    self.setFocusId(FIRST_ROW)
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
            entries = cat.rank_results(query, cat.search(query))
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
        finally:
            busy.close()
        if open_grid('Results for “{0}”'.format(query), entries=entries):
            self.go_home()

    def settings(self):
        last = db.meta('recs', {})
        engine = {'llm+jev': 'Claude + Jev', 'llm': 'Claude', 'jev': 'Jev', 'local': 'no AI key set'}.get(last.get('source'), 'not run yet')
        choice = xbmcgui.Dialog().select('Stream settings', [
            'AI keys and options', 'Refresh recommendations now (last: {0})'.format(engine),
            'Clear recommendations', 'Clear watch history'])
        if choice == 0:
            xbmcaddon.Addon('script.stream').openSettings()
        elif choice == 1:
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
            source = db.meta('recs', {}).get('source', 'local')
            xbmcgui.Dialog().ok('Stream', 'Recommendations updated ({0}).'.format(
                {'llm+jev': 'Claude + Jev', 'llm': 'Claude', 'jev': 'Jev'}.get(source, 'no AI key worked, used simple rules'))
                if made else 'Could not build recommendations. The catalogue index is empty or Kodi could not reach Sasta TV.')
            self.go_home()
        elif choice == 2:
            db.clear_suggestions()
            db.set_meta('recs', {})
            self.go_home()
        elif choice == 3 and xbmcgui.Dialog().yesno('Stream', 'Clear all watch history and resume points?'):
            db.clear_history()
            self.go_home()

    def onClick(self, control_id):
        if control_id == TABS_ID:
            key = self.getControl(TABS_ID).getSelectedItem().getProperty('key')
            if key == 'search':
                self.search()
            elif key == 'settings':
                self.settings()
            else:
                self.load_tab(key)
        elif FIRST_ROW <= control_id < FIRST_ROW + ROWS:
            li = self.getControl(control_id).getSelectedItem()
            if li.getProperty('t') == 'lang':
                cat.state['live_lang'] = li.getProperty('lang')
                cat.save_state()
                self.load_tab('live')
            elif activate(li):
                self.go_home()

    def onAction(self, action):
        focus = self.getFocusId()
        if action.getId() in ACTION_BACK:
            # Back climbs one level at a time: rows -> tabs -> Home tab -> exit.
            if focus != TABS_ID:
                self.setFocusId(TABS_ID)
            elif self.tab != 'home':
                self.go_home()
            else:
                self.close()
        elif action.getId() == ACTION_CONTEXT and FIRST_ROW <= focus < FIRST_ROW + ROWS:
            li = self.getControl(focus).getSelectedItem()
            if li and li.getProperty('t') != 'lang':
                result = context_menu(li, False)
                if result == 'home':
                    self.go_home()
                elif result == 'changed' and self.tab in ('home', 'mylist'):
                    self.load_tab(self.tab)


if __name__ == '__main__':
    try:
        cat.rpc('Addons.GetAddonDetails', {'addonid': 'plugin.video.sastatv'})
    except Exception:
        xbmcgui.Dialog().ok('Stream', 'Install the Sasta TV addon and sign in to it first.')
    else:
        window = Home('script-stream-home.xml', ADDON_PATH, 'Default', '1080i')
        window.doModal()
        del window
