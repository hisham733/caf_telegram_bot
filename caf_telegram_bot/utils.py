import frappe


def redact(value, *secrets):
	"""Replace known secrets in a string with a placeholder."""
	if value is None:
		return value
	value = str(value)
	for secret in secrets:
		if secret:
			value = value.replace(str(secret), "***")
	return value


def get_site_bot_token():
	return frappe.conf.get("telegram_bot_token")
