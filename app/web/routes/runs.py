from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import SearchRun
from app.db.session import get_db
from app.jobs import execute_search_run
from app.web.templating import templates

router = APIRouter()


@router.post("/search-runs")
def create_search_run(
    background_tasks: BackgroundTasks,
    search_url: str = Form(...),
    max_listings: int = Form(...),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    max_listings = max(1, min(max_listings, settings.max_listings_hard_cap))

    search_run = SearchRun(search_url=search_url, max_listings=max_listings, status="pending")
    db.add(search_run)
    db.commit()

    background_tasks.add_task(execute_search_run, search_run.id)
    return RedirectResponse(url="/", status_code=303)


@router.get("/search-runs/{run_id}/status", response_class=HTMLResponse)
def search_run_status(run_id: int, request: Request, db: Session = Depends(get_db)):
    search_run = db.get(SearchRun, run_id)
    response = templates.TemplateResponse(request, "_search_run_status.html", {"run": search_run})
    if search_run is not None and search_run.status in ("done", "error"):
        response.headers["HX-Refresh"] = "true"
    return response
