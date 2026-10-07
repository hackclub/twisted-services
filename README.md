# Twisted Services

Twisted Services is a FastAPI application that receives Slack events and
provides the `/twisted-ping` Slack command for posting announcements to
channels.

## Current features

- `GET /healthz` returns `{"status":"ok"}`.
- `POST /slack/events` receives requests from Slack through Slack Bolt.
- The `/twisted-ping` Slack command posts a message to the current channel.
  The command is available only to the channel creator, except in the
  configured exempt channels.
- Errors can be posted to a Slack logging channel when `LOGGING_CHANNEL_ID`
  is configured.
- A heartbeat message can be posted to the logging channel every 24 hours when
  `LOGGING_CHANNEL_ID` is configured.

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

`AUTH_BEARER_TOKEN`, `SHIP_CHANNEL_ID`, and `MASTER_LOGGING_CHANNEL_ID` are
retained for compatibility but are not used by any currently exposed
endpoint.

## Run locally

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

## HTTP endpoints

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
2. Subscribe to the `app_mention` event if mention logging is required.
3. Create the `/twisted-ping` slash command with the same request URL.
4. Grant the bot the permissions required to read channel membership and post
   messages.
5. Put the bot token and signing secret in the environment, not in source
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
