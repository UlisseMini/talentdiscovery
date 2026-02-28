#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""
Batch crawl: given a list of seed users, crawl each one's network,
deduplicate profiles, and produce a merged ranked output.

Smart about rate limits: for seeds with huge follower counts, only profiles
users who appear in multiple networks or are mutual follows. For smaller
networks (<500), profiles everyone.
"""

import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from crawl import (
    OUTPUT_DIR, UserProfile,
    get_gh_token, get_network, get_user_profiles, score_user,
)

MAX_CONCURRENT = 10
# Only fully profile networks smaller than this; for bigger ones, be selective
FULL_PROFILE_THRESHOLD = 500


async def batch_crawl(seeds: list[str]):
    token = get_gh_token()
    headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
    }
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    OUTPUT_DIR.mkdir(exist_ok=True)

    seed_networks: dict[str, dict] = {}
    # login -> list of seeds that see this user
    login_sources: dict[str, list[str]] = {}
    # login -> set of relationship types per seed
    login_rels: dict[str, dict[str, list[str]]] = {}
    # track mutual follows (strongest signal)
    mutual_follows: dict[str, set[str]] = {}  # seed -> set of mutual follow logins

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        # Step 1: Fetch all networks
        print(f"Fetching networks for {len(seeds)} seeds...")
        network_results = await asyncio.gather(
            *[get_network(client, seed, sem) for seed in seeds]
        )

        for seed, network in zip(seeds, network_results):
            seed_networks[seed] = network
            followers_set = set(network["followers"])
            following_set = set(network["following"])
            mutuals = followers_set & following_set
            mutual_follows[seed] = mutuals
            unique = followers_set | following_set

            print(f"  @{seed}: {len(followers_set)} followers, {len(following_set)} following, {len(mutuals)} mutual, {len(unique)} unique")

            # Save per-seed network file
            (OUTPUT_DIR / f"network_{seed}.json").write_text(json.dumps({
                "seed": seed,
                "followers": network["followers"],
                "following": network["following"],
                "mutuals": sorted(mutuals),
            }, indent=2))

            for login in unique:
                login_sources.setdefault(login, []).append(seed)
                rels = []
                if login in followers_set:
                    rels.append("follower")
                if login in following_set:
                    rels.append("following")
                login_rels.setdefault(login, {})[seed] = rels

        # Remove seeds from profile targets
        for seed in seeds:
            login_sources.pop(seed, None)
            login_rels.pop(seed, None)

        # Step 2: Decide who to profile
        # Priority tiers:
        #   1. Appears in 2+ seed networks (graph overlap = strong signal)
        #   2. Mutual follow of any seed
        #   3. In a small network (<500 total) — profile everyone
        multi_network = {
            login for login, sources in login_sources.items()
            if len(sources) >= 2
        }
        any_mutual = set()
        for seed, mutuals in mutual_follows.items():
            any_mutual |= mutuals
        any_mutual -= set(seeds)

        small_network_users = set()
        for seed, network in seed_networks.items():
            unique = set(network["followers"]) | set(network["following"])
            if len(unique) <= FULL_PROFILE_THRESHOLD:
                small_network_users |= unique
        small_network_users -= set(seeds)

        to_profile = sorted(multi_network | any_mutual | small_network_users)
        print(f"\nProfile targets: {len(to_profile)} users")
        print(f"  {len(multi_network)} in 2+ networks")
        print(f"  {len(any_mutual)} mutual follows")
        print(f"  {len(small_network_users)} from small networks")

        # Step 3: Profile in chunks
        chunk_size = 50
        all_profiles: list[UserProfile] = []
        for i in range(0, len(to_profile), chunk_size):
            chunk = to_profile[i:i + chunk_size]
            profiles = await get_user_profiles(client, chunk, sem)
            all_profiles.extend(profiles)
            print(f"  {min(i + chunk_size, len(to_profile))}/{len(to_profile)} done ({len(all_profiles)} profiles)")

        # Step 4: Score and rank
        for p in all_profiles:
            p.score = score_user(p)
        all_profiles.sort(key=lambda p: -p.score)

        # Step 5: Save merged output
        merged_file = OUTPUT_DIR / "merged_network.json"
        profiles_out = []
        for p in all_profiles:
            d = asdict(p)
            d["found_via"] = login_sources.get(p.login, [])
            d["connections"] = login_rels.get(p.login, {})
            d["in_multiple_networks"] = p.login in multi_network
            d["is_mutual_follow"] = p.login in any_mutual
            profiles_out.append(d)

        merged_data = {
            "seeds": seeds,
            "total_unique_across_networks": len(login_sources),
            "total_profiled": len(all_profiles),
            "profiles": profiles_out,
        }
        merged_file.write_text(json.dumps(merged_data, indent=2))

        # Step 6: Print results
        print(f"\n{'='*80}")
        print(f"Top developers across networks of: {', '.join('@'+s for s in seeds)}")
        print(f"{'='*80}\n")

        for i, p in enumerate(all_profiles[:50]):
            rels = login_rels.get(p.login, {})
            conn_parts = []
            for seed, rel_list in rels.items():
                conn_parts.append(f"@{seed}({'|'.join(rel_list)})")
            conn_str = ", ".join(conn_parts)

            flags = []
            if p.login in multi_network:
                flags.append("MULTI-NET")
            if p.login in any_mutual:
                flags.append("MUTUAL")
            flag_str = f" [{', '.join(flags)}]" if flags else ""

            bio_str = f" - {p.bio[:60]}" if p.bio else ""
            loc_str = f" [{p.location}]" if p.location else ""

            print(f"{i+1:2}. @{p.login} (score: {p.score}){flag_str}")
            print(f"    {p.name or '?'}{loc_str}{bio_str}")
            print(f"    {p.followers} followers | {p.total_stars}★ | {p.total_commits} commits | {p.total_prs} PRs")
            if conn_str:
                print(f"    Via: {conn_str}")
            if p.top_repos:
                top = p.top_repos[0]
                desc = (top.get('desc') or '')[:60]
                print(f"    Top repo: {top['name']} ({top['stars']}★) {desc}")
            if p.orgs:
                print(f"    Orgs: {', '.join(o['login'] for o in p.orgs[:5])}")
            print()

        print(f"Saved {len(all_profiles)} profiles to {merged_file}")

        # Rate limit check
        resp = await client.get("https://api.github.com/rate_limit")
        if resp.status_code == 200:
            rl = resp.json().get("resources", {}).get("graphql", {})
            print(f"GraphQL rate limit: {rl.get('remaining')}/{rl.get('limit')} remaining")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: batch_crawl.py user1 user2 user3 ...")
        sys.exit(1)
    asyncio.run(batch_crawl(sys.argv[1:]))
