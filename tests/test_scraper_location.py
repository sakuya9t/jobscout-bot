"""Regression tests for JSON-LD location flattening — a role open in many offices
(Meta lists dozens) used to build a location string longer than the 512-char column,
which aborted the whole company's batched insert so Meta never persisted."""
from app.models import Position
from app.services.scraper import _jsonld_location


def _job(*locations):
    return {"jobLocation": [{"name": n} for n in locations]}


def test_single_location():
    assert _jsonld_location(_job("Menlo Park, CA")) == "Menlo Park, CA"


def test_few_locations_joined_plainly():
    out = _jsonld_location(_job("Menlo Park, CA", "New York, NY", "Seattle, WA"))
    assert out == "Menlo Park, CA; New York, NY; Seattle, WA"
    assert "more" not in out


def test_many_locations_capped_with_more_tail_and_fits_column():
    cities = [f"City Number {i}, ST" for i in range(60)]
    out = _jsonld_location(_job(*cities))
    col_limit = Position.__table__.c.location.type.length  # the actual varchar size
    assert len(out) <= col_limit          # never overflows the column
    assert out.endswith("more")           # remaining offices summarized
    assert out.startswith("City Number 0, ST")  # first offices kept verbatim


def test_name_preferred_over_address():
    job = {"jobLocation": [{"name": "London, UK",
                            "address": {"addressLocality": "Wrongville"}}]}
    assert _jsonld_location(job) == "London, UK"


def test_no_location():
    assert _jsonld_location({"jobLocation": []}) is None
    assert _jsonld_location({}) is None
