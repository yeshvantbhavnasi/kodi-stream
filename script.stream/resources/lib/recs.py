# The recommendation engine: rows for the home screen, scored over the whole local index on the box itself.
#
# Every indexed title that has not been played, rated or dismissed gets a score from what the household has told
# Stream and what it does: the languages chosen at setup (first language leading), the genres chosen and watched,
# favourite actors and directors, TMDb's rating, India's streaming charts, how new the title is, and what was
# disliked. The rows are then cut from that one ranking: Top Picks (where a model, when one is set up, re-ranks the
# best candidates and explains them), Because you watched, genre rows per language, New Releases, Highly Rated,
# shows to binge, what is on each streaming service, and hidden gems. Rows are rebuilt in the background and kept
# for an hour, so Home opens at once.
import hashlib
import json
import re
import threading
import time

import ai
import catalogue as cat
import db
import discover
import library
import meta

ROW_SIZE = 24          # tiles per row on Home
KEEP_ROWS = 3600       # rebuild the rows after this long, or sooner when something was played or rated
MIN_ROW = 6            # a row needs at least this many titles to be worth showing
LANG_WEIGHT = (3.0, 2.2, 1.6, 1.0)
PLURAL = {'Action': 'Action', 'Comedy': 'Comedies', 'Thriller & Crime': 'Thrillers', 'Romance': 'Romances', 'Drama': 'Dramas',
          'Horror': 'Horror', 'Family': 'Family Favourites', 'Sci-fi & Fantasy': 'Sci-fi & Fantasy',
          'Devotional & Mythology': 'Devotional & Mythology', 'Reality & Talk': 'Reality & Talk Shows'}
# Streaming services worth a row of their own, as TMDb names them in India.
PROVIDERS = ['Netflix', 'Amazon Prime Video', 'Aha', 'ZEE5', 'Sony Liv', 'JioHotstar', 'Hotstar', 'Sun Nxt', 'ETV Win', 'Apple TV+', 'Lionsgate Play']
PROVIDER_LABEL = {'Amazon Prime Video': 'Prime Video', 'Sony Liv': 'SonyLIV', 'Hotstar': 'JioHotstar'}
KEYWORDS = {label: words for label, _, words in discover.GENRES}
TMDB_TO_GENRE = discover.TMDB_TO_GENRE
STATE = {'rows': None, 'built': 0, 'plays': -1, 'building': False}


def log(msg):
    cat.log('recs: ' + msg)


# ---------- what the household likes ----------

def _genre_labels(entry, data):
    """The browsing genres of a title: from its looked-up TMDb genres, otherwise from words in its description."""
    labels = {TMDB_TO_GENRE[g] for g in (data.get('genres') or []) if g in TMDB_TO_GENRE}
    if labels:
        return labels
    text = (entry['item'].get('plot') or '').lower()
    return {label for label, words in KEYWORDS.items() if any(w in text for w in words)}


def taste(metadata):
    """Weights for genres, people and languages from the setup answers and the watch history."""
    profile = db.meta('profile') or {}
    genres, people, bad_genres, bad_people = {}, {}, {}, set()
    for g in profile.get('genres', []):
        genres[g] = genres.get(g, 0) + 3.0
    for name in profile.get('people', []):
        people[name] = people.get(name, 0) + 3.0
    recent = db.recent(40)
    liked, disliked = db.rated(1), db.rated(-1)
    for weight, entries in ((1.0, recent), (2.0, liked)):
        for e in entries:
            data = metadata.get(e.get('series') or e['item']['name']) or {}
            for label in _genre_labels(e, data):
                genres[label] = genres.get(label, 0) + weight
            for name in (data.get('cast') or [])[:4] + (data.get('directors') or [])[:1]:
                people[name] = people.get(name, 0) + weight
    for e in disliked:
        data = metadata.get(e['item']['name']) or {}
        for label in _genre_labels(e, data):
            bad_genres[label] = bad_genres.get(label, 0) + 1.0
        bad_people.update((data.get('directors') or [])[:1])
    order = db.chosen_languages()
    charts = {}
    try:
        for n, title in enumerate(ai.charts()[:40]):
            charts[discover._plain(title.get('title', ''))] = n + 1
    except Exception:
        pass
    return {'genres': genres, 'people': people, 'bad_genres': bad_genres, 'bad_people': bad_people,
            'languages': order, 'charts': charts, 'liked_groups': {(e.get('group') or '').lower() for e in liked}}


def lang_rank(entry, order):
    group = (entry.get('group') or '').lower()
    for n, lang in enumerate(order):
        if db.lang_match(group, [lang]):
            return n
    return len(order)


# ---------- scoring ----------

def score(entry, data, t, now):
    """How well one unplayed title fits the household right now. Higher is better."""
    name = entry['item']['name']
    rank = lang_rank(entry, t['languages'])
    s = LANG_WEIGHT[min(rank, len(LANG_WEIGHT) - 1)] if t['languages'] else 1.0
    labels = _genre_labels(entry, data)
    s += sum(min(t['genres'].get(label, 0), 4.0) for label in labels) * 0.6
    s -= sum(min(t['bad_genres'].get(label, 0), 2.0) for label in labels) * 0.8
    folk = (data.get('cast') or [])[:4] + (data.get('directors') or [])[:1]
    s += min(sum(1.0 for n in folk if n in t['people']), 3.0) * 1.2
    if any(n in t['bad_people'] for n in folk):
        s -= 3.0
    rating, votes = data.get('tmdb_rating') or 0, data.get('votes') or 0
    if data.get('imdb_rating'):
        try:
            rating, votes = float(data['imdb_rating']), max(votes, 50)
        except ValueError:
            pass
    if rating and votes >= 10:
        s += max(-1.5, min(2.5, (rating - 6.0) * 0.8))
    plain = discover._plain(meta.split_title(name)[0])
    if plain in t['charts']:
        s += 2.5 - min(t['charts'][plain], 20) * 0.05
    age = (now - (entry.get('first_seen') or now)) / 86400.0
    s += 1.5 if age < 7 else 1.0 if age < 30 else 0.5 if age < 90 else 0.0
    year = data.get('year') or meta.split_title(name)[1]
    if year and year.isdigit() and int(year) < time.localtime(now).tm_year - 10:
        s -= 0.3
    if (entry.get('group') or '').lower() in t['liked_groups']:
        s += 0.3
    # a little deterministic jitter, so equal titles do not always sit in the same order
    s += int(hashlib.md5(name.encode('utf-8')).hexdigest()[:4], 16) / 65535.0 * 0.3
    return s


def excluded_names():
    """Catalogue names of the titles a posted list asked to leave out of every row on this box."""
    out = set()
    for posted in ai.lists():
        for title in posted.get('exclude') or []:
            entry = discover.match_title(title)
            if entry:
                out.add(entry['item']['name'])
            out.add(title)
    return out


def _same_film(name):
    """One key for every copy of a film: 'Nishaanchi (2025)' in Telugu, Tamil and Hindi, clean or cinema recording."""
    return discover.same_film(name)


# A row whose title names a language already holds that language. Mixed rows prefer Telugu instead.
_LANG_ROW = ('telugu', 'hindi', 'tamil', 'malayalam', 'kannada', 'marathi', 'gujarati', 'punjabi',
             'bengali', 'bangla', 'urdu', 'english', 'dubbed')


def _mixed_row(title):
    """True for a row that is not already about one language, and not a live sports row."""
    low = (title or '').lower()
    return not low.startswith('cricket') and not any(word in low for word in _LANG_ROW)


def _film_index():
    """{film key: [catalogue entries]} for choosing a Telugu copy when one exists."""
    index = {}
    for entry in db.all_titles():
        key = _same_film(entry['item']['name'])
        if key:
            index.setdefault(key, []).append(entry)
    return index


def _show_entry(entry):
    return entry.get('kind') in discover.SHOW_KINDS or (entry.get('item') or {}).get('t') == 'folder'


def _telugu_copies(entries, index):
    """Swap in the Telugu catalogue copy of a film when one exists. Anything without a Telugu copy stays as it is."""
    out = []
    for entry in entries:
        item = entry.get('item') or {}
        if cat.is_live(item) or 'youtube' in item.get('url', '') or db.lang_match(entry.get('group') or '', ['telugu']):
            out.append(entry)
            continue
        versions = [v for v in index.get(_same_film(item.get('name') or ''), [])
                    if _show_entry(v) == _show_entry(entry) and db.lang_match(v.get('group') or '', ['telugu'])]
        if not versions:
            out.append(entry)
            continue
        versions.sort(key=lambda v: meta.is_cinema_copy(v['item']['name']))
        chosen = versions[0]
        swapped = dict(entry, item=chosen['item'], parent=chosen['parent'], group=chosen.get('group'),
                       kind=chosen.get('kind') or entry.get('kind'))
        old = cat.title_case(entry.get('group') or '')
        if swapped.get('meta') and old and swapped['meta'].lower() == old.lower():
            swapped['meta'] = cat.title_case(chosen.get('group') or '')
        out.append(swapped)
    return out


def _no_repeats(entries, prefer_telugu=True):
    """A row shows a film once. Telugu wins when that copy is in the row; otherwise the first copy stays.
    A clean copy beats a cinema recording of the same film."""
    best, order = {}, []
    for entry in entries:
        key = _same_film(entry['item']['name']) or ('\0' + (entry['item'].get('url') or entry['item']['name']))
        if key not in best:
            order.append(key)
            best[key] = entry
            continue
        if _better_copy(entry, best[key], prefer_telugu):
            kept, dropped = entry, best[key]
        else:
            kept, dropped = best[key], entry
        merged = dict(kept)
        if dropped.get('suggestion'):
            merged['suggestion'] = True
        for field in ('reason', 'number', 'rank'):
            if not merged.get(field) and dropped.get(field):
                merged[field] = dropped[field]
        best[key] = merged
    return [best[key] for key in order]


def _better_copy(new, old, prefer_telugu):
    """Telugu replaces another language when that copy is wanted. With no Telugu copy, the one already kept stays,
    except a clean copy replaces a cinema recording of the same language."""
    new_group, old_group = new.get('group') or '', old.get('group') or ''
    if prefer_telugu and db.lang_match(new_group, ['telugu']) and not db.lang_match(old_group, ['telugu']):
        return True
    same = new_group.strip().lower() == old_group.strip().lower()
    return same and meta.is_cinema_copy(old['item']['name']) and not meta.is_cinema_copy(new['item']['name'])


def candidates():
    """Every unplayed, unrated, undismissed indexed title in the viewer's languages, each with its details and score,
    best first. Also returns the taste that scored them and the metadata map."""
    metadata = db.metadata_all()
    t = taste(metadata)
    skip = db.watched_names() | db.rated_names() | db.dismissed_names() | excluded_names()
    now = time.time()
    pool = []
    for entry in db.all_titles():
        name = entry['item']['name']
        if name in skip:
            continue
        data = metadata.get(name) or {}
        pool.append((score(entry, data, t, now), entry, data))
    if t['languages']:
        chosen = [p for p in pool if lang_rank(p[1], t['languages']) < len(t['languages'])]
        pool = chosen or pool
    # A film listed twice, as a clean copy and a cinema recording, is offered once: the clean copy.
    clean = {discover._plain(meta.split_title(e['item']['name'])[0]) + (e.get('group') or '').lower()
             for _, e, _ in pool if not meta.is_cinema_copy(e['item']['name'])}
    pool = [p for p in pool if not (meta.is_cinema_copy(p[1]['item']['name'])
                                    and discover._plain(meta.split_title(p[1]['item']['name'])[0]) + (p[1].get('group') or '').lower() in clean)]
    pool.sort(key=lambda p: -p[0])
    return pool, t, metadata


# ---------- rows ----------

def _balanced(scored, order, size):
    """A cut of the ranking with the household's language shares: about half from the first language, a third
    from the second, the rest from the others, each part in score order."""
    if not order:
        return [e for _, e, _ in scored[:size]]
    shares = [size // 2, size * 3 // 10] + [size // 5] * max(0, len(order) - 2)
    out, taken = [], set()
    for n in range(len(order)):
        mine = [p for p in scored if lang_rank(p[1], order) == n][:shares[n]]
        for _, e, _ in mine:
            out.append(e)
            taken.add(e['item']['name'])
    for _, e, _ in scored:
        if len(out) >= size:
            break
        if e['item']['name'] not in taken:
            out.append(e)
            taken.add(e['item']['name'])
    return out[:size]


def _with_meta(entries, text=None):
    return [dict(e, meta=text(e) if text else cat.title_case(e.get('group') or '')) for e in entries]


def _lang_label(group):
    return cat.title_case(group or '')


def posted_picks():
    """Titles from the lists posted to the service, newest list first, each with its reason and who posted it:
    they lead Top Picks whenever there are any. Returns ([entries], by)."""
    out, seen, by = [], set(), ''
    skip = db.watched_names() | db.dismissed_names() | excluded_names()
    for posted in ai.lists():
        who = posted.get('by') or ''
        for e in discover.list_entries(posted.get('items') or [], ROW_SIZE):
            name = e['item']['name']
            if name in seen or name in skip:
                continue
            seen.add(name)
            by = by or who
            out.append(dict(e, meta=e.get('reason') or (('Picked by ' + who) if who else _lang_label(e.get('group'))), suggestion=True,
                            number=len(out) + 1))
    return out[:ROW_SIZE], by


def lists_stamp():
    """When the newest posted list was posted; the rows are rebuilt when it changes."""
    return max([l.get('at', 0) for l in ai.lists()] or [0])


def resolve_lists():
    """Search the catalogue for posted titles the local index lacks (once a week per title), in the background, so
    the next rebuild of the rows can show them. True when something new was found."""
    found = 0
    for posted in ai.lists():
        for item in posted.get('items') or []:
            title = item.get('title') or ''
            if not title or discover.match_title(title, item.get('year') or '', item.get('kind') or '', item.get('lang') or ''):
                continue
            if discover.find_outside(title, item.get('year') or '', item.get('kind') or '', item.get('lang') or ''):
                found += 1
    if found:
        log('{0} posted titles found by searching the catalogue'.format(found))
    return found > 0


def top_picks(scored, order):
    """The titles posted to the service lead (with their reasons), then the model's picks with its reasons (when a
    model has run), then the best of the local ranking."""
    out, _ = posted_picks()
    names = {e['item']['name'] for e in out}
    picks = [p for p in db.suggestions() if p['item']['name'] not in names]
    names |= {p['item']['name'] for p in picks}
    out += [dict(p, meta=p['reason'] or _lang_label(p.get('group')), suggestion=True) for p in picks]
    rest = [p for p in scored if p[1]['item']['name'] not in names]
    # Mostly movies: shows take at most a quarter of what the local ranking adds.
    room = ROW_SIZE - len(out)
    shows = 0
    for e in _balanced(rest, order, room * 2):
        if len(out) >= ROW_SIZE:
            break
        if e.get('kind') in discover.SHOW_KINDS:
            if shows >= room // 4:
                continue
            shows += 1
        out.append(dict(e, meta=_lang_label(e.get('group')), suggestion=True))
    return out


def genre_rows(scored, t, limit=4):
    """'Telugu Thrillers', 'Hindi Comedies': the genres this household rates highest, in its first two languages."""
    rows = []
    genres = [g for g, _ in sorted(t['genres'].items(), key=lambda p: -p[1]) if g in PLURAL]
    langs = t['languages'][:2] or ['']
    for label in genres:
        for lang in langs:
            if len(rows) >= limit:
                return rows
            found = [e for s, e, d in scored if label in _genre_labels(e, d) and (not lang or db.lang_match(e.get('group') or '', [lang]))]
            if len(found) >= MIN_ROW:
                title = '{0} {1}'.format(cat.title_case(lang), PLURAL[label]).strip() if lang else PLURAL[label]
                rows.append((title, _with_meta(found[:ROW_SIZE])))
    return rows


def rated_rows(scored, t):
    rows = []
    for lang in t['languages'][:2] or ['']:
        found = [(d.get('tmdb_rating') or 0, e) for s, e, d in scored
                 if (d.get('tmdb_rating') or 0) >= 7.3 and (d.get('votes') or 0) >= 20 and (not lang or db.lang_match(e.get('group') or '', [lang]))]
        if len(found) >= MIN_ROW:
            found.sort(key=lambda p: -p[0])
            rows.append(('Highly Rated' + (' in ' + cat.title_case(lang) if lang else ''),
                         _with_meta([e for _, e in found[:ROW_SIZE]], lambda e: meta.rating_text(e.get('data', {})) or _lang_label(e.get('group')))))
            break
    return rows


BIGG_BOSS_TELUGU = {
    'item': {
        't': 'folder',
        'name': 'Bigg Boss',
        'url': ('http://sastatv.com/secured/php/fetchXclusiveS3.php?bkt=ZGVzaXR2c2hvd3M='
                '&order=recent&sdirurl=VGVsdWd1L1NUQVIlMjBNQUEvL0JpZ2clMjBCb3NzLw=='
                '&thumb=aHR0cDovL3NtYXJ0NGsubWUvc3RhbGtlcl9wb3J0YWwvc2NyZWVuc2hvdHMvMjIzNi8yMjM1NjEuanBlZw=='),
        'img': 'http://smart4k.me/stalker_portal/screenshots/2236/223561.jpeg',
    },
    'parent': ('http://sastatv.com/secured/php/fetchXclusiveS3.php?bkt=ZGVzaXR2c2hvd3M='
               '&order=recent&sdirurl=VGVsdWd1L1NUQVIlMjBNQUEv&lang=VGVsdWd1&thumb='),
    'group': 'Telugu',
    'kind': 'TV Shows',
    'meta': 'Telugu · Season 10',
}


def show_row(scored, order):
    picked = _with_meta(_balanced([p for p in scored if p[1].get('kind') in discover.SHOW_KINDS], order, ROW_SIZE))
    if not db.KIDS and db.lang_match('telugu'):
        bb = dict(BIGG_BOSS_TELUGU, item=dict(BIGG_BOSS_TELUGU['item']))
        picked = [bb] + [e for e in picked if _same_film((e.get('item') or {}).get('name') or '') != _same_film('Bigg Boss')]
        picked = picked[:ROW_SIZE]
    if len(picked) >= MIN_ROW:
        return [('Shows to Binge', picked)]
    return []


def provider_rows(scored, limit=3):
    """'On Netflix', 'On Aha': where titles originally stream, from TMDb's providers for India."""
    by = {}
    for s, e, d in scored:
        for p in d.get('providers') or []:
            if p in PROVIDERS:
                by.setdefault(p, []).append(e)
    rows = []
    for p, found in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if len(found) >= MIN_ROW and len(rows) < limit:
            rows.append(('On ' + PROVIDER_LABEL.get(p, p), _with_meta(found[:ROW_SIZE])))
    return rows


def gems(scored, now):
    found = [e for s, e, d in scored if (d.get('tmdb_rating') or 0) >= 7.0 and 5 <= (d.get('votes') or 0) <= 300
             and (now - (e.get('first_seen') or now)) > 60 * 86400]
    return [('Hidden Gems', _with_meta(found[:ROW_SIZE]))] if len(found) >= MIN_ROW else []


def because_rows(scored, limit=2):
    rows = []
    ranked = {p[1]['item']['name']: n for n, p in enumerate(scored)}
    for name, show, lang in db.recent_watched(4):
        if len(rows) >= limit:
            break
        alike = discover.similar_entries(name, show, lang, ROW_SIZE)
        # a bot's posted picks keep their order at the front; the rest follow the local ranking
        posted = [e for e in alike if e.get('reason')]
        rest = sorted([e for e in alike if not e.get('reason')], key=lambda e: ranked.get(e['item']['name'], len(ranked)))
        alike = posted + rest
        if len(alike) >= 4:
            short = meta.split_title(name)[0] or name
            rows.append(('Because you watched ' + (short if len(short) <= 32 else short[:31].rstrip() + '…'),
                         _with_meta(alike[:ROW_SIZE], lambda e: e.get('reason') or _lang_label(e.get('group')))))
    return rows


def list_rows(limit=3):
    """One row per list posted to the service, for a layout that wants them apart from Top Picks (Home folds them
    into Top Picks instead): the titles the catalogue can play, in the list's order, with the reason for each."""
    rows = []
    skip = db.watched_names() | db.dismissed_names()
    for posted in ai.lists()[:limit]:
        found = [e for e in discover.list_entries(posted.get('items') or [], ROW_SIZE) if e['item']['name'] not in skip]
        if len(found) >= 3:
            by = posted.get('by') or ''
            title = posted.get('title') or ('Picked by ' + by if by else 'Picked for You')
            rows.append((title, [dict(e, meta=e.get('reason') or (('Picked by ' + by) if by else _lang_label(e.get('group'))), suggestion=True) for e in found]))
    return rows


def build():
    """Curated recommendation rows for Home, best first: [(title, [entries])]. Kept lean (7-8 distinct sections)
    so Home stays scannable and uncluttered; full genre, provider, sports and YouTube browsing live on their tabs."""
    started = time.time()
    scored, t, metadata = candidates()
    for s, e, d in scored:
        e['data'] = {'tmdb_rating': d.get('tmdb_rating'), 'votes': d.get('votes'), 'imdb_rating': d.get('imdb_rating')}
    order = t['languages']
    rows = []
    picks = top_picks(scored, order)
    lead_keys = {_same_film(e['item']['name']) for e in picks[:12]}
    if picks:
        _, by = posted_picks()
        rows.append(('Top Picks for You' + ('  \u00b7  ' + by if by else ai.engine_note()), picks))
    fresh = discover.new_entries(ROW_SIZE * 2)
    if len(fresh) >= 4:
        # Show fresh arrivals that aren't already leading Top Picks first so the viewer sees new posters right away.
        fresh_first = [e for e in fresh if _same_film(e['item']['name']) not in lead_keys]
        fresh_rest = [e for e in fresh if _same_film(e['item']['name']) in lead_keys]
        rows.append(('New Releases', _with_meta((fresh_first + fresh_rest)[:ROW_SIZE])))
    trending = discover.india_top_combined(10)
    if trending:
        rows.append(('Top 10 Trending', [dict(e, meta=e['meta']) for e in trending]))
    else:
        home = discover.top_entries()
        if len(home) >= 3:
            rows.append(('Top 10 in Your Home', [dict(e, meta=e['meta']) for e in home]))
    rows += because_rows(scored, limit=1)
    rows += show_row(scored, order)
    taste_rows = genre_rows(scored, t, limit=1) or rated_rows(scored, t)
    rows += taste_rows
    try:
        cricket = cricket_rows()
    except Exception as e:
        log('cricket row failed: {0}'.format(e))
        cricket = []
    if cricket and any(e.get('soon') for e in cricket[0][1]):
        rows += cricket  # placed below all Movie & Show rows so poster rows stay cleanly grouped
    try:
        rows += video_rows()
    except Exception as e:
        log('video rows failed: {0}'.format(e))
    index = _film_index()
    # Mixed rows (Top Picks, Because you watched, New Releases, and the rest) show the Telugu copy of a film
    # when the catalogue has one, and the copy already chosen when it does not. A row named for a language stays in it.
    rows = [(title, _no_repeats(_telugu_copies(entries, index) if _mixed_row(title) else entries, _mixed_row(title)))
            for title, entries in rows]
    for _, entries in rows:
        for e in entries:
            e.pop('data', None)
            e.pop('source', None)
    log('{0} rows from {1} candidates in {2:.1f}s'.format(len(rows), len(scored), time.time() - started))
    return rows


def pool_for_model(limit=120):
    """The best candidates for the model to re-rank, language shares applied. A film the catalogue also has in
    Telugu is offered as that copy; with no Telugu copy the candidate already chosen is kept."""
    scored, t, _ = candidates()
    return _no_repeats(_telugu_copies(_balanced(scored, t['languages'], limit), _film_index()))


# ---------- keeping the rows ready ----------

def stale():
    return (STATE['rows'] is None or time.time() - STATE['built'] > KEEP_ROWS or STATE['plays'] != db.history_count()
            or db.meta('rows_dirty', False) or STATE.get('lists_at') != lists_stamp())


def rows(force=False, fetch=False):
    """The rows for Home: the kept ones when they are fresh, else rebuilt now. fetch asks the service for the posted
    lists first (every visit to Home does), and a list not seen before has its missing titles searched for in the
    background, after which the rows are rebuilt once more."""
    if fetch:
        try:
            ai.lists(fresh=True)
        except Exception as e:
            log('lists not refreshed: {0}'.format(e))
        force = True
    if force or stale():
        before = STATE.get('lists_at')
        try:
            built = build()
        except Exception as e:
            log('build failed: {0}'.format(e))
            built = STATE['rows'] or []
        STATE.update(rows=built, built=time.time(), plays=db.history_count(), lists_at=lists_stamp())
        db.set_meta('rows_dirty', False)
        if STATE['lists_at'] and STATE['lists_at'] != before and not STATE.get('resolving'):
            threading.Thread(target=_resolve_later, daemon=True).start()
        threading.Thread(target=_share_later, args=(built,), daemon=True).start()
    return STATE['rows'] or []


# ---------- cricket: live and upcoming matches, on the channels and events the catalogue carries ----------

CRICKET_HOURS = 36


def _when(start):
    """'Today 7:00 PM', 'Tomorrow 9:30 AM', 'Thu 2:00 PM' in the box's own time."""
    local = time.localtime(start)
    today = time.localtime()
    day = local.tm_yday - today.tm_yday + (local.tm_year - today.tm_year) * 365
    label = 'Today' if day == 0 else 'Tomorrow' if day == 1 else time.strftime('%a', local)
    return label + time.strftime(' %I:%M %p', local).replace(' 0', ' ')


def cricket_rows():
    """'Cricket: Live and Upcoming': matches that are live or start within a day and a half, each on the catalogue's
    live event for it when there is one, otherwise on the cricket channels. Only when the household follows cricket."""
    chosen = (db.meta('profile') or {}).get('sports', [])
    if chosen and 'Cricket' not in chosen:
        return []
    now = time.time()
    # live, or starting within a day and a half; a match past its start that ESPN still calls scheduled counts too
    matches = [m for m in ai.cricket() if (m.get('major') or (m.get('national') and not m.get('minor')))
               and (m.get('state') == 'in' or -6 * 3600 <= m.get('start', 0) - now <= CRICKET_HOURS * 3600)][:12]
    matches.sort(key=lambda m: (not m.get('india'), m.get('state') != 'in', not m.get('major'), m.get('start', 0)))
    if not matches:
        return []
    try:
        root = cat.URLS['sports']
        items = cat.showable(cat.get_dir(root))
    except Exception as e:
        log('sports listing unavailable: {0}'.format(e))
        return []
    events = [i for i in items if i['t'] == 'play' or any(w in i['name'].lower() for w in ('tour', 'series', ' cup', 'trophy', ' vs ', 'league', '20'))]
    channels = next((i for i in items if i['t'] == 'folder' and i['name'].strip().lower() == 'cricket'), None)
    out = []
    for m in matches:
        words = [w.lower() for t in m.get('teams') or [] for w in t.split() if len(w) > 3]
        found = next((e for e in events if sum(1 for w in words if w in e['name'].lower()) >= 2), None) or channels
        if not found:
            continue
        live = m.get('state') == 'in'
        when = 'Live now' if live else ('Starting · ' + _when(m['start'])) if m['start'] <= now else _when(m['start'])
        label = m.get('abbr') or m.get('name') or 'Cricket'
        meta_text = ' · '.join(x for x in (when, m.get('short') or m.get('series'), m.get('status') if live else '') if x)
        item = dict(found, name='{0}  ({1})'.format(label, m.get('short') or m.get('series') or 'Cricket')[:80],
                    plot='{0}. {1}{2}'.format(m.get('name') or label, m.get('series') or '', ('. ' + m['summary']) if m.get('summary') else ''))
        if m.get('logo'):
            item['img'] = m['logo']  # the team's badge (India's when India plays) on a square card
        out.append({'item': item, 'parent': root, 'group': 'Sports', 'meta': meta_text, 'card': True,
                    'soon': live or m.get('start', 0) - now <= 12 * 3600})  # on now or today: the row sits high
    return [('Cricket: Live and Upcoming', out)] if out else []


def share_catalogue(per_group=150):
    """Send the service the newest titles of the index per language and kind (movies and shows apart), combining
    stream.db and the local library so TV shows and deeper pages are included. Sent when the set changed."""
    metadata = db.metadata_all()
    groups = {}
    seen_urls = set()
    all_entries = list(db.all_titles())
    if not db.KIDS:
        try:
            all_entries += library.all_recent(per_group)
        except Exception as e:
            log('library merge for catalogue skipped: {0}'.format(e))
    for e in all_entries:
        url = (e.get('item') or {}).get('url') or ''
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        kind = 'show' if e.get('kind') in discover.SHOW_KINDS else 'movie' if e.get('kind') in discover.MOVIE_KINDS else ''
        if not kind:
            continue
        bucket = groups.setdefault(((e.get('group') or '').lower(), kind), [])
        if len(bucket) >= per_group:
            continue
        name = e['item']['name']
        if any(_same_film(name) == _same_film(x['name']) for x in bucket[-6:]):
            continue  # the PreDVD copy right after the clean one
        data = metadata.get(name) or {}
        title, year = meta.split_title(name)
        bucket.append({'name': name, 'kind': kind, 'lang': cat.title_case(e.get('group') or ''), 'year': data.get('year') or year,
                       'added': time.strftime('%Y-%m-%d', time.localtime(e.get('first_seen') or 0)),
                       'rating': data.get('tmdb_rating') or 0, 'genres': (data.get('genres') or [])[:4],
                       'plot': (data.get('overview') or e['item'].get('plot') or '')[:160]})
    titles = [t for bucket in groups.values() for t in bucket]
    digest = hashlib.md5(json.dumps([(t['name'], t['lang'], t['rating']) for t in titles], sort_keys=True).encode('utf-8')).hexdigest()
    if digest == db.meta('catalogue_shared'):
        return False
    if ai.share_catalogue(titles):
        db.set_meta('catalogue_shared', digest)
        log('{0} catalogue titles shared'.format(len(titles)))
        return True
    return False


def _share_later(built):
    """Send the rows to the service when they differ from what was sent last."""
    summary = [{'title': title, 'items': [{'name': e['item']['name'], 'reason': e.get('meta') or '', 'kind': e.get('kind') or '',
                                           'lang': e.get('group') or ''} for e in entries]} for title, entries in built]
    digest = hashlib.md5(json.dumps(summary, sort_keys=True).encode('utf-8')).hexdigest()
    if digest == STATE.get('shared'):
        return
    if ai.share_rows(summary):
        STATE['shared'] = digest


def _resolve_later():
    """Search the catalogue for a new list's unmatched titles; when any are found the rows are marked for a rebuild."""
    STATE['resolving'] = True
    try:
        if resolve_lists():
            mark_dirty()
    except Exception as e:
        log('resolving posted titles failed: {0}'.format(e))
    finally:
        STATE['resolving'] = False


def mark_dirty():
    db.set_meta('rows_dirty', True)


# ---------- YouTube: videos gathered for the household, scored like titles ----------

import youtube as yt  # noqa: E402

VIDEO_ROW = 24
VIDEOS_KEEP = 3600
SOURCE_WEIGHT = {'subs': 2.5, 'related': 2.0, 'search': 1.2, 'trailers': 1.0, 'pinned': 1.5}


def _video_item(item):
    vid = yt.video_id(item.get('url'))
    return dict(item, id=vid) if vid else None


def refresh_videos(max_age=VIDEOS_KEEP):
    """Gather videos worth recommending: new uploads from followed channels, videos related to the ones played
    last, and YouTube's results for the movies and shows played last. Throttled to once per max_age seconds."""
    if not yt.installed():
        return 0
    if max_age and time.time() - (db.meta('videos_at') or 0) < max_age:
        return 0
    gathered = 0
    sources = []
    if db.KIDS:
        pinned = [{'id': cid, 'name': name} for name, cid in yt.PINNED]
        sources.append(('pinned', lambda: yt.subscriptions(pinned, 40, rpc=cat.rpc)))
        for title, query in yt.TODDLER_ROWS:
            sources.append(('search', lambda q=query: yt.search(q, 20)))
    else:
        followed = db.channels()
        if followed:
            sources.append(('subs', lambda: yt.subscriptions(followed, 80, rpc=cat.rpc)))
        for video, name in db.recent_videos(3):
            sources.append(('related', lambda v=video: cat.get_dir(yt.related_url(v))))
        for name in db.recent_titles(3):
            title, year = meta.split_title(name)
            for extra in ('songs', 'interview'):
                sources.append(('search', lambda q=' '.join(p for p in (title, year, extra) if p): yt.search(q, 20)))
        for lang in (db.meta('profile') or {}).get('languages', [])[:2]:
            sources.append(('trailers', lambda q='latest {0} movie trailers'.format(lang): yt.search(q, 20)))
    for source, fetch in sources:
        try:
            items = [v for v in (_video_item(i) for i in fetch() if i.get('t') == 'play') if v]
        except Exception as e:
            log('videos from {0} failed: {1}'.format(source, e))
            continue
        db.index_videos(source, items)
        gathered += len(items)
    db.set_meta('videos_at', time.time())
    log('{0} videos gathered'.format(gathered))
    return gathered


def _video_score(video, favourites, words, now):
    item = video['item']
    s = max(SOURCE_WEIGHT.get(src, 0.5) for src in (video['source'].split(',') or ['']))
    by = item.get('by') or ''
    s += min(favourites.get(by, 0), 3) * 1.0
    text = item['name'].lower()
    s += min(sum(1 for w in words if w in text), 3) * 0.6
    duration = item.get('duration') or 0
    if duration and duration < 90:
        s -= 3.0  # shorts
    stamp = video.get('published') or ''
    if stamp[:10]:
        try:
            age = (now - time.mktime(time.strptime(stamp[:10], '%Y-%m-%d'))) / 86400.0
            s += 1.5 if age < 2 else 1.0 if age < 7 else 0.5 if age < 30 else 0.0
        except ValueError:
            pass
    s += int(hashlib.md5(video['id'].encode('utf-8')).hexdigest()[:4], 16) / 65535.0 * 0.3
    return s


def video_candidates():
    """Gathered videos the household has not played, best first: [(score, entry)]."""
    favourites = db.watched_channels()
    for c in db.channels():
        favourites[c['name']] = favourites.get(c['name'], 0) + 2
    words = set()
    for name in db.recent_titles(4):
        words.update(w for w in re.findall(r'[a-z]+', meta.split_title(name)[0].lower()) if len(w) > 3)
    for _, name in db.recent_videos(3):
        words.update(w for w in re.findall(r'[a-z]+', name.lower()) if len(w) > 4)
    now = time.time()
    scored = []
    for video in db.all_videos():
        entry = {'item': dict(video['item'], t='play'), 'parent': yt.VIRTUAL, 'group': 'YouTube', 'source': video['source']}
        scored.append((_video_score(video, favourites, words, now), entry))
    scored.sort(key=lambda p: -p[0])
    return scored


def video_rows(picks=None):
    """One crisp 'YouTube for You' wide-tile row for Home; full YouTube browsing lives on the YouTube tab."""
    if not yt.installed():
        return []
    scored = video_candidates()
    if len(scored) < MIN_ROW:
        return []
    seen = set()
    top = []
    for s, e in scored:
        if e['item']['id'] not in seen:
            seen.add(e['item']['id'])
            top.append(e)
        if len(top) >= VIDEO_ROW:
            break
    return [('YouTube for You', top)] if len(top) >= MIN_ROW else []
