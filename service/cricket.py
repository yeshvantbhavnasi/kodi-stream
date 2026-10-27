"""Cricket fixtures for the boxes' "Cricket: Live and Upcoming" row: every current series from ESPN's public
scoreboard feed (no key needed), with each match's teams, start time, state (scheduled, live, finished) and status
line. recs_server.py refreshes it every 30 minutes and serves GET /cricket; /context carries the next two days.
Standard library only."""
import json
import time
import urllib.request

URL = 'https://site.web.api.espn.com/apis/v2/scoreboard/header?sport=cricket'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
# Series everyone wants: the big tournaments and leagues. A match with India's national side counts as major too.
MAJOR = ('indian premier league', 'ipl', 'world cup', 'asia cup', 'champions trophy', 'world test championship', 'the hundred',
         'big bash', 'pakistan super league', 'caribbean premier league', 'sa20', 'major league cricket', 'women\'s premier league')
INDIA = ('india', 'ind')
# Series that sound major but are not: qualifiers, second-tier leagues, age-group and 'A' tours.
MINOR = ('league 2', 'challenge league', 'qualifier', 'under-19', 'under 19', 'u19', 'u-19', 'india a', ' a tour', 'emerging')
KEEP_DAYS = 10


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _stamp(text):
    """Seconds since the epoch from '2026-10-27T07:00:00Z'; 0 when unreadable."""
    try:
        return int(time.mktime(time.strptime(text[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone)
    except (TypeError, ValueError):
        return 0


def build(log=print):
    """{'at', 'matches': [...]}: live and upcoming matches in the next KEEP_DAYS days, live first, then by start."""
    data = _get(URL)
    now = time.time()
    matches = []
    for sport in data.get('sports') or []:
        for league in sport.get('leagues') or []:
            series = league.get('name') or ''
            short = league.get('shortName') or series
            for event in league.get('events') or []:
                state = ((event.get('fullStatus') or {}).get('type') or {}).get('state') or event.get('status') or ''
                start = _stamp(event.get('date') or '')
                if state == 'post' or not start or start > now + KEEP_DAYS * 86400:
                    continue
                if state == 'pre' and start < now - 6 * 3600:
                    continue
                teams = [c.get('displayName') or c.get('name') or '' for c in event.get('competitors') or []]
                logos = [c.get('logo') or '' for c in event.get('competitors') or []]
                abbr = [c.get('abbreviation') or c.get('name') or '' for c in event.get('competitors') or []]
                national = any(c.get('isNational') for c in event.get('competitors') or [])
                india = any(t.lower() in INDIA for t in teams + abbr)
                minor = any(w in series.lower() for w in MINOR)
                major = not minor and (india or any(w in series.lower() for w in MAJOR))
                status = (event.get('fullStatus') or {}).get('type') or {}
                matches.append({'id': str(event.get('id') or ''), 'series': series, 'short': short, 'tournament': bool(league.get('isTournament')),
                                'name': event.get('name') or ' v '.join(teams), 'abbr': ' v '.join(abbr), 'teams': teams,
                                'title': event.get('title') or '', 'start': start, 'end': _stamp(event.get('endDate') or ''),
                                'state': state, 'status': status.get('shortDetail') or status.get('detail') or '',
                                'summary': (event.get('fullStatus') or {}).get('summary') or event.get('summary') or '',
                                'national': national, 'india': india, 'major': major, 'minor': minor, 'venue': event.get('location') or '',
                                'logos': logos, 'logo': next((l for t, l in zip(teams + abbr, logos + logos) if l and t.lower() in INDIA), '') or next((l for l in logos if l), '')})
    # India's matches first, then whatever is live, then the big series, then the rest by start time
    matches.sort(key=lambda m: (not m['india'], m['state'] != 'in', not m['major'], not m['national'], m['start']))
    log('cricket: {0} live or upcoming matches'.format(len(matches)))
    return {'at': int(now), 'matches': matches[:200]}


def soon(fixtures, hours=48, limit=20):
    """The matches a bot or a box cares about: live now, or starting within `hours`; major ones first."""
    now = time.time()
    out = [m for m in (fixtures or {}).get('matches') or [] if (m['major'] or (m['national'] and not m.get('minor')))
           and (m['state'] == 'in' or 0 <= m['start'] - now <= hours * 3600)]
    return out[:limit]
