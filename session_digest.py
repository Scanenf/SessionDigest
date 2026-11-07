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
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from dataclasses import dataclass
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
        print(f"警告：项目映射读取失败（{path}）：{e}", file=sys.stderr)
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
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def lock_or_report(cfg: Config) -> Optional[ProcessLock]:
    """拿写锁；被占用时打印提示并返回 None。"""
    lock = ProcessLock(cfg.lock_file)
    if lock.acquire():
        return lock
    print(f"另一个 SessionDigest 进程正在运行（锁：{cfg.lock_file}）。\n"
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
            "files": OrderedDict(), "commands": [], "tool_results": [], "modified": [],
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
        if isinstance(args, dict):
            cmd = args.get("command") or args.get("cmd")
            if cmd and len(info["commands"]) < 60:
                info["commands"].append(trunc(cmd, 300))
            fp = args.get("file_path") or args.get("pattern")
            if fp:
                info["files"].setdefault(str(fp), None)


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

def _norm_match(s: str) -> str:
    s = str(s).replace("`", "").strip().lower()
    s = s.replace("\\", "/")
    return re.sub(r"\s+", " ", s)


def _source_text(info: dict) -> str:
    parts = []
    parts += info.get("prompts", [])
    parts += info.get("assistant", [])
    parts += info.get("commands", [])
    parts += info.get("tool_results", [])
    parts += list(info.get("files", {}).keys())
    parts += info.get("modified", [])
    return _norm_match("\n".join(str(p) for p in parts))


def verify_summary(text: str, info: dict, extra_source: str = "") -> str:
    """把摘要里反引号包裹的标识符与原文比对，未命中的标 ⚠。"""
    source = _source_text(info)
    if extra_source:
        source += "\n" + _norm_match(extra_source)
    if not source:
        return text

    out = []
    in_identifiers = False
    for line in text.splitlines():
        h = line.strip()
        if h.startswith("##"):
            in_identifiers = h.endswith("关键标识符")
        if in_identifiers and re.match(r"^-\s*[^：:]+[：:]", line):
            line = _mark_unverified(line, source)
        out.append(line)
    return "\n".join(out)


def _mark_unverified(line: str, source: str) -> str:
    def repl(m):
        frag = m.group(1)
        if len(frag.strip()) < 4:
            return m.group(0)
        needle = _norm_match(frag)
        if len(needle) > 60:
            needle = needle[:60]
        if needle and needle in source:
            return m.group(0)
        return f"`{frag}` ⚠"

    return re.sub(r"`([^`]+)`", repl, line)


def checked_summary(cfg: "Config", text: str, info: dict,
                    extra_source: str = "") -> str:
    """按配置决定是否做事实校验。"""
    return verify_summary(text, info, extra_source) if cfg.verify else text


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
2. 「关键标识符」必须逐字抄录，不得改写、概括或翻译；但只收关键项，宁可少列，不要罗列。每一项没有就写「无」。
3. 不要编造原文没有的事实。

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

要求：不要重复旧摘要里已有的内容；不要编造原文没有的事实；标识符逐字抄录。
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

        text, usage, truncated = _parse_response(fmt, d)
        if text:
            return text, usage
        if truncated and max_tokens < 32000:
            max_tokens *= 2
            continue
        raise RuntimeError(f"模型未返回文本（{fmt}）：{raw[:200]}")
    raise RuntimeError(f"请求失败：{last_err}")


def summarize_one(cfg: Config, info: dict, label: str):
    transcript = redact_text(build_transcript(info), cfg.redact)
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


def find_monthly_block(text: str, sid: str) -> Optional[Tuple[int, int]]:
    """定位 `session: \\`sid\\`` 所在的块，返回 [start, end)。"""
    marker = f"session: `{sid}`"
    i = text.find(marker)
    if i < 0:
        return None
    start = text.rfind("\n## ", 0, i)
    start = 0 if start < 0 else start + 1
    end = text.find("\n---", i)
    if end < 0:
        end = len(text)
    else:
        end += len("\n---")
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
    return text[:start] + body.rstrip() + "\n\n" + addition.strip() + "\n\n---" + text[end:]


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


def append_continuation(cfg: Config, info: dict, label: str, text: str,
                        sid: str, prev_summary: str) -> Optional[str]:
    path = obsidian_path(cfg, info, sid)
    if path:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cur = fh.read()
            write_text_atomic(path, redact_text(
                cur.rstrip("\n") + "\n\n" + text.strip() + "\n", cfg.redact))
        else:
            # 文件丢失时，用旧摘要 + 续段重建
            write_text_atomic(path, redact_text(obsidian_doc(
                info, label, prev_summary.rstrip() + "\n\n" + text, sid),
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
        if find_monthly_block(cur, sid):
            cur = insert_into_block(cur, sid, text)
        else:
            cur = replace_block(
                cur, sid,
                monthly_block(month, info, label,
                              prev_summary.rstrip() + "\n\n" + text, sid))
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
                return strip_frontmatter(raw).strip()
    if cfg.memory_dir:
        for p in glob.glob(os.path.join(cfg.memory_dir, "sessions", "*.md")):
            try:
                with open(p, encoding="utf-8") as fh:
                    raw = fh.read()
            except Exception:
                continue
            t = block_text(raw, key)
            if t:
                return t
    return None


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
    index = load_index(cfg)
    path = obsidian_path(cfg, info, sid)
    if not path and cfg.memory_dir:
        month = first_date_of(info)[:7] or datetime.now().strftime("%Y-%m")
        path = os.path.join(cfg.memory_dir, "sessions", f"{month}.md")
    prev = index["sessions"].get(sid) or {}
    index["sessions"][sid] = {
        "date": (info.get("_first_date") or info.get("start")),
        "project": label,
        "platform": info.get("kind"),
        "title": redact_text(title_of(info), cfg.redact),
        "sid": sid,
        "file": path,
        "source": source,
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
    failures = load_failures(cfg)
    ent = failures.get(key) or {"attempts": 0}
    ent.update({
        "path": path,
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
        print(f"  [{mode}] [{t.label}] {t.info.get('start')} · "
              f"{t.info.get('turns')} 条消息 · {os.path.basename(t.path)}")
    for n in notes or []:
        print(f"  · {n}")


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _today(info: dict) -> str:
    return (info.get("end") or info.get("start") or datetime.now().strftime("%Y-%m-%d %H:%M"))[:16]


def execute_task(cfg: Config, task: Task, state: dict) -> bool:
    sid = task.key
    prev_entry = state.get(sid) or {}
    info = dict(task.info)
    info["_first_date"] = prev_entry.get("first_date") or info.get("start")
    info["_first_title"] = prev_entry.get("first_title") or title_of(info)

    try:
        if task.mode == "full":
            text, usage, nchars = summarize_one(cfg, info, task.label)
            text = checked_summary(cfg, text, info)
            write_full(cfg, info, task.label, text, sid)
            sections = 0
            print(f"  全量 {nchars} 字符 -> in={usage.get('input_tokens')} "
                  f"out={usage.get('output_tokens')}")

        elif task.mode == "incremental":
            delta = redact_text(build_transcript(info), cfg.redact)
            prompt = continuation_prompt(task.prev_summary or "", info, delta,
                                         _today(info))
            text, usage = call_llm(cfg, prompt)
            text = checked_summary(cfg, text, info, task.prev_summary or "")
            append_continuation(cfg, info, task.label, text, sid,
                                task.prev_summary or "")
            sections = task.sections + 1
            print(f"  续写 {len(delta)} 字符 -> in={usage.get('input_tokens')} "
                  f"out={usage.get('output_tokens')}（第 {sections} 段）")

        else:  # merge
            text, usage = call_llm(cfg, merge_prompt(task.prev_summary or ""))
            text = checked_summary(cfg, text, info)
            write_full(cfg, info, task.label, text, sid)
            sections = 0
            print(f"  合并 -> in={usage.get('input_tokens')} "
                  f"out={usage.get('output_tokens')}")

        if task.mode in ("full", "merge"):
            new_offset = os.path.getsize(task.path)
        else:
            new_offset = task.new_offset

        state[sid] = {
            "offset": new_offset,
            "prefix_sha1": prefix_sha1(task.path, new_offset),
            "sections": sections,
            "date": info.get("_first_date"),
            "first_date": info.get("_first_date"),
            "first_title": info.get("_first_title"),
            "source": task.path,
            "tokens": usage,
            "done_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(cfg, state)
        update_index(cfg, info, task.label, text, sid, task.path, task.mode)
        clear_failure(cfg, sid)
        return True

    except Exception as e:
        print("  失败:", e, file=sys.stderr)
        if cfg.verbose:
            import traceback
            traceback.print_exc()
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
            print(f"  [{label}] {info.get('start')} · {info.get('turns')} 条消息 · {path}")
        return 0

    os.makedirs(cfg.out_dir, exist_ok=True)
    for i, (info, label, kind, path, key) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {label} | {info.get('start')} | {info.get('turns')} 条消息")
        try:
            text, usage, nchars = summarize_one(cfg, info, label)
        except Exception as e:
            print("  失败:", e, file=sys.stderr)
            if cfg.verbose:
                import traceback
                traceback.print_exc()
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
              f"out={usage.get('output_tokens')} -> {out}")
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
            p = f.get("path")
            if not p or not os.path.exists(p):
                continue
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
        print(f"[{i}/{len(tasks)}] {task.label} | {task.info.get('start')} | "
              f"{task.info.get('turns')} 条消息")
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
    state = load_state(cfg)
    done = 0
    for key, v in list(failures.items()):
        path = v.get("path")
        if not path or not os.path.exists(path):
            print(f"  跳过 {key[:8]}：源文件不存在")
            continue
        kind = v.get("kind") or "claude"
        info = parse_session(path, kind)
        if not info["turns"] and not info["prompts"]:
            continue
        task = Task(info=info, label=project_label(info, None, cfg.projects),
                    kind=kind, path=path, key=key, mode="full",
                    new_offset=os.path.getsize(path))
        print(f"[{key[:8]}] {task.label} | {info.get('start')}")
        if execute_task(cfg, task, state):
            done += 1
    print(f"完成 {done} 项")
    return 0 if done == len(failures) else 1


def summary_text(cfg: Config, rec: dict) -> str:
    """从索引记录里读回一份摘要的正文（去掉 frontmatter）。"""
    path = rec.get("file")
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except Exception:
        return ""
    monthly_root = os.path.normpath(
        os.path.join(cfg.memory_dir, "sessions")) if cfg.memory_dir else ""
    is_monthly = monthly_root and os.path.normpath(path).startswith(monthly_root)
    text = block_text(raw, rec.get("sid", "")) if is_monthly \
        else strip_frontmatter(raw)
    # 顺手去掉正文开头的一级标题，避免和输出里的“## 日期 · 项目”重复
    return re.sub(r"^\s*# [^\n]*\n+", "", text, count=1)


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
        docs.append({
            "sid": _frontmatter_value(raw, "session"),
            "date": _frontmatter_value(raw, "date"),
            "project": _frontmatter_value(raw, "project"),
            "title": _frontmatter_value(raw, "title"),
            "file": path,
        })
    return docs


def search_summaries(cfg: Config, query: str, top: int,
                     project: Optional[str]) -> List[dict]:
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
        text = summary_text(cfg, rec)
        if not text:
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


def cmd_find(cfg: Config, query: str, top: int, project: Optional[str]) -> int:
    if not text_tokens(query):
        print("查询词太短：至少给一个中文词或两个以上字母/数字。", file=sys.stderr)
        return 2
    results = search_summaries(cfg, query, top, project)
    if not results:
        print(f"没有找到与「{query}」相关的摘要。", file=sys.stderr)
        print("提示：这是关键词匹配，不是语义检索；换个更具体的词试试，"
              "或者先跑 --nightly 生成摘要。", file=sys.stderr)
        return 1
    print(f"# 与「{query}」相关的 {len(results)} 份摘要\n")
    for r in results:
        rec = r["rec"]
        print(f"## {str(rec.get('date', ''))[:10]} · {rec.get('project', '')} · "
              f"{rec.get('title', '')}  （相关度 {r['score']:.1f}）")
        print()
        print(r["text"].strip())
        print("\n---")
    return 0


def cmd_context(cfg: Config, n: int, project: Optional[str]) -> int:
    index = load_index(cfg)
    items = list(index["sessions"].values())
    if project:
        items = [v for v in items if project.lower() in str(v.get("project", "")).lower()]
    items.sort(key=lambda v: v.get("date") or "", reverse=True)
    items = items[:n]
    if not items:
        print("索引里没有匹配的会话。先跑 --nightly 或 --export。", file=sys.stderr)
        return 1

    for v in items:
        text = summary_text(cfg, v)
        print(f"\n## {v.get('date', '')[:10]} · {v.get('project', '')} · {v.get('title', '')}")
        print()
        print(text.strip() or "（摘要文件缺失）")
        print("\n---")
    return 0


def cmd_export(cfg: Config) -> int:
    lock = lock_or_report(cfg)
    if lock is None:
        return 1
    try:
        return _run_export(cfg)
    finally:
        lock.release()


def _run_export(cfg: Config) -> int:
    idx = {"claude": OrderedDict(), "codex": OrderedDict()}
    total = 0
    index = load_index(cfg)

    for mt, f, kind, proj_dir in collect_files(cfg):
        info = parse_session(f, kind)
        if not info["turns"] and not info["prompts"]:
            continue
        label = project_label(info, proj_dir, cfg.projects)
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
            "title": title, "sid": sid, "file": out, "source": f,
            "mode": "export", "updated": datetime.now().isoformat(timespec="seconds"),
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
    with open(os.path.join(cfg.out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    save_index(cfg, index)
    print(f"导出完成：{total} 场会话 -> {cfg.out_dir}")
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
            or args.find or args.retry_failed):
        ap.print_help()
        return 0

    cfg = build_config(args)
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
