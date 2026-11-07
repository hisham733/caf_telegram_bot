import secrets
from datetime import datetime

import frappe
from frappe.utils import flt, getdate, now_datetime, nowdate

from caf_telegram_bot.telegram_client import TelegramAPIError
from caf_telegram_bot.utils import redact

LINK_CACHE_PREFIX = "telegram_link:"
LINK_TTL = 900
LINK_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_TELEGRAM_MESSAGE = 4000

ALLOWED_FIELDS = {
	"status": "Task status",
	"priority": "Priority",
	"progress": "Progress (0-100)",
	"exp_start_date": "Expected start date",
	"exp_end_date": "Expected end date",
	"custom_completion_date": "Completion date",
	"custom_remark": "Remark",
	"custom_completed_by_bot": "Completed by",
}

DATE_FIELDS = {"exp_start_date", "exp_end_date", "custom_completion_date"}
FALLBACK_STATUSES = [
	"Open",
	"Working",
	"Pending Review",
	"Pending Investigation",
	"Pending Supplier",
	"Overdue",
	"Monitoring",
	"Pending Close",
	"Duplicate",
	"Completed",
	"Cancelled",
]
FALLBACK_PRIORITIES = ["Low", "Medium", "High", "Urgent"]
QUICK_STATUSES = ["Working", "Pending Review", "Completed", "Cancelled"]

COMMAND_MENU = [
	{"command": "status", "description": "Show linked task status"},
	{"command": "task", "description": "Show task details"},
	{"command": "linktask", "description": "Link this chat to a Task"},
	{"command": "set", "description": "Update a task field"},
	{"command": "done", "description": "Mark the linked task completed"},
	{"command": "link", "description": "Link your ERPNext account"},
	{"command": "members", "description": "Show group members"},
	{"command": "help", "description": "Show commands"},
]

HELP_TEXT = """CAF Task bot commands:

/status [task] - show status and quick buttons
/task [task] - show task details
/linktask <task> - link this chat to a Task
/set <field> <value> - update a task field
/done - mark the linked task Completed
/priority <value> - set priority
/progress <0-100> - set progress
/startdate <YYYY-MM-DD> - set expected start date
/due <YYYY-MM-DD> - set expected end date
/completed <YYYY-MM-DD> - set completion date
/remark <text> - set remark
/doneby <name> - set completed by
/link <code> - link your ERPNext account
/members - list group members
/help - show this message

Editable fields: {0}""".format(", ".join(ALLOWED_FIELDS))


class CommandError(Exception):
	pass


def generate_link_code(erpnext_user):
	code = "".join(secrets.choice(LINK_ALPHABET) for _ in range(8))
	frappe.cache.set_value(f"{LINK_CACHE_PREFIX}{code}", erpnext_user, expires_in_sec=LINK_TTL)
	return code


def consume_link_code(code):
	code = (code or "").strip().upper()
	if not code:
		return None
	key = f"{LINK_CACHE_PREFIX}{code}"
	user = frappe.cache.get_value(key)
	if user:
		frappe.cache.delete_value(key)
	return user


def get_command_menu():
	return COMMAND_MENU


def _clip(value, limit=MAX_TELEGRAM_MESSAGE):
	value = "" if value is None else str(value)
	if len(value) > limit:
		return value[: limit - 3] + "..."
	return value


def _bot_token(bot):
	try:
		return bot.get_password("token") if bot else None
	except Exception:
		return None


def _redact_bot(bot, value):
	if value is None:
		return value
	return redact(value, _bot_token(bot))


def _display_name(user):
	if not user:
		return "Unknown"
	name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part).strip()
	if name:
		return name
	if user.get("username"):
		return f"@{user['username']}"
	return str(user.get("id") or user.get("telegram_id") or "Unknown")


def _options(fieldname, fallback=None):
	field = frappe.get_meta("Task").get_field(fieldname)
	options = [option.strip() for option in (field.options or "").splitlines() if option.strip()]
	return options or list(fallback or [])


def _match_option(fieldname, raw_value, fallback=None):
	raw_value = (raw_value or "").strip()
	for option in _options(fieldname, fallback):
		if option.lower() == raw_value.lower():
			return option
	raise CommandError(
		"Invalid {0}: {1}. Allowed: {2}".format(
			fieldname, raw_value or "(empty)", ", ".join(_options(fieldname, fallback))
		)
	)


def _parse_date(raw_value):
	raw_value = (raw_value or "").strip()
	lower = raw_value.lower()
	if lower in ("today", "now"):
		return nowdate()
	if lower == "tomorrow":
		return frappe.utils.add_days(nowdate(), 1)
	try:
		return getdate(raw_value)
	except Exception as exc:
		raise CommandError("Invalid date. Use YYYY-MM-DD, today or tomorrow.") from exc


def _upsert_user(from_user):
	if not from_user or from_user.get("is_bot"):
		return None
	telegram_id = str(from_user.get("id") or "")
	if not telegram_id:
		return None

	if frappe.db.exists("Telegram User", telegram_id):
		doc = frappe.get_doc("Telegram User", telegram_id)
	else:
		doc = frappe.new_doc("Telegram User")
		doc.telegram_id = telegram_id
		doc.enabled = 1

	doc.username = from_user.get("username")
	doc.first_name = from_user.get("first_name")
	doc.last_name = from_user.get("last_name")
	doc.last_seen_on = now_datetime()
	doc.save(ignore_permissions=True)
	return doc


def _upsert_chat(chat, from_user, bot):
	if not chat or not chat.get("id"):
		return None
	chat_id = str(chat["id"])
	title = chat.get("title") or _display_name(from_user)
	chat_type = chat.get("type") or "group"

	if frappe.db.exists("Telegram Chat", chat_id):
		doc = frappe.get_doc("Telegram Chat", chat_id)
		changed = False
		if title and doc.title != title:
			doc.title = title
			changed = True
		if chat_type and doc.chat_type != chat_type:
			doc.chat_type = chat_type
			changed = True
		if not doc.bot and bot:
			doc.bot = bot.name
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return doc

	doc = frappe.new_doc("Telegram Chat")
	doc.chat_id = chat_id
	doc.title = title
	doc.chat_type = chat_type
	doc.enabled = 1
	doc.bot = bot.name if bot else None
	doc.save(ignore_permissions=True)
	return doc


def _get_chat(chat_id):
	if not chat_id or not frappe.db.exists("Telegram Chat", str(chat_id)):
		return None
	return frappe.get_doc("Telegram Chat", str(chat_id))


def _get_chat_task(chat_doc):
	if chat_doc and chat_doc.task and frappe.db.exists("Task", chat_doc.task):
		return frappe.get_doc("Task", chat_doc.task)
	return None


def _resolve_task(chat_doc, args):
	if args and not args[0].startswith("-"):
		name = args[0].strip()
		if frappe.db.exists("Task", name):
			return frappe.get_doc("Task", name)
		return None
	return _get_chat_task(chat_doc)


def _is_group_chat(chat_doc):
	return bool(chat_doc and chat_doc.chat_type in ("group", "supergroup"))


def _task_text(task, details=False):
	lines = [
		f"Task: {task.name}",
		f"Subject: {task.subject or '-'}",
		f"Status: {task.status or '-'}",
		f"Priority: {task.priority or '-'}",
		f"Progress: {int(flt(task.progress or 0))}%",
		f"Start: {task.exp_start_date or '-'}",
		f"Due: {task.exp_end_date or '-'}",
	]
	if task.get("custom_remark"):
		lines.append(f"Remark: {task.custom_remark}")
	if task.get("custom_completion_date"):
		lines.append(f"Completion date: {task.custom_completion_date}")
	if task.get("custom_completed_by_bot"):
		lines.append(f"Completed by: {task.custom_completed_by_bot}")
	if details and task.get("description"):
		lines.append("")
		lines.append(_clip(task.description, 1500))
	return _clip("\n".join(lines))


def _task_keyboard(task):
	available = set(_options("status", FALLBACK_STATUSES))
	quick = [status for status in QUICK_STATUSES if status in available]
	rows = []
	for index in range(0, len(quick), 2):
		rows.append(
			[
				{"text": status, "callback_data": f"set:status:{status}"}
				for status in quick[index : index + 2]
			]
		)
	rows.append(
		[
			{"text": "25%", "callback_data": "set:progress:25"},
			{"text": "50%", "callback_data": "set:progress:50"},
			{"text": "75%", "callback_data": "set:progress:75"},
			{"text": "100%", "callback_data": "set:progress:100"},
		]
	)
	rows.append([{"text": "Refresh", "callback_data": "refresh"}])
	return {"inline_keyboard": rows}


def _reply(client, chat_id, text, reply_markup=None):
	return client.send_message(chat_id, _clip(text), reply_markup=reply_markup)


def _sender_can_write(task, tuser, group_chat):
	if group_chat:
		return True
	if not tuser or not tuser.verified or not tuser.erpnext_user:
		return False
	return frappe.has_permission("Task", "write", doc=task, user=tuser.erpnext_user)


def _apply_field_change(task, field, raw_value, actor, group_chat, tuser):
	if field not in ALLOWED_FIELDS:
		raise CommandError(
			"Field {0} is not editable. Allowed: {1}".format(field, ", ".join(ALLOWED_FIELDS))
		)

	if not _sender_can_write(task, tuser, group_chat):
		if group_chat:
			raise CommandError("You do not have permission to update this Task.")
		raise CommandError("Link your ERPNext account first with /link CODE, or check Task permissions.")

	raw_value = (raw_value or "").strip()
	if not raw_value:
		raise CommandError("A value is required.")

	if field == "status":
		value = _match_option("status", raw_value, FALLBACK_STATUSES)
		task.status = value
		if value == "Completed":
			task.progress = 100
			if not task.get("completed_on"):
				task.completed_on = nowdate()
			if not task.get("custom_completion_date"):
				task.custom_completion_date = nowdate()
			if not task.get("custom_completed_by_bot"):
				task.custom_completed_by_bot = actor
	elif field == "priority":
		task.priority = _match_option("priority", raw_value, FALLBACK_PRIORITIES)
	elif field == "progress":
		try:
			value = float(raw_value)
		except ValueError as exc:
			raise CommandError("Progress must be a number between 0 and 100.") from exc
		if value < 0 or value > 100:
			raise CommandError("Progress must be between 0 and 100.")
		task.progress = value
	elif field in DATE_FIELDS:
		if raw_value.lower() in ("-", "clear", "none"):
			task.set(field, None)
		else:
			task.set(field, _parse_date(raw_value))
	else:
		task.set(field, raw_value)

	try:
		task.save(ignore_permissions=True)
	except frappe.ValidationError as exc:
		raise CommandError(str(exc)) from exc
	return task


def _handle_link(client, chat_doc, tuser, args):
	if not tuser:
		_reply(client, chat_doc.chat_id, "Could not identify your Telegram account.")
		return "no telegram user"

	if not args:
		if tuser.verified and tuser.erpnext_user:
			_reply(client, chat_doc.chat_id, f"Already linked to {tuser.erpnext_user}.")
		else:
			_reply(
				client,
				chat_doc.chat_id,
				"Generate a link code in ERPNext: Telegram User > Generate Link Code, "
				"then send /link CODE.",
			)
		return "link instructions"

	erpnext_user = consume_link_code(args[0])
	if not erpnext_user:
		_reply(client, chat_doc.chat_id, "That link code is invalid or expired. Generate a new one in ERPNext.")
		return "invalid code"

	if not frappe.db.exists("User", erpnext_user) or not frappe.db.get_value("User", erpnext_user, "enabled"):
		_reply(client, chat_doc.chat_id, "That ERPNext user is missing or disabled.")
		return "disabled user"

	tuser.erpnext_user = erpnext_user
	tuser.verified = 1
	tuser.linked_on = now_datetime()
	tuser.save(ignore_permissions=True)
	_reply(client, chat_doc.chat_id, f"Linked to ERPNext user {erpnext_user}.")
	return f"linked {erpnext_user}"


def _handle_linktask(client, chat_doc, tuser, args, group_chat):
	if not args:
		_reply(client, chat_doc.chat_id, "Usage: /linktask <task>")
		return "no task"

	task_name = args[0].strip()
	if not frappe.db.exists("Task", task_name):
		_reply(client, chat_doc.chat_id, f"Task not found: {task_name}")
		return "task not found"

	task = frappe.get_doc("Task", task_name)
	if not tuser or not tuser.verified or not tuser.erpnext_user:
		_reply(client, chat_doc.chat_id, "Link your ERPNext account first with /link CODE.")
		return "not linked"

	if not frappe.has_permission("Task", "write", doc=task, user=tuser.erpnext_user):
		_reply(client, chat_doc.chat_id, "You do not have permission to link this Task.")
		return "no permission"

	chat_doc.task = task.name
	chat_doc.save(ignore_permissions=True)

	if group_chat:
		task.custom_telegram_group_id = chat_doc.chat_id
		task.save(ignore_permissions=True)

	_reply(client, chat_doc.chat_id, f"Linked this chat to Task {task.name}: {task.subject or ''}")
	return f"linked task {task.name}"


def _handle_status(client, chat_doc, args, details=False):
	task = _resolve_task(chat_doc, args)
	if not task:
		if args:
			_reply(client, chat_doc.chat_id, f"Task not found: {args[0]}")
		else:
			_reply(
				client,
				chat_doc.chat_id,
				"No Task is linked to this chat. Use /linktask <task>.",
			)
		return "no task"

	_reply(
		client,
		chat_doc.chat_id,
		_task_text(task, details=details),
		reply_markup=_task_keyboard(task),
	)
	return f"showed {task.name}"


def _handle_members(client, chat_doc, group_chat):
	if not group_chat:
		_reply(client, chat_doc.chat_id, "Members are only available in group chats.")
		return "not a group"

	try:
		count = client.get_chat_members_count(chat_doc.chat_id)
		admins = client.get_chat_administrators(chat_doc.chat_id)
	except TelegramAPIError as exc:
		_reply(client, chat_doc.chat_id, f"Could not read members: {exc.description}")
		return "member error"

	lines = [f"Members: {count}", "", "Admins:"]
	for member in admins[:20]:
		user = member.get("user") or {}
		role = member.get("status") or "administrator"
		lines.append(f"- {_display_name(user)} ({role})")
	if len(admins) > 20:
		lines.append(f"... and {len(admins) - 20} more")
	_reply(client, chat_doc.chat_id, "\n".join(lines))
	return f"members {count}"


def _handle_set(client, chat_doc, tuser, args, group_chat):
	if len(args) < 2:
		_reply(
			client,
			chat_doc.chat_id,
			"Usage: /set <field> <value>\nFields: " + ", ".join(ALLOWED_FIELDS),
		)
		return "usage"

	field = args[0].strip().lower()
	raw_value = " ".join(args[1:]).strip()
	task = _get_chat_task(chat_doc)
	if not task:
		_reply(client, chat_doc.chat_id, "No Task is linked to this chat. Use /linktask <task>.")
		return "no task"

	actor = _display_name(tuser.as_dict() if tuser else {})
	try:
		task = _apply_field_change(task, field, raw_value, actor, group_chat, tuser)
	except CommandError as exc:
		_reply(client, chat_doc.chat_id, str(exc))
		return f"error: {exc}"

	_reply(client, chat_doc.chat_id, _task_text(task), reply_markup=_task_keyboard(task))
	return f"set {field} on {task.name}"


def _handle_callback(bot, callback, client):
	callback_id = callback.get("id")
	message = callback.get("message") or {}
	chat = message.get("chat") or {}
	from_user = callback.get("from") or {}
	data = callback.get("data") or ""

	if from_user.get("is_bot") or not chat.get("id"):
		if callback_id:
			client.answer_callback_query(callback_id, text="Unsupported action.", show_alert=True)
		return "ignored"

	chat_doc = _upsert_chat(chat, from_user, bot)
	tuser = _upsert_user(from_user)
	group_chat = chat.get("type") in ("group", "supergroup")

	if tuser and not tuser.enabled:
		if callback_id:
			client.answer_callback_query(callback_id, text="Your account is disabled.", show_alert=True)
		return "user disabled"

	if not chat_doc:
		if callback_id:
			client.answer_callback_query(callback_id, text="Chat not registered.", show_alert=True)
		return "no chat"

	task = _get_chat_task(chat_doc)
	if not task:
		if callback_id:
			client.answer_callback_query(
				callback_id, text="No Task is linked to this chat. Use /linktask.", show_alert=True
			)
		return "no task"

	if data == "refresh":
		if callback_id:
			client.answer_callback_query(callback_id, text="Refreshed")
		try:
			client.edit_message_text(
				chat_doc.chat_id,
				message.get("message_id"),
				_task_text(task),
				reply_markup=_task_keyboard(task),
			)
		except TelegramAPIError:
			pass
		return "refreshed"

	if not data.startswith("set:"):
		if callback_id:
			client.answer_callback_query(callback_id, text="Unknown action.", show_alert=True)
		return "unknown action"

	parts = data.split(":", 2)
	if len(parts) != 3:
		if callback_id:
			client.answer_callback_query(callback_id, text="Invalid action.", show_alert=True)
		return "invalid action"

	_, field, raw_value = parts
	actor = _display_name(from_user)
	try:
		task = _apply_field_change(task, field, raw_value, actor, group_chat, tuser)
	except CommandError as exc:
		if callback_id:
			client.answer_callback_query(callback_id, text=str(exc), show_alert=True)
		return f"error: {exc}"

	try:
		client.edit_message_text(
			chat_doc.chat_id,
			message.get("message_id"),
			_task_text(task),
			reply_markup=_task_keyboard(task),
		)
	except TelegramAPIError:
		pass

	if callback_id:
		client.answer_callback_query(callback_id, text=f"Set {field} to {raw_value}")
	return f"callback {field} on {task.name}"


def _handle_join_request(bot, join_request, client):
	chat = join_request.get("chat") or {}
	user = join_request.get("from") or {}
	chat_id = str(chat.get("id") or "")
	user_id = user.get("id")

	if not chat_id or not user_id:
		return "ignored"

	chat_doc = _get_chat(chat_id)
	if not chat_doc or not chat_doc.enabled:
		try:
			client.decline_chat_join_request(chat_id, user_id)
		except TelegramAPIError:
			pass
		return "unknown chat"

	invite = (join_request.get("invite_link") or {}).get("invite_link")
	if not chat_doc.invite_link or invite != chat_doc.invite_link:
		try:
			client.decline_chat_join_request(chat_id, user_id)
		except TelegramAPIError:
			pass
		return "declined invite mismatch"

	try:
		client.approve_chat_join_request(chat_id, user_id)
	except TelegramAPIError as exc:
		chat_doc.db_set("last_error", exc.description)
		frappe.db.commit()
		return f"approve failed: {exc.description}"

	_upsert_user(user)
	return f"approved {user_id}"


def _handle_my_chat_member(bot, change, client):
	chat = change.get("chat") or {}
	new_member = change.get("new_chat_member") or {}
	chat_id = str(chat.get("id") or "")
	if not chat_id:
		return "ignored"

	status = new_member.get("status")
	chat_doc = _get_chat(chat_id)
	if not chat_doc:
		return f"unknown chat {chat_id}"

	chat_doc.bot_is_admin = 1 if status in ("administrator", "creator") else 0
	chat_doc.can_invite_users = 1 if new_member.get("can_invite_users") else 0
	if chat.get("title"):
		chat_doc.title = chat["title"]
	chat_doc.save(ignore_permissions=True)
	return f"bot status {status}"


def _parse_command(text, bot_username):
	if not text or not text.startswith("/"):
		return None, []
	parts = text.split()
	head = parts[0][1:]
	target = None
	if "@" in head:
		head, _, target = head.partition("@")
	if target and bot_username and target.lower() != bot_username.lower():
		return None, []
	return head.lower(), parts[1:]


def _handle_message(bot, message, client):
	chat = message.get("chat") or {}
	from_user = message.get("from") or {}
	chat_id = str(chat.get("id") or "")
	text = (message.get("text") or "").strip()

	if not chat_id:
		return None, "no chat"
	if from_user.get("is_bot"):
		return None, "ignored bot"

	tuser = _upsert_user(from_user)
	chat_doc = _upsert_chat(chat, from_user, bot)
	group_chat = chat.get("type") in ("group", "supergroup")

	if tuser and not tuser.enabled:
		_reply(client, chat_id, "Your Telegram account is disabled in ERPNext.")
		return None, "user disabled"

	command, args = _parse_command(text, bot.username)
	if not command:
		return None, "ignored text"

	if command == "start":
		if group_chat:
			task = _get_chat_task(chat_doc)
			if task:
				_reply(client, chat_id, _task_text(task), reply_markup=_task_keyboard(task))
			else:
				_reply(
					client,
					chat_id,
					"CAF Task bot.\n\nThis group is not linked to a Task. "
					"A linked user can run /linktask <task>.\n\nUse /help for commands.",
				)
		else:
			_reply(
				client,
				chat_id,
				"CAF Task bot.\n\nGenerate a link code in ERPNext: Telegram User > "
				"Generate Link Code, then send /link CODE.\n\nUse /help for commands.",
			)
		return "start", "sent start"

	if command == "help":
		_reply(client, chat_id, HELP_TEXT)
		return "help", "sent help"

	if command == "link":
		return "link", _handle_link(client, chat_doc, tuser, args)

	if command == "linktask":
		return "linktask", _handle_linktask(client, chat_doc, tuser, args, group_chat)

	if command == "status":
		return "status", _handle_status(client, chat_doc, args)

	if command == "task":
		return "task", _handle_status(client, chat_doc, args, details=True)

	if command == "members":
		return "members", _handle_members(client, chat_doc, group_chat)

	if command == "set":
		return "set", _handle_set(client, chat_doc, tuser, args, group_chat)

	if command == "done":
		task = _get_chat_task(chat_doc)
		if not task:
			_reply(client, chat_id, "No Task is linked to this chat. Use /linktask <task>.")
			return "done", "no task"
		actor = _display_name(from_user)
		try:
			task = _apply_field_change(task, "status", "Completed", actor, group_chat, tuser)
		except CommandError as exc:
			_reply(client, chat_id, str(exc))
			return "done", f"error: {exc}"
		_reply(client, chat_id, _task_text(task), reply_markup=_task_keyboard(task))
		return "done", f"completed {task.name}"

	if command == "cancel":
		task = _get_chat_task(chat_doc)
		if not task:
			_reply(client, chat_id, "No Task is linked to this chat. Use /linktask <task>.")
			return "cancel", "no task"
		actor = _display_name(from_user)
		try:
			task = _apply_field_change(task, "status", "Cancelled", actor, group_chat, tuser)
		except CommandError as exc:
			_reply(client, chat_id, str(exc))
			return "cancel", f"error: {exc}"
		_reply(client, chat_id, _task_text(task), reply_markup=_task_keyboard(task))
		return "cancel", f"cancelled {task.name}"

	aliases = {
		"priority": "priority",
		"progress": "progress",
		"startdate": "exp_start_date",
		"due": "exp_end_date",
		"completed": "custom_completion_date",
		"remark": "custom_remark",
		"doneby": "custom_completed_by_bot",
	}
	if command in aliases:
		if not args:
			_reply(client, chat_id, f"Usage: /{command} <value>")
			return command, "usage"
		return command, _handle_set(
			client, chat_doc, tuser, [aliases[command], " ".join(args)], group_chat
		)

	_reply(client, chat_id, "Unknown command. Use /help.")
	return command, "unknown command"


def _dispatch(bot, update, client):
	if "message" in update:
		return _handle_message(bot, update["message"], client)
	if "callback_query" in update:
		return "callback", _handle_callback(bot, update["callback_query"], client)
	if "chat_join_request" in update:
		return "chat_join_request", _handle_join_request(bot, update["chat_join_request"], client)
	if "my_chat_member" in update:
		return "my_chat_member", _handle_my_chat_member(bot, update["my_chat_member"], client)
	return None, "ignored"


def _update_key(bot, update):
	raw_id = update.get("update_id")
	if raw_id is None:
		return None
	return f"{(bot.name or '')[:100]}:{raw_id}"


def _log(bot, update, status, command=None, result=None, error=None):
	key = _update_key(bot, update)
	if not key:
		return None
	if frappe.db.exists("Telegram Message Log", {"update_id": key}):
		return None

	message = update.get("message") or {}
	callback = update.get("callback_query") or {}
	join_request = update.get("chat_join_request") or {}
	change = update.get("my_chat_member") or {}
	source = message or callback.get("message") or join_request.get("chat") or change.get("chat") or {}
	chat = source.get("chat") if isinstance(source, dict) and source.get("chat") else source
	chat = chat or {}
	from_user = (
		message.get("from")
		or callback.get("from")
		or join_request.get("from")
		or (change.get("from") or {})
	)

	if message:
		message_type = "Command" if command else "Text"
	elif callback:
		message_type = "Callback"
	else:
		message_type = "System"

	event_time = None
	if message.get("date"):
		try:
			event_time = datetime.fromtimestamp(message["date"])
		except (ValueError, OSError):
			event_time = None

	telegram_user = None
	if from_user.get("id") and frappe.db.exists("Telegram User", str(from_user["id"])):
		telegram_user = str(from_user["id"])

	token = _bot_token(bot)
	text = (
		message.get("text")
		or message.get("caption")
		or callback.get("data")
		or join_request.get("invite_link", {}).get("invite_link")
		or ""
	)

	doc = frappe.get_doc(
		{
			"doctype": "Telegram Message Log",
			"update_id": key,
			"direction": "Inbound",
			"message_type": message_type,
			"status": status,
			"event_time": event_time or now_datetime(),
			"bot": bot.name,
			"chat_id": str(chat.get("id") or ""),
			"chat_title": chat.get("title") or "",
			"sender_id": str(from_user.get("id") or ""),
			"sender_username": from_user.get("username") or "",
			"sender_name": _display_name(from_user),
			"telegram_user": telegram_user,
			"command": command or "",
			"text": _clip(redact(text, token), 20000),
			"result": _clip(redact(result or "", token), 20000),
			"error": _clip(redact(error or "", token), 20000),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _safe_log(bot, update, status, command=None, result=None, error=None):
	try:
		return _log(bot, update, status, command=command, result=result, error=error)
	except Exception:
		frappe.log_error(
			title="Telegram log failed",
			message=_redact_bot(bot, frappe.get_traceback()),
		)
		return None


def handle_update(bot, update, client=None):
	key = _update_key(bot, update)
	if key and frappe.db.exists("Telegram Message Log", {"update_id": key}):
		return {"status": "duplicate"}

	command = None
	try:
		client = client or bot.get_client()
		command, result = _dispatch(bot, update, client)
		_safe_log(bot, update, "Success", command=command, result=result)
		return {"status": "success", "command": command, "result": result}
	except CommandError as exc:
		error = _redact_bot(bot, str(exc))
		_safe_log(bot, update, "Error", command=command, error=error)
		return {"status": "error", "command": command, "error": error}
	except Exception as exc:
		frappe.log_error(
			title="Telegram update failed",
			message=_redact_bot(bot, frappe.get_traceback()),
		)
		error = _redact_bot(bot, f"{exc.__class__.__name__}: {exc}")
		_safe_log(bot, update, "Error", command=command, error=error)
		return {"status": "error", "command": command, "error": error}
