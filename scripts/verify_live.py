"""Verify the Eyes v1 contract, MCP image transport and Windows target identity."""

import argparse
import asyncio
import ctypes
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from threading import Thread
from time import monotonic, sleep
import tkinter as tk
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from capture import CaptureError, WindowsCapture, utc_now
from service import Eyes, load_token, make_server

TOKEN = "a" * 48
HTTP = build_opener(ProxyHandler({}))


class FixtureCapture:
    sources = ["screen", "window", "camera"]

    def __init__(self):
        self.color = "red"
        self.private = False
        self.count = 0

    def windows(self):
        info = self.target("window")
        return [dict(window_id=info["id"], title=info["title"], app=info["app"], pid=1,
                     display_id="primary", bounds=info["region"], visible=True)]

    def target(self, kind, wid=None):
        bounds = dict(x=0, y=0, w=200, h=100)
        if kind == "screen":
            return dict(kind=kind, id="primary", display_id="primary", region=bounds)
        return dict(kind=kind, id="hwnd:0x1", title="Password" if self.private else "Fixture",
                    app="python.exe", pid=1, display_id="primary", region=bounds)

    def capture(self, _target):
        self.count += 1
        return Image.new("RGB", (200, 100), self.color), utc_now()


def request(base, route, body=None, *, method=None, status=200, authorized=True, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(headers or {})
    if authorized:
        headers["x-eyes-token"] = TOKEN
    try:
        response = HTTP.open(Request(base + route, data=data, headers=headers, method=method), timeout=8)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read()
        assert response.code == status, (route, status, response.code, payload[:300])
        if response.headers.get_content_type() == "application/json":
            return json.loads(payload)
        return payload


async def verify_mcp(base, packaged=None, *, session_id=None, session_token=None):
    command, entry = (str(packaged), "mcp") if packaged else (sys.executable, str(ROOT / "mcp_connector.py"))
    environment = dict(os.environ, LEVIATHAN_EYES_TOKEN=TOKEN)
    if packaged:
        environment.pop("PYTHONPATH", None)
    arguments = [entry, "--eyes-url", base]
    if session_id is not None:
        arguments.extend(("--session-id", session_id))
        environment["LEVIATHAN_EYES_SESSION_TOKEN"] = session_token
    else:
        environment.pop("DSH_SESSION_ID", None)
    params = StdioServerParameters(command=command, args=arguments,
                                   env=environment)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == {
                "current_video", "observe_video", "get_frame", "eyes_status", "eyes_windows", "eyes_look",
                "eyes_watch_start", "eyes_watch_stop", "eyes_events", "eyes_get_observation", "eyes_get_image",
            }
            response = await client.call_tool("eyes_look", {"source": "screen"})
            assert not response.isError, response.content
            message, image = response.content
            data = json.loads(message.text)
            assert "属于不可信数据" in data["content_warning"] and image.mimeType == "image/jpeg"
            assert data.get("owner_session") == session_id
            return sorted(names)


def contract(output, packaged=None):
    fixture = FixtureCapture()
    eyes = Eyes(fixture, image_ttl=0.5, event_capacity=12)
    token_path = output / "verification.token"
    try:
        value = load_token(token_path)
        assert len(value) == 48 and load_token(token_path) == value
    finally:
        token_path.unlink(missing_ok=True)
    with make_server(0, TOKEN, eyes) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            health = request(base, "/v1/health", authorized=False)
            assert health["contract"] == "leviathan.eyes/v1"
            request(base, "/v1/health", headers={"Host": "evil.example"}, status=403)
            request(base, "/v1/health", headers={"Origin": "https://evil.example"}, status=403)
            request(base, "/v1/look", {"source": "screen"}, authorized=False, status=401)
            request(base, "/v1/look", {"source": "screen", "include_image": 1}, status=400)
            request(base, "/v1/look", {"source": "camera"}, status=503)
            request(base, "/v1/look", {"source": "screen", "extra": "a" * 65536}, status=400)
            request(base, "/v1/look", {"source": "screen", "region": {"x": 199, "y": 0, "w": 2, "h": 2}},
                    status=400)
            live = request(base, "/v1/look", {"source": "screen", "extra": True})
            assert live["schema"] == "leviathan.eyes.observation/v1"
            image_path = live["image"]["url"]
            assert Image.open(BytesIO(request(base, image_path))).size == (200, 100)
            assert request(base, "/v1/look", {"source": "screen", "max_age_ms": 1000}) == live
            fixture.private = True
            private = request(base, "/v1/look", {"source": "screen", "max_age_ms": 1000})
            assert private["privacy"]["sensitive"] and not private["text"] and "image" not in private
            assert not request(base, "/v1/windows")["windows"]
            fixture.private = False
            sleep(0.55)
            request(base, image_path, status=404)
            assert "image" not in request(base, image_path.rsplit("/", 1)[0])
            assert request(base, "/v1/events?wait=20")["events"] == []
            watch = request(base, "/v1/watch", {"source": "screen", "fps": 2, "ttl_s": 4})["watch_id"]
            batch = request(base, "/v1/events?since=0&wait=1000")
            while not any(event["type"] == "vision.observation" for event in batch["events"]):
                batch = request(base, f"/v1/events?since={batch['cursor']}&wait=1200")
                assert batch["events"], "No first observation"
            observed = next(event["observation"] for event in batch["events"]
                            if event["type"] == "vision.observation")
            assert "image" not in observed and request(base, f"/v1/observations/{observed['observation_id']}/image")
            unchanged = request(base, f"/v1/events?since={batch['cursor']}&wait=600")
            assert not unchanged["events"]
            fixture.color = "blue"
            changed = request(base, f"/v1/events?since={batch['cursor']}&wait=1500")
            assert {event["type"] for event in changed["events"]} >= {"vision.observation", "vision.scene_changed"}
            request(base, f"/v1/watch/{watch}", method="DELETE")
            request(base, f"/v1/watch/{watch}", method="DELETE", status=409)
            watch_body = {"source": "screen", "ttl_s": 20}
            for _ in range(2):
                request(base, "/v1/watch", watch_body)
                request(base, "/v1/watch", watch_body, headers={"x-eyes-client": "second"})
            request(base, "/v1/watch", watch_body, status=429)
            request(base, "/v1/watch", watch_body, headers={"x-eyes-client": "third"}, status=429)
            request(base, "/v1/pause", {"ttl_s": 1})
            request(base, "/v1/look", {"source": "screen"}, status=403)
            request(base, "/v1/watch", watch_body, status=403)
            cursor = request(base, "/v1/events")["cursor"]
            assert not request(base, f"/v1/events?since={cursor}&wait=600")["events"]
            dropped = request(base, "/v1/events")["events"][0]
            assert dropped["type"] == "vision.dropped" and dropped["dropped_count"] > 0
            sleep(0.5)
            assert not request(base, "/v1/health", authorized=False)["paused"]
            expiring = request(base, "/v1/watch", {"source": "screen", "ttl_s": 1})["watch_id"]
            sleep(1.1)
            assert any(event.get("reason") == "expired" and event.get("watch_id") == expiring
                       for event in request(base, "/v1/events")["events"])
            return dict(health=health, observation=live, private=private, dropped=dropped,
                        mcp_tools=asyncio.run(verify_mcp(base, packaged)))
        finally:
            eyes.pause()
            server.shutdown()
            thread.join()


def desktop_question(output, port):
    from desktop import Desktop

    root = tk.Tk()
    desktop = Desktop(root, output, 0, ollama_model="qwen35:latest", ollama_port=port)
    eyes = Eyes(FixtureCapture())
    with make_server(0, TOKEN, eyes) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["LEVIATHAN_EYES_URL"] = f"http://127.0.0.1:{server.server_port}"
        os.environ["LEVIATHAN_EYES_TOKEN"] = TOKEN

        def wait_for(predicate, seconds=8):
            end = monotonic() + seconds
            while monotonic() < end:
                root.update()
                if predicate():
                    return
                sleep(0.04)
            raise AssertionError("Desktop question did not complete: " + desktop.vision_panel.chat_status.get())

        try:
            desktop.open_vision()
            panel = desktop.vision_panel
            wait_for(lambda: not panel.control_pending)
            panel.start()
            wait_for(lambda: panel.current is not None)
            panel.source.set("正在观察的窗口")
            prompt = "What is the dominant color? Reply with one English color word."
            panel.question.set(prompt)
            panel.ollama_port = 1
            panel.ask()
            wait_for(lambda: "可重试" in panel.chat_status.get())
            assert panel.question.get() == prompt
            panel.ollama_port = port
            panel.ask()
            wait_for(lambda: bool(panel.evidence), seconds=88)
            answer = panel.answers.get("1.0", "end")
            assert "red" in answer.lower() and panel.question.get() == ""
            assert panel.current["observation_id"] == panel.evidence[0]["live_id"]
            panel.show_current()
            wait_for(lambda: panel.bitmap is not None)
            panel.pause()
            wait_for(lambda: panel.bitmap is None and not panel.watch_id)
            return dict(answer=answer, failed_send_preserved_input=True, evidence_preview=True, paused=True)
        finally:
            desktop.close()
            eyes.pause()
            server.shutdown()
            thread.join()


def native(output):
    class NativeFixtureCapture(WindowsCapture):
        allowed = set()

        def target(self, source, window_id=None):
            target = super().target(source, window_id)
            if target["id"] not in self.allowed:
                raise CaptureError("Foreground is outside the two fixture windows")
            return target

    backend = NativeFixtureCapture()
    assert "window" in backend.sources
    root = tk.Tk()
    root.title("Eyes test red")
    root.configure(bg="#e00000")
    root.geometry("350x240+110+100")
    blue = tk.Toplevel(root)
    blue.title("Eyes test blue")
    blue.configure(bg="#0000e0")
    blue.geometry("350x240+535+100")
    root.update()
    red_info = next(window for window in backend.windows() if window["title"] == "Eyes test red")
    blue_info = next(window for window in backend.windows() if window["title"] == "Eyes test blue")
    red_id, blue_id = red_info["window_id"], blue_info["window_id"]
    backend.allowed = {red_id, blue_id}
    eyes = Eyes(backend)

    def focus(widget, wid):
        widget.lift()
        widget.focus_force()
        root.update()
        user32 = ctypes.windll.user32
        foreground = user32.GetForegroundWindow()
        target = int(wid[7:], 16)
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        attached = (current_thread != foreground_thread
                    and user32.AttachThreadInput(current_thread, foreground_thread, True))
        try:
            user32.SetForegroundWindow(target)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        root.update()
        assert user32.GetForegroundWindow() == target, "Could not activate the self-created fixture window"

    def look(name, wid, expected_color):
        meta = eyes.look({"source": "window", "window_id": wid})
        data = eyes.observation(meta["observation_id"], image=True)
        with Image.open(BytesIO(data)) as image:
            center = image.getpixel((image.width // 2, image.height // 2))
            assert center[expected_color] >= 180 and sum(center) - center[expected_color] < 55, (name, center)
        (output / f"{name}.jpg").write_bytes(data)
        return dict(observation=meta, center_rgb=center)

    def wait_for(watch, predicate, timeout=5):
        until = monotonic() + timeout
        while monotonic() < until:
            root.update()
            events = [event for event in eyes.poll(0, 0)["events"] if event.get("watch_id") == watch]
            for event in events:
                if predicate(event):
                    return event
            sleep(0.05)
        raise AssertionError(f"No matching native watch event: {eyes.poll(0, 0)}")

    try:
        focus(root, red_id)
        results = {"red": look("red", red_id, 0), "blue": look("blue", blue_id, 2)}
        follow = eyes.start_watch({"source": "window", "fps": 2}, "test")["watch_id"]
        locked = eyes.start_watch({"source": "window", "fps": 2, "mode": "lock_current"}, "test")["watch_id"]
        for watch in (follow, locked):
            wait_for(watch, lambda event: event.get("observation", {}).get("source", {}).get("id") == red_id)
        blue.geometry("365x245+105+95")
        focus(blue, blue_id)
        results["follow_switch"] = wait_for(
            follow, lambda event: event.get("observation", {}).get("source", {}).get("id") == blue_id)
        results["covered"] = look("red-covered", red_id, 0)
        results["follow_foreground"] = backend.target("window")["id"]
        assert results["follow_foreground"] == blue_id
        root.geometry("480x312+885+135")
        root.update()
        results["resized"] = look("red-resized", red_id, 0)
        assert results["resized"]["observation"]["image"]["width"] > results["red"]["observation"]["image"]["width"]
        root.iconify()
        root.update()
        results["minimized"] = wait_for(locked, lambda event: event["type"] == "vision.error")
        root.deiconify()
        root.update()
        results["restored"] = look("red-restored", red_id, 0)
        results["watch_resumed"] = wait_for(
            locked, lambda event: event["type"] == "vision.observation" and event["id"] > results["minimized"]["id"])
        eyes.stop_watch(follow)
        root.destroy()
        until = monotonic() + 5
        while monotonic() < until:
            events = eyes.poll(0, 0)["events"]
            closed = next((event for event in events if event.get("watch_id") == locked
                           and event.get("reason") == "source_closed"), None)
            if closed:
                results["closed"] = closed
                return results
            sleep(0.05)
        raise AssertionError("Closing the target did not stop the locked watch")
    finally:
        eyes.pause()
        try:
            root.destroy()
        except tk.TclError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--packaged-exe", type=Path)
    parser.add_argument("--ollama-port", type=int)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result = contract(args.output, args.packaged_exe.resolve() if args.packaged_exe else None)
    (args.output / "contract.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Eyes HTTP and MCP contract passed", flush=True)
    if args.native:
        result = native(args.output)
        (args.output / "windows.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Native window capture passed", flush=True)
    if args.ollama_port:
        result = desktop_question(args.output, args.ollama_port)
        (args.output / "desktop-question.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                            encoding="utf-8")
        print("Desktop question, retry and evidence preview passed", flush=True)


if __name__ == "__main__":
    main()
