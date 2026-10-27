# Episode names, stills and synopses for a season of a show. The provider only calls them "Episode 1", "Episode 2";
# TMDb knows most shows' episodes, and Wikipedia's episode tables cover many Indian shows TMDb has only numbered.
# Order of trust: TMDb (names, stills, runtimes), then Wikipedia (names, summaries), then the provider's own names.
# Everything is cached in the local database for a week.
import html
import json
import re
import time

import xbmc

import db
import meta

KEEP = 7 * 24 * 3600
WIKI = 'https://en.wikipedia.org/w/api.php'
UA = 'StreamKodiAddon/1.0 (https://github.com/yeshvantbhavnasi/kodi-stream)'
GENERIC = re.compile(r'^\s*(episode|ep\.?|part|chapter)\s*#?\d+\s*$', re.I)


def log(msg):
    xbmc.log('STREAM EPISODES: ' + msg, xbmc.LOGINFO)


def season_number(name):
    """1 from 'Season 1', 'S01', 'Season 1 (Hindi)'; 1 when no number is found."""
    m = re.search(r'(\d+)', name or '')
    return int(m.group(1)) if m else 1


def episode_number(name):
    """11 from 'Episode 11', 'EP11', 'S01E11', 'Show Name Part 3'; None when nothing looks like an episode number."""
    text = name or ''
    m = re.search(r'\bS\d+\s*E0*(\d+)', text, re.I) or re.search(r'\b(?:episode|ep\.?|part|chapter)\s*#?0*(\d+)', text, re.I)
    if m:
        return int(m.group(1))
    numbers = re.findall(r'(?<![\d(])\b0*(\d{1,3})\b(?![\d)])', re.sub(r'\(\d{4}\)', '', text))
    return int(numbers[-1]) if numbers else None


def _is_generic(title):
    return not title or bool(GENERIC.match(title))


# ---------- TMDb ----------

def tmdb_season(show_id, season):
    """{episode number: {'title', 'plot', 'still', 'runtime', 'aired'}} from TMDb; generic names are left blank."""
    data = meta._get('{0}/tv/{1}/season/{2}'.format(meta.TMDB, show_id, season), {'api_key': meta._setting('tmdb_key')})
    out = {}
    for e in data.get('episodes') or []:
        n = e.get('episode_number')
        if not n:
            continue
        title = (e.get('name') or '').strip()
        out[int(n)] = {'title': '' if _is_generic(title) else title, 'plot': (e.get('overview') or '').strip(),
                       'still': meta.IMAGE + 'w500' + e['still_path'] if e.get('still_path') else '',
                       'runtime': e.get('runtime') or 0, 'aired': e.get('air_date') or ''}
    return out


# ---------- Wikipedia ----------

def _wiki_get(params):
    import requests
    r = requests.get(WIKI, params=dict(params, format='json', formatversion=2), headers={'User-Agent': UA}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError('Wikipedia HTTP {0}'.format(r.status_code))
    return r.json()


def _wikitext(page):
    """The wikitext of a page, following one redirect; '' when the page does not exist."""
    for _ in range(2):
        reply = _wiki_get({'action': 'parse', 'page': page, 'prop': 'wikitext', 'redirects': 1})
        text = (reply.get('parse') or {}).get('wikitext') or ''
        m = re.match(r'#REDIRECT\s*\[\[([^\]|]+)', text, re.I)
        if not m:
            return text
        page = m.group(1)
    return ''


def _unwiki(text):
    """Plain text from a wikitext fragment: links, templates, references and quotes removed."""
    text = re.sub(r'<ref[^>]*/>', '', text)
    text = re.sub(r'<ref[^>]*>.*?</ref>', '', text, flags=re.S)
    text = re.sub(r'\{\{[^{}]*\}\}', '', text)  # templates such as {{Start date|...}}, {{nowrap|...}}
    text = re.sub(r'\[\[(?:[^\]|]*\|)?([^\]]+)\]\]', r'\1', text)
    text = re.sub(r"'{2,}", '', text)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(re.sub(r'\s+', ' ', text)).strip(' "“”')


def _episode_blocks(text):
    """[(season or None, {param: value})] for every {{Episode list}} on a page, in page order. A season comes from
    the nearest heading above the block ('Season 2 (2023)') or from the block's own season parameter."""
    out = []
    heading = re.compile(r'^(=+)\s*(.+?)\s*\1\s*$', re.M)
    marks = [(m.start(), m.group(2)) for m in heading.finditer(text)]
    for m in re.finditer(r'\{\{Episode list(?:/sublist\|[^|}]*)?\s*\|', text, re.I):
        depth, pos = 1, m.end()
        while depth and pos < len(text):
            if text.startswith('{{', pos):
                depth, pos = depth + 1, pos + 2
            elif text.startswith('}}', pos):
                depth, pos = depth - 1, pos + 2
            else:
                pos += 1
        body = text[m.end():pos - 2]
        params = {}
        for part in re.split(r'\|(?=\s*\w+\s*=)', body):
            key, _, value = part.partition('=')
            if _ and key.strip():
                params[key.strip().lower()] = value.strip()
        above = [h for start, h in marks if start < m.start()]
        season = None
        for h in reversed(above):
            found = re.search(r'(?:season|series)\s*(\d+)', h, re.I)
            if found:
                season = int(found.group(1))
                break
        out.append((season, params))
    return out


def wiki_pages(title):
    return [title, title + ' (TV series)', title + ' (web series)', title + ' (Indian TV series)', title + ' (Indian web series)',
            'List of ' + title + ' episodes']


def wiki_season(title, season):
    """{episode number: {'title', 'plot'}} from the show's Wikipedia page; {} when Wikipedia has no episode table."""
    for page in wiki_pages(title):
        try:
            text = _wikitext(page)
        except Exception as e:
            log('Wikipedia failed for {0}: {1}'.format(page, e))
            return {}
        if not text or '{{episode list' not in text.lower():
            continue
        blocks = _episode_blocks(text)
        seasons = {s for s, _ in blocks if s}
        out, counter = {}, 0
        for s, params in blocks:
            if seasons and s != season:
                continue
            if not seasons and season != 1:
                break
            counter += 1
            number = params.get('episodenumber2') or params.get('episodenumber') or ''
            m = re.search(r'\d+', number)
            n = int(m.group(0)) if m else counter
            name = _unwiki(params.get('title') or params.get('rtitle') or '')
            name = re.sub(r'^"|"$', '', name)
            summary = _unwiki(params.get('shortsummary') or '')
            if name or summary:
                out[n] = {'title': '' if _is_generic(name) else name, 'plot': summary}
        if out:
            return out
    return {}


# ---------- the combined answer ----------

def names(show, season_name, lang='', data=None):
    """{episode number: {'title', 'plot', 'still', 'runtime'}} for one season of a show, from the best sources that
    know it. An empty dict when nothing is known; the caller then shows the provider's own names."""
    season = season_number(season_name)
    key = 'episodes:{0}:{1}'.format(show, season)
    saved = db.meta(key)
    if saved and time.time() - saved.get('at', 0) < KEEP:
        return {int(k): v for k, v in saved['episodes'].items()}
    found = {}
    data = data if data is not None else meta.lookup(show, True, lang=lang)
    if data.get('id') and data.get('kind') == 'tv' and meta.enabled():
        try:
            found = tmdb_season(data['id'], season)
        except Exception as e:
            log('TMDb season failed for {0} S{1}: {2}'.format(show, season, e))
    if not any(v['title'] for v in found.values()):
        title = data.get('title') or meta.split_title(show)[0] or show
        wiki = wiki_season(title, season)
        if not wiki and title.lower() != show.lower():
            wiki = wiki_season(meta.split_title(show)[0] or show, season)
        for n, info in wiki.items():
            mine = found.setdefault(n, {'title': '', 'plot': '', 'still': '', 'runtime': 0})
            mine['title'] = mine['title'] or info['title']
            mine['plot'] = mine['plot'] or info['plot']
    if found:
        db.set_meta(key, {'at': time.time(), 'episodes': {str(n): v for n, v in found.items()}})
    else:
        db.set_meta(key, {'at': time.time() - KEEP + 24 * 3600, 'episodes': {}})  # try again tomorrow
    return found


def describe(number, info, fallback):
    """'3. Kishkindha Bound' for an episode with a known name, otherwise the provider's own name."""
    title = (info or {}).get('title')
    if title:
        return '{0}. {1}'.format(number, title) if number else title
    return fallback
