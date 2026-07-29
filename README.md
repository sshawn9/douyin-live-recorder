# douyin-live-recorder

Linux 下的抖音直播自动录制器，以及一组基于 FFmpeg 的视频文件工具。

录制器接收一个抖音用户 ID，持续刷新对应的直播页。检测到普通直播时，它直接保存页面提供的 HLS 视频流，不录屏、不重新编码；检测到 PK、连麦或多人直播时暂停录制，恢复普通直播后继续写入新的分片。一次程序运行产生的所有有效分片会在退出时无损合并为一个 MKV 文件。

## 运行要求

- Linux
- Python 3.14 和 uv，或者 Nix
- 使用 uv 运行时，系统 `PATH` 中必须有 `ffmpeg` 和 `ffprobe`

## 使用 Nix

Flake 已包含 Python 运行环境和 FFmpeg，可以直接运行：

```bash
nix run github:sshawn9/douyin-live-recorder -- USER_ID
```

运行视频工具：

```bash
nix run github:sshawn9/douyin-live-recorder#video-tools -- info VIDEO
```

## 使用 uv

```bash
uv sync
uv run douyin-live-recorder USER_ID
```

程序只有一个命令行参数：直播页 URL `https://live.douyin.com/USER_ID` 中的 `USER_ID`。

## 录制流程

程序启动后会并行运行两个任务：

1. 监控任务请求直播页并更新当前可录制的 HLS 地址。
2. 录制任务根据该地址启动或停止 FFmpeg。

当前轮询策略：

- 未开播：约 60 秒后重试。
- 正常直播：约 20 秒刷新一次状态。
- HTTP 请求失败：约 5 秒后重试。
- 页面结构无法解析：约 60 秒后重试。
- 所有等待时间包含 ±10% 的随机抖动。

直播时，程序读取页面状态中的 `mainCameraInfo.h265Stream.hls_pull_url`。当前没有清晰度选择、协议切换或候选线路切换。

FFmpeg 使用流复制模式保存视频和音频：

```text
-map 0:v:0 -map 0:a:0 -c copy -f mpegts
```

不会重新编码，因此录制过程不会主动降低视频画质。FFmpeg 会自动重连常见的临时网络错误；如果进程仍然退出，录制任务会重新启动它并创建新分片。

## 互动检测

程序只录制被判断为普通直播的部分。以下任一状态存在时，当前录制会停止：

- PK 中、惩罚阶段或正在切换到 PK
- `linker_map` 非空
- `linker_play_modes` 非空
- `function_type` 非空

互动结束后继续录制新的 TS 分片。该判断依赖抖音直播页当前的内部数据结构，并非公开、稳定的 API；抖音页面改版可能导致解析失败或互动状态误判。

当前程序不支持 Cookie、代理或浏览器登录态。公开直播页必须能够直接返回所需状态和视频流地址。

## 停止与恢复

按 `Ctrl-C` 或向进程发送 `SIGTERM` 会设置停止信号。录制任务随后按以下顺序关闭 FFmpeg：

1. 向 FFmpeg 标准输入发送 `q`，最多等待 15 秒。
2. 仍未退出则发送 `SIGTERM`，再等待 5 秒。
3. 最后才发送 `SIGKILL`。

正常停止能够让当前 MPEG-TS 分片完成收尾。程序随后删除空分片，使用 `ffprobe` 保留同时包含视频和音频的有效分片，并通过 `-c copy` 合并为 `recording.mkv`。合并不会删除原始 TS 分片。

同一用户同时只能运行一个实例。锁文件位于：

```text
recordings/USER_ID/.recorder.lock
```

锁文件退出后仍会保留，但文件本身不会阻止下次运行；真正的排他锁只在进程运行期间有效。

## 输出文件

第一次开始录制时创建目录：

```text
recordings/USER_ID/YYYYMMDD-HHMMSS-ffffff/
├── part-0001.ts
├── part-0002.ts
├── ...
├── segments.ffconcat
└── recording.mkv
```

一次程序运行期间，即使经历互动、断流、下播后重新开播，也会继续使用同一个录制目录，并在退出时合并为同一个 `recording.mkv`。如果没有有效分片，则不会生成合并文件。

## video-tools

`video-tools` 接受 FFmpeg/ffprobe 能读取的媒体文件，并且不会假定输入来自本录制器。

### 查看媒体信息和检查完整性

```bash
uv run video-tools info VIDEO
uv run video-tools info VIDEO --check quick
uv run video-tools info VIDEO --check full
```

检查模式：

- `quick`：只读取容器和流元数据。
- `balanced`：默认模式；读取全部压缩数据包，并解码最多 12 个均匀分布的样本，总计最多 60 秒。
- `full`：顺序解码完整的视频和音频流，最慢但覆盖最完整。

检查同时比较容器声明时长与实际数据包时间线。成功退出码为 `0`；确认存在错误或时间线不匹配时为 `2`；只有跳转解码样本报错、但完整数据包扫描正常时为 `3`，表示结果不确定，应使用 `--check full` 复查。

### 尝试修复损坏文件

```bash
uv run video-tools repair INPUT
uv run video-tools repair INPUT OUTPUT
uv run video-tools repair INPUT --check full
```

未指定输出文件时自动生成：

```text
NAME.repaired.EXT
```

修复操作会丢弃被标记为损坏的数据包、重新生成时间戳并重新封装现有流，不会重新编码。它能够抢救仍可读取的数据，但不能重建已经丢失或损坏的画面内容。即使验证结果仍为 `partial` 或 `inconclusive`，抢救出的文件也会保留。

### 无损删除片段

```bash
uv run video-tools cut INPUT --remove-start 30
uv run video-tools cut INPUT --remove-end 02:30
uv run video-tools cut INPUT --remove 10:00..12:30
uv run video-tools cut INPUT --remove 10:00..12:30 --remove 20:00..21:00
uv run video-tools cut INPUT OUTPUT --remove-start 30 --remove-end 60
```

时间支持秒、`MM:SS` 和 `HH:MM:SS`。未指定输出文件时生成 `NAME.cut.EXT`。

剪切会把删除范围向相邻关键帧扩展，然后复制保留的视频和音频数据。因此切点可能比请求值多删除一小段，但不会重新编码，也能避免从依赖帧开始造成的衔接解码错误。

### 拼接兼容文件

```bash
uv run video-tools concat PART1 PART2 PART3 --output OUTPUT
```

拼接使用 FFmpeg concat demuxer 和流复制。输入文件需要具有兼容的容器时间线、流布局和编码参数，否则 FFmpeg 可能拒绝拼接或产生不可用结果。

### 精简流和元数据

```bash
uv run video-tools compact INPUT OUTPUT
```

`compact` 只保留第一条视频流和可选的第一条音频流，删除其余流和文件元数据，不重新编码。

所有会写入指定目标文件的命令默认拒绝覆盖已有文件；确认需要覆盖时使用 `--overwrite`。

使用 Nix 时，将以上命令中的 `uv run video-tools` 替换为：

```bash
nix run github:sshawn9/douyin-live-recorder#video-tools --
```

## 代码结构

```text
src/douyin_live_recorder/
├── cli.py          CLI 入口和 FFmpeg 可用性检查
├── data.py         单个用户的共享运行状态
├── monitor.py      当前使用的直播页监控逻辑
├── monitor2.py     未接入运行流程的内部接口实验实现
├── recording.py    FFmpeg 生命周期、分片和最终合并
├── run.py          文件锁、信号和两个异步任务的组织
└── runtime.py      可响应停止信号的随机抖动等待

src/video_tools/
├── cli.py          video-tools 命令入口
├── ffmpeg.py       FFmpeg/ffprobe 公共操作
├── inspection.py   媒体信息和完整性检查
├── repair.py       损坏数据抢救
├── cutting.py      关键帧对齐剪切
└── editing.py      拼接和流精简
```

## 开发检查

项目仍使用 uv 管理开发环境：

```bash
uv sync --group dev
uv run --group dev ruff check src
uv run --group dev pyright src
```

## 限制

- 只支持 Linux；运行流程使用 `fcntl.flock` 和 Unix 信号。
- 依赖抖音网页内部状态，页面结构随时可能变化。
- 当前固定读取一种 HLS 字段，不比较多种清晰度或协议。
- 互动检测是启发式判断，不能保证没有漏判或误判。
- 流复制只能保留 CDN 已提供的画质，不能恢复直播源中已经损失的细节。
- `repair`、`cut` 和 `concat` 受 FFmpeg 对具体容器及编码格式的限制。

请只录制和处理你有权保存的内容，并遵守适用的平台规则、著作权和隐私要求。
