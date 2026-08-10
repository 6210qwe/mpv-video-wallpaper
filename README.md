# mpv-video-wallpaper

一个能在 **Windows 11 25H2（OS Build 26200）** 上稳定运行的视频桌面壁纸工具。
基于 [mpv](https://mpv.io/) 播放器 + 原生 Win32 窗口嵌入技术，支持单视频、目录循环、
填充方式（原比例 / 铺满裁剪）、声音开关，并提供简洁的 tkinter 图形界面。

> 核心目标：**频繁开关壁纸也不会把桌面搞崩**。

---

## 一、为什么会有这个项目

Windows 11 25H2（Build 26200）改了桌面窗口布局，导致旧式的桌面视频嵌入方式直接失效，因此需要一个针对新系统适配的方案。

### 1.1 根因：Win11 又改了桌面窗口布局

经过实测与排查，根因不在某个具体软件，而在 **Windows 11 25H2 的桌面架构变更**：

1. **Win11 26200 的 Progman 改用了 raised desktop 模式**（窗口带 `WS_EX_NOREDIRECTIONBITMAP` 样式）。
   - 经典的 `SetParent(视频窗口, WorkerW)` 技术在此环境下被 **DWM 合成器静默丢弃**——
     `SetParent` 返回“成功”（非零值），但 `GetParent` 验证发现窗口根本没真正挂上去。

2. **退出时反复调用 `SystemParametersInfo(SPI_SETDESKWALLPAPER)` 刷新桌面**，会逐步损坏 explorer 的桌面合成状态机；
   状态机一旦损坏，只能重启电脑恢复。

这就是桌面视频嵌入在 Win11 26200 上失灵的原因——不是软件的问题，是系统层面的窗口布局被改了。

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
- **切视频不靠 `--{ }` 分组、也不靠脚本在 EOF 后硬切**：
  - **单视频**：`--loop=inf` 直接常驻，全程零切换；
  - **按视频时长（多文件）**：把路径写成 `.m3u8` 播放列表文件交给 mpv（`--playlist` + `--loop-playlist=inf`），每个视频完整播完再接力，文件数无上限；
  - **固定时长**：当前视频 `--loop=inf` 一直在播放中循环，用定时器 `loadfile` 在「播放中途」切下一个，根本不会走到 EOF 冻结。
- **运行中改填充 / 改声音走 mpv IPC**（`set_property`），不重新嵌入桌面，也不重建播放列表。

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
python mpv_wallpaper.py "D:\videos" --fill contain --interval 10
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
| `--interval` | `10` | 固定时长模式下每视频最大播放秒数 |
| `--duration` | 关闭 | 加此参数改为「按视频时长」模式（每个视频完整播完再切） |

> 提示：桌面出现黑屏时，尝试 `--vo gpu`；mpv 秒退时，尝试 `--hwdec no`。

---

## 五、图形界面使用

```bash
python mpv_wallpaper_gui.py
```

界面字段：

- **视频目录**：选择存放视频文件的文件夹（支持 mp4/mkv/avi/webm/mov/flv，可勾选「包含子文件夹」）。
- **单个视频（循环播放）**：勾选后「浏览」改为选择单个视频文件并无限循环（全程零切换、最稳）；勾选时「包含子文件夹 / 播放顺序 / 切换方式 / 固定间隔」自动禁用。
- **mpv.exe 路径**：留空自动查找（PATH / 脚本同级目录）；也可点「浏览」指定。
- **播放顺序**：随机 / 顺序。
- **切换方式**：固定时长（每视频播固定秒数）/ 按视频时长（每个视频完整播完再切）。
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
2. **运行中零重嵌**：按视频时长模式由 mpv 播放列表内部切换（`--loop-playlist=inf`），固定时长模式由定时器 `loadfile` 在播放中途切换（当前视频 `--loop=inf` 永不 EOF）；改填充 / 改声音走 mpv IPC（`set_property`），
   不创建也不销毁嵌入窗口，从根本上消除“父窗口失败”的触发条件。
3. **一次性嵌入**：整个生命周期只在启动时嵌入一次桌面，之后全程复用同一个窗口。

---

## 七、已知限制 / 注意事项

- **多显示器**：当前嵌入主显示器（`Progman` 下的第一个可见 `WorkerW`），未做多屏分发。
- **桌面状态已损坏时**：若此前因其它壁纸软件把桌面搞崩了，
  本程序也可能无法嵌入——此时**重启一次电脑**即可恢复干净环境，之后本程序可正常使用。
- **mpv 路径**：自动查找仅覆盖 `PATH` 与脚本同级目录；放在别处请显式 `--mpv` 指定。
- 仅依赖标准库 + `tkinter`，无需 `pip install` 任何第三方包。

---

## 八、打包成单文件 exe（可选）

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name MpvWallpaper mpv_wallpaper_gui.py
```

生成的 `dist/MpvWallpaper.exe` 需与 `mpv_wallpaper.py` 同目录（或把 mpv.exe 一并打包/放到 PATH）。

