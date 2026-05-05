import os
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
				"text": {"type": "plain_text", "text": "Project Reviewed. Congratulations!"},
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
					"text": f"*Acceptance Feedback:*{feedback}\n\n*You get:* {currencies}",
				},
			},
			{
				"type": "divider",
			},
			{
				"type": "context",
				"elements": [
					{"type": "mrkdwn", "text": "Keep up the great work and continue to refine your alchemical skills!"},
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

	async def send_review_dm_reject(self, user_id: str, project_name: str, project_link: str, reviewer_name: str, reviewer_id: str, feedback: str) -> SlackDispatchResult:
		"""Send detailed rejection review to DM as mrkdwn blocks so it can be edited later."""
		conv = await self.app.client.conversations_open(users=user_id)
		channel = conv["channel"]["id"]

		blocks = [
			{
				"type": "header",
				"text": {"type": "plain_text", "text": "Oof! Your project needs some changes..."},
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
					"text": f"*Rejection Feedback:*{feedback}"
				},
			},
			{
				"type": "divider",
			},
			{
				"type": "context",
				"elements": [
					{"type": "mrkdwn", "text": "Don't give up! Review the feedback, make improvements, and ship again!"},
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


settings = get_settings()
slack_relay = SlackRelay(settings)
app = FastAPI(title=settings.app_name)


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
		"dm": dm_resp.dict(),
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


def main() -> None:
	uvicorn.run(app, host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
	main()

