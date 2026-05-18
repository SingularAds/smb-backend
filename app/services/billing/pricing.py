"""Billing Pricing Module — single source of truth for country → tier → price.

The backend is the source of truth. No pricing logic lives anywhere else.

Tier structure (from PRICING_MATRIX.md):
  T0     → €49 / €149
  T1     → €39 / €99
  T2     → €29 / €69  (default for unknown countries)
  T2.5   → €19 / €49
  T3     → €15 / €39
  T3-low → €9  / €29
  T4     → €7  / €19
"""

from __future__ import annotations

from typing import TypedDict


# ── Tier price table (EUR / month) ────────────────────────────────────────────

class TierPrices(TypedDict):
    starter: int  # EUR/month
    pro: int      # EUR/month


TIER_PRICES: dict[str, TierPrices] = {
    "T0":     {"starter": 49, "pro": 149},
    "T1":     {"starter": 39, "pro": 99},
    "T2":     {"starter": 29, "pro": 69},
    "T2.5":   {"starter": 19, "pro": 49},
    "T3":     {"starter": 15, "pro": 39},
    "T3-low": {"starter": 9,  "pro": 29},
    "T4":     {"starter": 7,  "pro": 19},
}

DEFAULT_TIER = "T2"  # Fall-back when country is unknown (T2 = Portugal pricing)


# ── Country → tier mapping (ISO 3166-1 alpha-2 upper-case) ───────────────────

COUNTRY_TIER: dict[str, str] = {
    # T0 — Premium
    "CH": "T0", "NO": "T0", "IS": "T0",

    # T1 — High Income
    "US": "T1", "GB": "T1", "CA": "T1", "IE": "T1", "AU": "T1", "NZ": "T1",
    "SG": "T1", "HK": "T1", "SE": "T1", "DK": "T1", "FI": "T1", "LU": "T1",
    "DE": "T1", "FR": "T1", "NL": "T1", "BE": "T1", "AT": "T1",
    "AE": "T1", "SA": "T1", "QA": "T1", "KW": "T1", "BH": "T1", "OM": "T1",
    "IL": "T1", "JP": "T1", "KR": "T1", "TW": "T1",

    # T2 — Western EU Mid Income
    "PT": "T2", "ES": "T2", "IT": "T2", "GR": "T2", "CY": "T2", "MT": "T2",

    # T2.5 — EU East Mid Income
    "PL": "T2.5", "CZ": "T2.5", "SK": "T2.5", "HU": "T2.5", "RO": "T2.5",
    "BG": "T2.5", "HR": "T2.5", "SI": "T2.5", "EE": "T2.5", "LV": "T2.5",
    "LT": "T2.5",

    # T3 — Lower Income
    "BR": "T3", "MX": "T3", "AR": "T3", "CL": "T3", "CO": "T3", "PE": "T3",
    "UY": "T3", "CR": "T3", "PA": "T3", "DO": "T3",
    "ZA": "T3", "MA": "T3", "EG": "T3",
    "ID": "T3", "PH": "T3", "MY": "T3", "VN": "T3", "TH": "T3", "TR": "T3",

    # T3-low — Low Income
    "IN": "T3-low", "PK": "T3-low", "BD": "T3-low", "LK": "T3-low",
    "NP": "T3-low", "NG": "T3-low", "KE": "T3-low", "GH": "T3-low",
    "TN": "T3-low", "DZ": "T3-low", "BO": "T3-low", "EC": "T3-low",
    "PY": "T3-low", "SV": "T3-low", "HN": "T3-low", "NI": "T3-low",
    "GT": "T3-low", "UA": "T3-low", "RS": "T3-low", "BA": "T3-low",
    "MK": "T3-low", "AL": "T3-low", "ME": "T3-low", "GE": "T3-low",
    "AM": "T3-low", "AZ": "T3-low",

    # T4 — Extreme Low Income
    "KH": "T4", "LA": "T4", "MM": "T4", "ET": "T4", "TZ": "T4",
    "UG": "T4", "VE": "T4",
}


# ── Phone calling-code → ISO country (longest-prefix-first matching) ─────────
# Used when country is not known from the lead or registration source.
# We sort descending by prefix length so more-specific codes match first.

_PHONE_PREFIX_COUNTRY: list[tuple[str, str]] = [
    # 3-digit prefixes (more specific; listed before 2-digit)
    ("351", "PT"), ("353", "IE"), ("354", "IS"), ("355", "AL"), ("356", "MT"),
    ("357", "CY"), ("358", "FI"), ("359", "BG"), ("370", "LT"), ("371", "LV"),
    ("372", "EE"), ("374", "AM"), ("380", "UA"), ("381", "RS"), ("382", "ME"),
    ("385", "HR"), ("386", "SI"), ("387", "BA"), ("389", "MK"),
    ("420", "CZ"), ("421", "SK"),
    ("880", "BD"), ("886", "TW"), ("852", "HK"), ("853", "MO"), ("855", "KH"),
    ("856", "LA"), ("960", "MV"), ("961", "LB"), ("962", "JO"),
    ("965", "KW"), ("966", "SA"), ("968", "OM"), ("971", "AE"), ("972", "IL"),
    ("973", "BH"), ("974", "QA"), ("975", "BT"), ("976", "MN"), ("977", "NP"),
    ("992", "TJ"), ("993", "TM"), ("994", "AZ"), ("995", "GE"),
    ("996", "KG"), ("998", "UZ"),
    ("212", "MA"), ("213", "DZ"), ("216", "TN"),
    ("218", "LY"), ("220", "GM"), ("221", "SN"), ("223", "ML"), ("224", "GN"),
    ("225", "CI"), ("226", "BF"), ("227", "NE"), ("228", "TG"), ("229", "BJ"),
    ("230", "MU"), ("231", "LR"), ("232", "SL"), ("233", "GH"), ("234", "NG"),
    ("237", "CM"), ("238", "CV"), ("240", "GQ"), ("241", "GA"), ("242", "CG"),
    ("243", "CD"), ("244", "AO"), ("245", "GW"), ("248", "SC"), ("249", "SD"),
    ("250", "RW"), ("251", "ET"), ("252", "SO"), ("253", "DJ"), ("254", "KE"),
    ("255", "TZ"), ("256", "UG"), ("257", "BI"), ("258", "MZ"), ("260", "ZM"),
    ("261", "MG"), ("263", "ZW"), ("264", "NA"), ("265", "MW"),
    ("266", "LS"), ("267", "BW"), ("268", "SZ"),
    # 2-digit prefixes
    ("20", "EG"), ("27", "ZA"), ("30", "GR"), ("31", "NL"), ("32", "BE"),
    ("33", "FR"), ("34", "ES"), ("36", "HU"), ("39", "IT"), ("40", "RO"),
    ("41", "CH"), ("43", "AT"), ("44", "GB"), ("45", "DK"), ("46", "SE"),
    ("47", "NO"), ("48", "PL"), ("49", "DE"),
    ("51", "PE"), ("52", "MX"), ("53", "CU"), ("54", "AR"), ("55", "BR"),
    ("56", "CL"), ("57", "CO"), ("58", "VE"),
    ("60", "MY"), ("61", "AU"), ("62", "ID"), ("63", "PH"),
    ("64", "NZ"), ("65", "SG"), ("66", "TH"),
    ("81", "JP"), ("82", "KR"), ("84", "VN"), ("86", "CN"),
    ("90", "TR"), ("91", "IN"), ("92", "PK"),
    ("94", "LK"), ("95", "MM"), ("98", "IR"),
    # 1-digit — US/Canada catch-all must be last
    ("1", "US"),
]
_PHONE_PREFIX_COUNTRY.sort(key=lambda t: len(t[0]), reverse=True)


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_country_from_phone(phone: str) -> str | None:
    """Infer ISO 3166-1 alpha-2 country code from a phone number's calling code.

    Returns None when the prefix doesn't match any known mapping.
    """
    digits = "".join(c for c in (phone or "") if c.isdigit()).lstrip("0")
    for prefix, iso in _PHONE_PREFIX_COUNTRY:
        if digits.startswith(prefix):
            return iso
    return None


def resolve_tier(country: str | None) -> str:
    """Return the billing tier string for a given ISO country code.

    Falls back to DEFAULT_TIER when country is unknown or not in the map.
    """
    if not country:
        return DEFAULT_TIER
    return COUNTRY_TIER.get(country.upper(), DEFAULT_TIER)


def resolve_prices(tier: str) -> TierPrices:
    """Return {starter, pro} monthly prices in EUR for the given tier.

    Falls back to T2 prices when tier is unrecognised.
    """
    return TIER_PRICES.get(tier, TIER_PRICES[DEFAULT_TIER])


def build_billing_snapshot(phone: str, country: str | None = None) -> dict:
    """Build the billing metadata dict to snapshot onto a business document.

    Country priority: explicit ``country`` argument > phone-prefix inference.
    The snapshot is stored at business-creation time; it never changes unless
    an admin explicitly overrides it.
    """
    resolved_country: str | None = country or resolve_country_from_phone(phone)
    tier = resolve_tier(resolved_country)
    prices = resolve_prices(tier)
    return {
        "billingCountry": resolved_country.upper() if resolved_country else None,
        "billingTier": tier,
        "starterPriceEur": prices["starter"],
        "proPriceEur": prices["pro"],
    }
