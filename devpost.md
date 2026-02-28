# Graph-based Agentic Talent Discovery

## Inspiration

The most talented engineers we know have tiny followings. A kid writes a compiler from scratch in Zig - 14 stars, 80 followers, no LinkedIn. Meanwhile, recruiters are fighting over the same pool of developers who already have 50K followers and an "open to work" banner.

The insight: **talent clusters socially**. Exceptional people follow other exceptional people. If you start from people you *know* are great, their combined social graph contains hundreds of undiscovered gems hiding in plain sight.

We wanted to build the tool that maps these hidden networks and lets anyone - not just programmers - explore them with natural language.

## What it does

Talent Discovery intelligently crawls GitHub's social graph starting from 27 seed developers we know are exceptional, builds a rich dataset of 3,263 developers in their extended networks, and gives non-technical users two powerful ways to explore it:

**1. AI Intelligence Terminal** - A chat interface powered by a full Claude Code agent. You type natural language - "Who are the most active Rust developers in this network?" or "Generate a recruiting dossier on @username" - and the agent searches the dataset, cross-references 518 hackathon projects, analyzes network position, and streams back data-rich recommendations in real time. You can see every tool call the agent makes as it works. This isn't a simple RAG lookup - it's a real Claude Code instance with access to Bash, file reading, web search, and the full dataset, reasoning through your question live.

**2. Network Explorer** - An interactive force-directed graph of 400 key developers with 1,874 follow-relationships. Every node is that developer's actual GitHub avatar with a colored ring showing how they were discovered. You can filter by language, community, or follower count. Click any node to see their full profile - repos, languages, commit history, which seed developers they're connected to, and their position in the network (PageRank, betweenness centrality, community membership). Drag nodes around, zoom into clusters, tune the physics in real time. A non-programmer can sit down and immediately start exploring who knows who and why they matter.

**The core value is the intelligent crawling and data collection.** Starting from 27 seeds, we crawl their followers and following lists via the GitHub GraphQL API, then profile every person with their repos, stars, commits, PRs, languages, organizations, and account age. We track *directionality* - who follows whom - and flag mutual follows as a stronger signal. Users appearing in multiple seed networks independently get flagged. The result is a rich, interconnected dataset that reveals structure no individual profile page could show you.

## How we built it

**Intelligent crawling:** `crawl.py` hits the GitHub GraphQL API to get a seed's full follower/following graph, then profiles every person with detailed repo analysis. `batch_crawl.py` orchestrates this across all 27 seeds with smart prioritization - with some seeds having 1000+ connections, we can't profile everyone. We built a priority system: profile users who appear in 2+ seed networks first (strongest signal), then mutual follows, then everyone from smaller networks. This got us from ~50K needed API calls down to ~5K while keeping the most interesting profiles. `scrape.py` separately enriches 518 hackathon projects with repo metadata and contributor mappings so we can cross-reference.

**Graph analysis:** `build_graph.py` uses NetworkX to compute PageRank (who's important in the network?), betweenness centrality (who bridges different communities?), and Louvain community detection (what clusters exist?) across a 400-node subgraph. Edges preserve directionality and mutual follow status.

**Agentic backend:** FastAPI server with the Claude Code SDK powering a real agent. When a user asks a question, we pre-search the local dataset for relevant profiles and pass them as context to a Claude Code instance. The agent can reason about the data, compare developers, generate dossiers, and search the web for additional context - all streamed to the user via SSE with full tool-call visibility. An MCP tool server (`mcp_talent.py`) exposes structured search, profile lookup, network connections, hackathon project search, and live GitHub fetching as tools the agent can use.

**Frontend:** Two single HTML files, zero build step. The chat terminal uses a dark terminal aesthetic with streaming markdown rendering and collapsible tool call traces. The network explorer uses the `force-graph` library with custom canvas rendering - each node is a GitHub avatar clipped to a circle with a colored ring, rendered at 60fps with labels, hover tooltips, and a full detail side panel.

**Deployment:** Modal for cloud hosting with volume-mounted data. Locally, every Python script uses `uv` inline script metadata for dependencies - no requirements.txt, no venv, just `uv run server.py` and you're up.

## Challenges we ran into

**GitHub API rate limits** were the biggest constraint. The GraphQL API allows 5,000 points/hour, and profiling a single user costs ~2 points. With 27 seeds having networks of 100-2000 people each, brute-force profiling was impossible. Our tiered priority system (multi-network > mutual follow > small network) was essential to getting a rich dataset within rate limits.

**Making Claude Code useful as a backend agent** required iteration. MCP tool integration via `create_sdk_mcp_server()` had a breaking bug (`CLIConnectionError: ProcessTransport is not ready for writing`). We worked around it by pre-computing search results locally and passing rich context to single-turn `query()` calls. The `PermissionMode` turned out to be a `Literal` type, not an enum - `"bypassPermissions"` as a string, not `PermissionMode.BYPASS_PERMISSIONS`. Small things, but they cost hours.

**Avatar rendering in the graph** was surprisingly hard. `github.com/{user}.png` redirects to `avatars.githubusercontent.com` which strips CORS headers on the redirect, so `crossOrigin='anonymous'` fails. We had to discover the direct `avatars.githubusercontent.com/{user}?s=64` URL which properly supports CORS. Then click events on the settings panel were propagating through to the graph canvas behind it, toggling things off - needed `stopPropagation()` fixes.

**Keeping the graph readable at 400 nodes** required careful tuning. Too much repulsion and the graph explodes to fill the screen; too little and it collapses into an unreadable blob. We exposed physics controls (repulsion, link distance, gravity) directly to the user so they can tune it themselves, which turned out to be the right call.

## Accomplishments that we're proud of

- **The dataset is genuinely useful.** 3,263 developers profiled from 27 seeds with full repo analysis, commit history, PR counts, org memberships, and cross-network relationship mapping. 518 hackathon projects enriched and cross-referenced. You can actually discover people through this that you'd never find on LinkedIn or GitHub search.

- **Non-programmers can use it.** The whole point was making this accessible. Type a question in English, get real answers backed by data. Or open the graph explorer and click around. No SQL, no API calls, no code.

- **The network visualization reveals real structure.** You can visually see clusters of Hack Club developers, systems programmers, and ML researchers naturally separate into communities. Developers who bridge these clusters are immediately visible as the nodes connecting different color groups.

- **Full agentic Claude Code in a web app.** Not a toy chatbot - a real Claude Code instance that can run Python, search the web, read files, and reason through multi-step questions about the dataset. With full tool call transparency so you can see how it's thinking.

- **Zero build tooling.** Every script is self-contained with `uv` inline metadata. Both frontends are single HTML files loading from CDN. `uv run server.py` and you're running. We spent time on the product, not on toolchain configuration.

## What we learned

- **Social graphs are incredibly high-signal.** Mutual follows between known-exceptional developers are a stronger signal than any resume keyword. Someone who appears in 4+ seed networks independently is almost always worth looking at.

- **Graph algorithms reveal hidden structure you can't see from individual profiles.** PageRank on the follow graph surfaces "connector" developers who bridge communities. Betweenness centrality finds people who are the sole link between two clusters. Louvain community detection finds natural groupings that map to real-world affiliations (same hackathon community, same programming niche, same school).

- **Claude Code SDK works for building real agent-powered products**, but you have to work with its constraints. Pre-computing context locally and using single-turn calls with `include_partial_messages=True` for streaming is the reliable pattern. Multi-turn tool use with MCP is the dream but not production-ready yet.

- **Intelligent crawl prioritization matters more than crawling everything.** Our tiered system (multi-network users > mutual follows > small networks) gave us 80% of the value with 10% of the API calls. The most interesting developers are almost always the ones who appear in multiple independent seed networks.

## What's next for Graph-based Agentic Talent Discovery

- **Deeper crawling** - Go 2 hops out from seeds. Profile the most-connected non-seed developers' networks to discover an entirely new ring of hidden talent.
- **Contribution graph** - Build a second graph layer based on who contributes to whose repos, not just who follows whom. Shared contributions are an even stronger signal than follows.
- **Temporal analysis** - Track developer activity over time. Someone whose commit velocity is accelerating is more interesting than someone who peaked 3 years ago.
- **Live monitoring** - Continuously re-crawl and alert when new developers enter the network or when someone ships a breakout repo.
- **Multi-turn agent sessions** - Let the agent maintain conversation context across questions so users can drill deeper ("tell me more about that third person" / "compare them to the Rust developers you found earlier").

## Built With

Claude Code SDK, FastAPI, force-graph, GitHub GraphQL API, Modal, NetworkX, Python, uv
