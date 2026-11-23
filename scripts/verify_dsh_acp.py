"""Test two temporary DSH ACP sessions against an explicitly configured local model."""

import argparse
import base64
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
from time import monotonic


class Acp:
    def __init__(self, command, cwd, env, log):
        self.events = Queue()
        self.id = 0
        self.updates = []
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=log, text=True, encoding="utf-8", env=env)

        def read():
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    continue
            self.events.put(None)

        Thread(target=read, daemon=True).start()

    def call(self, method, params, *, expect_error=False):
        self.id += 1
        request = dict(jsonrpc="2.0", id=self.id, method=method, params=params)
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        until = monotonic() + 90
        while monotonic() < until:
            try:
                message = self.events.get(timeout=max(0, until - monotonic()))
            except Empty:
                break
            if message is None:
                raise RuntimeError(f"DSH ACP exited during {method}")
            if message.get("method") == "session/update":
                self.updates.append(message["params"])
            if message.get("id") == self.id and "method" not in message:
                if ("error" in message) != expect_error:
                    raise RuntimeError(f"{method}: {message.get('error', message)}")
                return message.get("error" if expect_error else "result")
            if "id" in message and "method" in message:
                # Decline every runtime request for interactive permission.
                if message["method"] == "session/request_permission":
                    reply = dict(jsonrpc="2.0", id=message["id"], result=dict(outcome=dict(outcome="cancelled")))
                else:
                    reply = dict(jsonrpc="2.0", id=message["id"], error=dict(code=-32601, message="Unsupported"))
                self.process.stdin.write(json.dumps(reply) + "\n")
                self.process.stdin.flush()
        raise TimeoutError(method)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=8)


def response(updates, session):
    return "".join(item["update"].get("content", {}).get("text", "") for item in updates
                   if item.get("sessionId") == session
                   and item["update"].get("sessionUpdate") == "agent_message_chunk")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--image", type=Path, help="Optional JPEG from the self-created window fixture")
    parser.add_argument("--eyes-url", help="Optional isolated Eyes service for the MCP image test")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    workspace.mkdir(exist_ok=True)
    home = (output / "dsh-home").resolve()
    home.mkdir(exist_ok=True)
    patch = output / "acp-local.json"
    settings = dict(provider="eyes-local", model="qwen35:latest")
    patch.write_text(json.dumps([
        dict(id="llm-pi-ai", config=dict(providers={"eyes-local": {
            "api": "openai-completions", "baseURL": f"http://127.0.0.1:{args.port}/v1",
            "apiKeyEnv": "EYES_ACP_LOCAL_KEY",
            "retryPolicy": {"mode": "normal", "maxRetries": 0},
            "models": [{"id": "qwen35:latest", "name": "Qwen 3.5 local", "contextWindow": 32768,
                        "maxTokens": 128, "input": ["text", "image"]}],
        }})),
        dict(id="agent-default-model", config=settings),
        dict(id="acp", config=settings),
    ], indent=2), encoding="utf-8")
    # The local Ollama endpoint ignores this placeholder. No production DSH credentials are read.
    env = dict(os.environ, DSH_HOME=str(home), EYES_ACP_LOCAL_KEY="local-ollama",
               DSH_PERMISSION_MODE="read-only")
    command = ["cmd.exe", "/d", "/c", str(args.launcher.resolve()), "--profile", "acp", "--patch", str(patch)]
    params = dict(cwd=str(workspace), mcpServers=[])
    if args.eyes_url:
        if not args.eyes_url.startswith("http://127.0.0.1:") or not os.getenv("LEVIATHAN_EYES_TOKEN"):
            parser.error("--eyes-url requires an isolated loopback service and LEVIATHAN_EYES_TOKEN")
        connector = Path(__file__).resolve().parents[1] / "mcp_connector.py"
        params["mcpServers"].append(dict(name="eyes", command=sys.executable,
                                        args=[str(connector), "--eyes-url", args.eyes_url],
                                        env=[dict(name=key, value=os.environ[key])
                                             for key in ("LEVIATHAN_EYES_TOKEN", "PYTHONPATH") if key in os.environ]))
    init = dict(protocolVersion=1, clientCapabilities={})
    result = {}

    def save():
        (output / "acp-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with (output / "acp.log").open("w", encoding="utf-8") as log:
        api = Acp(command, workspace, env, log)
        try:
            result["initialize"] = api.call("initialize", init)
            a = api.call("session/new", params)["sessionId"]
            b = api.call("session/new", params)["sessionId"]
            assert a != b
            result.update(session_a=a, session_b=b)
            for session in (a, b):
                api.call("session/close", dict(sessionId=session))
            listing = api.call("session/list", {})
            result["listed_a_and_b"] = {item["sessionId"] for item in listing["sessions"]} >= {a, b}
            for session in (a, b):
                api.call("session/resume", dict(params, sessionId=session))
            api.updates.clear()
            result["prompt"] = api.call("session/prompt", dict(sessionId=a, prompt=[
                dict(type="text", text="Reply with the token EYES-SESSION-A. Do not use any tools."),
            ]))
            result["answer_a"] = response(api.updates, a)
            result["b_update_count"] = sum(item["sessionId"] == b for item in api.updates)
            assert "EYES-SESSION-A" in result["answer_a"] and result["b_update_count"] == 0
            if args.image and result["initialize"]["agentCapabilities"]["promptCapabilities"]["image"]:
                api.updates.clear()
                result["image_prompt"] = api.call("session/prompt", dict(sessionId=a, prompt=[
                    dict(type="image", mimeType="image/jpeg", data=base64.b64encode(args.image.read_bytes()).decode()),
                    dict(type="text", text="What is the dominant color in this image? Say one color. Do not use tools."),
                ]))
                result["image_answer_a"] = response(api.updates, a)
                result["image_b_update_count"] = sum(item["sessionId"] == b for item in api.updates)
            if args.eyes_url:
                api.updates.clear()
                result["mcp_prompt"] = api.call("session/prompt", dict(sessionId=a, prompt=[dict(
                    type="text", text="Call the Eyes MCP eyes_look tool with source screen and include_image true. "
                    "It is configured for a test fixture. Tell me the dominant color of the new tool result. "
                    "The screen color can be different from earlier messages.")]))
                result["mcp_answer_a"] = response(api.updates, a)
                result["mcp_a_tool_updates"] = [item["update"] for item in api.updates
                                                if item["sessionId"] == a and
                                                item["update"].get("sessionUpdate") in ("tool_call", "tool_call_update")]
                result["mcp_b_update_count"] = sum(item["sessionId"] == b for item in api.updates)
            api.call("session/close", dict(sessionId=a))
            api.call("session/close", dict(sessionId=b))
        finally:
            save()
            api.close()
        api = Acp(command, workspace, env, log)
        try:
            api.call("initialize", init)
            api.call("session/resume", dict(params, sessionId=a))
            result["reconnected_a"] = True
            stale = api.call("session/resume", dict(params, sessionId="session-eyes-missing"), expect_error=True)
            result["missing_session_error"] = stale["code"]
        finally:
            save()
            api.close()
    print("DSH ACP session isolation and reconnect verified")


if __name__ == "__main__":
    main()
