"""Install and check the daily briefings on this Mac. CLAUDE.md walks through when to run each step.

    python3 setup.py render                    fill prompts/templates with your name and this folder
    python3 setup.py launchd [--granola]       install the hourly job (and the nightly meeting archive)
    python3 setup.py check                     verify everything; prints no secrets
    python3 setup.py uninstall                 remove the background jobs
"""
import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import config

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "prompts" / "templates"
PROMPTS = HERE / "prompts"
AGENTS = Path.home() / "Library" / "LaunchAgents"
PLACEHOLDER = re.compile(r"{{([A-Z_]+)}}")
JOBS = {
    "hourly": {"args": ["actions.py", "tick"], "StartInterval": 3600,
               # a network change (reconnect, wake) retries queued Slack messages without waiting an hour
               "WatchPaths": ["/Library/Preferences/SystemConfiguration"], "RunAtLoad": True},
    "granola-archive": {"args": ["granola_archive.py"], "StartCalendarInterval": {"Hour": 21, "Minute": 7}},
}


def render(templates, out_dir, values):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for template in sorted(Path(templates).glob("*.md")):
        text = template.read_text()
        unknown = sorted(set(PLACEHOLDER.findall(text)) - set(values))
        if unknown:
            raise SystemExit(f"{template.name}: no value for {', '.join(unknown)}")
        target = out_dir / template.name
        target.write_text(PLACEHOLDER.sub(lambda m: values[m.group(1)], text))
        written.append(target)
    return written


def label(job):
    return f"com.personal-assistant.{job}"


def job_plist(job, python, app_dir):
    spec = JOBS[job]
    body = {
        "Label": label(job),
        "ProgramArguments": [python, *(f"{app_dir}/{a}" if a.endswith(".py") else a for a in spec["args"])],
        # launchd starts with a bare PATH; claude and python live in Homebrew's bin
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "StandardOutPath": f"{app_dir}/state/{job}.log",
        "StandardErrorPath": f"{app_dir}/state/{job}.log",
        **{key: value for key, value in spec.items() if key != "args"},
    }
    return plistlib.dumps(body)


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install_jobs(jobs):
    (HERE / "state").mkdir(exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for job in jobs:
        path = AGENTS / f"{label(job)}.plist"
        _launchctl("bootout", domain, str(path))
        path.write_bytes(job_plist(job, sys.executable, HERE))
        done = _launchctl("bootstrap", domain, str(path))
        print(f"{'✅' if done.returncode == 0 else '❌'} {label(job)} {done.stderr.strip()}")


def uninstall():
    for job in JOBS:
        path = AGENTS / f"{label(job)}.plist"
        _launchctl("bootout", f"gui/{os.getuid()}", str(path))
        path.unlink(missing_ok=True)
        print(f"removed {label(job)}")


def check():
    import slack_post

    settings = config.load()
    results = [("Python 3.9+", sys.version_info >= (3, 9), sys.version.split()[0]),
               ("claude CLI on PATH", bool(shutil.which("claude")), shutil.which("claude") or "install Claude Code")]
    for key in ("SLACK_BOT_TOKEN", "SLACK_USER_ID", "OWNER_NAME"):
        results.append((f"config {key}", bool(settings.get(key)), "set" if settings.get(key) else f"missing in {config.CONFIG_PATH}"))
    mode = config.CONFIG_PATH.stat().st_mode & 0o777 if config.CONFIG_PATH.exists() else None
    results.append(("config.env readable only by you", mode == 0o600, oct(mode) if mode else "no file"))
    if settings.get("SLACK_BOT_TOKEN") and settings.get("SLACK_USER_ID"):
        try:
            slack_post.dm_channel(settings["SLACK_BOT_TOKEN"], settings["SLACK_USER_ID"])
            results.append(("Slack bot can open your DM", True, "ok"))
        except (slack_post.SlackError, OSError, SystemExit) as error:
            results.append(("Slack bot can open your DM", False, str(error)))
    stale = [t.name for t in TEMPLATES.glob("*.md")
             if not (PROMPTS / t.name).exists() or (PROMPTS / t.name).stat().st_mtime < t.stat().st_mtime]
    results.append(("prompts rendered and current", not stale, ", ".join(stale) or "ok"))
    loaded = _launchctl("list").stdout
    results.append(("hourly job loaded", label("hourly") in loaded, label("hourly")))
    if settings.get("MEETINGS_DIR"):
        folder = Path(settings["MEETINGS_DIR"]).expanduser()
        results.append(("meetings folder exists", folder.is_dir(), str(folder)))
        results.append(("nightly meeting archive loaded", label("granola-archive") in loaded, label("granola-archive")))
    for name, ok, detail in results:
        print(f"{'✅' if ok else '❌'} {name} — {detail}")
    return all(ok for _, ok, _ in results)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("render")
    commands.add_parser("launchd").add_argument("--granola", action="store_true", help="also archive Granola nightly")
    commands.add_parser("check")
    commands.add_parser("uninstall")
    args = parser.parse_args()
    if args.command == "render":
        owner = config.require(config.load(), "OWNER_NAME")
        for path in render(TEMPLATES, PROMPTS, {"OWNER": owner.split()[0], "APP_DIR": str(HERE)}):
            print(f"✅ {path.relative_to(HERE)}")
    elif args.command == "launchd":
        install_jobs(["hourly", "granola-archive"] if args.granola else ["hourly"])
    elif args.command == "check":
        sys.exit(0 if check() else 1)
    else:
        uninstall()


if __name__ == "__main__":
    main()
