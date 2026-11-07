import time
import traceback

import frappe
from frappe.utils import now_datetime

from caf_telegram_bot import router
from caf_telegram_bot.telegram_client import TelegramAPIError
from caf_telegram_bot.utils import redact

ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request", "my_chat_member"]


def _bot_token(bot):
	try:
		return bot.get_password("token") if bot else None
	except Exception:
		return None


def _redact_bot(bot, value):
	if value is None:
		return value
	return redact(value, _bot_token(bot))


def get_enabled_bots(bot_name=None):
	filters = {"enabled": 1}
	if bot_name:
		filters["name"] = bot_name
	names = frappe.get_all(
		"Telegram Bot",
		filters=filters,
		pluck="name",
		order_by="is_primary desc, name asc",
	)
	return [frappe.get_doc("Telegram Bot", name) for name in names]


def _effective_timeout(bot, bot_count):
	configured = int(bot.polling_timeout or 30)
	if bot_count <= 1:
		return max(1, configured)
	return max(1, min(configured, max(2, 30 // bot_count)))


def _mark_error(bot, message):
	bot.db_set("status", "Error")
	bot.db_set("last_error", _redact_bot(bot, (message or "")[:500]))
	frappe.db.commit()


def _sleep_for_error(exc):
	delay = getattr(exc, "retry_after", None)
	if delay:
		return min(int(delay), 300)
	if getattr(exc, "error_code", None) == 409:
		return 10
	if getattr(exc, "error_code", None) == 401:
		return 60
	return 5


def _handle_api_error(bot, exc):
	description = _redact_bot(bot, getattr(exc, "description", None) or str(exc))
	_mark_error(bot, description)
	delay = _sleep_for_error(exc)
	print(f"[telegram] {bot.name}: {description}; retrying in {delay}s", flush=True)
	time.sleep(delay)


def sync_commands(bot):
	try:
		bot.get_client().set_my_commands(router.get_command_menu())
	except TelegramAPIError as exc:
		_mark_error(bot, exc.description)
		return False
	return True


def poll_bot(bot_name, timeout=None):
	bot = frappe.get_doc("Telegram Bot", bot_name)
	client = bot.get_client()

	last_update_id = str(bot.last_update_id or "").strip()
	offset = int(last_update_id) + 1 if last_update_id.isdigit() else None
	effective_timeout = timeout or int(bot.polling_timeout or 30)

	try:
		updates = client.get_updates(
			offset=offset,
			timeout=effective_timeout,
			allowed_updates=ALLOWED_UPDATES,
		)
	except TelegramAPIError as exc:
		_handle_api_error(bot, exc)
		return 0
	except Exception as exc:
		_mark_error(bot, f"{exc.__class__.__name__}: {exc}")
		print(f"[telegram] {bot.name}: {_redact_bot(bot, exc)}", flush=True)
		time.sleep(5)
		return 0

	if not updates:
		bot.db_set("last_poll_on", now_datetime())
		bot.db_set("status", "Active")
		bot.db_set("last_error", None)
		frappe.db.commit()
		return 0

	max_update_id = offset - 1 if offset else None
	processed = 0

	for update in updates:
		raw_update_id = update.get("update_id")
		if raw_update_id is not None:
			try:
				numeric_id = int(raw_update_id)
			except (TypeError, ValueError):
				numeric_id = None
			if numeric_id is not None and (max_update_id is None or numeric_id > max_update_id):
				max_update_id = numeric_id

		try:
			router.handle_update(bot, update, client=client)
			processed += 1
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="Telegram poll failed",
				message=_redact_bot(bot, traceback.format_exc()),
			)
		finally:
			frappe.db.commit()

	if max_update_id is not None:
		bot.db_set("last_update_id", str(max_update_id))
	bot.db_set("last_poll_on", now_datetime())
	bot.db_set("status", "Active")
	bot.db_set("last_error", None)
	frappe.db.commit()
	return processed


def run(bot_name=None, once=False):
	synced = set()
	empty_printed = False

	while True:
		bots = get_enabled_bots(bot_name)
		if not bots:
			if once:
				print("No enabled Telegram bots. Create a bot first.", flush=True)
				return
			if not empty_printed:
				print("[telegram] No enabled Telegram bots; waiting...", flush=True)
				empty_printed = True
			time.sleep(10)
			continue

		empty_printed = False
		timeout = _effective_timeout(bots[0], len(bots))

		for bot in bots:
			if bot.name not in synced:
				sync_commands(bot)
				synced.add(bot.name)

			try:
				poll_bot(bot.name, timeout=timeout)
			except Exception:
				frappe.db.rollback()
				frappe.log_error(
					title="Telegram poller failed",
					message=_redact_bot(bot, traceback.format_exc()),
				)
				time.sleep(2)

		if once:
			return
		time.sleep(0.5)
