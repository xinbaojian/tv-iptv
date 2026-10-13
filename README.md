# 电视 IPTV 投屏遥控器 📺

单文件 Python 服务：抓取公开 IPTV 直播源，通过 **DLNA 推链接播放**投给电视，手机/电脑浏览器即遥控器。**电视直连公网源优先，本机 ffmpeg 转码中转兜底**——不需要在电视上装任何东西，也不需要 ADB。

> 适合电视无法安装第三方 App 的场景（如部分海信 VIDAA、运营商定制机型）。

## 功能特性

- 🖥 **网页遥控器**：手机自适应页面，点频道即换台，支持多线路切换
- 🔌 **通用 DLNA**：支持任何带 DLNA 投屏的电视/盒子（海信、小米、TCL、创维、索尼等），乐播(HappyCast)优先
- 📡 **双模式播放**：直连公网源（零转码延迟）优先，失败自动落本机 ffmpeg 转码中转（1080p HLS，直播语义无限续播）
- 🔄 **刷新源**：一键重测全部源可用性，失效源自动后移
- ⬇ **拉源**：从 GitHub 开源源仓库拉取最新列表，按频道名匹配、测活后自动补充
- 🔍 **源仓库筛选**：关键字预览远端候选，缺失频道可测活添加
- ⚙️ **页面设置**：电视 IP / 本机 IP / 代理 / ffmpeg 路径，保存即写 `config.json`，重启不丢

## 支持的电视

| 协议 | 支持情况 |
|---|---|
| DLNA / UPnP MediaRenderer | ✅ 支持（海信、小米、TCL、创维、索尼等大多数智能电视及盒子） |
| 乐播 HappyCast（部分海信内置） | ✅ 支持（优先匹配） |
| AirPlay（苹果投屏） | ❌ 屏幕镜像协议，本项目是推链接播放 |
| Chromecast / Miracast | ❌ 同上 |

> 电视需与运行服务的电脑在同一局域网。个别品牌 DLNA 实现有差异，换台失败请看 `server.log` 排查。

## 快速开始

依赖：Python 3.8+（仅标准库，无需 pip 装包）；ffmpeg 可选（仅兜底中转模式需要，走系统 PATH）。

```bash
git clone https://github.com/<你的用户名>/tv-iptv.git
cd tv-iptv
cp channels.example.json channels.json   # 示例频道表, 或用"拉源"自己生成
python3 tv.py                            # 启动, Ctrl+C 停止
```

浏览器（手机/电脑均可）打开 `http://<本机IP>:8899`：

1. 首次使用进「⚙ 设置」：填电视 IP（保存后自动重连电视）、本机 IP、代理（可选）
2. 点「⬇ 拉源」从开源仓库拉取直播源，或直接点频道试播示例表
3. 点「🔄 刷新源」可随时体检全部源，失效源自动后移

## 配置

**config.json**（页面设置自动生成/更新，不入库）：

```json
{
  "TV_IP": "192.168.1.100",
  "MAC_IP": "192.168.1.50",
  "PROXY": "",
  "FFMPEG": "ffmpeg"
}
```

- `TV_IP`：电视 IP，保存后自动重新发现电视
- `MAC_IP`：本机对电视可见的 IP（写进中转流地址，必须电视可达）
- `PROXY`：拉取海外源/源仓库列表时使用的代理，留空不用
- `FFMPEG`：转码程序路径，默认走系统 PATH；环境变量 `FFMPEG` 优先级最高

**channels.json**（频道表，格式示例见 `channels.example.json`）：

```json
[
  {
    "name": "CCTV-1 综合",
    "sources": [
      {"url": "http://example.com/live/cctv1.m3u8", "proxy": false}
    ]
  }
]
```

- `sources` 顺序即线路优先级，每频道上限 8 条
- 页面「刷新源 / 拉源」会自动更新并写回此文件；手动编辑后重启生效

## 源拉取与筛选

内置两个开源源仓库（致谢见下），点「⬇ 拉源」后：

1. 拉取 `best-fan/iptv-sources` 的 `cn_all.m3u8` 与 `Guovin/iptv-api` 的 `result.m3u`（直连失败自动走代理）
2. 解析 m3u/txt 格式、URL 去重
3. 按频道名归一化匹配（`CCTV-1综合` / `CCTV1` 均匹配 CCTV-1）
4. 候选并发测活（HTTP 200 + 有效 HLS），存活的追加为频道新源并持久化

设置面板的「源仓库筛选」支持关键字预览远端候选；仓库里有而本地没有的频道可一键「测活添加」。

> 注意：拉源只保证"地址可用"，不校验画面内容；IPTV 聚合源可能出现测试卡，遇到换线路即可。

## API

| 接口 | 说明 |
|------|------|
| GET /api/channels | 频道列表 |
| GET /api/play/N[?src=k] | 播放第 N 个频道（可指定线路；直连失败自动中转） |
| GET /api/stop | 停止播放 |
| GET /api/status | 当前状态 + 刷新/拉源进度 |
| GET /api/refresh | 重测全部源可用性并调整线路顺序 |
| GET /api/pull | 从源仓库拉取并合并新源 |
| GET /api/sources/preview?kw= | 按关键字筛选远端源候选 |
| GET /api/sources/add?name= | 将远端同名频道测活后添加为本地频道 |
| GET /api/settings · /api/settings/save | 读取/保存配置（写入 config.json） |
| GET /hls/chN/live.m3u8 | 中转模式 HLS 播放列表（电视拉流用） |

## 已知问题

- 乐播播放器播 HLS 可能因缓冲耗尽自停，重新点台即恢复（服务端已用直播语义切片尽量规避）
- IPTV 聚合源稳定性天然有限：源失效属常态，用「刷新源 + 拉源 + 线路切换」组合应对
- 部分聚合源只提供标清(4:3)信号，电视会等比显示两侧留黑边；「拉源」可能带来高清源，可按分辨率重排线路（见 wiki/07）
- 电视投屏服务只在服务启动/保存设置时搜索一次，电视重开后请重启服务或保存一次设置

## 安全与隐私

`config.json`、`channels.json`、`server.log`、`hls_cache/`、`remote_sources/` 含你的**个人网络信息与收视数据**，已加入 `.gitignore`，请勿提交到公开仓库。首次运行会按默认值生成 config.json，请在页面设置中改成自己的环境。

## 项目经验

实战排错经验（DLNA 协议细节、乐播设备的怪癖、源测速方法、九类踩坑记录）沉淀在 [`wiki/`](wiki/README.md)，推荐从 `wiki/README.md` 读起。

## 参考与致谢

本项目的直播源数据来源与实现参考了以下优秀开源项目：

- [Guovin/iptv-api](https://github.com/Guovin/iptv-api) — IPTV 直播源自动采集、校验、测速工具，本工具「拉源」功能的候选列表来源之一
- [best-fan/iptv-sources](https://github.com/best-fan/iptv-sources) — 自动更新的国内直播源列表（cn_all/cn_cctv/cn_province），「拉源」功能的候选列表来源之一

感谢开源社区对 IPTV 可用性的持续维护。直播源版权归原播出机构所有，本项目仅用于个人学习研究，请勿用于商业用途。

## License

暂未指定；个人使用随意，开源发布建议 MIT（补充 LICENSE 文件即可）。
