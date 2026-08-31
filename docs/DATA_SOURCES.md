# Data sources

- **FPL API** (`fantasy.premierleague.com/api`) — prices, ownership %, points
  history, fixtures, birth dates, and FPL's own xG/xA/ICT/ep_next stats. No
  API key required. Also the source of **player news**: the `news` string is
  FPL's own summary of a club's press conference, and `src/fplquant/news/`
  parses it for the one thing `chance_of_playing_next_round` structurally
  cannot carry — when a player is due back. The text is templated rather than
  free-form (five shapes covered all 118 non-empty strings in the live pool on
  2026-08-31), and the parser is strict: wording it does not recognise produces
  no signal rather than a guess.

- **Public football news feeds** (BBC Sport, The Guardian, Sky Sports) — RSS,
  fetched daily by `fplquant-ingest-news` (`src/fplquant/news/feeds.py`),
  configurable via `FPLQUANT_NEWS_FEED_URLS`. **RSS deliberately, not page
  scraping**: a feed is published in order to be syndicated, so it is a contract
  rather than markup that changes without notice; it carries a stable per-item
  id and a timestamp, which is exactly the metadata needed and exactly what has
  to be reverse-engineered out of HTML; and it does not attract the bot-blocking
  that stops the Transfermarkt scrape running anywhere but a laptop. The client
  identifies the project honestly rather than impersonating a browser, and
  sleeps between requests.

  The value is narrow and specific: 47 of the 118 non-empty FPL news strings
  read "Unknown return date", and a press report of "out for six weeks" is the
  only estimate of when those players are back that exists anywhere. That is the
  *only* thing a feed is permitted to contribute.

  Entity resolution is the real risk here, not access. An item arrives as free
  text with no player id and has to be matched against a ~600-name pool full of
  ambiguous surnames, while availability is a *hard gate* on expected points.
  Four independent gates stand between an article and a projection
  (`news/resolve.py`, `news/extract.py`, `news/sources.py`,
  `news/availability.py`):

  1. **The match must be strong.** Longest match wins and claims its words, so
     a shorter name inside a longer one ("Gabriel" inside "Gabriel Jesus") never
     matches separately, and known non-player phrases — stadiums, competitions,
     club names — claim words the same way. A token that is somebody's first
     name never identifies a player alone, a rule derived from the pool itself
     rather than curated. A surname needs its club, with club names expanded so
     "Manchester United" corroborates a player FPL files under "Man Utd".
     Matching is over whole tokens, so "Sarr" does not find "Sarri". A short
     curated list covers surnames that are everyday English words ("Rice"),
     which is the one collision the data cannot detect on its own.
  2. **The text must carry a date.** "Back in training" is stored and displayed
     and consumed by nothing, because there is no date in it to build a recovery
     from. Durations are read at their upper bound, so estimates err late.
  3. **Confidence must clear `FPLQUANT_NEWS_MIN_MENTION_CONFIDENCE`** (0.8).
     Below it a mention still exists in the database and the API — a human
     reading a maybe-match is the right consumer for one — and cannot move a
     number.
  4. **FPL must already have ruled the player out without a date.** A report
     cannot change a category, cannot touch a fit player, cannot override FPL's
     own return date, and cannot contradict FPL about the next round.

  On top of that, `availability` floors every projection at FPL's published
  number, so even a wrong match at full confidence can only affect somebody
  already at zero and can only move them upward. `FPLQUANT_NEWS_FEEDS_FEED_THE_MODEL=false`
  keeps the ingest, the storage and the API while stopping consumption entirely.

  Articles and mentions are stored rather than consumed in flight, so anything
  this layer does to a projection is traceable to a URL. That is also what makes
  a rules change retroactive: `fplquant-ingest-news --reresolve` rebuilds every
  stored article's matches under the current rules, which is the only way to
  correct rows already written — the feeds will not carry those stories again.
  Nothing that serves a request touches the network.

- **Predicted-lineup and injury-aggregator sites** — still not pursued. These
  are HTML scraping rather than syndication, which is the fragility RSS avoids,
  and the ones checked either sit behind bot challenges or disallow automated
  crawlers by name in `robots.txt` (see the FBref and one-versus-one notes
  above).
- **Transfermarkt** (`transfermarkt.com`) — injury history (type, dates, days
  out, games missed). No official API — scraped via `TransfermarktClient`
  (`src/fplquant/data/transfermarkt_client.py`), identifying as a standard
  browser and rate-limited (~1.5s/request, configurable via
  `FPLQUANT_TRANSFERMARKT_REQUEST_DELAY_SECONDS`) to stay polite to their
  servers. Players are matched by fuzzy name + club similarity
  (`player_matching.py`); ambiguous/unmatched players are skipped rather than
  guessed at. Intended for personal, non-commercial analytics use — this is
  markup-scraping, not an API contract, so it may need adjustment if
  Transfermarkt changes their page structure.
- **FBref / StatsBomb open data** — investigated, not pursued. StatsBomb's
  open dataset (github.com/statsbomb/open-data) doesn't cover any recent
  Premier League season (last EPL coverage: 2015/16), so it can't enrich the
  current player pool. FBref has the right current-season data
  (progressive passes, etc.) but sits behind a Cloudflare bot challenge that
  blocks even a real headless browser — a deliberate anti-scraping measure,
  not light filtering, so it wasn't pursued further. A second candidate
  (one-versus-one.com) was technically scrapable but explicitly disallows
  `ClaudeBot` and most AI crawlers by name in `robots.txt`, so that wasn't
  pursued either. FPL's own `ict_index`/`creativity`/`threat`/`influence`
  and xG/xA remain the underlying-stats signal used throughout (form
  scoring, injury risk, similarity finder).

- **vaastav/Fantasy-Premier-League** (github.com/vaastav/Fantasy-Premier-League)
  — MIT-licensed archive of FPL's own API responses, season by season, going
  back to 2016/17. Used *only* to build training data for the learned minutes
  model (`fplquant-import-history`); nothing the app serves reads it, and it
  lands in its own table rather than alongside live-ingested rows — the element
  ids are not stable across seasons, so merging them would make the id
  ambiguous. Seasons from 2022-23 are the default import, since those are the
  ones publishing an explicit `starts` column. It also carries `saves`,
  `yellow_cards`, `red_cards` and `bps`, which FPL's per-player summaries do
  not expose and this schema therefore lacks — the card and save rates in
  `engine/scoring.py` are currently league-typical constants, and this is the
  data that could replace them with measured ones.
