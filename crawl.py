#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""
GitHub social graph crawler.
Given a seed user, fetches their followers/following, profiles each one via
GraphQL, and surfaces the most interesting ("cracked") developers.

Reusable functions:
  - get_gh_token() -> str
  - get_network(client, login) -> {followers: [str], following: [str]}
  - get_user_profiles(client, logins) -> [UserProfile]
  - score_user(profile) -> float
  - crawl(seed, depth=1) -> full pipeline
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

GRAPHQL_URL = "https://api.github.com/graphql"
MAX_CONCURRENT = 10
OUTPUT_DIR = Path(__file__).parent / "data"


def get_gh_token(token: str | None = None) -> str:
    if token:
        return token
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0:
        print("Error: `gh auth token` failed.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


@dataclass
class UserProfile:
    login: str
    name: str | None = None
    bio: str | None = None
    company: str | None = None
    location: str | None = None
    email: str | None = None
    twitter: str | None = None
    website: str | None = None
    created_at: str | None = None
    followers: int = 0
    following: int = 0
    public_repos: int = 0
    total_commits: int = 0
    total_prs: int = 0
    top_repos: list = field(default_factory=list)
    total_stars: int = 0
    top_languages: list = field(default_factory=list)
    orgs: list = field(default_factory=list)
    is_follower: bool = False
    is_following: bool = False
    score: float = 0.0


async def gql(client: httpx.AsyncClient, query: str, sem: asyncio.Semaphore) -> dict | None:
    """Execute a GraphQL query with rate limit handling."""
    async with sem:
        try:
            resp = await client.post(GRAPHQL_URL, json={"query": query})
            if resp.status_code == 403:
                reset = int(resp.headers.get("x-ratelimit-reset", 0))
                wait = max(reset - int(time.time()), 1)
                print(f"  Rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
                resp = await client.post(GRAPHQL_URL, json={"query": query})
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                # partial data is fine, just log
                for err in data["errors"]:
                    if "Could not resolve" not in err.get("message", ""):
                        print(f"  GQL warning: {err['message']}", file=sys.stderr)
            return data.get("data")
        except Exception as e:
            print(f"  GQL error: {e}", file=sys.stderr)
            return None


async def get_network(client: httpx.AsyncClient, login: str, sem: asyncio.Semaphore) -> dict:
    """Get followers and following lists for a user."""
    query = """
    {
      user(login: "%s") {
        followers(first: 100) {
          nodes { login }
          totalCount
          pageInfo { hasNextPage endCursor }
        }
        following(first: 100) {
          nodes { login }
          totalCount
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """ % login

    data = await gql(client, query, sem)
    if not data or not data.get("user"):
        return {"followers": [], "following": []}

    user = data["user"]
    followers = [n["login"] for n in user["followers"]["nodes"]]
    following = [n["login"] for n in user["following"]["nodes"]]

    # paginate if needed (>100)
    for edge_type in ("followers", "following"):
        page_info = user[edge_type]["pageInfo"]
        lst = followers if edge_type == "followers" else following
        while page_info["hasNextPage"]:
            pq = '{user(login: "%s") { %s(first: 100, after: "%s") { nodes { login } pageInfo { hasNextPage endCursor } } } }' % (
                login, edge_type, page_info["endCursor"]
            )
            pdata = await gql(client, pq, sem)
            if not pdata or not pdata.get("user"):
                break
            nodes = pdata["user"][edge_type]["nodes"]
            lst.extend(n["login"] for n in nodes)
            page_info = pdata["user"][edge_type]["pageInfo"]

    return {"followers": followers, "following": following}


async def get_user_profile(client: httpx.AsyncClient, login: str, sem: asyncio.Semaphore) -> UserProfile | None:
    """Fetch detailed profile for a single user."""
    query = """
    {
      user(login: "%s") {
        login name bio company location twitterUsername websiteUrl createdAt
        followers { totalCount }
        following { totalCount }
        repositories(first: 10, ownerAffiliations: OWNER, orderBy: {field: STARGAZERS, direction: DESC}) {
          totalCount
          nodes {
            name stargazerCount primaryLanguage { name }
            description isFork
          }
        }
        contributionsCollection {
          totalCommitContributions
          totalPullRequestContributions
        }
        organizations(first: 10) {
          nodes { login name }
        }
      }
    }
    """ % login

    data = await gql(client, query, sem)
    if not data or not data.get("user"):
        return None

    u = data["user"]
    repos = u["repositories"]["nodes"]
    non_fork_repos = [r for r in repos if not r["isFork"]]

    # aggregate stars and languages
    total_stars = sum(r["stargazerCount"] for r in non_fork_repos)
    lang_counts: dict[str, int] = {}
    for r in non_fork_repos:
        lang = r["primaryLanguage"]
        if lang:
            lang_counts[lang["name"]] = lang_counts.get(lang["name"], 0) + 1

    top_langs = sorted(lang_counts.items(), key=lambda x: -x[1])

    contribs = u["contributionsCollection"]

    return UserProfile(
        login=u["login"],
        name=u.get("name"),
        bio=u.get("bio"),
        company=u.get("company"),
        location=u.get("location"),
        email=None,
        twitter=u.get("twitterUsername"),
        website=u.get("websiteUrl"),
        created_at=u.get("createdAt"),
        followers=u["followers"]["totalCount"],
        following=u["following"]["totalCount"],
        public_repos=u["repositories"]["totalCount"],
        total_commits=contribs["totalCommitContributions"],
        total_prs=contribs["totalPullRequestContributions"],
        top_repos=[
            {"name": r["name"], "stars": r["stargazerCount"],
             "lang": r["primaryLanguage"]["name"] if r["primaryLanguage"] else None,
             "desc": r["description"]}
            for r in non_fork_repos[:5]
        ],
        total_stars=total_stars,
        top_languages=[lang for lang, _ in top_langs[:5]],
        orgs=[{"login": o["login"], "name": o.get("name")} for o in u["organizations"]["nodes"]],
    )


async def get_user_profiles(
    client: httpx.AsyncClient, logins: list[str], sem: asyncio.Semaphore
) -> list[UserProfile]:
    """Fetch profiles for a batch of users concurrently."""
    tasks = [get_user_profile(client, login, sem) for login in logins]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


def score_user(p: UserProfile) -> float:
    """
    Heuristic "cracked-ness" score. Higher = more interesting for recruiting.
    Biased toward: young accounts with high output, stars, PRs, and real projects.
    """
    # account age in years (younger + productive = more impressive)
    if p.created_at:
        from datetime import datetime, timezone
        created = datetime.fromisoformat(p.created_at.replace("Z", "+00:00"))
        age_years = max((datetime.now(timezone.utc) - created).days / 365.25, 0.5)
    else:
        age_years = 5.0  # assume average if unknown

    # stars per year
    stars_per_year = p.total_stars / age_years

    # commits per year (capped to avoid gaming)
    commits_per_year = min(p.total_commits, 3000) / age_years

    # PRs are a strong signal of collaboration
    pr_score = min(p.total_prs, 500)

    # follower ratio (followers vs following) - organic influence
    follower_score = p.followers

    score = (
        stars_per_year * 2.0         # stars are strong signal
        + commits_per_year * 0.05    # activity matters but diminishing
        + pr_score * 1.0             # PRs = collaboration
        + follower_score * 0.5       # social proof
        + p.public_repos * 0.2      # breadth
    )

    return round(score, 1)


async def crawl(seed: str, depth: int = 1, token: str | None = None, output_dir: Path | None = None):
    """
    Crawl from a seed user, fetch their network, profile everyone,
    score and rank them.
    """
    token = get_gh_token(token)
    output_dir = output_dir or OUTPUT_DIR
    headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
    }
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        # Step 1: Get seed's network
        print(f"Fetching network for @{seed}...")
        network = await get_network(client, seed, sem)
        followers_set = set(network["followers"])
        following_set = set(network["following"])
        all_logins = sorted(followers_set | following_set)
        print(f"  {len(followers_set)} followers, {len(following_set)} following, {len(all_logins)} unique")

        # Step 2: Profile everyone
        print(f"Profiling {len(all_logins)} users...")
        profiles = await get_user_profiles(client, all_logins, sem)
        print(f"  Got {len(profiles)} profiles")

        # Mark relationship to seed
        for p in profiles:
            p.is_follower = p.login in followers_set
            p.is_following = p.login in following_set

        # Step 3: Score and rank
        for p in profiles:
            p.score = score_user(p)
        profiles.sort(key=lambda p: -p.score)

        # Step 4: Save
        output_dir.mkdir(exist_ok=True)
        out_file = output_dir / f"network_{seed}.json"
        out_file.write_text(json.dumps({
            "seed": seed,
            "followers": network["followers"],
            "following": network["following"],
            "profiles": [asdict(p) for p in profiles],
        }, indent=2))

        # Step 5: Print results
        print(f"\n{'='*80}")
        print(f"Top developers in @{seed}'s network")
        print(f"{'='*80}\n")

        for i, p in enumerate(profiles[:30]):
            rel = []
            if p.is_follower:
                rel.append("follows you")
            if p.is_following:
                rel.append("you follow")
            rel_str = f" ({', '.join(rel)})" if rel else ""

            bio_str = f" - {p.bio[:60]}" if p.bio else ""
            loc_str = f" [{p.location}]" if p.location else ""

            print(f"{i+1:2}. @{p.login}{rel_str} (score: {p.score})")
            print(f"    {p.name or '?'}{loc_str}{bio_str}")
            print(f"    {p.followers} followers | {p.total_stars}★ | {p.total_commits} commits | {p.total_prs} PRs")
            if p.top_repos:
                top = p.top_repos[0]
                print(f"    Top repo: {top['name']} ({top['stars']}★) {top.get('desc','')[:50] if top.get('desc') else ''}")
            if p.orgs:
                print(f"    Orgs: {', '.join(o['login'] for o in p.orgs[:5])}")
            if p.email:
                print(f"    Email: {p.email}")
            print()

        print(f"Full data saved to {out_file}")


if __name__ == "__main__":
    seed = sys.argv[1] if len(sys.argv) > 1 else "ulissemini"
    asyncio.run(crawl(seed))
