# Ratings, posters, cast, reviews, trailers and similar titles from The Movie Database (TMDb), with the IMDb
# rating from OMDb when an OMDb key is also set. Everything is cached in the local database; nothing is fetched
# without a TMDb key.
import json
import re
import time

import xbmc
import xbmcaddon

import db

TMDB = 'https://api.themoviedb.org/3'
IMAGE = 'https://image.tmdb.org/t/p/'
KEEP = 30 * 24 * 3600      # refresh a found title after 30 days
RETRY = 3 * 24 * 3600      # look again for a title that was not found after 3 days
SIMILAR_KEEP = 7 * 24 * 3600

# Read-only TMDb key supplied by the addon's owner, used when no key is entered in the settings.
DEFAULT_TMDB_KEY = '4d6710a7541d9e89c5bec8af26aa96ec'

# Catalogue languages as TMDb's original-language codes. A Telugu film called "Eyes" must not be matched to
# an American one of the same name, so results in the section's language are preferred.
LANG_CODES = {'telugu': 'te', 'hindi': 'hi', 'tamil': 'ta', 'malayalam': 'ml', 'kannada': 'kn', 'marathi': 'mr',
              'gujarati': 'gu', 'punjabi': 'pa', 'bengali': 'bn', 'bangla': 'bn', 'urdu': 'ur', 'english': 'en',
              'south dubbed': 'hi', 'english dubbed': 'hi'}


def _setting(key):
    value = xbmcaddon.Addon('script.stream').getSetting(key).strip()
    return value or (DEFAULT_TMDB_KEY if key == 'tmdb_key' else '')


def enabled():
    return bool(_setting('tmdb_key'))


def split_title(name):
    """('Sardar 2', '2026') from 'Sardar 2 (2026) PreDVD'."""
    year = re.search(r'\((\d{4})\)', name)
    title = re.sub(r'\(\d{4}\).*$', '', name)
    title = re.sub(r'\b(PreDVD|Pre-DVD|DVDScr|HDCAM|HDTS|DVDRip|HDRip|WEBRip|WEB-DL|BluRay|4K|UHD)\b', '', title, flags=re.I)
    title = re.sub(r'\s*-\s*', ' ', title)
    return title.strip(), year.group(1) if year else ''


def lang_code(language):
    return LANG_CODES.get((language or '').lower().strip(), '')


def _get(url, params):
    import requests
    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200:
        raise RuntimeError('HTTP {0}'.format(r.status_code))
    return r.json()


def _choose(results, code, title):
    """The best of TMDb's search results: an exact title in the section's language first, then anything in that
    language, then TMDb's own first pick."""
    if not results:
        return None
    wanted = title.lower()

    def exact(r):
        return (r.get('title') or r.get('name') or '').lower() == wanted or (r.get('original_title') or r.get('original_name') or '').lower() == wanted
    if code:
        same = [r for r in results[:10] if r.get('original_language') == code]
        if same:
            return next((r for r in same if exact(r)), same[0])
    return next((r for r in results[:5] if exact(r)), results[0])


def _fetch(name, show, lang=''):
    key = _setting('tmdb_key')
    title, year = split_title(name)
    kind = 'tv' if show else 'movie'
    code = lang_code(lang)
    params = {'api_key': key, 'query': title}
    if year and not show:
        params['year'] = year
    results = _get('{0}/search/{1}'.format(TMDB, kind), params).get('results') or []
    if year and not show and not any(r.get('original_language') == code for r in results[:10] if code):
        # The year on the catalogue is sometimes the release in the viewer's country; widen the search.
        params.pop('year')
        more = _get('{0}/search/{1}'.format(TMDB, kind), params).get('results') or []
        results = results + [r for r in more if r not in results]
    chosen = _choose(results, code, title)
    if not chosen:
        return {}
    detail = _get('{0}/{1}/{2}'.format(TMDB, kind, chosen['id']),
                  {'api_key': key, 'append_to_response': 'videos,reviews,external_ids,credits'})
    videos = (detail.get('videos') or {}).get('results') or []
    trailers = [v for v in videos if v.get('site') == 'YouTube' and v.get('type') in ('Trailer', 'Teaser')]
    trailers.sort(key=lambda v: (v.get('type') != 'Trailer', not v.get('official', False)))
    reviews = [{'author': r.get('author', ''), 'text': (r.get('content') or '').strip()[:3000]}
               for r in ((detail.get('reviews') or {}).get('results') or [])[:5]]
    credits = detail.get('credits') or {}
    cast = [c.get('name', '') for c in (credits.get('cast') or [])[:5] if c.get('name')]
    if show:
        directors = [c.get('name', '') for c in (detail.get('created_by') or [])[:2] if c.get('name')]
    else:
        directors = [c.get('name', '') for c in (credits.get('crew') or []) if c.get('job') == 'Director'][:2]
    runtime = detail.get('runtime') or 0
    if not runtime and detail.get('episode_run_time'):
        runtime = detail['episode_run_time'][0]
    data = {
        'id': detail.get('id'),
        'kind': kind,
        'title': detail.get('title') or detail.get('name') or title,
        'year': (detail.get('release_date') or detail.get('first_air_date') or '')[:4],
        'language': detail.get('original_language') or '',
        'overview': detail.get('overview') or '',
        'poster': IMAGE + 'w342' + detail['poster_path'] if detail.get('poster_path') else '',
        'backdrop': IMAGE + 'w1280' + detail['backdrop_path'] if detail.get('backdrop_path') else '',
        'tmdb_rating': round(detail.get('vote_average') or 0, 1),
        'votes': detail.get('vote_count') or 0,
        'genres': [g['name'] for g in detail.get('genres') or []],
        'runtime': runtime,
        'seasons': detail.get('number_of_seasons') or 0,
        'cast': cast,
        'directors': directors,
        'trailer': trailers[0]['key'] if trailers else '',
        'reviews': reviews,
        'imdb_id': detail.get('imdb_id') or (detail.get('external_ids') or {}).get('imdb_id') or '',
        'imdb_rating': '',
    }
    omdb = _setting('omdb_key')
    if omdb and data['imdb_id']:
        try:
            rating = _get('https://www.omdbapi.com/', {'apikey': omdb, 'i': data['imdb_id']}).get('imdbRating')
            if rating and rating != 'N/A':
                data['imdb_rating'] = rating
        except Exception:
            pass
    return data


def lookup(name, show=False, fetch=True, lang=''):
    """Details for a title from the cache, fetching from TMDb when allowed. Returns {} when nothing is known."""
    cached = db.get_metadata(name)
    if cached is not None:
        data, age = cached
        # Entries saved by older versions have no cast; they are looked up again so the description panel fills in.
        fresh = age < RETRY if not data else (age < KEEP and 'cast' in data)
        if fresh or not fetch or not enabled():
            return data
    if not fetch or not enabled():
        return {}
    try:
        data = _fetch(name, show, lang)
    except Exception as e:
        xbmc.log('STREAM META: lookup failed: {0}'.format(e), xbmc.LOGINFO)
        return cached[0] if cached else {}
    db.put_metadata(name, data)
    return data


def similar(name, show=False, lang=''):
    """Titles TMDb lists as recommendations for, or similar to, a title: [{'title', 'year', 'lang'}], cached a week."""
    saved = db.meta('similar:' + name)
    if saved and time.time() - saved.get('at', 0) < SIMILAR_KEEP:
        return saved['titles']
    data = lookup(name, show, lang=lang)
    titles = []
    if data.get('id') and enabled():
        kind = data.get('kind') or ('tv' if show else 'movie')
        seen = set()
        for path in ('recommendations', 'similar'):
            try:
                results = _get('{0}/{1}/{2}/{3}'.format(TMDB, kind, data['id'], path), {'api_key': _setting('tmdb_key')}).get('results') or []
            except Exception as e:
                xbmc.log('STREAM META: {0} failed: {1}'.format(path, e), xbmc.LOGINFO)
                results = []
            for r in results:
                title = r.get('title') or r.get('name') or ''
                if title and title.lower() not in seen:
                    seen.add(title.lower())
                    titles.append({'title': title, 'year': (r.get('release_date') or r.get('first_air_date') or '')[:4],
                                   'lang': r.get('original_language') or ''})
    db.set_meta('similar:' + name, {'at': time.time(), 'titles': titles[:40]})
    return titles[:40]


def rating_text(data):
    """'★ 7.4': the IMDb rating when known, otherwise TMDb's once it has a few votes; '' when there is no rating."""
    if data.get('imdb_rating'):
        return '★ ' + data['imdb_rating']
    if data.get('tmdb_rating') and data.get('votes', 0) >= 5:
        return '★ {0}'.format(data['tmdb_rating'])
    return ''


def length_text(data):
    """'2h 13m' for a film, '3 seasons' for a show, '' when unknown."""
    if data.get('kind') == 'tv' and data.get('seasons'):
        return '{0} season{1}'.format(data['seasons'], '' if data['seasons'] == 1 else 's')
    minutes = data.get('runtime') or 0
    if not minutes:
        return ''
    hours, minutes = divmod(int(minutes), 60)
    return '{0}h {1}m'.format(hours, minutes) if hours else '{0}m'.format(minutes)


def people_text(data):
    """'Starring A, B, C · Directed by D', or '' when nobody is known."""
    parts = []
    if data.get('cast'):
        parts.append('Starring ' + ', '.join(data['cast'][:4]))
    if data.get('directors'):
        parts.append(('Created by ' if data.get('kind') == 'tv' else 'Directed by ') + ', '.join(data['directors'][:2]))
    return ' · '.join(parts)
