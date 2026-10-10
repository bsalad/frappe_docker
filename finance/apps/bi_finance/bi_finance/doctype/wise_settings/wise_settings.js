frappe.ui.form.on("Wise Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test connection"), () => {
			frappe.call({ method: "bi_finance.wise.test_connection", freeze: true });
		});
		frm.add_custom_button(__("Dry run"), () => {
			frappe.call({ method: "bi_finance.wise.sync_now", args: { dry_run: true }, freeze: true, freeze_message: __("Reading Wise") });
		});
		frm.add_custom_button(__("Sync now"), () => {
			frappe.confirm(__("Write the new Wise movements to ERPNext?"), () => {
				frappe.call({ method: "bi_finance.wise.sync_now", args: { dry_run: false }, freeze: true, freeze_message: __("Syncing Wise") });
			});
		});
	},
});
