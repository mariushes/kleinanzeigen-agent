from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class VehicleIdentity(Base):
    __tablename__ = "vehicle_identities"
    __table_args__ = (UniqueConstraint("canonical_label", name="uq_identity_canonical_label"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    generation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    engine_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    displacement_l: Mapped[float | None] = mapped_column(Float, nullable=True)
    fuel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trim: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canonical_label: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    listings: Mapped[list["Listing"]] = relationship(back_populates="identity")
    knowledge_entries: Mapped[list["KnowledgeEntry"]] = relationship(back_populates="identity")


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("kleinanzeigen_id", name="uq_listing_kleinanzeigen_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kleinanzeigen_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512))
    price_eur: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mileage_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    description_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seller_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    image_urls: Mapped[list] = mapped_column(JSON, default=list)

    identity_id: Mapped[int | None] = mapped_column(ForeignKey("vehicle_identities.id"), nullable=True)
    identity: Mapped[VehicleIdentity | None] = relationship(back_populates="listings")

    status: Mapped[str] = mapped_column(String(16), default="active")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    analyses: Mapped[list["Analysis"]] = relationship(back_populates="listing")


class BuyerCriteriaProfile(Base):
    """A named set of buyer requirements the analysis must judge each listing against.

    Deliberately data, not code: the camper-suitability wording lives in a seeded row
    (`app/criteria/seeds.py`), not in an `app/` branch — same rule as `knowledge_entries`.
    `free_text` is the user's own words (passed through verbatim), `flags` holds typed
    toggles/limits, and `aspects` is the list of things the extractor must rate:
    `[{"key": ..., "label": ..., "prompt": ...}]`.
    """

    __tablename__ = "buyer_criteria_profiles"
    __table_args__ = (UniqueConstraint("slug", name="uq_criteria_profile_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[dict] = mapped_column(JSON, default=dict)
    aspects: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    listing: Mapped[Listing] = relationship(back_populates="analyses")

    # Which buyer-criteria profile this verdict was judged under (NULL = none, the
    # pre-criteria behaviour). Stamped per analysis rather than read live, so a later run
    # under a different profile doesn't silently reinterpret this verdict.
    criteria_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("buyer_criteria_profiles.id"), nullable=True
    )
    criteria_profile: Mapped["BuyerCriteriaProfile | None"] = relationship()

    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    price: Mapped[dict] = mapped_column(JSON, default=dict)
    reliability: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-axis verdict: {overall_score, price/condition/reliability/positives:
    # {rating, note, has_data}}. Named after the Milestone-H holistic rework — it's the
    # verdict's axes, not a breakdown of an additive score (the old additive scorer is gone).
    verdict_axes: Mapped[dict] = mapped_column(JSON, default=dict)

    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reasoning_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CriteriaAssessment(Base):
    """Structured per-listing read against one buyer-criteria profile.

    Persisted alongside the verdict for the same reason the structured condition analysis
    is: the typed findings can be listed, filtered and reused later, even though the
    headline judgment is the single holistic call.
    """

    __tablename__ = "criteria_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    profile_id: Mapped[int] = mapped_column(ForeignKey("buyer_criteria_profiles.id"))
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id"), nullable=True)

    findings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_id: Mapped[int | None] = mapped_column(ForeignKey("vehicle_identities.id"), nullable=True)
    identity: Mapped[VehicleIdentity | None] = relationship(back_populates="knowledge_entries")

    entry_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    source_url: Mapped[str] = mapped_column(String(1024))
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_count: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KnowledgeResearchRun(Base):
    """Records which research angle has been covered for an identity, so a repeat
    collection run explores new angles instead of re-querying the same ones."""

    __tablename__ = "knowledge_research_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("vehicle_identities.id"))
    angle_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_url: Mapped[str] = mapped_column(String(1024))
    max_listings: Mapped[int] = mapped_column(Integer)
    # Buyer-criteria profile chosen on the dashboard when this run was started; every
    # listing analyzed by the run is judged under it.
    criteria_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("buyer_criteria_profiles.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    related_entity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
