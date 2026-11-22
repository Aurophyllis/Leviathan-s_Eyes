"""Client shared by the desktop preview and the local MCP connector."""

import json
import re
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


class VideoClient:
    def __init__(self, port):
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        self.base = f"http://127.0.0.1:{port}"
        self.opener = build_opener(ProxyHandler({}))

    def _request(self, path, body=None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.base + path, data=payload, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=120) as response:
                content_type = response.headers.get_content_type()
                data = response.read()
        except HTTPError as error:
            with error:
                data = error.read()
                if error.headers.get_content_type() == "application/json":
                    raise ValueError(json.loads(data)["error"]) from error
            raise
        return json.loads(data) if content_type == "application/json" else data

    def current(self):
        return self._request("/v1/videos/current")

    def observe(self, video_id, start_ms, end_ms, k):
        if not re.fullmatch(r"[0-9a-f]{32}", video_id):
            raise ValueError("Invalid video ID")
        body = {"start_ms": start_ms, "k": k, "method": "uniform"}
        if end_ms is not None:
            body["end_ms"] = end_ms
        return self._request(f"/v1/videos/{video_id}/observations", body)

    def image(self, path):
        if not re.fullmatch(r"/v1/observations/[0-9a-f]{32}/(sheet\.png|frame-[0-9]{3}\.png)", path):
            raise ValueError("Invalid observation image path")
        return self._request(path)

    def frame(self, observation_id, frame_id):
        return self.image(f"/v1/observations/{observation_id}/frame-{frame_id:03d}.png")
