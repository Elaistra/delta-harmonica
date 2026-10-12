"""B 站「三角洲行动口琴谱」在线曲库客户端（纯逻辑，不碰 Tk、不碰 Windows API）。

数据来源：https://www.bilibili.com/toy/deltaforce/index.html 的社区曲库。
前端 JS 里写死的 API 基址是 https://111.229.198.119/v1（证书签给域名、
按 IP 访问必然校验失败，所以这里用不校验证书的 SSL 上下文——
这是读取公开数据的常规做法，不涉及任何规避性质的操作）。

链路（前端 index-NVzN6OZV.js 逆向 + 逐条实测确认）：
    POST /installations      → installationToken（匿名设备凭证，30 天）
    POST /sessions/guest     → accessToken（游客会话，约 24h）
    GET  /scores?q=&limit=&cursor=   搜索（q 为空 = 最新上传）
    GET  /scores/stats?ids=  下载量 / 点赞（批量）
    GET  /scores/{id}/content  谱面内容（raw deflate 压缩的 DFHS 二进制）

DFHS 格式（v1，逐字段实测）：
    magic "DFHS" | version u8 | ppq varint | transpose zigzag varint
    | tempos: n varint + n×(delta_beat varint, bpm_hundredths varint)
    | time_signatures: n varint + n×(delta_beat, numerator, denominator)
    | measures: n varint + n×delta_beat
    | notes: n varint + n×(delta_start varint, duration varint, pitch u8)
    时间单位是 tick（ppq = 每 quarter 的 tick 数）。
"""

from __future__ import annotations

import json
import ssl
import struct
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

API_BASE = "https://111.229.198.119/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_MAGIC = b"DFHS"
_VERSION = 1


class BiliError(Exception):
    pass


# ── 底层 HTTP ────────────────────────────────────────────────────
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _http(method: str, url: str, token: str | None = None,
          body: dict | None = None, timeout: float = 15.0):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Origin", "https://www.bilibilitoy.com")
    req.add_header("Referer", "https://www.bilibilitoy.com/")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout, context=_CTX) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise BiliError(f"网络请求失败：{e}") from e


def _json(method: str, url: str, token: str | None = None, body: dict | None = None):
    status, data, _ = _http(method, url, token, body)
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception as e:
        raise BiliError(f"接口返回不是 JSON（HTTP {status}）") from e
    if status >= 400:
        msg = obj.get("message") or obj.get("code") or f"HTTP {status}"
        raise BiliError(f"接口错误：{msg}")
    return obj


# ── 会话（installation + 游客会话，内存缓存） ─────────────────────
class BiliClient:
    def __init__(self):
        self._install_token: str | None = None
        self._install_exp: float = 0.0
        self._access: str | None = None
        self._access_exp: float = 0.0

    def _ensure_install(self):
        if self._install_token and time.time() < self._install_exp - 86400 * 7:
            return
        obj = _json("POST", f"{API_BASE}/installations", body={})
        tok = obj.get("installationToken")
        exp = obj.get("expiresAt")
        if not tok:
            raise BiliError("拿不到设备凭证（installations）")
        self._install_token = tok
        try:
            self._install_exp = _parse_iso(exp)
        except Exception:
            self._install_exp = time.time() + 30 * 86400

    def _ensure_access(self):
        if self._access and time.time() < self._access_exp - 60:
            return
        self._ensure_install()
        obj = _json("POST", f"{API_BASE}/sessions/guest",
                    token=self._install_token, body={})
        tok = obj.get("accessToken")
        exp = obj.get("accessExpiresAt")
        if not tok:
            raise BiliError("拿不到游客会话（sessions/guest）")
        self._access = tok
        try:
            self._access_exp = _parse_iso(exp)
        except Exception:
            self._access_exp = time.time() + 3600

    def _token(self) -> str:
        self._ensure_access()
        return self._access  # type: ignore[return-value]

    # ── 业务接口 ────────────────────────────────────────────────
    def search(self, q: str = "", limit: int = 20,
               cursor: str | None = None) -> tuple[list[dict], str | None]:
        params = [f"limit={int(limit)}"]
        if q.strip():
            params.append(f"q={urllib.request.quote(q.strip())}")
        if cursor:
            params.append(f"cursor={urllib.request.quote(cursor)}")
        obj = _json("GET", f"{API_BASE}/scores?{'&'.join(params)}", token=self._token())
        return list(obj.get("scores") or []), obj.get("nextCursor")

    def stats(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        q = urllib.request.quote(",".join(ids), safe="")
        obj = _json("GET", f"{API_BASE}/scores/stats?ids={q}", token=self._token())
        return {s["scoreId"]: s for s in obj.get("stats") or [] if "scoreId" in s}

    def content(self, score_id: str) -> bytes:
        status, data, _ = _http("GET", f"{API_BASE}/scores/"
                                f"{urllib.request.quote(score_id, safe='')}/content",
                                token=self._token(), timeout=30.0)
        if status >= 400:
            try:
                msg = json.loads(data).get("message", "")
            except Exception:
                msg = f"HTTP {status}"
            raise BiliError(f"下载谱面失败：{msg}")
        return data


def _parse_iso(s: str) -> float:
    # "2026-11-11T05:11:50.518Z" → unix 秒
    from datetime import datetime, timezone
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ── DFHS 解码 ────────────────────────────────────────────────────
class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def u8(self) -> int:
        v = self.d[self.i]
        self.i += 1
        return v

    def varint(self) -> int:
        shift = 0
        out = 0
        while True:
            b = self.u8()
            out |= (b & 0x7F) << shift
            if not (b & 0x80):
                return out
            shift += 7
            if shift > 63:
                raise BiliError("varint 过长，数据损坏")


def _zigzag(v: int) -> int:
    return v // 2 if v % 2 == 0 else -((v + 1) // 2)


def decode_dfhs(raw: bytes) -> dict:
    """raw = /scores/{id}/content 的返回体（raw deflate 包着 DFHS）。"""
    try:
        data = zlib.decompress(raw, -15)
    except zlib.error as e:
        raise BiliError(f"谱面解压失败：{e}") from e
    r = _Reader(data)
    if data[:4] != _MAGIC:
        raise BiliError("不是 DFHS 谱面")
    r.i = 4
    version = r.u8()
    if version != _VERSION:
        raise BiliError(f"暂不支持的 DFHS 版本 {version}")
    ppq = r.varint()
    if not (24 <= ppq <= 9600):
        raise BiliError(f"PPQ 异常：{ppq}")
    transpose = _zigzag(r.varint())

    tempos = []
    n = r.varint()
    if n > 4096:
        raise BiliError("tempo 事件过多")
    acc = 0
    for _ in range(n):
        acc += r.varint()
        bpm = r.varint() / 100
        tempos.append((acc, bpm))
    if not tempos:
        tempos = [(0, 120.0)]

    n = r.varint()
    if n > 1024:
        raise BiliError("拍号事件过多")
    acc = 0
    for _ in range(n):
        acc += r.varint()
        num = r.varint()
        den = r.varint()
        # 拍号只影响展示，MIDI 里照抄即可

    n = r.varint()
    if n > 100000:
        raise BiliError("小节过多")
    for _ in range(n):
        r.varint()

    n = r.varint()
    if n > 1000000:
        raise BiliError("音符过多")
    notes = []
    acc = 0
    for _ in range(n):
        acc += r.varint()
        dur = r.varint()
        pitch = r.u8()
        notes.append((acc, max(1, dur), pitch))

    return {"ppq": ppq, "transpose": transpose, "tempos": tempos, "notes": notes}


# ── DFHS → MIDI（format 0 单轨） ─────────────────────────────────
def to_midi(score: dict) -> bytes:
    ppq = score["ppq"]
    trans = score.get("transpose", 0)
    events: list[tuple[int, int, bytes]] = []          # (tick, 次序, 字节)

    last = 0
    for tick, bpm in score["tempos"]:
        us = max(1, round(60_000_000 / bpm))
        events.append((tick, 0, b"\xff\x51\x03" + struct.pack(">I", us)[1:]))
        last = max(last, tick)
    for start, dur, pitch in score["notes"]:
        p = min(127, max(0, pitch + trans))
        events.append((start, 2, bytes((0x90, p, 96))))
        events.append((start + dur, 1, bytes((0x80, p, 0))))

    events.sort(key=lambda e: (e[0], e[1]))
    track = bytearray()
    last = 0
    for tick, _, payload in events:
        track += _write_varint(max(0, tick - last))
        track += payload
        last = tick
    track += b"\x00\xff\x2f\x00"

    head = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq)
    body = b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    return head + body


def _write_varint(v: int) -> bytes:
    out = bytearray()
    out.append(v & 0x7F)
    v >>= 7
    while v:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(out))


# ── 下载到曲库 ───────────────────────────────────────────────────
def download_midi(client: BiliClient, score: dict, lib_dir: Path) -> Path:
    """下载一首并转成 MIDI 存入曲库根目录，返回落盘路径（重名自动 (2)(3)…）。"""
    from .library import sanitize_name
    raw = client.content(score["id"])
    score_dfhs = decode_dfhs(raw)
    midi = to_midi(score_dfhs)
    base = sanitize_name(score.get("title") or score["id"]) or score["id"]
    path = lib_dir / f"{base}.mid"
    k = 2
    while path.exists():
        path = lib_dir / f"{base}({k}).mid"
        k += 1
    path.write_bytes(midi)
    return path
