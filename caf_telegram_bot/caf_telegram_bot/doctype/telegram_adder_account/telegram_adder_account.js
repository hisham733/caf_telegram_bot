frappe.ui.form.on("Telegram Adder Account", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Send Login OTP"), () => {
			frappe.call({
				method: "caf_telegram_bot.api.start_login",
				args: { adder_account: frm.doc.name },
				freeze: true,
				freeze_message: __("Requesting OTP from Telegram..."),
				callback: (r) => {
					if (r.exc) return;
					if (r.message && r.message.status === "already_logged_in") {
						frappe.show_alert({
							message: __("This account is already logged in."),
							indicator: "green",
						});
						frm.reload_doc();
						return;
					}
					frappe.msgprint({
						title: __("OTP Sent"),
						message: __(
							"Telegram sent a login code to {0}. Click Verify Login OTP and enter it.",
							[frm.doc.phone]
						),
						indicator: "green",
					});
				},
			});
		}, __("Login"));

		frm.add_custom_button(__("Verify Login OTP"), () => {
			frappe.prompt(
				{
					fieldname: "code",
					fieldtype: "Data",
					label: __("OTP Code"),
					reqd: 1,
				},
				(values) => {
					const finish = (args) => {
						frappe.call({
							method: "caf_telegram_bot.api.finish_login",
							args: Object.assign({ adder_account: frm.doc.name }, args),
							freeze: true,
							freeze_message: __("Verifying with Telegram..."),
							callback: (r) => {
								if (r.exc) return;
								if (r.message && r.message.status === "password_required") {
									frappe.prompt(
										{
											fieldname: "password",
											fieldtype: "Password",
											label: __("Two-Step Verification Password"),
											reqd: 1,
										},
										(pw) => finish({ password: pw.password }),
										__("2FA Password"),
										__("Verify")
									);
									return;
								}
								frappe.show_alert({
									message: __("Adder account logged in."),
									indicator: "green",
								});
								frm.reload_doc();
							},
						});
					};
					finish({ code: values.code });
				},
				__("Verify Login OTP"),
				__("Verify")
			);
		}, __("Login"));

		frm.add_custom_button(__("Check Login"), () => {
			frappe.call({
				method: "caf_telegram_bot.api.check_login",
				args: { adder_account: frm.doc.name },
				freeze: true,
				callback: (r) => {
					if (r.exc) return;
					const msg = r.message || {};
					if (msg.status === "logged_in") {
						frappe.msgprint({
							title: __("Logged In"),
							message: __("Telegram account: {0} (id {1})", [
								msg.username || msg.name || "-",
								msg.telegram_id || "-",
							]),
							indicator: "green",
						});
						frm.reload_doc();
					} else {
						frappe.msgprint({
							title: __("Not Logged In"),
							message: __("Use Send Login OTP to log in this account."),
							indicator: "orange",
						});
					}
				},
			});
		}, __("Login"));

		frm.add_custom_button(__("Create Bot via BotFather"), () => {
			frappe.prompt(
				[
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
						args: {
							adder_account: frm.doc.name,
							display_name: values.display_name,
							username: values.username,
						},
						freeze: true,
						freeze_message: __("Talking to BotFather..."),
						callback: (r) => {
							if (r.exc || !r.message) return;
							frappe.show_alert({
								message: __("Bot created: @{0}", [r.message.username]),
								indicator: "green",
							});
							frappe.set_route("Form", "Telegram Bot", r.message.name);
						},
					});
				},
				__("Create Bot via BotFather"),
				__("Create")
			);
		});
	},
});
