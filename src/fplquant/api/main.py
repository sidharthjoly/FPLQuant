from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from fplquant.api.routers import (
    form,
    market,
    meta,
    news,
    optimizer,
    planner,
    players,
    risk,
    transfers,
)
from fplquant.config import REPO_ROOT, settings
from fplquant.optimizer.types import InfeasibleSquadError

app = FastAPI(
    title="FPL Quant API",
    description="Fantasy Premier League analytics and squad optimization.",
    version="0.1.0",
)

# The frontend (GitHub Pages) and backend (droplet) are deployed separately,
# so this is real cross-origin traffic, not just a local-dev convenience.
# See Settings.cors_allowed_origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(InfeasibleSquadError)
def infeasible_squad_handler(_request: Request, exc: InfeasibleSquadError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(players.router)
app.include_router(form.router)
app.include_router(risk.router)
app.include_router(market.router)
app.include_router(news.router)
app.include_router(optimizer.router)
app.include_router(planner.router)
app.include_router(transfers.router)
app.include_router(meta.router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# Mounted last so it only catches paths no API route already claimed — the
# dashboard is a static, buildless HTML/CSS/JS app (see frontend/) served
# from the same origin as the API, avoiding CORS entirely for it.
frontend_dir = REPO_ROOT / "frontend"
if frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
