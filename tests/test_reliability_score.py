from app.analysis.reliability_score import assess_reliability_risk
from app.db.models import KnowledgeEntry


def entry(entry_type, component="x", severity=None, onset_km=None, stance=None):
    return KnowledgeEntry(
        entry_type=entry_type,
        payload={"component": component, "severity": severity, "onset_km": onset_km, "stance": stance},
        source_url="https://x",
    )


def test_no_entries_is_no_risk():
    risk = assess_reliability_risk([], tier=None, listing_mileage_km=100000)
    assert risk.level == "none"
    assert risk.penalty == 0


def test_catastrophic_past_onset_is_severe():
    entries = [entry("common_problem", "EGR cooler", severity="catastrophic", onset_km=120000)]
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=314000)
    assert risk.level == "severe"
    assert risk.penalty == 32  # 32 * 1.0
    assert any("EGR cooler" in d for d in risk.drivers)


def test_catastrophic_below_onset_is_high_not_severe():
    entries = [entry("common_problem", "EGR cooler", severity="catastrophic", onset_km=120000)]
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=60000)
    assert risk.level == "high"
    assert risk.penalty == 22


def test_unfavorable_variant_escalates_catastrophic_to_severe():
    entries = [
        entry("common_problem", "engine", severity="catastrophic"),
        entry("config_advice", "179 PS", stance="unfavorable"),
    ]
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=None)
    assert risk.level == "severe"


def test_tier_scaling_reduces_penalty():
    entries = [entry("common_problem", "EGR cooler", severity="catastrophic", onset_km=120000)]
    risk = assess_reliability_risk(entries, tier="same_model", listing_mileage_km=314000)
    assert risk.level == "severe"
    assert risk.penalty == round(32 * 0.6)  # 19


def test_unrated_problem_counts_as_moderate_and_flags_unrated():
    entries = [entry("common_problem", "mystery issue")]  # no severity
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=100000)
    assert risk.level == "low"  # moderate severity, no mileage trigger → low
    assert risk.has_unrated_entries is True


def test_major_problem_past_onset_is_high():
    entries = [entry("common_problem", "turbo", severity="major", onset_km=80000)]
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=200000)
    assert risk.level == "high"


def test_unfavorable_variant_alone_is_moderate():
    entries = [entry("config_advice", "179 PS biturbo", stance="unfavorable")]
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=100000)
    assert risk.level == "moderate"


def test_mileage_expectation_triggers_without_severity():
    entries = [
        entry("common_problem", "engine", severity="major"),
        entry("mileage_expectation", "overall", onset_km=150000),
    ]
    # major + mileage trigger → high
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=160000)
    assert risk.level == "high"
    assert any("past known trouble mileage" in d for d in risk.drivers)


def test_strengths_and_positive_overall_earn_bonus():
    entries = [
        entry("common_problem", "EGR cooler", severity="major", onset_km=120000),
        entry("strength", "gearbox"),
        entry("strength", "chassis"),
        entry("overall_assessment", "overall") ,
    ]
    entries[-1].payload["sentiment"] = "positive"
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=200000)

    # major + past onset → high (penalty 22); bonus = 2 strengths ×3 + 6 overall = 12
    assert risk.level == "high"
    assert risk.penalty == 22
    assert risk.bonus == 12
    assert risk.net_penalty == 10
    assert len(risk.positives) == 3


def test_favorable_config_advice_counts_as_strength():
    entries = [entry("config_advice", "1.9 TDI", stance="favorable")]
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=None)
    assert risk.level == "none"
    assert risk.bonus == 3
    assert risk.net_penalty == -3  # net positive effect on the score


def test_negative_overall_adds_penalty():
    entries = [entry("overall_assessment", "overall")]
    entries[0].payload["sentiment"] = "negative"
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=None)
    assert risk.penalty == 6
    assert "overall reputation: negative" in risk.drivers


def test_strength_bonus_is_capped():
    entries = [entry("strength", f"part{i}") for i in range(6)]
    risk = assess_reliability_risk(entries, tier="exact_identity", listing_mileage_km=None)
    assert risk.bonus == 9  # capped despite 6 strengths


def test_bonus_scales_with_match_tier():
    entries = [entry("strength", "gearbox"), entry("strength", "chassis")]
    risk = assess_reliability_risk(entries, tier="same_model", listing_mileage_km=None)
    assert risk.bonus == round(6 * 0.6)
