"""Serve timestamped video observations over a local HTTP interface."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from math import ceil
from pathlib import Path
import re
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from app import extract, frame_times, run
from sampling import METHODS


def make_sheet(folder, frames):
    columns = min(4, len(frames))
    rows = ceil(len(frames) / columns)
    width, height = 320, 210
    sheet = Image.new("RGB", (width * columns, height * rows), "#17212b")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=17)
    for index, frame in enumerate(frames):
        x, y = (index % columns) * width, (index // columns) * height
        with Image.open(folder / frame["image"]) as image:
            image.thumbnail((width - 8, height - 28))
            sheet.paste(image, (x + (width - image.width) // 2, y + (height - 24 - image.height) // 2))
        draw.text((x + 8, y + height - 23),
                  f'{frame["frame_id"]}  |  {frame["t_ms"] / 1000:.3f} s',
                  fill="white", font=font)
    sheet.save(folder / "sheet.png")


class VideoSession:
    def __init__(self, video, output):
        self.video = video
        self.output = output
        self.frames = frame_times(video)
        probe = json.loads(run("ffprobe", "-v", "error", "-select_streams", "V:0",
                               "-show_entries", "stream=width,height,duration", "-of", "json", str(video)))
        stream = probe["streams"][0]
        duration = stream.get("duration", "N/A")
        duration_ms = round(float(duration) * 1000) if duration != "N/A" else 0
        self.info = {
            "video_id": uuid4().hex, "width": stream["width"], "height": stream["height"],
            "duration_ms": max(duration_ms, self.frames[-1][0] + 1),
        }
        self.observations = {}

    def observe(self, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        start = body.get("start_ms", 0)
        end = body.get("end_ms", self.info["duration_ms"])
        count = body.get("k", 4)
        if any(type(value) is not int for value in (start, end, count)):
            raise ValueError("start_ms, end_ms and k must be integers")
        if not 0 <= start < end <= self.info["duration_ms"] or not 1 <= count <= 128:
            raise ValueError("Interval must be within the video and k must be between 1 and 128")
        method = body.get("method", "uniform")
        if method not in METHODS:
            raise ValueError(f"Method must be one of: {', '.join(METHODS)}")
        observation = uuid4().hex
        folder = self.output / observation
        extract(self.video, folder, start, end, count, frames=self.frames, method=method)
        data = json.loads((folder / "observation.json").read_text(encoding="utf-8"))
        frames = data["frames"]
        base = f"/v1/observations/{observation}"
        allowed = {frame["image"] for frame in frames}
        if frames:
            make_sheet(folder, frames)
            allowed.add("sheet.png")
        for frame in frames:
            frame["image_url"] = f'{base}/{frame.pop("image")}'
        data.update(observation_id=observation, video_id=self.info["video_id"],
                    sheet_url=f"{base}/sheet.png" if frames else None)
        (folder / "response.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.observations[observation] = allowed
        return data


class ObservationHandler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type="application/json"):
        if content_type == "application/json":
            body = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        session = self.server.session
        if session is not None and self.path == "/v1/videos/current":
            self.reply(200, session.info)
            return
        match = re.fullmatch(r"/v1/observations/([0-9a-f]{32})/(sheet\.png|frame-[0-9]{3}\.png)", self.path)
        if session is not None and match:
            observation, name = match.groups()
            if name in session.observations.get(observation, ()):
                try:
                    data = (session.output / observation / name).read_bytes()
                except FileNotFoundError:
                    pass
                else:
                    self.reply(200, data, "image/png")
                    return
        self.reply(404, {"error": "Resource not found"})

    def do_POST(self):
        session = self.server.session
        if session is None or self.path != f'/v1/videos/{session.info["video_id"]}/observations':
            self.reply(404, {"error": "Video or route not found"})
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_384:
                raise ValueError("Expected a JSON body of at most 16384 bytes")
            body = json.loads(self.rfile.read(length))
        except ValueError as error:
            self.reply(422, {"error": str(error)})
            return
        try:
            data = session.observe(body)
        except (ValueError, OSError) as error:
            self.reply(422, {"error": str(error)})
            return
        self.reply(201, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        session = VideoSession(args.video.resolve(), args.output.resolve())
        with HTTPServer(("127.0.0.1", args.port), ObservationHandler) as server:
            server.session = session
            print(f"http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
