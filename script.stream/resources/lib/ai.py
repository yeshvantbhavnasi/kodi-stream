# Recommendations and smart search. The picks come from Stream's own recommendation service on a machine in the home
# (service/recs_server.py, which keeps a batch ready per profile), or straight from a local model served by Ollama
# (Gemma by default), or from Claude on Amazon Bedrock. Keys come from the addon settings and are never logged.
# Kodi's Python cannot install the Anthropic SDK, so Bedrock is called over plain HTTPS.
import json
import re
import time
from itertools import zip_longest

import xbmc
import xbmcaddon

import db

JEV_URL = 'https://api.typesafe.ai/v1/systemone'
OLLAMA_MODEL = 'gemma3:4b'
REFRESH_AFTER = 3600       # a batch older than this is rebuilt while Stream is open
PENDING_AFTER = 180        # when the service is rebuilding in the background, ask again this soon
LAST = {'building': False, 'model': ''}  # what the service said after the last request: still rebuilding, and which model
PICKS = 15


def _setting(key):
    return xbmcaddon.Addon('script.stream').getSetting(key).strip()


def enabled():
    return _setting('ai_enabled') != 'false'


def ollama_url():
    """The Ollama server's address (blank in the settings switches it off)."""
    return _setting('ollama_url').rstrip('/')


def service_url():
    """Stream's recommendation service (blank in the settings switches it off)."""
    return _setting('recs_url').rstrip('/')


def provider():
    """'service', 'ollama', 'bedrock' or '' for what answers."""
    if not enabled():
        return ''
    return 'service' if service_url() else 'ollama' if ollama_url() else 'bedrock' if _setting('bedrock_token') else ''


def profile_key():
    """This box and profile, as the service files its batches: 'Kodi (Mac) 52c8d4aa36d7/default'. The install id keeps
    two boxes with the same name apart, and each profile on a box has its own batch."""
    try:
        import audit
        install = audit._install_id()
    except Exception:
        install = ''
    return '{0} {1}/{2}'.format(xbmc.getInfoLabel('System.FriendlyName') or 'kodi', install, getattr(db, 'ACTIVE', 'default')).strip()


def has_llm():
    return bool(provider())


def pretty_model(name):
    """'gemma3:4b' -> 'Gemma 3 4B'; anything else as given."""
    m = re.match(r'^gemma(\d+)(?:[.:\-]?n?)?[:\-]?(\d+b)?', name or '')
    if m:
        return 'Gemma ' + m.group(1) + (' ' + m.group(2).upper() if m.group(2) else '')
    return name or ''


def engine_note():
    """'  ·  Gemma 3 4B via yeshvants-mac-mini, 09:44' for the Recommended row's title: which model built the current
    batch, where, and when; 'updating…' is added while the service is rebuilding. Empty without a model."""
    last = db.meta('recs', {})
    source = last.get('source') or ''
    if not last.get('at') or source in ('', 'local', 'jev'):
        return ''
    if source.startswith('bedrock') or source.startswith('llm'):
        model, via = 'Claude', ''
    else:
        model = pretty_model(last.get('model') or _setting('ollama_model') or OLLAMA_MODEL)
        host = re.sub(r'^https?://', '', service_url() if source.startswith('service') else ollama_url()).split(':')[0].split('.')[0]
        via = ' via ' + host if host and host not in ('127', 'localhost') else ''
    when = time.strftime('%H:%M', time.localtime(last['at']))
    return '  \u00b7  {0}{1}, {2}{3}'.format(model, via, when, '  \u00b7  updating\u2026' if last.get('pending') else '')


def engine_name(source):
    """What produced a batch of suggestions, for the settings tiles."""
    names = {'service': 'the recommendation service', 'ollama': (_setting('ollama_model') or OLLAMA_MODEL) + ' on Ollama',
             'bedrock': 'Claude', 'llm': 'Claude', 'jev': 'Jev'}
    return ' + '.join(names.get(part, part) for part in (source or '').split('+')) if source else ''


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


def ask_ollama(system, prompt, max_tokens=4000):
    """One chat request to a model served by Ollama, asked to answer in JSON."""
    import requests
    body = {'model': _setting('ollama_model') or OLLAMA_MODEL, 'stream': False, 'format': 'json', 'keep_alive': '30m',
            'options': {'temperature': 0.2, 'num_ctx': 12288, 'num_predict': max_tokens},
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]}
    r = requests.post(ollama_url() + '/api/chat', json=body, timeout=300)
    if r.status_code != 200:
        raise RuntimeError('Ollama returned HTTP {0}: {1}'.format(r.status_code, r.text[:200]))
    return (r.json().get('message') or {}).get('content', '')


def ask(system, prompt, max_tokens=4000):
    """One question to whichever model is set up: Ollama first, otherwise Claude on Bedrock."""
    if provider() == 'ollama':
        return ask_ollama(system, prompt, max_tokens)
    return ask_claude(system, prompt, max_tokens)


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
    genres = _genres(item['name'])
    if genres:
        text += ' {' + ', '.join(genres[:3]) + '}'
    if item.get('plot'):
        text += ' - ' + item['plot'][:160]
    return text


LANGUAGES = ('telugu', 'tamil', 'hindi', 'malayalam', 'kannada', 'marathi', 'gujarati', 'punjabi', 'bengali', 'english')


def ask_service(kind, system, prompt_intro, candidates, count):
    """Picks from the recommendation service: [{'name', 'reason'}]. A recommendation request gets the last batch for
    this profile straight away while the service rebuilds in the background; a search waits for its answer."""
    import requests
    body = {'profile': profile_key(), 'kind': kind, 'system': system, 'intro': prompt_intro, 'count': count,
            'candidates': candidates}
    r = requests.post(service_url() + '/pick', json=body, timeout=240, headers={'X-Stream-Key': _setting('recs_key')})
    if r.status_code != 200:
        raise RuntimeError('service returned HTTP {0}: {1}'.format(r.status_code, r.text[:200]))
    reply = r.json()
    if kind == 'recommend':
        LAST['building'] = bool(reply.get('building'))
    LAST['model'] = reply.get('model') or LAST['model']
    log('service answered {0} ({1}, {2}s{3})'.format(kind, 'fresh' if reply.get('fresh') else 'last batch',
                                                    reply.get('took', 0), ', rebuilding' if reply.get('building') else ''))
    return reply.get('picks', [])


def _pick(goal_for_jev, system, prompt_intro, pool, count, shortlist_size=60, kind='recommend'):
    """Shared pipeline: Jev narrows the pool, the model picks and explains. Either step may be absent."""
    ids = {'c{0}'.format(n): e for n, e in enumerate(pool)}
    order = list(ids)
    source = 'local'
    if has_jev():
        try:
            order = jev_rank(goal_for_jev, [(cid, _describe(e)) for cid, e in ids.items()])
            source = 'jev'
        except Exception as e:
            log('Jev ranking skipped: {0}'.format(e))
    if provider() == 'service':
        shortlist = order[:shortlist_size]
        try:
            picks = ask_service(kind, system, prompt_intro, [{'id': cid, 'name': ids[cid]['item']['name'], 'text': _describe(ids[cid])}
                                                             for cid in shortlist], count)
            by_name = {ids[cid]['item']['name']: ids[cid] for cid in shortlist}
            chosen = [{'entry': by_name[p['name']], 'reason': str(p.get('reason', ''))[:160]} for p in picks if p.get('name') in by_name]
            if chosen:
                return chosen[:count], 'service' + ('+jev' if source == 'jev' else '')
        except Exception as e:
            log('service step skipped: {0}'.format(e))
    elif has_llm():
        shortlist = order[:shortlist_size]
        listing = '\n'.join('{0}: {1}'.format(cid, _describe(ids[cid])) for cid in shortlist)
        prompt = ('{0}\n\nCandidates:\n{1}\n\nPick the {2} best candidates, best first. Reply with JSON only, shaped as '
                  '{{"picks": [{{"id": "c0", "reason": "why, for the viewer, in at most ten words"}}]}}. Use only ids from the list.'
                  ).format(prompt_intro, listing, count)
        try:
            started = time.time()
            picks = _parse_json(ask(system, prompt)).get('picks', [])
            chosen = [{'entry': ids[p['id']], 'reason': str(p.get('reason', ''))[:160]} for p in picks if p.get('id') in ids]
            log('{0} picked {1} of {2} in {3:.1f}s'.format(provider(), len(chosen), len(shortlist), time.time() - started))
            if chosen:
                return chosen[:count], provider() + ('+jev' if source == 'jev' else '')
        except Exception as e:
            log('{0} step skipped: {1}'.format(provider(), e))
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


def _genres(name):
    """Genres already looked up for a title; empty when it has not been looked up."""
    cached = db.get_metadata(name)
    return (cached[0] or {}).get('genres', []) if cached else []


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


def priority_languages():
    """Languages in the order recommendations favour them: the ones chosen at setup, first language leading."""
    return list((db.meta('profile') or {}).get('languages', []))


def watched_genres(limit=4):
    """The genres this household plays and likes most, from the looked-up details of recent and liked titles."""
    taste = {}
    for entry in db.recent(40) + db.rated(1):
        for genre in _genres(entry.get('series') or entry['item']['name']):
            taste[genre] = taste.get(genre, 0) + 1
    return [g for g, _ in sorted(taste.items(), key=lambda p: -p[1])[:limit]]


def _lang_rank(entry, order):
    group = (entry.get('group') or '').lower()
    for n, lang in enumerate(order):
        if db.lang_match(group, [lang.lower()]):
            return n
    return len(order)


def _by_priority(pool, order, size=60):
    """Arrange the pool so the shortlist the model sees is weighted by language: about half from the first language,
    a third from the second, the rest from the others, each part in the pool's own order."""
    if not order:
        return pool
    shares = [size // 2, size * 3 // 10] + [size // 5] * max(0, len(order) - 2)
    chosen, taken = [], set()
    for n, lang in enumerate(order):
        for e in [e for e in pool if _lang_rank(e, order) == n][:shares[n]]:
            chosen.append(e)
            taken.add(e['item']['name'])
    return chosen + [e for e in pool if e['item']['name'] not in taken]


def _balance(picks, pool, order):
    """Hold the model to the language shares: about half the row from the first language, a third from the second.
    A language the model under-served is topped up from the best-scored candidates in that language, each marked
    with its genres, so the row always carries the mix the household asked for."""
    shares = [PICKS // 2, PICKS * 3 // 10] + [PICKS // 5] * max(0, len(order) - 2)
    chosen = {p['entry']['item']['name'] for p in picks}
    out, taken = [], set()
    for n, lang in enumerate(order):
        mine = [p for p in picks if _lang_rank(p['entry'], order) == n][:shares[n]]
        for e in pool:
            if len(mine) >= shares[n]:
                break
            if _lang_rank(e, order) == n and e['item']['name'] not in chosen and e['item']['name'] not in taken:
                genres = _genres(e['item']['name'])[:2]
                mine.append({'entry': e, 'reason': (', '.join(genres) + ' · in ' + lang) if genres else 'New in ' + lang})
        for p in mine:
            if p['entry']['item']['name'] not in taken:
                out.append(p)
                taken.add(p['entry']['item']['name'])
    for p in picks:  # whatever the model picked beyond the shares, in its order
        if p['entry']['item']['name'] not in taken and len(out) < PICKS:
            out.append(p)
            taken.add(p['entry']['item']['name'])
    return out[:PICKS]


def _local_order(pool):
    """No AI key: score by chosen genres (keywords in the description), then spread across languages and types."""
    profile = db.meta('profile') or {}
    words = [w for g in profile.get('genres', []) for w in GENRES.get(g, [])]
    liked_groups = {(e.get('group') or '').lower() for e in db.rated(1)}

    # Genres of what was watched or liked (from looked-up details) pull similar titles up the list.
    taste = {}
    for entry in db.recent(30) + db.rated(1):
        for genre in _genres(entry.get('series') or entry['item']['name']):
            taste[genre] = taste.get(genre, 0) + 1

    def score(entry):
        text = (entry['item']['name'] + ' ' + entry['item'].get('plot', '')).lower()
        match = sum(min(taste.get(g, 0), 3) for g in _genres(entry['item']['name']))
        return match + sum(1 for w in words if w in text) + (1 if (entry.get('group') or '').lower() in liked_groups else 0)

    buckets = {}
    for entry in sorted(pool, key=lambda e: -score(e)):
        buckets.setdefault((entry.get('group'), entry.get('kind')), []).append(entry)
    return [e for group in zip_longest(*buckets.values()) for e in group if e]


def recommend(top_groups, force=False):
    """Build and store a new batch of suggestions when the last one is stale. Returns True if a batch was saved."""
    plays = db.history_count()
    last = db.meta('recs', {})
    age = time.time() - last.get('at', 0)
    stale = (age > REFRESH_AFTER or plays - last.get('plays', 0) >= 3 or db.meta('recs_dirty', False)
             or (last.get('pending') and age > PENDING_AFTER))
    if not (force or stale):
        return False
    # Every unplayed title is fetched (the first language alone can hold hundreds), so the language shares below
    # are taken from the whole index rather than from whatever a cut by preference happened to leave.
    pool = db.candidates(2000, top_groups)
    if not db.KIDS:
        pool = db.only_chosen(pool, lambda e: e.get('group'))
    order = priority_languages()
    genres = watched_genres()
    # Language shares are applied before the pool is cut, so the second language is not crowded out by the first.
    pool = _by_priority(pool, order, 120)[:120]
    if not pool:
        return False
    pool = _by_priority(_local_order(pool), order)
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
    if order or genres:
        rules = []
        if order:
            shares = [PICKS // 2, PICKS * 3 // 10] + [PICKS // 5] * max(0, len(order) - 2)
            want = ', '.join('about {0} from {1}'.format(n, lang) for n, lang in zip(shares, order[:3]))
            rules.append('favour titles in ' + ', then '.join(order) + ', in that order: ' + want + (', the rest from the other languages' if len(order) > 3 else ''))
        if genres:
            rules.append('within a language prefer the genres they watch most: ' + ', '.join(genres))
        intro = 'Priorities: ' + '; '.join(rules) + '.\n\n' + intro
    system = RECOMMENDER + (' This is a children\'s profile: every pick must be suitable for young children.' if db.KIDS else '')
    picks, source = _pick(goal, system, intro, pool, PICKS)
    if order and source != 'local':
        # A language the model passed over gets its own, smaller question with only that language's titles offered.
        shares = [PICKS // 2, PICKS * 3 // 10] + [PICKS // 5] * max(0, len(order) - 2)
        for n, lang in enumerate(order[:2]):
            have = [p for p in picks if _lang_rank(p['entry'], order) == n]
            mine = [e for e in pool if _lang_rank(e, order) == n]
            if len(have) < shares[n] and mine:
                extra, _ = _pick(goal, system, 'Only titles in {0} are offered here; pick the ones this household would play.\n\n'.format(lang) + intro,
                                 mine, shares[n] - len(have), 40, kind='recommend-' + lang.lower())
                names = {p['entry']['item']['name'] for p in picks}
                picks += [p for p in extra if p['entry']['item']['name'] not in names]
        picks = _balance(picks, pool, order)
        picks.sort(key=lambda p: _lang_rank(p['entry'], order))  # stable: the model's order holds within a language
    if source == 'local':
        for p in picks:
            if p['entry'].get('group'):
                p['reason'] = 'New in {0}'.format(p['entry']['group'].title())
    changed = db.save_suggestions(picks, source)
    # 'pending' means the service handed back its last batch and is building a new one: ask again in a few minutes.
    db.set_meta('recs', {'at': time.time(), 'plays': plays, 'source': source, 'model': LAST['model'] or _setting('ollama_model') or OLLAMA_MODEL,
                         'pending': source.startswith('service') and LAST['building']})
    db.set_meta('recs_dirty', False)
    log('{0} {1} suggestions via {2}'.format('saved' if changed else 'kept', len(picks), source))
    return changed


def smart_search(query):
    """Natural-language search over the local index. Returns [{'entry', 'reason'}] or [] when AI is off."""
    if not (has_llm() or has_jev()):
        return []
    hits = []
    for word in [w for w in re.findall(r'\w+', query) if len(w) > 3][:4]:
        for hit in db.find_titles(word, 20):
            if all(hit['item']['name'] != p['item']['name'] for p in hits):
                hits.append(hit)
    pool = db.only_chosen(db.candidates(150, db.chosen_languages()), lambda e: e.get('group'))
    pool = hits + [p for p in pool if all(p['item']['name'] != h['item']['name'] for h in hits)]
    asked = [w for w in re.findall(r'\w+', query.lower()) if w in LANGUAGES]
    if asked:  # "telugu comedy": only that language's titles are offered
        pool = [p for p in pool if db.lang_match(p.get('group') or '', asked)] or pool
    if not pool:
        return []
    system = 'You match a viewer\'s request to titles in a fixed catalogue. Only pick titles that genuinely fit.'
    picks, source = _pick('A title that fits this request: ' + query, system, 'The viewer asked for: ' + query, pool, 8, 40, kind='search')
    return picks if source != 'local' else []
