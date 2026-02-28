# Graph-based Agentic Talent Discovery

## Inspiration

We kept noticing the same pattern: the most talented engineers we know have tiny followings. A kid writes a compiler from scratch in Zig - 14 stars, 80 followers, no LinkedIn. Meanwhile, recruiters are fighting over the same pool of developers who already have 50K followers and a "open to work" banner.

The insight: **talent clusters socially**. Exceptional people follow other exceptional people. If you know 27 genuinely cracked developers, their combined social graph contains hundreds of undiscovered gems - you just need the right algorithm to find them.

We wanted to build the tool that finds the person *before* they blow up.

## What it does

Talent Discovery crawls GitHub's social graph starting from 27 seed developers we know are exceptional, profiles 3,263 people in their extended networks, and uses multi-signal scoring to surface undiscovered talent.

**Two interfaces:**

**1. AI Intelligence Terminal** - A Claude-powered chat interface where you type natural language queries like "Find undiscovered Rust developers with fewer than 500 followers" or "Generate a recruiting dossier on @username." The agent searches the dataset, cross-references hackathon projects, analyzes network position, and streams back data-rich recommendations in real time. You can see every tool call it makes.

**2. Network Explorer** - An interactive force-directed graph of 400 key developers with 1,874 edges. Every node renders the developer's GitHub avatar with a tier-colored ring. You can filter by language, community, follower count, and diamond score. Click any node to see their full profile, repos, graph centrality metrics, and which seeds they're connected to. Physics are tunable - adjust repulsion, gravity, link distance in real time.

**The scoring is the core product:**

- **Cracked Score** - Optimized for undiscovered talent. Log-scaled stars per year, youth multiplier (a 2-year-old account with 500 stars beats a 15-year-old account with 10K), famous penalty (>5K followers = already discovered), follow-farm detection, and network bonuses for appearing in multiple seed graphs.
- **Diamond Score** - Pattern-matches repos against 60+ technical depth signals (compilers, proof assistants, FPGA, kernel, crypto). Flags "HIGH ALPHA" when someone has technical repos but <200 followers. Checks for strong languages (Rust, Haskell, Zig, Lean), academic affiliations, and cross-network validation.
- **Graph Metrics** - PageRank, betweenness centrality, and Louvain community detection across the full network. Developers who bridge communities or have high centrality despite low follower counts are especially interesting.

## How we built it

**Data pipeline:** We start with 27 seed GitHub usernames. `crawl.py` hits the GitHub GraphQL API to get each seed's followers and following lists, then profiles every person with their repos, stars, commits, PRs, languages, and orgs. `batch_crawl.py` orchestrates this across all seeds with smart deduplication - users appearing in multiple seed networks get flagged as higher signal. `scrape.py` enriches 518 hackathon projects with repo metadata and contributor mappings.

**Scoring:** `analyze.py` runs the Diamond Score algorithm - regex-matching repo descriptions against technical signal patterns, checking for strong languages, cross-referencing network position, and flagging undervalued profiles. The Cracked Score is computed at server startup with the full age/output/fame/network formula.

**Graph analysis:** `build_graph.py` uses NetworkX to compute PageRank, betweenness centrality, and Louvain community detection across a filtered 400-node subgraph. Edges track directionality (who follows whom) and mutual follow status.

**Backend:** FastAPI server with the Claude Code SDK powering the AI agent. The agent gets pre-searched local results as context and generates responses with `max_turns=1` for speed. SSE streaming for real-time token delivery. MCP tool server (`mcp_talent.py`) exposes the dataset through structured tools.

**Frontend:** Two single HTML files, zero build step. The chat terminal uses a dark terminal aesthetic with glass morphism, streaming markdown rendering, and tool call visibility. The network explorer uses the `force-graph` library with custom canvas rendering for avatar-clipped nodes with tier-colored rings.

**Deployment:** Modal for cloud hosting with volume-mounted data. Locally, everything runs with `uv` inline script metadata - no requirements.txt, no venv, just `uv run server.py`.

## Challenges we ran into

**GitHub API rate limits** were the biggest constraint. With 27 seeds and some having 1000+ followers, we needed to be strategic. We built a priority system: only fully profile users who appear in multiple seed networks, are mutual follows, or come from small networks (<500 people). This got us from needing ~50K API calls down to ~5K while keeping the most interesting profiles.

**Avatar CORS** was surprisingly tricky. `github.com/{user}.png` redirects to `avatars.githubusercontent.com` which strips CORS headers on the redirect. We had to figure out the direct `avatars.githubusercontent.com/{user}?s=64` URL format which supports `crossOrigin='anonymous'` properly.

**Scoring calibration** required iteration. Our first Cracked Score was dominated by mega-repos (one 50K-star repo would bury everyone). Switching to log-scaled stars fixed this. The youth multiplier also needed tuning - we settled on an exponential curve where accounts under 2 years get 5x and accounts over 12 years get 0.3x.

**Claude Code SDK MCP tools** had a breaking bug where `create_sdk_mcp_server()` with `@tool` decorators would fail with `CLIConnectionError`. We worked around it by pre-computing search results locally and passing them as context to single-turn `query()` calls instead.

## Accomplishments that we're proud of

- **The scoring actually works.** When we sort by Cracked Score, the top results are genuinely impressive developers with tiny followings who would never show up in a traditional search. The Diamond Score's "HIGH ALPHA" flag consistently finds people writing compilers and proof assistants with <100 followers.

- **3,263 developers profiled** from 27 seeds with full repo analysis, commit history, PR counts, org memberships, and cross-network relationship mapping. 518 hackathon projects enriched and cross-referenced.

- **The network visualization is beautiful.** 400 nodes with actual GitHub avatars, tier-colored rings, Louvain communities, tunable physics, and a full filter/search/detail panel. It genuinely reveals structure - you can see clusters of Hack Club developers, systems programmers, and ML researchers naturally separate.

- **Real-time AI agent** that can answer "find me a Rust developer connected to 3+ seeds with fewer than 500 followers" and stream back specific, data-backed recommendations with tool call transparency.

- **Zero build tooling.** Every script is self-contained with `uv` inline metadata. Both frontends are single HTML files. `uv run server.py` and you're running.

## What we learned

- **Social graphs are incredibly high-signal for talent.** Mutual follows between known-exceptional developers are a stronger signal than any resume keyword. Someone who appears in 4+ seed networks independently is almost always interesting.

- **Famous ≠ best.** Our scoring penalizes fame by design, and the results validate this. Many of the highest-cracked-score developers have <500 followers but repos that demonstrate deep technical skill.

- **Graph algorithms reveal hidden structure.** PageRank on the follow graph surfaces "connector" developers who bridge communities. Betweenness centrality finds people who are the sole link between two clusters. These metrics correlate with, but are distinct from, raw follower counts.

- **Claude Code SDK is powerful but immature.** MCP tool integration has rough edges, but the `query()` API with streaming works well for building agent-powered interfaces. Pre-computing context and using single-turn calls is more reliable than multi-turn tool use.

## What's next for Graph-based Agentic Talent Discovery

- **Deeper crawling** - Go 2 hops out from seeds instead of 1. Profile the followers of the highest-scored non-seed developers to find even more hidden talent.
- **Temporal analysis** - Track how developer activity changes over time. Someone whose commit velocity is accelerating is more interesting than someone who peaked 3 years ago.
- **Repo-level graph** - Build a contribution graph (who contributes to whose repos) in addition to the follow graph. Shared repo contributions are an even stronger signal than follows.
- **Outreach integration** - Surface contact information and generate personalized outreach messages based on the developer's actual work, not generic templates.
- **Live monitoring** - Continuously crawl and alert when a new developer enters the network or an existing one's score spikes (new breakout repo, joined interesting org, etc.).

## Built With

Claude Code SDK, FastAPI, force-graph, GitHub GraphQL API, Modal, NetworkX, Python, uv
