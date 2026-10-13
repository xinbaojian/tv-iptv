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
import json
import os
import re
import socket
import socketserver
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

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
        v = str(cfg.get(k) or "").strip()
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
    ip_re = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
    tv = str(d.get("tv_ip") or "").strip()
    mac = str(d.get("mac_ip") or "").strip()
    proxy = str(d.get("proxy") or "").strip()
    ff = str(d.get("ffmpeg") or "").strip()
    if not ip_re.match(tv):
        return "电视 IP 格式不对"
    if not ip_re.match(mac):
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
    """把内存中的频道表写回 channels.json (刷新源/拉源后调用)"""
    try:
        with open(CHANNELS_PATH, "w", encoding="utf-8") as f:
            json.dump(CHANNELS, f, ensure_ascii=False, indent=1)
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

def alive_check(url, timeout=4):
    """源地址可用性: HTTP 200 且返回有效 HLS 播放列表"""
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

def guess_tv_ctrl():
    """SSDP 失败时直接猜 49152 (乐播 DLNA 默认端口)"""
    return f"http://{TV_IP}:49152/_urn:schemas-upnp-org:service:AVTransport_control", f"未知设备({TV_IP})"

# ================= DLNA 控制 =================
class TV:
    def __init__(self):
        url, name = find_tv() or guess_tv_ctrl()
        self.ctrl, self.name = url, name
        print(f"[电视] 控制: {self.ctrl} (设备: {self.name})")

    def soap(self, action, args_xml):
        # 坑12修复: 失败必须返回 int (原返回错误字符串, 调用方 r_set==200 比较永远 False,
        # 且 500 被吞成字符串导致"直连为何失败"无从排查)
        return self.soap_raw(action, args_xml)[0]

    def state(self):
        for attempt in range(3):
            code, body = self.soap_raw("GetTransportInfo", "<InstanceID>0</InstanceID>")
            if code == 200:
                m = re.search(r"CurrentTransportState>([^<]+)", body)
                if m:
                    return m.group(1)
            time.sleep(1)
        return "UNKNOWN"

    def soap_raw(self, action, args_xml):
        body = (f'<?xml version="1.0" encoding="utf-8"?>'
                f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                f'<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                f'{args_xml}</u:{action}></s:Body></s:Envelope>').encode()
        req = urllib.request.Request(self.ctrl, data=body, headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"'})
        try:
            r = urllib.request.urlopen(req, timeout=8)
            return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:200]
        except Exception as e:
            return 0, str(e)

    def current_uri(self):
        """电视实际持有的 URI (GetMediaInfo). 状态 PLAYING 不代表播的是新 URL, 必须核对它"""
        for attempt in range(3):
            code, body = self.soap_raw("GetMediaInfo", "<InstanceID>0</InstanceID>")
            if code == 200:
                m = re.search(r"<CurrentURI>(.*?)</CurrentURI>", body)
                if m:
                    return m.group(1)
            time.sleep(1)
        return None

    def play_url(self, url):
        """设置 URI 并播放. 乐播对带 query 参数的 URL 返回 500 (坑12),
        剥掉 query 重试一次; 返回 (set状态码, play状态码, 实际下发的URL)"""
        r_set = self.soap("SetAVTransportURI",
                          f"<InstanceID>0</InstanceID><CurrentURI>{url}</CurrentURI>"
                          f"<CurrentURIMetaData>&lt;DIDL-Lite&gt;&lt;/DIDL-Lite&gt;</CurrentURIMetaData>")
        final_url = url
        if r_set != 200 and "?" in url:
            stripped = url.split("?", 1)[0]
            r2 = self.soap("SetAVTransportURI",
                           f"<InstanceID>0</InstanceID><CurrentURI>{stripped}</CurrentURI>"
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
        os.makedirs(chdir, exist_ok= True)
        for old in os.listdir(chdir):
            try:
                os.unlink(os.path.join(chdir, old))
            except FileNotFoundError:
                pass
        env = dict(os.environ)
        if use_proxy and PROXY:
            env["http_proxy"] = env["https_proxy"] = PROXY
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
        with self.lock:
            self.proc = subprocess.Popen(cmd, stderr=errlog, env=env)
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
        self.mode = None      # direct / relay
        self.lock = threading.Lock()

    def play(self, idx, src_idx=None):
        ch = CHANNELS[idx]
        name = ch["name"]
        sources = ch["sources"]
        if src_idx is None:
            src_idx = self.cur_src if idx == self.current else 0
        src_idx = max(0, min(src_idx, len(sources) - 1))
        url = sources[src_idx]["url"]
        use_proxy = sources[src_idx].get("proxy", False)
        ret = {"ok": True, "mode": "starting", "name": name, "src": src_idx}
        t = threading.Thread(target=self._play_sync, args=(idx, src_idx, url, use_proxy), daemon=True)
        t.start()
        self.current = idx
        self.cur_src = src_idx
        self.mode = "starting"
        return ret

    def _try_direct(self, url):
        """直连一条源: Set+Play 后核对电视实际 URI + PLAYING 才算成功 (坑12).
        只看 PLAYING 会被旧流骗过——电视拒绝换源时仍保持 PLAYING。"""
        r_set, r_play, final_url = self.tv.play_url(url)
        if r_set != 200 or r_play != 200:
            print(f"  [直连] Set/Play 失败 set={r_set} play={r_play} url={url[:80]}")
            return False
        for _ in range(4):  # 最多等 8 秒
            time.sleep(2)
            st = self.tv.state()
            if st == "NO_MEDIA_PRESENT":
                break
            uri = self.tv.current_uri()
            if st == "PLAYING" and uri is not None and uri.rstrip("/") == final_url.rstrip("/"):
                return True
        print(f"  [直连] 未确认 state={self.tv.state()} uri={self.tv.current_uri()} want={final_url[:80]}")
        return False

    def _play_sync(self, idx, src_idx, url, use_proxy):
        with self.lock:
            name = CHANNELS[idx]["name"]
            sources = CHANNELS[idx]["sources"]
            # ---- 直连优先: 用户选的源先试, 再按优先级轮询其余源 ----
            order = [src_idx] + [k for k in range(len(sources)) if k != src_idx]
            for k in order:
                print(f"[直连尝试] {name} 源{k}")
                if self._try_direct(sources[k]["url"]):
                    self.current = idx
                    self.cur_src = k
                    self.mode = "direct"
                    print(f"[直连] {name} 源{k}")
                    return
            print(f"[直连全失败→中转] {name} 源{src_idx}")
            # ---- 兜底: 中转 (用用户指定的源转码, 本机 HLS 地址无 query, 电视必收) ----
            try:
                self.relay.stop()
                hls_url = self.relay.start(idx, url, use_proxy)
                ok = self._try_direct(hls_url)
                self.current = idx
                self.cur_src = src_idx
                self.mode = "relay"
                print(f"[中转] {name} 源{src_idx} 确认={ok}")
            except Exception as e:
                print(f"[中转失败] {name}: {e}")

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

    def _alive(self, url):
        return alive_check(url)

    def _run(self, skip):
        try:
            sem = threading.Semaphore(4)
            marks = {}
            self.total = sum(len(c["sources"]) for c in CHANNELS)
            def probe(ci, si, url):
                with sem:
                    marks[(ci, si)] = self._alive(url)
                    with self.lock:
                        self.done += 1
            ts = [threading.Thread(target=probe, args=(ci, si, s["url"]), daemon=True)
                  for ci, c in enumerate(CHANNELS) for si, s in enumerate(c["sources"])]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            dead = reordered = skipped = 0
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
            sem = threading.Semaphore(4)
            marks = {}
            def probe(k, e):
                with sem:
                    marks[(k, url_key(e["url"]))] = alive_check(e["url"])
                    with self.lock:
                        self.done += 1
            ts = [threading.Thread(target=probe, args=(k, e), daemon=True) for k, e in todo]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            added = {}
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
        sem = threading.Semaphore(4)
        def probe(e):
            with sem:
                marks[e["url"]] = alive_check(e["url"])
        ts = [threading.Thread(target=probe, args=(e,), daemon=True) for e in cands]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        alive = [e for e in cands if marks.get(e["url"])]
        if not alive:
            return 0, "候选源测活全部失败"
        CHANNELS.append({"name": name, "sources": [{"url": e["url"], "proxy": False} for e in alive]})
        save_channels_file()
        return len(alive), None

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
function renderCH(){
 document.getElementById('chs').innerHTML=CH.map((c,i)=>
   `<button id="b${i}" onclick="go('/api/play/${i}')">${c.name}</button>`).join('');
}
function reloadCH(){
 fetch('/api/channels').then(r=>r.json()).then(d=>{CH=d;renderCH();upd();}).catch(()=>{});
}
fetch('/api/channels').then(r=>r.json()).then(d=>{
 CH=d; renderCH(); upd();
});
function upd(){fetch('/api/status').then(r=>r.json()).then(s=>{
 const now=document.getElementById('now'), srcs=document.getElementById('srcs');
 now.textContent = s.ch>=0 ? `▶ ${s.name} [${s.mode==='relay'?'中转':s.mode==='direct'?'直连':(s.mode||'')}] ${s.state}` : '已停止';
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
    const esc=g.name.replace(/'/g,"");
    const hosts=g.hosts.slice(0,2).join('，');
    return `<div style="margin:3px 0">● ${g.name} <span style="color:#888">(${g.count}条 | ${hosts})</span> `+
      (g.exists? '<span style="color:#7ee787">✓已有</span>'
               : `<button style="width:auto;min-height:0;padding:4px 10px;font-size:12px;background:#1f6f3f;text-align:center" onclick="addChannel('${esc}')">测活添加</button>`)+
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
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
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
            if idx >= len(CHANNELS):
                return self.reply(404, '{"error":"no such channel"}')
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            src_idx = int(qs["src"][0]) if "src" in qs else None
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
    logf = open(os.path.join(BASE, "server.log"), "a", buffering=1)
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
