import frappe
from frappe.model.document import Document


class TelegramBot(Document):
	def get_token(self) -> str:
		return self.get_password("token")

	def get_client(self):
		from caf_telegram_bot.telegram_client import TelegramBotClient

		return TelegramBotClient(self)
