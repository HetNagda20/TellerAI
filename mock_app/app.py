"""A deliberately 'legacy' credit-union servicing console.

Table-based layout, no ids/data-testid attributes, an iframe for the balance
panel, a persistent top tab bar (Accounts / Transfers / Loans / Cards), and
real business-error branches (not found, locked account, validation, a
session interstitial). This stands in for the kind of no-API, no-clean-DOM
back-office app the automation system targets.

Loans and Cards are placeholder servicing areas (tab + member lookup + a
"not available in this demo" page) — present for navigational completeness,
deliberately not built out with real data/logic. Everything under Accounts
and Transfers is real.
"""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mock_app.data import (
    MEMBERS,
    SUBACCOUNTS,
    TRANSACTIONS,
    next_transfer_id,
    record_subaccount,
    record_transaction,
)

APP_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="CU Servicing Console (mock)")


@app.middleware("http")
async def log_automation_source(request: Request, call_next):
    """Live, human-watchable evidence of who's driving the browser on every request —
    not just the per-run JSON in /evidence/. X-Automation-Source/X-Run-Id are set by
    agent/loop.py (discovery) and replay/executor.py (replay); absent entirely for an
    organic manual request (e.g. someone just clicking around in a browser).
    """
    source = request.headers.get("x-automation-source", "manual")
    run_id = request.headers.get("x-run-id", "")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    response = await call_next(request)
    tag = f"{source}:{run_id}" if run_id else source
    print(f"[{ts}] [{tag}] {request.method} {request.url.path} -> {response.status_code}")
    return response


def _automation_source(request: Request) -> tuple[str, str]:
    return request.headers.get("x-automation-source", "manual"), request.headers.get("x-run-id", "")


def _member_lookup_response(request: Request, member_id: str, active_tab: str, redirect_to: str):
    member_id = member_id.strip()
    if member_id not in MEMBERS:
        return templates.TemplateResponse(
            request, "not_found.html", {"member_id": member_id, "active_tab": active_tab}, status_code=404
        )
    return RedirectResponse(url=redirect_to.format(member_id=member_id), status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {"active_tab": None})


@app.get("/accounts", response_class=HTMLResponse)
def accounts_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "tab_lookup.html",
        {"active_tab": "accounts", "tab_title": "Accounts", "search_action": "/accounts/search", "error": None},
    )


@app.post("/accounts/search", response_class=HTMLResponse)
def accounts_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "accounts", "/member/{member_id}")


@app.get("/transfers", response_class=HTMLResponse)
def transfers_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "tab_lookup.html",
        {"active_tab": "transfers", "tab_title": "Transfers", "search_action": "/transfers/search", "error": None},
    )


@app.post("/transfers/search", response_class=HTMLResponse)
def transfers_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "transfers", "/member/{member_id}/transfer")


@app.get("/loans", response_class=HTMLResponse)
def loans_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "tab_lookup.html",
        {"active_tab": "loans", "tab_title": "Loans", "search_action": "/loans/search", "error": None},
    )


@app.post("/loans/search", response_class=HTMLResponse)
def loans_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "loans", "/member/{member_id}/loans")


@app.get("/member/{member_id}/loans", response_class=HTMLResponse)
def loans_placeholder(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request,
        "placeholder.html",
        {"member_id": member_id, "member": member, "feature_name": "Loans", "active_tab": "loans"},
    )


@app.get("/cards", response_class=HTMLResponse)
def cards_lookup(request: Request):
    return templates.TemplateResponse(
        request,
        "tab_lookup.html",
        {"active_tab": "cards", "tab_title": "Cards", "search_action": "/cards/search", "error": None},
    )


@app.post("/cards/search", response_class=HTMLResponse)
def cards_search(request: Request, member_id: str = Form(...)):
    return _member_lookup_response(request, member_id, "cards", "/member/{member_id}/cards")


@app.get("/member/{member_id}/cards", response_class=HTMLResponse)
def cards_placeholder(request: Request, member_id: str):
    member = MEMBERS[member_id]
    return templates.TemplateResponse(
        request,
        "placeholder.html",
        {"member_id": member_id, "member": member, "feature_name": "Cards", "active_tab": "cards"},
    )


@app.get("/member/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        return templates.TemplateResponse(
            request, "not_found.html", {"member_id": member_id, "active_tab": "accounts"}, status_code=404
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


@app.get("/member/{member_id}/new-subaccount", response_class=HTMLResponse)
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


@app.post("/member/{member_id}/new-subaccount", response_class=HTMLResponse)
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


@app.post("/member/{member_id}/new-subaccount/confirm", response_class=HTMLResponse)
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
            "active_tab": "accounts",
        },
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


@app.get("/member/{member_id}/transfer", response_class=HTMLResponse)
def transfer_form(request: Request, member_id: str):
    member = MEMBERS[member_id]
    if member.get("locked"):
        return templates.TemplateResponse(
            request, "locked.html", {"member_id": member_id, "active_tab": "transfers"}, status_code=403
        )
    return templates.TemplateResponse(
        request, "transfer_form.html", {"member_id": member_id, "member": member, "error": None, "active_tab": "transfers"}
    )


@app.post("/member/{member_id}/transfer", response_class=HTMLResponse)
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
                "active_tab": "transfers",
            },
            status_code=400,
        )

    to_member = MEMBERS.get(to_member_id)
    if to_member is None:
        return templates.TemplateResponse(
            request,
            "transfer_not_found.html",
            {"member_id": member_id, "to_member_id": to_member_id, "active_tab": "transfers"},
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
                "active_tab": "transfers",
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
            "active_tab": "transfers",
        },
    )


@app.post("/member/{member_id}/transfer/confirm", response_class=HTMLResponse)
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
            "active_tab": "transfers",
        },
    )
