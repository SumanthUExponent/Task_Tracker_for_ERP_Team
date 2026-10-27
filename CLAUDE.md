# CLAUDE.md — Daily briefings: install, update, maintain

You are installing (or maintaining) a personal assistant for the user on **their own Mac**. It reads their
Slack, Gmail, Calendar, Granola meetings and Claude Code history and gives them:

- **9:00** a lean Slack to-do brief (Mondays: a weekly summary of all of last week)
- **18:00** a 3W recap (what went well / went wrong / what's next)
- **TickTick** as the place they read and tick tasks: To-Do, Replies, Waiting on, Projects, Briefings
- reminders, Slack replies (`done 1 3`), a meeting archive to Google Drive, and retries when offline

Everything runs locally. Nothing leaves the Mac except messages to the user's own Slack DM and their own TickTick.

## Install — follow in order, one step at a time

Do every step yourself when you can; stop and wait whenever a step needs the user (marked **USER**).
Run commands from the repo root. Never print, echo or ask the user to paste a token into chat.

### 0. Check the machine
```bash
sw_vers -productVersion; python3 --version; which claude; ls -d /Applications/Claude.app
```
Needs macOS, Python 3.9+, Claude Code CLI, Claude Desktop, and a paid claude.ai plan (local scheduled tasks
and connectors). Python must be stdlib only — **never pip-install anything for this repo.**

### 1. Prove the code works here
```bash
python3 -m unittest
```
All tests must pass before going further. If one fails, stop and report it; do not edit tests to pass.

### 2. Connectors — **USER**
At **claude.ai/customize/connectors**, connect **Slack, Gmail, Google Calendar, TickTick**, and **Granola** if
they use it. Confirm in a **new** Claude Code session with `/mcp` (connectors load only at session start).

### 3. Slack bot — **USER**
The briefs are posted by a bot, because Slack never notifies you of your own messages.
- If the workspace already has a **Daily Brief** app, ask its owner to add the user as a **Collaborator**
  (api.slack.com/apps → Daily Brief → Collaborators). The user then copies the **Bot User OAuth Token**.
- Otherwise: api.slack.com/apps → Create New App → From a manifest → paste `slack-app-manifest.yaml` →
  Install (an admin may need to approve). Scopes: `chat:write`, `im:write`, `im:history`; the Messages tab
  must allow sending messages.
- Their member ID: Slack → their profile → ⋮ → Copy member ID (starts with `U`).

### 4. Configure — **USER types the token, you write the rest**
Ask for their full name (as it appears in Slack/email) and member ID, and whether they want the meeting
archive (if yes: a folder inside their Google Drive for desktop sync, plus its Drive link). Then have them run
this in their own terminal — the token never passes through chat:
```bash
mkdir -p ~/.config/personal-assistant && touch ~/.config/personal-assistant/config.env && chmod 600 ~/.config/personal-assistant/config.env
read -s "T?Paste the xoxb- bot token: " && echo "SLACK_BOT_TOKEN=$T" >> ~/.config/personal-assistant/config.env && unset T && echo saved
```
Then append the non-secret lines yourself (see `config.py` for every key):
```
SLACK_USER_ID=U…
OWNER_NAME=First Last
MEETINGS_DIR=~/path/inside/Google Drive/Meetings        # optional
MEETINGS_DRIVE_URL=https://drive.google.com/drive/folders/…   # optional
```

### 5. Render and schedule
```bash
python3 setup.py render              # writes prompts/*.md from prompts/templates/ with their name and this folder
python3 setup.py launchd             # add --granola if they use Granola
python3 setup.py check               # every line must be ✅
```

### 6. Live Slack test — **USER confirms**
```bash
printf 'Daily Brief test · if your phone buzzed, delivery works\n' > /tmp/brief-test.md && python3 slack_post.py post --file /tmp/brief-test.md
```
Ask whether their phone buzzed. If not, the bot or DM setup is wrong — fix before going on.

### 7. TickTick — **USER**
The free plan allows 9 lists **including TickTick's starter lists** (Welcome, Work, Learning, Study…), and the
connector cannot delete lists. Ask the user to delete the starter lists they don't use in the app.
The first brief creates the 5 lists by itself (`prompts/ticktick.md`).

### 8. The two scheduled tasks (Claude Desktop → Code tab → Routines)
Create two **local** tasks ("Only on this computer"), working folder = this repo, **model Haiku**,
**permission mode Ask** (not Auto). If you are running inside Claude Desktop and have a scheduled-task tool,
create them yourself; otherwise give the user this text to paste into a Desktop Code session:

> Create two local scheduled tasks, both "Only on this computer", working folder `<repo path>`, model Haiku,
> permission mode Ask:
> 1. `morning-todo`, custom cron `57 8 * * *`, instructions: `Read <repo path>/prompts/morning.md and follow it exactly.`
> 2. `evening-3w`, custom cron `57 17 * * *`, instructions: `Read <repo path>/prompts/evening.md and follow it exactly.`

### 9. First run — **USER approves tools**
Routines → `morning-todo` → **Run now**, open the run, and choose **Always allow** for exactly:
- Bash `python3 <repo>/cc_digest.py`, `python3 <repo>/actions.py`, `python3 <repo>/slack_post.py`
- file edits inside `<repo>/briefs/`
- the **read/search** tools of Slack, Gmail, Calendar, Granola, and the TickTick tools
- **Deny** any Slack/Gmail send, reply, react or label tool — the briefs never send as the user.

Repeat for `evening-3w`. Then check `briefs/<today>-am.md`, the Slack DM, and TickTick against each other.

### 10. Meeting backfill (only with `--granola`)
```bash
python3 granola_archive.py --days 30     # Granola deletes older meetings on lower plans
```

## Update
```bash
git pull && python3 -m unittest && python3 setup.py render && python3 setup.py check
```
## Uninstall
`python3 setup.py uninstall`, delete the two tasks in Routines, remove `~/.config/personal-assistant/`.

## Working on this repo
- **Edit `prompts/templates/`, never `prompts/*.md`** (those are generated by `setup.py render`).
- Python stdlib only; tests are `python3 -m unittest`; write the failing test first.
- Never commit `config.env`, `briefs/`, `state/` or anything with a token or someone's messages.
- Brief format rules: lines under 80 characters, main part ≤ 12 lines (weekly ≤ 18, evening ≤ 14).
  Haiku ignores word limits — always cap by lines.

## Known traps (each one cost a broken run)
- Headless `claude -p` permission rules: `Write(path)` is ignored — use `Edit(path)`; and an absolute path
  needs `//` (`Edit(//Users/me/repo/briefs/**)`). A single leading `/` matches nothing.
- Slack `conversations.replies` rejects JSON bodies (`invalid_arguments`) — read methods are form-encoded.
- Slack connector messages are sent as the user and never notify them → the bot token exists for that.
- Claude Code swaps a large tool result (~80 KB) for a "saved to <file>" pointer; `granola_archive.py`
  reads the file. Keep transcript pages at 30,000 characters so `next_offset` stays visible.
- Granola: personal API keys need a Business plan, so the archive goes through the connector.
- TickTick: no note-list kind, no delete-list tool, 9-list cap counts starter lists, tags are cut at a hyphen.
- launchd `WatchPaths` on the network config fires every few minutes on flaky Wi-Fi; the hourly job
  skips unless an hour passed or the outbox has messages.
- Claude Desktop never retries a scheduled run that failed offline; the hourly job runs a missed brief
  (no morning file by 10:30, no evening file by 18:30) headless.
