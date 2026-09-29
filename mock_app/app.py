"""A deliberately legacy credit-union servicing console.

Table-based layout, no ids/data-testid attributes, an iframe for the balance
panel, a persistent top tab bar (Accounts / Transactions / Loans / Cards),
and real business-error branches (not found, locked account, validation, a
session interstitial). This stands in for the kind of no-API, no-clean-DOM
back-office app the automation system targets.

Navigation shape: each real section (Accounts, Transactions, Loans) has a
landing page with two choices, and member lookup happens after the choice,
not before it, so there is no single shared search page every operation
starts from. Cards is a static placeholder with no member lookup.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from logging_config import configure_logging
from mock_app.data import (
    LOANS,
    MEMBERS,
    SUBACCOUNTS,
    TRANSACTIONS,
    next_transfer_id,
    record_loan,
    record_subaccount,
    record_transaction,
)

configure_logging("mock_app")
logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="CU Servicing Console (mock)")


# -- Test-only fault injection --------------------------------------------
#
# Not part of the business API surface. Exists so an external test/experiment can
# arm exactly one failure scenario for the next matching write, then have it
# auto-consume, without touching any real business data and without any seam
# into replay/executor.py or the artifact/replay engine at all. A real caller
# never hits /__test__/*, and no normal request can trigger this by accident.
_armed_test_failure: str | None = None


def _consume_armed_test_failure() -> str | None:
    global _armed_test_failure
    scenario, _armed_test_failure = _armed_test_failure, None
    return scenario


@app.post("/__test__/arm_failure")
def arm_test_failure(scenario: str = Form(...)):
    global _armed_test_failure
    _armed_test_failure = scenario
    logger.warning("test fault armed scenario=%s", scenario)
    return {"armed": scenario}


@app.middleware("http")
async def log_automation_source(request: Request, call_next):
    """Live, human-watchable evidence of who is driving the browser on every
    request, not just the per-run JSON in /evidence/. X-Automation-Source and
    X-Run-Id are set by agent/loop.py (discovery) and replay/executor.py
    (replay); absent entirely for an organic manual request."""
    source = request.headers.get("x-automation-source", "manual")
    run_id = request.headers.get("x-run-id", "")
    response = await call_next(request)
    tag = f"{source}:{run_id}" if run_id else source
    level = logging.INFO if response.status_code < 400 else logging.WARNING
    logger.log(level, "[%s] %s %s -> %s", tag, request.method, request.url.path, response.status_code)
    return response


def _automation_source(request: Request) -> tuple[str, str]:
    return request.headers.get("x-automation-source", "manual"), request.headers.get("x-run-id", "")


def _member_lookup_response(request: Request, member_id: str, active_tab: str, redirect_to: str, back_url: str):
    member_id = member_id.strip()
    if member_id not in MEMBERS:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"member_id": member_id, "active_tab": active_tab, "back_url": back_url},
            status_code=404,
        )
    return RedirectResponse(url=redirect_to.format(member_id=member_id), status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {"active_tab": None})


# -- Accounts ---------------------------------------------------------------

@app.get("/accounts", response_class=HTMLResponse)
def accounts_landing(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "active_tab": "accounts",
            "section_title": "Accounts",
            "choices": [
                {"label": "Member Inquiry", "href": "/accounts/view"},
                {"label": "New Account", "href": "/accounts/create"},
            ],
        },
    )


@app.get("/accounts/view", response_class=HTMLResponse)
def accounts_view_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "lookup.html",
        {"active_tab": "accounts", "heading": "Member Inquiry", "search_action": "/accounts/view/search", "error": None},
    )


@app.post("/accounts/view/search", response_class=HTMLResponse)
def accounts_view_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "accounts", "/member/{member_id}", "/accounts/view")


@app.get("/accounts/create", response_class=HTMLResponse)
def accounts_create_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "lookup.html",
        {"active_tab": "accounts", "heading": "New Account", "search_action": "/accounts/create/search", "error": None},
    )


@app.post("/accounts/create/search", response_class=HTMLResponse)
def accounts_create_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "accounts", "/member/{member_id}/accounts/new", "/accounts/create")


@app.get("/member/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        return templates.TemplateResponse(
            request, "not_found.html", {"member_id": member_id, "active_tab": "accounts", "back_url": "/accounts/view"}, status_code=404
        )
    return templates.TemplateResponse(
        request,
        "member_detail.html",
        {
            "member_id": member_id,
            "member": member,
            "subaccounts": SUBACCOUNTS.get(member_id, []),
            "active_tab": "accounts",
        },
    )


@app.get("/member/{member_id}/balance-frame", response_class=HTMLResponse)
def balance_frame(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request, "balance_frame.html", {"member": member}
    )


@app.get("/member/{member_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request, "edit_form.html", {"member_id": member_id, "member": member, "error": None, "active_tab": "accounts"}
    )


@app.post("/member/{member_id}/edit", response_class=HTMLResponse)
def edit_submit(request: Request, member_id: str, phone: str = Form(...), address: str = Form(...)):
    member = MEMBERS[member_id]
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) != 10:
        return templates.TemplateResponse(
            request,
            "edit_form.html",
            {
                "member_id": member_id,
                "member": member,
                "error": "Phone number must contain exactly 10 digits.",
                "active_tab": "accounts",
            },
            status_code=400,
        )
    member["phone"] = phone
    member["address"] = address
    return templates.TemplateResponse(
        request, "edit_success.html", {"member_id": member_id, "member": member, "active_tab": "accounts"}
    )


@app.get("/member/{member_id}/accounts/new", response_class=HTMLResponse)
def new_subaccount_form(request: Request, member_id: str, ack: str | None = None):
    member = MEMBERS[member_id]
    if member.get("locked"):
        return templates.TemplateResponse(
            request, "locked.html", {"member_id": member_id, "active_tab": "accounts"}, status_code=403
        )
    if member.get("session_interstitial") and ack != "1":
        return templates.TemplateResponse(
            request, "session_interstitial.html", {"member_id": member_id, "active_tab": "accounts"}
        )
    return templates.TemplateResponse(
        request,
        "new_subaccount_form.html",
        {"member_id": member_id, "member": member, "error": None, "active_tab": "accounts"},
    )


@app.post("/member/{member_id}/accounts/new", response_class=HTMLResponse)
def new_subaccount_review(
    request: Request,
    member_id: str,
    account_type: str = Form(...),
    initial_deposit: str = Form(...),
    funding_source: str = Form(...),
):
    member = MEMBERS[member_id]
    try:
        amount = float(initial_deposit)
    except ValueError:
        amount = None

    if amount is None or amount < 25:
        return templates.TemplateResponse(
            request,
            "new_subaccount_form.html",
            {
                "member_id": member_id,
                "member": member,
                "error": "Initial deposit must be a number of at least $25.00.",
                "active_tab": "accounts",
            },
            status_code=400,
        )

    if funding_source in ("checking", "savings"):
        source_balance = member["checking_balance"] if funding_source == "checking" else member["savings_balance"]
        if amount > source_balance:
            return templates.TemplateResponse(
                request,
                "new_subaccount_form.html",
                {
                    "member_id": member_id,
                    "member": member,
                    "error": "Insufficient funds in the selected funding account.",
                    "active_tab": "accounts",
                },
                status_code=400,
            )

    return templates.TemplateResponse(
        request,
        "confirmation.html",
        {
            "member_id": member_id,
            "member": member,
            "account_type": account_type,
            "initial_deposit": amount,
            "funding_source": funding_source,
            "active_tab": "accounts",
        },
    )


@app.post("/member/{member_id}/accounts/new/confirm", response_class=HTMLResponse)
def new_subaccount_confirm(
    request: Request,
    member_id: str,
    account_type: str = Form(...),
    initial_deposit: float = Form(...),
    funding_source: str = Form(...),
):
    member = MEMBERS[member_id]
    source, run_id = _automation_source(request)
    entry = record_subaccount(member_id, account_type, initial_deposit, source=source, run_id=run_id)
    confirmation_number = entry["id"]
    if funding_source in ("checking", "savings"):
        balance_key = f"{funding_source}_balance"
        member[balance_key] -= initial_deposit
        record_transaction(
            member_id,
            f"Transfer to new sub-account {confirmation_number}",
            funding_source,
            -initial_deposit,
            source=source,
            run_id=run_id,
        )
    return templates.TemplateResponse(
        request,
        "success.html",
        {
            "member_id": member_id,
            "member": member,
            "account_type": account_type,
            "initial_deposit": initial_deposit,
            "funding_source": funding_source,
            "confirmation_number": confirmation_number,
            "active_tab": "accounts",
        },
    )


# -- Transactions -------------------------------------------------------------

@app.get("/transactions", response_class=HTMLResponse)
def transactions_landing(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "active_tab": "transactions",
            "section_title": "Transactions",
            "choices": [
                {"label": "Transaction Inquiry", "href": "/transactions/view"},
                {"label": "Transaction Entry", "href": "/transactions/new"},
            ],
        },
    )


@app.get("/transactions/view", response_class=HTMLResponse)
def transactions_view_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "lookup.html",
        {"active_tab": "transactions", "heading": "Transaction Inquiry", "search_action": "/transactions/view/search", "error": None},
    )


@app.post("/transactions/view/search", response_class=HTMLResponse)
def transactions_view_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "transactions", "/member/{member_id}/transactions", "/transactions/view")


@app.get("/transactions/new", response_class=HTMLResponse)
def transactions_new_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "lookup.html",
        {"active_tab": "transactions", "heading": "Transaction Entry", "search_action": "/transactions/new/search", "error": None},
    )


@app.post("/transactions/new/search", response_class=HTMLResponse)
def transactions_new_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "transactions", "/member/{member_id}/transactions/new", "/transactions/new")


@app.get("/member/{member_id}/transactions", response_class=HTMLResponse)
def transactions(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "member_id": member_id,
            "member": member,
            "transactions": TRANSACTIONS.get(member_id, []),
            "active_tab": "transactions",
        },
    )


@app.get("/member/{member_id}/transactions/new", response_class=HTMLResponse)
def transaction_type(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request, "transaction_type.html", {"member_id": member_id, "member": member, "active_tab": "transactions"}
    )


@app.get("/member/{member_id}/transactions/new/transfer", response_class=HTMLResponse)
def transfer_form(request: Request, member_id: str):
    member = MEMBERS[member_id]
    if member.get("locked"):
        return templates.TemplateResponse(
            request, "locked.html", {"member_id": member_id, "active_tab": "transactions"}, status_code=403
        )
    return templates.TemplateResponse(
        request, "transfer_form.html", {"member_id": member_id, "member": member, "error": None, "active_tab": "transactions"}
    )


@app.post("/member/{member_id}/transactions/new/transfer", response_class=HTMLResponse)
def transfer_review(
    request: Request,
    member_id: str,
    from_account: str = Form(...),
    to_member_id: str = Form(...),
    to_account: str = Form(...),
    amount: str = Form(...),
):
    member = MEMBERS[member_id]
    to_member_id = to_member_id.strip()

    try:
        amount_val = float(amount)
    except ValueError:
        amount_val = None

    if amount_val is None or amount_val <= 0:
        return templates.TemplateResponse(
            request,
            "transfer_form.html",
            {
                "member_id": member_id,
                "member": member,
                "error": "Transfer amount must be a positive number.",
                "active_tab": "transactions",
            },
            status_code=400,
        )

    to_member = MEMBERS.get(to_member_id)
    if to_member is None:
        return templates.TemplateResponse(
            request,
            "transfer_not_found.html",
            {"member_id": member_id, "to_member_id": to_member_id, "active_tab": "transactions"},
            status_code=404,
        )

    from_balance = member["checking_balance"] if from_account == "checking" else member["savings_balance"]
    if amount_val > from_balance:
        return templates.TemplateResponse(
            request,
            "transfer_form.html",
            {
                "member_id": member_id,
                "member": member,
                "error": "Insufficient funds in the selected account.",
                "active_tab": "transactions",
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "transfer_confirm.html",
        {
            "member_id": member_id,
            "member": member,
            "from_account": from_account,
            "to_member_id": to_member_id,
            "to_member": to_member,
            "to_account": to_account,
            "amount": amount_val,
            "active_tab": "transactions",
        },
    )


@app.post("/member/{member_id}/transactions/new/transfer/confirm", response_class=HTMLResponse)
def transfer_confirm(
    request: Request,
    member_id: str,
    from_account: str = Form(...),
    to_member_id: str = Form(...),
    to_account: str = Form(...),
    amount: float = Form(...),
):
    member = MEMBERS[member_id]
    to_member = MEMBERS[to_member_id]
    confirmation_number = next_transfer_id()
    source, run_id = _automation_source(request)

    from_key = f"{from_account}_balance"
    to_key = f"{to_account}_balance"
    member[from_key] -= amount
    to_member[to_key] += amount
    record_transaction(member_id, f"Transfer to {to_member_id} ({confirmation_number})", from_account, -amount, source=source, run_id=run_id)
    record_transaction(to_member_id, f"Transfer from {member_id} ({confirmation_number})", to_account, amount, source=source, run_id=run_id)

    # Test-only fault injection (see _armed_test_failure above): deliberately AFTER the
    # mutation/ledger writes above, never instead of them, the write must be completely
    # normal and indistinguishable from any other transfer for this experiment to mean
    # anything. Triggered only by an explicit prior call to /__test__/arm_failure; never by
    # member id, account, or amount, and it never runs for an ordinary request.
    if _consume_armed_test_failure() == "post_commit_response_failure":
        return templates.TemplateResponse(
            request, "post_commit_response_failure.html", {"active_tab": "transactions"}, status_code=503
        )

    return templates.TemplateResponse(
        request,
        "transfer_success.html",
        {
            "member_id": member_id,
            "member": member,
            "from_account": from_account,
            "to_member_id": to_member_id,
            "to_member": to_member,
            "to_account": to_account,
            "amount": amount,
            "confirmation_number": confirmation_number,
            "active_tab": "transactions",
        },
    )


# -- Loans --------------------------------------------------------------------

@app.get("/loans", response_class=HTMLResponse)
def loans_landing(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "active_tab": "loans",
            "section_title": "Loans",
            "choices": [
                {"label": "Loan Inquiry", "href": "/loans/view"},
                {"label": "Loan Origination", "href": "/loans/create"},
            ],
        },
    )


@app.get("/loans/view", response_class=HTMLResponse)
def loans_view_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "lookup.html",
        {"active_tab": "loans", "heading": "Loan Inquiry", "search_action": "/loans/view/search", "error": None},
    )


@app.post("/loans/view/search", response_class=HTMLResponse)
def loans_view_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "loans", "/member/{member_id}/loans", "/loans/view")


@app.get("/loans/create", response_class=HTMLResponse)
def loans_create_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "lookup.html",
        {"active_tab": "loans", "heading": "Loan Origination", "search_action": "/loans/create/search", "error": None},
    )


@app.post("/loans/create/search", response_class=HTMLResponse)
def loans_create_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "loans", "/member/{member_id}/loans/new", "/loans/create")


@app.get("/member/{member_id}/loans", response_class=HTMLResponse)
def member_loans(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request,
        "member_loans.html",
        {"member_id": member_id, "member": member, "loans": LOANS.get(member_id, []), "active_tab": "loans"},
    )


@app.get("/member/{member_id}/loans/new", response_class=HTMLResponse)
def loan_form(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request, "loan_form.html", {"member_id": member_id, "member": member, "error": None, "active_tab": "loans"}
    )


@app.post("/member/{member_id}/loans/new", response_class=HTMLResponse)
def loan_review(
    request: Request,
    member_id: str,
    loan_amount: str = Form(...),
    loan_purpose: str = Form(...),
    interest_rate: str = Form(...),
):
    member = MEMBERS[member_id]

    try:
        amount = float(loan_amount)
    except ValueError:
        amount = None
    try:
        rate = float(interest_rate)
    except ValueError:
        rate = None

    if amount is None or amount <= 0:
        return templates.TemplateResponse(
            request,
            "loan_form.html",
            {"member_id": member_id, "member": member, "error": "Loan amount must be a positive number.", "active_tab": "loans"},
            status_code=400,
        )
    if rate is None or rate < 0:
        return templates.TemplateResponse(
            request,
            "loan_form.html",
            {"member_id": member_id, "member": member, "error": "Interest rate must be a non-negative number.", "active_tab": "loans"},
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "loan_review.html",
        {
            "member_id": member_id,
            "member": member,
            "loan_amount": amount,
            "loan_purpose": loan_purpose,
            "interest_rate": rate,
            "active_tab": "loans",
        },
    )


@app.post("/member/{member_id}/loans/new/confirm", response_class=HTMLResponse)
def loan_confirm(
    request: Request,
    member_id: str,
    loan_amount: float = Form(...),
    loan_purpose: str = Form(...),
    interest_rate: float = Form(...),
):
    member = MEMBERS[member_id]
    source, run_id = _automation_source(request)
    loan = record_loan(member_id, loan_amount, loan_purpose, interest_rate, source=source, run_id=run_id)
    return templates.TemplateResponse(
        request, "loan_success.html", {"member_id": member_id, "member": member, "loan": loan, "active_tab": "loans"}
    )


# -- Cards ----------------------------------------------------------------

@app.get("/cards", response_class=HTMLResponse)
def cards(request: Request):
    return templates.TemplateResponse(request, "cards.html", {"active_tab": "cards"})
