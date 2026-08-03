# douyin-live-recorder

Linux 下的抖音直播自动录制器。

录制器接收一个抖音用户 ID。没有可用 HLS 地址时，它访问对应的直播页；取得地址后只刷新 HLS 播放列表并下载原始 MPEG-TS 分片。当前地址失败、结束或停止产生新分片时，程序才重新访问直播页获取地址。一次程序运行产生的所有分片会在退出时由 FFmpeg 无损合并为一个 MKV 文件。

## 运行要求

- Linux
- Python 3.14 和 uv，或者 Nix
- 使用 uv 运行时，系统 `PATH` 中必须有 `ffmpeg`

## 使用 Nix

Flake 已包含 Python 运行环境和 FFmpeg，可以直接运行：

```bash
nix run github:sshawn9/douyin-live-recorder -- USER_ID
```

Flake 打包了全部 Python 依赖，并将 `ffmpeg-headless` 作为运行时依赖加入录制器的 `PATH`，不依赖系统安装的 FFmpeg。

## 使用 uv

```bash
uv sync
uv run douyin-live-recorder USER_ID
```

这种方式使用 uv 管理 Python 依赖，并直接查找系统 `PATH` 中的 `ffmpeg`。

## 使用 Just

```bash
just run-uv USER_ID
just run-nix USER_ID
```

两个 recipe 分别对应上述 uv 和 Nix 运行方式。

程序只有一个命令行参数：直播页 URL `https://live.douyin.com/USER_ID` 中的 `USER_ID`。

## 录制流程

程序按照以下顺序运行：

1. 取得单用户锁并创建本次运行的录制目录。
2. 同时启动播放列表刷新任务、三个分片下载 worker 和失败分片重试任务。
3. 刷新任务没有 HLS 地址时请求直播页，取得最高画质地址后只刷新该地址的 HLS 播放列表。
4. 下载池中的三个 worker 独立消费刷新任务产生的分片队列，不参与地址解析。
5. 播放列表连续请求失败、出现 `EXT-X-ENDLIST`，或超过 `max(2 秒, 2.5 × TARGETDURATION)` 没有推进时，刷新任务重新请求直播页获取地址。

直播页重试策略：

- 未开播：约 60 秒后重试。
- HTTP 请求失败：约 5 秒后重试。
- 页面结构无法解析：约 60 秒后重试。
- 直播中但暂时没有 HLS 地址：约 5 秒后重试。
- 所有等待时间包含 ±10% 的随机抖动。

直播时，程序读取页面状态中的 `mainCameraInfo.h265Stream.hls_pull_url_map`，按预设清晰度顺序选择最高 HLS 线路。当前没有协议切换或候选线路切换。

播放列表刷新任务持续按 `EXT-X-MEDIA-SEQUENCE` 发现分片，三个异步 worker 消费正常下载队列。首次下载失败的分片进入独立重试队列，最多在首次失败后的 30 秒内重试，并由 semaphore 限制为两个并发请求。每个完整 MPEG-TS 分片先写入临时文件，通过长度和同步字节检查后再原子改名。

第一次取得 HLS 地址以及重新开播时，当前播放列表中仍然可见的分片都会加入下载队列，以尽量补回页面轮询期间已经产生的内容。不同地址的 `MEDIA-SEQUENCE` 只在各自 Playlist 范围内跟踪；分片文件名同时包含本地 Playlist 编号和上游 media sequence，最终按这两个值排序。

下载过程不调用 FFmpeg，也不重新编码，因此不会主动降低视频画质。退出时才调用 FFmpeg，以 `-c copy` 将本地分片重新封装为 MKV。

## 互动内容

当前程序不检测 PK、连麦或多人直播，取得最高画质 HLS 地址后会连续录制其中的全部内容。这样直播期间不需要周期请求完整页面，也不会因互动状态字段变化而中断录制。

当前程序不支持 Cookie、代理或浏览器登录态。公开直播页必须能够直接返回所需状态和视频流地址。

## 停止与恢复

按 `Ctrl-C` 或向进程发送 `SIGTERM` 会设置停止信号。播放列表刷新任务停止生产，三个下载 worker 和重试任务排空已经入队的分片后退出。随后程序通过 `-c copy` 合并为 `recording.mkv`。合并不会删除原始 TS 分片；合并失败时临时 MKV 会被删除，原始分片会保留。

同一用户同时只能运行一个实例。锁文件位于：

```text
recordings/USER_ID/.recorder.lock
```

锁文件退出后仍会保留，但文件本身不会阻止下次运行；真正的排他锁只在进程运行期间有效。

## 输出文件

程序取得单用户锁后立即创建本次运行的目录：

```text
recordings/USER_ID/YYYYMMDD-HHMMSS-ffffff/
├── runtime.log
├── segments/
│   ├── segment-0001-1785505602.ts
│   ├── segment-0001-1785505603.ts
│   └── ...
├── segments.ffconcat
└── recording.mkv
```

`runtime.log` 记录主要流程、异常和最终状态，包括每个 Playlist 的 sequence 范围、未观察到的 sequence 以及永久失败分片。一次程序运行期间，即使经历断流或下播后重新开播，也会继续使用同一个录制目录，并在退出时合并为同一个 `recording.mkv`。如果始终未开播或没有下载成功的分片，目录和日志仍会保留，但不会生成 `segments.ffconcat` 或合并文件。

## 代码结构

```text
src/douyin_live_recorder/
├── cli.py          CLI 入口和 FFmpeg 可用性检查
├── run.py          锁、信号、任务编排和最终收尾
├── data.py         共享数据结构、日志和可响应停止信号的等待
├── display.py      终端状态展示
├── douyin.py       从抖音直播页解析最高画质 HLS 地址
├── producer.py     地址生命周期、播放列表解析和分片生产
├── downloader.py   分片下载池、重试和 MPEG-TS 校验
└── output.py       FFmpeg 合并
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
- 当前只录制最高 HLS 线路，不比较 HTTP-FLV，也不同时录制备用线路。
- 当前下载器只接受未加密、非 byte-range 的 MPEG-TS HLS，不支持 fMP4 HLS。
- 不检测或跳过 PK、连麦等互动内容。
- 流复制只能保留 CDN 已提供的画质，不能恢复直播源中已经损失的细节。

请只录制和处理你有权保存的内容，并遵守适用的平台规则、著作权和隐私要求。
