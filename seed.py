"""Generate deterministic synthetic fixture data for the Investor Services
Copilot (Vanterra Group).

Mirrors the structural pattern of the reference seed.py: builds
fully-formed, already-persisted-looking records (ids pre-assigned) rather
than API create-payloads, uses a fixed random seed for reproducibility, and
calls targeted inject_*() mutation passes on the generated data immediately
before writing -- so the same boundary/messy/insufficient-data fixtures
exist on every run, at the same indices, every time.

Seven related tables instead of the reference's three, in dependency
order:

- representatives.json  -- Representative roster (no FK to anything --
                            entitlement is session-binding only, a locked
                            design decision; see conversation history)
- parties.json           -- the Party (investor) catalog
- accounts.json           -- Account rows, party_id references parties.json
- positions.json          -- Position rows, account_id references accounts.json
- transactions.json       -- Transaction rows, account_id references accounts.json
- beneficiaries.json      -- Beneficiary rows, account_id references accounts.json
- trusted_contacts.json   -- TrustedContact rows, party_id references parties.json

Faker IS used here, unlike the reference file: chef/ingredient names in that
project were domain-specific nouns better served by curated pools, but
names/addresses/phones/emails here are genuinely generic-personal data --
exactly the case Faker is for. Domain-specific values (fund tickers, account
types, transaction types) still come from curated pools below, same
reasoning as the reference file's TITLES/CATEGORIES.

Backlog traceability
---------------------
TOOLS-02  Seed data: at least 50 investor (Party) records.
TOOLS-03  A record either side of every threshold in Section 2.3 -- the
          deterministic-rules boundary-test fixtures.
TOOLS-04  Messy records: missing beneficiary, no trusted contact, inactive
          account, inconsistent address formats, nulls.
TOOLS-05  At least one record that drives a rule to `insufficient_data`
          (a party with no birth year on file).

KNOWN GAP: most Section 2.3 thresholds (RMD start age by birth year, QCD
annual limit, wire-fraud callback window, mobile check-deposit limits,
margin-interest tiers) depend on the VG-OP-* operational/policy documents,
which have not been supplied yet. Only two thresholds are confirmed today,
from the VG-PR-* product corpus and the assignment brief itself:
  - $5,000 average account balance -> $25/yr service fee, waived with
    e-delivery (stated identically in VG-PR-001/002/003/004).
  - $50,000 outgoing wire -> fraud callback required (stated directly in
    the assignment brief, Section 7.1, as the worked boundary example).
Everything else in RULES_THRESHOLDS is a TODO pending the VG-OP-* corpus --
do not invent threshold values here.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timezone
from pathlib import Path

from faker import Faker

random.seed(42)
fake = Faker()
Faker.seed(42)

# ---------------------------------------------------------------------------
# Fixture output paths -- defined locally, same reasoning as the reference
# seed.py: no import-time dependency on the rest of the application package,
# so this script runs standalone.
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_REPRESENTATIVES_FIXTURE = DATA_DIR / "representatives.json"
DEFAULT_PARTIES_FIXTURE = DATA_DIR / "parties.json"
DEFAULT_ACCOUNTS_FIXTURE = DATA_DIR / "accounts.json"
DEFAULT_POSITIONS_FIXTURE = DATA_DIR / "positions.json"
DEFAULT_TRANSACTIONS_FIXTURE = DATA_DIR / "transactions.json"
DEFAULT_BENEFICIARIES_FIXTURE = DATA_DIR / "beneficiaries.json"
DEFAULT_TRUSTED_CONTACTS_FIXTURE = DATA_DIR / "trusted_contacts.json"

_FIXED_NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _iso_date(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------------------
# Domain pools (curated, not Faker -- product/domain nouns, not personal data)
# ---------------------------------------------------------------------------

# ticker -> approximate NAV/price, drawn directly from the VG-PR-* fact
# sheets and prospectuses supplied, so seeded Position rows look plausible
# rather than randomly priced. VHDY is approximated (no NAV in its fact
# sheet, only yield/ratio figures) -- flagged so it isn't mistaken for a
# corpus-confirmed figure like the rest.
TICKERS: dict[str, float] = {
    "VTMSI": 144.81,  # VG-PR-001, Total Stock Market Index Fund, Investor Shares
    "VTMSA": 144.81,  # VG-PR-001, Premier Shares (same portfolio, different NAV in reality;
                       # approximated equal here since no separate NAV was supplied)
    "VTMS": 144.81,   # VG-PR-001 / VG-PR-005, ETF Shares
    "V500I": 487.27,  # VG-PR-002, 500 Index Fund, Investor Shares
    "V500A": 487.27,  # VG-PR-002, Premier Shares
    "V500": 487.27,   # VG-PR-002, ETF Shares
    "VTBMI": 9.91,    # VG-PR-003, Total Bond Market Index Fund, Investor Shares
    "VTBMA": 9.91,    # VG-PR-003, Premier Shares
    "VTBM": 9.91,     # VG-PR-003, ETF Shares
    "VTR50": 46.86,   # VG-PR-004, Target Retirement 2050 Fund
    "VHDY": 68.40,    # VG-PR-006, High Dividend Yield ETF -- APPROXIMATED, not corpus-sourced
    "VFMMF": 1.00,    # VG-PR-007, Federal Money Market Fund, stable $1.00 NAV by design
}
TICKER_LIST = list(TICKERS)

ACCOUNT_TYPES = ["individual", "joint", "traditional_ira", "roth_ira", "margin"]
TRANSACTION_TYPES = ["wire", "ach", "check_deposit"]
TRANSACTION_STATUSES = ["completed", "pending", "held", "rejected"]
TRANSACTION_STATUS_WEIGHTS = [70, 15, 10, 5]
BENEFICIARY_RELATIONSHIPS = ["spouse", "child", "parent", "sibling", "trust", "other"]
TEAMS = ["Northeast Servicing", "Southeast Servicing", "Central Servicing", "West Servicing"]

# TODO(VG-OP-*): confirm published thresholds once the operational corpus is
# supplied -- RMD start age by birth year, QCD annual limit, mobile
# check-deposit daily/monthly limits, and margin-interest rate tiers all
# belong here and are NOT yet seeded anywhere in this file. Only the two
# thresholds below are corpus-confirmed as of this version.
RULES_THRESHOLDS = {
    "fee_waiver_balance": 5_000.00,        # VG-PR-001/002/003/004, confirmed
    "wire_callback_threshold": 50_000.00,  # brief Section 7.1, confirmed
}


# ---------------------------------------------------------------------------
# Generators, in dependency order
# ---------------------------------------------------------------------------

def make_representatives(n: int) -> list[dict]:
    """Representative roster. Carries no foreign key to Party or Account --
    entitlement is session-binding only (locked design decision), so there
    is deliberately no roster/book-of-business join table here.
    """
    reps = []
    for i in range(1, n + 1):
        reps.append({
            "rep_id": f"REP-{i:03d}",
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "employee_status": "active",
            "team": random.choice(TEAMS),
            "license_status": "licensed",
        })
    return reps


def make_parties(n: int) -> list[dict]:
    """Party (investor) catalog. TOOLS-02: at least 50 records (default)."""
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
            "phone": fake.phone_number(),
            "email": fake.email(),
            "e_delivery_election": random.random() < 0.6,
        })
    return parties


def make_accounts(parties: list[dict]) -> list[dict]:
    """1-3 accounts per party (an investor can hold several)."""
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
                "account_type": account_type,
                "opened_date": _iso_date(fake.date_between(start_date="-15y", end_date="-30d")),
                "status": "active",
                "cash_balance": cash_balance,
                "average_balance": round(cash_balance * random.uniform(0.85, 1.1), 2),
                "margin_enabled": margin_enabled,
                "debit_balance": round(random.uniform(0, 20_000), 2) if margin_enabled else 0.0,
            })
            counter += 1
    return accounts


def make_positions(accounts: list[dict]) -> list[dict]:
    """0-4 positions per account, tickers drawn from the confirmed fund list."""
    positions = []
    for account in accounts:
        n_positions = random.randint(0, 4)
        for ticker in random.sample(TICKER_LIST, k=n_positions):
            positions.append({
                "account_id": account["account_id"],
                "ticker": ticker,
                "quantity": round(random.uniform(1, 500), 3),
                "price": TICKERS[ticker],
            })
    return positions


def make_transactions(accounts: list[dict]) -> list[dict]:
    """0-5 transactions per account. 'held' status carries hold_start_date;
    all others leave it null.
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


def make_beneficiaries(accounts: list[dict]) -> list[dict]:
    """IRA accounts always get a beneficiary; other account types get one
    about half the time. Deliberate absence (TOOLS-04's 'no beneficiary'
    case) is handled by inject_messy_records(), not here, so that specific
    fixture is indexed and traceable rather than an accident of the coin
    flip below.
    """
    beneficiaries = []
    for account in accounts:
        is_ira = account["account_type"] in ("traditional_ira", "roth_ira")
        if not (is_ira or random.random() < 0.5):
            continue
        n = random.choice([1, 1, 2])
        remaining = 100.0
        for idx in range(n):
            pct = remaining if idx == n - 1 else round(random.uniform(10, remaining - 10), 1)
            remaining -= pct
            beneficiaries.append({
                "account_id": account["account_id"],
                "relationship": random.choice(BENEFICIARY_RELATIONSHIPS),
                "type": "primary" if idx == 0 else "contingent",
                "name": fake.name(),
                "allocation_pct": pct,
            })
    return beneficiaries


def make_trusted_contacts(parties: list[dict]) -> list[dict]:
    """~70% of parties get a trusted contact on file. Deliberate absence
    (TOOLS-04) is handled by inject_messy_records(), not here.
    """
    contacts = []
    for party in parties:
        if random.random() < 0.7:
            contacts.append({
                "party_id": party["party_id"],
                "name": fake.name(),
                "relationship": random.choice(BENEFICIARY_RELATIONSHIPS),
                "phone": fake.phone_number(),
            })
    return contacts


# ---------------------------------------------------------------------------
# Targeted mutation passes -- same mechanism as the reference inject_mess():
# specific, indexed edits applied to the generated data immediately before
# writing, so the same fixtures exist at the same identifiable records every
# run. Split into three functions (rather than one inject_mess()) because
# the backlog gives these three genuinely different jobs -- see the module
# docstring's traceability table.
# ---------------------------------------------------------------------------

def _first_account_for(accounts: list[dict], party_id: str) -> dict:
    return next(a for a in accounts if a["party_id"] == party_id)


def inject_boundary_fixtures(parties: list[dict], accounts: list[dict], transactions: list[dict]) -> None:
    """TOOLS-03: a record on each side of every CONFIRMED Section 2.3
    threshold. These are the fixtures RULES-11's boundary unit tests are
    written against, so the values themselves (not just their presence)
    are load-bearing -- do not "clean up" or re-round them later.

    Fee-waiver threshold ($5,000 average balance): four accounts covering
    all combinations of {just under, just over} x {e-delivery on, off} --
    matches the exact scenario named in the brief's Section 7.1 example.

    Wire callback threshold ($50,000): one pending wire just under, one
    at the threshold.

    TODO(VG-OP-*): RMD birth-year band edges, QCD annual limit, mobile
    check-deposit daily/monthly limits, and margin-interest tiers are NOT
    seeded here -- their source thresholds haven't been supplied yet. Add
    corresponding fixtures here once VG-OP-* lands; do not invent values.
    """
    fee_waiver = RULES_THRESHOLDS["fee_waiver_balance"]
    combos = [(-20, True), (-20, False), (20, True), (20, False)]  # (delta, e_delivery)
    for party, (delta, e_delivery) in zip(parties[0:4], combos):
        party["e_delivery_election"] = e_delivery
        account = _first_account_for(accounts, party["party_id"])
        account["average_balance"] = fee_waiver + delta
        account["cash_balance"] = fee_waiver + delta

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


def inject_messy_records(
    parties: list[dict],
    accounts: list[dict],
    beneficiaries: list[dict],
    trusted_contacts: list[dict],
) -> None:
    """TOOLS-04: schema-valid but operationally awkward records the tool
    layer must handle with defined behavior, not a stack trace. Unlike the
    reference inject_mess(), none of this violates a field constraint --
    every value here is legal for its type, just messy the way real
    servicing data actually is.
    """
    # No beneficiary on file for this account.
    no_beneficiary_party = parties[6]
    no_beneficiary_account = _first_account_for(accounts, no_beneficiary_party["party_id"])
    beneficiaries[:] = [
        b for b in beneficiaries if b["account_id"] != no_beneficiary_account["account_id"]
    ]

    # No trusted contact on file for this party.
    no_contact_party = parties[7]
    trusted_contacts[:] = [
        c for c in trusted_contacts if c["party_id"] != no_contact_party["party_id"]
    ]

    # Inactive account.
    inactive_party = parties[8]
    inactive_account = _first_account_for(accounts, inactive_party["party_id"])
    inactive_account["status"] = "inactive"

    # Inconsistent address format -- all-caps, comma stripped -- vs. the
    # standard "street, city, state zip" shape every other party gets.
    inconsistent_party = parties[9]
    inconsistent_party["address"] = inconsistent_party["address"].upper().replace(",", "")

    # Null phone -- schema allows it; not every servicing record has one.
    null_phone_party = parties[10]
    null_phone_party["phone"] = None


def inject_insufficient_data(parties: list[dict]) -> None:
    """TOOLS-05: a Party with no birth year on file, so the RMD rule has a
    fixture that must return insufficient_data (naming the missing field)
    rather than guessing or silently defaulting.
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
    accounts_out: str | Path = DEFAULT_ACCOUNTS_FIXTURE,
    positions_out: str | Path = DEFAULT_POSITIONS_FIXTURE,
    transactions_out: str | Path = DEFAULT_TRANSACTIONS_FIXTURE,
    beneficiaries_out: str | Path = DEFAULT_BENEFICIARIES_FIXTURE,
    trusted_contacts_out: str | Path = DEFAULT_TRUSTED_CONTACTS_FIXTURE,
) -> None:
    if n_parties < 12:
        n_parties = 12  # inject_* functions index up to parties[11]

    representatives = make_representatives(n_representatives)
    parties = make_parties(n_parties)
    accounts = make_accounts(parties)
    positions = make_positions(accounts)
    transactions = make_transactions(accounts)
    beneficiaries = make_beneficiaries(accounts)
    trusted_contacts = make_trusted_contacts(parties)

    inject_boundary_fixtures(parties, accounts, transactions)
    inject_messy_records(parties, accounts, beneficiaries, trusted_contacts)
    inject_insufficient_data(parties)

    _write(representatives_out, representatives)
    _write(parties_out, parties)
    _write(accounts_out, accounts)
    _write(positions_out, positions)
    _write(transactions_out, transactions)
    _write(beneficiaries_out, beneficiaries)
    _write(trusted_contacts_out, trusted_contacts)

    print(
        f"Wrote {len(representatives)} representatives to {representatives_out}\n"
        f"Wrote {len(parties)} parties to {parties_out} "
        f"(4 boundary [TOOLS-03], 4 messy [TOOLS-04], 1 insufficient_data [TOOLS-05])\n"
        f"Wrote {len(accounts)} accounts to {accounts_out}\n"
        f"Wrote {len(positions)} positions to {positions_out}\n"
        f"Wrote {len(transactions)} transactions to {transactions_out} "
        f"(2 boundary [TOOLS-03])\n"
        f"Wrote {len(beneficiaries)} beneficiaries to {beneficiaries_out}\n"
        f"Wrote {len(trusted_contacts)} trusted contacts to {trusted_contacts_out}"
    )


if __name__ == "__main__":
    main()
