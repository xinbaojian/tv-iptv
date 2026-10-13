#!/usr/bin/env python3
"""
源速度感知排序回归测试 (坑25)
================================
背景: 「刷新源」原来只测"活不活"(HTTP 200 + #EXTINF)。晚高峰源站照样回 200,
但吞吐只有码率的 1/3 —— 下载追不上播放, 电视端必卡, 而慢源一直霸占第 0 位。

本测试用本地假源站复现三种源: 快(余量足) / 慢(活着但喂不动) / 死(404),
验证 speed_check 的余量计算和 Refresher 的排序逻辑:
    python3 tests/test_source_speed.py
"""
import http.server
import importlib.util
import os
import socket
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_tv():
    spec = importlib.util.spec_from_file_location("tv_speed_test", os.path.join(BASE, "tv.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["tv_speed_test"] = m
    spec.loader.exec_module(m)
    return m

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

# 一个"分片"的体积与时长: 2MB / 4.0s => 码率 4 Mbps
SEG_BYTES = 2 * 1024 * 1024
SEG_DUR = 4.0

def make_source(seg_delay):
    """起一个假 HLS 源: m3u8 + 一个 ts 分片. seg_delay = 响应分片前先睡几秒 (模拟慢源)"""
    hits = {"m3u8": 0, "ts": 0}
    lock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith(".m3u8"):
                with lock:
                    hits["m3u8"] += 1
                body = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:%.1f,\nseg.ts\n"
                        % SEG_DUR).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.endswith(".ts"):
                if seg_delay:
                    time.sleep(seg_delay)
                with lock:
                    hits["ts"] += 1
                start, end = 0, SEG_BYTES - 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    start = int(a)
                    end = min(int(b), SEG_BYTES - 1) if b else SEG_BYTES - 1
                chunk = (start, end + 1)
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, SEG_BYTES))
                self.send_header("Content-Length", str(chunk[1] - chunk[0]))
                self.end_headers()
                self.wfile.write(b"\x47" * (chunk[1] - chunk[0]))
                return
            self.send_error(404)

    class S(http.server.ThreadingHTTPServer):
        daemon_threads = True

    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/live.m3u8" % srv.server_address[1], hits

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")

def main():
    tv = load_tv()
    fast_srv, fast_url, _ = make_source(seg_delay=0.0)      # 秒回 => 余量巨大
    slow_srv, slow_url, _ = make_source(seg_delay=6.0)      # 4s 的片要 6s+ 才回 => 余量 <1
    dead_port = free_port()
    dead_url = "http://127.0.0.1:%d/live.m3u8" % dead_port  # 无人监听 => 死源

    print("\n[1] speed_check: 余量 = 片时长 / 整片下载耗时")
    m_fast = tv.speed_check(fast_url)
    m_slow = tv.speed_check(slow_url)
    m_dead = tv.speed_check(dead_url)
    check("快源余量为正数", isinstance(m_fast, float) and m_fast > 1.0, "%.2f" % (m_fast or -1))
    check("慢源(4s 片要 6s+)余量 < 1", isinstance(m_slow, float) and 0 < m_slow < 1.0,
          "%.3f" % (m_slow or -1))
    check("慢源余量明显小于快源", (m_slow or 0) < (m_fast or 99) / 10.0)
    check("死源返回 None", m_dead is None)
    check("Range 命中时余量按整片推算 (非按 1MB 局部)",
          isinstance(m_fast, float) and m_fast > 1.0,
          "2MB 片只拉了头部 1MB, 仍算出整片口径")

    print("\n[2] Refresher: 活源按余量降序, 死源垫底 (坑25 核心)")
    saved_channels, saved_save = tv.CHANNELS, tv.save_channels_file
    tv.CHANNELS = [{"name": "测试频道", "sources": [
        {"url": slow_url, "proxy": False},   # 原第 0 位: 活但喂不动
        {"url": dead_url, "proxy": False},   # 原第 1 位: 死
        {"url": fast_url, "proxy": False},   # 原第 2 位: 活且快
    ]}]
    tv.save_channels_file = lambda: None
    try:
        r = tv.Refresher()
        r._run(skip=-1)
        order = [s["url"] for s in tv.CHANNELS[0]["sources"]]
        check("快源升到第 0 位", order[0] == fast_url, order[0])
        check("慢源降到快源之后", order[1] == slow_url)
        check("死源垫底", order[2] == dead_url)
        res = r.snapshot()["result"] or {}
        check("结果里 dead=1", res.get("dead") == 1, str(res))
        check("结果里 slow=1 (活着但喂不动)", res.get("slow") == 1, str(res))
    finally:
        tv.CHANNELS, tv.save_channels_file = saved_channels, saved_save

    print("\n[3] 中转切片: -hls_time 2 不再被默认 GOP 架空 (坑25伴生)")
    src = open(os.path.join(BASE, "tv.py"), encoding="utf-8").read()
    check("强制每 2s 关键帧的参数在位",
          all(t in src for t in ('"-force_key_frames"', '"expr:gte(t,n_forced*2)"',
                                 '"-sc_threshold"', '"-keyint_min"')))

    fast_srv.shutdown()
    slow_srv.shutdown()
    print(f"\n{'=' * 52}\n结果: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    print('=' * 52)
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
