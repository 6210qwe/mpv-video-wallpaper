# mpv-video-wallpaper

一个能在 **Windows 11 25H2（OS Build 26200）** 上稳定运行的视频桌面壁纸工具。
基于 [mpv](https://mpv.io/) 播放器 + 原生 Win32 窗口嵌入技术，支持单视频、目录循环、
填充方式（原比例 / 铺满裁剪）、声音开关，并提供简洁的 tkinter 图形界面。

> 核心目标：**频繁开关壁纸也不会把桌面搞崩**。

---

## 一、为什么会有这个项目

我有一台新电脑（Windows 11 25H2，Build 26200），想用视频当桌面壁纸，并且**有频繁关闭 / 重启壁纸软件的需求**（因为电脑上长期跑着别的任务，不想因为壁纸软件出问题而被迫重启，那样太耽误事）。

于是我试了市面上常见的方案，结果都翻车了。

### 1.1 踩过的坑

| 软件 | 表现 |
|------|------|
| **ChromaFlux** (`ChromaFluxService.exe`) | 正常用没问题；但只要反复「启动 → 关闭 → 再启动」循环约 **5~6 次**，就会持续弹出**“设置桌面父窗口失败”**，此后无论如何重启该软件都无效，**必须重启电脑**才能恢复。 |
| **Lively Wallpaper** | 同样的现象——反复启停 5 次左右就失效。 |
| **Wallpaper Engine 免费版** | 功能受限，不满足需求；付费版又担心付完钱也是同样的坑。 |

### 1.2 排查结论

经过大量实测（PowerShell / Python+ctypes 各种脚本），逐步定位到根因：

1. **这不是某个软件的 bug，是 Windows 11 25H2 桌面架构层面的变更。**
   - 同一份 ChromaFlux 在 **Windows 10** 上反复启停几十次完全正常；
   - 在 **Windows 11 Build 26200** 上必现；
   - **Lively Wallpaper 在 26200 上同样出问题**，说明是系统级的。

2. **Win11 26200 的 Progman 改用了 raised desktop 模式**（窗口带 `WS_EX_NOREDIRECTIONBITMAP` 样式）。
   - 经典的 `SetParent(视频窗口, WorkerW)` 技术在此环境下被 **DWM 合成器静默丢弃**——
     `SetParent` 返回“成功”（非零值），但 `GetParent` 验证发现窗口根本没真正挂上去。

3. **退出时反复调用 `SystemParametersInfo(SPI_SETDESKWALLPAPER)` 刷新桌面**，会逐步损坏 explorer 的桌面合成状态机；
   状态机一旦损坏，只能重启电脑恢复。

> 参考项目（方案验证来源）：
> - Gitee: [zip-ping/bsod-simulation-app](https://gitee.com/zip-ping/bsod-simulation-app) —— Win10 22H2 到 Win11 26200 通用的桌面层注入方案，明确写了「不加 `WS_EX_LAYERED`，跨进程非 LAYERED 子窗口会被 DWM 静默丢弃」。

---

## 二、技术方案（为什么它能用）

针对上面的根因，本项目采用与经典方案不同的嵌入策略：

```
经典方案（Win11 26200 失效）          本项目方案（已验证可用）
-----------------------------------   -----------------------------------
1. 创建普通顶层窗口                   1. 创建窗口时即带 WS_EX_LAYERED
2. 发 0x052c 让 Progman 建 WorkerW       + SetLayeredWindowAttributes(255)
3. SetParent(窗口, WorkerW)  ← 被丢弃   2. 直接以 WorkerW 为父窗口创建子窗口
4. 退出时 SystemParametersInfo 刷新     3. 退出时只 DestroyWindow + 终止 mpv
                                       （绝不调 SystemParametersInfo）
```

关键点：

- **`WS_EX_LAYERED` 是让 DWM 在 raised desktop 下呈现子窗口的唯一可靠方式。**
  实测：不加 LAYERED 时窗口能挂上 WorkerW 但黑屏；加上后视频正常出画面。
- **创建时即为 WorkerW 子窗口**，不走 `SetParent`，绕开“被静默丢弃”的坑。
- **退出只销毁窗口 + 终止 mpv 进程**，不调用 `SystemParametersInfo`，因此不会累积损坏桌面状态——这正是「能频繁开关」的根本原因。
- **运行中切视频 / 改填充 / 改声音全部走 mpv IPC**，不重新嵌入桌面，从源头上杜绝“父窗口失败”。

### 2.1 兼容性说明

| 系统 | 状态 |
|------|------|
| Windows 11 25H2（Build 26200） | ✅ 已实测可用 |
| Windows 10 | ✅ 经典方案本就可用，本方案同样适用 |
| 其他 Win11 版本 | 未逐一测试；原理上 raised desktop 的版本都需要 LAYERED，故应通用 |

---

## 三、环境要求

- Windows 10 / 11（推荐 11 25H2 及以上）
- Python **3.8+**（GUI 依赖标准库 `tkinter`，通常随 Python 安装包自带）
- [mpv 播放器](https://mpv.io/installation/)（Windows 下载 portable 版解压即可）

### 3.1 安装 mpv

1. 到 https://mpv.io/installation/ 下载 Windows 版（如 `mpv-x86_64-*.7z`）；
2. 解压得到 `mpv.exe`；
3. 二选一让程序能找到它：
   - **放到 PATH**（推荐）：把 `mpv.exe` 所在目录加入系统环境变量 `PATH`；
   - **放到脚本同级目录**：把 `mpv.exe`（或含 `mpv.exe` 的 `mpv/` 文件夹）放在本项目目录里。

> 程序启动时会自动查找 mpv：先查 `PATH`，再查**脚本同级目录**（`mpv.exe` 或 `mpv/mpv.exe`）。
> 找不到时会提示你用 `--mpv` 参数显式指定路径。

---

## 四、命令行使用

### 4.1 单视频（铺满裁剪，默认）

```bash
python mpv_wallpaper.py "D:\video.mp4"
```

### 4.2 指定 mpv 路径（自动查找失败时）

```bash
python mpv_wallpaper.py "D:\video.mp4" --mpv "C:\mpv\mpv.exe"
```

### 4.3 目录循环（无限轮播，原比例，每视频 10 秒）

```bash
python mpv_wallpaper.py "D:\videos" --fill contain --rounds 0 --interval 10
```

### 4.4 开启声音

```bash
python mpv_wallpaper.py "D:\video.mp4" --audio
```

### 4.5 参数一览

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `path` | （必填） | 视频文件**或**目录路径 |
| `--mpv` | 自动查找 | mpv.exe 路径（留空则查 PATH + 脚本同级目录） |
| `--fill` | `cover` | `cover`=铺满裁剪；`contain`=原视频比例（黑边不裁切） |
| `--audio` | 关闭 | 加此参数才出声（默认静音） |
| `--vo` | `direct3d` | 渲染器：`direct3d` / `gpu` / `auto` |
| `--hwdec` | `auto-copy` | 硬件解码：`auto-copy` / `auto` / `no` |
| `--rounds` | `0` | 目录循环轮数，`0`=无限循环 |
| `--interval` | `10` | 每视频播放秒数（目录模式） |

> 提示：桌面出现黑屏时，尝试 `--vo gpu`；mpv 秒退时，尝试 `--hwdec no`。

---

## 五、图形界面使用

```bash
python mpv_wallpaper_gui.py
```

界面字段：

- **视频目录**：选择存放 mp4 的文件夹（可勾选「包含子文件夹」）。
- **mpv.exe 路径**：留空自动查找（PATH / 脚本同级目录）；也可点「浏览」指定。
- **播放顺序**：随机 / 顺序。
- **切换方式**：固定时长 / 按视频时长（自动读取 mp4 时长，无需 ffmpeg）。
- **固定间隔**：「固定时长」模式下每视频播放秒数。
- **填充方式**：原比例 / 铺满裁剪。
- **开启声音**：勾选后出声（默认静音）。

操作：**▶ 开始** 启动壁纸；运行中改「填充方式 / 声音」会**即时生效且不重嵌桌面**；
**■ 停止** 干净卸载桌面层（不损坏系统）。关闭窗口也会自动清理。

配置会自动保存到
`%APPDATA%\MpvWallpaper\config.json`，下次打开无需重选。

---

## 六、为什么能“频繁开关不崩”

1. **退出路径干净**：只 `DestroyWindow` + 终止 mpv，**绝不调用 `SystemParametersInfo`**，
   因而不会损坏 explorer 的桌面合成状态机。
2. **运行中零重嵌**：切换视频 / 改填充 / 改声音全部走 mpv IPC（`loadfile` / `set_property`），
   不创建也不销毁嵌入窗口，从根本上消除“父窗口失败”的触发条件。
3. **一次性嵌入**：整个生命周期只在启动时嵌入一次桌面，之后全程复用同一个窗口。

---

## 七、已知限制 / 注意事项

- **多显示器**：当前嵌入主显示器（`Progman` 下的第一个可见 `WorkerW`），未做多屏分发。
- **桌面状态已损坏时**：若此前因其他软件（ChromaFlux / Lively）把桌面搞崩了，
  本程序也可能无法嵌入——此时**重启一次电脑**即可恢复干净环境，之后本程序可正常使用。
- **mpv 路径**：自动查找仅覆盖 `PATH` 与脚本同级目录；放在别处请显式 `--mpv` 指定。
- 仅依赖标准库 + `tkinter`，无需 `pip install` 任何第三方包。

---

## 八、项目结构

```
mpv-video-wallpaper/
├── mpv_wallpaper.py     # 播放核心：Win32 嵌入 + mpv 控制（可作为模块 import）
├── mpv_wallpaper_gui.py # 图形界面：控制上面的播放核心
└── README.md            # 本文档
```

`mpv_wallpaper_gui.py` 通过 `import mpv_wallpaper` 调用 `WallpaperPlayer`，
两个文件需放在同一目录。

---

## 九、打包成单文件 exe（可选）

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name MpvWallpaper mpv_wallpaper_gui.py
```

生成的 `dist/MpvWallpaper.exe` 需与 `mpv_wallpaper.py` 同目录（或把 mpv.exe 一并打包/放到 PATH）。

---

## 十、常见问题

**Q: 提示“找不到 mpv.exe”？**
A: 把 mpv.exe 加入 PATH，或放到脚本同级目录（`mpv.exe` 或 `mpv/mpv.exe`），
   或用 `--mpv` 显式指定。

**Q: 桌面黑屏但有声音？**
A: 渲染器兼容问题，试 `--vo gpu`（命令行）或在 GUI 改渲染器后重试。

**Q: 之前被别的壁纸软件搞崩了，现在本程序也失效？**
A: 桌面合成状态机已被损坏，重启一次电脑即可。本程序本身不会造成这种损坏。

**Q: 这和 ChromaFlux / Lively 是什么关系？**
A: 没有关系。本项目是独立重写、专为 Win11 26200 的 raised desktop 架构设计，
   不依赖也更稳定。发现的问题已整理可作为 ChromaFlux 作者的修复参考。
