#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["networkx", "python-louvain", "scipy"]
# ///
"""Build graph.json for the force-directed visualization."""

import json, glob, os
from collections import Counter
import networkx as nx
import community as community_louvain

DATA = "data"
OUT = os.path.join(DATA, "graph.json")

# ---------- load data ----------
with open(os.path.join(DATA, "merged_network.json")) as f:
    merged = json.load(f)

with open(os.path.join(DATA, "diamonds.json")) as f:
    diamonds = json.load(f)

with open(os.path.join(DATA, "promising_devs.json")) as f:
    promising = json.load(f)

# ---------- build profile lookup ----------
profile_by_login = {}
for p in merged["profiles"]:
    profile_by_login[p["login"]] = p
for p in diamonds["profiles"]:
    if p["login"] not in profile_by_login:
        profile_by_login[p["login"]] = p
    else:
        profile_by_login[p["login"]]["diamond_score"] = p.get("diamond_score", 0)
        profile_by_login[p["login"]]["diamond_reasons"] = p.get("diamond_reasons", [])

# promising devs: tier + reason
promising_tier = {p["login"]: p["tier"] for p in promising}
promising_reason = {p["login"]: p.get("reason", "") for p in promising}

# ---------- determine ALL seeds (from all network files) ----------
all_seeds = set()
for fpath in glob.glob(os.path.join(DATA, "network_*.json")):
    name = os.path.basename(fpath).replace("network_", "").replace(".json", "")
    all_seeds.add(name)
all_seeds.update(merged["seeds"])
print(f"Total seeds: {len(all_seeds)}")

# ---------- select nodes ----------
selected = set(all_seeds)

for p in promising:
    selected.add(p["login"])

for p in merged["profiles"]:
    n_seeds = len(p.get("connections", {}))
    if n_seeds >= 3:
        selected.add(p["login"])

diamond_sorted = sorted(diamonds["profiles"], key=lambda p: p.get("diamond_score", 0), reverse=True)
MAX_NODES = 400
for p in diamond_sorted:
    if len(selected) >= MAX_NODES:
        break
    ds = p.get("diamond_score", 0)
    if ds >= 25:
        selected.add(p["login"])

print(f"Selected nodes: {len(selected)}")

# ---------- build directed edges from network files ----------
# Track direction: who follows whom
directed_edges = set()  # (follower, followed)
for fpath in glob.glob(os.path.join(DATA, "network_*.json")):
    with open(fpath) as f:
        net = json.load(f)
    seed = net["seed"]
    if seed not in selected:
        continue
    for follower in net.get("followers", []):
        if follower in selected:
            directed_edges.add((follower, seed))
    for following in net.get("following", []):
        if following in selected:
            directed_edges.add((seed, following))

# also from merged connections
for p in merged["profiles"]:
    if p["login"] not in selected:
        continue
    for seed_login, rels in p.get("connections", {}).items():
        if seed_login not in selected:
            continue
        for rel in rels:
            if rel == "follower":
                directed_edges.add((p["login"], seed_login))
            elif rel == "following":
                directed_edges.add((seed_login, p["login"]))

# collapse to undirected with mutual flag
undirected = {}
for a, b in directed_edges:
    key = tuple(sorted([a, b]))
    if key not in undirected:
        undirected[key] = {"directions": set()}
    undirected[key]["directions"].add((a, b))

edges_out = []
for (a, b), info in undirected.items():
    mutual = len(info["directions"]) >= 2
    edges_out.append({"source": a, "target": b, "mutual": mutual})

print(f"Total edges: {len(edges_out)} ({sum(1 for e in edges_out if e['mutual'])} mutual)")

# ---------- build NetworkX graph for metrics ----------
G = nx.Graph()
for login in selected:
    G.add_node(login)
for e in edges_out:
    G.add_edge(e["source"], e["target"])

pagerank = nx.pagerank(G)
betweenness = nx.betweenness_centrality(G)
communities = community_louvain.best_partition(G)
degree = dict(G.degree())

print(f"Communities detected: {len(set(communities.values()))}")

# ---------- assign tiers ----------
def get_tier(login):
    if login in all_seeds:
        return "seed"
    if login in promising_tier:
        return f"tier{promising_tier[login]}"
    p = profile_by_login.get(login, {})
    ds = p.get("diamond_score", 0)
    if ds >= 60:
        return "tier1"
    elif ds >= 40:
        return "tier2"
    elif ds >= 25:
        return "tier3"
    return "other"

# ---------- build output ----------
nodes = []
for login in selected:
    p = profile_by_login.get(login, {})
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
        "diamond_score": round(p.get("diamond_score", 0), 1),
        "diamond_reasons": p.get("diamond_reasons", []),
        "reason": promising_reason.get(login, ""),
        "location": p.get("location") or "",
        "company": p.get("company") or "",
        "website": p.get("website") or "",
        "twitter": p.get("twitter") or "",
        "created_at": p.get("created_at") or "",
        "connected_seeds": list(p.get("connections", {}).keys()) if p.get("connections") else [],
        # graph metrics
        "pagerank": round(pagerank.get(login, 0), 6),
        "betweenness": round(betweenness.get(login, 0), 6),
        "community": communities.get(login, 0),
        "degree": degree.get(login, 0),
    })

graph = {"nodes": nodes, "links": edges_out}
with open(OUT, "w") as f:
    json.dump(graph, f)

print(f"\nWrote {OUT}: {len(nodes)} nodes, {len(edges_out)} links")

tiers = Counter(n["tier"] for n in nodes)
for t, c in sorted(tiers.items()):
    print(f"  {t}: {c}")

# top pagerank
print("\nTop 10 by PageRank:")
for n in sorted(nodes, key=lambda n: n["pagerank"], reverse=True)[:10]:
    print(f"  {n['id']:20s} PR={n['pagerank']:.5f}  tier={n['tier']}  deg={n['degree']}")

print("\nTop 10 by Betweenness:")
for n in sorted(nodes, key=lambda n: n["betweenness"], reverse=True)[:10]:
    print(f"  {n['id']:20s} BW={n['betweenness']:.5f}  tier={n['tier']}  deg={n['degree']}")
