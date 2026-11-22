"""Offline AKS frame selection from fixed frame IDs and relevance scores."""

import heapq
from math import isfinite
from statistics import fmean, pstdev


def select_aks(frame_ids, relevance, budget, *, stride=1, t1=0.8, t2=-100, max_depth=5):
    """Reproduce AKS's normalized-score partition and per-interval frame quotas."""
    if len(frame_ids) != len(relevance) or min(budget, stride) < 1 or max_depth < 0:
        raise ValueError("Matching frame and score lengths, positive budget and stride are required")
    stop = len(frame_ids) - len(frame_ids) % stride
    ids = frame_ids[:stop:stride]
    scores = relevance[:stop:stride]
    if not all(isfinite(score) for score in scores):
        raise ValueError("AKS scores must be finite")
    if len(ids) < budget:
        return list(ids)
    minimum, maximum = min(scores), max(scores)
    span = maximum - minimum
    normalized = [(score - minimum) / span if span else 0.0 for score in scores]
    chosen = []
    intervals = [(0, len(ids), 0)]
    while intervals:
        start, end, depth = intervals.pop()
        quota = budget // 2**depth
        if start == end or not quota:
            continue
        segment = normalized[start:end]
        top = heapq.nlargest(budget, range(len(segment)), key=segment.__getitem__)
        gain = fmean(segment[i] for i in top) - fmean(segment)
        if depth == max_depth or (gain > t1 and pstdev(segment) > t2):
            chosen.extend(ids[start + i] for i in top[:quota])
        else:
            middle = start + (end - start) // 2
            intervals.extend(((start, middle, depth + 1), (middle, end, depth + 1)))
    return sorted(chosen)
