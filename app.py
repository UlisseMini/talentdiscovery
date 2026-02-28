"""
Modal app: GitHub Intelligence Platform
FastAPI web service + Claude Agent SDK chat
"""

import json
import math
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Modal setup
# ---------------------------------------------------------------------------

app = modal.App("talent-discovery")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi",
        "uvicorn",
        "httpx",
        "sse-starlette",
        "claude-agent-sdk",
    )
    # Create non-root user so Claude CLI allows bypassPermissions
    .run_commands(
        "useradd -m -s /bin/bash agent",
        "mkdir -p /data && chown agent:agent /data",
        "mkdir -p /home/agent/.claude && chown -R agent:agent /home/agent",
    )
    .add_local_file("index.html", remote_path="/app/index.html")
)

volume = modal.Volume.from_name("talent-data")

VOLUME_PATH = "/data"
MERGED_FILE = f"{VOLUME_PATH}/data/merged_network.json"
ENRICHED_FILE = f"{VOLUME_PATH}/enriched_winners.json"

# In-memory user session store (maps session cookie -> {github_token, login, ...})
# For production, use Modal Dict or a database
user_sessions: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Data loading (runs once per container)
# ---------------------------------------------------------------------------

PROFILES: list[dict] = []
PROFILES_BY_LOGIN: dict[str, dict] = {}
PROFILES_BY_LANGUAGE: dict[str, list[dict]] = defaultdict(list)
SEARCH_STRINGS: dict[str, str] = {}
HACKATHON_PROJECTS: list[dict] = []
SEEDS: list[str] = []
ALL_LANGUAGES: list[str] = []
_DATA_LOADED = False


def load_data():
    global PROFILES, SEEDS, HACKATHON_PROJECTS, ALL_LANGUAGES, _DATA_LOADED
    if _DATA_LOADED:
        return

    PROFILES.clear()
    PROFILES_BY_LOGIN.clear()
    PROFILES_BY_LANGUAGE.clear()
    SEARCH_STRINGS.clear()
    HACKATHON_PROJECTS.clear()
    SEEDS.clear()
    ALL_LANGUAGES.clear()

    raw = json.loads(Path(MERGED_FILE).read_text())
    SEEDS.extend(raw.get("seeds", []))
    PROFILES.extend(raw["profiles"])

    lang_counts: dict[str, int] = defaultdict(int)

    for p in PROFILES:
        login = p["login"]
        PROFILES_BY_LOGIN[login] = p
        for lang in p.get("top_languages", []):
            PROFILES_BY_LANGUAGE[lang].append(p)
            lang_counts[lang] += 1
        parts = [
            login, p.get("name") or "", p.get("bio") or "",
            p.get("company") or "", p.get("location") or "",
            " ".join(p.get("top_languages", [])),
            " ".join(o.get("login", "") for o in p.get("orgs", [])),
            " ".join(r.get("name", "") + " " + (r.get("desc") or "") for r in p.get("top_repos", [])),
        ]
        SEARCH_STRINGS[login] = " ".join(parts).lower()

    ALL_LANGUAGES.extend(
        lang for lang, _ in sorted(lang_counts.items(), key=lambda x: -x[1])
    )

    if Path(ENRICHED_FILE).exists():
        HACKATHON_PROJECTS.extend(json.loads(Path(ENRICHED_FILE).read_text()))

    # Compute cracked_score
    for p in PROFILES:
        created = p.get("created_at", "")
        if created:
            try:
                age = max((datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days / 365.25, 0.5)
            except Exception:
                age = 5.0
        else:
            age = 5.0

        log_stars = math.log10(p.get("total_stars", 0) + 1)
        stars_per_year = log_stars / age
        commits_per_year = min(p.get("total_commits", 0), 2000) / age
        prs_per_year = min(p.get("total_prs", 0), 300) / age

        if age < 8:
            youth_mult = max(1.0, 5.0 - age * 0.5)
        else:
            youth_mult = max(0.1, 1.0 - (age - 8) * 0.13)

        output = p.get("total_stars", 0) + p.get("total_commits", 0) * 2 + p.get("total_prs", 0) * 5
        follow_ratio = p.get("followers", 0) / max(output, 1)
        farm_penalty = min(1.0, 2.0 / max(follow_ratio, 0.01)) if follow_ratio > 5 else 1.0

        famous_penalty = 1.0
        if p.get("followers", 0) > 20000: famous_penalty = 0.2
        elif p.get("followers", 0) > 10000: famous_penalty = 0.5
        elif p.get("followers", 0) > 5000: famous_penalty = 0.7

        net_bonus = 1.5 if p.get("in_multiple_networks") else 1.0
        mutual_bonus = 1.3 if p.get("is_mutual_follow") else 1.0

        cracked = (
            stars_per_year * 100.0 + commits_per_year * 0.3
            + prs_per_year * 2.0 + p.get("public_repos", 0) * 0.1
        ) * youth_mult * farm_penalty * famous_penalty * net_bonus * mutual_bonus

        p["cracked_score"] = round(cracked, 1)
        p["account_age_years"] = round(age, 1)

    PROFILES.sort(key=lambda p: -p.get("cracked_score", 0))
    _DATA_LOADED = True
    print(f"Loaded {len(PROFILES)} profiles, {len(HACKATHON_PROJECTS)} hackathon projects, {len(ALL_LANGUAGES)} languages")


# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are a talent discovery agent for a recruiting platform. You have access to a dataset of GitHub developers crawled from interconnected social networks, plus hackathon projects.

## Data Location
- Developer profiles: /data/data/merged_network.json (JSON with "profiles" array, "seeds" array)
- Hackathon projects: /data/enriched_winners.json (JSON array of projects)

## Quick Data Access
You can use Python to query the data. Here's a starter pattern:

```python
import json
data = json.load(open('/data/data/merged_network.json'))
profiles = data['profiles']

# Search by keyword
results = [p for p in profiles if 'rust' in ' '.join(p.get('top_languages', [])).lower()]

# Sort by cracked_score (our proprietary "undiscovered talent" metric)
results.sort(key=lambda p: -p.get('cracked_score', 0))

# Key fields per profile:
# login, name, bio, company, location, followers, following
# total_stars, total_commits, total_prs, public_repos
# top_languages: [str], top_repos: [{name, stars, lang, desc}]
# orgs: [{login, name}], created_at, account_age_years, cracked_score
# in_multiple_networks (bool), is_mutual_follow (bool)
# found_via: [seed_logins], connections: {seed: [relationship_types]}
```

## Cracked Score
Our proprietary score that favors undiscovered talent:
- Log-scaled stars per year (prevents mega-repos from dominating)
- Youth multiplier (younger accounts with high output = more impressive)
- Famous penalty (>5K followers = already discovered)
- Follow-farm penalty (high followers, low output = suspicious)
- Network bonus (found via multiple seed networks = strong signal)

## Your Job
1. Use Bash to run Python snippets that query the data
2. Use WebSearch/WebFetch for live GitHub research
3. Provide data-driven recommendations with specific numbers
4. Be concise but thorough - cite login, stars, commits, languages, notable repos
5. Highlight network signals: MULTI-NET, MUTUAL flags

Always cite specific numbers from the data. Don't make up information."""


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

web_app = FastAPI(title="Talent Discovery Platform")


@web_app.on_event("startup")
async def startup():
    load_data()


@web_app.get("/")
async def serve_frontend():
    # Serve the index.html from the mounted volume or local
    local_html = Path("/app/index.html")
    if local_html.exists():
        return FileResponse(local_html, media_type="text/html")
    return HTMLResponse("<h1>index.html not found</h1>", status_code=500)


@web_app.get("/api/stats")
async def stats():
    load_data()
    total_stars = sum(p.get("total_stars", 0) for p in PROFILES)
    total_commits = sum(p.get("total_commits", 0) for p in PROFILES)
    return {
        "total_developers": len(PROFILES),
        "total_projects": len(HACKATHON_PROJECTS),
        "total_languages": len(ALL_LANGUAGES),
        "total_stars": total_stars,
        "total_commits": total_commits,
        "seeds": SEEDS,
        "top_languages": ALL_LANGUAGES[:20],
    }


@web_app.get("/api/profiles")
async def list_profiles(
    q: str | None = Query(None),
    language: str | None = Query(None),
    min_score: float = Query(0),
    sort: str = Query("cracked"),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
):
    load_data()
    results = PROFILES

    if q:
        q_lower = q.lower()
        results = [p for p in results if q_lower in SEARCH_STRINGS.get(p["login"], "")]

    if language:
        results = [p for p in results if language in p.get("top_languages", [])]

    if min_score > 0:
        results = [p for p in results if p.get("cracked_score", 0) >= min_score]

    sort_keys = {
        "cracked": "cracked_score", "score": "score", "stars": "total_stars",
        "followers": "followers", "commits": "total_commits",
    }
    key = sort_keys.get(sort, "cracked_score")
    results = sorted(results, key=lambda p: -p.get(key, 0))

    total = len(results)
    results = results[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "profiles": results}


@web_app.get("/api/profiles/{login}")
async def get_profile(login: str):
    load_data()
    profile = PROFILES_BY_LOGIN.get(login)
    if not profile:
        return JSONResponse({"error": "Not found"}, status_code=404)

    projects = []
    for proj in HACKATHON_PROJECTS:
        contributors = proj.get("repo_data", {}).get("contributors", []) if proj.get("repo_data") else []
        team = proj.get("team_members", [])
        gh_logins = [c.get("github_username") for c in contributors if c.get("github_username")]
        gh_logins += [(m.get("github", "") or "").rstrip("/").split("/")[-1] for m in team if m.get("github")]
        if login in gh_logins:
            projects.append({
                "title": proj.get("title"), "tagline": proj.get("tagline"),
                "hackathon": proj.get("hackathon"), "prizes": proj.get("prizes", []),
                "url": proj.get("url"),
            })
    return {**profile, "hackathon_projects": projects}


@web_app.get("/api/graph/{login}")
async def get_graph(login: str):
    load_data()
    profile = PROFILES_BY_LOGIN.get(login)
    if not profile:
        return JSONResponse({"error": "Not found"}, status_code=404)

    nodes = [{"id": login, "label": profile.get("name") or login, "type": "target",
              "score": profile.get("score", 0), "avatar": f"https://github.com/{login}.png"}]
    edges = []
    connections = profile.get("connections", {})
    found_via = profile.get("found_via", [])

    for seed in found_via:
        if seed not in PROFILES_BY_LOGIN:
            continue
        seed_p = PROFILES_BY_LOGIN[seed]
        nodes.append({
            "id": seed, "label": seed_p.get("name") or seed, "type": "seed",
            "score": seed_p.get("score", 0), "avatar": f"https://github.com/{seed}.png",
        })
        for rel in connections.get(seed, []):
            if rel == "follower":
                edges.append({"source": login, "target": seed, "type": "follows"})
            elif rel == "following":
                edges.append({"source": seed, "target": login, "type": "follows"})

    connected_logins = set()
    for p in PROFILES:
        if p["login"] == login:
            continue
        shared = set(p.get("found_via", [])) & set(found_via)
        if shared and len(connected_logins) < 12:
            connected_logins.add(p["login"])
            nodes.append({
                "id": p["login"], "label": p.get("name") or p["login"], "type": "peer",
                "score": p.get("score", 0), "avatar": f"https://github.com/{p['login']}.png",
            })
            for seed in shared:
                edges.append({"source": p["login"], "target": seed, "type": "shared_network"})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# GitHub OAuth
# ---------------------------------------------------------------------------

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")


@web_app.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID:
        return JSONResponse({"error": "GitHub OAuth not configured"}, status_code=500)
    # Get the base URL from the request
    base_url = str(request.base_url).rstrip("/")
    callback_url = f"{base_url}/auth/callback"
    state = uuid.uuid4().hex
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={callback_url}"
        f"&scope=read:user"
        f"&state={state}"
    )
    return RedirectResponse(url)


@web_app.get("/auth/callback")
async def auth_callback(code: str = "", state: str = ""):
    if not code:
        return JSONResponse({"error": "No code provided"}, status_code=400)

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET, "code": code},
            headers={"Accept": "application/json"},
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            return JSONResponse({"error": "Failed to get token", "detail": data}, status_code=400)

        # Get user info
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        user = user_resp.json()

    session_id = uuid.uuid4().hex
    user_sessions[session_id] = {
        "github_token": token,
        "login": user.get("login"),
        "name": user.get("name"),
        "avatar_url": user.get("avatar_url"),
    }

    response = RedirectResponse("/")
    response.set_cookie("session", session_id, httponly=True, max_age=86400 * 7)
    return response


@web_app.get("/auth/me")
async def auth_me(request: Request):
    session_id = request.cookies.get("session")
    if not session_id or session_id not in user_sessions:
        return JSONResponse({"authenticated": False})
    user = user_sessions[session_id]
    return {"authenticated": True, "login": user["login"], "name": user.get("name"), "avatar_url": user.get("avatar_url")}


@web_app.post("/auth/logout")
async def auth_logout(request: Request):
    session_id = request.cookies.get("session")
    if session_id and session_id in user_sessions:
        del user_sessions[session_id]
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


# ---------------------------------------------------------------------------
# Chat endpoint (Claude Agent SDK)
# ---------------------------------------------------------------------------

@web_app.get("/api/chat")
async def chat(request: Request, q: str = Query(...), session_id: str | None = Query(None)):
    load_data()

    # Get user's GitHub token if authenticated
    cookie_session = request.cookies.get("session")
    github_token = None
    if cookie_session and cookie_session in user_sessions:
        github_token = user_sessions[cookie_session].get("github_token")

    async def event_stream():
        start = time.time()
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions, query as agent_query,
                AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, ToolResultBlock,
            )

            agent_env = {}
            if github_token:
                agent_env["GITHUB_TOKEN"] = github_token

            stderr_lines = []
            def capture_stderr(line: str):
                stderr_lines.append(line)
                print(f"[agent stderr] {line}", flush=True)

            agent_env["HOME"] = "/home/agent"

            options = ClaudeAgentOptions(
                allowed_tools=["Bash", "Read", "Glob", "Grep", "WebSearch", "WebFetch"],
                permission_mode="bypassPermissions",
                system_prompt=AGENT_SYSTEM_PROMPT,
                max_turns=15,
                model="claude-sonnet-4-5-20250929",
                cwd="/data",
                env=agent_env,
                include_partial_messages=True,
                stderr=capture_stderr,
                user="agent",
                **({"resume": session_id} if session_id else {}),
            )

            async for msg in agent_query(prompt=q, options=options):
                if await request.is_disconnected():
                    break

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield {"event": "text", "data": json.dumps({"text": block.text})}
                        elif isinstance(block, ToolUseBlock):
                            yield {"event": "tool_call", "data": json.dumps({
                                "tool": block.name,
                                "input": str(block.input)[:200],
                            })}
                        elif isinstance(block, ToolResultBlock):
                            content_preview = str(block.content)[:150] if block.content else ""
                            yield {"event": "tool_result", "data": json.dumps({
                                "tool_use_id": block.tool_use_id,
                                "content": content_preview,
                            })}

                elif isinstance(msg, ResultMessage):
                    elapsed = time.time() - start
                    yield {"event": "done", "data": json.dumps({
                        "duration": round(elapsed, 1),
                        "cost": getattr(msg, "total_cost_usd", None),
                        "turns": getattr(msg, "num_turns", None),
                        "session_id": getattr(msg, "session_id", None),
                    })}

        except Exception as e:
            import traceback
            traceback.print_exc()
            error_detail = str(e)
            if stderr_lines:
                error_detail += "\n\nSTDERR:\n" + "\n".join(stderr_lines[-20:])
            yield {"event": "error", "data": json.dumps({"error": error_detail})}

    return EventSourceResponse(event_stream())


# ---------------------------------------------------------------------------
# Dossier endpoint
# ---------------------------------------------------------------------------

@web_app.get("/api/dossier/{login}")
async def agent_dossier(request: Request, login: str):
    load_data()
    profile = PROFILES_BY_LOGIN.get(login)
    if not profile:
        return JSONResponse({"error": "Not found"}, status_code=404)

    profile_json = json.dumps({k: v for k, v in profile.items() if k != "connections"}, indent=2)

    # Find hackathon projects
    projects = []
    for proj in HACKATHON_PROJECTS:
        contributors = proj.get("repo_data", {}).get("contributors", []) if proj.get("repo_data") else []
        team = proj.get("team_members", [])
        gh_logins = [c.get("github_username") for c in contributors if c.get("github_username")]
        gh_logins += [(m.get("github", "") or "").rstrip("/").split("/")[-1] for m in team if m.get("github")]
        if login in gh_logins:
            projects.append({"title": proj.get("title"), "tagline": proj.get("tagline"),
                           "hackathon": proj.get("hackathon"), "prizes": proj.get("prizes", [])})

    prompt = f"""Generate a recruiting dossier for @{login}.

FULL PROFILE:
{profile_json}

NETWORK: Found via: {profile.get("found_via", [])}, connections: {json.dumps(profile.get("connections", {}))}
Multi-network: {profile.get("in_multiple_networks")}, Mutual follow: {profile.get("is_mutual_follow")}

HACKATHON PROJECTS: {json.dumps(projects) if projects else "None found"}

Cover: Overview, Technical Profile, Collaboration Signal, Hackathon Record, Network Position, Recruiting Assessment.
Cite numbers. Be honest about gaps. Use WebSearch to look up their recent GitHub activity and any other public info."""

    async def event_stream():
        start = time.time()
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions, query as agent_query,
                AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, ToolResultBlock,
            )

            options = ClaudeAgentOptions(
                allowed_tools=["Bash", "Read", "Glob", "Grep", "WebSearch", "WebFetch"],
                permission_mode="bypassPermissions",
                system_prompt=AGENT_SYSTEM_PROMPT,
                max_turns=10,
                model="claude-sonnet-4-5-20250929",
                cwd="/data",
                include_partial_messages=True,
                user="agent",
                env={"HOME": "/home/agent"},
            )

            async for msg in agent_query(prompt=prompt, options=options):
                if await request.is_disconnected():
                    break
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield {"event": "text", "data": json.dumps({"text": block.text})}
                        elif isinstance(block, ToolUseBlock):
                            yield {"event": "tool_call", "data": json.dumps({
                                "tool": block.name, "input": str(block.input)[:200],
                            })}
                elif isinstance(msg, ResultMessage):
                    elapsed = time.time() - start
                    yield {"event": "done", "data": json.dumps({
                        "duration": round(elapsed, 1),
                        "cost": getattr(msg, "total_cost_usd", None),
                        "turns": getattr(msg, "num_turns", None),
                    })}
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield {"event": "error", "data": json.dumps({"error": str(e)})}

    return EventSourceResponse(event_stream())


# ---------------------------------------------------------------------------
# Modal entrypoint
# ---------------------------------------------------------------------------

@app.function(
    image=image,
    volumes={VOLUME_PATH: volume},
    secrets=[modal.Secret.from_name("anthropic-key")],
    timeout=600,
)
@modal.asgi_app()
def web():
    return web_app
