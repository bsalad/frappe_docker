"""Wise sync: each currency as a Bank Account, its activities and payouts as Bank Transactions, through bank_feed.

Run by the hourly scheduler (sync_scheduled) and by the Test connection, Dry run and Sync now buttons of Wise Settings.
A dry run writes nothing: it reports what each account would get. The token is read with get_password; it is never
logged or returned to the page.

The rows come from the activities (card spend, deposits), the transfers (payouts) and each payout's quote (its fee), in
wise_client.feed_rows. The balances give the end-balance check. Statements are not read.
"""

import collections
import datetime

import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe import _

from bi_finance import bank_feed, wise_client

BANK = "WISE PAYMENTS LIMITED"

# an account opened for a new currency takes the first free number of this range, next to Wise Kontokorrent (1021)
ACCOUNT_NUMBERS = range(1022, 1029)

# the activities and payouts are read from a week before the last sync, so a movement Wise posts late is not missed
OVERLAP = datetime.timedelta(days=7)


def _settings():
    settings = frappe.get_single("Wise Settings")
    token = settings.get_password("api_token", raise_exception=False)
    if not token or not settings.profile_id:
        frappe.throw(_("Enter the API token and the profile id in Wise Settings first."))
    return settings, token


@frappe.whitelist()
def test_connection():
    """The profiles the token can see, as a message. Reads only."""
    settings = frappe.get_single("Wise Settings")
    token = settings.get_password("api_token", raise_exception=False)
    if not token:
        frappe.throw(_("Enter the API token in Wise Settings first."))
    try:
        found = wise_client.profiles(token)
    except wise_client.WiseError as err:
        frappe.throw(str(err))
    lines = ["{} {}".format(p.get("type"), p.get("id")) for p in found]
    frappe.msgprint(_("Connected. Profiles: {0}").format(", ".join(lines) or _("none")))
    return lines


@frappe.whitelist()
def sync_now(dry_run=True):
    """Sync every balance now. dry_run, the default, reports what would be written; a live run writes it."""
    dry_run = frappe.parse_json(dry_run) if isinstance(dry_run, str) else bool(dry_run)
    report = run(dry_run=dry_run)
    frappe.msgprint(_("{0} account(s) {1}.").format(len(report["accounts"]), _("planned") if dry_run else _("synced")))
    frappe.msgprint("<br>".join(_summary_lines(report)))
    return report


def sync_scheduled():
    """The hourly job: a live run, only when Wise Settings has sync on and a token and profile."""
    settings = frappe.get_single("Wise Settings")
    if not settings.sync_enabled:
        return
    run(dry_run=False)


def run(dry_run):
    """One pass over the profile's activities, payouts and balances. Returns the report of each currency and the totals.

    Every amount is shown in its own currency and in CHF: the CHF the source gave where it did, else at the rate of the
    day it falls on. Nothing is written when dry_run.
    """
    settings, token = _settings()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    start = _start(settings, now)
    profile = settings.profile_id
    balances = _balances(wise_client.balances(token, profile))
    activity_items = wise_client.activities(token, profile, start)
    transfer_items = wise_client.transfers(token, profile, start, now)
    fees, no_fee_option = _fees(token, profile, transfer_items)
    recipients = wise_client.payout_recipients(token, transfer_items)
    rows, skipped = wise_client.feed_rows(activity_items, transfer_items, fees)
    rates = {}
    accounts = []
    for currency in sorted(set(balances) | {row["currency"] for row in rows}):
        # the account name is planned before it exists on a dry run
        bank_account = _bank_account_name(currency)
        currency_rows = [row for row in rows if row["currency"] == currency]
        if currency != "CHF":
            # the rate of every day this account's amounts fall on, so the conversion below needs no lookup per day
            rates.update(bank_feed.load_rates(currency, _first_day(start, currency_rows, bank_account), now.date(), dry_run))
        create, summary = bank_feed.plan(bank_account, currency_rows)
        if not dry_run and create:
            # an account is opened only for a movement it takes: a currency Wise holds at zero gets none
            ensure_bank_account(currency)
            bank_feed.write(bank_account, create)
            frappe.db.commit()
        accounts.append(_account_report(currency, bank_account, currency_rows, create, summary, balances.get(currency), now, rates))
    if not dry_run:
        # one field, not settings.save(): a save would write back the token and switches as read at the start of the run
        frappe.db.set_single_value("Wise Settings", "last_sync", now)
        frappe.db.commit()
    return {
        "accounts": accounts,
        "total": _total(accounts),
        "parsed": len(rows),
        "skipped": dict(skipped),
        "payouts_without_balance_fee": no_fee_option,
        # the fees read from the quotes: how many, and how many are zero (a zero fee books no line)
        "payout_fees": {"read": len(fees), "zero": sum(1 for fee in fees.values() if not fee)},
        "payout_recipients": recipients,
        "fetched": {
            "activities": len(activity_items),
            "activity_types": dict(collections.Counter(item.get("type") for item in activity_items)),
            "transfers": len(transfer_items),
            "transfer_statuses": dict(collections.Counter(item.get("status") for item in transfer_items)),
            "transfer_routes": dict(
                collections.Counter("{}>{}".format(item.get("sourceCurrency"), item.get("targetCurrency")) for item in transfer_items)
            ),
        },
    }


def _balances(items):
    """The balance per currency: the standard and savings amounts of a currency added together."""
    out = collections.defaultdict(float)
    for item in items:
        out[item["currency"]] += _amount(item)
    return {currency: round(amount, 2) for currency, amount in out.items()}


def _fees(token, profile, transfer_items):
    """The CHF fee of each completed payout that has a quote: {transfer id: fee}, and the count of quotes with no balance fee."""
    fees = {}
    no_fee_option = 0
    for item in transfer_items:
        if item.get("status") != wise_client.COMPLETED_TRANSFER or not item.get("quoteUuid"):
            continue
        fee = wise_client.quote_fee(wise_client.quote(token, profile, item["quoteUuid"]))
        if fee is None:
            no_fee_option += 1
        else:
            fees[str(item["id"])] = fee
    return fees, no_fee_option


def _account_report(currency, bank_account, rows, create, summary, balance, now, rates):
    """One currency's row: the counts, the Wise balance and ERPNext's ledger, each in the currency and in CHF, and the check.

    The ledger is the GL account of the Bank Account (its GL Entry net), where bexio booked every movement. The Bank
    Transaction net is kept beside it as a second column, and the Bank Transactions no row matches are counted per month.

    The check: the Wise balance now against the ledger's net. The start balance, the Wise balance less the net of this
    run's rows, must equal the ledger's net before the first row's day: the account began where the ledger says.
    """
    create_chf, create_gap = _create_chf(create, currency, rates)
    erp_by_day = _gl_net_by_day(bank_account)
    erp_net = round(sum(erp_by_day.values()), 2)
    erp_chf, erp_gap = _chf_sum(erp_by_day, currency, rates)
    bank_transaction_net = round(sum(_bank_transaction_net_by_day(bank_account).values()), 2)
    rate = _rate(currency, str(now.date()), rates)
    period_net = round(sum(row["deposit"] - row["withdrawal"] for row in rows), 2)
    first_day = min((row["date"] for row in rows if row["date"]), default=None)
    ledger_before = round(sum(net for day, net in erp_by_day.items() if first_day and day < first_day), 2)
    wise_start = round(balance - period_net, 2) if balance is not None else None
    created_ids = {id(row) for row in create}
    report = {
        "currency": currency,
        "bank_account": bank_account,
        "parsed": len(rows),
        "create": len(create),
        "create_by_month": _by_month(create),
        "create_by_kind": _by_kind(create),
        # the rows that were not created: already fed, or matched to a bexio line (the FEE rows show whether a fee matched)
        "parsed_by_kind": _by_kind(rows),
        "matched_by_kind": _by_kind([row for row in rows if id(row) not in created_ids]),
        "by_month": _by_month_check(rows, erp_by_day),
        # the Bank Transactions no feed row matches, per month (counts): bexio's Wise lines that the feed does not cover
        "unmatched_bank_transactions": _unmatched_by_month(bank_account, rows),
        **summary,
        "create_chf": create_chf,
        "erp_net": erp_net,
        "erp_net_chf": erp_chf,
        "bank_transaction_net": bank_transaction_net,
        "no_rate_days": create_gap + erp_gap + (0 if rate else 1),
        "period_net": period_net,
        "wise_balance": balance,
        "wise_balance_chf": round(balance * rate, 2) if balance is not None and rate else None,
        "wise_start": wise_start,
        "ledger_before_first_day": ledger_before,
        "start_agrees": wise_start is not None and abs(wise_start - ledger_before) < 0.005,
        "agrees": balance is not None and abs(balance - erp_net) < 0.005,
        "difference": round(balance - erp_net, 2) if balance is not None else None,
    }
    return report


def _total(accounts):
    """The CHF totals over the currencies. A balance or a day with no rate is left out and counted in no_rate_days."""
    return {
        "wise_balance_chf": round(sum(a["wise_balance_chf"] or 0 for a in accounts), 2),
        "erp_net_chf": round(sum(a["erp_net_chf"] for a in accounts), 2),
        "create_chf": round(sum(a["create_chf"] for a in accounts), 2),
        "no_rate_days": sum(a["no_rate_days"] for a in accounts),
        "agrees": all(a["agrees"] for a in accounts),
    }


def _summary_lines(report):
    """One line per currency for the message: the counts, the amounts in the currency and in CHF, the check."""
    lines = []
    for a in report["accounts"]:
        months = ", ".join("{} {}".format(month, n) for month, n in sorted(a["create_by_month"].items()))
        kinds = ", ".join("{} {}".format(kind, n) for kind, n in sorted(a["create_by_kind"].items()))
        lines.append(
            "{}: {} to create ({} CHF) [{}] [{}]; Wise {} ({} CHF); ERPNext {} ({} CHF); agrees: {}; {} already fed, {} already imported".format(
                a["currency"],
                a["create"],
                a["create_chf"],
                months or "none",
                kinds or "none",
                a["wise_balance"],
                a["wise_balance_chf"],
                a["erp_net"],
                a["erp_net_chf"],
                "yes" if a["agrees"] else "no",
                a["already_fed"],
                a["already_imported"],
            )
        )
    total = report["total"]
    lines.append(
        "Total CHF: create {}, Wise {}, ERPNext {}; days without a rate: {}; skipped {}".format(
            total["create_chf"], total["wise_balance_chf"], total["erp_net_chf"], total["no_rate_days"], report["skipped"]
        )
    )
    return lines


def _rate(currency, day, rates):
    """The rate of a currency to CHF on a day (a string), from ERPNext's Currency Exchange rates. 0.0 when none is known.

    Cached per run: a backfill covers many days, and ERPNext's fallback may fetch a rate from its configured source.
    """
    if currency == "CHF":
        return 1.0
    key = (currency, day)
    if key not in rates:
        rates[key] = get_exchange_rate(currency, "CHF", day) or 0.0
    return rates[key]


def _create_chf(create, currency, rates):
    """The CHF value of the rows to create: the CHF the source gave where it did, the rest at each day's rate.

    Returns the total and the count of days with no rate (left out).
    """
    given = 0.0
    for row in create:
        if row["chf"] is not None:
            given += row["chf"] if row["deposit"] >= row["withdrawal"] else -row["chf"]
    total, no_rate = _chf_sum(_net_by_day([row for row in create if row["chf"] is None]), currency, rates)
    return round(given + total, 2), no_rate


def _chf_sum(net_by_day, currency, rates):
    """The sum of per-day amounts in CHF, each day at its own rate, and the count of days with no rate (left out)."""
    total = 0.0
    no_rate = 0
    for day, net in net_by_day.items():
        rate = _rate(currency, day, rates)
        if rate:
            total += net * rate
        else:
            no_rate += 1
    return round(total, 2), no_rate


def _net_by_day(rows):
    """The feed rows' deposits less withdrawals, per day."""
    net = collections.defaultdict(float)
    for row in rows:
        net[row["date"]] += row["deposit"] - row["withdrawal"]
    return net


def _by_month(rows):
    """The count of feed rows per month (YYYY-MM)."""
    return dict(collections.Counter(row["date"][:7] for row in rows))


def _by_month_check(rows, erp_by_day):
    """Per month (YYYY-MM): the feed's net of its rows, ERPNext's net of the account, and the difference (ledger less feed).

    The remaining gap of the balance check, month by month: the amounts go to the private dry-run file, not to a note.
    """
    feed = collections.defaultdict(float)
    for row in rows:
        feed[row["date"][:7]] += row["deposit"] - row["withdrawal"]
    ledger = collections.defaultdict(float)
    for day, net in erp_by_day.items():
        ledger[day[:7]] += net
    return {
        month: {"feed": round(feed[month], 2), "ledger": round(ledger[month], 2), "difference": round(ledger[month] - feed[month], 2)}
        for month in sorted(set(feed) | set(ledger))
    }


def _by_kind(rows):
    """The count of feed rows per activity type (CARD_PAYMENT, BALANCE_DEPOSIT, TRANSFER, FEE)."""
    return dict(collections.Counter(row["kind"] for row in rows))


def ensure_bank_account(currency):
    """The Bank Account of a currency, with its GL account, made when missing. An existing one is left as it is."""
    name = _bank_account_name(currency)
    if frappe.db.exists("Bank Account", name):
        return name
    chf = frappe.db.get_value("Bank Account", "Wise CHF - " + BANK, ["account", "company"], as_dict=True)
    parent = frappe.db.get_value("Account", chf["account"], "parent_account")
    return bank_feed.open_bank_account("Wise", BANK, currency, parent, chf["company"], ACCOUNT_NUMBERS)


def _bank_account_name(currency):
    return bank_feed.bank_account_name("Wise", currency, BANK)


def _start(settings, now):
    if settings.last_sync:
        return settings.last_sync - OVERLAP
    # a Date single comes back as its stored string, not a date: the first sync failed on it
    return datetime.datetime.combine(frappe.utils.getdate(settings.backfill_from), datetime.time.min)


def _amount(balance):
    return float((balance.get("amount") or {}).get("value") or 0)


def _first_day(start, rows, bank_account):
    """The earliest day an amount of this account falls on: the sync start, a feed row, or a Bank Transaction there."""
    days = [start.date()] + [datetime.date.fromisoformat(row["date"]) for row in rows if row["date"]]
    earliest = frappe.db.sql(
        "select min(date) from `tabBank Transaction` where bank_account = %s and docstatus < 2", bank_account
    )[0][0]
    if earliest:
        days.append(earliest)
    return min(days)


def _gl_net_by_day(bank_account):
    """The GL net of the Bank Account's GL account per posting day: debit less credit in the account's currency, not cancelled.

    The ledger of the balance check. bexio booked every movement of the Wise account on its GL account (1021 for CHF); the
    Bank Transactions hold only what bexio's bank sync brought in, so they are a second column, not the ledger.
    """
    gl_account = frappe.db.get_value("Bank Account", bank_account, "account")
    if not gl_account:
        return {}
    days = frappe.db.sql(
        "select posting_date, coalesce(sum(debit_in_account_currency - credit_in_account_currency), 0) from `tabGL Entry`"
        " where account = %s and is_cancelled = 0 group by posting_date",
        gl_account,
    )
    return {str(day): float(net) for day, net in days}


def _bank_transaction_net_by_day(bank_account):
    """The account's submitted or draft Bank Transactions, deposits less withdrawals, per day. Opening balances are not in it."""
    if not frappe.db.exists("Bank Account", bank_account):
        return {}
    days = frappe.db.sql(
        "select date, coalesce(sum(deposit - withdrawal), 0) from `tabBank Transaction`"
        " where bank_account = %s and docstatus < 2 group by date",
        bank_account,
    )
    return {str(day): float(net) for day, net in days}


def _unmatched_by_month(bank_account, rows):
    """Per month (YYYY-MM): the account's Bank Transactions that no feed row matches, as counts."""
    if not frappe.db.exists("Bank Account", bank_account):
        return {}
    existing = frappe.get_all(
        "Bank Transaction",
        filters={"bank_account": bank_account, "docstatus": ["<", 2]},
        fields=["transaction_id", "date", "deposit", "withdrawal"],
    )
    return unmatched_by_month(existing, rows)


def unmatched_by_month(existing, rows):
    """Per month (YYYY-MM): the Bank Transactions no feed row matches, as counts. Pure: the caller reads both lists.

    existing: the account's Bank Transactions (transaction_id, date, deposit, withdrawal). A Bank Transaction matches a row
    by transaction_id; a bexio line, which has none, matches a row of the same date and amount, once each, as
    bank_feed.new_rows does. Only the counts are returned: the amounts stay out of the report.
    """
    fed = {row["transaction_id"] for row in rows}
    # the rows that no Bank Transaction carries by id: their date and amount are what a bexio line is matched on
    carried = {t["transaction_id"] for t in existing if t["transaction_id"] in fed}
    wanted = collections.Counter(bank_feed._key(row) for row in rows if row["transaction_id"] not in carried)
    unmatched = collections.Counter()
    for t in existing:
        if t["transaction_id"]:
            if t["transaction_id"] not in fed:
                unmatched[str(t["date"])[:7]] += 1
        elif wanted[bank_feed._key(t)] > 0:
            wanted[bank_feed._key(t)] -= 1
        else:
            unmatched[str(t["date"])[:7]] += 1
    return dict(sorted(unmatched.items()))
