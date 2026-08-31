from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FPLQUANT_", env_file=".env")

    database_url: str = f"sqlite:///{REPO_ROOT / 'data' / 'fplquant.db'}"
    fpl_base_url: str = "https://fantasy.premierleague.com/api"
    # Public MIT-licensed archive of past FPL seasons (vaastav/Fantasy-Premier-League),
    # used only to build training data — see src/fplquant/data/history.py.
    fpl_history_base_url: str = (
        "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master"
    )
    http_timeout_seconds: float = 15.0
    http_retries: int = 3

    transfermarkt_base_url: str = "https://www.transfermarkt.com"
    # Identify as a normal browser — Transfermarkt has no public API and blocks
    # generic scraper user agents. Requests are rate-limited (see
    # transfermarkt_request_delay_seconds) to stay polite to their servers.
    transfermarkt_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    transfermarkt_request_delay_seconds: float = 1.5

    # Public football news feeds, comma-separated. RSS deliberately, not page
    # scraping: a feed is published *for* syndication, so it is a stable
    # contract rather than markup that changes without notice, and it sidesteps
    # the bot-blocking that stops the Transfermarkt scrape running anywhere but
    # a laptop. Anything added here should be a real feed from a publisher that
    # allows it — see docs/DATA_SOURCES.md.
    news_feed_urls: str = (
        "https://feeds.bbci.co.uk/sport/football/rss.xml,"
        "https://www.theguardian.com/football/rss,"
        "https://www.skysports.com/rss/12040"
    )
    # Identifies the project honestly rather than impersonating a browser. The
    # Transfermarkt client does the opposite because it has to; a feed reader
    # has no reason to.
    news_user_agent: str = "FPLQuant/0.1 (+https://github.com/SidharthJoly/FPLQuant)"
    news_request_delay_seconds: float = 1.0
    # How far back a fetched article can be and still say anything about who is
    # fit. A month-old "out for three weeks" is not news, it is history, and
    # acting on it would put a returned player back on the sidelines.
    news_article_max_age_days: int = 21
    # How sure the resolver has to be that an article is about a given player
    # before the match is allowed to reach the model. Below this the mention is
    # still stored and displayed — it is evidence a human can read — but it
    # cannot move a projection. See `fplquant.news.resolve`.
    news_min_mention_confidence: float = 0.8
    # Master switch for whether feed-derived signals reach the model at all.
    # Turning it off leaves the ingest, the stored articles and the API intact
    # and simply stops them being consumed, which is what you want the moment a
    # publisher's wording starts producing matches you don't trust.
    news_feeds_feed_the_model: bool = True

    redis_url: str = "redis://localhost:6379/0"
    optimize_cache_ttl_seconds: int = 3600

    # Wall-clock ceiling on the multi-gameweek solve, which is by far the most
    # expensive thing the API does — a ten-gameweek horizon with all three
    # chips will happily use two minutes of CPU looking for the last tenth of a
    # point. The CLI can afford that; a shared HTTP service holding a worker
    # for that long cannot, least of all on a single-core free-tier VM. The
    # solver keeps the best plan it has found when the clock runs out, and in
    # practice the incumbent stops improving long before this fires.
    plan_solver_time_limit_seconds: int = 20

    # Comma-separated allowed CORS origins, or "*" for all. The frontend is
    # deployed separately (GitHub Pages) from the backend (droplet), so this
    # needs the Pages origin explicitly — same-origin local dev doesn't hit
    # CORS at all. Add a custom domain here (comma-separated) once it exists.
    cors_allowed_origins: str = (
        "https://fplquant.sidharthjoly.com,http://fplquant.sidharthjoly.com,"
        "https://sidharthjoly.github.io,http://localhost:8000,http://127.0.0.1:8000"
    )

    @property
    def news_feed_url_list(self) -> list[str]:
        return [url.strip() for url in self.news_feed_urls.split(",") if url.strip()]

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        if self.cors_allowed_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


settings = Settings()
