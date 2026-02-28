"""
Modal app: GitHub Intelligence Platform
FastAPI web service + Claude Agent SDK chat
"""

import glob
import json
import math
import os
import random
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
    .add_local_file("viz.html", remote_path="/app/viz.html")
    .add_local_file("promising.html", remote_path="/app/promising.html")
    .add_local_file("sessions.html", remote_path="/app/sessions.html")
)

volume = modal.Volume.from_name("talent-data")
crawl_state = modal.Dict.from_name("crawl-state", create_if_missing=True)

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
GRAPH_DATA: dict = {}
_DATA_LOADED = False
_LAST_RELOAD: float = 0.0


# ---------------------------------------------------------------------------
# Pure-Python graph algorithms (no networkx dependency)
# ---------------------------------------------------------------------------

def _compute_pagerank(adj: dict[str, set[str]], nodes: list[str], iterations: int = 20, damping: float = 0.85) -> dict[str, float]:
    """Power-iteration PageRank on an undirected graph."""
    n = len(nodes)
    if n == 0:
        return {}
    rank = {v: 1.0 / n for v in nodes}
    for _ in range(iterations):
        new_rank = {}
        for v in nodes:
            s = sum(rank[u] / len(adj[u]) for u in adj[v] if adj[u])
            new_rank[v] = (1 - damping) / n + damping * s
        rank = new_rank
    return rank


def _compute_betweenness(adj: dict[str, set[str]], nodes: list[str]) -> dict[str, float]:
    """Brandes algorithm for betweenness centrality on an undirected graph."""
    cb = {v: 0.0 for v in nodes}
    for s in nodes:
        # BFS
        stack = []
        pred: dict[str, list[str]] = {v: [] for v in nodes}
        sigma = {v: 0.0 for v in nodes}
        sigma[s] = 1.0
        dist = {v: -1 for v in nodes}
        dist[s] = 0
        queue = [s]
        qi = 0
        while qi < len(queue):
            v = queue[qi]
            qi += 1
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = {v: 0.0 for v in nodes}
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
            if w != s:
                cb[w] += delta[w]
    # Normalize for undirected graph
    n = len(nodes)
    if n > 2:
        norm = 1.0 / ((n - 1) * (n - 2))
        for v in nodes:
            cb[v] *= norm
    return cb


def _compute_communities(adj: dict[str, set[str]], nodes: list[str], iterations: int = 10) -> dict[str, int]:
    """Label propagation community detection."""
    label = {v: i for i, v in enumerate(nodes)}
    node_list = list(nodes)
    for _ in range(iterations):
        random.shuffle(node_list)
        changed = False
        for v in node_list:
            if not adj[v]:
                continue
            counts: dict[int, int] = defaultdict(int)
            for u in adj[v]:
                counts[label[u]] += 1
            max_count = max(counts.values())
            best = [lbl for lbl, cnt in counts.items() if cnt == max_count]
            new_label = min(best)  # deterministic tie-breaking
            if new_label != label[v]:
                label[v] = new_label
                changed = True
        if not changed:
            break
    # Renumber communities to 0..k-1
    unique = sorted(set(label.values()))
    remap = {old: i for i, old in enumerate(unique)}
    return {v: remap[label[v]] for v in nodes}


def _build_graph(profiles_by_login: dict, seeds: list[str], promising_devs: list[dict]) -> dict:
    """Build graph data at runtime. Returns {"nodes": [...], "links": [...]}."""
    MAX_NODES = 400

    # Build promising lookups
    promising_tier = {p["login"]: p["tier"] for p in promising_devs}
    promising_reason = {p["login"]: p.get("reason", "") for p in promising_devs}

    # Determine all seeds (from network files too)
    all_seeds = set(seeds)
    for fpath in glob.glob(f"{VOLUME_PATH}/data/network_*.json"):
        name = os.path.basename(fpath).replace("network_", "").replace(".json", "")
        all_seeds.add(name)

    # ---- Select nodes ----
    # Priority: seeds > promising > 3+ connections > top cracked_score
    must_include = set(all_seeds)
    for p in promising_devs:
        must_include.add(p["login"])

    candidates_3conn = []
    for login, p in profiles_by_login.items():
        if login not in must_include and len(p.get("connections", {})) >= 3:
            candidates_3conn.append((login, p.get("cracked_score", 0)))
    candidates_3conn.sort(key=lambda x: -x[1])

    selected = set(must_include)
    for login, _ in candidates_3conn:
        if len(selected) >= MAX_NODES:
            break
        selected.add(login)
    # Fill remaining with top cracked_score
    by_cracked = sorted(profiles_by_login.values(), key=lambda p: -p.get("cracked_score", 0))
    for p in by_cracked:
        if len(selected) >= MAX_NODES:
            break
        selected.add(p["login"])

    # ---- Build directed edges ----
    directed_edges: set[tuple[str, str]] = set()

    # From network_*.json files
    for fpath in glob.glob(f"{VOLUME_PATH}/data/network_*.json"):
        try:
            net = json.loads(Path(fpath).read_text())
        except Exception:
            continue
        seed = net.get("seed", "")
        if seed not in selected:
            continue
        for follower in net.get("followers", []):
            if follower in selected:
                directed_edges.add((follower, seed))
        for following in net.get("following", []):
            if following in selected:
                directed_edges.add((seed, following))

    # From merged connections
    for login in selected:
        p = profiles_by_login.get(login, {})
        for seed_login, rels in p.get("connections", {}).items():
            if seed_login not in selected:
                continue
            for rel in rels:
                if rel == "follower":
                    directed_edges.add((login, seed_login))
                elif rel == "following":
                    directed_edges.add((seed_login, login))

    # Collapse to undirected with mutual flag
    undirected: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for a, b in directed_edges:
        key = (min(a, b), max(a, b))
        if key not in undirected:
            undirected[key] = set()
        undirected[key].add((a, b))

    edges_out = []
    for (a, b), directions in undirected.items():
        mutual = len(directions) >= 2
        edges_out.append({"source": a, "target": b, "mutual": mutual})

    # ---- Build adjacency for algorithms ----
    node_list = sorted(selected)
    adj: dict[str, set[str]] = {v: set() for v in node_list}
    for e in edges_out:
        adj[e["source"]].add(e["target"])
        adj[e["target"]].add(e["source"])

    # ---- Compute metrics ----
    pagerank = _compute_pagerank(adj, node_list)
    betweenness = _compute_betweenness(adj, node_list)
    communities = _compute_communities(adj, node_list)
    degree = {v: len(adj[v]) for v in node_list}

    # ---- Assign tiers ----
    def get_tier(login):
        if login in all_seeds:
            return "seed"
        if login in promising_tier:
            return f"tier{promising_tier[login]}"
        p = profiles_by_login.get(login, {})
        cs = p.get("cracked_score", 0)
        if cs >= 60:
            return "tier1"
        elif cs >= 40:
            return "tier2"
        elif cs >= 25:
            return "tier3"
        return "other"

    # ---- Build output nodes ----
    nodes = []
    for login in node_list:
        p = profiles_by_login.get(login, {})
        top_repos = []
        for r in (p.get("top_repos") or [])[:5]:
            top_repos.append({
                "name": r["name"],
                "stars": r.get("stars", 0),
                "lang": r.get("lang"),
                "desc": (r.get("desc") or "")[:120],
            })
        top_lang = p.get("top_languages", [None])[0] if p.get("top_languages") else None
        languages = p.get("top_languages", [])[:5]

        nodes.append({
            "id": login,
            "name": p.get("name") or login,
            "bio": (p.get("bio") or "")[:300],
            "tier": get_tier(login),
            "followers": p.get("followers", 0),
            "following": p.get("following", 0),
            "total_stars": p.get("total_stars", 0),
            "total_commits": p.get("total_commits", 0),
            "total_prs": p.get("total_prs", 0),
            "public_repos": p.get("public_repos", 0),
            "top_lang": top_lang,
            "languages": languages,
            "top_repos": top_repos,
            "score": round(p.get("score", 0), 1),
            "diamond_score": round(p.get("cracked_score", 0), 1),
            "reason": promising_reason.get(login, ""),
            "location": p.get("location") or "",
            "company": p.get("company") or "",
            "website": p.get("website") or "",
            "twitter": p.get("twitter") or "",
            "created_at": p.get("created_at") or "",
            "connected_seeds": list(p.get("connections", {}).keys()) if p.get("connections") else [],
            "pagerank": round(pagerank.get(login, 0), 6),
            "betweenness": round(betweenness.get(login, 0), 6),
            "community": communities.get(login, 0),
            "degree": degree.get(login, 0),
        })

    return {"nodes": nodes, "links": edges_out}


def _build_data():
    """Read data files and build all indexes. Returns a dict of all computed state."""
    raw = json.loads(Path(MERGED_FILE).read_text())
    seeds = raw.get("seeds", [])
    profiles = list(raw["profiles"])

    profiles_by_login = {}
    profiles_by_language: dict[str, list[dict]] = defaultdict(list)
    search_strings = {}
    lang_counts: dict[str, int] = defaultdict(int)

    for p in profiles:
        login = p["login"]
        profiles_by_login[login] = p
        for lang in p.get("top_languages", []):
            profiles_by_language[lang].append(p)
            lang_counts[lang] += 1
        parts = [
            login, p.get("name") or "", p.get("bio") or "",
            p.get("company") or "", p.get("location") or "",
            " ".join(p.get("top_languages", [])),
            " ".join((o if isinstance(o, str) else o.get("login", "")) for o in p.get("orgs", [])),
            " ".join(r.get("name", "") + " " + (r.get("desc") or "") for r in p.get("top_repos", [])),
        ]
        search_strings[login] = " ".join(parts).lower()

    all_languages = [lang for lang, _ in sorted(lang_counts.items(), key=lambda x: -x[1])]

    hackathon_projects = []
    if Path(ENRICHED_FILE).exists():
        hackathon_projects = json.loads(Path(ENRICHED_FILE).read_text())

    # Compute cracked_score
    for p in profiles:
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

    profiles.sort(key=lambda p: -p.get("cracked_score", 0))

    # Load promising devs for graph building
    promising_devs = []
    promising_path = Path(f"{VOLUME_PATH}/data/promising_devs.json")
    if promising_path.exists():
        try:
            promising_devs = json.loads(promising_path.read_text())
        except Exception as e:
            print(f"Failed to load promising_devs.json: {e}")

    # Build runtime graph
    graph_data = {}
    try:
        graph_data = _build_graph(profiles_by_login, seeds, promising_devs)
        print(f"Built graph: {len(graph_data.get('nodes', []))} nodes, {len(graph_data.get('links', []))} links")
    except Exception as e:
        print(f"Failed to build graph: {e}")
        import traceback; traceback.print_exc()

    return {
        "profiles": profiles,
        "profiles_by_login": profiles_by_login,
        "profiles_by_language": dict(profiles_by_language),
        "search_strings": search_strings,
        "hackathon_projects": hackathon_projects,
        "seeds": seeds,
        "all_languages": all_languages,
        "graph_data": graph_data,
    }


def load_data():
    global PROFILES, SEEDS, HACKATHON_PROJECTS, ALL_LANGUAGES, GRAPH_DATA, _DATA_LOADED, _LAST_RELOAD

    # Periodically reload volume to pick up crawler changes
    if _DATA_LOADED:
        if time.time() - _LAST_RELOAD < 60:
            return
        try:
            volume.reload()
        except Exception as e:
            print(f"volume.reload() failed: {e}")
        _LAST_RELOAD = time.time()
        # Fall through to re-read files

    try:
        data = _build_data()
    except Exception as e:
        print(f"load_data() failed to build data: {e}")
        import traceback; traceback.print_exc()
        if _DATA_LOADED:
            return  # Keep serving stale data rather than crashing
        raise

    # Atomic swap: replace all module-level state at once
    PROFILES.clear()
    PROFILES.extend(data["profiles"])
    PROFILES_BY_LOGIN.clear()
    PROFILES_BY_LOGIN.update(data["profiles_by_login"])
    PROFILES_BY_LANGUAGE.clear()
    PROFILES_BY_LANGUAGE.update(data["profiles_by_language"])
    SEARCH_STRINGS.clear()
    SEARCH_STRINGS.update(data["search_strings"])
    HACKATHON_PROJECTS.clear()
    HACKATHON_PROJECTS.extend(data["hackathon_projects"])
    SEEDS.clear()
    SEEDS.extend(data["seeds"])
    ALL_LANGUAGES.clear()
    ALL_LANGUAGES.extend(data["all_languages"])
    GRAPH_DATA.clear()
    GRAPH_DATA.update(data.get("graph_data", {}))

    _DATA_LOADED = True
    _LAST_RELOAD = time.time()
    print(f"Loaded {len(PROFILES)} profiles, {len(HACKATHON_PROJECTS)} hackathon projects, {len(ALL_LANGUAGES)} languages")


# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are a talent discovery agent for a recruiting platform. You have access to a dataset of GitHub developers crawled from interconnected social networks, plus hackathon projects.

## Data Location
- Developer profiles: /data/data/merged_network.json (JSON with "profiles" array, "seeds" array)
- Hackathon projects: /data/enriched_winners.json (JSON array of projects)
- Network graph: /data/data/graph.json (nodes with pagerank, betweenness, community, diamond_score; links with mutual flag)

## Quick Data Access
You can use Python to query the data. Here's a starter pattern:

```python
import json
data = json.load(open('/data/data/merged_network.json'))
profiles = data['profiles']

# Search by keyword
results = [p for p in profiles if 'rust' in ' '.join(p.get('top_languages', [])).lower()]

# Sort by total_stars or any metric
results.sort(key=lambda p: -p.get('total_stars', 0))

# Key fields per profile:
# login, name, bio, company, location, followers, following
# total_stars, total_commits, total_prs, public_repos
# top_languages: [str], top_repos: [{name, stars, lang, desc}]
# orgs: [{login, name}], created_at, account_age_years, cracked_score
# in_multiple_networks (bool), is_mutual_follow (bool)
# found_via: [seed_logins], connections: {seed: [relationship_types]}
```

## GitHub API Access
You have a GITHUB_TOKEN env var (5000 req/hr). Use Python to fetch live data:

```python
import httpx, os
headers = {"Authorization": f"bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}

# Useful endpoints:
r = httpx.get("https://api.github.com/users/{login}", headers=headers)              # profile, bio, follower counts
r = httpx.get("https://api.github.com/users/{login}/repos?sort=stars&per_page=10", headers=headers)  # top repos
r = httpx.get("https://api.github.com/users/{login}/events/public?per_page=30", headers=headers)     # recent activity
r = httpx.get("https://api.github.com/repos/{owner}/{repo}", headers=headers)        # repo details, stars, forks
r = httpx.get("https://api.github.com/repos/{owner}/{repo}/contributors", headers=headers)  # contributors
r = httpx.get("https://api.github.com/repos/{owner}/{repo}/commits?per_page=5", headers=headers)     # recent commits
r = httpx.get("https://api.github.com/search/repositories?q=language:rust+stars:>100", headers=headers)  # search repos
r = httpx.get("https://api.github.com/search/users?q=location:SF+followers:>50", headers=headers)    # search users
data = r.json()
```

Use these to go beyond our dataset — fetch recent activity, verify profiles, discover new repos, check commit recency.

## Your Job
1. Use Bash to run Python snippets that query the data
2. Use WebSearch/WebFetch for live GitHub research, and the GitHub API via GITHUB_TOKEN for detailed data
3. Provide data-driven recommendations with specific numbers
4. Be concise but thorough - cite login, stars, commits, languages, notable repos
5. Highlight network signals: MULTI-NET, MUTUAL flags

Always cite specific numbers from the data. Don't make up information."""


CRAWLER_SYSTEM_PROMPT = """You are a talent discovery crawler running autonomously every 10 minutes.
Your job: expand the GitHub talent network by exploring social graphs, profiling promising developers, and saving results.
You're looking for "cracked devs" — extremely talented builders who aren't well-known yet.

## Data Files (all under /data/)
- /data/data/merged_network.json — THE MAIN FILE. JSON with "profiles" array and "seeds" array. Each profile has: login, name, bio, company, location, followers, following, public_repos, total_commits, total_prs, top_repos [{name, stars, lang, desc}], total_stars, top_languages, orgs, found_via, connections, in_multiple_networks, is_mutual_follow, score, created_at
- /data/data/network_*.json — per-seed follower/following lists with profiles
- /data/data/promising_devs.json — CURATED LIST of exceptional developers. You MUST read this and append to it when you find someone truly exceptional. See format below.
- /data/crawl_log.json — YOUR crawl history. Read this first, update it when done!

## Promising Devs Curation (IMPORTANT)
You are responsible for maintaining /data/data/promising_devs.json. This is a JSON array of exceptional developers with these fields:
- login: GitHub username
- tier: 1 (absolute hidden gem) or 2 (strong under-radar talent)
- reason: 1-3 sentence editorial explanation of WHY they're exceptional
- Plus all standard profile fields (name, bio, followers, total_stars, top_repos, top_languages, etc.)

### Tier 1 criteria (hidden gems — very selective, ~1 per run if any):
- Absurdly low visibility (12-100 followers) relative to exceptional technical depth
- Working in hard domains: CPU/hardware design, PL theory, real cryptography/ZK, kernel/OS dev, graphics engines, formal verification
- The "wow" factor: "People who design CPUs for fun are exceptionally rare"
- Near-zero self-promotion despite extraordinary work

Example tier 1 reasons:
- "18 followers. 6-stage pipelined RISC-V CPU on FPGA in SystemVerilog. People who design CPUs for fun are exceptionally rare."
- "30 followers. GPU SHA-256 in CUDA, autodiff library in C, ZK-SNARKs. Systems+crypto+ML at extreme low visibility."
- "75 followers. Lisp interpreter in sed. Lambda calculus compiler in C. Cubical type theory in OCaml. Extraordinary PL theory depth."
- "12 followers. WebGPU path tracing, voxel fractals, BVH construction. Serious graphics engineering at near-zero visibility."

### Tier 2 criteria (strong under-radar — maybe 2-3 per run):
- Low visibility (<500 followers) with strong technical work
- Top school/company credentials (CMU, Caltech, MIT, Stanford, OpenAI, Stripe) combined with real projects
- Strong network signals (connected to multiple seeds, mutual follows)
- Solid technical depth but not quite jaw-dropping

Example tier 2 reasons:
- "UBC '26. OpenAI + Stripe intern. E2EE key recovery protocol in Rust. CTF competitor. Under-radar for that resume."
- "CMU PhD. CalcuLaTeX (406★), tinyvm, physics sims. Beautiful educational tools. Shows exceptional taste."
- "88 followers. RISC-V→ARM binary translator in Rust. TockOS formal verification. @tock contributor."

### Rules for promising_devs.json:
- Read the existing list first. Never add duplicates.
- Be VERY selective. Only add someone if you'd genuinely be impressed reviewing their GitHub.
- The reason field is editorial — write it like a talent scout's note, not a data dump.
- Include follower count in the reason to emphasize the visibility gap.
- It's fine to add 0 people in a run. Don't lower the bar.

## GitHub API
GITHUB_TOKEN is set in env. Use Python with httpx.

### GraphQL (POST https://api.github.com/graphql)
```python
import httpx, os, json
headers = {"Authorization": f"bearer {os.environ['GITHUB_TOKEN']}", "Content-Type": "application/json"}
client = httpx.Client(headers=headers, timeout=30)

# Get someone's network (followers + following)
query = '''{ user(login: "%s") {
  followers(first: 100) { nodes { login } totalCount pageInfo { hasNextPage endCursor } }
  following(first: 100) { nodes { login } totalCount pageInfo { hasNextPage endCursor } }
} }''' % login
resp = client.post("https://api.github.com/graphql", json={"query": query})

# Profile a user in detail
query = '''{ user(login: "%s") {
  login name bio company location twitterUsername websiteUrl createdAt
  followers { totalCount } following { totalCount }
  repositories(first: 10, ownerAffiliations: OWNER, orderBy: {field: STARGAZERS, direction: DESC}) {
    totalCount
    nodes { name stargazerCount primaryLanguage { name } description isFork }
  }
  contributionsCollection { totalCommitContributions totalPullRequestContributions }
  organizations(first: 10) { nodes { login name } }
} }''' % login
```

### REST (GET https://api.github.com/...)
```python
headers = {"Authorization": f"bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
# /users/{login} — profile
# /users/{login}/repos?sort=stars&per_page=10 — top repos
# /users/{login}/events/public?per_page=30 — recent activity
# /search/users?q=language:rust+followers:<100 — find niche devs
# /search/repositories?q=stars:10..500+language:zig — find hidden gems
# /repos/{owner}/{repo}/contributors — find collaborators
```

## Strategy
1. Read /data/crawl_log.json to see what's been done. Read /data/data/merged_network.json to get existing logins.
2. Check rate limit: GET https://api.github.com/rate_limit
3. **USE MOST OF YOUR API BUDGET.** You have 5,000 GraphQL + 5,000 REST calls per hour. Previous runs only used 70-370 total — that's 2-7% of the budget. You should aim to use ~4,000 GraphQL and ~2,000 REST calls per run. Leave 500 GraphQL + 500 REST as buffer. Check rate_limit periodically during your run and keep going until you're near the limit.
4. Pick MANY expansion targets — explore 10-20+ people's networks per run, not just 2-3:
   - High-scoring profiles whose networks haven't been explored yet
   - Users appearing in multiple seed networks (strong signal)
   - Contributors to interesting repos by existing high-scorers
   - GitHub search: niche language + low followers combos (e.g., Zig, Nim, Gleam devs with <500 followers)
   - People whose followers overlap heavily with our existing profiles
5. For each target: fetch their followers/following, cross-reference with existing profiles.
6. Profile promising NEW connections via GraphQL (young accounts, technical languages, real projects).
7. Append new profiles to merged_network.json's "profiles" array. PRESERVE all existing data!
8. Evaluate new profiles for promising_devs.json. If any are truly exceptional, append them.
9. Update /data/crawl_log.json with what you explored and discovered.

## Efficiency tips for maximizing API usage
- Use GraphQL to batch profile lookups (you can fetch followers + following + repos in one query per user)
- Write Python scripts to /tmp/ and run them — scripts can loop through many targets efficiently in a single tool call
- Process targets in batches: write a script that iterates over 10+ targets, fetches all their networks, and saves results
- Don't stop after finding a few promising devs — keep exploring until the API budget is nearly exhausted

## Rules
- NEVER overwrite or remove existing profiles. Only append new ones.
- Always check if a login already exists before adding it.
- Leave 500 GraphQL + 500 REST as buffer. USE THE REST of your API budget aggressively.
- Write Python scripts to /tmp/ and run them for complex operations. Scripts can loop through many API calls efficiently.
- Quality over quantity for PROMISING DEVS curation — but explore broadly. Profile hundreds of people, curate the exceptional few.
- Log everything to crawl_log.json so future runs know what's been done.
- Explore aggressively: 10-20+ expansion targets per run. Use scripts to batch API calls.
- Set found_via and connections fields on new profiles to track provenance.
- Periodically check rate_limit mid-run. If you have >1000 calls remaining, keep going!
"""


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


@web_app.get("/promising")
async def serve_promising():
    html = Path("/app/promising.html")
    if html.exists():
        return FileResponse(html, media_type="text/html")
    return HTMLResponse("<h1>promising.html not found</h1>", status_code=500)


@web_app.get("/viz")
async def serve_viz():
    viz_html = Path("/app/viz.html")
    if viz_html.exists():
        return FileResponse(viz_html, media_type="text/html")
    return HTMLResponse("<h1>viz.html not found</h1>", status_code=500)


@web_app.get("/data/graph.json")
async def serve_graph_json():
    load_data()
    if GRAPH_DATA and GRAPH_DATA.get("nodes"):
        return JSONResponse(GRAPH_DATA)
    return JSONResponse({"error": "graph not available"}, status_code=404)


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


@web_app.get("/api/crawl-status")
async def crawl_status_endpoint():
    status = {}
    for k in ["last_run", "last_cost", "last_turns", "last_skip", "last_profiles_added"]:
        try:
            status[k] = await crawl_state.get.aio(k)
        except KeyError:
            status[k] = None
    return JSONResponse(status)


@web_app.get("/sessions")
async def serve_sessions():
    html = Path("/app/sessions.html")
    if html.exists():
        return FileResponse(html, media_type="text/html")
    return HTMLResponse("<h1>sessions.html not found</h1>", status_code=500)


@web_app.get("/api/sessions")
async def list_sessions():
    sessions_dir = Path(f"{VOLUME_PATH}/sessions")
    if not sessions_dir.exists():
        return JSONResponse([])
    sessions = []
    for f in sorted(sessions_dir.glob("session_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            sessions.append({
                "filename": f.name,
                "timestamp": data.get("timestamp"),
                "duration_seconds": data.get("duration_seconds"),
                "cost_usd": data.get("cost_usd"),
                "num_turns": data.get("num_turns"),
                "initial_profile_count": data.get("initial_profile_count"),
                "gql_remaining_start": data.get("gql_remaining_start"),
                "rest_remaining_start": data.get("rest_remaining_start"),
                "message_count": len(data.get("messages", [])),
            })
        except Exception:
            continue
    return JSONResponse(sessions)


@web_app.get("/api/sessions/{filename}")
async def get_session(filename: str):
    # Sanitize filename
    if "/" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    session_file = Path(f"{VOLUME_PATH}/sessions/{filename}")
    if not session_file.exists():
        return JSONResponse({"error": "session not found"}, status_code=404)
    return JSONResponse(json.loads(session_file.read_text()))


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
        yield {"event": "status", "data": json.dumps({"text": "Starting sandbox..."})}
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions, query as agent_query,
                AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, ToolResultBlock,
            )

            agent_env = {}
            # Use user's token if authenticated, otherwise fall back to platform token
            gh_token = github_token or os.environ.get("GITHUB_TOKEN", "")
            if gh_token:
                agent_env["GITHUB_TOKEN"] = gh_token

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

            yield {"event": "status", "data": json.dumps({"text": "Thinking..."})}
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
                env={"HOME": "/home/agent", **({
                    "GITHUB_TOKEN": os.environ["GITHUB_TOKEN"]
                } if os.environ.get("GITHUB_TOKEN") else {})},
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
    secrets=[modal.Secret.from_name("anthropic-key"), modal.Secret.from_name("github-token")],
    timeout=600,
    min_containers=1,
    scaledown_window=1200,
)
@modal.asgi_app()
def web():
    return web_app


# ---------------------------------------------------------------------------
# Autonomous crawler (runs every 10 minutes via Modal cron)
# ---------------------------------------------------------------------------

@app.function(
    image=image,
    volumes={VOLUME_PATH: volume},
    secrets=[
        modal.Secret.from_name("claude-oauth"),
        modal.Secret.from_name("github-token"),
    ],
    schedule=modal.Cron("0 * * * *"),
    timeout=3600,
)
async def expand_network():
    import httpx

    await volume.reload.aio()

    # Read current state
    merged = json.loads(Path(MERGED_FILE).read_text())
    profile_count = len(merged.get("profiles", []))
    existing_logins = {p["login"] for p in merged.get("profiles", [])}

    # Load crawl log
    crawl_log_path = Path(f"{VOLUME_PATH}/crawl_log.json")
    try:
        crawl_log = json.loads(crawl_log_path.read_text())
    except FileNotFoundError:
        crawl_log = {"runs": [], "total_profiles_added": 0, "explored_logins": []}

    # Check rate limit
    headers = {"Authorization": f"bearer {os.environ['GITHUB_TOKEN']}"}
    resp = httpx.get("https://api.github.com/rate_limit", headers=headers)
    rate = resp.json()
    gql_remaining = rate["resources"]["graphql"]["remaining"]
    rest_remaining = rate["resources"]["core"]["remaining"]

    if gql_remaining < 300 and rest_remaining < 300:
        print(f"Rate limit low: GQL={gql_remaining}, REST={rest_remaining}. Skipping.")
        await crawl_state.put.aio("last_skip", time.time())
        return

    # Build context for Claude
    num_runs = len(crawl_log.get('runs', []))
    explored = crawl_log.get('explored_logins', [])
    # Extract explored logins from runs if top-level key missing
    if not explored:
        for run in crawl_log.get('runs', []):
            explored.extend(run.get('targets_explored', []))
            explored.extend(run.get('expansion_targets', []))

    context = f"""Dataset: {profile_count} profiles, {len(merged.get('seeds', []))} seeds.
GitHub API rate limits: GraphQL={gql_remaining}/5000, REST={rest_remaining}/5000.
Previous crawler runs: {num_runs}.
Recently explored logins: {explored[-20:]}
Existing logins count: {len(existing_logins)}"""

    prompt = f"""{context}

Expand the talent network. Read the existing data files, pick smart expansion targets, crawl their GitHub connections via the API, profile promising new developers, and save results back to the data files.

IMPORTANT: You have ~{gql_remaining} GraphQL and ~{rest_remaining} REST API calls available. USE MOST OF THEM. Write Python scripts that batch-process many targets. Explore 10-20+ people's networks. Keep going until you're near the rate limit (check periodically with GET /rate_limit). Previous runs only used 2-7% of the budget — we want 60-80%+."""

    from claude_agent_sdk import (
        ClaudeAgentOptions, query as agent_query,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    )

    options = ClaudeAgentOptions(
        allowed_tools=["Bash", "Read", "Write", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        system_prompt=CRAWLER_SYSTEM_PROMPT,
        max_turns=50,
        model="claude-opus-4-6",
        cwd="/data",
        env={
            "HOME": "/home/agent",
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            # CLAUDE_CODE_OAUTH_TOKEN is auto-detected by the SDK from env
        },
        include_partial_messages=True,
        user="agent",
    )

    print(f"Starting crawler run. {profile_count} existing profiles, GQL={gql_remaining}, REST={rest_remaining}")

    result = None
    session_messages = []
    run_start = time.time()
    async for msg in agent_query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            msg_data = {"type": "assistant", "timestamp": time.time(), "blocks": []}
            for block in msg.content:
                if isinstance(block, TextBlock):
                    msg_data["blocks"].append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    msg_data["blocks"].append({
                        "type": "tool_use",
                        "tool": block.name,
                        "id": block.id,
                        "input": str(block.input)[:2000],
                    })
                elif isinstance(block, ToolResultBlock):
                    msg_data["blocks"].append({
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": str(block.content)[:2000] if block.content else "",
                    })
            session_messages.append(msg_data)
        elif isinstance(msg, ResultMessage):
            result = msg
            print(f"Crawler finished: cost=${getattr(msg, 'total_cost_usd', '?')}, turns={getattr(msg, 'num_turns', '?')}")

    await volume.commit.aio()

    # Save session transcript to volume
    try:
        sessions_dir = Path(f"{VOLUME_PATH}/sessions")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        session_file = sessions_dir / f"session_{ts}.json"
        session_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.time() - run_start, 1),
            "cost_usd": getattr(result, "total_cost_usd", None) if result else None,
            "num_turns": getattr(result, "num_turns", None) if result else None,
            "session_id": getattr(result, "session_id", None) if result else None,
            "initial_profile_count": profile_count,
            "gql_remaining_start": gql_remaining,
            "rest_remaining_start": rest_remaining,
            "prompt": prompt,
            "messages": session_messages,
        }
        session_file.write_text(json.dumps(session_data))
        await volume.commit.aio()
        print(f"Saved session transcript: {session_file}")
    except Exception as e:
        print(f"Failed to save session transcript: {e}")

    # Check if profiles were added
    try:
        new_merged = json.loads(Path(MERGED_FILE).read_text())
        new_count = len(new_merged.get("profiles", []))
        profiles_added = new_count - profile_count
    except Exception:
        profiles_added = 0

    # Log to modal.Dict (async)
    await crawl_state.put.aio("last_run", time.time())
    await crawl_state.put.aio("last_cost", getattr(result, "total_cost_usd", None) if result else None)
    await crawl_state.put.aio("last_turns", getattr(result, "num_turns", None) if result else None)
    await crawl_state.put.aio("last_profiles_added", profiles_added)

    print(f"Crawler done. Profiles added this run: {profiles_added}")
