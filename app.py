"""Extract a uniform set of timestamped frames from a local video."""

import argparse
from bisect import bisect_left
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import tempfile


def run(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, encoding="utf-8",
                            errors="replace", check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"{args[0]} exited with {result.returncode}")
    return result.stdout


def frame_times(video):
    data = json.loads(run(
        "ffprobe", "-v", "error", "-select_streams", "V:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(video),
    ))
    frames = []
    first = None
    for index, frame in enumerate(data.get("frames", [])):
        stamp = frame.get("best_effort_timestamp_time")
        if stamp in (None, "N/A"):
            continue
        pts = Decimal(stamp)
        if first is None:
            first = pts
        time_ms = round((pts - first) * 1000)
        if not frames or time_ms > frames[-1][0]:
            frames.append((time_ms, index))
    if not frames:
        raise ValueError("Video contains no frames with usable timestamps")
    return frames


def uniform(frames, start, end, count):
    available = [(time, index) for time, index in frames if start <= time < end]
    if len(available) <= count:
        return available
    times = [time for time, _ in available]
    chosen = []
    lower = 0
    for slot in range(count):
        target = start + (slot + 0.5) * (end - start) / count
        upper = len(available) - (count - slot)
        right = min(bisect_left(times, target, lower, upper + 1), upper)
        left = max(lower, right - 1)
        nearest = min((left, right), key=lambda i: (abs(times[i] - target), i))
        chosen.append(available[nearest])
        lower = nearest + 1
    return chosen


def extract(video, output, start, end, count, *, frames=None):
    if output.exists():
        raise ValueError(f"Output directory already exists: {output}")
    if not video.is_file():
        raise ValueError(f"Video file does not exist: {video}")
    if frames is None:
        frames = frame_times(video)
    end = end if end is not None else frames[-1][0] + 1
    if end <= start:
        raise ValueError("End must be greater than start")
    selected = uniform(frames, start, end, count)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        stage = Path(folder) / "result"
        stage.mkdir()
        if selected:
            expression = "+".join(f"eq(n,{index})" for _, index in selected)
            run("ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", "0:V:0",
                "-vf", f"select='{expression}'", "-frames:v", str(len(selected)),
                "-fps_mode", "passthrough", "-start_number", "1", "frame-%03d.png",
                cwd=stage)
        records = []
        for number, (time, _) in enumerate(selected, 1):
            name = f"frame-{number:03d}.png"
            if not (stage / name).is_file():
                raise ValueError(f"Expected FFmpeg output is missing: {name}")
            records.append({"frame_id": number, "t_ms": time, "reason": "uniform", "image": name})
        manifest = {
            "method": "uniform", "start_ms": start, "end_ms": end, "k": count,
            "shortfall": count - len(records), "frames": records,
        }
        (stage / "observation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-ms", type=int, default=0)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--k", type=int, default=4)
    args = parser.parse_args()
    if args.start_ms < 0 or not 1 <= args.k <= 128:
        parser.error("Use a nonnegative start and a frame count between 1 and 128")
    try:
        extract(args.video.resolve(), args.output.absolute(), args.start_ms, args.end_ms, args.k)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
