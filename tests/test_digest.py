# -*- coding: utf-8 -*-
"""SessionDigest 单元测试：python -m unittest discover -s tests -v"""
import json
import os
import shutil
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
            sd, "_local_usernames", return_value=["邵长荣", "shaorong"])
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
            (r"C:\Users\邵长荣\proj\a.py", r"~\proj\a.py"),
            ("C:/Users/shaorong/proj/a.py", "~/proj/a.py"),
            ("/home/shaorong/proj/a.py", "~/proj/a.py"),
            ("/Users/shaorong/proj/a.py", "~/proj/a.py"),
            ("/c/Users/邵长荣/proj/a.py", "~/proj/a.py"),
        ]
        for raw, want in cases:
            self.assertEqual(sd.redact_text(raw, "strict"), want)

    def test_safe_and_off_keep_paths(self):
        path = r"C:\Users\邵长荣\proj\a.py"
        self.assertEqual(sd.redact_text(path, "safe"), path)
        self.assertEqual(sd.redact_text(path, "off"), path)

    def test_strict_masks_standalone_username(self):
        self.assertEqual(sd.redact_text("邵长荣的电脑", "strict"), "~的电脑")

    def test_transcript_is_redacted_before_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            info = sd._blank_info("claude")
            info["prompts"] = [
                "key 是 sk-ant-api03-abcdefghijklmnop，路径 "
                r"C:\Users\邵长荣\proj\a.py"]
            captured = {}

            def fake_call(cfg, prompt, **kwargs):
                captured["prompt"] = prompt
                return "ok", {}

            with mock.patch.object(sd, "call_llm", side_effect=fake_call):
                sd.summarize_one(cfg, info, "shop")
            self.assertNotIn("sk-ant-api03", captured["prompt"])
            self.assertNotIn("邵长荣", captured["prompt"])
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


if __name__ == "__main__":
    unittest.main()
