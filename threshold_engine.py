from datetime import date, datetime, timedelta

RULES_RATES=[ 
        { "min":0, "max":24999,"rate":.0950 },
        { "min":25000,"max":49999,"rate":.0900},
        { "min":50000, "max":99999,"rate":.0825},
        { "min":100000, "max":249999, "rate":.0750 },
        { "min":250000, "max":499999, "rate":.0650},
        { "min":500000, "max":999999, "rate":.0600 }, 
        { "min":1000000,  "max":float("inf"), "rate":.0550} 
        ]

RMD_RULES = [
    {"birth_year_min": 1950, "birth_year_max": 1950, "rmd_age": 72},
    {"birth_year_min": 1951, "birth_year_max": 1959, "rmd_age": 73},
    {"birth_year_min": 1960, "birth_year_max": float("inf"), "rmd_age": 75}
]

def get_rmd_age(birth_year):
    for rule in RMD_RULES:
        if rule["birth_year_min"] <= birth_year <= rule["birth_year_max"]:
            return rule["rmd_age"]
    raise ValueError("No matching RMD rule found")

RULES_1 =[
        {"rule_id": "RMD_START_AGE".lower(), "RMD_RULES": RMD_RULES},
        {"rule_id": "QCD_ANNUAL_LIMIT".lower(), "QCD": 108000},
        {"rule_id": "WIRE_CALLBACK".lower(), "min_hr_wf": 0, "max_hr_wf": 24}, # Wire fraud hour callback threshold
        {"rule_id": "WIRE_ACH_DAILY_LIMIT_INCOMING".lower(), "action": "Vanterra → Bank", "limit": 250_000 },
        {"rule_id": "WIRE_ACH_DAILY_LIMIT_OUTGOING".lower(), "action": "Bank → Venterra", "limit": 100_000 },
        {"rule_id": "MOBILE_CHECK_DEPOSIT_LIMIT".lower(), "daily_mobile_check_deposit_limit": 100000, "max_per_check": 100000, "monthly_mobile_check_deposit_limit": 250000}, # Daily Mobile check deposit
        # {"rule_id": "MOBILE_DEPOSIT_MONTHLY", }, # Monthly Mobile check deposit
        {"rule_id":"MARGIN_INTEREST_RATE".lower(), "MARGIN_TIER": RULES_RATES},
        {"rule_id":"service_fee_waiver", "min": 0, "max_amount": 5000, "service_fee": 25},
        {"rule_id":"TEMP_HOLD".lower(),"temp_min": 1, "temp_max": 5},
        {"name": "need a name"}
        


]

CORRESPONDENCE_TEMPLATES: dict[str, dict] = {
    "beneficiary_update_confirmation": {
        "required_fields": ["party_name", "account_id", "beneficiary_summary"],
        "body": (
            "Dear {party_name},\n\nThis confirms your beneficiary designation on "
            "account {account_id} has been updated as follows: {beneficiary_summary}\n\n"
            "If you did not request this change, contact us immediately at 800-555-0199."
        ),
        "required_disclosure": (
            "Beneficiary designations govern the disposition of retirement and "
            "TOD-eligible accounts independent of your will. See VG-OP-002 for details."
        ),
    },
    "wire_callback_confirmation_letter": {
        "required_fields": ["party_name", "account_id", "wire_amount", "recipient_name"],
        "body": (
            "Dear {party_name},\n\nThis confirms the outgoing wire of {wire_amount} from "
            "account {account_id} to {recipient_name} following verbal callback confirmation."
        ),
        "required_disclosure": (
            "Wires are generally final once sent. Contact us immediately at "
            "800-555-0166 if you did not authorize this transfer."
        ),
    },
    "rmd_distribution_notice": {
        "required_fields": ["party_name", "account_id", "rmd_amount", "tax_year"],
        "body": (
            "Dear {party_name},\n\nYour Required Minimum Distribution of {rmd_amount} for "
            "tax year {tax_year} has been processed from account {account_id}."
        ),
        "required_disclosure": (
            "This distribution is taxable as ordinary income. Vanterra does not "
            "provide tax advice; consult a tax professional. See VG-OP-003."
        ),
    },
}


def _business_days_between(start: date, end: date) -> int:
    d, n = start, 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def get_rate(balance):
    for rule in RULES_RATES:
        if (
            balance >= rule["min"] and
            (rule["max"] is None or balance < rule["max"])
        ):
            return rule["rate"]





def main():
    num = 100234

    the_rate = get_rate(num)

    print(f"this is the {the_rate}")


if __name__ == "__main__":
    main()