"""Generate deterministic synthetic fixture data for the Investor Services
Copilot (Vanterra Group).

v2 -- rewritten against the hand-drawn ERD supplied in conversation.
Structural changes from v1:
  - Representative is no longer un-linked: Account.assigned_rep_id is a
    real FK to Representative. This REVERSES the earlier "entitlement is
    session-binding only" decision -- flagged here because it changes what
    SEC-05/06/07's fixtures need to look like (see note below).
  - Products is a new, GLOBAL fund catalog (one row per ticker/share
    class -- Investor/Premier/ETF), not account-scoped. Position keeps its
    own ticker/quantity/price and looks NAV up from Products.
  - Renamed PKs to match the diagram: Representative.unique_id,
    Transaction.transaction_id (was UniqueID), Beneficiary.beneficiary_id
    and Balance/Position.position_id are now real PKs (previously
    Position had none).

Three fields are NOT in the diagram but are re-added here because a
locked rules-engine requirement depends on them and no other field
carries the value:
  - Account.debit_balance      -- RULES-07 (margin interest tier)
  - Transaction.hold_start_date -- RULES-09 (temporary hold duration)
  - Party.e_delivery_election   -- fee-waiver rule; also what TOOLS-03's
                                    four boundary fixtures are built around

Everything else mirrors the reference seed.py's structural pattern: fixed
random seed, fully-formed pre-persisted records, locally-defined fixture
paths, and targeted inject_*() mutation passes applied immediately before
writing.

Backlog traceability
---------------------
TOOLS-02  Seed data: at least 50 investor (Party) records.
TOOLS-03  A record either side of every threshold in Section 2.3 -- the
          deterministic-rules boundary-test fixtures.
TOOLS-04  Messy records: missing beneficiary, no trusted contact, inactive
          account, inconsistent address formats, nulls.
TOOLS-05  At least one record that drives a rule to `insufficient_data`
          (a party with no birth year on file).

v3 -- VG-OP-* operational corpus integrated (waves 1 & 2). RULES_THRESHOLDS
is now populated from real source documents; see inline citations. Two
structural changes from v2 worth flagging:

1. FEE-WAIVER RULE RESHAPED, not just re-thresholded. VG-OP-017 SS3 is the
   ground truth and it is NOT the single-account balance test v1/v2 built.
   The fee is waived if ANY of five conditions hold:
     (a) enrolled in e-delivery for all required documents
     (b) HOUSEHOLD assets >= $5,000 across ALL of the party's accounts
     (c) enrolled in Personal Advisor Services (any tier)
     (d) primary brokerage relationship
     (e) inherited IRA or certain employer-sponsored accounts
   Only (a) and (b) are testable with fields already in this schema --
   (c)/(d)/(e) would need new Party/Account fields (e.g. is_pas_enrolled)
   that don't exist yet and aren't added here without confirming scope.
   inject_boundary_fixtures() below seeds (a) and (b) only, and forces the
   four fee-waiver test parties to a SINGLE account each so "household
   total" is unambiguous and exactly on the boundary.

2. RMD start age for birth year <= 1950 is explicitly NOT a single number
   in VG-OP-003 SS3 ("70-1/2 or 72 depending on prior law") -- there is no
   one boundary value to seed for that band. Only the two real transition
   boundaries (1950->1951 and 1959->1960) are seeded; the pre-1951 band
   itself is left unfixtured pending clarification of which of the two
   ages applies to which sub-range.

KNOWN GAP, still open: QCD ($108,000/yr, age 70.5+) and the mobile-app
channel ceilings (wire >$10K, Roth conversion >$25K must use web/phone)
have confirmed values but no natural seed fixture in this schema -- QCD
isn't a modeled transaction type, and channel ceilings aren't a data-layer
concern. Both are recorded in RULES_THRESHOLDS for the rules engine to
consume directly; no corresponding boundary records exist in the JSON.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from faker import Faker

random.seed(42)
fake = Faker()
Faker.seed(42)

# ---------------------------------------------------------------------------
# Fixture output paths -- defined locally so this script has no import-time
# dependency on the rest of the application package.
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_REPRESENTATIVES_FIXTURE = DATA_DIR / "representatives.json"
DEFAULT_PARTIES_FIXTURE = DATA_DIR / "parties.json"
DEFAULT_PRODUCTS_FIXTURE = DATA_DIR / "products.json"
DEFAULT_ACCOUNTS_FIXTURE = DATA_DIR / "accounts.json"
DEFAULT_POSITIONS_FIXTURE = DATA_DIR / "positions.json"
DEFAULT_TRANSACTIONS_FIXTURE = DATA_DIR / "transactions.json"
DEFAULT_BENEFICIARIES_FIXTURE = DATA_DIR / "beneficiaries.json"
DEFAULT_TRUSTED_CONTACTS_FIXTURE = DATA_DIR / "trusted_contacts.json"

_FIXED_NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _iso_date(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------------------
# Domain pools
# ---------------------------------------------------------------------------

# ticker -> (fund_name, share_class, NAV). Drawn directly from the VG-PR-*
# fact sheets / prospectuses supplied. share_class distinguishes Investor /
# Premier / ETF shares of the same underlying fund, per the "the product is
# the ETF, index fund, and premier shares" clarification.
PRODUCT_CATALOG: dict[str, tuple[str, str, float]] = {
    "VTMSI": ("Total Stock Market Index Fund", "Investor", 144.81),  # VG-PR-001
    "VTMSA": ("Total Stock Market Index Fund", "Premier", 144.81),   # VG-PR-001
    "VTMS":  ("Total Stock Market Index Fund", "ETF", 144.81),       # VG-PR-001 / VG-PR-005
    "V500I": ("500 Index Fund", "Investor", 487.27),                 # VG-PR-002
    "V500A": ("500 Index Fund", "Premier", 487.27),                  # VG-PR-002
    "V500":  ("500 Index Fund", "ETF", 487.27),                      # VG-PR-002
    "VTBMI": ("Total Bond Market Index Fund", "Investor", 9.91),     # VG-PR-003
    "VTBMA": ("Total Bond Market Index Fund", "Premier", 9.91),      # VG-PR-003
    "VTBM":  ("Total Bond Market Index Fund", "ETF", 9.91),          # VG-PR-003
    "VTR50": ("Target Retirement 2050 Fund", "Investor", 46.86),     # VG-PR-004
    "VHDY":  ("High Dividend Yield ETF", "ETF", 68.40),              # VG-PR-006, NAV approximated
    "VFMMF": ("Federal Money Market Fund", "Investor", 1.00),        # VG-PR-007, stable $1.00 NAV
}
TICKER_LIST = list(PRODUCT_CATALOG)

ACCOUNT_TYPES = ["individual", "joint", "traditional_ira", "roth_ira", "margin"]
TRANSACTION_TYPES = ["wire", "ach", "check_deposit"]
TRANSACTION_STATUSES = ["completed", "pending", "held", "rejected"]
TRANSACTION_STATUS_WEIGHTS = [70, 15, 10, 5]
BENEFICIARY_TYPES = ["primary", "contingent"]
BENEFICIARY_RELATIONSHIPS = ["spouse", "child", "parent", "sibling", "trust", "other"]

# All values below are corpus-confirmed as of v3 -- see inline citations.
# Two items remain genuinely open, not just unfixtured: the fee-waiver
# rule's (c)/(d)/(e) conditions (need new schema fields, see module
# docstring) and the pre-1951 RMD age ("70-1/2 or 72 depending on prior
# law" -- VG-OP-003 doesn't give a single boundary value).
RULES_THRESHOLDS = {
    "fee_waiver_household_balance": 5_000.00,   # VG-OP-017 SS3 (supersedes VG-PR-*'s simpler framing)
    "wire_callback_threshold": 50_000.00,       # VG-OP-005 SS3.4 / VG-OP-011 SS5
    "qcd_annual_limit": 108_000.00,             # VG-OP-003 SS7, requires age >= 70.5
    "qcd_min_age": 70.5,                        # VG-OP-003 SS7
    "mobile_check_deposit_per_check": 100_000.00,  # VG-OP-016, "Mobile Check Deposit"
    "mobile_check_deposit_daily": 100_000.00,      # VG-OP-016
    "mobile_check_deposit_monthly": 250_000.00,    # VG-OP-016, rolling 30 days
    "temp_hold_initial_business_days": 15,      # VG-OP-013 SS2.5, FINRA Rule 2165
    "temp_hold_extension_business_days": 25,    # VG-OP-013 SS2.5
    "temp_hold_max_business_days": 40,          # VG-OP-013 SS2.5
    "ach_outgoing_daily_limit": 100_000.00,     # VG-OP-005 SS2.3
    "ach_incoming_daily_limit": 250_000.00,     # VG-OP-005 SS2.3
    "mfa_ach_threshold": 25_000.00,             # VG-OP-011 SS3.1 -- harness/auth concern, not a RULES output, kept here for the harness to consume
    "pdt_equity_minimum": 25_000.00,            # VG-OP-009 SS5
    "margin_account_minimum_equity": 2_000.00,  # VG-OP-009 SS3.1
}

# RMD start age by birth year (VG-OP-003 SS3, SECURE 2.0). The <=1950 band
# is intentionally NOT reduced to one number -- see module docstring.
RMD_AGE_BANDS = [
    {"born_through": 1950, "rmd_age": None, "note": "70-1/2 or 72 depending on prior law -- not a single seedable value"},
    {"born_min": 1951, "born_max": 1959, "rmd_age": 73},
    {"born_min": 1960, "born_max": None, "rmd_age": 75},
]

# Margin interest rate tiers by debit balance, March 2026 (VG-OP-009 SS4).
# (tier_floor, tier_ceiling_inclusive, annual_rate_pct); ceiling None = open-ended top tier.
MARGIN_INTEREST_TIERS = [
    (0.00, 24_999.99, 9.50),
    (25_000.00, 49_999.99, 9.00),
    (50_000.00, 99_999.99, 8.25),
    (100_000.00, 249_999.99, 7.50),
    (250_000.00, 499_999.99, 6.50),
    (500_000.00, 999_999.99, 6.00),
    (1_000_000.00, None, 5.50),
]


# ---------------------------------------------------------------------------
# Generators, in dependency order
# ---------------------------------------------------------------------------

def make_representatives(n: int) -> list[dict]:
    """No FK to Party -- but DOES get referenced by Account.assigned_rep_id
    (see Account below). Fields match the diagram exactly: no
    employee_status/team, unlike the earlier draft.
    """
    reps = []
    for i in range(1, n + 1):
        reps.append({
            "unique_id": f"REP-{i:03d}",
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "license_status": "licensed",
        })
    return reps


def make_parties(n: int) -> list[dict]:
    """Party (investor) catalog. TOOLS-02: at least 50 records (default).
    e_delivery_election is re-added (not in the diagram) -- required by
    the fee-waiver rule and by TOOLS-03's boundary fixtures.
    """
    parties = []
    for i in range(1, n + 1):
        dob = fake.date_of_birth(minimum_age=18, maximum_age=95)
        parties.append({
            "party_id": f"PTY-{i:05d}",
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "date_of_birth": _iso_date(dob),
            "tin_masked": f"***-**-{random.randint(1000, 9999)}",
            "address": f"{fake.street_address()}, {fake.city()}, {fake.state_abbr()} {fake.zipcode()}",
            "phone": fake.phone_number(),  # kept as str -- see conversation note on the int flag
            "email": fake.email(),
            "e_delivery_election": random.random() < 0.6,
        })
    return parties


def make_products() -> list[dict]:
    """Global fund catalog -- one row per ticker/share class. NOT
    account-scoped (the diagram's Account_ID FK on Products is dropped;
    a fund's ticker/NAV isn't owned by one account).
    """
    products = []
    for i, ticker in enumerate(TICKER_LIST, start=1):
        fund_name, share_class, nav = PRODUCT_CATALOG[ticker]
        products.append({
            "product_id": f"PRD-{i:05d}",
            "ticker": ticker,
            "fund_name": fund_name,
            "share_class": share_class,
            "nav": nav,
        })
    return products


def make_accounts(parties: list[dict], representatives: list[dict]) -> list[dict]:
    """1-3 accounts per party. assigned_rep_id is a real FK now (reverses
    the earlier session-binding-only entitlement decision -- see module
    docstring). debit_balance is re-added -- required by RULES-07 and not
    in the diagram.
    """
    accounts = []
    counter = 1
    for party in parties:
        for _ in range(random.randint(1, 3)):
            account_type = random.choice(ACCOUNT_TYPES)
            margin_enabled = account_type == "margin"
            cash_balance = round(random.uniform(100, 250_000), 2)
            accounts.append({
                "account_id": f"ACC-{counter:05d}",
                "party_id": party["party_id"],
                "assigned_rep_id": random.choice(representatives)["unique_id"],
                "account_type": account_type,
                "opened_date": _iso_date(fake.date_between(start_date="-15y", end_date="-30d")),
                "status": "active",
                "cash_balance": cash_balance,
                "margin_enabled": margin_enabled,
                "debit_balance": round(random.uniform(0, 20_000), 2) if margin_enabled else 0.0,
            })
            counter += 1
    return accounts


def make_positions(accounts: list[dict]) -> list[dict]:
    """0-4 positions per account. ticker looks up NAV from Products at
    generation time (price here is the position's own recorded price --
    it can drift from the catalog's current NAV over time, same as a real
    brokerage statement).
    """
    positions = []
    counter = 1
    for account in accounts:
        n_positions = random.randint(0, 4)
        for ticker in random.sample(TICKER_LIST, k=n_positions):
            _, _, nav = PRODUCT_CATALOG[ticker]
            positions.append({
                "position_id": f"POS-{counter:05d}",
                "account_id": account["account_id"],
                "ticker": ticker,
                "quantity": round(random.uniform(1, 500), 3),
                "price": nav,
            })
            counter += 1
    return positions


def make_transactions(accounts: list[dict]) -> list[dict]:
    """0-5 transactions per account. hold_start_date is re-added -- not in
    the diagram, but required by RULES-09 (temporary hold duration).
    """
    transactions = []
    counter = 1
    for account in accounts:
        for _ in range(random.randint(0, 5)):
            status = random.choices(TRANSACTION_STATUSES, weights=TRANSACTION_STATUS_WEIGHTS)[0]
            txn_date = fake.date_between(start_date="-1y", end_date="today")
            transactions.append({
                "transaction_id": f"TXN-{counter:06d}",
                "account_id": account["account_id"],
                "type": random.choice(TRANSACTION_TYPES),
                "amount": round(random.uniform(50, 40_000), 2),
                "date": _iso_date(txn_date),
                "status": status,
                "hold_start_date": _iso_date(txn_date) if status == "held" else None,
            })
            counter += 1
    return transactions


def make_beneficiaries(accounts: list[dict], max_per_account: int = 50) -> list[dict]:
    """IRA accounts always get beneficiaries; other types get them ~half
    the time. allocation_value is numeric (not str, per the diagram) so it
    can be validated to sum to 100 per account. Diagram labels this
    "(1 to 50)" as the per-account cardinality ceiling; typical accounts
    get 1-3 -- 50 is a hard cap, not a realistic default, so this stays
    weighted toward small counts rather than uniformly sampling up to 50.
    Deliberate absence (TOOLS-04) is handled in inject_messy_records().
    """
    beneficiaries = []
    counter = 1
    for account in accounts:
        is_ira = account["account_type"] in ("traditional_ira", "roth_ira")
        if not (is_ira or random.random() < 0.5):
            continue
        n = min(random.choice([1, 1, 2, 3]), max_per_account)
        remaining = 100.0
        for idx in range(n):
            pct = remaining if idx == n - 1 else round(random.uniform(10, remaining - 10), 1)
            remaining -= pct
            beneficiaries.append({
                "beneficiary_id": f"BEN-{counter:05d}",
                "account_id": account["account_id"],
                "type": "primary" if idx == 0 else "contingent",
                "name": fake.name(),
                "relationship": random.choice(BENEFICIARY_RELATIONSHIPS),
                "allocation_value": pct,
            })
            counter += 1
    return beneficiaries


def make_trusted_contacts(parties: list[dict]) -> list[dict]:
    """~70% of parties get a trusted contact on file (TCP in the diagram).
    Deliberate absence (TOOLS-04) is handled in inject_messy_records().
    """
    contacts = []
    counter = 1
    for party in parties:
        if random.random() < 0.7:
            contacts.append({
                "tcp_id": f"TCP-{counter:05d}",
                "party_id": party["party_id"],
                "name": fake.name(),
                "relationship": random.choice(BENEFICIARY_RELATIONSHIPS),
                "phone": fake.phone_number(),
            })
            counter += 1
    return contacts


# ---------------------------------------------------------------------------
# Targeted mutation passes -- same mechanism as the reference inject_mess():
# specific, indexed edits applied to the generated data immediately before
# writing. Split into three functions because the backlog gives these three
# genuinely different jobs -- see the module docstring's traceability table.
# ---------------------------------------------------------------------------

def _first_account_for(accounts: list[dict], party_id: str) -> dict:
    return next(a for a in accounts if a["party_id"] == party_id)


def _accounts_for(accounts: list[dict], party_id: str) -> list[dict]:
    return [a for a in accounts if a["party_id"] == party_id]


def _subtract_business_days(from_date: date, n: int) -> date:
    """Weekend-excluding business-day subtraction. Does NOT account for
    the VG-OP-015 federal-holiday calendar -- an approximation, flagged
    here rather than silently treated as exact. Close enough for boundary
    fixtures; a real rules engine should use the actual holiday calendar.
    """
    d = from_date
    remaining = n
    while remaining > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            remaining -= 1
    return d


def inject_boundary_fixtures(
    parties: list[dict],
    accounts: list[dict],
    transactions: list[dict],
    representatives: list[dict],
) -> None:
    """TOOLS-03: a record on each side of every CONFIRMED Section 2.3
    threshold, now sourced from the VG-OP-* corpus. Values are
    load-bearing -- RULES-11's boundary unit tests are written against
    them. Uses parties[0:19), so n_parties must be >= 19 (enforced in
    main()).

    Index map (documented here since later fixtures depend on earlier
    ones NOT touching the same records):
      parties[0:4]   fee waiver (household balance x e-delivery)
      parties[4:6]   wire callback threshold
      parties[6:11]  messy records (TOOLS-04, injected separately)
      parties[11]    insufficient_data (TOOLS-05, injected separately)
      parties[12:14] RMD 1950/1951 transition
      parties[14:16] RMD 1959/1960 transition
      parties[16]    owns appended margin-interest-tier + margin-minimum
                      -equity boundary accounts
      parties[17]    owns appended temporary-hold boundary transactions
      parties[18]    owns appended mobile-check-deposit boundary transactions
    """
    # --- Fee waiver: VG-OP-017 SS3 -- HOUSEHOLD balance, not single-account.
    # Force these four parties to exactly one account each so "household
    # total" is unambiguous and lands exactly on the boundary. Conditions
    # (c)/(d)/(e) of SS3 aren't seeded -- no schema field for them yet.
    fee_waiver = RULES_THRESHOLDS["fee_waiver_household_balance"]
    combos = [(-20, True), (-20, False), (20, True), (20, False)]  # (delta, e_delivery)
    for party, (delta, e_delivery) in zip(parties[0:4], combos):
        party["e_delivery_election"] = e_delivery
        party_accounts = _accounts_for(accounts, party["party_id"])
        primary = party_accounts[0]
        primary["cash_balance"] = fee_waiver + delta
        for extra in party_accounts[1:]:
            accounts.remove(extra)  # single account only -> household total == primary.cash_balance

    # --- Wire callback: VG-OP-005 SS3.4 / VG-OP-011 SS5.
    wire_threshold = RULES_THRESHOLDS["wire_callback_threshold"]
    account_under = _first_account_for(accounts, parties[4]["party_id"])
    account_at = _first_account_for(accounts, parties[5]["party_id"])
    transactions.append({
        "transaction_id": "TXN-BOUND-01",
        "account_id": account_under["account_id"],
        "type": "wire",
        "amount": wire_threshold - 1,   # $49,999 -- below threshold, no callback required
        "date": _iso_date(_FIXED_NOW.date()),
        "status": "pending",
        "hold_start_date": None,
    })
    transactions.append({
        "transaction_id": "TXN-BOUND-02",
        "account_id": account_at["account_id"],
        "type": "wire",
        "amount": wire_threshold,       # $50,000 -- at threshold, callback required
        "date": _iso_date(_FIXED_NOW.date()),
        "status": "pending",
        "hold_start_date": None,
    })

    # --- RMD start age transitions: VG-OP-003 SS3. Only the two real
    # transition boundaries are seeded -- the <=1950 band has no single
    # value to fixture (see module docstring).
    parties[12]["date_of_birth"] = _iso_date(date(1950, 6, 15))  # legacy band, deliberately unfixtured age
    parties[13]["date_of_birth"] = _iso_date(date(1951, 6, 15))  # age-73 band begins
    parties[14]["date_of_birth"] = _iso_date(date(1959, 6, 15))  # age-73 band, upper edge
    parties[15]["date_of_birth"] = _iso_date(date(1960, 6, 15))  # age-75 band begins

    # --- Margin interest tiers: VG-OP-009 SS4. One pair (just under the
    # tier's ceiling, just at the next tier's floor) per transition, all
    # owned by a single dedicated party so they don't interact with any
    # other fixture's account count.
    margin_owner = parties[16]["party_id"]
    counter = 90_000  # high, unlikely-to-collide account numbering for appended fixtures
    for floor, ceiling, _rate in MARGIN_INTEREST_TIERS:
        if ceiling is None:
            continue  # open-ended top tier has no upper edge to fixture
        counter += 1
        accounts.append({
            "account_id": f"ACC-BOUND-{counter:05d}",
            "party_id": margin_owner,
            "assigned_rep_id": random.choice(representatives)["unique_id"],
            "account_type": "margin",
            "opened_date": _iso_date(_FIXED_NOW.date()),
            "status": "active",
            "cash_balance": 5_000.00,
            "margin_enabled": True,
            "debit_balance": round(ceiling, 2),          # just inside the lower tier
        })
        counter += 1
        accounts.append({
            "account_id": f"ACC-BOUND-{counter:05d}",
            "party_id": margin_owner,
            "assigned_rep_id": random.choice(representatives)["unique_id"],
            "account_type": "margin",
            "opened_date": _iso_date(_FIXED_NOW.date()),
            "status": "active",
            "cash_balance": 5_000.00,
            "margin_enabled": True,
            "debit_balance": round(ceiling + 0.01, 2),   # just inside the next tier up
        })

    # --- Margin account minimum equity: VG-OP-009 SS3.1 ($2,000).
    margin_min = RULES_THRESHOLDS["margin_account_minimum_equity"]
    for delta in (-1.00, 0.00):
        counter += 1
        accounts.append({
            "account_id": f"ACC-BOUND-{counter:05d}",
            "party_id": margin_owner,
            "assigned_rep_id": random.choice(representatives)["unique_id"],
            "account_type": "margin",
            "opened_date": _iso_date(_FIXED_NOW.date()),
            "status": "active",
            "cash_balance": round(margin_min + delta, 2),
            "margin_enabled": True,
            "debit_balance": 0.0,
        })

    # --- Temporary hold: VG-OP-013 SS2.5, FINRA Rule 2165. Boundaries at
    # the initial-hold expiry (15 business days) and the absolute maximum
    # (40 business days). Approximate business-day math -- see
    # _subtract_business_days().
    hold_owner_account = _first_account_for(accounts, parties[17]["party_id"])
    today = _FIXED_NOW.date()
    hold_boundaries = [
        ("TXN-BOUND-HOLD-15", 15, "pending"),   # at initial-hold limit -- must extend or release
        ("TXN-BOUND-HOLD-16", 16, "held"),      # past initial limit -- in extension window
        ("TXN-BOUND-HOLD-40", 40, "held"),      # at absolute maximum
        ("TXN-BOUND-HOLD-41", 41, "pending"),   # past absolute maximum -- hold must be lifted
    ]
    for txn_id, business_days_ago, status in hold_boundaries:
        start = _subtract_business_days(today, business_days_ago)
        transactions.append({
            "transaction_id": txn_id,
            "account_id": hold_owner_account["account_id"],
            "type": "check_deposit",
            "amount": 15_000.00,
            "date": _iso_date(start),
            "status": status,
            "hold_start_date": _iso_date(start),
        })

    # --- Mobile check deposit: VG-OP-016 ($100,000 per check).
    deposit_owner_account = _first_account_for(accounts, parties[18]["party_id"])
    per_check = RULES_THRESHOLDS["mobile_check_deposit_per_check"]
    transactions.append({
        "transaction_id": "TXN-BOUND-DEPOSIT-01",
        "account_id": deposit_owner_account["account_id"],
        "type": "check_deposit",
        "amount": per_check - 1,   # $99,999 -- under per-check limit
        "date": _iso_date(_FIXED_NOW.date()),
        "status": "pending",
        "hold_start_date": None,
    })
    transactions.append({
        "transaction_id": "TXN-BOUND-DEPOSIT-02",
        "account_id": deposit_owner_account["account_id"],
        "type": "check_deposit",
        "amount": per_check,       # $100,000 -- at per-check limit
        "date": _iso_date(_FIXED_NOW.date()),
        "status": "pending",
        "hold_start_date": None,
    })


def inject_messy_records(
    parties: list[dict],
    accounts: list[dict],
    beneficiaries: list[dict],
    trusted_contacts: list[dict],
) -> None:
    """TOOLS-04: schema-valid but operationally awkward records the tool
    layer must handle with defined behavior, not a stack trace.
    """
    no_beneficiary_party = parties[6]
    no_beneficiary_account = _first_account_for(accounts, no_beneficiary_party["party_id"])
    beneficiaries[:] = [
        b for b in beneficiaries if b["account_id"] != no_beneficiary_account["account_id"]
    ]

    no_contact_party = parties[7]
    trusted_contacts[:] = [
        c for c in trusted_contacts if c["party_id"] != no_contact_party["party_id"]
    ]

    inactive_party = parties[8]
    inactive_account = _first_account_for(accounts, inactive_party["party_id"])
    inactive_account["status"] = "inactive"

    inconsistent_party = parties[9]
    inconsistent_party["address"] = inconsistent_party["address"].upper().replace(",", "")

    null_phone_party = parties[10]
    null_phone_party["phone"] = None


def inject_insufficient_data(parties: list[dict]) -> None:
    """TOOLS-05: a Party with no birth year on file, so the RMD rule has a
    fixture that must return insufficient_data rather than guessing.
    """
    parties[11]["date_of_birth"] = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _write(path: str | Path, data: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main(
    n_parties: int = 50,
    n_representatives: int = 12,
    representatives_out: str | Path = DEFAULT_REPRESENTATIVES_FIXTURE,
    parties_out: str | Path = DEFAULT_PARTIES_FIXTURE,
    products_out: str | Path = DEFAULT_PRODUCTS_FIXTURE,
    accounts_out: str | Path = DEFAULT_ACCOUNTS_FIXTURE,
    positions_out: str | Path = DEFAULT_POSITIONS_FIXTURE,
    transactions_out: str | Path = DEFAULT_TRANSACTIONS_FIXTURE,
    beneficiaries_out: str | Path = DEFAULT_BENEFICIARIES_FIXTURE,
    trusted_contacts_out: str | Path = DEFAULT_TRUSTED_CONTACTS_FIXTURE,
) -> None:
    if n_parties < 19:
        n_parties = 19  # inject_boundary_fixtures indexes up to parties[18]

    representatives = make_representatives(n_representatives)
    parties = make_parties(n_parties)
    products = make_products()
    accounts = make_accounts(parties, representatives)
    positions = make_positions(accounts)
    transactions = make_transactions(accounts)
    beneficiaries = make_beneficiaries(accounts)
    trusted_contacts = make_trusted_contacts(parties)

    inject_boundary_fixtures(parties, accounts, transactions, representatives)
    inject_messy_records(parties, accounts, beneficiaries, trusted_contacts)
    inject_insufficient_data(parties)

    _write(representatives_out, representatives)
    _write(parties_out, parties)
    _write(products_out, products)
    _write(accounts_out, accounts)
    _write(positions_out, positions)
    _write(transactions_out, transactions)
    _write(beneficiaries_out, beneficiaries)
    _write(trusted_contacts_out, trusted_contacts)

    print(
        f"Wrote {len(representatives)} representatives to {representatives_out}\n"
        f"Wrote {len(parties)} parties to {parties_out} "
        f"(4 boundary [TOOLS-03], 4 messy [TOOLS-04], 1 insufficient_data [TOOLS-05])\n"
        f"Wrote {len(products)} products to {products_out} (global fund catalog)\n"
        f"Wrote {len(accounts)} accounts to {accounts_out}\n"
        f"Wrote {len(positions)} positions to {positions_out}\n"
        f"Wrote {len(transactions)} transactions to {transactions_out} "
        f"(2 boundary [TOOLS-03])\n"
        f"Wrote {len(beneficiaries)} beneficiaries to {beneficiaries_out}\n"
        f"Wrote {len(trusted_contacts)} trusted contacts to {trusted_contacts_out}"
    )


if __name__ == "__main__":
    main()
