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
import meta

ROW_SIZE = 40          # tiles per row
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
    order = [g.lower() for g in profile.get('languages', [])]
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
    title, year = meta.split_title(name)
    return discover._plain(title) + year


def _no_repeats(entries):
    """A row shows a film once, whichever language or copy came first."""
    seen, out = set(), []
    for e in entries:
        key = _same_film(e['item']['name'])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


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


def show_row(scored, order):
    found = [e for s, e, d in scored if e.get('kind') in discover.SHOW_KINDS]
    if len(found) >= MIN_ROW:
        return [('Shows to Binge', _with_meta(_balanced([p for p in scored if p[1].get('kind') in discover.SHOW_KINDS], order, ROW_SIZE)))]
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
        alike.sort(key=lambda e: ranked.get(e['item']['name'], len(ranked)))
        if len(alike) >= 4:
            short = meta.split_title(name)[0] or name
            rows.append(('Because you watched ' + (short if len(short) <= 32 else short[:31].rstrip() + '…'), _with_meta(alike[:ROW_SIZE])))
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
    """All the recommendation rows, best first: [(title, [entries])]. Entries carry 'meta' and may carry
    'suggestion' (top picks) and 'rank'."""
    started = time.time()
    scored, t, metadata = candidates()
    for s, e, d in scored:
        e['data'] = {'tmdb_rating': d.get('tmdb_rating'), 'votes': d.get('votes'), 'imdb_rating': d.get('imdb_rating')}
    order = t['languages']
    now = time.time()
    rows = []
    picks = top_picks(scored, order)
    if picks:
        _, by = posted_picks()
        rows.append(('Top Picks for You' + ('  \u00b7  ' + by if by else ai.engine_note()), picks))
    try:
        videos = video_rows(picks)
    except Exception as e:
        log('video rows failed: {0}'.format(e))
        videos = []
    rows += [r for r in videos if r[0].startswith('Tonight')]  # the mixed row sits right under the picks
    rows += because_rows(scored)
    fresh = discover.new_entries(ROW_SIZE)
    if len(fresh) >= 4:
        rows.append(('New Releases', _with_meta(fresh)))
    for lang, kind, chart in discover.india_top_rows()[:3]:  # the first language's movies and shows, then the next language
        rows.append(('Top 10 {0} {1}'.format(lang, kind), [dict(e, meta=e['meta']) for e in chart]))
    home = discover.top_entries()
    if len(home) >= 3:
        rows.append(('Top 10 in Your Home', [dict(e, meta=e['meta']) for e in home]))
    rows += genre_rows(scored, t)
    rows += rated_rows(scored, t)
    rows += show_row(scored, order)
    rows += [r for r in videos if not r[0].startswith('Tonight')]
    rows += provider_rows(scored)
    rows += gems(scored, now)
    rows = [(title, _no_repeats(entries)) for title, entries in rows]
    for _, entries in rows:
        for e in entries:
            e.pop('data', None)
            e.pop('source', None)
    log('{0} rows from {1} candidates in {2:.1f}s'.format(len(rows), len(scored), time.time() - started))
    return rows


def pool_for_model(limit=120):
    """The best candidates for the model to re-rank, language shares applied."""
    scored, t, _ = candidates()
    return _balanced(scored, t['languages'], limit)


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

VIDEO_ROW = 40
SOURCE_WEIGHT = {'subs': 2.5, 'related': 2.0, 'search': 1.2, 'trailers': 1.0, 'pinned': 1.5}


def _video_item(item):
    vid = yt.video_id(item.get('url'))
    return dict(item, id=vid) if vid else None


def refresh_videos():
    """Gather videos worth recommending: new uploads from followed and pinned channels, videos related to the ones
    played last, and YouTube's results for the movies and shows played last. Network work; run in the background."""
    if not yt.installed():
        return 0
    gathered = 0
    pinned = [{'id': cid, 'name': name} for name, cid in yt.PINNED]
    sources = [('pinned', lambda: yt.subscriptions(pinned, 40))]
    if not db.KIDS:
        followed = db.channels()
        if followed:
            sources.append(('subs', lambda: yt.subscriptions(followed, 80)))
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


def video_rows(picks):
    """'YouTube for You', 'Because you watched <video>' (wide rows), and 'Tonight', a row that mixes the top picks
    with the best videos."""
    if not yt.installed():
        return []
    scored = video_candidates()
    if len(scored) < MIN_ROW:
        return []
    rows = []
    seen = set()
    top = []
    for s, e in scored:
        if e['item']['id'] not in seen:
            seen.add(e['item']['id'])
            top.append(e)
        if len(top) >= VIDEO_ROW:
            break
    rows.append(('YouTube for You', top))
    for video, name in db.recent_videos(1):
        alike = [e for s, e in scored if 'related' in e.get('source', '') and e['item']['id'] != video][:VIDEO_ROW]
        if len(alike) >= MIN_ROW:
            short = yt.clean(name)
            rows.append(('More videos like ' + (short if len(short) <= 30 else short[:29].rstrip() + '…'), alike))
            break
    if picks:
        mixed, videos = [], [e for s, e in scored[:12]]
        for n, e in enumerate(picks[:24]):
            mixed.append(e)
            if n % 2 == 1 and videos:
                mixed.append(videos.pop(0))
        rows.append(('Tonight: Movies, Shows and Videos', mixed))
    return rows
