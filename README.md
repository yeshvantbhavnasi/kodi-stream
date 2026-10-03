# Stream for Kodi

A poster-row home screen for Kodi that sits on top of the Sasta TV addon: tabs for Movies, Shows, Live TV and Kids,
Continue Watching, My List, ranked search, and a "Recommended for You" row.

Stream does not provide any content or accounts. It only re-presents what the Sasta TV addon already lists, and hands
playback to that addon, so Sasta TV must be installed and signed in on the same Kodi.

## Install

1. In Kodi, open **Settings → System → Add-ons** and turn on **Unknown sources**.
2. Open **Settings → File manager → Add source** and enter `https://yeshvantbhavnasi.github.io/kodi-stream/`. Name it `stream`.
3. Open **Add-ons → Install from zip file → stream** and choose `repository.stream-1.0.0.zip`.
4. Open **Add-ons → Install from repository → Stream Repository → Program add-ons → Stream** and install it.

Updates then arrive through the repository.

## AI recommendations (optional)

Everything works without keys. To have Claude choose the recommendations, open **Stream → Settings → AI keys and options**
and enter an Amazon Bedrock API key. A Jev (TypeSafe) key can be added to pre-rank candidates. Keys are stored only in
Kodi's addon data on that device.

## Development

Edit the addon under `script.stream/`, raise the version in its `addon.xml`, then run `python3 build.py` and commit.
