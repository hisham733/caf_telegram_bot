import frappe
from frappe.model.document import Document


class TelegramAdderAccount(Document):
	def validate(self):
		if not str(self.api_id or "").strip().isdigit():
			frappe.throw("API ID must be numeric.")

	def get_api_credentials(self) -> dict:
		return {
			"api_id": int(self.api_id),
			"api_hash": self.get_password("api_hash"),
			"session": self.get_password("session", raise_exception=False),
			"phone": self.phone,
		}
