# Rows built from the local index and TMDb: titles like one just watched, browsing by genre, what is new,
# and what the household plays most. Everything prefers the viewer's languages, first language first.
import re
import time

import ai
import catalogue as cat
import db
import meta

MOVIE_KINDS = ('Movies',)
SHOW_KINDS = ('Web Series', 'TV Shows', 'English TV')
TITLE_KINDS = MOVIE_KINDS + SHOW_KINDS

# Genres offered for browsing. Each maps to TMDb's genre names (for titles that have been looked up) and to
# words found in the catalogue's own descriptions (for the rest).
GENRES = [
    ('Action', ['Action', 'Adventure', 'War'], ['action', 'fight', 'mission', 'gangster', 'revenge', 'battle', 'warrior']),
    ('Comedy', ['Comedy'], ['comedy', 'funny', 'hilarious', 'laugh', 'comic']),
    ('Thriller & Crime', ['Thriller', 'Crime', 'Mystery'], ['thriller', 'murder', 'crime', 'police', 'investigat', 'detective', 'mystery', 'killer', 'heist']),
    ('Romance', ['Romance'], ['love', 'romance', 'romantic', 'wedding', 'couple']),
    ('Drama', ['Drama', 'History'], ['drama', 'emotional', 'struggle', 'journey']),
    ('Horror', ['Horror'], ['horror', 'ghost', 'haunted', 'spirit', 'paranormal', 'supernatural']),
    ('Family', ['Family', 'Animation'], ['family', 'father', 'mother', 'children', 'brother', 'sister']),
    ('Sci-fi & Fantasy', ['Science Fiction', 'Fantasy'], ['sci-fi', 'future', 'alien', 'fantasy', 'magic', 'myth', 'superhero']),
    ('Devotional & Mythology', [], ['god', 'goddess', 'divine', 'devotion', 'temple', 'mytholog', 'lord ']),
]
TMDB_TO_GENRE = {name: label for label, names, _ in GENRES for name in names}


def _lang_rank(entry, order=None):
    order = cat.top_groups() if order is None else order
    group = (entry.get('group') or '').lower()
    for n, lang in enumerate(order):
        if db.lang_match(group, [lang]):
            return n
    return len(order)


def by_language(entries, limit=None):
    """The viewer's languages first (their main language leading), then the rest; stable otherwise."""
    order = cat.top_groups()
    ranked = sorted(enumerate(entries), key=lambda p: (_lang_rank(p[1], order), p[0]))
    out = [e for _, e in ranked]
    return out[:limit] if limit else out


def chosen_only(entries):
    """Only the viewer's languages, unless that would leave nothing."""
    return db.only_chosen(entries, lambda e: e.get('group'))


def _dedupe(entries):
    seen, out = set(), []
    for e in entries:
        name = e['item']['name']
        if name not in seen:
            seen.add(name)
            out.append(e)
    return out


def genre_entries(genre, kinds, limit=80):
    """Titles of a genre: looked-up TMDb genres and description words, in the viewer's languages, newest first."""
    spec = next((g for g in GENRES if g[0] == genre), None)
    if not spec:
        return []
    entries = chosen_only(_dedupe(db.by_genre(kinds, spec[1], spec[2])))
    return by_language(entries, limit)


def genre_tiles(kinds):
    """[(genre, count)] for the genres that have enough titles to be worth a tile."""
    out = []
    for genre, names, words in GENRES:
        entries = chosen_only(_dedupe(db.by_genre(kinds, names, words)))
        if len(entries) >= 4:
            out.append((genre, len(entries)))
    return out


def similar_entries(name, show=False, lang='', limit=20):
    """Titles like the one named: a list a bot posted to the service for this title first (with its reasons), then
    TMDb's picks that exist in the catalogue, then titles of the same genres in the same language. Empty when too
    little is known."""
    kinds = SHOW_KINDS if show else MOVIE_KINDS
    posted = ai.related(name)
    lead = list_entries(posted.get('items') or [], limit) if posted else []  # the bot's picks, in its order
    if len(lead) >= limit:
        return lead
    found = []
    try:
        for pick in meta.similar(name, show, lang):
            for hit in db.find_by_title(pick['title'], kinds, 2):
                base = meta.split_title(hit['item']['name'])[0].lower()
                if base == pick['title'].lower() or (pick['year'] and pick['year'] in hit['item']['name']):
                    found.append(hit)
    except Exception as e:
        cat.log('similar lookup failed: {0}'.format(e))
    found = chosen_only(_dedupe(found)) if found else []
    if len(found) < limit:
        data = meta.lookup(name, show, fetch=False, lang=lang)
        labels = {TMDB_TO_GENRE[g] for g in data.get('genres', []) if g in TMDB_TO_GENRE}
        names = [n for label, ns, _ in GENRES if label in labels for n in ns]
        words = [w for label, _, ws in GENRES if label in labels for w in ws][:12]
        if names or words:
            same = db.by_genre(kinds, names, words, 300)
            if lang:
                same = [e for e in same if db.lang_match(e.get('group') or '', [lang.lower()])] or same
            found = _dedupe(found + by_language(chosen_only(same)))
    base = meta.split_title(name)[0].lower()
    found = [e for e in found if meta.split_title(e['item']['name'])[0].lower() != base]
    names = {e['item']['name'] for e in lead}
    return (lead + [e for e in by_language(found, limit) if e['item']['name'] not in names])[:limit]


def new_entries(limit=24):
    """Titles added to the catalogue this week, in the viewer's languages."""
    return by_language(chosen_only(_dedupe(db.new_titles())), limit)


CHART_LANGUAGES = {'te': 'telugu', 'hi': 'hindi', 'en': 'english'}
CHART_SOURCES = {'netflix': 'Netflix', 'prime': 'Prime Video', 'aha': 'Aha', 'tmdb': 'Trending'}


def _plain(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _chart_match(title):
    """The catalogue entry for a chart title: same name (ignoring punctuation, year and quality words) in the
    title's language, the right year first for a movie; or None when the catalogue does not have it."""
    lang = CHART_LANGUAGES[title['lang']]
    kinds = MOVIE_KINDS if title['kind'] == 'movie' else SHOW_KINDS
    names = [title['title']] + ([title['title'].split(':')[0]] if ':' in title['title'] else [])
    for name in names:
        want = _plain(name)
        found = [e for e in db.find_by_title(name, kinds, limit=12)
                 if _plain(meta.split_title(e['item']['name'])[0]) == want and db.lang_match(e.get('group') or '', [lang])]
        if found:
            found.sort(key=lambda e: meta.split_title(e['item']['name'])[1] != title.get('year'))
            return found[0]
    return None


def match_title(title, year='', kind='', lang=''):
    """The catalogue entry for a title named from outside (a chart, a bot's list): same name ignoring punctuation,
    year and quality words; kind 'movie' or 'show' (either when blank); lang a TMDb code ('te') or a section name
    ('Telugu'), any language when blank. None when the catalogue does not have it."""
    kinds = MOVIE_KINDS if kind == 'movie' else SHOW_KINDS if kind in ('show', 'tv') else TITLE_KINDS
    code = (lang or '').lower().strip()
    group = CHART_LANGUAGES.get(code) or next((name for name, c in meta.LANG_CODES.items() if c == code), '') or code
    title, named_year = meta.split_title(title or '')  # 'Nayyi Navelli (2026) PreDVD' -> 'Nayyi Navelli', '2026'
    year = str(year or named_year or '')
    names = [title] + ([title.split(':')[0]] if ':' in title else [])
    # The language asked for first; failing that the household's languages (a Hindi show often sits in the dubbed
    # sections), then any language.
    languages = [group] if group else []
    languages += [l for l in db.chosen_languages() if l not in languages] + ['']
    for name in names:
        want = _plain(name)
        if not want:
            continue
        # 'Court: State vs A Nobody' is listed as 'Court State Vs. A Nobody': fetch by the first words, compare without punctuation
        words = re.findall(r'[A-Za-z0-9]+', name)
        hits = db.find_by_title(name, kinds, limit=12)
        if words:
            hits += db.find_by_title(' '.join(words[:2]), kinds, limit=40)
            if len(words[0]) > 3:
                hits += db.find_by_title(words[0], kinds, limit=60)  # 'Search- The Naina Murder Case'
        same = [e for e in hits if _plain(meta.split_title(e['item']['name'])[0]) == want]
        for language in languages:
            found = [e for e in same if not language or db.lang_match(e.get('group') or '', [language])]
            if found:
                # the right year first, and a clean copy before a cinema recording of the same film
                found.sort(key=lambda e: (meta.split_title(e['item']['name'])[1] != year, meta.is_cinema_copy(e['item']['name'])))
                return found[0]
    return None


FOUND_KEEP = 7 * 24 * 3600


def find_outside(title, year='', kind='', lang=''):
    """A posted title the local index lacks, searched for in the catalogue itself (the index only holds the newest
    pages of each section). A hit is indexed, so later matching is local; the answer, found or not, is kept a week."""
    key = 'found:{0}:{1}:{2}'.format(_plain(title), year or '', (lang or '').lower())
    saved = db.meta(key)
    if saved and time.time() - saved.get('at', 0) < FOUND_KEEP:
        return saved.get('entry')
    kinds = MOVIE_KINDS if kind == 'movie' else SHOW_KINDS if kind in ('show', 'tv') else TITLE_KINDS
    code = (lang or '').lower().strip()
    group = CHART_LANGUAGES.get(code) or next((name for name, c in meta.LANG_CODES.items() if c == code), '') or code
    title, named_year = meta.split_title(title or '')
    year = str(year or named_year or '')
    # 'Stree 2: Sarkate Ka Aatank' is listed as plain 'Stree 2': the part before a colon is searched for and accepted too
    variants = [title] + ([title.split(':')[0].strip()] if ':' in title else [])
    wanted = {_plain(v) for v in variants if _plain(v)}
    entry = None
    try:
        for query in reversed(variants):  # the short form first: the catalogue's search likes short queries
            hits = cat.rank_results(query, cat.search(query))
            hits.sort(key=lambda f: meta.is_cinema_copy(f['item']['name']))  # a clean copy before a cinema recording
            for found in hits:
                name = found['item']['name']
                section_kind, section_lang = cat.section_of(found['parent'])
                if found['item']['t'] not in ('play', 'folder') or section_kind not in kinds:
                    continue
                if _plain(meta.split_title(name)[0]) not in wanted:
                    continue
                language = section_lang or found.get('group') or ''
                if group and not db.lang_match(language, [group]):
                    continue
                found_year = meta.split_title(name)[1]
                if year and found_year and found_year != year:
                    continue
                entry = {'item': found['item'], 'parent': found['parent'], 'group': language}
                db.index_titles(found['parent'], language, section_kind, [found['item']])
                break
            if entry:
                break
    except Exception as e:
        cat.log('catalogue search for "{0}" failed: {1}'.format(title, e))
    db.set_meta(key, {'at': time.time(), 'entry': entry})
    return entry


def list_entries(items, limit=40):
    """Catalogue entries for a posted list's items, in the list's order, each carrying the item's reason."""
    out, seen = [], set()
    for item in items:
        entry = match_title(item.get('title') or '', item.get('year') or '', item.get('kind') or '', item.get('lang') or '')
        if not entry or entry['item']['name'] in seen:
            continue
        seen.add(entry['item']['name'])
        out.append(dict(entry, reason=(item.get('reason') or '')[:160]))
        if len(out) >= limit:
            break
    return out


def india_top_rows(limit=10, least=3):
    """India's streaming charts (Netflix, Prime Video, Aha and TMDb buzz, merged by the recommendation service), split
    into one Top 10 per language and kind: [(language, 'Movies' | 'Shows', [tiles with their rank])], the viewer's
    languages in their own order, movies before shows. Only titles the catalogue can play are kept, and a row with fewer
    than `least` of them is left out."""
    chosen = db.chosen_languages()
    langs = [l for l in chosen if l in CHART_LANGUAGES.values()] if chosen else list(CHART_LANGUAGES.values())
    buckets = {(lang, kind): [] for lang in langs for kind in ('movie', 'tv')}
    seen = set()
    for title in ai.charts():
        bucket = buckets.get((CHART_LANGUAGES.get(title.get('lang')), title.get('kind')))
        if bucket is None or len(bucket) == limit:
            continue
        entry = _chart_match(title)
        if not entry or entry['item']['name'] in seen:
            continue
        seen.add(entry['item']['name'])
        charts = ', '.join('{0} #{1}'.format(CHART_SOURCES[s], r)
                           for s, r in sorted(title.get('ranks', {}).items(), key=lambda p: p[1]) if s in CHART_SOURCES)
        bucket.append(dict(entry, charts=charts))
    rows = []
    for lang in langs:
        for kind, label in (('movie', 'Movies'), ('tv', 'Shows')):
            found = buckets[(lang, kind)]
            if len(found) < least:
                continue
            name = cat.title_case(lang)
            tiles = [dict(e, meta='#{0} {1} {2} in India · {3}'.format(n, name, label, e.pop('charts')), rank=n)
                     for n, e in enumerate(found, 1)]
            rows.append((name, label, tiles))
    return rows


def top_entries(limit=10):
    """The household's most played movies and shows this month, as tiles with their rank."""
    out = []
    for n, pick in enumerate(db.top_titles(30, limit)):
        entry = db.find_show(pick['title']) if pick['series'] else pick['entry']
        if not entry:
            continue
        times = 'played {0} time{1}'.format(pick['plays'], '' if pick['plays'] == 1 else 's')
        out.append(dict(entry, meta='#{0} in your home · {1}'.format(n + 1, times), rank=n + 1))
    return out

