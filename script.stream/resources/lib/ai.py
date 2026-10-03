# Recommendations and smart search. Keys come from the addon settings and are never logged.
# Kodi's Python cannot install the Anthropic SDK, so Bedrock is called over plain HTTPS.
import json
import re
import time
from itertools import zip_longest

import xbmc
import xbmcaddon

import db

JEV_URL = 'https://api.typesafe.ai/v1/systemone'
REFRESH_AFTER = 12 * 3600
PICKS = 15


def _setting(key):
    return xbmcaddon.Addon('script.stream').getSetting(key).strip()


def enabled():
    return _setting('ai_enabled') != 'false'


def has_llm():
    return enabled() and bool(_setting('bedrock_token'))


def has_jev():
    return enabled() and bool(_setting('jev_key'))


def log(msg):
    xbmc.log('STREAM AI: {0}'.format(msg), xbmc.LOGINFO)


def ask_claude(system, prompt, max_tokens=4000):
    """One Messages-format request to Claude on Amazon Bedrock, authenticated with a Bedrock API key."""
    import requests
    region = _setting('bedrock_region') or 'us-east-1'
    model = _setting('bedrock_model') or 'anthropic.claude-opus-5-5'
    url = 'https://bedrock-runtime.{0}.amazonaws.com/model/{1}/invoke'.format(region, requests.utils.quote(model, safe=''))
    body = {'anthropic_version': 'bedrock-2023-05-31', 'max_tokens': max_tokens, 'system': system,
            'messages': [{'role': 'user', 'content': prompt}]}
    r = requests.post(url, json=body, timeout=120,
                      headers={'Authorization': 'Bearer ' + _setting('bedrock_token'), 'Accept': 'application/json'})
    if r.status_code != 200:
        raise RuntimeError('Bedrock returned HTTP {0}: {1}'.format(r.status_code, r.text[:200]))
    reply = r.json()
    if reply.get('stop_reason') == 'refusal':
        raise RuntimeError('The model declined this request')
    return ''.join(b.get('text', '') for b in reply.get('content', []) if b.get('type') == 'text')


def _parse_json(text):
    m = re.search(r'\{.*\}', text, re.S)
    return json.loads(m.group(0)) if m else {}


def jev_rank(goal, entries):
    """Order entries by how well Jev judges each one to fit the goal. Returns ids, best first."""
    import requests
    order = []
    for start in range(0, len(entries), 40):
        chunk = entries[start:start + 40]
        criteria = {cid: text for cid, text in chunk}
        body = {'model': 'jev-latest', 'state': {'request': goal},
                'questions': {'best': {'type': 'choice', 'criteria': criteria,
                                       'instructions': {'goal': goal, 'rules': 'Choose the title that best fits the request.'}}}}
        r = requests.post(JEV_URL, json=body, timeout=30, headers={'Authorization': 'Bearer ' + _setting('jev_key')})
        if r.status_code != 200:
            raise RuntimeError('Jev returned HTTP {0}'.format(r.status_code))
        probs = (r.json().get('answers', {}).get('best', {}) or {}).get('probabilities', {}) or {}
        order += [(probs.get(cid, 0), cid) for cid, _ in chunk]
    order.sort(key=lambda p: -p[0])
    return [cid for _, cid in order]


def _describe(entry):
    item = entry['item']
    text = item['name']
    if entry.get('group'):
        text += ' [{0}]'.format(entry['group'])
    if item.get('plot'):
        text += ' - ' + item['plot'][:160]
    return text


def _pick(goal_for_jev, system, prompt_intro, pool, count):
    """Shared pipeline: Jev narrows the pool, Claude picks and explains. Either step may be absent."""
    ids = {'c{0}'.format(n): e for n, e in enumerate(pool)}
    order = list(ids)
    source = 'local'
    if has_jev():
        try:
            order = jev_rank(goal_for_jev, [(cid, _describe(e)) for cid, e in ids.items()])
            source = 'jev'
        except Exception as e:
            log('Jev ranking skipped: {0}'.format(e))
    if has_llm():
        shortlist = order[:60]
        listing = '\n'.join('{0}: {1}'.format(cid, _describe(ids[cid])) for cid in shortlist)
        prompt = ('{0}\n\nCandidates:\n{1}\n\nPick the {2} best candidates, best first. Reply with JSON only, shaped as '
                  '{{"picks": [{{"id": "c0", "reason": "one short sentence for the viewer"}}]}}. Use only ids from the list.'
                  ).format(prompt_intro, listing, count)
        try:
            picks = _parse_json(ask_claude(system, prompt)).get('picks', [])
            chosen = [{'entry': ids[p['id']], 'reason': str(p.get('reason', ''))[:160]} for p in picks if p.get('id') in ids]
            if chosen:
                return chosen[:count], 'llm+jev' if source == 'jev' else 'llm'
        except Exception as e:
            log('Claude step skipped: {0}'.format(e))
    return [{'entry': ids[cid], 'reason': ''} for cid in order[:count]], source


def _timeline():
    """The viewer's interactions as one line each, oldest first: when, what they did, to which title."""
    lines = []
    for e in db.events(80):
        t = time.localtime(e['at'])
        part = 'morning' if t.tm_hour < 12 else 'afternoon' if t.tm_hour < 17 else 'evening' if t.tm_hour < 22 else 'late night'
        tags = ', '.join(x for x in (e['grp'], e['kind']) if x)
        line = '{0} {1} | {2} | {3}'.format(time.strftime('%Y-%m-%d %a', t), part, e['action'], e['name'])
        if tags:
            line += ' [{0}]'.format(tags)
        if e['detail']:
            line += ' | ' + e['detail']
        lines.append(line)
    return lines


# Approach borrowed from Netflix's recommendation foundation model: treat the viewer's history as an
# ordered sequence of rich interaction events, and have one model predict the next several interactions
# rather than only the very next one. Netflix trains its own transformer on billions of events; with a
# single household's data we instead give Claude the event sequence and let it predict from the catalogue.
RECOMMENDER = (
    'You are the recommendation model for a streaming catalogue of South Asian and English films and shows. '
    'You are given one household\'s interaction history as a time-ordered sequence of events. Each event has a date, '
    'time of day, an action (played, stopped with percent watched, finished, listed, unlisted, dismissed, searched), '
    'a title, and its language and type. Treat it like a sequence model would: recent events matter most, finishing or '
    'listing or liking a title is a strong positive signal, stopping early, dismissing or disliking is a negative one, and searches show intent. '
    'When the household has stated languages, genres, likes or dislikes, respect them. '
    'Predict the next several titles this household will choose to play from the candidate list - not just the single most '
    'likely next one, so cover their main languages and moods and include one or two plausible stretches. '
    'New titles have no history; judge them from title, language, year and description.')


GENRES = {
    'Action': ['action', 'fight', 'war', 'mission', 'gangster', 'revenge', 'battle'],
    'Comedy': ['comedy', 'funny', 'hilarious', 'laugh', 'comic', 'quirky'],
    'Drama': ['drama', 'emotional', 'struggle', 'journey', 'life of'],
    'Romance': ['love', 'romance', 'romantic', 'wedding', 'marriage', 'couple'],
    'Thriller & Crime': ['thriller', 'murder', 'crime', 'police', 'investigat', 'detective', 'mystery', 'killer', 'heist'],
    'Horror': ['horror', 'ghost', 'haunted', 'spirit', 'paranormal', 'supernatural'],
    'Family': ['family', 'father', 'mother', 'children', 'brother', 'sister'],
    'Sci-fi & Fantasy': ['sci-fi', 'future', 'alien', 'fantasy', 'magic', 'myth', 'superhero'],
    'Reality & Talk': ['reality', 'contest', 'host', 'talk show', 'game show'],
    'Devotional & Mythology': ['god', 'goddess', 'divine', 'devotion', 'temple', 'mytholog'],
}


def _profile_lines():
    """What the viewer told us directly: languages, genres, liked and disliked titles."""
    profile = db.meta('profile') or {}
    lines = []
    if profile.get('languages'):
        lines.append('Languages they chose: ' + ', '.join(profile['languages']))
    if profile.get('genres'):
        lines.append('Genres they chose: ' + ', '.join(profile['genres']))
    liked = [_describe(e)[:100] for e in db.rated(1)[:25]]
    disliked = [_describe(e)[:100] for e in db.rated(-1)[:25]]
    if liked:
        lines.append('Titles they marked as liked: ' + '; '.join(liked))
    if disliked:
        lines.append('Titles they marked as disliked (avoid similar): ' + '; '.join(disliked))
    return lines


def _local_order(pool):
    """No AI key: score by chosen genres (keywords in the description), then spread across languages and types."""
    profile = db.meta('profile') or {}
    words = [w for g in profile.get('genres', []) for w in GENRES.get(g, [])]
    liked_groups = {(e.get('group') or '').lower() for e in db.rated(1)}

    def score(entry):
        text = (entry['item']['name'] + ' ' + entry['item'].get('plot', '')).lower()
        return sum(1 for w in words if w in text) + (1 if (entry.get('group') or '').lower() in liked_groups else 0)

    buckets = {}
    for entry in sorted(pool, key=lambda e: -score(e)):
        buckets.setdefault((entry.get('group'), entry.get('kind')), []).append(entry)
    return [e for group in zip_longest(*buckets.values()) for e in group if e]


def recommend(top_groups, force=False):
    """Build and store a new batch of suggestions when the last one is stale. Returns True if a batch was saved."""
    plays = db.history_count()
    last = db.meta('recs', {})
    stale = (time.time() - last.get('at', 0) > REFRESH_AFTER or plays - last.get('plays', 0) >= 3
             or db.meta('recs_dirty', False))
    if not (force or stale):
        return False
    pool = db.only_chosen(db.candidates(400, top_groups), lambda e: e.get('group'))[:120]
    if not pool:
        return False
    pool = _local_order(pool)
    timeline = _timeline()
    watched = db.recent(30)
    stated = _profile_lines()
    if timeline:
        intro = 'Interaction history, oldest first:\n' + '\n'.join(timeline)
        goal = 'The title this household plays next. Recent activity: ' + '; '.join(timeline[-12:])
    elif watched:
        intro = 'The viewer recently watched (most recent first):\n' + '\n'.join('- ' + _describe(w) for w in watched)
        goal = 'A title this viewer will enjoy next. They recently watched: ' + '; '.join(_describe(w)[:90] for w in watched)
    else:
        intro = 'There is no history yet. Favour widely appealing, well-regarded titles across the main languages.'
        goal = 'A widely appealing, well-regarded recent title'
    if stated:
        intro = 'What the household told us:\n' + '\n'.join('- ' + line for line in stated) + '\n\n' + intro
        goal += '. ' + ' '.join(stated)[:600]
    picks, source = _pick(goal, RECOMMENDER, intro, pool, PICKS)
    if source == 'local':
        for p in picks:
            if p['entry'].get('group'):
                p['reason'] = 'New in {0}'.format(p['entry']['group'].title())
    db.save_suggestions(picks, source)
    db.set_meta('recs', {'at': time.time(), 'plays': plays, 'source': source})
    db.set_meta('recs_dirty', False)
    log('saved {0} suggestions via {1}'.format(len(picks), source))
    return True


def smart_search(query):
    """Natural-language search over the local index. Returns [{'entry', 'reason'}] or [] when AI is off."""
    if not (has_llm() or has_jev()):
        return []
    pool = db.candidates(150)
    for word in [w for w in re.findall(r'\w+', query) if len(w) > 3][:4]:
        for hit in db.find_titles(word, 20):
            if all(hit['item']['name'] != p['item']['name'] for p in pool):
                pool.append(hit)
    if not pool:
        return []
    system = 'You match a viewer\'s request to titles in a fixed catalogue. Only pick titles that genuinely fit.'
    picks, source = _pick('A title that fits this request: ' + query, system, 'The viewer asked for: ' + query, pool, 12)
    return picks if source != 'local' else []
