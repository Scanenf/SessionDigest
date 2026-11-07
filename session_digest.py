#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SessionDigest — 把 Claude Code / Codex 的本地会话记录蒸馏成结构化 Markdown 摘要。

模式：
  --export        全量导出，不调用模型
  --llm N         预览：对最近 N 场会话生成摘要
  --nightly       增量：新会话全量摘要，旧会话只摘要新增部分并追加「续」段
  --context N     输出最近 N 份摘要拼接文本
  --find 关键词   按相关度查找历史摘要（本地关键词匹配）
  --retry-failed  只重试失败队列

隐私提示：
  本程序会读取本机会话记录，并把会话内容发送到你配置的模型端点。
  请只对属于自己的会话运行；公开分享输出前，先检查是否含密钥、内网地址、
  客户名称等敏感信息。
"""
from __future__ import annotations

import argparse
import copy
import glob
import hashlib
import hmac
import json
import os
import re
import shlex
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple

VERSION = "0.4.0"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Config:
    claude_root: str
    codex_root: str
    out_dir: str
    obsidian_dir: Optional[str]
    memory_dir: Optional[str]
    state_file: str
    index_file: str
    failures_file: str
    lock_file: str
    projects: Dict[str, str]
    base_url: str
    api_key: Optional[str]
    model: str
    api_format: str = "anthropic"
    min_turns: int = 8
    min_age_hours: int = 6
    max_sessions: int = 20
    merge_after: int = 3
    min_delta_turns: int = 4
    min_delta_chars: int = 500
    force: bool = False
    verify: bool = True
    redact: str = "strict"
    verbose: bool = False
    ref_key_file: Optional[str] = None
    roots_file: Optional[str] = None


@dataclass
class Task:
    info: dict
    label: str
    kind: str
    path: str
    key: str
    mode: str                      # full | incremental | merge
    start_offset: int = 0
    new_offset: int = 0
    prev_summary: Optional[str] = None
    sections: int = 0


def _expand(p: Optional[str]) -> Optional[str]:
    return os.path.expanduser(p) if p else p


def _state_dir(cfg: Config) -> str:
    return os.path.dirname(cfg.state_file) or "."


def load_projects(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    try:
        with open(_expand(path), encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if k}
    except Exception as e:
        print(f"警告：项目映射读取失败（{redact_text(str(path), 'strict')}）："
              f"{safe_error(e)}", file=sys.stderr)
        return {}


def resolve_llm(args) -> Tuple[str, Optional[str], str]:
    fmt = getattr(args, "api_format", None) or "anthropic"
    base = args.base_url or os.environ.get("ANTHROPIC_BASE_URL")
    tok = (args.api_key
           or os.environ.get("ANTHROPIC_AUTH_TOKEN")
           or os.environ.get("ANTHROPIC_API_KEY")
           or (os.environ.get("OPENAI_API_KEY") if fmt == "openai" else None))
    model = args.model or os.environ.get("ANTHROPIC_MODEL")
    if fmt == "openai":
        model = model or os.environ.get("OPENAI_MODEL")

    if not (base and tok):
        try:
            p = os.path.expanduser("~/.claude/settings.json")
            with open(p, encoding="utf-8") as f:
                env = json.load(f).get("env") or {}
            base = base or env.get("ANTHROPIC_BASE_URL")
            tok = tok or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
            model = model or env.get("ANTHROPIC_MODEL")
        except Exception:
            pass

    if fmt == "openai":
        return (base or "https://api.openai.com").rstrip("/"), tok, model or ""
    return (base or "https://api.anthropic.com").rstrip("/"), tok, (model or "claude-sonnet-5-5")


# ---------------------------------------------------------------------------
# 进程锁与原子写入
# ---------------------------------------------------------------------------

class ProcessLock:
    """跨平台非阻塞互斥锁：同一时间只允许一个写入进程。

    用操作系统的文件锁（Windows: msvcrt，POSIX: fcntl），进程退出或崩溃时
    由系统自动释放，不会留下需要手工清理的死锁文件。
    """

    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"\n")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        fh, self.fh = self.fh, None
        if not fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def write_text_atomic(path: str, text: str) -> None:
    """先写临时文件再替换，避免进程中断留下半截文件。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        # 替换失败时不要把含正文的临时文件留在磁盘上
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def lock_or_report(cfg: Config) -> Optional[ProcessLock]:
    """拿写锁；被占用时打印提示并返回 None。"""
    lock = ProcessLock(cfg.lock_file)
    if lock.acquire():
        return lock
    print(f"另一个 SessionDigest 进程正在运行"
          f"（锁：{redact_text(str(cfg.lock_file), 'strict')}）。\n"
          f"等它结束后再试；如果确认没有进程在跑，删掉该文件即可。",
          file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S)
_URL_AUTH_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
_TOKEN_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
_ASSIGN_RE = re.compile(
    r"""(?i)\b(api[_-]?key|access[_-]?key|secret[_-]?key|secret|token|password|passwd|pwd)\b"""
    r"""(\s*["']?\s*[:=]\s*)(["']?)([A-Za-z0-9_\-./+=]{8,})\3""")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")

_HOME_PATTERNS = [
    # C:\Users\name   C:/Users/name
    re.compile(r"(?i)\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'`]+"),
    # /c/Users/name（Git Bash / MSYS）
    re.compile(r"(?i)(?<![\w.])/[a-z]/Users/[^/\s\"'`]+"),
    # /home/name  /Users/name
    re.compile(r"(?<![\w.])/(?:home|Users)/[^/\s\"'`]+"),
    # /root
    re.compile(r"(?<![\w.])/root(?=[/\s\"'`]|$)"),
]

_USERNAME_STOPLIST = {
    "root", "user", "admin", "administrator", "guest", "runner", "ubuntu",
    "windows", "win", "pc", "desktop", "laptop", "home", "test", "claude",
}


def _local_usernames() -> List[str]:
    names = []
    for key in ("USERNAME", "USER", "LOGNAME"):
        v = os.environ.get(key)
        if v:
            names.append(v)
    try:
        import getpass
        names.append(getpass.getuser())
    except Exception:
        pass
    out = []
    for n in names:
        n = str(n).strip()
        if len(n) >= 3 and n.lower() not in _USERNAME_STOPLIST and n not in out:
            out.append(n)
    return out


def _redact_credentials(text: str) -> str:
    text = _PRIVATE_KEY_RE.sub("[私钥已隐藏]", text)
    text = _URL_AUTH_RE.sub(r"\1***:***@", text)
    for pat in _TOKEN_RES:
        text = pat.sub("***", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _ASSIGN_RE.sub(r"\1\2\3***\3", text)
    text = _EMAIL_RE.sub("<email>", text)
    text = _PRIVATE_IP_RE.sub("<private-ip>", text)
    return text


def _redact_paths(text: str) -> str:
    for pat in _HOME_PATTERNS:
        text = pat.sub("~", text)
    for name in _local_usernames():
        # 前后不能紧挨字母数字，避免误伤更长的单词
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])",
                      "~", text, flags=re.I)
    return text


def redact_text(text: str, mode: str = "strict") -> str:
    """off：原样；safe：遮凭据/邮箱/内网 IP；strict：再把主目录用户名换成 ~。"""
    if not text or mode == "off":
        return text
    text = _redact_credentials(text)
    if mode == "strict":
        text = _redact_paths(text)
    return text


def safe_error(exc, mode: str = "strict") -> str:
    """异常信息也可能带路径或密钥，打印前统一脱敏。"""
    return redact_text(str(exc), mode)


# 路径引用 v2：不保存绝对路径，也不要求相对路径的每个目录名都是明文。
#
#   {"v": 2, "root": "claude", "root_id": "r2_...",
#    "parts": [{"n": "proj"}, {"h": "h2_..."}, {"n": "a.jsonl"}],
#    "sid": "...", "hint": {"size": 1234, "head_sha1": "..."}}
#
# root_id 是本地密钥对规范化根目录做的 HMAC；h 是对敏感目录名做的 HMAC。
# 根目录整体移动后用 roots.json 的绑定把旧 root_id 指到新 root_id，
# 历史 ref 不需要修改。
_REF_ROOTS = ("obsidian", "memory", "out", "claude", "codex")
_REF_VERSION = 2
_KEY_DOMAIN_ROOT = b"root\0"
_KEY_DOMAIN_PART = b"part\0"
_RECOVERY_MAX_FILES = 5000
_RECOVERY_MAX_SECONDS = 2.0
_TOKEN_CACHE: dict = {}


def _root_dir(cfg: Config, root: str) -> Optional[str]:
    return {
        "obsidian": cfg.obsidian_dir,
        "memory": cfg.memory_dir,
        "out": cfg.out_dir,
        "claude": cfg.claude_root,
        "codex": cfg.codex_root,
    }.get(root)


def _abs_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _is_within(path: str, root: str) -> bool:
    """path 是否在 root 之内（大小写不敏感，跨盘符返回 False）。"""
    if not path or not root:
        return False
    try:
        p = os.path.normcase(_abs_path(path))
        r = os.path.normcase(_abs_path(root))
        return os.path.commonpath([p, r]) == r
    except (ValueError, TypeError):
        return False


def _norm_root(path: str) -> str:
    """根目录规范化：展开 ~、绝对路径、解析符号链接、统一大小写与分隔符。"""
    p = os.path.realpath(_abs_path(path))
    p = os.path.normcase(p).replace("\\", "/")
    return p.rstrip("/") or p


def _norm_part(name: str) -> str:
    """目录项规范化：Unicode NFC + 平台大小写规则。"""
    return os.path.normcase(unicodedata.normalize("NFC", str(name)))


def _ref_key_path(cfg: Config) -> str:
    return _expand(cfg.ref_key_file) or os.path.join(_state_dir(cfg), "digest.key")


def _roots_path(cfg: Config) -> str:
    return _expand(cfg.roots_file) or os.path.join(_state_dir(cfg), "roots.json")


def _load_ref_key(cfg: Config, create: bool = False) -> Optional[bytes]:
    """读取本地 HMAC 密钥；create=False 时绝不写盘（只读命令必须安全）。"""
    path = _ref_key_path(cfg)
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 16:
            return key
    except OSError:
        pass
    if not create:
        return None
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = os.urandom(32)
    # Windows 上 os.open 默认是文本模式，会把随机字节里的 \n 写成 \r\n，
    # 导致“返回的密钥”和“文件里的密钥”不一致；必须显式加 O_BINARY。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(key)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        return key
    except FileExistsError:
        # 另一个进程刚创建，读它的
        return _load_ref_key(cfg, create=False)
    except OSError:
        return None


def ensure_ref_key(cfg: Config) -> Optional[bytes]:
    """写命令入口调用；只读命令不要调用。"""
    return _load_ref_key(cfg, create=True)


def _hmac_hex(key: bytes, domain: bytes, value: str) -> str:
    return hmac.new(key, domain + value.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _root_id_for_path(key: bytes, path: str) -> str:
    return "r2_" + _hmac_hex(key, _KEY_DOMAIN_ROOT, _norm_root(path))


def _part_token(key: bytes, name: str) -> str:
    return "h2_" + _hmac_hex(key, _KEY_DOMAIN_PART, _norm_part(name))


def _sensitive_component(name: str) -> bool:
    """目录名是否应改成 HMAC：严格脱敏会改动它，或命中已知用户目录形态。"""
    if not name:
        return False
    if redact_text(name, "strict") != name:
        return True
    low = name.lower()
    if low.startswith("c--users-") or "-users-" in low:
        return True
    return False


def _path_hint(path: Optional[str]) -> Optional[dict]:
    """恢复线索：只存大小和前 64KB 哈希，不存内容。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    hint = {"size": size}
    try:
        with open(path, "rb") as f:
            chunk = f.read(65536)
        if chunk:
            hint["head_sha1"] = hashlib.sha1(chunk).hexdigest()[:16]
    except OSError:
        pass
    return hint


def _parts_for_rel(key: bytes, rel: str) -> List[dict]:
    parts = []
    names = rel.replace(os.sep, "/").split("/") if os.sep != "/" \
        else rel.split("/")
    for name in names:
        if not name or name == ".":
            continue
        if _sensitive_component(name):
            parts.append({"h": _part_token(key, name)})
        else:
            parts.append({"n": name})
    return parts


def make_path_ref(cfg: Config, path: Optional[str],
                  preferred: Optional[str] = None,
                  sid: Optional[str] = None,
                  key: Optional[bytes] = None) -> Optional[dict]:
    """把绝对路径转成 v2 ref；不在已知根目录内或没有密钥则返回 None。"""
    if not path:
        return None
    key = key or _load_ref_key(cfg, create=False)
    if not key:
        return None
    order = ([preferred] if preferred else []) + \
            [r for r in _REF_ROOTS if r != preferred]
    for root in order:
        base = _root_dir(cfg, root)
        if not base or not _is_within(path, base):
            continue
        try:
            rel = os.path.relpath(_abs_path(path), _abs_path(base))
        except ValueError:
            continue
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        ref = {
            "v": _REF_VERSION,
            "root": root,
            "root_id": _root_id_for_path(key, base),
            "parts": _parts_for_rel(key, rel),
        }
        if sid:
            ref["sid"] = sid
        hint = _path_hint(path)
        if hint:
            ref["hint"] = hint
        return ref
    return None


def _ref_from_rel(cfg: Config, root: str, rel: str, key: bytes,
                  sid: Optional[str] = None) -> Optional[dict]:
    base = _root_dir(cfg, root)
    if not base or not rel:
        return None
    path = os.path.abspath(os.path.join(_abs_path(base), *rel.split("/")))
    if not _is_within(path, base):
        return None
    ref = {
        "v": _REF_VERSION,
        "root": root,
        "root_id": _root_id_for_path(key, base),
        "parts": _parts_for_rel(key, rel),
    }
    if sid:
        ref["sid"] = sid
    hint = _path_hint(path)
    if hint:
        ref["hint"] = hint
    return ref


def _legacy_plain_path(raw) -> bool:
    """旧字段是否是未脱敏、可直接使用的绝对路径。"""
    if not isinstance(raw, str) or not raw:
        return False
    if raw.startswith("~") or "<" in raw or ">" in raw or "***" in raw:
        return False
    return os.path.isabs(raw) or bool(re.match(r"^[A-Za-z]:[\\/]", raw))


def load_root_bindings(cfg: Config) -> dict:
    """只读 roots.json。里面只有 root_id 之间的绑定，没有路径。"""
    try:
        with open(_roots_path(cfg), encoding="utf-8") as f:
            data = json.load(f)
        bindings = data.get("bindings") if isinstance(data, dict) else None
        return bindings if isinstance(bindings, dict) else {}
    except Exception:
        return {}


def save_root_bindings(cfg: Config, bindings: dict) -> None:
    data = {"version": 1, "bindings": bindings}
    write_text_atomic(_roots_path(cfg),
                      json.dumps(data, ensure_ascii=False, indent=1))


def _follow_binding(bindings: dict, start: str, target: str,
                    limit: int = 16) -> bool:
    """沿 old -> new 绑定链走，能否从 start 到达 target；检测成环。"""
    cur, seen = start, set()
    while cur:
        if cur == target:
            return True
        if cur in seen or len(seen) >= limit:
            return False
        seen.add(cur)
        nxt = bindings.get(cur)
        if isinstance(nxt, dict):
            nxt = nxt.get("to")
        cur = nxt if isinstance(nxt, str) else None
    return False


def _bind_root_path(cfg: Config, kind: str, target: str,
                    key: bytes) -> Tuple[Optional[str], str]:
    root = _root_dir(cfg, kind)
    if not root:
        return None, "root_not_configured"
    current = _root_id_for_path(key, root)
    if current == target or _follow_binding(load_root_bindings(cfg), target, current):
        return _abs_path(root), "ok"
    return None, "unbound_root"


@dataclass
class PathResolution:
    path: Optional[str] = None
    status: str = "missing"
    detail: str = ""
    candidates: List[str] = field(default_factory=list)
    recovered: bool = False

    def ok(self) -> bool:
        return self.status in ("ok", "recovered")


def _dir_token_map(base: str, key: bytes) -> Dict[str, List[str]]:
    """一次列目录，建立 HMAC(token) -> 目录项 的映射；带 mtime 失效。"""
    try:
        st = os.stat(base)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    cache_key = (base, hashlib.sha256(key).hexdigest()[:8], sig)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    out: Dict[str, List[str]] = {}
    try:
        for name in os.listdir(base):
            out.setdefault(_part_token(key, name), []).append(name)
    except OSError:
        return {}
    if len(_TOKEN_CACHE) > 512:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[cache_key] = out
    return out


def _resolve_v2(cfg: Config, ref: dict, rec: Optional[dict]) -> PathResolution:
    kind = ref.get("root")
    target = ref.get("root_id")
    parts = ref.get("parts")
    if not isinstance(kind, str) or not isinstance(target, str) \
            or not isinstance(parts, list) or not parts:
        return PathResolution(status="invalid")
    key = _load_ref_key(cfg, create=False)
    if not key:
        return PathResolution(status="key_missing")
    base, bind_status = _bind_root_path(cfg, kind, target, key)
    if not base:
        return PathResolution(status=bind_status)

    cur = base
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if not isinstance(part, dict):
            return PathResolution(status="invalid")
        if isinstance(part.get("n"), str) and part["n"]:
            name = part["n"]
            if os.sep in name or "/" in name:
                return PathResolution(status="invalid")
        elif isinstance(part.get("h"), str) and part["h"]:
            matches = _dir_token_map(cur, key).get(part["h"], [])
            if len(matches) > 1:
                return PathResolution(status="ambiguous",
                                      detail="masked-component-collision",
                                      candidates=[os.path.join(cur, m) for m in matches])
            if not matches:
                return _recover(cfg, kind, base, ref, rec)
            name = matches[0]
        else:
            return PathResolution(status="invalid")
        cur = os.path.join(cur, name)
        if not last and not os.path.isdir(cur):
            return _recover(cfg, kind, base, ref, rec)

    if not _is_within(cur, base):
        return PathResolution(status="invalid", detail="outside-root")
    if os.path.exists(cur):
        return PathResolution(path=cur, status="ok")
    return _recover(cfg, kind, base, ref, rec)


def _resolve_v1(cfg: Config, ref: dict) -> PathResolution:
    root_name = ref.get("root")
    rel = ref.get("rel")
    if not isinstance(root_name, str) or not isinstance(rel, str) or not rel:
        return PathResolution(status="invalid")
    base = _root_dir(cfg, root_name)
    if not base:
        return PathResolution(status="root_not_configured")
    path = os.path.abspath(os.path.join(_abs_path(base), *rel.split("/")))
    if not _is_within(path, base):
        return PathResolution(status="invalid", detail="outside-root")
    if os.path.exists(path):
        return PathResolution(path=path, status="ok")
    return PathResolution(status="missing")


def _file_contains(path: str, needle: str, limit: int = 65536) -> bool:
    try:
        with open(path, "rb") as f:
            raw = f.read(limit)
        return needle.encode("utf-8") in raw
    except OSError:
        return False


def _candidate_score(path: str, sid: str, hint: dict, basename: str) -> int:
    score = 0
    name = os.path.basename(path)
    if basename and name == basename:
        score += 8
    if sid:
        # 知道 sid 时，sid 是硬条件：文件名相同但 sid 不同的文件不能算候选
        if sid in name:
            score += 6
        elif _file_contains(path, sid):
            score += 4
        else:
            return -1
    try:
        size = os.path.getsize(path)
    except OSError:
        size = -1
    if hint.get("size") is not None and size == hint["size"]:
        score += 2
    if hint.get("head_sha1"):
        try:
            with open(path, "rb") as f:
                chunk = f.read(65536)
            if hashlib.sha1(chunk).hexdigest()[:16] == hint["head_sha1"]:
                score += 4
        except OSError:
            pass
    return score


def _recover(cfg: Config, kind: str, base: str, ref: dict,
             rec: Optional[dict]) -> PathResolution:
    """精确解析失败后，用 sid / 文件名 / 大小 / 前缀哈希定位，限时限量。"""
    sid = ref.get("sid") or (rec or {}).get("sid") or ""
    sid = str(sid) if sid else ""
    hint = ref.get("hint") if isinstance(ref.get("hint"), dict) else {}
    basename = ""
    for part in reversed(ref.get("parts") or []):
        if isinstance(part, dict) and isinstance(part.get("n"), str):
            basename = part["n"]
            break

    if not sid and not basename and not hint:
        return PathResolution(status="missing")

    pattern = os.path.join(base, "**", "*.jsonl") if kind in ("claude", "codex") \
        else os.path.join(base, "**", "*.md")
    started = time.monotonic()
    scanned = 0
    matches = []
    limited = False
    try:
        paths = glob.iglob(pattern, recursive=True)
        for path in paths:
            scanned += 1
            if scanned > _RECOVERY_MAX_FILES:
                limited = True
                break
            if time.monotonic() - started > _RECOVERY_MAX_SECONDS:
                limited = True
                break
            score = _candidate_score(path, sid, hint, basename)
            if score > 0:
                matches.append((score, path))
    except OSError:
        return PathResolution(status="unreadable", detail="recovery-scan-failed")

    if limited:
        return PathResolution(status="recovery_limited",
                              detail=f"scanned={scanned}")
    if not matches:
        return PathResolution(status="missing")
    matches.sort(key=lambda x: x[0], reverse=True)
    unique = list(dict.fromkeys(p for _s, p in matches))
    if len(unique) == 1:
        return PathResolution(path=unique[0], status="recovered", recovered=True)
    return PathResolution(status="ambiguous", candidates=unique,
                          detail=f"candidates={len(unique)}")


def resolve_ref(cfg: Config, ref, rec: Optional[dict] = None) -> PathResolution:
    if not isinstance(ref, dict):
        return PathResolution(status="invalid")
    if ref.get("v") == _REF_VERSION:
        return _resolve_v2(cfg, ref, rec)
    if "rel" in ref:
        return _resolve_v1(cfg, ref)
    return PathResolution(status="invalid")


def resolve_meta(cfg: Config, rec: dict, key: str) -> PathResolution:
    """先解析 ref；只有未脱敏的旧绝对路径才允许作为运行期回退。"""
    if not isinstance(rec, dict):
        return PathResolution(status="invalid")
    res = resolve_ref(cfg, rec.get(key + "_ref"), rec)
    if res.ok():
        return res

    legacy = rec.get(key)
    if isinstance(legacy, str) and legacy:
        if _legacy_plain_path(legacy):
            path = os.path.expanduser(legacy)
            if os.path.exists(path):
                return PathResolution(path=path, status="ok", detail="legacy-path")
            if res.status not in ("ambiguous", "recovery_limited", "unreadable"):
                return PathResolution(status="missing", detail="legacy-path-missing")
        elif res.status in ("missing", "invalid", "legacy_redacted",
                            "root_not_configured"):
            return PathResolution(status="legacy_redacted", detail=res.detail)
    return res


def resolve_path_ref(cfg: Config, ref) -> Optional[str]:
    """兼容旧调用：只返回可用的路径，不暴露状态。"""
    res = resolve_ref(cfg, ref)
    return res.path if res.ok() else None


def resolve_meta_path(cfg: Config, rec: dict, key: str) -> Optional[str]:
    """兼容旧调用：只返回可用的路径，不暴露状态。"""
    res = resolve_meta(cfg, rec, key)
    return res.path if res.ok() else None


# ---------------------------------------------------------------------------
# 会话解析
# ---------------------------------------------------------------------------

SYS_PATTERNS = [
    r"<local-command-caveat>.*?</local-command-caveat>",
    r"<ide_opened_file>.*?</ide_opened_file>",
    r"<file_content>.*?</file_content>",
    r"<file_map>.*?</file_map>",
    r"<workspace>.*?</workspace>",
    r"<user_instructions>.*?</user_instructions>",
    r"<context_info>.*?</context_info>",
    r"<system-reminder>.*?</system-reminder>",
    r"<command-name>.*?</command-name>",
]


def trunc(s, n=200):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:n] + ("…" if len(s) > n else "")


def clean_prompt(s):
    s = str(s)
    for p in SYS_PATTERNS:
        s = re.sub(p, "", s, flags=re.S)
    s = re.sub(r"<[^>]{1,60}>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def tval(block):
    if isinstance(block, dict):
        t = block.get("text")
        return t if isinstance(t, str) else ""
    if isinstance(block, str):
        return block
    return ""


def walk_blocks(content: object) -> Iterator[Tuple[str, object]]:
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    yield ("text", b.get("text", ""))
                elif t == "tool_use":
                    yield ("tool_use", b)
                elif t == "tool_result":
                    yield ("tool_result", b)
                elif t == "thinking":
                    yield ("thinking", b.get("thinking", ""))
    elif isinstance(content, str):
        yield ("text", content)


def _blank_info(kind: str) -> dict:
    return {"kind": kind, "prompts": [], "assistant": [], "tools": Counter(),
            "files": OrderedDict(), "commands": [], "command_events": [],
            "tool_results": [], "modified": [],
            "turns": 0, "cwd": None, "branch": None,
            "start": None, "end": None, "session": None, "error": None}


def _fmt_ts(ts) -> Optional[str]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16].replace("T", " ")


_SUCCESS_PATTERNS = [
    r"\bexit\s+code\s*[:=]?\s*0\b",
    r"\bexit_code\s*[:=]?\s*0\b",
    r"\bprocess\s+exited\s+with\s+(?:code\s+)?0\b",
    r'"success"\s*:\s*true',
    r"\ball\s+tests?\s+passed\b",
    r"\b\d+\s+passed\b",
    r"\b0\s+failed\b",
    r"\bexit\s+status\s*[:=]?\s*0\b",
]


def success_evidence(result: str) -> bool:
    """只认明确的成功证据；没有证据不等于成功。"""
    text = str(result or "")
    if not text:
        return False
    low = text.lower()
    if re.search(r"\b[1-9]\d*\s+(?:failed|failure|errors?)\b", low):
        return False
    if re.search(r"\bexpected\s+(?:exit\s+code|success|0\s+errors?)\b", low):
        return False
    return any(re.search(p, low) for p in _SUCCESS_PATTERNS)


def _add_command_event(info: dict, cmd: str, tool_use_id: Optional[str],
                       channel: str) -> None:
    if not cmd or len(info["command_events"]) >= 200:
        return
    info["command_events"].append({
        "cmd": trunc(cmd, 300),
        "tool_use_id": tool_use_id or "",
        "channel": channel,
        "result": "",
        "is_error": None,
        "success_evidence": False,
    })


def _attach_tool_result(info: dict, tool_use_id: Optional[str],
                        result: str, is_error=None) -> None:
    if not tool_use_id:
        return
    for ev in reversed(info["command_events"]):
        if ev.get("tool_use_id") == tool_use_id:
            ev["result"] = trunc(result, 400)
            ev["is_error"] = bool(is_error) if is_error is not None else None
            ev["success_evidence"] = (
                not ev["is_error"] and success_evidence(result))
            return


def _parse_claude_line(e: dict, info: dict) -> None:
    ts = _fmt_ts(e.get("timestamp"))
    if ts:
        if info["start"] is None:
            info["start"] = ts
        info["end"] = ts

    typ = e.get("type")
    if not typ or typ == "summary":
        return
    msg = e.get("message") or {}
    role = msg.get("role")

    if typ == "user" and role == "user":
        info["turns"] += 1
        text_parts = []
        has_tool_result = False
        for kind, data in walk_blocks(msg.get("content")):
            if kind == "text":
                text_parts.append(data)
            elif kind == "tool_result":
                has_tool_result = True
                c = data.get("content")
                got = []
                if isinstance(c, list):
                    got = [tval(cb) for cb in c]
                elif isinstance(c, str):
                    got.append(c)
                text_parts.extend(got)
                res = " ".join(x for x in got if x.strip())
                if res and len(info["tool_results"]) < 40:
                    info["tool_results"].append(trunc(res, 400))
                _attach_tool_result(info, data.get("tool_use_id"), res,
                                    data.get("is_error"))
        joined = " ".join(p for p in text_parts if p.strip())
        if not has_tool_result:
            p = clean_prompt(joined)
            if p:
                info["prompts"].append(p)

    elif typ == "assistant":
        info["turns"] += 1
        text_parts = []
        for kind, data in walk_blocks(msg.get("content")):
            if kind == "text":
                text_parts.append(data)
            elif kind == "tool_use":
                name = data.get("name", "?")
                info["tools"][name] += 1
                inp = data.get("input") or {}
                if not isinstance(inp, dict):
                    continue
                cmd = inp.get("command")
                if cmd and len(info["commands"]) < 60:
                    info["commands"].append(trunc(cmd, 300))
                if cmd:
                    _add_command_event(info, cmd, data.get("id"), "tool_use")
                fp = inp.get("file_path") or inp.get("pattern")
                if fp:
                    key = str(fp)
                    info["files"].setdefault(key, None)
                    if name in ("Edit", "Write", "NotebookEdit") \
                            and key not in info["modified"]:
                        info["modified"].append(key)
        t = " ".join(x for x in text_parts if x.strip()).strip()
        if t:
            info["assistant"].append(t)

    if e.get("cwd"):
        info["cwd"] = e["cwd"]
    if e.get("gitBranch"):
        info["branch"] = e["gitBranch"]
    if e.get("sessionId"):
        info["session"] = e["sessionId"]


def _parse_codex_line(e: dict, info: dict) -> None:
    ts = _fmt_ts(e.get("timestamp"))
    if ts:
        if info["start"] is None:
            info["start"] = ts
        info["end"] = ts

    p = e.get("payload") or {}
    pt = p.get("type") or e.get("type")
    if pt in ("session_meta", "task_started", "turn_started"):
        info["session"] = p.get("session_id") or info["session"]
        if p.get("cwd"):
            info["cwd"] = p["cwd"]
        return

    if pt == "user_message":
        info["turns"] += 1
        m = p.get("message")
        if isinstance(m, str):
            info["prompts"].append(m)
        elif isinstance(m, dict):
            c = m.get("content")
            if isinstance(c, str):
                info["prompts"].append(c)
    elif pt == "agent_message":
        info["turns"] += 1
        m = p.get("message")
        if isinstance(m, str):
            info["assistant"].append(m)
    elif pt in ("function_call", "custom_tool_call", "web_search_call"):
        name = p.get("name") or pt
        info["tools"][name] += 1
        args = p.get("arguments") or p.get("input") or p.get("command") or ""
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                args = parsed if isinstance(parsed, dict) else {}
            except Exception:
                args = {}
        if isinstance(args, dict):
            cmd = args.get("command") or args.get("cmd")
            if cmd and len(info["commands"]) < 60:
                info["commands"].append(trunc(cmd, 300))
            if cmd and pt in ("function_call", "custom_tool_call"):
                _add_command_event(info, cmd,
                                   p.get("call_id") or p.get("id"), pt)
            fp = args.get("file_path") or args.get("pattern")
            if fp:
                info["files"].setdefault(str(fp), None)
    elif pt in ("function_call_output", "custom_tool_call_output",
                "tool_result"):
        out = p.get("output")
        if isinstance(out, dict):
            out = out.get("content") or out.get("text") or ""
        elif not isinstance(out, str):
            out = str(out or "")
        _attach_tool_result(info, p.get("call_id") or p.get("tool_call_id"),
                            out, p.get("is_error"))


def parse_claude(path: str) -> dict:
    info = _blank_info("claude")
    try:
        with open(path, encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                _parse_claude_line(e, info)
    except Exception as ex:
        info["error"] = str(ex)
    return info


def parse_codex(path: str) -> dict:
    info = _blank_info("codex")
    try:
        with open(path, encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                _parse_codex_line(e, info)
    except Exception as ex:
        info["error"] = str(ex)
    return info


def parse_session(path: str, kind: str) -> dict:
    return parse_claude(path) if kind == "claude" else parse_codex(path)


def parse_incremental(path: str, kind: str, start_offset: int) -> Tuple[dict, int]:
    """只解析 start_offset 之后的完整行，返回 (info, 新的偏移量)。"""
    info = _blank_info(kind)
    handler = _parse_claude_line if kind == "claude" else _parse_codex_line
    with open(path, "rb") as f:
        if start_offset > 0:
            f.seek(start_offset - 1)
            if f.read(1) != b"\n":
                # 旧偏移量落在行中间：跳到下一行，最多丢一行残片，
                # 不能把半行当完整记录解析。
                skipped = f.readline()
                start_offset += len(skipped)
        f.seek(start_offset)
        data = f.read()

    chunks = data.split(b"\n")
    complete = chunks[:-1]          # 最后一段没有换行，可能是写了一半的行
    consumed = sum(len(c) + 1 for c in complete)

    for raw in complete:
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        handler(e, info)

    return info, start_offset + consumed


def prefix_sha1(path: str, offset: int) -> str:
    """文件前 offset 字节的哈希，用来判断会话文件是否被重写过。"""
    if offset <= 0:
        return ""
    h = hashlib.sha1()
    left = offset
    try:
        with open(path, "rb") as f:
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def session_key(info: dict, path: str) -> str:
    return info.get("session") or os.path.splitext(os.path.basename(path))[0]


# ---------------------------------------------------------------------------
# 事实校验
# ---------------------------------------------------------------------------

_LEGEND = ("> 校验图例：⚠ = 原文中未找到或未执行；"
           "⚠≠ = 原文有相近但不同的条目；"
           "⚠? = 原文未证实执行结果。\n\n")

_LEVEL_ORDER = {"missing": 0, "mentioned": 1, "executed": 2, "succeeded": 3,
                "result_verified": 4}

_SHELL_WRAPPERS = {"bash", "sh", "zsh", "fish", "cmd", "cmd.exe",
                   "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_INTERPRETERS = {"python", "python3", "py", "python.exe", "python3.exe"}
_RUNNER_PROGRAMS = {"git", "pip", "pip3", "cargo", "go", "dotnet", "make",
                    "docker", "kubectl", "poetry"}
_PACKAGE_MANAGERS = {"npm", "yarn", "pnpm", "bun"}
_RESULT_RE = re.compile(r"通过|成功|修复|解决|正常|生效|已完成")


def _norm_match(s: str) -> str:
    s = str(s).replace("`", "").strip().lower()
    s = s.replace("\\", "/")
    return re.sub(r"\s+", " ", s)


def _source_text(info: dict) -> str:
    """校验只认原文事实：prompt、工具输出、文件、命令，不认助手正文。"""
    parts = []
    parts += info.get("prompts", [])
    parts += info.get("commands", [])
    parts += info.get("tool_results", [])
    parts += list(info.get("files", {}).keys())
    parts += info.get("modified", [])
    parts += [str(e.get("result") or "") for e in _events(info)]
    return _norm_match("\n".join(str(p) for p in parts))


def _strip_quotes(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    return tok


def _norm_argv_token(tok: str, index: int) -> str:
    t = str(tok)
    pathlike = index == 0 or "/" in t or "\\" in t \
        or bool(re.match(r"^[A-Za-z]:", t))
    if not pathlike:
        return t
    t = t.replace("\\", "/")
    while t.startswith("./"):
        t = t[2:]
    if re.match(r"^[A-Za-z]:/", t):
        t = t[0].lower() + t[1:]
    if index == 0:
        t = t.lower()
    return t


def canonical_argv(cmd, _depth: int = 0) -> List[str]:
    """把命令拆成可严格比较的 argv；只做无歧义的规范化。"""
    s = str(cmd or "").strip()
    if not s:
        return []
    try:
        toks = shlex.split(s, posix=False)
    except ValueError:
        toks = s.split()
    toks = [_strip_quotes(t) for t in toks]
    if len(toks) >= 3 and os.path.basename(toks[0]).lower() in _SHELL_WRAPPERS:
        flag = toks[1].lower()
        if flag in ("-c", "/c", "-command", "-commandwithargs", "--command"):
            inner = " ".join(toks[2:])
            if _depth < 2:
                return canonical_argv(inner, _depth + 1)
    if toks and os.path.basename(toks[0]).lower() in _INTERPRETERS \
            and len(toks) >= 3 and toks[1] == "-m":
        toks = toks[2:]
    return [_norm_argv_token(t, i) for i, t in enumerate(toks)]


def _subcommand(pos: List[str]) -> Optional[str]:
    """识别常见工具的 subcommand，用于区分 build 和 build:prod。"""
    if not pos:
        return None
    prog = pos[0]
    if prog in _PACKAGE_MANAGERS:
        if len(pos) >= 3 and pos[1] in ("run", "exec", "script"):
            return pos[2]
        return pos[1] if len(pos) >= 2 else None
    if prog in _RUNNER_PROGRAMS:
        return pos[1] if len(pos) >= 2 else None
    return None


def compare_argv(candidate: str, source: str) -> Tuple[str, str]:
    """严格比较两条命令；ok 只代表 argv 完全一致。"""
    cand_text = str(candidate or "")
    if "..." in cand_text or "…" in cand_text:
        return "conflict", "abbreviated"
    c = canonical_argv(cand_text)
    s = canonical_argv(source)
    if not c or not s:
        return "not_found", ""
    if c[0] != s[0]:
        return "not_found", "program-differs"
    c_rest, s_rest = c[1:], s[1:]
    c_flags = Counter(t for t in c_rest if t.startswith("-"))
    s_flags = Counter(t for t in s_rest if t.startswith("-"))
    c_pos = [t for t in c_rest if not t.startswith("-")]
    s_pos = [t for t in s_rest if not t.startswith("-")]
    c_sub = _subcommand([t for t in c if not t.startswith("-")])
    s_sub = _subcommand([t for t in s if not t.startswith("-")])
    if c_sub is not None and s_sub is not None and c_sub != s_sub:
        return "conflict", "subcommand-conflict"
    if len(c_pos) < len(s_pos):
        return "conflict", "omitted-args"
    if len(c_pos) > len(s_pos):
        return "conflict", "extra-args"
    for a, b in zip(c_pos, s_pos):
        if a != b:
            return "conflict", "value-conflict"
    if c_flags != s_flags:
        return "conflict", "flags-differ"
    return "ok", ""


def _events(info: dict) -> List[dict]:
    evs = info.get("command_events")
    if isinstance(evs, list) and evs:
        return [e for e in evs if isinstance(e, dict)]
    # 兼容手工构造的 info：只有 commands 时视为 executed，但没有成功证据
    return [{"cmd": str(c), "channel": "tool_use", "tool_use_id": "",
             "result": "", "is_error": None, "success_evidence": False}
            for c in info.get("commands", [])]


def _event_level(ev: dict) -> str:
    if ev.get("success_evidence") and not ev.get("is_error"):
        return "succeeded"
    return "executed"


def _mentioned_text(info: dict) -> str:
    parts = list(info.get("prompts", []))
    parts += list(info.get("tool_results", []))
    parts += list(info.get("commands", []))
    parts += [str(e.get("result") or "") for e in _events(info)]
    return _norm_match("\n".join(str(p) for p in parts))


def _argv_overlap(a: str, b: str) -> float:
    ca = Counter(canonical_argv(a))
    cb = Counter(canonical_argv(b))
    total = sum(ca.values()) + sum(cb.values())
    if not total:
        return 0.0
    return sum((ca & cb).values()) / total


def match_command(candidate: str, info: dict):
    """返回 (status, reason, event, level)。

    status: ok | conflict | not_executed | not_found。
    多条来源取最优：ok > conflict（token 重合度最高）> not_found > not_executed。
    """
    best = None
    for ev in _events(info):
        status, reason = compare_argv(candidate, ev.get("cmd", ""))
        if status == "ok":
            return "ok", "", ev, _event_level(ev)
        if status == "conflict":
            score = _argv_overlap(candidate, ev.get("cmd", ""))
            # 参数不同的命令不算成功，即使同程序的另一条命令成功了
            if best is None or score > best[0]:
                best = (score, reason, ev)
    if best is not None:
        return "conflict", best[1], best[2], "executed"
    needle = _norm_match(candidate)
    if needle and needle in _mentioned_text(info):
        return "not_executed", "", None, "mentioned"
    return "not_found", "", None, "missing"


def _near_identifier(needle: str, source: str) -> bool:
    if not needle:
        return False
    base = os.path.basename(needle.replace("\\", "/")).lower()
    if len(base) >= 4 and base in source:
        return True
    toks = text_tokens(needle)
    if toks:
        hit = sum(1 for t in toks if t in source)
        if hit / len(toks) >= 0.6:
            return True
    return False


def _fragment_level(frag: str, info: dict, source: str) -> str:
    status, _reason, _ev, level = match_command(frag, info)
    if status == "ok":
        return level
    if status == "conflict":
        return "executed"
    needle = _norm_match(frag)
    if needle and needle in source:
        return "mentioned"
    return "missing"


def _sentence_parts(line: str) -> List[str]:
    return re.findall(r"[^。！？；!?;]+[。！？；!?;]?", line)


def check_outcome_claims(text: str, info: dict) -> str:
    """结果性表述缺少执行证据时加 ⚠?；只处理与标识符同句的动词。"""
    if not _RESULT_RE.search(text):
        return text
    source = _source_text(info)
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#") or "⚠?" in line:
            out.append(line)
            continue
        pieces = []
        for seg in _sentence_parts(line):
            if not _RESULT_RE.search(seg) or "`" not in seg:
                pieces.append(seg)
                continue
            frags = re.findall(r"`([^`]+)`", seg)
            levels = [_fragment_level(f, info, source) for f in frags]
            best = max(levels, key=lambda x: _LEVEL_ORDER.get(x, 0))
            if _LEVEL_ORDER.get(best, 0) >= _LEVEL_ORDER["succeeded"]:
                pieces.append(seg)
                continue
            m = re.match(r"^(.*?)([。！？；!?;]+)$", seg)
            if m:
                pieces.append(m.group(1).rstrip() + " ⚠?" + m.group(2))
            else:
                pieces.append(seg.rstrip() + " ⚠?")
        out.append("".join(pieces))
    return "\n".join(out)


def _mark_fragments(body: str, info: dict, source: str,
                    category: str) -> str:
    def repl(m):
        frag = m.group(1)
        if len(frag.strip()) < 4:
            return m.group(0)
        tail = body[m.end():].lstrip()
        if tail.startswith("⚠"):
            return m.group(0)
        if category == "命令":
            status, _reason, _ev, _level = match_command(frag, info)
            if status == "ok":
                return m.group(0)
            return f"`{frag}` " + ("⚠≠" if status == "conflict" else "⚠")
        needle = _norm_match(frag)
        if needle and (needle in source
                       or (len(needle) > 60 and needle[:60] in source)):
            return m.group(0)
        if _near_identifier(needle, source):
            return f"`{frag}` ⚠≠"
        return f"`{frag}` ⚠"

    return re.sub(r"`([^`]+)`", repl, body)


def verify_summary(text: str, info: dict) -> str:
    """把摘要里反引号包裹的标识符与原文比对，按证据等级加标记。"""
    source = _source_text(info)

    out = []
    in_identifiers = False
    for line in text.splitlines():
        h = line.strip()
        if h.startswith("#"):
            in_identifiers = "关键标识符" in h
        if in_identifiers:
            m = re.match(r"^(\s*-\s*)([^：:]+?)(\s*[：:])(.*)$", line)
            if m:
                line = (m.group(1) + m.group(2) + m.group(3)
                        + _mark_fragments(m.group(4), info, source,
                                          m.group(2).strip()))
        out.append(line)
    result = check_outcome_claims("\n".join(out), info)
    if "⚠" in result and "校验图例" not in result:
        result = _LEGEND + result
    return result


def carry_forward_warnings(text: str, prev_summary: str, info: dict) -> str:
    """旧摘要不是事实来源；它标过的条目在新文本里继续保持标记。

    只有当前增量记录重新提供了依据时，才允许去掉旧标记。
    """
    if not prev_summary or "⚠" not in prev_summary:
        return text
    pairs = re.findall(r"`([^`]+)`\s*(⚠≠|⚠\?|⚠)", prev_summary)
    if not pairs:
        return text
    source = _source_text(info)
    out = text
    for frag, mark in pairs:
        needle = _norm_match(frag)
        if needle and needle in source:
            continue
        if match_command(frag, info)[0] == "ok":
            continue
        pat = re.compile(r"`" + re.escape(frag) + r"`(?!\s*⚠)")
        out = pat.sub(lambda m: f"`{frag}` {mark}", out)
    if "⚠" in out and "校验图例" not in out:
        out = _LEGEND + out
    return out


def checked_summary(cfg: "Config", text: str, info: dict,
                    prev_summary: str = "") -> str:
    """按配置决定是否做事实校验；旧摘要只用于继承警告，不作为依据。"""
    if not cfg.verify:
        return text
    return carry_forward_warnings(verify_summary(text, info), prev_summary, info)


# ---------------------------------------------------------------------------
# 摘要输入与模型调用
# ---------------------------------------------------------------------------

def title_of(info: dict) -> str:
    if info.get("_first_title"):
        return info["_first_title"]
    if info["prompts"]:
        return trunc(clean_prompt(info["prompts"][0]), 40) or "未命名会话"
    return "未命名会话"


def first_date_of(info: dict) -> str:
    return (info.get("_first_date") or info.get("start") or "unknown").split(" ")[0]


def project_label(info: dict, proj_dir: Optional[str], projects: Dict[str, str]) -> str:
    candidates = []
    if info.get("cwd"):
        candidates.append(os.path.basename(os.path.normpath(str(info["cwd"]))))
    if proj_dir:
        candidates.append(os.path.basename(os.path.normpath(proj_dir)))
    for c in candidates:
        if c and c in projects:
            return projects[c]
    for c in candidates:
        if c:
            return c
    return "未分类"


def build_transcript(info: dict, max_chars: int = 36000) -> str:
    L = [f"平台: {'Claude Code' if info['kind'] == 'claude' else 'Codex'}",
         f"时间: {info.get('start')} ~ {info.get('end')}"]
    if info.get("cwd"):
        L.append(f"工作目录: {info['cwd']}")
    if info.get("branch"):
        L.append(f"分支: {info['branch']}")

    L += ["", "## 用户消息"]
    L += ["- " + trunc(p, 900) for p in info["prompts"][:40]]

    L += ["", "## 助手回复"]
    L += ["- " + trunc(a, 800) for a in info["assistant"][:25]]

    if info.get("commands"):
        L += ["", "## 执行过的命令"]
        L += ["- `" + trunc(c, 300) + "`" for c in info["commands"][:40]]
    if info.get("tool_results"):
        L += ["", "## 工具输出片段（可能含报错原文）"]
        L += ["- " + trunc(t, 400) for t in info["tool_results"][:25]]
    if info.get("modified"):
        L += ["", "## 修改过的文件"]
        L += ["- " + str(f) for f in info["modified"][:30]]
    if info["files"]:
        L += ["", "## 涉及文件"]
        L += ["- " + str(f) for f in list(info["files"].keys())[:40]]
    if info["tools"]:
        L += ["", "## 工具统计",
              ", ".join(f"{k}x{v}" for k, v in info["tools"].most_common(15))]

    s = "\n".join(L)
    if len(s) <= max_chars:
        return s
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return s[:head] + "\n\n...（中段省略）...\n\n" + s[-tail:]


def summary_prompt(info: dict, label: str, transcript: str) -> str:
    return f"""你在为一个程序员整理 AI 编程会话的记忆索引。读者是下一次会话里的 AI 和本人。
只输出 Markdown 正文，不要开场白，不要用代码块包裹整篇。

会话元数据：
- 项目：{label}
- 平台：{'Claude Code' if info['kind'] == 'claude' else 'Codex'}
- 时间：{info.get('start')} ~ {info.get('end')}
- 轮数：{info.get('turns')}

写作要求：
1. 用叙事体。人称固定：第二人称「你」只指用户（会话发起者），AI 一律称「助手」；不要用「他」「她」指人。
2. 「关键标识符」必须逐字抄录，不得改写、概括或翻译；不要加省略号，不要补全参数。但只收关键项，宁可少列，不要罗列。每一项没有就写「无」。
3. 只写会话记录里明确出现的事实。助手自己的推测、承诺、计划或「应该没问题」不算事实，不能当成已发生的结果。
4. 命令只有记录里确实调用执行过才能写进「命令」；只在讨论里提到的，要写「提到」而不是「执行」。没有明确成功输出（如 exit code 0、Process exited with code 0、All tests passed）时，不得写「通过」「成功」「修复完成」。
5. 不要自己添加 ⚠、⚠≠、⚠? 等标记，程序会统一校验和标注。

严格按以下结构输出：

## 这一段在做什么
（2-4 句）

## 走过的路
（2-5 句，讲卡在哪、怎么绕过去的）

## 关键标识符
- 路径：（只列本场实际修改、写入或报过错的，最多 8 条；仅读过的不列）
- 命令：（只列改变状态或解决问题的关键命令，最多 5 条）
- 参数/flag：（最多 8 个）
- 报错原文：（最多 3 条）
- 版本/模型：（最多 6 个）
- URL：（只列直接影响决策的，最多 3 条）

## 还没完的事
（1-3 条；没有就写「无」）

以下是会话记录：

{transcript}
"""


def continuation_prompt(prev_summary: str, info: dict, delta: str, date: str) -> str:
    return f"""这是同一场 AI 编程会话此前已经生成的摘要：

{prev_summary}

这是这场会话之后新增的对话：

{delta}

请只总结新增部分带来的变化，输出 Markdown：
## 续 · {date}

### 新增了什么
（2-4 句）

### 修正了什么
（若新增内容推翻了旧结论，写明「此前 X，现在 Y」；没有就写「无」）

### 关键标识符
- 路径：（只列新增部分实际修改、写入或报过错的，最多 6 条）
- 命令：（最多 4 条）
- 报错原文：（最多 2 条）

### 还没完的事
（1-3 条；没有就写「无」）

要求：不要重复旧摘要里已有的内容；只写新增记录里明确出现的事实，助手的推测不算事实；
标识符逐字抄录，不要加省略号；命令只有确实执行过才能写成「执行」，没有明确成功输出不得写「通过」「成功」；
不要自己添加 ⚠、⚠≠、⚠? 标记。
"""


def merge_prompt(prev_summary: str) -> str:
    return f"""以下是同一场 AI 编程会话的原始摘要和多次续写。请合并成一份完整摘要，
去掉重复，保留所有关键标识符和未完成事项。若续写推翻了旧结论，以最新结论为准。

只输出合并后的 Markdown 正文，保持以下结构：

## 这一段在做什么
（2-4 句）

## 走过的路
（2-5 句）

## 关键标识符
- 路径：
- 命令：
- 参数/flag：
- 报错原文：
- 版本/模型：
- URL：

## 还没完的事
（1-3 条）

要求：标识符逐字保留，不要加省略号或补全参数；不要编造待合并内容里没有的事实；
不要自己添加 ⚠、⚠≠、⚠? 标记，已有的标记保持原样。

以下是待合并内容：

{prev_summary}
"""


def api_endpoint(cfg: Config) -> str:
    """按接口格式拼出请求地址；OpenAI 兼容端点可能已带 /v1。"""
    base = cfg.base_url.rstrip("/")
    if cfg.api_format == "openai":
        return base + ("/chat/completions" if re.search(r"/v\d+$", base)
                       else "/v1/chat/completions")
    return base + "/v1/messages"


def _parse_response(fmt: str, d: dict) -> Tuple[str, dict, bool]:
    if not isinstance(d, dict):
        raise RuntimeError("模型端点返回的 JSON 不是对象")
    if fmt == "openai":
        choices = d.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI 兼容响应缺少 choices：{str(d)[:200]}")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        else:
            text = content or ""
        u = d.get("usage") or {}
        usage = {"input_tokens": u.get("prompt_tokens"),
                 "output_tokens": u.get("completion_tokens")}
        return str(text).strip(), usage, choices[0].get("finish_reason") == "length"

    text = "".join(b.get("text", "") for b in d.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text").strip()
    return text, d.get("usage") or {}, d.get("stop_reason") == "max_tokens"


def _request_llm(cfg: Config, fmt: str, url: str, prompt: str,
                 max_tokens: int, timeout: int):
    """发一次请求；网络错误重试一次，返回 (text, usage, truncated)。"""
    last_err = None
    for net_attempt in range(2):
        body = json.dumps({
            "model": cfg.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        headers = {"content-type": "application/json",
                   "authorization": "Bearer " + cfg.api_key}
        if fmt == "anthropic":
            headers.update({"anthropic-version": "2023-06-01",
                            "x-api-key": cfg.api_key})
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            if e.code in (408, 429, 500, 502, 503, 504) and net_attempt == 0:
                last_err = e
                time.sleep(2)
                continue
            raise RuntimeError(
                f"模型端点返回 HTTP {e.code}：{detail or e.reason}\n"
                f"当前接口格式：{fmt}（{url}）。如果这是自定义地址，"
                f"确认 --api-format 与端点格式一致。") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
            if net_attempt == 0:
                time.sleep(2)
                continue
            raise RuntimeError(f"请求失败：{e}") from e

        try:
            d = json.loads(raw)
        except ValueError as e:
            raise RuntimeError(
                f"模型端点返回的不是 JSON（当前按 {fmt} 格式请求 {url}）："
                f"{raw[:200]}") from e

        return _parse_response(fmt, d)
    raise RuntimeError(f"请求失败：{last_err}")


def call_llm(cfg: Config, prompt: str, max_tokens: int = 8000, timeout: int = 300):
    if not cfg.api_key:
        raise RuntimeError(
            "缺少 API key：请设置 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY，"
            "或传入 --api-key")
    fmt = cfg.api_format if cfg.api_format in ("anthropic", "openai") else "anthropic"
    if fmt == "openai" and not cfg.model:
        raise RuntimeError(
            "OpenAI 兼容模式需要指定模型：--model 或环境变量 OPENAI_MODEL")
    url = api_endpoint(cfg)

    budget = max_tokens
    expansions = 0
    while True:
        text, usage, truncated = _request_llm(
            cfg, fmt, url, prompt, budget, timeout)
        if not truncated:
            if text:
                return text, usage
            raise RuntimeError(f"模型未返回文本（{fmt}）")
        # 被截断的正文不能当成完整摘要写入；先扩容重试，到上限就报错。
        if expansions >= 2 or budget >= 32000:
            raise RuntimeError(
                f"模型输出被截断，max_tokens={budget} 已达上限。"
                "为避免写入不完整摘要，本次不保存，下一轮会重试。")
        budget = min(budget * 2, 32000)
        expansions += 1


def summarize_one(cfg: Config, info: dict, label: str):
    transcript = redact_text(build_transcript(info), cfg.redact)
    label = redact_text(label, cfg.redact)
    text, usage = call_llm(cfg, summary_prompt(info, label, transcript))
    return text, usage, len(transcript)


def estimate(chars: int) -> int:
    return chars // 3


# ---------------------------------------------------------------------------
# 输出：文件与块操作
# ---------------------------------------------------------------------------

def _safe_name(s: str, limit: int = 40) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s)[:limit]


def strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[end + 5:].lstrip("\n")
    return text


def _next_monthly_block(text: str, pos: int) -> int:
    """找下一个真正的会话块标题，跳过摘要正文里的 `## 续` 等子标题。"""
    i = pos
    while True:
        j = text.find("\n## ", i)
        if j < 0:
            return len(text)
        window = text[j:j + 400]
        dated = re.match(r"\n## \d{4}-\d{2}-\d{2}\b", window)
        if dated and re.search(r"\nsession:\s*`", window):
            return j
        i = j + 1


def find_monthly_block(text: str, sid: str) -> Optional[Tuple[int, int]]:
    """定位 `session: \\`sid\\`` 所在的块，返回 [start, end)。

    块尾以下一个会话块标题为准，而不是第一个 `---`：
    合并后的摘要正文本身可能包含分隔线。
    """
    marker = f"session: `{sid}`"
    i = text.find(marker)
    if i < 0:
        return None
    start = text.rfind("\n## ", 0, i)
    start = 0 if start < 0 else start + 1
    end = _next_monthly_block(text, i)
    return start, end


def block_text(text: str, sid: str) -> Optional[str]:
    span = find_monthly_block(text, sid)
    if not span:
        return None
    start, end = span
    block = text[start:end]
    block = re.sub(r"^## .*\n+", "", block)
    block = re.sub(r"^session: `[^`]+`\n+", "", block)
    block = re.sub(r"\n*---\s*$", "", block)
    return block.strip()


def replace_block(text: str, sid: str, new_block: str) -> str:
    span = find_monthly_block(text, sid)
    if not span:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "\n" + new_block
    start, end = span
    return text[:start] + new_block + text[end:]


def insert_into_block(text: str, sid: str, addition: str) -> str:
    span = find_monthly_block(text, sid)
    if not span:
        return replace_block(text, sid, addition)
    start, end = span
    block = text[start:end]
    body = re.sub(r"\n*---\s*$", "", block)
    tail = text[end:]
    if tail and not tail.startswith("\n\n"):
        tail = "\n" + tail
    return text[:start] + body.rstrip() + "\n\n" + addition.strip() + "\n\n---" + tail


def monthly_block(month: str, info: dict, label: str, text: str, sid: str) -> str:
    date = first_date_of(info)
    return "\n".join([
        f"## {date} · {label} · {info.get('turns')} 条消息",
        "",
        f"session: `{sid}`",
        "",
        text.strip(),
        "",
        "---",
        "",
    ])


def obsidian_path(cfg: Config, info: dict, sid: str) -> Optional[str]:
    if not cfg.obsidian_dir:
        return None
    date = first_date_of(info)
    return os.path.join(cfg.obsidian_dir, date[:7], f"{date}_{sid[:8]}.md")


def obsidian_doc(info: dict, label: str, text: str, sid: str) -> str:
    date = first_date_of(info)
    title = title_of(info).replace('"', "'")
    return "\n".join([
        "---",
        f'title: "{title}"',
        f"date: {date}",
        f"project: {label}",
        f"platform: {'claude-code' if info['kind'] == 'claude' else 'codex'}",
        f'session: "{sid}"',
        f"turns: {info.get('turns')}",
        "tags: [自动摘要]",
        "---", "",
        f"# {title}", "", text.strip(), "",
    ])


def write_full(cfg: Config, info: dict, label: str, text: str, sid: str) -> Optional[str]:
    path = obsidian_path(cfg, info, sid)
    if path:
        write_text_atomic(path,
                          redact_text(obsidian_doc(info, label, text, sid), cfg.redact))

    month = first_date_of(info)[:7] or datetime.now().strftime("%Y-%m")
    if cfg.memory_dir:
        d = os.path.join(cfg.memory_dir, "sessions")
        os.makedirs(d, exist_ok=True)
        mpath = os.path.join(d, f"{month}.md")
        try:
            with open(mpath, encoding="utf-8") as fh:
                cur = fh.read()
        except Exception:
            cur = f"# {month} 会话摘要\n\n> 自动生成。原文可在本地会话记录中检索。\n"
        cur = replace_block(cur, sid, monthly_block(month, info, label, text, sid))
        write_text_atomic(mpath, redact_text(cur, cfg.redact))
        update_memory_index(cfg, month)
    return path


def continuation_marker(sid: str, new_offset: int) -> str:
    return f"<!-- sd:cont key={sid} offset={int(new_offset)} -->"


def append_continuation(cfg: Config, info: dict, label: str, text: str,
                        sid: str, prev_summary: str,
                        new_offset: int = 0) -> Optional[str]:
    """追加续段；同一偏移量的续段只写一次，便于崩溃后安全重跑。"""
    marker = continuation_marker(sid, new_offset)
    body = marker + "\n\n" + text.strip()

    path = obsidian_path(cfg, info, sid)
    if path:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cur = fh.read()
            if marker not in cur:
                write_text_atomic(path, redact_text(
                    cur.rstrip("\n") + "\n\n" + body + "\n", cfg.redact))
        else:
            # 文件丢失时，用旧摘要 + 续段重建
            write_text_atomic(path, redact_text(obsidian_doc(
                info, label, prev_summary.rstrip() + "\n\n" + body, sid),
                cfg.redact))

    month = first_date_of(info)[:7] or datetime.now().strftime("%Y-%m")
    if cfg.memory_dir:
        d = os.path.join(cfg.memory_dir, "sessions")
        os.makedirs(d, exist_ok=True)
        mpath = os.path.join(d, f"{month}.md")
        try:
            with open(mpath, encoding="utf-8") as fh:
                cur = fh.read()
        except Exception:
            cur = f"# {month} 会话摘要\n\n> 自动生成。原文可在本地会话记录中检索。\n"
        if marker in cur:
            pass
        elif find_monthly_block(cur, sid):
            cur = insert_into_block(cur, sid, body)
            write_text_atomic(mpath, redact_text(cur, cfg.redact))
        else:
            cur = replace_block(
                cur, sid,
                monthly_block(month, info, label,
                              prev_summary.rstrip() + "\n\n" + body, sid))
            write_text_atomic(mpath, redact_text(cur, cfg.redact))
        update_memory_index(cfg, month)
    return path


def update_memory_index(cfg: Config, month: str) -> bool:
    if not cfg.memory_dir:
        return False
    idx = os.path.join(cfg.memory_dir, "MEMORY.md")
    line = f"- [会话摘要 {month}](sessions/{month}.md) — 本月 AI 会话的自动叙事索引"
    try:
        with open(idx, encoding="utf-8") as f:
            cur = f.read()
    except Exception:
        cur = ""
    if f"sessions/{month}.md" in cur:
        return False
    if cur and not cur.endswith("\n"):
        cur += "\n"
    write_text_atomic(idx, cur + line + "\n")
    return True


def strip_continuation_markers(text: str) -> str:
    return re.sub(r"<!--\s*sd:cont[^>]*-->\s*\n?", "", text or "")


def find_previous_summary(cfg: Config, key: str) -> Optional[str]:
    """从已写入的 Obsidian 或月度文件里读回上一份摘要。"""
    if cfg.obsidian_dir:
        pattern = os.path.join(cfg.obsidian_dir, "*", f"*_{key[:8]}.md")
        for p in glob.glob(pattern):
            try:
                with open(p, encoding="utf-8") as fh:
                    raw = fh.read()
            except Exception:
                continue
            if f'session: "{key}"' in raw:
                return strip_continuation_markers(
                    strip_frontmatter(raw)).strip()
    if cfg.memory_dir:
        for p in glob.glob(os.path.join(cfg.memory_dir, "sessions", "*.md")):
            try:
                with open(p, encoding="utf-8") as fh:
                    raw = fh.read()
            except Exception:
                continue
            t = block_text(raw, key)
            if t:
                return strip_continuation_markers(t).strip()
    return None


# ---------------------------------------------------------------------------
# 结论代理与合并防丢
# ---------------------------------------------------------------------------

_DECISION_RE = re.compile(r"因为|由于|所以|决定|改为|不再|暂时|尚未|完成|采用|保留|放弃")
_SUPERSEDED_RE = re.compile(r"改为|不再|此前|推翻|废弃|作废")
_MAX_FACTS = 40


def _summary_sections(text: str) -> List[Tuple[str, List[str]]]:
    sections: List[Tuple[str, List[str]]] = []
    cur_name = ""
    cur_lines: List[str] = []

    def flush():
        if cur_name or cur_lines:
            sections.append((cur_name, cur_lines))

    for line in str(text or "").splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            cur_name = m.group(2).strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    flush()
    return sections


def _fact_tokens(text: str) -> List[str]:
    return sorted(text_tokens(text))


def extract_conclusions(text: str, limit: int = _MAX_FACTS) -> List[dict]:
    """把摘要里的标识符、待办和决策句抽成可比较的结论代理。

    这是近似检查，不证明语义完全保留。
    """
    facts: List[dict] = []
    for name, lines in _summary_sections(strip_frontmatter(text or "")):
        body = "\n".join(lines)
        if "关键标识符" in name:
            for frag in re.findall(r"`([^`]+)`", body):
                frag = frag.strip()
                if len(frag) >= 4 and not frag.startswith("⚠"):
                    facts.append({"type": "identifier", "text": frag,
                                  "tokens": _fact_tokens(frag)})
        if "还没完" in name or "待办" in name:
            for line in lines:
                s = re.sub(r"^\s*[-*]\s+", "", line).strip()
                if s and s not in ("无", "暂无", "无。"):
                    facts.append({"type": "todo", "text": s,
                                  "tokens": _fact_tokens(s)})
        if any(k in name for k in ("走过的路", "这一段", "修正了什么",
                                   "新增了什么", "关键标识符")):
            for sentence in re.split(r"(?<=[。！？；])", body):
                s = sentence.strip()
                if len(s) < 6 or not _DECISION_RE.search(s):
                    continue
                statement, reason = s, ""
                m = re.search(r"(因为|由于)(.+)$", s)
                if m:
                    statement = s[:m.start()].strip(" ，,。；;")
                    reason = m.group(2).strip(" 。；;")
                facts.append({
                    "type": "decision",
                    "statement": statement,
                    "reason": reason,
                    "status": ("superseded"
                               if _SUPERSEDED_RE.search(s) else "active"),
                    "tokens": _fact_tokens(statement),
                    "reason_tokens": _fact_tokens(reason),
                })

    seen = set()
    out = []
    for f in facts:
        key = (f["type"], f.get("text") or f.get("statement"))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out[:limit]


def merge_fact_lists(*lists) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for lst in lists:
        for f in lst or []:
            if not isinstance(f, dict):
                continue
            key = (f.get("type"), f.get("text") or f.get("statement"))
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out[:_MAX_FACTS]


def _coverage(need: List[str], have: set) -> float:
    need_set = set(need or [])
    if not need_set:
        return 1.0
    return len(need_set & have) / len(need_set)


def merge_loss_check(old_facts: List[dict], merged: str) -> List[str]:
    """返回合并后可能丢失的旧结论；空列表表示通过。"""
    merged_text = str(merged or "")
    merged_norm = _norm_match(merged_text)
    merged_tokens = text_tokens(merged_text)
    lost: List[str] = []
    for f in old_facts or []:
        if not isinstance(f, dict):
            continue
        typ = f.get("type")
        if typ in ("identifier", "todo"):
            text = str(f.get("text") or "")
            needle = _norm_match(text)
            if needle and needle in merged_norm:
                continue
            if typ == "todo" and _coverage(f.get("tokens"), merged_tokens) >= 0.6:
                continue
            lost.append(text)
        elif typ == "decision":
            statement = str(f.get("statement") or "")
            if _coverage(f.get("tokens"), merged_tokens) < 0.6:
                lost.append("结论：" + statement)
                continue
            if f.get("reason") and _coverage(f.get("reason_tokens"),
                                             merged_tokens) < 0.5:
                lost.append("原因：" + str(f.get("reason")))
                continue
            if f.get("status") == "superseded" \
                    and not _SUPERSEDED_RE.search(merged_text):
                lost.append("状态：" + statement)
    return lost


# ---------------------------------------------------------------------------
# 索引与失败队列
# ---------------------------------------------------------------------------

def load_index(cfg: Config) -> dict:
    try:
        with open(cfg.index_file, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("sessions"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "sessions": {}}


def save_index(cfg: Config, index: dict) -> None:
    write_text_atomic(cfg.index_file,
                      json.dumps(index, ensure_ascii=False, indent=1))


def update_index(cfg: Config, info: dict, label: str, text: str, sid: str,
                 source: str, mode: str) -> None:
    ensure_ref_key(cfg)
    index = load_index(cfg)
    path = obsidian_path(cfg, info, sid)
    if not path and cfg.memory_dir:
        month = first_date_of(info)[:7] or datetime.now().strftime("%Y-%m")
        path = os.path.join(cfg.memory_dir, "sessions", f"{month}.md")
    index["sessions"][sid] = {
        "date": (info.get("_first_date") or info.get("start")),
        "project": redact_text(label, cfg.redact),
        "platform": info.get("kind"),
        "title": redact_text(title_of(info), cfg.redact),
        "sid": sid,
        "file": redact_text(path or "", cfg.redact),
        "source": redact_text(source or "", cfg.redact),
        "file_ref": make_path_ref(cfg, path, "obsidian", sid=sid),
        "source_ref": make_path_ref(
            cfg, source, "claude" if info.get("kind") == "claude" else "codex",
            sid=sid),
        "mode": mode,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    save_index(cfg, index)


def load_failures(cfg: Config) -> dict:
    try:
        with open(cfg.failures_file, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_failures(cfg: Config, failures: dict) -> None:
    write_text_atomic(cfg.failures_file,
                      json.dumps(failures, ensure_ascii=False, indent=1))


def record_failure(cfg: Config, key: str, path: str, error: str,
                   kind: str = "claude") -> None:
    ensure_ref_key(cfg)
    failures = load_failures(cfg)
    ent = failures.get(key) or {"attempts": 0}
    ent.update({
        "path": redact_text(str(path or ""), cfg.redact),
        "path_ref": make_path_ref(
            cfg, path, "claude" if kind == "claude" else "codex", sid=key),
        "kind": kind,
        "error": redact_text(str(error), cfg.redact)[:500],
        "attempts": int(ent.get("attempts", 0)) + 1,
        "last": datetime.now().isoformat(timespec="seconds"),
    })
    failures[key] = ent
    save_failures(cfg, failures)


def clear_failure(cfg: Config, key: str) -> None:
    failures = load_failures(cfg)
    if key in failures:
        del failures[key]
        save_failures(cfg, failures)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def load_state(cfg: Config) -> dict:
    try:
        with open(cfg.state_file, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(cfg: Config, state: dict) -> None:
    write_text_atomic(cfg.state_file,
                      json.dumps(state, ensure_ascii=False, indent=1))


def ref_migration_status(cfg: Config, ref, raw=None) -> str:
    """只读判断一条 ref 的状态，不需要密钥、不写盘。"""
    if isinstance(ref, dict) and ref.get("v") == _REF_VERSION:
        return "ok"
    if isinstance(ref, dict) and isinstance(ref.get("rel"), str):
        root = ref.get("root")
        return "v1" if isinstance(root, str) and _root_dir(cfg, root) \
            else "unconfigured"
    if _legacy_plain_path(raw):
        for kind in _REF_ROOTS:
            base = _root_dir(cfg, kind)
            if base and _is_within(raw, base):
                return "legacy_raw"
        return "legacy_outside_root"
    if isinstance(raw, str) and raw:
        return "legacy_redacted"
    return "none"


def upgrade_ref(cfg: Config, ref, sid: Optional[str] = None,
                raw=None, key: Optional[bytes] = None):
    """把 v1 / 无 ref 的记录升级成 v2；返回 (新 ref, 状态)。"""
    key = key or _load_ref_key(cfg, create=False)
    if isinstance(ref, dict) and ref.get("v") == _REF_VERSION:
        new = copy.deepcopy(ref)
        changed = False
        if sid and not new.get("sid"):
            new["sid"] = sid
            changed = True
        return new, ("upgraded" if changed else "ok")
    if isinstance(ref, dict) and isinstance(ref.get("rel"), str):
        root = ref.get("root")
        if not isinstance(root, str) or not _root_dir(cfg, root):
            return ref, "unconfigured"
        if not key:
            return ref, "key_missing"
        new = _ref_from_rel(cfg, root, ref["rel"], key, sid=sid)
        return (new, "upgraded") if new else (ref, "invalid")
    if _legacy_plain_path(raw):
        if not key:
            return None, "key_missing"
        new = make_path_ref(cfg, raw, sid=sid, key=key)
        return (new, "legacy_raw") if new else (None, "legacy_outside_root")
    if isinstance(raw, str) and raw:
        return None, "legacy_redacted"
    return None, "none"


def _bump(report: Optional[dict], section: str, status: str) -> None:
    if report is None:
        return
    report.setdefault(section, Counter())[status] += 1


def scrub_state(cfg: Config, state: dict,
                report: Optional[dict] = None) -> bool:
    """原地脱敏旧 state 条目，并把 ref 升级到 v2；返回是否有改动。"""
    changed = False
    key_bytes = _load_ref_key(cfg, create=False)
    for sid, entry in state.items():
        if not isinstance(entry, dict):
            continue
        status = ref_migration_status(cfg, entry.get("source_ref"),
                                      entry.get("source"))
        _bump(report, "state", status)
        new_ref, new_status = upgrade_ref(
            cfg, entry.get("source_ref"), sid=str(sid),
            raw=entry.get("source"), key=key_bytes)
        if new_ref is not None and new_ref != entry.get("source_ref"):
            entry["source_ref"] = new_ref
            changed = True
        for key in ("first_title", "source"):
            value = entry.get(key)
            if not isinstance(value, str):
                continue
            clean = redact_text(value, cfg.redact)
            if clean != value:
                entry[key] = clean
                changed = True
        if new_status in ("key_missing", "invalid", "unconfigured"):
            _bump(report, "state_errors", new_status)
    return changed


def migrate_index(cfg: Config, index: dict,
                  report: Optional[dict] = None) -> bool:
    """旧索引只存绝对路径；先生成 v2 ref，再脱敏字段。幂等。"""
    changed = False
    sessions = index.get("sessions")
    if not isinstance(sessions, dict):
        return False
    key_bytes = _load_ref_key(cfg, create=False)
    for sid, rec in sessions.items():
        if not isinstance(rec, dict):
            continue
        if not rec.get("sid"):
            rec["sid"] = str(sid)
            changed = True
        for key in ("file", "source"):
            raw = rec.get(key)
            status = ref_migration_status(cfg, rec.get(key + "_ref"), raw)
            _bump(report, "index", status)
            new_ref, new_status = upgrade_ref(
                cfg, rec.get(key + "_ref"), sid=str(sid), raw=raw,
                key=key_bytes)
            if new_ref is not None and new_ref != rec.get(key + "_ref"):
                rec[key + "_ref"] = new_ref
                changed = True
            if isinstance(raw, str):
                clean = redact_text(raw, cfg.redact)
                if clean != raw:
                    rec[key] = clean
                    changed = True
            if new_status in ("key_missing", "invalid", "unconfigured"):
                _bump(report, "index_errors", new_status)
        for key in ("project", "title"):
            value = rec.get(key)
            if isinstance(value, str):
                clean = redact_text(value, cfg.redact)
                if clean != value:
                    rec[key] = clean
                    changed = True
    return changed


def migrate_failures(cfg: Config, failures: dict,
                     report: Optional[dict] = None) -> bool:
    """旧失败队列的 path 是明文绝对路径；补 v2 ref 后脱敏。幂等。"""
    changed = False
    key_bytes = _load_ref_key(cfg, create=False)
    for sid, entry in failures.items():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        status = ref_migration_status(cfg, entry.get("path_ref"), raw)
        _bump(report, "failures", status)
        new_ref, new_status = upgrade_ref(
            cfg, entry.get("path_ref"), sid=str(sid), raw=raw, key=key_bytes)
        if new_ref is not None and new_ref != entry.get("path_ref"):
            entry["path_ref"] = new_ref
            changed = True
        if isinstance(raw, str):
            clean = redact_text(raw, cfg.redact)
            if clean != raw:
                entry["path"] = clean
                changed = True
        if new_status in ("key_missing", "invalid", "unconfigured"):
            _bump(report, "failures_errors", new_status)
    return changed


def migrate_metadata(cfg: Config, report: Optional[dict] = None) -> bool:
    """非 dry-run 入口统一调用；只在确有改动时写盘。"""
    ensure_ref_key(cfg)
    changed = False
    state = load_state(cfg)
    if scrub_state(cfg, state, report):
        save_state(cfg, state)
        changed = True
    index = load_index(cfg)
    if migrate_index(cfg, index, report):
        save_index(cfg, index)
        changed = True
    failures = load_failures(cfg)
    if migrate_failures(cfg, failures, report):
        save_failures(cfg, failures)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# 扫描与规划
# ---------------------------------------------------------------------------

def iter_session_files(cfg: Config) -> Iterator[Tuple[str, str, Optional[str]]]:
    if os.path.isdir(cfg.claude_root):
        for proj_dir in glob.glob(os.path.join(cfg.claude_root, "*")):
            if not os.path.isdir(proj_dir):
                continue
            for f in glob.glob(os.path.join(proj_dir, "*.jsonl")):
                yield f, "claude", proj_dir
    if os.path.isdir(cfg.codex_root):
        for f in glob.glob(os.path.join(cfg.codex_root, "**", "*.jsonl"), recursive=True):
            yield f, "codex", None


def collect_files(cfg: Config) -> List[Tuple[float, str, str, Optional[str]]]:
    files = []
    for f, kind, proj_dir in iter_session_files(cfg):
        try:
            files.append((os.path.getmtime(f), f, kind, proj_dir))
        except OSError:
            continue
    files.sort(key=lambda x: x[0], reverse=True)
    return files


def _migrate_entry(cfg: Config, state: dict, key: str, path: str) -> None:
    """v0.1 状态没有 offset：补签，不重跑。"""
    size = os.path.getsize(path)
    state[key]["offset"] = size
    state[key]["prefix_sha1"] = prefix_sha1(path, size)
    state[key].setdefault("sections", 0)


def plan_nightly(cfg: Config, state: dict) -> Tuple[List[Task], List[str]]:
    now = time.time()
    tasks: List[Task] = []
    notes: List[str] = []

    for mt, path, kind, proj_dir in collect_files(cfg):
        if now - mt < cfg.min_age_hours * 3600:
            continue
        key = os.path.splitext(os.path.basename(path))[0]
        entry = None if cfg.force else state.get(key)

        # 先用文件名猜 key，解析后再用真实 session id 修正
        if entry is None:
            info = parse_session(path, kind)
            if not info["turns"] and not info["prompts"]:
                continue
            key = session_key(info, path)
            entry = None if cfg.force else state.get(key)
            if entry is None:
                if info["turns"] < cfg.min_turns:
                    continue
                size = os.path.getsize(path)
                tasks.append(Task(
                    info=info, label=project_label(info, proj_dir, cfg.projects),
                    kind=kind, path=path, key=key, mode="full",
                    start_offset=0, new_offset=size))
                if len(tasks) >= cfg.max_sessions:
                    break
                continue

        # 旧条目迁移
        if "offset" not in entry:
            _migrate_entry(cfg, state, key, path)
            notes.append(f"迁移旧状态：{key[:8]}（不重跑）")
            continue

        offset = int(entry["offset"])
        size = os.path.getsize(path)

        # 先校验前缀：文件被重写（哪怕大小相同）都要整场重跑
        if size < offset or prefix_sha1(path, offset) != entry.get("prefix_sha1"):
            info = parse_session(path, kind)
            tasks.append(Task(
                info=info, label=project_label(info, proj_dir, cfg.projects),
                kind=kind, path=path, key=key, mode="full",
                start_offset=0, new_offset=size))
            notes.append(f"文件被重写，整场重跑：{key[:8]}")
            if len(tasks) >= cfg.max_sessions:
                break
            continue

        if size == offset:
            if int(entry.get("sections", 0)) >= cfg.merge_after:
                info = parse_session(path, kind)
                prev = find_previous_summary(cfg, key)
                if prev:
                    tasks.append(Task(
                        info=info, label=project_label(info, proj_dir, cfg.projects),
                        kind=kind, path=path, key=key, mode="merge",
                        start_offset=offset, new_offset=offset,
                        prev_summary=prev, sections=int(entry.get("sections", 0))))
            continue

        delta, new_offset = parse_incremental(path, kind, offset)
        delta_chars = len(build_transcript(delta))
        if delta["turns"] < cfg.min_delta_turns and delta_chars < cfg.min_delta_chars:
            continue
        prev = find_previous_summary(cfg, key)
        if not prev:
            info = parse_session(path, kind)
            tasks.append(Task(
                info=info, label=project_label(info, proj_dir, cfg.projects),
                kind=kind, path=path, key=key, mode="full",
                start_offset=0, new_offset=size))
            notes.append(f"找不到旧摘要，整场重跑：{key[:8]}")
            if len(tasks) >= cfg.max_sessions:
                break
            continue

        info = delta
        info["session"] = info.get("session") or key
        tasks.append(Task(
            info=info, label=project_label(info, proj_dir, cfg.projects),
            kind=kind, path=path, key=key, mode="incremental",
            start_offset=offset, new_offset=new_offset, prev_summary=prev,
            sections=int(entry.get("sections", 0))))
        if len(tasks) >= cfg.max_sessions:
            break

    if tasks:
        tasks.sort(key=lambda t: t.info.get("start") or "", reverse=True)
    return tasks, notes


def plan_preview(cfg: Config, n: int) -> List[Tuple[dict, str, str, str, str]]:
    out = []
    for _mt, f, kind, proj_dir in collect_files(cfg):
        info = parse_session(f, kind)
        if not info["turns"] and not info["prompts"]:
            continue
        out.append((info, project_label(info, proj_dir, cfg.projects), kind, f,
                    session_key(info, f)))
        if len(out) >= n:
            break
    return out


def print_plan(tasks, notes: Optional[List[str]] = None) -> None:
    chars = sum(len(build_transcript(t.info)) for t in tasks)
    print(f"计划处理 {len(tasks)} 项，预计输入约 {estimate(chars):,} tokens"
          f"（输出另计，按你所用模型的价格收费）")
    for t in tasks:
        mode = {"full": "全量", "incremental": "续写", "merge": "合并"}[t.mode]
        print(f"  [{mode}] [{redact_text(t.label, 'strict')}] "
              f"{t.info.get('start')} · {t.info.get('turns')} 条消息 · "
              f"{redact_text(os.path.basename(t.path), 'strict')}")
    for n in notes or []:
        print(f"  · {n}")


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _today(info: dict) -> str:
    return (info.get("end") or info.get("start") or datetime.now().strftime("%Y-%m-%d %H:%M"))[:16]


def execute_task(cfg: Config, task: Task, state: dict) -> bool:
    ensure_ref_key(cfg)
    sid = task.key
    prev_entry = state.get(sid) or {}
    info = dict(task.info)
    info["_first_date"] = prev_entry.get("first_date") or info.get("start")
    info["_first_title"] = redact_text(
        prev_entry.get("first_title") or title_of(info), cfg.redact)

    try:
        if task.mode == "full":
            text, usage, nchars = summarize_one(cfg, info, task.label)
            text = checked_summary(cfg, text, info)
            write_full(cfg, info, task.label, text, sid)
            sections = 0
            # state 里的结论代理同样要脱敏，不能绕过落盘前的隐私处理
            facts = extract_conclusions(redact_text(text, cfg.redact))
            print(f"  全量 {nchars} 字符 -> in={usage.get('input_tokens')} "
                  f"out={usage.get('output_tokens')}")

        elif task.mode == "incremental":
            delta = redact_text(build_transcript(info), cfg.redact)
            prev = redact_text(task.prev_summary or "", cfg.redact)
            prompt = continuation_prompt(prev, info, delta, _today(info))
            text, usage = call_llm(cfg, prompt)
            text = checked_summary(cfg, text, info, prev)
            # 偏移量用调用前确定的边界：调用期间追加的行留给下一轮
            append_continuation(cfg, info, task.label, text, sid, prev,
                                task.new_offset)
            sections = task.sections + 1
            old_facts = prev_entry.get("digest_facts") \
                or extract_conclusions(prev)
            facts = merge_fact_lists(
                old_facts,
                extract_conclusions(redact_text(text, cfg.redact)))
            print(f"  续写 {len(delta)} 字符 -> in={usage.get('input_tokens')} "
                  f"out={usage.get('output_tokens')}（第 {sections} 段）")

        else:  # merge
            prev = redact_text(task.prev_summary or "", cfg.redact)
            text, usage = call_llm(cfg, merge_prompt(prev))
            text = checked_summary(cfg, text, info)
            old_facts = prev_entry.get("digest_facts") \
                or extract_conclusions(prev)
            safe_text = redact_text(text, cfg.redact)
            lost = merge_loss_check(old_facts, safe_text)
            if lost:
                rejections = int(prev_entry.get("merge_rejections", 0)) + 1
                prev_entry = dict(prev_entry)
                prev_entry["merge_rejections"] = rejections
                if rejections >= 3:
                    prev_entry["sections"] = 0
                    prev_entry["merge_rejections"] = 0
                    state[sid] = prev_entry
                    save_state(cfg, state)
                    print("  合并连续 3 次会丢失旧结论，保留原摘要并跳过合并。",
                          file=sys.stderr)
                    clear_failure(cfg, sid)
                    return True
                state[sid] = prev_entry
                save_state(cfg, state)
                raise RuntimeError(
                    "合并会丢失旧结论：" + "；".join(lost[:3]))
            write_full(cfg, info, task.label, text, sid)
            sections = 0
            facts = extract_conclusions(safe_text)
            print(f"  合并 -> in={usage.get('input_tokens')} "
                  f"out={usage.get('output_tokens')}")

        # 先更新索引，再写 state（提交点）：state 写成功才代表这一轮完成。
        update_index(cfg, info, task.label, text, sid, task.path, task.mode)

        state[sid] = {
            "offset": task.new_offset,
            "prefix_sha1": prefix_sha1(task.path, task.new_offset),
            "sections": sections,
            "date": info.get("_first_date"),
            "first_date": info.get("_first_date"),
            "first_title": redact_text(info.get("_first_title") or "", cfg.redact),
            "source": redact_text(task.path, cfg.redact),
            "source_ref": make_path_ref(
                cfg, task.path,
                "claude" if task.kind == "claude" else "codex", sid=sid),
            "tokens": usage,
            "digest_facts": facts,
            "merge_rejections": 0,
            "done_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(cfg, state)
        clear_failure(cfg, sid)
        return True

    except Exception as e:
        print("  失败:", safe_error(e, cfg.redact), file=sys.stderr)
        if cfg.verbose:
            import traceback
            print(redact_text(traceback.format_exc(), cfg.redact),
                  file=sys.stderr)
        record_failure(cfg, sid, task.path, e, task.kind)
        return False


# ---------------------------------------------------------------------------
# 模式
# ---------------------------------------------------------------------------

def cmd_preview(cfg: Config, n: int, dry: bool) -> int:
    todo = plan_preview(cfg, n)
    if not todo:
        print("没有找到可处理的会话。")
        return 0
    chars = sum(len(build_transcript(i)) for i, *_ in todo)
    print(f"计划处理 {len(todo)} 场会话，预计输入约 {estimate(chars):,} tokens"
          f"（输出另计，按你所用模型的价格收费）")
    if dry:
        for info, label, _kind, path, _key in todo:
            print(f"  [{redact_text(label, cfg.redact)}] {info.get('start')} · "
                  f"{info.get('turns')} 条消息 · "
                  f"{redact_text(path, cfg.redact)}")
        return 0

    os.makedirs(cfg.out_dir, exist_ok=True)
    for i, (info, label, kind, path, key) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {redact_text(label, cfg.redact)} | "
              f"{info.get('start')} | {info.get('turns')} 条消息")
        try:
            text, usage, nchars = summarize_one(cfg, info, label)
        except Exception as e:
            print("  失败:", safe_error(e, cfg.redact), file=sys.stderr)
            if cfg.verbose:
                import traceback
                print(redact_text(traceback.format_exc(), cfg.redact),
                      file=sys.stderr)
            continue
        text = checked_summary(cfg, text, info)
        date = (info.get("start") or "unknown").split(" ")[0]
        title = title_of(info).replace('"', "'")
        out = os.path.join(cfg.out_dir, f"{date}_{key[:8]}.md")
        doc = "\n".join([
            "---", f'title: "{title}"', f"project: {label}",
            f"platform: {'claude-code' if kind == 'claude' else 'codex'}",
            f'session: "{key}"', f"turns: {info.get('turns')}",
            "---", "", f"# {title}", "", text, "",
            "---", f"原文：`{path}`",
        ])
        write_text_atomic(out, redact_text(doc, cfg.redact))
        print(f"  {nchars} 字符 -> in={usage.get('input_tokens')} "
              f"out={usage.get('output_tokens')} -> "
              f"{redact_text(out, cfg.redact)}")
    return 0


def cmd_nightly(cfg: Config, dry: bool) -> int:
    if not dry and not (cfg.memory_dir or cfg.obsidian_dir):
        print("错误：--nightly 需要至少一个输出目录：--memory-dir 或 --obsidian-dir",
              file=sys.stderr)
        return 2
    if dry:
        return _run_nightly(cfg, dry)
    lock = lock_or_report(cfg)
    if lock is None:
        return 1
    try:
        return _run_nightly(cfg, dry)
    finally:
        lock.release()


def _run_nightly(cfg: Config, dry: bool) -> int:
    if not dry:
        migrate_metadata(cfg)
    state = load_state(cfg)
    failures = load_failures(cfg)
    retry = [k for k, v in failures.items() if int(v.get("attempts", 0)) < 3]

    tasks, notes = plan_nightly(cfg, state)

    if retry:
        print(f"失败队列中有 {len(retry)} 项待重试")
        planned = {t.key for t in tasks}
        for key in retry:
            if key in planned:
                continue
            f = failures[key]
            res = resolve_meta(cfg, f, "path")
            if not res.ok():
                print(f"  跳过 {str(key)[:8]}：源文件无法定位（{res.status}）",
                      file=sys.stderr)
                continue
            p = res.path
            kind = f.get("kind") or "claude"
            info = parse_session(p, kind)
            if info["turns"] or info["prompts"]:
                tasks.insert(0, Task(info=info, label=project_label(info, None, cfg.projects),
                                     kind=kind, path=p, key=key, mode="full",
                                     new_offset=os.path.getsize(p)))

    if not tasks:
        print("没有需要提炼的新会话。")
        for n in notes:
            print(f"  · {n}")
        return 0

    print_plan(tasks, notes)
    if dry:
        return 0

    done = 0
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {redact_text(task.label, cfg.redact)} | "
              f"{task.info.get('start')} | {task.info.get('turns')} 条消息")
        if execute_task(cfg, task, state):
            done += 1

    print(f"完成 {done} 项")
    return 0 if done == len(tasks) else 1


def cmd_retry_failed(cfg: Config, dry: bool) -> int:
    failures = load_failures(cfg)
    if not failures:
        print("失败队列为空。")
        return 0
    print(f"失败队列：{len(failures)} 项")
    if dry:
        for key, v in failures.items():
            print(f"  {key[:8]} · 尝试 {v.get('attempts', 0)} 次 · {v.get('error', '')[:80]}")
        return 0
    lock = lock_or_report(cfg)
    if lock is None:
        return 1
    try:
        return _run_retry_failed(cfg, failures)
    finally:
        lock.release()


def _run_retry_failed(cfg: Config, failures: dict) -> int:
    migrate_metadata(cfg)
    failures = load_failures(cfg)
    state = load_state(cfg)
    done = 0
    for key, v in list(failures.items()):
        res = resolve_meta(cfg, v, "path")
        if not res.ok():
            print(f"  跳过 {key[:8]}：源文件无法定位（{res.status}）")
            continue
        path = res.path
        kind = v.get("kind") or "claude"
        info = parse_session(path, kind)
        if not info["turns"] and not info["prompts"]:
            continue
        task = Task(info=info, label=project_label(info, None, cfg.projects),
                    kind=kind, path=path, key=key, mode="full",
                    new_offset=os.path.getsize(path))
        print(f"[{key[:8]}] {redact_text(task.label, cfg.redact)} | "
              f"{info.get('start')}")
        if execute_task(cfg, task, state):
            done += 1
    print(f"完成 {done} 项")
    return 0 if done == len(failures) else 1


def summary_result(cfg: Config, rec: dict) -> Tuple[str, PathResolution]:
    """读回摘要正文，同时返回明确状态，避免“空字符串”混淆缺失与空文件。"""
    res = resolve_meta(cfg, rec, "file")
    if not res.ok():
        return "", res
    path = res.path
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except Exception:
        return "", PathResolution(path=path, status="unreadable",
                                  detail="read-failed")
    monthly_root = os.path.join(cfg.memory_dir, "sessions") if cfg.memory_dir else ""
    is_monthly = bool(monthly_root) and _is_within(path, monthly_root)
    if is_monthly:
        text = block_text(raw, rec.get("sid", ""))
        if text is None:
            return "", PathResolution(path=path, status="missing_block",
                                      detail="session-block-not-found")
    else:
        text = strip_frontmatter(raw)
    # 顺手去掉正文开头的一级标题，避免和输出里的“## 日期 · 项目”重复
    text = re.sub(r"^\s*# [^\n]*\n+", "", text or "", count=1)
    text = redact_text(text, cfg.redact)
    if not text.strip():
        return text, PathResolution(path=path, status="empty",
                                    recovered=res.recovered)
    return text, PathResolution(path=path, status=res.status,
                                detail=res.detail, recovered=res.recovered)


def summary_text(cfg: Config, rec: dict) -> str:
    """兼容调用：只要正文，状态由 summary_result 提供。"""
    return summary_result(cfg, rec)[0]


def migration_status_counts(cfg: Config) -> Counter:
    """只读统计旧 ref 数量，供 --find / --context 提示。"""
    counts: Counter = Counter()
    state = load_state(cfg)
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        st = ref_migration_status(cfg, entry.get("source_ref"),
                                  entry.get("source"))
        if st not in ("ok", "none"):
            counts[st] += 1
    index = load_index(cfg)
    for rec in (index.get("sessions") or {}).values():
        if not isinstance(rec, dict):
            continue
        for key in ("file", "source"):
            st = ref_migration_status(cfg, rec.get(key + "_ref"),
                                      rec.get(key))
            if st not in ("ok", "none"):
                counts[st] += 1
    for entry in load_failures(cfg).values():
        if not isinstance(entry, dict):
            continue
        st = ref_migration_status(cfg, entry.get("path_ref"),
                                  entry.get("path"))
        if st not in ("ok", "none"):
            counts[st] += 1
    return counts


def legacy_ref_warning(cfg: Config) -> None:
    """只读命令用：只提示，不迁移、不创建密钥、不写任何文件。"""
    counts = migration_status_counts(cfg)
    total = sum(counts.values())
    if not total:
        return
    print(f"提示：检测到 {total} 条旧路径引用尚未迁移。"
          f"运行 --migrate --dry-run 查看计划。", file=sys.stderr)


# 中文用相邻二字切词（不依赖分词库），英文/数字按词切
_CJK_CLASS = "[㐀-鿿]"
_WORD_RE = re.compile(r"[a-z0-9_][a-z0-9_./-]*")
_CJK_RUN_RE = re.compile(f"{_CJK_CLASS}+")


def text_tokens(text: str) -> set:
    text = (text or "").lower()
    toks = {t for t in _WORD_RE.findall(text) if len(t) >= 2}
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            toks.add(run)
        else:
            toks.update(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def _frontmatter_value(raw: str, key: str) -> str:
    m = re.search(rf"^{key}: *(.*)$", raw, re.M)
    v = (m.group(1) or "").strip() if m else ""
    return v.strip('"').strip("'")


def _docs_from_vault(cfg: Config) -> List[dict]:
    """索引缺失时，直接从 Obsidian 目录恢复候选列表。"""
    docs = []
    if not cfg.obsidian_dir or not os.path.isdir(cfg.obsidian_dir):
        return docs
    for path in glob.glob(os.path.join(cfg.obsidian_dir, "*", "*.md")):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except Exception:
            continue
        # 只索引本工具生成的摘要，避免把 vault 里的普通笔记拉进 --find。
        if not _frontmatter_value(raw, "session"):
            continue
        tags = _frontmatter_value(raw, "tags")
        if "自动摘要" not in tags and "关键标识符" not in raw:
            continue
        docs.append({
            "sid": _frontmatter_value(raw, "session"),
            "date": _frontmatter_value(raw, "date"),
            "project": redact_text(_frontmatter_value(raw, "project"), cfg.redact),
            "title": redact_text(_frontmatter_value(raw, "title"), cfg.redact),
            "file": path,
        })
    return docs


def search_summaries(cfg: Config, query: str, top: int,
                     project: Optional[str],
                     unresolved: Optional[List[dict]] = None) -> List[dict]:
    """按关键词相关度排序，返回 top 条带 score 的记录。"""
    q = text_tokens(query)
    if not q:
        return []
    index = load_index(cfg)
    recs = list(index["sessions"].values())
    if not recs:
        recs = _docs_from_vault(cfg)
    if project:
        recs = [r for r in recs if project.lower() in str(r.get("project", "")).lower()]

    docs = []
    for rec in recs:
        text, status = summary_result(cfg, rec)
        if not text:
            if unresolved is not None:
                unresolved.append({
                    "sid": rec.get("sid"),
                    "status": status.status,
                })
            continue
        head = f"{rec.get('title', '')} {rec.get('project', '')}"
        docs.append({"rec": rec, "text": text,
                     "head_tokens": text_tokens(head),
                     "body_tokens": text_tokens(text)})
    if not docs:
        return []

    n = len(docs)
    idf = {}
    for t in q:
        df = sum(1 for d in docs if t in d["head_tokens"] or t in d["body_tokens"])
        idf[t] = 1.0 + (n / df if df else 0) ** 0.5

    scored = []
    for d in docs:
        score, hits = 0.0, []
        for t in q:
            if t in d["head_tokens"]:
                score += 3.0 * idf[t]
                hits.append(t)
            elif t in d["body_tokens"]:
                score += idf[t]
                hits.append(t)
        if score > 0:
            scored.append({"rec": d["rec"], "text": d["text"],
                           "score": score, "hits": hits})
    scored.sort(key=lambda x: (x["score"], x["rec"].get("date") or ""), reverse=True)
    return scored[:top]


def _display(cfg: Config, value) -> str:
    return redact_text("" if value is None else str(value), cfg.redact)


def cmd_find(cfg: Config, query: str, top: int, project: Optional[str]) -> int:
    if not text_tokens(query):
        print("查询词太短：至少给一个中文词或两个以上字母/数字。", file=sys.stderr)
        return 2
    legacy_ref_warning(cfg)
    unresolved: List[dict] = []
    results = search_summaries(cfg, query, top, project, unresolved)
    if not results:
        print(f"没有找到与「{_display(cfg, query)}」相关的摘要。", file=sys.stderr)
        print("提示：这是关键词匹配，不是语义检索；换个更具体的词试试，"
              "或者先跑 --nightly 生成摘要。", file=sys.stderr)
    else:
        print(f"# 与「{_display(cfg, query)}」相关的 {len(results)} 份摘要\n")
        for r in results:
            rec = r["rec"]
            print(f"## {_display(cfg, str(rec.get('date', ''))[:10])} · "
                  f"{_display(cfg, rec.get('project'))} · "
                  f"{_display(cfg, rec.get('title'))}  （相关度 {r['score']:.1f}）")
            print()
            print(r["text"].strip())
            print("\n---")
    if unresolved:
        print(f"另有 {len(unresolved)} 份摘要无法定位，未参与匹配：", file=sys.stderr)
        for item in unresolved[:10]:
            print(f"  · {str(item.get('sid') or '?')[:8]}：{item['status']}",
                  file=sys.stderr)
        if len(unresolved) > 10:
            print(f"  · 其余 {len(unresolved) - 10} 份略", file=sys.stderr)
        print("可运行 --migrate 或 --repair-ref 修复引用。", file=sys.stderr)
        return 1
    return 0 if results else 1


def cmd_context(cfg: Config, n: int, project: Optional[str]) -> int:
    legacy_ref_warning(cfg)
    index = load_index(cfg)
    items = list(index["sessions"].values())
    if project:
        items = [v for v in items if project.lower() in str(v.get("project", "")).lower()]
    items.sort(key=lambda v: v.get("date") or "", reverse=True)
    items = items[:n]
    if not items:
        print("索引里没有匹配的会话。先跑 --nightly 或 --export。", file=sys.stderr)
        return 1

    failed = False
    for v in items:
        text, res = summary_result(cfg, v)
        print(f"\n## {_display(cfg, str(v.get('date', ''))[:10])} · "
              f"{_display(cfg, v.get('project'))} · {_display(cfg, v.get('title'))}")
        print()
        if not res.ok():
            failed = True
            print(f"（无法定位摘要文件：{res.status}）")
        elif not text.strip():
            failed = True
            print(f"（摘要文件为空：{res.status}）")
        else:
            print(text.strip())
        print("\n---")
    return 1 if failed else 0


def cmd_export(cfg: Config) -> int:
    lock = lock_or_report(cfg)
    if lock is None:
        return 1
    try:
        return _run_export(cfg)
    finally:
        lock.release()


def _run_export(cfg: Config) -> int:
    migrate_metadata(cfg)
    idx = {"claude": OrderedDict(), "codex": OrderedDict()}
    total = 0
    index = load_index(cfg)

    for mt, f, kind, proj_dir in collect_files(cfg):
        info = parse_session(f, kind)
        if not info["turns"] and not info["prompts"]:
            continue
        label = redact_text(
            project_label(info, proj_dir, cfg.projects), cfg.redact)
        sid = session_key(info, f)
        title = redact_text(title_of(info), cfg.redact)
        fname = f"{sid[:8]}_{_safe_name(title)}.md"
        if kind == "claude":
            out = os.path.join(cfg.out_dir, "Claude Code", _safe_name(label, 80), fname)
        else:
            out = os.path.join(cfg.out_dir, "Codex", fname)
        write_plain_session(out, info, label, cfg.redact)
        idx[kind].setdefault(label, []).append((fname, info, out))
        total += 1
        index["sessions"][sid] = {
            "date": info.get("start"), "project": label, "platform": kind,
            "title": title, "sid": sid,
            "file": redact_text(out, cfg.redact),
            "source": redact_text(f, cfg.redact),
            "file_ref": make_path_ref(cfg, out, "out"),
            "source_ref": make_path_ref(cfg, f, kind),
            "mode": "export",
            "updated": datetime.now().isoformat(timespec="seconds"),
        }

    lines = ["---", "title: 会话记忆库", "tags: [index]", "---", "",
             "# 会话记忆库", "",
             "> 由 SessionDigest 根据本地会话记录自动生成。每个文件是一段对话的结构化摘录。", ""]
    for kind, groups in idx.items():
        name = "Claude Code" if kind == "claude" else "Codex"
        if not groups:
            continue
        lines.append(f"## {name}")
        for label, items in groups.items():
            lines.append(f"### {label}")
            for fname, info, _out in items:
                d = (info.get("start") or "")[:10]
                lines.append(f"- [[{os.path.splitext(fname)[0]}]] — {d} · "
                             f"{redact_text(title_of(info), cfg.redact)}")
            lines.append("")

    os.makedirs(cfg.out_dir, exist_ok=True)
    write_text_atomic(os.path.join(cfg.out_dir, "README.md"),
                      redact_text("\n".join(lines), cfg.redact))
    save_index(cfg, index)
    print(f"导出完成：{total} 场会话 -> {redact_text(cfg.out_dir, cfg.redact)}")
    return 0


def write_plain_session(path: str, info: dict, label: str,
                        redact_mode: str = "strict") -> None:
    title = title_of(info)
    md = ["---",
          f'title: "{title.replace(chr(34), chr(39))}"',
          f'date: {(info.get("start") or "").split(" ")[0] or "未知"}',
          f"project: {label}",
          f"platform: {info['kind']}",
          f'session: "{info.get("session") or ""}"',
          f'turns: {info.get("turns")}',
          f'tools: "{",".join(sorted(info["tools"]))}"',
          "---", "", f"# {title}", ""]
    md.append(f"- **日期**: {info.get('start')}  ~  {info.get('end')}")
    md.append(f"- **项目**: {label}")
    md.append(f"- **平台**: {'Claude Code' if info['kind'] == 'claude' else 'Codex'}")
    if info.get("cwd"):
        md.append(f"- **工作目录**: `{info['cwd']}`")
    if info.get("branch"):
        md.append(f"- **分支**: `{info['branch']}`")
    md.append(f"- **消息数**: {info.get('turns')}")
    if info["tools"]:
        md.append(f"- **工具**: {', '.join(f'{k}×{v}' for k, v in info['tools'].most_common(10))}")
    if info["files"]:
        md.append("- **涉及文件**:")
        md += [f"  - `{f}`" for f in list(info["files"].keys())[:15]]
    if info["prompts"]:
        md += ["", "## 用户提问"]
        md += [f"{i}. {trunc(p, 500)}" for i, p in enumerate(info["prompts"], 1)]
    if info["assistant"]:
        md += ["", "## 关键回答"]
        md += [f"- {trunc(a, 400)}" for a in info["assistant"][:8]]
    if info.get("error"):
        md += ["", f"> 解析错误: {info['error']}"]

    write_text_atomic(path, redact_text("\n".join(md), redact_mode))


# ---------------------------------------------------------------------------
# ref 迁移与根目录绑定命令
# ---------------------------------------------------------------------------

def migration_plan(cfg: Config) -> dict:
    """只读计划：不创建密钥、不写 roots.json、不取锁。"""
    plan = {
        "state": Counter(), "index": Counter(), "failures": Counter(),
        "key_exists": os.path.exists(_ref_key_path(cfg)),
    }
    for entry in load_state(cfg).values():
        if isinstance(entry, dict):
            plan["state"][ref_migration_status(
                cfg, entry.get("source_ref"), entry.get("source"))] += 1
    for rec in (load_index(cfg).get("sessions") or {}).values():
        if not isinstance(rec, dict):
            continue
        for key in ("file", "source"):
            plan["index"][ref_migration_status(
                cfg, rec.get(key + "_ref"), rec.get(key))] += 1
    for entry in load_failures(cfg).values():
        if isinstance(entry, dict):
            plan["failures"][ref_migration_status(
                cfg, entry.get("path_ref"), entry.get("path"))] += 1
    return plan


def _print_plan_section(name: str, counts: Counter) -> int:
    if not counts:
        print(f"  {name}: 无记录")
        return 0
    order = ("ok", "v1", "legacy_raw", "legacy_redacted", "unconfigured",
             "legacy_outside_root", "key_missing", "invalid", "none")
    parts = [f"{k}={counts[k]}" for k in order if counts.get(k)]
    print(f"  {name}: " + "  ".join(parts))
    return sum(v for k, v in counts.items()
               if k not in ("ok", "none"))


def cmd_migrate(cfg: Config, dry: bool) -> int:
    if dry:
        plan = migration_plan(cfg)
        print("迁移计划（dry-run，不写任何文件）：")
        pending = 0
        for name in ("state", "index", "failures"):
            pending += _print_plan_section(name, plan[name])
        if not plan["key_exists"]:
            print("  将创建本地密钥 digest.key（仅用于 HMAC 标识）")
        print(f"  待处理 ref：{pending}")
        return 0

    lock = lock_or_report(cfg)
    if lock is None:
        return 1
    try:
        had_key = os.path.exists(_ref_key_path(cfg))
        report: dict = {}
        changed = migrate_metadata(cfg, report)
        plan = migration_plan(cfg)
        if not had_key:
            print("已创建本地密钥 digest.key（仅用于 HMAC 标识）。")
        print("迁移完成。" if changed else "没有需要迁移的 ref。")
        unresolved = 0
        for name in ("state", "index", "failures"):
            counts = plan[name]
            unresolved += sum(v for k, v in counts.items()
                              if k not in ("ok", "none"))
            _print_plan_section(name, counts)
        if unresolved:
            # 迁移前记录的 legacy_outside_root 在迁移后会被脱敏成
            # legacy_redacted，不能从 plan 反推，必须看 report。
            outside = sum(report.get(name, Counter()).get(
                "legacy_outside_root", 0)
                for name in ("state", "index", "failures"))
            if outside:
                print(f"有 {outside} 条旧路径不在任何已知根目录内，"
                      "无法生成 v2 ref；可把文件移到已知根下，"
                      "或继续依赖旧的明文路径回退。", file=sys.stderr)
            remaining = unresolved - outside
            if remaining > 0:
                print(f"另有 {remaining} 条 ref 无法自动迁移。"
                      "运行 --migrate --dry-run 查看，"
                      "或用 --repair-ref 指定实际文件。", file=sys.stderr)
            return 1
        return 0
    finally:
        lock.release()


def _collect_ref_roots(cfg: Config) -> Dict[str, Counter]:
    """统计 state/index/failures 里 v2 ref 的 root_id，不读路径。"""
    out: Dict[str, Counter] = {r: Counter() for r in _REF_ROOTS}

    def add(ref):
        if isinstance(ref, dict) and ref.get("v") == _REF_VERSION:
            kind = ref.get("root")
            rid = ref.get("root_id")
            if kind in out and isinstance(rid, str):
                out[kind][rid] += 1

    for entry in load_state(cfg).values():
        if isinstance(entry, dict):
            add(entry.get("source_ref"))
    for rec in (load_index(cfg).get("sessions") or {}).values():
        if isinstance(rec, dict):
            add(rec.get("file_ref"))
            add(rec.get("source_ref"))
    for entry in load_failures(cfg).values():
        if isinstance(entry, dict):
            add(entry.get("path_ref"))
    return out


def cmd_roots(cfg: Config) -> int:
    key = _load_ref_key(cfg, create=False)
    refs = _collect_ref_roots(cfg)
    bindings = load_root_bindings(cfg)
    any_ref = any(refs.values())
    if not any_ref:
        legacy = migration_status_counts(cfg)
        if sum(legacy.values()):
            print("只有旧格式引用，还没有 root_id。先运行 --migrate。")
        else:
            print("还没有 v2 路径引用。先在写命令或 --migrate 中生成。")
        return 0
    if not key:
        print("本地密钥不存在，无法计算当前根目录的 root_id。"
              "先运行 --migrate。", file=sys.stderr)
    print("根目录绑定（不显示实际路径）：")
    for kind in _REF_ROOTS:
        counts = refs[kind]
        root = _root_dir(cfg, kind)
        current = _root_id_for_path(key, root) if (key and root) else None
        if not counts and not current:
            continue
        print(f"\n{kind}")
        if current:
            print(f"  当前：{current}")
        else:
            print("  当前：未配置或缺少密钥")
        if not counts:
            print("  历史：无")
            continue
        for rid, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            if current and rid == current:
                state = "bound"
            elif current and _follow_binding(bindings, rid, current):
                state = "bound"
            else:
                state = "unbound"
            print(f"  历史：{rid}  refs={n}  {state}")
    return 0


def cmd_rebind_root(cfg: Config, old_id: str, kind: Optional[str],
                    dry: bool, force: bool) -> int:
    if not re.match(r"^r2_[0-9a-f]{32}$", old_id or ""):
        print("root_id 格式不对，应形如 r2_<32 位十六进制>。", file=sys.stderr)
        return 2
    refs = _collect_ref_roots(cfg)
    if not kind:
        kinds = [k for k, counts in refs.items() if counts.get(old_id)]
        if len(kinds) != 1:
            print("无法确定 root_id 属于哪个根目录；请加 --root-kind "
                  "obsidian|memory|out|claude|codex。", file=sys.stderr)
            return 2
        kind = kinds[0]
    if kind not in _REF_ROOTS:
        print("未知的根目录类型。", file=sys.stderr)
        return 2
    root = _root_dir(cfg, kind)
    if not root:
        print(f"当前没有配置 {kind} 根目录。", file=sys.stderr)
        return 2
    key = _load_ref_key(cfg, create=False)
    if not key and not dry:
        key = ensure_ref_key(cfg)
    if not key:
        print("dry-run：本地密钥尚不存在，实际执行时会先创建 digest.key。")
        return 0
    current = _root_id_for_path(key, root)
    if current == old_id:
        print("当前根目录已经就是该 root_id，无需绑定。")
        return 0
    bindings = load_root_bindings(cfg)
    existing = bindings.get(old_id)
    if isinstance(existing, dict):
        existing = existing.get("to")
    if existing and existing != current:
        # 根目录可能又移动了一次：显式重绑覆盖旧目标即可，历史 ref 仍不变
        print(f"更新 {old_id} 的绑定：{existing} -> {current}")
    if _follow_binding(bindings, current, old_id):
        print("这次绑定会形成环，已拒绝。", file=sys.stderr)
        return 1
    print(f"将把 {old_id} 绑定到当前 {kind} 根目录 {current}（历史 ref 不变）。")
    if dry:
        return 0
    lock = lock_or_report(cfg)
    if lock is None:
        return 1
    try:
        bindings[old_id] = current
        save_root_bindings(cfg, bindings)
        print("已保存 roots.json。")
        return 0
    finally:
        lock.release()


def cmd_repair_ref(cfg: Config, sid: str, source: Optional[str],
                   filepath: Optional[str], dry: bool) -> int:
    """显式修复一条记录：只写 v2 ref，不保存用户给的绝对路径。"""
    if not source and not filepath:
        print("请至少给 --source 或 --file。", file=sys.stderr)
        return 2
    for label, path in (("source", source), ("file", filepath)):
        if path and not os.path.exists(path):
            print(f"{label} 路径不存在（已脱敏显示）："
                  f"{redact_text(path, cfg.redact)}", file=sys.stderr)
            return 2
    key = _load_ref_key(cfg, create=False)
    if not key and not dry:
        key = ensure_ref_key(cfg)
    if not key:
        if not dry:
            print("需要本地密钥；先运行 --migrate。", file=sys.stderr)
            return 1
        # dry-run 用一次性占位密钥生成计划，不落盘
        key = b"\0" * 32

    def build(path, preferred):
        return make_path_ref(cfg, path, preferred, sid=sid, key=key)

    src_ref = build(source, "claude") if source else None
    file_ref = build(filepath, "obsidian") if filepath else None
    if source and not src_ref:
        print("source 不在已知根目录内，无法生成 ref。", file=sys.stderr)
        return 2
    if filepath and not file_ref:
        print("file 不在已知根目录内，无法生成 ref。", file=sys.stderr)
        return 2

    state = load_state(cfg)
    index = load_index(cfg)
    failures = load_failures(cfg)
    changed_state = changed_index = changed_failures = False
    if src_ref and isinstance(state.get(sid), dict):
        state[sid]["source_ref"] = src_ref
        changed_state = True
    if isinstance((index.get("sessions") or {}).get(sid), dict):
        rec = index["sessions"][sid]
        if file_ref:
            rec["file_ref"] = file_ref
            changed_index = True
        if src_ref:
            rec["source_ref"] = src_ref
            changed_index = True
    if src_ref and isinstance(failures.get(sid), dict):
        failures[sid]["path_ref"] = src_ref
        changed_failures = True

    if not (changed_state or changed_index or changed_failures):
        print(f"没有找到 {sid[:8]} 对应的记录。", file=sys.stderr)
        return 1
    print(f"将修复 {sid[:8]}："
          f"state={'yes' if changed_state else 'no'} "
          f"index={'yes' if changed_index else 'no'} "
          f"failures={'yes' if changed_failures else 'no'}")
    if dry:
        return 0
    lock = lock_or_report(cfg)
    if lock is None:
        return 1
    try:
        if changed_state:
            save_state(cfg, state)
        if changed_index:
            save_index(cfg, index)
        if changed_failures:
            save_failures(cfg, failures)
        print("已修复。")
        return 0
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_config(args) -> Config:
    base_url, api_key, model = resolve_llm(args)
    state_file = args.state_file or os.path.expanduser(
        "~/.claude-session-digest/state.json")
    state_file = _expand(state_file) or state_file
    sdir = os.path.dirname(state_file) or "."
    return Config(
        claude_root=_expand(args.claude_root) or os.path.expanduser("~/.claude/projects"),
        codex_root=_expand(args.codex_root) or os.path.expanduser("~/.codex/sessions"),
        out_dir=_expand(args.out) or "digest-output",
        obsidian_dir=_expand(args.obsidian_dir),
        memory_dir=_expand(args.memory_dir),
        state_file=state_file,
        index_file=_expand(args.index_file) or os.path.join(sdir, "index.json"),
        failures_file=_expand(args.failures_file) or os.path.join(sdir, "failures.json"),
        lock_file=_expand(args.lock_file) or state_file + ".lock",
        projects=load_projects(args.projects_file),
        ref_key_file=_expand(getattr(args, "ref_key_file", None)),
        roots_file=_expand(getattr(args, "roots_file", None)),
        base_url=base_url,
        api_key=api_key,
        model=model,
        api_format=args.api_format,
        min_turns=args.min_turns,
        min_age_hours=args.min_age_hours,
        max_sessions=args.max_sessions,
        merge_after=args.merge_after,
        min_delta_turns=args.min_delta_turns,
        min_delta_chars=args.min_delta_chars,
        force=args.force,
        verify=not args.no_verify,
        redact=args.redact,
        verbose=args.verbose,
    )


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="session-digest",
        description="把 Claude Code / Codex 会话蒸馏成结构化 Markdown 摘要。",
        epilog="示例：\n"
               "  python session_digest.py --export\n"
               "  python session_digest.py --llm 3 --dry-run\n"
               "  python session_digest.py --nightly --obsidian-dir ~/vault/摘要\n"
               "  python session_digest.py --context 5 --project shop\n"
               "  python session_digest.py --find \"库存 架构\"\n")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--export", action="store_true", help="全量导出，不调用模型")
    mode.add_argument("--llm", type=int, metavar="N", help="预览：对最近 N 场会话生成摘要")
    mode.add_argument("--nightly", action="store_true", help="增量提炼新会话")
    mode.add_argument("--context", type=int, metavar="N", help="输出最近 N 份摘要的拼接文本")
    mode.add_argument("--find", metavar="QUERY", help="按关键词查找相关摘要（本地匹配）")
    mode.add_argument("--retry-failed", action="store_true", help="只重试失败队列")
    mode.add_argument("--migrate", action="store_true",
                      help="把旧路径引用迁移到 v2（配合 --dry-run 只查看计划）")
    mode.add_argument("--roots", action="store_true",
                      help="列出根目录 root_id 绑定情况（不显示实际路径）")
    mode.add_argument("--rebind-root", metavar="ROOT_ID",
                      help="把历史 root_id 绑定到当前根目录")
    mode.add_argument("--repair-ref", metavar="SID",
                      help="用 --source / --file 显式修复一条记录")

    ap.add_argument("--root-kind", choices=list(_REF_ROOTS), default=None,
                    help="--rebind-root 时指定根目录类型")
    ap.add_argument("--source", default=None,
                    help="--repair-ref 的源会话文件路径")
    ap.add_argument("--file", dest="file_path", default=None,
                    help="--repair-ref 的摘要文件路径")
    ap.add_argument("--ref-key-file", default=None,
                    help="HMAC 密钥文件（默认与 state 同目录的 digest.key）")
    ap.add_argument("--roots-file", default=None,
                    help="root_id 绑定文件（默认与 state 同目录的 roots.json）")

    ap.add_argument("--top", type=int, default=3, help="--find 返回几条（默认 3）")
    ap.add_argument("--project", default=None, help="--context / --find 时按项目名过滤")
    ap.add_argument("--claude-root", default=None,
                    help="Claude Code 会话目录（默认 ~/.claude/projects）")
    ap.add_argument("--codex-root", default=None,
                    help="Codex 会话目录（默认 ~/.codex/sessions）")
    ap.add_argument("--out", default=None, help="导出 / 预览输出目录（默认 digest-output）")
    ap.add_argument("--obsidian-dir", default=None, help="Obsidian 摘要目录；给了才会写")
    ap.add_argument("--memory-dir", default=None, help="memory 目录；给了才会写索引")
    ap.add_argument("--state-file", default=None,
                    help="去重与偏移状态（默认 ~/.claude-session-digest/state.json）")
    ap.add_argument("--index-file", default=None, help="索引 JSON（默认与 state 同目录）")
    ap.add_argument("--failures-file", default=None, help="失败队列 JSON（默认与 state 同目录）")
    ap.add_argument("--lock-file", default=None,
                    help="进程锁文件（默认 state 文件后加 .lock）")
    ap.add_argument("--projects-file", default=None,
                    help="项目名映射 JSON，如 {\"my-repo\": \"我的项目\"}")

    ap.add_argument("--base-url", default=None, help="模型 API 地址")
    ap.add_argument("--api-key", default=None, help="模型 API key（也可用环境变量）")
    ap.add_argument("--model", default=None,
                    help="模型名（Anthropic 默认 claude-sonnet-5-5；OpenAI 兼容模式必须指定）")
    ap.add_argument("--api-format", choices=["anthropic", "openai"], default="anthropic",
                    help="接口格式：anthropic（/v1/messages）或 openai（/chat/completions）")

    ap.add_argument("--min-turns", type=int, default=8,
                    help="新会话少于 N 条消息时跳过（默认 8）")
    ap.add_argument("--min-age-hours", type=int, default=6,
                    help="结束不足 N 小时的会话跳过（默认 6）")
    ap.add_argument("--max-sessions", type=int, default=20,
                    help="单次最多处理 N 项（默认 20）")
    ap.add_argument("--merge-after", type=int, default=3,
                    help="续写达到 N 段后合并整理（默认 3）")
    ap.add_argument("--min-delta-turns", type=int, default=4,
                    help="新增少于 N 条消息且字数不足时跳过（默认 4）")
    ap.add_argument("--min-delta-chars", type=int, default=500,
                    help="新增少于 N 字符且消息不足时跳过（默认 500）")
    ap.add_argument("--force", action="store_true", help="忽略状态，全部重跑")
    ap.add_argument("--no-verify", action="store_true",
                    help="不做事实校验（不把标识符与原文比对、不加 ⚠ 标记）")
    ap.add_argument("--redact", choices=["off", "safe", "strict"], default="strict",
                    help="脱敏：off 原样；safe 遮凭据/邮箱/内网 IP；"
                         "strict 再把主目录用户名换成 ~（默认）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出计划和 token 估算，不调用模型、不写文件")
    ap.add_argument("--verbose", action="store_true", help="失败时打印完整堆栈")

    args = ap.parse_args(argv)
    if not (args.export or args.llm or args.nightly or args.context
            or args.find or args.retry_failed or args.migrate or args.roots
            or args.rebind_root or args.repair_ref):
        ap.print_help()
        return 0

    cfg = build_config(args)
    if args.roots:
        return cmd_roots(cfg)
    if args.migrate:
        return cmd_migrate(cfg, args.dry_run)
    if args.rebind_root:
        return cmd_rebind_root(cfg, args.rebind_root, args.root_kind,
                               args.dry_run, args.force)
    if args.repair_ref:
        return cmd_repair_ref(cfg, args.repair_ref, args.source,
                              args.file_path, args.dry_run)
    if args.export:
        return cmd_export(cfg)
    if args.llm:
        return cmd_preview(cfg, args.llm, args.dry_run)
    if args.context:
        return cmd_context(cfg, args.context, args.project)
    if args.find:
        return cmd_find(cfg, args.find, max(1, args.top), args.project)
    if args.retry_failed:
        return cmd_retry_failed(cfg, args.dry_run)
    return cmd_nightly(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
