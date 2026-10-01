"""In-memory core banking data. Deliberately tiny; stands in for a legacy admin console."""

from datetime import datetime

MEMBERS = {
    "10001": {
        "name": "Alice Johnson",
        "member_since": "2014-03-02",
        "checking_balance": 812.44,
        "savings_balance": 1204.09,
        "locked": False,
        "phone": "555-201-4488",
        "address": "142 Willow St, Springfield, IL 62701",
    },
    "10002": {
        "name": "Bob Lee",
        "member_since": "2019-11-19",
        "checking_balance": 133.02,
        "savings_balance": 40.00,
        "locked": True,  # triggers a permission-denied business outcome on new sub-accounts / transfers
        "phone": "555-390-1122",
        "address": "87 Birch Ave, Springfield, IL 62702",
    },
    "20001": {
        "name": "Carol Nguyen",
        "member_since": "2021-06-08",
        "checking_balance": 2210.77,
        "savings_balance": 9800.15,
        "locked": False,
        "session_interstitial": True,  # triggers a "session renewing" dialog before write flows
        "phone": "555-762-0093",
        "address": "9 Cedar Ct, Springfield, IL 62703",
    },
    "12345": {
        "name": "Het Nagda",
        "member_since": "2023-01-15",
        "checking_balance": 8732.00,
        "savings_balance": 1562.00,
        "locked": False,
        "phone": "555-410-7729",
        "address": "56 Maple Dr, Springfield, IL 62704",
    },
}

# member_id -> list of {id, type, balance, created_at, source, run_id}
SUBACCOUNTS: dict[str, list[dict]] = {mid: [] for mid in MEMBERS}

# member_id -> list of {id, principal, purpose, interest_rate, status, created_at, source, run_id}
LOANS: dict[str, list[dict]] = {mid: [] for mid in MEMBERS}

# member_id -> transaction rows, newest first. Seed rows have source="seed". Write routes read
# X-Automation-Source and X-Run-Id, so the data shows whether discovery or replay made the change.
TRANSACTIONS: dict[str, list[dict]] = {
    "10001": [
        {"timestamp": "2026-09-20 09:14:02.000", "description": "Grocery Mart", "account": "checking", "amount": -64.21, "balance_after": 812.44, "source": "seed", "run_id": ""},
        {"timestamp": "2026-09-18 08:00:11.000", "description": "Payroll Deposit", "account": "checking", "amount": 1500.00, "balance_after": 876.65, "source": "seed", "run_id": ""},
        {"timestamp": "2026-09-15 14:32:47.000", "description": "Electric Co-op", "account": "checking", "amount": -112.30, "balance_after": -623.35, "source": "seed", "run_id": ""},
        {"timestamp": "2026-09-10 00:05:00.000", "description": "Interest Payment", "account": "savings", "amount": 3.12, "balance_after": 1204.09, "source": "seed", "run_id": ""},
        {"timestamp": "2026-08-28 16:47:19.000", "description": "Transfer to Savings", "account": "savings", "amount": 200.00, "balance_after": 1200.97, "source": "seed", "run_id": ""},
    ],
    "10002": [
        {"timestamp": "2026-09-19 11:02:55.000", "description": "ATM Withdrawal", "account": "checking", "amount": -40.00, "balance_after": 133.02, "source": "seed", "run_id": ""},
        {"timestamp": "2026-09-05 08:00:03.000", "description": "Payroll Deposit", "account": "checking", "amount": 173.02, "balance_after": 173.02, "source": "seed", "run_id": ""},
    ],
    "20001": [
        {"timestamp": "2026-09-21 08:00:07.000", "description": "Payroll Deposit", "account": "checking", "amount": 2200.00, "balance_after": 2210.77, "source": "seed", "run_id": ""},
        {"timestamp": "2026-09-12 00:05:00.000", "description": "Interest Payment", "account": "savings", "amount": 24.50, "balance_after": 9800.15, "source": "seed", "run_id": ""},
        {"timestamp": "2026-08-30 10:18:32.000", "description": "Wire In", "account": "savings", "amount": 5000.00, "balance_after": 9775.65, "source": "seed", "run_id": ""},
    ],
    "12345": [
        {"timestamp": "2026-09-22 09:00:00.000", "description": "Payroll Deposit", "account": "checking", "amount": 4200.00, "balance_after": 8732.00, "source": "seed", "run_id": ""},
        {"timestamp": "2026-09-11 00:05:00.000", "description": "Interest Payment", "account": "savings", "amount": 6.02, "balance_after": 1562.00, "source": "seed", "run_id": ""},
    ],
}

_next_subaccount_seq = 5000
_next_transfer_seq = 7000
_next_loan_seq = 3000


def next_subaccount_id() -> str:
    global _next_subaccount_seq
    _next_subaccount_seq += 1
    return f"SA-{_next_subaccount_seq}"


def next_transfer_id() -> str:
    global _next_transfer_seq
    _next_transfer_seq += 1
    return f"TXF-{_next_transfer_seq}"


def next_loan_id() -> str:
    global _next_loan_seq
    _next_loan_seq += 1
    return f"LN-{_next_loan_seq}"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def record_transaction(
    member_id: str, description: str, account: str, amount: float, source: str = "manual", run_id: str = ""
) -> None:
    member = MEMBERS[member_id]
    balance_after = member["checking_balance"] if account == "checking" else member["savings_balance"]
    TRANSACTIONS.setdefault(member_id, []).insert(
        0,
        {
            "timestamp": _now(),
            "description": description,
            "account": account,
            "amount": amount,
            "balance_after": balance_after,
            "source": source,
            "run_id": run_id,
        },
    )


def record_subaccount(
    member_id: str, account_type: str, balance: float, source: str = "manual", run_id: str = ""
) -> dict:
    entry = {
        "id": next_subaccount_id(),
        "type": account_type,
        "balance": balance,
        "created_at": _now(),
        "source": source,
        "run_id": run_id,
    }
    SUBACCOUNTS.setdefault(member_id, []).append(entry)
    return entry


def record_loan(
    member_id: str, principal: float, purpose: str, interest_rate: float, source: str = "manual", run_id: str = ""
) -> dict:
    entry = {
        "id": next_loan_id(),
        "principal": principal,
        "purpose": purpose,
        "interest_rate": interest_rate,
        "status": "Active",
        "created_at": _now(),
        "source": source,
        "run_id": run_id,
    }
    LOANS.setdefault(member_id, []).append(entry)
    return entry
