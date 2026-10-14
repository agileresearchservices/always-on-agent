# Slack setup (personal workspace, private channel)

The bridge (`python -m aoa.slack_bridge`) is a separate process that talks to Slack over Socket Mode (an
outbound websocket, so no public URL) and to the aoa server over its normal HTTP API. Slack menu names and
scope names below are from memory; check them against Slack's current docs if something doesn't match.

## 1. Create the workspace and channel
1. Create a free workspace for yourself (or use one you own).
2. Create a **private** channel, e.g. `#aoa-agent`.

## 2. Create the app (api.slack.com/apps -> Create New App -> From scratch)
1. **Socket Mode:** enable it. Create an app-level token with the `connections:write` scope.
   Copy it (starts with `xapp-`) -> `SLACK_APP_TOKEN`.
2. **OAuth & Permissions -> Bot Token Scopes:** `chat:write`, `groups:history`.
3. **Event Subscriptions:** enable, and under "Subscribe to bot events" add `message.groups`.
4. **Interactivity & Shortcuts:** turn on (with Socket Mode no request URL is needed). This powers the
   Approve/Deny buttons.
5. **Install to Workspace.** Copy the Bot User OAuth Token (starts with `xoxb-`) -> `SLACK_BOT_TOKEN`.
   If you change scopes or events later, reinstall the app.

## 3. Invite the bot and collect IDs
1. In `#aoa-agent`: `/invite @<your app name>`.
2. Channel ID: channel name -> About -> bottom of the dialog (starts with `C` or `G`) -> `SLACK_CHANNEL_ID`.
3. Your member ID: your profile -> "..." -> Copy member ID (starts with `U`) -> `SLACK_ALLOWED_USER_ID`.

## 4. Configure and run
Add to `.env` (see `.env.example`): `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID`,
`SLACK_ALLOWED_USER_ID`. Then, with the Temporal server, worker and API server already running:

```bash
pip install -e '.[slack]'
set -a; . ./.env; set +a
python -m aoa.slack_bridge
```

Post a message in `#aoa-agent`; the reply appears in the same channel.

## Behavior and trust
- Only messages from `SLACK_ALLOWED_USER_ID` in `SLACK_CHANNEL_ID` reach the agent, as trusted user messages.
  Everything else (other people, other channels, bots, edits) is ignored.
- Approval requests arrive with Approve/Deny buttons; clicks from anyone but the allowed user are ignored.
  Typing `approve apr-xxxxxxxx` or `deny apr-xxxxxxxx` also works.
- On first start the bridge begins at the current end of the outbox, so old history is not replayed into
  Slack. Its position is stored in `<AOA_DATA_DIR>/slack_cursor_<session>.txt`.
- The bridge does not yet implement `external_send` to Slack; that tool is still a stub.
