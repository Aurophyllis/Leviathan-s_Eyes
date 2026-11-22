"""Compare equal-budget selectors on annotated synthetic videos."""

import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sampling import METHODS, pick_nested
from serve import VideoSession


RATE = 8
SECONDS = 12
CASES = {
    "hard_cut": [(2000, 2375, "triangle"), (8250, 8625, "circle")],
    "quiet_detail": [(5750, 6250, "small_circle")],
    "cross_scene": [(1500, 1875, "circle"), (5750, 6250, "small_circle"),
                    (9000, 9375, "triangle")],
    "jitter": [(5500, 6500, "small_circle"), (10250, 10625, "circle")],
}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def draw_sample(case, time, index):
    background = "#253447"
    if case == "hard_cut" and time >= 2000:
        background = "#632734" if time < 8250 else "#174d3f"
    image = Image.new("RGB", (320, 180), background)
    draw = ImageDraw.Draw(image)
    if case in {"quiet_detail", "cross_scene", "jitter"}:
        if time < 5000 or 7000 <= time < 9750:
            x = (index * 53) % 220
            if case == "jitter":
                for offset in range(-64, 320, 64):
                    shift = 32 if index % 2 else 0
                    draw.rectangle((offset + shift, 0, offset + shift + 31, 179), fill="#d8ac21")
            else:
                draw.rectangle((x, 35, x + 96, 143), fill="#d8ac21")
    for start, end, shape in CASES[case]:
        if not start <= time < end:
            continue
        if shape == "triangle":
            draw.polygon((160, 29, 95, 149, 225, 149), fill="white")
        elif shape == "circle":
            draw.ellipse((103, 33, 216, 146), fill="white")
        else:
            draw.ellipse((154, 84, 165, 95), fill="white")
    return image


def generate(case, directory):
    path = directory / "fixture.mp4"
    command = ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
               "-video_size", "320x180", "-framerate", str(RATE), "-i", "pipe:0",
               "-c:v", "mpeg4", "-q:v", "3", str(path)]
    video = b"".join(draw_sample(case, index * 1000 // RATE, index).tobytes()
                     for index in range(SECONDS * RATE))
    subprocess.run(command, input=video, check=True)
    write_json(directory / "annotation.json", {"ffmpeg": command, "fps": RATE, "duration_ms": SECONDS * 1000,
               "evidence": [{"start_ms": start, "end_ms": end, "shape": shape}
                            for start, end, shape in CASES[case]]})
    return path


def evaluate(output):
    output.mkdir(parents=True)
    trials = []
    for case, evidence in CASES.items():
        case_dir = output / case
        case_dir.mkdir()
        session = VideoSession(generate(case, case_dir), case_dir / "observations")
        for count in (4, 8):
            for method in METHODS:
                began = perf_counter()
                result = session.observe({"method": method, "k": count})
                elapsed = perf_counter() - began
                times = [frame["t_ms"] for frame in result["frames"]]
                if len(times) != count or times != sorted(set(times)) or result["method"] != method:
                    raise RuntimeError(f"Broken frame budget or order: {case}, {count}, {result}")
                hits = [any(start <= time < end for time in times) for start, end, _ in evidence]
                trials.append({
                    "case": case, "k": count, "method": method, "hits": sum(hits),
                    "required": len(hits), "all_hit": all(hits), "evidence_hits": hits,
                    "timestamps_ms": times, "seconds": round(elapsed, 4),
                    "observation": f'{case}/observations/{result["observation_id"]}',
                })
        nested = [row for row in trials if row["case"] == case and row["method"] == "change_nested"]
        if not set(nested[0]["timestamps_ms"]) <= set(nested[1]["timestamps_ms"]):
            raise RuntimeError(f"Nested sampling lost a frame when the budget increased: {case}")
        trace = json.loads((output / nested[0]["observation"] / "sampling_trace.json").read_text())
        previous = set()
        for budget in range(1, 17):
            selection = pick_nested(trace["candidates"], budget, 0, SECONDS * 1000)
            current = {index for _, index, _ in selection}
            if len(current) != budget or not previous <= current:
                raise RuntimeError(f"Nested sampler violated budget or prefix at {case}, k={budget}")
            previous = current
    write_json(output / "trials.json", trials)
    with (output / "trials.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(trials[0]))
        writer.writeheader()
        writer.writerows(trials)
    summary = []
    for count in (4, 8):
        for method in METHODS:
            rows = [row for row in trials if row["k"] == count and row["method"] == method]
            summary.append({"k": count, "method": method, "hits": sum(row["hits"] for row in rows),
                            "required": sum(row["required"] for row in rows),
                            "all_hit_cases": sum(row["all_hit"] for row in rows), "cases": len(rows)})
    write_json(output / "summary.json", summary)
    for row in summary:
        print(f'{row["k"]:>2}  {row["method"]:15}  {row["hits"]}/{row["required"]} evidence intervals'
              f'  {row["all_hit_cases"]}/{row["cases"]} complete cases')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    evaluate(parser.parse_args().output.resolve())
