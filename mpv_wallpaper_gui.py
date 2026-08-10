#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mpv 视频壁纸 —— 简洁 GUI 控制面板
用 mpv 把视频变成会动、有声、可轮播的桌面壁纸。
仅依赖标准库 + tkinter, 可一键打包为单文件 exe。

依赖: 同目录下的 mpv_wallpaper.py (播放核心)
打包: pyinstaller --noconsole --onefile --name MpvWallpaper mpv_wallpaper_gui.py
"""
import os
import json
import time
import glob
import struct
import random
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

import mpv_wallpaper  # 播放核心 (WallpaperPlayer)

APP_NAME = "MpvWallpaper"
CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", APP_NAME, "config.json"
)
DEFAULT_WAIT = 30


def get_mp4_duration(path):
    """纯 Python 读取 mp4 时长（秒），无需 ffmpeg。失败返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    moov = data.find(b"moov")
    if moov == -1:
        return None
    idx = data.find(b"mvhd", moov)
    if idx == -1:
        return None
    try:
        version = data[idx + 8]
        if version == 1:
            timescale = struct.unpack(">I", data[idx + 24:idx + 28])[0]
            duration = struct.unpack(">Q", data[idx + 28:idx + 36])[0]
        else:
            timescale = struct.unpack(">I", data[idx + 16:idx + 20])[0]
            duration = struct.unpack(">I", data[idx + 20:idx + 24])[0]
    except Exception:
        return None
    if timescale == 0:
        return None
    return duration / timescale


class WallpaperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.player = None        # WallpaperPlayer 实例
        self.running = False
        self.thread = None
        self.config = self._load_config()
        self._build_vars()
        self._build_ui()
        self._apply_config()
        self._on_switch_mode()

    # ------------------------------------------------------------------ #
    # 配置（自动持久化）
    # ------------------------------------------------------------------ #
    def _load_config(self) -> dict:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        data = {
            "video_dir": self.video_dir.get(),
            "mpv_path": self.mpv_path.get(),
            "mode": self.mode.get(),
            "switch_mode": self.switch_mode.get(),
            "wait": self.wait_var.get(),
            "subfolders": self.subfolders.get(),
            "panscan": self.panscan_var.get(),
            "audio": self.audio_var.get(),
        }
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"配置保存失败: {e}")

    def _build_vars(self):
        self.video_dir = tk.StringVar()
        self.mpv_path = tk.StringVar()
        self.mode = tk.StringVar(value="random")
        self.switch_mode = tk.StringVar(value="fixed")
        self.wait_var = tk.IntVar(value=DEFAULT_WAIT)
        self.subfolders = tk.BooleanVar(value=False)
        self.panscan_var = tk.StringVar(value="1.0")   # "0.0" 原比例 / "1.0" 铺满裁剪
        self.audio_var = tk.BooleanVar(value=False)    # True=有声 / False=静音

    def _apply_config(self):
        self.video_dir.set(self.config.get("video_dir", ""))
        self.mpv_path.set(self.config.get("mpv_path", ""))
        self.mode.set(self.config.get("mode", "random"))
        self.switch_mode.set(self.config.get("switch_mode", "fixed"))
        self.wait_var.set(self.config.get("wait", DEFAULT_WAIT))
        self.subfolders.set(self.config.get("subfolders", False))
        self.panscan_var.set(str(self.config.get("panscan", "1.0")))
        self.audio_var.set(self.config.get("audio", False))

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        self.root.title("mpv 视频壁纸")
        self.root.geometry("560x690")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        ttk.Label(self.root, text="mpv 视频壁纸",
                  font=("Segoe UI", 16, "bold")).pack(pady=(14, 0))
        ttk.Label(self.root, text="会动 · 有声 · 可轮播的桌面壁纸",
                  foreground="#777").pack(pady=(0, 10))

        # 视频目录
        f1 = ttk.LabelFrame(self.root, text="视频目录", padding=10)
        f1.pack(fill="x", padx=14, pady=6)
        row = ttk.Frame(f1)
        row.pack(fill="x")
        self.w_video_entry = ttk.Entry(row, textvariable=self.video_dir)
        self.w_video_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.w_video_btn = ttk.Button(row, text="浏览", width=7,
                                      command=self._browse_video)
        self.w_video_btn.pack(side="left")
        self.w_sub_chk = ttk.Checkbutton(f1, text="包含子文件夹",
                                         variable=self.subfolders)
        self.w_sub_chk.pack(anchor="w", pady=(6, 0))

        # mpv 路径
        f2 = ttk.LabelFrame(self.root, text="mpv.exe 路径（留空自动查找）",
                            padding=10)
        f2.pack(fill="x", padx=14, pady=6)
        row2 = ttk.Frame(f2)
        row2.pack(fill="x")
        self.w_mpv_entry = ttk.Entry(row2, textvariable=self.mpv_path)
        self.w_mpv_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.w_mpv_btn = ttk.Button(row2, text="浏览", width=7,
                                    command=self._browse_mpv)
        self.w_mpv_btn.pack(side="left")

        # 播放设置
        f3 = ttk.LabelFrame(self.root, text="播放设置", padding=10)
        f3.pack(fill="x", padx=14, pady=6)
        row_mode = ttk.Frame(f3)
        row_mode.pack(fill="x")
        ttk.Label(row_mode, text="播放顺序").pack(side="left")
        self.w_mode_random = ttk.Radiobutton(
            row_mode, text="随机", value="random", variable=self.mode)
        self.w_mode_random.pack(side="left", padx=(4, 16))
        self.w_mode_seq = ttk.Radiobutton(
            row_mode, text="顺序", value="sequential", variable=self.mode)
        self.w_mode_seq.pack(side="left")

        row_switch = ttk.Frame(f3)
        row_switch.pack(fill="x", pady=(8, 0))
        ttk.Label(row_switch, text="切换方式").pack(side="left")
        self.w_switch_fixed = ttk.Radiobutton(
            row_switch, text="固定时长", value="fixed",
            variable=self.switch_mode, command=self._on_switch_mode)
        self.w_switch_fixed.pack(side="left", padx=(4, 12))
        self.w_switch_dur = ttk.Radiobutton(
            row_switch, text="按视频时长", value="duration",
            variable=self.switch_mode, command=self._on_switch_mode)
        self.w_switch_dur.pack(side="left")

        row_wait = ttk.Frame(f3)
        row_wait.pack(fill="x", pady=(8, 0))
        ttk.Label(row_wait, text="固定间隔").pack(side="left")
        self.w_wait_spin = ttk.Spinbox(
            row_wait, from_=3, to=600, increment=1, width=6,
            textvariable=self.wait_var)
        self.w_wait_spin.pack(side="left", padx=(4, 0))
        ttk.Label(row_wait, text="秒（仅“固定时长”模式使用）").pack(side="left", padx=(4, 0))

        row_fill = ttk.Frame(f3)
        row_fill.pack(fill="x", pady=(8, 0))
        ttk.Label(row_fill, text="填充方式").pack(side="left")
        self.w_fill_fit = ttk.Radiobutton(
            row_fill, text="原比例", value="0.0", variable=self.panscan_var,
            command=self._on_fill_changed)
        self.w_fill_fit.pack(side="left", padx=(4, 12))
        self.w_fill_cover = ttk.Radiobutton(
            row_fill, text="铺满裁剪", value="1.0", variable=self.panscan_var,
            command=self._on_fill_changed)
        self.w_fill_cover.pack(side="left")

        row_audio = ttk.Frame(f3)
        row_audio.pack(fill="x", pady=(8, 0))
        self.w_audio_chk = ttk.Checkbutton(
            row_audio, text="开启声音", variable=self.audio_var,
            command=self._on_audio_changed)
        self.w_audio_chk.pack(side="left")

        # 开始 / 停止
        bf = ttk.Frame(self.root)
        bf.pack(fill="x", padx=14, pady=(10, 4))
        self.btn_start = ttk.Button(bf, text="▶ 开始", command=self.start)
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_stop = ttk.Button(bf, text="■ 停止", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True)

        # 日志
        lf = ttk.LabelFrame(self.root, text="运行状态", padding=8)
        lf.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.log_text = scrolledtext.ScrolledText(
            lf, height=10, state="disabled",
            font=("Consolas", 9), fg="#222")
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _browse_video(self):
        d = filedialog.askdirectory(title="选择视频目录")
        if d:
            self.video_dir.set(d)

    def _browse_mpv(self):
        f = filedialog.askopenfilename(title="选择 mpv.exe",
                                       filetypes=[("mpv", "mpv.exe")])
        if f:
            self.mpv_path.set(f)

    def _on_switch_mode(self):
        state = "disabled" if self.switch_mode.get() == "duration" else "normal"
        try:
            self.w_wait_spin.configure(state=state)
        except Exception:
            pass

    def _set_inputs(self, state):
        for w in (self.w_video_entry, self.w_video_btn, self.w_sub_chk,
                  self.w_mpv_entry, self.w_mpv_btn,
                  self.w_mode_random, self.w_mode_seq, self.w_wait_spin,
                  self.w_switch_fixed, self.w_switch_dur,
                  self.w_fill_fit, self.w_fill_cover, self.w_audio_chk):
            try:
                w.configure(state=state)
            except Exception:
                pass

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
        self.root.after(0, lambda: self._log_insert(line))

    def _log_insert(self, line):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _on_close(self):
        self._save_config()
        if self.running:
            self.running = False
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=5)
        if self.player is not None:
            self.player.stop()
            self.player = None
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # 播放控制（mpv 核心：首次 embed 一次, 之后全走 IPC, 绝不重嵌）
    # ------------------------------------------------------------------ #
    def _scan_videos(self):
        base = self.video_dir.get()
        pattern = os.path.join(base, "**", "*.mp4") if self.subfolders.get() \
            else os.path.join(base, "*.mp4")
        return sorted(glob.glob(pattern, recursive=self.subfolders.get()))

    def _build_playlist(self, videos):
        pl = list(videos)
        if self.mode.get() == "random":
            random.shuffle(pl)
        return pl

    def _wait_secs(self, video_path):
        if self.switch_mode.get() == "duration":
            dur = get_mp4_duration(video_path)
            if dur:
                return dur + 1.0
            self.log("    ⚠ 无法读取时长，回退到固定间隔")
        return float(max(1, self.wait_var.get()))

    def _on_fill_changed(self):
        """运行中改填充：运行时下发 panscan, 不重嵌。"""
        if not self.running or self.player is None:
            return
        self.player.set_panscan(float(self.panscan_var.get()))

    def _on_audio_changed(self):
        """运行中改声音：运行时下发 mute, 不重嵌。"""
        if not self.running or self.player is None:
            return
        self.player.set_mute(not self.audio_var.get())

    def start(self):
        video_dir = self.video_dir.get().strip()
        mpv_path = self.mpv_path.get().strip()
        if not video_dir or not os.path.isdir(video_dir):
            self.log("⚠ 请先选择有效的视频目录")
            return
        videos = self._scan_videos()
        if not videos:
            self.log("⚠ 该目录没有找到 mp4 视频")
            return

        self._save_config()
        mode_name = "随机" if self.mode.get() == "random" else "顺序"
        switch_name = "按视频时长" if self.switch_mode.get() == "duration" else "固定时长"
        self.log(f"▶ 启动壁纸：共 {len(videos)} 个视频，{mode_name} / {switch_name}")

        # 创建播放器 + 嵌入桌面 + 启动 mpv（仅此一次 embed）
        self.player = mpv_wallpaper.WallpaperPlayer(
            mpv_path=mpv_path or None,
            fill="cover" if self.panscan_var.get() == "1.0" else "contain",
            audio=self.audio_var.get(),
        )
        if not self.player.check_prerequisites():
            self.player = None
            return
        if not self.player.embed():
            self.player.stop()
            self.player = None
            return
        if not self.player.launch_mpv(videos[0]):
            self.player.stop()
            self.player = None
            return

        self.running = True
        self._set_inputs("disabled")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.thread = threading.Thread(target=self._playback_loop, daemon=True)
        self.thread.start()

    def _playback_loop(self):
        try:
            playlist = self._build_playlist(self._scan_videos())
            # 第一支已在 start() 里播上, 这里只等时长 → 切下一个
            self._wait(self._wait_secs(playlist[0]))
            idx = 1
            while self.running:
                if idx >= len(playlist):          # 一轮播完 → 继续循环
                    playlist = self._build_playlist(self._scan_videos())
                    idx = 0
                item = playlist[idx]
                name = os.path.basename(item)
                if not self.player.is_running():
                    self.log("  ✗ mpv 已退出（桌面状态可能已退化，建议重启电脑后重试）")
                    break
                self.player.play(item)
                self.log(f"  ▶ {name}  ({idx + 1}/{len(playlist)})")
                self._wait(self._wait_secs(item))
                idx += 1
        except Exception as e:
            self.log(f"✗ 运行出错：{e}")
        finally:
            try:
                self.root.after(0, self._on_stopped)
            except Exception:
                pass

    def _wait(self, secs):
        for _ in range(int(secs * 10)):
            if not self.running:
                return
            time.sleep(0.1)

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.log("■ 正在停止（卸载桌面嵌入层）…")
        if self.player is not None:
            self.player.stop()
            self.player = None
        self._on_stopped()

    def _on_stopped(self):
        self.running = False
        self._set_inputs("normal")
        self._on_switch_mode()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.log("■ 已停止")


def main():
    root = tk.Tk()
    WallpaperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
