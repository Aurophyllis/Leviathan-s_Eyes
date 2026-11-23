"""Windows capture backends. All frames stay in memory."""

import ctypes as ct
from ctypes import wintypes as wt
from datetime import datetime, timezone
import os
from threading import Event

from PIL import Image, ImageGrab


class CaptureError(Exception):
    def __init__(self, message, *, closed=False):
        super().__init__(message)
        self.closed = closed


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


if os.name == "nt":
    user = ct.WinDLL("user32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    dwm = ct.WinDLL("dwmapi")
    enum_callback = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def signature(dll, name, result, *args):
        function = getattr(dll, name)
        function.argtypes = args
        function.restype = result
        return function

    foreground = signature(user, "GetForegroundWindow", wt.HWND)
    enum_windows = signature(user, "EnumWindows", wt.BOOL, enum_callback, wt.LPARAM)
    visible = signature(user, "IsWindowVisible", wt.BOOL, wt.HWND)
    exists = signature(user, "IsWindow", wt.BOOL, wt.HWND)
    minimized = signature(user, "IsIconic", wt.BOOL, wt.HWND)
    window_title = signature(user, "GetWindowTextW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
    window_rect = signature(user, "GetWindowRect", wt.BOOL, wt.HWND, ct.POINTER(wt.RECT))
    window_pid = signature(user, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ct.POINTER(wt.DWORD))
    process_open = signature(kernel, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    process_name = signature(kernel, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD,
                             wt.LPWSTR, ct.POINTER(wt.DWORD))
    process_close = signature(kernel, "CloseHandle", wt.BOOL, wt.HANDLE)
    window_attribute = signature(dwm, "DwmGetWindowAttribute", ct.c_long, wt.HWND, wt.DWORD,
                                 ct.c_void_p, wt.DWORD)
    system_metrics = signature(user, "GetSystemMetrics", ct.c_int, ct.c_int)


def rectangle(rect):
    return dict(x=rect.left, y=rect.top, w=rect.right - rect.left, h=rect.bottom - rect.top)


def describe(hwnd):
    rect = wt.RECT()
    if not exists(hwnd) or not window_rect(hwnd, ct.byref(rect)):
        raise CaptureError("Window is closed", closed=True)
    cloaked = wt.DWORD()
    window_attribute(hwnd, 14, ct.byref(cloaked), ct.sizeof(cloaked))
    extended = wt.RECT()
    if window_attribute(hwnd, 9, ct.byref(extended), ct.sizeof(extended)) == 0:
        rect = extended
    title = ct.create_unicode_buffer(4096)
    window_title(hwnd, title, len(title))
    pid = wt.DWORD()
    window_pid(hwnd, ct.byref(pid))
    app = ""
    handle = process_open(0x1000, False, pid.value)
    if handle:
        try:
            path = ct.create_unicode_buffer(32768)
            length = wt.DWORD(len(path))
            if process_name(handle, 0, path, ct.byref(length)):
                app = os.path.basename(path.value)
        finally:
            process_close(handle)
    bounds = rectangle(rect)
    center_x = bounds["x"] + bounds["w"] // 2
    center_y = bounds["y"] + bounds["h"] // 2
    display = "primary" if 0 <= center_x < system_metrics(0) and 0 <= center_y < system_metrics(1) else None
    return dict(window_id=f"hwnd:0x{hwnd:X}", title=title.value, app=app, pid=pid.value,
                display_id=display, bounds=bounds, visible=bool(visible(hwnd) and not cloaked.value))


def window_handle(window_id):
    hwnd = foreground() if not window_id else int(window_id[7:], 16)
    if not hwnd:
        raise CaptureError("No foreground window")
    return hwnd


class WindowsCapture:
    def __init__(self):
        self.sources = []
        if os.name != "nt":
            return
        user.SetProcessDpiAwarenessContext(ct.c_void_p(-4))
        self.sources.append("screen")
        try:
            from windows_capture import WindowsCapture as Wgc
            self.wgc = Wgc
            self.sources.append("window")
            self.sources.append("camera")
        except ImportError:
            pass

    def windows(self):
        result = []

        @enum_callback
        def append(hwnd, _):
            if visible(hwnd) and not minimized(hwnd):
                try:
                    info = describe(hwnd)
                except CaptureError:
                    return True
                if info["visible"]:
                    result.append(info)
            return True

        if os.name == "nt" and not enum_windows(append, 0):
            raise CaptureError("Cannot enumerate windows")
        return result

    def target(self, source, window_id=None):
        if source == "window":
            hwnd = window_handle(window_id)
            info = describe(hwnd)
            if not info["visible"] or minimized(hwnd):
                raise CaptureError("Window is minimized or unavailable")
            return dict(kind=source, id=info["window_id"], title=info["title"], app=info["app"],
                        display_id=info["display_id"], region=info["bounds"], pid=info["pid"])
        if source == "screen":
            return dict(kind=source, id="primary", display_id="primary",
                        region=dict(x=0, y=0, w=system_metrics(0), h=system_metrics(1)))
        return dict(kind=source, id="camera:0")

    def capture(self, target):
        try:
            if target["kind"] == "screen":
                image = ImageGrab.grab()
                return image, utc_now()
            if target["kind"] == "window":
                return self.capture_window(window_handle(target["id"]))
            return self.capture_camera()
        except (OSError, RuntimeError, ValueError) as error:
            raise CaptureError("Windows capture source unavailable") from error

    def capture_window(self, hwnd):
        capture = self.wgc(window_hwnd=hwnd, cursor_capture=False)
        ready = Event()
        frames = []

        @capture.event
        def on_frame_arrived(frame, control):
            try:
                if exists(hwnd) and not minimized(hwnd):
                    pixels = frame.frame_buffer.tobytes()
                    image = Image.frombytes("RGB", (frame.width, frame.height), pixels, "raw", "BGRX")
                    frames.append((image, utc_now()))
            finally:
                control.stop()
                ready.set()

        @capture.event
        def on_closed():
            ready.set()

        control = capture.start_free_threaded()
        try:
            ready.wait(1.5)
        finally:
            if not control.is_finished():
                control.stop()
        if not exists(hwnd):
            raise CaptureError("Window is closed", closed=True)
        if not frames or minimized(hwnd):
            raise CaptureError("Window did not produce a fresh frame")
        image, timestamp = frames[0]
        if not image.getbbox():
            raise CaptureError("Window produced a black or protected frame")
        return image, timestamp

    @staticmethod
    def capture_camera():
        import cv2

        camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        try:
            ok, pixels = camera.read()
            if not ok:
                raise CaptureError("Camera did not produce a frame")
            return Image.fromarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB)), utc_now()
        finally:
            camera.release()
