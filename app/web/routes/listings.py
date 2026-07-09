from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db.models import Listing
from app.db.session import get_db
from app.jobs import execute_reanalyze
from app.services.listings import get_listing_detail
from app.web.templating import templates

router = APIRouter()


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_detail(listing_id: int, request: Request, db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if listing is None:
        return templates.TemplateResponse(
            request, "listing_detail.html", {"listing": None, "analysis": None}, status_code=404
        )

    detail = get_listing_detail(db, listing)
    return templates.TemplateResponse(
        request,
        "listing_detail.html",
        {
            "listing": detail.listing,
            "analysis": detail.analysis,
            "kb_entries": detail.reliability.entries,
            "kb_tier": detail.reliability.tier,
            "knowledge_is_newer": detail.knowledge_is_newer,
        },
    )


@router.post("/listings/{listing_id}/reanalyze")
def reanalyze_listing(
    listing_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    listing = db.get(Listing, listing_id)
    if listing is not None:
        background_tasks.add_task(execute_reanalyze, listing_id)
    return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)
