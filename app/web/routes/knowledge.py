from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import VehicleIdentity
from app.db.session import get_db
from app.jobs import execute_knowledge_run
from app.services.knowledge import (
    covered_research_angles,
    get_llm_spend,
    list_identity_coverage,
    list_identity_entries,
)
from app.web.templating import templates

router = APIRouter()


@router.get("/knowledge", response_class=HTMLResponse)
def knowledge_admin(request: Request, db: Session = Depends(get_db)):
    spend = get_llm_spend(db)
    return templates.TemplateResponse(
        request,
        "knowledge.html",
        {
            "coverage": list_identity_coverage(db),
            "llm_input_tokens": spend.input_tokens,
            "llm_output_tokens": spend.output_tokens,
            "llm_call_count": spend.call_count,
            "max_queries": get_settings().knowledge_default_max_queries,
        },
    )


@router.get("/knowledge/{identity_id}", response_class=HTMLResponse)
def knowledge_identity(identity_id: int, request: Request, db: Session = Depends(get_db)):
    identity = db.get(VehicleIdentity, identity_id)
    if identity is None:
        return templates.TemplateResponse(
            request, "knowledge_identity.html", {"identity": None}, status_code=404
        )
    return templates.TemplateResponse(
        request,
        "knowledge_identity.html",
        {
            "identity": identity,
            "entries": list_identity_entries(db, identity_id),
            "covered_angles": covered_research_angles(db, identity_id),
        },
    )


@router.post("/knowledge/{identity_id}/collect")
def collect_knowledge(
    identity_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    identity = db.get(VehicleIdentity, identity_id)
    if identity is not None:
        background_tasks.add_task(execute_knowledge_run, identity_id)
    return RedirectResponse(url="/knowledge", status_code=303)
