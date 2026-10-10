frappe.query_reports["Payroll Hand-over"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "year",
			label: __("Year"),
			fieldtype: "Int",
			reqd: 1,
			default: frappe.datetime.get_today().substr(0, 4),
		},
		{
			fieldname: "month",
			label: __("Month"),
			fieldtype: "Select",
			options: "\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12",
			description: __("Empty: one row per employee for the whole year"),
		},
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: "Hand-over\nLohnausweis",
			default: "Hand-over",
			description: __("Lohnausweis: the amounts by the Form 11 lines"),
		},
	],
};
