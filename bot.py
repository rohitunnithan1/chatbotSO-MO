#!/usr/bin/env python3
"""
ATI Ops Bot — Slack bot for manufacturing and sales operations queries
Answers questions about Jira MOM/DEL orders and Salesforce pipeline.
"""

import os
import re
import json
import time
import requests as req_lib
from datetime import date
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from openai import OpenAI

# ── Configuration ─────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN  = os.environ["SLACK_APP_TOKEN"]
JIRA_EMAIL       = os.environ["JIRA_EMAIL"]
JIRA_TOKEN       = os.environ["JIRA_API_TOKEN"]
JIRA_BASE        = "https://ati-motors.atlassian.net"
OPENAI_KEY       = os.environ["OPENAI_API_KEY"]
APPS_SCRIPT_URL  = os.environ.get("APPS_SCRIPT_URL", "")

# ── OpenAI setup ──────────────────────────────────────────────────────────────
openai_client = OpenAI(api_key=OPENAI_KEY)

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

# ── Per-user conversation memory ──────────────────────────────────────────────
_conversations: dict = {}
MAX_HISTORY = 12  # 6 exchanges (user + assistant pairs)

def get_history(user_id: str) -> list:
    return _conversations.get(user_id, [])

def save_to_history(user_id: str, role: str, content: str):
    if user_id not in _conversations:
        _conversations[user_id] = []
    _conversations[user_id].append({"role": role, "content": content})
    # Keep only last MAX_HISTORY messages
    if len(_conversations[user_id]) > MAX_HISTORY:
        _conversations[user_id] = _conversations[user_id][-MAX_HISTORY:]


# ── Jira REST helper ──────────────────────────────────────────────────────────
def jira_search(jql: str, fields: list, max_results: int = 200) -> list:
    auth = (JIRA_EMAIL, JIRA_TOKEN)
    all_issues = []
    next_page_token = None

    while len(all_issues) < max_results:
        payload = {"jql": jql, "fields": fields, "maxResults": 50}
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        resp = req_lib.post(
            f"{JIRA_BASE}/rest/api/3/search/jql",
            auth=auth,
            json=payload,
            timeout=20
        )
        print(f"[jira] POST /search/jql status={resp.status_code}")
        if not resp.ok:
            print(f"[jira] error body: {resp.text[:500]}")
            resp.raise_for_status()
        data = resp.json()
        issues = data.get("issues", [])
        all_issues.extend(issues)
        next_page_token = data.get("nextPageToken")
        if not next_page_token or len(issues) < 50:
            break

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
        print("[warn] SF: no APPS_SCRIPT_URL configured")
        return []
    cached = cache_get("sf")
    if cached is not None:
        return cached
    try:
        resp = req_lib.get(
            APPS_SCRIPT_URL,
            params={"action": "data"},
            headers={"Accept": "application/json"},
            timeout=15,
            allow_redirects=True
        )
        print(f"[sf] status={resp.status_code} content-type={resp.headers.get('content-type','')}")
        if resp.ok and "application/json" in resp.headers.get("content-type", ""):
            data = resp.json()
            cache_set("sf", data)
            print(f"[cache] Fetched {len(data)} SF opportunities")
            return data
        else:
            print(f"[warn] SF returned non-JSON: {resp.text[:200]}")
            return []
    except Exception as e:
        print(f"[warn] SF fetch failed: {e}")
        return []


# ── AI answer ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are ATI Ops Bot, the operations assistant for ATI Motors' manufacturing and sales team.

## DATA SOURCES

### Jira MOM — Build Rolling Forecast
One ticket per AMR unit being manufactured. Created automatically when a DEL ticket is approved.
- **Status progression**: To Do → Kitting → Production → Bringup → Validation → Dispatch → Done
- **MRS status** = Build is BLOCKED due to material shortage. These are stuck and cannot proceed.
- **key fields**: summary (customer name), amrType, dueDate, expectedDispatch, actualDispatch
- expectedDispatch = planned dispatch date. actualDispatch = confirmed date (only set when Done/Dispatched).
- If expectedDispatch is in the past and status is not Done/Dispatch, the unit is OVERDUE.

### Jira DEL — Delivery Orders
One ticket per customer sales order. When approved, MOM tickets are auto-created (one per unit).
- **key fields**: summary (customer name + product), amrType, qty (number of units), location, poNumber, status, dueDate
- DEL and MOM tickets for the same customer share the customer name in their summary field.
- If qty=3 for a DEL ticket, there will be 3 MOM tickets for that customer.
- DEL statuses: Requested → Approved → In Progress → Dispatched

### Salesforce Pipeline (if available)
Upcoming deals not yet converted to DEL tickets. Shows future demand.

## HOW TO ANSWER

**Customer queries**: Match customer name across both DEL (the order) and MOM (each unit being built).

**DEL ↔ MOM linking**: To find manufacturing status for a DEL order, find MOM tickets with the same customer name in summary. Example: DEL-50 "HUL Haldia" → look for MOM tickets with "HUL Haldia" in summary.

**Material-blocked builds**: Filter MOM tickets where status = "MRS". These are stuck waiting for parts. Report them clearly as BLOCKED.

**Dispatch questions**:
- Use actualDispatch for units with status Done or Dispatch
- Use expectedDispatch for units still in progress
- Flag any unit where expectedDispatch < today and status ≠ Done as OVERDUE ⏰

**Formatting rules**:
- Lead with the insight or summary, not raw data. E.g. "3 units are delayed" not a list of ticket IDs.
- Only mention ticket keys when they add value (e.g. "MOM-101 (HUL Haldia) is overdue").
- For summaries, group by theme (e.g. by status, by customer, by product type) — don't dump every ticket.
- Cap lists at 5 items unless the user asks for everything. For longer lists, summarise: "6 units total — 3 XT Lite, 2 Pallet Mover, 1 10K".
- Dates are YYYY-MM-DD. Today is {TODAY}.
- If a data source is unavailable, say so clearly and answer from what you have.
- Remember the conversation history. If the user says "tell me more about that" or "what about the first one", refer back to your previous answer.
"""

def answer_question(question: str, user_id: str = "default") -> str:
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
    if sf and isinstance(sf, list):
        context += (
            f"\n## Salesforce — {len(sf)} opportunities\n"
            f"{json.dumps(sf[:120], separators=(',', ':'))}\n"
        )
    elif sf and isinstance(sf, dict):
        sf_list = sf.get("rows") or sf.get("data") or sf.get("opportunities") or []
        context += (
            f"\n## Salesforce — {len(sf_list)} opportunities\n"
            f"{json.dumps(sf_list[:120], separators=(',', ':'))}\n"
        )
        print(f"[sf] dict keys: {list(sf.keys())}")
    else:
        context += "\n## Salesforce — not available\n"

    system_message = SYSTEM_PROMPT.replace("{TODAY}", today) + f"\n\nDATA:\n{context}"

    # Build messages: system + conversation history + new question
    messages = [{"role": "system", "content": system_message}]
    messages.extend(get_history(user_id))
    messages.append({"role": "user", "content": question})

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=1000,
        temperature=0.1
    )
    answer = response.choices[0].message.content.strip()

    # Save exchange to memory
    save_to_history(user_id, "user", question)
    save_to_history(user_id, "assistant", answer)

    return answer


# ── Slack handlers ────────────────────────────────────────────────────────────
def _reply_with_answer(question: str, channel: str, thread_ts: str, client, user_id: str = "default"):
    """Post a 'thinking' message, fetch answer, update in place."""
    r = client.chat_postMessage(
        channel=channel,
        text="🔍 Looking that up...",
        thread_ts=thread_ts
    )
    thinking_ts = r["ts"]

    try:
        reply = answer_question(question, user_id)
    except Exception as e:
        import traceback
        traceback.print_exc()
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

    _reply_with_answer(text, event["channel"], thread_ts, client, user_id=event.get("user", "unknown"))


@app.event("message")
def handle_dm(event, client):
    # Only respond to direct messages
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or not event.get("text", "").strip():
        return

    text = event["text"].strip()
    channel = event["channel"]
    user_id = event.get("user", "unknown")

    r = client.chat_postMessage(channel=channel, text="🔍 Looking that up...")
    thinking_ts = r["ts"]

    try:
        reply = answer_question(text, user_id)
    except Exception as e:
        import traceback
        traceback.print_exc()
        reply = f"⚠️ Something went wrong: {e}"

    client.chat_update(channel=channel, ts=thinking_ts, text=reply)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("ATI Ops Bot starting...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
