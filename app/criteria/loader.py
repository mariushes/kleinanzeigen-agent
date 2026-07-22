"""Load buyer-criteria profiles from YAML files into the database.

Deliberately generic: this module knows nothing about campers or any other criteria set —
it reads every `*.yaml` in `app/criteria/profiles/` and upserts it by `slug`. The wording
lives in those files (data, version-controlled, diffable); adding a criteria set means
adding a file, never a code branch.

The YAML files are the *source*, the DB rows are the working copy. Loading is idempotent
and runs on app startup, so editing a file takes effect on the next start — that is the
current way to edit a profile, since there is no editor UI yet. Once profiles become
UI-editable, the loader stays useful as a first-run bootstrap.

Run manually with: `uv run python -m app.criteria.loader`
"""

from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.db.models import BuyerCriteriaProfile

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

# Fields carried from the YAML onto the row. `slug` is the identity and handled separately.
_FIELDS = ("name", "description", "free_text", "flags", "aspects")


class ProfileFileError(ValueError):
    """A profile YAML file is missing required fields or has the wrong shape."""


def parse_profile(raw: dict, source: str) -> dict:
    """Validate one profile mapping and return it normalized.

    Raises `ProfileFileError` rather than silently loading a half-formed profile: a
    profile with no aspects would produce a prompt that asks the model for nothing.
    """
    if not isinstance(raw, dict):
        raise ProfileFileError(f"{source}: expected a YAML mapping at the top level")

    slug = raw.get("slug")
    if not slug or not isinstance(slug, str):
        raise ProfileFileError(f"{source}: missing required string field 'slug'")
    if not raw.get("name"):
        raise ProfileFileError(f"{source}: missing required field 'name'")

    aspects = raw.get("aspects") or []
    if not isinstance(aspects, list) or not aspects:
        raise ProfileFileError(f"{source}: 'aspects' must be a non-empty list")
    for i, aspect in enumerate(aspects):
        if not isinstance(aspect, dict):
            raise ProfileFileError(f"{source}: aspect #{i + 1} must be a mapping")
        missing = [k for k in ("key", "label", "prompt") if not aspect.get(k)]
        if missing:
            raise ProfileFileError(
                f"{source}: aspect #{i + 1} ({aspect.get('key', '?')}) missing {', '.join(missing)}"
            )

    keys = [a["key"] for a in aspects]
    if len(set(keys)) != len(keys):
        raise ProfileFileError(f"{source}: duplicate aspect keys in {keys}")

    flags = raw.get("flags") or {}
    if not isinstance(flags, dict):
        raise ProfileFileError(f"{source}: 'flags' must be a mapping")

    return {
        "slug": slug,
        "name": raw["name"],
        "description": raw.get("description"),
        "free_text": raw.get("free_text"),
        "flags": flags,
        "aspects": aspects,
    }


def read_profile_files(directory: Path | None = None) -> list[dict]:
    """Parse every `*.yaml` in `directory` (default: the bundled profiles dir)."""
    directory = directory or PROFILES_DIR
    if not directory.is_dir():
        return []
    return [
        parse_profile(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)
        for path in sorted(directory.glob("*.yaml"))
    ]


def load_profiles(db: Session, directory: Path | None = None) -> list[BuyerCriteriaProfile]:
    """Upsert the profile files into `buyer_criteria_profiles`, keyed by slug.

    Idempotent. Profiles present in the DB but not on disk are left alone — the loader
    only ever adds or refreshes, so a future UI-created profile isn't deleted by a restart.
    """
    profiles = []
    for spec in read_profile_files(directory):
        profile = (
            db.query(BuyerCriteriaProfile).filter(BuyerCriteriaProfile.slug == spec["slug"]).first()
        )
        if profile is None:
            profile = BuyerCriteriaProfile(slug=spec["slug"])
            db.add(profile)
        for field in _FIELDS:
            setattr(profile, field, spec[field])
        profiles.append(profile)
    db.commit()
    return profiles


def main() -> None:
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        profiles = load_profiles(db)
        for profile in profiles:
            print(f"loaded profile {profile.slug!r} ({profile.name}) — {len(profile.aspects)} aspects")
    finally:
        db.close()


if __name__ == "__main__":
    main()
