# FPL Quant

[![CI](https://github.com/SidharthJoly/FPLQuant/actions/workflows/ci.yml/badge.svg)](https://github.com/SidharthJoly/FPLQuant/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/SidharthJoly/FPLQuant/main/badges/coverage.json)](https://github.com/SidharthJoly/FPLQuant/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/)
[![Linting: ruff](https://img.shields.io/badge/linting-ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)](https://mypy-lang.org/)
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
- **Squad optimizer** — ILP selection (PuLP) under budget, position, and
  club-count constraints, with an optional Sharpe-style risk-adjusted
  objective.
- **Fixture-adjusted predictions** — opponent strength, venue, and playing
  chance folded into expected points.
- **Transfer planner** — pulls a real FPL team by ID and recommends
  transfers, accounting for -4 point hits, wildcards, and free hits.
- **Player similarity** — per-90 stat vectors, cosine k-NN, and PCA/t-SNE
  projections for finding comparable or cheaper alternatives.
- **API and dashboard** — FastAPI backend with Redis caching, plus a
  no-build-step frontend (optimizer, player explorer, market ticker,
  transfers).

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

### Preseason behaviour

Anything derived from per-gameweek history — the form leaderboard, the market
layer, and player similarity — is empty until matches have been played, since
the FPL API only publishes gameweek history once the season is underway. The
optimizer is the exception: it falls back to FPL's own `ep_next` estimate for
players without history, and switches to the EWMA-based `points_form` once
that data exists.

## Commands

| Command | Description |
| --- | --- |
| `fplquant-ingest` | Pull players, teams, fixtures, and gameweek stats from the FPL API |
| `fplquant-ingest-injuries` | Resolve Transfermarkt player matches and sync injury history |
| `fplquant-form` | Print the EWMA form leaderboard |
| `fplquant-optimize` | Select an optimal 15-man squad and starting XI |
| `fplquant-risk` | Print the injury risk leaderboard |
| `fplquant-market` | Price/ownership momentum, volatility, and teammate correlation |
| `fplquant-similar` | Find players most similar to a given player |
| `fplquant-projection` | Export a PCA/t-SNE projection of the player space |
| `fplquant-api` | Run the FastAPI backend and dashboard |

Injury ingestion is deliberately separate from the main ingest: it scrapes
Transfermarkt and is rate-limited.

### Examples

```bash
uv run fplquant-optimize --budget 100.0 --max-per-club 3

# Risk-adjusted: maximizes expected_points * (1 - injury_risk) / (1 + volatility
# penalty) instead of raw expected points — see src/fplquant/risk/adjusted.py
uv run fplquant-optimize --risk-adjusted --risk-aversion 1.0 --injury-weight 1.0

uv run fplquant-similar "Haaland"                     # most similar players
uv run fplquant-similar "Haaland" --cheaper-only      # cheaper alternatives
uv run fplquant-projection --method pca --output player_projection.json
```

Alongside the 15-man squad, both the CLI and the `/optimize` endpoint return a
starting XI: the best of FPL's eight legal formations for that squad
([`src/fplquant/optimizer/starting_xi.py`](src/fplquant/optimizer/starting_xi.py)
— an exhaustive search, since points are additive per position), a captain and
vice-captain, and the point value of playing Bench Boost or Triple Captain that
week.

## API

```bash
uv run fplquant-api    # http://localhost:8000
```

The dashboard is served at `/`, with Optimizer, Player Explorer, Market
Ticker, and Transfers tabs. Auto-generated interactive docs are at `/docs` (Swagger) and
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
the HTTP API, on top of a fixture-adjusted expected-points model, an ILP squad
optimizer, and a transfer planner. The frontend is vanilla HTML/CSS/JS with no
build step. Full write-up in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
src/fplquant/
  config.py         typed settings (pydantic-settings), env-overridable
  models/           SQLAlchemy ORM models + engine/session setup
  data/             FPL API client + ingestion pipeline
  form/             EWMA-based form scoring (points + underlying stats)
  optimizer/        ILP squad selection (PuLP), budget/position/club constraints
  risk/             injury risk scoring + risk-adjusted expected points
  market/           price/ownership momentum, volatility, teammate correlation
  similarity/       per-90 stat vectors, cosine k-NN, PCA/t-SNE projection
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
