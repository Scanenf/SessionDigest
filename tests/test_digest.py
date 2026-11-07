# -*- coding: utf-8 -*-
"""SessionDigest 单元测试：python -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

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


if __name__ == "__main__":
    unittest.main()
