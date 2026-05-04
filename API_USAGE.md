# Master Alchemist — API Usage

This document explains how to call the Master Alchemist API (`POST /ship`), what data the API expects from callers, and how Bearer authorization works.

## Endpoints

- `GET /healthz` — health check, returns `{ "status": "ok" }`.
- `POST /slack/events` — Slack callback endpoint (used when configuring Slack Request URLs).

## Authorization — Bearer token

The API uses a simple bearer token in the `Authorization` request header.

- Header format: `Authorization: Bearer <token>`
- The server checks `<token>` against the shared secret configured by the operator (stored in the server environment as `AUTH_BEARER_TOKEN`).

Example curl with bearer token:

```bash
curl -X POST http://127.0.0.1:8000/ship \
  -H "Authorization: Bearer replace-me" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"U123ABCD","project_name":"Awesome Widget","project_link":"https://example.com/awesome-widget"}'
```

If the header is missing or the token does not match, the API returns `401 Unauthorized`.

## What to extract from the caller

When you design the client or document the UI/API consumer, ask the user to provide:

- A Slack `user_id` (required) — the person who shipped the project.
- A `project_name` (required).
- A `project_link` (required).

## Response

On success, the API returns a JSON object like:

```json
{
  "public": { "ok": true, "channel": "C12345678", "ts": "168..." },
  "dm": { "ok": true, "channel": "D12345678", "ts": "168..." }
}
```

If `SHIP_CHANNEL_ID` is missing, you'll get a `400 Bad Request`. On bad or missing authorization, you'll get `401 Unauthorized`.

## Notes

- Keep the bearer token secret — anyone with the token can post messages to your Slack workspace via this API.

## POST /ship — project submission shortcut

This endpoint is designed for "shipping" a project. It sends a public notification to the configured ship channel and a DM to the submitting user.

- Endpoint: `POST /ship`
- Required body JSON:
  - `user_id` (string): Slack user ID of the submitter (e.g. `U123ABCD`).
  - `project_name` (string): Name of the project.
  - `project_link` (string): A URL to the submitted project.

Example:

```json
{
  "user_id": "U123ABCD",
  "project_name": "Awesome Widget",
  "project_link": "https://example.com/awesome-widget"
}
```

Behavior:
- Posts to the `SHIP_CHANNEL_ID` configured in the environment with the message:
  - `<@USERID> Your *{project name}* has been submitted for review.`
- Sends a direct message to the user with a short confirmation and a link:
  - Heading: `Project Submitted for Review`
  - Body: `Your project *{project name}* has been submitted for review.`
  - A line labeled `View Your Project` that points to the provided project URL.

If the server has no `SHIP_CHANNEL_ID` configured, `/ship` returns `400 Bad Request`.

## Environment variables: direct vs .env fallback

You can provide configuration by exporting environment variables directly (recommended):

```bash
export AUTH_BEARER_TOKEN=replace-me
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_SIGNING_SECRET=...
export SHIP_CHANNEL_ID=C12345678
```

When the service starts it first prefers the real environment. If a variable is not present, the app will look for a local `.env` file and use values from there for any keys that are missing. This keeps local development convenient while keeping production configurations secure in the real environment.

If you want, I can add a small client snippet (Python/JS) that demonstrates calling `/ship` with the required header.