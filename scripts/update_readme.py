"""Refresh the auto-generated sections of README.md.

Each section lives between <!-- NAME:START --> and <!-- NAME:END --> markers.
A section that is not configured is emptied; a section whose data source fails
is left untouched, so a flaky API never wipes good content.

Environment:
  GH_USERNAME        GitHub user whose public activity is shown (required)
  GITHUB_TOKEN       optional, raises the GitHub API rate limit
  LEETCODE_USERNAME  optional, enables the LeetCode section
  BLOG_FEED_URL      optional RSS/Atom feed URL, enables the blog section
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
METRICS_SVG = ROOT / "github-metrics.svg"
USER_AGENT = "profile-readme-updater"
TIMEOUT = 20


class SourceError(Exception):
    """A data source could not be reached; keep the existing section."""


def fetch(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except Exception as exc:
        raise SourceError(f"{url}: {exc}") from exc


def ago(iso_or_dt):
    dt = iso_or_dt if isinstance(iso_or_dt, datetime) else datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


# ---------------------------------------------------------------- activity

def describe_event(event):
    repo = event["repo"]["name"]
    link = f"[{repo}](https://github.com/{repo})"
    kind, payload = event["type"], event.get("payload", {})

    if kind == "PushEvent":
        branch = payload.get("ref", "").removeprefix("refs/heads/")
        return f"⬆️ Pushed to `{branch}` in {link}" if branch else f"⬆️ Pushed to {link}"
    # The public Events API trims payloads (no html_url, often no merged flag),
    # so links are built from the repo name and number.
    if kind == "PullRequestEvent":
        pr = payload.get("pull_request", {})
        number = payload.get("number") or pr.get("number")
        pr_link = f"[#{number}](https://github.com/{repo}/pull/{number})"
        action = payload.get("action")
        if action == "merged" or (action == "closed" and pr.get("merged")):
            return f"🎉 Merged PR {pr_link} in {link}"
        if action in ("opened", "reopened"):
            return f"💪 Opened PR {pr_link} in {link}"
    if kind == "IssuesEvent" and payload.get("action") in ("opened", "closed"):
        number = payload.get("issue", {}).get("number")
        if number:
            icon = "❗" if payload["action"] == "opened" else "✔️"
            return f"{icon} {payload['action'].capitalize()} issue [#{number}](https://github.com/{repo}/issues/{number}) in {link}"
    if kind == "CreateEvent" and payload.get("ref_type") == "repository":
        return f"✨ Created repository {link}"
    if kind == "ReleaseEvent" and payload.get("action") == "published":
        tag = payload.get("release", {}).get("tag_name")
        if tag:
            return f"🚀 Released [{tag}](https://github.com/{repo}/releases/tag/{tag}) of {link}"
    if kind == "ForkEvent":
        return f"🍴 Forked {link}"
    if kind == "WatchEvent":
        return f"⭐ Starred {link}"
    return None


def activity_section(username, token, limit=5):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    events = json.loads(fetch(f"https://api.github.com/users/{username}/events/public?per_page=100", headers=headers))
    events.sort(key=lambda e: e["created_at"], reverse=True)  # API order isn't strictly chronological

    lines, seen = [], set()
    for event in events:
        try:
            text = describe_event(event)
        except (KeyError, TypeError, AttributeError):
            continue  # unexpected payload shape; skip rather than fail the run
        if not text or text in seen:  # collapse repeated pushes to the same branch
            continue
        seen.add(text)
        lines.append(f"{len(lines) + 1}. {text} · <sub>{ago(event['created_at'])}</sub>")
        if len(lines) == limit:
            break
    if not lines:
        return ""
    return "### ⚡ Recent Activity\n\n" + "\n".join(lines)


# ---------------------------------------------------------------- leetcode

LEETCODE_QUERY = """
query profile($username: String!) {
  allQuestionsCount { difficulty count }
  matchedUser(username: $username) {
    profile { ranking }
    submitStatsGlobal { acSubmissionNum { difficulty count } }
  }
  recentAcSubmissionList(username: $username, limit: 5) { title titleSlug timestamp }
}
"""


def leetcode_section(username):
    body = json.dumps({"query": LEETCODE_QUERY, "variables": {"username": username}}).encode()
    resp = json.loads(fetch(
        "https://leetcode.com/graphql",
        data=body,
        headers={"Content-Type": "application/json", "Referer": f"https://leetcode.com/u/{username}/"},
    ))
    data = resp.get("data") or {}
    user = data.get("matchedUser")
    if not user:
        raise SourceError(f"LeetCode user {username!r} not found: {resp.get('errors')}")

    totals = {q["difficulty"]: q["count"] for q in data["allQuestionsCount"]}
    solved = {s["difficulty"]: s["count"] for s in user["submitStatsGlobal"]["acSubmissionNum"]}
    rows = "\n".join(
        f"| {icon} {level} | **{solved.get(level, 0)}** / {totals.get(level, 0)} |"
        for icon, level in (("🟢", "Easy"), ("🟡", "Medium"), ("🔴", "Hard"))
    )
    out = [
        "### 🧠 LeetCode",
        "",
        f"[@{username}](https://leetcode.com/u/{username}/) · **{solved.get('All', 0)}** solved"
        f" · rank {user['profile']['ranking']:,}",
        "",
        "| Difficulty | Solved |",
        "| --- | --- |",
        rows,
    ]
    recent = data.get("recentAcSubmissionList") or []
    if recent:
        out += ["", "**Recently solved**", ""]
        out += [
            f"- [{s['title']}](https://leetcode.com/problems/{s['titleSlug']}/) · "
            f"<sub>{ago(datetime.fromtimestamp(int(s['timestamp']), timezone.utc))}</sub>"
            for s in recent
        ]
    return "\n".join(out)


# ---------------------------------------------------------------- blog

ATOM = "{http://www.w3.org/2005/Atom}"


def blog_section(feed_url, limit=5):
    try:
        root = ET.fromstring(fetch(feed_url))
    except ET.ParseError as exc:
        raise SourceError(f"{feed_url}: not a valid feed ({exc})") from exc

    posts = []
    for item in root.iter("item"):  # RSS 2.0
        posts.append((item.findtext("title", "").strip(), item.findtext("link", "").strip()))
    for entry in root.iter(f"{ATOM}entry"):  # Atom
        link = entry.find(f"{ATOM}link[@rel='alternate']")
        if link is None:
            link = entry.find(f"{ATOM}link")
        posts.append((entry.findtext(f"{ATOM}title", "").strip(), link.get("href", "") if link is not None else ""))

    lines = [f"- [{title}]({url})" for title, url in posts[:limit] if title and url]
    if not lines:
        return ""
    return "### ✍️ Latest Posts\n\n" + "\n".join(lines)


# ---------------------------------------------------------------- metrics

def metrics_section():
    # Only show the image once the metrics workflow has generated it.
    if not METRICS_SVG.exists():
        return ""
    return '<p align="center">\n  <img src="github-metrics.svg" alt="GitHub metrics" />\n</p>'


# ---------------------------------------------------------------- main

def replace_section(text, name, content):
    pattern = re.compile(rf"(<!-- {name}:START -->)(.*?)(<!-- {name}:END -->)", re.DOTALL)
    if not pattern.search(text):
        print(f"skip {name}: markers not found")
        return text
    body = f"\n{content}\n" if content else "\n"
    return pattern.sub(lambda m: m.group(1) + body + m.group(3), text, count=1)


def main():
    username = os.environ.get("GH_USERNAME")
    if not username:
        sys.exit("GH_USERNAME is required")
    leetcode_user = os.environ.get("LEETCODE_USERNAME", "").strip()
    feed_url = os.environ.get("BLOG_FEED_URL", "").strip()

    builders = {
        "ACTIVITY": lambda: activity_section(username, os.environ.get("GITHUB_TOKEN")),
        "LEETCODE": lambda: leetcode_section(leetcode_user) if leetcode_user else "",
        "BLOG": lambda: blog_section(feed_url) if feed_url else "",
        "METRICS": metrics_section,
    }

    original = README.read_text(encoding="utf-8")
    text = original
    for name, build in builders.items():
        try:
            text = replace_section(text, name, build())
            print(f"updated {name}")
        except SourceError as exc:
            print(f"kept {name}: {exc}", file=sys.stderr)

    if text != original:
        README.write_text(text, encoding="utf-8", newline="\n")
        print("README.md changed")
    else:
        print("README.md unchanged")


if __name__ == "__main__":
    main()
