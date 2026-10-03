# Ratings, posters, reviews and trailers from The Movie Database (TMDb), with the IMDb rating from OMDb when
# an OMDb key is also set. Everything is cached in the local database; nothing is fetched without a TMDb key.
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


# Read-only TMDb key supplied by the addon's owner, used when no key is entered in the settings.
DEFAULT_TMDB_KEY = '4d6710a7541d9e89c5bec8af26aa96ec'


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


def _get(url, params):
    import requests
    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200:
        raise RuntimeError('HTTP {0}'.format(r.status_code))
    return r.json()


def _fetch(name, show):
    key = _setting('tmdb_key')
    title, year = split_title(name)
    kind = 'tv' if show else 'movie'
    params = {'api_key': key, 'query': title}
    if year and not show:
        params['year'] = year
    results = _get('{0}/search/{1}'.format(TMDB, kind), params).get('results') or []
    if not results and year and not show:
        params.pop('year')
        results = _get('{0}/search/{1}'.format(TMDB, kind), params).get('results') or []
    if not results:
        return {}
    detail = _get('{0}/{1}/{2}'.format(TMDB, kind, results[0]['id']),
                  {'api_key': key, 'append_to_response': 'videos,reviews,external_ids'})
    videos = (detail.get('videos') or {}).get('results') or []
    trailers = [v for v in videos if v.get('site') == 'YouTube' and v.get('type') in ('Trailer', 'Teaser')]
    trailers.sort(key=lambda v: (v.get('type') != 'Trailer', not v.get('official', False)))
    reviews = [{'author': r.get('author', ''), 'text': (r.get('content') or '').strip()[:3000]}
               for r in ((detail.get('reviews') or {}).get('results') or [])[:5]]
    data = {
        'title': detail.get('title') or detail.get('name') or title,
        'overview': detail.get('overview') or '',
        'poster': IMAGE + 'w342' + detail['poster_path'] if detail.get('poster_path') else '',
        'backdrop': IMAGE + 'w1280' + detail['backdrop_path'] if detail.get('backdrop_path') else '',
        'tmdb_rating': round(detail.get('vote_average') or 0, 1),
        'votes': detail.get('vote_count') or 0,
        'genres': [g['name'] for g in detail.get('genres') or []],
        'runtime': detail.get('runtime') or 0,
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


def lookup(name, show=False, fetch=True):
    """Details for a title from the cache, fetching from TMDb when allowed. Returns {} when nothing is known."""
    cached = db.get_metadata(name)
    if cached is not None:
        data, age = cached
        if age < (KEEP if data else RETRY) or not fetch or not enabled():
            return data
    if not fetch or not enabled():
        return {}
    try:
        data = _fetch(name, show)
    except Exception as e:
        xbmc.log('STREAM META: lookup failed: {0}'.format(e), xbmc.LOGINFO)
        return cached[0] if cached else {}
    db.put_metadata(name, data)
    return data


def rating_text(data):
    """'★ 7.4': the IMDb rating when known, otherwise TMDb's once it has a few votes; '' when there is no rating."""
    if data.get('imdb_rating'):
        return '★ ' + data['imdb_rating']
    if data.get('tmdb_rating') and data.get('votes', 0) >= 5:
        return '★ {0}'.format(data['tmdb_rating'])
    return ''
