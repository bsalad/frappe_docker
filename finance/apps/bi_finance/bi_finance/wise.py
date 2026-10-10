"""Wise sync: each balance as a Bank Account, its statements as Bank Transactions, through bank_feed.

Run by the hourly scheduler (sync_scheduled) and by the Test connection, Dry run and Sync now buttons of Wise Settings.
A dry run writes nothing: it reports what each account would get. The token is read with get_password; it is never
logged or returned to the page.
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

# statements are read from a week before the last sync, so a movement Wise posts late is not missed
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
    """One pass over the profile's balances. Returns {"accounts": one row per currency, "total": the CHF totals}.

    Every amount is shown in its own currency and in CHF, at the rate of the day it falls on. Nothing is written when
    dry_run.
    """
    settings, token = _settings()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    start = _start(settings, now)
    rates = {}
    accounts = []
    for balance in wise_client.balances(token, settings.profile_id):
        currency = balance["currency"]
        # the balance id is needed for the statement; the account name is planned before it exists on a dry run
        bank_account = _bank_account_name(currency)
        rows = []
        for window_start, window_end in wise_client.windows(start, now):
            body = wise_client.statement(token, settings.profile_id, balance["id"], currency, window_start, window_end)
            rows += wise_client.statement_rows(body, balance["id"], currency)
        if currency != "CHF":
            # the rate of every day this account's amounts fall on, so the conversion below needs no lookup per day
            rates.update(bank_feed.load_rates(currency, _first_day(start, rows, bank_account), now.date(), dry_run))
        create, summary = bank_feed.plan(bank_account, rows)
        if not dry_run:
            ensure_bank_account(currency)
            bank_feed.write(bank_account, create)
            frappe.db.commit()
        accounts.append(_account_report(currency, bank_account, create, summary, _amount(balance), now, rates))
    if not dry_run:
        # one field, not settings.save(): a save would write back the token and switches as read at the start of the run
        frappe.db.set_single_value("Wise Settings", "last_sync", now)
        frappe.db.commit()
    return {"accounts": accounts, "total": _total(accounts)}


def _account_report(currency, bank_account, create, summary, balance, now, rates):
    """One currency's row: the counts, the Wise balance and ERPNext's net, each in the currency and in CHF."""
    create_chf, create_gap = _chf_sum(_net_by_day(create), currency, rates)
    erp_by_day = _erp_net_by_day(bank_account)
    erp_chf, erp_gap = _chf_sum(erp_by_day, currency, rates)
    rate = _rate(currency, str(now.date()), rates)
    return {
        "currency": currency,
        "bank_account": bank_account,
        "create": len(create),
        "create_by_month": _by_month(create),
        **summary,
        "wise_balance": balance,
        "wise_balance_chf": round(balance * rate, 2) if rate else None,
        "erp_net": round(sum(erp_by_day.values()), 2),
        "erp_net_chf": erp_chf,
        "create_chf": create_chf,
        "no_rate_days": create_gap + erp_gap + (0 if rate else 1),
    }


def _total(accounts):
    """The CHF totals over the currencies. A balance or a day with no rate is left out and counted in no_rate_days."""
    return {
        "wise_balance_chf": round(sum(a["wise_balance_chf"] or 0 for a in accounts), 2),
        "erp_net_chf": round(sum(a["erp_net_chf"] for a in accounts), 2),
        "create_chf": round(sum(a["create_chf"] for a in accounts), 2),
        "no_rate_days": sum(a["no_rate_days"] for a in accounts),
    }


def _summary_lines(report):
    """One line per currency for the message: the counts, then the amounts in the currency and in CHF."""
    lines = []
    for a in report["accounts"]:
        months = ", ".join("{} {}".format(month, n) for month, n in sorted(a["create_by_month"].items()))
        lines.append(
            "{}: {} to create ({} CHF) [{}]; Wise {} ({} CHF); ERPNext {} ({} CHF); {} already fed, {} already imported".format(
                a["currency"],
                a["create"],
                a["create_chf"],
                months or "none",
                a["wise_balance"],
                a["wise_balance_chf"],
                a["erp_net"],
                a["erp_net_chf"],
                a["already_fed"],
                a["already_imported"],
            )
        )
    total = report["total"]
    lines.append(
        "Total CHF: create {}, Wise {}, ERPNext {}; days without a rate: {}".format(
            total["create_chf"], total["wise_balance_chf"], total["erp_net_chf"], total["no_rate_days"]
        )
    )
    return lines


def _rate(currency, day, rates):
    """The rate of a currency to CHF on a day (a string), from ERPNext's Currency Exchange rates. 0.0 when none is known.

    Cached per run: a backfill covers many days, and ERPNext's fallback may fetch a rate from its configured source.
    """
    key = (currency, day)
    if key not in rates:
        rates[key] = get_exchange_rate(currency, "CHF", day) or 0.0
    return rates[key]


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
    """The earliest day an amount of this account falls on: the sync start, a statement row, or a Bank Transaction there."""
    days = [start.date()] + [datetime.date.fromisoformat(row["date"]) for row in rows if row["date"]]
    earliest = frappe.db.sql(
        "select min(date) from `tabBank Transaction` where bank_account = %s and docstatus < 2", bank_account
    )[0][0]
    if earliest:
        days.append(earliest)
    return min(days)


def _erp_net_by_day(bank_account):
    """The account's submitted or draft Bank Transactions, deposits less withdrawals, per day. Opening balances are not in it."""
    if not frappe.db.exists("Bank Account", bank_account):
        return {}
    days = frappe.db.sql(
        "select date, coalesce(sum(deposit - withdrawal), 0) from `tabBank Transaction`"
        " where bank_account = %s and docstatus < 2 group by date",
        bank_account,
    )
    return {str(day): float(net) for day, net in days}
