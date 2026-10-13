#!/usr/bin/env python3
"""
电视控制端点发现回归测试 (坑24)
================================
背景: 乐播 DLNA 端口不固定, 电视/投屏应用重启后会重选端口 (亲历 49152 → 49153)。
SSDP 若拿不到应答 (宿主防火墙拦掉组播回包), 旧代码死猜 49152 → 所有 SOAP 被拒
→ 遥控器每个台都 set=0 play=0, 也就是"所有频道无法播放"。

本测试用假 DLNA 服务端复现该场景, 不依赖真电视, 随时可跑:
    python3 tests/test_tv_discovery.py
"""
import http.server
import importlib.util
import os
import socket
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_tv():
    spec = importlib.util.spec_from_file_location("tv_under_test", os.path.join(BASE, "tv.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["tv_under_test"] = m
    spec.loader.exec_module(m)
    return m

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

class FakeRenderer(http.server.BaseHTTPRequestHandler):
    """只认正确的 SOAP 路径 + SOAPACTION 的假 DLNA MediaRenderer.
    同时支持乐播 (/_urn:...AVTransport_control) 与海信 HiSmart (/upnp/control/mingusavtr) 两种路径"""
    lebo_path = "/_urn:schemas-upnp-org:service:AVTransport_control"
    hismart_path = "/upnp/control/mingusavtr"

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path not in (self.lebo_path, self.hismart_path):
            self.send_error(404)
            return
        action = self.headers.get("SOAPACTION", "")
        if "GetTransportInfo" not in action:
            self.send_error(500)
            return
        body = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                b'<s:Body><u:GetTransportInfoResponse '
                b'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                b'<CurrentTransportState>PAUSED_PLAYBACK</CurrentTransportState>'
                b'</u:GetTransportInfoResponse></s:Body></s:Envelope>')
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

class PlainHTTP(http.server.BaseHTTPRequestHandler):
    """端口通、但不是 DLNA 控制端点 (对任何请求都回 200 空 JSON), 用来验证"端口通≠可用" """
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST

def serve(handler):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")

def main():
    tv = load_tv()
    dead = free_port()                       # 无人监听 → 连接被拒
    his, his_port = serve(FakeRenderer)      # 假 HiSmart (端口固定那类)
    lebo, lebo_port = serve(FakeRenderer)    # 假乐播 (端口漂移那类)
    plain, plain_port = serve(PlainHTTP)

    his_good = f"http://127.0.0.1:{his_port}{FakeRenderer.hismart_path}"
    lebo_good = f"http://127.0.0.1:{lebo_port}{FakeRenderer.lebo_path}"

    print("\n[1] soap_probe: 只有真 SOAP 端点才算活")
    check("真 DLNA 端点 → True", tv.soap_probe(his_good) is True)
    check("乐播路径端点 → True", tv.soap_probe(lebo_good) is True)
    check("无人监听端口 → False", tv.soap_probe(f"http://127.0.0.1:{dead}{FakeRenderer.lebo_path}") is False)
    check("端口通但非 SOAP → False", tv.soap_probe(f"http://127.0.0.1:{plain_port}{FakeRenderer.lebo_path}") is False,
          "端口通 ≠ 可用, 必须验 SOAP")

    print("\n[2] probe_ctrl_candidates: 并行探测能在候选里挑出活的那个")
    cands = [f"http://127.0.0.1:{dead}{FakeRenderer.lebo_path}", his_good]
    check("挑中活端点", tv.probe_ctrl_candidates(cands) == his_good)
    check("全死则返回 None",
          tv.probe_ctrl_candidates([f"http://127.0.0.1:{dead}{FakeRenderer.lebo_path}"]) is None)
    check("空候选返回 None", tv.probe_ctrl_candidates([]) is None)

    print("\n[3] 核心回归: SSDP 无应答时必须探测出真端口, 而不是死猜 49152")
    tv.TV_IP = "127.0.0.1"
    tv.find_tv = lambda *a, **k: None                      # 模拟防火墙拦掉 SSDP 回包
    tv.HISMART_PORTS = (his_port,)
    tv.LEBO_PORTS = (49152, lebo_port)                     # 49152 是旧的硬编码猜测 (死的)
    url, name = tv.discover_tv_ctrl()
    check("发现真实端点", url == his_good, url)
    check("不再返回已失效的 49152", ":49152/" not in url, url)

    print("\n[4] 优先级: HiSmart 固定端口 > SSDP > 乐播漂移端口")
    tv.find_tv = lambda *a, **k: (lebo_good, "客厅电视")   # SSDP 也活着
    url, _ = tv.discover_tv_ctrl()
    check("HiSmart 与 SSDP 都可用时选 HiSmart (端口更稳)", url == his_good, url)

    tv.HISMART_PORTS = (dead,)                             # HiSmart 不可用 → 退回 SSDP
    url, name = tv.discover_tv_ctrl()
    check("HiSmart 挂了则用 SSDP 结果", url == lebo_good, url)
    check("SSDP 路径下保留友好设备名", name == "客厅电视", name)

    tv.find_tv = lambda *a, **k: (f"http://127.0.0.1:{dead}{FakeRenderer.lebo_path}", "死端点")
    url, _ = tv.discover_tv_ctrl()
    check("SSDP 结果已死 → 落到乐播端口列表", url == lebo_good, url)

    tv.LEBO_PORTS = (dead,)                                # 全灭 → 兜底默认值
    url, _ = tv.discover_tv_ctrl()
    check("全灭时返回兜底地址 (不抛异常)", url.startswith("http://127.0.0.1:"), url)

    print("\n[5] TV 类: 连续失败后自动重新发现控制端点")
    obj = tv.TV.__new__(tv.TV)                             # 不走 __init__, 不碰真电视
    obj.soap_lock = threading.Lock()
    obj.fail_lock = threading.Lock()
    obj.fail_streak = 0
    obj.rediscovering = False
    obj.last_rediscover = 0.0
    obj.ctrl = f"http://127.0.0.1:{dead}{FakeRenderer.lebo_path}"
    obj.name = "旧端点"
    obj.REDISCOVER_AFTER = 3
    obj.REDISCOVER_COOLDOWN = 10.0
    tv.discover_tv_ctrl = lambda *a, **k: (his_good, "客厅电视")
    code, _ = obj.soap_raw("GetTransportInfo", "<InstanceID>0</InstanceID>", timeout=1)
    check("第 1 次失败: 尚未重发现 (不误伤瞬时抖动)", obj.ctrl != his_good and code == 0)
    obj.soap_raw("GetTransportInfo", "<InstanceID>0</InstanceID>", timeout=1)
    obj.soap_raw("GetTransportInfo", "<InstanceID>0</InstanceID>", timeout=1)
    check("第 3 次失败: 已切到新端点", obj.ctrl == his_good, obj.ctrl)
    code, _ = obj.soap_raw("GetTransportInfo", "<InstanceID>0</InstanceID>", timeout=1)
    check("切换后 SOAP 恢复正常", code == 200, f"code={code}")

    print("\n[6] 真电视联调 (可选; 用 TV_IP 环境变量指定, 不在线则跳过)")
    tv2 = load_tv()
    real_ip = os.environ.get("TV_IP", "").strip()
    if not real_ip:
        print("       未设 TV_IP 环境变量, 跳过 (例: TV_IP=192.168.1.10 python3 tests/test_tv_discovery.py)")
    else:
        tv2.TV_IP = real_ip
        real = tv2.find_tv(timeout=3)
        print(f"       SSDP: {real[0] if real else '(无应答)'}")
        url, name = tv2.discover_tv_ctrl()
        print(f"       discover_tv_ctrl -> {url}  ({name})")
        check("真电视发现到可用端点", tv2.soap_probe(url) is True, url)
        if "38400" in url:
            print("       注: 命中 HiSmart 固定端口 (乐播端口漂移时的稳定兜底)")

    his.shutdown()
    lebo.shutdown()
    plain.shutdown()
    print(f"\n{'='*52}\n结果: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    print('='*52)
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
