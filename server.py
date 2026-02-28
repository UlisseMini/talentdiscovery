#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "claude-code-sdk", "httpx", "sse-starlette"]
# ///
"""
Recruiting platform backend.
Serves the frontend, provides API endpoints for browsing/filtering developers,
and agent-powered natural language search + dossier generation via Claude Code SDK.
"""

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
import uvicorn

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data"
MERGED_FILE = DATA_DIR / "merged_network.json"
ENRICHED_FILE = Path(__file__).parent / "enriched_winners.json"

# Globals populated at startup
PROFILES: list[dict] = []
PROFILES_BY_LOGIN: dict[str, dict] = {}
PROFILES_BY_LANGUAGE: dict[str, list[dict]] = defaultdict(list)
SEARCH_STRINGS: dict[str, str] = {}  # login -> lowered searchable text
HACKATHON_PROJECTS: list[dict] = []
SEEDS: list[str] = []
ALL_LANGUAGES: list[str] = []


def load_data():
    global PROFILES, SEEDS, HACKATHON_PROJECTS, ALL_LANGUAGES

    # Clear to avoid double-loading on reimport
    PROFILES.clear()
    PROFILES_BY_LOGIN.clear()
    PROFILES_BY_LANGUAGE.clear()
    SEARCH_STRINGS.clear()
    HACKATHON_PROJECTS.clear()
    SEEDS.clear()
    ALL_LANGUAGES.clear()

    raw = json.loads(MERGED_FILE.read_text())
    SEEDS.extend(raw.get("seeds", []))
    PROFILES.extend(raw["profiles"])

    lang_counts: dict[str, int] = defaultdict(int)

    for p in PROFILES:
        login = p["login"]
        PROFILES_BY_LOGIN[login] = p

        for lang in p.get("top_languages", []):
            PROFILES_BY_LANGUAGE[lang].append(p)
            lang_counts[lang] += 1

        # Build full-text search string
        parts = [
            login,
            p.get("name") or "",
            p.get("bio") or "",
            p.get("company") or "",
            p.get("location") or "",
            " ".join(p.get("top_languages", [])),
            " ".join(o.get("login", "") for o in p.get("orgs", [])),
            " ".join(r.get("name", "") + " " + (r.get("desc") or "") for r in p.get("top_repos", [])),
        ]
        SEARCH_STRINGS[login] = " ".join(parts).lower()

    ALL_LANGUAGES.extend(
        lang for lang, _ in sorted(lang_counts.items(), key=lambda x: -x[1])
    )

    if ENRICHED_FILE.exists():
        HACKATHON_PROJECTS.extend(json.loads(ENRICHED_FILE.read_text()))

    # Compute cracked_score: favors young accounts with high output-per-year
    # This is the recruiting score — we want undiscovered talent, not famous devs
    from datetime import datetime, timezone
    for p in PROFILES:
        created = p.get("created_at", "")
        if created:
            try:
                age = max((datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days / 365.25, 0.5)
            except Exception:
                age = 5.0
        else:
            age = 5.0

        import math
        # Use log-scaled stars to avoid mega-repos dominating
        stars = p.get("total_stars", 0)
        log_stars = math.log10(stars + 1)  # 0-6 scale
        stars_per_year = log_stars / age
        commits_per_year = min(p.get("total_commits", 0), 2000) / age
        prs_per_year = min(p.get("total_prs", 0), 300) / age

        # Youth multiplier: exponential bonus for young accounts
        # <2y: 5x, 3y: 3.3x, 5y: 2x, 8y: 1x, 12y: 0.3x, 15y+: 0.15x
        if age < 8:
            youth_mult = max(1.0, 5.0 - age * 0.5)
        else:
            youth_mult = max(0.1, 1.0 - (age - 8) * 0.13)

        # Penalize follow-farming (high followers, low stars/commits)
        output = stars + p.get("total_commits", 0) * 2 + p.get("total_prs", 0) * 5
        follow_ratio = p.get("followers", 0) / max(output, 1)
        farm_penalty = min(1.0, 2.0 / max(follow_ratio, 0.01)) if follow_ratio > 5 else 1.0

        # Famous penalty: if followers > 10K, they're already discovered
        famous_penalty = 1.0
        if p.get("followers", 0) > 20000:
            famous_penalty = 0.2
        elif p.get("followers", 0) > 10000:
            famous_penalty = 0.5
        elif p.get("followers", 0) > 5000:
            famous_penalty = 0.7

        # Network bonus (found via our seeds = good signal)
        net_bonus = 1.5 if p.get("in_multiple_networks") else 1.0
        mutual_bonus = 1.3 if p.get("is_mutual_follow") else 1.0

        cracked = (
            stars_per_year * 100.0
            + commits_per_year * 0.3
            + prs_per_year * 2.0
            + p.get("public_repos", 0) * 0.1
        ) * youth_mult * farm_penalty * famous_penalty * net_bonus * mutual_bonus

        p["cracked_score"] = round(cracked, 1)
        p["account_age_years"] = round(age, 1)

    # Sort profiles by cracked_score by default
    PROFILES.sort(key=lambda p: -p.get("cracked_score", 0))

    print(f"Loaded {len(PROFILES)} profiles, {len(HACKATHON_PROJECTS)} hackathon projects, {len(ALL_LANGUAGES)} languages")


load_data()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Talent Discovery Platform")


@app.get("/")
async def serve_frontend():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


@app.get("/api/stats")
async def stats():
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


@app.get("/api/profiles")
async def list_profiles(
    q: str | None = Query(None),
    language: str | None = Query(None),
    min_score: float = Query(0),
    sort: str = Query("cracked"),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
):
    results = PROFILES

    # Text search
    if q:
        q_lower = q.lower()
        results = [p for p in results if q_lower in SEARCH_STRINGS.get(p["login"], "")]

    # Language filter
    if language:
        results = [p for p in results if language in p.get("top_languages", [])]

    # Score filter
    if min_score > 0:
        results = [p for p in results if p.get("score", 0) >= min_score]

    # Sort
    if sort == "cracked":
        results = sorted(results, key=lambda p: -p.get("cracked_score", 0))
    elif sort == "score":
        results = sorted(results, key=lambda p: -p.get("score", 0))
    elif sort == "stars":
        results = sorted(results, key=lambda p: -p.get("total_stars", 0))
    elif sort == "followers":
        results = sorted(results, key=lambda p: -p.get("followers", 0))
    elif sort == "commits":
        results = sorted(results, key=lambda p: -p.get("total_commits", 0))

    total = len(results)
    results = results[offset : offset + limit]

    return {"total": total, "offset": offset, "limit": limit, "profiles": results}


@app.get("/api/profiles/{login}")
async def get_profile(login: str):
    profile = PROFILES_BY_LOGIN.get(login)
    if not profile:
        return JSONResponse({"error": "Not found"}, status_code=404)

    # Find hackathon projects this developer contributed to
    projects = []
    for proj in HACKATHON_PROJECTS:
        contributors = proj.get("repo_data", {}).get("contributors", []) if proj.get("repo_data") else []
        team = proj.get("team_members", [])
        gh_logins = [
            c.get("github_username") for c in contributors if c.get("github_username")
        ] + [
            (m.get("github", "") or "").rstrip("/").split("/")[-1]
            for m in team if m.get("github")
        ]
        if login in gh_logins:
            projects.append({
                "title": proj.get("title"),
                "tagline": proj.get("tagline"),
                "hackathon": proj.get("hackathon"),
                "prizes": proj.get("prizes", []),
                "url": proj.get("url"),
            })

    return {**profile, "hackathon_projects": projects}


@app.get("/api/graph/{login}")
async def get_graph(login: str):
    profile = PROFILES_BY_LOGIN.get(login)
    if not profile:
        return JSONResponse({"error": "Not found"}, status_code=404)

    nodes = [{"id": login, "label": profile.get("name") or login, "type": "target",
              "score": profile.get("score", 0), "avatar": f"https://github.com/{login}.png"}]
    edges = []

    connections = profile.get("connections", {})
    found_via = profile.get("found_via", [])

    # Add seed nodes
    for seed in found_via:
        if seed not in PROFILES_BY_LOGIN:
            continue
        seed_p = PROFILES_BY_LOGIN.get(seed, {})
        nodes.append({
            "id": seed,
            "label": seed_p.get("name") or seed,
            "type": "seed",
            "score": seed_p.get("score", 0),
            "avatar": f"https://github.com/{seed}.png",
        })

        rels = connections.get(seed, [])
        for rel in rels:
            if rel == "follower":
                edges.append({"source": login, "target": seed, "type": "follows"})
            elif rel == "following":
                edges.append({"source": seed, "target": login, "type": "follows"})

    # Add other connected profiled developers (shared seed networks)
    connected_logins = set()
    for p in PROFILES:
        if p["login"] == login:
            continue
        p_found = set(p.get("found_via", []))
        shared = p_found & set(found_via)
        if shared and len(connected_logins) < 12:
            connected_logins.add(p["login"])
            nodes.append({
                "id": p["login"],
                "label": p.get("name") or p["login"],
                "type": "peer",
                "score": p.get("score", 0),
                "avatar": f"https://github.com/{p['login']}.png",
            })
            for seed in shared:
                edges.append({"source": p["login"], "target": seed, "type": "shared_network"})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# MCP Tools for Agent
# ---------------------------------------------------------------------------

from claude_code_sdk import tool, create_sdk_mcp_server, query, ClaudeCodeOptions, TextBlock, ToolUseBlock, ToolResultBlock, AssistantMessage, ResultMessage

TOOL_LIST = []


@tool("search_developers", "Search developers by text query across names, bios, languages, repos, orgs. Returns top matches with scores.",
      {"type": "object", "properties": {"query": {"type": "string", "description": "Search text"}, "limit": {"type": "integer", "description": "Max results (default 10)"}}, "required": ["query"]})
async def search_developers_tool(params):
    q = params["query"].lower()
    limit = params.get("limit", 10)
    results = []
    for p in PROFILES:
        if q in SEARCH_STRINGS.get(p["login"], ""):
            results.append(p)
    results.sort(key=lambda p: -p.get("score", 0))
    results = results[:limit]
    return {"count": len(results), "developers": [
        {"login": p["login"], "name": p.get("name"), "score": p.get("score", 0),
         "bio": p.get("bio"), "location": p.get("location"),
         "followers": p.get("followers", 0), "total_stars": p.get("total_stars", 0),
         "total_commits": p.get("total_commits", 0), "total_prs": p.get("total_prs", 0),
         "top_languages": p.get("top_languages", []),
         "top_repo": p["top_repos"][0] if p.get("top_repos") else None,
         "in_multiple_networks": p.get("in_multiple_networks", False),
         "is_mutual_follow": p.get("is_mutual_follow", False)}
        for p in results
    ]}

TOOL_LIST.append(search_developers_tool)


@tool("get_developer_profile", "Get full profile for a developer by GitHub login.",
      {"type": "object", "properties": {"login": {"type": "string", "description": "GitHub username"}}, "required": ["login"]})
async def get_developer_profile_tool(params):
    login = params["login"]
    p = PROFILES_BY_LOGIN.get(login)
    if not p:
        return {"error": f"Developer '{login}' not found in dataset"}
    return {k: v for k, v in p.items() if k != "connections"}

TOOL_LIST.append(get_developer_profile_tool)


@tool("search_hackathon_projects", "Search hackathon projects by text or language.",
      {"type": "object", "properties": {"query": {"type": "string", "description": "Search text"}, "language": {"type": "string", "description": "Filter by programming language"}}, "required": []})
async def search_hackathon_projects_tool(params):
    q = (params.get("query") or "").lower()
    lang = params.get("language")
    results = []
    for proj in HACKATHON_PROJECTS:
        text = " ".join([
            proj.get("title", ""), proj.get("tagline", ""),
            proj.get("hackathon", ""),
            (proj.get("repo_data") or {}).get("description", "") or "",
            " ".join((proj.get("repo_data") or {}).get("topics", [])),
        ]).lower()
        repo_lang = (proj.get("repo_data") or {}).get("language", "")

        if q and q not in text:
            continue
        if lang and lang.lower() != (repo_lang or "").lower():
            continue

        results.append({
            "title": proj.get("title"),
            "tagline": proj.get("tagline"),
            "hackathon": proj.get("hackathon"),
            "prizes": proj.get("prizes", []),
            "url": proj.get("url"),
            "language": repo_lang,
            "stars": (proj.get("repo_data") or {}).get("stars", 0),
            "contributors": [
                c.get("github_username") for c in
                ((proj.get("repo_data") or {}).get("contributors", []))
                if c.get("github_username")
            ],
        })
    results.sort(key=lambda x: -x.get("stars", 0))
    return {"count": len(results), "projects": results[:15]}

TOOL_LIST.append(search_hackathon_projects_tool)


@tool("get_network_connections", "Get a developer's network connections to seeds and other profiled developers.",
      {"type": "object", "properties": {"login": {"type": "string", "description": "GitHub username"}}, "required": ["login"]})
async def get_network_connections_tool(params):
    login = params["login"]
    p = PROFILES_BY_LOGIN.get(login)
    if not p:
        return {"error": f"Developer '{login}' not found"}
    return {
        "login": login,
        "found_via": p.get("found_via", []),
        "connections": p.get("connections", {}),
        "in_multiple_networks": p.get("in_multiple_networks", False),
        "is_mutual_follow": p.get("is_mutual_follow", False),
    }

TOOL_LIST.append(get_network_connections_tool)


@tool("fetch_github_profile", "Live-fetch a GitHub profile not in our dataset. Uses GitHub GraphQL API.",
      {"type": "object", "properties": {"login": {"type": "string", "description": "GitHub username to fetch"}}, "required": ["login"]})
async def fetch_github_profile_tool(params):
    login = params["login"]
    # Check cache first
    if login in PROFILES_BY_LOGIN:
        return {"source": "cached", **{k: v for k, v in PROFILES_BY_LOGIN[login].items() if k != "connections"}}

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from crawl import get_user_profile, score_user, get_gh_token
        import httpx

        token = get_gh_token()
        headers = {"Authorization": f"bearer {token}", "Content-Type": "application/json"}
        sem = asyncio.Semaphore(5)
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            profile = await get_user_profile(client, login, sem)
            if not profile:
                return {"error": f"Could not fetch profile for '{login}'"}
            profile.score = score_user(profile)
            from dataclasses import asdict
            return {"source": "live_fetch", **asdict(profile)}
    except Exception as e:
        return {"error": f"Failed to fetch: {str(e)}"}

TOOL_LIST.append(fetch_github_profile_tool)


# Create MCP server config
MCP_SERVER = create_sdk_mcp_server("talent-search", version="1.0.0", tools=TOOL_LIST)

AGENT_SYSTEM_PROMPT = """You are a talent discovery agent for a recruiting platform. You have access to a dataset of 1,194 profiled GitHub developers crawled from interconnected social networks, plus 518 hackathon projects.

Your job is to help find and analyze developers based on natural language queries.

**Workflow:**
1. Use search_developers to find candidates matching the query
2. Use get_developer_profile for deeper dives on promising candidates
3. Use search_hackathon_projects to find relevant project experience
4. Use get_network_connections to understand social graph position
5. Use fetch_github_profile for developers not in the dataset

**Response format:**
- Lead with a brief summary of findings
- For each candidate, mention: name, @login, key stats (stars, commits, PRs), languages, notable repos
- Highlight network connections (MULTI-NET = found via multiple seed networks, MUTUAL = mutual follow with seeds)
- Rank by relevance to the query, not just raw score
- Be concise but data-rich

**Important:** Always cite specific numbers from the data. Don't make up information."""


# ---------------------------------------------------------------------------
# SSE Streaming endpoints
# ---------------------------------------------------------------------------

# Ensure claude CLI is findable by the SDK subprocess
_AGENT_ENV = {"PATH": os.environ.get("PATH", "") + ":/Users/ulissemini/.local/bin"}
_AGENT_TOOLS = [
    "mcp__talent__search_developers",
    "mcp__talent__get_developer_profile",
    "mcp__talent__search_hackathon_projects",
    "mcp__talent__get_network_connections",
    "mcp__talent__fetch_github_profile",
]

async def _streaming_prompt(text: str):
    """Wrap a string prompt into an async iterable for streaming mode.
    This is required because SDK MCP tools need streaming mode to respond
    to control requests via stdin (string mode closes stdin immediately)."""
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


def _search_local(q: str, limit: int = 20) -> list[dict]:
    """Smart local search across profiles."""
    q_lower = q.lower()
    keywords = q_lower.split()
    results = []
    for p in PROFILES:
        search_text = SEARCH_STRINGS.get(p["login"], "")
        # Score by keyword matches
        match_count = sum(1 for kw in keywords if kw in search_text)
        if match_count > 0:
            results.append((p, match_count))
    # Sort by match count then cracked_score
    results.sort(key=lambda x: (-x[1], -x[0].get("cracked_score", 0)))
    return [p for p, _ in results[:limit]]


def _format_profiles_for_llm(profiles: list[dict], limit: int = 15) -> str:
    """Format profiles as compact text for LLM context."""
    lines = []
    for p in profiles[:limit]:
        top_repo = p["top_repos"][0] if p.get("top_repos") else None
        repo_str = f', top repo: {top_repo["name"]} ({top_repo["stars"]}★)' if top_repo else ""
        orgs = ", ".join(o["login"] for o in p.get("orgs", [])[:3])
        lines.append(
            f'@{p["login"]} ({p.get("name","?")}): '
            f'score={p.get("cracked_score",0):.0f}, age={p.get("account_age_years","?")}y, '
            f'{p.get("followers",0)} followers, {p.get("total_stars",0)}★, '
            f'{p.get("total_commits",0)} commits, {p.get("total_prs",0)} PRs, '
            f'langs=[{", ".join(p.get("top_languages",[])[:4])}]'
            f'{repo_str}'
            f'{", orgs: " + orgs if orgs else ""}'
            f'{", bio: " + p["bio"][:60] if p.get("bio") else ""}'
            f'{" [MULTI-NET]" if p.get("in_multiple_networks") else ""}'
            f'{" [MUTUAL]" if p.get("is_mutual_follow") else ""}'
        )
    return "\n".join(lines)


@app.get("/api/search")
async def agent_search(request: Request, q: str = Query(...)):
    # Pre-search locally to provide context
    local_results = _search_local(q, limit=25)
    # Also get top cracked devs as fallback
    top_cracked = sorted(PROFILES, key=lambda p: -p.get("cracked_score", 0))[:20]

    context = f"""SEARCH RESULTS for "{q}" ({len(local_results)} matches):
{_format_profiles_for_llm(local_results)}

TOP CRACKED DEVELOPERS (for reference):
{_format_profiles_for_llm(top_cracked)}

DATASET: {len(PROFILES)} developers from interconnected GitHub networks of {len(SEEDS)} seeds.
"""

    prompt = f"""{AGENT_SYSTEM_PROMPT}

Here is the developer data from our dataset:

{context}

User query: {q}

Analyze the data above and provide a detailed response. Recommend specific developers with their stats. Be data-driven and cite specific numbers."""

    async def event_stream():
        start = time.time()
        try:
            yield {"event": "tool_call", "data": json.dumps({"tool": "search_developers", "input": {"query": q}})}
            yield {"event": "tool_result", "data": json.dumps({"tool_use_id": "local", "content": f"Found {len(local_results)} matches"})}

            options = ClaudeCodeOptions(
                permission_mode="bypassPermissions",
                max_turns=1,
                include_partial_messages=True,
                env=_AGENT_ENV,
            )
            async for msg in query(prompt=prompt, options=options):
                if await request.is_disconnected():
                    break

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield {"event": "text", "data": json.dumps({"text": block.text})}

                elif isinstance(msg, ResultMessage):
                    elapsed = time.time() - start
                    yield {"event": "done", "data": json.dumps({
                        "duration": round(elapsed, 1),
                        "cost": msg.total_cost_usd,
                        "turns": msg.num_turns,
                    })}

        except Exception as e:
            import traceback
            print(f"Agent search error: {traceback.format_exc()}", flush=True)
            yield {"event": "error", "data": json.dumps({"error": str(e)})}

    return EventSourceResponse(event_stream())


@app.get("/api/dossier/{login}")
async def agent_dossier(request: Request, login: str):
    profile = PROFILES_BY_LOGIN.get(login)
    if not profile:
        return JSONResponse({"error": "Not found"}, status_code=404)

    # Gather all data locally
    profile_text = _format_profiles_for_llm([profile], limit=1)

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

    connections = profile.get("connections", {})
    found_via = profile.get("found_via", [])

    dossier_prompt = f"""{AGENT_SYSTEM_PROMPT}

Generate a recruiting dossier for @{login}.

FULL PROFILE:
{json.dumps({k: v for k, v in profile.items() if k != "connections"}, indent=2)}

NETWORK: Found via: {found_via}, connections: {json.dumps(connections)}
Multi-network: {profile.get("in_multiple_networks")}, Mutual follow: {profile.get("is_mutual_follow")}

HACKATHON PROJECTS: {json.dumps(projects) if projects else "None found"}

Cover: Overview, Technical Profile, Collaboration Signal, Hackathon Record, Network Position, Recruiting Assessment.
Cite numbers. Be honest about gaps."""

    async def event_stream():
        start = time.time()
        try:
            yield {"event": "tool_call", "data": json.dumps({"tool": "get_developer_profile", "input": {"login": login}})}
            yield {"event": "tool_result", "data": json.dumps({"tool_use_id": "local", "content": "Profile loaded"})}

            options = ClaudeCodeOptions(
                permission_mode="bypassPermissions",
                max_turns=1,
                include_partial_messages=True,
                env=_AGENT_ENV,
            )
            async for msg in query(prompt=dossier_prompt, options=options):
                if await request.is_disconnected():
                    break

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield {"event": "text", "data": json.dumps({"text": block.text})}

                elif isinstance(msg, ResultMessage):
                    elapsed = time.time() - start
                    yield {"event": "done", "data": json.dumps({
                        "duration": round(elapsed, 1),
                        "cost": msg.total_cost_usd,
                        "turns": msg.num_turns,
                    })}

        except Exception as e:
            import traceback
            print(f"Dossier error: {traceback.format_exc()}", flush=True)
            yield {"event": "error", "data": json.dumps({"error": str(e)})}

    return EventSourceResponse(event_stream())


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
