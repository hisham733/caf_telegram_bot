import frappe
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime

from caf_telegram_bot import mtproto
from caf_telegram_bot.telegram_client import TelegramBotClient


def get_bot(bot_name=None):
	if bot_name:
		bot = frappe.get_doc("Telegram Bot", bot_name)
	else:
		name = frappe.db.get_value(
			"Telegram Bot", {"is_primary": 1, "enabled": 1}, "name"
		)
		if not name:
			name = frappe.db.get_value(
				"Telegram Bot", {"enabled": 1}, "name", order_by="creation asc"
			)
		if not name:
			frappe.throw("No enabled Telegram Bot is configured.")
		bot = frappe.get_doc("Telegram Bot", name)

	if not bot.enabled:
		frappe.throw(f"Telegram Bot {bot.name} is disabled.")
	if not bot.username:
		frappe.throw(f"Telegram Bot {bot.name} has no username.")
	return bot


def get_adder_account(bot):
	if not bot.adder_account:
		frappe.throw(f"Telegram Bot {bot.name} has no Adder Account set.")
	account = frappe.get_doc("Telegram Adder Account", bot.adder_account)
	if not account.enabled:
		frappe.throw(f"Telegram Adder Account {account.name} is disabled.")

	if account.flood_wait_until:
		until = get_datetime(account.flood_wait_until)
		if until > now_datetime():
			frappe.throw(
				f"Telegram Adder Account {account.name} is in flood wait until {until}."
			)
		account.db_set(
			{
				"status": "Active" if account.status == "Flood Wait" else account.status,
				"flood_wait_until": None,
			}
		)
		frappe.db.commit()

	if account.status != "Active":
		frappe.throw(
			f"Telegram Adder Account {account.name} is not logged in. "
			f"Status: {account.status}."
		)
	return account


def get_verified_members():
	return frappe.get_all(
		"Telegram User",
		filters={"verified": 1, "enabled": 1, "erpnext_user": ["is", "set"]},
		fields=["name", "telegram_id", "username", "erpnext_user"],
		order_by="creation asc",
	)


def get_add_limit(account):
	today = getdate()
	last_add = getdate(account.last_add_on) if account.last_add_on else None
	if last_add != today and int(account.adds_today or 0):
		account.adds_today = 0
		account.db_set("adds_today", 0)
		frappe.db.commit()

	limit = int(account.daily_add_limit or 0)
	used = int(account.adds_today or 0)
	return max(0, limit - used)


def provision_task_group(task_name, bot_name=None, add_members=True, user=None):
	if not task_name:
		frappe.throw("Task is required.")
	task = frappe.get_doc("Task", task_name)
	user = user or frappe.session.user
	if not frappe.has_permission("Task", "write", doc=task, user=user):
		frappe.throw(
			"You do not have permission to edit this Task.", frappe.PermissionError
		)

	bot = get_bot(bot_name)
	account = get_adder_account(bot)

	if task.custom_telegram_group_id:
		return _ensure_existing_group(task, bot, account)

	title = _group_title(task)
	about = _group_about(task)
	members = get_verified_members() if add_members else []
	add_limit = get_add_limit(account) if members else 0

	result = mtproto.create_group(
		account,
		title=title,
		about=about,
		bot_username=bot.username,
		members=members,
		add_limit=add_limit,
	)
	chat_id = result["chat_id"]

	chat = frappe.new_doc("Telegram Chat")
	chat.chat_id = chat_id
	chat.title = result.get("title") or title
	chat.chat_type = "supergroup"
	chat.enabled = 1
	chat.task = task.name
	chat.bot = bot.name
	chat.insert(ignore_permissions=True)

	added = result.get("added") or []
	failed = result.get("failed") or {}
	_update_add_counters(account, added, result.get("flood_wait"))

	privacy_warning = _ensure_privacy_disabled(bot, account)
	invite_link, invite_error = _create_invite_link(chat, bot)
	welcome_error = _send_welcome(bot, chat_id, task, invite_link)

	frappe.db.set_value(
		"Task", task.name, "custom_telegram_group_id", chat_id
	)
	frappe.db.commit()

	message = _summary_message(
		"Created", chat, added, failed, privacy_warning, invite_error
	)
	return {
		"status": "created",
		"chat": chat.name,
		"chat_id": chat_id,
		"title": chat.title,
		"invite_link": chat.invite_link,
		"added": added,
		"failed": failed,
		"privacy_warning": privacy_warning,
		"invite_error": invite_error,
		"welcome_error": welcome_error,
		"message": message,
	}


def _ensure_existing_group(task, bot, account):
	chat_id = str(task.custom_telegram_group_id or "").strip()
	chat_name = frappe.db.get_value("Telegram Chat", {"chat_id": chat_id}, "name")
	if chat_name:
		chat = frappe.get_doc("Telegram Chat", chat_name)
		if chat.task and chat.task != task.name:
			frappe.throw(
				f"Telegram group {chat.chat_id} is already linked to Task {chat.task}."
			)

		changed = False
		if not chat.task:
			chat.task = task.name
			changed = True
		if not chat.bot:
			chat.bot = bot.name
			changed = True
		if changed:
			chat.save(ignore_permissions=True)
			frappe.db.commit()

		invite_error = None
		if not chat.invite_link:
			link_bot = _bot_for_chat(chat, bot)
			if not link_bot:
				invite_error = (
					f"Telegram Bot {chat.bot} is not available; cannot create an invite link."
				)
			else:
				_, invite_error = _create_invite_link(chat, link_bot)

		return {
			"status": "already_exists",
			"chat": chat.name,
			"chat_id": chat.chat_id,
			"title": chat.title,
			"invite_link": chat.invite_link,
			"added": [],
			"failed": {},
			"privacy_warning": None,
			"invite_error": invite_error,
			"message": _summary_message(
				"Existing", chat, [], {}, None, invite_error
			),
		}

	try:
		info = TelegramBotClient(bot).get_chat(chat_id)
	except Exception as exc:
		frappe.throw(
			f"Task already has Telegram group ID {chat_id}, but no Telegram Chat "
			f"record exists and the bot cannot access the group: {exc}. "
			"Clear the field to create a new group."
		)

	chat = frappe.new_doc("Telegram Chat")
	chat.chat_id = chat_id
	chat.title = info.get("title") or task.subject or task.name
	chat.chat_type = _chat_type(info.get("type"))
	chat.enabled = 1
	chat.task = task.name
	chat.bot = bot.name
	_refresh_admin_flags(chat, bot)
	chat.insert(ignore_permissions=True)

	invite_link, invite_error = _create_invite_link(chat, bot)
	welcome_error = _send_welcome(bot, chat_id, task, invite_link)
	frappe.db.commit()

	return {
		"status": "linked_existing",
		"chat": chat.name,
		"chat_id": chat.chat_id,
		"title": chat.title,
		"invite_link": chat.invite_link,
		"added": [],
		"failed": {},
		"privacy_warning": None,
		"invite_error": invite_error,
		"welcome_error": welcome_error,
		"message": _summary_message(
			"Linked", chat, [], {}, None, invite_error
		),
	}


def _group_title(task):
	subject = (task.subject or task.name).strip()
	return f"{task.name} - {subject}"[:128]


def _group_about(task):
	parts = [f"ERPNext Task {task.name}"]
	if task.project:
		parts.append(f"Project: {task.project}")
	if task.exp_end_date:
		parts.append(f"Due: {frappe.format(task.exp_end_date, {'fieldtype': 'Date'})}")
	return " | ".join(parts)[:255]


def _chat_type(telegram_type):
	return telegram_type if telegram_type in ("supergroup", "group", "channel") else "supergroup"


def _bot_for_chat(chat, fallback):
	if not chat.bot:
		return fallback
	try:
		return get_bot(chat.bot)
	except Exception:
		return fallback


def _refresh_admin_flags(chat, bot):
	if not bot.bot_id:
		return
	try:
		member = TelegramBotClient(bot).get_chat_member(chat.chat_id, bot.bot_id)
	except Exception:
		return
	status = member.get("status")
	chat.bot_is_admin = 1 if status in ("administrator", "creator") else 0
	chat.can_invite_users = (
		1 if status == "creator" or member.get("can_invite_users") else 0
	)


def _create_invite_link(chat, bot):
	try:
		client = TelegramBotClient(bot)
		result = client.create_chat_invite_link(
			chat.chat_id,
			name=f"Task {chat.task or chat.chat_id}"[:32],
			creates_join_request=True,
		)
	except Exception as exc:
		chat.last_error = str(exc)[:500]
		chat.save(ignore_permissions=True)
		frappe.db.commit()
		return None, str(exc)

	chat.invite_link = result.get("invite_link")
	chat.invite_link_expiry = None
	chat.bot_is_admin = 1
	chat.can_invite_users = 1
	chat.last_error = None
	chat.save(ignore_permissions=True)
	frappe.db.commit()
	return chat.invite_link, None


def _send_welcome(bot, chat_id, task, invite_link):
	lines = [f"Task {task.name}: {task.subject or ''}".strip()]
	if task.status:
		lines.append(f"Status: {task.status}")
	try:
		lines.append(f"ERPNext: {frappe.utils.get_url_to_form('Task', task.name)}")
	except Exception:
		pass
	if invite_link:
		lines.append(f"Invite link: {invite_link}")
	lines.append("Use /help to see the available commands.")

	try:
		TelegramBotClient(bot).send_message(
			chat_id, "\n".join(lines), disable_web_page_preview=True
		)
	except Exception as exc:
		return str(exc)
	return None


def _ensure_privacy_disabled(bot, account):
	if bot.privacy_disabled:
		return None
	try:
		mtproto.disable_bot_privacy(account, bot.username)
	except Exception as exc:
		return str(exc)
	bot.db_set("privacy_disabled", 1)
	frappe.db.commit()
	return None


def _update_add_counters(account, added, flood_wait):
	if added:
		account.adds_today = int(account.adds_today or 0) + len(added)
		account.db_set("adds_today", account.adds_today)
		account.db_set("last_add_on", now_datetime())
		frappe.db.commit()

	if flood_wait:
		seconds = int(flood_wait)
		account.db_set(
			{
				"status": "Flood Wait",
				"flood_wait_until": add_to_date(now_datetime(), seconds=seconds),
				"last_error": f"Flood wait after member add: retry after {seconds} seconds.",
			}
		)
		frappe.db.commit()


def _summary_message(action, chat, added, failed, privacy_warning, invite_error):
	parts = [f"{action} Telegram group {chat.title} ({chat.chat_id})."]
	if added:
		parts.append(f"Added {len(added)} member(s).")
	if failed:
		parts.append(f"{len(failed)} member(s) could not be added.")
	if privacy_warning:
		parts.append(f"Privacy warning: {privacy_warning}")
	if invite_error:
		parts.append(f"Invite link error: {invite_error}")
	if chat.invite_link:
		parts.append(f"Invite link: {chat.invite_link}")
	return " ".join(parts)
