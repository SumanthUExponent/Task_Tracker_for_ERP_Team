# Daily briefings for your Mac

A personal assistant that reads your **Slack, Gmail, Calendar, Granola meetings and Claude Code sessions**
and keeps you on top of your work — without you building a to-do list by hand.

| When | What you get |
|---|---|
| **9:00** | A 30-second Slack brief: your Top 3, a reply batch, who you're waiting on, free time today |
| **Monday 9:00** | A weekly summary: last week's results and open to-dos, this week's plan |
| **18:00** | A 3W recap: what went well, what went wrong (with next steps), what's next |
| **All day** | **TickTick** holds it: ✅ To-Do · 💬 Replies · ⏳ Waiting on · 📁 Projects · 📒 Briefings — tick to complete |
| **Nightly** | Every meeting archived as a searchable note (verbatim transcript, decisions, action items) in Google Drive |

It's built to be lean: every item starts with a verb, fits on one line, and says who is waiting and why.
If you're offline, nothing is lost — messages queue and send when you're back, and a missed brief runs late.

## Install (about 20 minutes)

```bash
git clone https://github.com/SumanthUExponent/Task_Tracker_for_ERP_Team.git ~/personal-assistant
cd ~/personal-assistant && claude
```
Then tell Claude: **"Install this for me."** It follows [`CLAUDE.md`](CLAUDE.md), does everything it can
itself, and stops when it needs you (connecting your accounts, pasting the Slack bot token in your own
terminal, approving tools once).

**You need:** a Mac, Claude Desktop + Claude Code on a paid claude.ai plan, TickTick (free), and Slack.
Granola and Google Drive for desktop are optional (for the meeting archive).

## Privacy
Runs entirely on your Mac with your own connectors. It only ever writes to **your** Slack DM and **your**
TickTick — never sends, replies or reacts as you. Your token lives in `~/.config/personal-assistant/config.env`
(readable only by you) and is never committed.

## Daily use
- Tick tasks off in TickTick — the evening recap counts them.
- Or reply in a brief's Slack thread: `done 1 3` · `drop 2` · `2 fri` · `add call Asha re budget`.
- Update: `git pull && python3 setup.py render && python3 setup.py check`.
