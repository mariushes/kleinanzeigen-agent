"""FastAPI app assembly. Routes live in `app/web/routes/`, one router per concern;
data access lives in `app/services/`. This module only wires them together."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR
from app.web.routes import dashboard, knowledge, listings, runs

app = FastAPI(title="Kleinanzeigen Van-Buying Agent")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web" / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(listings.router)
app.include_router(runs.router)
app.include_router(knowledge.router)
