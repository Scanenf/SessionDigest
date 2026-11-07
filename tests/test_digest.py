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
import session_digest as sd  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
