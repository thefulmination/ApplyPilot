"""Tests for applypilot.discovery.schema (Pydantic validation + null-rate metric)."""

from __future__ import annotations

import logging

import pytest

from applypilot.discovery.schema import SIGNAL_FIELDS, JobListing, validate_jobs


# ---------------------------------------------------------------------------
# JobListing model
# ---------------------------------------------------------------------------

class TestJobListingModel:
    def test_valid_minimal_job(self):
        job = JobListing.model_validate({"url": "https://example.com/job/1"})
        assert job.url == "https://example.com/job/1"

    def test_url_is_stripped(self):
        job = JobListing.model_validate({"url": "  https://example.com/  "})
        assert job.url == "https://example.com/"

    def test_empty_url_raises(self):
        with pytest.raises(Exception):
            JobListing.model_validate({"url": ""})

    def test_whitespace_only_url_raises(self):
        with pytest.raises(Exception):
            JobListing.model_validate({"url": "   "})

    def test_extra_fields_pass_through(self):
        job = JobListing.model_validate({
            "url": "https://example.com/job/2",
            "department": "Engineering",
            "some_custom_field": "hello",
        })
        dumped = job.model_dump()
        assert dumped.get("department") == "Engineering"
        assert dumped.get("some_custom_field") == "hello"

    def test_optional_fields_default_to_none(self):
        job = JobListing.model_validate({"url": "https://example.com/job/3"})
        assert job.title is None
        assert job.company is None
        assert job.location is None

    def test_all_fields_accepted(self):
        job = JobListing.model_validate({
            "url": "https://example.com/j",
            "title": "Software Engineer",
            "description": "Short desc",
            "full_description": "Long desc",
            "location": "Remote",
            "site": "LinkedIn",
            "company": "Acme Corp",
            "salary": "$100k",
            "application_url": "https://apply.example.com",
            "source_board": "linkedin",
            "strategy": "jobspy",
        })
        assert job.title == "Software Engineer"
        assert job.company == "Acme Corp"


# ---------------------------------------------------------------------------
# validate_jobs
# ---------------------------------------------------------------------------

class TestValidateJobs:
    def _make_job(self, url="https://example.com/job/1", **overrides) -> dict:
        base = {
            "url": url,
            "title": "Engineer",
            "full_description": "Some long description text",
            "location": "Remote",
            "company": "Acme",
        }
        base.update(overrides)
        return base

    def test_valid_jobs_pass_through(self):
        jobs = [self._make_job(url=f"https://example.com/{i}") for i in range(3)]
        valid, report = validate_jobs(jobs, board="test")
        assert len(valid) == 3
        assert report["valid"] == 3
        assert report["dropped_url"] == 0
        assert report["null_rate"] == 0.0

    def test_jobs_missing_url_are_dropped(self):
        jobs = [
            self._make_job(url="https://example.com/ok"),
            {"title": "No URL job"},
            {"url": ""},
        ]
        valid, report = validate_jobs(jobs, board="test")
        assert len(valid) == 1
        assert report["dropped_url"] == 2

    def test_report_structure(self):
        jobs = [self._make_job()]
        _, report = validate_jobs(jobs, board="myboard")
        assert report["board"] == "myboard"
        assert "total" in report
        assert "valid" in report
        assert "dropped_url" in report
        assert "null_counts" in report
        assert "null_rate" in report

    def test_null_counts_per_signal_field(self):
        jobs = [
            self._make_job(url="https://a.com/1", title=None),
            self._make_job(url="https://a.com/2", company=None),
            self._make_job(url="https://a.com/3"),
        ]
        _, report = validate_jobs(jobs, board="test")
        assert report["null_counts"]["title"] == 1
        assert report["null_counts"]["company"] == 1
        assert report["null_counts"]["full_description"] == 0

    def test_null_rate_is_fraction_of_valid_with_any_null(self):
        jobs = [
            self._make_job(url="https://a.com/1", title=None),
            self._make_job(url="https://a.com/2"),
            self._make_job(url="https://a.com/3"),
            self._make_job(url="https://a.com/4"),
        ]
        _, report = validate_jobs(jobs, board="test")
        assert report["null_rate"] == pytest.approx(0.25)

    def test_empty_string_signal_field_counts_as_null(self):
        jobs = [
            self._make_job(url="https://a.com/1", title="", location="  "),
        ]
        _, report = validate_jobs(jobs, board="test")
        assert report["null_counts"]["title"] == 1
        assert report["null_counts"]["location"] == 1
        assert report["null_rate"] == 1.0

    def test_empty_batch_returns_zero_null_rate(self):
        _, report = validate_jobs([], board="empty")
        assert report["null_rate"] == 0.0
        assert report["total"] == 0
        assert report["valid"] == 0

    def test_warn_logged_for_high_null_rate(self, caplog):
        jobs = [self._make_job(url=f"https://a.com/{i}", title=None) for i in range(5)]
        with caplog.at_level(logging.WARNING, logger="applypilot.discovery.schema"):
            validate_jobs(jobs, board="drifted_board")
        assert any("High null rate" in r.message for r in caplog.records)

    def test_warn_logged_for_dropped_urls(self, caplog):
        jobs = [{"title": "no url"}, {"url": ""}]
        with caplog.at_level(logging.WARNING, logger="applypilot.discovery.schema"):
            validate_jobs(jobs, board="bad_board")
        assert any("dropped" in r.message.lower() for r in caplog.records)

    def test_no_warn_below_threshold(self, caplog):
        # 10% null rate (1/10) should not trigger warning
        jobs = [self._make_job(url=f"https://a.com/{i}") for i in range(9)]
        jobs.append(self._make_job(url="https://a.com/9", title=None))
        with caplog.at_level(logging.WARNING, logger="applypilot.discovery.schema"):
            validate_jobs(jobs, board="ok_board")
        assert not any("High null rate" in r.message for r in caplog.records)

    def test_extra_fields_preserved_in_output(self):
        jobs = [self._make_job(custom_field="keep_me")]
        valid, _ = validate_jobs(jobs, board="test")
        assert valid[0].get("custom_field") == "keep_me"

    def test_signal_fields_constant(self):
        assert "title" in SIGNAL_FIELDS
        assert "full_description" in SIGNAL_FIELDS
        assert "location" in SIGNAL_FIELDS
        assert "company" in SIGNAL_FIELDS
