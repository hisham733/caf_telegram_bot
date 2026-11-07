frappe.ui.form.on("Task", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const has_group = Boolean(frm.doc.custom_telegram_group_id);
		frm.add_custom_button(
			has_group ? __("Open Telegram Group") : __("Create Telegram Group"),
			() => {
				if (has_group) {
					open_group(frm);
				} else {
					create_group(frm, false);
				}
			},
			__("Telegram")
		);
	},
});

function open_group(frm) {
	frappe.call({
		method: "caf_telegram_bot.api.get_task_group",
		args: { task: frm.doc.name },
		callback: (r) => {
			const data = r.message || {};
			if (data.invite_link) {
				window.open(data.invite_link, "_blank");
				return;
			}
			frappe.confirm(
				__("No invite link is stored for this group. Try to create one now?"),
				() => create_group(frm, true)
			);
		},
	});
}

function create_group(frm, quiet) {
	const run = () => {
		frappe.call({
			method: "caf_telegram_bot.api.create_task_group",
			args: { task: frm.doc.name },
			freeze: true,
			freeze_message: __("Provisioning Telegram group..."),
			callback: (r) => {
				if (r.exc || !r.message) {
					return;
				}
				const data = r.message;
				frm.reload_doc();

				let message = frappe.utils.escape_html(
					data.message || __("Telegram group ready.")
				);
				if (data.invite_link) {
					message += `<br><br><a href="${data.invite_link}" target="_blank">${data.invite_link}</a>`;
				}
				if (data.privacy_warning) {
					message += `<br><br><b>${__("Privacy warning")}:</b> ${frappe.utils.escape_html(
						data.privacy_warning
					)}`;
				}
				if (data.invite_error) {
					message += `<br><br><b>${__("Invite link error")}:</b> ${frappe.utils.escape_html(
						data.invite_error
					)}`;
				}
				frappe.msgprint({
					title: __("Telegram Group"),
					message,
					indicator: data.invite_error ? "orange" : "green",
				});
			},
		});
	};

	if (quiet) {
		run();
		return;
	}
	frappe.confirm(
		__("Create a Telegram supergroup for this Task and add all verified members?"),
		run
	);
}
