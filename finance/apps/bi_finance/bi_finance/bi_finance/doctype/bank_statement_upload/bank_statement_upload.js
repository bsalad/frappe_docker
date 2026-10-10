frappe.ui.form.on("Bank Statement Upload", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status === "Imported") return;
		frm.add_custom_button(__("Dry run"), () => run(frm, "dry_run"));
		if (frm.doc.status === "Dry run") {
			frm.add_custom_button(__("Import"), () => {
				frappe.confirm(__("Import the new transactions into the bank ledger? The upload is imported once."), () => run(frm, "import_statement"));
			});
		}
	},
	// The Bank Account defaults to the one whose IBAN is the file's; a chosen account is never replaced.
	statement_file(frm) {
		if (!frm.doc.statement_file || frm.doc.bank_account) return;
		frappe.call({
			method: "bi_finance.camt_import.account_for_file",
			args: { file_url: frm.doc.statement_file },
			callback: (r) => r.message && frm.set_value("bank_account", r.message),
		});
	},
});

// The server reads the saved file and account, so unsaved edits are saved first.
function run(frm, method) {
	const call = () => frappe.call({
		method: `bi_finance.bi_finance.doctype.bank_statement_upload.bank_statement_upload.${method}`,
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: method === "dry_run" ? __("Reading the statement") : __("Importing the statement"),
		// A failed run has set the record to Failed on the server: show that too.
		callback: () => frm.reload_doc(),
		error: () => frm.reload_doc(),
	});
	if (frm.is_dirty()) {
		frm.save().then(call);
	} else {
		call();
	}
}
