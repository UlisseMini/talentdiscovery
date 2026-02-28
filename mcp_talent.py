#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]", "httpx"]
# ///
"""
MCP stdio server for talent search tools.
Spawned by the Claude Agent SDK as a subprocess.
Loads data from DATA_DIR (default: ./data/) and exposes search/profile tools.
"""

import json
import os
import sys
import asyncio
from collections import defaultdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("TALENT_DATA_DIR", Path(__file__).parent / "data"))
MERGED_FILE = DATA_DIR / "merged_network.json"
ENRICHED_FILE = Path(os.environ.get("ENRICHED_FILE", Path(__file__).parent / "enriched_winners.json"))

PROFILES: list[dict] = []
PROFILES_BY_LOGIN: dict[str, dict] = {}
SEARCH_STRINGS: dict[str, str] = {}
HACKATHON_PROJECTS: list[dict] = []
SEEDS: list[str] = []


def load_data():
    global PROFILES, SEEDS, HACKATHON_PROJECTS

    raw = json.loads(MERGED_FILE.read_text())
    SEEDS.extend(raw.get("seeds", []))
    PROFILES.extend(raw["profiles"])

    for p in PROFILES:
        login = p["login"]
        PROFILES_BY_LOGIN[login] = p
        parts = [
            login, p.get("name") or "", p.get("bio") or "",
            p.get("company") or "", p.get("location") or "",
            " ".join(p.get("top_languages", [])),
            " ".join(o.get("login", "") for o in p.get("orgs", [])),
            " ".join(r.get("name", "") + " " + (r.get("desc") or "") for r in p.get("top_repos", [])),
        ]
        SEARCH_STRINGS[login] = " ".join(parts).lower()

    if ENRICHED_FILE.exists():
        HACKATHON_PROJECTS.extend(json.loads(ENRICHED_FILE.read_text()))

    # Compute cracked_score
    from datetime import datetime, timezone
    import math
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
    print(f"[mcp_talent] Loaded {len(PROFILES)} profiles, {len(HACKATHON_PROJECTS)} projects", file=sys.stderr)


load_data()

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("talent-search")


@mcp.tool()
def search_developers(query: str, limit: int = 10) -> str:
    """Search developers by text query across names, bios, languages, repos, orgs. Returns top matches."""
    q = query.lower()
    results = [p for p in PROFILES if q in SEARCH_STRINGS.get(p["login"], "")]
    results.sort(key=lambda p: -p.get("cracked_score", 0))
    results = results[:limit]
    return json.dumps({"count": len(results), "developers": [
        {"login": p["login"], "name": p.get("name"), "cracked_score": p.get("cracked_score", 0),
         "bio": p.get("bio"), "location": p.get("location"),
         "followers": p.get("followers", 0), "total_stars": p.get("total_stars", 0),
         "total_commits": p.get("total_commits", 0), "total_prs": p.get("total_prs", 0),
         "top_languages": p.get("top_languages", []),
         "account_age_years": p.get("account_age_years"),
         "top_repo": p["top_repos"][0] if p.get("top_repos") else None,
         "in_multiple_networks": p.get("in_multiple_networks", False),
         "is_mutual_follow": p.get("is_mutual_follow", False)}
        for p in results
    ]})


@mcp.tool()
def get_developer_profile(login: str) -> str:
    """Get full profile for a developer by GitHub login."""
    p = PROFILES_BY_LOGIN.get(login)
    if not p:
        return json.dumps({"error": f"Developer '{login}' not found in dataset"})
    return json.dumps({k: v for k, v in p.items() if k != "connections"})


@mcp.tool()
def search_hackathon_projects(query: str = "", language: str = "") -> str:
    """Search hackathon projects by text or language."""
    q = query.lower()
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
        if language and language.lower() != (repo_lang or "").lower():
            continue
        results.append({
            "title": proj.get("title"), "tagline": proj.get("tagline"),
            "hackathon": proj.get("hackathon"), "prizes": proj.get("prizes", []),
            "url": proj.get("url"), "language": repo_lang,
            "stars": (proj.get("repo_data") or {}).get("stars", 0),
            "contributors": [
                c.get("github_username") for c in
                ((proj.get("repo_data") or {}).get("contributors", []))
                if c.get("github_username")
            ],
        })
    results.sort(key=lambda x: -x.get("stars", 0))
    return json.dumps({"count": len(results), "projects": results[:15]})


@mcp.tool()
def get_network_connections(login: str) -> str:
    """Get a developer's network connections to seeds and other profiled developers."""
    p = PROFILES_BY_LOGIN.get(login)
    if not p:
        return json.dumps({"error": f"Developer '{login}' not found"})
    return json.dumps({
        "login": login,
        "found_via": p.get("found_via", []),
        "connections": p.get("connections", {}),
        "in_multiple_networks": p.get("in_multiple_networks", False),
        "is_mutual_follow": p.get("is_mutual_follow", False),
    })


@mcp.tool()
def fetch_github_profile(login: str) -> str:
    """Live-fetch a GitHub profile not in our dataset using GitHub API. Requires GITHUB_TOKEN env var."""
    if login in PROFILES_BY_LOGIN:
        p = PROFILES_BY_LOGIN[login]
        return json.dumps({"source": "cached", **{k: v for k, v in p.items() if k != "connections"}})

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return json.dumps({"error": "No GITHUB_TOKEN available for live fetching"})

    # Synchronous fetch using httpx
    import httpx
    headers = {"Authorization": f"bearer {token}", "Content-Type": "application/json"}
    query_gql = """
    { user(login: "%s") {
        login name bio company location twitterUsername websiteUrl createdAt
        followers { totalCount } following { totalCount }
        repositories(first: 10, ownerAffiliations: OWNER, orderBy: {field: STARGAZERS, direction: DESC}) {
          totalCount nodes { name stargazerCount primaryLanguage { name } description isFork }
        }
        contributionsCollection { totalCommitContributions totalPullRequestContributions }
        organizations(first: 10) { nodes { login name } }
    }}""" % login

    try:
        resp = httpx.post("https://api.github.com/graphql", json={"query": query_gql}, headers=headers, timeout=15)
        data = resp.json().get("data", {}).get("user")
        if not data:
            return json.dumps({"error": f"User '{login}' not found on GitHub"})
        repos = [r for r in data["repositories"]["nodes"] if not r["isFork"]]
        total_stars = sum(r["stargazerCount"] for r in repos)
        lang_counts: dict[str, int] = {}
        for r in repos:
            if r["primaryLanguage"]:
                lang_counts[r["primaryLanguage"]["name"]] = lang_counts.get(r["primaryLanguage"]["name"], 0) + 1
        return json.dumps({
            "source": "live_fetch", "login": data["login"], "name": data.get("name"),
            "bio": data.get("bio"), "company": data.get("company"), "location": data.get("location"),
            "followers": data["followers"]["totalCount"], "following": data["following"]["totalCount"],
            "public_repos": data["repositories"]["totalCount"],
            "total_commits": data["contributionsCollection"]["totalCommitContributions"],
            "total_prs": data["contributionsCollection"]["totalPullRequestContributions"],
            "total_stars": total_stars,
            "top_repos": [{"name": r["name"], "stars": r["stargazerCount"],
                          "lang": r["primaryLanguage"]["name"] if r["primaryLanguage"] else None,
                          "desc": r["description"]} for r in repos[:5]],
            "top_languages": sorted(lang_counts, key=lambda l: -lang_counts[l])[:5],
            "orgs": [{"login": o["login"], "name": o.get("name")} for o in data["organizations"]["nodes"]],
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch: {str(e)}"})


@mcp.tool()
def get_top_developers(sort_by: str = "cracked_score", limit: int = 20) -> str:
    """Get top developers sorted by a metric. sort_by: cracked_score, total_stars, followers, total_commits, total_prs."""
    key = sort_by if sort_by in ("cracked_score", "total_stars", "followers", "total_commits", "total_prs") else "cracked_score"
    sorted_profiles = sorted(PROFILES, key=lambda p: -p.get(key, 0))[:limit]
    return json.dumps({"count": len(sorted_profiles), "developers": [
        {"login": p["login"], "name": p.get("name"), "cracked_score": p.get("cracked_score", 0),
         "bio": p.get("bio"), "location": p.get("location"),
         "followers": p.get("followers", 0), "total_stars": p.get("total_stars", 0),
         "total_commits": p.get("total_commits", 0), "total_prs": p.get("total_prs", 0),
         "top_languages": p.get("top_languages", []),
         "account_age_years": p.get("account_age_years"),
         "in_multiple_networks": p.get("in_multiple_networks", False)}
        for p in sorted_profiles
    ]})


@mcp.tool()
def get_dataset_stats() -> str:
    """Get statistics about the dataset: total developers, languages, seeds, etc."""
    lang_counts: dict[str, int] = defaultdict(int)
    for p in PROFILES:
        for lang in p.get("top_languages", []):
            lang_counts[lang] += 1
    top_langs = sorted(lang_counts.items(), key=lambda x: -x[1])[:20]
    return json.dumps({
        "total_developers": len(PROFILES),
        "total_projects": len(HACKATHON_PROJECTS),
        "seeds": SEEDS,
        "top_languages": [l for l, _ in top_langs],
        "total_stars": sum(p.get("total_stars", 0) for p in PROFILES),
        "total_commits": sum(p.get("total_commits", 0) for p in PROFILES),
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")
