"""Deterministic salary-range extraction from a job posting's text.

Pay-transparency laws (CA/CO/NY/WA + ~15 more states) push a growing share of
postings to state the pay range in the description we already scrape. The formats
vary wildly across employers/ATSes, though, so a naive ``$\\d+`` scan misses most
of them (see the analysis that motivated this module):

  * ``$120,000 - $150,000``            (Anthropic/Greenhouse, Netflix prose)
  * ``$262000 - $365000 (USD)``        (Google — no thousands comma)
  * ``104,000 USD - 166,750 USD``      (NVIDIA — trailing currency, no ``$``)
  * ``292,500 PLN - 507,000 PLN``      (NVIDIA — non-USD)
  * ``$86,900/yr ... up to $185,000/yr`` (Amazon — geographic min/max, far apart)
  * ``32.00 - 57.00 USD hourly``       (Amazon — hourly)

This is the cheap, free, deterministic pass: it finds *currency-anchored* money
tokens that sit under a pay-keyword umbrella (or carry their own ``/yr`` marker),
rejects bonus/equity/relocation amounts, then reports the min & max in the
dominant currency. The LLM scoring pass refines/overrides this for the subset it
scores (it reads the same description and catches prose the regex can't), so this
module deliberately errs toward precision: when unsure, return ``None`` rather
than a wrong number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Rough, static USD conversion rates used ONLY to order a mixed-currency salary sort
# (display always stays in the native currency). A sort needs sane relative order, not
# precision or live FX — without this a ₹7,250,000 (~$87k) role outranks a $300k one,
# or a "$4,370,000 TWD" (~$135k) one tops the list. ~96% of our parsed pay is USD.
# This dict is also the single source of truth for which currency *codes* we recognize
# in text (``_CURRENCIES`` below is derived from it), so the two never drift apart.
USD_RATES = {
    "USD": 1.0, "CAD": 0.73, "AUD": 0.66, "NZD": 0.61, "GBP": 1.27, "EUR": 1.08,
    "CHF": 1.12, "JPY": 0.0067, "CNY": 0.14, "INR": 0.012, "PLN": 0.25, "BRL": 0.18,
    "MXN": 0.055, "SGD": 0.74, "HKD": 0.128, "TWD": 0.031, "KRW": 0.00073,
    "ILS": 0.27, "AED": 0.27, "SAR": 0.27, "ZAR": 0.055, "THB": 0.028, "MYR": 0.22,
    "SEK": 0.095, "DKK": 0.145, "NOK": 0.093,
}

# Currency codes we recognize as a prefix/suffix in text. A bare ``$`` is assumed USD
# only when no explicit code accompanies it — many currencies print "$" (CAD/AUD/TWD/…),
# so an adjacent code always wins over the glyph (see ``_explicit_code``).
_CURRENCIES = "|".join(USD_RATES)

# A money token: optional currency prefix, a number in one of the real-world shapes
# above, optional trailing currency/k. Kept deliberately tight so 2-3 digit noise
# ("$36", "401(k)", "$20B", "$1 trillion") does NOT match.
_MONEY = re.compile(
    r"(?P<cur1>\$|" + _CURRENCIES + r")?\s?"
    r"(?P<num>"
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"   # 1,234 / 12,345.67  (>=4 digits via comma)
    r"|\d{4,7}(?:\.\d{1,2})?"             # 262000             (plain 4-7 digits)
    r"|\d{1,3}(?:\.\d+)?\s?[kK]"          # 120k / 85K
    r"|\d{2,3}\.\d{2}"                    # 32.00              (hourly, w/ cents)
    r")"
    r"\s?(?P<cur2>" + _CURRENCIES + r"|[kK])?",
    re.I,
)

# A pay-keyword "umbrella": a currency token within ~220 chars *after* one of these
# is treated as compensation even when no per-period marker is attached.
_PAY_ANCHOR = re.compile(
    r"salary|base pay|pay range|pay scale|compensation|the range for this role|"
    r"on-target earnings|\bOTE\b|/\s?yr|/\s?year|per year|per annum|annually|"
    r"hourly|per hour|/\s?hr",
    re.I,
)
_ANCHOR_WINDOW = 220  # chars an anchor reaches forward to bless a money token

# Per-period markers searched in the ~25 chars *after* a token to label it.
_PERIOD = [
    ("hour", re.compile(r"\s?(?:/\s?hr|/\s?hour|per hour|hourly|an hour)", re.I)),
    ("year", re.compile(r"\s?(?:/\s?yr|/\s?year|per year|per annum|annually|/\s?annum)", re.I)),
    ("month", re.compile(r"\s?(?:/\s?mo|per month|monthly|a month)", re.I)),
    ("week", re.compile(r"\s?(?:/\s?wk|per week|weekly|a week)", re.I)),
]
# Words right after a money token that mean it is NOT base pay (so we skip it). The
# optional "in/of/as" connector catches "$200,000 in equity" without also nuking a
# legitimate "$180,000 - $200,000 + equity" (a bare "+ equity" has no connector here).
_NOT_BASE = re.compile(
    r"\s{0,3}(?:\)|USD|per|/)?\s*(?:(?:in|of|as)\s+)?"
    r"(?:sign[\s-]?on|signing|referral|relocation|stipend|bonus|rsus?|equity|stock|"
    r"award|grant|gift|credit|discount|fee|donation|prize|funding|raised|"
    r"valuation|revenue|\bARR\b|sales|billings)",
    re.I,
)

# Separator that joins the two halves of an explicit range ("$120k - $150k").
_RANGE_SEP = re.compile(r"\s*(?:-|–|—|to)\s*")

# Sane bounds so a stray number can't masquerade as a salary.
_BOUNDS = {"year": (10_000, 10_000_000), "month": (1_000, 1_000_000),
           "week": (200, 200_000), "hour": (2, 2_000)}


@dataclass(frozen=True)
class SalaryRange:
    min: int
    max: int
    currency: str           # ISO-ish code, "USD" for a bare "$"
    period: str             # "year" | "hour" | "month" | "week"

    def as_fields(self) -> dict:
        """Column kwargs for persisting onto a Position."""
        return {
            "salary_min": self.min, "salary_max": self.max,
            "salary_currency": self.currency, "salary_period": self.period,
        }


_SYMBOLS = {"USD": "$", "CAD": "C$", "AUD": "A$", "NZD": "NZ$", "GBP": "£", "EUR": "€",
            "JPY": "¥", "CNY": "¥", "INR": "₹"}
_PERIOD_SUFFIX = {"year": "/yr", "hour": "/hr", "month": "/mo", "week": "/wk"}

# Multipliers to put every period on a common annual footing so ranges sort together
# (an $80/hr role outranks a $120k/yr one). 2080 = 40h × 52w. Mirror this in the SQL
# CASE used by the live job-list ordering (services.reporter).
ANNUAL_FACTOR = {"hour": 2080, "month": 12, "week": 52}  # "year" → 1


def annualize(value: int | None, period: str | None) -> int | None:
    """Scale a per-period amount to an annual figure for cross-period comparison."""
    if value is None:
        return None
    return value * ANNUAL_FACTOR.get(period or "year", 1)


def annual_usd(value: int | None, period: str | None, currency: str | None) -> float | None:
    """Annual figure converted to approximate USD — the common key for sorting a
    mixed-currency, mixed-period job list. None when there's no amount."""
    annual = annualize(value, period)
    if annual is None:
        return None
    return annual * USD_RATES.get((currency or "USD").upper(), 1.0)


def format_range(min_: int | None, max_: int | None,
                 currency: str | None, period: str | None) -> str | None:
    """Human-readable one-liner for a stored range, e.g. ``$120,000–$150,000/yr`` or
    ``PLN 292,500–507,000/yr``. Returns None when there's no range to show."""
    if min_ is None and max_ is None:
        return None
    lo = min_ if min_ is not None else max_
    hi = max_ if max_ is not None else min_
    cur = (currency or "USD").upper()
    sym = _SYMBOLS.get(cur)
    money = (lambda n: f"{sym}{n:,}") if sym else (lambda n: f"{n:,}")
    body = money(lo) if lo == hi else f"{money(lo)}–{money(hi)}"
    if not sym:
        body = f"{cur} {body}"
    return body + _PERIOD_SUFFIX.get(period or "year", "")


def _strip_markup(text: str) -> str:
    """Some ATSes (Anthropic/Greenhouse) leave HTML in the stored description, and the
    range separator is an entity (``&mdash;``). Decode the few entities we care about
    and drop tags so the numbers sit in plain text."""
    text = (text.replace("&mdash;", " - ").replace("&ndash;", " - ")
                .replace("&minus;", " - ").replace("&nbsp;", " ").replace("&amp;", "&"))
    return re.sub(r"<[^>]+>", " ", text)


def _to_int(num: str) -> float | None:
    num = num.strip()
    mult = 1
    if num[-1] in "kK":
        mult, num = 1_000, num[:-1].strip()
    num = num.replace(",", "")
    try:
        return float(num) * mult
    except ValueError:
        return None


def _period_after(text: str, end: int) -> str | None:
    window = text[end : end + 25]
    for name, rx in _PERIOD:
        if rx.match(window):
            return name
    return None


def _explicit_code(m: re.Match) -> str | None:
    """A 3-letter currency code attached to the token, if any (NOT the bare ``$``,
    which is ambiguous). This wins over ``$`` so "$4,370,000 TWD" reads as TWD."""
    for c in (m.group("cur1"), m.group("cur2")):
        if c and c != "$" and c not in ("k", "K"):
            return c.upper()
    return None


def _currency_of(m: re.Match) -> str | None:
    """Currency for one token: an explicit code if present, else USD for a bare ``$``,
    else None (no currency signal — token isn't trusted as money)."""
    return _explicit_code(m) or ("USD" if m.group("cur1") == "$" else None)


def _range_currency(a: re.Match, b: re.Match) -> str | None:
    """Currency for an explicit ``A - B`` range: an explicit code on EITHER side wins
    (so the trailing "TWD" in "$3.8M - $4.37M TWD" isn't lost to A's bare ``$``)."""
    code = _explicit_code(a) or _explicit_code(b)
    if code:
        return code
    return "USD" if (a.group("cur1") == "$" or b.group("cur1") == "$") else None


def extract_salary(text: str | None) -> SalaryRange | None:
    """Best-effort min/max pay range from a posting. Returns ``None`` when no
    confident currency-anchored figure is present."""
    if not text:
        return None
    text = _strip_markup(text)
    lower = text.lower()
    has_hourly = bool(re.search(r"/\s?hr|per hour|hourly|an hour", lower))
    has_annual = bool(re.search(r"/\s?yr|/\s?year|per year|per annum|annually|base salary|annual", lower))
    anchors = [m.start() for m in _PAY_ANCHOR.finditer(text)]
    matches = list(_MONEY.finditer(text))

    def under_anchor(pos: int) -> bool:
        return any(0 <= pos - a <= _ANCHOR_WINDOW for a in anchors)

    def decide_period(own: str | None, value: float) -> str:
        return own or ("hour" if (has_hourly and not has_annual and value < 2_000) else "year")

    # currency -> list[(value, period)]
    found: dict[str, list[tuple[float, str]]] = {}
    consumed: set[int] = set()  # match indices already taken by an explicit range

    def add(value: float | None, currency: str, period: str) -> None:
        if value is None:
            return
        lo, hi = _BOUNDS[period]
        if lo <= value <= hi:
            found.setdefault(currency, []).append((value, period))

    # Pass 1 — explicit adjacent ranges "$A - $B". The single strongest signal: trust
    # it on currency alone (no anchor needed), and let either side's currency cover both
    # ("32.00 - 57.00 USD", "$262000 - $365000").
    for i in range(len(matches) - 1):
        a, b = matches[i], matches[i + 1]
        if not _RANGE_SEP.fullmatch(text[a.end() : b.start()]):
            continue
        cur = _range_currency(a, b)
        if cur is None and not under_anchor(a.start()):
            continue
        if _NOT_BASE.match(text, b.end()):  # "$X - $Y bonus / equity"
            continue
        cur = cur or "USD"
        period = _period_after(text, b.end()) or _period_after(text, a.end())
        va, vb = _to_int(a.group("num")), _to_int(b.group("num"))
        add(va, cur, decide_period(period, va or 0))
        add(vb, cur, decide_period(period, vb or 0))
        consumed.update((i, i + 1))

    # Pass 2 — standalone currency tokens under a pay umbrella or carrying their own
    # period marker ("ranges from $86,900/yr ... up to $185,000/yr", where the two
    # halves are too far apart to be an adjacent range).
    for i, m in enumerate(matches):
        if i in consumed:
            continue
        cur = _currency_of(m)
        if cur is None or _NOT_BASE.match(text, m.end()):
            continue
        own_period = _period_after(text, m.end())
        if own_period is None and not under_anchor(m.start()):
            continue
        value = _to_int(m.group("num"))
        add(value, cur, decide_period(own_period, value or 0))

    if not found:
        return None
    currency = "USD" if "USD" in found else max(found, key=lambda c: len(found[c]))
    values = [v for v, _ in found[currency]]
    periods = [p for _, p in found[currency]]
    period = "hour" if periods.count("hour") > len(periods) / 2 else max(set(periods), key=periods.count)
    return SalaryRange(min=int(round(min(values))), max=int(round(max(values))),
                       currency=currency, period=period)
