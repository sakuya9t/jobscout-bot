"""Unit tests for the deterministic salary-range extractor. Fixtures are real
formats observed across employers/ATSes in the live DB (Anthropic/Greenhouse HTML,
Google's no-comma numbers, NVIDIA's trailing-currency multi-level ranges, Amazon's
far-apart geographic min/max and hourly, etc.)."""
from app.services.salary import SalaryRange, extract_salary


def _r(text):
    return extract_salary(text)


def test_dollar_range_with_commas():
    assert _r("The range for this role is $600,000.00 - $1,066,000.00.") == \
        SalaryRange(600000, 1066000, "USD", "year")


def test_google_no_thousands_comma():
    # $262000 (no comma) — missed by a comma-anchored regex; the explicit range saves it.
    assert _r("US: $262000 - $365000 (USD) + 25% bonus target") == \
        SalaryRange(262000, 365000, "USD", "year")


def test_trailing_currency_no_dollar_sign_multi_level():
    # NVIDIA lists multiple levels; we span the global min/max.
    txt = ("The base salary range is 104,000 USD - 166,750 USD for Level 3, "
           "and 128,000 USD - 201,250 USD for Level 4")
    assert _r(txt) == SalaryRange(104000, 201250, "USD", "year")


def test_non_usd_currency():
    assert _r("For Poland: The base salary range is 292,500 PLN - 507,000 PLN") == \
        SalaryRange(292500, 507000, "PLN", "year")


def test_explicit_code_beats_dollar_glyph():
    # Waymo/Ashby prints "$" even for TWD; the trailing code must win over the glyph,
    # on either side of the range (here only the max carries "TWD").
    txt = ('<div class="title">Salary Range</div><div class="pay-range"><span>$3,800,000</span>'
           '<span class="divider">&mdash;</span><span>$4,370,000 TWD</span></div>')
    assert _r(txt) == SalaryRange(3800000, 4370000, "TWD", "year")


def test_currency_inherited_across_range():
    # Only the second half carries "USD"; it covers the first half too.
    assert _r("USA, VA, Manassas - 32.00 - 57.00 USD hourly") == \
        SalaryRange(32, 57, "USD", "hour")


def test_geographic_min_max_far_apart():
    # The two halves are sentences apart, joined only by "/yr" markers + a pay anchor.
    txt = ("the base pay for this position ranges from $86,900/yr in our lowest "
           "geographic market up to $185,000/yr in our highest geographic market.")
    assert _r(txt) == SalaryRange(86900, 185000, "USD", "year")


def test_k_suffix():
    assert _r("The salary range for this role is $120k - $150k plus equity.") == \
        SalaryRange(120000, 150000, "USD", "year")


def test_html_entities_and_tags_are_stripped():
    txt = ('Annual Salary:</div><div class="pay-range"><span>$222,800</span>'
           '<span class="divider">&mdash;</span><span>$290,000 USD</span></div>')
    assert _r(txt) == SalaryRange(222800, 290000, "USD", "year")


def test_keeps_range_followed_by_equity():
    # "+ equity" after the max must NOT void the range.
    assert _r("Base salary range $180,000 - $200,000 + equity + benefits.") == \
        SalaryRange(180000, 200000, "USD", "year")


def test_rejects_referral_bonus():
    assert _r("We offer a $10,000 referral bonus and competitive compensation.") is None


def test_rejects_equity_only_amount():
    assert _r("Compensation includes $200,000 in equity grants.") is None


def test_rejects_funding_and_revenue_numbers():
    assert _r("We raised over $250,000,000 in funding and drove $1 trillion in billings.") is None


def test_rejects_vague_pay_language():
    assert _r("Base pay is dependent upon many factors such as training and experience.") is None


def test_empty_and_none():
    assert _r(None) is None
    assert _r("") is None
    assert _r("Senior Backend Engineer, fully remote.") is None


def test_as_fields_shape():
    fields = SalaryRange(100000, 150000, "USD", "year").as_fields()
    assert fields == {
        "salary_min": 100000, "salary_max": 150000,
        "salary_currency": "USD", "salary_period": "year",
    }
