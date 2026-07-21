---
description: Trigger and monitor an app deployment via the-bridge (Docker deployment manager on Unraid). Use when the user asks to deploy, redeploy, push to prod/production, or ship the current app — no confirmation needed for this repo's own app (BRIDGE_APP_ID).
name: the-bridge-deploy
---

# the-bridge deploy

REST API for deploying/monitoring Docker apps on Unraid. This repo (Chain Breakers Podcast)'s app id is stored in `BRIDGE_APP_ID` in this repo's `.env` — use it directly when the user says "deploy this app" / "deploy" with no other app named. Don't ask which app.

**If anything here looks stale** (base URL, ports, token var name), re-resolve via `mcp__homelab-kb-http__query_service("the-bridge")` before proceeding. Full machine-readable contract: `GET http://the-bridge.homelab/api/openapi.json`.

## Auth

Bearer token in `BRIDGE_API_TOKEN`, app id in `BRIDGE_APP_ID`, both stored in this repo's `.env` (not the shell env). Source them per-command, never print the token:

```bash
set -a; source .env; set +a
curl -sS ... -H "Authorization: Bearer $BRIDGE_API_TOKEN"
```

## Flow

1. **Trigger:**
   ```bash
   curl -sS -X POST "http://the-bridge.homelab/api/apps/$BRIDGE_APP_ID/deploy" -H "Authorization: Bearer $BRIDGE_API_TOKEN"
   # => 202 {"deployment_id":N,"app_id":<id>,"status":"pending"}
   ```

2. **Stream logs** — use the `Monitor` tool (not a blocking foreground loop) so the user sees progress without you polling manually. Poll `GET /api/deployments/{id}/log?offset=N`:
   - Response body: log text chunk from `offset`.
   - Headers: `X-Log-Offset` (next offset to request), `X-Deploy-Status` (`pending|running|success|failed`), `X-Deploy-Done` (`true` once terminal).
   - Loop: print body if non-empty, advance offset from `X-Log-Offset`, sleep ~2s, stop when `X-Deploy-Done: true`.

3. **Final status / commit info** (or if the monitor dies, re-check directly):
   ```bash
   curl -sS "http://the-bridge.homelab/api/deployments/{id}" -H "Authorization: Bearer $BRIDGE_API_TOKEN"
   # {id, app_id, app_name, status, commit_sha, commit_message, started_at, finished_at, log_length}
   ```

## Other apps

`GET /api/apps` (bearer) lists `id, name, branch, status, repo_url` — use this if the user names an app other than this repo's own.

## Gotchas

- **zsh reserves `status` as a special read-only variable.** A poll-loop script that assigns to a shell var literally named `status` dies silently with `read-only variable: status` — this kills your monitor script, not the actual deployment (the deploy keeps running server-side regardless). Name the shell var `deploy_status` or `dstatus` instead.
- `X-Deploy-Done: true` means terminal state reached; check `X-Deploy-Status` (or the `/deployments/{id}` status field) for success vs failed — done doesn't imply success.
- Error responses (401 unauthorized, 404 not found, 503 token not configured) come back as `{"error": "..."}`.
