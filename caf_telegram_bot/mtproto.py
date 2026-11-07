import asyncio
import re
import threading

import frappe

TOKEN_PATTERN = re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{30,50})\b")
LOGIN_CACHE_PREFIX = "telegram_login:"
LOGIN_CACHE_TTL = 600


class MTProtoError(Exception):
	pass


def _run(coro):
	"""Run a coroutine from Frappe's synchronous context."""
	try:
		asyncio.get_running_loop()
	except RuntimeError:
		return asyncio.run(coro)

	result = {}

	def runner():
		try:
			result["value"] = asyncio.run(coro)
		except BaseException as exc:
			result["error"] = exc

	thread = threading.Thread(target=runner, daemon=True)
	thread.start()
	thread.join()
	if "error" in result:
		raise result["error"]
	return result.get("value")


def _client(account, session=None, require_session=True):
	from telethon import TelegramClient
	from telethon.sessions import StringSession

	credentials = account.get_api_credentials()
	if not credentials.get("api_hash"):
		raise MTProtoError("API hash is missing on the adder account.")
	if session is None:
		session = credentials.get("session") or ""
	if require_session and not session:
		raise MTProtoError(
			"Adder account is not logged in. Use Send Login OTP in the form or run "
			f"`bench telegram login --account {account.name}`."
		)
	return TelegramClient(StringSession(session), credentials["api_id"], credentials["api_hash"])


def _cache_key(account):
	return f"{LOGIN_CACHE_PREFIX}{account.name}"


def _format_error(exc):
	from telethon.errors import FloodWaitError

	if isinstance(exc, FloodWaitError):
		return f"Telegram flood wait: retry after {exc.seconds} seconds."
	return str(exc) or exc.__class__.__name__


async def _start_login(account):
	client = _client(
		account,
		session=account.get_password("session", raise_exception=False) or "",
		require_session=False,
	)
	await client.connect()
	try:
		if await client.is_user_authorized():
			return {"status": "already_logged_in", "session": client.session.save()}

		credentials = account.get_api_credentials()
		if not credentials.get("phone"):
			raise MTProtoError("Phone number is missing on the adder account.")

		sent = await client.send_code_request(credentials["phone"])
		frappe.cache.set_value(
			_cache_key(account),
			{
				"session": client.session.save(),
				"phone_code_hash": sent.phone_code_hash,
			},
			expires_in_sec=LOGIN_CACHE_TTL,
		)
		return {"status": "otp_sent"}
	finally:
		await client.disconnect()


async def _finish_login(account, code=None, password=None):
	from telethon.errors import SessionPasswordNeededError

	cached = frappe.cache.get_value(_cache_key(account))
	if not cached:
		raise MTProtoError("No pending login. Click Send Login OTP first.")

	client = _client(account, session=cached["session"])
	await client.connect()
	try:
		if code:
			try:
				await client.sign_in(
					phone=account.phone,
					code=code,
					phone_code_hash=cached["phone_code_hash"],
				)
			except SessionPasswordNeededError:
				frappe.cache.set_value(
					_cache_key(account),
					{"session": client.session.save(), "phone_code_hash": cached["phone_code_hash"]},
					expires_in_sec=LOGIN_CACHE_TTL,
				)
				return {"status": "password_required"}
		elif password:
			await client.sign_in(password=password)
		else:
			raise MTProtoError("Provide the OTP code or the 2FA password.")

		session = client.session.save()
		return {"status": "logged_in", "session": session}
	finally:
		await client.disconnect()


async def _check_login(account):
	client = _client(account)
	await client.connect()
	try:
		if not await client.is_user_authorized():
			return {"status": "not_logged_in"}
		me = await client.get_me()
		return {
			"status": "logged_in",
			"telegram_id": str(me.id),
			"username": me.username,
			"name": " ".join(part for part in [me.first_name, me.last_name] if part),
		}
	finally:
		await client.disconnect()


async def _create_bot(account, display_name, username):
	if not display_name or not str(display_name).strip():
		raise MTProtoError("Bot display name is required.")
	username = (username or "").strip().lstrip("@")
	if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,28}[Bb][Oo][Tt]", username):
		raise MTProtoError(
			"Bot username must be 5-32 characters, start with a letter, contain only letters, "
			"digits or underscores, and end with 'bot'."
		)

	client = _client(account)
	await client.connect()
	try:
		if not await client.is_user_authorized():
			raise MTProtoError("Adder account is not logged in.")

		async with client.conversation("BotFather", timeout=45) as conversation:
			await conversation.send_message("/newbot")
			await conversation.get_response()

			await conversation.send_message(str(display_name).strip())
			response = await conversation.get_response()
			text = response.message or ""
			if text and "choose a username" not in text.lower() and "username" not in text.lower():
				if "sorry" in text.lower() or "invalid" in text.lower():
					raise MTProtoError(f"BotFather rejected the name: {text[:300]}")

			await conversation.send_message(username)
			response = await conversation.get_response()
			text = response.message or ""

			match = TOKEN_PATTERN.search(text)
			if not match:
				lower = text.lower()
				if "already taken" in lower or "sorry" in lower or "invalid" in lower:
					raise MTProtoError(f"BotFather rejected the username: {text[:300]}")
				raise MTProtoError(f"BotFather did not return a token: {text[:300]}")

			return {"token": match.group(1), "username": username, "display_name": display_name}
	finally:
		await client.disconnect()


def _find_button(message, needle):
	needle = (needle or "").strip().lstrip("@").lower()
	if not needle:
		return None
	buttons = [button for row in (getattr(message, "buttons", None) or []) for button in row]
	for button in buttons:
		text = (getattr(button, "text", "") or "").strip()
		if text.lstrip("@").lower() == needle:
			return button
	for button in buttons:
		if needle in (getattr(button, "text", "") or "").lower():
			return button
	return None


async def _disable_bot_privacy(account, bot_username):
	client = _client(account)
	await client.connect()
	try:
		if not await client.is_user_authorized():
			raise MTProtoError("Adder account is not logged in.")
		bot_username = (bot_username or "").strip().lstrip("@").lower()
		if not bot_username:
			raise MTProtoError("Bot username is missing.")

		async with client.conversation("BotFather", timeout=45) as conversation:
			await conversation.send_message("/setprivacy")
			response = await conversation.get_response()
			button = _find_button(response, bot_username)
			if not button:
				raise MTProtoError("BotFather did not list this bot under /setprivacy.")
			await button.click()
			response = await conversation.get_response()
			text = response.text or ""
			if "current status is: disabled" in text.lower():
				return {"status": "already_disabled", "message": text}

			disable_button = _find_button(response, "disable")
			if not disable_button:
				raise MTProtoError(f"Could not find the Disable button: {text[:250]}")
			await disable_button.click()
			final = await conversation.get_response()
			final_text = final.text or ""
			if "disabled" in final_text.lower() or "success" in final_text.lower():
				return {"status": "disabled", "message": final_text}
			raise MTProtoError(f"Unexpected BotFather response: {final_text[:250]}")
	finally:
		await client.disconnect()


async def _invite_members(client, channel_input, members, exclude_ids=None, exclude_usernames=None, add_limit=None):
	from telethon import functions, utils
	from telethon.errors import (
		FloodWaitError,
		PeerFloodError,
		UserAlreadyParticipantError,
		UserChannelsTooMuchError,
		UserIsBotError,
		UserNotMutualContactError,
		UserPrivacyRestrictedError,
	)

	exclude_ids = set(exclude_ids or [])
	exclude_usernames = {
		(username or "").lstrip("@").lower()
		for username in (exclude_usernames or [])
		if username
	}
	candidates = []
	for member in members or []:
		username = (member.get("username") or "").strip().lstrip("@")
		telegram_id = str(member.get("telegram_id") or "").strip()
		if username and username.lower() in exclude_usernames:
			continue
		if telegram_id:
			try:
				if int(telegram_id) in exclude_ids:
					continue
			except ValueError:
				telegram_id = ""
		if not username and not telegram_id:
			continue
		candidates.append(
			{"label": username or telegram_id, "target": username or int(telegram_id)}
		)

	limit = len(candidates) if add_limit is None else max(0, int(add_limit))
	added = []
	failed = {}
	flood_wait = None

	for index, candidate in enumerate(candidates):
		if index >= limit:
			failed[candidate["label"]] = "Skipped due to the daily add limit."
			continue
		try:
			entity = await client.get_input_entity(candidate["target"])
			user = utils.get_input_user(entity)
			await client(functions.channels.InviteToChannelRequest(channel_input, [user]))
			added.append(candidate["label"])
			await asyncio.sleep(2)
		except UserAlreadyParticipantError:
			added.append(candidate["label"])
		except FloodWaitError as exc:
			flood_wait = exc.seconds
			failed[candidate["label"]] = f"Flood wait: retry after {exc.seconds} seconds."
			for remaining in candidates[index + 1 :]:
				failed[remaining["label"]] = "Skipped after Telegram flood wait."
			break
		except PeerFloodError:
			flood_wait = 3600
			failed[candidate["label"]] = "Too many add attempts; retry later."
			for remaining in candidates[index + 1 :]:
				failed[remaining["label"]] = "Skipped after Telegram flood wait."
			break
		except (
			UserPrivacyRestrictedError,
			UserNotMutualContactError,
			UserChannelsTooMuchError,
			UserIsBotError,
		) as exc:
			failed[candidate["label"]] = _format_error(exc)
		except Exception as exc:
			failed[candidate["label"]] = _format_error(exc)

	return {"added": added, "failed": failed, "flood_wait": flood_wait}


async def _create_group(account, title, about, bot_username, members=None, add_limit=None):
	from telethon import functions, utils
	from telethon.errors import UserAlreadyParticipantError
	from telethon.tl.types import ChatAdminRights

	client = _client(account)
	await client.connect()
	try:
		if not await client.is_user_authorized():
			raise MTProtoError("Adder account is not logged in.")
		bot_username = (bot_username or "").strip().lstrip("@")
		if not bot_username:
			raise MTProtoError("Bot username is missing.")

		me = await client.get_me()
		created = await client(
			functions.channels.CreateChannelRequest(
				title=(title or "Task")[:128],
				about=(about or "")[:255],
				megagroup=True,
			)
		)
		channel = created.chats[0]
		channel_input = await client.get_input_entity(channel)

		try:
			bot_input = utils.get_input_user(await client.get_input_entity(bot_username))
			await client(functions.channels.InviteToChannelRequest(channel_input, [bot_input]))
		except UserAlreadyParticipantError:
			pass
		except Exception as exc:
			try:
				await client(functions.channels.DeleteChannelRequest(channel_input))
			except Exception:
				pass
			raise MTProtoError(
				f"Could not add the bot to the new group: {_format_error(exc)}"
			) from exc

		await asyncio.sleep(1)
		try:
			rights = ChatAdminRights(
				invite_users=True,
				delete_messages=True,
				ban_users=True,
				pin_messages=True,
				manage_call=True,
			)
			await client(
				functions.channels.EditAdminRequest(
					channel_input, bot_input, rights, rank="Task Bot"
				)
			)
		except Exception as exc:
			try:
				await client(functions.channels.DeleteChannelRequest(channel_input))
			except Exception:
				pass
			raise MTProtoError(
				f"Could not promote the bot to admin: {_format_error(exc)}"
			) from exc

		member_result = await _invite_members(
			client,
			channel_input,
			members or [],
			exclude_ids={me.id},
			exclude_usernames={me.username} if me.username else set(),
			add_limit=add_limit,
		)
		return {
			"chat_id": str(utils.get_peer_id(channel)),
			"title": channel.title,
			"added": member_result["added"],
			"failed": member_result["failed"],
			"flood_wait": member_result["flood_wait"],
		}
	finally:
		await client.disconnect()


def _save_login(account, session):
	account.session = session
	account.status = "Active"
	account.last_login_on = frappe.utils.now_datetime()
	account.last_error = None
	account.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.cache.delete_value(_cache_key(account))


def start_login(account):
	try:
		result = _run(_start_login(account))
	except MTProtoError:
		raise
	except Exception as exc:
		account.db_set("last_error", _format_error(exc))
		frappe.db.commit()
		raise MTProtoError(_format_error(exc)) from exc

	if result.get("status") == "already_logged_in":
		_save_login(account, result["session"])
		return {"status": "already_logged_in"}
	return {"status": "otp_sent", "phone": account.phone}


def finish_login(account, code=None, password=None):
	try:
		result = _run(_finish_login(account, code=code, password=password))
	except MTProtoError:
		raise
	except Exception as exc:
		account.db_set("last_error", _format_error(exc))
		frappe.db.commit()
		raise MTProtoError(_format_error(exc)) from exc

	if result.get("status") == "logged_in":
		_save_login(account, result["session"])
		return {"status": "logged_in"}
	return result


def check_login(account):
	try:
		result = _run(_check_login(account))
	except MTProtoError:
		raise
	except Exception as exc:
		account.db_set("last_error", _format_error(exc))
		frappe.db.commit()
		raise MTProtoError(_format_error(exc)) from exc

	if result.get("status") == "logged_in":
		account.db_set("status", "Active")
		account.db_set("last_error", None)
		frappe.db.commit()
	return result


def disable_bot_privacy(account, bot_username):
	try:
		return _run(_disable_bot_privacy(account, bot_username))
	except MTProtoError:
		raise
	except Exception as exc:
		account.db_set("last_error", _format_error(exc))
		frappe.db.commit()
		raise MTProtoError(_format_error(exc)) from exc


def create_group(account, title, about, bot_username, members=None, add_limit=None):
	try:
		return _run(
			_create_group(
				account,
				title=title,
				about=about,
				bot_username=bot_username,
				members=members,
				add_limit=add_limit,
			)
		)
	except MTProtoError:
		raise
	except Exception as exc:
		account.db_set("last_error", _format_error(exc))
		frappe.db.commit()
		raise MTProtoError(_format_error(exc)) from exc


def create_bot(account, display_name, username):
	from caf_telegram_bot.telegram_client import TelegramBotClient

	try:
		result = _run(_create_bot(account, display_name, username))
	except MTProtoError:
		raise
	except Exception as exc:
		account.db_set("last_error", _format_error(exc))
		frappe.db.commit()
		raise MTProtoError(_format_error(exc)) from exc

	try:
		info = TelegramBotClient.from_token(result["token"]).get_me()
	except Exception as exc:
		raise MTProtoError(f"Bot was created but token verification failed: {exc}") from exc
	username = info.get("username") or result["username"]

	existing = frappe.db.get_value("Telegram Bot", {"username": username}, "name")
	if existing:
		bot = frappe.get_doc("Telegram Bot", existing)
	else:
		bot = frappe.new_doc("Telegram Bot")
		bot.bot_name = result["display_name"]
		if frappe.db.exists("Telegram Bot", bot.bot_name):
			bot.bot_name = f"{result['display_name']} ({username})"

	bot.token = result["token"]
	bot.username = username
	bot.bot_id = str(info.get("id") or "")
	bot.enabled = 1
	bot.status = "Active"
	bot.last_error = None
	bot.adder_account = account.name
	if not frappe.db.exists("Telegram Bot", {"is_primary": 1}):
		bot.is_primary = 1
	bot.save(ignore_permissions=True)
	frappe.db.commit()

	return bot
