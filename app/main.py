from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from sqlalchemy import func

from app.config import BASE_DIR, get_settings
from app.db.models import (
    Analysis,
    KnowledgeEntry,
    KnowledgeResearchRun,
    Listing,
    LlmCall,
    SearchRun,
    VehicleIdentity,
)
from app.db.session import get_db
from app.jobs import execute_knowledge_run, execute_reanalyze, execute_search_run
from app.knowledge.retrieval import get_reliability_summary

app = FastAPI(title="Kleinanzeigen Van-Buying Agent")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "web" / "templates")

_SORT_KEYS = {
    "score": lambda row: -(row["analysis"].overall_score or 0),
    "price": lambda row: row["listing"].price_eur if row["listing"].price_eur is not None else float("inf"),
    "mileage": lambda row: row["listing"].mileage_km if row["listing"].mileage_km is not None else float("inf"),
}


def _latest_analysis(listing: Listing) -> Analysis | None:
    if not listing.analyses:
        return None
    return max(listing.analyses, key=lambda a: a.created_at)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, sort: str = "score", db: Session = Depends(get_db)):
    rows = []
    for listing in db.query(Listing).all():
        analysis = _latest_analysis(listing)
        if analysis is not None:
            rows.append({"listing": listing, "analysis": analysis})
    rows.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["score"]))

    active_run = (
        db.query(SearchRun)
        .filter(SearchRun.status.in_(["pending", "running"]))
        .order_by(SearchRun.created_at.desc())
        .first()
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "rows": rows,
            "sort": sort,
            "active_run": active_run,
            "default_max_listings": get_settings().default_max_listings,
            "max_listings_hard_cap": get_settings().max_listings_hard_cap,
        },
    )


@app.post("/search-runs")
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


@app.get("/search-runs/{run_id}/status", response_class=HTMLResponse)
def search_run_status(run_id: int, request: Request, db: Session = Depends(get_db)):
    search_run = db.get(SearchRun, run_id)
    response = templates.TemplateResponse(request, "_search_run_status.html", {"run": search_run})
    if search_run is not None and search_run.status in ("done", "error"):
        response.headers["HX-Refresh"] = "true"
    return response


@app.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_detail(listing_id: int, request: Request, db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if listing is None:
        return templates.TemplateResponse(
            request, "listing_detail.html", {"listing": None, "analysis": None}, status_code=404
        )

    analysis = _latest_analysis(listing)

    # Live-retrieve current reliability knowledge for this listing's identity rather than
    # replaying the snapshot frozen into the verdict: knowledge collected after this
    # listing was analyzed should still show here. If the current KB has more coverage
    # than the verdict used, flag that the score is stale and re-analysis would fold it in.
    reliability = get_reliability_summary(db, listing.identity)
    used_entry_ids = set(analysis.reliability.get("entry_ids", [])) if analysis else set()
    knowledge_is_newer = analysis is not None and bool(
        {e.id for e in reliability.entries} - used_entry_ids
    )

    return templates.TemplateResponse(
        request,
        "listing_detail.html",
        {
            "listing": listing,
            "analysis": analysis,
            "kb_entries": reliability.entries,
            "kb_tier": reliability.tier,
            "knowledge_is_newer": knowledge_is_newer,
        },
    )


@app.post("/listings/{listing_id}/reanalyze")
def reanalyze_listing(
    listing_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    listing = db.get(Listing, listing_id)
    if listing is not None:
        background_tasks.add_task(execute_reanalyze, listing_id)
    return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_admin(request: Request, db: Session = Depends(get_db)):
    identities = db.query(VehicleIdentity).order_by(VehicleIdentity.canonical_label).all()
    coverage = []
    for identity in identities:
        entry_count = (
            db.query(func.count(KnowledgeEntry.id))
            .filter(KnowledgeEntry.identity_id == identity.id)
            .scalar()
        )
        listing_count = (
            db.query(func.count(Listing.id)).filter(Listing.identity_id == identity.id).scalar()
        )
        coverage.append(
            {"identity": identity, "entry_count": entry_count, "listing_count": listing_count}
        )

    totals = db.query(
        func.coalesce(func.sum(LlmCall.input_tokens), 0),
        func.coalesce(func.sum(LlmCall.output_tokens), 0),
        func.count(LlmCall.id),
    ).one()

    return templates.TemplateResponse(
        request,
        "knowledge.html",
        {
            "coverage": coverage,
            "llm_input_tokens": totals[0],
            "llm_output_tokens": totals[1],
            "llm_call_count": totals[2],
            "max_queries": get_settings().knowledge_default_max_queries,
        },
    )


@app.get("/knowledge/{identity_id}", response_class=HTMLResponse)
def knowledge_identity(identity_id: int, request: Request, db: Session = Depends(get_db)):
    identity = db.get(VehicleIdentity, identity_id)
    if identity is None:
        return templates.TemplateResponse(
            request, "knowledge_identity.html", {"identity": None}, status_code=404
        )
    entries = (
        db.query(KnowledgeEntry)
        .filter(KnowledgeEntry.identity_id == identity_id)
        .order_by(KnowledgeEntry.entry_type, KnowledgeEntry.confidence.desc())
        .all()
    )
    covered_angles = [
        r.angle_key
        for r in db.query(KnowledgeResearchRun)
        .filter(KnowledgeResearchRun.identity_id == identity_id)
        .all()
    ]
    return templates.TemplateResponse(
        request,
        "knowledge_identity.html",
        {"identity": identity, "entries": entries, "covered_angles": covered_angles},
    )


@app.post("/knowledge/{identity_id}/collect")
def collect_knowledge(
    identity_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    identity = db.get(VehicleIdentity, identity_id)
    if identity is not None:
        background_tasks.add_task(execute_knowledge_run, identity_id)
    return RedirectResponse(url="/knowledge", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
