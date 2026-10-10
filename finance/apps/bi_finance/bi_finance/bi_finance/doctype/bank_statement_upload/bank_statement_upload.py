"""A UBS camt file uploaded for import: the record of what entered the bank ledger from it.

Dry run reads the attached file with camt_import.dry_run and writes nothing; the record keeps the summary.
Import needs a dry run on the same file, and runs once: the record then stays Imported with its count.
A failed run leaves the record Failed with the reason, and nothing of that run in the ledger.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from bi_finance import camt_import


class BankStatementUpload(Document):
    def validate(self):
        before = self.get_doc_before_save()
        if before is None or (before.statement_file == self.statement_file and before.bank_account == self.bank_account):
            return
        if before.status == "Imported":
            frappe.throw("An imported upload keeps its file and Bank Account")
        # The dry run was on the old file: a new one needs its own.
        self.status = "Draft"
        self.summary = None
        self.detected_version = None

    def run_dry_run(self):
        if self.status == "Imported":
            frappe.throw("{} is imported already; it keeps its dry run".format(self.name))
        try:
            result = camt_import.dry_run(self.statement_file, self.bank_account)
        except Exception as err:
            self._fail(err)
        self._record(result, "Dry run")
        self.save()
        return {"status": self.status, "summary": self.summary}

    def run_import(self):
        if self.status == "Imported":
            frappe.throw("{} was imported on {}; an upload imports once".format(self.name, self.imported_on))
        if self.status != "Dry run":
            frappe.throw("Run the dry run on this file before the import")
        try:
            result = camt_import.import_statement(self.statement_file, self.bank_account, upload=self.name)
        except Exception as err:
            self._fail(err)
        self._record(result, "Imported")
        self.summary += "\nImported: {}".format(result["imported"])
        self.imported_count = result["imported"]
        self.imported_on = now_datetime()
        self.imported_by = frappe.session.user
        self.save()
        return {"status": self.status, "imported": self.imported_count, "summary": self.summary}

    def _record(self, result, status):
        self.status = status
        self.detected_version = result["version"]
        self.summary = summary_text(result)

    def _fail(self, err):
        # The import may have written some Bank Transactions before it failed: they go, and the record says why.
        frappe.db.rollback()
        self.db_set({"status": "Failed", "summary": str(err)}, update_modified=True)
        frappe.db.commit()
        frappe.throw(str(err))


def summary_text(result):
    """The dry run's (or import's) result as the record's summary: plain lines, one per count."""
    counts = result["counts"]
    lines = [
        "Bank Account {} (IBAN {}, {})".format(result["bank_account"], result["iban"], result["currency"]),
        "File {}".format(result["version"]),
        "Transactions: {} new, {} already on the account, {} matched to bexio lines".format(counts["new"], counts["id"], counts["bexio"]),
        "Skipped: {} not booked, {} covered by a details entry".format(counts["skipped"]["not booked"], counts["skipped"]["covered"]),
    ]
    if counts["by_month"]:
        lines.append("By booking month:")
        for month, month_counts in counts["by_month"].items():
            lines.append("  {}: {} new, {} already on the account, {} matched to bexio lines".format(
                month, month_counts["new"], month_counts["id"], month_counts["bexio"]))
    if result["balances"]:
        lines.append("Balance check (reported, never blocks):")
        for code, check in result["balances"].items():
            lines.append("  {} {}: file {:.2f}, ledger {:.2f}, difference {:.2f}".format(
                code, check["date"], check["file"], check["ledger"], check["diff"]))
    return "\n".join(lines)


@frappe.whitelist(methods=["POST"])
def dry_run(name):
    upload = frappe.get_doc("Bank Statement Upload", name)
    upload.check_permission("write")
    return upload.run_dry_run()


@frappe.whitelist(methods=["POST"])
def import_statement(name):
    upload = frappe.get_doc("Bank Statement Upload", name)
    upload.check_permission("write")
    return upload.run_import()
