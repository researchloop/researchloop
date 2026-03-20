# Security

ResearchLoop implements several layers of security to protect the orchestrator, dashboard, and communication between components.

## Authentication

### Dashboard authentication

The web dashboard requires password authentication. Passwords are stored as bcrypt hashes.

**Setting the password:**

- **First-run setup** -- on first visit, `/dashboard/setup` prompts for a password
- **Environment variable** -- `RESEARCHLOOP_DASHBOARD_PASSWORD` is auto-hashed on startup
- **Pre-hashed** -- `RESEARCHLOOP_DASHBOARD_PASSWORD_HASH` or `dashboard.password_hash` in the config

**Sessions:**

- Signed cookies using `itsdangerous.URLSafeTimedSerializer`
- 7-day expiry (`SESSION_MAX_AGE = 604800`)
- The signing key is auto-generated on first use and persisted in the database `settings` table, so sessions survive server restarts
- Cookies are set with `httponly=True` and `samesite=lax`

### API authentication

The REST API supports two authentication methods:

1. **Bearer token** -- obtained via `POST /api/auth` with the dashboard password. Used by the CLI (`researchloop connect`). Tokens are the same signed format as session cookies.
2. **Shared secret** -- passed via the `X-Shared-Secret` header. Used for server-to-server communication.

If no `shared_secret` is configured, API endpoints are accessible without authentication.

### CLI authentication

The CLI authenticates using the bearer token mechanism:

```bash
researchloop connect https://your-server.fly.dev
# Prompts for password, gets token from /api/auth
# Saves to ~/.config/researchloop/credentials.json (mode 600)
```

The CLI automatically re-authenticates on 401 responses by prompting for the password again.

## Webhook security

### Per-sprint webhook tokens

Each sprint is assigned a unique webhook token at creation time. The sprint runner includes this token in all webhook requests:

- **Completion webhook** -- `X-Webhook-Token` header on `POST /api/webhook/sprint-complete`
- **Heartbeat** -- `X-Webhook-Token` header on `POST /api/webhook/heartbeat`
- **Artifact upload** -- `X-Webhook-Token` header on `POST /api/artifacts/{sprint_id}`

The orchestrator validates the token against the sprint's stored `webhook_token` before processing any webhook request.

This prevents:

- Unauthorized completion of sprints
- Spoofed heartbeats
- Unauthorized artifact uploads

## CSRF protection

All mutating dashboard actions (forms that submit POST requests) are protected by CSRF tokens:

- Tokens are derived from the session token and signing secret using HMAC-SHA256
- Each form includes a hidden `csrf_token` field
- The server validates the CSRF token before processing the request
- Invalid CSRF tokens return a 403 response

Protected actions include: sprint creation, cancellation, deletion, resubmission, loop creation/stop/resume, and login.

## Slack security

### Signature verification

All incoming Slack events are verified using HMAC-SHA256 signature verification:

1. Slack sends `X-Slack-Request-Timestamp` and `X-Slack-Signature` headers
2. The server computes the expected signature using the Slack signing secret
3. Requests with invalid signatures are rejected with 403

The signing secret must be configured -- if `bot_token` is set but `signing_secret` is not, events are rejected with a 500 error.

### User access control

- `allowed_user_ids` restricts which Slack users can interact with the bot
- `restrict_to_channel` limits bot responses to a specific channel (DMs are always allowed)
- Bot messages are filtered out to prevent loops

### Event deduplication

Slack may retry event delivery. The bot tracks processed event IDs and ignores duplicates.

## SSH security

The orchestrator connects to HPC clusters via SSH using key-based authentication:

- SSH keys are configured per-cluster via `key_path`
- In Docker/Fly.io deployments, the key is injected from a secret via the entrypoint script
- Connection pooling via `SSHManager` avoids repeated key exchanges

## Path traversal protection

The dashboard artifact download and PDF download routes validate that resolved file paths are within the configured `artifact_dir`:

```python
if not str(file_path).startswith(str(artifact_dir) + "/"):
    raise HTTPException(status_code=403, detail="Access denied: path traversal detected")
```

## Recommendations

1. **Always set a shared secret** -- without it, API endpoints are open
2. **Use environment variables for secrets** -- never commit tokens or passwords to the config file
3. **Set allowed_user_ids for Slack** -- restrict who can start sprints
4. **Use HTTPS** -- Fly.io provides this automatically; for other deployments, use a reverse proxy
5. **Rotate the shared secret** if compromised -- update both the orchestrator config and any scripts that use it
6. **Set file permissions on credentials** -- the CLI saves credentials with mode 600, but verify this on your system
