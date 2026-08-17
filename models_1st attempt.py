from __future__ import annotations

from datetime import date
from typing import Optional, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------
# Literal sets / domain constants
# --------------------------------------------------------------------------

AccountType = Literal["individual", "joint", "traditional_ira", "roth_ira", "margin"]
AccountStatus = Literal["active", "inactive"]

TransactionType = Literal["wire", "ach", "check_deposit"]
TransactionStatus = Literal["completed", "pending", "held", "rejected"]

BeneficiaryType = Literal["primary", "contingent"]
ShareClass = Literal["Investor", "Premier", "ETF"]
LicenseStatus = Literal["licensed", "unlicensed", "suspended"]

IRA_ACCOUNT_TYPES: set[str] = {"traditional_ira", "roth_ira"}


def _check_debit_balance_requires_margin(debit_balance: float, margin_enabled: bool) -> None:
    """Shared Account rule: a debit balance can only exist on a margin
    account (VG-OP-009 SS3-SS4 -- margin interest is charged on debit
    balances, which by definition only accrue on margin accounts).
    """
    if debit_balance > 0 and not margin_enabled:
        raise ValueError("debit_balance > 0 requires margin_enabled=True")


def _check_hold_start_date_requires_held(status: str, hold_start_date: Optional[date]) -> None:
    """Shared Transaction rule: hold_start_date is the anchor for the
    FINRA Rule 2165 / VG-OP-013 SS2.5 15+25+40 business-day clock, so a
    transaction currently in 'held' status must carry one.
    """
    if status == "held" and hold_start_date is None:
        raise ValueError("status 'held' requires hold_start_date")


# --------------------------------------------------------------------------
# Read models
# --------------------------------------------------------------------------

class Representative(BaseModel):
    """A rep who services investor accounts.

    No FK to Party or Account on THIS model -- the relationship runs the
    other way (Account.assigned_rep_id -> Representative.unique_id). This
    is the locked design decision from earlier in the project: a rep's
    entitlement to touch a given account is determined by that FK plus
    the live session binding, checked at the tool-dispatch layer -- NOT
    by anything this schema can express on its own. A Pydantic model
    validates shape; it has no session context to authorize against. See
    SEC-05/06/07 for where that check actually lives.
    """

    unique_id: str
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    license_status: LicenseStatus


class Party(BaseModel):
    """The investor. date_of_birth is Optional -- None is a legitimate,
    reachable state (TOOLS-05's insufficient_data fixture), not an error;
    any RMD-band calculation reading this field must handle None
    explicitly rather than assume it's always present.
    """

    party_id: str
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: Optional[date] = None
    tin_masked: str = Field(min_length=1, max_length=20)
    address: str = Field(min_length=1)
    phone: Optional[str] = None  # nullable -- TOOLS-04's messy-record fixture
    email: str = Field(min_length=3)
    e_delivery_election: bool


class Product(BaseModel):
    """Global fund catalog entry -- one row per ticker/share class, NOT
    account-scoped (see conversation history: the diagram's Account_ID FK
    on this table was dropped as a drafting artifact).
    """

    product_id: str
    ticker: str = Field(min_length=1, max_length=10)
    fund_name: str = Field(min_length=1, max_length=200)
    share_class: ShareClass
    nav: float = Field(gt=0)


class Account(BaseModel):
    """assigned_rep_id is a real FK to Representative -- this is the
    Representative-logic reversal from the session-binding-only design:
    an account is assigned to a specific rep, not just whoever's session
    happens to be bound to it at call time. debit_balance can only be
    nonzero on a margin account (enforced below).

    NOT enforced here, and deliberately so: VG-OP-002's requirement that
    IRA account types carry a beneficiary designation. That's a
    cross-entity constraint (Account + Beneficiary are separate top-level
    tables, not embedded), so it belongs at the store/rules-engine layer,
    not in a single-record Pydantic model that can't see the Beneficiary
    table.
    """

    account_id: str
    party_id: str
    assigned_rep_id: str
    account_type: AccountType
    opened_date: date
    status: AccountStatus
    cash_balance: float = Field(ge=0)
    margin_enabled: bool
    debit_balance: float = Field(ge=0, default=0.0)

    @model_validator(mode="after")
    def _validate_debit_balance(self) -> "Account":
        _check_debit_balance_requires_margin(self.debit_balance, self.margin_enabled)
        return self


class Position(BaseModel):
    """A holding within an account. ticker is a soft reference into the
    Product catalog (not a DB-enforced FK at this layer) -- the store
    layer is responsible for rejecting a ticker with no matching Product.
    """

    position_id: str
    account_id: str
    ticker: str = Field(min_length=1, max_length=10)
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)


class Transaction(BaseModel):
    """hold_start_date is required exactly when status is 'held' --
    enforced below. It's allowed to persist on other statuses too (e.g. a
    transaction that WAS held and is now completed/rejected keeps its
    historical hold_start_date rather than losing it), so the check is
    one-directional, not a strict iff.
    """

    transaction_id: str
    account_id: str
    type: TransactionType
    amount: float = Field(gt=0)
    date: date
    status: TransactionStatus
    hold_start_date: Optional[date] = None

    @model_validator(mode="after")
    def _validate_hold_start_date(self) -> "Transaction":
        _check_hold_start_date_requires_held(self.status, self.hold_start_date)
        return self


class Beneficiary(BaseModel):
    """allocation_value is validated per-record (0, 100]. The "all
    beneficiaries on one account must sum to 100%" rule (VG-OP-002 SS3.1)
    is a cross-record constraint this single-Beneficiary model can't see
    -- that check belongs at the store layer, over all Beneficiary rows
    sharing an account_id, the same way RecipeQuery's min/max check is
    single-record but Recipe-wide consistency lives elsewhere.
    """

    beneficiary_id: str
    account_id: str
    type: BeneficiaryType
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=50)
    allocation_value: float = Field(gt=0, le=100)


class TrustedContact(BaseModel):
    """VG-OP-013's TCP. No authority to act on the account -- this model
    intentionally carries no permissions/authorization fields, since a
    TCP is a contact point only (see VG-OP-013 SS2.1).
    """

    tcp_id: str
    party_id: str
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=1, max_length=30)


# --------------------------------------------------------------------------
# Write models -- Representative
# --------------------------------------------------------------------------

class RepresentativeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    license_status: LicenseStatus = "licensed"


class RepresentativePatch(BaseModel):
    """All fields optional; only sent fields are applied. Does NOT include
    a way to bulk-reassign this rep's accounts -- that's a separate,
    explicit operation (AccountPatch.assigned_rep_id, one account at a
    time), not a side effect of patching the rep's own profile.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    license_status: Optional[LicenseStatus] = None


class RepresentativeDelete(BaseModel):
    """Confirmation payload -- client echoes the id back. Store-layer
    question this schema can't answer on its own (same shape as the
    Chef/Ingredient cascade questions in the reference file): what
    happens to Accounts still pointing at this unique_id via
    assigned_rep_id? Block the delete, or require reassignment first?
    Left as an open store-layer decision, not resolved here.
    """

    model_config = ConfigDict(extra="forbid")

    unique_id: str


class RepresentativeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    license_status: Optional[LicenseStatus] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None


# --------------------------------------------------------------------------
# Write models -- Party
# --------------------------------------------------------------------------

class PartyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: Optional[date] = None
    tin_masked: str = Field(min_length=1, max_length=20)
    address: str = Field(min_length=1)
    phone: Optional[str] = None
    email: str = Field(min_length=3)
    e_delivery_election: bool = False


class PartyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    date_of_birth: Optional[date] = None
    address: Optional[str] = Field(default=None, min_length=1)
    phone: Optional[str] = None
    email: Optional[str] = Field(default=None, min_length=3)
    e_delivery_election: Optional[bool] = None


class PartyDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    party_id: str


class PartyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_name: Optional[str] = None  # partial match
    e_delivery_election: Optional[bool] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None


# --------------------------------------------------------------------------
# Write models -- Account
# --------------------------------------------------------------------------

class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    party_id: str
    assigned_rep_id: str
    account_type: AccountType
    opened_date: date
    cash_balance: float = Field(ge=0, default=0.0)
    margin_enabled: bool = False
    debit_balance: float = Field(ge=0, default=0.0)

    @model_validator(mode="after")
    def _validate_debit_balance(self) -> "AccountCreate":
        _check_debit_balance_requires_margin(self.debit_balance, self.margin_enabled)
        return self


class AccountPatch(BaseModel):
    """assigned_rep_id here is the direct analogue of RecipePatch.chef_id
    -- reassigning an account to a different representative, same pattern
    as reassigning a recipe to a different chef. This is the concrete
    place "the Representative logic" surfaces in the write path.

    status is included (unlike most fields, deliberately settable) since
    TOOLS-04's inactive-account fixture is exactly this transition in
    practice.
    """

    model_config = ConfigDict(extra="forbid")

    assigned_rep_id: Optional[str] = None  # reassign to a different representative
    status: Optional[AccountStatus] = None
    cash_balance: Optional[float] = Field(default=None, ge=0)
    margin_enabled: Optional[bool] = None
    debit_balance: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_debit_balance_if_both_present(self) -> "AccountPatch":
        if self.debit_balance is not None and self.margin_enabled is not None:
            _check_debit_balance_requires_margin(self.debit_balance, self.margin_enabled)
        return self


class AccountDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str


class AccountQuery(BaseModel):
    """assigned_rep_id filter is what makes "show me my book" a real,
    servable query for a rep -- the other concrete surface of the
    Representative relationship, on the read side.
    """

    model_config = ConfigDict(extra="forbid")

    party_id: Optional[str] = None
    assigned_rep_id: Optional[str] = None
    account_type: Optional[AccountType] = None
    status: Optional[AccountStatus] = None
    margin_enabled: Optional[bool] = None
    min_cash_balance: Optional[float] = Field(default=None, ge=0)
    max_cash_balance: Optional[float] = Field(default=None, ge=0)
    sort: Optional[Literal["cash_balance", "-cash_balance", "opened_date", "-opened_date"]] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None

    @model_validator(mode="after")
    def _validate_balance_range(self) -> "AccountQuery":
        if self.min_cash_balance is not None and self.max_cash_balance is not None:
            if self.min_cash_balance > self.max_cash_balance:
                raise ValueError("min_cash_balance must be <= max_cash_balance")
        return self


# --------------------------------------------------------------------------
# Write models -- Product catalog
# --------------------------------------------------------------------------

class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=10)
    fund_name: str = Field(min_length=1, max_length=200)
    share_class: ShareClass
    nav: float = Field(gt=0)


class ProductPatch(BaseModel):
    """NAV is the only field expected to change often (daily); fund_name
    and share_class patches are legitimate (fund rename, share-class
    correction) but rare -- included for completeness, not frequent use.
    """

    model_config = ConfigDict(extra="forbid")

    fund_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    share_class: Optional[ShareClass] = None
    nav: Optional[float] = Field(default=None, gt=0)


class ProductDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str


class ProductQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: Optional[str] = None
    share_class: Optional[ShareClass] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None


# --------------------------------------------------------------------------
# Write models -- Position
# --------------------------------------------------------------------------

class PositionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    ticker: str = Field(min_length=1, max_length=10)
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)


class PositionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, gt=0)


class PositionDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str


class PositionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: Optional[str] = None
    ticker: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None


# --------------------------------------------------------------------------
# Write models -- Transaction
# --------------------------------------------------------------------------

class TransactionCreate(BaseModel):
    """No `status` field on create -- every new transaction starts
    'pending' by store-layer default, the same way RecipeCreate omits
    ingredients because a freshly-created recipe is legitimately a draft.
    Status transitions happen via TransactionPatch.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: str
    type: TransactionType
    amount: float = Field(gt=0)
    date: date


class TransactionPatch(BaseModel):
    """The status transition surface, including the hold lifecycle
    (VG-OP-013 SS2.5). hold_start_date must be set in the same PATCH that
    sets status='held', since the cross-field check below needs both
    values present together to validate.
    """

    model_config = ConfigDict(extra="forbid")

    status: Optional[TransactionStatus] = None
    hold_start_date: Optional[date] = None

    @model_validator(mode="after")
    def _validate_hold_start_date_if_status_present(self) -> "TransactionPatch":
        if self.status is not None:
            _check_hold_start_date_requires_held(self.status, self.hold_start_date)
        return self


class TransactionDelete(BaseModel):
    """Transactions are financial records -- deletion should be rare/
    disallowed in a real system (corrections are usually a reversing
    entry, not a delete). Included for schema completeness/symmetry with
    the reference file's pattern; the store layer may choose to reject
    this endpoint outright rather than honor it.
    """

    model_config = ConfigDict(extra="forbid")

    transaction_id: str


class TransactionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: Optional[str] = None
    type: Optional[TransactionType] = None
    status: Optional[TransactionStatus] = None
    min_amount: Optional[float] = Field(default=None, gt=0)
    max_amount: Optional[float] = Field(default=None, gt=0)
    sort: Optional[Literal["date", "-date", "amount", "-amount"]] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None

    @model_validator(mode="after")
    def _validate_amount_range(self) -> "TransactionQuery":
        if self.min_amount is not None and self.max_amount is not None:
            if self.min_amount > self.max_amount:
                raise ValueError("min_amount must be <= max_amount")
        return self


# --------------------------------------------------------------------------
# Write models -- Beneficiary
# --------------------------------------------------------------------------

class BeneficiaryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    type: BeneficiaryType
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=50)
    allocation_value: float = Field(gt=0, le=100)


class BeneficiaryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Optional[BeneficiaryType] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    relationship: Optional[str] = Field(default=None, min_length=1, max_length=50)
    allocation_value: Optional[float] = Field(default=None, gt=0, le=100)


class BeneficiaryDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beneficiary_id: str


class BeneficiaryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: Optional[str] = None
    type: Optional[BeneficiaryType] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None


# --------------------------------------------------------------------------
# Write models -- TrustedContact
# --------------------------------------------------------------------------

class TrustedContactCreate(BaseModel):
    """VG-OP-013 SS2.4 allows up to two TCPs per party -- that count limit
    is a cross-record constraint (how many existing TCP rows does this
    party already have?), so it's enforced at the store layer, not here.
    """

    model_config = ConfigDict(extra="forbid")

    party_id: str
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=1, max_length=30)


class TrustedContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    relationship: Optional[str] = Field(default=None, min_length=1, max_length=50)
    phone: Optional[str] = Field(default=None, min_length=1, max_length=30)


class TrustedContactDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tcp_id: str


class TrustedContactQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    party_id: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None
