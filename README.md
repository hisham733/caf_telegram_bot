# CAF Telegram Bot

Telegram bot integration for **ERPNext v15** Task control and group management.

Create bots directly from ERPNext, link Telegram users to ERPNext accounts, provision one Telegram
supergroup per Task, and let the team update Task status, priority, progress, dates, and remarks from
Telegram without opening the desk.

---

## Features

- **Create bots from ERPNext** using an MTProto account and `@BotFather` — no manual BotFather visit.
- **Link Telegram users** to ERPNext users with short-lived link codes.
- **One supergroup per Task**:
  - Creates the supergroup with the adder account.
  - Adds the bot and promotes it to administrator with invite rights.
  - Adds all verified Telegram users (respecting the daily add limit).
  - Creates a join-request invite link and sends a welcome message.
  - Stores the group ID on the Task and creates a `Telegram Chat` record.
- **Polling worker** using the Telegram Bot API `getUpdates` — no public webhook required.
- **Command handling** for status, task details, priority, progress, dates, remarks, completion, and cancellation.
- **Inline keyboard** buttons for quick status and progress updates.
- **Join-request approval** for the group invite link.
- **Token redaction** in logs and API error messages.
- **CLI** for polling, importing bots, creating groups, and inspecting configuration.

---

## Requirements

- Frappe Framework v15 / ERPNext v15
- Python 3.10+
- A dedicated Telegram account for MTProto actions (the **adder account**)
- Telegram API credentials from <https://my.telegram.org> (`api_id`, `api_hash`)
- `telethon >= 1.36, < 2`

The app relies on these custom fields on **Task**:

| Field | Type |
|---|---|
| `custom_telegram_group_id` | Data |
| `custom_completion_date` | Date |
| `custom_remark` | Small Text |
| `custom_completed_by_bot` | Data |

They are defined in the `caf` app fixtures in this bench. If you install this app standalone,
create the fields with **Customize Form** or a fixture before using the Telegram commands.

---

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/hisham733/caf_telegram_bot
bench --site <site> install-app caf_telegram_bot
bench --site <site> migrate
```

Install the Python dependency if it is not already available:

```bash
./env/bin/pip install "telethon>=1.36,<2"
```

Restart the bench:

```bash
bench restart
```

---

## Configuration

### 1. Create a Telegram Adder Account

1. Go to **Telegram Adder Account** → **New**.
2. Enter:
   - **Account Name** — a local label, for example `CAF Bot`.
   - **Phone** — the dedicated Telegram account phone number, including country code.
   - **API ID** and **API Hash** — from <https://my.telegram.org>.
   - **Daily Add Limit** — maximum members to add per day (default `20`).
3. Save, then click **Send Login OTP**.
4. Enter the OTP and, if two-step verification is enabled, the password.
5. The status must become **Active**.

You can also log in from the CLI:

```bash
bench --site <site> telegram login --account "CAF Bot"
```

> Use a dedicated account. It creates groups and adds members, and Telegram applies account-level
> flood limits to these actions.

### 2. Create a Telegram Bot

From the desk:

1. Go to **Telegram Bot** → **Create Bot via BotFather**.
2. Choose the adder account, enter a display name, and a username ending in `bot`.
3. Wait for the BotFather conversation to finish. The bot is saved automatically.

Or import an existing token:

```bash
bench --site <site> telegram import-bot --token "<bot-token>" --adder-account "CAF Bot"
```

Then open the **Telegram Bot** record and confirm:

- **Enabled** is checked.
- **Adder Account** is set.
- **Is Primary** is checked for the bot that should create Task groups.

### 3. Disable BotFather Privacy Mode (recommended)

Open the **Telegram Bot** record and click **Disable Privacy Mode**. This runs the `/setprivacy`
conversation with `@BotFather` and stores the result in **Privacy Disabled**.

Why disable it?

- Group administrators already receive all messages, so the bot works as admin either way.
- If the bot is ever missing admin rights, disabled privacy still lets it receive `/commands`.

The trade-off is that the bot receives all group messages. Use trusted groups.

### 4. Run the polling worker

The bot only replies while a poller is running:

```bash
bench --site <site> telegram poll
```

Or add it to your process manager / `Procfile`:

```procfile
telegram: bench --site <site> telegram poll
```

Run only **one** poller per bot token. A second poller causes Telegram `409 Conflict` errors.
For a one-shot check:

```bash
bench --site <site> telegram poll --once
```

---

## Usage

### Link a Telegram user to ERPNext

1. As a System Manager, open **Telegram User** → **Generate Link Code**.
2. Choose the ERPNext user. The code is valid for **15 minutes**.
3. The user opens a private chat with the bot, sends `/start`, then:

   ```text
   /link ABC12345
   ```

4. The Telegram user record becomes **Verified** and is linked to the ERPNext user.

The Generate Link Code button is on the System Manager-only Telegram User list, so an administrator
normally generates the code for each user.

### Create a group for a Task

1. Create and save a Task.
2. On the Task form, open **Telegram → Create Telegram Group** and confirm.

The app:

- Creates a supergroup titled `<TASK-ID> - <Subject>`.
- Invites the bot and promotes it to administrator with invite rights.
- Adds verified Telegram users up to the adder account's daily limit.
- Creates a join-request invite link.
- Sends a welcome message with the Task details and ERPNext link.
- Sets `Task.custom_telegram_group_id`.
- Creates a **Telegram Chat** record linked to the Task.

If a Task already has a group ID, the action reuses the existing group and creates an invite link
if one is missing.

Use **Telegram → Open Telegram Group** to open the invite link. Users who were not added
automatically can join through the invite link; the bot approves requests that use that link.

### Commands

| Command | Description |
|---|---|
| `/start` | Show the linked Task and quick actions |
| `/help` | Show the command list |
| `/status [task]` | Show status, priority, progress, and dates |
| `/task [task]` | Show Task details including description |
| `/set <field> <value>` | Update an editable field |
| `/priority <value>` | Set priority |
| `/progress <0-100>` | Set progress |
| `/startdate <YYYY-MM-DD>` | Set expected start date |
| `/due <YYYY-MM-DD>` | Set expected end date |
| `/completed <YYYY-MM-DD>` | Set completion date |
| `/remark <text>` | Set remark |
| `/doneby <name>` | Set completed by |
| `/done` | Mark the linked Task **Completed** |
| `/cancel` | Mark the linked Task **Cancelled** |
| `/members` | List group members |
| `/linktask <task>` | Link the current chat to a Task |
| `/link <code>` | Link your Telegram account to ERPNext |

Editable fields: `status`, `priority`, `progress`, `exp_start_date`, `exp_end_date`,
`custom_completion_date`, `custom_remark`, `custom_completed_by_bot`.

Use `-`, `clear`, or `none` as a date value to clear a date field.

### Inline buttons

`/status` and `/start` show buttons for:

- Working
- Pending Review
- Completed
- Cancelled
- 25%, 50%, 75%, 100% progress
- Refresh

---

## Permissions and security

- **Group commands:** any member of a Task group can update the linked Task. Only invite trusted
  people to Task groups.
- **Private chat:** a user must be linked and have **Task write** permission.
- **`/linktask`:** requires a linked user with Task write permission.
- **Bot tokens** use Frappe `Password` fields and are encrypted at rest.
- **Token redaction** is applied to Telegram API errors and worker logs.
- **Never commit** bot tokens, API hashes, session strings, or `site_config.json`.
- **One poller per token.** Multiple pollers compete for updates.
- **Daily add limit** protects the adder account. Remaining members should use the invite link.

---

## CLI reference

```bash
bench --site <site> telegram list-bots
bench --site <site> telegram import-bot --token "<token>" --adder-account "CAF Bot"
bench --site <site> telegram ping --bot <bot-name>
bench --site <site> telegram login --account "CAF Bot"
bench --site <site> telegram create-bot --account "CAF Bot" --name "CAF Bot" --username caf_task_bot
bench --site <site> telegram create-task-group --task TASK-2026-00001
bench --site <site> telegram create-task-group --task TASK-2026-00001 --no-members
bench --site <site> telegram poll
bench --site <site> telegram poll --bot <bot-name> --once
```

`create-task-group` accepts:

- `--task` — Task name (required)
- `--bot` — Telegram Bot record; defaults to the primary bot
- `--no-members` — create the group without adding verified members

---

## Architecture

```text
ERPNext Desk
  ├─ Telegram Adder Account ── MTProto (Telethon) ── @BotFather / group creation
  ├─ Telegram Bot ─────────── Bot API (requests) ── messages, commands, invite links
  ├─ Telegram User ────────── link codes, verification, ERPNext user mapping
  └─ Telegram Chat ────────── Task ↔ Telegram group mapping

telegram poll (worker)
  └─ getUpdates → router.py → commands / callbacks / join requests
```

### Main modules

| Path | Responsibility |
|---|---|
| `caf_telegram_bot/mtproto.py` | Telethon client, adder login, BotFather bot creation, privacy toggle, group creation, member invites |
| `caf_telegram_bot/telegram_client.py` | Minimal Telegram Bot API client with retries and token redaction |
| `caf_telegram_bot/poller.py` | Long polling, update offset persistence, backoff, multi-bot handling |
| `caf_telegram_bot/router.py` | Command dispatch, permissions, inline callbacks, join requests, link codes |
| `caf_telegram_bot/provisioning.py` | Task group provisioning and `Telegram Chat` creation |
| `caf_telegram_bot/api.py` | Whitelisted desk methods for login, bot creation, link codes, groups, privacy |
| `caf_telegram_bot/commands.py` | `bench telegram` CLI |
| `caf_telegram_bot/public/js/task.js` | Task form Telegram buttons |
| `caf_telegram_bot/.../telegram_bot/telegram_bot.js` | Telegram Bot form privacy button |

### Doctypes

- **Telegram Bot** — token, username, primary flag, poller state.
- **Telegram Adder Account** — MTProto credentials, session, login status, add limits.
- **Telegram User** — Telegram identity, ERPNext user link, verification.
- **Telegram Chat** — Telegram chat ID, linked Task, bot, invite link, admin flags.
- **Telegram Message Log** — processed update log for deduplication and diagnostics.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot does not reply | Poller not running | Run `bench --site <site> telegram poll` |
| `409 Conflict` | Two pollers for the same token | Stop the duplicate poller |
| `401 Unauthorized` | Invalid bot token | Re-import or update the token |
| `Flood wait` | Telegram rate limit | Wait until `flood_wait_until`; reduce add limit |
| User not added to group | Daily add limit reached | Share the invite link; the bot approves join requests |
| Invite link error | Bot is not admin or lacks invite rights | Promote the bot to admin with **Invite Users** |
| `No Task is linked to this chat` | Chat is not linked to a Task | Use `/linktask <task>` or create the group from the Task |
| `That link code is invalid or expired` | Code older than 15 minutes or already used | Generate a new link code |
| BotFather did not return a token | Username taken or invalid | Use a unique username ending in `bot` |
| `Adder account is not logged in` | MTProto session missing | Re-run login and OTP verification |

Logs:

```bash
tail -f logs/telegram.log
tail -f logs/telegram.error.log
tail -f sites/<site>/logs/database.log
```

---

## Development

```bash
cd apps/caf_telegram_bot
pre-commit install
pre-commit run --all-files
```

The project uses:

- `ruff` for Python linting and formatting
- `eslint` / `prettier` for JavaScript
- `pyupgrade` for Python syntax upgrades

Run the poller once after code changes:

```bash
bench --site <site> clear-cache
bench --site <site> telegram poll --once
```

---

## License

MIT
