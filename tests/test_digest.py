# -*- coding: utf-8 -*-
"""SessionDigest 单元测试：python -m unittest discover -s tests -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_digest as sd  # noqa: E402
import replay  # noqa: E402


def make_cfg(tmp):
    return sd.Config(
        claude_root=os.path.join(tmp, "claude"),
        codex_root=os.path.join(tmp, "codex"),
        out_dir=os.path.join(tmp, "out"),
        obsidian_dir=os.path.join(tmp, "vault"),
        memory_dir=os.path.join(tmp, "memory"),
        state_file=os.path.join(tmp, "state.json"),
        index_file=os.path.join(tmp, "index.json"),
        failures_file=os.path.join(tmp, "failures.json"),
        lock_file=os.path.join(tmp, "digest.lock"),
        projects={},
        base_url="http://127.0.0.1:1",
        api_key="test",
        model="test-model",
        min_turns=4,
        min_age_hours=0,
    )


def claude_lines(sid, pairs, start_minute=0):
    lines = []
    for i in range(pairs):
        minute = start_minute + i * 2
        lines.append({
            "type": "user",
            "timestamp": f"2026-11-01T10:{minute:02d}:00Z",
            "sessionId": sid,
            "cwd": "C:/work/shop",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": f"问题 {i} " + "x" * 120}]},
        })
        lines.append({
            "type": "assistant",
            "timestamp": f"2026-11-01T10:{minute + 1:02d}:00Z",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": f"回答 {i} " + "y" * 120},
                                    {"type": "tool_use", "id": f"t{i}", "name": "Read",
                                     "input": {"file_path": f"C:/work/shop/f{i}.py"}}]},
        })
    return lines


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestParsers(unittest.TestCase):
    def test_parse_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.jsonl")
            write_jsonl(p, claude_lines("sid-1", 3))
            info = sd.parse_claude(p)
            self.assertEqual(info["session"], "sid-1")
            self.assertEqual(info["turns"], 6)
            self.assertEqual(len(info["prompts"]), 3)
            self.assertIn("问题 0", info["prompts"][0])
            self.assertIn("Read", info["tools"])
            self.assertIn("C:/work/shop/f0.py", info["files"])

    def test_parse_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.jsonl")
            write_jsonl(p, [
                {"timestamp": "2026-11-05T09:00:00Z",
                 "payload": {"type": "session_meta", "session_id": "cx-1", "cwd": "C:/pet"}},
                {"timestamp": "2026-11-05T09:01:00Z",
                 "payload": {"type": "user_message", "message": "加个气泡"}},
                {"timestamp": "2026-11-05T09:02:00Z",
                 "payload": {"type": "agent_message", "message": "加好了"}},
                {"timestamp": "2026-11-05T09:03:00Z",
                 "payload": {"type": "function_call", "name": "shell",
                             "arguments": {"command": "npm run build"}}},
            ])
            info = sd.parse_codex(p)
            self.assertEqual(info["session"], "cx-1")
            self.assertEqual(info["turns"], 2)
            self.assertIn("npm run build", info["commands"])

    def test_incremental_only_reads_complete_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.jsonl")
            recs = claude_lines("sid-2", 1)
            first = json.dumps(recs[0], ensure_ascii=False) + "\n"
            second = json.dumps(recs[1], ensure_ascii=False)
            with open(p, "w", encoding="utf-8") as f:
                f.write(first + second)          # 第二行没有换行
            with open(p, "rb") as f:
                raw = f.read()
            expected = raw.find(b"\n") + 1       # 文件里的实际字节数（Windows 下含 \r）
            info, new_offset = sd.parse_incremental(p, "claude", 0)
            self.assertEqual(info["turns"], 1)    # 只吃到完整的第一行
            self.assertEqual(new_offset, expected)

    def test_prefix_sha1_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.jsonl")
            with open(p, "wb") as f:
                f.write(b"abc\ndef\n")
            h1 = sd.prefix_sha1(p, 4)
            with open(p, "wb") as f:
                f.write(b"xyz\ndef\n")
            h2 = sd.prefix_sha1(p, 4)
            self.assertNotEqual(h1, h2)


class TestVerify(unittest.TestCase):
    def test_marks_missing_identifiers(self):
        info = sd._blank_info("claude")
        info["prompts"] = ["改 /a/b.py，执行 pytest -q"]
        info["command_events"] = [{
            "cmd": "pytest -q", "tool_use_id": "t1", "channel": "tool_use",
            "result": "", "is_error": None, "success_evidence": False}]
        text = ("## 关键标识符\n"
                "- 路径：`/a/b.py`\n"
                "- 路径：`/missing/file.py`\n"
                "- 命令：`pytest -q`\n")
        out = sd.verify_summary(text, info)
        self.assertNotIn("`/a/b.py` ⚠", out)
        self.assertIn("`/missing/file.py` ⚠", out)
        self.assertNotIn("`pytest -q` ⚠", out)

    def test_no_verify_disables_marking(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg.verify = False
            info = sd._blank_info("claude")
            info["prompts"] = ["x"]
            text = "## 关键标识符\n- 路径：`/missing/file.py`\n"
            self.assertNotIn("⚠", sd.checked_summary(cfg, text, info))

    def test_checks_continuation_section(self):
        info = sd._blank_info("claude")
        info["prompts"] = ["出现报错 boom"]
        text = "### 关键标识符\n- 报错原文：`boom`\n- 报错原文：`nope`\n"
        out = sd.verify_summary(text, info)
        self.assertNotIn("`boom` ⚠", out)
        self.assertIn("`nope` ⚠", out)


class TestRedact(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            sd, "_local_usernames", return_value=["测试用户", "testuser"])
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_masks_credentials(self):
        raw = ("key sk-ant-api03-abcdefghijklmnop\n"
               "token=abc123456789\n"
               "Authorization: Bearer abcdefgh12345678\n"
               "ghp_abcdefghijklmnopqrstuvwxyz\n")
        out = sd.redact_text(raw, "safe")
        for secret in ("sk-ant-api03-abcdefghijklmnop",
                       "abc123456789",
                       "abcdefgh12345678",
                       "ghp_abcdefghijklmnopqrstuvwxyz"):
            self.assertNotIn(secret, out)
        self.assertIn("token=***", out)

    def test_masks_json_style_secret(self):
        out = sd.redact_text('{"password": "hunter2hunter2"}', "safe")
        self.assertNotIn("hunter2hunter2", out)
        self.assertIn('"password": "***"', out)

    def test_masks_email_and_private_ip(self):
        out = sd.redact_text("联系 a.b@example.com，或 192.168.1.20", "safe")
        self.assertIn("<email>", out)
        self.assertIn("<private-ip>", out)
        self.assertNotIn("192.168.1.20", out)

    def test_strict_replaces_home_prefix(self):
        cases = [
            (r"C:\Users\测试用户\proj\a.py", r"~\proj\a.py"),
            ("C:/Users/testuser/proj/a.py", "~/proj/a.py"),
            ("/home/testuser/proj/a.py", "~/proj/a.py"),
            ("/Users/testuser/proj/a.py", "~/proj/a.py"),
            ("/c/Users/测试用户/proj/a.py", "~/proj/a.py"),
        ]
        for raw, want in cases:
            self.assertEqual(sd.redact_text(raw, "strict"), want)

    def test_safe_and_off_keep_paths(self):
        path = r"C:\Users\测试用户\proj\a.py"
        self.assertEqual(sd.redact_text(path, "safe"), path)
        self.assertEqual(sd.redact_text(path, "off"), path)

    def test_strict_masks_standalone_username(self):
        self.assertEqual(sd.redact_text("测试用户的电脑", "strict"), "~的电脑")

    def test_transcript_is_redacted_before_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            info = sd._blank_info("claude")
            info["prompts"] = [
                "key 是 sk-ant-api03-abcdefghijklmnop，路径 "
                r"C:\Users\测试用户\proj\a.py"]
            captured = {}

            def fake_call(cfg, prompt, **kwargs):
                captured["prompt"] = prompt
                return "ok", {}

            with mock.patch.object(sd, "call_llm", side_effect=fake_call):
                sd.summarize_one(cfg, info, "shop")
            self.assertNotIn("sk-ant-api03", captured["prompt"])
            self.assertNotIn("测试用户", captured["prompt"])
            self.assertIn(r"~\proj\a.py", captured["prompt"])

    def test_failure_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            sd.record_failure(cfg, "sid-z", "/src.jsonl",
                              "401 sk-ant-api03-abcdefghijklmnop", "claude")
            saved = sd.load_failures(cfg)["sid-z"]["error"]
            self.assertNotIn("sk-ant-api03", saved)


class TestBlocks(unittest.TestCase):
    def setUp(self):
        self.text = (
            "# 2026-11 会话摘要\n\n"
            "## 2026-11-01 · shop · 8 条消息\n\n"
            "session: `abc`\n\nold body\n\n---\n\n"
            "## 2026-11-02 · pet · 4 条消息\n\n"
            "session: `def`\n\nother body\n\n---\n"
        )

    def test_find_and_replace(self):
        self.assertIsNotNone(sd.find_monthly_block(self.text, "abc"))
        new = sd.replace_block(self.text, "abc", "## 2026-11-01 · shop · 8 条消息\n\n"
                                                "session: `abc`\n\nnew body\n\n---\n")
        self.assertIn("new body", new)
        self.assertNotIn("old body", new)
        self.assertIn("other body", new)

    def test_insert_into_block(self):
        new = sd.insert_into_block(self.text, "abc", "## 续 · 2026-11-01 12:00\n\nmore")
        self.assertIn("old body", new)
        self.assertIn("## 续", new)
        self.assertIn("other body", new)

    def test_block_text(self):
        self.assertEqual(sd.block_text(self.text, "abc"), "old body")

    def test_strip_frontmatter(self):
        doc = "---\ntitle: x\n---\n\n# H\n\nbody"
        self.assertEqual(sd.strip_frontmatter(doc).lstrip("\n"), "# H\n\nbody")


class TestPlanning(unittest.TestCase):
    def _prepare(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = make_cfg(tmp)
        path = os.path.join(cfg.claude_root, "proj", "a.jsonl")
        write_jsonl(path, claude_lines("sid-plan", 3))
        return tmp, cfg, path

    def test_new_session_is_full(self):
        tmp, cfg, path = self._prepare()
        tasks, _ = sd.plan_nightly(cfg, {})
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].mode, "full")

    def test_unchanged_session_is_skipped(self):
        tmp, cfg, path = self._prepare()
        size = os.path.getsize(path)
        state = {"sid-plan": {"offset": size,
                              "prefix_sha1": sd.prefix_sha1(path, size),
                              "sections": 0}}
        tasks, _ = sd.plan_nightly(cfg, state)
        self.assertEqual(tasks, [])

    def test_appended_session_is_incremental(self):
        tmp, cfg, path = self._prepare()
        size = os.path.getsize(path)
        state = {"sid-plan": {"offset": size,
                              "prefix_sha1": sd.prefix_sha1(path, size),
                              "sections": 0}}
        # 先写一份旧摘要，plan 才能读到
        vault = os.path.join(cfg.obsidian_dir, "2026-11")
        os.makedirs(vault)
        with open(os.path.join(vault, "2026-11-01_sid-plan.md"), "w",
                  encoding="utf-8") as f:
            f.write('---\nsession: "sid-plan"\n---\n\nold summary\n')
        with open(path, "a", encoding="utf-8") as f:
            for r in claude_lines("sid-plan", 3, start_minute=10):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tasks, _ = sd.plan_nightly(cfg, state)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].mode, "incremental")
        self.assertGreater(tasks[0].new_offset, size)
        self.assertIn("old summary", tasks[0].prev_summary)

    def test_rewritten_file_is_full(self):
        tmp, cfg, path = self._prepare()
        size = os.path.getsize(path)
        state = {"sid-plan": {"offset": size,
                              "prefix_sha1": "deadbeefdeadbeef",
                              "sections": 0}}
        tasks, _ = sd.plan_nightly(cfg, state)
        self.assertEqual(tasks[0].mode, "full")

    def test_merge_when_threshold_reached(self):
        tmp, cfg, path = self._prepare()
        size = os.path.getsize(path)
        state = {"sid-plan": {"offset": size,
                              "prefix_sha1": sd.prefix_sha1(path, size),
                              "sections": cfg.merge_after}}
        vault = os.path.join(cfg.obsidian_dir, "2026-11")
        os.makedirs(vault)
        with open(os.path.join(vault, "2026-11-01_sid-plan.md"), "w",
                  encoding="utf-8") as f:
            f.write('---\nsession: "sid-plan"\n---\n\nold summary\n')
        tasks, _ = sd.plan_nightly(cfg, state)
        self.assertEqual(tasks[0].mode, "merge")

    def test_old_state_entry_is_migrated_not_rerun(self):
        tmp, cfg, path = self._prepare()
        state = {"sid-plan": {"date": "2026-11-01", "source": path}}
        tasks, notes = sd.plan_nightly(cfg, state)
        self.assertEqual(tasks, [])
        self.assertIn("offset", state["sid-plan"])
        self.assertTrue(notes)


class TestIndexAndFailures(unittest.TestCase):
    def test_index_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            info = sd._blank_info("claude")
            info["session"] = "sid-x"
            info["prompts"] = ["做点事"]
            info["start"] = "2026-11-01 10:00"
            sd.update_index(cfg, info, "shop", "body", "sid-x", "/src.jsonl", "full")
            idx = sd.load_index(cfg)
            self.assertIn("sid-x", idx["sessions"])
            self.assertEqual(idx["sessions"]["sid-x"]["project"], "shop")

    def test_failure_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            sd.record_failure(cfg, "sid-y", "/src.jsonl", "boom", "codex")
            f = sd.load_failures(cfg)
            self.assertEqual(f["sid-y"]["attempts"], 1)
            self.assertEqual(f["sid-y"]["kind"], "codex")
            sd.clear_failure(cfg, "sid-y")
            self.assertNotIn("sid-y", sd.load_failures(cfg))


class TestContext(unittest.TestCase):
    def test_context_filters_by_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            os.makedirs(cfg.obsidian_dir, exist_ok=True)
            for sid, project, date in [("a", "shop", "2026-11-01"),
                                       ("b", "pet", "2026-11-02")]:
                p = os.path.join(cfg.obsidian_dir, date[:7], f"{date}_{sid}.md")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f'---\nsession: "{sid}"\n---\n\n# {project}\nbody {project}\n')
                idx = sd.load_index(cfg)
                idx["sessions"][sid] = {"date": date, "project": project,
                                        "title": project, "sid": sid, "file": p}
                sd.save_index(cfg, idx)
            buf = StringIO()
            with redirect_stdout(buf):
                rc = sd.cmd_context(cfg, 5, "shop")
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("body shop", out)
            self.assertNotIn("body pet", out)


class _FakeAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(raw.decode("utf-8")) if raw else {},
        })
        if not self.server.responses:
            status, body = 500, b"no response queued"
        else:
            status, body = self.server.responses.pop(0)
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@contextmanager
def fake_api(responses):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAPI)
    srv.responses = list(responses)
    srv.requests = []
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


ANTHROPIC_OK = {
    "content": [{"type": "text", "text": "摘要正文"}],
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "stop_reason": "end_turn",
}
OPENAI_OK = {
    "choices": [{"message": {"content": "摘要正文"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 8, "completion_tokens": 4},
}


class TestLock(unittest.TestCase):
    def test_second_process_cannot_acquire(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "digest.lock")
            first, second = sd.ProcessLock(path), sd.ProcessLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_nightly_refuses_when_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            os.makedirs(cfg.obsidian_dir, exist_ok=True)
            hold = sd.ProcessLock(cfg.lock_file)
            self.assertTrue(hold.acquire())
            try:
                buf = StringIO()
                with redirect_stdout(buf), redirect_stderr(buf):
                    rc = sd.cmd_nightly(cfg, dry=False)
            finally:
                hold.release()
            self.assertEqual(rc, 1)
            self.assertIn("另一个 SessionDigest 进程正在运行", buf.getvalue())


class TestAtomicWrite(unittest.TestCase):
    def test_original_file_survives_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"old": true}')
            with mock.patch.object(sd.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    sd.write_text_atomic(path, '{"new": true}')
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"old": true}')


class TestLLM(unittest.TestCase):
    def _cfg(self, tmp, fmt="anthropic"):
        cfg = make_cfg(tmp)
        cfg.api_format = fmt
        cfg.model = "test-model"
        return cfg

    def test_anthropic_success(self):
        with tempfile.TemporaryDirectory() as tmp, fake_api([(200, ANTHROPIC_OK)]) as srv:
            cfg = self._cfg(tmp)
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            text, usage = sd.call_llm(cfg, "写摘要")
            self.assertEqual(text, "摘要正文")
            self.assertEqual(usage["output_tokens"], 5)
            self.assertEqual(srv.requests[0]["path"], "/v1/messages")

    def test_openai_success_and_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp, fake_api([(200, OPENAI_OK)]) as srv:
            cfg = self._cfg(tmp, "openai")
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            text, usage = sd.call_llm(cfg, "写摘要")
            self.assertEqual(text, "摘要正文")
            self.assertEqual(usage["input_tokens"], 8)
            self.assertEqual(srv.requests[0]["path"], "/v1/chat/completions")

    def test_openai_base_with_v1_is_not_doubled(self):
        with tempfile.TemporaryDirectory() as tmp, fake_api([(200, OPENAI_OK)]) as srv:
            cfg = self._cfg(tmp, "openai")
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}/v1"
            sd.call_llm(cfg, "写摘要")
            self.assertEqual(srv.requests[0]["path"], "/v1/chat/completions")

    def test_http_error_is_reported_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp, \
                fake_api([(400, {"error": "bad model"})]) as srv:
            cfg = self._cfg(tmp)
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            with self.assertRaises(RuntimeError) as cm:
                sd.call_llm(cfg, "写摘要")
            self.assertIn("HTTP 400", str(cm.exception))
            self.assertIn("--api-format", str(cm.exception))
            self.assertEqual(len(srv.requests), 1)

    def test_server_error_retried_once(self):
        with tempfile.TemporaryDirectory() as tmp, \
                fake_api([(500, b"oops"), (200, ANTHROPIC_OK)]) as srv:
            cfg = self._cfg(tmp)
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            with mock.patch.object(sd.time, "sleep", return_value=None):
                text, _ = sd.call_llm(cfg, "写摘要")
            self.assertEqual(text, "摘要正文")
            self.assertEqual(len(srv.requests), 2)

    def test_non_json_response_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp, \
                fake_api([(200, b"<html>oops</html>")]) as srv:
            cfg = self._cfg(tmp)
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            with self.assertRaises(RuntimeError) as cm:
                sd.call_llm(cfg, "写摘要")
            self.assertIn("不是 JSON", str(cm.exception))

    def test_network_timeout_becomes_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            cfg.base_url = "http://127.0.0.1:1"
            with mock.patch.object(sd.urllib.request, "urlopen",
                                   side_effect=TimeoutError("slow")):
                with mock.patch.object(sd.time, "sleep", return_value=None):
                    with self.assertRaises(RuntimeError) as cm:
                        sd.call_llm(cfg, "写摘要")
            self.assertIn("请求失败", str(cm.exception))

    def test_truncated_answer_is_retried_with_larger_budget(self):
        empty = {"content": [], "usage": {}, "stop_reason": "max_tokens"}
        with tempfile.TemporaryDirectory() as tmp, \
                fake_api([(200, empty), (200, ANTHROPIC_OK)]) as srv:
            cfg = self._cfg(tmp)
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            text, _ = sd.call_llm(cfg, "写摘要", max_tokens=100)
            self.assertEqual(text, "摘要正文")
            self.assertEqual(srv.requests[0]["body"]["max_tokens"], 100)
            self.assertEqual(srv.requests[1]["body"]["max_tokens"], 200)


class TestFind(unittest.TestCase):
    def _seed(self, cfg):
        os.makedirs(cfg.obsidian_dir, exist_ok=True)
        idx = sd.load_index(cfg)
        docs = [
            ("a", "shop", "2026-11-01", "导出中文名乱码", "库存系统 架构 改成导出服务"),
            ("b", "pet", "2026-11-02", "桌宠气泡", "猫 桌面 气泡 动画 库存 无关"),
        ]
        for sid, project, date, title, body in docs:
            p = os.path.join(cfg.obsidian_dir, date[:7], f"{date}_{sid}.md")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(f'---\ntitle: "{title}"\ndate: {date}\nproject: {project}\n'
                        f'session: "{sid}"\n---\n\n# {title}\n\n{body}\n')
            idx["sessions"][sid] = {"date": date, "project": project, "title": title,
                                    "sid": sid, "file": p}
        sd.save_index(cfg, idx)
        return idx

    def test_find_ranks_matching_summary_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            self._seed(cfg)
            results = sd.search_summaries(cfg, "库存 架构", 3, None)
            self.assertEqual(results[0]["rec"]["sid"], "a")
            self.assertGreater(results[0]["score"], results[1]["score"])

    def test_find_respects_project_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            self._seed(cfg)
            results = sd.search_summaries(cfg, "库存", 3, "pet")
            self.assertEqual([r["rec"]["sid"] for r in results], ["b"])

    def test_find_prints_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            self._seed(cfg)
            buf = StringIO()
            with redirect_stdout(buf):
                rc = sd.cmd_find(cfg, "架构", 3, None)
            self.assertEqual(rc, 0)
            self.assertIn("改成导出服务", buf.getvalue())
            self.assertNotIn("动画 库存 无关", buf.getvalue())

    def test_find_without_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            self._seed(cfg)
            buf = StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                rc = sd.cmd_find(cfg, "量子计算", 3, None)
            self.assertEqual(rc, 1)
            self.assertIn("不是语义检索", buf.getvalue())


class PrivacyScan:
    """把“可能落盘/打印的文本”集中起来，检查已知哨兵是否漏出。"""

    REQUIRED = {
        "state", "index", "failures", "obsidian", "monthly", "export_doc",
        "export_readme", "memindex", "stdout", "stderr", "traceback", "temp",
        "backup", "lock", "model_request", "untracked_repo", "ignored_log",
    }

    def __init__(self, sentinels):
        self.needles = [s.encode("utf-8") for s in sentinels]
        self.scanned = set()
        self.leaks = []

    def add_bytes(self, category, data):
        self.scanned.add(category)
        for needle in self.needles:
            if needle in data:
                self.leaks.append((category, needle.decode("utf-8", "replace")))

    def add_text(self, category, text):
        self.add_bytes(category, str(text).encode("utf-8"))

    def add_file(self, category, path):
        with open(path, "rb") as f:
            self.add_bytes(category, f.read())

    def add_tree(self, category, root):
        self.scanned.add(category)
        if not os.path.isdir(root):
            return
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                try:
                    self.add_file(category, os.path.join(dirpath, name))
                except OSError:
                    pass


def _sha256(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _snapshot(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            out[os.path.relpath(p, root)] = _sha256(p)
    return out


class TestPrivacyMetadata(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            sd, "_local_usernames", return_value=["测试用户"])
        self.addCleanup(patcher.stop)
        patcher.start()

    def _prepare(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = make_cfg(tmp)
        src = os.path.join(cfg.claude_root, "proj", "a.jsonl")
        write_jsonl(src, claude_lines("sid-meta", 3))
        return tmp, cfg, src

    def test_index_and_failure_store_refs_not_sensitive_text(self):
        tmp, cfg, src = self._prepare()
        info = sd._blank_info("claude")
        info["session"] = "sid-meta"
        info["prompts"] = ["测试用户的会话"]
        info["start"] = "2026-11-01 10:00"
        sd.update_index(cfg, info, "测试用户项目", "body", "sid-meta",
                        src, "full")
        rec = sd.load_index(cfg)["sessions"]["sid-meta"]
        source_ref = rec["source_ref"]
        self.assertEqual(source_ref["v"], 2)
        self.assertEqual(source_ref["root"], "claude")
        self.assertEqual([p.get("n") for p in source_ref["parts"]],
                         ["proj", "a.jsonl"])
        self.assertNotIn("rel", source_ref)
        self.assertEqual(rec["file_ref"]["root"], "obsidian")
        self.assertNotIn("测试用户", json.dumps(rec, ensure_ascii=False))

        sd.record_failure(cfg, "sid-meta", src, "boom 测试用户", "claude")
        fail = sd.load_failures(cfg)["sid-meta"]
        self.assertEqual(fail["path_ref"]["v"], 2)
        self.assertEqual(fail["path_ref"]["root"], "claude")
        self.assertNotIn("rel", fail["path_ref"])
        self.assertNotIn("测试用户", json.dumps(fail, ensure_ascii=False))

    def test_execute_task_scrubs_state_and_index(self):
        tmp, cfg, src = self._prepare()
        info = sd.parse_session(src, "claude")
        task = sd.Task(info=info, label="shop", kind="claude", path=src,
                       key="sid-meta", mode="full", new_offset=os.path.getsize(src))
        with mock.patch.object(sd, "call_llm",
                               return_value=("摘要 测试用户", {})):
            self.assertTrue(sd.execute_task(cfg, task, {}))
        with open(cfg.state_file, encoding="utf-8") as f:
            state_blob = f.read()
        with open(cfg.index_file, encoding="utf-8") as f:
            index_blob = f.read()
        self.assertNotIn("测试用户", state_blob)
        self.assertNotIn("测试用户", index_blob)
        self.assertIn("source_ref", state_blob)
        self.assertIn("source_ref", index_blob)

    def test_legacy_metadata_migrates_and_is_idempotent(self):
        tmp, cfg, src = self._prepare()
        obs = os.path.join(cfg.obsidian_dir, "2026-11", "2026-11-01_sid-meta.md")
        os.makedirs(os.path.dirname(obs), exist_ok=True)
        with open(obs, "w", encoding="utf-8") as f:
            f.write('---\nsession: "sid-meta"\n---\n\nold\n')
        index = {"version": 1, "sessions": {"sid-meta": {
            "date": "2026-11-01", "project": "测试用户项目",
            "title": "测试用户标题", "sid": "sid-meta",
            "file": obs, "source": src, "mode": "full", "updated": "x"}}}
        state = {"sid-meta": {"offset": os.path.getsize(src),
                              "prefix_sha1": sd.prefix_sha1(src, os.path.getsize(src)),
                              "sections": 0, "first_title": "测试用户标题",
                              "source": src}}
        failures = {"sid-meta": {"attempts": 1, "path": src, "kind": "claude",
                                 "error": "boom", "last": "x"}}
        for path, data in ((cfg.index_file, index), (cfg.state_file, state),
                           (cfg.failures_file, failures)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

        self.assertTrue(sd.migrate_metadata(cfg))
        for path in (cfg.index_file, cfg.state_file, cfg.failures_file):
            with open(path, encoding="utf-8") as f:
                self.assertNotIn("测试用户", f.read())
        rec = sd.load_index(cfg)["sessions"]["sid-meta"]
        migrated_state = sd.load_state(cfg)["sid-meta"]
        self.assertEqual(sd.resolve_meta_path(cfg, rec, "file"), obs)
        self.assertEqual(sd.resolve_meta_path(cfg, migrated_state, "source"), src)

        before = _snapshot(tmp)
        self.assertFalse(sd.migrate_metadata(cfg))
        self.assertEqual(_snapshot(tmp), before)

    def test_dry_run_does_not_migrate_or_lock(self):
        tmp, cfg, src = self._prepare()
        size = os.path.getsize(src)
        state = {"sid-meta": {"offset": size,
                              "prefix_sha1": sd.prefix_sha1(src, size),
                              "sections": 0, "first_title": "测试用户标题",
                              "source": src}}
        with open(cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        before = _snapshot(tmp)
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            sd._run_nightly(cfg, dry=True)
        self.assertEqual(_snapshot(tmp), before)
        self.assertFalse(os.path.exists(cfg.lock_file))


class TestPrivacyReadTime(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            sd, "_local_usernames", return_value=["测试用户"])
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_summary_and_commands_redact_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            obs = os.path.join(cfg.obsidian_dir, "2026-11", "2026-11-01_sid-r.md")
            os.makedirs(os.path.dirname(obs), exist_ok=True)
            with open(obs, "w", encoding="utf-8") as f:
                f.write('---\ntitle: "测试用户标题"\nproject: 测试用户项目\n'
                        'session: "sid-r"\n---\n\n# 测试用户标题\n\n'
                        '路径 C:\\Users\\测试用户\\proj\\a.py\n')
            idx = sd.load_index(cfg)
            idx["sessions"]["sid-r"] = {
                "date": "2026-11-01", "project": "测试用户项目",
                "title": "测试用户标题", "sid": "sid-r", "file": obs}
            sd.save_index(cfg, idx)

            text = sd.summary_text(cfg, idx["sessions"]["sid-r"])
            self.assertNotIn("测试用户", text)
            self.assertIn("~", text)

            for func, args in ((sd.cmd_context, (cfg, 5, None)),
                               (sd.cmd_find, (cfg, "路径", 3, None))):
                buf = StringIO()
                with redirect_stdout(buf), redirect_stderr(buf):
                    func(*args)
                self.assertNotIn("测试用户", buf.getvalue())

    def test_vault_fallback_only_indexes_digest_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            digest = os.path.join(cfg.obsidian_dir, "2026-11", "a.md")
            note = os.path.join(cfg.obsidian_dir, "2026-11", "b.md")
            os.makedirs(os.path.dirname(digest), exist_ok=True)
            with open(digest, "w", encoding="utf-8") as f:
                f.write('---\ntitle: "测试用户标题"\nproject: 测试用户项目\n'
                        'session: "sid-d"\ntags: [自动摘要]\n---\n\n'
                        '## 关键标识符\n- 路径：`a.py`\n')
            with open(note, "w", encoding="utf-8") as f:
                f.write('---\ntitle: 私人笔记\n---\n\n路径 a.py 测试用户\n')
            docs = sd._docs_from_vault(cfg)
            self.assertEqual([d["sid"] for d in docs], ["sid-d"])
            self.assertNotIn("测试用户", json.dumps(docs, ensure_ascii=False))

    def test_model_label_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            info = sd._blank_info("claude")
            captured = {}

            def fake_call(cfg, prompt, **kwargs):
                captured["prompt"] = prompt
                return "ok", {}

            with mock.patch.object(sd, "call_llm", side_effect=fake_call):
                sd.summarize_one(cfg, info, "测试用户项目")
            self.assertNotIn("测试用户", captured["prompt"])

    def test_failure_and_traceback_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg.verbose = True
            src = os.path.join(cfg.claude_root, "proj", "a.jsonl")
            write_jsonl(src, claude_lines("sid-e", 3))
            info = sd.parse_session(src, "claude")
            task = sd.Task(info=info, label="shop", kind="claude", path=src,
                           key="sid-e", mode="full", new_offset=os.path.getsize(src))
            err = StringIO()
            with mock.patch.object(
                    sd, "call_llm",
                    side_effect=RuntimeError("boom 测试用户 sk-ant-abcdefghijklmnop")):
                with redirect_stdout(StringIO()), redirect_stderr(err):
                    self.assertFalse(sd.execute_task(cfg, task, {}))
            self.assertNotIn("测试用户", err.getvalue())
            self.assertNotIn("sk-ant-", err.getvalue())

    def test_print_plan_redacts_path_and_label(self):
        info = sd._blank_info("claude")
        info["start"] = "2026-11-01 10:00"
        info["turns"] = 6
        task = sd.Task(info=info, label="测试用户项目", kind="claude",
                       path=r"C:\Users\测试用户\proj\a.jsonl", key="x", mode="full")
        buf = StringIO()
        with redirect_stdout(buf):
            sd.print_plan([task])
        self.assertNotIn("测试用户", buf.getvalue())


class TestPrivacyScanCoverage(unittest.TestCase):
    SENTINELS = ["邵哨兵", "sk-ant-SENTINEL1234567890",
                 "sentinel@example.com", "192.168.77.88",
                 r"C:\Users\邵哨兵"]

    def setUp(self):
        patcher = mock.patch.object(
            sd, "_local_usernames", return_value=["邵哨兵"])
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_known_sentinels_do_not_appear_in_produced_artifacts(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = make_cfg(tmp)
        cfg.verbose = True
        cfg.projects = {"shop": "邵哨兵项目"}
        src = os.path.join(cfg.claude_root, "proj", "a.jsonl")
        lines = claude_lines("sid-p", 3)
        lines[0]["message"]["content"][0]["text"] = (
            "请处理 " + " ".join(self.SENTINELS) + " " + "x" * 80)
        write_jsonl(src, lines)

        sentinel_text = " ".join(self.SENTINELS)
        captured = []

        def fake_call(cfg, prompt, **kwargs):
            captured.append(prompt)
            return "## 这一段在做什么\n" + sentinel_text, {
                "input_tokens": 1, "output_tokens": 1}

        state = {}
        with mock.patch.object(sd, "call_llm", side_effect=fake_call):
            tasks, _ = sd.plan_nightly(cfg, state)
            self.assertEqual(len(tasks), 1)
            self.assertTrue(sd.execute_task(cfg, tasks[0], state))
        sd.record_failure(cfg, "sid-f", src, "boom " + sentinel_text, "claude")
        sd._run_export(cfg)

        out_buf, err_buf = StringIO(), StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            sd.cmd_find(cfg, "邵哨兵", 3, None)
            sd.cmd_context(cfg, 5, None)
        with redirect_stdout(StringIO()), redirect_stderr(err_buf):
            sd.cmd_find(cfg, "不存在的词", 3, None)

        fail_info = sd.parse_session(src, "claude")
        fail_task = sd.Task(info=fail_info, label="shop", kind="claude", path=src,
                            key="sid-boom", mode="full",
                            new_offset=os.path.getsize(src))
        trace_buf = StringIO()
        with mock.patch.object(sd, "call_llm",
                               side_effect=RuntimeError("boom " + sentinel_text)):
            with redirect_stdout(StringIO()), redirect_stderr(trace_buf):
                sd.execute_task(cfg, fail_task, {})

        tmp_target = os.path.join(cfg.out_dir, "atomic.txt")
        with mock.patch.object(sd.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sd.write_text_atomic(tmp_target, sentinel_text)
        self.assertFalse(os.path.exists(tmp_target + ".tmp"))

        backup = cfg.state_file + ".bak"
        shutil.copyfile(cfg.state_file, backup)
        lock = sd.ProcessLock(cfg.lock_file)
        self.assertTrue(lock.acquire())
        lock.release()
        lock_snapshot = os.path.join(tmp, "lock-copy")
        shutil.copyfile(cfg.lock_file, lock_snapshot)

        log_path = os.path.join(tmp, "nightly.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(out_buf.getvalue() + err_buf.getvalue())

        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        shutil.copyfile(cfg.index_file, os.path.join(repo, "index.json"))
        try:
            subprocess.run(["git", "init", "-q"], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)
            listing = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=repo, capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            # 没装 git 时退回全目录遍历，仍然扫描同一批文件
            listing = "\n".join(
                "?? " + os.path.relpath(os.path.join(dp, name), repo)
                for dp, _dirs, files in os.walk(repo) for name in files)

        scan = PrivacyScan(self.SENTINELS)
        scan.add_file("state", cfg.state_file)
        scan.add_file("index", cfg.index_file)
        scan.add_file("failures", cfg.failures_file)
        scan.add_file("memindex", os.path.join(cfg.memory_dir, "MEMORY.md"))
        scan.add_file("export_readme", os.path.join(cfg.out_dir, "README.md"))
        scan.add_tree("obsidian", cfg.obsidian_dir)
        scan.add_tree("monthly", os.path.join(cfg.memory_dir, "sessions"))
        scan.add_tree("export_doc", cfg.out_dir)
        scan.add_text("stdout", out_buf.getvalue())
        scan.add_text("stderr", err_buf.getvalue())
        scan.add_text("traceback", trace_buf.getvalue())
        scan.add_text("temp", "")            # 临时文件已确认不存在
        scan.add_file("backup", backup)
        scan.add_file("lock", lock_snapshot)
        scan.add_text("model_request", "\n".join(captured))
        scan.add_file("ignored_log", log_path)
        for line in listing.splitlines():
            rel = line[3:].strip()
            if rel:
                scan.add_file("untracked_repo", os.path.join(repo, rel))

        self.assertEqual(PrivacyScan.REQUIRED - scan.scanned, set())
        self.assertEqual(scan.leaks, [])


def _write_session(path, sid, text="hello"):
    write_jsonl(path, [{
        "type": "user", "timestamp": "2026-11-01T10:00:00Z",
        "sessionId": sid, "cwd": "C:/work/shop",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }])


def _cfg_with_root(tmp, claude_root=None, codex_root=None):
    cfg = make_cfg(tmp)
    if claude_root:
        cfg.claude_root = claude_root
    if codex_root:
        cfg.codex_root = codex_root
    return cfg


class TestRefStability(unittest.TestCase):
    """v0.5.0 ref v2：根绑定、掩码目录、恢复线索与明确失败状态。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_cfg(self.tmp)

    def _ref(self, path, sid, preferred="claude"):
        sd.ensure_ref_key(self.cfg)
        return sd.make_path_ref(self.cfg, path, preferred, sid=sid)

    def test_normal_root_reads(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-normal")
        ref = self._ref(src, "sid-normal")
        self.assertEqual(ref["v"], 2)
        self.assertEqual(ref["root"], "claude")
        self.assertNotIn(self.tmp, json.dumps(ref, ensure_ascii=False))
        res = sd.resolve_ref(self.cfg, ref)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.path, src)

    def test_root_move_needs_rebind_and_historical_ref_is_unchanged(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-move")
        ref = self._ref(src, "sid-move")
        frozen = json.loads(json.dumps(ref))

        moved = os.path.join(self.tmp, "claude-moved")
        shutil.move(self.cfg.claude_root, moved)
        cfg2 = _cfg_with_root(self.tmp, claude_root=moved)
        res = sd.resolve_ref(cfg2, ref)
        self.assertEqual(res.status, "unbound_root")
        self.assertIsNone(res.path)

        rc = sd.cmd_rebind_root(cfg2, ref["root_id"], "claude", False, False)
        self.assertEqual(rc, 0)
        self.assertEqual(ref, frozen)
        res = sd.resolve_ref(cfg2, ref)
        self.assertEqual(res.status, "ok")
        self.assertTrue(os.path.samefile(res.path, src.replace(
            self.cfg.claude_root, moved)))

        # 再整体移动一次：旧 ref -> 第一次的新 id -> 第二次的新 id
        moved2 = os.path.join(self.tmp, "claude-moved-2")
        shutil.move(moved, moved2)
        cfg3 = _cfg_with_root(self.tmp, claude_root=moved2)
        self.assertEqual(sd.resolve_ref(cfg3, ref).status, "unbound_root")
        self.assertEqual(sd.cmd_rebind_root(
            cfg3, ref["root_id"], "claude", False, False), 0)
        self.assertEqual(sd.resolve_ref(cfg3, ref).status, "ok")

    def test_changed_root_without_rebind_reports_unbound(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-unbound")
        ref = self._ref(src, "sid-unbound")
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        cfg2 = _cfg_with_root(self.tmp, claude_root=other)
        res = sd.resolve_ref(cfg2, ref)
        self.assertEqual(res.status, "unbound_root")
        self.assertEqual(res.path, None)

    def test_subdir_move_recovers_or_reports_ambiguity(self):
        src = os.path.join(self.cfg.claude_root, "proj", "sub", "a.jsonl")
        _write_session(src, "sid-sub")
        ref = self._ref(src, "sid-sub")
        dest = os.path.join(self.cfg.claude_root, "other", "a.jsonl")
        os.makedirs(os.path.dirname(dest))
        shutil.move(src, dest)
        res = sd.resolve_ref(self.cfg, ref)
        self.assertEqual(res.status, "recovered")
        self.assertTrue(res.recovered)
        self.assertEqual(res.path, dest)

        duplicate = os.path.join(self.cfg.claude_root, "other2", "a.jsonl")
        os.makedirs(os.path.dirname(duplicate))
        shutil.copyfile(dest, duplicate)
        res = sd.resolve_ref(self.cfg, ref)
        self.assertEqual(res.status, "ambiguous")
        self.assertIsNone(res.path)

    def test_renamed_source_recovers_by_sid(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-rename")
        ref = self._ref(src, "sid-rename")
        dest = os.path.join(self.cfg.claude_root, "proj", "renamed.jsonl")
        os.rename(src, dest)
        res = sd.resolve_ref(self.cfg, ref)
        self.assertEqual(res.status, "recovered")
        self.assertEqual(res.path, dest)

        with open(dest, "w", encoding="utf-8") as f:
            f.write('{"type": "user", "message": "different"}\n')
        self.assertEqual(sd.resolve_ref(self.cfg, ref).status, "missing")

    def test_deleted_source_is_missing_not_empty(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-deleted")
        ref = self._ref(src, "sid-deleted")
        os.remove(src)
        res = sd.resolve_ref(self.cfg, ref)
        self.assertEqual(res.status, "missing")
        self.assertIsNone(res.path)

    def test_same_filename_in_two_projects_never_crosses(self):
        p1 = os.path.join(self.cfg.claude_root, "proj1", "a.jsonl")
        p2 = os.path.join(self.cfg.claude_root, "proj2", "a.jsonl")
        _write_session(p1, "sid-one")
        _write_session(p2, "sid-two")
        r1 = self._ref(p1, "sid-one")
        r2 = self._ref(p2, "sid-two")
        self.assertEqual(sd.resolve_ref(self.cfg, r1).path, p1)
        self.assertEqual(sd.resolve_ref(self.cfg, r2).path, p2)

        # 两个原始路径都移走，恢复必须按 sid 选对，不能按文件名
        d1 = os.path.join(self.cfg.claude_root, "moved1", "a.jsonl")
        d2 = os.path.join(self.cfg.claude_root, "moved2", "a.jsonl")
        for d in (d1, d2):
            os.makedirs(os.path.dirname(d))
        shutil.move(p1, d1)
        shutil.move(p2, d2)
        self.assertEqual(sd.resolve_ref(self.cfg, r1).path, d1)
        self.assertEqual(sd.resolve_ref(self.cfg, r2).path, d2)

    def test_legacy_path_outside_known_roots_is_reported_not_guessed(self):
        outside = os.path.join(self.tmp, "elsewhere", "a.jsonl")
        _write_session(outside, "sid-outside")
        self.assertEqual(
            sd.ref_migration_status(self.cfg, None, outside),
            "legacy_outside_root")
        sd.save_index(self.cfg, {"version": 1, "sessions": {
            "sid-outside": {"sid": "sid-outside", "source": outside}}})
        before = _snapshot(self.tmp)
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = sd.cmd_migrate(self.cfg, dry=False)
        self.assertEqual(rc, 1)
        rec = sd.load_index(self.cfg)["sessions"]["sid-outside"]
        self.assertNotIn("source_ref", rec)
        # 不认识的旧路径不会被强行写成错误 ref
        self.assertIn("不在任何已知根目录内", buf.getvalue())
        self.assertNotEqual(_snapshot(self.tmp), before)  # 只多了密钥/锁

    def test_ambiguous_ref_is_not_downgraded_by_stale_legacy_path(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-amb-legacy")
        ref = self._ref(src, "sid-amb-legacy")
        for sub in ("other", "other2"):
            dup = os.path.join(self.cfg.claude_root, sub, "a.jsonl")
            os.makedirs(os.path.dirname(dup))
            shutil.copyfile(src, dup)
        os.remove(src)
        rec = {
            "sid": "sid-amb-legacy",
            "source_ref": ref,
            # 旧字段是已不存在的绝对路径：不能把 ambiguous 降级成 missing
            "source": os.path.join(self.tmp, "gone", "a.jsonl"),
        }
        res = sd.resolve_meta(self.cfg, rec, "source")
        self.assertEqual(res.status, "ambiguous")
        self.assertIsNone(res.path)
        self.assertGreaterEqual(len(res.candidates), 2)

    def test_legacy_redacted_path_does_not_fall_back(self):
        rec = {"sid": "sid-redacted", "file": "~/vault/2026-11/x.md"}
        res = sd.resolve_meta(self.cfg, rec, "file")
        self.assertEqual(res.status, "legacy_redacted")
        self.assertIsNone(res.path)
        text, res2 = sd.summary_result(self.cfg, rec)
        self.assertEqual(text, "")
        self.assertEqual(res2.status, "legacy_redacted")

    def test_v1_metadata_migrates_to_v2(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-migrate")
        obs = os.path.join(self.cfg.obsidian_dir, "2026-11", "x.md")
        os.makedirs(os.path.dirname(obs))
        with open(obs, "w", encoding="utf-8") as f:
            f.write('---\nsession: "sid-migrate"\n---\n\nbody\n')
        index = {"version": 1, "sessions": {"sid-migrate": {
            "sid": "sid-migrate", "file": obs, "source": src,
            "file_ref": {"root": "obsidian", "rel": "2026-11/x.md"},
            "source_ref": {"root": "claude", "rel": "proj/a.jsonl"}}}}
        state = {"sid-migrate": {"offset": 1, "source": src,
                                 "source_ref": {"root": "claude",
                                                "rel": "proj/a.jsonl"}}}
        failures = {"sid-migrate": {"attempts": 1, "path": src,
                                    "path_ref": {"root": "claude",
                                                 "rel": "proj/a.jsonl"}}}
        for path, data in ((self.cfg.index_file, index),
                           (self.cfg.state_file, state),
                           (self.cfg.failures_file, failures)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

        self.assertTrue(sd.migrate_metadata(self.cfg))
        rec = sd.load_index(self.cfg)["sessions"]["sid-migrate"]
        for ref in (rec["file_ref"], rec["source_ref"]):
            self.assertEqual(ref["v"], 2)
            self.assertIn("root_id", ref)
        self.assertNotIn(self.tmp, json.dumps(rec, ensure_ascii=False))
        self.assertEqual(sd.resolve_meta(self.cfg, rec, "file").path, obs)
        self.assertEqual(sd.resolve_meta(
            self.cfg, sd.load_state(self.cfg)["sid-migrate"], "source").path,
            src)
        self.assertFalse(sd.migrate_metadata(self.cfg))

    def test_interrupted_migration_rerun_has_no_duplicates(self):
        src = os.path.join(self.cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-crash")
        index = {"version": 1, "sessions": {"sid-crash": {
            "sid": "sid-crash", "source": src,
            "source_ref": {"root": "claude", "rel": "proj/a.jsonl"}}}}
        state = {"sid-crash": {"offset": 1, "source": src,
                               "source_ref": {"root": "claude",
                                              "rel": "proj/a.jsonl"}}}
        with open(self.cfg.index_file, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

        real_save = sd.save_index
        calls = {"n": 0}

        def flaky(cfg, data):
            if calls["n"] == 0:
                calls["n"] += 1
                raise OSError("simulated interruption")
            return real_save(cfg, data)

        with mock.patch.object(sd, "save_index", side_effect=flaky):
            with self.assertRaises(OSError):
                sd.migrate_metadata(self.cfg)

        # state 已迁移，index 还是 v1；重跑必须补齐且不产生重复
        self.assertEqual(sd.load_state(self.cfg)["sid-crash"]
                         ["source_ref"]["v"], 2)
        self.assertEqual(sd.load_index(self.cfg)["sessions"]["sid-crash"]
                         ["source_ref"]["rel"], "proj/a.jsonl")
        self.assertTrue(sd.migrate_metadata(self.cfg))
        rec = sd.load_index(self.cfg)["sessions"]["sid-crash"]
        self.assertEqual(rec["source_ref"]["v"], 2)
        self.assertEqual(set(sd.load_index(self.cfg)["sessions"]),
                         {"sid-crash"})
        self.assertEqual(set(sd.load_state(self.cfg)), {"sid-crash"})
        self.assertFalse(sd.migrate_metadata(self.cfg))


class TestRefPrivacy(unittest.TestCase):
    def test_username_directory_is_hashed_and_still_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            with mock.patch.object(sd, "_local_usernames",
                                   return_value=["testuser"]):
                src = os.path.join(cfg.claude_root,
                                   "c--Users-testuser-shop", "a.jsonl")
                _write_session(src, "sid-masked")
                sd.ensure_ref_key(cfg)
                ref = sd.make_path_ref(cfg, src, "claude", sid="sid-masked")
                self.assertEqual([p.get("n") for p in ref["parts"]],
                                 [None, "a.jsonl"])
                self.assertIn("h", ref["parts"][0])
                blob = json.dumps(ref, ensure_ascii=False)
                self.assertNotIn("testuser", blob)
                self.assertNotIn("c--Users", blob)
                res = sd.resolve_ref(cfg, ref)
                self.assertEqual(res.status, "ok")
                self.assertEqual(res.path, src)

                # 同一目录名 token 稳定，不同目录名 token 不同
                token = sd._part_token(sd._load_ref_key(cfg), "c--Users-testuser-shop")
                self.assertEqual(token, ref["parts"][0]["h"])
                self.assertNotEqual(
                    token, sd._part_token(sd._load_ref_key(cfg), "other-shop"))
                self.assertNotIn("testuser", token)

    def test_key_file_is_written_verbatim_in_binary_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            # 含 \n 的密钥：Windows 文本模式会把 \n 扩成 \r\n，必须按二进制写
            key = b"\n" * 32
            with mock.patch.object(sd.os, "urandom", return_value=key):
                self.assertEqual(sd.ensure_ref_key(cfg), key)
            with open(sd._ref_key_path(cfg), "rb") as f:
                self.assertEqual(f.read(), key)
            self.assertEqual(sd._load_ref_key(cfg), key)

    def test_missing_key_breaks_v2_but_does_not_create_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            src = os.path.join(cfg.claude_root, "proj", "a.jsonl")
            _write_session(src, "sid-key")
            sd.ensure_ref_key(cfg)
            ref = sd.make_path_ref(cfg, src, "claude", sid="sid-key")
            os.remove(sd._ref_key_path(cfg))
            res = sd.resolve_ref(cfg, ref)
            self.assertEqual(res.status, "key_missing")
            self.assertFalse(os.path.exists(sd._ref_key_path(cfg)))
            self.assertFalse(os.path.exists(sd._roots_path(cfg)))

    def test_masked_component_scan_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            with mock.patch.object(sd, "_local_usernames",
                                   return_value=["testuser"]):
                src = os.path.join(cfg.claude_root,
                                   "c--Users-testuser-shop", "a.jsonl")
                _write_session(src, "sid-cache")
                sd.ensure_ref_key(cfg)
                ref = sd.make_path_ref(cfg, src, "claude", sid="sid-cache")
                with mock.patch.object(sd.os, "listdir",
                                       wraps=os.listdir) as listed:
                    sd.resolve_ref(cfg, ref)
                    first = listed.call_count
                    sd.resolve_ref(cfg, ref)
                    self.assertEqual(listed.call_count, first)
                self.assertGreaterEqual(first, 1)

    def test_recovery_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            src = os.path.join(cfg.claude_root, "proj", "a.jsonl")
            _write_session(src, "sid-limit")
            sd.ensure_ref_key(cfg)
            ref = sd.make_path_ref(cfg, src, "claude", sid="sid-limit")
            os.remove(src)
            for i in range(3):
                _write_session(os.path.join(
                    cfg.claude_root, f"p{i}", "b.jsonl"), "sid-limit")
            with mock.patch.object(sd, "_RECOVERY_MAX_FILES", 1):
                self.assertEqual(sd.resolve_ref(cfg, ref).status,
                                 "recovery_limited")

    def test_binding_chain_and_cycle(self):
        a, b, c = "r2_" + "a" * 32, "r2_" + "b" * 32, "r2_" + "c" * 32
        self.assertTrue(sd._follow_binding({a: b, b: c}, a, c))
        self.assertFalse(sd._follow_binding({a: b, b: a}, a, c))
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            sd.ensure_ref_key(cfg)
            with mock.patch.object(sd, "_root_id_for_path", return_value=c):
                sd.save_root_bindings(cfg, {c: b})
                rc = sd.cmd_rebind_root(cfg, b, "claude", False, False)
                self.assertEqual(rc, 1)

    def test_repair_ref_writes_only_v2_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            src = os.path.join(cfg.claude_root, "proj", "a.jsonl")
            _write_session(src, "sid-repair")
            index = {"version": 1, "sessions": {"sid-repair": {
                "sid": "sid-repair", "source": "~/a.jsonl"}}}
            with open(cfg.index_file, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False)
            before = _snapshot(tmp)
            self.assertEqual(sd.cmd_repair_ref(
                cfg, "sid-repair", src, None, True), 0)
            self.assertEqual(_snapshot(tmp), before)
            self.assertEqual(sd.cmd_repair_ref(
                cfg, "sid-repair", src, None, False), 0)
            rec = sd.load_index(cfg)["sessions"]["sid-repair"]
            self.assertEqual(rec["source_ref"]["v"], 2)
            self.assertNotIn(tmp, json.dumps(rec, ensure_ascii=False))
            self.assertEqual(sd.resolve_meta(cfg, rec, "source").path, src)


class TestRefReadOnly(unittest.TestCase):
    def _legacy_setup(self, tmp):
        cfg = make_cfg(tmp)
        src = os.path.join(cfg.claude_root, "proj", "a.jsonl")
        _write_session(src, "sid-ro")
        obs = os.path.join(cfg.obsidian_dir, "2026-11", "x.md")
        os.makedirs(os.path.dirname(obs))
        with open(obs, "w", encoding="utf-8") as f:
            f.write('---\ntitle: "x"\nproject: p\nsession: "sid-ro"\n---\n\n'
                    '## 这一段在做什么\n\nhello\n')
        index = {"version": 1, "sessions": {"sid-ro": {
            "sid": "sid-ro", "date": "2026-11-01", "project": "p", "title": "x",
            "source": src, "file": obs,
            "file_ref": {"root": "obsidian", "rel": "2026-11/x.md"},
            "source_ref": {"root": "claude", "rel": "proj/a.jsonl"}}}}
        with open(cfg.index_file, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        return cfg

    def test_find_and_context_never_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._legacy_setup(tmp)
            before = _snapshot(tmp)
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                sd.cmd_find(cfg, "hello", 3, None)
                sd.cmd_context(cfg, 5, None)
            self.assertEqual(_snapshot(tmp), before)
            self.assertFalse(os.path.exists(sd._ref_key_path(cfg)))
            self.assertFalse(os.path.exists(sd._roots_path(cfg)))
            self.assertFalse(os.path.exists(cfg.lock_file))
            self.assertIn("尚未迁移", err.getvalue())

    def test_migrate_dry_run_and_real_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._legacy_setup(tmp)
            before = _snapshot(tmp)
            self.assertEqual(sd.cmd_migrate(cfg, True), 0)
            self.assertEqual(_snapshot(tmp), before)
            self.assertFalse(os.path.exists(sd._ref_key_path(cfg)))
            self.assertEqual(sd.cmd_migrate(cfg, False), 0)
            self.assertTrue(os.path.exists(sd._ref_key_path(cfg)))
            self.assertEqual(sd.load_index(cfg)["sessions"]["sid-ro"]
                             ["file_ref"]["v"], 2)
            after = _snapshot(tmp)
            self.assertEqual(sd.cmd_migrate(cfg, False), 0)
            self.assertEqual(_snapshot(tmp), after)

    def test_roots_lists_bindings_without_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._legacy_setup(tmp)
            sd.cmd_migrate(cfg, False)
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                rc = sd.cmd_roots(cfg)
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("claude", text)
            self.assertIn("refs=", text)
            self.assertIn("r2_", text)
            self.assertNotIn(tmp, text)

    def test_find_reports_unresolved_instead_of_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            index = {"version": 1, "sessions": {"sid-bad": {
                "sid": "sid-bad", "date": "2026-11-01", "project": "p",
                "title": "x", "file": "~/vault/missing.md"}}}
            with open(cfg.index_file, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False)
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = sd.cmd_find(cfg, "missing", 3, None)
            self.assertEqual(rc, 1)
            self.assertIn("无法定位", err.getvalue())
            self.assertIn("legacy_redacted", err.getvalue())

    def test_unresolved_summary_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            index = {"version": 1, "sessions": {"sid-bad": {
                "sid": "sid-bad", "date": "2026-11-01", "project": "p",
                "title": "x", "file": "~/vault/missing.md"}}}
            with open(cfg.index_file, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False)
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                rc = sd.cmd_context(cfg, 5, None)
            self.assertEqual(rc, 1)
            self.assertIn("legacy_redacted", out.getvalue())
            self.assertNotIn("（摘要文件缺失）", out.getvalue())


class TestRepoHygiene(unittest.TestCase):
    def test_license_and_readme_privacy_wording(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "LICENSE"), encoding="utf-8") as f:
            self.assertIn("Copyright (c) 2026 Scannef", f.read())
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("风险降低措施，不是安全边界", readme)
        for bad in ("零泄漏", "绝对安全", "保证不含敏感信息"):
            self.assertNotIn(bad, readme)


class TestVerifyMatching(unittest.TestCase):
    """命令比对只认规范化后 argv 完全相同；参数不同必须 ⚠≠。"""

    def _status(self, candidate, source):
        return sd.compare_argv(candidate, source)[0]

    def _reason(self, candidate, source):
        return sd.compare_argv(candidate, source)[1]

    def test_ok_cases(self):
        cases = [
            ("pytest tests/test_export.py -q",
             "python -m pytest tests/test_export.py -q"),
            ("pytest -q tests/test_export.py",
             "pytest tests/test_export.py -q"),
            ('pytest "tests/a b.py"', "pytest 'tests/a b.py'"),
            ("python C:\\work\\a.py --x", "python C:/work/a.py --x"),
            ('bash -c "pytest -q"', "pytest -q"),
        ]
        for candidate, source in cases:
            with self.subTest(candidate=candidate, source=source):
                self.assertEqual(self._status(candidate, source), "ok")

    def test_conflict_cases(self):
        cases = [
            ("pytest", "pytest tests/test_export.py -q", "omitted-args"),
            ("pytest tests/test_export.py",
             "pytest tests/test_export.py -q", "flags-differ"),
            ("pytest tests -v", "pytest tests -q", "flags-differ"),
            ("git push origin main", "git push origin feature",
             "value-conflict"),
            ("git push", "git push origin main", "omitted-args"),
            ("npm run build", "npm run build:prod", "subcommand-conflict"),
            ("pip install requests", "pip install requests==2.31",
             "value-conflict"),
            ("pytest --port 8080", "pytest --port 8081", "value-conflict"),
            ("pytest -v -v", "pytest -v", "flags-differ"),
            ("pytest tests/...", "pytest tests/test_export.py -q",
             "abbreviated"),
        ]
        for candidate, source, reason in cases:
            with self.subTest(candidate=candidate, source=source):
                self.assertEqual(self._status(candidate, source), "conflict")
                self.assertEqual(self._reason(candidate, source), reason)

    def test_program_differs_is_not_found(self):
        self.assertEqual(self._status("rm -rf build", "pytest tests -q"),
                         "not_found")
        self.assertEqual(self._status("make deploy", "npm run build"),
                         "not_found")

    def test_ok_wins_over_earlier_conflict(self):
        info = sd._blank_info("claude")
        info["command_events"] = [
            {"cmd": "pytest tests -q", "tool_use_id": "t1",
             "channel": "tool_use", "result": "", "is_error": None,
             "success_evidence": False},
            {"cmd": "pytest tests", "tool_use_id": "t2",
             "channel": "tool_use", "result": "", "is_error": None,
             "success_evidence": False},
        ]
        status, reason = sd.match_command("pytest tests", info)[:2]
        self.assertEqual(status, "ok")
        self.assertEqual(reason, "")

    def test_prompt_only_command_is_not_executed(self):
        info = sd._blank_info("claude")
        info["prompts"] = ["先跑 pytest tests -q 看看"]
        status, _reason, _ev, level = sd.match_command("pytest tests -q", info)
        self.assertEqual(status, "not_executed")
        self.assertEqual(level, "mentioned")


class TestVerifyEvidenceLevels(unittest.TestCase):
    def _info(self, result="", is_error=None, success=None):
        info = sd._blank_info("claude")
        info["command_events"] = [{
            "cmd": "pytest tests -q", "tool_use_id": "t1",
            "channel": "tool_use", "result": result,
            "is_error": is_error,
            "success_evidence": (sd.success_evidence(result)
                                 if success is None else success)}]
        return info

    def test_mentioned_vs_executed_vs_succeeded(self):
        info = sd._blank_info("claude")
        info["prompts"] = ["pytest tests -q"]
        self.assertEqual(sd.match_command("pytest tests -q", info)[3],
                         "mentioned")

        info = self._info()
        self.assertEqual(sd.match_command("pytest tests -q", info)[3],
                         "executed")

        info = self._info("All tests passed")
        self.assertEqual(sd.match_command("pytest tests -q", info)[3],
                         "succeeded")

    def test_generic_output_is_not_success_evidence(self):
        for text in ("", "done", "已修复", "看起来没问题"):
            with self.subTest(text=text):
                self.assertFalse(sd.success_evidence(text))

    def test_explicit_success_signals(self):
        for text in ("exit code 0", "Process exited with code 0",
                     '"success": true', "All tests passed", "3 passed"):
            with self.subTest(text=text):
                self.assertTrue(sd.success_evidence(text))

    def test_mixed_pass_and_fail_is_not_success(self):
        self.assertFalse(sd.success_evidence("3 passed, 1 failed"))

    def test_error_flag_beats_success_text(self):
        info = self._info("All tests passed", is_error=True)
        self.assertEqual(sd.match_command("pytest tests -q", info)[3],
                         "executed")

    def test_result_verified_is_never_assigned(self):
        info = self._info("All tests passed")
        levels = {sd.match_command("pytest tests -q", info)[3] for _ in range(5)}
        self.assertNotIn("result_verified", levels)


class TestVerifyRegressionH1H2(unittest.TestCase):
    """H1：助手正文不算事实；H2：续写不能把旧摘要当原文。"""

    def test_assistant_text_is_not_a_source(self):
        info = sd._blank_info("claude")
        info["assistant"] = ["我看过了 /a/b.py，测试通过"]
        text = "## 关键标识符\n- 路径：`/a/b.py`\n"
        out = sd.verify_summary(text, info)
        self.assertIn("`/a/b.py` ⚠", out)

    def test_assistant_command_is_not_executed(self):
        info = sd._blank_info("claude")
        info["assistant"] = ["可以运行 pytest tests -q"]
        status, _reason, _ev, _level = sd.match_command("pytest tests -q",
                                                        info)
        self.assertEqual(status, "not_found")

    def test_prompt_text_is_source_for_paths(self):
        info = sd._blank_info("claude")
        info["prompts"] = ["改 /a/b.py"]
        text = "## 关键标识符\n- 路径：`/a/b.py`\n"
        self.assertNotIn("⚠", sd.verify_summary(text, info))

    def test_previous_summary_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            info = sd._blank_info("claude")
            info["prompts"] = ["继续改别的东西"]
            prev = "## 关键标识符\n- 路径：`/old/verified.py`\n"
            text = "### 关键标识符\n- 路径：`/old/verified.py`\n"
            out = sd.checked_summary(cfg, text, info, prev)
            self.assertIn("`/old/verified.py` ⚠", out)

    def test_previous_warning_is_carried_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            info = sd._blank_info("claude")
            prev = "## 关键标识符\n- 路径：`/old/broken.py` ⚠\n"
            text = "### 关键标识符\n- 路径：`/old/broken.py`\n"
            out = sd.checked_summary(cfg, text, info, prev)
            self.assertIn("`/old/broken.py` ⚠", out)

    def test_carried_warning_clears_when_delta_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            info = sd._blank_info("claude")
            info["prompts"] = ["又看了 /old/broken.py"]
            prev = "## 关键标识符\n- 路径：`/old/broken.py` ⚠\n"
            text = "### 关键标识符\n- 路径：`/old/broken.py`\n"
            out = sd.checked_summary(cfg, text, info, prev)
            self.assertNotIn("⚠", out)


class TestVerifyOutput(unittest.TestCase):
    def _info(self, cmd="pytest tests -q", result="", is_error=None):
        info = sd._blank_info("claude")
        info["command_events"] = [{
            "cmd": cmd, "tool_use_id": "t1", "channel": "tool_use",
            "result": result, "is_error": is_error,
            "success_evidence": sd.success_evidence(result)}]
        return info

    def test_legend_inserted_once(self):
        info = self._info()
        text = "## 关键标识符\n- 命令：`pytest`\n"
        out = sd.verify_summary(text, info)
        self.assertIn("校验图例", out)
        self.assertIn("⚠≠", out)
        self.assertEqual(out.count("校验图例"), 1)
        self.assertEqual(sd.verify_summary(out, info).count("校验图例"), 1)

    def test_conflict_uses_near_marker(self):
        info = self._info()
        text = "## 关键标识符\n- 命令：`pytest tests -q -v`\n"
        out = sd.verify_summary(text, info)
        self.assertIn("`pytest tests -q -v` ⚠≠", out)

    def test_missing_command_uses_plain_marker(self):
        info = self._info()
        text = "## 关键标识符\n- 命令：`make deploy`\n"
        out = sd.verify_summary(text, info)
        self.assertIn("`make deploy` ⚠", out)

    def test_outcome_claim_without_success_evidence(self):
        info = self._info()
        text = "## 走过的路\n跑到最后 `pytest tests -q` 测试通过。\n"
        out = sd.verify_summary(text, info)
        self.assertIn("⚠?", out)

    def test_outcome_claim_with_success_evidence_is_clean(self):
        info = self._info(result="All tests passed")
        text = "## 走过的路\n跑到最后 `pytest tests -q` 测试通过。\n"
        out = sd.verify_summary(text, info)
        self.assertNotIn("⚠?", out)

    def test_no_verify_skips_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            cfg.verify = False
            info = self._info()
            text = "## 关键标识符\n- 命令：`make deploy`\n"
            self.assertEqual(sd.checked_summary(cfg, text, info), text)

    def test_markers_have_no_duplicates(self):
        info = self._info()
        text = "## 关键标识符\n- 命令：`make deploy`\n"
        once = sd.verify_summary(text, info)
        twice = sd.verify_summary(once, info)
        self.assertEqual(once, twice)


class TestCommandEventsParsing(unittest.TestCase):
    def test_claude_tool_use_and_result_correlate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.jsonl")
            write_jsonl(p, [
                {"type": "user", "sessionId": "s", "message": {"role": "user",
                 "content": [{"type": "text", "text": "跑测试"}]}},
                {"type": "assistant", "message": {"role": "assistant",
                 "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                              "input": {"command": "pytest tests -q"}}]}},
                {"type": "user", "message": {"role": "user",
                 "content": [{"type": "tool_result", "tool_use_id": "t1",
                              "is_error": False,
                              "content": "All tests passed"}]}},
            ])
            info = sd.parse_claude(p)
            self.assertEqual(info["commands"], ["pytest tests -q"])
            self.assertEqual(len(info["command_events"]), 1)
            ev = info["command_events"][0]
            self.assertEqual(ev["tool_use_id"], "t1")
            self.assertTrue(ev["success_evidence"])
            self.assertEqual(ev["result"], "All tests passed")

    def test_codex_call_output_correlates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.jsonl")
            write_jsonl(p, [
                {"payload": {"type": "function_call", "name": "shell",
                             "call_id": "c1",
                             "arguments": json.dumps(
                                 {"command": "npm run build"})}},
                {"payload": {"type": "function_call_output", "call_id": "c1",
                             "output": "Process exited with code 0"}},
            ])
            info = sd.parse_codex(p)
            self.assertEqual(len(info["command_events"]), 1)
            self.assertTrue(info["command_events"][0]["success_evidence"])


FULL_SUMMARY = """## 这一段在做什么
保留导出逻辑。

## 走过的路
决定保留 `/a/keep.py`，因为兼容旧客户端。

## 关键标识符
- 路径：`/a/keep.py`
- 命令：`pytest tests -q`

## 还没完的事
- 大文件内存占用未测
"""


class TestIncrementalReliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_cfg(self.tmp)
        os.makedirs(self.cfg.obsidian_dir, exist_ok=True)
        os.makedirs(self.cfg.memory_dir, exist_ok=True)
        self.cfg.min_delta_turns = 1
        self.cfg.min_delta_chars = 1

    def _seed(self, sid="sid-inc", pairs=3, start_minute=0):
        path = os.path.join(self.cfg.claude_root, "proj", f"{sid}.jsonl")
        write_jsonl(path, claude_lines(sid, pairs, start_minute=start_minute))
        return path

    def _append(self, path, sid, pairs=3, start_minute=10):
        with open(path, "a", encoding="utf-8") as f:
            for r in claude_lines(sid, pairs, start_minute=start_minute):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _state_for(self, path, sid, offset, sections=0):
        return {sid: {"offset": offset,
                      "prefix_sha1": sd.prefix_sha1(path, offset),
                      "sections": sections}}

    def _full(self, path, sid, summary=FULL_SUMMARY, state=None):
        info = sd.parse_session(path, "claude")
        task = sd.Task(info=info, label="shop", kind="claude", path=path,
                       key=sid, mode="full", start_offset=0,
                       new_offset=os.path.getsize(path))
        state = state if state is not None else {}
        with mock.patch.object(
                sd, "call_llm",
                return_value=(summary, {"input_tokens": 1, "output_tokens": 1})):
            ok = sd.execute_task(self.cfg, task, state)
        self.assertTrue(ok)
        return state

    def _doc(self, sid):
        path = sd.obsidian_path(self.cfg, {"start": "2026-11-01 10:00"}, sid)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_crash_after_write_does_not_duplicate_continuation(self):
        sid = "sid-crash"
        path = self._seed(sid)
        state = self._full(path, sid)
        offset = os.path.getsize(path)
        self._append(path, sid)
        tasks, _ = sd.plan_nightly(self.cfg, state)
        self.assertEqual(tasks[0].mode, "incremental")
        same_offset = tasks[0].new_offset

        calls = {"n": 0}
        real_save = sd.save_state

        def flaky(cfg, st):
            if calls["n"] == 0:
                calls["n"] += 1
                raise OSError("crash before state")
            real_save(cfg, st)

        with mock.patch.object(
                sd, "call_llm",
                side_effect=[("## 续 · 一\n\n第一次", {"output_tokens": 1}),
                             ("## 续 · 二\n\n第二次", {"output_tokens": 1})]), \
                mock.patch.object(sd, "save_state", side_effect=flaky):
            self.assertFalse(sd.execute_task(self.cfg, tasks[0], state))

        # 重跑：offset 相同，续段标记已存在，不能再追加一段
        state2 = sd.load_state(self.cfg)
        tasks2, _ = sd.plan_nightly(self.cfg, state2)
        self.assertEqual(tasks2[0].new_offset, same_offset)
        with mock.patch.object(
                sd, "call_llm",
                return_value=("## 续 · 二\n\n第二次", {"output_tokens": 1})):
            self.assertTrue(sd.execute_task(self.cfg, tasks2[0], state2))

        doc = self._doc(sid)
        self.assertEqual(doc.count("<!-- sd:cont key="), 1)
        self.assertEqual(doc.count("## 续"), 1)
        self.assertEqual(sd.load_state(self.cfg)[sid]["sections"], 1)

    def test_three_appends_then_merge(self):
        sid = "sid-multi"
        path = self._seed(sid)
        state = self._full(path, sid)
        last_offset = state[sid]["offset"]
        for i in range(3):
            self._append(path, sid, start_minute=10 + i * 10)
            tasks, _ = sd.plan_nightly(self.cfg, state)
            self.assertEqual(tasks[0].mode, "incremental")
            self._run_incremental(tasks[0], state, f"## 续 · {i}\n\n第 {i} 段")
            self.assertGreater(state[sid]["offset"], last_offset)
            last_offset = state[sid]["offset"]

        doc = self._doc(sid)
        self.assertEqual(doc.count("## 续"), 3)
        self.assertIn("/a/keep.py", doc)          # 旧事实仍在
        self.assertEqual(state[sid]["sections"], 3)
        prev = sd.find_previous_summary(self.cfg, sid)
        self.assertNotIn("sd:cont", prev)

        tasks, _ = sd.plan_nightly(self.cfg, state)
        self.assertEqual(tasks[0].mode, "merge")

    def _run_incremental(self, task, state, text):
        with mock.patch.object(
                sd, "call_llm",
                return_value=(text, {"input_tokens": 1, "output_tokens": 1})):
            return sd.execute_task(self.cfg, task, state)

    def test_truncation_does_not_advance_offset(self):
        sid = "sid-trunc"
        path = self._seed(sid)
        state = self._full(path, sid)
        old_offset = state[sid]["offset"]
        self._append(path, sid)
        tasks, _ = sd.plan_nightly(self.cfg, state)
        task = tasks[0]
        doc_path = sd.obsidian_path(self.cfg, {"start": "2026-11-01 10:00"},
                                    sid)
        with open(doc_path, "rb") as f:
            before = f.read()
        with mock.patch.object(
                sd, "call_llm",
                side_effect=RuntimeError("模型输出被截断，max_tokens 已达上限")):
            self.assertFalse(sd.execute_task(self.cfg, task, state))
        self.assertEqual(state[sid]["offset"], old_offset)
        with open(doc_path, "rb") as f:
            self.assertEqual(f.read(), before)
        fails = sd.load_failures(self.cfg)
        self.assertEqual(fails[sid]["attempts"], 1)

        tasks2, _ = sd.plan_nightly(self.cfg, sd.load_state(self.cfg))
        self.assertEqual(tasks2[0].mode, "incremental")
        self.assertEqual(tasks2[0].start_offset, old_offset)

    def test_merge_error_leaves_file_and_sections_unchanged(self):
        sid = "sid-merge-err"
        path = self._seed(sid)
        state = self._full(path, sid)
        state[sid]["sections"] = 3
        doc_path = sd.obsidian_path(self.cfg, {"start": "2026-11-01 10:00"},
                                    sid)
        with open(doc_path, "rb") as f:
            before = f.read()
        task = sd.Task(info=sd.parse_session(path, "claude"), label="shop",
                       kind="claude", path=path, key=sid, mode="merge",
                       start_offset=state[sid]["offset"],
                       new_offset=state[sid]["offset"],
                       prev_summary=sd.find_previous_summary(self.cfg, sid),
                       sections=3)
        with mock.patch.object(sd, "call_llm",
                               side_effect=RuntimeError("endpoint down")):
            self.assertFalse(sd.execute_task(self.cfg, task, state))
        with open(doc_path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(state[sid]["sections"], 3)

    def test_merge_rejects_conclusion_loss_three_times(self):
        sid = "sid-merge-loss"
        path = self._seed(sid)
        state = self._full(path, sid)
        state[sid]["sections"] = 3
        sd.save_state(self.cfg, state)
        doc_path = sd.obsidian_path(self.cfg, {"start": "2026-11-01 10:00"},
                                    sid)
        with open(doc_path, "rb") as f:
            before = f.read()

        lossy = ("## 这一段在做什么\n保留了导出逻辑。\n\n"
                 "## 走过的路\n做了一些调整。\n\n"
                 "## 关键标识符\n- 路径：`/a/other.py`\n\n"
                 "## 还没完的事\n- 大文件内存占用未测\n")

        for attempt in range(3):
            st = sd.load_state(self.cfg)
            task = sd.Task(info=sd.parse_session(path, "claude"), label="shop",
                           kind="claude", path=path, key=sid, mode="merge",
                           start_offset=st[sid]["offset"],
                           new_offset=st[sid]["offset"],
                           prev_summary=sd.find_previous_summary(self.cfg, sid),
                           sections=int(st[sid].get("sections", 0)))
            with mock.patch.object(
                    sd, "call_llm",
                    return_value=(lossy, {"output_tokens": 1})):
                ok = sd.execute_task(self.cfg, task, st)
            with open(doc_path, "rb") as f:
                self.assertEqual(f.read(), before)
            if attempt < 2:
                self.assertFalse(ok)
                self.assertIn("/a/keep.py",
                              sd.load_failures(self.cfg)[sid]["error"])
            else:
                self.assertTrue(ok)

        final = sd.load_state(self.cfg)[sid]
        self.assertEqual(final["sections"], 0)
        self.assertEqual(final.get("merge_rejections", 0), 0)

    def test_lines_appended_during_call_are_processed_next_round(self):
        sid = "sid-append"
        path = self._seed(sid)
        state = self._full(path, sid)
        self._append(path, sid)
        tasks, _ = sd.plan_nightly(self.cfg, state)
        task = tasks[0]
        pre_call = os.path.getsize(path)

        def llm(cfg, prompt):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"type": "user", "sessionId": sid,
                     "message": {"role": "user",
                                 "content": [{"type": "text",
                                              "text": "调用期间新增"}]}},
                    ensure_ascii=False) + "\n")
            return "## 续 · 调用期间", {"output_tokens": 1}

        with mock.patch.object(sd, "call_llm", side_effect=llm):
            self.assertTrue(sd.execute_task(self.cfg, task, state))
        self.assertEqual(state[sid]["offset"], pre_call)
        tasks2, _ = sd.plan_nightly(self.cfg, state)
        self.assertEqual(tasks2[0].mode, "incremental")
        self.assertEqual(tasks2[0].start_offset, pre_call)

    def test_offset_inside_a_line_is_aligned_forward(self):
        sid = "sid-align"
        path = self._seed(sid, pairs=3)
        with open(path, "rb") as f:
            lines = f.read().split(b"\n")
        offset = len(lines[0]) + 1 + 5      # 第一条换行后、第二条中间
        info, new_offset = sd.parse_incremental(path, "claude", offset)
        self.assertEqual(new_offset, os.path.getsize(path))
        self.assertEqual(info["turns"], 4)  # 丢掉残行，后 4 条完整记录
        self.assertEqual(len(info["prompts"]), 2)

    def test_digest_facts_do_not_store_raw_secrets(self):
        sid = "sid-secret"
        path = self._seed(sid)
        summary = ("## 关键标识符\n"
                   "- token：`sk-ant-api03-abcdefghijklmnop`\n\n"
                   "## 还没完的事\n- 无\n")
        self._full(path, sid, summary=summary)
        with open(self.cfg.state_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("sk-ant-api03", raw)

    def test_merge_text_with_hr_and_heading_does_not_split_block(self):
        sid_a, sid_b = "sid-block-a", "sid-block-b"
        info_a = sd._blank_info("claude")
        info_a.update({"session": sid_a, "start": "2026-11-01 10:00",
                       "turns": 4, "prompts": ["A"]})
        info_b = sd._blank_info("claude")
        info_b.update({"session": sid_b, "start": "2026-11-01 11:00",
                       "turns": 4, "prompts": ["B"]})
        sd.write_full(self.cfg, info_a, "shopA", "old A", sid_a)
        sd.write_full(self.cfg, info_b, "shopB", "old B", sid_b)
        merged = "## 这一段在做什么\n混排。\n\n---\n\n## 续 · 2026-11-02\n新段。"
        sd.write_full(self.cfg, info_a, "shopA", merged, sid_a)

        mpath = os.path.join(self.cfg.memory_dir, "sessions", "2026-11.md")
        with open(mpath, encoding="utf-8") as f:
            raw = f.read()
        self.assertEqual(raw.count("session: `"), 2)
        self.assertIn("old B", raw)
        span = sd.find_monthly_block(raw, sid_a)
        self.assertIsNotNone(span)
        block = raw[span[0]:span[1]]
        self.assertIn("## 续 · 2026-11-02", block)
        self.assertIn("---", block)


class TestLLMTruncation(unittest.TestCase):
    def _cfg(self, tmp):
        cfg = make_cfg(tmp)
        cfg.api_format = "anthropic"
        cfg.model = "test-model"
        return cfg

    def test_nonempty_truncated_is_not_accepted(self):
        partial = {"content": [{"type": "text", "text": "半截摘要"}],
                   "usage": {}, "stop_reason": "max_tokens"}
        with tempfile.TemporaryDirectory() as tmp, \
                fake_api([(200, partial), (200, ANTHROPIC_OK)]) as srv:
            cfg = self._cfg(tmp)
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            text, _ = sd.call_llm(cfg, "写摘要", max_tokens=100)
            self.assertEqual(text, "摘要正文")
            self.assertEqual([r["body"]["max_tokens"] for r in srv.requests],
                             [100, 200])

    def test_truncated_at_cap_raises(self):
        partial = {"content": [{"type": "text", "text": "半截摘要"}],
                   "usage": {}, "stop_reason": "max_tokens"}
        with tempfile.TemporaryDirectory() as tmp, \
                fake_api([(200, partial)]) as srv:
            cfg = self._cfg(tmp)
            cfg.base_url = f"http://127.0.0.1:{srv.server_port}"
            with self.assertRaises(RuntimeError) as cm:
                sd.call_llm(cfg, "写摘要", max_tokens=32000)
            self.assertIn("截断", str(cm.exception))
            self.assertEqual(len(srv.requests), 1)


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture_file(name, filename):
    return os.path.join(FIXTURES, name, filename)


def load_fixture_json(name, filename):
    with open(fixture_file(name, filename), encoding="utf-8") as f:
        return json.load(f)


def install_claude_fixture(cfg, name, limit=None):
    src = fixture_file(name, "session.jsonl")
    dst = os.path.join(cfg.claude_root, "shop", "session.jsonl")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        lines = f.readlines()
    with open(dst, "w", encoding="utf-8") as f:
        f.writelines(lines[:limit] if limit else lines)
    return dst


def install_codex_fixture(cfg, name, limit=None):
    src = fixture_file(name, "session.jsonl")
    dst = os.path.join(cfg.codex_root, "pet", "session.jsonl")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        lines = f.readlines()
    with open(dst, "w", encoding="utf-8") as f:
        f.writelines(lines[:limit] if limit else lines)
    return dst


def read_fixture_lines(name):
    with open(fixture_file(name, "session.jsonl"), encoding="utf-8") as f:
        return f.readlines()


def run_with_replay(cfg, replay_obj):
    with mock.patch.object(sd, "call_llm", new=replay_obj):
        return sd._run_nightly(cfg, dry=False)


class TestGoldenFixtures(unittest.TestCase):
    def test_all_replays_validate_against_prompt_version(self):
        for name in ("basic_claude", "incremental_codex",
                     "adversarial_merge"):
            data = replay.load_replay(fixture_file(name, "llm_replay.json"))
            self.assertEqual(data["prompt_version"], sd.PROMPT_VERSION)
            self.assertTrue(data["entries"])
            for entry in data["entries"]:
                self.assertIn(entry["mode"],
                              ("full", "incremental", "merge"))

    def test_expected_files_are_json(self):
        for name in ("basic_claude", "incremental_codex",
                     "adversarial_merge"):
            expected = load_fixture_json(name, "expected.json")
            self.assertIsInstance(expected.get("expected_facts")
                                  or expected.get("expected_full_facts"), dict)
            self.assertIn("forbidden_strings", expected)

    def test_prompt_version_mismatch_is_rejected(self):
        data = load_fixture_json("basic_claude", "llm_replay.json")
        data["prompt_version"] = sd.PROMPT_VERSION - 1
        with self.assertRaises(replay.ReplayError):
            replay.validate_replay(data)

    def test_basic_recorded_response_matches_reviewed_snapshot(self):
        data = replay.load_replay(fixture_file("basic_claude",
                                               "llm_replay.json"))
        self.assertEqual(data["kind"], "recorded")
        self.assertTrue(data["model"])
        self.assertTrue(data["recorded_at"])
        with open(fixture_file("basic_claude", "expected_summary.md"),
                  encoding="utf-8") as f:
            reviewed = f.read()
        self.assertEqual(data["entries"][0]["response"].strip(),
                         reviewed.strip())


class TestGoldenPipeline(unittest.TestCase):
    def test_basic_claude_full_pipeline_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            path = install_claude_fixture(cfg, "basic_claude")
            info = sd.parse_session(path, "claude")
            key = sd.session_key(info, path)
            replay_obj = replay.ReplayLLM(
                fixture_file("basic_claude", "llm_replay.json"))

            self.assertEqual(run_with_replay(cfg, replay_obj), 0)

            state = sd.load_state(cfg)
            entry = state[key]
            self.assertEqual(entry["offset"], os.path.getsize(path))
            self.assertEqual(entry["sections"], 0)
            self.assertIn("digest_facts", entry)
            self.assertEqual(len(replay_obj.calls), 1)

            summary = open(sd.obsidian_path(cfg, info, key),
                           encoding="utf-8").read()
            expected = load_fixture_json("basic_claude", "expected.json")
            score = replay.score_summary(summary, expected)
            self.assertTrue(score["ok"], score)
            self.assertEqual(score["fabricated"], [])
            self.assertEqual(score["foreign"], [])
            concl = replay.check_conclusions(summary, expected)
            self.assertTrue(concl["ok"], concl)
            names = [n for n, _ in sd._summary_sections(summary)]
            for heading in ("这一段在做什么", "走过的路", "关键标识符",
                            "还没完的事"):
                self.assertTrue(any(heading in n for n in names), names)
            self.assertNotIn("⚠", summary)
            self.assertNotIn("助手提到", summary)

            index = sd.load_index(cfg)
            self.assertEqual(index["sessions"][key]["prompt_version"],
                             sd.PROMPT_VERSION)

    def test_basic_claude_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            path = install_claude_fixture(cfg, "basic_claude")
            info = sd.parse_session(path, "claude")
            key = sd.session_key(info, path)
            replay_obj = replay.ReplayLLM(
                fixture_file("basic_claude", "llm_replay.json"))
            run_with_replay(cfg, replay_obj)
            spath = sd.obsidian_path(cfg, info, key)
            with open(spath, "rb") as f:
                before = f.read()
            offset_before = sd.load_state(cfg)[key]["offset"]

            self.assertEqual(run_with_replay(cfg, replay_obj), 0)
            self.assertEqual(len(replay_obj.calls), 1)
            with open(spath, "rb") as f:
                self.assertEqual(f.read(), before)
            self.assertEqual(sd.load_state(cfg)[key]["offset"], offset_before)

    def test_incremental_codex_appends_then_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            split = load_fixture_json("incremental_codex",
                                      "expected.json")["split_after"]
            path = install_codex_fixture(cfg, "incremental_codex", limit=split)
            info = sd.parse_session(path, "codex")
            key = sd.session_key(info, path)
            replay_obj = replay.ReplayLLM(
                fixture_file("incremental_codex", "llm_replay.json"))

            self.assertEqual(run_with_replay(cfg, replay_obj), 0)
            state = sd.load_state(cfg)
            self.assertEqual(state[key]["sections"], 0)

            with open(path, "a", encoding="utf-8") as f:
                f.writelines(read_fixture_lines("incremental_codex")[split:])
            size = os.path.getsize(path)

            self.assertEqual(run_with_replay(cfg, replay_obj), 0)
            state = sd.load_state(cfg)
            self.assertEqual(state[key]["sections"], 1)
            self.assertEqual(state[key]["offset"], size)
            spath = sd.obsidian_path(cfg, info, key)
            summary = open(spath, encoding="utf-8").read()
            self.assertEqual(summary.count("<!-- sd:cont"), 1)
            self.assertIn("npm run build:prod", summary)
            self.assertIn("devicePixelRatio", summary)

            # 把状态标成已续写三次，下一轮应进入合并
            state[key]["sections"] = cfg.merge_after
            sd.save_state(cfg, state)
            self.assertEqual(run_with_replay(cfg, replay_obj), 0)
            state = sd.load_state(cfg)
            self.assertEqual(state[key]["sections"], 0)
            merged = open(spath, encoding="utf-8").read()
            self.assertIn("npm run build", merged)
            self.assertIn("npm run build:prod", merged)
            self.assertIn("devicePixelRatio", merged)
            self.assertIn("4K", merged)
            self.assertNotIn("<!-- sd:cont", merged)
            self.assertNotIn("## 续", merged)
            names = [n for n, _ in sd._summary_sections(merged)]
            for heading in ("这一段在做什么", "走过的路", "关键标识符",
                            "还没完的事"):
                self.assertTrue(any(heading in n for n in names), names)
            self.assertNotIn("⚠", merged)
            self.assertEqual([c["mode"] for c in replay_obj.calls],
                             ["full", "incremental", "merge"])

            expected = load_fixture_json("incremental_codex", "expected.json")
            score = replay.score_summary(merged, expected)
            self.assertTrue(score["ok"], score)


class TestGoldenQualitySnapshot(unittest.TestCase):
    def test_recorded_response_passes_golden_detectors(self):
        """记录响应的快照，不是对模型普遍质量的结论。"""
        data = replay.load_replay(fixture_file("basic_claude",
                                               "llm_replay.json"))
        response = data["entries"][0]["response"]
        expected = load_fixture_json("basic_claude", "expected.json")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            path = install_claude_fixture(cfg, "basic_claude")
            info = sd.parse_session(path, "claude")
            checked = sd.checked_summary(cfg, response, info)

        score = replay.score_summary(checked, expected)
        self.assertTrue(score["ok"], score)
        concl = replay.check_conclusions(checked, expected)
        self.assertTrue(concl["ok"], concl)
        self.assertEqual(replay.certainty_overreach(checked), 0)
        self.assertNotIn("⚠", checked)


class TestGoldenSemanticErrors(unittest.TestCase):
    def setUp(self):
        self.expected = load_fixture_json("basic_claude", "expected.json")
        self.recorded = replay.load_replay(
            fixture_file("basic_claude", "llm_replay.json"))["entries"][0]["response"]
        with tempfile.TemporaryDirectory() as tmp:
            self.cfg = make_cfg(tmp)
            path = install_claude_fixture(self.cfg, "basic_claude")
            self.info = sd.parse_session(path, "claude")

    def test_key_fact_omission_detected(self):
        summary = self.recorded.replace("src/export.py", "")
        score = replay.score_summary(summary, self.expected)
        self.assertIn("paths:src/export.py", score["missing"])
        self.assertFalse(score["ok"])

    def test_fabricated_fact_marked(self):
        summary = (self.recorded +
                   "\n\n## 关键标识符\n- 路径：`/missing/fabricated.py`\n")
        marked = sd.verify_summary(summary, self.info)
        self.assertIn("`/missing/fabricated.py` ⚠", marked)
        score = replay.score_summary(marked, self.expected)
        self.assertEqual(score["fabricated"], ["/missing/fabricated.py"])

    def test_todo_marked_done_detected(self):
        expected = {"key_conclusions": [{
            "id": "mem-test",
            "require": ["大文件内存占用"],
            "status": "completed",
        }]}
        summary = "## 还没完的事\n- 大文件内存占用还没测。\n"
        result = replay.check_conclusions(summary, expected)
        self.assertIn("mem-test", result["status_mismatch"])
        self.assertFalse(result["ok"])

    def test_speculation_as_fact_detected(self):
        info = sd._blank_info("claude")
        info["prompts"] = ["改 /a/b.py，修好它"]
        summary = ("## 关键标识符\n"
                   "- 路径：`/missing/fabricated.py` 已修复。\n"
                   "## 还没完的事\n- 无\n")
        marked = sd.verify_summary(summary, info)
        self.assertIn("⚠", marked)
        self.assertIn("⚠?", marked)
        self.assertEqual(replay.certainty_overreach(marked), 1)

    def test_superseded_decision_as_current_detected(self):
        expected = {"key_conclusions": [{
            "id": "redis-superseded",
            "require": ["Redis"],
            "status": "superseded",
            "forbid": ["目前仍然"],
        }]}
        summary = "## 这一段在做什么\n目前仍然继续用 Redis。\n"
        result = replay.check_conclusions(summary, expected)
        self.assertIn("redis-superseded", result["stale_decision"])
        self.assertFalse(result["ok"])

    def test_cross_project_fact_detected(self):
        summary = (self.recorded +
                   "\n\n## 关键标识符\n- 路径：`other-project/api.py`\n")
        score = replay.score_summary(summary, self.expected)
        self.assertIn("other-project/api.py", score["foreign"])
        marked = sd.verify_summary(summary, self.info)
        self.assertIn("`other-project/api.py` ⚠", marked)

    def test_reason_loss_detected(self):
        expected = load_fixture_json("adversarial_merge", "expected.json")
        summary = ("## 这一段在做什么\n"
                   "在 shop 项目里暂不换用 Redis，继续用本地文件缓存。\n")
        result = replay.check_conclusions(summary, expected)
        self.assertIn("no-redis", result["missing_reason"])
        self.assertNotIn("no-redis", result["missing_statement"])

    def test_merge_loss_check_flags_reason_loss(self):
        replay_data = replay.load_replay(
            fixture_file("adversarial_merge", "llm_replay.json"))
        full = replay_data["entries"][0]["response"]
        bad_merge = replay_data["entries"][1]["response"]
        old_facts = sd.extract_conclusions(full)
        lost = replay.merge_loss_check(old_facts, bad_merge)
        self.assertTrue(any("原因" in item for item in lost), lost)

    def test_merge_rejects_conclusion_loss_without_touching_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            path = install_claude_fixture(cfg, "adversarial_merge")
            info = sd.parse_session(path, "claude")
            key = sd.session_key(info, path)
            replay_data = replay.load_replay(
                fixture_file("adversarial_merge", "llm_replay.json"))
            full = replay_data["entries"][0]["response"]
            sd.write_full(cfg, info, "shop", full, key)
            spath = sd.obsidian_path(cfg, info, key)
            sd.update_index(cfg, info, "shop", full, key, path, "full")
            with open(spath, "rb") as f:
                before = f.read()
            size = os.path.getsize(path)
            sd.save_state(cfg, {key: {
                "offset": size,
                "prefix_sha1": sd.prefix_sha1(path, size),
                "sections": 3,
                "first_date": info.get("start"),
                "first_title": sd.title_of(info),
                "digest_facts": sd.extract_conclusions(full),
                "merge_rejections": 0,
            }})

            replay_obj = replay.ReplayLLM(replay_data)
            self.assertEqual(run_with_replay(cfg, replay_obj), 1)

            with open(spath, "rb") as f:
                self.assertEqual(f.read(), before)
            state = sd.load_state(cfg)
            self.assertEqual(state[key]["sections"], 3)
            self.assertEqual(state[key]["merge_rejections"], 1)
            failures = sd.load_failures(cfg)
            self.assertIn("原因", failures[key]["error"])
            self.assertIn("部署环境", failures[key]["error"])


if __name__ == "__main__":
    unittest.main()
