"""Evaluate fixed frame and relevance budgets on a Video-MME / KFS-Bench subset."""

import argparse
from collections import defaultdict
import csv
from hashlib import file_digest
import json
from pathlib import Path
import shutil
import sys
from time import perf_counter

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aks import select_aks
from app import frame_times, run, uniform
from clip_scorer import ClipScorer
from focus_replay import load_focus, replay
from sampling import GRID, classify, hsv_change, pick_nested
from serve import make_sheet
from compare_methods import model_answer, save


def digest(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def candidates(video, directory, count):
    raw_times = frame_times(video)
    end = raw_times[-1][0] + 1
    # The first common budget is a deterministic candidate pool spanning the full clip.
    timeline = uniform(raw_times, 0, end, min(count, len(raw_times)))
    directory.mkdir()
    select = "+".join(f"eq(n,{index})" for _, index in timeline)
    run("ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", "0:V:0",
        "-vf", f"select='{select}',scale='min(512,iw)':-2:flags=area",
        "-frames:v", str(len(timeline)), "-fps_mode", "passthrough", "-start_number", "1", "frame-%03d.png",
        cwd=directory)
    paths = [directory / f"frame-{index + 1:03d}.png" for index in range(len(timeline))]
    if not all(path.is_file() for path in paths):
        raise RuntimeError("Failed to decode at least one common candidate frame")
    return timeline, paths, end


def score_change(paths, timeline):
    scores = []
    previous = None
    for path in paths:
        with Image.open(path) as image:
            channels = image.convert("RGB").resize(GRID, Image.Resampling.BOX).convert("HSV").split()
        scores.append(hsv_change(previous, channels) if previous else 0.0)
        previous = channels
    return classify(timeline, scores)


def grouped_evidence(parts, duration):
    scenes = defaultdict(list)
    for part in parts:
        start, end = part["start_ms"], part["end_ms"]
        if not 0 <= start < end <= duration + 1000:
            raise ValueError(f"KFS-Bench annotation exceeds source duration: {part}")
        scenes[part["scene_id"]].append((start, min(end, duration)))
    return scenes


def measure_hits(times, scenes):
    return {name: any(start <= time < end for start, end in intervals for time in times)
            for name, intervals in scenes.items()}


def benchmark(manifests, output, clip_folder, focus_archive, count, k, seed, model):
    clip = ClipScorer(clip_folder)
    focus_class = load_focus(focus_archive)
    output.mkdir(parents=True)
    inputs = {}
    for manifest in manifests:
        inputs[manifest.name + str(len(inputs))] = {
            "sha256": digest(manifest), "samples": json.loads(manifest.read_text(encoding="utf-8"))["samples"],
        }
    config = {
        "candidate_cap": count, "k": k, "seed": seed, "model": model,
        "clip_checkpoint_sha256": digest(clip_folder / "model_quantized.onnx"),
        "manifests": inputs,
    }
    save(output / "protocol.json", config)
    rows = []
    seen = set()
    for manifest in manifests:
        for sample in json.loads(manifest.read_text(encoding="utf-8"))["samples"]:
            if sample["id"] in seen:
                raise ValueError("Duplicate question ID")
            seen.add(sample["id"])
            video = manifest.parent / sample["file"]
            if digest(video) != sample["video_sha256"]:
                raise ValueError(f'Video hash mismatch for {sample["id"]}')
            directory = output / sample["id"]
            directory.mkdir()
            start = perf_counter()
            timeline, paths, duration = candidates(video, directory / "candidates", count)
            preparation = perf_counter() - start
            # FOCUS arm geometry is in candidate positions, with at least one position per virtual second.
            candidate_fps = max(1.0, len(paths) * 1000 / duration)
            save(directory / "candidate_timeline.json", {
                "duration_ms": duration, "focus_candidate_fps": candidate_fps,
                "frames": [{"t_ms": time, "original_index": index} for time, index in timeline],
            })
            by_position = [(time, position) for position, (time, _) in enumerate(timeline)]
            if len(paths) < k:
                raise ValueError("Not enough common candidate frames for requested k")
            scenes = grouped_evidence(sample["evidence"], duration)
            ceiling = measure_hits([time for time, _ in timeline], scenes)
            question = sample["question"]
            save(directory / "question.json", sample)

            def score_batch(indices, query):
                return clip.score(query, [paths[index] for index in indices])

            start = perf_counter()
            focal, trace = replay(focus_class, len(paths), candidate_fps, question, None, k, seed,
                                  batch_score=score_batch)
            focus_time = perf_counter() - start
            budget = trace["verification"]["unique_scoring_calls"]
            save(directory / "focus_trace.json", trace)
            start = perf_counter()
            limited = [position for _, position in uniform(by_position, 0, duration, budget)]
            limited_scores = score_batch(limited, question)
            aks = select_aks(limited, limited_scores, k, max_depth=k.bit_length() - 1)
            aks_time = perf_counter() - start
            save(directory / "aks_scores.json", dict(zip(limited, limited_scores)))
            start = perf_counter()
            change = score_change(paths, by_position)
            nested = [pos for _, pos, _ in pick_nested(change, k, 0, duration)]
            change_time = perf_counter() - start
            save(directory / "changes.json", change)
            start = perf_counter()
            uniform_ids = [position for _, position in uniform(by_position, 0, duration, k)]
            methods = {
                "uniform": (uniform_ids, 0, perf_counter() - start),
                "change_nested": (nested, 0, change_time),
                "aks_matched": (aks, budget, aks_time),
                "focus": (focal, budget, focus_time),
            }
            for method, (indices, calls, elapsed) in methods.items():
                if len(indices) != k or indices != sorted(set(indices)):
                    raise RuntimeError(f"Invalid selection in {method}: {indices}")
                folder = directory / method
                folder.mkdir()
                frames = []
                for number, index in enumerate(indices, 1):
                    name = f"frame-{number:03d}.png"
                    shutil.copyfile(paths[index], folder / name)
                    frames.append({"frame_id": number, "t_ms": timeline[index][0], "image": name})
                save(folder / "frames.json", frames)
                make_sheet(folder, frames)
                hits = measure_hits([frame["t_ms"] for frame in frames], scenes)
                row = {"id": sample["id"], "duration_group": sample["duration_group"], "method": method,
                       "k": k, "candidate_count": len(paths), "scoring_calls": calls,
                       "scene_hits": sum(hits.values()), "scene_count": len(hits),
                       "candidate_scene_hits": sum(ceiling.values()), "all_scenes": all(hits.values()),
                       "shared_preparation_seconds": round(preparation, 3),
                       "selector_seconds": round(elapsed, 3), "choice": None, "correct": None}
                if model:
                    reply = model_answer(model, question, sample["options"], folder / "sheet.png")
                    save(folder / "model.json", reply)
                    row.update(choice=reply["choice"], correct=reply["choice"] == sample["answer"])
                save(folder / "result.json", {**row, "scene_hits_by_id": hits, "candidate_ceiling_by_id": ceiling})
                rows.append(row)
                print(f'{sample["id"]}/{method}: {row["scene_hits"]}/{row["scene_count"]} scenes, '
                      f'B={calls}, answer={row["choice"]}', flush=True)
    save(output / "trials.json", rows)
    with (output / "trials.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = []
    for method in methods:
        subset = [row for row in rows if row["method"] == method]
        summary.append({
            "method": method, "cases": len(subset), "scene_hits": sum(row["scene_hits"] for row in subset),
            "scene_count": sum(row["scene_count"] for row in subset),
            "candidate_scene_hits": sum(row["candidate_scene_hits"] for row in subset),
            "scoring_calls": sum(row["scoring_calls"] for row in subset),
            "correct": sum(row["correct"] for row in subset) if model else None,
        })
    save(output / "summary.json", {"k": k, "model": model, "results": summary})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip_folder", type=Path)
    parser.add_argument("focus_archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("manifests", type=Path, nargs="+")
    parser.add_argument("--candidate-cap", type=int, default=64)
    parser.add_argument("--k", type=int, choices=(4, 8), default=4)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--ollama-model")
    args = parser.parse_args()
    if not args.k <= args.candidate_cap <= 128:
        parser.error("candidate-cap must be between k and 128")
    benchmark([path.resolve() for path in args.manifests], args.output.resolve(), args.clip_folder.resolve(),
              args.focus_archive.resolve(), args.candidate_cap, args.k, args.seed, args.ollama_model)
