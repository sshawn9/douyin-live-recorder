# douyin-live-recorder

Linux 下的纯 CLI 抖音直播监控与录制器。输入直播页的数字用户 ID，程序周期检查开播状态，开播后把 CDN 视频流交给 FFmpeg 直接封装到本地；不截屏、不重新编码。

## 能力边界

- 默认优先 `origin` 源，并优先 HLS。仍可用 `--protocol flv` 强制选择 FLV。
- 同一清晰度会保留网页暴露的多组 HLS/FLV 地址；FFmpeg 退出或地址不可用时先按顺序切换候选，候选耗尽后再刷新直播页并写入新分片。
- Ctrl-C 会先给 FFmpeg 发送 SIGINT，让当前 MPEG-TS 分片收尾，然后以 `-c copy` 无损合并成 MKV。原始分片始终保留。
- `--skip-interactions` 会在检测到 PK、连麦或多人直播时停止写入，互动结束后用新地址继续录制。
- 同一个用户 ID 有进程锁，避免误开两个实例。

“跳过互动”依据直播页当前暴露的 `pkStore`、`linkmicStore` 和 `linker_map` 状态，是启发式检测而非抖音公开接口。页面状态存在几秒延迟，抖音改版也可能导致漏检；程序会把判断证据和跳过时间写入 `session.json`，便于审计。占位的连麦座位不会被误判为正在连麦。

## 安装

需要 Python 3.14+、uv 和系统 FFmpeg：

```bash
uv sync
```

程序按以下顺序选择 FFmpeg：`--ffmpeg` 指定路径、系统 `PATH`；找不到时直接报错。NixOS 可以在带有 Nixpkgs FFmpeg 的临时环境中启动：

```bash
nix shell nixpkgs#ffmpeg-headless
uv run douyin-live-recorder 95426912878
```

## 使用

直接监控并录制最高原始画质：

```bash
uv run douyin-live-recorder 95426912878
```

跳过 PK、连麦和多人直播：

```bash
uv run douyin-live-recorder 95426912878 --skip-interactions
```

只看一次状态和可用画质，不录制，也不会输出带签名的 CDN URL：

```bash
uv run douyin-live-recorder 95426912878 --status-only
```

常用参数：

```text
--output-dir DIR                 输出根目录，默认 ./recordings
--poll-interval 60               未开播时检查间隔，最小 15 秒
--interaction-poll-interval 5    跳过互动模式的状态检查间隔，最小 3 秒
--quality origin|highest|NAME    默认 origin
--protocol auto|flv|hls          默认 auto（同清晰度优先 HLS）
--[no-]merge                     结束时是否无损合并为 recording.mkv
--ffmpeg PATH                    显式指定 FFmpeg；默认查找系统 PATH
--proxy URL                      可选代理
```

多数公开直播页无需浏览器打开或登录。遇到登录墙或风控时，可提供你自己的 Cookie；程序不会内置或共享 Cookie：

```bash
export DOUYIN_COOKIE='ttwid=...; __ac_nonce=...'
uv run douyin-live-recorder 95426912878
```

也可将完整 Cookie 内容放在权限受控的文件中：

```bash
chmod 600 ~/.config/douyin-live-recorder/cookie.txt
uv run douyin-live-recorder 95426912878 \
  --cookie-file ~/.config/douyin-live-recorder/cookie.txt
```

不建议把 Cookie 直接写进命令参数、配置仓库或日志。

## 输出结构

```text
recordings/<user-id>/<时间_主播_房间>/
├── part-0001.ts
├── part-0002.ts
├── segments.ffconcat
├── recording.mkv
├── session.json
└── ffmpeg.log
```

每次断线恢复或跳过互动都会产生新分片。`recording.mkv` 只是重新封装，不损失画质；如果不同分片的编码参数发生变化而无法无损合并，程序保留所有 TS 分片，并在 `session.json` 记录失败原因。

## 架构

- `models.py`：仅定义数据结构，不负责网络、文件或进程操作。
- `douyin.py`：获取并解析直播页，识别状态、源流和互动状态。
- `streams.py`：纯选择策略，决定清晰度和协议。
- `monitor.py`：监控/断线恢复/跳过互动状态机。
- `recorder.py`：只管理 FFmpeg 生命周期、分片收尾和无损合并。
- `session.py`：只负责场次目录与结构化元数据持久化。

## 风控策略

- 未开播默认 60 秒轮询并加入随机抖动，不进行高频探测。
- 403、418、429、空响应、解析失败和服务端错误会指数退避，最长 15 分钟。
- 请求错误与“未开播”严格区分，错误不会导致错误结束场次。
- 每次断流重新解析页面，不长期复用过期签名 URL。
- 复用单个 HTTP 会话，只保留极低连接并发；CDN 候选只在当前地址失败后顺序尝试，不批量并发探测。
- 不自动读取浏览器资料、不硬编码共享 Cookie，也不记录完整视频流 URL。

这些措施只能降低风控概率，不能保证平台永不拦截。请只录制你有权保存的内容，并遵守平台规则、著作权和隐私要求。

## 测试

```bash
uv run python -m unittest discover -s tests -v
uv run --group dev ruff check src tests
uv run --group dev pyright src tests
```
