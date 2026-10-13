# 03 - DLNA 控制通道：乐播 SOAP

这是整个方案的核心：**把电视当成一个 DLNA MediaRenderer，用 SOAP 指令让它播放任意 m3u8 URL**。

## 控制地址获取

```python
# 1. SSDP 找设备
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
ST: urn:schemas-upnp-org:device:MediaRenderer:1

# 2. 从 LOCATION 拉 description.xml
URLBase:    http://192.168.1.10:49152
controlURL: _urn:schemas-upnp-org:service:AVTransport_control

# 3. ⚠️ 拼接规则（本案最大的坑）
# controlURL 以下划线开头时必须补 "/"：
ctrl = base.rstrip("/") + "/" + ctrl_url.lstrip("/")
# 错误拼接 "http://x:49152_urn:..." → urllib 报 "nonnumeric port: 'AVTransport_control'"
# 症状：所有 SOAP 调用静默失败，遥控器 state 永远 UNKNOWN
```

## 核心 SOAP 调用

```python
def soap(action, args_xml):
    body = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
{args_xml}</u:{action}></s:Body></s:Envelope>'''.encode()
    req = urllib.request.Request(ctrl, data=body, headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"'})
    return urllib.request.urlopen(req, timeout=8).status

# 播放一个 URL（两步）
soap("SetAVTransportURI",
     '<InstanceID>0</InstanceID>'
     f'<CurrentURI>{url}</CurrentURI>'
     '<CurrentURIMetaData>&lt;DIDL-Lite&gt;&lt;/DIDL-Lite&gt;</CurrentURIMetaData>')
soap("Play", '<InstanceID>0</InstanceID><Speed>1</Speed>')

# 查状态（返回 CurrentTransportState: PLAYING/PAUSED_PLAYBACK/STOPPED...）
soap("GetTransportInfo", '<InstanceID>0</InstanceID>')

# 停止
soap("Stop", '<InstanceID>0</InstanceID>')
```

## 乐播播放器的行为特性（实测）

1. **HLS (m3u8) 才能长播**。mpegts 裸流被当静态文件"下载"，拉完缓冲约 30 秒就自停（PAUSED_PLAYBACK）
2. 直连失败后电视停在 PAUSED_PLAYBACK——Set 新 URI 前先 Stop 可提高成功率
3. 电视自己拉流时，SOAP 响应变慢；**并发 SOAP 会被限流**（查状态要重试 3 次）
4. 电视能直接播公网 http m3u8（含 4K），但拉流路径卡不卡取决于公网质量——这是引入 Mac 中转的原因
5. **SOAP 请求绝不能走系统代理**：Clash (代理(config.json PROXY)) 会拦下局域网请求。tv.py 启动时清空 `http_proxy/https_proxy/all_proxy` 并设 `no_proxy=*`

## 延迟数字（体感参考）

| 操作 | 延迟 |
|------|------|
| Set+Play 到出画面（直连） | 3-8 秒 |
| 换台（直连） | 同上 |
| 直连失败判定 | 12 秒（轮询 6×2s） |
| 中转启动到出画面 | 5-10 秒（含 ffmpeg 起动+首个 2s 切片） |

## 音量

音量走 RenderingControl 服务（SetVolume），本案未做——电视遥控器管音量更直接。
