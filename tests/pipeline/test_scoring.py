from fakes import FakeAI

from jobaplier.ai import SCORING_MODEL
from jobaplier.config import Preferences
from jobaplier.models import Job
from jobaplier.pipeline.scoring import ScoreResult, score_job
from jobaplier.profile.cv import Profile

PROFILE = Profile(
    summary="Backendutvecklare med fokus på Python",
    skills=["Python", "PostgreSQL"],
    years_experience=6,
    roles=["Backendutvecklare"],
    languages=["sv", "en"],
    education=["MSc Datateknik"],
)
PREFS = Preferences(roles=["Backend"], locations=["Stockholm"], exclude_keywords=["php"])
RESULT = ScoreResult(score=80, reasons="Bra match.", apply_method_hint="unknown")


def job(**kwargs) -> Job:
    base = dict(
        dedupe_key="k",
        title="Backendutvecklare",
        company="Exempel AB",
        location="Stockholm",
        remote=True,
        description="Vi söker en Python-utvecklare. Unik annonstext 4711.",
    )
    return Job(**{**base, **kwargs})


def test_prompt_contains_profile_prefs_and_ad():
    ai = FakeAI({ScoreResult: [RESULT]})
    out = score_job(job(), PROFILE, PREFS, ai)

    assert out.value == RESULT
    prompt = ai.calls[0]["prompt"]
    assert "Backendutvecklare med fokus på Python" in prompt
    assert "PostgreSQL" in prompt
    assert '"exclude_keywords"' in prompt and "php" in prompt
    assert "Exempel AB" in prompt
    assert "Stockholm" in prompt
    assert "Unik annonstext 4711" in prompt
    assert "Swedish" in ai.calls[0]["system"]


def test_incomplete_job_states_only_title_and_company_known():
    ai = FakeAI({ScoreResult: [RESULT]})
    score_job(
        job(incomplete=True, description="", location=None, remote=None),
        PROFILE,
        PREFS,
        ai,
    )
    prompt = ai.calls[0]["prompt"]
    assert "only the title and company are known" in prompt
    assert "Description:" not in prompt
    assert "Location: unknown" in prompt


def test_uses_haiku_low_effort():
    seen = {}

    class Spy(FakeAI):
        def complete(self, **kwargs):
            seen.update(kwargs)
            return super().complete(**kwargs)

    score_job(job(), PROFILE, PREFS, Spy({ScoreResult: [RESULT]}))

    assert seen["model"] == SCORING_MODEL == "claude-haiku-5-5"
    assert seen["effort"] == "low"
    assert seen["purpose"] == "scoring"
    assert seen["max_tokens"] == 1024
    assert seen["schema"] is ScoreResult
