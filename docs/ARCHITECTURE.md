# Architecture

```
FPL API (fantasy.premierleague.com/api)
        │
        ▼
FPLClient (src/fplquant/data/fpl_client.py)   — HTTP wrapper, retries
        │
        ▼
ingest.py (src/fplquant/data/ingest.py)       — upserts into ORM models
        │
        ▼
SQLAlchemy ORM (src/fplquant/models/orm.py)   — Team, Player, Fixture,
        │                                        PlayerGameweekStat
        ▼
SQLite (data/fplquant.db), schema managed by Alembic (alembic/)

Transfermarkt (transfermarkt.com)
        │
        ▼
TransfermarktClient (src/fplquant/data/transfermarkt_client.py) — scrapes
        │                                     player search + injury history
        ▼
player_matching.py                    — fuzzy name+club matching to FPL players
        │
        ▼
ingest_injuries.py                    — caches the match, syncs InjuryRecord rows
```

Two scheduled GitHub Actions workflows keep the database fresh:
- `.github/workflows/ingest.yml` — daily, pulls prices/points/fixtures from the FPL API
- `.github/workflows/ingest_injuries.yml` — weekly, resolves + syncs Transfermarkt
  injury history (lower frequency since it's rate-limited scraping over the full
  player pool)

Both upload the resulting SQLite database as a build artifact.

```
FastAPI app (src/fplquant/api/main.py)
        ├── /players, /players/{id}, /players/{id}/similar
        ├── /form, /risk
        ├── /market/momentum, /market/volatility, /market/correlation
        ├── /optimize  ── cached in Redis (api/cache.py), keyed on request params
        └── /transfers/plan  ── see "Fixture-adjusted predictions & transfer planner" below
```

All read endpoints query the same SQLite database and reuse the exact same
scoring/optimizer modules as the CLIs — the API is a thin HTTP layer over
them, not a separate implementation. `/optimize` is the one expensive
computation (an ILP solve, plus — for risk-adjusted requests — the
form/volatility/injury-risk pipelines), so it's the one endpoint that's
cached; the cache degrades gracefully (falls through to a fresh computation,
logging a warning) if Redis is unreachable rather than ever failing a request.

```
frontend/ (static, no build step — no Node/npm involved)
  index.html   ── 4 tabs: Optimizer, Explorer, Market, Transfers, plus a
                  persistent header (nav, live deadline countdown, price/
                  ownership ticker tape) and a squad-summary hero
  config.js    ── API_BASE: same-origin locally, the backend's URL once deployed
  api.js       ── fetch wrapper over the endpoints above
  kits.js      ── real home-kit colors per club, for the jersey icons
  components.js ── jersey icon (SVG), donut gauge, fixture-context meta line
  optimizer.js / explorer.js / ticker.js / transfers.js / main.js
```

The design — "Nocturne", a dark-first quant-terminal aesthetic — was built in
Claude's design tool and imported via the `claude_design` MCP, then
implemented against the real API (not the mock data the design tool preview
used). The starting-XI pitch view positions players by formation row with
jersey icons in each club's real kit colors; the header's ticker tape and
deadline countdown are driven by `/market/momentum` and the new
`/meta/next-deadline` endpoint.

## Fixture-adjusted predictions & transfer planner

Every predicted-points number in the app — the optimizer, the starting XI's
bench/start and captain choices, and the transfer planner — is fixture-adjusted
for each player's *next match specifically* (`src/fplquant/form/fixtures.py`),
not just a season-long average:

- **Opponent strength**: a continuous multiplier (clamped 0.7–1.3) built from
  each team's own attack/defence ratings, position-aware — GKP/DEF care about
  the opponent's attacking strength (clean sheet odds), MID/FWD care about the
  opponent's defensive strength — plus FPL's own 1–5 fixture difficulty rating
  surfaced alongside it for display.
- **Venue**: home/away, since those ratings differ by venue.
- **Chance of playing**: FPL's own `chance_of_playing_next_round` when set
  (press-conference news), else inferred from `status`. Next-round only — the
  news layer below extends it across the horizon, and deliberately leaves this
  path untouched.

This feeds the risk-adjusted scorer too (`src/fplquant/risk/adjusted.py`), so
"risk-adjusted" and "fixture-adjusted" compose rather than compete.


## The news layer

`chance_of_playing_next_round` is a single number, and the horizon projection
used to apply it to every gameweek in a five-week window. A player serving a
ban that expired on the Tuesday was therefore worth zero points until February;
one carrying a knock this week was still carrying it in October. Both are wrong
in the direction that matters most to a transfer, which is a commitment over
exactly that horizon — and zero specifically, because
`build_horizon_candidates_from_db` trims the pool on horizon value, so a player
worth nothing is one the multi-period program can never select.

`src/fplquant/news/` supplies the missing dimension. It parses FPL's published
`news` string — which is templated, not free text: five shapes covered all 118
non-empty strings in the pool — into a category, a condition, and a return date
where one is stated. `availability.py` turns that into availability *per
gameweek*, evaluated against each club's own kickoff, since a round spans three
or four days and a ban ending on the Saturday clears a club playing Sunday and
not one playing Friday.

Two rules make it safe to compose with everything already in the model:

- **It cannot contradict FPL about the next round.** Availability at a player's
  next actual match is `chance_of_playing` verbatim, taken rather than
  recomputed. FPL's percentage *is* the press-conference summary, so discounting
  it a second time would charge the same evidence twice.
- **It can only give availability back.** Every projection is floored at the
  published number, so the layer can restore a suspended player once his ban
  expires but can never newly rule anyone out. A spurious zero silently deletes
  a fit player from the squad; a spurious one only makes the model optimistic
  about somebody FPL has already flagged.

Three shapes of news behave differently, and conflating them would be the whole
mistake. A **suspension** ends on a date that is known, so it steps cleanly to
full availability. An **injury with a stated return date** ends on a club's
estimate, and estimates slip late far more often than early, so the date is read
as optimistic and recovery ramps in over a window that widens with the forecast
horizon. A **doubt** — a percentage with no date — is a statement about one
match, so availability recovers toward a ceiling over a window set by the grade.
Everything else, including an injury with no return date (47 of 118 strings) and
any wording the parser does not recognise, keeps the published number in every
gameweek.

The numbers flow through `engine/minutes.py` and `engine/usage.py` into
`engine/horizon.py`, which recomputes usage once per distinct availability
vector. It has to be recomputed rather than scaled afterwards: shares are
normalised within a club, so a striker returning takes goal share back *off*
the teammates who absorbed it, and scaling one side of that trade would count
those goals twice. Everything downstream of the horizon follows: the candidate
pool (`optimizer/candidates.py`), the multi-period program
(`optimizer/multiperiod.py`), `fplquant-plan` and `/plan`.

The **single-gameweek** transfer planner (`transfers/planner.py`, served at
`/transfers`) is deliberately *not* affected. It scores the next match only, through
`form/fixtures.py`, where FPL's published percentage is already the right and
complete answer. Only the multi-gameweek path has a horizon for this layer to
say anything about.

### Two sources, and they are not peers

`news/sources.py` defines a `NewsSource` protocol with an `authoritative` flag,
and that flag is the whole safety design. `FPLPlayerNewsSource` is
authoritative: FPL's percentage is the official line and the number the rest of
the model already consumes. `ExternalNewsSource` is supplementary: it reads
press items that `fplquant-ingest-news` stored from public RSS feeds
(`news/feeds.py`), resolved to players by `news/resolve.py` and read for a
return date by `news/extract.py`.

A supplementary source may contribute exactly one thing — a return date for a
player FPL has ruled out *without* giving one, which is 47 of the 118 non-empty
news strings in the pool. `merge_reported_return` in `news/availability.py`
enforces it: category, condition and next-round availability always come from
FPL, so a misresolved article cannot change what kind of absence a player has,
cannot touch a fit player, and cannot rule anybody out. Combined with the floor
("only ever give availability back"), the worst a wrong match can do is make the
model optimistic about somebody already at zero.

A reported date is also believed more slowly than an official one: the slippage
window around it is stretched by `REPORTED_RETURN_SLIP_MULTIPLIER`, because a
journalist relaying a club's estimate is a real signal and a second-hand one.

The ingest is a separate process from the reading side on purpose — nothing that
serves a request touches the network — and it fails loudly when it fetches
articles and resolves nobody, because a broken resolver and a quiet news day are
indistinguishable from the outside. See `docs/DATA_SOURCES.md` for the feeds and
the resolution rules, and `DEPLOYMENT.md` for the daily cron.

The **transfer planner** (`src/fplquant/transfers/`) pulls a manager's current
squad from their public FPL team ID — no login needed, the same data FPL's own
site shows on a manager's profile — and solves an ILP (`propose_transfers`,
extending the same PuLP formulation as the squad optimizer) for the transfers
that maximize next-match expected points *net of the real -4-per-transfer hit*
beyond the manager's free transfers. Because making no transfers is always a
free, feasible option, a transfer is only ever recommended when its expected
gain outweighs its cost — "is this transfer worth the hit" is answered by the
optimization itself, not a separate heuristic.

The eleven that score are chosen inside that program, with the bench discounted
to `BENCH_WEIGHT`, exactly as the multi-period planner does it. Maximizing the
fifteen-man total instead makes a substitute's projection count as much as a
starter's, and the consequence was concrete: it spent a -4 hit swapping the
*backup* goalkeeper for a better backup goalkeeper. Wildcard/Free Hit chips are
supported (they lift the transfer limit and the hit entirely for that
gameweek). Sell price is approximated as current market value, since FPL's
sell-on-fee data isn't available without authenticating as the manager.

Note: FPL only exposes a manager's picks once their first gameweek deadline
has passed, so the transfer planner has nothing to plan from until then — it
returns a clear "season hasn't started yet" message rather than an error in
that case.

Two ways to serve it, both supported:
- **Local / same-origin:** FastAPI mounts `frontend/` itself (`StaticFiles` at
  `/`, after all API routes) — no CORS needed, `config.js`'s default
  `API_BASE = ""` just works.
- **Deployed:** the frontend is published to **GitHub Pages**
  (`.github/workflows/pages.yml`, triggered on any push touching `frontend/`)
  while the backend runs separately on the droplet — genuine cross-origin
  traffic. `config.js`'s `API_BASE` needs to point at the droplet's URL, and
  `Settings.cors_allowed_origins` (`src/fplquant/config.py`) needs the Pages
  origin allowed — it already defaults to `https://fplquant.sidharthjoly.com`
  (the custom subdomain the site runs on) plus `https://sidharthjoly.github.io`
  and localhost, override via `FPLQUANT_CORS_ALLOWED_ORIGINS` if that ever
  changes.

Vanilla HTML/CSS/JS by design: no bundler, no framework, no `npm install`.

## The points engine

`src/fplquant/engine/` replaces "past points × a fixture multiplier" with a
top-down model of how points actually get scored. Four layers, each usable on
its own:

```
rates.py     fitted team attack/leak multipliers -> expected goals per fixture
minutes.py   absolute start probability, normalised to eleven shirts per club
usage.py     a club's goals split among its players by shrunk per-90 rates
scoring.py   FPL's scoring table applied to the above -> points, rule by rule
horizon.py   the above, per fixture, over N gameweeks (doubles and blanks)
simulate.py  Monte Carlo over the same structure -> distributions, correlations
```

The dependency direction is strictly downward: `scoring.py` is pure and imports
no database, `usage.py` and `minutes.py` read the ORM, and `horizon.py` is the
only module that walks the fixture calendar. `simulate.py` reads `horizon.py`'s
output rather than recomputing anything, which is what keeps the sampler and the
closed form in agreement.

### Where the shrinkage lives

Every layer has a prior and a credibility weight, because the season this is
most useful in is the one where there is barely any data:

| Layer | Estimate | Prior | Evidence |
| --- | --- | --- | --- |
| `rates` | team attack/leak | FPL ratings blended with squad value | matches played, decayed by gameweek |
| `minutes` | start probability | softmax over price within the position group | matches started |
| `usage` | goals and assists per 90 | league rate scaled by price | minutes played |
| `usage` | bonus per appearance | implied by expected involvements | appearances |

The blend is a credibility weight `n / (n + k)` throughout, matching
`fplquant.form.scoring`. None of it switches over at a threshold.

### Multi-period planning

`src/fplquant/optimizer/multiperiod.py` is a single integer program over the
horizon. Variables are indexed by `(player, gameweek)`: squad membership,
starting XI, captaincy, buy, and sell, plus a per-gameweek free-transfer balance
and hit count. The flow constraint

```
squad[p, t] == squad[p, t-1] + buy[p, t] - sell[p, t]
```

is what makes it a plan rather than a sequence of independent picks. Free
transfers carry over with `balance[t] <= available - used_free[t] + 1`, capped
at five; `used_free` is a variable rather than `transfers - hits` because a
wildcard consumes no free transfers, and an expression would charge fifteen
against a balance floored at one and make the week infeasible.

Chips are binaries constrained to at most one play per half-season, matching
FPL's two chip sets (gameweeks 1-19 and 20-38). Bench boost and triple captain
each multiply a chip decision by a selection decision, linearised with an
auxiliary variable pinned below both factors. The free hit additionally
constrains next week's squad back to last week's — two inequalities that bind
only when the chip is played — since without the reversion it dominates the
wildcard and is always chosen; and it is barred from the final gameweek, whose
reversion would fall outside the horizon.

The candidate pool is trimmed to the top of each position by projected points,
plus every owned player unconditionally — several binaries per player per
gameweek across 600 players is not a tractable program, and the trimmed players
are by construction ones the objective would never have picked.


## Point-in-time snapshots

`Player` and `Team` are current-state rows: `now_cost`, `ep_next`, `status`,
`chance_of_playing_next_round`, `news`, `form`, `selected_by_percent` and the
team strength ratings are all overwritten on every ingest. The database therefore
always knows what is true today and never what was true before a past
deadline.

That is fine for predicting and fatal for checking. A backtest has to rebuild
the world as it looked before a gameweek, and `chance_of_playing` is a hard
zero gate on every player's expected points — replaying an old round against
today's `status` leaks the future in both directions, zeroing out a player who
actually played and crediting one who was ruled out. The result looks
plausible and means nothing.

`PlayerGameweekStat` already preserves per-round price and ownership, so those
are recoverable. Nothing recovers the rest: FPL publishes no history for them.
`player_snapshots` and `team_snapshots` archive them once per day, upserted on
the date so re-running an ingest updates that day's row rather than growing the
table. Each row records `next_event`, the gameweek it leads into, so a backtest
can ask for "the state going into GW5" directly.

Written at the end of `run_ingest`, inside a `try`, so the existing daily cron
collects them with no schedule change and a failure here can never break the
refresh every other feature depends on. Roughly 630 rows a day.

The scoring harness that consumes this doesn't exist yet. The table does,
because this is the one kind of data that cannot be backfilled — every week
without it is a week that can never be properly replayed.
