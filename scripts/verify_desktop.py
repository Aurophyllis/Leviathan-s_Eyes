"""Verify the Windows preview and MCP image transport; optionally query local Ollama."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
import tkinter as tk
from urllib.request import ProxyHandler, Request, build_opener

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image, ImageDraw, ImageGrab

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from desktop import Desktop


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fixture(output):
    for index, color in enumerate(("#8b242e", "#17653d", "#1946ae", "#916e10"), 1):
        image = Image.new("RGB", (320, 180), color)
        draw = ImageDraw.Draw(image)
        if index == 3:
            draw.ellipse((99, 29, 220, 150), fill="white")
        else:
            draw.rectangle((103, 33, 216, 146), fill="white")
        image.save(output / f"fixture-{index}.png")
    video = output / "形状切换 Luo's sample.mp4"
    command = ["ffmpeg", "-v", "error", "-framerate", "1", "-i", "fixture-%d.png",
               "-c:v", "mpeg4", str(video)]
    subprocess.run(command, cwd=output, check=True)
    save(output / "fixture.json", {"command": command, "expected_frame": 3,
                                   "expected_time_ms": 2000, "expected_color": "blue"})
    return video


def until(root, predicate):
    deadline = monotonic() + 30
    while not predicate():
        root.update()
        if monotonic() > deadline:
            raise RuntimeError("Desktop action did not finish")
        sleep(0.03)


def screenshot(root, target):
    root.update()
    window = int(root.tk.call("wm", "frame", root._w), 0)
    ImageGrab.grab(window=window).save(target)


def ollama(model, prompt, picture):
    body = {
        "model": model, "stream": False, "think": False,
        "options": {"num_ctx": 4096, "temperature": 0, "num_predict": 150},
        "messages": [{"role": "user", "content": prompt, "images": [picture.data]}],
    }
    request = Request("http://127.0.0.1:11434/api/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
        return json.load(response)


async def verify_connector(port, output, model):
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(REPO / "mcp_connector.py"), "--port", str(port)],
                                   env=dict(os.environ))
    with (output / "mcp.log").open("w", encoding="utf-8") as log:
        async with stdio_client(params, errlog=log) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                current = await session.call_tool("current_video", {})
                if current.isError:
                    raise RuntimeError(str(current.content))
                video = current.structuredContent or json.loads(current.content[0].text)
                result = await session.call_tool("observe_video", {"video_id": video["video_id"], "k": 4})
                if result.isError:
                    raise RuntimeError(str(result.content))
                observation = json.loads(next(item.text for item in result.content if item.type == "text"))
                sheet = next(item for item in result.content if item.type == "image")
                if sheet.mimeType != "image/png" or [f["t_ms"] for f in observation["frames"]] != [0, 1000, 2000, 3000]:
                    raise RuntimeError(f"Unexpected MCP observation: {observation}")
                sheet_bytes = base64.b64decode(sheet.data)
                (output / "mcp-sheet.png").write_bytes(sheet_bytes)
                save(output / "mcp-observation.json", observation)
                prompt = "Look at the four numbered panels. Which panel has a white circle instead of a white square? Reply with just the panel number."
                selected = 3
                if model:
                    answer = await asyncio.to_thread(ollama, model, prompt, sheet)
                    save(output / "model-sheet.json", {"prompt": prompt, "response": answer})
                    reply = answer["message"]["content"].strip()
                    if reply.strip(".") in {"1", "2", "3", "4"}:
                        selected = int(reply.strip("."))
                    else:
                        raise RuntimeError(f"Model did not return a usable panel number: {reply!r}")
                result = await session.call_tool("get_frame", {"observation_id": observation["observation_id"],
                                                               "frame_id": selected})
                if result.isError:
                    raise RuntimeError(str(result.content))
                frame = next(item for item in result.content if item.type == "image")
                png = base64.b64decode(frame.data)
                if frame.mimeType != "image/png" or not png.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("MCP get_frame did not return PNG image bytes")
                (output / "mcp-frame.png").write_bytes(png)
                summary = {"tools": [tool.name for tool in tools.tools], "selected_frame": selected,
                           "timestamp_ms": observation["frames"][selected - 1]["t_ms"],
                           "sheet_sha256": hashlib.sha256(sheet_bytes).hexdigest(),
                           "frame_sha256": hashlib.sha256(png).hexdigest()}
                if model:
                    prompt = "What is the background color outside the white shape? Reply with one English color word."
                    answer = await asyncio.to_thread(ollama, model, prompt, frame)
                    save(output / "model-frame.json", {"prompt": prompt, "response": answer})
                    color = answer["message"]["content"].strip()
                    summary.update(model=model, answer=color,
                                   matches_fixture=selected == 3 and color.lower().strip(".") == "blue")
                save(output / "verification.json", summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ollama-model", help="Use an installed vision model via local Ollama")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True)
    video = fixture(output)
    root = tk.Tk()
    desktop = Desktop(root, output / "observations", 0)
    try:
        root.geometry("1080x650+40+40")
        root.lift()
        desktop.load_video(video)
        until(root, lambda: desktop.server.session is not None)
        desktop.observe_button.invoke()
        until(root, lambda: desktop.observation is not None)
        if desktop.frames.size() != 4 or desktop.image is None:
            raise RuntimeError("Desktop did not show the four frames")
        screenshot(root, output / "desktop-sheet.png")
        desktop.frames.selection_set(2)
        desktop.frames.event_generate("<<ListboxSelect>>")
        until(root, lambda: "2.000" in desktop.status.get())
        screenshot(root, output / "desktop-frame.png")
        if (output / "desktop-sheet.png").read_bytes() == (output / "desktop-frame.png").read_bytes():
            raise RuntimeError("Desktop screenshot did not change after selecting a frame")
        asyncio.run(verify_connector(desktop.server.server_port, output, args.ollama_model))
    finally:
        desktop.close()
    print(f"Desktop and MCP verified. Evidence: {output}")


if __name__ == "__main__":
    main()
