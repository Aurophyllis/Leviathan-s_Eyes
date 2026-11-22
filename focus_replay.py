"""Replay the pinned FOCUS selector with an injected frame scorer."""

from hashlib import sha256
from math import log, sqrt
from statistics import fmean, pvariance
from types import ModuleType
from zipfile import ZipFile


REFERENCE = "d469757cd89976117467294fd1f177026d1a627d"
SCRIPT_SHA256 = "404e266328f051c0377688eac72fe5f24b777f52c008bf1103337b261c34beeb"


def load_focus(archive):
    with ZipFile(archive) as files:
        source = files.read("FOCUS-main/focus.py")
    if sha256(source).hexdigest() != SCRIPT_SHA256:
        raise ValueError("FOCUS source hash differs from the pinned, inspected snapshot")
    module = ModuleType("focus_reference")
    exec(compile(source, "FOCUS-main/focus.py", "exec"), module.__dict__)
    return module.FOCUS


def confidence_score(values, total_samples):
    mean = fmean(values)
    if total_samples < 2:
        return mean
    log_total = log(total_samples)
    variance = max(pvariance(values), 1e-6)
    count = len(values)
    return mean + sqrt(2 * log_total * variance / count) + 3 * log_total / count


class VideoTimeline:
    def __init__(self, frame_count, fps):
        self.frame_count = frame_count
        self.fps = fps

    def __len__(self):
        return self.frame_count

    def get_avg_fps(self):
        return self.fps


def replay(selector_class, frame_count, fps, query, score, k, seed):
    import numpy as np

    batches = []

    def score_batch(video, question, indices):
        positions = [int(index) for index in indices]
        values = [float(score(index, question)) for index in positions]
        batches.append(dict(zip(positions, values)))
        return values

    selector = selector_class(score_batch)
    selected, trace = selector.select_keyframes(VideoTimeline(frame_count, fps), query, k,
                                               rng=np.random.default_rng(seed))
    selected = [int(index) for index in selected]
    if len(selected) != min(k, frame_count) or selected != sorted(set(selected)):
        raise RuntimeError(f"FOCUS returned an unexpected frame list: {selected}")
    measured = set().union(*batches)
    coarse, fine = trace["coarse_sampling"], trace["fine_sampling"]
    coarse_scores = batches[0]
    if sum(map(len, batches)) != len(measured) or trace["video_metadata"]["budget_used"] != len(measured):
        raise RuntimeError("FOCUS scoring budget does not match measured distinct frame calls")
    checks = []
    for arm in trace["arms_info"]["arms"]:
        values = [value for index, value in coarse_scores.items()
                  if arm["start_frame"] <= index <= arm["end_frame"]]
        expected = confidence_score(values, len(coarse_scores))
        error = abs(expected - arm["focus_after_coarse"])
        if error > 1e-10:
            raise RuntimeError(f"Coarse Bernstein bound mismatch for arm {arm['arm_id']}")
        checks.append({"arm_id": arm["arm_id"], "recomputed": expected, "absolute_error": error})
    trace["verification"] = {
        "reference_commit": REFERENCE, "reference_script_sha256": SCRIPT_SHA256,
        "seed": seed, "unique_scoring_calls": len(measured),
        "coarse_calls": coarse["budget_used"], "fine_calls": fine["budget_used"],
        "coarse_bounds": checks, "interpolated_selected": sorted(set(selected) - measured),
    }
    return selected, trace
