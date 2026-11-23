"""HTTP client for the local Eyes live service."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from session_scope import token_path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class EyesClient:
    def __init__(self, base=None, token=None, *, session_id=None):
        self.base = (base or os.getenv("LEVIATHAN_EYES_URL") or "http://127.0.0.1:8769").rstrip("/")
        parsed = urlsplit(self.base)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username is not None or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Eyes URL must be a local HTTP origin")
        self.token = token
        self.session_id = session_id
        self.session_path = token_path(session_id) if session_id is not None else None
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, *, body=None, method=None, public=False, timeout=5):
        if not path.startswith("/v1/") or urlsplit(path).netloc:
            raise ValueError("Expected an Eyes API path")
        headers = {}
        if not public:
            if self.session_path:
                headers["x-eyes-session"] = self.session_id
                token = self.token or os.getenv("LEVIATHAN_EYES_SESSION_TOKEN")
                if not token:
                    token = self.session_path.read_text(encoding="ascii").strip()
            else:
                token = self.token or os.getenv("LEVIATHAN_EYES_TOKEN")
                if not token:
                    token = (Path.home() / ".leviathan" / "eyes" / "token").read_text(encoding="ascii").strip()
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.http.open(request, timeout=timeout) as response:
                payload = response.read()
                if response.headers.get_content_type().startswith("image/"):
                    return response.headers.get_content_type(), payload
                return json.loads(payload)
        except HTTPError as error:
            with error:
                try:
                    message = json.load(error)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    message = f"HTTP {error.code}"
            raise OSError(f"Eyes ({error.code}) {message}") from error
        except URLError as error:
            raise OSError(f"Eyes live service unavailable ({self.base})") from error

    def events(self, since=0, wait_ms=0):
        query = urlencode({"since": since, "wait": wait_ms})
        return self.request(f"/v1/events?{query}", timeout=max(5, wait_ms / 1000 + 5))

    def observation(self, observation_id, *, image=False):
        if "/" in observation_id or "?" in observation_id or "#" in observation_id:
            raise ValueError("Invalid observation ID")
        suffix = "/image" if image else ""
        return self.request(f"/v1/observations/{observation_id}{suffix}")
