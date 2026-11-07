import json
import time

import frappe
import requests

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 30


class TelegramAPIError(frappe.ValidationError):
	def __init__(self, error_code=None, description=None, parameters=None, method=None):
		self.error_code = error_code
		self.description = description or "Unknown Telegram API error"
		self.parameters = parameters or {}
		self.method = method
		super().__init__(self.description)

	@property
	def retry_after(self):
		if isinstance(self.parameters, dict):
			return self.parameters.get("retry_after")
		return None


class TelegramBotClient:
	"""Minimal synchronous client for the Telegram Bot API.

	Uses plain HTTP requests so it works with any python-telegram-bot version
	installed in the bench environment.
	"""

	def __init__(self, bot, timeout=DEFAULT_TIMEOUT):
		if isinstance(bot, str):
			bot = frappe.get_doc("Telegram Bot", bot)
		self.bot = bot
		self.token = bot.get_password("token") if hasattr(bot, "get_password") else None
		self.timeout = timeout
		if not self.token:
			frappe.throw("Telegram Bot token is not set.")

	@classmethod
	def from_token(cls, token, timeout=DEFAULT_TIMEOUT):
		client = cls.__new__(cls)
		client.bot = None
		client.token = token
		client.timeout = timeout
		return client

	def _redact(self, value):
		if value and self.token:
			return str(value).replace(self.token, "***")
		return value

	def _url(self, method):
		return f"{TELEGRAM_API_BASE}/bot{self.token}/{method}"

	@staticmethod
	def _encode(params):
		encoded = {}
		for key, value in (params or {}).items():
			if value is None:
				continue
			if isinstance(value, (dict, list)):
				value = json.dumps(value)
			elif isinstance(value, bool):
				value = "true" if value else "false"
			encoded[key] = value
		return encoded

	def call(self, method, params=None, timeout=None):
		response = None
		for attempt in range(2):
			try:
				response = requests.post(
					self._url(method),
					data=self._encode(params),
					timeout=timeout or self.timeout,
				)
				break
			except requests.Timeout as exc:
				raise TelegramAPIError(
					description=f"Network timeout calling {method}: {self._redact(str(exc))}",
					method=method,
				) from None
			except requests.ConnectionError as exc:
				if attempt == 0:
					time.sleep(1)
					continue
				raise TelegramAPIError(
					description=f"Network error calling {method}: {self._redact(str(exc))}",
					method=method,
				) from None
			except requests.RequestException as exc:
				raise TelegramAPIError(
					description=f"Network error calling {method}: {self._redact(str(exc))}",
					method=method,
				) from None

		if response is None:
			raise TelegramAPIError(description=f"No response from Telegram for {method}", method=method)

		try:
			payload = response.json()
		except ValueError as exc:
			raise TelegramAPIError(
				error_code=response.status_code,
				description=f"Invalid response from Telegram for {method}",
				method=method,
			) from exc

		if not payload.get("ok"):
			raise TelegramAPIError(
				error_code=payload.get("error_code"),
				description=payload.get("description"),
				parameters=payload.get("parameters"),
				method=method,
			)

		return payload.get("result")

	def get_me(self):
		return self.call("getMe")

	def get_updates(self, offset=None, timeout=30, allowed_updates=None):
		return self.call(
			"getUpdates",
			{
				"offset": offset,
				"timeout": timeout,
				"allowed_updates": allowed_updates,
			},
			timeout=timeout + 10,
		)

	def send_message(
		self,
		chat_id,
		text,
		parse_mode=None,
		reply_markup=None,
		reply_to_message_id=None,
		disable_web_page_preview=None,
	):
		return self.call(
			"sendMessage",
			{
				"chat_id": chat_id,
				"text": text,
				"parse_mode": parse_mode,
				"reply_markup": reply_markup,
				"reply_to_message_id": reply_to_message_id,
				"disable_web_page_preview": disable_web_page_preview,
			},
		)

	def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
		return self.call(
			"editMessageText",
			{
				"chat_id": chat_id,
				"message_id": message_id,
				"text": text,
				"parse_mode": parse_mode,
				"reply_markup": reply_markup,
			},
		)

	def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
		return self.call(
			"answerCallbackQuery",
			{
				"callback_query_id": callback_query_id,
				"text": text,
				"show_alert": show_alert,
			},
		)

	def set_my_commands(self, commands):
		return self.call("setMyCommands", {"commands": commands})

	def get_chat(self, chat_id):
		return self.call("getChat", {"chat_id": chat_id})

	def get_chat_member(self, chat_id, user_id):
		return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

	def get_chat_administrators(self, chat_id):
		return self.call("getChatAdministrators", {"chat_id": chat_id})

	def get_chat_members_count(self, chat_id):
		return self.call("getChatMemberCount", {"chat_id": chat_id})

	def create_chat_invite_link(
		self,
		chat_id,
		name=None,
		expire_date=None,
		member_limit=None,
		creates_join_request=None,
	):
		return self.call(
			"createChatInviteLink",
			{
				"chat_id": chat_id,
				"name": name,
				"expire_date": expire_date,
				"member_limit": member_limit,
				"creates_join_request": creates_join_request,
			},
		)

	def revoke_chat_invite_link(self, chat_id, invite_link):
		return self.call("revokeChatInviteLink", {"chat_id": chat_id, "invite_link": invite_link})

	def approve_chat_join_request(self, chat_id, user_id):
		return self.call("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})

	def decline_chat_join_request(self, chat_id, user_id):
		return self.call("declineChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})

	def ban_chat_member(self, chat_id, user_id, until_date=None, revoke_messages=None):
		return self.call(
			"banChatMember",
			{
				"chat_id": chat_id,
				"user_id": user_id,
				"until_date": until_date,
				"revoke_messages": revoke_messages,
			},
		)

	def unban_chat_member(self, chat_id, user_id, only_if_banned=None):
		return self.call(
			"unbanChatMember",
			{
				"chat_id": chat_id,
				"user_id": user_id,
				"only_if_banned": only_if_banned,
			},
		)

	def delete_message(self, chat_id, message_id):
		return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def get_client(bot):
	if isinstance(bot, TelegramBotClient):
		return bot
	return TelegramBotClient(bot)
