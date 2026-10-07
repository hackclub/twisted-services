import asyncio
import os
import re
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp
from starlette.routing import Match


def load_dotenv_if_present(path: str = ".env") -> None:
	"""Load key=value pairs from a .env file into os.environ for keys that are missing.

	Preferred configuration is via real environment variables. If a variable is
	not present, this will attempt to read a local `.env` file and populate
	missing keys so the app can fall back to developer convenience files.
	"""
	if not os.path.exists(path):
		return

	try:
		with open(path, "r", encoding="utf8") as fh:
			for line in fh:
				line = line.strip()
				if not line or line.startswith("#") or "=" not in line:
					continue
				key, _, val = line.partition("=")
				key = key.strip()
				val = val.strip().strip('"').strip("'")
				if os.getenv(key) is None:
					os.environ[key] = val
	except Exception:
		# don't fail startup on malformed .env; prefer explicit env vars
		return


# populate missing env vars from .env (if present)
load_dotenv_if_present()


@dataclass(frozen=True)
class Settings:
	app_name: str = os.getenv("APP_NAME", "Twisted Services")
	api_host: str = os.getenv("API_HOST", "0.0.0.0")
	api_port: int = int(os.getenv("API_PORT", "8000"))
	auth_bearer_token: str = os.getenv("AUTH_BEARER_TOKEN", "")
	slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
	slack_signing_secret: str = os.getenv("SLACK_SIGNING_SECRET", "")
	# Optional ship channel id for the /ship endpoint; can be set as an environment variable
	master_logging_channel_id: str = os.getenv("MASTER_LOGGING_CHANNEL_ID", "")
	# Optional logging channel id for periodic heartbeat messages; can be set as an environment variable
	logging_channel_id: str = os.getenv("LOGGING_CHANNEL_ID", "")
	# Optional user id to cc in logging thread; set to empty to disable
	logging_cc_user_id: str = os.getenv("LOGGING_CC_USER_ID", "")

class SlackDispatchResult(BaseModel):
	ok: bool
	channel: str
	ts: str | None = None


def _truncate_message(value: str, limit: int = 3000) -> str:
	if len(value) <= limit:
		return value
	return f"{value[:limit]}...(truncated)"


def _format_request_snapshot(request: Request, body: bytes | None) -> str:
	sensitive_headers = {
		"authorization",
		"proxy-authorization",
		"cookie",
		"set-cookie",
		"x-slack-signature",
	}
	redacted = "[redacted]"
	headers_lines = []
	for key, value in request.headers.items():
		if key.lower() in sensitive_headers:
			headers_lines.append(f"{key}: {redacted}")
		else:
			headers_lines.append(f"{key}: {value}")
	headers_text = "\n".join(headers_lines)
	body_text = ""
	if body:
		body_text = body.decode("utf-8", errors="replace")
	return "\n\n".join(
		[
			f"Request headers:\n{headers_text or '(none)'}",
			f"Request body:\n{body_text or '(empty)'}",
		]
	)


def _format_response_snapshot(body_text: str) -> str:
	return f"Response body:\n{body_text or '(empty)'}"


def _format_traceback(trace: str) -> str:
	return f"Traceback:\n{trace}"


def _join_detail(*parts: str | None) -> str | None:
	items = [part for part in parts if part]
	if not items:
		return None
	return "\n\n".join(items)


async def _extract_response_body(response: Response) -> tuple[Response, str | None]:
	body = getattr(response, "body", None)
	if isinstance(body, (bytes, bytearray)) and body:
		return response, body.decode("utf-8", errors="replace")
	if isinstance(body, str) and body:
		return response, body
	body_iterator = getattr(response, "body_iterator", None)
	if body_iterator is None:
		return response, None
	chunks: list[bytes] = []
	async for chunk in body_iterator:
		if isinstance(chunk, (bytes, bytearray)):
			chunks.append(bytes(chunk))
		else:
			chunks.append(str(chunk).encode("utf-8"))
	if not chunks:
		return response, None
	body_bytes = b"".join(chunks)
	headers = dict(response.headers)
	headers.pop("content-length", None)
	headers.pop("transfer-encoding", None)
	return (
		Response(
			content=body_bytes,
			status_code=response.status_code,
			headers=headers,
			media_type=response.media_type,
			background=response.background,
		),
		body_bytes.decode("utf-8", errors="replace"),
	)


def _is_known_route(request: Request) -> bool:
	for route in request.app.router.routes:
		match, _ = route.matches(request.scope)
		if match is Match.FULL:
			return True
	return False


def get_settings() -> Settings:
	return Settings()


def verify_bearer_token(
	authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
	settings = get_settings()
	if not settings.auth_bearer_token:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Server missing AUTH_BEARER_TOKEN; configure it in the environment",
		)
	expected = f"Bearer {settings.auth_bearer_token}"
	if authorization != expected:
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Invalid or missing bearer token.",
			headers={"WWW-Authenticate": "Bearer"},
		)


class SlackRelay:
	# Channels exempt from channel manager check for /twisted-ping
	TWISTED_PING_EXEMPT_CHANNELS = [
		"C02185NHSFK", #pingus-pongus
		"C0BLMQVEWKA", #twisted-dev
		"C0AD3HRV4F8", #twisters
		"C0BR0LLL7G9", #twistwrights
	]

	def __init__(self, settings: Settings) -> None:
		self.settings = settings
		self.app = AsyncApp(
			token=settings.slack_bot_token,
			signing_secret=settings.slack_signing_secret,
		)
		self.handler = AsyncSlackRequestHandler(self.app)
		self._register_default_handlers()

	def _register_default_handlers(self) -> None:
		@self.app.action(re.compile(".*"))
		async def _ack_any_action(ack) -> None:
			await ack()

		@self.app.command("/twisted-ping")
		async def handle_twisted_ping(ack, command, client, say) -> None:
			await ack()
			await self._process_twisted_ping(command, client, say)

		@self.app.event("app_mention")
		async def handle_message_events(body, logger) -> None:
			"""Handle message events (logs for now; can be extended later)."""
			logger.info(f"Message event received: {body.get('event', {}).get('type')}")

	async def _is_channel_manager(self, channel_id: str, user_id: str) -> bool:
		"""Check if a user is a channel manager for the given channel."""
		try:
			info = await self.app.client.conversations_info(channel=channel_id)
			channel = info.get("channel", {})
			creator_user_id = channel.get("creator")
			if user_id == creator_user_id:
				return True
			
			# Check channel members with manager role or use conversations_members for moderators
			members = await self.app.client.conversations_members(channel=channel_id)
			member_list = members.get("members", [])
			
			# For now, check if user is the channel creator
			# In a production system, you might check Slack Connect or other RBAC mechanisms
			return user_id == creator_user_id
		except Exception:
			return False

	async def _process_twisted_ping(self, command: dict[str, Any], client: Any, say: Any) -> None:
		"""Process /twisted-ping command."""
		try:
			# Parse command text: [channel/here] [msg can be multiline]
			text = command.get("text", "").strip()
			if not text:
				await client.chat_postEphemeral(
					channel=command["channel_id"],
					user=command["user_id"],
					text="Usage: `/twisted-ping [channel/here] [message]`",
				)
				return

			# Split text to get channel and message
			parts = text.split(None, 1)  # Split on first whitespace
			
			channel_spec = parts[0]
			message = parts[1] if len(parts) > 1 else "(no message)"

			# Resolve channel
			target_channel = command["channel_id"]

			# Remove < > if channel is formatted as <#C123456> 
			if channel_spec.lower() == "here":
				message = f"<!here> {message}"
			else:
				message = f"<!channel> {message}"

			# Check if channel is exempt from CM check
			if target_channel not in self.TWISTED_PING_EXEMPT_CHANNELS:
				# Check if user is channel manager
				is_cm = await self._is_channel_manager(target_channel, command["user_id"])
				if not is_cm:
					await client.chat_postEphemeral(
						channel=command["channel_id"],
						user=command["user_id"],
						text=":loll:You are not a channel manager. Only channel managers can use `/twisted-ping` in this channel.",
					)
					return

			blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]
			# Fetch user's profile for name and avatar
			user_info = await self.app.client.users_info(user=command["user_id"])
			user_profile = user_info.get("user", {})
			user_name = user_profile.get("profile", {}).get("display_name") or user_profile.get("real_name", "Unknown")
			user_avatar = user_profile.get("profile", {}).get("image_512") or user_profile.get("profile", {}).get("image_original", "")

			# Send message to target channel
			await client.chat_postMessage(
				channel=target_channel,
				text=message,
				blocks=blocks,
				username=user_name,
				icon_url=user_avatar,
			)
		except Exception as exc:
			await client.chat_postEphemeral(
				channel=command["channel_id"],
				user=command["user_id"],
				text=f"Error processing ping: {str(exc)[:100]}",
			)
			await self.log_error(
				"Error in /twisted-ping command",
				detail=f"Command: {command}\n\nError:\n{traceback.format_exc()}",
			)

	@staticmethod
	def _thread_detail_messages(detail: str | None, limit: int = 3500) -> list[str]:
		if not detail:
			return []
		safe_detail = detail.replace("```", "``\\`")
		chunk_size = max(1, limit - 8)
		chunks = [safe_detail[i : i + chunk_size] for i in range(0, len(safe_detail), chunk_size)]
		return [f"```\n{chunk}\n```" for chunk in chunks]

	async def log_error(self, message: str, detail: str | None = None) -> None:
		"""Best-effort error logging to the configured logging channel."""
		if not self.settings.logging_channel_id:
			return
		try:
			resp = await self.app.client.chat_postMessage(
				channel=self.settings.logging_channel_id,
				text=message,
			)
			thread_ts = resp.get("ts") if resp.get("ok") else None
			if thread_ts:
				messages = self._thread_detail_messages(detail)
				for thread_message in messages:
					await self.app.client.chat_postMessage(
						channel=self.settings.logging_channel_id,
						text=thread_message,
						thread_ts=thread_ts,
					)
				if self.settings.logging_cc_user_id:
					await self.app.client.chat_postMessage(
						channel=self.settings.logging_channel_id,
						text=f"CC: <@{self.settings.logging_cc_user_id}>",
						thread_ts=thread_ts,
					)
		except Exception as exc:
			print(f"Failed to write to logging channel: {exc}")

	async def _resolve_target_channel(self, target_id: str) -> str:
		if target_id.startswith("U"):
			conv = await self.app.client.conversations_open(users=target_id)
			return conv["channel"]["id"]
		if target_id.startswith("C") or target_id.startswith("G"):
			return target_id
		raise HTTPException(status_code=400, detail="target_id must start with U for DM or C/G for channel")

	async def send_block_kit(self, target_id: str, title: str, blocks: list[dict]) -> SlackDispatchResult:
		channel = await self._resolve_target_channel(target_id)
		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=title,
			blocks=blocks
		)
		return SlackDispatchResult(ok=bool(resp["ok"]), channel=channel, ts=resp.get("ts"))

async def bot_heartbeat_task(settings: Settings, slack_relay: SlackRelay) -> None:
	"""Background task that sends a heartbeat message every 24 hrs to the logging channel."""
	if not settings.logging_channel_id:
		return

	while True:
		try:
			await asyncio.sleep(86400)  # 24 hrs
			resp = await slack_relay.app.client.chat_postMessage(
				channel=settings.logging_channel_id,
				text="Bot is Online!",
			)
			if not resp.get("ok"):
				print(f"Failed to send heartbeat: {resp}")
		except Exception as e:
			print(f"Heartbeat task error (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
	"""Manage app startup and shutdown, including background tasks."""
	task = None
	if settings.logging_channel_id:
		task = asyncio.create_task(bot_heartbeat_task(settings, slack_relay))
	yield
	if task:
		task.cancel()
		try:
			await task
		except asyncio.CancelledError:
			pass


settings = get_settings()
slack_relay = SlackRelay(settings)
app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.middleware("http")
async def error_logging_middleware(request: Request, call_next):
	path = request.url.path
	if request.url.query:
		path = f"{path}?{request.url.query}"
	is_known_route = _is_known_route(request)
	should_log = is_known_route and request.url.path != "/slack/events"
	try:
		response = await call_next(request)
	except Exception as exc:
		if should_log:
			request_body = None
			try:
				request_body = await request.body()
			except Exception:
				request_body = None
			await slack_relay.log_error(
				_truncate_message(
					f":warning: {request.method} {path} -> 500 {type(exc).__name__}: {exc}"
				),
				detail=_join_detail(
					_format_request_snapshot(request, request_body),
					_format_traceback(traceback.format_exc()),
				),
			)
		raise
	if should_log and response.status_code >= 400:
		request_body = None
		try:
			request_body = await request.body()
		except Exception:
			request_body = None
		response, response_body = await _extract_response_body(response)
		await slack_relay.log_error(
			_truncate_message(
				f":warning: {request.method} {path} -> {response.status_code}"
			),
			detail=_join_detail(
				_format_request_snapshot(request, request_body),
				_format_response_snapshot(response_body or ""),
			),
		)
	return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
	return {"status": "ok"}


@app.post("/slack/events", status_code=200)
async def slack_events(request: Request):
	# Slack signature verification is enforced by the Bolt handler when a signing secret is set.
	if not settings.slack_signing_secret:
		is_slack_request = bool(
			request.headers.get("X-Slack-Signature")
			and request.headers.get("X-Slack-Request-Timestamp")
		)
		await slack_relay.log_error(
			":warning: /slack/events received without SLACK_SIGNING_SECRET; cannot verify signature."
		)
		if is_slack_request:
			# Acknowledge Slack to avoid retries even when we cannot verify.
			return Response(status_code=status.HTTP_200_OK)
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Server missing SLACK_SIGNING_SECRET; configure it in the environment",
		)
	try:
		response = await slack_relay.handler.handle(request)
		request_body = None
		try:
			request_body = await request.body()
		except Exception:
			request_body = None
		response, response_body = await _extract_response_body(response)
		if response.status_code >= 400:
			await slack_relay.log_error(
				_truncate_message(
					f":warning: {request.method} /slack/events -> {response.status_code}"
				),
				detail=_join_detail(
					_format_request_snapshot(request, request_body),
					_format_response_snapshot(response_body or ""),
				),
			)
		return response
	except Exception as exc:
		request_body = None
		try:
			request_body = await request.body()
		except Exception:
			request_body = None
		await slack_relay.log_error(
			f":warning: /slack/events handler error: {exc}",
			detail=_join_detail(
				_format_request_snapshot(request, request_body),
				_format_traceback(traceback.format_exc()),
			),
		)
		# Always acknowledge to prevent Slack retries from leaking errors to callers.
		return Response(status_code=status.HTTP_200_OK)

def main() -> None:
	uvicorn.run(app, host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
	main()

