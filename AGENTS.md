# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## 项目概览

单文件 Python 3 脚本 `tv.py`，把 IPTV 直播源投屏到不能装 APK/无 ADB 的海信电视（VIDAA 系统，乐播/HappyCast DLNA）。核心思路：**电视直连公网源优先，Mac 用 ffmpeg 转码中转兜底**。手机/电脑浏览器打开遥控器（`http://192.168.1.9:8899`）点台即换台。

- 仅依赖 Python 标准库（`http.server`、`urllib`、`subprocess` 等），**无需 pip 安装任何包、无虚拟环境**。
- 兜底模式用系统 PATH 里的 ffmpeg（`FFMPEG` 常量默认 `"ffmpeg"`，可用环境变量 `FFMPEG` 覆盖；当前是 brew 的 `/opt/homebrew/bin/ffmpeg` 9.0.2，自带 CA 证书，https 源可直接拉）。
- 无测试框架、无 linter、无构建步骤。所有逻辑集中在一个文件里；`tests/` 下是免依赖的回归脚本（`python3 tests/…` 直接跑）。

## 交流规则

- 使用中文回复

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

# 回归测试（无需真电视，起假 DLNA 服务端；改发现/控制端点逻辑后必跑）
python3 tests/test_tv_discovery.py                   # 期望 "17 passed, 0 failed"
TV_IP=192.168.1.10 python3 tests/test_tv_discovery.py  # 追加真电视联调 (18 passed)
python3 tests/test_source_speed.py                    # 源速度排序 (11 passed)

# 改完内嵌 JS 必做（坑18：全角标点会炸掉整页脚本，Python 编译器查不出来）
python3 -c "import re;s=open('tv.py',encoding='utf-8').read();m=re.search(r'PAGE = \"\"\"(.*?)\"\"\"',s,re.S);open('/tmp/p.js','w').write(re.search(r'<script>(.*?)</script>',m.group(1),re.S).group(1))" && node --check /tmp/p.js
```

启动后访问 `http://192.168.1.9:8899/`（或 `/remote`）即遥控器页面。环境变量中的代理会在 `main()` 里被显式清空（`tv.py:405`），因为局域网 DLNA/SOAP 控制不能被代理拦截。

## 高层架构

整个系统是一个进程，内部分三层，入口都在 `tv.py`：

1. **`TV` 类（DLNA 控制层，`tv.py:115`）** — 发现设置 IP 上的 DLNA MediaRenderer（通用协议，不限于海信/乐播），端点优先级 **HiSmart(38400 固定端口) > SSDP > 乐播(4915x 会漂移)**，每个候选都经 SOAP 验活，见坑24。用 SOAP/HTTP 向电视发 `SetAVTransportURI` + `Play` 控制播放，`GetTransportInfo` 查状态。所有方法都吞异常、返回状态码或简短错误串。

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
- **坑 12/13 乐播换源两大怪癖**：带 query 的 URL 会被 SetAVTransportURI 以 500 拒绝（`play_url` 自动剥 query 重试）；电视拒绝换源时仍报 PLAYING——换源成功必须 `GetMediaInfo` 核对 CurrentURI 一致（见 `_try_direct`）。
- **坑 16 自死锁**：`threading.Lock` 不可重入，持锁时调用会抢同一把锁的方法必死锁（`Relay.start` 持锁调 `stop` 曾冻死整个换台队列）。
- **坑 17 `no_proxy='*'` 杀代理**：进程级 no_proxy 会让 curl/urllib 连显式代理都绕过；拉源 fetch 用清洗过环境的 curl 子进程。
- **坑 18/19/20**：改内嵌 JS 后必须 `node --check`（全角标点炸整页）；源画质随时间波动（分辨率排序按最近实测）；HTTP 入参名单独显式列出勿与常量名"恰好同形"。完整清单见 wiki/06 坑12~20。
- **坑 24 死猜端口 = 定时炸弹（2026-10-13 全线故障）**：乐播 DLNA 端口不固定，电视重启后 `49152` → `49153`；叠加宿主 firewalld 拦掉 SSDP 回包，`find_tv()` 返回 `None`，旧代码死猜 `49152` → **所有频道 set=0 play=0、state=UNKNOWN**。现为 A/B 两层根治 + 三条代码保险：
  - **A. host 放行电视 UDP 回包**（根治 SSDP 失联，已落 permanent）：`firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="<电视IP>" protocol value="udp" accept'`。**语法坑**：必须写 `protocol value="udp"`，写成 `protocol="udp"` 报 `INVALID_RULE` 且静默无效；`--add-service=ssdp`（只放 1900）**无效**，因为被拦的是回包的目标临时端口。
  - **B. HiSmart 38400 主力**：电视固件自带的 HiSmart renderer 端口固定，不像乐播漂移。`discover_tv_ctrl()` 优先级 = **HiSmart(38400) > SSDP > 乐播(4915x)**；SSDP 仍用于取友好设备名并作次选。
  - **代码保险**：`soap_probe()` 验活（端口通/能下 description.xml 都不算，必须收到 `CurrentTransportState` 应答）、`TV._maybe_rediscover()` 连续 3 次传输层失败自动热切换。**改发现逻辑时：任何"先试默认端口"的兜底都必须带验活与重发现。**
  - **判据**：`set=0`=连不上控制通道，`set=500`=电视拒绝播放——排查先分清这两类。`RelTime` 在两个 renderer 上恒为 `00:00:00`，**不能用来判断是否真在播**；要证真在播，就让电视拉一个你控制的探针 URL 并观察访问日志。
- **坑 25 测活≠测速，"活着的源"也会卡（2026-10-13 全频道卡顿）**：晚高峰源站照样回 HTTP 200，但吞吐只剩码率的 1/3，下载追不上播放 → 电视全卡，而只按"活/死"排序的「刷新源」让慢源永远霸占第 0 位。现 `speed_check()` 对首个 ts 分片发 Range 拉头部 1MB，按 Content-Range 总长推算整片耗时，**余量=片时长÷整片下载耗时，<1 数学上必卡**；「刷新源」改为死源垫底、活源按余量降序（结果新增 `slow` 计数）。伴生修复：中转 `-hls_time 2` 被 x264 默认 GOP（≈10s）架空、实际产出 5~10s 大切片，加 `-force_key_frames expr:gte(t,n_forced*2)` 等参数后精确 2.000s。**验证源必须 `curl -L`（这批源大量 302/301，不跟重定向会把好源误判成死源）。**
- **排障顺序（坑 24/25 教训）**：所有频道同时**失败** → 先查控制通道（`state` 是否 UNKNOWN），源不会整齐地全体阵亡；所有频道同时**卡顿** → 先算各源吞吐余量（对 ts 分片实测，<1 必卡），别急着怀疑刚改的代码——时间相关≠因果，先证伪；怀疑"某次提交改坏的"时先 `git diff <commit> -- tv.py | grep -E "^[+-].*(端口|find_tv|SSDP)"` 确认是否真碰到相关代码，再用**同一份代码在两台机器跑**（Mac 正常/服务器失败 = 环境问题，非代码）。

## 页面与部署

页面功能（刷新源/拉源/设置/源筛选）、云端部署形态 A/B/C、通用 DLNA 发现设计、开源脱敏五步清单 → 详见 `wiki/08-页面功能与部署.md`。要点：8899 无鉴权，暴露公网前必须加 token；电视只在启动/保存设置时搜索一次。

## 文件结构

- `tv.py` — 全部业务逻辑（配置、SSDP、DLNA、中转、HTTP 遥控器、源刷新/拉取）。
- `channels.json` — 频道表（tv.py 启动加载；刷新源/拉源后自动写回）。
- `config.json` — 页面设置持久化（TV_IP / MAC_IP / PROXY / FFMPEG）。
- `remote_sources/parsed.json` — GitHub 源仓库拉取缓存（可删）。
- `server.log` — 服务运行日志（stdout 同时落盘）。
- `hls_cache/` — ffmpeg 中转切片运行时目录（`chN/live.m3u8` + `seg*.ts`，`ff_{N}.log` 是 ffmpeg 错误日志，排错先看这里）。
- `wiki/` — 实战经验存档（`01`~`08`），按"偏好完整度"保存，含电视探测、ADB 不可行、DLNA 协议、选源验证、架构、排错（坑1~20）、源刷新、页面功能与部署全流程。新接手先读 `wiki/README.md` 和 `05-架构设计.md`。
