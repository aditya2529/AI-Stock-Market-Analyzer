"""FastAPI application — serves REST API + static dashboard."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from api.routes import signals, portfolio, market, health, dashboard

app = FastAPI(
    title="AI Stock Analyzer",
    description="Phase 2 — Paper Trading Dashboard API",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(signals.router)
app.include_router(portfolio.router)
app.include_router(market.router)
app.include_router(health.router)
app.include_router(dashboard.router)  # P46 — trader-grade observability


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


# Serve the single-page dashboard
_DASHBOARD = Path(__file__).parent.parent / "dashboard"

app.mount("/static", StaticFiles(directory=str(_DASHBOARD)), name="static")


@app.get("/", include_in_schema=False)
def serve_dashboard():
    return FileResponse(str(_DASHBOARD / "index.html"))
