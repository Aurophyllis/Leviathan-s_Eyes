"""Loopback HTTP service for Leviathan's Eyes live observations."""

import argparse
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
from threading import BoundedSemaphore, Condition, Event, Thread, Timer
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from PIL import ImageChops, ImageStat

from capture import CaptureError, WindowsCapture, utc_now
from session_scope import CONFIG_DIR, token_path

CONTRACT = "leviathan.eyes/v1"
VERSION = "0.1.0"
IMAGE_TTL = 60
OBSERVATION_TTL = 300
MAX_BODY = 65536
MAX_EVENTS = 50
MAX_WATCHES = 4
MAX_WATCHES_PER_CLIENT = 2
MAX_IMAGE = 1_500_000
DEFAULT_DENY = (
    "1password", "bitwarden", "keepass", "lastpass", "password", "密码", "银行", "支付",
    "网上银行", "banking", "paypal", "alipay", "私密聊天", "private chat", "wechat", "微信",
    "qq.exe", "signal.exe", "telegram", "whatsapp", "windows security", "windows 安全",
    "sechealthui", "credential", "凭据",
)


def identifier(prefix):
    return f"{prefix}_{uuid4().hex}"


def future(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


class ApiError(Exception):
    def __init__(self, status, code, message, retryable=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


def invalid(message):
    return ApiError(400, "invalid_request", message)


def unavailable(message):
    return ApiError(503, "source_unavailable", message, True)


def forbidden(message):
    return ApiError(403, "forbidden", message)


def missing(message):
    return ApiError(404, "not_found", message)


def limited(message):
    return ApiError(429, "rate_limited", message, True)


def number(value, name, low, high, *, integer=False):
    if (type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high
            or (isinstance(value, float) and not math.isfinite(value))):
        raise invalid(f"{name} must be between {low} and {high}")
    return value


def flag(body, name, default):
    value = body.get(name, default)
    if type(value) is not bool:
        raise invalid(f"{name} must be a boolean")
    return value


def window_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"hwnd:0x[0-9a-fA-F]{1,16}", value):
        raise invalid("window_id must be a HWND from /v1/windows")
    return f"hwnd:0x{int(value[7:], 16):X}"


def region(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid("region must be an object")
    return {key: number(value.get(key), "region." + key, minimum, 32768, integer=True)
            for key, minimum in (("x", 0), ("y", 0), ("w", 1), ("h", 1))}


def overlaps(first, second):
    return all(first[key] < second[key] + second[size] and second[key] < first[key] + first[size]
               for key, size in (("x", "w"), ("y", "h")))


def compress(image):
    image.thumbnail((1920, 1920))
    for quality in (85, 70, 50, 30):
        result = BytesIO()
        image.save(result, format="JPEG", quality=quality, optimize=True)
        if result.tell() <= MAX_IMAGE:
            return result.getvalue()
    image.thumbnail((960, 960))
    result = BytesIO()
    image.save(result, format="JPEG", quality=65)
    return result.getvalue()


@dataclass
class Watch:
    id: str
    client: str
    source: str
    windows: dict
    ttl: int
    fps: float
    threshold: float
    include_image: bool
    owner_session: str | None = None
    stop: Event = field(default_factory=Event)


@dataclass
class EventStream:
    events: deque
    cursor: int = 0


class Eyes:
    def __init__(self, backend, config=None, *, image_ttl=IMAGE_TTL, event_capacity=256):
        config = config or {}
        self.backend = backend
        allowed = config.get("allowed_sources", ["screen", "window"])
        if not isinstance(allowed, list) or any(item not in ("screen", "window", "camera") for item in allowed):
            raise ValueError("allowed_sources must list screen, window or camera")
        deny = config.get("denylist", [])
        if not isinstance(deny, list) or any(not isinstance(item, str) or not item.strip() for item in deny):
            raise ValueError("denylist must list non-empty strings")
        self.sources = [source for source in backend.sources if source in allowed]
        self.denylist = DEFAULT_DENY + tuple(value.casefold() for value in deny)
        self.image_ttl = image_ttl
        self.boot_id = identifier("boot")
        self.condition = Condition()
        self.capture_slots = BoundedSemaphore(MAX_WATCHES)
        self.event_slots = BoundedSemaphore(8)
        self.observations = OrderedDict()
        self.latest = {}
        self.event_capacity = event_capacity
        self.streams = {}
        self.watches = {}
        self.finished = deque(maxlen=256)
        self.paused = False
        self.generation = 0
        self.pause_timer = None

    def health(self):
        with self.condition:
            return dict(ok=True, service="leviathan-eyes", version=VERSION, contract=CONTRACT,
                        paused=self.paused, sources=self.sources)

    def capabilities(self):
        return dict(sources=self.sources, features=["look", "watch", "events", "windows", "pause"],
                    limits=dict(image_ttl_s=self.image_ttl, image_max_bytes=MAX_IMAGE,
                                image_max_edge=1920, body_max_bytes=MAX_BODY, max_events=MAX_EVENTS,
                                max_watches=MAX_WATCHES, max_watches_per_client=MAX_WATCHES_PER_CLIENT,
                                max_ttl_s=3600, fps_min=0.1, fps_max=2, event_wait_max_ms=30000))

    def source(self, body):
        source = body.get("source")
        if source not in ("screen", "window", "camera"):
            raise invalid("source must be screen, window or camera")
        if source not in self.sources:
            raise unavailable("Source is disabled or unavailable")
        return source

    def sensitive(self, item):
        content = (item.get("app", "") + " " + item.get("title", "")).casefold()
        return not item.get("app") or any(term in content for term in self.denylist)

    def windows(self):
        with self.condition:
            self.require_active()
            return {"windows": [item for item in self.backend.windows()
                                if item["title"] and not self.sensitive(item)]}

    def require_active(self):
        if self.paused:
            raise forbidden("Vision is paused")
        return self.generation

    def target_sensitive(self, target, crop):
        if target["kind"] == "window":
            return self.sensitive(target)
        if target["kind"] == "camera":
            return False
        bounds = target["region"]
        if crop:
            bounds = dict(crop, x=bounds["x"] + crop["x"], y=bounds["y"] + crop["y"])
        return any(self.sensitive(item) and overlaps(bounds, item["bounds"])
                   for item in self.backend.windows())

    def prune(self):
        now = monotonic()
        for key, row in list(self.observations.items()):
            if row[0] + OBSERVATION_TTL <= now:
                del self.observations[key]
            elif row[2] is not None and row[0] + self.image_ttl <= now:
                meta = dict(row[1])
                meta.pop("image", None)
                self.observations[key] = row[0], meta, None
        self.latest = {key: value for key, value in self.latest.items() if value in self.observations}

    def observation(self, key, image=False, owner_session=None):
        with self.condition:
            self.prune()
            if image:
                self.require_active()
            row = self.observations.get(key)
            if row is None or row[1].get("owner_session") != owner_session or (image and row[2] is None):
                raise missing("Observation or image does not exist or has expired")
            return row[2] if image else row[1]

    def read_frame(self, target, crop):
        if self.target_sensitive(target, crop):
            return None, utc_now(), True
        image, at = self.backend.capture(target)
        refreshed = self.backend.target(target["kind"], target["id"])
        if (target.get("pid"), target.get("region")) != (refreshed.get("pid"), refreshed.get("region")):
            raise CaptureError("Source changed during capture")
        if self.target_sensitive(refreshed, crop):
            return None, at, True
        if crop:
            x, y, w, h = (crop[key] for key in ("x", "y", "w", "h"))
            if x + w > image.width or y + h > image.height:
                raise invalid("region extends beyond the captured source")
            image = image.crop((x, y, x + w, y + h))
        return image, at, False

    def record(self, target, crop, image, at, sensitive, include_image, *, store_image,
               changed=True, owner_session=None):
        key = identifier("obs")
        source = {key: value for key, value in target.items() if key != "pid"}
        if crop:
            bounds = source.get("region", {"x": 0, "y": 0})
            source["region"] = dict(crop, x=bounds["x"] + crop["x"], y=bounds["y"] + crop["y"])
        if sensitive:
            source.pop("title", None)
            source.pop("app", None)
        meta = dict(schema="leviathan.eyes.observation/v1", observation_id=key, captured_at=at,
                    source=source, scene=dict(summary="敏感画面，已跳过" if sensitive else ""),
                    text=[], objects=[], change=dict(is_new_scene=changed, summary=""),
                    privacy=dict(sensitive=sensitive, redacted=["image", "text", "objects"] if sensitive
                                 else [], reason="denylist_or_unknown_window" if sensitive else None))
        if owner_session is not None:
            meta["owner_session"] = owner_session
        data = compress(image) if store_image and image is not None else None
        if data is not None and include_image:
            meta["image"] = dict(url=f"/v1/observations/{key}/image", width=image.width,
                                 height=image.height, expires_at=future(self.image_ttl))
        self.prune()
        self.observations[key] = monotonic(), meta, data
        if len(self.observations) > 64:
            self.observations.popitem(last=False)
        return meta

    def look(self, body, owner_session=None):
        source = self.source(body)
        wid = body.get("window_id")
        wid = window_id(wid) if wid is not None else None
        if source == "screen" and body.get("display_id") not in (None, "primary"):
            raise ApiError(422, "unprocessable", "Only the primary display is available")
        if body.get("detail", "brief") not in ("brief", "normal", "detailed"):
            raise invalid("Unknown detail level")
        if "question" in body and not isinstance(body["question"], str):
            raise invalid("question must be a string")
        crop = region(body.get("region"))
        age = number(body.get("max_age_ms", 0), "max_age_ms", 0, 2147483647, integer=True)
        include = flag(body, "include_image", True)
        with self.condition:
            generation = self.require_active()
        if not self.capture_slots.acquire(blocking=False):
            raise limited("Too many captures in progress")
        try:
            target = self.backend.target(source, wid)
            cache_key = (owner_session, source, target["id"], target.get("pid"), target.get("title"),
                         tuple(target.get("region", {}).values()), tuple(crop.values()) if crop else (),
                         include)
            # Check the current target and privacy policy before even serving a cached frame.
            sensitive = self.target_sensitive(target, crop)
            with self.condition:
                if self.require_active() != generation:
                    raise forbidden("Vision was paused during capture")
                self.prune()
                cached = self.observations.get(self.latest.get(cache_key))
                if (age and not sensitive and cached and not cached[1]["privacy"]["sensitive"]
                        and (not include or cached[2] is not None) and (monotonic() - cached[0]) * 1000 <= age):
                    return cached[1]
            image, at, sensitive = self.read_frame(target, crop) if not sensitive else (None, utc_now(), True)
            with self.condition:
                if self.require_active() != generation:
                    raise forbidden("Vision was paused during capture")
                meta = self.record(target, crop, image, at, sensitive, include, store_image=include,
                                   owner_session=owner_session)
                self.latest[cache_key] = meta["observation_id"]
                return meta
        finally:
            self.capture_slots.release()

    def stream(self, owner_session):
        if owner_session not in self.streams:
            self.streams[owner_session] = EventStream(deque(maxlen=self.event_capacity))
        return self.streams[owner_session]

    def emit(self, kind, watch, **fields):
        stream = self.stream(watch.owner_session)
        stream.cursor += 1
        stream.events.append(dict(id=stream.cursor, type=kind, at=utc_now(), watch_id=watch.id, **fields))
        self.condition.notify_all()

    def poll(self, since, wait, *, owner_session=None):
        if not self.event_slots.acquire(blocking=False):
            raise limited("Too many event polls")
        try:
            with self.condition:
                stream = self.stream(owner_session)
                if since > stream.cursor:
                    since = 0
                self.condition.wait_for(lambda: stream.cursor > since, timeout=wait / 1000)
                events = [event for event in stream.events if event["id"] > since]
                if events:
                    oldest = events[0]["id"]
                    if len(events) > MAX_EVENTS or oldest > since + 1:
                        oldest = max(oldest, events[-1]["id"] - MAX_EVENTS + 2)
                        dropped = oldest - since - 1
                        events = [dict(id=oldest - 1, type="vision.dropped", at=utc_now(),
                                       dropped_count=dropped)] + [event for event in events if event["id"] >= oldest]
                return dict(boot_id=self.boot_id, cursor=stream.cursor, events=events)
        finally:
            self.event_slots.release()

    def start_watch(self, body, client, owner_session=None):
        source = self.source(body)
        ids = body.get("window_ids", [])
        if not isinstance(ids, list) or len(ids) > 4 or (ids and source != "window"):
            raise invalid("window_ids must list up to four window targets")
        ids = [window_id(value) for value in ids]
        if body.get("mode") == "lock_current":
            if source != "window":
                raise invalid("lock_current is only available for windows")
            if ids:
                raise invalid("lock_current takes no window_ids")
            ids = [self.backend.target("window")["id"]]
        if body.get("mode") not in (None, "lock_current"):
            raise invalid("Unknown watch mode")
        fps = number(body.get("fps", 0.5), "fps", 0.1, 2)
        threshold = number(body.get("change_threshold", 0.08), "change_threshold", 0, 1)
        ttl = number(body.get("ttl_s", 600), "ttl_s", 1, 3600, integer=True)
        include = flag(body, "include_image", False)
        with self.condition:
            self.require_active()
            if (len(self.watches) >= MAX_WATCHES or
                    sum((watch.owner_session, watch.client) == (owner_session, client)
                        for watch in self.watches.values()) >= MAX_WATCHES_PER_CLIENT):
                raise limited("Watch limit reached")
            pinned = {wid: self.backend.target("window", wid)["pid"] for wid in ids}
            watch = Watch(identifier("watch"), client, source, pinned, ttl, fps, threshold, include, owner_session)
            self.watches[watch.id] = watch
            ends = monotonic() + ttl
            result = dict(watch_id=watch.id, boot_id=self.boot_id, created_at=utc_now(),
                          expires_at=future(ttl), fps=fps)
            if owner_session is not None:
                result["owner_session"] = owner_session
            self.emit("vision.watch.started", watch, source=source, window_ids=ids,
                      mode=("locked" if pinned else "foreground") if source == "window" else source)
            Thread(target=self.run_watch, args=(watch, ends), daemon=True).start()
            return result

    def run_watch(self, watch, ends):
        prior = {}
        errors = {}
        try:
            while not watch.stop.is_set() and monotonic() < ends:
                started = monotonic()
                for wid in watch.windows or (None,):
                    if watch.stop.is_set() or monotonic() >= ends:
                        break
                    if not self.capture_slots.acquire(blocking=False):
                        continue
                    try:
                        self.watch_frame(watch, wid, ends, prior, errors)
                    except CaptureError as error:
                        with self.condition:
                            if watch.stop.is_set() or monotonic() >= ends:
                                break
                            prior.pop(wid, None)
                            if errors.get(wid) != str(error):
                                errors[wid] = str(error)
                                self.emit("vision.error", watch, window_id=wid,
                                          error=dict(code="source_unavailable", message=str(error),
                                                     retryable=not error.closed))
                            if error.closed and wid:
                                self.stop_watch(watch.id, "source_closed", owner_session=watch.owner_session)
                    finally:
                        self.capture_slots.release()
                watch.stop.wait(max(0, min(ends, started + 1 / watch.fps) - monotonic()))
        finally:
            with self.condition:
                if watch.id in self.watches:
                    self.stop_watch(watch.id, "expired" if monotonic() >= ends else "capture_error",
                                    owner_session=watch.owner_session)

    def watch_frame(self, watch, wid, ends, prior, errors):
        target = self.backend.target(watch.source, wid)
        if wid and target["pid"] != watch.windows[wid]:
            raise CaptureError("Window identity changed", closed=True)
        image, at, sensitive = self.read_frame(target, None)
        identity = (target["id"], target.get("pid"), sensitive)
        preview = image.resize((64, 36)).convert("RGB") if image is not None else None
        previous = prior.get(wid)
        changed = previous is None or previous[0] != identity
        if not changed and preview is not None and previous[1] is not None:
            diff = ImageStat.Stat(ImageChops.difference(previous[1], preview)).mean
            changed = sum(diff) / (3 * 255) > watch.threshold
        if not changed:
            return
        prior[wid] = identity, preview
        errors.pop(wid, None)
        with self.condition:
            if watch.stop.is_set() or monotonic() >= ends:
                return
            meta = self.record(target, None, image, at, sensitive, watch.include_image, store_image=True,
                               owner_session=watch.owner_session)
            self.emit("vision.observation", watch, observation=meta)
            if previous is not None:
                self.emit("vision.scene_changed", watch, observation_id=meta["observation_id"])

    def stop_watch(self, key, reason="requested", *, owner_session=None):
        with self.condition:
            watch = self.watches.get(key)
            if watch is None or watch.owner_session != owner_session:
                if (key, owner_session) in self.finished:
                    raise ApiError(409, "conflict", "Watch has already stopped")
                raise missing("Unknown watch")
            del self.watches[key]
            watch.stop.set()
            self.finished.append((key, owner_session))
            self.emit("vision.watch.stopped", watch, reason=reason)
            return dict(watch_id=key, stopped=True)

    def pause(self, seconds=None):
        with self.condition:
            self.generation += 1
            self.paused = True
            self.observations.clear()
            self.latest.clear()
            for watch in list(self.watches.values()):
                self.stop_watch(watch.id, "paused", owner_session=watch.owner_session)
            if self.pause_timer:
                self.pause_timer.cancel()
            self.pause_timer = None
            if seconds is not None:
                self.pause_timer = Timer(seconds, self.resume, args=(self.generation,))
                self.pause_timer.daemon = True
                self.pause_timer.start()
            return dict(paused=True)

    def resume(self, generation=None):
        with self.condition:
            if self.paused and (generation is None or generation == self.generation):
                self.paused = False
                self.generation += 1
                if self.pause_timer:
                    self.pause_timer.cancel()
                    self.pause_timer = None
            return dict(paused=self.paused)


def valid_local_header(value, *, origin=False):
    try:
        parsed = urlsplit(value if origin else "//" + value)
        return (parsed.hostname in ("127.0.0.1", "localhost", "::1")
                and (parsed.port is None or 0 < parsed.port < 65536)
                and not (parsed.path or parsed.query or parsed.fragment or parsed.username is not None)
                and (not origin or parsed.scheme in ("http", "https")))
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass

    def respond(self, status, result):
        if isinstance(result, bytes):
            data, media = result, "image/jpeg"
        else:
            data = json.dumps(result, ensure_ascii=False).encode()
            media = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise invalid("Transfer-Encoding is unsupported")
        lengths = self.headers.get_all("Content-Length") or ["0"]
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
            raise invalid("Invalid Content-Length")
        size = int(lengths[0])
        if size > MAX_BODY:
            raise invalid("Request body exceeds 64 KB")
        try:
            payload = json.loads(self.rfile.read(size)) if size else {}
        except (ValueError, RecursionError, TimeoutError):
            raise invalid("Invalid JSON request body") from None
        if not isinstance(payload, dict):
            raise invalid("Expected a JSON object")
        return payload

    def dispatch(self):
        if self.client_address[0] != "127.0.0.1":
            raise forbidden("Only loopback clients are allowed")
        hosts = self.headers.get_all("Host") or []
        origins = self.headers.get_all("Origin") or []
        if (len(hosts) != 1 or not valid_local_header(hosts[0]) or len(origins) > 1
                or (origins and not valid_local_header(origins[0], origin=True))):
            raise forbidden("Only loopback Host and Origin values are allowed")
        request = urlsplit(self.path)
        if request.netloc or request.scheme:
            raise invalid("Expected a local request path")
        route = request.path
        method = self.command
        eyes = self.server.eyes
        if (method, route) == ("GET", "/v1/health"):
            return eyes.health()
        authorization = self.headers.get("Authorization", "")
        presented = authorization[7:] if authorization.startswith("Bearer ") else self.headers.get("x-eyes-token", "")
        scopes = self.headers.get_all("x-eyes-session") or []
        if len(scopes) > 1:
            raise invalid("Expected at most one Eyes session")
        owner = scopes[0] if scopes else None
        expected = self.server.token
        if owner is not None:
            try:
                path = token_path(owner, self.server.session_dir)
                expected = path.read_text(encoding="ascii").strip()
            except ValueError as error:
                raise invalid("Invalid Eyes session ID or credential file") from error
            except FileNotFoundError as error:
                raise missing("Eyes session is not registered or has been revoked") from error
        if not re.fullmatch(r"[0-9a-fA-F]{48}", expected) or not compare_digest(presented.encode(), expected.encode()):
            raise ApiError(401, "unauthorized", "A valid Eyes token is required")
        if (method, route) == ("GET", "/v1/capabilities"):
            return eyes.capabilities()
        if (method, route) == ("GET", "/v1/windows"):
            return eyes.windows()
        if (method, route) == ("POST", "/v1/look"):
            return eyes.look(self.body(), owner)
        if (method, route) == ("POST", "/v1/watch"):
            client = self.headers.get("x-eyes-client", "default")
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", client):
                raise invalid("Invalid x-eyes-client")
            return eyes.start_watch(self.body(), client, owner)
        if (method, route) == ("GET", "/v1/events"):
            params = parse_qs(request.query, keep_blank_values=True)
            values = []
            for name, max_value in (("since", 2**63 - 1), ("wait", 30000)):
                value = params.get(name, ["0"])
                if len(value) != 1 or not re.fullmatch(r"[0-9]{1,19}", value[0]):
                    raise invalid(f"Invalid {name}")
                values.append(number(int(value[0]), name, 0, max_value, integer=True))
            return eyes.poll(*values, owner_session=owner)
        if (method, route) == ("POST", "/v1/pause"):
            if owner is not None:
                raise forbidden("Global controls require the local owner token")
            body = self.body()
            seconds = number(body["ttl_s"], "ttl_s", 1, 3600, integer=True) if "ttl_s" in body else None
            return eyes.pause(seconds)
        if (method, route) == ("POST", "/v1/resume"):
            if owner is not None:
                raise forbidden("Global controls require the local owner token")
            self.body()
            return eyes.resume()
        if method == "GET":
            match = re.fullmatch(r"/v1/observations/(obs_[0-9a-f]{32})(/image)?", route)
            if match:
                return eyes.observation(match[1], image=bool(match[2]), owner_session=owner)
        if method == "DELETE":
            match = re.fullmatch(r"/v1/watch/(watch_[0-9a-f]{32})", route)
            if match:
                return eyes.stop_watch(match[1], owner_session=owner)
        raise missing("Unknown endpoint")

    def handle_api(self):
        try:
            try:
                result = self.dispatch()
            except CaptureError as error:
                raise unavailable(str(error)) from error
            self.respond(200, result)
        except ApiError as error:
            self.respond(error.status, dict(error=dict(code=error.code, message=str(error),
                              retryable=error.retryable, request_id=identifier("req"))))

    do_GET = do_POST = do_DELETE = do_OPTIONS = handle_api


def make_server(port, token, eyes, session_dir=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = token
    server.eyes = eyes
    server.session_dir = session_dir
    return server


def load_token(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[a-fA-F0-9]{48}", token):
            raise ValueError("Token file must contain 48 hexadecimal characters")
        return token
    try:
        with os.fdopen(fd, "w", encoding="ascii") as file:
            if os.name == "nt":
                result = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"])
                match = re.search(rb'"(S-1-[0-9-]+)"\s*$', result)
                if not match:
                    raise ValueError("Could not determine the current Windows SID")
                sid = match[1].decode("ascii")
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
                               check=True, stdout=subprocess.DEVNULL)
            token = secrets.token_hex(24)
            file.write(token + "\n")
            return token
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument("--config", type=Path, default=CONFIG_DIR / "config.json")
    parser.add_argument("--token-file", type=Path, default=CONFIG_DIR / "token")
    parser.add_argument("--session-dir", type=Path, default=CONFIG_DIR / "sessions")
    parser.add_argument("--register-session", help="Create the local evidence credential and exit")
    args = parser.parse_args()
    if args.register_session:
        try:
            path = token_path(args.register_session, args.session_dir)
        except ValueError as error:
            parser.error(str(error))
        load_token(path)
        print(f"Eyes session registered: {args.register_session}")
        return
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    if not isinstance(config, dict):
        parser.error("Config must be a JSON object")
    port = args.port if args.port is not None else config.get("port", 8769)
    if type(port) is not int or not 1 <= port <= 65535:
        parser.error("Port must be between 1 and 65535")
    eyes = Eyes(WindowsCapture(), config)
    with make_server(port, load_token(args.token_file), eyes, args.session_dir) as server:
        print(f"Eyes live service listening on http://127.0.0.1:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            eyes.pause()


if __name__ == "__main__":
    main()
