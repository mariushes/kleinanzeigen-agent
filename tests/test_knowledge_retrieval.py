"""Tiered retrieval of reliability knowledge for a vehicle identity.

The behaviour under test is mostly about *which knowledge is honestly applicable*:
a listing that never revealed its engine should see everything known about its model
line, while a listing with a known engine must not silently inherit another engine's
faults just because its own knowledge hasn't been collected yet.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, KnowledgeEntry, VehicleIdentity
from app.knowledge.retrieval import get_reliability_summary


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def an_identity(db, *, model="T5 Transporter", engine_code=None, generation=None, label=None):
    identity = VehicleIdentity(
        brand="Volkswagen",
        model=model,
        generation=generation,
        engine_code=engine_code,
        canonical_label=label or f"Volkswagen | {model} | {engine_code or 'n/a'}",
    )
    db.add(identity)
    db.commit()
    return identity


def add_entry(db, identity, component):
    db.add(
        KnowledgeEntry(
            identity_id=identity.id,
            entry_type="common_problem",
            payload={"component": component, "detail": f"{component} fails"},
            source_url="https://forum",
        )
    )
    db.commit()


def test_no_identity_means_no_coverage():
    summary = get_reliability_summary(make_db(), None)

    assert summary.has_coverage is False
    assert summary.tier is None


def test_exact_identity_is_preferred():
    db = make_db()
    identity = an_identity(db, engine_code="1.9 TDI 102 PS")
    add_entry(db, identity, "timing belt")

    summary = get_reliability_summary(db, identity)

    assert summary.tier == "exact_identity"
    assert [e.payload["component"] for e in summary.entries] == ["timing belt"]


def test_identity_without_an_engine_gets_all_knowledge_for_the_model():
    """An ad that never named its engine has no "exact" variant to be precise about, so
    everything known about the model line applies equally — that's the correct answer,
    not a degraded fallback."""
    db = make_db()
    vague = an_identity(db, engine_code=None, label="Volkswagen | T5 Transporter")
    sibling_a = an_identity(db, engine_code="1.9 TDI 102 PS")
    sibling_b = an_identity(db, engine_code="2.0 TDI 102 PS")
    add_entry(db, sibling_a, "timing belt")
    add_entry(db, sibling_b, "EGR cooler")

    summary = get_reliability_summary(db, vague)

    assert summary.tier == "model_wide"
    assert {e.payload["component"] for e in summary.entries} == {"timing belt", "EGR cooler"}


def test_model_wide_includes_the_vague_identitys_own_entries():
    db = make_db()
    vague = an_identity(db, engine_code=None, label="Volkswagen | T5 Transporter")
    sibling = an_identity(db, engine_code="2.0 TDI 102 PS")
    add_entry(db, vague, "sliding door")
    add_entry(db, sibling, "EGR cooler")

    summary = get_reliability_summary(db, vague)

    assert {e.payload["component"] for e in summary.entries} == {"sliding door", "EGR cooler"}


def test_model_wide_does_not_cross_model_lines():
    db = make_db()
    vague = an_identity(db, model="T5 Transporter", engine_code=None)
    other_model = an_identity(db, model="T6 Transporter", engine_code="2.0 TDI 150 PS")
    add_entry(db, other_model, "AdBlue system")

    summary = get_reliability_summary(db, vague)

    assert summary.has_coverage is False


def test_known_engine_still_falls_back_to_same_model_when_it_has_no_knowledge():
    """Documents current behaviour, and the reason it's flagged in the UI.

    This is the observed contamination case: a 1.9 TDI with no knowledge of its own
    inherits the 2.0 TDI's EGR/DPF faults, which are not its faults. Making this a
    scope-filtered union instead of an all-or-nothing fallback is the open rework in
    PLAN.md; until then the tier is surfaced prominently so the read isn't mistaken for
    an exact match.
    """
    db = make_db()
    target = an_identity(db, engine_code="1.9 TDI 102 PS")
    sibling = an_identity(db, engine_code="2.0 TDI 102 PS")
    add_entry(db, sibling, "EGR cooler")

    summary = get_reliability_summary(db, target)

    assert summary.tier == "same_model"
    assert [e.payload["component"] for e in summary.entries] == ["EGR cooler"]
