frappe.listview_settings["Telegram User"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Generate Link Code"), () => {
			frappe.prompt(
				[
					{
						fieldname: "erpnext_user",
						fieldtype: "Link",
						options: "User",
						label: __("ERPNext User"),
						default: frappe.session.user,
						reqd: 1,
					},
				],
				(values) => {
					frappe.call({
						method: "caf_telegram_bot.api.generate_link_code",
						args: { erpnext_user: values.erpnext_user },
						callback: (r) => {
							if (!r.message) return;
							frappe.msgprint({
								title: __("Telegram Link Code"),
								indicator: "blue",
								message: __(
									"Send <b>/link {0}</b> to the Telegram bot within {1} minutes.<br>The code is linked to {2}.",
									[
										r.message.code,
										Math.round((r.message.expires_in || 900) / 60),
										r.message.erpnext_user,
									]
								),
							});
						},
					});
				},
				__("Generate Telegram Link Code"),
				__("Generate")
			);
		});
	},
};
