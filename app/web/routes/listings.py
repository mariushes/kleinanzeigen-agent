from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db.models import Listing
from app.db.session import get_db
from app.jobs import execute_reanalyze
from app.services.criteria import get_assessment, list_profiles
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
    assessment = get_assessment(db, detail.analysis)
    return templates.TemplateResponse(
        request,
        "listing_detail.html",
        {
            "listing": detail.listing,
            "analysis": detail.analysis,
            "kb_entries": detail.reliability.entries,
            "kb_tier": detail.reliability.tier,
            "knowledge_is_newer": detail.knowledge_is_newer,
            # The verdict shows the criteria it was judged under; the re-analyze form
            # defaults to those, so re-running keeps the same criteria unless changed.
            "criteria_profiles": list_profiles(db),
            "criteria_profile": detail.analysis.criteria_profile if detail.analysis else None,
            "criteria_findings": assessment.findings if assessment else None,
        },
    )


@router.post("/listings/{listing_id}/reanalyze")
def reanalyze_listing(
    listing_id: int,
    background_tasks: BackgroundTasks,
    criteria_profile_id: str = Form(""),
    db: Session = Depends(get_db),
):
    listing = db.get(Listing, listing_id)
    if listing is not None:
        background_tasks.add_task(
            execute_reanalyze,
            listing_id,
            criteria_profile_id=int(criteria_profile_id) if criteria_profile_id else None,
        )
    return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)
