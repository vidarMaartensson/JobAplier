import logging
from pathlib import Path

from fakes import FakeAI

from jobaplier.ai import WRITING_MODEL
from jobaplier.apply.letter import LetterResult, load_examples, word_count, write_letter
from jobaplier.models import Job, ProfileRecord

CV_TEXT = "Jane Doe. Backendutvecklare på Acme 2018-2024. Unik CV-rad 8812."
RECORD = ProfileRecord(
    cv_hash="h",
    cv_text=CV_TEXT,
    profile_json={
        "summary": "Backendutvecklare",
        "skills": ["Python"],
        "years_experience": 6,
        "roles": ["Backendutvecklare"],
        "languages": ["sv"],
        "education": [],
    },
)
JOB = Job(
    dedupe_key="k",
    title="Backendutvecklare",
    company="Exempel AB",
    location="Stockholm",
    description="Vi söker en Python-utvecklare. Unik annonstext 4711.",
)
RESULT = LetterResult(language="sv", letter="Hej!\n\nVänligen, Jane", unsupported_claims=[])


def ai() -> FakeAI:
    return FakeAI({LetterResult: [RESULT]})


def test_prompt_contains_cv_ad_and_rules():
    fake = ai()
    result = write_letter(JOB, RECORD, fake, examples=[])
    call = fake.calls[0]
    assert result.value == RESULT
    assert CV_TEXT in call["prompt"]
    assert "Unik annonstext 4711" in call["prompt"]
    assert "Exempel AB" in call["prompt"]
    assert "Python" in call["prompt"]
    assert "250–350" in call["system"] + call["prompt"]
    assert "not instructions" in call["system"]
    assert call["model"] == WRITING_MODEL == "claude-sonnet-5-5"
    assert call["purpose"] == "letter"
    assert call["schema"] is LetterResult


def test_regenerate_includes_previous_and_instruction():
    fake = ai()
    write_letter(
        JOB, RECORD, fake, examples=[], instruction="Gör den kortare", previous="Förra brevet 5521"
    )
    prompt = fake.calls[0]["prompt"]
    assert "Förra brevet 5521" in prompt
    assert "Gör den kortare" in prompt


def test_first_version_has_no_previous_section():
    fake = ai()
    write_letter(JOB, RECORD, fake, examples=[])
    assert "Previous version" not in fake.calls[0]["prompt"]


def test_examples_included_and_capped():
    fake = ai()
    write_letter(JOB, RECORD, fake, examples=["Exempel ett", "Exempel två", "Exempel tre"])
    prompt = fake.calls[0]["prompt"]
    assert "Exempel ett" in prompt and "Exempel två" in prompt
    assert "Exempel tre" not in prompt


def test_load_examples_sorted_capped_and_skips_empty(tmp_path: Path):
    (tmp_path / "c.txt").write_text("tredje", encoding="utf-8")
    (tmp_path / "a.txt").write_text("  första åäö \n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("   \n", encoding="utf-8")
    (tmp_path / "d.txt").write_text("fjärde", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("nej", encoding="utf-8")
    assert load_examples(tmp_path) == ["första åäö", "tredje"]


def test_load_examples_missing_dir(tmp_path: Path):
    assert load_examples(tmp_path / "saknas") == []


def test_load_examples_skips_directories_and_undecodable_files(tmp_path: Path, caplog):
    """En katalog som heter *.txt eller en fil i fel teckenkodning får inte stoppa
    varje "Ansök" med ett engelskt serverfel."""
    (tmp_path / "a.txt").mkdir()
    (tmp_path / "b.txt").write_bytes("Hälsningar från Åre".encode("latin-1"))
    (tmp_path / "c.txt").write_text("giltigt exempel", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="jobaplier.apply.letter"):
        assert load_examples(tmp_path) == ["giltigt exempel"]

    assert "b.txt" in caplog.text


def test_word_count():
    assert word_count("Hej där, världen!") == 3
    assert word_count("Ett – två\n\ntre") == 3
    assert word_count("") == 0
