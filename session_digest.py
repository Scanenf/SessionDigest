#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SessionDigest — 把 Claude Code / Codex 的本地会话记录蒸馏成结构化 Markdown 摘要。

三种模式：
  --export     全量导出，不调用模型，生成可浏览的会话索引
  --llm N      预览：对最近 N 场会话生成摘要，写到临时目录
  --nightly    正式：增量提炼新会话，写入 memory/ 和 Obsidian

隐私提示：
  本程序会读取本机会话记录，并把会话内容发送到你配置的模型端点。
  请只对属于自己的会话运行；公开分享输出前，先检查是否含密钥、内网地址、
  客户名称等敏感信息。
"""
from __future__ import annotations

import argparse
import glob
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

VERSION = "0.1.0"


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
    projects: Dict[str, str]
    base_url: str
    api_key: Optional[str]
    model: str
    min_turns: int = 8
    min_age_hours: int = 6
    max_sessions: int = 20
    force: bool = False
    verbose: bool = False


def _expand(p: Optional[str]) -> Optional[str]:
    return os.path.expanduser(p) if p else p


def load_projects(path: Optional[str]) -> Dict[str, str]:
    """可选的项目名映射：{"目录名或 cwd 名": "展示名"}。"""
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
    """命令行 > 环境变量 > ~/.claude/settings.json 的 env 块 > 默认值。"""
    base = args.base_url or os.environ.get("ANTHROPIC_BASE_URL")
    tok = (args.api_key
           or os.environ.get("ANTHROPIC_AUTH_TOKEN")
           or os.environ.get("ANTHROPIC_API_KEY"))
    model = args.model or os.environ.get("ANTHROPIC_MODEL")

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

    return (base or "https://api.anthropic.com").rstrip("/"), tok, (model or "claude-sonnet-5-5")


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


def parse_claude(path: str) -> dict:
    info = _blank_info("claude")
    try:
        with open(path, encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = _fmt_ts(e.get("timestamp"))
                if ts:
                    if info["start"] is None:
                        info["start"] = ts
                    info["end"] = ts
                typ = e.get("type")
                if not typ or typ == "summary":
                    continue
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
                    continue

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
    except Exception as ex:
        info["error"] = str(ex)
    return info


def parse_session(path: str, kind: str) -> dict:
    return parse_claude(path) if kind == "claude" else parse_codex(path)


# ---------------------------------------------------------------------------
# 项目名与文件扫描
# ---------------------------------------------------------------------------

def session_key(info: dict, path: str) -> str:
    return info.get("session") or os.path.splitext(os.path.basename(path))[0]


def project_label(info: dict, proj_dir: Optional[str], projects: Dict[str, str]) -> str:
    """优先用会话里记录的工作目录名，其次用 Claude 项目目录名。"""
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


def iter_session_files(cfg: Config) -> Iterator[Tuple[str, str, Optional[str]]]:
    """yield (path, kind, 项目目录)。"""
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


# ---------------------------------------------------------------------------
# 摘要输入与模型调用
# ---------------------------------------------------------------------------

def title_of(info: dict) -> str:
    if info["prompts"]:
        return trunc(clean_prompt(info["prompts"][0]), 40) or "未命名会话"
    return "未命名会话"


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


def call_llm(cfg: Config, prompt: str, max_tokens: int = 8000, timeout: int = 300):
    if not cfg.api_key:
        raise RuntimeError(
            "缺少 API key：请设置 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY，"
            "或传入 --api-key")
    last_err = None
    for net_attempt in range(2):
        body = json.dumps({
            "model": cfg.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            cfg.base_url + "/v1/messages", data=body, method="POST",
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": cfg.api_key,
                "authorization": "Bearer " + cfg.api_key,
            })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if net_attempt == 0:
                time.sleep(2)
                continue
            raise

        text = "".join(b.get("text", "") for b in d.get("content", [])
                       if b.get("type") == "text").strip()
        usage = d.get("usage") or {}
        if text:
            return text, usage
        if d.get("stop_reason") == "max_tokens" and max_tokens < 32000:
            max_tokens *= 2
            continue
        raise RuntimeError(f"模型未返回文本: stop_reason={d.get('stop_reason')}")
    raise RuntimeError(f"请求失败: {last_err}")


def summarize_one(cfg: Config, info: dict, label: str):
    transcript = build_transcript(info)
    text, usage = call_llm(cfg, summary_prompt(info, label, transcript))
    return text, usage, len(transcript)


def estimate(transcripts_chars: int) -> int:
    """粗略估算输入 token：中文约 1 字 1 token，中英混合按 3 字符/token 估。"""
    return transcripts_chars // 3


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _safe_name(s: str, limit: int = 40) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s)[:limit]


def write_obsidian(cfg: Config, info: dict, label: str, text: str,
                   sid: str) -> Optional[str]:
    if not cfg.obsidian_dir:
        return None
    date = (info.get("start") or "unknown").split(" ")[0]
    sid = sid or session_key(info, "")
    d = os.path.join(cfg.obsidian_dir, date[:7])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{date}_{sid[:8]}.md")
    title = title_of(info).replace('"', "'")
    md = "\n".join([
        "---",
        f'title: "{title}"',
        f"date: {date}",
        f"project: {label}",
        f"platform: {'claude-code' if info['kind'] == 'claude' else 'codex'}",
        f'session: "{sid}"',
        f"turns: {info.get('turns')}",
        "tags: [自动摘要]",
        "---", "",
        f"# {title}", "", text, "",
    ])
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


def append_monthly(cfg: Config, month: str, info: dict, label: str, text: str,
                   sid: str) -> Optional[str]:
    if not cfg.memory_dir:
        return None
    d = os.path.join(cfg.memory_dir, "sessions")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{month}.md")
    new_file = not os.path.exists(path)
    date = (info.get("start") or "unknown").split(" ")[0]
    sid = sid or session_key(info, "")
    block = []
    if new_file:
        block += [f"# {month} 会话摘要", "",
                  "> 自动生成。原文可在本地会话记录中检索。", ""]
    block += [f"## {date} · {label} · {info.get('turns')} 条消息", "",
              f"session: `{sid}`", "", text, "", "---", ""]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(block))
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
    with open(idx, "w", encoding="utf-8") as f:
        f.write(cur + line + "\n")
    return True


def save_state(cfg: Config, state: dict) -> None:
    path = cfg.state_file
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_state(cfg: Config) -> dict:
    try:
        with open(cfg.state_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------

def plan_nightly(cfg: Config, state: dict) -> List[Tuple[dict, str, str, str, str]]:
    """返回 [(info, label, kind, path, key)]，按修改时间倒序，最多 max_sessions 条。"""
    now = time.time()
    todo = []
    for mt, f, kind, proj_dir in collect_files(cfg):
        if now - mt < cfg.min_age_hours * 3600:
            continue
        info = parse_session(f, kind)
        if not info["turns"] and not info["prompts"]:
            continue
        key = session_key(info, f)
        if key in state and not cfg.force:
            continue
        if info["turns"] < cfg.min_turns:
            continue
        todo.append((info, project_label(info, proj_dir, cfg.projects), kind, f, key))
        if len(todo) >= cfg.max_sessions:
            break
    return todo


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


def print_plan(todo, cfg: Config) -> int:
    chars = 0
    for info, _label, _kind, path, _key in todo:
        chars += len(build_transcript(info))
    print(f"计划处理 {len(todo)} 场会话，预计输入约 {estimate(chars):,} tokens"
          f"（输出另计，按你所用模型的价格收费）")
    return chars


# ---------------------------------------------------------------------------
# 模式
# ---------------------------------------------------------------------------

def cmd_preview(cfg: Config, n: int, dry: bool) -> int:
    todo = plan_preview(cfg, n)
    if not todo:
        print("没有找到可处理的会话。")
        return 0
    print_plan(todo, cfg)
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
        date = (info.get("start") or "unknown").split(" ")[0]
        title = title_of(info).replace('"', "'")
        out = os.path.join(cfg.out_dir, f"{date}_{key[:8]}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join([
                "---", f'title: "{title}"', f"project: {label}",
                f"platform: {'claude-code' if kind == 'claude' else 'codex'}",
                f'session: "{key}"', f"turns: {info.get('turns')}",
                "---", "", f"# {title}", "", text, "",
                "---", f"原文：`{path}`",
            ]))
        print(f"  {nchars} 字符 -> in={usage.get('input_tokens')} "
              f"out={usage.get('output_tokens')} -> {out}")
    return 0


def cmd_nightly(cfg: Config, dry: bool) -> int:
    if not dry and not (cfg.memory_dir or cfg.obsidian_dir):
        print("错误：--nightly 需要至少一个输出目录：--memory-dir 或 --obsidian-dir",
              file=sys.stderr)
        return 2

    state = load_state(cfg)
    todo = plan_nightly(cfg, state)
    if not todo:
        print("没有需要提炼的新会话。")
        return 0
    print_plan(todo, cfg)
    if dry:
        for info, label, _kind, path, key in todo:
            print(f"  [{label}] {info.get('start')} · {info.get('turns')} 条消息 · "
                  f"{os.path.basename(path)}")
        return 0

    done = 0
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

        month = (info.get("start") or "")[:7] or datetime.now().strftime("%Y-%m")
        append_monthly(cfg, month, info, label, text, key)
        write_obsidian(cfg, info, label, text, key)
        update_memory_index(cfg, month)

        state[key] = {
            "date": info.get("start"),
            "source": path,
            "tokens": usage,
            "done_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(cfg, state)
        done += 1
        print(f"  {nchars} 字符 -> in={usage.get('input_tokens')} "
              f"out={usage.get('output_tokens')}")

    print(f"完成 {done} 场")
    return 0


def cmd_export(cfg: Config) -> int:
    """全量导出，不调模型。生成按项目分组的 Markdown 和 README 索引。"""
    idx = {"claude": OrderedDict(), "codex": OrderedDict()}
    total = 0

    for mt, f, kind, proj_dir in collect_files(cfg):
        info = parse_session(f, kind)
        if not info["turns"] and not info["prompts"]:
            continue
        label = project_label(info, proj_dir, cfg.projects)
        sid = session_key(info, f)
        fname = f"{sid[:8]}_{_safe_name(title_of(info))}.md"
        if kind == "claude":
            out = os.path.join(cfg.out_dir, "Claude Code", _safe_name(label, 80), fname)
        else:
            out = os.path.join(cfg.out_dir, "Codex", fname)
        write_plain_session(out, info, label)
        idx[kind].setdefault(label, []).append((fname, info))
        total += 1

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
            for fname, info in items:
                d = (info.get("start") or "")[:10]
                lines.append(f"- [[{os.path.splitext(fname)[0]}]] — {d} · {title_of(info)}")
            lines.append("")

    os.makedirs(cfg.out_dir, exist_ok=True)
    with open(os.path.join(cfg.out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"导出完成：{total} 场会话 -> {cfg.out_dir}")
    return 0


def write_plain_session(path: str, info: dict, label: str) -> None:
    title = title_of(info)
    md = ["---",
          f'title: "{title}"',
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

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_config(args) -> Config:
    base_url, api_key, model = resolve_llm(args)
    state_file = args.state_file or os.path.expanduser(
        "~/.claude-session-digest/state.json")
    return Config(
        claude_root=_expand(args.claude_root) or os.path.expanduser("~/.claude/projects"),
        codex_root=_expand(args.codex_root) or os.path.expanduser("~/.codex/sessions"),
        out_dir=_expand(args.out) or "digest-output",
        obsidian_dir=_expand(args.obsidian_dir),
        memory_dir=_expand(args.memory_dir),
        state_file=_expand(state_file),
        projects=load_projects(args.projects_file),
        base_url=base_url,
        api_key=api_key,
        model=model,
        min_turns=args.min_turns,
        min_age_hours=args.min_age_hours,
        max_sessions=args.max_sessions,
        force=args.force,
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
               "  python session_digest.py --nightly --obsidian-dir ~/vault/摘要\n")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--export", action="store_true",
                      help="全量导出，不调用模型")
    mode.add_argument("--llm", type=int, metavar="N",
                      help="预览：对最近 N 场会话生成摘要")
    mode.add_argument("--nightly", action="store_true",
                      help="正式：增量提炼新会话")

    ap.add_argument("--claude-root", default=None,
                    help="Claude Code 会话目录（默认 ~/.claude/projects）")
    ap.add_argument("--codex-root", default=None,
                    help="Codex 会话目录（默认 ~/.codex/sessions）")
    ap.add_argument("--out", default=None,
                    help="导出 / 预览输出目录（默认 digest-output）")
    ap.add_argument("--obsidian-dir", default=None,
                    help="Obsidian 摘要目录；给了才会写")
    ap.add_argument("--memory-dir", default=None,
                    help="memory 目录；给了才会写 MEMORY.md 索引")
    ap.add_argument("--state-file", default=None,
                    help="去重状态文件（默认 ~/.claude-session-digest/state.json）")
    ap.add_argument("--projects-file", default=None,
                    help="项目名映射 JSON，如 {\"my-repo\": \"我的项目\"}")

    ap.add_argument("--base-url", default=None, help="模型 API 地址")
    ap.add_argument("--api-key", default=None, help="模型 API key（也可用环境变量）")
    ap.add_argument("--model", default=None, help="模型名（默认 claude-sonnet-5-5）")

    ap.add_argument("--min-turns", type=int, default=8,
                    help="少于 N 条消息的会话跳过（默认 8）")
    ap.add_argument("--min-age-hours", type=int, default=6,
                    help="结束不足 N 小时的会话跳过（默认 6）")
    ap.add_argument("--max-sessions", type=int, default=20,
                    help="单次最多处理 N 场（默认 20）")
    ap.add_argument("--force", action="store_true", help="忽略去重状态，重跑")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出计划和 token 估算，不调用模型、不写文件")
    ap.add_argument("--verbose", action="store_true", help="失败时打印完整堆栈")

    args = ap.parse_args(argv)
    if not (args.export or args.llm or args.nightly):
        ap.print_help()
        return 0

    cfg = build_config(args)
    if args.export:
        return cmd_export(cfg)
    if args.llm:
        return cmd_preview(cfg, args.llm, args.dry_run)
    return cmd_nightly(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
