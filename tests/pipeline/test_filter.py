from jobaplier.config import Preferences
from jobaplier.models import Job
from jobaplier.pipeline.filter import hard_filter


def make_job(**kw) -> Job:
    base = dict(
        dedupe_key="k",
        title="Backend Developer",
        company="Exempel AB",
        location="Stockholm",
        remote=False,
    )
    base.update(kw)
    return Job(**base)


def test_no_preferences_passes():
    assert hard_filter(make_job(), Preferences()) is None


def test_excluded_company_uses_normalization():
    prefs = Preferences(exclude_companies=["Exempel"])
    assert (
        hard_filter(make_job(company="Exempel AB"), prefs)
        == "Uteslutet företag: Exempel AB"
    )


def test_other_company_not_excluded():
    prefs = Preferences(exclude_companies=["Annat"])
    assert hard_filter(make_job(), prefs) is None


def test_excluded_keyword_in_title():
    prefs = Preferences(exclude_keywords=["Konsult"])
    job = make_job(title="Systemutvecklare, KONSULTuppdrag")
    assert hard_filter(job, prefs) == "Uteslutet nyckelord: Konsult"


def test_company_rule_comes_before_keyword_rule():
    prefs = Preferences(exclude_companies=["Exempel"], exclude_keywords=["backend"])
    assert hard_filter(make_job(), prefs) == "Uteslutet företag: Exempel AB"


def test_location_mismatch():
    prefs = Preferences(locations=["Göteborg"], remote_ok=False)
    assert hard_filter(make_job(location="Malmö"), prefs) == "Ort matchar inte: Malmö"


def test_location_substring_casefold_matches():
    prefs = Preferences(locations=["stockholm"])
    assert hard_filter(make_job(location="Stockholms län"), prefs) is None


def test_unknown_location_passes():
    prefs = Preferences(locations=["Göteborg"], remote_ok=False)
    assert hard_filter(make_job(location=None), prefs) is None


def test_remote_overrides_location():
    prefs = Preferences(locations=["Göteborg"], remote_ok=True)
    assert hard_filter(make_job(location="Malmö", remote=True), prefs) is None


def test_remote_does_not_override_when_remote_not_ok():
    prefs = Preferences(locations=["Göteborg"], remote_ok=False)
    job = make_job(location="Malmö", remote=True)
    assert hard_filter(job, prefs) == "Ort matchar inte: Malmö"


def test_remote_unknown_does_not_override_location():
    prefs = Preferences(locations=["Göteborg"], remote_ok=True)
    job = make_job(location="Malmö", remote=None)
    assert hard_filter(job, prefs) == "Ort matchar inte: Malmö"


def test_mid_only_drops_senior():
    prefs = Preferences(seniority=["junior", "mid"])
    job = make_job(title="Senior Backend Developer")
    assert hard_filter(job, prefs) == "Senioritet: senior"


def test_seniority_abbreviation_and_phrase_markers():
    prefs = Preferences(seniority=["mid"])
    assert hard_filter(make_job(title="Sr. Engineer"), prefs) == "Senioritet: senior"
    assert (
        hard_filter(make_job(title="Head of Engineering"), prefs) == "Senioritet: lead"
    )
    assert (
        hard_filter(make_job(title="Principal Engineer"), prefs) == "Senioritet: lead"
    )


def test_seniority_marker_needs_whole_word():
    prefs = Preferences(seniority=["mid"])
    assert hard_filter(make_job(title="Seniority Analyst"), prefs) is None
    assert hard_filter(make_job(title="Juniorsupport"), prefs) is None
    assert hard_filter(make_job(title="Leadership Coach"), prefs) is None


def test_allowed_seniority_passes_and_unmarked_title_passes():
    prefs = Preferences(seniority=["senior"])
    assert hard_filter(make_job(title="Senior Developer"), prefs) is None
    assert hard_filter(make_job(title="Developer"), prefs) is None


def test_empty_seniority_means_no_filter():
    assert hard_filter(make_job(title="Senior Developer"), Preferences()) is None
