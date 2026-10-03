# Stream for Kodi

A poster-row home screen for Kodi that sits on top of the Sasta TV addon: tabs for Movies, Shows, Live TV and Kids,
Continue Watching, My List, ranked search, and a "Recommended for You" row.

Stream does not provide any content or accounts. It only re-presents what the Sasta TV addon already lists, and hands
playback to that addon, so Sasta TV must be installed and signed in on the same Kodi.

## Install

1. In Kodi, open **Settings → System → Add-ons** and turn on **Unknown sources**.
2. Download the repository installer to the device:
   `https://github.com/yeshvantbhavnasi/kodi-stream/raw/main/repository.stream-1.0.0.zip`
   (on a Fire TV, the Downloader app can fetch this address).
3. In Kodi, open **Add-ons → Install from zip file** and choose the downloaded file.
4. Open **Add-ons → Install from repository → Stream Repository → Program add-ons → Stream** and install it.

Updates then arrive through the repository.

## Using it

| Action | How |
|---|---|
| Move around | Arrow keys or the remote's direction pad; OK opens or plays |
| Go back one level | Back: rows → tabs → Home tab → exit |
| Jump to Home from any inner screen | Choose the **⌂ Home** tile at the start of the list, or hold OK and pick **Go to Home** |
| Add or remove a title from My List | Hold OK (or press the menu key) on the title and pick **Add to My List** / **Remove from My List** |
| Hide a suggestion | Hold OK on it in "Recommended for You" and pick **Not interested** |
| Resume a title | Play it again; Stream offers **Resume from…** or **Start from the beginning** |
| Search | **Search** tab; a request of three or more words also asks the AI when a key is set |
| Enter AI keys, refresh or clear data | **Settings** tab |

## How it works

### Where the catalogue comes from

Stream asks Kodi to list the Sasta TV addon's folders (`Files.GetDirectory`) and sorts each entry into a type: folder,
playable title, next page, search box or year filter. Only catalogue folders are ever opened; the Sasta TV addon's own
actions (settings, cache clearing, log upload) are never called.

Listings are cached on the device (`cache.json`). A cached copy is shown immediately and refreshed in the background
once it is older than 30 minutes, so screens open quickly.

To play something, Stream builds the same link the Sasta TV addon uses for that title and asks Kodi to open it. The
Sasta TV addon then starts the player exactly as if the title had been clicked inside it.

### The database

Everything Stream remembers lives in one SQLite file, `stream.db`, in Kodi's addon data folder for `script.stream`.

| Table | What it holds | Written when |
|---|---|---|
| `titles` | The catalogue index: name, language, type, description, poster and where it was listed | A movie or show listing is fetched, and once a day for the first page of every main section |
| `history` | One row per title played: last played time, play count, position and duration | A title starts playing; position is updated every 5 seconds during playback |
| `mylist` | Titles saved to My List | **Add to My List** / **Remove from My List** |
| `suggestions` | Every batch of recommendations: rank, title, reason, which engine produced it, dismissed flag | A recommendation run finishes; **Not interested** sets the dismissed flag |
| `events` | The interaction log: played, stopped (with percent watched), finished, listed, unlisted, dismissed, searched | Each of those actions happens |
| `meta` | Small bookkeeping values, such as when recommendations last ran | As needed |

### How recent views reach the screen

1. You press OK on a title. Stream adds or updates its row in `history` (time played, play count) and writes a
   `played` event.
2. While it plays, a background watcher reads the player's position every 5 seconds and saves it to that `history`
   row. Live channels are not tracked this way, since they have no position.
3. When playback stops, Stream writes a `stopped` event with the percent watched, or `finished` at 90% or more.
4. The next time the Home tab loads, it queries the database:
   - **Continue Watching** shows `history` rows stopped after the first minute and before the last 5%.
   - **Recently Played** shows the 30 most recent `history` rows, newest first.
5. Languages you play most are counted (`state.json`) and their rows move to the top of each tab.

### How My List works

Holding OK on a title opens a small menu. **Add to My List** stores the title in the `mylist` table and writes a
`listed` event; **Remove from My List** deletes the row and writes `unlisted`. The **My List** tab reads that table,
newest first. Saved live channels are looked up again when played, because their links expire.

### How recommendations work

A new batch is built in the background when the addon opens, if the last batch is more than 12 hours old or three or
more titles have been played since. It can also be forced from **Settings → Refresh recommendations now**. Each batch
is saved to `suggestions`, and the Home tab shows the latest batch minus anything dismissed or already played.

Every run starts the same way: Stream takes up to 120 candidate titles from `titles` that you have not played or
dismissed, preferring your most-played languages. Kids titles are left out of the pool. What happens next depends on
which keys are set.

**Without any AI key.** Stream takes turns across languages and types (for example Hindi movies, Telugu movies, Hindi
web series) so the row is varied, and picks the first 15. Each is labelled "New in <language>". Nothing leaves the
device.

**With a Bedrock key (Claude).** Claude is the recommendation engine. Stream sends it:

- your interaction history as a time-ordered list of events, one per line, for example
  `2026-10-03 Sat evening | stopped | Sardar 2 (2026) [Telugu, Movies] | watched 85%`;
- the candidate list, each with name, language and a short description.

Claude is asked to act like a sequence model: weigh recent events most, treat finishing or listing a title as a strong
positive and stopping early or dismissing as a negative, and predict the next several titles you would choose to play
rather than only the single most likely one. It returns 15 picks, each with a one-sentence reason that is shown under
the title.

This borrows the idea behind Netflix's recommendation foundation model, which treats each member's history as a
sequence of rich interaction events and predicts upcoming interactions. Netflix trains its own model on billions of
events; one household has far too little data for that, so Stream gives the event sequence to a general model instead.

**With a Jev key as well.** Jev first scores the candidates against a summary of your recent activity and the best 60
go to Claude, so Claude chooses from a stronger shortlist. With only a Jev key, the top 15 by Jev's score are used.

If a key is wrong or a service is unreachable, that step is skipped and the run falls back to the next simpler method,
so the row is always filled. The Settings tab shows which engine produced the last batch.

### How search works

A search runs the query against every section of the catalogue that has a search box (each movie and web-series
language, English titles, kids). The results are merged into one list and ordered by:

1. how closely the title matches (exact title, then starts with the query, then contains it as a word);
2. your most-played languages;
3. release year, newest first.

When a key is set and the query is three or more words (for example "feel-good Telugu family comedy"), Stream also
asks the AI to pick matching titles from the local index. Those appear first, marked "Best match" with a reason.

### What leaves the device

| Situation | Data sent | To |
|---|---|---|
| No AI keys | Nothing beyond what the Sasta TV addon itself requests | - |
| Bedrock key set | Your interaction events, candidate titles and descriptions, search requests of three or more words | Amazon Bedrock, in the region you choose |
| Jev key set | A summary of recent activity or the search request, plus candidate titles and descriptions | TypeSafe's Jev service |

Keys are stored as plain text in Kodi's addon settings on that device and are never written to Kodi's log.

## AI settings

Open **Stream → Settings → AI keys and options**.

| Setting | Meaning | Default |
|---|---|---|
| Use AI for recommendations and search | Master switch | On |
| Amazon Bedrock API key | A Bedrock API key (bearer token), not AWS access keys | empty |
| Bedrock region | Region the request is sent to | `us-east-1` |
| Bedrock model ID | Which Claude model to use | `anthropic.claude-opus-5-5` |
| Jev (TypeSafe) API key | Optional pre-ranking | empty |

## Development

| Path | Purpose |
|---|---|
| `script.stream/default.py` | The two screens (home rows and grid), navigation, playback, menus |
| `script.stream/resources/lib/catalogue.py` | Reading and caching Sasta TV listings, building play links, search ranking |
| `script.stream/resources/lib/db.py` | The SQLite store |
| `script.stream/resources/lib/ai.py` | Recommendations and smart search (Bedrock and Jev calls) |
| `script.stream/resources/skins/Default/` | Screen layouts and images |
| `repository.stream/` | The repository installer addon |
| `zips/` | Generated files Kodi downloads; do not edit by hand |

To release a change: edit the addon, raise the version in `script.stream/addon.xml`, run `python3 build.py`, then
commit and push.
