#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""
Scrape GitHub repos from hackathon winners data.
Extracts: repo metadata, languages, contributor emails from commits.
Uses `gh auth token` for auth - no token management needed.
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

INPUT_FILE = Path.home() / "liquidhacks" / "hackathon_winners.json"
OUTPUT_FILE = Path(__file__).parent / "enriched_winners.json"
MAX_CONCURRENT = 20  # parallel requests


def get_gh_token() -> str:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0:
        print("Error: `gh auth token` failed. Make sure gh CLI is authenticated.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def parse_repo(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL."""
    parsed = urlparse(url.rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


async def fetch_json(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> dict | list | None:
    async with sem:
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                # rate limited - wait and retry once
                reset = int(e.response.headers.get("x-ratelimit-reset", 0))
                wait = max(reset - int(time.time()), 1)
                print(f"  Rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
            print(f"  HTTP {e.response.status_code} for {url}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  Error fetching {url}: {e}", file=sys.stderr)
            return None


async def scrape_repo(client: httpx.AsyncClient, repo: str, sem: asyncio.Semaphore) -> dict | None:
    base = f"https://api.github.com/repos/{repo}"

    # fetch repo info, languages, and commits in parallel
    info, languages, commits = await asyncio.gather(
        fetch_json(client, base, sem),
        fetch_json(client, f"{base}/languages", sem),
        fetch_json(client, f"{base}/commits?per_page=100", sem),
    )

    if info is None:
        return None

    # extract unique contributor emails from commits
    contributors = {}
    if isinstance(commits, list):
        for c in commits:
            commit = c.get("commit", {})
            for role in ("author", "committer"):
                person = commit.get(role, {})
                email = person.get("email", "")
                name = person.get("name", "")
                if not email or "noreply" in email or email == "":
                    continue
                if email not in contributors:
                    # try to get GitHub username from top-level author/committer
                    gh_user = None
                    top_level = c.get(role)
                    if isinstance(top_level, dict):
                        gh_user = top_level.get("login")
                    contributors[email] = {
                        "name": name,
                        "email": email,
                        "github_username": gh_user,
                    }

    return {
        "full_name": info.get("full_name"),
        "description": info.get("description"),
        "language": info.get("language"),
        "languages": languages or {},
        "stars": info.get("stargazers_count", 0),
        "forks": info.get("forks_count", 0),
        "topics": info.get("topics", []),
        "created_at": info.get("created_at"),
        "updated_at": info.get("updated_at"),
        "contributors": list(contributors.values()),
    }


async def main():
    data = json.loads(INPUT_FILE.read_text())
    token = get_gh_token()

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    # collect all repos to scrape
    repo_map: dict[str, list[int]] = {}  # repo -> list of project indices
    for i, project in enumerate(data):
        gh_url = project.get("links", {}).get("github")
        if not gh_url:
            continue
        repo = parse_repo(gh_url)
        if repo:
            repo_map.setdefault(repo, []).append(i)

    repos = list(repo_map.keys())
    print(f"Scraping {len(repos)} repos from {len(data)} projects...")

    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        results_list = await asyncio.gather(*[scrape_repo(client, repo, sem) for repo in repos])

    # enrich original data
    repo_results = dict(zip(repos, results_list))
    enriched_count = 0
    email_count = 0
    for project in data:
        gh_url = project.get("links", {}).get("github")
        if not gh_url:
            continue
        repo = parse_repo(gh_url)
        if repo and repo_results.get(repo):
            project["repo_data"] = repo_results[repo]
            enriched_count += 1
            email_count += len(repo_results[repo]["contributors"])

    OUTPUT_FILE.write_text(json.dumps(data, indent=2))

    print(f"\nDone!")
    print(f"  Enriched {enriched_count}/{len(data)} projects")
    print(f"  Found {email_count} unique contributor emails")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
