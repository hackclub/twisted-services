import asyncio
import aiohttp
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import traceback
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient
from starlette.responses import RedirectResponse
from starlette.routing import Match

from twisted_services.storage import (
	OAuthSession,
	SlackInstallation,
	build_database,
	decrypt_token,
	encrypt_token,
	find_installation,
	find_session,
	initialize_database,
	new_session_token,
	session_digest,
)


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
	oauth_client_id: str = os.getenv("SLACK_CLIENT_ID", "")
	oauth_client_secret: str = os.getenv("SLACK_CLIENT_SECRET", "")
	oauth_redirect_uri: str = os.getenv("SLACK_OAUTH_REDIRECT_URI", "")
	oauth_state_secret: str = os.getenv("OAUTH_STATE_SECRET", "")
	oauth_session_hours: int = int(os.getenv("OAUTH_SESSION_HOURS", "24"))
	oauth_cookie_secure: bool = os.getenv("OAUTH_COOKIE_SECURE", "true").lower() == "true"
	public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
	r2_endpoint: str = os.getenv("R2_ENDPOINT", "")
	r2_access_key_id: str = os.getenv("R2_ACCESS_KEY_ID", "")
	r2_secret_access_key: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
	r2_bucket: str = os.getenv("R2_BUCKET", "")
	r2_public_base_url: str = os.getenv("R2_PUBLIC_BASE_URL", "https://twisted.hackclub-assets.com")

class SlackDispatchResult(BaseModel):
	ok: bool
	channel: str
	ts: str | None = None


class OAuthSendRequest(BaseModel):
	target_id: str = Field(min_length=1)
	message: str = Field(min_length=1, max_length=40000)


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


def _oauth_state(settings: Settings) -> str:
	if not settings.oauth_state_secret:
		raise HTTPException(status_code=503, detail="Server missing OAUTH_STATE_SECRET")
	payload = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
	signature = hmac.new(settings.oauth_state_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
	return f"{payload}.{signature}"


def _verify_oauth_state(value: str, settings: Settings) -> bool:
	parts = value.split(".")
	if len(parts) != 3 or not settings.oauth_state_secret:
		return False
	payload = ".".join(parts[:2])
	try:
		created = int(parts[0])
	except ValueError:
		return False
	if abs(time.time() - created) > 600:
		return False
	expected = hmac.new(settings.oauth_state_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected, parts[2])


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
		# The app uses custom OAuth routes below, while Bolt itself uses one
		# configured bot token. Hide OAuth env vars during construction so
		# Bolt does not auto-enable multi-workspace installation lookup.
		oauth_environment = {
			key: os.environ.pop(key, None)
			for key in ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET")
		}
		try:
			self.app = AsyncApp(
				token=settings.slack_bot_token,
				signing_secret=settings.slack_signing_secret,
			)
		finally:
			for key, value in oauth_environment.items():
				if value is not None:
					os.environ[key] = value
		self.handler = AsyncSlackRequestHandler(self.app)
		self._register_default_handlers()

	def _register_default_handlers(self) -> None:
		@self.app.command("/twisted-ping")
		async def handle_twisted_ping(ack, command, client, say) -> None:
			await ack()
			await self._process_twisted_ping(command, client, say)

		@self.app.command("/twistedping")
		async def handle_twistedping_alias(ack, command, client, say) -> None:
			await ack()
			await self._process_twisted_ping(command, client, say)

		@self.app.command("/twisted-oauth")
		async def handle_twisted_oauth(ack) -> None:
			base_url = self.settings.public_base_url or self.settings.oauth_redirect_uri.removesuffix("/oauth/callback").rstrip("/")
			if not base_url or not self.settings.oauth_client_id:
				await ack(response_type="ephemeral", text="OAuth is not configured. Set SLACK_CLIENT_ID and SLACK_OAUTH_REDIRECT_URI first.")
				return
			await ack(response_type="ephemeral", text=f"<{base_url}/oauth|Connect your Slack account> to enable Send as User.")

		@self.app.shortcut("edit_twisted_message")
		async def handle_edit_shortcut(ack, shortcut, client) -> None:
			await ack()
			await self._open_edit_modal(shortcut, client)

		@self.app.view("twisted_ping_modal")
		async def handle_ping_submission(ack, body, view, client) -> None:
			await ack()
			await self._submit_ping_modal(body, view, client)

		@self.app.view("twisted_edit_modal")
		async def handle_edit_submission(ack, body, view, client) -> None:
			await ack()
			await self._submit_edit_modal(body, view, client)

		@self.app.event("app_mention")
		async def handle_message_events(body, client, logger) -> None:
			event = body.get("event", {})
			logger.info(f"Message event received: {event.get('type')}")
			if event.get("files"):
				await self._upload_mentioned_files(event, client)

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
		"""Authorize the command before opening the composer modal."""
		try:
			target_channel = command["channel_id"]
			if target_channel not in self.TWISTED_PING_EXEMPT_CHANNELS:
				is_cm = await self._is_channel_manager(target_channel, command["user_id"])
				if not is_cm:
					await client.chat_postEphemeral(
						channel=command["channel_id"],
						user=command["user_id"],
						text=":loll:You are not a channel manager. Only channel managers can use `/twisted-ping` in this channel.",
					)
					return
			await client.views_open(trigger_id=command["trigger_id"], view=self._ping_modal(command))
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
	def _ping_modal(command: dict[str, Any]) -> dict[str, Any]:
		return {
			"type": "modal", "callback_id": "twisted_ping_modal",
			"private_metadata": json.dumps({"channel_id": command["channel_id"], "team_id": command.get("team_id", ""), "user_id": command["user_id"]}),
			"title": {"type": "plain_text", "text": "Twisted message"}, "submit": {"type": "plain_text", "text": "Send"}, "close": {"type": "plain_text", "text": "Cancel"},
			"blocks": [
				{"type": "input", "block_id": "message", "label": {"type": "plain_text", "text": "Message"}, "element": {"type": "plain_text_input", "action_id": "value", "multiline": True, "max_length": 3000}},
				{"type": "input", "block_id": "send_as", "label": {"type": "plain_text", "text": "Send as"}, "element": {"type": "radio_buttons", "action_id": "value", "options": [{"text": {"type": "plain_text", "text": "User"}, "value": "user"}, {"text": {"type": "plain_text", "text": "Twisted - YSWS"}, "value": "anon"}], "initial_option": {"text": {"type": "plain_text", "text": "Twisted - YSWS"}, "value": "anon"}}},
				{"type": "input", "block_id": "schedule", "optional": True, "label": {"type": "plain_text", "text": "Schedule message"}, "element": {"type": "checkboxes", "action_id": "enabled", "options": [{"text": {"type": "plain_text", "text": "Send later"}, "value": "yes"}]}},
				{"type": "input", "block_id": "schedule_time", "optional": True, "label": {"type": "plain_text", "text": "When"}, "element": {"type": "datetimepicker", "action_id": "value"}},
			],
		}

	@staticmethod
	def _rich_text_to_mrkdwn(value: dict[str, Any]) -> str:
		if isinstance(value, str):
			return value.strip()
		if not isinstance(value, dict):
			return ""
		for nested_key in ("value", "rich_text_value"):
			nested = value.get(nested_key)
			if nested is not None and nested is not value:
				return SlackRelay._rich_text_to_mrkdwn(nested)

		def render(elements: list[dict[str, Any]]) -> str:
			parts: list[str] = []
			for element in elements:
				kind = element.get("type")
				if kind == "text":
					text = element.get("text", "")
					style = element.get("style", {})
					if style.get("bold"): text = f"*{text}*"
					if style.get("italic"): text = f"_{text}_"
					if style.get("strike"): text = f"~{text}~"
					if style.get("code"): text = f"`{text}`"
					parts.append(text)
				elif kind == "link":
					parts.append(f"<{element.get('url', '')}|{element.get('text') or element.get('url', '')}>")
				elif kind == "user":
					parts.append(f"<@{element.get('user_id')}>")
				elif kind == "broadcast":
					target = element.get("range", "channel")
					if target == "everyone": target = "channel"
					parts.append(f"<!{target}|{target}>")
				elif kind == "emoji":
					parts.append(f":{element.get('name') or element.get('unicode') or ''}:")
				elif kind in {"rich_text_section", "rich_text_preformatted", "rich_text_quote"}:
					content = render(element.get("elements", []))
					if kind == "rich_text_preformatted": content = f"```{content}```"
					if kind == "rich_text_quote": content = "\n".join(f"> {line}" for line in content.splitlines())
					parts.append(content)
				elif kind == "rich_text_list":
					style = element.get("style", "bullet")
					items = []
					for index, item in enumerate(element.get("elements", []), start=1):
						prefix = f"{index}. " if style == "ordered" else "• "
						items.append(prefix + render(item.get("elements", [])))
					parts.append("\n".join(items))
				if kind in {"rich_text_section", "rich_text_preformatted", "rich_text_quote", "rich_text_list"}:
					parts.append("\n")
			return "".join(parts)

		return render(value.get("elements", [])).strip()

	@staticmethod
	def _view_value(view: dict[str, Any], block_id: str, action_id: str = "value") -> Any:
		return view.get("state", {}).get("values", {}).get(block_id, {}).get(action_id, {})

	async def _submit_ping_modal(self, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
		metadata = json.loads(view.get("private_metadata", "{}"))
		rich_value = self._view_value(view, "message")
		message = self._rich_text_to_mrkdwn(rich_value) or "(no message)"
		send_as = self._view_value(view, "send_as").get("selected_option", {}).get("value", "anon")
		schedule_enabled = bool(self._view_value(view, "schedule", "enabled").get("selected_options"))
		schedule_value = self._view_value(view, "schedule_time").get("selected_date_time")
		message_kwargs: dict[str, Any] = {
			"channel": metadata["channel_id"],
			"text": message,
			"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message, "verbatim": False}}],
		}
		identity_kwargs: dict[str, Any] = {}
		if send_as == "user":
			user_client = await self._oauth_client(metadata["team_id"], metadata["user_id"])
			if user_client is not None:
				client = user_client
			else:
				profile = (await self.app.client.users_info(user=metadata["user_id"])).get("user", {}).get("profile", {})
				identity_kwargs.update(username=profile.get("display_name") or profile.get("real_name", "Unknown"), icon_url=profile.get("image_512") or profile.get("image_original", ""))
		else:
			identity_kwargs.update(username="Twisted - YSWS", icon_url="https://docs.twisted.hackclub.com/assets/favicon.png")
		message_kwargs.update(identity_kwargs)
		if schedule_enabled:
			if not schedule_value:
				await client.chat_postEphemeral(channel=metadata["channel_id"], user=metadata["user_id"], text="Choose a date and time for the scheduled message.")
				return
			await client.chat_scheduleMessage(**message_kwargs, post_at=int(schedule_value))
		else:
			await client.chat_postMessage(**message_kwargs)

	async def _oauth_client(self, team_id: str, user_id: str) -> AsyncWebClient | None:
		if not team_id: return None
		async with db_sessionmaker() as database:
			installation = await find_installation(database, team_id, user_id)
		if installation is None: return None
		return AsyncWebClient(token=decrypt_token(installation.token_ciphertext))

	async def _can_edit_bot_message(self, channel_id: str, user_id: str) -> bool:
		return channel_id == "C0AD3HRV4F8" or await self._is_channel_manager(channel_id, user_id)

	async def _open_edit_modal(self, shortcut: dict[str, Any], client: Any) -> None:
		channel = shortcut.get("channel", {})
		message = shortcut.get("message", {})
		channel_id = channel.get("id", "")
		user_id = shortcut.get("user", {}).get("id", "")
		if not message.get("bot_id") and message.get("subtype") != "bot_message":
			await client.chat_postEphemeral(channel=channel_id, user=user_id, text="Only messages sent by Twisted can be edited.")
			return
		if not await self._can_edit_bot_message(channel_id, user_id):
			await client.chat_postEphemeral(channel=channel_id, user=user_id, text="Only a channel manager or a member of #twisters can edit Twisted messages.")
			return
		initial = {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": message.get("text", "")}]}]}
		await client.views_open(
			trigger_id=shortcut["trigger_id"],
			view={
				"type": "modal", "callback_id": "twisted_edit_modal",
				"private_metadata": json.dumps({"channel_id": channel_id, "message_ts": message.get("ts", "")}),
				"title": {"type": "plain_text", "text": "Edit Twisted message"}, "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Cancel"},
				"blocks": [{"type": "input", "block_id": "message", "label": {"type": "plain_text", "text": "Message"}, "element": {"type": "rich_text_input", "action_id": "value", "initial_value": initial}}],
			},
		)

	async def _submit_edit_modal(self, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
		metadata = json.loads(view.get("private_metadata", "{}"))
		user_id = body.get("user", {}).get("id", "")
		if not await self._can_edit_bot_message(metadata["channel_id"], user_id):
			return
		message = self._rich_text_to_mrkdwn(self._view_value(view, "message")) or "(no message)"
		await client.chat_update(channel=metadata["channel_id"], ts=metadata["message_ts"], text=message, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}])

	@staticmethod
	def _slugify_filename(filename: str) -> str:
		base, extension = os.path.splitext(filename)
		slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "file"
		return f"{slug}{extension.lower()}"

	async def _upload_mentioned_files(self, event: dict[str, Any], client: Any) -> None:
		settings = self.settings
		if event.get("channel") != "C0AD3HRV4F8" or not all((settings.r2_endpoint, settings.r2_access_key_id, settings.r2_secret_access_key, settings.r2_bucket)):
			return
		channel_id = event.get("channel", "")
		thread_ts = event.get("thread_ts") or event.get("ts")
		for file_info in event.get("files", []):
			status = await client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=f"Uploading `{file_info.get('name', 'file')}`...")
			try:
				async with aiohttp.ClientSession() as session:
					async with session.get(file_info["url_private_download"], headers={"Authorization": f"Bearer {settings.slack_bot_token}"}) as response:
						response.raise_for_status()
						content = await response.read()
				key = f"{secrets.token_urlsafe(12)}/{self._slugify_filename(file_info.get('name', 'file'))}"
				await asyncio.to_thread(self._put_r2_object, key, content, file_info.get("mimetype", "application/octet-stream"))
				url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
				await client.chat_update(channel=channel_id, ts=status["ts"], text=url, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"Uploaded: <{url}|{file_info.get('name', 'file')}>"}}])
			except Exception as exc:
				await client.chat_update(channel=channel_id, ts=status.get("ts"), text=f"Upload failed: {str(exc)[:200]}")

	def _put_r2_object(self, key: str, content: bytes, content_type: str) -> None:
		import boto3
		r2 = boto3.client("s3", endpoint_url=self.settings.r2_endpoint, aws_access_key_id=self.settings.r2_access_key_id, aws_secret_access_key=self.settings.r2_secret_access_key, region_name="auto")
		r2.put_object(Bucket=self.settings.r2_bucket, Key=key, Body=content, ContentType=content_type)

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
	await initialize_database(db_engine)
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
	await db_engine.dispose()


settings = get_settings()
slack_relay = SlackRelay(settings)
db_engine, db_sessionmaker = build_database()
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


@app.get("/oauth")
async def oauth_login() -> RedirectResponse:
	if not settings.oauth_client_id or not settings.oauth_redirect_uri:
		raise HTTPException(status_code=503, detail="Slack OAuth is not configured")
	state = _oauth_state(settings)
	query = urlencode({
		"client_id": settings.oauth_client_id,
		"redirect_uri": settings.oauth_redirect_uri,
		"user_scope": "chat:write,im:write",
		"state": state,
	})
	return RedirectResponse(f"https://slack.com/oauth/v2/authorize?{query}", status_code=307)


@app.get("/oauth/callback")
async def oauth_callback(request: Request) -> Response:
	if not settings.oauth_client_id or not settings.oauth_client_secret or not settings.oauth_redirect_uri:
		raise HTTPException(status_code=503, detail="Slack OAuth is not configured")
	if not _verify_oauth_state(request.query_params.get("state", ""), settings):
		raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
	code = request.query_params.get("code", "")
	if not code:
		raise HTTPException(status_code=400, detail="Slack OAuth did not return a code")

	try:
		async with aiohttp.ClientSession() as client:
			async with client.post(
				"https://slack.com/api/oauth.v2.access",
				data={
					"client_id": settings.oauth_client_id,
					"client_secret": settings.oauth_client_secret,
					"code": code,
					"redirect_uri": settings.oauth_redirect_uri,
				},
			) as slack_response:
				payload = await slack_response.json()
	except Exception as exc:
		raise HTTPException(status_code=502, detail="Unable to contact Slack OAuth") from exc
	if not payload.get("ok") or not payload.get("authed_user", {}).get("access_token"):
		raise HTTPException(status_code=502, detail="Slack OAuth authorization failed")

	team_id = payload.get("team", {}).get("id")
	slack_user_id = payload.get("authed_user", {}).get("id")
	if not team_id or not slack_user_id:
		raise HTTPException(status_code=502, detail="Slack OAuth response was missing team or user")
	async with db_sessionmaker() as database:
		installation = await find_installation(database, team_id, slack_user_id)
		if installation is None:
			installation = SlackInstallation(
				team_id=team_id,
				slack_user_id=slack_user_id,
				token_ciphertext=encrypt_token(payload["authed_user"]["access_token"]),
			)
			session_token = new_session_token()
			database.add(installation)
		else:
			installation.token_ciphertext = encrypt_token(payload["authed_user"]["access_token"])
			session_token = new_session_token()
		database.add(OAuthSession(
			session_hash=session_digest(session_token),
			team_id=team_id,
			slack_user_id=slack_user_id,
			expires_at=datetime.now(ZoneInfo("UTC")) + timedelta(hours=settings.oauth_session_hours),
		))
		await database.commit()
	response = Response(content="Slack OAuth login complete. You can now call /oauth/send.", media_type="text/plain")
	response.set_cookie(
		"twisted_oauth_session",
		session_token,
		max_age=settings.oauth_session_hours * 3600,
		httponly=True,
		secure=settings.oauth_cookie_secure,
		samesite="lax",
	)
	return response


async def oauth_session(request: Request) -> OAuthSession:
	token = request.cookies.get("twisted_oauth_session")
	if not token:
		raise HTTPException(status_code=401, detail="Sign in at /oauth first")
	async with db_sessionmaker() as database:
		session = await find_session(database, token)
	if session is None:
		raise HTTPException(status_code=401, detail="OAuth session is missing or expired")
	return session


@app.get("/oauth/me")
async def oauth_me(session: OAuthSession = Depends(oauth_session)) -> dict[str, str]:
	return {"team_id": session.team_id, "slack_user_id": session.slack_user_id}


@app.post("/oauth/send", response_model=SlackDispatchResult)
async def oauth_send(
	payload: OAuthSendRequest,
	session: OAuthSession = Depends(oauth_session),
) -> SlackDispatchResult:
	async with db_sessionmaker() as database:
		installation = await find_installation(database, session.team_id, session.slack_user_id)
	if installation is None:
		raise HTTPException(status_code=401, detail="Slack OAuth installation not found")
	try:
		client = AsyncWebClient(token=decrypt_token(installation.token_ciphertext))
		target = payload.target_id
		if target.startswith("U"):
			opened = await client.conversations_open(users=target)
			target = opened["channel"]["id"]
		elif not (target.startswith("C") or target.startswith("G")):
			raise HTTPException(status_code=400, detail="target_id must start with U, C, or G")
		result = await client.chat_postMessage(channel=target, text=payload.message)
	except HTTPException:
		raise
	except Exception as exc:
		raise HTTPException(status_code=502, detail="Slack message delivery failed") from exc
	return SlackDispatchResult(ok=bool(result.get("ok")), channel=target, ts=result.get("ts"))


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

