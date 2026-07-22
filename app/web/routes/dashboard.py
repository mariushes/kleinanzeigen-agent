from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import SearchRun
from app.db.session import get_db
from app.services.criteria import list_profiles
from app.services.listings import list_analyzed_listings
from app.web.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, sort: str = "score", db: Session = Depends(get_db)):
    rows = list_analyzed_listings(db, sort)

    active_run = (
        db.query(SearchRun)
        .filter(SearchRun.status.in_(["pending", "running"]))
        .order_by(SearchRun.created_at.desc())
        .first()
    )

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "rows": rows,
            "sort": sort,
            "active_run": active_run,
            "default_max_listings": settings.default_max_listings,
            "max_listings_hard_cap": settings.max_listings_hard_cap,
            # Criteria are chosen per search run, so the choice lives on the search form
            # and is recorded on what it produces — no global "current profile" state.
            "criteria_profiles": list_profiles(db),
        },
    )


@router.get("/health")
def health():
    return {"status": "ok"}
