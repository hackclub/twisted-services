# Twisted Services

Twisted Services is a FastAPI application that receives Slack events and
provides the `/twisted-ping` Slack command for posting announcements to
channels.

## Current features

- `GET /healthz` returns `{"status":"ok"}`.
- `POST /slack/events` receives requests from Slack through Slack Bolt.
- The `/twisted-ping` (or `/twistedping`) Slack command opens a rich-text
  composer for notifying here, the channel, or nobody, sending as the user or
  Twisted, and scheduling delivery.
  The command is available only to the channel creator, except in the
  configured exempt channels.
- The `/twisted-oauth` Slack command sends an ephemeral link to the OAuth login
  page for enabling Send as User.
- Errors can be posted to a Slack logging channel when `LOGGING_CHANNEL_ID`
  is configured.
- A heartbeat message can be posted to the logging channel every 24 hours when
  `LOGGING_CHANNEL_ID` is configured.
- `GET /oauth` signs in with Slack OAuth and stores the user token encrypted at
  rest; `POST /oauth/send` sends a message as that user.
- A message shortcut named `edit_twisted_message` lets channel managers and
  members of `#twisters` edit messages sent by the bot.
- Files attached to an `@Twisted` mention in `#twisters` are uploaded to R2 and
  linked in the same thread.

## Configuration

The application reads real environment variables first. If a variable is not
set, it loads missing values from a local `.env` file. Start with
[`.env.sample`](./.env.sample), but replace all placeholder values before
running the application.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `APP_NAME` | No | `Twisted Services` | FastAPI application name. |
| `API_HOST` | No | `0.0.0.0` | Host used by the built-in Uvicorn server. |
| `API_PORT` | No | `8000` | Port used by the built-in Uvicorn server. |
| `SLACK_BOT_TOKEN` | Yes | — | Slack bot token, usually beginning with `xoxb-`. |
| `SLACK_SIGNING_SECRET` | Recommended | — | Used by Slack Bolt to verify Slack requests. Without it, signed requests are acknowledged but cannot be verified. |
| `LOGGING_CHANNEL_ID` | No | — | Slack channel for error logs and the daily heartbeat. |
| `LOGGING_CC_USER_ID` | No | — | Optional Slack user ID to mention in error-log threads. |
| `SLACK_CLIENT_ID` | For OAuth | — | Slack OAuth v2 client ID. |
| `SLACK_CLIENT_SECRET` | For OAuth | — | Slack OAuth v2 client secret. Keep it out of source control. |
| `SLACK_OAUTH_REDIRECT_URI` | For OAuth | — | Must exactly match the callback URL configured in Slack. |
| `OAUTH_STATE_SECRET` | For OAuth | — | Long random secret used to sign OAuth state values. |
| `OAUTH_TOKEN_ENCRYPTION_KEY` | For OAuth | — | Fernet key used to encrypt Slack user tokens in the database. |
| `R2_ENDPOINT` | For uploads | — | Cloudflare R2 S3-compatible endpoint. |
| `R2_ACCESS_KEY_ID` | For uploads | — | R2 access key. |
| `R2_SECRET_ACCESS_KEY` | For uploads | — | R2 secret key. |
| `R2_BUCKET` | For uploads | — | R2 bucket name. |
| `R2_PUBLIC_BASE_URL` | No | `https://twisted.hackclub-assets.com` | Public URL prefix for uploaded files. |
| `DATABASE_URL` | No | Local SQLite | Async SQLAlchemy URL. Use `postgresql+asyncpg://...` for PostgreSQL. |

`AUTH_BEARER_TOKEN`, `SHIP_CHANNEL_ID`, and `MASTER_LOGGING_CHANNEL_ID` are
retained for compatibility but are not used by any currently exposed
endpoint.

Generate the Fernet key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Run locally

The repository includes `run.sh`, which loads `.env`, starts the configured
database when needed, and redeploys the bot by stopping the previous PID and
starting a fresh process:

```bash
cp env.sample .env
# Edit .env with your credentials.
chmod +x run.sh
./run.sh
```

SQLite requires no separate service. For PostgreSQL, set `DATABASE_URL` and
use an externally managed database, or set `CREATE_DB_CONTAINER=true` along
with `POSTGRES_PASSWORD` to let `run.sh` create or restart a local Docker
container. Logs are written to `twisted-services.log`; the current PID is kept
in `.twisted-services.pid`.

### Linux and macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .env.sample .env
# Edit .env with your Slack credentials and channel IDs.

python twisted_services/main.py
```

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.sample .env
# Edit .env with your Slack credentials and channel IDs.

py .\twisted_services\main.py
```

The server listens on `http://127.0.0.1:8000` by default. `API_HOST` and
`API_PORT` control the bind address and port.

## Run with Uvicorn

The application can also be started with Uvicorn directly:

```bash
uvicorn twisted_services.main:app --host 0.0.0.0 --port 8000
```

## Run with Docker

Build the image:

```bash
docker build -t twisted-services .
```

Run it with the required Slack settings:

```bash
docker run --rm -p 8000:8000 \
  -e SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN" \
  -e SLACK_SIGNING_SECRET="$SLACK_SIGNING_SECRET" \
  -e LOGGING_CHANNEL_ID="$LOGGING_CHANNEL_ID" \
  -e LOGGING_CC_USER_ID="$LOGGING_CC_USER_ID" \
  twisted-services
```

For a read-only container filesystem, use PostgreSQL and inject the database
URL at runtime. Do not use `run.sh` inside the container because it writes a
PID file and log file; `run.sh` is for local Codespaces only.

```bash
docker run --rm --read-only -p 8000:8000 \
  -e SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN" \
  -e SLACK_SIGNING_SECRET="$SLACK_SIGNING_SECRET" \
  -e DATABASE_URL="postgresql+asyncpg://user:password@postgres-host:5432/twisted_services" \
  -e OAUTH_TOKEN_ENCRYPTION_KEY="$OAUTH_TOKEN_ENCRYPTION_KEY" \
  -e OAUTH_STATE_SECRET="$OAUTH_STATE_SECRET" \
  twisted-services
```

The image runs from `/`, which always exists on the runtime filesystem, and
starts with `/usr/local/bin/python -m twisted_services.main`. The application creates its tables in the
configured PostgreSQL database during startup and does not need to write to the
container filesystem.

## HTTP endpoints

### Slack OAuth

Register `https://your-host.example/oauth/callback` as the Slack OAuth redirect
URL and grant the user scopes `chat:write` and `im:write`. Open
`https://your-host.example/oauth` in a browser to sign in. The callback sets an
HttpOnly session cookie. Use that session to send as the signed-in user:

```bash
curl -b cookies.txt -c cookies.txt -L https://your-host.example/oauth
curl -b cookies.txt -X POST https://your-host.example/oauth/send \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"C0123456789","message":"Hello from OAuth"}'
```

`target_id` may be a channel (`C...` or `G...`) or a user (`U...`); user IDs
are opened as direct messages. OAuth tokens are encrypted with Fernet before
being written through SQLAlchemy.

### `GET /healthz`

This endpoint does not require authentication:

```bash
curl -sS http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"status":"ok"}
```

### `POST /slack/events`

Slack calls this endpoint for URL verification, slash commands, actions, and
events. Requests should be sent by Slack with
`X-Slack-Request-Timestamp` and `X-Slack-Signature` headers.

When `SLACK_SIGNING_SECRET` is configured, Slack Bolt verifies the request.
When it is missing, unsigned requests receive `503`; requests that include
Slack signature headers are acknowledged with `200` so Slack does not retry
them.

For URL verification, Slack Bolt returns the challenge response. A plain
`curl` request is not a valid Slack request unless it includes a matching
signature.

## Slack app setup

Configure the Slack app to:

1. Set the **Request URL** for events and slash commands to
   `https://your-host.example/slack/events`.
2. Subscribe to `app_mention`; file uploads require the mentioned message to
  include its file payload.
3. Create `/twisted-ping`, `/twistedping`, and `/twisted-oauth` with the same
  request URL.
4. Add a message shortcut with callback ID `edit_twisted_message`.
5. Grant the bot the permissions required to read channel membership and post
   messages.
6. Put the bot token and signing secret in the environment, not in source
   control.

Use `/twisted-ping here message` to notify the current channel with an
`@here` mention. Any other first argument produces an `@channel` mention.
The command can be used by the channel creator, or in one of the exempt
channels defined in `twisted_services/main.py`.

## Endpoint test scripts

`test_endpoints.sh`, `test_endpoints.ps1`, and `test_endpoint.sh` are retained
as legacy scripts for the former shipping and fulfillment API. They target
endpoints that are not exposed by the current application and should not be
used as health checks for this version. Use `/healthz` to verify that the
server is running.

## Security

- Keep `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` secret.
- Put the application behind HTTPS when it is reachable from the internet.
- Restrict access to the logging channel and use the least-privileged Slack
  scopes needed by the bot.
- Use PostgreSQL in production, set a strong `OAUTH_TOKEN_ENCRYPTION_KEY` and
  `OAUTH_STATE_SECRET`, and keep both outside source control.
