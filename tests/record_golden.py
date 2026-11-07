#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性录制 Golden Dataset 的模型响应。

默认测试不会调用这个脚本。录制后会先脱敏再写盘，但提交前仍需人工审阅：
确认响应里没有真实姓名、密钥、客户名称或内网地址。

示例：
  python tests/record_golden.py tests/fixtures/basic_claude --mode full
  python tests/record_golden.py tests/fixtures/incremental_codex --mode full
  python tests/record_golden.py tests/fixtures/incremental_codex --mode incremental \
      --start-line 7 --prev-summary /tmp/full.md --append
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import session_digest as sd  # noqa: E402


def _kind(path, requested):
    if requested and requested != "auto":
        return requested
    with open(path, encoding="utf-8", errors="replace") as f:
        first = f.readline()
    try:
        obj = json.loads(first)
    except Exception:
        return "claude"
    return "codex" if "payload" in obj else "claude"


def _parse_slice(path, kind, start_line):
    """只解析从 start_line（1 起）开始的记录。"""
    if not start_line or start_line <= 1:
        return sd.parse_session(path, kind)
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines[start_line - 1:])
        return sd.parse_session(tmp, kind)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def build_cfg(args):
    base_url, api_key, model = sd.resolve_llm(args)
    return sd.Config(
        claude_root="", codex_root="", out_dir="",
        obsidian_dir=None, memory_dir=None,
        state_file=os.path.join(tempfile.gettempdir(), "sd-record-state.json"),
        index_file=os.path.join(tempfile.gettempdir(), "sd-record-index.json"),
        failures_file=os.path.join(tempfile.gettempdir(), "sd-record-failures.json"),
        lock_file=os.path.join(tempfile.gettempdir(), "sd-record.lock"),
        projects={}, base_url=base_url, api_key=api_key, model=model,
        api_format=args.api_format, min_turns=0, min_age_hours=0,
        redact=args.redact,
    )


def build_prompt(args, info, label):
    if args.mode == "full":
        transcript = sd.redact_text(sd.build_transcript(info), args.redact)
        return sd.summary_prompt(info, label, transcript)
    if not args.prev_summary:
        raise SystemExit(f"--mode {args.mode} 需要 --prev-summary")
    with open(args.prev_summary, encoding="utf-8") as f:
        prev = sd.redact_text(f.read(), args.redact)
    if args.mode == "merge":
        return sd.merge_prompt(prev)
    delta = sd.redact_text(sd.build_transcript(info), args.redact)
    return sd.continuation_prompt(prev, info, delta, sd._today(info))


def main(argv=None):
    ap = argparse.ArgumentParser(description="录制 Golden Dataset 模型响应")
    ap.add_argument("fixture", help="fixture 目录（含 session.jsonl）")
    ap.add_argument("--mode", choices=("full", "incremental", "merge"),
                    default="full")
    ap.add_argument("--kind", choices=("auto", "claude", "codex"), default="auto")
    ap.add_argument("--start-line", type=int, default=1,
                    help="incremental 模式从第几行开始作为新增部分（1 起）")
    ap.add_argument("--prev-summary", help="incremental/merge 使用的旧摘要")
    ap.add_argument("--label", help="项目标签，默认取 expected.json 的 project")
    ap.add_argument("--api-format", choices=("anthropic", "openai"),
                    default="anthropic")
    ap.add_argument("--base-url")
    ap.add_argument("--api-key")
    ap.add_argument("--model")
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--redact", choices=("off", "safe", "strict"),
                    default="strict")
    ap.add_argument("--append", action="store_true",
                    help="保留已有 entries，追加本轮响应")
    ap.add_argument("--force", action="store_true", help="覆盖已有文件")
    args = ap.parse_args(argv)

    session = os.path.join(args.fixture, "session.jsonl")
    out = os.path.join(args.fixture, "llm_replay.json")
    if os.path.exists(out) and not (args.append or args.force):
        raise SystemExit(f"{out} 已存在；用 --append 追加或 --force 覆盖")

    kind = _kind(session, args.kind)
    info = _parse_slice(session, kind, args.start_line
                        if args.mode == "incremental" else 1)
    label = args.label
    if not label:
        try:
            with open(os.path.join(args.fixture, "expected.json"),
                      encoding="utf-8") as f:
                label = json.load(f).get("project") or ""
        except Exception:
            label = ""
    label = label or os.path.basename(os.path.abspath(args.fixture))

    cfg = build_cfg(args)
    prompt = build_prompt(args, info, label)
    text, usage = sd.call_llm(cfg, prompt, max_tokens=args.max_tokens)
    text = sd.redact_text(text, args.redact).strip()

    if args.append and os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {
            "replay_version": 1,
            "kind": "recorded",
            "model": cfg.model,
            "api_format": cfg.api_format,
            "prompt_version": sd.PROMPT_VERSION,
            "max_tokens": args.max_tokens,
            "recorded_at": date.today().isoformat(),
            "entries": [],
        }
    data["entries"].append({
        "mode": args.mode,
        "response": text,
        "usage": usage,
    })
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 {out}（mode={args.mode}, model={cfg.model}, "
          f"prompt_version={sd.PROMPT_VERSION}）")
    print("提交前请人工审阅响应：确认没有真实姓名、密钥、客户名称或内网地址。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
