# -*- coding: utf-8 -*-
"""Golden Dataset 回放与检测工具。

这些函数只描述“回放响应是否满足 fixture 里写下的契约”，
不证明模型对任意会话的摘要质量。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import session_digest as sd  # noqa: E402

REPLAY_VERSION = 1
_MODES = ("full", "incremental", "merge")


class ReplayError(RuntimeError):
    pass


def load_replay(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    validate_replay(data, path)
    return data


def validate_replay(data, path="<replay>"):
    if not isinstance(data, dict):
        raise ReplayError(f"{path}: 回放文件不是对象")
    if data.get("replay_version") != REPLAY_VERSION:
        raise ReplayError(
            f"{path}: replay_version={data.get('replay_version')}，"
            f"需要 {REPLAY_VERSION}")
    if int(data.get("prompt_version") or 0) != int(sd.PROMPT_VERSION):
        raise ReplayError(
            f"{path}: prompt_version={data.get('prompt_version')}，"
            f"当前代码是 {sd.PROMPT_VERSION}；请重新录制")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ReplayError(f"{path}: entries 为空")
    for i, entry in enumerate(entries):
        if entry.get("mode") not in _MODES:
            raise ReplayError(f"{path}: 第 {i} 条 mode 无效：{entry.get('mode')}")
        if not isinstance(entry.get("response"), str) or not entry["response"].strip():
            raise ReplayError(f"{path}: 第 {i} 条 response 为空")


def prompt_mode(prompt):
    """按提示词里的稳定标志判断本轮是哪种模式。"""
    text = str(prompt or "")
    if "以下是同一场 AI 编程会话的原始摘要和多次续写" in text:
        return "merge"
    if "这是这场会话之后新增的对话" in text:
        return "incremental"
    return "full"


class ReplayLLM:
    """替换 sd.call_llm：每种模式按 entries 顺序消费一次。"""

    def __init__(self, source):
        if isinstance(source, dict):
            data = source
            validate_replay(data)
        else:
            data = load_replay(source)
        self.data = data
        self.entries = list(data.get("entries") or [])
        self.used = set()
        self.calls = []

    def reset(self):
        self.used.clear()
        self.calls.clear()

    def __call__(self, cfg, prompt, max_tokens=8000, timeout=300):
        mode = prompt_mode(prompt)
        for i, entry in enumerate(self.entries):
            if i in self.used:
                continue
            if entry.get("mode") != mode:
                continue
            self.used.add(i)
            self.calls.append({
                "mode": mode,
                "prompt": prompt,
                "max_tokens": max_tokens,
            })
            usage = entry.get("usage") or {
                "input_tokens": 0, "output_tokens": 0}
            return entry["response"], usage
        raise ReplayError(
            f"没有可用的 {mode} 回放响应；已有模式："
            f"{[e.get('mode') for e in self.entries]}")


def _norm(text):
    return sd._norm_match(text or "")


def _facts_block(expected):
    for key in ("expected_facts", "expected_merged_facts"):
        block = expected.get(key)
        if isinstance(block, dict):
            return block
    return {}


def score_summary(summary, expected):
    """检查摘要是否包含期望事实、是否出现禁止/跨项目事实。"""
    text = str(summary or "")
    norm = _norm(text)
    missing = []
    facts = _facts_block(expected)
    for group in ("paths", "commands", "decisions", "open_items"):
        for item in facts.get(group) or []:
            if _norm(item) not in norm:
                missing.append(f"{group}:{item}")

    fabricated = [x for x in expected.get("forbidden_facts") or []
                  if _norm(x) in norm]
    foreign = [x for x in expected.get("foreign_facts") or []
               if _norm(x) in norm]
    forbidden_strings = [s for s in expected.get("forbidden_strings") or []
                         if str(s).lower() in text.lower()]
    return {
        "ok": not (missing or fabricated or foreign or forbidden_strings),
        "missing": missing,
        "fabricated": fabricated,
        "foreign": foreign,
        "forbidden_strings": forbidden_strings,
    }


def _section_lines(summary, names):
    out = []
    for name, lines in sd._summary_sections(sd.strip_frontmatter(summary or "")):
        if any(n in name for n in names):
            out.extend(lines)
    return out


def _todo_text(summary):
    return "\n".join(_section_lines(summary, ("还没完", "待办")))


def check_conclusions(summary, expected):
    """检查关键结论的陈述、原因、状态是否被保留或写错。"""
    text = str(summary or "")
    norm = _norm(text)
    todos = _norm(_todo_text(text))
    missing_statement = []
    missing_reason = []
    status_mismatch = []
    stale_decision = []
    foreign = [x for x in expected.get("foreign_facts") or []
               if _norm(x) in norm]

    for concl in expected.get("key_conclusions") or []:
        cid = concl.get("id") or concl.get("statement") or "?"
        require = concl.get("require") or []
        if not all(_norm(t) in norm for t in require):
            missing_statement.append(cid)

        reason_require = concl.get("require_reason") or []
        if reason_require and not all(_norm(t) in norm for t in reason_require):
            missing_reason.append(cid)

        forbid = concl.get("forbid") or []
        if any(_norm(t) in norm for t in forbid):
            stale_decision.append(cid)

        status = concl.get("status") or "active"
        if status == "superseded":
            if not sd._SUPERSEDED_RE.search(text):
                stale_decision.append(cid)
        elif status == "completed":
            if not any(w in text for w in ("完成", "已修复", "通过", "搞定")):
                status_mismatch.append(cid)
            if all(_norm(t) in todos for t in require if len(_norm(t)) >= 2):
                status_mismatch.append(cid)

    return {
        "ok": not (missing_statement or missing_reason
                   or status_mismatch or stale_decision or foreign),
        "missing_statement": missing_statement,
        "missing_reason": missing_reason,
        "status_mismatch": status_mismatch,
        "stale_decision": stale_decision,
        "foreign": foreign,
    }


def certainty_overreach(summary):
    """统计摘要里被程序补上的“结果未经证实”标记（不含图例）。"""
    text = str(summary or "").replace(sd._LEGEND, "")
    return text.count("⚠?")


# 合并防丢使用实现里的同一个近似检查，避免测试与运行时代码分叉。
merge_loss_check = sd.merge_loss_check
