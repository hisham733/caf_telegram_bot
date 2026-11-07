frappe.listview_settings["Telegram Bot"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Create Bot via BotFather"), () => {
			frappe.prompt(
				[
					{
						fieldname: "adder_account",
						fieldtype: "Link",
						options: "Telegram Adder Account",
						label: __("Adder Account"),
						reqd: 1,
					},
					{
						fieldname: "display_name",
						fieldtype: "Data",
						label: __("Bot Display Name"),
						reqd: 1,
					},
					{
						fieldname: "username",
						fieldtype: "Data",
						label: __("Bot Username"),
						reqd: 1,
						description: __(
							"5-32 characters, letters/digits/underscores, must end with 'bot'."
						),
					},
				],
				(values) => {
					frappe.call({
						method: "caf_telegram_bot.api.create_bot",
						args: values,
						freeze: true,
						freeze_message: __("Talking to BotFather..."),
						callback: (r) => {
							if (r.exc || !r.message) return;
							frappe.show_alert({
								message: __("Bot created: @{0}", [r.message.username]),
								indicator: "green",
							});
							listview.refresh();
							frappe.set_route("Form", "Telegram Bot", r.message.name);
						},
					});
				},
				__("Create Bot via BotFather"),
				__("Create")
			);
		});
	},
};
