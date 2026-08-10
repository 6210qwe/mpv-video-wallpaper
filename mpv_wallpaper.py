#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mpv 视频壁纸播放器 (Win11 25H2 / Build 26200 兼容)

原理: Win11 26200 的 Progman 是 raised desktop (带 WS_EX_NOREDIRECTIONBITMAP),
经典 SetParent 会被 DWM 静默丢弃。本脚本改为:
  1. 窗口创建时即带 WS_EX_LAYERED + SetLayeredWindowAttributes(255), 让 DWM 正常合成
  2. CreateWindowExW 时直接把父窗口设为 Progman 的 WorkerW 子窗口 (从出生就是子窗口)
  3. 退出时只 DestroyWindow + 终止 mpv, 不调 SystemParametersInfo, 不损坏桌面

可作为命令行工具使用, 也可被 mpv_wallpaper_gui.py 作为模块 import。

命令行:
  python mpv_wallpaper.py "D:\\a.mp4" --mpv "C:\\mpv\\mpv.exe"
  python mpv_wallpaper.py "D:\\videos" --fill contain --interval 10 --mpv "C:\\mpv\\mpv.exe"
"""

import ctypes
import ctypes.wintypes as w
import subprocess
import threading
import time
import os
import json
import glob
import re
import random
import sys
import argparse
import shutil

# ==================== Win32 常量 ====================
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CLIPCHILDREN = 0x02000000
LWA_ALPHA = 0x00000002
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SW_SHOW = 5
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

# ==================== Win32 类型 ====================
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


# ==================== Win32 API 声明 ====================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = w.ATOM

user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = w.HWND

user32.DestroyWindow.argtypes = [w.HWND]
user32.DestroyWindow.restype = w.BOOL

user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LRESULT

user32.SetLayeredWindowAttributes.argtypes = [w.HWND, w.COLORREF, w.BYTE, w.DWORD]
user32.SetLayeredWindowAttributes.restype = w.BOOL

user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.ShowWindow.restype = w.BOOL

user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

user32.EnumWindows.argtypes = [WNDENUMPROC, w.LPARAM]
user32.EnumWindows.restype = w.BOOL

user32.EnumChildWindows.argtypes = [w.HWND, WNDENUMPROC, w.LPARAM]
user32.EnumChildWindows.restype = w.BOOL

user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

user32.IsWindow.argtypes = [w.HWND]
user32.IsWindow.restype = w.BOOL

user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsWindowVisible.restype = w.BOOL

user32.SendMessageTimeoutW.argtypes = [
    w.HWND, w.UINT, w.WPARAM, w.LPARAM,
    w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = ctypes.c_longlong

kernel32.CreateFileW.argtypes = [
    w.LPCWSTR, w.DWORD, w.DWORD,
    ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE,
]
kernel32.CreateFileW.restype = w.HANDLE

kernel32.WriteFile.argtypes = [
    w.HANDLE, ctypes.c_void_p, w.DWORD,
    ctypes.POINTER(w.DWORD), ctypes.c_void_p,
]
kernel32.WriteFile.restype = w.BOOL

kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CloseHandle.restype = w.BOOL

kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HMODULE

kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = w.DWORD

# DPI 感知 (必须在创建窗口前)
try:
    shcore = ctypes.windll.shcore
    shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
    shcore.SetProcessDpiAwareness.restype = ctypes.c_long
    shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

# 全局引用 (防止 GC 回收回调)
_g_wndproc = None
_g_hinstance = None
_g_class_registered = False


# ==================== 辅助函数 ====================

def find_window_by_class(cls_name):
    """找指定类名的顶层窗口 (返回第一个)"""
    found = []

    def _cb(hwnd, lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == cls_name:
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def find_children_by_class(parent_hwnd, cls_name, recursive=True):
    """找父窗口下指定类名的所有子窗口（含后代，兼容 Win10/Win11 不同桌面层级）。

    Win10 的 WorkerW 通常嵌套在 SHELLDLL_DefView 之下，不是 Progman 的直接子窗口；
    若只枚举直接子会漏掉它，从而在 Win10 上提示"找不到 WorkerW 子窗口"。
    recursive=True（默认）递归枚举所有后代，两种布局都能命中。
    """
    found = []

    def _cb(hwnd, lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == cls_name:
            found.append(hwnd)
        if recursive:
            user32.EnumChildWindows(hwnd, WNDENUMPROC(_cb), 0)
        return True

    cb = WNDENUMPROC(_cb)  # 保活回调，避免 ctypes 临时对象被 GC
    user32.EnumChildWindows(parent_hwnd, cb, 0)
    return found


def _default_wndproc(hwnd, msg, wparam, lparam):
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ==================== 播放器 ====================

class WallpaperPlayer:
    """
    mpv 视频壁纸播放器 (Win11 26200 兼容)

    嵌入策略: 窗口创建时即设为 Progman 下 WorkerW 的 LAYERED 子窗口。
    退出: 终止 mpv -> CloseHandle -> DestroyWindow, 不损坏桌面状态机。
    改填充/改声音走 mpv IPC; 切视频在按时长/单视频模式下由 mpv 内部循环完成,
    固定时长模式下才走 IPC loadfile 切换。全程不重嵌、不重启桌面。
    """

    PIPE_NAME = r"\\.\pipe\mpvwallpaper"

    def __init__(self, mpv_path=None, vo="direct3d", hwdec="auto-copy",
                 fill="cover", audio=True):
        self.vo = vo
        self.hwdec = hwdec
        self.fill = fill
        self.audio = audio          # True=有声(默认不静音) / False=静音
        self._auto_mpv = mpv_path is None   # 是否走自动查找
        self.mpv_path = mpv_path or self._find_mpv()
        self.hwnd = None
        self.mpv_proc = None
        self.pipe_handle = None
        self._stopped = False
        self._mpv_lines = []        # mpv 输出缓冲, 异常时回吐
        self._ipc_alive = False
        self.on_now_playing = None     # 回调: (path:str, duration:float|None) -> 显示"正在播放"
        self._np_path = None
        self._np_duration = None
        self._np_last = None
        self._np_got_dur = False
        self._register_window_class()

    # -------------------- 初始化 --------------------
    @staticmethod
    def _find_mpv():
        """留空时自动查找 mpv.exe: 仅查 PATH 与脚本同级目录"""
        # 1) PATH 中直接可用的 mpv
        p = shutil.which("mpv") or shutil.which("mpv.exe")
        if p:
            return p
        # 2) 脚本同级目录 / 同级 mpv 文件夹
        script_dir = os.path.dirname(os.path.abspath(__file__))
        for c in (os.path.join(script_dir, "mpv.exe"),
                  os.path.join(script_dir, "mpv", "mpv.exe")):
            if os.path.isfile(c):
                return c
        return None

    def _register_window_class(self):
        global _g_wndproc, _g_hinstance, _g_class_registered
        if _g_class_registered:
            return
        _g_wndproc = WNDPROC(_default_wndproc)
        _g_hinstance = kernel32.GetModuleHandleW(None)

        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = _g_wndproc
        wc.hInstance = _g_hinstance
        wc.hbrBackground = None  # 无背景刷, 避免覆盖 mpv 渲染
        wc.lpszClassName = "MpvWallpaperWnd"

        atom = user32.RegisterClassW(ctypes.byref(wc))
        err = kernel32.GetLastError()
        if not atom and err != 1410:  # 1410 = 类已存在
            print(f"[X] RegisterClass 失败 (error={err})")
            return
        _g_class_registered = True

    # -------------------- 前置检查 / 嵌入 --------------------
    def check_prerequisites(self):
        if not self.mpv_path:
            print("[X] 找不到 mpv.exe (已自动搜索 PATH 与脚本同级目录)")
            print("    用 --mpv 参数指定路径, 或把 mpv.exe 放到 PATH")
            return False
        print(f"  mpv: {self.mpv_path}  (自动查找)" if self._auto_mpv
              else f"  mpv: {self.mpv_path}")
        if not find_window_by_class("Progman"):
            print("[X] 找不到 Progman 窗口 (explorer 未运行或桌面已损坏)")
            print("    请重启电脑后再试")
            return False
        return True

    def embed(self):
        """嵌入桌面: 创建为 WorkerW 的 LAYERED 子窗口"""
        progman = find_window_by_class("Progman")
        if not progman:
            print("[X] 找不到 Progman")
            return False

        # 发 0x52c 确保 Progman 下有 WorkerW (已存在则跳过即可, 不重复发)
        result = ctypes.c_size_t(0)
        user32.SendMessageTimeoutW(
            progman, 0x052c, 0, 0, 0, 5000, ctypes.byref(result)
        )

        workerws = find_children_by_class(progman, "WorkerW")
        target_ww = None
        for ww in workerws:
            if user32.IsWindowVisible(ww):
                target_ww = ww
                break
        if not target_ww and workerws:
            target_ww = workerws[0]

        if not target_ww:
            print("[X] 找不到 WorkerW 子窗口, 桌面可能已损坏")
            print("    请重启电脑后再试")
            return False

        sw = user32.GetSystemMetrics(SM_CXSCREEN)
        sh = user32.GetSystemMetrics(SM_CYSCREEN)

        # 关键: WS_EX_LAYERED 必须在创建时带上, 否则 DWM 会静默丢弃子窗口渲染
        hwnd = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_LAYERED,
            "MpvWallpaperWnd",
            "MpvWallpaper",
            WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
            0, 0, sw, sh,
            target_ww,  # 直接指定父窗口 = WorkerW
            None, _g_hinstance, None,
        )
        if not hwnd:
            print(f"[X] 创建窗口失败 (error={kernel32.GetLastError()})")
            return False

        user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        user32.ShowWindow(hwnd, SW_SHOW)
        self.hwnd = hwnd
        return True

    # -------------------- 启动 mpv --------------------
    def launch(self, files, playlist_file=None):
        """启动 mpv 播放 files (路径列表)。只需调用一次。

        - 单文件: --loop=inf 常驻壁纸
        - 多文件 + playlist_file: --playlist=文件 --loop-playlist=inf
              (mpv 内部循环切换, 可承载数万文件, 无 EOF 冻结, 不用 --{ } 分组)
        - 多文件无 playlist_file: 兜底分支, 直接把文件列表当命令行参数
              (当前 GUI/CLI 均不触发此分支; 文件数多时请用 playlist_file, 避免 argv 过长撑爆命令行)
        """
        if not files:
            return False
        panscan = "1.0" if self.fill == "cover" else "0"
        cmd = [
            self.mpv_path,
            f"--wid={self.hwnd}",
            "--no-terminal",
            f"--input-ipc-server={self.PIPE_NAME}",
            f"--vo={self.vo}",
            f"--hwdec={self.hwdec}",
            "--no-osc",
            "--cursor-autohide=no",
            f"--panscan={panscan}",
            "--load-scripts=no",
            "--msg-level=all=v",
        ]
        # 音频: 始终加载音轨, 用 mute 控制开关, 这样运行中也能即时切换
        cmd.insert(2, "--mute=yes" if not self.audio else "--mute=no")

        if len(files) == 1:
            cmd.append("--loop=inf")          # 单视频常驻壁纸
            cmd.append(files[0])
        elif playlist_file:
            cmd.append(f"--playlist={playlist_file}")
            cmd.append("--loop-playlist=inf")
        else:
            cmd.append("--loop-playlist=inf")
            cmd += files

        print(f"  VO={self.vo}  hwdec={self.hwdec}  fill={self.fill} "
              f"(panscan={panscan})  audio={'开' if self.audio else '关'}")
        print(f"  视频数: {len(files)}  (mpv 内部循环切换, 无 EOF 冻结, 无 --{{}} 分组)")

        self.mpv_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

        def _read_output(proc):
            try:
                for line in proc.stdout:
                    line = line.decode("utf-8", errors="replace").rstrip()
                    if line:
                        self._mpv_lines.append(line)
                        print(f"  [mpv] {line}")
            except Exception:
                pass

        threading.Thread(target=_read_output, args=(self.mpv_proc,),
                         daemon=True).start()

        self.pipe_handle = self._connect_pipe()
        if self.pipe_handle and self.on_now_playing:
            # 订阅 path/duration 属性变化, 供 GUI 实时显示"正在播放: 名称 + 时长"
            self._ipc_command(["observe_property", 1, "path"])
            self._ipc_command(["observe_property", 2, "duration"])
            self._ipc_alive = True
            threading.Thread(target=self._ipc_reader, daemon=True).start()
        print("  IPC: 已连接" if self.pipe_handle else "  [!] IPC 未连接 (无法运行时改填充/声音)")
        return True

    def play(self, video_path, loop=True):
        """切换视频 (不重嵌)。loop=True 让新视频 --loop=inf, 始终在播放中切换,
        避开 EOF 冻结 (上次卡死就是因为 EOF + keep-open 后硬切)。"""
        if loop:
            return self._ipc_command(["loadfile", video_path, "replace", "--loop=inf"])
        return self._ipc_command(["loadfile", video_path, "replace"])

    def _connect_pipe(self, retries=40, delay=0.15):
        for _ in range(retries):
            h = kernel32.CreateFileW(
                self.PIPE_NAME,
                GENERIC_READ | GENERIC_WRITE,
                0, None, OPEN_EXISTING, 0, None,
            )
            if h and h != -1 and h != ctypes.c_void_p(-1).value:
                return h
            time.sleep(delay)
        return None

    # -------------------- 运行时控制 (走 IPC, 不重嵌) --------------------
    def _ipc_command(self, cmd_list):
        if not self.pipe_handle:
            return False
        payload = json.dumps({"command": cmd_list}) + "\n"
        data = payload.encode("utf-8")
        written = w.DWORD(0)
        return kernel32.WriteFile(
            self.pipe_handle, data, len(data), ctypes.byref(written), None
        )

    def _ipc_reader(self):
        """后台读取 mpv IPC 管道, 解析 property-change 事件。
        仅当设置了 on_now_playing 回调时才启动, 用于实时显示"正在播放"信息。"""
        try:
            buf = b""
            chunk = ctypes.create_string_buffer(4096)
            rd = w.DWORD(0)
            while self._ipc_alive and self.pipe_handle:
                ok = kernel32.ReadFile(self.pipe_handle, chunk, 4096,
                                       ctypes.byref(rd), None)
                if not ok or rd.value == 0:
                    break
                buf += chunk.raw[:rd.value]
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    self._handle_ipc(msg)
        except Exception:
            pass
        finally:
            self._ipc_alive = False

    def _handle_ipc(self, msg):
        """处理 mpv IPC 的 property-change 事件: 当前文件路径变化 + 时长 -> 回调显示正在播放"""
        if msg.get("event") != "property-change":
            return
        name = msg.get("name")
        data = msg.get("data")
        if name == "path":
            if data and data != self._np_path:
                self._np_path = data
                self._np_got_dur = False   # 等待新文件的时长到达
        elif name == "duration":
            try:
                self._np_duration = float(data) if data is not None else None
            except Exception:
                self._np_duration = None
        # 新文件已加载且拿到时长 -> 触发一次"正在播放"回调
        if (self._np_path and self._np_path != self._np_last
                and self._np_duration and not self._np_got_dur):
            self._np_got_dur = True
            self._np_last = self._np_path
            if self.on_now_playing:
                try:
                    self.on_now_playing(self._np_path, self._np_duration)
                except Exception:
                    pass

    def set_panscan(self, value):
        """运行中改填充: 0=原比例, 1=铺满裁剪"""
        return self._ipc_command(["set_property", "panscan", float(value)])

    def set_mute(self, muted):
        """运行中改声音: True=静音, False=有声"""
        return self._ipc_command(["set_property", "mute", bool(muted)])

    def is_running(self):
        return self.mpv_proc is not None and self.mpv_proc.poll() is None

    # -------------------- 退出 --------------------
    def dump_mpv_output(self, max_lines=30):
        if not self._mpv_lines:
            return
        n = min(max_lines, len(self._mpv_lines))
        print(f"  --- mpv 输出 (最后 {n} 行) ---")
        for ln in self._mpv_lines[-max_lines:]:
            print(f"  [mpv] {ln}")

    def kill_mpv_now(self):
        """立即向 mpv 发送终止信号并关闭 IPC 管道, 立即返回(**不等待**)。

        目的是让音频/视频在点击"停止"的瞬间就停止。
        该方法**不阻塞**, 可在 UI 主线程同步调用。
        真正的等待 + 窗口销毁请交给 terminate_mpv()/stop()。
        """
        if self._stopped:
            return
        self._stopped = True
        self._ipc_alive = False
        print("\n--- 清理：终止 mpv ---")
        if self.pipe_handle:
            kernel32.CloseHandle(self.pipe_handle)
            self.pipe_handle = None
        if self.mpv_proc and self.is_running():
            self.mpv_proc.terminate()  # 强制杀进程, 音频立刻停
            print("  mpv 终止信号已发送")

    def terminate_mpv(self):
        """终止 mpv 进程并等待其彻底退出。

        设计为可放后台线程执行, 不阻塞 UI 线程。
        注意: 这里**不**销毁窗口, 因为窗口销毁(DestroyWindow)必须在创建它的
        同一线程(即 UI 线程)进行, 由 destroy_window() 负责。
        """
        self.kill_mpv_now()
        if self.mpv_proc and self.is_running():
            try:
                self.mpv_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.mpv_proc.kill()
            # 给操作系统一点时间回收 mpv 的子窗口。若立刻 DestroyWindow,
            # Windows 会向尚未消失的 mpv 子窗口发消息并挂起, 导致调用线程卡死。
            for _ in range(20):
                if self.mpv_proc.poll() is not None:
                    break
                time.sleep(0.05)
            print("  mpv 已终止")

    def hide_window(self):
        """立即隐藏桌面窗口, 让壁纸在点"停止"的瞬间消失 (即时视觉反馈)。
        **必须**在创建该窗口的 UI 线程调用。"""
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.ShowWindow(self.hwnd, 0)  # SW_HIDE = 0

    def destroy_window(self):
        """销毁桌面 LAYERED 窗口。**必须**在创建该窗口的 UI 线程调用。"""
        if self.hwnd and user32.IsWindow(self.hwnd):
            print("  销毁窗口")
            user32.DestroyWindow(self.hwnd)
            print("  窗口已销毁")
        self.hwnd = None

    def stop(self):
        """同步退出: 终止 mpv + 销毁窗口。供程序关闭(_on_close)等需要同步等待的场景。"""
        if self._stopped:
            return
        self.terminate_mpv()
        self.destroy_window()
        print("--- 已退出 (未调用 SystemParametersInfo) ---")


# ==================== 主函数 (命令行) ====================

VIDEO_EXTS = ("*.mp4", "*.mkv", "*.avi", "*.webm", "*.mov", "*.flv")


def natural_sort_key(path):
    """自然排序 key: 把文件名里的数字当数值比较, 顺序与 Windows 资源管理器一致
    (例如 '视频2.mp4' 排在 '视频10.mp4' 前面)。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", path)]


def write_playlist_file(paths, path=None):
    """把路径列表写成 mpv 播放列表文件 (.m3u8, UTF-8), 供 --playlist 使用。
    可承载任意数量文件 (不受命令行长度限制), 路径一行一个即可。返回文件路径。"""
    if path is None:
        import tempfile
        fd, path = tempfile.mkstemp(prefix="mpvwp_", suffix=".m3u8")
        os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for p in paths:
            f.write(p.replace("\\", "/") + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description="mpv 视频壁纸 (Win11 26200 兼容)")
    parser.add_argument("path", help="视频文件或目录")
    parser.add_argument("--vo", default="direct3d",
                        help="渲染器: direct3d / gpu / auto (默认 direct3d)")
    parser.add_argument("--hwdec", default="auto-copy",
                        help="硬件解码: auto-copy / auto / no (默认 auto-copy)")
    parser.add_argument("--fill", default="cover", choices=["cover", "contain"],
                        help="cover=铺满裁剪, contain=原视频比例 (默认 cover)")
    parser.add_argument("--audio", action="store_true", default=False,
                        help="开启声音 (默认静音)")
    parser.add_argument("--interval", type=int, default=10,
                        help="固定间隔模式: 每视频最大播放秒数 (默认 10)")
    parser.add_argument("--duration", action="store_true", default=False,
                        help="按视频时长播放(不截断); 默认按 --interval 固定间隔切换")
    parser.add_argument("--mpv", default=None, help="mpv.exe 路径")
    args = parser.parse_args()

    # 收集视频
    if os.path.isfile(args.path):
        videos = [os.path.abspath(args.path)]
        single_mode = True
    elif os.path.isdir(args.path):
        videos = []
        for ext in VIDEO_EXTS:
            videos.extend(glob.glob(os.path.join(args.path, ext)))
        if not videos:
            print(f"[X] 目录中没有视频文件: {args.path}")
            return
        random.shuffle(videos)   # 首轮也打乱, 避免第一个视频永远固定
        single_mode = False
    else:
        print(f"[X] 路径不存在: {args.path}")
        return

    print(f"视频数量: {len(videos)}")
    if single_mode:
        print("模式: 单视频常驻 (Ctrl+C 退出)")
    elif args.duration:
        print("模式: 目录循环 - 按视频时长 (Ctrl+C 退出)")
    else:
        print(f"模式: 目录循环 - 固定间隔 {args.interval}s (Ctrl+C 退出)")

    # 单视频 → --loop=inf 常驻; 多视频 → 播放列表文件(时长) 或 定时 loadfile(固定间隔)
    player = WallpaperPlayer(
        mpv_path=args.mpv, vo=args.vo, hwdec=args.hwdec,
        fill=args.fill, audio=args.audio,
    )

    print("\n[1/3] 前置检查...")
    if not player.check_prerequisites():
        return
    print("  mpv / Progman: 全部 OK")

    print("\n[2/3] 嵌入桌面...")
    if not player.embed():
        player.stop()
        return
    print(f"  窗口已嵌入桌面 (hwnd={player.hwnd})")

    print("\n[3/3] 启动 mpv...")
    m3u_path = None
    if single_mode:
        if not player.launch([videos[0]]):
            player.stop()
            return
    elif args.duration:
        m3u_path = write_playlist_file(videos)
        if not player.launch(videos, playlist_file=m3u_path):
            player.stop()
            return
    else:
        # 固定间隔: 首视频 --loop=inf 常驻, 定时器 loadfile 切换 (播放中途切, 避开 EOF 冻结)
        if not player.launch([videos[0]]):
            player.stop()
            return
    time.sleep(2)

    print("\n播放中... (Ctrl+C 退出)\n")
    try:
        if single_mode or args.duration:
            while player.is_running():
                time.sleep(1)
        else:
            idx = 1
            n = len(videos)
            while player.is_running():
                player.play(videos[idx % n], loop=True)   # 播放中途切换, 不踩 EOF
                idx += 1
                for _ in range(max(1, args.interval) * 10):
                    if not player.is_running():
                        break
                    time.sleep(0.1)
                if not player.is_running():
                    break
    except KeyboardInterrupt:
        print("\n[Ctrl+C]")
    finally:
        player.stop()
        if m3u_path and os.path.isfile(m3u_path):
            try:
                os.remove(m3u_path)
            except Exception:
                pass

    if not player.is_running():
        print("\n[mpv 已自行退出]")
        if player.mpv_proc is not None:
            print(f"  退出码: {player.mpv_proc.returncode}")
        player.dump_mpv_output()
        print("\n可尝试:")
        print(f'  python "{sys.argv[0]}" "{args.path}" --vo gpu --mpv "{args.mpv}"')
        print(f'  python "{sys.argv[0]}" "{args.path}" --vo direct3d --hwdec no --mpv "{args.mpv}"')


if __name__ == "__main__":
    main()
