import ast

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.criteria.loader import (
    PROFILES_DIR,
    ProfileFileError,
    load_profiles,
    parse_profile,
    read_profile_files,
)
from app.db.models import Base, BuyerCriteriaProfile

VALID = {
    "slug": "boat",
    "name": "Boat towing",
    "aspects": [{"key": "tow_hitch", "label": "Tow hitch", "prompt": "Is one fitted?"}],
}


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def write_profile(tmp_path, name: str, data: dict):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return tmp_path


def test_parse_profile_normalizes_optional_fields():
    parsed = parse_profile(VALID, "boat.yaml")

    assert parsed["slug"] == "boat"
    assert parsed["description"] is None
    assert parsed["free_text"] is None
    assert parsed["flags"] == {}


@pytest.mark.parametrize(
    "raw, expected_message",
    [
        ({**VALID, "slug": None}, "slug"),
        ({**VALID, "name": None}, "name"),
        ({**VALID, "aspects": []}, "non-empty list"),
        ({**VALID, "aspects": [{"key": "a", "label": "A"}]}, "prompt"),
        ({**VALID, "flags": ["not", "a", "mapping"]}, "flags"),
        (
            {
                **VALID,
                "aspects": [
                    {"key": "dup", "label": "A", "prompt": "p"},
                    {"key": "dup", "label": "B", "prompt": "p"},
                ],
            },
            "duplicate",
        ),
    ],
)
def test_parse_profile_rejects_malformed_files(raw, expected_message):
    """A half-formed profile would silently produce a prompt that asks for nothing, so the
    loader fails loudly instead."""
    with pytest.raises(ProfileFileError, match=expected_message):
        parse_profile(raw, "broken.yaml")


def test_load_profiles_inserts_then_upserts_by_slug(tmp_path):
    db = make_db()
    write_profile(tmp_path, "boat.yaml", VALID)

    load_profiles(db, tmp_path)
    assert db.query(BuyerCriteriaProfile).count() == 1

    # Editing the file and reloading updates in place rather than adding a second row.
    write_profile(tmp_path, "boat.yaml", {**VALID, "name": "Boat towing (revised)"})
    load_profiles(db, tmp_path)

    profile = db.query(BuyerCriteriaProfile).one()
    assert profile.name == "Boat towing (revised)"


def test_load_profiles_leaves_rows_that_have_no_file(tmp_path):
    """Profiles created outside the files (e.g. a future editor UI) must survive a restart."""
    db = make_db()
    db.add(BuyerCriteriaProfile(slug="handmade", name="Hand made", flags={}, aspects=[]))
    db.commit()

    write_profile(tmp_path, "boat.yaml", VALID)
    load_profiles(db, tmp_path)

    assert {p.slug for p in db.query(BuyerCriteriaProfile).all()} == {"handmade", "boat"}


def test_load_profiles_is_a_noop_for_a_missing_directory(tmp_path):
    db = make_db()

    assert load_profiles(db, tmp_path / "nope") == []


def test_bundled_profile_files_are_valid():
    """The shipped YAML must parse — it's the source of truth for the camper criteria."""
    profiles = read_profile_files()

    assert profiles, "expected at least one bundled profile"
    assert "camper" in {p["slug"] for p in profiles}


def test_bundled_camper_profile_is_usable():
    camper = next(p for p in read_profile_files() if p["slug"] == "camper")

    assert camper["free_text"]
    # The aspects the user specifically asked for: conversion status and build quality.
    keys = {a["key"] for a in camper["aspects"]}
    assert {"interior_status", "build_quality"} <= keys


def test_no_criteria_wording_leaks_into_app_code():
    """Criteria are data, not code: the camper wording lives in YAML only, so `app/` stays
    use-case agnostic — the same rule that keeps vehicle knowledge out of `app/`.

    Only *executable* code is checked. Comments and docstrings may of course say "camper"
    to explain the design; what must not exist is a string literal or identifier that makes
    the code behave differently for one criteria set.
    """
    banned = ("camper", "wohnmobil")
    offenders = []

    for path in sorted(PROFILES_DIR.parent.parent.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Identify docstring nodes by identity, not by text: `ast.get_docstring` returns
        # cleaned text that no longer equals the raw `ast.Constant` value.
        docstring_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    if isinstance(first.value.value, str):
                        docstring_nodes.add(id(first.value))

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstring_nodes:
                    continue
                if any(word in node.value.lower() for word in banned):
                    offenders.append(f"{path.name}:{node.lineno} string {node.value[:40]!r}")
            elif isinstance(node, ast.Name) and any(w in node.id.lower() for w in banned):
                offenders.append(f"{path.name}:{node.lineno} name {node.id!r}")
            elif isinstance(node, ast.Attribute) and any(w in node.attr.lower() for w in banned):
                offenders.append(f"{path.name}:{node.lineno} attribute {node.attr!r}")

    assert offenders == [], f"camper-specific wording found in app code: {offenders}"
