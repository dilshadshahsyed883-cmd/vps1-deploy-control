"""Read-only, bounded near-duplicate review for the local SML catalog.

Exact SHA256 matches work without AI. Perceptual comparisons require matching
model versions, a 64-bit dHash distance <= 6 and CLIP cosine >= 0.93.
Seven non-overlapping hash chunks make candidate discovery complete for the
six-bit distance threshold, without scanning every possible pair. Never deletes.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import re

_HASH = re.compile(r"^[0-9a-fA-F]{16}$")
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_CHUNKS = (10, 9, 9, 9, 9, 9, 9)


def _load(raw, default):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if value is not None else default
    except (ValueError, TypeError):
        return default


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        numerator = sum(float(x) * float(y) for x, y in zip(a, b))
        den = math.sqrt(sum(float(x) ** 2 for x in a) * sum(float(y) ** 2 for y in b))
        result = numerator / den if den else 0.0
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _fingerprints(asset, record):
    machine = _load(record.get("machine_json"), {})
    if not isinstance(machine, dict):
        machine = {}
    quality = machine.get("quality", {})
    if not isinstance(quality, dict):
        quality = {}
    raw_hash = quality.get("dhash")
    dhash = int(raw_hash, 16) if isinstance(raw_hash, str) and _HASH.fullmatch(raw_hash) else None
    raw_sha = asset.get("sha256")
    sha = raw_sha.lower() if isinstance(raw_sha, str) and _SHA.fullmatch(raw_sha) else None
    vector = _load(record.get("embedding_json"), [])
    if not isinstance(vector, list):
        vector = []
    try:
        score = float(quality.get("score", asset.get("quality") or 0))
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    return sha, dhash, vector, record.get("model_version") or "", score


def review_duplicates(assets, records, public_asset, *, limit=40, offset=0, pair_budget=250000):
    """Return deterministic review groups; no writes and no deletion actions."""
    if not 1 <= limit <= 200 or offset < 0 or pair_budget < 1:
        raise ValueError("Invalid pagination or comparison budget")
    data = []
    for asset in assets:
        if asset.get("type") not in ("image", "video") or not asset.get("id"):
            continue
        record = records.get((asset["type"], asset["id"]), {})
        data.append((asset, *_fingerprints(asset, record)))

    parents = list(range(len(data)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parents[max(ri, rj)] = min(ri, rj)

    seen_sha = {}
    buckets = defaultdict(list)
    comparisons = 0
    budget_exceeded = False
    for i, (asset, sha, dhash, vector, version, _) in enumerate(data):
        kind = asset["type"]
        if sha:
            key = (kind, sha)
            if key in seen_sha:
                union(i, seen_sha[key])
            else:
                seen_sha[key] = i
        if dhash is None or not vector or not version:
            continue
        candidates = set()
        shift = 0
        for chunk_idx, width in enumerate(_CHUNKS):
            key = (kind, version, chunk_idx, (dhash >> shift) & ((1 << width) - 1))
            candidates.update(buckets[key])
            buckets[key].append(i)
            shift += width
        for j in sorted(candidates):
            other, other_sha, other_hash, other_vec, other_version, _ = data[j]
            if find(i) == find(j):
                continue
            if other_hash is None or (dhash ^ other_hash).bit_count() > 6:
                continue
            if comparisons >= pair_budget:
                budget_exceeded = True
                break
            comparisons += 1
            if _cosine(vector, other_vec) >= 0.93:
                union(i, j)
        # Exhausted comparisons are explicitly reported; no partial set is
        # presented as a complete scan.
        if budget_exceeded:
            break

    components = defaultdict(list)
    for i in range(len(data)):
        components[find(i)].append(i)
    clusters = [members for members in components.values() if len(members) > 1]
    clusters.sort(key=lambda members: max(data[i][0].get("created_at") or "" for i in members), reverse=True)

    groups = []
    for members in clusters[offset:offset + limit]:
        def rank(i):
            asset, _, _, _, _, score = data[i]
            return (score, int(asset.get("width") or 0) * int(asset.get("height") or 0), asset.get("created_at") or "")
        best = max(members, key=rank)
        asset, sha, dhash, vector, version, _ = data[best]
        matches = []
        for j in members:
            if j == best:
                continue
            other, other_sha, other_hash, other_vec, other_version, _ = data[j]
            exact = bool(sha and sha == other_sha)
            similarity = 1.0 if exact else _cosine(vector, other_vec) if version == other_version else 0.0
            distance = (dhash ^ other_hash).bit_count() if dhash is not None and other_hash is not None else None
            matches.append({"asset": public_asset(other), "exact": exact,
                            "hash_distance": distance, "similarity": round(similarity, 4)})
        groups.append({
            "asset": public_asset(asset),
            "matches": matches,
            "recommended_keep": asset["id"],
            "reason": "SHA-256 exact match or connected dHash <=6 and CLIP >=0.93 matches. Clusters can be indirect; inspect every item. Recommend highest quality then resolution. No automatic deletion.",
            "group_size": len(members),
        })
    return {
        "groups": groups, "total_groups": len(clusters), "offset": offset, "limit": limit,
        "scanned": len(data), "indexed": sum(bool(sha or (dhash is not None and vec)) for _, sha, dhash, vec, _, _ in data),
        "comparisons": comparisons, "incomplete": budget_exceeded, "automatic_deletion": False,
    }
