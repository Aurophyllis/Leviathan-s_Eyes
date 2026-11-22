"""Replay archived BLIP scores and verify against the pinned AKS author script."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aks import select_aks


REFERENCE = "b0b8a58fedf1d05d78151e2969cecde60e83721d"
SCRIPT_SHA256 = "594557130aa702fb1c3eafae9df120514d36e9684443b415e0483dce14b4f149"
SCORE_BASE = "AKS-main/outscores/longvideobench/blip/"
RESULT_BASE = "AKS-main/selected_frames/longvideobench/blip/"


def save(path, data, *, compact=False):
    path.write_text(json.dumps(data, indent=None if compact else 2) + "\n", encoding="utf-8")


def compare(actual, expected):
    if len(actual) != len(expected):
        raise RuntimeError(f"Unequal row counts: {len(actual)} and {len(expected)}")
    different = [index for index, (a, b) in enumerate(zip(actual, expected)) if a != b]
    return {"rows": len(actual), "exact": len(actual) - len(different),
            "different_0based": different}


def verify(archive, output):
    with ZipFile(archive) as files:
        source = files.read("AKS-main/frame_select.py")
        if sha256(source).hexdigest() != SCRIPT_SHA256:
            raise ValueError("Author script hash differs from the inspected AKS snapshot")
        if files.comment and files.comment.decode("ascii") != REFERENCE:
            raise ValueError("Archive commit differs from the pinned AKS snapshot")
        scores_bytes = files.read(SCORE_BASE + "scores.json")
        frames_bytes = files.read(SCORE_BASE + "frames.json")
        published = {32: json.loads(files.read(RESULT_BASE + "longvideobench_32.json")),
                     64: json.loads(files.read(RESULT_BASE + "selected_frames.json"))}
    scores, frames = json.loads(scores_bytes), json.loads(frames_bytes)
    if len(scores) != len(frames):
        raise ValueError("Archive has unequal score and frame rows")
    output.mkdir(parents=True)
    inputs = output / "reference_inputs"
    inputs.mkdir()
    (inputs / "frame_select.py").write_bytes(source)
    (inputs / "scores.json").write_bytes(scores_bytes)
    (inputs / "frames.json").write_bytes(frames_bytes)
    summary = {
        "reference_commit": REFERENCE, "reference_script_sha256": SCRIPT_SHA256,
        "scores_sha256": sha256(scores_bytes).hexdigest(), "frames_sha256": sha256(frames_bytes).hexdigest(),
        "dataset": "LongVideoBench", "features": "BLIP", "stride": 1, "t1": 0.8, "t2": -100,
        "max_depth": 5, "budgets": {},
    }
    for budget in (32, 64):
        folder = output / f"k{budget}"
        folder.mkdir()
        author = folder / "author"
        author.mkdir()
        command = [sys.executable, str(inputs / "frame_select.py"), "--score_path", str(inputs / "scores.json"),
                   "--frame_path", str(inputs / "frames.json"), "--output_file", str(author),
                   "--max_num_frames", str(budget)]
        save(folder / "command.json", command)
        with (folder / "author.log").open("w", encoding="utf-8") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        oracle = json.loads((author / "longvideobench/blip/selected_frames.json").read_text(encoding="utf-8"))
        ours = [select_aks(ids, row, budget) for ids, row in zip(frames, scores)]
        save(folder / "ours.json", ours, compact=True)
        results = {"author_rerun": compare(ours, oracle), "author_archive": compare(ours, published[budget])}
        summary["budgets"][budget] = results
        print(f'k={budget}: rerun {results["author_rerun"]["exact"]}/{len(ours)} exact; '
              f'archived output {results["author_archive"]["exact"]}/{len(ours)} exact')
    save(output / "summary.json", summary)
    if any(row["author_rerun"]["different_0based"] for row in summary["budgets"].values()):
        raise RuntimeError("AKS parity failed. See corresponding frame lists and summary.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    verify(args.archive.resolve(), args.output.resolve())
