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
					"text": f"Your project *{project_name}* has been submitted for review.",
				},
			},
			{
				"type": "section",
				"text": {
					"type": "mrkdwn",
					"text": f"<{project_link}|View You Project>",
				},
			},
		]

		resp = await self.app.client.chat_postMessage(
			channel=channel,
			text=f"Your project {project_name} has been submitted for review.",
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


def main() -> None:
	uvicorn.run(app, host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
	main()
