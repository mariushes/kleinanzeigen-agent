"""FastAPI app assembly. Routes live in `app/web/routes/`, one router per concern;
data access lives in `app/services/`. This module only wires them together."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR
from app.criteria.loader import load_profiles
from app.db.session import SessionLocal
from app.web.routes import dashboard, knowledge, listings, runs


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Buyer-criteria profiles are authored as YAML (`app/criteria/profiles/`) and upserted
    # by slug on startup, so editing a profile file takes effect on the next run. Fail-soft:
    # a malformed profile must not stop the app from serving existing verdicts.
    db = SessionLocal()
    try:
        load_profiles(db)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not load criteria profiles: {exc}")
    finally:
        db.close()
    yield


app = FastAPI(title="Kleinanzeigen Van-Buying Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web" / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(listings.router)
app.include_router(runs.router)
app.include_router(knowledge.router)
