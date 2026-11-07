# SessionDigest

把 **Claude Code** 和 **Codex** 的本地会话记录，蒸馏成结构化、人类可读的 Markdown 摘要。

不装服务、不起守护进程、不建数据库。一个 Python 文件，跑完就退。

> **隐私警告**
>
> 本程序会读取你的本地会话记录，并**把会话内容发送到你配置的模型端点**。
> 会话里可能包含 API key、内网地址、客户名、源代码片段。
> **只对你自己的会话运行**；公开分享输出前，先检查有没有敏感信息。
> 默认使用官方 Anthropic API，你也可以配一个本地或自建端点。

---

## 它做什么

一次运行，把一场 AI 编程会话整理成四块：

```markdown
## 这一段在做什么
你在给 Flask 库存系统加导出功能，卡在中文文件名乱码。

## 走过的路
先试了 openpyxl 默认编码，无效；定位到 Content-Disposition
没有用 RFC 5987 编码。改成 filename*=UTF-8''… 后正常。

## 关键标识符
- 路径：`app/export.py`
- 命令：`python -m pytest tests/test_export.py`
- 参数/flag：`filename*=UTF-8''`
- 报错原文：`UnicodeEncodeError: 'latin-1' codec can't encode characters`
- 版本/模型：openpyxl 3.1.2
- URL：无

## 还没完的事
- 导出大文件时内存占用还没测
```

这不是“聊天记录备份”，而是**给下一次会话用的上下文**：路径、命令、报错原文都逐字保留，AI 拿起来能直接接着干。

## 它不是什么

- 不是检索工具。要搜原文，用 ripgrep 或专门的会话检索工具。
- 不是自动注入。它不会在开场把内容塞进上下文——由你决定什么时候读。
- 不是云端服务。所有文件都在本地。

## 三种模式

| 模式 | 调用模型 | 输出 | 用途 |
|---|---|---|---|
| `--export` | 否 | 按项目分组的会话摘录 + 索引 | 先看看库里有什么 |
| `--llm N` | 是 | 临时目录里的 N 份摘要 | 试效果、调 prompt |
| `--nightly` | 是 | `memory/` 与 Obsidian，增量去重 | 挂定时任务长期跑 |

## 安装

需要 Python 3.8+，无第三方依赖。

```bash
git clone https://github.com/Scanenf/claude-session-digest.git
cd claude-session-digest

# 设置模型凭证（二选一）
export ANTHROPIC_AUTH_TOKEN="sk-ant-..."      # Linux / macOS
set ANTHROPIC_AUTH_TOKEN=sk-ant-...           # Windows cmd
```

也支持 `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`。
没有设置时，会尝试读取 `~/.claude/settings.json` 里的 `env` 块（这是 Claude Code 用户的常见配置位置）。

## 快速开始

```bash
# 1. 先看看能导出什么，不花钱
python session_digest.py --export

# 2. 估算最近 5 场会花多少 token，不调用模型
python session_digest.py --llm 5 --dry-run

# 3. 真的生成 5 份摘要，输出到 digest-output/
python session_digest.py --llm 5

# 4. 正式跑：写入 Obsidian，增量处理新会话
python session_digest.py --nightly \
  --obsidian-dir ~/vault/AI摘要 \
  --memory-dir ~/.claude/projects/<项目>/memory
```

`--nightly` 至少需要 `--obsidian-dir` 或 `--memory-dir` 之一。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--claude-root` | `~/.claude/projects` | Claude Code 会话目录 |
| `--codex-root` | `~/.codex/sessions` | Codex 会话目录 |
| `--out` | `digest-output` | `--export` / `--llm` 的输出目录 |
| `--obsidian-dir` | 无 | Obsidian 摘要根目录，给了才写 |
| `--memory-dir` | 无 | 写入 `sessions/YYYY-MM.md` 并维护 `MEMORY.md` |
| `--state-file` | `~/.claude-session-digest/state.json` | 去重状态 |
| `--projects-file` | 无 | 项目名映射，见下 |
| `--base-url` / `--api-key` / `--model` | 环境变量 | 模型配置 |
| `--min-turns` | 8 | 少于 N 条消息的会话跳过 |
| `--min-age-hours` | 6 | 刚结束的会话先不动 |
| `--max-sessions` | 20 | 单次上限，控制花费 |
| `--force` | 关 | 忽略去重，重跑全部 |
| `--dry-run` | 关 | 只规划、估算，不调用模型不写文件 |
| `--verbose` | 关 | 失败时打印完整堆栈 |

> “消息数”指用户消息与助手消息各计一条。所以 `--min-turns 8` 大约是 4 个来回。

### 项目名映射

Claude Code 的目录名是路径编码（如 `c--Users-me-projects-shop`），不好读。
可以给一个 JSON，把目录名或工作目录名映射成展示名：

```json
{
  "shop": "紫洋库存系统",
  "gen": "上班猫桌宠"
}
```

```bash
python session_digest.py --nightly --projects-file projects.json --obsidian-dir ...
```

未匹配的项目会自动使用工作目录名。

## 定时任务

### Windows

`run_nightly.bat`：

```bat
@echo off
chcp 65001 >nul
set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%session_digest.py" --nightly --obsidian-dir D:\vault\AI摘要 %*
```

任务计划程序 → 新建任务 → 每天凌晨执行，起始于脚本目录。日志用 `>> nightly.log 2>&1` 追加。

### macOS / Linux

`run_nightly.sh`：

```sh
#!/usr/bin/env sh
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/session_digest.py" --nightly --obsidian-dir "$HOME/vault/AI摘要" "$@"
```

```bash
chmod +x run_nightly.sh
crontab -e
# 每天 3:07 跑一次
7 3 * * * /path/to/run_nightly.sh >> /path/to/nightly.log 2>&1
```

## 输出结构

```
digest-output/                    # --export / --llm
├── README.md                     # 索引
├── Claude Code/
│   └── my-project/
│       └── 1a2b3c4d_会话标题.md
└── Codex/
    └── ...

<obsidian-dir>/                   # --nightly
└── 2026-11/
    └── 2026-11-07_1a2b3c4d.md   # 带 frontmatter，可被 Dataview 查询

<memory-dir>/                     # --nightly
├── sessions/2026-11.md           # 按月追加
└── MEMORY.md                     # 自动插入一行索引
```

## 常见问题

**摘要会不会写错？**
会。它是模型归纳的，不是程序抽取的。路径、命令、报错原文在 prompt 里要求逐字抄录，但叙述部分仍可能有偏差。把它当作“带方向的索引”，关键事实以原文为准。

**会花多少钱？**
取决于会话长度和模型。先用 `--dry-run` 看估算。一场典型会话的摘要在 1–2k output token 左右。`--max-sessions` 是硬上限。

**去重怎么算的？**
按 session id（或文件名）记录在 `state.json` 里。删除该文件等于全部重跑。`--force` 可以临时忽略。会话文件被追加更新后不会自动重跑——需要 `--force` 或删掉对应条目。

**Claude Code 升级后格式变了怎么办？**
解析失败不会崩溃，只会少解析内容。用 `--verbose` 看异常堆栈，然后提 issue 附上一段脱敏后的 jsonl。

**和 claude-mem / claude-conversation-extractor 有什么区别？**
SessionDigest 同时支持 Claude Code 和 Codex；零依赖、无后台进程；输出是给人和 AI 读的笔记，不是检索索引。它和检索型工具是互补关系，可以同时用。

**支持别的 agent 吗？**
解析器在 `parse_claude` / `parse_codex` 两个函数里，加一个 `parse_xxx` 并接进 `parse_session` 即可。欢迎 PR，附一段样本格式。

## License

[MIT](LICENSE)
