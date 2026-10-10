frappe.query_reports["Cash Flow Forecast"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "as_of_date",
			label: __("As of Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			// off: the forecast of the documents alone, without the receipts of invoices not issued yet
			fieldname: "include_run_rate",
			label: __("Include new sales run-rate"),
			fieldtype: "Check",
			default: 1,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		// the week with the lowest closing balance stands out in the table
		if (data && data.lowest) {
			value = `<span style="color: var(--red-500); font-weight: 600">${value}</span>`;
		}
		return value;
	},
};
