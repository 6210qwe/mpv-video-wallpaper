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


def fmt_duration(sec):
    """秒 -> M:SS / H:MM:SS"""
    if not sec or sec <= 0:
        return "--:--"
    sec = int(round(sec))
    h, m = divmod(sec, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class WallpaperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.player = None        # WallpaperPlayer 实例
        self.running = False
        self.thread = None
        self._playlist = None
        self._run_token = 0          # 每轮运行的唯一代次, 防止旧线程的 _on_stopped 误复位新一轮
        self._stopped_token = -1      # 被用户主动停止的是哪一轮
        self.config = self._load_config()
        self._build_vars()
        self._build_ui()
        self._apply_config()
        self._on_switch_mode()
        self._on_single_changed()

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
            "single": self.single_var.get(),
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
        self.single_var = tk.BooleanVar(value=False)   # True=单个视频(循环播放)
        self.panscan_var = tk.StringVar(value="1.0")   # "0.0" 原比例 / "1.0" 铺满裁剪
        self.audio_var = tk.BooleanVar(value=False)    # True=有声 / False=静音

    def _apply_config(self):
        self.video_dir.set(self.config.get("video_dir", ""))
        self.mpv_path.set(self.config.get("mpv_path", ""))
        self.mode.set(self.config.get("mode", "random"))
        self.switch_mode.set(self.config.get("switch_mode", "fixed"))
        self.wait_var.set(self.config.get("wait", DEFAULT_WAIT))
        self.subfolders.set(self.config.get("subfolders", False))
        self.single_var.set(self.config.get("single", False))
        self.panscan_var.set(str(self.config.get("panscan", "1.0")))
        self.audio_var.set(self.config.get("audio", False))

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        self.root.title("mpv 视频壁纸")
        self.root.geometry("560x690")
        self.root.minsize(520, 560)           # 最小尺寸兜底, 缩太小时布局不会乱
        self.root.resizable(True, True)       # 允许自由拖拽缩放窗口
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
        self.w_single_chk = ttk.Checkbutton(
            f1, text="单个视频（循环播放）", variable=self.single_var,
            command=self._on_single_changed)
        self.w_single_chk.pack(anchor="w", pady=(2, 0))

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
        if self.single_var.get():
            f = filedialog.askopenfilename(
                title="选择单个视频文件",
                filetypes=[("视频文件", "*.mp4;*.mkv;*.avi;*.webm;*.mov;*.flv"),
                           ("所有文件", "*.*")])
            if f:
                self.video_dir.set(f)
        else:
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

    def _on_single_changed(self):
        """单个视频模式: 禁用与目录 / 轮播相关的控件"""
        single = self.single_var.get()
        state = "disabled" if single else "normal"
        for w in (self.w_sub_chk, self.w_mode_random, self.w_mode_seq,
                  self.w_switch_fixed, self.w_switch_dur, self.w_wait_spin):
            try:
                w.configure(state=state)
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
        exts = mpv_wallpaper.VIDEO_EXTS
        files = []
        if self.subfolders.get():
            for ext in exts:
                files.extend(glob.glob(os.path.join(base, "**", ext),
                                      recursive=True))
        else:
            for ext in exts:
                files.extend(glob.glob(os.path.join(base, ext)))
        # 自然排序, 与 Windows 资源管理器一致 (数字按数值比, 如 2 < 10)
        return sorted(files, key=mpv_wallpaper.natural_sort_key)

    def _build_playlist(self, videos):
        pl = list(videos)
        if self.mode.get() == "random":
            random.shuffle(pl)
        return pl

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

    def _on_now_playing(self, path, duration):
        """mpv 播放核心回调: 在日志中显示正在播放的视频名称 + 时长"""
        if not self.running:
            return
        name = os.path.basename(path)
        self.root.after(0, lambda: self.log(f"▶ {name}  ({fmt_duration(duration)})"))

    def start(self):
        mpv_path = self.mpv_path.get().strip()
        if self.single_var.get():
            # 单个视频: 循环播放该文件 (最稳, 全程零切换, 无 EOF 冻结)
            f = self.video_dir.get().strip()
            if not f or not os.path.isfile(f):
                self.log("⚠ 请先选择单个视频文件")
                return
            videos = [os.path.abspath(f)]
        else:
            video_dir = self.video_dir.get().strip()
            if not video_dir or not os.path.isdir(video_dir):
                self.log("⚠ 请先选择有效的视频目录")
                return
            videos = self._scan_videos()
            if not videos:
                self.log("⚠ 该目录没有找到视频文件")
                return

        playlist = self._build_playlist(videos)
        self._playlist = playlist
        if len(videos) == 1:
            self.log(f"▶ 启动壁纸：单个视频循环 → {os.path.basename(videos[0])}")
        else:
            mode_name = "随机" if self.mode.get() == "random" else "顺序"
            switch_name = "按视频时长" if self.switch_mode.get() == "duration" else "固定时长"
            self.log(f"▶ 启动壁纸：共 {len(videos)} 个视频，{mode_name} / {switch_name}")

        # 创建播放器 + 嵌入桌面 + 启动 mpv（仅此一次 embed）
        # 按视频时长(多文件): 写成 m3u8 播放列表交给 mpv 内部循环(可承载数万文件, 无 EOF 冻结)
        # 单视频: 直接 --loop=inf 常驻(不写 m3u8)
        # 固定时长: 首视频 --loop=inf, 定时器 loadfile 切换(播放中途切, 不踩 EOF), 不用 --{ } 分组
        self.player = mpv_wallpaper.WallpaperPlayer(
            mpv_path=mpv_path or None,
            fill="cover" if self.panscan_var.get() == "1.0" else "contain",
            audio=self.audio_var.get(),
        )
        self.player.on_now_playing = self._on_now_playing
        if not self.player.check_prerequisites():
            self.player = None
            return
        if not self.player.embed():
            self.player.stop()
            self.player = None
            return

        self._m3u_path = None
        use_duration = (self.switch_mode.get() == "duration") or len(playlist) == 1
        if use_duration:
            # 时长模式 / 单视频: 播放列表文件 + mpv 内部循环
            if len(playlist) == 1:
                if not self.player.launch([playlist[0]]):
                    self.player.stop(); self.player = None; return
            else:
                self._m3u_path = mpv_wallpaper.write_playlist_file(playlist)
                if not self.player.launch(playlist, playlist_file=self._m3u_path):
                    self.player.stop(); self.player = None; return
        else:
            # 固定时长: 首视频 --loop=inf, 之后定时 loadfile 切换
            if not self.player.launch([playlist[0]]):
                self.player.stop(); self.player = None; return

        self._run_token += 1
        token = self._run_token
        self.running = True
        self._set_inputs("disabled")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.thread = threading.Thread(target=self._playback_loop,
                                       args=(token,), daemon=True)
        self.thread.start()

    def _playback_loop(self, token):
        try:
            playlist = self._playlist
            n = len(playlist)
            if (self.switch_mode.get() == "duration") or n == 1:
                # 时长模式 / 单视频: mpv 内部循环, 这里只监控存活
                while self.running:
                    if not self.player.is_running():
                        self.log("  ✗ mpv 已退出（桌面状态可能已退化，建议重启电脑后重试）")
                        break
                    time.sleep(1)
            else:
                # 固定时长: 定时 loadfile 切换 (当前视频恒在播放中, 避开 EOF 冻结)
                interval = max(1, self.wait_var.get())
                idx = 1  # 第 0 支已在 launch 中播上
                while self.running:
                    if not self.player.is_running():
                        self.log("  ✗ mpv 已退出（桌面状态可能已退化，建议重启电脑后重试）")
                        break
                    self.player.play(playlist[idx % n], loop=True)
                    idx += 1
                    for _ in range(interval * 10):
                        if not self.running:
                            break
                        time.sleep(0.1)
        except Exception as e:
            self.log(f"✗ 运行出错：{e}")
        finally:
            # 仅当此轮 mpv 意外退出(用户未主动停止这一轮)时才复位按钮;
            # 用户点停止时 stop() 已复位, 且旧线程若误触发 _on_stopped 也会被 token 校验拦截
            try:
                if self._stopped_token != token:
                    self.root.after(0, lambda: self._on_stopped(token))
            except Exception:
                pass

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._stopped_token = self._run_token
        if self.player is not None:
            self.player.stop()
            self.player = None
        if getattr(self, "_m3u_path", None) and os.path.isfile(self._m3u_path):
            try:
                os.remove(self._m3u_path)
            except Exception:
                pass
        self._on_stopped(self._run_token)

    def _on_stopped(self, token=None):
        # token 校验: 仅当仍是同一轮运行时才复位按钮, 防止旧线程的延迟回调误复位新一轮
        if token is not None and token != self._run_token:
            return
        self.running = False
        self._set_inputs("normal")
        self._on_switch_mode()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")


def main():
    root = tk.Tk()
    WallpaperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
