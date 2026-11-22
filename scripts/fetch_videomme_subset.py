"""Fetch selected Video-MME members with HTTPS byte ranges and join KFS-Bench evidence."""

import argparse
import csv
from hashlib import file_digest
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from urllib.request import Request, urlopen
from zipfile import ZipFile

import pyarrow.parquet as pq


REVISION = "ead1408f75b618502df9a1d8e0950166bf0a2a0b"
BASE = f"https://huggingface.co/datasets/lmms-eval/Video-MME/resolve/{REVISION}/"
ANNOTATIONS = BASE + "videomme/test-00000-of-00001.parquet"


class RemoteZip:
    """Seekable reader that refuses a server response without a matching range."""

    def __init__(self, url):
        self.url = url
        self.offset = 0
        self.transferred = 0
        self.begin, self.size, self.cache = self.range("-65536")

    def range(self, span):
        request = Request(self.url, headers={"Range": "bytes=" + span, "User-Agent": "LeviathanEyesResearch/1"})
        with urlopen(request, timeout=90) as response:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if response.status != 206 or not match:
                raise ValueError("Video-MME download server did not honor a byte range")
            left, right, total = map(int, match.groups())
            expected = (max(0, total - int(span[1:])), total - 1) if span.startswith("-") else tuple(
                map(int, span.split("-")))
            if (left, right) != expected:
                raise ValueError("Returned video archive range does not match the request")
            data = response.read(right - left + 2)
        if len(data) != right - left + 1:
            raise ValueError("Unexpected archive range length")
        self.transferred += len(data)
        return left, total, data

    def seek(self, offset, whence=0):
        position = {0: offset, 1: self.offset + offset, 2: self.size + offset}[whence]
        if position < 0:
            raise OSError("Negative seek")
        self.offset = position
        return self.offset

    def tell(self):
        return self.offset

    def seekable(self):
        return True

    def read(self, size=-1):
        remaining = max(0, self.size - self.offset)
        size = remaining if size < 0 else min(remaining, size)
        if not size:
            return b""
        start, stop = self.offset, self.offset + size
        if start < self.begin or stop > self.begin + len(self.cache):
            right = min(self.size - 1, start + max(size, 2**22) - 1)
            self.begin, total, self.cache = self.range(f"{start}-{right}")
            if total != self.size:
                raise ValueError("Archive size changed during download")
        self.offset = stop
        return self.cache[start - self.begin:stop - self.begin]


def timestamp_ms(value):
    seconds = 0
    for unit in value.split(":"):
        seconds = seconds * 60 + float(unit)
    return round(seconds * 1000)


def annotations(parquet, kfs_archive):
    with ZipFile(kfs_archive) as archive:
        raw = archive.read("KFS-Bench-main/vidmme_anno.csv")
    spans = {}
    for row in csv.DictReader(raw.decode("utf-8-sig").splitlines()):
        start, end = timestamp_ms(row["start"]), timestamp_ms(row["end"])
        if 0 <= start < end:
            spans.setdefault(row["id"], []).append({"scene_id": row["scene_id"],
                                                     "start_ms": start, "end_ms": end})
    by_video = {}
    for row in pq.read_table(parquet).to_pylist():
        if row["question_id"] in spans:
            by_video.setdefault(row["videoID"], []).append({
                "id": row["question_id"], "duration_group": row["duration"],
                "task_type": row["task_type"], "question": row["question"],
                "source_video_url": row["url"],
                "options": [re.sub(r"^[A-E][.)]\s*", "", item) for item in row["options"]],
                "answer": row["answer"], "evidence": spans[row["question_id"]],
            })
    return by_video, sha256_bytes(raw)


def sha256_bytes(data):
    from hashlib import sha256

    return sha256(data).hexdigest()


def hash_file(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def fetch(parquet, kfs, output, chunks, video_limit, max_mib, groups):
    by_video, kfs_hash = annotations(parquet, kfs)
    output.mkdir(parents=True, exist_ok=False)
    videos = []
    used = set()
    for chunk in chunks:
        name = f"videos_chunked_{chunk:02d}.zip"
        remote = RemoteZip(BASE + name)
        with ZipFile(remote) as archive:
            candidates = sorted(archive.infolist(), key=lambda member: (member.file_size, member.filename))
            for member in candidates:
                path = PurePosixPath(member.filename)
                video_id = path.stem
                if len(videos) == video_limit:
                    break
                if (path.suffix.lower() != ".mp4" or video_id in used or video_id not in by_video
                        or member.file_size > max_mib * 2**20 or not re.fullmatch(r"[A-Za-z0-9_-]+", video_id)):
                    continue
                if by_video[video_id][0]["duration_group"] not in groups:
                    continue
                # Prefer a multi-scene question when available, then break ties by stable question ID.
                sample = min(by_video[video_id], key=lambda row: (
                    -len({part["scene_id"] for part in row["evidence"]}), row["id"]))
                file = output / f"{video_id}.mp4"
                with archive.open(member) as source, file.open("xb") as target:
                    shutil.copyfileobj(source, target, length=2**20)
                used.add(video_id)
                videos.append({**sample, "file": file.name, "video_id": video_id, "source_archive": name,
                               "member": member.filename, "video_sha256": hash_file(file)})
                print(f'{sample["id"]}: {file.name}, {sample["duration_group"]}, {member.file_size} bytes', flush=True)
        print(f"{name}: transferred {remote.transferred} archive bytes", flush=True)
        if len(videos) == video_limit:
            break
    manifest = {"source_revision": REVISION, "annotations_url": ANNOTATIONS,
                "questions_sha256": hash_file(parquet), "kfs_vidmme_sha256": kfs_hash,
                "chunks": chunks, "max_mib_per_video": max_mib, "duration_groups": groups, "samples": videos}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if len(videos) != video_limit:
        raise RuntimeError(f"Found {len(videos)} eligible videos out of {video_limit} requested")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path)
    parser.add_argument("kfs_archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chunks", type=int, nargs="+", default=[20, 12, 6])
    parser.add_argument("--videos", type=int, default=3)
    parser.add_argument("--max-mib", type=int, default=40)
    parser.add_argument("--groups", choices=("short", "medium", "long"), nargs="+",
                        default=["short", "medium", "long"])
    args = parser.parse_args()
    fetch(args.parquet, args.kfs_archive, args.output, args.chunks, args.videos, args.max_mib, args.groups)
