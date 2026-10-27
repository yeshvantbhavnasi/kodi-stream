# Stream recommendation service

The backend for [Stream](https://github.com/yeshvantbhavnasi/kodi-stream), a poster-row home screen for Kodi. One
small HTTP server, standard library only, that runs on a machine in the home (a Mac mini under launchd here) and:

- asks a local model (Gemma through Ollama) to pick titles for each TV box and profile, keeps the last answer per
  profile and rebuilds it in the background, so a box gets its "Recommended for You" row at once;
- takes lists of titles from other bots or people (`POST /lists`), "titles like this" lists (`POST /related`), a
  Top 20 chart (`POST /top10`) and good/bad verdicts from its web page, and hands them to the boxes;
- gathers what every box reports (its rows, its recent plays, its catalogue) into `GET /context`, so a bot can
  pick titles the catalogue actually holds;
- scrapes India's streaming charts (Netflix, Prime Video, Aha, TMDb) twice a day and ESPN's cricket fixtures every
  half hour;
- serves a web page with one tab per box: the current picks, the reasons and recent searches.

Everything it remembers lives in one SQLite file (`STATE`, in WAL mode): the model's batches and their history,
feedback, posted lists, related titles, each box's rows and catalogue, and the chart and cricket caches.

## Files

| File | What it is |
|---|---|
| `recs_server.py` | The server. The docstring at the top lists every endpoint and setting |
| `charts.py` | India's streaming charts, merged into one ranking (`GET /top10`) |
| `cricket.py` | Live and upcoming cricket from ESPN's public feed (`GET /cricket`) |
| `BOT_INSTRUCTIONS.md` | What to tell a bot that posts lists: the household, the context it should read, the APIs for devices, recommendations and the Top 20 chart |
| `com.yeshvant.stream-recs.plist` | The launchd agent that keeps the service running on the Mac |
| `com.yeshvant.cloudflared.plist` | The launchd agent for the Cloudflare tunnel that publishes it as `https://recs.senapati.si` |

## Running it

```
PORT=8770 BIND=127.0.0.1,<tailscale address> OLLAMA_URL=http://127.0.0.1:11434 OLLAMA_MODEL=gemma3:4b \
STATE=~/Library/Application\ Support/stream-recs/batches.db TOKEN=<service key> POST_PIN=<posting pin> \
python3 recs_server.py
```

| Setting | Meaning |
|---|---|
| `PORT`, `BIND` | Port (8770) and the addresses to listen on, comma separated (127.0.0.1 by default) |
| `OLLAMA_URL`, `OLLAMA_MODEL` | Where Ollama answers and which model to ask |
| `STATE` | The SQLite file. Its folder is created; the JSON files an earlier version kept there are taken over once |
| `TOKEN` | When set, every request except `/health` must carry it (`X-Stream-Key` header or `?key=`) |
| `POST_PIN` | What a bot must send to post lists, charts and related titles (`?pin=`, `X-Stream-Pin` or `"pin"` in the body); the TOKEN when unset |
| `EMBED_ORIGINS` | Pages allowed to show the web page in a frame |
| `TMDB_KEY` | TMDb key for the charts' language lookup (the add-on's built-in key by default) |

On the Mac the launch agent runs it: copy the plist to `~/Library/LaunchAgents`, fill in the key and pin, and
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yeshvant.stream-recs.plist`. After a code change:

```
launchctl kickstart -k gui/$(id -u)/com.yeshvant.stream-recs
```

The log is `~/Library/Logs/stream-recs.log`. To try a change without touching the live service, start a second copy
on another port with its own `STATE` (`PORT=8771 STATE=/tmp/recs-test/batches.db python3 recs_server.py`).

## Endpoints

Read the docstring in `recs_server.py` for the full list and the shapes. In short:

| Call | Who | What |
|---|---|---|
| `POST /pick` | a box | Ask for picks (recommend or search) from the model; answered from the kept batch, rebuilt in the background |
| `POST /rows`, `POST /catalogue` | a box | What the box is showing and what its catalogue holds |
| `GET /lists?profile=`, `GET /top10`, `GET /cricket` | a box | The posted lists (and related titles), the chart, the fixtures |
| `GET /context` | a bot | Everything a bot needs before posting: the current list, each profile's history and candidates, the catalogue, what wants a related list |
| `GET /devices`, `GET /recs?profile=` | a bot | The boxes the service has heard from; what each is being recommended |
| `POST /lists`, `DELETE /lists?name=` | a bot | A list of titles for every box or one profile, shown as a row |
| `POST /related`, `DELETE /related?title=` | a bot | Titles like one title, for More like this and Because you watched |
| `POST /top10`, `DELETE /top10` | a bot | A Top 20 chart in place of the scraped one, held for a day by default |
| `POST /feedback`, `GET /feedback` | the web page | Good or bad verdicts on picks |
| `GET /`, `GET /text`, `GET /batches`, `GET /history` | a person | The web page, the plain-text report, the raw batches and how they moved |
| `GET /health` | anyone | Liveness and whether Ollama answers |
