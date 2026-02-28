#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Analyze merged network data to find diamond-in-the-rough developers.
Looks for people who:
  - Don't market themselves (low followers, low stars)
  - But have interesting/technical repos
  - And are connected to multiple known-good people
  - Bonus: young accounts, academic affiliations, technical depth
"""

import json
import re
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(__file__).parent / "data"


def load_all_crawl_data():
    """Load the merged network plus original ulissemini crawl."""
    merged = json.load(open(DATA_DIR / "merged_network.json"))

    # also load the first crawl (ulissemini's network)
    try:
        uli = json.load(open(DATA_DIR / "network_ulissemini.json"))
        uli_profiles = {p["login"]: p for p in uli.get("profiles", [])}
    except:
        uli_profiles = {}

    return merged, uli_profiles


# Keywords that signal technical depth in repo names/descriptions
TECHNICAL_SIGNALS = [
    # compilers/PLs
    r"compiler", r"parser", r"interpreter", r"language", r"vm\b", r"virtual.machine",
    r"ast\b", r"lexer", r"tokenizer", r"type.check", r"type.system",
    # systems
    r"kernel", r"os\b", r"operating.system", r"bootloader", r"driver",
    r"allocator", r"malloc", r"scheduler", r"filesystem",
    r"risc.?v", r"arm\b", r"x86", r"assembly", r"elf\b",
    # crypto/security
    r"crypto", r"cipher", r"encrypt", r"hash", r"signature", r"zero.knowledge",
    r"zk\b", r"proof", r"ecdsa", r"rsa\b", r"exploit", r"fuzzer", r"ctf\b",
    # ML/AI research
    r"transformer", r"attention", r"diffusion", r"reinforcement.learn", r"\brl\b",
    r"neural", r"backprop", r"gradient", r"interpretab", r"mechanistic",
    r"alignment", r"fine.?tun", r"lora\b", r"rlhf",
    # math/formal
    r"theorem.prov", r"formal.verif", r"lean\b", r"coq\b", r"proof.assist",
    r"category.theory", r"type.theory", r"lambda.calc",
    # hardware/embedded
    r"fpga", r"verilog", r"hdl\b", r"pcb\b", r"embedded", r"microcontroller",
    r"arduino", r"stm32", r"esp32", r"risc", r"soc\b",
    # infra/systems programming
    r"vulkan", r"opengl", r"gpu\b", r"cuda\b", r"shader",
    r"distributed", r"consensus", r"raft\b", r"paxos",
    # from scratch signals
    r"from.scratch", r"from.first.principles", r"minimal", r"tiny",
    r"self.host", r"bootstrap",
]

# Strong languages (building things in these = signal)
STRONG_LANGS = {"Rust", "Haskell", "OCaml", "C", "Assembly", "Zig", "Lean", "Scheme", "Forth", "Nix", "Elixir", "Erlang", "C++"}

# Known good seeds (all previous crawl seeds)
ALL_SEEDS = {
    "UlisseMini", "kognise", "kdrag0n", "slightknack", "cosmicoptima",
    "Mr-Bossman", "Uzay-G", "xrsrke",
    "siraben", "kewbish", "ellenjxu", "mkhan45", "cronokirby",
    "rishiosaur", "Jemoka", "iamnbutler",
    "sampoder", "ClaireBookworm", "malted", "cjdenio", "exu3",
    "jasonappah", "maxwofford",
    "duck-master", "laurgao", "exanova-y", "quantum9Innovation",
}


def compute_diamond_score(p: dict) -> dict:
    """
    Score a profile for "diamond in the rough" potential.
    Returns a dict with the score and reasoning.
    """
    reasons = []
    score = 0.0

    # === NEGATIVE SIGNALS (too established, bots, empty) ===
    if p["followers"] > 3000:
        return {"score": -1, "reasons": ["too established"]}
    if p["total_commits"] == 0 and p["total_prs"] == 0 and p["total_stars"] == 0:
        return {"score": -1, "reasons": ["empty profile"]}
    if not p.get("top_repos"):
        return {"score": -1, "reasons": ["no repos"]}

    # Check for follower-bot patterns (high followers, no real code)
    if p["followers"] > 1000 and p["total_stars"] < 50 and p["total_commits"] < 100:
        return {"score": -1, "reasons": ["likely follower farming"]}

    # === REPO QUALITY (most important) ===
    repos = p.get("top_repos", [])
    technical_repo_count = 0
    for r in repos:
        name = (r.get("name") or "").lower()
        desc = (r.get("desc") or "").lower()
        text = f"{name} {desc}"

        for pattern in TECHNICAL_SIGNALS:
            if re.search(pattern, text, re.IGNORECASE):
                technical_repo_count += 1
                reasons.append(f"technical repo: {r['name']}")
                score += 15
                break

    # Building in strong languages
    langs = set(p.get("top_languages", []))
    strong_lang_count = len(langs & STRONG_LANGS)
    if strong_lang_count > 0:
        score += strong_lang_count * 8
        reasons.append(f"strong langs: {', '.join(langs & STRONG_LANGS)}")

    # === SOCIAL SIGNAL (connected to good people) ===
    connections = p.get("connections", {})
    connected_seeds = [s for s in connections.keys() if s in ALL_SEEDS]

    if len(connected_seeds) >= 3:
        score += 30
        reasons.append(f"connected to {len(connected_seeds)} seeds: {', '.join(connected_seeds[:5])}")
    elif len(connected_seeds) >= 2:
        score += 20
        reasons.append(f"connected to {len(connected_seeds)} seeds: {', '.join(connected_seeds)}")
    elif len(connected_seeds) == 1:
        score += 8
        reasons.append(f"connected to @{connected_seeds[0]}")

    # Mutual follows are stronger signal
    if p.get("is_mutual_follow"):
        score += 15
        reasons.append("mutual follow with seed")

    # In multiple networks
    if p.get("in_multiple_networks"):
        score += 10
        reasons.append("appears in multiple seed networks")

    # === ACTIVITY SIGNAL ===
    # PRs to other projects = collaboration
    if p["total_prs"] > 50:
        score += 15
        reasons.append(f"{p['total_prs']} PRs (strong collaborator)")
    elif p["total_prs"] > 10:
        score += 8
        reasons.append(f"{p['total_prs']} PRs")

    # Consistent commits
    if p["total_commits"] > 500:
        score += 5
        reasons.append(f"{p['total_commits']} commits (very active)")
    elif p["total_commits"] > 100:
        score += 3

    # === UNDERVALUED SIGNAL (this is the alpha) ===
    # High technical signal but low followers = undervalued
    if technical_repo_count >= 2 and p["followers"] < 200:
        score += 25
        reasons.append("HIGH ALPHA: technical repos + low visibility")
    elif technical_repo_count >= 1 and p["followers"] < 100:
        score += 20
        reasons.append("HIGH ALPHA: technical repo + very low visibility")

    # Young account with real output
    if p.get("created_at"):
        created = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
        age_years = (datetime.now(timezone.utc) - created).days / 365.25
        if age_years < 5 and p["total_commits"] > 200:
            score += 10
            reasons.append(f"young account ({age_years:.1f}y) with real output")

    # === BIO/ORG SIGNALS ===
    bio = (p.get("bio") or "").lower()
    orgs = [o.get("login", "").lower() for o in (p.get("orgs") or [])]

    academic_signals = ["mit", "stanford", "cmu", "berkeley", "harvard", "caltech",
                       "cornell", "princeton", "cambridge", "oxford", "eth", "epfl",
                       "'26", "'27", "'28", "'25", "student", "undergrad", "phd"]
    for sig in academic_signals:
        if sig in bio or sig in str(p.get("company", "")).lower():
            score += 10
            reasons.append(f"academic signal: '{sig}' in bio/company")
            break

    interesting_orgs = ["hackclub", "nixos", "rust-lang", "llvm", "mozilla",
                       "google", "meta", "apple", "openai", "anthropic",
                       "deepmind", "huggingface"]
    for org in orgs:
        for sig in interesting_orgs:
            if sig in org:
                score += 5
                reasons.append(f"interesting org: {org}")
                break

    return {"score": round(score, 1), "reasons": reasons}


def main():
    merged, uli_profiles = load_all_crawl_data()
    profiles = merged["profiles"]

    print(f"Analyzing {len(profiles)} profiles from {len(merged['seeds'])} seeds...\n")

    # Score everyone
    scored = []
    for p in profiles:
        if p["login"] in ALL_SEEDS:
            continue  # skip seeds themselves
        result = compute_diamond_score(p)
        if result["score"] > 0:
            scored.append({**p, "diamond_score": result["score"], "diamond_reasons": result["reasons"]})

    scored.sort(key=lambda x: -x["diamond_score"])

    # Save full results
    output = {
        "total_analyzed": len(profiles),
        "total_promising": len(scored),
        "profiles": scored,
    }
    (DATA_DIR / "diamonds.json").write_text(json.dumps(output, indent=2))

    # Print top results grouped by tier
    tier1 = [p for p in scored if p["diamond_score"] >= 60]
    tier2 = [p for p in scored if 40 <= p["diamond_score"] < 60]
    tier3 = [p for p in scored if 25 <= p["diamond_score"] < 40]

    def print_profile(i, p):
        bio = (p.get("bio") or "")[:70]
        loc = p.get("location") or ""
        company = p.get("company") or ""

        print(f"  {i}. @{p['login']} (diamond: {p['diamond_score']}, followers: {p['followers']}, stars: {p['total_stars']})")
        print(f"     {p.get('name') or '?'} [{loc}] {company}")
        if bio:
            print(f"     Bio: {bio}")
        print(f"     {p['total_commits']} commits | {p['total_prs']} PRs | langs: {', '.join(p.get('top_languages', [])[:4])}")
        if p.get("top_repos"):
            for r in p["top_repos"][:3]:
                desc = (r.get("desc") or "")[:55]
                print(f"     - {r['name']} ({r['stars']}★, {r.get('lang','?')}) {desc}")
        if p.get("orgs"):
            print(f"     Orgs: {', '.join(o['login'] for o in p['orgs'][:5])}")
        reasons = [r for r in p["diamond_reasons"] if "HIGH ALPHA" in r or "technical" in r or "seed" in r or "mutual" in r]
        if reasons:
            print(f"     WHY: {'; '.join(reasons[:4])}")
        print()

    print(f"{'='*80}")
    print(f"TIER 1: HIGHEST CONVICTION ({len(tier1)} people)")
    print(f"{'='*80}\n")
    for i, p in enumerate(tier1, 1):
        print_profile(i, p)

    print(f"{'='*80}")
    print(f"TIER 2: STRONG ({len(tier2)} people)")
    print(f"{'='*80}\n")
    for i, p in enumerate(tier2, 1):
        print_profile(i, p)

    print(f"{'='*80}")
    print(f"TIER 3: INTERESTING ({len(tier3)} people)")
    print(f"{'='*80}\n")
    for i, p in enumerate(tier3[:30], 1):  # cap at 30 for readability
        print_profile(i, p)

    print(f"\nTotal: {len(tier1)} tier 1 + {len(tier2)} tier 2 + {len(tier3)} tier 3 = {len(tier1)+len(tier2)+len(tier3)} promising devs")
    print(f"Full data saved to {DATA_DIR / 'diamonds.json'}")


if __name__ == "__main__":
    main()
