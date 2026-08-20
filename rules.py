@tool
def evaluate_policy_rule(
    rule_name: Annotated[
        str,
        "One of: fee_waiver, wire_callback_required, "
        "ach_outgoing_limit, ach_incoming_limit, rmd_start_age, "
        "margin_interest_rate, mobile_check_deposit_limit, "
        "qcd_limit, temporary_hold_status"
    ],
    account_id: Annotated[
        Optional[str],
        "Account the rule applies to, if applicable"
    ] = None,
    amount: Annotated[
        Optional[float],
        "Dollar amount, for amount-based rules"
    ] = None,
    transaction_id: Annotated[
        Optional[str],
        "Transaction id, for temporary_hold_status"
    ] = None,
) -> dict:
    """
    ACTION tool.

    Runs a deterministic Vanterra policy rule.
    This function does NOT use an LLM to make the decision.

    Returns:
        rule_id
        decision
        source_doc
        inputs_used

    Some rules apply to a party rather than an individual account.
    For those rules, the party is found automatically using account_id.
    """

    # ---------------------------------------------------------
    # STEP 1: Find the account
    # ---------------------------------------------------------

    try:
        if account_id:
            account = _get_account(account_id)
        else:
            account = None

    except ValueError as error:
        return {
            "error": str(error)
        }

    # ---------------------------------------------------------
    # STEP 2: Find the party that owns the account
    # ---------------------------------------------------------

    party = None

    if account:
        party_id = account["party_id"]
        party = _store.parties_by_id.get(party_id)

    # =========================================================
    # RULE 1: FEE WAIVER
    # =========================================================

    if rule_name == "fee_waiver":

        # This rule needs an account so we can find the party.
        if party is None:
            return {
                "error": "fee_waiver requires account_id"
            }

        # Find all accounts belonging to this party.
        party_accounts = []

        for account in _store.accounts:

            if account["party_id"] == party["party_id"]:
                party_accounts.append(account)

        # Add the cash balances from all household accounts.
        household_total = 0

        for account in party_accounts:
            household_total += account["cash_balance"]

        # The fee is waived if either condition is true:
        #
        # 1. Household has at least $5,000
        # OR
        # 2. The customer selected electronic delivery.
        has_required_balance = household_total >= 5_000.00
        uses_e_delivery = party["e_delivery_election"]

        waived = has_required_balance or uses_e_delivery

        if waived:
            decision = "waived"
        else:
            decision = "fee_applies"

        return {
            "rule_id": "fee_waiver",
            "decision": decision,
            "source_doc": "VG-OP-017 SS3",
            "inputs_used": {
                "household_total_balance": household_total,
                "e_delivery_election": party["e_delivery_election"]
            }
        }

    # =========================================================
    # RULE 2: WIRE CALLBACK REQUIRED
    # =========================================================

    if rule_name == "wire_callback_required":

        if amount is None:
            return {
                "error": "wire_callback_required requires amount"
            }

        # Wires of $50,000 or more require a callback.
        callback_required = amount >= 50_000.00

        if callback_required:
            decision = "callback_required"
        else:
            decision = "callback_not_required"

        return {
            "rule_id": "wire_callback_required",
            "decision": decision,
            "source_doc": "VG-OP-005 SS3.4",
            "inputs_used": {
                "amount": amount
            }
        }

    # =========================================================
    # RULE 3: ACH OUTGOING LIMIT
    # =========================================================

    if rule_name == "ach_outgoing_limit":

        if amount is None:
            return {
                "error": "ach_outgoing_limit requires amount"
            }

        # This checks only the current transaction.
        # It does NOT add together multiple ACH transactions.
        exceeds_limit = amount > 100_000.00

        if exceeds_limit:
            decision = "exceeds_limit"
        else:
            decision = "within_limit"

        return {
            "rule_id": "ach_outgoing_limit",
            "decision": decision,
            "source_doc": "VG-OP-005 SS2.3",
            "inputs_used": {
                "amount": amount
            }
        }

    # =========================================================
    # RULE 4: ACH INCOMING LIMIT
    # =========================================================

    if rule_name == "ach_incoming_limit":

        if amount is None:
            return {
                "error": "ach_incoming_limit requires amount"
            }

        # This checks only the current transaction.
        # It does NOT aggregate multiple ACH transactions.
        exceeds_limit = amount > 250_000.00

        if exceeds_limit:
            decision = "exceeds_limit"
        else:
            decision = "within_limit"

        return {
            "rule_id": "ach_incoming_limit",
            "decision": decision,
            "source_doc": "VG-OP-005 SS2.3",
            "inputs_used": {
                "amount": amount
            }
        }

    # =========================================================
    # RULE 5: RMD START AGE
    # =========================================================

    if rule_name == "rmd_start_age":

        if party is None:
            return {
                "error": "rmd_start_age requires account_id"
            }

        # Get the customer's date of birth.
        date_of_birth = party.get("date_of_birth")

        if date_of_birth is None:
            return {
                "rule_id": "rmd_start_age",
                "decision": "insufficient_data",
                "source_doc": "VG-OP-003 SS3",
                "inputs_used": {
                    "date_of_birth": None,
                    "missing_field": "date_of_birth"
                }
            }

        # Convert the date of birth from text into a date.
        birth_date = date.fromisoformat(date_of_birth)

        # Extract only the year.
        birth_year = birth_date.year

        # Check each RMD age band.
        for band in RMD_AGE_BANDS:

            # Some bands describe people born through
            # a particular year.
            if "born_through" in band:

                if birth_year <= band["born_through"]:
                    return {
                        "rule_id": "rmd_start_age",
                        "decision": "insufficient_data",
                        "source_doc": "VG-OP-003 SS3",
                        "inputs_used": {
                            "birth_year": birth_year,
                            "reason": (
                                "prior-law band has no single "
                                "seedable value"
                            )
                        }
                    }

            # Check whether the person's birth year
            # falls inside this band's range.
            if "born_min" in band:

                born_min = band["born_min"]
                born_max = band["born_max"]

                meets_minimum = birth_year >= born_min

                if born_max is None:
                    meets_maximum = True
                else:
                    meets_maximum = birth_year <= born_max

                if meets_minimum and meets_maximum:
                    return {
                        "rule_id": "rmd_start_age",
                        "decision": {
                            "rmd_start_age": band["rmd_age"]
                        },
                        "source_doc": "VG-OP-003 SS3",
                        "inputs_used": {
                            "birth_year": birth_year
                        }
                    }

        return {
            "error": f"birth_year {birth_year} matched no RMD band"
        }

    # =========================================================
    # RULE 6: MARGIN INTEREST RATE
    # =========================================================

    if rule_name == "margin_interest_rate":

        # We need an account because the debit balance
        # belongs to the account.
        if account is None:
            return {
                "error": "margin_interest_rate requires account_id"
            }

        debit_balance = account.get("debit_balance")

        if debit_balance is None:
            return {
                "error": "margin_interest_rate requires account_id"
            }

        # Check each margin interest tier.
        for tier in MARGIN_INTEREST_TIERS:

            floor = tier[0]
            ceiling = tier[1]
            rate = tier[2]

            meets_floor = debit_balance >= floor

            if ceiling is None:
                meets_ceiling = True
            else:
                meets_ceiling = debit_balance <= ceiling

            if meets_floor and meets_ceiling:
                return {
                    "rule_id": "margin_interest_rate",
                    "decision": {
                        "annual_rate_pct": rate
                    },
                    "source_doc": "VG-OP-009 SS4",
                    "inputs_used": {
                        "debit_balance": debit_balance
                    }
                }

        return {
            "error": (
                f"debit_balance {debit_balance} "
                "matched no margin tier"
            )
        }

    # =========================================================
    # RULE 7: MOBILE CHECK DEPOSIT LIMIT
    # =========================================================

    if rule_name == "mobile_check_deposit_limit":

        if account is None or amount is None:
            return {
                "error": (
                    "mobile_check_deposit_limit requires "
                    "account_id and amount"
                )
            }

        # Get today's date.
        today = date.today()

        # Calculate the date 30 days ago.
        month_ago = today - timedelta(days=30)

        # Start with an empty list.
        recent_transactions = []

        # Look through this account's transactions.
        transactions = _store.transactions_for(account_id)

        for transaction in transactions:

            # We only care about check deposits.
            is_check_deposit = (
                transaction["type"] == "check_deposit"
            )

            # Convert the transaction date from text to a date.
            transaction_date = date.fromisoformat(
                transaction["date"]
            )

            # Check whether the transaction happened
            # within the last 30 days.
            is_recent = transaction_date >= month_ago

            if is_check_deposit and is_recent:
                recent_transactions.append(transaction)

        # -----------------------------------------------------
        # Calculate the monthly total.
        # -----------------------------------------------------

        monthly_total = 0

        for transaction in recent_transactions:
            monthly_total += transaction["amount"]

        # Include the new deposit being evaluated.
        monthly_total += amount

        # -----------------------------------------------------
        # Calculate today's total.
        # -----------------------------------------------------

        daily_total = 0

        for transaction in recent_transactions:

            if transaction["date"] == today.isoformat():
                daily_total += transaction["amount"]

        # Include the new deposit being evaluated.
        daily_total += amount

        # -----------------------------------------------------
        # Find any rule violations.
        # -----------------------------------------------------

        violations = []

        # Check the maximum amount for one check.
        if amount > 100_000.00:
            violations.append(
                "exceeds_per_check_limit"
            )

        # Check the maximum amount allowed per day.
        if daily_total > 100_000.00:
            violations.append(
                "exceeds_daily_limit"
            )

        # Check the maximum amount allowed per month.
        if monthly_total > 250_000.00:
            violations.append(
                "exceeds_monthly_limit"
            )

        # If there are any violations, reject the deposit.
        if violations:
            decision = "rejected"
        else:
            decision = "accepted"

        return {
            "rule_id": "mobile_check_deposit_limit",
            "decision": decision,
            "source_doc": "VG-OP-016",
            "inputs_used": {
                "amount": amount,
                "daily_total": daily_total,
                "monthly_total": monthly_total,
                "violations": violations
            }
        }

    # =========================================================
    # RULE 8: QCD LIMIT
    # =========================================================

    if rule_name == "qcd_limit":

        if party is None or amount is None:
            return {
                "error": "qcd_limit requires account_id and amount"
            }

        date_of_birth = party.get("date_of_birth")

        if date_of_birth is None:
            return {
                "rule_id": "qcd_limit",
                "decision": "insufficient_data",
                "source_doc": "VG-OP-003 SS7",
                "inputs_used": {
                    "date_of_birth": None,
                    "missing_field": "date_of_birth"
                }
            }

        # Calculate the person's age in years.
        birth_date = date.fromisoformat(date_of_birth)
        days_alive = (date.today() - birth_date).days
        age_years = days_alive / 365.25

        # A QCD is eligible when BOTH conditions are true:
        #
        # 1. Person is at least 70.5 years old.
        # 2. Amount is no more than $108,000.
        meets_age_requirement = age_years >= 70.5
        meets_amount_requirement = amount <= 108_000.00

        eligible = (
            meets_age_requirement
            and meets_amount_requirement
        )

        if eligible:
            decision = "eligible"
        else:
            decision = "not_eligible"

        return {
            "rule_id": "qcd_limit",
            "decision": decision,
            "source_doc": "VG-OP-003 SS7",
            "inputs_used": {
                "age_years": round(age_years, 1),
                "amount": amount
            }
        }

    # =========================================================
    # RULE 9: TEMPORARY HOLD STATUS
    # =========================================================

    if rule_name == "temporary_hold_status":

        if account is None or transaction_id is None:
            return {
                "error": (
                    "temporary_hold_status requires "
                    "account_id and transaction_id"
                )
            }

        # Find the transaction we are checking.
        transaction = None

        transactions = _store.transactions_for(account_id)

        for current_transaction in transactions:

            if (
                current_transaction["transaction_id"]
                == transaction_id
            ):
                transaction = current_transaction
                break

        # If we cannot find the transaction,
        # or there is no hold date, the transaction
        # is not currently considered to be on hold.
        if transaction is None:
            return {
                "rule_id": "temporary_hold_status",
                "decision": "not_on_hold",
                "source_doc": "VG-OP-013 SS2.5",
                "inputs_used": {}
            }

        hold_start_date = transaction.get("hold_start_date")

        if not hold_start_date:
            return {
                "rule_id": "temporary_hold_status",
                "decision": "not_on_hold",
                "source_doc": "VG-OP-013 SS2.5",
                "inputs_used": {}
            }

        # Calculate how many business days have passed
        # since the hold started.
        start_date = date.fromisoformat(hold_start_date)

        business_days_elapsed = _business_days_between(
            start_date,
            date.today()
        )

        # Determine the current status.
        if business_days_elapsed <= 15:
            decision = "within_initial_hold"

        elif business_days_elapsed <= 40:
            decision = "within_extension_window"

        else:
            decision = "must_release_or_escalate"

        return {
            "rule_id": "temporary_hold_status",
            "decision": decision,
            "source_doc": "VG-OP-013 SS2.5",
            "inputs_used": {
                "hold_start_date": hold_start_date,
                "business_days_elapsed": business_days_elapsed
            }
        }

    # =========================================================
    # UNKNOWN RULE
    # =========================================================

    return {
        "error": f"Unknown rule_name: {rule_name}"
    }
