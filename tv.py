#!/usr/bin/env python3
"""
客厅电视 IPTV 播放器 (海信电视 · 乐播 DLNA)
==========================================
架构: 电视直连优先 / Mac 中转兜底 / 网页遥控器

  你 ──> 手机/电脑浏览器遥控器 (http://<本机IP>:8899)
            │
            ├─ 直连模式: 电视自己拉公网直播源 (零转码, 延迟最低)
            └─ 兜底模式: Mac ffmpeg 拉流转码 1080p HLS, 电视从 Mac 拉
               (源卡顿/4K 太重/海外源走代理时用)

启动: python3 tv.py          # 前台运行, Ctrl+C 停止
      python3 tv.py --port 8899
"""

import argparse
import http.server
import ipaddress
import json
import os
import queue
import re
import signal
import socket
import socketserver
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.sax.saxutils

# ================= 配置 =================
# 默认值; 页面"设置"保存后写入 config.json, 重启不丢 (config.json 优先于这里的默认值)
# 注意: 开源仓库不收录 config.json/channels.json 等本地数据, 首次启动会按默认值生成
DEFAULTS = {
    "TV_IP": "192.168.1.100",   # 电视 (DLNA) IP — 按自己家网段在页面设置里修改
    "MAC_IP": "192.168.1.50",   # 本机 (对电视可见的 IP)
    "PROXY": "",                # 兜底模式拉国际源时可走代理 (http://ip:port), 留空=不用
    "FFMPEG": "ffmpeg",         # 兜底转码程序, 默认走系统 PATH
}
BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
HLS_ROOT = os.path.join(BASE, "hls_cache")
FFMPEG_ENV = os.environ.get("FFMPEG", "").strip()  # 环境变量优先级最高
TV_IP, MAC_IP, PROXY = DEFAULTS["TV_IP"], DEFAULTS["MAC_IP"], DEFAULTS["PROXY"]
FFMPEG = FFMPEG_ENV or DEFAULTS["FFMPEG"]

def current_settings():
    return {"TV_IP": TV_IP, "MAC_IP": MAC_IP, "PROXY": PROXY, "FFMPEG": FFMPEG}

def save_config_file():
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(current_settings(), f, ensure_ascii=False, indent=2)
        return None
    except Exception as e:
        return f"写入 config.json 失败: {e}"

def load_config():
    """启动时读 config.json 覆盖默认值; 文件不存在则用默认值创建"""
    global TV_IP, MAC_IP, PROXY, FFMPEG
    cfg = {}
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[配置] config.json 解析失败, 使用默认值: {e}")
    for k in DEFAULTS:
        v = cfg.get(k)
        v = str(v).strip() if isinstance(v, str) else ""  # 非字符串(如手写数字/对象)一律忽略
        if not v:
            continue
        if k == "TV_IP":
            TV_IP = v
        elif k == "MAC_IP":
            MAC_IP = v
        elif k == "PROXY":
            PROXY = v
        elif k == "FFMPEG" and not FFMPEG_ENV:
            FFMPEG = v
    if not os.path.exists(CFG_PATH):
        err = save_config_file()
        print(f"[配置] 已生成默认 config.json{'' if not err else ': ' + err}")

def save_settings(d):
    """校验 + 应用 + 持久化, 返回错误信息或 None"""
    global TV_IP, MAC_IP, PROXY, FFMPEG

    def valid_ip(s):
        try:
            ipaddress.IPv4Address(s)  # 只认 IPv4: MAC_IP 要拼进 http://IP:port, IPv6 得加方括号
            return True
        except Exception:
            return False

    tv = str(d.get("tv_ip") or "").strip()
    mac = str(d.get("mac_ip") or "").strip()
    proxy = str(d.get("proxy") or "").strip()
    ff = str(d.get("ffmpeg") or "").strip()
    if not valid_ip(tv):
        return "电视 IP 格式不对"
    if not valid_ip(mac):
        return "本机 IP 格式不对"
    if proxy and not re.match(r"^https?://", proxy):
        return "代理地址需以 http:// 或 https:// 开头 (留空表示不用代理)"
    if not ff:
        return "ffmpeg 路径不能为空"
    TV_IP, MAC_IP, PROXY, FFMPEG = tv, mac, proxy, ff
    return save_config_file()

# 频道表: 不再硬编码, 存于 channels.json (页面"刷新源/拉源"会更新并写回)
CHANNELS = []
CHANNELS_PATH = os.path.join(BASE, "channels.json")
# 保护 CHANNELS 变异 + channels.json 写盘: "刷新源"与"拉源"可并行, 两个 json.dump
# 各自 open(...,"w") 截断后交叉增量写会产出损坏 JSON (重启后频道全丢).
# 用 RLock: 变异块内会再调 save_channels_file (同线程二次获取)
CH_LOCK = threading.RLock()

def load_channels():
    """启动时从 channels.json 读频道表; 文件损坏/缺失则频道列表为空"""
    global CHANNELS
    try:
        with open(CHANNELS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list) and data
        clean = []
        for c in data:
            srcs = []
            for s in (c.get("sources") or []):
                u = str(s.get("url") or "").strip()
                if u:
                    srcs.append({"url": u, "proxy": bool(s.get("proxy", False))})
            if c.get("name") and srcs:
                clean.append({"name": str(c["name"]).strip(), "sources": srcs})
        assert clean
        CHANNELS = clean
        print(f"[频道] 已加载 {len(CHANNELS)} 个频道: {CHANNELS_PATH}")
        return True
    except FileNotFoundError:
        print(f"[频道] 未找到 {CHANNELS_PATH}, 频道列表为空 (可用页面'拉源'生成)")
    except Exception as e:
        print(f"[频道] channels.json 加载失败: {e}")
    return False

def save_channels_file():
    """把内存中的频道表写回 channels.json (刷新源/拉源后调用).
    原子写: 临时文件 + os.replace, 防崩溃/并发写出半截文件; 持 CH_LOCK 防交叉写"""
    tmp = CHANNELS_PATH + ".tmp"
    try:
        with CH_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(CHANNELS, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CHANNELS_PATH)
        return None
    except Exception as e:
        print(f"[频道] 写入 channels.json 失败: {e}")
        return str(e)

def name_key(s):
    """频道名归一化, 供远端源名称与本地频道匹配 (CCTV-1综合/CCTV1 -> CCTV1)"""
    s = str(s).upper()
    if re.search(r"CCTV\s*5\s*\+", s):
        return "CCTV5+"
    m = re.search(r"CCTV\s*[-–]?\s*(\d{1,2})", s)
    if m:
        return f"CCTV{m.group(1)}"
    s = re.sub(r"(高清|超清|标清|蓝光|4K|8K|HD|FHD|UHD|IPTV|TEST|频道)", "", s)
    s = re.sub(r"[^0-9A-Z\u4e00-\u9fa5]", "", s)
    return s

def url_key(u):
    """URL 去重键: 主机+端口+路径 (忽略 query 里的易变令牌)"""
    try:
        p = urllib.parse.urlsplit(u)
        return f"{(p.hostname or '').lower()}:{p.port or ''}{p.path.rstrip('/')}"
    except Exception:
        return u

def parallel_map(items, fn, workers=6):
    """固定 worker 池并行处理, 替代一任务一线程 (刷新源 255 条源会起 255 个线程,
    拉源测活可达 400+; worker 数即并发上限, 替代原来的 Semaphore)"""
    items = list(items)
    if not items:
        return
    q = queue.Queue()
    for it in items:
        q.put(it)

    def worker():
        while True:
            try:
                it = q.get_nowait()
            except queue.Empty:
                return
            try:
                fn(it)
            except Exception as e:
                print(f"[并行] 任务异常: {e}")
            finally:
                q.task_done()

    ts = [threading.Thread(target=worker, daemon=True)
          for _ in range(min(workers, len(items)))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

def alive_check(url, timeout=4, use_proxy=False):
    """源地址可用性: HTTP 200 且返回有效 HLS 播放列表.
    use_proxy: 源标记 proxy=True 时显式走 PROXY. 注意进程里 http_proxy 已被 main()
    清空且 no_proxy='*' —— urllib 默认处理器永远直连, 走代理必须像坑17 那样用
    清洗过环境的 curl 子进程 + 显式 -x (urllib 的 ProxyHandler 也咨询 no_proxy)"""
    if use_proxy and PROXY:
        try:
            env = dict(os.environ)
            env.pop("no_proxy", None)
            env.pop("NO_PROXY", None)
            r = subprocess.run(["curl", "-sfL", "--max-time", str(timeout + 2),
                                "-A", "lavf", "-x", PROXY, url],
                               capture_output=True, timeout=timeout + 6, env=env)
            return r.returncode == 0 and b"#EXTINF" in r.stdout[:8192]
        except Exception:
            return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lavf"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = r.read(4096).decode(errors="replace")
            return r.status == 200 and "#EXTINF" in head
    except Exception:
        return False

# ================= 电视发现 (SSDP, 通用 DLNA——不限于海信/乐播) =================
def find_tv(timeout=3):
    """SSDP 搜索设置 IP 上的 DLNA MediaRenderer, 返回 (AVTransport 控制 URL, 设备名).
    兼容任意品牌 (海信/小米/TCL/创维/索尼...), 乐播(HappyCast) 优先"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        for st in (b"urn:schemas-upnp-org:device:MediaRenderer:1",
                   b"urn:schemas-upnp-org:device:MediaRenderer:2"):
            s.sendto(b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                     b'MAN: "ssdp:discover"\r\nMX: 1\r\n' + b"ST: " + st + b"\r\n\r\n",
                     ("239.255.255.250", 1900))
        locs = set()
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                break
            if addr[0] != TV_IP:
                continue
            m = re.search(rb"LOCATION:\s*(\S+)", data)
            if m:
                locs.add(m.group(1).decode())
        s.close()
        fallback = None
        for loc in locs:
            try:
                xml = urllib.request.urlopen(loc, timeout=4).read().decode(errors="replace")
            except Exception:
                continue
            if "AVTransport" not in xml:
                continue  # 不是播放设备
            base = re.search(r"<URLBase>(.*?)</URLBase>", xml)
            ctrl = re.search(r"<controlURL>(.*?)</controlURL>", xml)
            if not ctrl:
                continue
            fname = re.search(r"<friendlyName>(.*?)</friendlyName>", xml)
            # controlURL 以 _ 开头时必须补 / (否则端口解析失败, 坑6)
            sep = "" if ctrl.group(1).startswith("/") else "/"
            baseurl = base.group(1).rstrip("/") if base else f"http://{TV_IP}:49152"
            cand = (baseurl + sep + ctrl.group(1),
                    (fname.group(1) if fname else "").strip() or TV_IP)
            if "HappyCast" in xml or "LEBO" in xml or "乐播" in xml:
                return cand  # 乐播优先
            fallback = fallback or cand
        return fallback
    except Exception:
        pass
    return None

# ---- 控制端点校验与候选探测 ----
# 坑24: 乐播 DLNA 端口不是固定的. 电视/投屏应用重启后会重选端口 (亲历 49152 → 49153),
# 而 SSDP 一旦拿不到应答 (如宿主防火墙拦掉组播回包), 旧代码只能死猜 49152 ——
# 猜错即所有 SOAP 连接被拒, 表现就是"所有频道都无法播放" (set=0 play=0, state=UNKNOWN).
# 故: ①候选端口成片探测; ②任何拿到的地址一律用 SOAP 验活后才采用; ③运行中连续失败自动重发现.
#
# 优先级 (2026-10-13 调整): SSDP > HiSmart 38400 > 乐播 4915x.
# 电视固件自带的 HiSmart renderer 端口固定 (38400), 不像乐播那样重启漂移, 因此作为
# 乐播不可用时的首选兜底; 乐播端口列表仍保留 (HiSmart 万一被固件关掉时不至于全灭).
# 实测两者能力等价: Set/Play/GetMediaInfo/GetTransportInfo 均 200, CurrentURI 比对一致,
# 且都真的回拉了本机切片 (用 18999 探针服务验证电视确实发起 /probe.m3u8 + /seg1.ts)。
HISMART_AVT_PATH = "/upnp/control/mingusavtr"
HISMART_PORTS = (38400,)
LEBO_AVT_PATH = "/_urn:schemas-upnp-org:service:AVTransport_control"
LEBO_PORTS = (49152, 49153, 49154, 49150, 49155, 49156, 49157, 49158, 49159, 49160)

def soap_envelope(action, args_xml):
    """SOAP 1.1 信封. TV.soap_raw 与 soap_probe 共用一份, 免得两处信封漂移"""
    return (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f'{args_xml}</u:{action}></s:Body></s:Envelope>').encode()

def soap_probe(url, timeout=2):
    """端点是否真能用: 返回 200 且响应体确实是 GetTransportInfo 应答才算数.
    比"端口通"或"description.xml 能下载"更可信——只有它能证明这里是可用的 AVTransport.
    注意两个反向陷阱: ①500 不算成功 (端点活着但这个动作不认); ②端口通、回 200 但内容
    不是 DLNA 应答 (例如同机别的 HTTP 服务恰好占了这个端口) 也不算成功"""
    req = urllib.request.Request(
        url, data=soap_envelope("GetTransportInfo", "<InstanceID>0</InstanceID>"),
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            body = r.read(4096).decode(errors="replace")
    except Exception:
        return False
    return "GetTransportInfoResponse" in body or "CurrentTransportState" in body

def probe_ctrl_candidates(cands, timeout=2, workers=6):
    """并行探测一层候选端点, 命中即短路返回. 并行是为了把最坏耗时压到单次超时的量级
    (电视关机时逐个串行探测 10 个端口要 20s, 并行只要 2s)"""
    cands = list(cands)
    if not cands:
        return None
    hit, lock, q = [None], threading.Lock(), queue.Queue()
    for c in cands:
        q.put(c)

    def worker():
        while True:
            with lock:
                if hit[0]:
                    return
            try:
                c = q.get_nowait()
            except queue.Empty:
                return
            if soap_probe(c, timeout):
                with lock:
                    if not hit[0]:
                        hit[0] = c
                return

    ts = [threading.Thread(target=worker, daemon=True)
          for _ in range(min(workers, len(cands)))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return hit[0]

def discover_tv_ctrl(timeout=3):
    """返回 (控制URL, 设备名). 优先级: HiSmart(端口固定) > SSDP > 乐播(端口漂移).
    任何来源的地址都必须验活才算数——"拿到了地址"≠"地址活着":
    电视重启后 SSDP 缓存会过期, 半死端点也会一直不应答"""
    found = find_tv(timeout)  # SSDP: 顺带拿到友好设备名 (失败返回 None, 不阻塞后续探测)
    dev = found[1] if found else f"未知设备({TV_IP})"

    # 1) HiSmart: 电视固件自带, 端口固定 38400, 不受乐播重启漂移影响 → 最稳, 优先
    url = probe_ctrl_candidates([f"http://{TV_IP}:{p}{HISMART_AVT_PATH}" for p in HISMART_PORTS])
    if url:
        print(f"[电视] 端口探测命中 (HiSmart 固定端口): {url}")
        return url, dev

    # 2) SSDP 结果: 端口真实, 但要验活 (缓存/半死端点不可信)
    if found:
        if soap_probe(found[0]):
            return found
        print(f"[电视] SSDP 端点不应答, 转端口探测: {found[0]}")

    # 3) 乐播端口列表: 端口会漂移, 只能枚举兜底
    url = probe_ctrl_candidates([f"http://{TV_IP}:{p}{LEBO_AVT_PATH}" for p in LEBO_PORTS])
    if url:
        print(f"[电视] 端口探测命中 (乐播): {url}")
        return url, dev

    # 全都不通: 给个默认值, 至少日志里能看到试的是哪个 (播放会失败, 但状态可查)
    return f"http://{TV_IP}:{LEBO_PORTS[0]}{LEBO_AVT_PATH}", dev

# ================= DLNA 控制 =================
class TV:
    # 连续 N 次传输层失败即认为端点已死, 触发重新发现 (坑24)
    REDISCOVER_AFTER = 3
    REDISCOVER_COOLDOWN = 10.0
    # 类级默认值: 记账代码在 __init__ 之前/之外被调用时也不炸 (坑7 老调 —— 兜底路径要最健壮)
    fail_lock = None
    fail_streak = 0
    rediscovering = False
    last_rediscover = 0.0

    def __init__(self):
        # 坑5: 电视 SOAP 并发弱. status 轮询(每2.5s)与播放确认循环会同时打电视,
        # 并发请求偶发被断连 → 直连误判失败 → 白白降级到中转. 所有 SOAP 用这把锁串行化.
        self.soap_lock = threading.Lock()
        self.fail_lock = threading.Lock()
        self.fail_streak = 0        # 连续传输层失败次数 (HTTPError 不算: 端点活着)
        self.rediscovering = False
        self.last_rediscover = 0.0
        url, name = discover_tv_ctrl()  # 坑24: SSDP 结果必须验活, 否则探测候选端口
        self.ctrl, self.name = url, name
        print(f"[电视] 控制: {self.ctrl} (设备: {self.name})")

    def _fail_guard(self):
        """惰性取记账锁: 即使对象没走 __init__ (测试桩/异常构造) 也不会炸.
        坑7 老调 —— 兜底记账代码本身要最健壮"""
        lock = getattr(self, "fail_lock", None)
        if lock is None:
            lock = self.fail_lock = threading.Lock()
        return lock

    def _note_ok(self):
        with self._fail_guard():
            self.fail_streak = 0

    def _maybe_rediscover(self):
        """传输层连续失败 → 电视可能换端口/重启了, 重新发现一次.
        只调模块级 soap_probe (不碰 soap_lock / soap_raw), 否则持锁重入必自死锁 (坑16)"""
        with self._fail_guard():
            self.fail_streak = getattr(self, "fail_streak", 0) + 1
            now = time.time()
            if (self.fail_streak < self.REDISCOVER_AFTER or self.rediscovering
                    or now - getattr(self, "last_rediscover", 0.0) < self.REDISCOVER_COOLDOWN):
                return
            self.rediscovering = True
            self.last_rediscover = now
        try:
            print(f"[电视] 连续 {self.fail_streak} 次 SOAP 失败, 重新发现控制端点…")
            url, name = discover_tv_ctrl()
            if url and url != self.ctrl and soap_probe(url):
                self.ctrl, self.name = url, name
                print(f"[电视] 控制端点已更新: {self.ctrl} (设备: {self.name})")
                with self._fail_guard():
                    self.fail_streak = 0
        except Exception as e:
            print(f"[电视] 重新发现失败: {e}")
        finally:
            with self.fail_lock:
                self.rediscovering = False

    def soap(self, action, args_xml):
        # 坑12修复: 失败必须返回 int (原返回错误字符串, 调用方 r_set==200 比较永远 False,
        # 且 500 被吞成字符串导致"直连为何失败"无从排查)
        return self.soap_raw(action, args_xml)[0]

    def state(self):
        for attempt in range(3):
            code, body = self.soap_raw("GetTransportInfo", "<InstanceID>0</InstanceID>", timeout=4)
            if code == 200:
                m = re.search(r"CurrentTransportState>([^<]+)", body)
                if m:
                    return m.group(1)
            time.sleep(1)
        return "UNKNOWN"

    def soap_raw(self, action, args_xml, timeout=8):
        req = urllib.request.Request(self.ctrl, data=soap_envelope(action, args_xml), headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"'})
        try:
            with self.soap_lock:
                r = urllib.request.urlopen(req, timeout=timeout)
                body = r.read().decode(errors="replace")
            self._note_ok()   # 拿锁外记账, 避免与重发现路径交叉持锁
            return r.status, body
        except urllib.error.HTTPError as e:
            # 端点活着 (应答了, 只是这个动作不认) —— 不算端点故障, 不触发重发现
            self._note_ok()
            return e.code, e.read().decode(errors="replace")[:200]
        except Exception as e:
            self._maybe_rediscover()  # 连不上: 端点可能已失效 (坑24)
            return 0, str(e)

    def current_uri(self):
        """电视实际持有的 URI (GetMediaInfo). 状态 PLAYING 不代表播的是新 URL, 必须核对它"""
        for attempt in range(3):
            code, body = self.soap_raw("GetMediaInfo", "<InstanceID>0</InstanceID>", timeout=4)
            if code == 200:
                m = re.search(r"<CurrentURI>(.*?)</CurrentURI>", body, re.S)
                if m:
                    # 返回的是 XML 原文, & 会被编码成 &amp; — 反转义后才能和下发 URL 比对
                    return xml.sax.saxutils.unescape(m.group(1))
            time.sleep(1)
        return None

    def play_url(self, url):
        """设置 URI 并播放.
        URL 必须 XML 转义后再进 SOAP 体: 直播源 query 里大量 &token=... (115/255 条),
        裸 & 不是合法 XML 实体, 电视解析器直接 500 —— 这才是坑12"带 query 必拒"的真根因,
        转义后大部分源不必再剥 query. 乐播仍有缺陷时保留剥 query 重试兜底.
        返回 (set状态码, play状态码, 实际下发的URL)"""
        r_set = self.soap("SetAVTransportURI",
                          f"<InstanceID>0</InstanceID><CurrentURI>{xml.sax.saxutils.escape(url)}</CurrentURI>"
                          f"<CurrentURIMetaData>&lt;DIDL-Lite&gt;&lt;/DIDL-Lite&gt;</CurrentURIMetaData>")
        final_url = url
        if r_set != 200 and "?" in url:
            stripped = url.split("?", 1)[0]
            r2 = self.soap("SetAVTransportURI",
                           f"<InstanceID>0</InstanceID><CurrentURI>{xml.sax.saxutils.escape(stripped)}</CurrentURI>"
                           f"<CurrentURIMetaData>&lt;DIDL-Lite&gt;&lt;/DIDL-Lite&gt;</CurrentURIMetaData>")
            if r2 == 200:
                r_set, final_url = r2, stripped
        r_play = self.soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        return r_set, r_play, final_url

# ================= 兜底: Mac 中转 (ffmpeg HLS) =================
class Relay:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        os.makedirs(HLS_ROOT, exist_ok=True)

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
                try:
                    self.proc.wait(3)
                except Exception:
                    pass
            self.proc = None

    def start(self, idx, relay_url, use_proxy=False):
        """启动频道中转, 等首个切片就绪, 返回本机 HLS 地址"""
        chdir = os.path.join(HLS_ROOT, f"ch{idx}")
        # 换台即清掉其他频道的残留: chN/ 切片与 ff_N.log 只增不减 (49 个频道轮着中转能堆几百 MB);
        # 旧 ffmpeg 已被 stop() 杀死, 留着旧切片电视也只会 404
        try:
            for d in os.listdir(HLS_ROOT):
                if d.startswith("ch") and d[2:].isdigit() and int(d[2:]) != idx:
                    shutil.rmtree(os.path.join(HLS_ROOT, d), ignore_errors=True)
            for f in os.listdir(HLS_ROOT):
                if f.startswith("ff_") and f.endswith(".log") and f != f"ff_{idx}.log":
                    try:
                        os.unlink(os.path.join(HLS_ROOT, f))
                    except FileNotFoundError:
                        pass
        except Exception:
            pass
        os.makedirs(chdir, exist_ok= True)
        for old in os.listdir(chdir):
            try:
                os.unlink(os.path.join(chdir, old))
            except FileNotFoundError:
                pass
        env = dict(os.environ)
        if use_proxy and PROXY:
            env["http_proxy"] = env["https_proxy"] = PROXY
            # 坑17 同款: 进程级 no_proxy='*' 会让 ffmpeg 连显式 http_proxy 都绕过
            # (libavformat/http.c 有 no_proxy 匹配, * 匹配所有主机) → 海外源静默直连失败
            env.pop("no_proxy", None)
            env.pop("NO_PROXY", None)
        errlog = open(os.path.join(HLS_ROOT, f"ff_{idx}.log"), "wb")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-timeout", "8000000",  # http 输入 8s 超时, 防源假死静默挂住 (12:42 事件)
               "-i", relay_url,
               "-vf", "scale=-2:1080",
               "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", "3500k", "-maxrate", "4500k", "-bufsize", "9000k",
               "-c:a", "aac", "-b:a", "128k", "-ac", "2",
               "-f", "hls",
               "-hls_time", "2",
               "-hls_list_size", "6",
               "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
               "-hls_segment_filename", os.path.join(chdir, "seg%05d.ts"),
               os.path.join(chdir, "live.m3u8")]
        # 坑16: stop() 会抢同一把 self.lock (Lock 不可重入), 持锁调用必自死锁
        # (12:42 事件真凶: 兜底线程冻死, Player.lock 被占, 后续换台全部排队)
        self.stop()
        try:
            with self.lock:
                # 子进程在 fork/exec 时已继承 fd 副本, 父进程这份必须关 —
                # 否则每次中转泄漏一个句柄 (长跑 + 频繁兜底会耗尽 fd)
                self.proc = subprocess.Popen(cmd, stderr=errlog, env=env)
        finally:
            errlog.close()
        # 等首个切片 (最多 15s)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = open(os.path.join(HLS_ROOT, f"ff_{idx}.log"), errors="replace").read()[:150]
                raise RuntimeError(f"ffmpeg 退出: {err}")
            m3u8 = os.path.join(chdir, "live.m3u8")
            if os.path.exists(m3u8) and "#EXTINF" in open(m3u8, errors="replace").read():
                return f"http://{MAC_IP}:{PORT}/hls/ch{idx}/live.m3u8"
            time.sleep(0.3)
        raise RuntimeError("15 秒内没有生成切片")

# ================= 播放管理 =================
class Player:
    def __init__(self):
        self.tv = TV()
        self.relay = Relay()
        self.refresher = Refresher()
        self.puller = SourcePuller()
        self.current = -1     # 当前频道索引
        self.cur_src = 0      # 当前源索引
        self.mode = None      # direct / relay / starting / error
        self.gen = 0          # 换台世代号: 每次 play +1, 旧线程发现 gen 过期即作废
        self.gen_lock = threading.Lock()
        self.lock = threading.Lock()

    def play(self, idx, src_idx=None):
        ch = CHANNELS[idx]
        name = ch["name"]
        sources = ch["sources"]
        if src_idx is None:
            src_idx = self.cur_src if idx == self.current else 0
        src_idx = max(0, min(src_idx, len(sources) - 1))
        ret = {"ok": True, "mode": "starting", "name": name, "src": src_idx}
        with self.gen_lock:
            self.gen += 1
            gen = self.gen
        # 乐观状态必须先于 t.start() 写: 若放在 start() 之后, GIL 可能在 start() 处
        # 切换到播放线程, 它瞬间跑完(电视拒连快/ffmpeg秒退)后主线程才恢复, 会用
        # "starting" 覆盖掉线程写好的最终态 (direct/relay/error) —— 真竞态, 已复现
        self.current = idx
        self.cur_src = src_idx
        self.mode = "starting"
        t = threading.Thread(target=self._play_sync,
                             args=(idx, src_idx, gen), daemon=True)
        t.start()
        return ret

    def _try_direct(self, url):
        """直连一条源: Set+Play 后核对电视实际 URI + PLAYING 才算成功 (坑12).
        只看 PLAYING 会被旧流骗过——电视拒绝换源时仍保持 PLAYING。"""
        r_set, r_play, final_url = self.tv.play_url(url)
        if r_set != 200 or r_play != 200:
            print(f"  [直连] Set/Play 失败 set={r_set} play={r_play} url={url[:80]}")
            return False
        st = uri = None  # 坑7: 先赋值, 异常路径也拿得到值 (失败日志不再重复打 state/uri)
        for _ in range(4):  # 最多等 8 秒
            time.sleep(2)
            st = self.tv.state()
            if st == "NO_MEDIA_PRESENT":
                break
            uri = self.tv.current_uri()
            if st == "PLAYING" and uri is not None and uri.rstrip("/") == final_url.rstrip("/"):
                return True
        print(f"  [直连] 未确认 state={st} uri={uri} want={final_url[:80]}")
        return False

    def _play_sync(self, idx, src_idx, gen):
        with self.lock:
            name = CHANNELS[idx]["name"]
            sources = CHANNELS[idx]["sources"]
            # ---- 直连优先: 用户选的源先试, 再按优先级轮询其余源 ----
            order = [src_idx] + [k for k in range(len(sources)) if k != src_idx]
            for k in order:
                if gen != self.gen:  # 连点多台只播最后一个: 旧请求及时作废, 不白跑 6×10s
                    print("[换台] 已被更新的请求取代, 放弃")
                    return
                print(f"[直连尝试] {name} 源{k}")
                if self._try_direct(sources[k]["url"]):
                    if gen != self.gen:
                        return
                    self.current = idx
                    self.cur_src = k
                    self.mode = "direct"
                    print(f"[直连] {name} 源{k}")
                    return
            print(f"[直连全失败→中转] {name} 源{src_idx}")
            # ---- 兜底: 中转 (用户选的源优先, 失败再按优先级转其它源; 本机 HLS 地址无 query, 电视必收) ----
            for k in order:
                if gen != self.gen:
                    return
                try:
                    self.relay.stop()
                    hls_url = self.relay.start(idx, sources[k]["url"], sources[k].get("proxy", False))
                    ok = self._try_direct(hls_url)
                    if gen != self.gen:
                        return
                    self.current = idx
                    self.cur_src = k
                    self.mode = "relay"
                    print(f"[中转] {name} 源{k} 确认={ok}")
                    return
                except Exception as e:
                    print(f"[中转失败] {name} 源{k}: {e}")
            if gen == self.gen:
                # 全部源直连+中转都失败: 置 error 态, 否则 mode 永远停在 "starting"
                self.mode = "error"
                print(f"[中转全失败] {name}: 所有源直连和中转都失败")

    def stop(self):
        with self.lock:
            self.relay.stop()
            self.tv.soap("Stop", "<InstanceID>0</InstanceID>")
            self.current = -1
            self.mode = None
            return {"ok": True}

    def status(self):
        return {"ch": self.current, "src": self.cur_src, "mode": self.mode, "state": self.tv.state(),
                "name": CHANNELS[self.current]["name"] if 0 <= self.current < len(CHANNELS) else None,
                "nsrc": len(CHANNELS[self.current]["sources"]) if 0 <= self.current < len(CHANNELS) else 0,
                "refresh": self.refresher.snapshot(), "pull": self.puller.snapshot()}

# ================= 源刷新 (页面"刷新源"按钮) =================
class Refresher:
    """后台重测各频道源地址可用性, 把失效源挪到该频道末尾 (不删除, 不中断当前播放)"""
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.result = None

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "done": self.done,
                    "total": self.total, "result": self.result}

    def start(self, player):
        with self.lock:
            if self.running:
                return False
            self.running, self.done, self.total, self.result = True, 0, 0, None
            skip = player.current
        threading.Thread(target=self._run, args=(skip,), daemon=True).start()
        return True

    def _alive(self, url, use_proxy=False):
        return alive_check(url, use_proxy=use_proxy)

    def _run(self, skip):
        try:
            marks = {}
            self.total = sum(len(c["sources"]) for c in CHANNELS)

            def probe(item):
                ci, si, url, use_proxy = item
                marks[(ci, si)] = self._alive(url, use_proxy)
                with self.lock:
                    self.done += 1

            parallel_map([(ci, si, s["url"], s.get("proxy", False))
                          for ci, c in enumerate(CHANNELS) for si, s in enumerate(c["sources"])],
                         probe)
            dead = reordered = skipped = 0
            with CH_LOCK:  # 与"拉源"并发时保护 sources 重排
                for ci, c in enumerate(CHANNELS):
                    srcs = c["sources"]
                    if ci == skip:  # 正在播的频道不动, 避免换掉当前源
                        skipped += 1
                        continue
                    order = sorted(range(len(srcs)), key=lambda si: (0 if marks.get((ci, si)) else 1, si))
                    if order != list(range(len(srcs))):
                        reordered += 1
                    dead += sum(1 for si in order if not marks.get((ci, si)))
                    c["sources"][:] = [srcs[i] for i in order]
            with self.lock:
                self.result = {"checked": len(marks), "dead": dead,
                               "reordered": reordered, "skipped": skipped}
            save_channels_file()  # 源顺序调整持久化
        except Exception as e:
            with self.lock:
                self.result = {"error": str(e)[:200]}
        finally:
            with self.lock:
                self.running = False

# ================= 远端源仓库拉取 (best-fan / iptv-api) =================
REMOTE_LISTS = [
    ("best-fan全量", "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_all.m3u8"),
    ("iptv-api(master)", "https://raw.githubusercontent.com/Guovin/iptv-api/master/output/result.m3u"),
    ("iptv-api(gd精准)", "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u"),
]
REMOTE_DIR = os.path.join(BASE, "remote_sources")

class SourcePuller:
    """从 GitHub 源仓库拉取 m3u 列表, 按频道名匹配、测活后合并进 channels.json"""
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stage = ""
        self.done = 0
        self.total = 0
        self.result = None
        self.entries = []
        self.fetched_at = None
        # 恢复上次拉取的缓存, 让筛选/添加无需先重新拉取
        try:
            with open(os.path.join(REMOTE_DIR, "parsed.json"), encoding="utf-8") as f:
                cache = json.load(f)
            self.entries = cache.get("entries", [])
            self.fetched_at = cache.get("fetched_at")
        except Exception:
            pass

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "stage": self.stage, "done": self.done,
                    "total": self.total, "result": self.result,
                    "entries": len(self.entries), "fetched_at": self.fetched_at}

    def _fetch(self, url):
        """curl 直连优先, 失败走设置里的代理.
        两个坑: ①不用 urllib——进程级 no_proxy='*' (保护 DLNA) 会让 urllib 连显式代理也被绕过;
        ②curl 子进程会继承 no_proxy 环境变量, 同样绕过显式 -x, 所以传清洗后的 env (坑17)"""
        env = dict(os.environ)
        env.pop("no_proxy", None)
        env.pop("NO_PROXY", None)
        for extra in ([], ["-x", PROXY] if PROXY else []):
            try:
                r = subprocess.run(["curl", "-sL", "--max-time", "25", "-A", "Mozilla/5.0",
                                    *extra, url], capture_output=True, timeout=30, env=env)
                if r.returncode == 0 and r.stdout:
                    text = r.stdout.decode(errors="replace")
                    if "#EXTINF" in text or text.lower().startswith("#"):
                        return text
            except Exception:
                continue
        return None

    @staticmethod
    def parse(text):
        """兼容 m3u (#EXTINF) 与 txt (名字,url1,url2) 两种格式"""
        out = []
        if "#EXTINF" in text:
            name = None
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#EXTINF"):
                    name = line.split(",", 1)[-1].strip()
                elif line.startswith("#"):
                    continue
                elif name:
                    for u in line.split(","):
                        u = u.strip()
                        if u.startswith(("http://", "https://")):
                            out.append({"name": name, "url": u})
                    name = None
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "," not in line:
                    continue
                parts = line.split(",")
                nm = parts[0].strip()
                for u in parts[1:]:
                    u = u.strip()
                    if u.startswith(("http://", "https://")):
                        out.append({"name": nm, "url": u})
        return out

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.running, self.done, self.total, self.result = True, 0, 0, None
            self.stage = "开始"
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        try:
            files = []
            entries = {}
            self.total = len(REMOTE_LISTS)
            for tag, url in REMOTE_LISTS:
                with self.lock:
                    self.stage = f"拉取 {tag}"
                text = self._fetch(url)
                n = 0
                if text:
                    for e in self.parse(text):
                        k = url_key(e["url"])
                        if k and k not in entries:
                            e["src"] = tag
                            entries[k] = e
                            n += 1
                files.append({"name": tag, "entries": n, "ok": bool(text)})
                with self.lock:
                    self.done += 1
            self.entries = list(entries.values())
            self.fetched_at = time.strftime("%m-%d %H:%M")
            try:
                os.makedirs(REMOTE_DIR, exist_ok=True)
                with open(os.path.join(REMOTE_DIR, "parsed.json"), "w", encoding="utf-8") as f:
                    json.dump({"fetched_at": self.fetched_at, "entries": self.entries}, f, ensure_ascii=False)
            except Exception:
                pass
            # ---- 按频道名匹配, 候选测活后追加 (每频道最多 8 条源, 跳过正在播频道) ----
            with CH_LOCK:
                index = {name_key(c["name"]): c for c in CHANNELS}
                by_key = {}
                for e in self.entries:
                    by_key.setdefault(name_key(e["name"]), []).append(e)
                todo = []
                for k, cands in by_key.items():
                    c = index.get(k)
                    if not c:
                        continue
                    have = {url_key(s["url"]) for s in c["sources"]}
                    room = max(0, 8 - len(c["sources"]))
                    for e in cands:
                        if room <= 0:
                            break
                        if url_key(e["url"]) not in have:
                            todo.append((k, e))
                            room -= 1
            with self.lock:
                self.stage = "测活合并"
                self.total = len(todo)
                self.done = 0
            marks = {}

            def probe(item):
                k, e = item
                marks[(k, url_key(e["url"]))] = alive_check(e["url"])
                with self.lock:
                    self.done += 1
            parallel_map(todo, probe)
            added = {}
            with CH_LOCK:  # 与"刷新源"并发时保护 append 与去重检查
                for k, e in todo:
                    if not marks.get((k, url_key(e["url"]))):
                        continue
                    c = index[k]
                    if len(c["sources"]) >= 8 or url_key(e["url"]) in {url_key(s["url"]) for s in c["sources"]}:
                        continue
                    c["sources"].append({"url": e["url"], "proxy": False})
                    added[c["name"]] = added.get(c["name"], 0) + 1
            err = save_channels_file()
            with self.lock:
                self.result = {"files": files, "total": len(self.entries),
                               "added": added, "save_err": err}
        except Exception as e:
            with self.lock:
                self.result = {"error": str(e)[:200]}
        finally:
            with self.lock:
                self.running = False

    def preview(self, kw):
        """按关键字筛选远端源, 按频道名分组"""
        kwk = str(kw or "").strip().upper()
        if not kwk:
            return {"total": 0, "groups": []}
        groups = {}
        for e in self.entries:
            if kwk in e["name"].upper():
                k = name_key(e["name"])
                g = groups.setdefault(k, {"name": e["name"], "count": 0, "hosts": []})
                g["count"] += 1
                host = urllib.parse.urlsplit(e["url"]).hostname or ""
                if host and host not in g["hosts"]:
                    g["hosts"].append(host)
        lst = sorted(groups.values(), key=lambda g: -g["count"])[:30]
        existing = {name_key(c["name"]) for c in CHANNELS}
        for g in lst:
            g["exists"] = name_key(g["name"]) in existing
        return {"total": sum(g["count"] for g in lst), "groups": lst,
                "fetched_at": self.fetched_at}

    def add_channel(self, name):
        """按频道名测活远端候选源, 全部存活才新增频道; 返回 (添加的源数, 错误)"""
        name = str(name or "").strip()
        if not name:
            return 0, "缺少频道名"
        k = name_key(name)
        cands = [e for e in self.entries if name_key(e["name"]) == k][:6]
        if not cands:
            return 0, "没有匹配的远端源 (请先拉源)"
        if any(name_key(c["name"]) == k for c in CHANNELS):
            return 0, "频道已存在"
        marks = {}

        def probe(e):
            marks[e["url"]] = alive_check(e["url"])
        parallel_map(cands, probe)
        alive = [e for e in cands if marks.get(e["url"])]
        if not alive:
            return 0, "候选源测活全部失败"
        err = None
        with CH_LOCK:
            # 测活要好几秒, 期间可能被并发刷新/添加改动, 落库前再查一次
            if any(name_key(c["name"]) == k for c in CHANNELS):
                return 0, "频道已存在"
            CHANNELS.append({"name": name, "sources": [{"url": e["url"], "proxy": False} for e in alive]})
            err = save_channels_file()
        return len(alive), err

# ================= 网页遥控器 =================
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>电视遥控</title><style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,PingFang SC,sans-serif;background:#111;color:#eee;margin:0 auto;
  padding:calc(10px + env(safe-area-inset-top)) 10px calc(16px + env(safe-area-inset-bottom));max-width:720px}
h1{font-size:17px;margin:4px 0 8px}
.now{background:#1c2b1c;border:1px solid #2f5d2f;color:#7ee787;border-radius:8px;padding:9px 10px;
  font-size:14px;margin-bottom:8px;line-height:1.45;word-break:break-all}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
button{font-family:inherit;padding:12px 8px;font-size:15px;border:0;border-radius:9px;background:#2a2a2e;
  color:#eee;text-align:left;min-height:46px;touch-action:manipulation}
button.on{background:#1f6f3f}
button:active{background:#3a3a3e}
.srcs{display:none;margin-bottom:8px;line-height:2}
.srclbl{font-size:13px;color:#999;margin-right:6px}
button.src{display:inline-block;width:auto;padding:7px 16px;margin:2px 4px 2px 0;font-size:14px;min-height:0;
  border-radius:7px;background:#333;border:1px solid #555}
button.srcon{background:#1f6f3f;border-color:#2f9f5f}
.ops{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.ops button{text-align:center;font-weight:600}
.stopbtn{background:#7f1d1d}
.stopbtn.off{background:#333;color:#888;font-weight:400}
.rbtn{background:#1c3d5c}
.pbtn{background:#4a3d1c}
.rbtn:disabled,.pbtn:disabled{opacity:.55}
.mode{font-size:12px;color:#888;margin-top:8px;text-align:center;min-height:15px;line-height:1.5}
.panel{display:none;background:#191922;border:1px solid #333;border-radius:10px;padding:4px 12px 12px;margin:10px 0}
.panel label{display:block;font-size:13px;color:#aaa;margin:10px 0 4px}
.panel input{width:100%;font-size:16px;padding:10px;border-radius:8px;border:1px solid #444;background:#101014;color:#eee}
.panel .hint{font-size:12px;color:#777;margin-top:10px;line-height:1.5}
@media (min-width:560px){
  .grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
  body{padding-top:calc(14px + env(safe-area-inset-top))}
}
</style></head><body>
<h1>📺 客厅电视</h1>
<div class="now" id="now">加载中…</div>
<div class="srcs" id="srcs"></div>
<div class="grid" id="chs"></div>
<div class="ops">
 <button class="rbtn" id="rbtn" onclick="refreshSrc()">🔄 刷新源</button>
 <button class="stopbtn" id="stopbtn" onclick="go('/api/stop')">⏹ 停止</button>
 <button class="pbtn" id="pbtn" onclick="pullSources()">⬇ 拉源</button>
 <button onclick="openSettings()">⚙ 设置</button>
</div>
<div class="panel" id="set">
 <label>电视 IP</label><input id="s_tv" autocomplete="off">
 <label>本机 IP (对电视可见)</label><input id="s_mac" autocomplete="off">
 <label>代理地址 (拉海外源用, 留空=不用)</label><input id="s_proxy" autocomplete="off" placeholder="http://ip:port">
 <label>ffmpeg 路径 (默认走系统 PATH)</label><input id="s_ff" autocomplete="off">
 <div class="ops" style="margin-top:12px">
  <button style="background:#1f6f3f;text-align:center" onclick="saveSettings()">💾 保存</button>
  <button style="background:#444;text-align:center" onclick="closeSettings()">取消</button>
 </div>
 <div class="hint">保存后写入 config.json，重启不丢失；修改电视 IP 会自动重连电视。</div>
 <div class="hint" style="border-top:1px solid #333;padding-top:8px;margin-top:14px">
  <b style="color:#bbb">📺 适配电视说明</b><br>
  ✅ <b>支持任何带 DLNA 投屏的电视/盒子</b>（海信、小米、TCL、创维、索尼等大多数智能电视）：本工具走标准 DLNA「推链接播放」协议，保存设置后会自动搜索电视 IP 上的投屏服务，乐播(HappyCast)优先。<br>
  ❌ 不支持 AirPlay（苹果投屏）、Chromecast、Miracast——这些是屏幕镜像协议，本工具是推链接播放，不镜像屏幕。<br>
  🔁 换电视：把上面「电视 IP」改成新电视的地址保存即可自动重连；个别品牌 DLNA 实现有差异，若换台失败请看服务端 server.log 排查。<br>
  ⏳ 电视只在服务启动和保存设置时搜索一次；电视中途换网或关机重开后，请重启服务或保存一次设置。
 </div>
 <label style="margin-top:14px">源仓库筛选 (best-fan / iptv-api，先点页面"拉源"或此处查询缓存)</label>
 <div style="display:flex;gap:6px">
  <input id="s_kw" style="flex:1" placeholder="关键字，如 CCTV1 / 湖南卫视" autocomplete="off">
  <button style="width:auto;min-height:0;padding:8px 14px;background:#1c3d5c;text-align:center" onclick="querySources()">查询</button>
 </div>
 <div id="kwres" style="font-size:13px;color:#bbb;max-height:240px;overflow:auto;margin-top:8px;line-height:1.6"></div>
</div>
<div class="mode" id="mode"></div>
<script>
let CH=[], refreshing=false, pulling=false;
// 外部数据(远端源仓库的频道名)进 innerHTML 前必须转义, 否则名字里带 <img onerror=...> 即 XSS
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function renderCH(){
 document.getElementById('chs').innerHTML=CH.map((c,i)=>
   `<button id="b${i}" onclick="go('/api/play/${i}')">${esc(c.name)}</button>`).join('');
}
function reloadCH(){
 fetch('/api/channels').then(r=>r.json()).then(d=>{CH=d;renderCH();upd();}).catch(()=>{});
}
fetch('/api/channels').then(r=>r.json()).then(d=>{
 CH=d; renderCH(); upd();
});
function upd(){fetch('/api/status').then(r=>r.json()).then(s=>{
 const now=document.getElementById('now'), srcs=document.getElementById('srcs');
 now.textContent = s.ch>=0 ? `▶ ${s.name} [${s.mode==='relay'?'中转':s.mode==='direct'?'直连':s.mode==='error'?'失败':(s.mode||'')}] ${s.state}` : '已停止';
 CH.forEach((c,i)=>{const b=document.getElementById('b'+i); if(b) b.className=(i===s.ch?'on':'');});
 if(s.ch>=0 && s.nsrc>1){
  srcs.innerHTML = '<span class="srclbl">线路:</span>' + Array.from({length:s.nsrc},(_,k)=>
   `<button class="src ${k===s.src?'srcon':''}" onclick="go('/api/play/${s.ch}?src='+${k})">${k+1}</button>`).join('');
  srcs.style.display='block';
 } else { srcs.style.display='none'; }
 const sb=document.getElementById('stopbtn');
 const playing = s.ch>=0 && s.state!=='STOPPED';
 if(playing){ sb.textContent='⏹ 停止'; sb.className='stopbtn'; sb.disabled=false; }
 else { sb.textContent='⏹ 已停止'; sb.className='stopbtn off'; sb.disabled=true; }
 const rb=document.getElementById('rbtn'), md=document.getElementById('mode');
 const rf=s.refresh||{};
 if(rf.running){
  refreshing=true; rb.disabled=true;
  rb.textContent=rf.total? `刷新中 ${rf.done}/${rf.total}` : '刷新中…';
  md.textContent='正在检测各频道源地址可用性, 失效源将自动后移…';
 } else if(refreshing){
  refreshing=false; rb.disabled=false; rb.textContent='🔄 刷新源';
  if(rf.result){
   if(rf.result.error){ md.textContent='刷新失败: '+rf.result.error; }
   else{
    md.textContent=`刷新完成: 检测 ${rf.result.checked} 条源, 失效 ${rf.result.dead} 条已后移, 调整 ${rf.result.reordered} 个频道`+
      (rf.result.skipped?` (跳过正在播的 ${rf.result.skipped} 个)`:'');
    setTimeout(()=>{md.textContent='';},8000);
   }
  }
 }
 const pb=document.getElementById('pbtn');
 const pl=s.pull||{};
 if(pl.running){
  pulling=true; pb.disabled=true;
  pb.textContent=pl.stage + (pl.total? ` ${pl.done}/${pl.total}` : '…');
  md.textContent=`拉取远端源中 (${pl.entries} 条已缓存)…`;
 } else if(pulling){
  pulling=false; pb.disabled=false; pb.textContent='⬇ 拉源';
  const r=pl.result;
  if(r){
   if(r.error){ md.textContent='拉源失败: '+r.error; }
   else{
    const ok=r.files.filter(f=>f.ok).map(f=>`${f.name} ${f.entries}条`).join('，');
    const addN=Object.values(r.added||{}).reduce((a,b)=>a+b,0);
    md.textContent=`拉源完成 (${r.total} 条去重)${ok?'：'+ok:''}`+
      (addN? `；测活新增 ${addN} 条到 ${Object.keys(r.added).length} 个频道` : '；无新增')+
      (r.save_err?'；写入失败!':'');
    setTimeout(()=>{md.textContent='';},12000);
   }
  }
 }
}).catch(()=>{});}
function refreshSrc(){
 if(refreshing) return;
 fetch('/api/refresh').then(r=>r.json()).then(d=>{
  if(d.ok){ refreshing=true; setTimeout(upd,300); }
 }).catch(()=>{});
}
function pullSources(){
 if(pulling) return;
 fetch('/api/pull').then(r=>r.json()).then(d=>{
  if(d.ok){ pulling=true; setTimeout(upd,300); }
 }).catch(()=>{});
}
function querySources(){
 const kw=document.getElementById('s_kw').value.trim();
 const box=document.getElementById('kwres');
 if(!kw){ box.textContent='请输入关键字'; return; }
 box.textContent='查询中…';
 fetch('/api/sources/preview?kw='+encodeURIComponent(kw)).then(r=>r.json()).then(d=>{
  if(!d.groups || !d.groups.length){
   box.textContent = d.fetched_at? `无匹配 (缓存 ${d.fetched_at}，共 ${d.total||0} 条)` : '缓存为空，请先点页面"⬇ 拉源"';
   return;
  }
  box.innerHTML = `<div style="color:#888;margin-bottom:4px">匹配 ${d.total} 条 / ${d.groups.length} 个频道名 (缓存 ${d.fetched_at})</div>` +
   d.groups.map(g=>{
    const nm=encodeURIComponent(g.name).replace(/'/g,"%27");
    const hosts=g.hosts.slice(0,2).map(esc).join('，');
    return `<div style="margin:3px 0">● ${esc(g.name)} <span style="color:#888">(${g.count}条 | ${hosts})</span> `+
      (g.exists? '<span style="color:#7ee787">✓已有</span>'
               : `<button style="width:auto;min-height:0;padding:4px 10px;font-size:12px;background:#1f6f3f;text-align:center" onclick="addChannel(decodeURIComponent('${nm}'))">测活添加</button>`)+
      `</div>`;
   }).join('');
 }).catch(()=>{ box.textContent='查询失败'; });
}
function addChannel(name){
 fetch('/api/sources/add?name='+encodeURIComponent(name)).then(r=>r.json()).then(d=>{
  if(d.ok){
   querySources(); reloadCH();
   const md=document.getElementById('mode');
   md.textContent=`已添加频道「${d.name}」(${d.sources} 条存活源)`;
   setTimeout(()=>{md.textContent='';},6000);
  } else { alert('添加失败: '+d.error); }
 }).catch(()=>{});
}
function openSettings(){
 fetch('/api/settings').then(r=>r.json()).then(d=>{
  document.getElementById('s_tv').value=d.TV_IP;
  document.getElementById('s_mac').value=d.MAC_IP;
  document.getElementById('s_proxy').value=d.PROXY;
  document.getElementById('s_ff').value=d.FFMPEG;
  document.getElementById('set').style.display='block';
 }).catch(()=>{});
}
function closeSettings(){ document.getElementById('set').style.display='none'; }
function saveSettings(){
 const q=new URLSearchParams({
  tv_ip:document.getElementById('s_tv').value.trim(),
  mac_ip:document.getElementById('s_mac').value.trim(),
  proxy:document.getElementById('s_proxy').value.trim(),
  ffmpeg:document.getElementById('s_ff').value.trim()});
 fetch('/api/settings/save?'+q.toString()).then(r=>r.json()).then(d=>{
  const md=document.getElementById('mode');
  if(d.ok){
   closeSettings();
   md.textContent='设置已保存到 config.json'+(d.reconnecting?'，正在重连电视…':'');
   setTimeout(()=>{md.textContent='';},6000);
  } else { md.textContent='保存失败: '+d.error; }
 }).catch(()=>{ md.textContent='保存请求失败'; });
}
function go(u){fetch(u).then(r=>r.json()).then(d=>{ if(d && d.ok===false) alert('播放失败: '+(d.name||'')); setTimeout(upd,800); setTimeout(upd,3000);});}
setInterval(upd,2500);
upd();
</script></body></html>"""

# ================= HTTP 服务 =================
PORT = 8899

class Handler(http.server.BaseHTTPRequestHandler):
    player: Player = None

    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        self._responded = True
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # 统一兜底: handler 抛异常时连接被直接断掉, 前端 .catch(()=>{}) 静默无感
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端主动断开 (页面刷新/快速切台), 正常现象
        except Exception as e:
            print(f"[HTTP] {self.path} 处理异常: {e}")
            if not getattr(self, "_responded", False):  # 已开始发送响应就不能再补 500
                try:
                    self.reply(500, json.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False))
                except Exception:
                    pass

    def _do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        # HLS 切片服务 (兜底模式)
        m = re.match(r"^/hls/ch(\d+)/(live\.m3u8|seg\d+\.ts)$", path)
        if m:
            fname = os.path.join(HLS_ROOT, f"ch{m.group(1)}", m.group(2))
            if not os.path.exists(fname):
                return self.reply(404, b"{}", "video/mp2t")
            data = open(fname, "rb").read()
            ctype = "application/vnd.apple.mpegurl" if fname.endswith(".m3u8") else "video/mp2t"
            self.send_response(200)
            self._responded = True
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/", "/remote"):
            return self.reply(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/channels":
            return self.reply(200, json.dumps(
                [{"name": c["name"], "nsrc": len(c["sources"])} for c in CHANNELS], ensure_ascii=False))
        if path == "/api/status":
            return self.reply(200, json.dumps(self.player.status(), ensure_ascii=False))
        m = re.match(r"^/api/play/(\d+)$", path)
        if m:
            idx = int(m.group(1))
            if not CHANNELS:
                return self.reply(404, '{"error":"频道列表为空, 请先用页面拉源或编辑 channels.json"}')
            if idx >= len(CHANNELS):
                return self.reply(404, '{"error":"no such channel"}')
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                src_idx = int(qs["src"][0]) if "src" in qs else None
            except (KeyError, IndexError, ValueError):
                src_idx = None  # src=abc 之类的脏参数当作默认源, 不炸 500
            try:
                return self.reply(200, json.dumps(self.player.play(idx, src_idx), ensure_ascii=False))
            except Exception as e:
                return self.reply(500, json.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False))
        if path == "/api/stop":
            return self.reply(200, json.dumps(self.player.stop(), ensure_ascii=False))
        if path == "/api/refresh":
            started = self.player.refresher.start(self.player)
            return self.reply(200, json.dumps({"ok": True, "started": started}, ensure_ascii=False))
        if path == "/api/pull":
            started = self.player.puller.start()
            return self.reply(200, json.dumps({"ok": True, "started": started}, ensure_ascii=False))
        if path == "/api/sources/preview":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self.reply(200, json.dumps(self.player.puller.preview(qs.get("kw", [""])[0]), ensure_ascii=False))
        if path == "/api/sources/add":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = qs.get("name", [""])[0]
            added, err = self.player.puller.add_channel(name)
            if err:
                return self.reply(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            return self.reply(200, json.dumps({"ok": True, "name": name, "sources": added}, ensure_ascii=False))
        if path == "/api/settings":
            return self.reply(200, json.dumps(current_settings(), ensure_ascii=False))
        if path == "/api/settings/save":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            err = save_settings({k: qs.get(k, [""])[0] for k in ("tv_ip", "mac_ip", "proxy", "ffmpeg")})
            if err:
                return self.reply(400, json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            # TV_IP 可能变了: 后台重建 TV 控制通道 (SSDP 重发现/重猜端口)
            threading.Thread(target=lambda: setattr(self.player, "tv", TV()), daemon=True).start()
            print(f"[设置] 已保存: TV={TV_IP} MAC={MAC_IP} PROXY={PROXY or '(无)'} FFMPEG={FFMPEG}, 电视重连中")
            return self.reply(200, json.dumps({"ok": True, "reconnecting": True}, ensure_ascii=False))
        return self.reply(404, '{"error":"not found"}')

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class _Tee:
    """stdout/stderr 同时写终端与 server.log——本进程曾把日志打进已死管道全丢, 排错无凭据"""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

def main():
    global PORT
    load_config()  # config.json 覆盖默认配置 (须在任何组件使用 TV_IP/MAC_IP/PROXY/FFMPEG 之前)
    load_channels()  # 频道表从 channels.json 读取
    # 局域网地址绝不走系统代理 (电视 SOAP/DLNA 控制会被代理拦截)
    for v in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(v, None)
    os.environ["no_proxy"] = "*"
    log_path = os.path.join(BASE, "server.log")
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            os.replace(log_path, log_path + ".1")  # 超 5MB 轮转一份, 常驻服务日志不无限涨
    except Exception:
        pass
    logf = open(log_path, "a", buffering=1)
    sys.stdout = _Tee(sys.stdout, logf)
    sys.stderr = _Tee(sys.stderr, logf)
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    PORT = args.port

    if not shutil.which(FFMPEG):
        print(f"[警告] PATH 中找不到 ffmpeg ({FFMPEG}), 兜底中转模式不可用; 可设环境变量 FFMPEG 指定路径")

    player = Player()
    Handler.player = player

    def _on_sigterm(_sig, _frm):
        # pkill 等默认发 SIGTERM: 必须先杀 ffmpeg 子进程再退出, 否则孤儿转码继续
        # 1080p 烧 CPU/上行 (坑14). 不抢 Player.lock (可能被换台线程持有几十秒), 直接停 relay
        print("\n[退出] 收到 SIGTERM, 停止中转…")
        try:
            player.relay.stop()
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    print(f"[OK] 遥控器: http://{MAC_IP}:{PORT}/")
    print(f"[OK] 频道: {len(CHANNELS)} 个 | 双模式: 直连优先, 失败自动转 Mac 中转")
    print("[OK] Ctrl+C 退出")
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[退出] 停止中转与播放…")
            player.stop()

if __name__ == "__main__":
    main()
