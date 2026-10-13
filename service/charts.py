"""India's streaming charts for the "Top 10 in India" row: each source gives a ranked list of titles, TMDb tells each
title's original language, and the lists are merged into one ranking (a title earns 11 - rank points from every chart it
is on, times the source's weight). Standard library only; recs_server.py refreshes it twice a day and serves GET /top10.

Sources: Netflix's own weekly Top 10 for India, TMDb's most popular recent titles per language (the "buzz" signal),
and Prime Video and Aha from their own apps' Top 10 rails (unofficial: when one breaks it is skipped and logged).
"""
import csv
import io
import json
import os
import time
import urllib.parse
import urllib.request

TMDB = 'https://api.themoviedb.org/3'
TMDB_KEY = os.environ.get('TMDB_KEY', '4d6710a7541d9e89c5bec8af26aa96ec')  # the add-on's built-in key by default
NETFLIX_TSV = 'https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv'
AHA_RAILS = 'https://api-aha-cdn.api.ahacms.firstlight.ai/catalog/storefront/landingscreen'
LANGUAGES = ('te', 'hi', 'en')
WEIGHTS = {'netflix': 1.0, 'prime': 1.0, 'aha': 1.0, 'tmdb': 0.6}
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'


def _get(url, params=None, headers=None, timeout=30):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=dict({'User-Agent': UA}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _tmdb(path, **params):
    return json.loads(_get(TMDB + path, dict(params, api_key=TMDB_KEY)))


# ---------- sources: each returns [{'title', 'kind': 'movie' | 'tv', 'rank'}], best first ----------

def netflix():
    """Netflix's latest weekly Top 10 for India, films and TV (season titles reduced to the show)."""
    rows = list(csv.DictReader(io.StringIO(_get(NETFLIX_TSV, timeout=120).decode('utf-8')), delimiter='\t'))
    india = [r for r in rows if r.get('country_iso2') == 'IN']
    if not india:
        return []
    week = max(r['week'] for r in india)
    out = []
    for r in india:
        if r['week'] != week:
            continue
        out.append({'title': r['show_title'], 'kind': 'tv' if r['category'].startswith('TV') else 'movie',
                    'rank': int(r['weekly_rank'])})
    return sorted(out, key=lambda e: (e['rank'], e['kind']))


def tmdb_buzz(days=120):
    """The most popular titles of the last few months in each language, by TMDb's popularity (a stand-in for buzz)."""
    since = time.strftime('%Y-%m-%d', time.gmtime(time.time() - days * 86400))
    out = []
    for lang in LANGUAGES:
        for kind, date_field in (('movie', 'primary_release_date.gte'), ('tv', 'first_air_date.gte')):
            params = {'with_original_language': lang, 'sort_by': 'popularity.desc', date_field: since}
            if lang == 'en':
                params['region'] = 'IN'
            results = _tmdb('/discover/' + kind, **params).get('results') or []
            for n, r in enumerate(results[:10]):
                out.append({'title': r.get('title') or r.get('name'), 'kind': kind, 'rank': n + 1,
                            'lang': lang, 'tmdb': r.get('id'), 'year': (r.get('release_date') or r.get('first_air_date') or '')[:4]})
    return out


def prime():
    """Prime Video publishes no India chart that can be read from outside India without scraping (against its
    terms); left empty until a sanctioned source (e.g. FlixPatrol's paid API) is added."""
    return []


def aha():
    """Aha's Telugu "Trending Now" rail ("Trending Top 10 in India"), from the catalogue API its own apps use. It is
    chosen by Aha's editors rather than measured, and unofficial: the rail is found by its name, not a fixed id."""
    data = json.loads(_get(AHA_RAILS, {'acl': 'te', 'reg': 'in', 'dt': 'web', 'sfInfo': 'true',
                                       'cPageNumber': 1, 'cPageSize': 25}))
    for rail in data['data']['t'][0]['c']:
        name = next((x.get('n') or '' for x in rail.get('lon', []) if x.get('lang') == 'en'), '')
        if 'trending' not in name.lower() and 'top 10' not in name.lower():
            continue
        out = []
        for item in rail.get('cd') or []:
            kind = {'movie': 'movie', 'webseries': 'tv'}.get(item.get('cty'))
            title = next((x.get('n') for x in item.get('lon', []) if x.get('lang') == 'en'), '')
            if kind and title:
                out.append({'title': title.strip(), 'kind': kind, 'rank': len(out) + 1, 'lang': 'te'})
        if out:
            return out[:10]
    return []


SOURCES = {'netflix': netflix, 'prime': prime, 'aha': aha, 'tmdb': tmdb_buzz}


# ---------- language lookup and merge ----------

def identify(title, kind, cache):
    """TMDb id, original language and year for a chart title (remembered in cache)."""
    key = kind + ':' + title.lower()
    if key not in cache:
        found = {}
        try:
            results = _tmdb('/search/' + kind, query=title).get('results') or []
            if results:
                # A chart lists what is on now: prefer a title in our languages from the last few years over an older
                # namesake that happens to be more popular.
                recent = str(int(time.strftime('%Y')) - 3)
                r = max(results[:8], key=lambda x: (x.get('original_language') in LANGUAGES,
                                                    (x.get('release_date') or x.get('first_air_date') or '0')[:4] >= recent,
                                                    x.get('popularity') or 0))
                found = {'tmdb': r.get('id'), 'lang': r.get('original_language') or '',
                         'year': (r.get('release_date') or r.get('first_air_date') or '')[:4]}
        except Exception:
            pass
        cache[key] = found
    return cache[key]


def build(log=print):
    """Fetch every source and merge them: {'at', 'sources': {name: count or error}, 'titles': [...]}, best first."""
    cache, merged, report = {}, {}, {}
    for name, fetch in SOURCES.items():
        try:
            entries = fetch()
            report[name] = len(entries)
        except Exception as e:
            report[name] = 'failed: {0}'.format(e)
            log('top10: {0} failed: {1}'.format(name, e))
            continue
        for e in entries:
            info = {'tmdb': e['tmdb'], 'lang': e['lang'], 'year': e['year']} if e.get('tmdb') else dict(identify(e['title'], e['kind'], cache))
            if e.get('lang') and not e.get('tmdb'):
                info['lang'] = e['lang']
            if info.get('lang') not in LANGUAGES:
                continue
            key = '{0}:{1}'.format(e['kind'], info.get('tmdb') or e['title'].lower())
            t = merged.setdefault(key, {'title': e['title'], 'kind': e['kind'], 'lang': info['lang'],
                                        'year': info.get('year', ''), 'tmdb': info.get('tmdb'), 'score': 0.0, 'ranks': {}})
            if name not in t['ranks'] or e['rank'] < t['ranks'][name]:
                t['score'] += WEIGHTS[name] * (11 - e['rank']) - WEIGHTS[name] * (11 - t['ranks'].get(name, 11))
                t['ranks'][name] = e['rank']
    titles = sorted(merged.values(), key=lambda t: (-t['score'], min(t['ranks'].values())))
    for t in titles:
        t['score'] = round(t['score'], 1)
    log('top10: built {0} titles from {1}'.format(len(titles), report))
    return {'at': int(time.time()), 'sources': report, 'titles': titles[:60]}


if __name__ == '__main__':
    print(json.dumps(build(), indent=1, ensure_ascii=False)[:4000])
