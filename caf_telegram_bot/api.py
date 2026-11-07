import frappe

from caf_telegram_bot import mtproto, provisioning, router


def _account(name):
	if not name:
		frappe.throw("Adder account is required.")
	return frappe.get_doc("Telegram Adder Account", name)


@frappe.whitelist(methods=["POST"])
def start_login(adder_account):
	frappe.only_for("System Manager")
	try:
		return mtproto.start_login(_account(adder_account))
	except mtproto.MTProtoError as exc:
		frappe.throw(str(exc))


@frappe.whitelist(methods=["POST"])
def finish_login(adder_account, code=None, password=None):
	frappe.only_for("System Manager")
	try:
		return mtproto.finish_login(_account(adder_account), code=code, password=password)
	except mtproto.MTProtoError as exc:
		frappe.throw(str(exc))


@frappe.whitelist(methods=["POST"])
def check_login(adder_account):
	frappe.only_for("System Manager")
	try:
		return mtproto.check_login(_account(adder_account))
	except mtproto.MTProtoError as exc:
		frappe.throw(str(exc))


@frappe.whitelist(methods=["POST"])
def create_bot(adder_account, display_name, username):
	frappe.only_for("System Manager")
	try:
		bot = mtproto.create_bot(_account(adder_account), display_name, username)
	except mtproto.MTProtoError as exc:
		frappe.throw(str(exc))

	return {"name": bot.name, "username": bot.username, "bot_id": bot.bot_id}


@frappe.whitelist(methods=["POST"])
def create_task_group(task, bot=None, add_members=True):
	task_doc = frappe.get_doc("Task", task)
	if not frappe.has_permission("Task", "write", doc=task_doc):
		frappe.throw(
			"You do not have permission to edit this Task.", frappe.PermissionError
		)
	if isinstance(add_members, str):
		add_members = add_members.lower() not in ("0", "false", "no", "off", "")
	try:
		return provisioning.provision_task_group(
			task,
			bot_name=bot,
			add_members=bool(add_members),
			user=frappe.session.user,
		)
	except (frappe.PermissionError, frappe.ValidationError):
		raise
	except Exception as exc:
		frappe.throw(str(exc))


@frappe.whitelist()
def get_task_group(task):
	task_doc = frappe.get_doc("Task", task)
	if not frappe.has_permission("Task", "read", doc=task_doc):
		frappe.throw(
			"You do not have permission to view this Task.", frappe.PermissionError
		)
	chat = frappe.db.get_value(
		"Telegram Chat",
		{"task": task},
		["name", "chat_id", "title", "invite_link", "bot", "enabled"],
		as_dict=True,
	)
	return chat or {}


@frappe.whitelist(methods=["POST"])
def disable_bot_privacy(bot_name):
	frappe.only_for("System Manager")
	bot = frappe.get_doc("Telegram Bot", bot_name)
	if not bot.adder_account:
		frappe.throw(f"Telegram Bot {bot.name} has no Adder Account set.")
	account = frappe.get_doc("Telegram Adder Account", bot.adder_account)
	try:
		result = mtproto.disable_bot_privacy(account, bot.username)
	except mtproto.MTProtoError as exc:
		frappe.throw(str(exc))
	bot.db_set("privacy_disabled", 1)
	frappe.db.commit()
	return result


@frappe.whitelist()
def generate_link_code(erpnext_user=None):
	if frappe.session.user == "Guest":
		frappe.throw("Please log in.")

	user = erpnext_user or frappe.session.user
	if user != frappe.session.user and "System Manager" not in frappe.get_roles():
		frappe.throw("Only a System Manager can generate link codes for other users.")

	if not frappe.db.exists("User", user) or not frappe.db.get_value("User", user, "enabled"):
		frappe.throw("That ERPNext user is missing or disabled.")

	code = router.generate_link_code(user)
	return {
		"code": code,
		"erpnext_user": user,
		"expires_in": router.LINK_TTL,
		"instructions": f"Send /link {code} to the Telegram bot.",
	}
