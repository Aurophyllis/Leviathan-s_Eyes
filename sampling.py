"""Query-independent visual change scoring for a fixed frame budget."""

import subprocess
import tempfile

METHODS = ("uniform", "change_only", "change_guarded", "change_nested")
GRID = (64, 36)
CUT_MIN = 18
CUT_RATIO = 1.5
MOTION_MIN = 3
MOTION_BUDGET = 4
FALLBACK_MS = 2000
MIN_SPACING_MS = 350
HUE_DISTANCE = [min(distance, 256 - distance) * 2 for distance in range(256)]
TRACE_CONFIG = {
    "grid": GRID, "cut_min": CUT_MIN, "cut_ratio": CUT_RATIO,
    "motion_min": MOTION_MIN, "motion_budget": MOTION_BUDGET,
    "fallback_ms": FALLBACK_MS, "spacing_ms": MIN_SPACING_MS,
    "nested_anchor_interval": 4,
}


def hsv_change(previous, current):
    from PIL import ImageChops, ImageStat

    old_h, old_s, old_v = previous
    new_h, new_s, new_v = current
    hue = ImageChops.difference(old_h, new_h).point(HUE_DISTANCE)
    hue = ImageChops.multiply(hue, ImageChops.darker(old_s, new_s))
    saturation = ImageChops.difference(old_s, new_s)
    value = ImageChops.difference(old_v, new_v)
    return sum(weight * ImageStat.Stat(channel).mean[0]
               for weight, channel in zip((0.25, 0.25, 0.5), (hue, saturation, value)))


def scan_changes(video, candidates):
    """Stream small RGB frames, retaining only scores and the previous image."""
    from PIL import Image

    if not candidates:
        return []
    first, last = candidates[0][1], candidates[-1][1]
    wanted = {index for _, index in candidates}
    byte_count = GRID[0] * GRID[1] * 3
    filters = f"select='between(n,{first},{last})',scale={GRID[0]}:{GRID[1]}:flags=area"
    command = ("ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", "0:V:0",
               "-vf", filters, "-frames:v", str(last - first + 1), "-fps_mode", "passthrough",
               "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1")
    scores = []
    previous = None
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors) as process:
            try:
                for index in range(first, last + 1):
                    data = process.stdout.read(byte_count)
                    if len(data) != byte_count:
                        break
                    if index in wanted:
                        channels = Image.frombytes("RGB", GRID, data).convert("HSV").split()
                        scores.append(hsv_change(previous, channels) if previous else 0.0)
                        previous = channels
            finally:
                process.stdout.close()
                code = process.wait()
        if code or len(scores) != len(candidates):
            errors.seek(0)
            raise ValueError(errors.read().decode("utf-8", errors="replace").strip()
                             or "FFmpeg did not return all requested thumbnail frames")
    return scores


def classify(candidates, scores):
    rows = []
    last_time = last_trigger = candidates[0][0]
    motion = 0.0
    for index, ((time, source_index), score) in enumerate(zip(candidates, scores)):
        left = scores[index - 1] if index else 0
        right = scores[index + 1] if index + 1 < len(scores) else 0
        motion += max(score - MOTION_MIN, 0) * min(time - last_time, 1000) / 1000
        if (score >= CUT_MIN and score >= CUT_RATIO * max(left, right)
                and time - last_trigger >= MIN_SPACING_MS):
            trigger = "cut"
        elif motion >= MOTION_BUDGET and time - last_trigger >= MIN_SPACING_MS:
            trigger = "motion"
        elif time - last_trigger >= FALLBACK_MS:
            trigger = "timeout"
        else:
            trigger = "change_score"
        if trigger != "change_score":
            motion = 0.0
            last_trigger = time
        rows.append({"t_ms": time, "source_index": source_index,
                     "change_score": round(score, 5), "trigger": trigger})
        last_time = time
    return rows


def rank_changes(rows):
    priorities = {"cut": 3, "motion": 2, "timeout": 1, "change_score": 0}
    return sorted(rows, key=lambda row: (
        -priorities[row["trigger"]], -row["change_score"], row["t_ms"],
    ))


def pick_changes(rows, count, anchors=()):
    chosen = {index: (time, "time_anchor") for time, index in anchors}
    ranked = rank_changes(rows)
    for spacing in (MIN_SPACING_MS, 0):
        for row in ranked:
            if len(chosen) == count:
                break
            index, time = row["source_index"], row["t_ms"]
            if index not in chosen and all(abs(time - t) >= spacing for t, _ in chosen.values()):
                chosen[index] = (time, row["trigger"])
    return sorted((time, index, reason) for index, (time, reason) in chosen.items())


def pick_nested(rows, count, start, end):
    """Use a budget-independent selection order, then display its prefix in time order."""
    ranked = rank_changes(rows)
    chosen = {}
    anchor_interval = TRACE_CONFIG["nested_anchor_interval"]
    for slot in range(min(count, len(rows))):
        if slot % anchor_interval == 0:
            anchor = slot // anchor_interval
            level = (anchor + 1).bit_length() - 1
            offset = anchor + 1 - 2**level
            target = start + (2 * offset + 1) * (end - start) / 2**(level + 1)
            remaining = (row for row in rows if row["source_index"] not in chosen)
            row = min(remaining, key=lambda item: (abs(item["t_ms"] - target), item["t_ms"]))
            reason = "time_anchor"
        else:
            first = None
            for candidate in ranked:
                index, time = candidate["source_index"], candidate["t_ms"]
                if index in chosen:
                    continue
                if first is None:
                    first = candidate
                if all(abs(time - t) >= MIN_SPACING_MS for t, _ in chosen.values()):
                    first = candidate
                    break
            row = first
            reason = row["trigger"]
        chosen[row["source_index"]] = (row["t_ms"], reason)
    return sorted((time, index, reason) for index, (time, reason) in chosen.items())
