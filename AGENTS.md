# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## 项目概览

单文件 Python 3 脚本 `tv.py`，把 IPTV 直播源投屏到不能装 APK/无 ADB 的海信电视（VIDAA 系统，乐播/HappyCast DLNA）。核心思路：**电视直连公网源优先，Mac 用 ffmpeg 转码中转兜底**。手机/电脑浏览器打开遥控器（`http://192.168.1.9:8899`）点台即换台。

- 仅依赖 Python 标准库（`http.server`、`urllib`、`subprocess` 等），**无需 pip 安装任何包、无虚拟环境**。
- 兜底模式用系统 PATH 里的 ffmpeg（`FFMPEG` 常量默认 `"ffmpeg"`，可用环境变量 `FFMPEG` 覆盖；当前是 brew 的 `/opt/homebrew/bin/ffmpeg` 9.0.2，自带 CA 证书，https 源可直接拉）。
- 无测试框架、无 linter、无构建步骤。所有逻辑集中在一个文件里。

## 常用命令

```bash
# 启动（前台，Ctrl+C 停止）
cd ~/workspace/python/tv-iptv && python3 tv.py
python3 tv.py --port 8899          # 可改端口，默认 8899

# 干净重启（必看下方"坑 8"）——旧进程占端口会让新代码不生效
pkill -9 -f tv.py                  # 宽松匹配，能杀掉 Xcode/系统 python 拉起的进程
lsof -ti :8899 | xargs kill -9     # 端口兜底清理
python3 tv.py

# 端到端自测（验证某频道是否真在播）
curl -s "http://127.0.0.1:8899/api/play/0?src=0"
sleep 8 && curl -s http://127.0.0.1:8899/api/status   # 期望看到 "PLAYING"
```

启动后访问 `http://192.168.1.9:8899/`（或 `/remote`）即遥控器页面。环境变量中的代理会在 `main()` 里被显式清空（`tv.py:405`），因为局域网 DLNA/SOAP 控制不能被代理拦截。

## 高层架构

整个系统是一个进程，内部分三层，入口都在 `tv.py`：

1. **`TV` 类（DLNA 控制层，`tv.py:115`）** — 通过 SSDP 发现设置 IP 上的 DLNA MediaRenderer（通用协议，不限于海信/乐播；乐播 HappyCast 优先，设备名随日志输出）。用 SOAP/HTTP 向电视发 `SetAVTransportURI` + `Play` 控制播放，`GetTransportInfo` 查状态。所有方法都吞异常、返回状态码或简短错误串。

2. **`Relay` 类（Mac 中转层，`tv.py:169`）** — 兜底用时拉起 ffmpeg 把公网源转码成 1080p HLS 切片（`scale=-2:1080`，2 秒一片，列表保 6 片，`omit_endlist` 让电视当作直播无限刷新）。切片落在 `hls_cache/chN/`，由 HTTP Handler 的 `/hls/chN/...` 路由对外提供。支持 `use_proxy` 走 config.json 的 `PROXY` 拉海外源。

3. **`Player` 类（播放管理层，`tv.py:226`）+ HTTP `Handler`（`tv.py:345`）** — 网页遥控器后端。`play(idx, src_idx)` **立即返回** `{"mode":"starting"}`，把确认和降级放进后台线程 `_play_sync`（`tv.py:252`）执行——这是刻意设计，否则遥控器会卡 30 秒。**降级逻辑**：先直连 SOAP，循环 6×2 秒查 `GetTransportInfo`，`PLAYING` 则定格直连；超时/失败（`NO_MEDIA_PRESENT` 立即跳）则起 `Relay` 中转，把本机 HLS 地址发给电视。

数据流一句话：

```
遥控器(浏览器 HTTP) ─> Handler ─> Player.play() [异步线程]
                          ├─ 直连: SOAP 公网 m3u8 ─> 电视自己拉流
                          └─ 兜底: ffmpeg 转码 HLS ─> 电视从 Mac 拉 /hls/chN/live.m3u8
```

关键配置在 `config.json`（TV_IP / MAC_IP / PROXY / FFMPEG），由页面「⚙ 设置」按钮读写，重启不丢；`tv.py` 的 `DEFAULTS` 只是首次生成 config.json 前的兜底值，`FFMPEG` 环境变量优先级最高。**MAC_IP 必须对电视可见**——它写死进 HLS 地址，错了电视拉不到中转流；改 TV_IP 保存后服务会自动重连电视。

## 频道表 `channels.json`（tv.py 不再硬编码）

格式为 JSON 数组：`[{"name": 名称, "sources": [{"url": m3u8地址, "proxy": false}]}]`，启动时加载，缺失则频道列表为空。

- `sources` 顺序即优先级；每频道上限 8 条源。
- 页面「🔄 刷新源」：重测全部源可用性，失效源自动后移，并写回 channels.json。
- 页面「⬇ 拉源」：从 GitHub 仓库（best-fan/iptv-sources、Guovin/iptv-api）拉取列表 → 按频道名归一化匹配（`name_key()`）→ 测活 → 追加为新源并持久化。
- 设置面板「源仓库筛选」：关键字预览远端候选，缺失频道可「测活添加」为新频道。
- 手动编辑 channels.json 后重启生效；`remote_sources/parsed.json` 是拉源缓存，可删。
- 多源频道在遥控器页面显示「线路 1 2 3…」按钮，可手动切换。
- API 切源：`GET /api/play/{ch}?src={k}`。

**加频道/换源流程**见 `wiki/07-源刷新指南.md`：拉 best-fan/iptv-sources 列表 → curl 测活(HTTP 200) → 对 **ts 分片** 测速（不是 m3u8，见坑 10）→ ffmpeg 抓帧人眼/AI 验台标防测试卡（通道号会错位，必做）→ 改 `channels.json` → 重启验证。

## 已踩过的坑（改代码前必读 `wiki/06-排错实录.md`）

这些坑都在真实故障里出现过，改动相关代码时务必规避：

- **坑 6（最大坑）URLBase 拼 controlURL 漏斜杠**：`http://x:49152_urn:...` 端口被解析成 `49152_urn`，导致 `state` 永远 UNKNOWN。修复逻辑在 `find_tv`（`tv.py:103`）：`controlURL` 不以 `/` 开头时要补 `/`。**任何改 DLNA 地址拼接的代码都要保留这个补斜杠判断。**
- **坑 9 `proxy: false` NameError**：生成 `CHANNELS` 源码时用了 JS 字面量 `false`，Python 会报 `NameError`。**必须用 `False/True`**。
- **坑 8 旧进程杀不干净**：`pkill -f "python3 tv.py"` 杀不到 Xcode/系统 python 拉起的进程（命令行是 `Python tv.py`），端口仍被占，新进程静默退出。一律用 `pkill -9 -f tv.py` 并按端口兜底。
- **坑 7 UnboundLocalError**：兜底路径在异常场景才触发，其代码要最健壮。`st` 等变量需在 `if` 前先赋值 `None`。
- **坑 1/2/3 ffmpeg 相关**：裸 mpegts 推流 30 秒电视自停（必须用 HLS + `omit_endlist`）；自编译 ffmpeg 无 CA bundle，https 源报 certificate verify failed（国内源改 http 明文）；输出文件已存在会交互式挂死（`-y` 必加）。
- **坑 5 电视 SOAP 并发弱**：请求间需 sleep，查询加 3 次重试，否则偶发 "Remote end closed connection"。

## 文件结构

- `tv.py` — 全部业务逻辑（配置、SSDP、DLNA、中转、HTTP 遥控器、源刷新/拉取）。
- `channels.json` — 频道表（tv.py 启动加载；刷新源/拉源后自动写回）。
- `config.json` — 页面设置持久化（TV_IP / MAC_IP / PROXY / FFMPEG）。
- `remote_sources/parsed.json` — GitHub 源仓库拉取缓存（可删）。
- `server.log` — 服务运行日志（stdout 同时落盘）。
- `hls_cache/` — ffmpeg 中转切片运行时目录（`chN/live.m3u8` + `seg*.ts`，`ff_{N}.log` 是 ffmpeg 错误日志，排错先看这里）。
- `wiki/` — 实战经验存档（`01`~`07`），按"偏好完整度"保存，含电视探测、ADB 不可行、DLNA 协议、选源验证、架构、排错、源刷新全流程。新接手先读 `wiki/README.md` 和 `05-架构设计.md`。
