"""Windows desktop preview for Leviathan's Eyes."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
import tkinter as tk
from tkinter import filedialog, ttk

from PIL import Image, ImageTk

from serve import ObservationHandler, VideoSession
from sampling import METHODS
from video_client import VideoClient


class Desktop:
    def __init__(self, root, output, port):
        self.root = root
        self.output = output
        self.server = HTTPServer(("127.0.0.1", port), ObservationHandler)
        self.server.session = None
        self.client = VideoClient(self.server.server_port)
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.observation = None
        self.image = None
        self.bitmap = None
        Thread(target=self.server.serve_forever, daemon=True).start()
        root.title("Leviathan's Eyes")
        root.geometry("1080x650")
        root.minsize(760, 480)
        root.configure(bg="#101923")
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#101923")
        style.configure("TLabel", background="#101923", foreground="#e6eef7", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("TButton", padding=(12, 6))
        main = ttk.Frame(root, padding=20)
        main.pack(fill="both", expand=True)
        header = ttk.Frame(main)
        header.pack(fill="x")
        ttk.Label(header, text="Leviathan's Eyes", style="Title.TLabel").pack(side="left")
        self.open_button = ttk.Button(header, text="打开视频", command=self.pick_video)
        self.open_button.pack(side="right")
        self.video_label = ttk.Label(main, text="尚未打开视频")
        self.video_label.pack(anchor="w", pady=(6, 12))

        toolbar = ttk.Frame(main)
        toolbar.pack(fill="x", pady=(0, 12))
        self.start = self.field(toolbar, "起点 ms", "0")
        self.end = self.field(toolbar, "终点 ms", "")
        self.count = self.field(toolbar, "帧数", "4")
        self.method_labels = dict(zip(("均匀", "画面变化", "变化与覆盖", "嵌套覆盖"), METHODS))
        self.method = tk.StringVar(value="均匀")
        ttk.Combobox(toolbar, textvariable=self.method, values=list(self.method_labels),
                     state="readonly", width=10).pack(side="left", padx=(0, 8))
        self.observe_button = ttk.Button(toolbar, text="生成拼图", command=self.observe, state="disabled")
        self.observe_button.pack(side="left", padx=(2, 8))
        self.sheet_button = ttk.Button(toolbar, text="返回拼图", command=self.show_sheet, state="disabled")
        self.sheet_button.pack(side="left")

        body = ttk.Frame(main)
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body)
        sidebar.pack(side="right", fill="y", padx=(12, 0))
        ttk.Label(sidebar, text="帧与时间").pack(anchor="w", pady=(0, 6))
        self.frames = tk.Listbox(sidebar, width=18, bg="#1b2a38", fg="#e6eef7",
                                 selectbackground="#325f75", font=("Consolas", 11), exportselection=False)
        self.frames.pack(side="left", fill="y")
        scrollbar = ttk.Scrollbar(sidebar, command=self.frames.yview)
        scrollbar.pack(side="right", fill="y")
        self.frames.configure(yscrollcommand=scrollbar.set)
        self.frames.bind("<<ListboxSelect>>", self.select_frame)
        self.canvas = tk.Canvas(body, bg="#080e15", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.render())
        self.status = tk.StringVar(value="打开本地视频以开始")
        footer = ttk.Frame(main)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        ttk.Label(footer, text=self.client.base).pack(side="right")

    @staticmethod
    def field(parent, label, initial):
        ttk.Label(parent, text=label).pack(side="left")
        value = tk.StringVar(value=initial)
        ttk.Entry(parent, width=8, textvariable=value).pack(side="left", padx=(5, 12))
        return value

    def pick_video(self):
        path = filedialog.askopenfilename(filetypes=[
            ("视频", "*.mp4 *.mkv *.mov *.webm *.avi"), ("所有文件", "*.*")])
        if path:
            self.load_video(Path(path))

    def submit(self, work, done):
        self.open_button.configure(state="disabled")
        self.observe_button.configure(state="disabled")
        self.sheet_button.configure(state="disabled")
        self.frames.configure(state="disabled")
        future = self.worker.submit(work)

        def poll():
            if not future.done():
                self.root.after(40, poll)
                return
            self.open_button.configure(state="normal")
            self.frames.configure(state="normal")
            try:
                done(future.result())
            except (ValueError, OSError) as error:
                self.status.set(str(error).splitlines()[0])
            self.observe_button.configure(state="normal" if self.server.session else "disabled")
            sheet_available = self.observation and self.observation["sheet_url"]
            self.sheet_button.configure(state="normal" if sheet_available else "disabled")

        self.root.after(40, poll)

    def load_video(self, path):
        self.status.set("正在读取视频时间轴…")

        def loaded(session):
            self.server.session = session
            self.observation = None
            self.frames.delete(0, "end")
            self.start.set("0")
            self.end.set(str(session.info["duration_ms"]))
            self.video_label.configure(text=path.name)
            self.show(None)
            self.status.set("视频已打开，可以生成拼图")

        self.submit(lambda: VideoSession(path.resolve(), self.output), loaded)

    def observe(self):
        try:
            start, end, count = int(self.start.get()), int(self.end.get()), int(self.count.get())
        except ValueError:
            self.status.set("起点、终点和帧数须为整数")
            return
        video_id = self.server.session.info["video_id"]
        method = self.method_labels[self.method.get()]
        self.status.set("正在生成拼图…")

        def work():
            result = self.client.observe(video_id, start, end, count, method)
            path = result["sheet_url"]
            return result, self.client.image(path) if path else None

        def displayed(preview):
            self.observation, image = preview
            self.frames.delete(0, "end")
            for frame in self.observation["frames"]:
                self.frames.insert("end", f'{frame["frame_id"]:>3}   {frame["t_ms"] / 1000:>7.3f} s')
            self.show(image)
            self.status.set(f'已选 {len(self.observation["frames"])} / {count} 帧')

        self.submit(work, displayed)

    def show_sheet(self):
        path = self.observation["sheet_url"]
        self.submit(lambda: self.client.image(path), self.show)

    def select_frame(self, event=None):
        selected = self.frames.curselection()
        if selected and self.observation:
            frame = self.observation["frames"][selected[0]]

            def displayed(data):
                self.show(data)
                self.status.set(f'第 {frame["frame_id"]} 帧 · {frame["t_ms"] / 1000:.3f} 秒')

            self.submit(lambda: self.client.image(frame["image_url"]), displayed)

    def show(self, data):
        self.image = None
        if data:
            with Image.open(BytesIO(data)) as image:
                self.image = image.copy()
        self.render()

    def render(self):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.delete("all")
        if self.image is None:
            self.canvas.create_text(width // 2, height // 2, fill="#9eb4c6",
                                    text="此处显示拼图或单帧", font=("Segoe UI", 12))
            return
        image = self.image.copy()
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.bitmap = ImageTk.PhotoImage(image, master=self.root)
        self.canvas.create_image(width // 2, height // 2, image=self.bitmap)

    def close(self):
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.server.shutdown()
        self.server.server_close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = tk.Tk()
    try:
        Desktop(root, args.output.resolve(), args.port)
    except OSError as error:
        root.destroy()
        parser.exit(1, f"无法启动本机服务: {error}\n")
    root.mainloop()


if __name__ == "__main__":
    main()
