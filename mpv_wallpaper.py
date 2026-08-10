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
  python mpv_wallpaper.py "D:\\videos" --fill contain --rounds 0 --mpv "C:\\mpv\\mpv.exe"
"""

import ctypes
import ctypes.wintypes as w
import subprocess
import threading
import time
import os
import json
import glob
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


def find_children_by_class(parent_hwnd, cls_name):
    """找父窗口下指定类名的所有子窗口"""
    found = []

    def _cb(hwnd, lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == cls_name:
            found.append(hwnd)
        return True

    user32.EnumChildWindows(parent_hwnd, WNDENUMPROC(_cb), 0)
    return found


def _default_wndproc(hwnd, msg, wparam, lparam):
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ==================== 播放器 ====================

class WallpaperPlayer:
    """
    mpv 视频壁纸播放器 (Win11 26200 兼容)

    嵌入策略: 窗口创建时即设为 Progman 下 WorkerW 的 LAYERED 子窗口。
    退出: 终止 mpv -> CloseHandle -> DestroyWindow, 不损坏桌面状态机。
    运行中切视频/改填充/改声音全部走 mpv IPC, 不重嵌、不重启。
    """

    PIPE_NAME = r"\\.\pipe\mpvwallpaper"

    def __init__(self, mpv_path=None, vo="direct3d", hwdec="auto-copy",
                 fill="cover", audio=True, loop=True):
        self.vo = vo
        self.hwdec = hwdec
        self.fill = fill
        self.audio = audio          # True=有声(默认不静音) / False=静音
        self.loop = loop            # True=单曲循环(壁纸常驻) / False=播一次靠脚本切换(轮播)
        self._auto_mpv = mpv_path is None   # 是否走自动查找
        self.mpv_path = mpv_path or self._find_mpv()
        self.hwnd = None
        self.mpv_proc = None
        self.pipe_handle = None
        self._stopped = False
        self._mpv_lines = []        # mpv 输出缓冲, 异常时回吐
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
    def launch_mpv(self, video_path):
        """启动 mpv, 渲染到已嵌入的窗口 (仅需调用一次)"""
        panscan = "1.0" if self.fill == "cover" else "0"
        loop_flag = "--loop=inf" if self.loop else "--loop=no"
        cmd = [
            self.mpv_path,
            f"--wid={self.hwnd}",
            loop_flag,
            "--keep-open",
            "--no-terminal",
            f"--input-ipc-server={self.PIPE_NAME}",
            f"--vo={self.vo}",
            f"--hwdec={self.hwdec}",
            "--no-osc",
            "--cursor-autohide=no",
            f"--panscan={panscan}",
            "--load-scripts=no",
            "--msg-level=all=v",
            video_path,
        ]
        # 音频: 始终加载音轨, 用 mute 控制开关, 这样运行中也能即时切换
        cmd.insert(2, "--mute=yes" if not self.audio else "--mute=no")

        print(f"  VO={self.vo}  hwdec={self.hwdec}  fill={self.fill} "
              f"(panscan={panscan})  audio={'开' if self.audio else '关'}")
        print(f"  视频: {os.path.basename(video_path)}")

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
        print("  IPC: 已连接" if self.pipe_handle else "  [!] IPC 未连接 (无法切换视频)")
        return True

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

    def play(self, video_path):
        """切换视频 (不重新嵌入)"""
        return self._ipc_command(["loadfile", video_path, "replace"])

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

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        print("\n--- 清理 ---")
        if self.pipe_handle:
            kernel32.CloseHandle(self.pipe_handle)
            self.pipe_handle = None
        if self.mpv_proc and self.is_running():
            self.mpv_proc.terminate()
            try:
                self.mpv_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.mpv_proc.kill()
            print("  mpv 已终止")
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.DestroyWindow(self.hwnd)
            print("  窗口已销毁")
        self.hwnd = None
        print("--- 已退出 (未调用 SystemParametersInfo) ---")


# ==================== 主函数 (命令行) ====================

VIDEO_EXTS = ("*.mp4", "*.mkv", "*.avi", "*.webm", "*.mov", "*.flv")


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
    parser.add_argument("--rounds", type=int, default=0,
                        help="目录循环轮数, 0=无限循环 (默认 0)")
    parser.add_argument("--interval", type=int, default=10,
                        help="每视频播放秒数 (默认 10)")
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
        print("模式: 单视频 (Ctrl+C 退出)")
    else:
        mode = "无限循环" if args.rounds == 0 else f"{args.rounds} 轮"
        print(f"模式: 目录循环 ({mode}, 每视频 {args.interval}s, Ctrl+C 退出)")

    # 单视频 / 仅 1 个视频的目录 → 循环播放(常驻壁纸);
    # 多视频目录 → 播一次靠脚本切换(避免回放开头几秒)
    loop = single_mode or len(videos) == 1
    player = WallpaperPlayer(
        mpv_path=args.mpv, vo=args.vo, hwdec=args.hwdec,
        fill=args.fill, audio=args.audio, loop=loop,
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
    if not player.launch_mpv(videos[0]):
        player.stop()
        return
    time.sleep(2)

    if single_mode:
        print("\n播放中... (Ctrl+C 退出)\n")
        try:
            while player.is_running():
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[Ctrl+C]")
        finally:
            player.stop()
        if not player.is_running():
            print("\n[mpv 已自行退出]")
            if player.mpv_proc is not None:
                print(f"  退出码: {player.mpv_proc.returncode}")
            player.dump_mpv_output()
            print("\n可尝试:")
            print(f'  python "{sys.argv[0]}" "{args.path}" --vo gpu --mpv "{args.mpv}"')
            print(f'  python "{sys.argv[0]}" "{args.path}" --vo direct3d --hwdec no --mpv "{args.mpv}"')
    else:
        print("\n循环播放中... (Ctrl+C 退出)\n")
        round_num = 0
        try:
            while args.rounds == 0 or round_num < args.rounds:
                round_num += 1
                if args.rounds != 0:
                    print(f"===== 第 {round_num}/{args.rounds} 轮 =====")
                random.shuffle(videos)
                for i, video in enumerate(videos):
                    if not player.is_running():
                        print("[!] mpv 已退出, 停止循环")
                        break
                    player.play(video)
                    print(f"  [{i+1}/{len(videos)}] {os.path.basename(video)}")
                    time.sleep(args.interval)
                else:
                    continue
                break
            else:
                if args.rounds != 0:
                    print("\n所有轮次播放完毕")
        except KeyboardInterrupt:
            print("\n[Ctrl+C]")
        finally:
            player.stop()


if __name__ == "__main__":
    main()
