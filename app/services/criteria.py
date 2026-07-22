"""Read-side data functions for buyer-criteria profiles.

HTTP- and template-agnostic like the other services: a session goes in, plain data comes
out, so the same functions back the web pages and (later) the chat agent's tools.

Read-only by design for now — profiles are authored as YAML in `app/criteria/profiles/`
and loaded by `app/criteria/loader.py`. When profiles become editable in the UI, the write
functions belong here.
"""

from sqlalchemy.orm import Session

from app.db.models import Analysis, BuyerCriteriaProfile, CriteriaAssessment


def list_profiles(db: Session) -> list[BuyerCriteriaProfile]:
    """All known profiles, in a stable order for rendering a dropdown."""
    return db.query(BuyerCriteriaProfile).order_by(BuyerCriteriaProfile.name).all()


def get_profile(db: Session, profile_id: int | None) -> BuyerCriteriaProfile | None:
    """A profile by id; None for "no criteria", which is a valid choice everywhere."""
    if profile_id is None:
        return None
    return db.get(BuyerCriteriaProfile, profile_id)


def get_profile_by_slug(db: Session, slug: str) -> BuyerCriteriaProfile | None:
    return db.query(BuyerCriteriaProfile).filter(BuyerCriteriaProfile.slug == slug).first()


def get_assessment(db: Session, analysis: Analysis | None) -> CriteriaAssessment | None:
    """The structured criteria findings behind one verdict, if it was judged under a profile."""
    if analysis is None or analysis.criteria_profile_id is None:
        return None
    return (
        db.query(CriteriaAssessment)
        .filter(CriteriaAssessment.analysis_id == analysis.id)
        .order_by(CriteriaAssessment.created_at.desc())
        .first()
    )
