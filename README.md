# Talent Discovery

**The best engineers aren't looking for jobs. We find them anyway.**

Talent Discovery crawls GitHub's social graph starting from 27 known-exceptional seed developers, profiles 3,263 people in their extended networks, scores them with a proprietary "cracked score" algorithm, and surfaces undiscovered talent through an AI-powered chat interface and interactive network visualization.

## The Problem

Traditional recruiting finds people who are actively job-seeking. But the best engineers are heads-down building - they have 200 followers, no LinkedIn, and a compiler they wrote from scratch sitting at 15 stars. They're invisible to conventional search.

## Our Approach

We flip recruiting on its head: instead of searching by resume keywords, we **crawl the social graphs of people we already know are exceptional** and use graph analysis + heuristic scoring to find the diamonds in the rough.

### Pipeline

```
27 seed developers (known-exceptional)
        |
        v
  GitHub GraphQL API crawl
  (followers, following, repos, commits, PRs, orgs)
        |
        v
  3,263 profiled developers
  1,874 follow-relationships mapped
        |
        v
  Multi-signal scoring
  - Cracked Score (undiscovered talent metric)
  - Diamond Score (technical depth + low visibility)
  - PageRank & betweenness centrality
  - Louvain community detection
        |
        v
  Two interfaces:
  1. AI Chat Terminal (Claude-powered natural language search)
  2. Force-directed Network Explorer (interactive graph viz)
```

### The Cracked Score

Our proprietary scoring algorithm specifically optimizes for **undiscovered talent** - not who has the most stars:

- **Log-scaled stars/year** - a 19-year-old with 500 stars in 2 years scores higher than a senior dev with 10K stars over 15 years
- **Youth multiplier** - younger accounts with high output get exponential bonuses (accounts <2y get 5x, >12y get 0.3x)
- **Famous penalty** - >5K followers means you're already discovered (0.7x), >20K gets 0.2x
- **Follow-farm detection** - high followers with low actual code output gets penalized
- **Network bonus** - appearing in multiple seed networks (1.5x) or mutual follows with seeds (1.3x) is strong signal

### The Diamond Score

A complementary scoring system that finds technical depth others miss:

- Regex-matches repo names/descriptions against 60+ technical signal patterns (compilers, kernels, proof assistants, FPGA, etc.)
- Bonuses for strong languages (Rust, Haskell, OCaml, Zig, Lean)
- **HIGH ALPHA flag**: technical repos + <200 followers = undervalued
- Academic signals (MIT, Stanford, CMU in bio)
- Cross-network social graph validation

## Features

### AI Intelligence Terminal

A chat interface backed by Claude that can search, filter, and generate recruiting dossiers in real time. Natural language queries like:

- "Find undiscovered Rust developers with fewer than 500 followers"
- "Who are the youngest developers with the highest cracked scores?"
- "Generate a dossier on @username"

The agent has access to the full dataset and streams responses with tool call visibility.

### Network Explorer

Interactive force-directed graph visualization of 400 key developers across the network:

- **400 nodes, 1,874 edges, 4 detected communities**
- GitHub avatar rendering with tier-colored rings (seed/tier1/tier2/tier3)
- PageRank and betweenness centrality computed per-node
- Louvain community detection for cluster identification
- Tunable physics (repulsion, link distance, gravity) and display settings
- Filter by tier, language, followers, diamond score, community
- Click any node for full profile details (repos, stats, graph metrics, connected seeds)

## Architecture

```
crawl.py          - GitHub GraphQL crawler (single seed)
batch_crawl.py    - Multi-seed batch crawler with deduplication
scrape.py         - Hackathon project enrichment (repo metadata, contributors)
analyze.py        - Diamond-in-the-rough scoring engine
build_graph.py    - NetworkX graph analysis (PageRank, communities)
server.py         - FastAPI backend + Claude Code SDK agent
app.py            - Modal deployment variant
mcp_talent.py     - MCP stdio server for tool-use integration
index.html        - AI chat terminal frontend
viz.html          - Force-directed network explorer
```

**Zero build step.** Every Python script uses `uv` inline script metadata for dependencies. Frontend is two single HTML files loading libraries from CDN.

## Running It

```bash
# Crawl a seed's network
uv run crawl.py <github_username>

# Batch crawl multiple seeds
uv run batch_crawl.py user1 user2 user3

# Run the diamond analysis
uv run analyze.py

# Build the graph visualization data
uv run build_graph.py

# Start the main platform
uv run server.py
# -> http://localhost:8000

# Serve the network explorer
python3 -m http.server 8001
# -> http://localhost:8001/viz.html
```

## Dataset

| Metric | Value |
|--------|-------|
| Seed developers | 27 |
| Profiled developers | 3,263 |
| Hackathon projects | 518 |
| Languages tracked | 194 |
| Total stars across profiles | 4.8M |
| Graph nodes (explorer) | 400 |
| Graph edges | 1,874 (507 mutual) |
| Communities detected | 4 |

## Built With

- **Claude Code SDK** - AI agent backend for natural language search
- **FastAPI** - API server
- **NetworkX** - Graph algorithms (PageRank, betweenness, Louvain)
- **force-graph** - WebGL force-directed graph rendering
- **GitHub GraphQL API** - Data source
- **uv** - Python package management
- **Modal** - Cloud deployment (optional)
