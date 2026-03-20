# Slack Integration

ResearchLoop includes a Slack bot that provides notifications, sprint management commands, and conversational research assistance.

## Setup

### 1. Create a Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**
2. Choose **From scratch** and name it (e.g., "ResearchLoop")
3. Select your workspace

### 2. Configure event subscriptions

1. Go to **Event Subscriptions** and enable events
2. Set the **Request URL** to: `https://your-server.fly.dev/api/slack/events`
3. Under **Subscribe to bot events**, add:
    - `app_mention` -- responds when mentioned in channels
    - `message.im` -- responds to direct messages

### 3. Add OAuth scopes

Go to **OAuth & Permissions** and add these **Bot Token Scopes**:

- `chat:write` -- send messages
- `files:write` -- upload PDF reports as attachments

### 4. Install the app

Click **Install to Workspace** and authorize the app.

### 5. Set environment variables

Copy the **Bot User OAuth Token** and **Signing Secret** from your app settings:

```bash
RESEARCHLOOP_SLACK_BOT_TOKEN="xoxb-..."
RESEARCHLOOP_SLACK_SIGNING_SECRET="..."
RESEARCHLOOP_SLACK_CHANNEL_ID="C0123456789"
RESEARCHLOOP_SLACK_ALLOWED_USER_IDS="U01ABC,U02DEF"
```

!!! note "Channel ID vs User ID"
    For notifications, `channel_id` can be either a channel ID (starts with `C`) or a user ID (starts with `U`). Setting it to your user ID sends notifications as DMs.

## Commands

The bot responds to these commands in DMs or when @mentioned:

| Command | Description |
|---------|-------------|
| `sprint run <study> <idea>` | Submit a new sprint |
| `sprint list` | List the 10 most recent sprints |
| `auth status` | Check if Claude CLI is authenticated on the server |
| `help` | Show available commands |

### Examples

```
sprint run my-study try using a larger learning rate
sprint list
help
```

## Conversational mode

Beyond structured commands, the Slack bot supports free-form conversations. Messages in a thread are tracked as a Claude session, so the bot maintains context within a thread.

The bot can:

- **Discuss research ideas** and help plan sprints
- **Review results** from completed sprints
- **Look up papers** and references (web search and fetch)
- **Suggest next steps** based on sprint results
- **Execute actions** when you ask (start sprints, check status, start loops)

### Actions

When the bot determines you want to perform an action, it uses structured action tags internally:

| Action | Parameters | Description |
|--------|-----------|-------------|
| `sprint_run` | `study`, `idea` | Start a sprint |
| `sprint_list` | `study` (optional) | List sprints |
| `sprint_show` | `id` | Show sprint details |
| `sprint_cancel` | `id` | Cancel a sprint |
| `study_show` | `name` | Show study info |
| `loop_start` | `study`, `count`, `context` | Start an auto-loop |

### Thread context

When a sprint notification is sent in a thread, the bot automatically associates that thread with the sprint. Subsequent messages in the thread have access to the sprint's details, making it easy to discuss results or request follow-up actions.

## Notifications

The bot sends notifications to the configured `channel_id` for:

- **Sprint started** -- includes study name and idea
- **Sprint completed** -- includes summary, with PDF report attached if available
- **Sprint failed** -- includes error message

Notification messages are posted in threads. If you reply to a notification, the conversation continues with full sprint context.

## User access control

### Allowed users

Set `allowed_user_ids` to restrict which Slack users can interact with the bot:

```bash
RESEARCHLOOP_SLACK_ALLOWED_USER_IDS="U01ABC,U02DEF"
```

If not set, all users can interact with the bot.

Unauthorized users receive a "Sorry, you're not authorized" message.

### Channel restriction

Set `restrict_to_channel = true` in the config to limit the bot to only respond in the configured `channel_id`. DMs are always allowed regardless of this setting.

```toml
[slack]
restrict_to_channel = true
channel_id = "C0123456789"
```

## Security

- **Signature verification** -- all incoming events are verified using the Slack signing secret (HMAC-SHA256). Events with invalid signatures are rejected with a 403 error.
- **Event deduplication** -- Slack may retry event delivery. The bot tracks processed event IDs to avoid duplicate handling.
- **Bot message filtering** -- messages from bots are ignored to prevent loops.
- **Background processing** -- events are acknowledged with a 200 response immediately, then processed in a background task to avoid Slack's 3-second timeout.
