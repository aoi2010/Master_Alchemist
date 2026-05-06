import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp


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
	app_name: str = os.getenv("APP_NAME", "Master Alchemist")
	api_host: str = os.getenv("API_HOST", "0.0.0.0")
	api_port: int = int(os.getenv("API_PORT", "8000"))
	auth_bearer_token: str = os.getenv("AUTH_BEARER_TOKEN", "")
	slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
	slack_signing_secret: str = os.getenv("SLACK_SIGNING_SECRET", "")
	# Optional ship channel id for the /ship endpoint; can be set as an environment variable
	ship_channel_id: str = os.getenv("SHIP_CHANNEL_ID", "")
	# Optional logging channel id for periodic heartbeat messages; can be set as an environment variable
	logging_channel_id: str = os.getenv("LOGGING_CHANNEL_ID", "")


class ShipPayload(BaseModel):
	user_id: str = Field(min_length=1)
	project_name: str = Field(min_length=1)
	project_link: str = Field(min_length=1)


class ReviewAcceptPayload(BaseModel):
	user_id: str = Field(min_length=1, description="Slack user ID of project submitter")
	project_name: str = Field(min_length=1)
	project_link: str = Field(min_length=1)
	reviewer_id: str = Field(min_length=1, description="Slack user ID of reviewer")
	feedback: str = Field(min_length=1, max_length=2000)
	currencies: str = Field(min_length=1, description="Currency reward string e.g. '100 Gold, 50 Silver'")


class ReviewRejectPayload(BaseModel):
	user_id: str = Field(min_length=1, description="Slack user ID of project submitter")
	project_name: str = Field(min_length=1)
	project_link: str = Field(min_length=1)
	reviewer_id: str = Field(min_length=1, description="Slack user ID of reviewer")
	feedback: str = Field(min_length=1, max_length=2000)


class FulfillOrderPayload(BaseModel):
	user_id: str = Field(min_length=1, description="Slack user ID of order recipient")
	order_id: str = Field(min_length=1)
	item_name: str = Field(min_length=1)
	qty: str = Field(min_length=1)
	cost: str = Field(min_length=1)


class FulfillFullfilledPayload(BaseModel):
	user_id: str = Field(min_length=1, description="Slack user ID of order recipient")
	order_id: str = Field(min_length=1)
	item_name: str = Field(min_length=1)
	qty: str = Field(min_length=1)
	cost: str = Field(min_length=1)
	fulfilled_by: str = Field(min_length=1)
	tracking_details: str = Field(min_length=1)


class CustomMessagePayload(BaseModel):
	target_id: str = Field(min_length=1, description="Slack user ID or channel ID")
	message: str = Field(min_length=1, max_length=4000)


class SlackDispatchResult(BaseModel):
	ok: bool
	channel: str
	ts: str | None = None


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
	def __init__(self, settings: Settings) -> None:
		self.settings = settings
		self.app = AsyncApp(
			token=settings.slack_bot_token,
			signing_secret=settings.slack_signing_secret,
		)
		self.handler = AsyncSlackRequestHandler(self.app)

	async def _resolve_target_channel(self, target_id: str) -> str:
		if target_id.startswith("U"):
			conv = await self.app.client.conversations_open(users=target_id)
			return conv["channel"]["id"]
		if target_id.startswith("C") or target_id.startswith("G"):
			return target_id
		raise HTTPException(status_code=400, detail="target_id must start with U for DM or C/G for channel")

	@staticmethod
	def _format_order_fields(order_id: str, item_name: str, qty: str, cost: str) -> list[dict[str, str]]:
		return [
			{"type": "mrkdwn", "text": f"*Order ID:* {order_id}"},
			{"type": "mrkdwn", "text": f"*Item:* {item_name}"},
			{"type": "mrkdwn", "text": f"*Quantity:* {qty}"},
			{"type": "mrkdwn", "text": f"*Total:* {cost}"},
		]

	async def _send_order_update_dm(
		self,
		user_id: str,
		headline: str,
		status_line: str,
		order_id: str,
		item_name: str,
		qty: str,
		cost: str,
		closing_line: str,
		extra_lines: list[str] | None = None,
	) -> SlackDispatchResult:
		channel = await self._resolve_target_channel(user_id)
		blocks = [
			{
				"type": "header",
				"text": {"type": "plain_text", "text": headline},
			},
			{
				"type": "section",
				"text": {"type": "mrkdwn", "text": f"*Your Order Status:* {status_line}"},
			},
			{
				"type": "section",
				"text": {"type": "mrkdwn", "text": "*Order Details:*"},
				"fields": self._format_order_fields(order_id, item_name, qty, cost),
			},
			{
				"type": "divider",
			},
			{
				"type": "context",
				"elements": [
					{"type": "mrkdwn", "text": closing_line},
					*([{"type": "mrkdwn", "text": line} for line in (extra_lines or [])]),
				],
			},
		]

		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=f"{headline} {order_id}",
			blocks=blocks,
		)

		return SlackDispatchResult(ok=bool(resp["ok"]), channel=channel, ts=resp.get("ts"))

	async def send_custom_message(self, target_id: str, message: str) -> SlackDispatchResult:
		channel = await self._resolve_target_channel(target_id)
		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=message,
			blocks=[
				{
					"type": "section",
					"text": {"type": "mrkdwn", "text": message},
				},
			],
		)
		return SlackDispatchResult(ok=bool(resp["ok"]), channel=channel, ts=resp.get("ts"))

	async def send_dm(self, user_id: str, project_name: str, project_link: str) -> SlackDispatchResult:
		# Open a DM channel with the user (conversations_open returns channel info)
		conv = await self.app.client.conversations_open(users=user_id)
		channel = conv["channel"]["id"]

		blocks = [
			{
				"type": "header",
				"text": {"type": "plain_text", "text": "Project Submitted for Review"},
			},
			{
				"type": "section",
				"text": {
					"type": "mrkdwn",
					"text": f"Your project <{project_link}|*{project_name}*> has been submitted for review.",
				},
			},
		]

		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=f"Your project {project_name} has been submitted for review.",
			blocks=blocks,
		)

		return SlackDispatchResult(ok=bool(resp["ok"]), channel=channel, ts=resp.get("ts"))


	async def post_review_accept(self, user_id: str, project_name: str, project_link: str, reviewer_id: str, feedback: str, currencies: str) -> dict[str, Any]:
		"""Post acceptance review with custom reviewer profile in channel and detailed message in DM."""
		ship_channel = self.settings.ship_channel_id
		if not ship_channel:
			raise HTTPException(status_code=400, detail="SHIP_CHANNEL_ID not configured")

		# Fetch reviewer's profile for name and avatar
		user_info = await self.app.client.users_info(user=reviewer_id)
		user_profile = user_info.get("user", {})
		reviewer_name = user_profile.get("profile", {}).get("display_name") or user_profile.get("real_name", "Unknown")
		reviewer_avatar = user_profile.get("profile", {}).get("image_192") or user_profile.get("profile", {}).get("image_512", "")

		# Post to ship channel spoofed as reviewer
		channel_message = f"<@{user_id}> Your *{project_name}* has been reviewed. Please check your DM by <@U0B18V07GQ3> for details."
		resp = await self.app.client.chat_postMessage(
			channel=ship_channel,
			text=channel_message,
			username=reviewer_name,
			icon_url=reviewer_avatar,
		)

		# Send detailed review to DM
		await self.send_review_dm_accept(user_id, project_name, project_link, reviewer_name, reviewer_id, feedback, currencies)

		return {"ok": bool(resp["ok"]), "channel": ship_channel, "ts": resp.get("ts")}

	async def post_review_reject(self, user_id: str, project_name: str, project_link: str, reviewer_id: str, feedback: str) -> dict[str, Any]:
		"""Post rejection review with custom reviewer profile in channel and detailed message in DM."""
		ship_channel = self.settings.ship_channel_id
		if not ship_channel:
			raise HTTPException(status_code=400, detail="SHIP_CHANNEL_ID not configured")

		# Fetch reviewer's profile for name and avatar
		user_info = await self.app.client.users_info(user=reviewer_id)
		user_profile = user_info.get("user", {})
		reviewer_name = user_profile.get("profile", {}).get("display_name") or user_profile.get("real_name", "Unknown")
		reviewer_avatar = user_profile.get("profile", {}).get("image_192") or user_profile.get("profile", {}).get("image_512", "")

		# Post to ship channel spoofed as reviewer
		channel_message = f"<@{user_id}> Your *{project_name}* has been reviewed. Please check your DM by <@U0B18V07GQ3> for details."
		resp = await self.app.client.chat_postMessage(
			channel=ship_channel,
			text=channel_message,
			username=reviewer_name,
			icon_url=reviewer_avatar,
		)

		# Send detailed review to DM
		await self.send_review_dm_reject(user_id, project_name, project_link, reviewer_name, reviewer_id, feedback)

		return {"ok": bool(resp["ok"]), "channel": ship_channel, "ts": resp.get("ts")}

	async def send_review_dm_accept(self, user_id: str, project_name: str, project_link: str, reviewer_name: str, reviewer_id: str, feedback: str, currencies: str) -> SlackDispatchResult:
		"""Send detailed acceptance review to DM as mrkdwn blocks so it can be edited later."""
		conv = await self.app.client.conversations_open(users=user_id)
		channel = conv["channel"]["id"]

		blocks = [
			{
				"type": "header",
				"text": {"type": "plain_text", "text": ":tada: Project Reviewed. Congratulations!"},
			},
			{
				"type": "section",
				"text": {
					"type": "mrkdwn",
					"text": f"Nice Master Cleric <@{reviewer_id}> has been impressed by your project *{project_name}*.",
				},
			},
			{
				"type": "section",
				"text": {
					"type": "mrkdwn",
					"text": f"*Acceptance Feedback:* {feedback}\n\n*You get:* {currencies}",
				},
			},
			{
				"type": "divider",
			},
			{
				"type": "context",
				"elements": [
					{"type": "mrkdwn", "text": "Keep up the great work and continue to refine your alchemical skills! :alchemize:"},
				],
			},
			{
				"type": "actions",
				"elements": [
					{
						"type": "button",
						"text": {"type": "plain_text", "text": "View Project"},
						"url": project_link,
					}
				],
			},
		]

		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=f"{reviewer_name} reviewed {project_name}",
			blocks=blocks,
		)

		return SlackDispatchResult(ok=bool(resp["ok"]), channel=channel, ts=resp.get("ts"))

	async def send_fulfill_pending_dm(self, payload: FulfillOrderPayload) -> SlackDispatchResult:
		return await self._send_order_update_dm(
			user_id=payload.user_id,
			headline=f":shopping_trolley: Order #{payload.order_id} Update",
			status_line="Pending",
			order_id=payload.order_id,
			item_name=payload.item_name,
			qty=payload.qty,
			cost=payload.cost,
			closing_line="Thanking you for participating in Alchemize with us! :alchemize:",
			extra_lines=None,
		)

	async def send_fulfill_approved_dm(self, payload: FulfillOrderPayload) -> SlackDispatchResult:
		return await self._send_order_update_dm(
			user_id=payload.user_id,
			headline=f":white_check_mark: Order #{payload.order_id} Approved!",
			status_line="Approved. Pending Fulfillment.",
			order_id=payload.order_id,
			item_name=payload.item_name,
			qty=payload.qty,
			cost=payload.cost,
			closing_line="We'll notify you when your order ships. Thank You for your patience! :alchemize:",
			extra_lines=None,
		)

	async def send_fulfill_fullfilled_dm(self, payload: FulfillFullfilledPayload) -> SlackDispatchResult:
		channel = await self._resolve_target_channel(payload.user_id)
		blocks = [
			{
				"type": "header",
				"text": {"type": "plain_text", "text": f":tada: Order #{payload.order_id} Fulfilled!"},
			},
			{
				"type": "section",
				"text": {"type": "mrkdwn", "text": "*Your Order Status:* Your order has been fulfilled and is on its way! Make sure to show off what you do with it in <#C07UMRYJ1LH> when it arrives!"},
			},
			{
				"type": "section",
				"text": {"type": "mrkdwn", "text": "*Order Details:*"},
				"fields": self._format_order_fields(payload.order_id, payload.item_name, payload.qty, payload.cost)
				+ [
					{"type": "mrkdwn", "text": f"*Fulfilled By:* {payload.fulfilled_by}"},
					{"type": "mrkdwn", "text": f"*Tracking Details:* :package: {payload.tracking_details}"},
				],
			},
			{
				"type": "divider",
			},
			{
				"type": "context",
				"elements": [
					{"type": "mrkdwn", "text": "Thanking you for participating in Alchemize with us! :alchemize:"},
				],
			},
		]

		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=f"Order #{payload.order_id} Fulfilled!",
			blocks=blocks,
		)

		return SlackDispatchResult(ok=bool(resp["ok"]), channel=channel, ts=resp.get("ts"))

	async def send_review_dm_reject(self, user_id: str, project_name: str, project_link: str, reviewer_name: str, reviewer_id: str, feedback: str) -> SlackDispatchResult:
		"""Send detailed rejection review to DM as mrkdwn blocks so it can be edited later."""
		conv = await self.app.client.conversations_open(users=user_id)
		channel = conv["channel"]["id"]

		blocks = [
			{
				"type": "header",
				"text": {"type": "plain_text", "text": ":x: Oof! Your project needs some changes..."},
			},
			{
				"type": "section",
				"text": {
					"type": "mrkdwn",
					"text": f"Nice Master Cleric <@{reviewer_id}> has reviewed your project *{project_name}*."
				},
			},
			{
				"type": "section",
				"text": {
					"type": "mrkdwn",
					"text": f"*Rejection Feedback:* {feedback}"
				},
			},
			{
				"type": "divider",
			},
			{
				"type": "context",
				"elements": [
					{"type": "mrkdwn", "text": "Don't give up! Review the feedback, make improvements, and ship again! :muscle:"},
				],
			},
			{
				"type": "actions",
				"elements": [
					{
						"type": "button",
						"text": {"type": "plain_text", "text": "View Project"},
						"url": project_link,
					},
				],
			},
		]

		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=f"{reviewer_name} reviewed {project_name}",
			blocks=blocks,
		)

		return SlackDispatchResult(ok=bool(resp["ok"]), channel=channel, ts=resp.get("ts"))


async def bot_heartbeat_task(settings: Settings, slack_relay: SlackRelay) -> None:
	"""Background task that sends a heartbeat message every 5 minutes to the logging channel."""
	if not settings.logging_channel_id:
		return

	while True:
		try:
			await asyncio.sleep(300)  # 5 minutes
			resp = await slack_relay.app.client.chat_postMessage(
				channel=settings.logging_channel_id,
				text=":alchemist: Bot is online!",
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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
	return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request):
	return await slack_relay.handler.handle(request)


@app.post("/ship")
async def ship_project(
	payload: ShipPayload,
	_: None = Depends(verify_bearer_token),
) -> dict[str, Any]:
	"""Handle a project submission (ship) from a user.

	Sends a public message to the configured ship channel and DM's the submitting user.
	"""
	settings = get_settings()
	ship_channel = settings.ship_channel_id
	if not ship_channel:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Server missing SHIP_CHANNEL_ID; configure it in the environment",
		)

	# Public channel notification: ping the user and mention the project (bold project name)
	public_message = f"<@{payload.user_id}> Your *{payload.project_name}* has been submitted for review."
	public_resp = await slack_relay.app.client.chat_postMessage(
		channel=ship_channel,
		text=public_message,
	)

	# Direct message to the submitter
	dm_resp = await slack_relay.send_dm(payload.user_id, payload.project_name, payload.project_link)

	return {
		"public": {"ok": bool(public_resp["ok"]), "channel": ship_channel, "ts": public_resp.get("ts")},
		"dm": dm_resp.model_dump(),
	}


@app.post("/review-accept")
async def review_accept(
	payload: ReviewAcceptPayload,
	_: None = Depends(verify_bearer_token),
) -> dict[str, Any]:
	"""Handle a positive review for a submitted project.

	Posts a review message to the ship channel with custom reviewer profile (name/avatar)
	and sends DM notification to the project submitter.
	"""
	review_response = await slack_relay.post_review_accept(
		user_id=payload.user_id,
		project_name=payload.project_name,
		project_link=payload.project_link,
		reviewer_id=payload.reviewer_id,
		feedback=payload.feedback,
		currencies=payload.currencies,
	)
	return {"ok": review_response["ok"], "channel": review_response["channel"], "ts": review_response.get("ts")}


@app.post("/review-reject")
async def review_reject(
	payload: ReviewRejectPayload,
	_: None = Depends(verify_bearer_token),
) -> dict[str, Any]:
	"""Handle a negative review for a submitted project.

	Posts a review message to the ship channel with custom reviewer profile (name/avatar)
	and sends DM notification to the project submitter.
	"""
	review_response = await slack_relay.post_review_reject(
		user_id=payload.user_id,
		project_name=payload.project_name,
		project_link=payload.project_link,
		reviewer_id=payload.reviewer_id,
		feedback=payload.feedback,
	)
	return {"ok": review_response["ok"], "channel": review_response["channel"], "ts": review_response.get("ts")}


@app.post("/fulfill_pending")
async def fulfill_pending(
	payload: FulfillOrderPayload,
	_: None = Depends(verify_bearer_token),
) -> dict[str, Any]:
	response = await slack_relay.send_fulfill_pending_dm(payload)
	return {"ok": response.ok, "channel": response.channel, "ts": response.ts}


@app.post("/fulfill_approved")
async def fulfill_approved(
	payload: FulfillOrderPayload,
	_: None = Depends(verify_bearer_token),
) -> dict[str, Any]:
	response = await slack_relay.send_fulfill_approved_dm(payload)
	return {"ok": response.ok, "channel": response.channel, "ts": response.ts}


@app.post("/fulfill_fullfilled")
async def fulfill_fullfilled(
	payload: FulfillFullfilledPayload,
	_: None = Depends(verify_bearer_token),
) -> dict[str, Any]:
	response = await slack_relay.send_fulfill_fullfilled_dm(payload)
	return {"ok": response.ok, "channel": response.channel, "ts": response.ts}


@app.post("/custom")
async def custom_message(
	payload: CustomMessagePayload,
	_: None = Depends(verify_bearer_token),
) -> dict[str, Any]:
	response = await slack_relay.send_custom_message(payload.target_id, payload.message)
	return {"ok": response.ok, "channel": response.channel, "ts": response.ts}


def main() -> None:
	uvicorn.run(app, host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
	main()

