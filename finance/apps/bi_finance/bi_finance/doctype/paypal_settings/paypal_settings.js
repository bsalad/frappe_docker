frappe.ui.form.on("PayPal Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test connection"), () => {
			frappe.call({ method: "bi_finance.paypal.test_connection", freeze: true });
		});
		frm.add_custom_button(__("Dry run"), () => {
			frappe.call({ method: "bi_finance.paypal.sync_now", args: { dry_run: true }, freeze: true, freeze_message: __("Reading PayPal") });
		});
		frm.add_custom_button(__("Sync now"), () => {
			frappe.confirm(__("Write the new PayPal transactions to ERPNext?"), () => {
				frappe.call({ method: "bi_finance.paypal.sync_now", args: { dry_run: false }, freeze: true, freeze_message: __("Syncing PayPal") });
			});
		});
	},
});
