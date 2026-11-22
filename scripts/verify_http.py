"""Exercise the local HTTP service and retain evidence for the course report."""

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image

local_http = build_opener(ProxyHandler({}))


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def request(base, route, body=None, expected=200):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(base + route, data=data, headers={"Content-Type": "application/json"})
    try:
        response = local_http.open(req, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        status = response.code
        content_type = response.headers.get_content_type()
        payload = response.read()
    if status != expected:
        raise RuntimeError(f"{route}: expected HTTP {expected}, got {status}: {payload[:300]!r}")
    return json.loads(payload) if content_type == "application/json" else payload


def verify(base, output):
    info = request(base, "/v1/videos/current")
    if (info["width"], info["height"], info["duration_ms"]) != (320, 180, 4000):
        raise RuntimeError(f"Unexpected video metadata: {info}")
    save_json(output / "video.json", info)
    route = f'/v1/videos/{info["video_id"]}/observations'
    checks = []
    for label, start, end, times in (
        ("main", 1000, 3000, [1200, 1700, 2200, 2700]),
        ("short", 0, 300, [0, 100, 200]),
        ("empty", 3950, 4000, []),
    ):
        body = {"start_ms": start, "end_ms": end, "k": 4, "method": "uniform"}
        result = request(base, route, body, expected=201)
        folder = output / "http" / label
        folder.mkdir(parents=True)
        save_json(folder / "request.json", body)
        save_json(folder / "response.json", result)
        actual = [frame["t_ms"] for frame in result["frames"]]
        if actual != times or result["shortfall"] != 4 - len(times):
            raise RuntimeError(f"{label}: unexpected timestamps or shortfall: {result}")
        if bool(result["sheet_url"]) != bool(times):
            raise RuntimeError(f"{label}: unexpected sheet availability")
        images = []
        urls = [frame["image_url"] for frame in result["frames"]]
        if result["sheet_url"]:
            urls.append(result["sheet_url"])
        for url in urls:
            payload = request(base, url)
            name = url.rsplit("/", 1)[-1]
            (folder / name).write_bytes(payload)
            with Image.open(BytesIO(payload)) as image:
                size = list(image.size)
                image.verify()
            expected_size = [320 * len(times), 210] if name == "sheet.png" else [320, 180]
            if size != expected_size:
                raise RuntimeError(f"{name}: expected size {expected_size}, got {size}")
            images.append({"file": name, "size": size, "sha256": hashlib.sha256(payload).hexdigest()})
        checks.append({"case": label, "timestamps_ms": actual, "images": images})
    errors = [
        ("invalid_interval", route, {"start_ms": 100, "end_ms": 100}, 422),
        ("unknown_video", "/v1/videos/unknown/observations", {}, 404),
        ("unknown_image", "/v1/observations/" + "0" * 32 + "/sheet.png", None, 404),
    ]
    for label, error_route, body, status in errors:
        response = request(base, error_route, body, expected=status)
        checks.append({"case": label, "status": status, "response": response})
    save_json(output / "verification.json", checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="A new directory for all verification outputs")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1]
    clip = output / "取帧测试 Luo's clip.mp4"
    fixture_command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                       "testsrc2=duration=4:size=320x180:rate=10", "-c:v", "mpeg4", str(clip)]
    command = [sys.executable, str(repo / "serve.py"), str(clip), "--port", "0",
               "--output", str(output / "observations")]
    save_json(output / "commands.json", {"fixture": fixture_command, "server": command})
    subprocess.run(fixture_command, check=True)
    with (output / "server.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True) as server:
            try:
                base = server.stdout.readline().strip()
                if not base.startswith("http://127.0.0.1:"):
                    raise RuntimeError("HTTP service did not start. See server.log")
                verify(base, output)
            finally:
                if server.poll() is None:
                    server.terminate()
    print(f"HTTP verification passed. Evidence: {output}")


if __name__ == "__main__":
    main()
