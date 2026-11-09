"""Question-understanding tests on deliberately messy data (tests/fixtures/make_messy.py)."""
import os
from datetime import date

import pytest

import student_nlu as n

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def ask(q):
    r = n.answer(q)
    assert r is not None, q
    return r["answer"]


def test_spelling_variants_are_one_college():
    a = ask("What is the count of GMC Kadapa for all batches?")
    assert "**26 students**" in a and "GMC Kadapa" in a and "Kadappa" in a  # 14 + 8 + 4
    assert "Fathima" not in a.split("_")[0]  # different college in the same town


def test_full_name_and_initials():
    assert "**20 students**" in ask("How many students joined from Siddhartha Medical College Vijayawada?")
    assert "**20 students**" in ask("SMC students count")


def test_month_question_reports_zero_and_alternatives():
    a = ask("In October how many admissions were done at Narayana Medical College?")
    assert "couldn't find" in a and "joined in these months instead" in a


def test_comparison_lists_each_college():
    r = n.answer("GMC Kadapa vs Siddhartha vs Narayana")
    got = {row["College"]: row["students"] for row in r["rows"]}
    assert got["Government Medical College, Kadapa"] == 26 and got["Siddhartha Medical College, Vijayawada"] == 20
    assert any("Narayana" in k for k in got) and r["chart"]


def test_place_only_includes_all_colleges_there():
    a = ask("students from kadapa")
    assert "**32 students**" in a  # GMC 26 + Fathima 6


def test_batch_code_and_college_combined():
    assert "**10 students**" in ask("how many students in SSP2B batch from GMC Kadapa")


def test_pending_is_dues_not_status():
    a = ask("pending dues of GMC Kadapa students")
    assert "have dues" in a and "status" not in a.lower()


def test_unknown_college_suggests():
    assert "Did you mean" in ask("how many students from guntur medical college")


def test_period_parsing():
    s, e, label, assumed = n.parse_period("in October how many", today=date(2026, 11, 9))
    assert (s, e, assumed) == (date(2026, 10, 1), date(2026, 11, 1), True)
    s, e, label, _ = n.parse_period("between August and October 2025", today=date(2026, 11, 9))
    assert (s, e) == (date(2025, 8, 1), date(2025, 11, 1))
    s, _, _, assumed = n.parse_period("december admissions", today=date(2026, 11, 9))
    assert s == date(2025, 12, 1) and assumed


def test_followup_keeps_context():
    assert n.with_context("what about Siddhartha?", "GMC Kadapa batch wise").endswith("batch wise")
    assert "Narayana" in n.with_context("and in November 2025?", "Narayana admissions in 2026")
