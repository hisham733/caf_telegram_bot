import click

import frappe
from frappe.commands import get_site, pass_context

from caf_telegram_bot import mtproto
from caf_telegram_bot.telegram_client import TelegramAPIError, TelegramBotClient
from caf_telegram_bot.utils import get_site_bot_token


@click.group("telegram")
def telegram():
	"""Manage Telegram bots for CAF Telegram Bot."""


def _connect(context):
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	return site


@click.command("list-bots")
@pass_context
def list_bots(context):
	"""List configured Telegram bots."""
	_connect(context)
	try:
		bots = frappe.get_all(
			"Telegram Bot",
			fields=["name", "username", "enabled", "is_primary", "status"],
			order_by="is_primary desc, name asc",
		)
		if not bots:
			click.echo("No Telegram bots configured.")
			return
		for bot in bots:
			flags = []
			if bot.enabled:
				flags.append("enabled")
			if bot.is_primary:
				flags.append("primary")
			flags.append(bot.status or "Unknown")
			click.echo(f"- {bot.name} (@{bot.username or '?'}) [{', '.join(flags)}]")
	finally:
		frappe.destroy()


@click.command("import-bot")
@click.option("--name", "bot_name", default=None, help="Local name for the bot record.")
@click.option("--token", default=None, help="Bot token. Defaults to site_config telegram_bot_token.")
@click.option("--adder-account", default=None, help="Optional Telegram Adder Account to link.")
@click.option("--primary/--no-primary", default=None, help="Force or prevent primary flag.")
@pass_context
def import_bot(context, bot_name=None, token=None, adder_account=None, primary=None):
	"""Create or update a Telegram Bot from a BotFather token."""
	_connect(context)
	try:
		token = token or get_site_bot_token()
		if not token:
			click.secho(
				"No token supplied and telegram_bot_token is missing from site_config.",
				fg="red",
			)
			return

		try:
			info = TelegramBotClient.from_token(token).get_me()
		except TelegramAPIError as exc:
			click.secho(f"Telegram API error: {exc.description}", fg="red")
			return

		username = info.get("username")
		existing = None
		if username:
			existing = frappe.db.get_value("Telegram Bot", {"username": username}, "name")
		if not existing:
			existing = frappe.db.get_value("Telegram Bot", {"bot_name": bot_name or username or "Default Bot"})

		if existing:
			doc = frappe.get_doc("Telegram Bot", existing)
		else:
			doc = frappe.new_doc("Telegram Bot")
			doc.bot_name = bot_name or username or "Default Bot"

		doc.token = token
		doc.username = username
		doc.bot_id = str(info.get("id") or "")
		doc.enabled = 1
		doc.status = "Active"
		doc.last_error = None
		if adder_account:
			doc.adder_account = adder_account

		has_primary = frappe.db.exists("Telegram Bot", {"is_primary": 1})
		if primary is True:
			doc.is_primary = 1
		elif primary is False:
			doc.is_primary = 0
		elif not has_primary:
			doc.is_primary = 1

		doc.save(ignore_permissions=True)
		frappe.db.commit()
		click.secho(f"Saved Telegram Bot {doc.name} (@{username}).", fg="green")
	finally:
		frappe.destroy()


@click.command("ping")
@click.option("--bot", "bot_name", required=True, help="Telegram Bot record name.")
@pass_context
def ping(context, bot_name):
	"""Call getMe and refresh the stored bot identity."""
	_connect(context)
	try:
		doc = frappe.get_doc("Telegram Bot", bot_name)
		try:
			info = doc.get_client().get_me()
		except TelegramAPIError as exc:
			doc.db_set("status", "Error")
			doc.db_set("last_error", exc.description)
			frappe.db.commit()
			click.secho(f"Telegram API error: {exc.description}", fg="red")
			return

		doc.db_set("username", info.get("username"))
		doc.db_set("bot_id", str(info.get("id") or ""))
		doc.db_set("status", "Active")
		doc.db_set("last_error", None)
		frappe.db.commit()
		click.secho(f"OK: @{info.get('username')} (id {info.get('id')})", fg="green")
	finally:
		frappe.destroy()


@click.command("login")
@click.option("--account", "account_name", required=True, help="Telegram Adder Account name.")
@pass_context
def login(context, account_name):
	"""Log in an MTProto adder account and store its session."""
	_connect(context)
	try:
		account = frappe.get_doc("Telegram Adder Account", account_name)
		try:
			result = mtproto.start_login(account)
			if result.get("status") == "already_logged_in":
				click.secho("Already logged in.", fg="green")
				return

			code = click.prompt("OTP code")
			result = mtproto.finish_login(account, code=code)
			if result.get("status") == "password_required":
				password = click.prompt("Two-step verification password", hide_input=True)
				result = mtproto.finish_login(account, password=password)

			if result.get("status") == "logged_in":
				click.secho("Login successful; session saved.", fg="green")
			else:
				click.secho(f"Unexpected login result: {result}", fg="red")
		except mtproto.MTProtoError as exc:
			click.secho(f"Login failed: {exc}", fg="red")
	finally:
		frappe.destroy()


@click.command("create-bot")
@click.option("--account", "account_name", required=True, help="Logged-in Telegram Adder Account.")
@click.option("--name", "display_name", required=True, help="Bot display name.")
@click.option("--username", required=True, help="Bot username (must end with 'bot').")
@pass_context
def create_bot_command(context, account_name, display_name, username):
	"""Create a bot through @BotFather using the adder account."""
	_connect(context)
	try:
		account = frappe.get_doc("Telegram Adder Account", account_name)
		try:
			bot = mtproto.create_bot(account, display_name, username)
		except mtproto.MTProtoError as exc:
			click.secho(f"Could not create bot: {exc}", fg="red")
			return
		click.secho(f"Created Telegram Bot {bot.name} (@{bot.username}).", fg="green")
	finally:
		frappe.destroy()


@click.command("create-task-group")
@click.option("--task", "task_name", required=True, help="Task name.")
@click.option("--bot", "bot_name", default=None, help="Telegram Bot name (default: primary).")
@click.option("--no-members", is_flag=True, help="Do not add verified members.")
@pass_context
def create_task_group_command(context, task_name, bot_name=None, no_members=False):
	"""Create or link a Telegram supergroup for a Task."""
	_connect(context)
	try:
		from caf_telegram_bot import provisioning

		try:
			result = provisioning.provision_task_group(
				task_name,
				bot_name=bot_name,
				add_members=not no_members,
				user="Administrator",
			)
		except Exception as exc:
			click.secho(f"Could not provision group: {exc}", fg="red")
			return

		click.secho(
			result.get("message") or f"Telegram group ready: {result.get('chat_id')}",
			fg="green",
		)
		if result.get("invite_link"):
			click.echo(result["invite_link"])
	finally:
		frappe.destroy()


@click.command("poll")
@click.option("--bot", "bot_name", default=None, help="Poll only this Telegram Bot.")
@click.option("--once", is_flag=True, help="Poll once and exit.")
@pass_context
def poll(context, bot_name=None, once=False):
	"""Poll enabled bots and process Telegram updates."""
	_connect(context)
	try:
		from caf_telegram_bot.poller import run

		try:
			run(bot_name=bot_name, once=once)
		except KeyboardInterrupt:
			click.echo("Stopped.")
	finally:
		frappe.destroy()


commands = [telegram]
telegram.add_command(list_bots)
telegram.add_command(import_bot)
telegram.add_command(ping)
telegram.add_command(login)
telegram.add_command(create_bot_command)
telegram.add_command(create_task_group_command)
telegram.add_command(poll)
