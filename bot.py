#!/usr/bin/env python3
"""
ATI Ops Bot — Slack bot for manufacturing and sales operations queries
Answers questions about Jira MOM/DEL orders and Salesforce pipeline.
"""

import os
import re
import json
import time
import base64
import urllib.request
import urllib.error
from datetime import date
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai

# ── Configuration ─────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN  = os.environ["SLACK_APP_TOKEN"]
JIRA_EMAIL       = os.environ["JIRA_EMAIL"]
JIRA_TOKEN       = os.environ["JIRA_API_TOKEN"]
JIRA_BASE        = "https://ati-motors.atlassian.net"
GEMINI_KEY       = os.environ["GEMINI_API_KEY"]
APPS_SCRIPT_URL  = os.environ.get("APPS_SCRIPT_URL", "")

# ── Gemini setup ──────────────────────────────────────────────────────────────
gemini = genai.Client(api_key=GEMINI_KEY)

# ── Slack app ─────────────────────────────────────────────────────────────────
app = App(token=SLACK_BOT_TOKEN)

# ── Simple in-memory cache (5 min TTL) ───────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 300

def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None

def cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}


# ── Jira REST helper ──────────────────────────────────────────────────────────
def jira_search(jql: str, fields: list, max_results: int = 200) -> list:
    auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    all_issues = []
    start_at = 0

    while len(all_issues) < max_results:
        payload = json.dumps({
            "jql": jql,
            "fields": fields,
            "maxResults": 50,
            "startAt": start_at
        }).encode()
        req = urllib.request.Request(
            f"{JIRA_BASE}/rest/api/3/search",
            data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        issues = data.get("issues", [])
        all_issues.extend(issues)
        if len(issues) < 50:
            break
        start_at += 50

    return all_issues


# ── Data fetchers ─────────────────────────────────────────────────────────────
def fetch_mom() -> list:
    cached = cache_get("mom")
    if cached is not None:
        return cached

    issues = jira_search(
        jql="project = MOM AND issuetype = Task ORDER BY created DESC",
        fields=["summary", "status", "duedate",
                "customfield_10642", "customfield_10591",
                "customfield_11765", "customfield_10743"]
    )
    result = []
    for i in issues:
        f = i["fields"]
        result.append({
            "key": i["key"],
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "amrType": ((f.get("customfield_10642") or {}).get("value")
                        or f.get("customfield_10591") or ""),
            "dueDate":          f.get("duedate") or "",
            "expectedDispatch": f.get("customfield_11765") or "",
            "actualDispatch":   f.get("customfield_10743") or ""
        })
    cache_set("mom", result)
    print(f"[cache] Fetched {len(result)} MOM tickets")
    return result


def fetch_del() -> list:
    cached = cache_get("del")
    if cached is not None:
        return cached

    issues = jira_search(
        jql='project = DEL AND issuetype = Task AND status != "Requested" ORDER BY created DESC',
        fields=["summary", "status", "duedate",
                "customfield_10354", "customfield_10263",
                "customfield_10261", "customfield_10320"]
    )
    result = []
    for i in issues:
        f = i["fields"]
        result.append({
            "key": i["key"],
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "amrType": (f.get("customfield_10354") or {}).get("value") or "",
            "qty":      f.get("customfield_10263") or "",
            "location": f.get("customfield_10261") or "",
            "poNumber": f.get("customfield_10320") or "",
            "dueDate":  f.get("duedate") or ""
        })
    cache_set("del", result)
    print(f"[cache] Fetched {len(result)} DEL tickets")
    return result


def fetch_sf() -> list:
    if not APPS_SCRIPT_URL:
        return []
    cached = cache_get("sf")
    if cached is not None:
        return cached
    try:
        req = urllib.request.Request(
            f"{APPS_SCRIPT_URL}?action=data",
            headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        cache_set("sf", data)
        print(f"[cache] Fetched {len(data)} SF opportunities")
        return data
    except Exception as e:
        print(f"[warn] SF fetch failed: {e}")
        return []


# ── AI answer ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are ATI Ops Bot, the operations assistant for ATI Motors' manufacturing and sales team.

You have access to live data from:
1. **Jira MOM** (Build Rolling Forecast) — one ticket per AMR unit being manufactured
   - status: "To Do" = not started | "In Progress" = building | "Done" = dispatched
   - key fields: summary (contains customer name), amrType, dueDate, expectedDispatch, actualDispatch
2. **Jira DEL** (Delivery Orders) — one ticket per customer sales order
   - key fields: summary (customer), amrType, qty, location, poNumber, status, dueDate
3. **Salesforce pipeline** — upcoming deals (if available)

Answer rules:
- Be concise and factual. Use bullet points for lists.
- Always include ticket keys (e.g. MOM-101) when referencing specific units.
- When asked about a customer, match their name in the summary field.
- Dates are YYYY-MM-DD. Today is {TODAY}.
- If a data source is unavailable, say so and answer from what you have.
- For dispatch questions, use actualDispatch if status=Done, otherwise expectedDispatch.
- Flag overdue items (expectedDispatch in the past, status not Done) clearly.
"""

def answer_question(question: str) -> str:
    today = date.today().isoformat()

    mom      = fetch_mom()
    del_data = fetch_del()
    sf       = fetch_sf()

    context = (
        f"## MOM — {len(mom)} manufacturing units\n"
        f"{json.dumps(mom, separators=(',', ':'))}\n\n"
        f"## DEL — {len(del_data)} delivery orders\n"
        f"{json.dumps(del_data, separators=(',', ':'))}\n"
    )
    if sf:
        context += (
            f"\n## Salesforce — {len(sf)} opportunities\n"
            f"{json.dumps(sf[:120], separators=(',', ':'))}\n"
        )
    else:
        context += "\n## Salesforce — not available\n"

    prompt = (
        SYSTEM_PROMPT.replace("{TODAY}", today)
        + f"\n\nDATA:\n{context}\n\nQuestion: {question}"
    )

    response = gemini.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt
    )
    return response.text.strip()


# ── Slack handlers ────────────────────────────────────────────────────────────
def _reply_with_answer(question: str, channel: str, thread_ts: str, client):
    """Post a 'thinking' message, fetch answer, update in place."""
    r = client.chat_postMessage(
        channel=channel,
        text="🔍 Looking that up...",
        thread_ts=thread_ts
    )
    thinking_ts = r["ts"]

    try:
        reply = answer_question(question)
    except Exception as e:
        reply = f"⚠️ Something went wrong: {e}"

    client.chat_update(channel=channel, ts=thinking_ts, text=reply)


@app.event("app_mention")
def handle_mention(event, client):
    text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
    thread_ts = event.get("thread_ts") or event.get("ts")

    if not text:
        client.chat_postMessage(
            channel=event["channel"],
            text="👋 Hi! Ask me anything about orders, manufacturing status, or the pipeline.",
            thread_ts=thread_ts
        )
        return

    _reply_with_answer(text, event["channel"], thread_ts, client)


@app.event("message")
def handle_dm(event, client):
    # Only respond to direct messages
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or not event.get("text", "").strip():
        return

    text = event["text"].strip()
    channel = event["channel"]

    r = client.chat_postMessage(channel=channel, text="🔍 Looking that up...")
    thinking_ts = r["ts"]

    try:
        reply = answer_question(text)
    except Exception as e:
        reply = f"⚠️ Something went wrong: {e}"

    client.chat_update(channel=channel, ts=thinking_ts, text=reply)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("ATI Ops Bot starting...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
