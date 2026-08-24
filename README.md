# FPL Quant

[![CI](https://github.com/SidharthJoly/FPLQuant/actions/workflows/ci.yml/badge.svg)](https://github.com/SidharthJoly/FPLQuant/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/SidharthJoly/FPLQuant/main/badges/coverage.json)](https://github.com/SidharthJoly/FPLQuant/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/)
[![Linting: ruff](https://img.shields.io/badge/linting-ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)](https://mypy-lang.org/)
[![Container](https://img.shields.io/badge/ghcr.io-fplquant-blue?logo=docker&logoColor=white)](https://github.com/sidharthjoly/FPLQuant/pkgs/container/fplquant)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Demo](https://img.shields.io/badge/demo-live-brightgreen.svg)](https://fplquant.sidharthjoly.com/)

A Fantasy Premier League analytics and squad optimization platform. It treats
players like financial instruments — combining price momentum, volatility, and
portfolio theory with sports analytics (injury risk, form, fixture difficulty)
to select a risk-adjusted squad.

Live at **[fplquant.sidharthjoly.com](https://fplquant.sidharthjoly.com/)** —
a static frontend on GitHub Pages talking to a FastAPI backend on an Oracle
Cloud VM.

## Features

- **Data pipeline** — FPL API ingestion into SQLite via SQLAlchemy, schema
  managed by Alembic.
- **Form analysis** — EWMA of points and underlying stats (xG, xA, ICT).
- **Injury risk** — age, position, minutes load, and Transfermarkt injury
  history combined into a per-player risk score.
- **Market layer** — price and ownership momentum, points volatility, and
  teammate correlation, computed from per-gameweek time series.
- **Points engine** — fitted team goal rates, allocated top-down to players,
  scored through FPL's actual scoring table. Produces a per-rule breakdown and
  a full distribution rather than a single number.
- **Multi-gameweek horizon** — projections fixture by fixture over the next N
  rounds, including double and blank gameweeks.
- **Squad optimizer** — ILP selection (PuLP) under budget, position, and
  club-count constraints, with an optional Sharpe-style risk-adjusted
  objective.
- **Multi-period planner** — one integer program over the whole horizon:
  squad, starting XI, captain, transfers, free-transfer banking, hits, and
  chip timing, all as decision variables.
- **Monte Carlo** — gameweeks simulated match by match, so teammates correlate
  structurally. Gives floors, ceilings, haul odds, and squad-level risk.
- **Fixture-adjusted predictions** — opponent strength, venue, and playing
  chance folded into expected points.
- **Lineup and rotation** — club formations inferred from who actually gets
  picked, plus rest days and minutes load, combined into a start-probability
  nudge on expected points, and surfaced per player as odds to be named in the
  XI (with the fitness news applied as a hard gate on top).
- **Transfer planner** — pulls a real FPL team by ID and recommends
  transfers, accounting for -4 point hits, wildcards, and free hits.
- **Player similarity** — per-90 stat vectors, cosine k-NN, and PCA/t-SNE
  projections for finding comparable or cheaper alternatives.
- **API and dashboard** — FastAPI backend with Redis caching, plus a
  no-build-step frontend (optimizer, player explorer, market ticker,
  transfers, and a multi-gameweek planner). Every player shown anywhere — on the pitch, in the dugout, in a
  transfer suggestion, on the market tape — opens their explorer profile.

## Screenshots

The optimizer's starting-XI pitch view uses jersey icons colored by each club's
real kit. **These are demo data** — the 2026/27 season has not started, so the
screenshots are seeded with synthetic gameweek history on top of real FPL
player and team data, not live results.

<img src="docs/screenshots/optimizer.png" alt="Squad optimizer" width="720" />
<img src="docs/screenshots/explorer.png" alt="Player explorer" width="720" />
<img src="docs/screenshots/ticker.png" alt="Market view" width="720" />
<img src="docs/screenshots/ticker_dark.png" alt="Market view, light mode" width="720" />

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13+.

```bash
uv sync                       # install dependencies into .venv
cp .env.example .env          # optional — defaults work out of the box
uv run alembic upgrade head   # create data/fplquant.db and apply the schema
uv run fplquant-ingest        # pull live data from the FPL API (~1-2 min)
uv run fplquant-optimize      # select an optimal 15-man squad within budget
uv run fplquant-api           # serve the dashboard at http://localhost:8000
```

### Early-season behaviour

Anything derived from per-gameweek history — the form leaderboard, the market
layer, and player similarity — is empty until matches have been played, since
the FPL API only publishes gameweek history once the season is underway.

Predictions handle the same scarcity by *credibility weighting* rather than by
switching over at some threshold. A player's EWMA form is blended toward FPL's
own `ep_next` in proportion to how many appearances back it, so with no history
the estimate is pure `ep_next`, and form takes over as evidence accumulates.
This matters more than it sounds: after one gameweek a player's EWMA form is
exactly that gameweek's score, so taken at face value it would have the
optimizer rebuild the whole squad around last week's highest scorers.

The lineup signals behave the same way. Inferred formations are shrunk toward a
4-4-2 prior, start rates toward a positional prior, and the rotation adjustment
is expressed as a multiplier centred on 1.0 that does nothing at all until
there is something to say. Early in the season the part that actually carries
information is rest days, which come from the fixture calendar rather than from
match history.

The points engine follows the same discipline one level up. Team goal rates
start at a prior and are moved by the match record in proportion to how many
matches back it; player scoring rates start at a price-implied prior and give
way to a player's own per-90 numbers as minutes accumulate; and start
probabilities start at a softmax over price and give way to who actually gets
picked. None of it switches over at a threshold — with an empty database the
whole engine still produces a usable projection, built entirely from priors.

## Commands

| Command | Description |
| --- | --- |
| `fplquant-ingest` | Pull players, teams, fixtures, and gameweek stats from the FPL API |
| `fplquant-ingest-injuries` | Resolve Transfermarkt player matches and sync injury history |
| `fplquant-form` | Print the EWMA form leaderboard |
| `fplquant-optimize` | Select an optimal 15-man squad and starting XI |
| `fplquant-risk` | Print the injury risk leaderboard |
| `fplquant-lineup` | Next-match start probabilities, rest, and inferred club formations |
| `fplquant-market` | Price/ownership momentum, volatility, and teammate correlation |
| `fplquant-similar` | Find players most similar to a given player |
| `fplquant-projection` | Export a PCA/t-SNE projection of the player space |
| `fplquant-project` | Multi-gameweek expected points, with team ratings and simulation |
| `fplquant-plan` | Plan squad, transfers, captaincy, and chips over a horizon |
| `fplquant-api` | Run the FastAPI backend and dashboard |

Injury ingestion is deliberately separate from the main ingest: it scrapes
Transfermarkt and is rate-limited.

### Examples

```bash
uv run fplquant-optimize --budget 100.0 --max-per-club 3

# Risk-adjusted: maximizes expected_points * (1 - injury_risk) / (1 + volatility
# penalty) instead of raw expected points — see src/fplquant/risk/adjusted.py
uv run fplquant-optimize --risk-adjusted --risk-aversion 1.0 --injury-weight 1.0

uv run fplquant-lineup                                # start probabilities
uv run fplquant-lineup --shapes                       # inferred club formations

uv run fplquant-similar "Haaland"                     # most similar players
uv run fplquant-similar "Haaland" --cheaper-only      # cheaper alternatives
uv run fplquant-projection --method pca --output player_projection.json

# Multi-gameweek projections. Blanks show as "—", doubles as "*"
uv run fplquant-project --horizon 5
uv run fplquant-project --ratings                     # fitted team goal rates
uv run fplquant-project --simulate --seed 1           # floors, ceilings, haul odds
uv run fplquant-project --explain "Haaland"           # the model's full reasoning

# Plan a horizon, letting the solver decide when to play the chips
uv run fplquant-plan --horizon 5
uv run fplquant-plan --team-id 1234567 --free-transfers 2 \
    --chips wildcard bench_boost triple_captain
```

Alongside the 15-man squad, both the CLI and the `/optimize` endpoint return a
starting XI: the best of FPL's eight legal formations for that squad
([`src/fplquant/optimizer/starting_xi.py`](src/fplquant/optimizer/starting_xi.py)
— an exhaustive search, since points are additive per position), a captain and
vice-captain, and the point value of playing Bench Boost or Triple Captain that
week.

## The points engine

The original expected-points model is an EWMA of a player's past FPL points,
multiplied by a fixture-difficulty factor. That is a reasonable first pass and
it has a structural problem: points are a *consequence*, not a quantity in
their own right. A defender's expected points are dominated by the probability
their side keeps a clean sheet, which is a property of the opponent's attack
and has nothing to do with the defender's own scoring history — so scaling
their past points by one blunt multiplier moves the wrong term.

[`src/fplquant/engine/`](src/fplquant/engine/) models the components instead,
top-down, in four layers.

**Team goal rates** ([`rates.py`](src/fplquant/engine/rates.py)) fit each club's
attacking and defensive multipliers from the results so far, as a damped
multiplicative fixed point: given what the model currently believes about the
opponents a club faced, how many goals *should* they have scored, and how many
did they? Because each club's correction depends on its opponents' current
ratings, the passes are iterated until they settle, so beating three
relegation candidates is not mistaken for beating the top three. Expected goals
carry more weight than the scoreline, since xG settles over a handful of games
where goals take most of a season.

Fitting forty parameters against a dozen matches is underdetermined, not merely
noisy, so the multipliers start at a prior and the record moves them by a
credibility weight. The prior is itself two readings blended: FPL's published
team ratings, and the combined price of a club's fifteen most expensive
players. The second is there because the first is unreliable — FPL's granular
`strength_attack_*` columns are **zero for every club** for much of preseason,
and the coarse `strength_overall_*` rating has four distinct values across
twenty clubs. Squad value is continuous, never missing, and reprices itself.

**Minutes** ([`minutes.py`](src/fplquant/engine/minutes.py)) estimate the
absolute probability of starting, under the constraint that a club starts
exactly eleven players: probabilities within a position group are normalised to
the slots that group's inferred formation fills. That constraint is what makes
the estimate self-correcting — an injury to a first-choice striker
redistributes his minutes to the rest of the forward line rather than
evaporating. The prior is a softmax over price within each club's position
group, since FPL prices are compressed but their ordering is informative.

**Usage** ([`usage.py`](src/fplquant/engine/usage.py)) splits a club's goals
among its players by credibility-shrunk per-90 rates, normalised so the shares
sum to one. The model is therefore internally consistent: sum a club's players'
expected goals for a fixture and the club's expected goals come back out.

**Scoring** ([`scoring.py`](src/fplquant/engine/scoring.py)) converts all of
that into points through FPL's rules — clean sheet probability as `exp(-λ)`
gated on 60 minutes, the goals-conceded penalty as `E[floor(K/2)]` rather than
`floor(E[K]/2)`, saves from the opponent's rate, bonus from BPS history.

`fplquant-project --explain` prints the whole chain for one player:

```
Gonzalo (FUL, £6.0m)
  starts 97% of the time, 82 expected minutes; rate estimate is 0% their own
  record, the rest price-implied
  takes 23.4% of their club's goals and 13.2% of the assists, at 0.34 goals
  and 0.12 assists per 90
  GW2 vs SUN (A): 3.82 pts  [xG for 1.39, against 1.41, clean sheet 24%]
      appearance +1.83  goals +1.30  assists +0.40  clean sheet +0.00
      conceded +0.00  saves +0.00  bonus +0.40  cards -0.12
```

### Simulation

[`simulate.py`](src/fplquant/engine/simulate.py) samples a gameweek match by
match: both sides' goals are drawn from the fitted Poisson rates, then
allocated to players by a multinomial over the usage shares. Because every
player in a match reads the same two draws, teammate correlation is
*structural* rather than estimated — a defender's clean sheet and his
goalkeeper's arrive in the same simulations, and a squad's variance reflects
that three defenders from one club are one bet held three times. It also
double-checks the model: the sampler and the closed form are independent
implementations, and their means agree to within a few hundredths of a point
across the whole player pool ([`tests/test_engine_simulate.py`](tests/test_engine_simulate.py)).

### Multi-period planning

A single-gameweek solver will take a -4 for one good fixture, sell the player
next week for another -4, and never notice that banking the free transfer would
have got the same squad for nothing. It cannot value a free transfer, because a
free transfer is worth exactly the flexibility it gives you *later*, and later
is not in its model.

[`multiperiod.py`](src/fplquant/optimizer/multiperiod.py) solves the whole
horizon as one integer program. Squad membership, the starting XI, the captain,
transfers, hits, and the free-transfer balance are all variables indexed by
gameweek, tied together by a flow constraint — this week's squad is last week's,
plus what you bought, minus what you sold. Chips are opt-in binaries the solver
places wherever they are worth most, which is a question a one-week model cannot
even ask: knowing which week to triple-captain requires looking at all of them
together.

Choosing the XI *inside* the optimization is a real improvement on the
single-gameweek path, which maximizes the 15-man total and then picks an XI from
whatever it bought — that values a fourth goalkeeper the same as a first-choice
striker.

Only the first gameweek's moves are meant to be executed; re-solve once the next
round's news lands. That is model predictive control, and it is why later
gameweeks are discounted in the objective.

```
$ uv run fplquant-plan --horizon 5 --chips wildcard bench_boost triple_captain
Horizon GW1-GW5 · 231.1 expected points · 0 points of hits · solver Optimal

GW4  57.1 pts  3-4-3  [BENCH BOOST]
  free transfers 2
  OUT Wirtz            (3.31)   IN Isak             (5.20)
  C Isak, VC Haaland
```

The Planner tab in the dashboard shows the same plan as a timeline — one card
per gameweek, the first one highlighted because it is the only one you act on —
and clicking a card shows the fifteen you would own that week.

One chip per gameweek is a constraint, not an assumption. Without it the solver
stacks them: a wildcard makes a whole squad's transfers free, a bench boost then
scores the bench it just bought, and all three pile into the week with the best
fixtures, producing a plan worth more points than any you are allowed to play.

Known approximations, all deliberate: prices are held constant across the
horizon, so it cannot plan around price rises; selling fees are not modelled;
and the pool is trimmed to the top of each position, since several binaries per
player per gameweek over 600 players is not a tractable program.

## API

```bash
uv run fplquant-api    # http://localhost:8000
```

The dashboard is served at `/`, with Optimizer, Player Explorer, Market
Ticker, Transfers, and Planner tabs. The horizon endpoints are `GET
/projections` (with optional `simulate=true`) and `POST /plan`.

`/plan` is the most expensive thing the API does, so its solve is capped at
`FPLQUANT_PLAN_SOLVER_TIME_LIMIT_SECONDS` (default 20). The solver keeps the
best plan it has found when the clock runs out; the CLI's `--time-limit` is
uncapped by comparison, since a terminal can afford to wait and a shared
worker cannot. Auto-generated interactive docs are at `/docs` (Swagger) and
`/redoc`.

Redis is optional. Caching is best-effort: if Redis is unreachable, the API
logs a warning and falls through to a fresh computation.

## Docker

```bash
docker compose up                                # api (8000) + redis (6379)
docker compose exec api uv run fplquant-ingest   # populate data in the container
```

The image is published to
[GitHub Container Registry](https://github.com/sidharthjoly?tab=packages) on
every push to `main` and can be run without cloning the repo:

```bash
docker run -p 8000:8000 \
  -e FPLQUANT_DATABASE_URL=sqlite:////app/data/fplquant.db \
  -v "$(pwd)/data:/app/data" \
  ghcr.io/sidharthjoly/fplquant:latest
```

The container needs a schema (`alembic upgrade head`) and data
(`fplquant-ingest`) before `/optimize` returns anything useful;
`docker-compose.yml` wires both into its startup command. See
[`src/fplquant/config.py`](src/fplquant/config.py) for the full list of
`FPLQUANT_*` environment variables (Redis URL, CORS origins, HTTP timeouts).
This is the same image that runs in production.

## Architecture

A FastAPI backend (SQLite plus a Redis cache) is shared by both the CLIs and
the HTTP API, on top of a Poisson points engine, an ILP squad optimizer, and
single- and multi-gameweek transfer planners. The frontend is vanilla HTML/CSS/JS with no
build step. Full write-up in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
src/fplquant/
  config.py         typed settings (pydantic-settings), env-overridable
  models/           SQLAlchemy ORM models + engine/session setup
  data/             FPL API client + ingestion pipeline
  form/             EWMA-based form scoring (points + underlying stats)
  lineup/           inferred formations, start probability, fatigue
  optimizer/        ILP squad selection (PuLP), budget/position/club constraints
  risk/             injury risk scoring + risk-adjusted expected points
  market/           price/ownership momentum, volatility, teammate correlation
  similarity/       per-90 stat vectors, cosine k-NN, PCA/t-SNE projection
  engine/           Poisson points engine: team rates, minutes, usage, scoring,
                    multi-gameweek horizon, Monte Carlo simulation
  api/              FastAPI backend (routers/, schemas.py, cache.py)
frontend/           static dashboard (vanilla HTML/CSS/JS, served by the API)
alembic/            database migrations
tests/              pytest suite (mirrors src/ layout)
```

## Data sources

The FPL API (prices, points, fixtures, xG/xA/ICT) and Transfermarkt (injury
history, scraped and fuzzy-matched to FPL players). Full breakdown, including
sources investigated and not pursued, in
[`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).

## Development

```bash
uv run pytest                 # tests, with coverage
uv run ruff check .           # lint
uv run black .                # format
uv run mypy src               # type check
uv run pre-commit install     # enable pre-commit hooks (ruff, black, mypy)
```

Creating a migration after changing `src/fplquant/models/orm.py`:

```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
```

## Deployment

The application is split across two hosts:

- **Frontend** — [GitHub Pages](https://fplquant.sidharthjoly.com/) behind a
  custom subdomain (`fplquant.sidharthjoly.com`, CNAMEd to
  `sidharthjoly.github.io`), deployed by `.github/workflows/pages.yml` on every
  push touching `frontend/`.
- **Backend** — FastAPI and Redis on an Oracle Cloud Always Free VM, fronted by
  Caddy at `https://fplquant.duckdns.org` for automatic HTTPS via DuckDNS and
  Let's Encrypt. Redeployed by `.github/workflows/deploy.yml` (manually
  triggered); the VM pulls the prebuilt image rather than building from source.

The server keeps its own data fresh via cron (`scripts/cron_ingest*.sh`), and
`.github/workflows/keepalive.yml` pings `/health` every 15 minutes to stay clear
of Oracle's idle-instance reclaim policy. Full runbook, including OCI firewall
configuration, in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## License

[MIT](LICENSE). Data retrieved from the FPL API and Transfermarkt remains
subject to those providers' own terms.
