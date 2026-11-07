frappe.ui.form.on("Telegram Bot", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.privacy_disabled) {
			return;
		}
		if (!frm.doc.adder_account || !frm.doc.username) {
			return;
		}
		frm.add_custom_button(__("Disable Privacy Mode"), () => {
			frappe.confirm(
				__("Ask @BotFather to disable group privacy mode for @{0}?", [
					frm.doc.username,
				]),
				() => {
					frappe.call({
						method: "caf_telegram_bot.api.disable_bot_privacy",
						args: { bot_name: frm.doc.name },
						freeze: true,
						freeze_message: __("Contacting @BotFather..."),
						callback: (r) => {
							if (r.exc) {
								return;
							}
							frm.reload_doc();
							frappe.show_alert({
								message: __("Privacy mode disabled."),
								indicator: "green",
							});
						},
					});
				}
			);
		});
	},
});
