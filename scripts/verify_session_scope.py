"""Verify private observations and, optionally, exact routing on an isolated DSH Web Host."""

import argparse
import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
from queue import Queue
import re
import subprocess
from threading import Thread
from uuid import uuid4

from PIL import Image

from verify_live import Eyes, FixtureCapture, TOKEN, make_server, request, verify_mcp
from dsh_web import DshError, DshWeb
from session_scope import token_path


class FixtureModel(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        images = [part["image_url"]["url"] for msg in body["messages"]
                  for part in (msg["content"] if isinstance(msg.get("content"), list) else [])
                  if part.get("type") == "image_url"]
        color = "No image"
        if images:
            with Image.open(BytesIO(base64.b64decode(images[-1].split(",", 1)[1]))) as image:
                red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
                color = "Red" if red > 200 and max(green, blue) < 50 else "Unexpected image"
        chunk = dict(id="fixture", object="chat.completion.chunk", created=0, model=body["model"])
        chunks = [
            dict(chunk, choices=[dict(index=0, delta=dict(role="assistant", content=color), finish_reason=None)]),
            dict(chunk, choices=[dict(index=0, delta={}, finish_reason="stop")]),
        ]
        data = ("".join(f"data: {json.dumps(item)}\n\n" for item in chunks) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def verify_scope(output, a, b):
    session_dir = output / "sessions"
    session_dir.mkdir(exist_ok=True)
    a_token, b_token = "b" * 48, "c" * 48
    # Disposable fixture credentials. Production uses service --register-session.
    token_path(a, session_dir).write_text(a_token, encoding="ascii")
    token_path(b, session_dir).write_text(b_token, encoding="ascii")
    backend = FixtureCapture()
    eyes = Eyes(backend)
    with make_server(0, TOKEN, eyes, session_dir) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        a_auth = {"x-eyes-session": a, "x-eyes-token": a_token}
        b_auth = {"x-eyes-session": b, "x-eyes-token": b_token}
        body = {"source": "screen", "max_age_ms": 5000}
        try:
            first = request(base, "/v1/look", body, authorized=False, headers=a_auth)
            second = request(base, "/v1/look", body, authorized=False, headers=b_auth)
            assert first["owner_session"] == a and second["owner_session"] == b
            assert first["observation_id"] != second["observation_id"]
            image = first["image"]["url"]
            metadata = image.rsplit("/", 1)[0]
            request(base, metadata, authorized=False, headers=b_auth, status=404)
            request(base, image, authorized=False, headers=b_auth, status=404)
            request(base, image, status=404)
            request(base, "/v1/look", body, headers={"x-eyes-session": a}, status=401)
            request(base, "/v1/look", body, authorized=False,
                    headers={"x-eyes-session": str(uuid4()), "x-eyes-token": a_token}, status=404)
            request(base, "/v1/pause", {}, authorized=False, headers=a_auth, status=403)
            watch = request(base, "/v1/watch", body, authorized=False, headers=a_auth)
            assert watch["owner_session"] == a
            request(base, "/v1/watch/" + watch["watch_id"], method="DELETE",
                    authorized=False, headers=b_auth, status=404)
            assert request(base, "/v1/events?wait=100", authorized=False, headers=b_auth)["events"] == []
            assert request(base, "/v1/events", authorized=False, headers=a_auth)["events"]
            pixels = request(base, image, authorized=False, headers=a_auth)
            asyncio.run(verify_mcp(base, session_id=a, session_token=a_token))
            (output / "red.jpg").write_bytes(pixels)
            token_path(a, session_dir).unlink()
            request(base, image, authorized=False, headers=a_auth, status=404)
            return first
        finally:
            eyes.pause()
            server.shutdown()
            thread.join()


def check_web(output, cli, port):
    workspace = output / "fixture"
    workspace.mkdir(exist_ok=True)
    settings = dict(provider="eyes-local", model="qwen35:latest")
    patch = output / "web-local.json"
    patch.write_text(json.dumps([
        dict(id="llm-pi-ai", config=dict(providers={"eyes-local": {
            "api": "openai-completions", "baseURL": f"http://127.0.0.1:{port}/v1",
            "apiKeyEnv": "EYES_TEST_KEY", "retryPolicy": {"mode": "normal", "maxRetries": 0},
            "models": [{"id": "qwen35:latest", "contextWindow": 32768, "maxTokens": 512,
                        "input": ["text", "image"]}],
        }})), dict(id="agent-default-model", config=settings),
        dict(id="session-title-llm", disabled=True),
    ], indent=2), encoding="utf-8")
    proc = subprocess.Popen(["node", str(cli.resolve()), "--profile", "web", "--patch", str(patch),
                             "--no-open", "--port", "0", "--host", "127.0.0.1"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=workspace, text=True, encoding="utf-8", errors="replace",
                            env=dict(os.environ, DSH_HOME=str(output / "dsh-home"),
                                     DSH_PERMISSION_MODE="read-only", EYES_TEST_KEY="localhost"))
    launch = Queue()

    def read():
        for line in proc.stdout:
            found = re.search(r"http://127\.0\.0\.1:\d+/\?token=[^\s\x1b]+", line)
            if found:
                launch.put(found[0])
        launch.put(None)

    Thread(target=read, daemon=True).start()
    try:
        launch_url = launch.get(timeout=35)
        if launch_url is None:
            raise RuntimeError("Isolated DSH Web startup failed")
        web = DshWeb(launch_url)
        web.connect()
        a, b = [web.rpc("session/create", {"request": {"cwd": str(workspace)}})["sessionId"]
                for _ in range(2)]
        assert a != b
        baseline_b = web.session(b)["asOfSeq"]
        first = verify_scope(output, a, b)
        delivery = web.prepare(a)
        web.send(delivery, "What is the dominant color? Answer with one English color word. Do not call tools.",
                 first["observation_id"], "image/jpeg", (output / "red.jpg").read_bytes())
        answer = web.wait_reply(delivery, timeout=180)
        normalized = answer.strip(" .。!！").casefold()
        assert normalized in {"red", "红色", "红"}, answer
        assert web.session(b)["asOfSeq"] == baseline_b
        reconnect = DshWeb(launch_url)
        reconnect.connect()
        assert reconnect.prepare(a).session_id == a
        try:
            reconnect.prepare("session-" + str(uuid4()))
        except DshError as error:
            assert error.code == 404, error
        else:
            raise AssertionError("Missing DSH session accepted")
        return dict(session_a=a, session_b=b, answer_a=answer, used_requested_english=normalized == "red",
                    request_id=delivery.request_id,
                    observation_id=first["observation_id"], b_unchanged=True, reconnected_a=True,
                    missing_session_status=404)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dsh-cli-js", type=Path)
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument("--fixture-model", action="store_true", help="Use a deterministic local image-checking endpoint")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = {"session_scope": True}
    if args.dsh_cli_js and args.fixture_model:
        with ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel) as model:
            thread = Thread(target=model.serve_forever, daemon=True)
            thread.start()
            try:
                result["dsh_web"] = check_web(output, args.dsh_cli_js, model.server_port)
                result["dsh_web"]["model"] = "fixture endpoint validating the received JPEG pixels"
            finally:
                model.shutdown()
                thread.join()
    elif args.dsh_cli_js:
        result["dsh_web"] = check_web(output, args.dsh_cli_js, args.ollama_port)
        result["dsh_web"]["model"] = "local Ollama qwen35:latest"
    else:
        verify_scope(output, str(uuid4()), str(uuid4()))
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Eyes session scope verified", flush=True)


if __name__ == "__main__":
    main()
