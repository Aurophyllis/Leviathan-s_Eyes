"""Compare AKS, FOCUS and visual change sampling on the same synthetic questions."""

import argparse
import base64
import csv
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aks import select_aks
from app import frame_times, uniform, write_selected_frames
from focus_replay import load_focus, replay
from sampling import classify, pick_nested, scan_changes
from serve import make_sheet


# The scorer detects requested shape classes from pixels. Annotation intervals are used only for evaluation.
QUESTIONS = {
    "hard_cut": ("Which is the order of the large white shapes?", ["triangle, then circle",
                 "circle, then triangle", "two circles", "no white shapes"], "A", ("triangle", "circle")),
    "quiet_detail": ("What small white mark briefly appears in the quiet middle of the clip?",
                     ["a line", "a round dot", "a triangle", "no mark"], "B", ("small_circle",)),
    "cross_scene": ("What is the order of the three white marks?",
                   ["triangle, dot, large circle", "large circle, triangle, dot",
                    "large circle, dot, triangle", "dot, triangle, large circle"],
                   "C", ("circle", "small_circle", "triangle")),
    "jitter": ("What white marks appear during the quiet middle and the later still period, in order?",
               ["only a triangle", "only a square", "no marks", "a tiny dot, then a large circle"],
               "D", ("small_circle", "circle")),
}


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def white_masks(video, count):
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:V:0", "-vf", "scale=64:36:flags=area",
         "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    pixels = np.frombuffer(completed.stdout, dtype=np.uint8).reshape(-1, 36, 64, 3)
    if len(pixels) != count:
        raise RuntimeError("Thumbnail frames do not match the candidate timeline")
    return np.all(pixels >= 210, axis=-1)


def template_masks():
    templates = {}
    for name in ("triangle", "circle", "small_circle"):
        image = Image.new("L", (320, 180))
        draw = ImageDraw.Draw(image)
        if name == "triangle":
            draw.polygon((160, 29, 95, 149, 225, 149), fill=255)
        else:
            bounds = (103, 33, 216, 146) if name == "circle" else (154, 84, 165, 95)
            draw.ellipse(bounds, fill=255)
        templates[name] = np.asarray(image.resize((64, 36), Image.Resampling.BOX)) >= 210
    return templates


def shape_score(mask, templates):
    return max(np.count_nonzero(mask & template) / np.count_nonzero(mask | template)
               for template in templates)


def model_answer(model, question, options, sheet):
    prompt = question + "\n" + "\n".join(f"{letter}. {option}" for letter, option in zip("ABCD", options))
    prompt += "\nUse the numbered thumbnails in time order as visual evidence. Reply with just A, B, C or D."
    body = {"model": model, "stream": False, "think": False,
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 24},
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(sheet.read_bytes()).decode("ascii")]}]}
    request = Request("http://127.0.0.1:11434/api/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
        result = json.load(response)
    answer = result["message"]["content"].strip()
    match = re.match(r"^([A-D])(?:[.)](?:\s|$)|$)", answer, re.IGNORECASE)
    return {"prompt": prompt, "response": result, "choice": match[1].upper() if match else None}


def observation(video, timeline, indices, folder):
    folder.mkdir()
    write_selected_frames(video, folder, [timeline[index][1] for index in indices])
    frames = [{"frame_id": n, "t_ms": timeline[index][0], "image": f"frame-{n:03}.png"}
              for n, index in enumerate(indices, 1)]
    make_sheet(folder, frames)
    save(folder / "frames.json", frames)
    return folder / "sheet.png"


def compare(archive, fixtures, output, k, model, seed):
    selector = load_focus(archive)
    output.mkdir(parents=True)
    templates = template_masks()
    results = []
    aks_depth = k.bit_length() - 1
    for case, (question, options, correct, shapes) in QUESTIONS.items():
        source = fixtures / case
        video = source / "fixture.mp4"
        annotation = json.loads((source / "annotation.json").read_text(encoding="utf-8"))
        timeline = frame_times(video)
        count, duration = len(timeline), annotation["duration_ms"]
        if not 1 <= k <= count or [index for _, index in timeline] != list(range(count)):
            raise ValueError("Expected the unmodified synthetic videos and k within their frame count")
        case_dir = output / case
        case_dir.mkdir()
        save(case_dir / "question.json", {
            "video_sha256": sha256(video.read_bytes()).hexdigest(), "question": question,
            "options": options, "correct": correct, "scorer_templates": shapes, "annotation": annotation,
        })
        masks = white_masks(video, count)
        requested = [templates[name] for name in shapes]

        def score(index, query):
            if query != question:
                raise ValueError("Unexpected query in frame scorer")
            return shape_score(masks[index], requested)

        focal, trace = replay(selector, count, annotation["fps"], question, score, k, seed)
        save(case_dir / "focus_trace.json", trace)
        budget = trace["verification"]["unique_scoring_calls"]
        all_ids = list(range(count))
        full_scores = [score(index, question) for index in all_ids]
        limited_ids = [index for _, index in uniform(timeline, 0, duration, budget)]
        limited_scores = [score(index, question) for index in limited_ids]
        change = classify(timeline, scan_changes(video, timeline))
        save(case_dir / "scoring.json", {"full_scores": full_scores, "limited_candidate_ids": limited_ids,
                                         "limited_scores": limited_scores, "change": change})
        selections = {
            "uniform": ([index for _, index in uniform(timeline, 0, duration, k)], 0),
            "change_nested": ([index for _, index, _ in pick_nested(change, k, 0, duration)], 0),
            "aks_full": (select_aks(all_ids, full_scores, k, max_depth=aks_depth), count),
            "aks_matched": (select_aks(limited_ids, limited_scores, k, max_depth=aks_depth), budget),
            "focus": (focal, budget),
        }
        for method, (indices, calls) in selections.items():
            if len(indices) != k:
                raise RuntimeError(f"Frame budget mismatch for {case}/{method}: {indices}")
            folder = case_dir / method
            sheet = observation(video, timeline, indices, folder)
            times = [timeline[index][0] for index in indices]
            hits = [any(item["start_ms"] <= t < item["end_ms"] for t in times)
                    for item in annotation["evidence"]]
            row = {"case": case, "method": method, "k": k, "scoring_calls": calls, "candidate_frames": count,
                   "hits": sum(hits), "required": len(hits), "all_evidence": all(hits),
                   "timestamps_ms": times, "choice": None, "correct": None}
            if model:
                reply = model_answer(model, question, options, sheet)
                save(folder / "model.json", reply)
                row.update(choice=reply["choice"], correct=reply["choice"] == correct)
            save(folder / "result.json", row)
            results.append(row)
            print(f'{case}/{method}: {row["hits"]}/{row["required"]} evidence, B={calls}, answer={row["choice"]}',
                  flush=True)
    save(output / "trials.json", results)
    with (output / "trials.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    summary = []
    for method in selections:
        rows = [row for row in results if row["method"] == method]
        summary.append({"method": method, "evidence_hits": sum(row["hits"] for row in rows),
                        "evidence_required": sum(row["required"] for row in rows),
                        "complete_cases": sum(row["all_evidence"] for row in rows),
                        "correct_answers": sum(row["correct"] for row in rows) if model else None,
                        "cases": len(rows), "scoring_calls": sum(row["scoring_calls"] for row in rows)})
    save(output / "summary.json", {"k": k, "seed": seed, "aks_max_depth": aks_depth,
                                    "model": model, "results": summary})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("focus_archive", type=Path)
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--k", type=int, choices=(4, 8), default=4)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--ollama-model")
    args = parser.parse_args()
    compare(args.focus_archive.resolve(), args.fixtures.resolve(), args.output.resolve(),
            args.k, args.ollama_model, args.seed)
