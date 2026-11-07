# SessionDigest

把 **Claude Code** 和 **Codex** 的本地会话记录，蒸馏成结构化、人类可读的 Markdown 摘要。

不装服务、不起守护进程、不建数据库。一个 Python 文件，跑完就退。

> **隐私警告**
>
> 本程序会读取你的本地会话记录，并**把会话内容发送到你配置的模型端点**。
> 会话里可能包含 API key、内网地址、客户名、源代码片段。
> 发送和落盘前默认会做脱敏（见下文「脱敏」），但脱敏是启发式的，不是保险箱。
> **只对你自己的会话运行**；公开分享输出前，仍然应该过目一遍。
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

- 不是检索工具。`--find` 只是在摘要里做关键词匹配；要搜原文或做语义检索，
  用 ripgrep、deja-vu 这类专门工具。
- 不是自动注入。它不会在开场把内容塞进上下文——由你决定什么时候读。
- 不是云端服务。所有文件都在本地。

## 和同类工具比

这个方向已经有不少项目。下表按“做法”分类，不是排名，星数是 2026 年 11 月的粗略值，
这些项目迭代很快，具体能力以各自的 README 为准。

| 项目 | 星 | 做法 | 和 SessionDigest 的关系 |
|---|---:|---|---|
| [claude-mem](https://github.com/thedotmack/claude-mem) | ~114k | Hook 自动捕获 → AI 压缩 → 下次会话自动注入，支持多个 agent | 最完整的方案。要“自动”，用它 |
| [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) | ~1.3k | 会话结束 Hook → 每日日志 → 编译成概念化知识文章 | 比“一场一份摘要”更深一层，但依赖 Hook |
| [deja-vu](https://github.com/vshulcz/deja-vu) | ~1.4k | 跨 43 种 agent 的本地会话纯检索 | 检索，不归纳。与本项目互补 |
| [claude-code-log](https://github.com/daaain/claude-code-log) | ~1.3k | jsonl → 可读 HTML / Markdown（Codex 为 beta） | 转格式，不做摘要 |
| [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) | ~1.7k | 把 transcript 发布成网页 | 展示，不做归纳 |
| [claude-conversation-extractor](https://github.com/ZeroSumQuant/claude-conversation-extractor) | ~692 | 把对话提取成 markdown | 提取，不归纳 |
| [claude-obsidian-sessions](https://github.com/plcosta/claude-obsidian-sessions) | — | Stop Hook → Haiku 摘要 → Obsidian 笔记 | 输出形态接近，但只支持 Claude、依赖 Bun |

那为什么还写这个？四个理由：

1. **零配置**。不装 Hook、不装运行时、不起后台进程、不改 Claude 配置。
   clone 下来，一个 Python 文件跑完就退。
2. **增量续写**。同一场会话继续聊，只摘要新增部分并追加「续」段，满 N 段再合并。
   多数同类是“会话结束跑一次”，或每次整场重跑。
3. **事实校验**。摘要里的路径、命令、报错会回原文比对，对不上的标 `⚠`。
4. **你决定上下文何时进入**。不自动注入，用 `--context` 手动拼接。
   这不是功能缺失，是取舍：不做悄悄消耗 token 的事。

如果你要的是“装完就忘、自动带上下文”，用 claude-mem；
要完整的可读归档，用 claude-code-log；要在所有会话原文里做语义检索，用 deja-vu
（本项目的 `--find` 只匹配摘要关键词）。
它们和本项目可以同时装，不冲突。

## 五种模式

| 模式 | 调用模型 | 输出 | 用途 |
|---|---|---|---|
| `--export` | 否 | 按项目分组的会话摘录 + 索引 | 先看看库里有什么 |
| `--llm N` | 是 | 临时目录里的 N 份摘要 | 试效果、调 prompt |
| `--nightly` | 是 | `memory/` 与 Obsidian，增量续写 | 挂定时任务长期跑 |
| `--context N` | 否 | 最近 N 份摘要的拼接文本 | 手动喂给下一次会话 |
| `--find 关键词` | 否 | 相关度最高的 N 份摘要 | 按问题找历史 |

## 增量续写

同一场会话继续聊时，不会整场重跑：

1. state 里记录上次处理到的**字节偏移量**和前缀哈希
2. 下次夜间任务只读新增部分，生成一段「## 续 · 时间」
3. 续段追加到同一份摘要末尾，不新建文件
4. 续段累计到 `--merge-after` 次（默认 3），自动做一次合并整理，压回一份完整摘要

新增内容太少（默认少于 4 条消息且不足 500 字符）就跳过，等下次一起处理。
会话文件被重写、前缀哈希对不上时，回退为整场重跑。

## 事实校验

摘要生成后，程序会把「关键标识符」里反引号包裹的路径、命令、报错拿回原文做匹配，
按证据强度加三种标记：

| 标记 | 含义 |
|---|---|
| `⚠` | 原文中未找到；或命令只在讨论里被提到，没有实际执行记录 |
| `⚠≠` | 原文有相近但不同的条目（参数、subcommand 或 basename 不一致） |
| `⚠?` | 摘要写了“通过/成功/修复完成”，但原文没有足够的结果证据 |

首次出现标记时会在摘要开头插入一行图例：

```markdown
> 校验图例：⚠ = 原文中未找到或未执行；⚠≠ = 原文有相近但不同的条目；⚠? = 原文未证实执行结果。
```

命令比对是严格 argv 比对，不是子串或子序列匹配：去掉成对引号、解开
`bash -c` / `powershell -Command` 包装、去掉 `python -m` 前缀之后，
flag 顺序无关但重复计数，位置参数顺序和值必须一致。`pytest` 与
`pytest tests/ -q` 会判 `⚠≠`，不会因为前缀相同就放过。

程序只把明确的成功输出当作成功证据（`exit code 0`、`Process exited with code 0`、
`"success": true`、`All tests passed`、`N passed`）。没有证据不等于成功；
助手正文里的“应该没问题”也不算出事结果的事实来源。

旧摘要只用于继承警告，不作为事实来源：续写里重新提到的旧条目，如果新增记录
没有再次佐证，仍会保留原来的 `⚠` 标记；只有增量记录里重新出现时才会清掉。

**边界**：命令校验只回答“摘要里的命令和来源记录是否一致”，不回答
“命令是否真的执行成功”。日志里出现命令不等于测试通过。
`⚠≠` 的 basename/subcommand 启发式和 `⚠?` 的结果词检测都会误报，
用 `--no-verify` 可整体关闭校验。

## 脱敏

默认开启 `--redact strict`。处理覆盖四处：

1. **发给模型之前**：会话转录、项目显示名、续写/合并时带上的旧摘要。
2. **写入摘要文件之前**：Obsidian 笔记、月度文件、`--export` 文档和 README、
   frontmatter 与 `MEMORY.md` 标题。
3. **写入元数据之前**：`state.json` / `index.json` / `failures.json` 里的标题、
   项目名、路径。路径改成 v2 引用（`file_ref` / `source_ref` / `path_ref`）：
   `root_id` 是本地密钥对根目录的 HMAC，敏感目录名再单独做 HMAC；不保存绝对路径。
   旧数据用 `--migrate` 迁移，迁移是幂等的。
4. **打印之前**：`--find` / `--context` / `--llm` / `--export` 的终端输出、
   失败信息、`--verbose` 的异常栈、锁文件提示。

会被替换的内容：

| 类型 | 例子 | 替换为 |
|---|---|---|
| 常见密钥 | `sk-ant-...`、`ghp_...`、`AKIA...`、`xoxb-...` | `***` |
| 带名字段 | `api_key=...`、`token: ...`、`password=...` | 保留字段名，值变 `***` |
| Bearer token | `Authorization: Bearer eyJ...` | `Bearer ***` |
| 私钥块 | `-----BEGIN PRIVATE KEY-----` | `[私钥已隐藏]` |
| URL 里的账号密码 | `https://user:pass@host` | `https://***:***@host` |
| 邮箱 | `someone@example.com` | `<email>` |
| 内网 IP | `10.x.x.x`、`172.16-31.x.x`、`192.168.x.x` | `<private-ip>` |
| 主目录 | `C:\Users\name\...`、`/home/name/...`、`/Users/name/...` | `~/...` |
| 用户名单独出现 | 句子里的 `name` | `~` |

**不替换**：项目内的相对路径、命令、报错原文——这些正是摘要的用处。

三档：

```bash
python session_digest.py --nightly ...              # 默认 strict
python session_digest.py --nightly --redact safe ...   # 只遮凭据，保留完整路径
python session_digest.py --redact off ...              # 完全不处理
```

`--export` 的导出文件同样脱敏；想要原始归档就显式用 `--redact off`。

### 边界：风险降低，不是安全边界

自动脱敏是**风险降低措施，不是安全边界**。它只能降低已知格式的泄漏概率；
测试里的哨兵扫描只能证明“已知哨兵没有出现在已扫描的产物中”，
不能证明不存在未识别格式的泄漏。分享输出前仍然需要人工过目。

**无法可靠自动识别，必须人工检查**：

- 写在自然语言里的密码、口令、恢复码；
- 自定义或私有 token、无固定前缀的 base64 长串、跨行拆分的密钥；
- 客户名、人名、项目代号等业务敏感词；
- 不在主目录下的敏感绝对路径（如 `D:\客户\合同`）；
- 内网主机名、域名；
- Claude 编码目录里的用户名（如 `c--Users-alice-projects-shop`）；
- 代码片段本身的商业逻辑；
- 图片、截图里的内容。

### 路径引用与根目录移动

元数据里的路径引用不再保存绝对路径，也不要求每个目录名都是明文：

- 根目录身份是 `root_id`，由本地密钥 `digest.key` 对规范化根路径做 HMAC；
- 目录名如果会被脱敏，或命中 `c--Users-*` 这类用户目录形态，就存成 HMAC
  组件（`h2_...`），解析时扫描同级目录匹配；
- 每条 ref 带 session id、文件大小和前缀哈希，精确解析失败后才用于恢复；
- 找不到时不返回空结果，也不猜一个文件，而是明确报告
  `unbound_root` / `missing` / `ambiguous` / `legacy_redacted` /
  `key_missing` / `recovery_limited`。

根目录整体移动后，旧 ref 不需要修改：

```bash
python session_digest.py --roots
python session_digest.py --rebind-root r2_旧id --claude-root /新位置
```

`--roots` 只显示 root_id 和引用数量，不显示实际路径。`--rebind-root` 只写
`roots.json` 的 root_id 绑定，不碰 state/index/failures。

**旧 ref 无法自动恢复时**：`--migrate` 会列出无法迁移的条目；用
`--repair-ref <sid> --source <新路径>` 或 `--repair-ref <sid> --file <新路径>`
显式修复。修复只写 v2 ref，不保存你输入的绝对路径。

只读命令（`--find` / `--context`）不会暗中迁移，也不会创建密钥；检测到旧引用
时只在 stderr 提示。`--migrate --dry-run` 前后文件快照完全一致。

**无法自动恢复的情况与处理：**

| 状态 | 原因 | 用户需要做什么 |
|---|---|---|
| `unbound_root` | 根目录搬走或换了配置 | `--roots` 找到旧 root_id，再 `--rebind-root` |
| `root_not_configured` | 旧 ref 的根目录当前没配 | 补上对应 `--claude-root` / `--obsidian-dir` 等再迁移 |
| `legacy_redacted` | 旧字段已经被脱敏，原文不可逆 | `--repair-ref <sid> --source/--file <实际路径>` |
| `key_missing` | `digest.key` 丢失或从未创建 | 从备份恢复同一个密钥；否则只能逐条 `--repair-ref` |
| `key_missing`（密钥被换新） | 新密钥算出的 root_id / HMAC 与历史不同 | 恢复旧密钥；旧密钥已丢失则历史掩码 ref 无法解析 |
| `ambiguous` | 多个文件都像目标 | 用 `--repair-ref` 指定唯一文件，程序不会替你猜 |
| `recovery_limited` | 恢复扫描超过 5000 个文件或 2 秒 | 缩小根目录，或用 `--repair-ref` 直接指定 |
| `legacy_outside_root` | 旧路径不在任何已知根目录内 | 无法生成 v2 ref；文件仍在时继续用旧路径回退，或把它移到已知根下 |

密钥只用于 HMAC，不加密内容；但**丢了密钥就再也算不出同样的 root_id 和
`h2_` 组件**。请把 `digest.key` 和 `roots.json` 与 state 一起备份，且不要提交到
仓库（`.gitignore` 已排除）。

## 按问题找历史

`--context` 是“最近 N 场”，`--find` 才是“和这个问题有关的”：

```bash
python session_digest.py --find "库存 架构"
python session_digest.py --find "中文文件名 乱码" --top 5 --project shop
```

它在本地的摘要文本里做关键词打分：中文按相邻二字切词，英文按词切分，
标题权重是正文的三倍，再按词频稀有度加权。不调用模型、不上传任何内容。

**说清楚它是什么**：关键词匹配，不是语义检索。问“之前为什么改架构”，
它找的是包含“架构”“改”“为什么”这些词的摘要；换个说法（比如“重构的原因”）
可能就找不到。要语义检索，得配向量库或让模型来挑，那就不是零依赖了。

匹配不到时会明确提示，而不是拿几份无关的凑数。

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
没有设置时，会尝试读取 `~/.claude/settings.json` 里的 `env` 块。

### 用 OpenAI 兼容接口

默认请求 Anthropic Messages API（`/v1/messages`）。DeepSeek、硅基流动、
本地 Ollama 等 OpenAI 兼容端点，加 `--api-format openai`：

```bash
python session_digest.py --llm 3 \
  --api-format openai \
  --base-url https://api.deepseek.com/v1 \
  --api-key sk-... \
  --model deepseek-chat
```

- OpenAI 兼容模式**必须**指定 `--model`（或环境变量 `OPENAI_MODEL`）
- 地址已经带 `/v1` 就原样使用，不会重复拼接
- 环境变量可用 `OPENAI_API_KEY` / `OPENAI_MODEL`

两种格式的报文不通用。配错时程序会报 HTTP 错误并提示检查 `--api-format`，
而不是悄悄返回空摘要。

## 快速开始

```bash
# 1. 先看看能导出什么，不花钱
python session_digest.py --export

# 2. 估算最近 5 场会花多少 token，不调用模型
python session_digest.py --llm 5 --dry-run

# 3. 真的生成 5 份摘要，输出到 digest-output/
python session_digest.py --llm 5

# 4. 正式跑：写入 Obsidian，增量处理新会话和续写部分
python session_digest.py --nightly \
  --obsidian-dir ~/vault/AI摘要 \
  --memory-dir ~/.claude/projects/<项目>/memory

# 5. 把最近 5 份摘要拼起来，直接粘贴给 AI
python session_digest.py --context 5 --project shop

# 6. 或者按问题找相关历史
python session_digest.py --find "库存 架构" --top 3
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
| `--state-file` | `~/.claude-session-digest/state.json` | 偏移量与去重状态 |
| `--index-file` | 与 state 同目录 | 会话索引，供 `--context` / `--find` 使用 |
| `--failures-file` | 与 state 同目录 | 失败队列 |
| `--lock-file` | state 文件 + `.lock` | 进程锁，防止两个实例同时写 |
| `--projects-file` | 无 | 项目名映射，见下 |
| `--ref-key-file` | 与 state 同目录的 `digest.key` | HMAC 密钥；丢失后掩码 ref 无法解析 |
| `--roots-file` | 与 state 同目录的 `roots.json` | root_id 绑定；只有 id，没有路径 |
| `--migrate` | 关 | 迁移旧路径引用；配 `--dry-run` 只查看计划 |
| `--roots` | 关 | 列出 root_id 与引用数量，不显示路径 |
| `--rebind-root` | 无 | 把历史 root_id 绑定到当前根目录 |
| `--root-kind` | 自动判断 | `--rebind-root` 时指定根目录类型 |
| `--repair-ref` | 无 | 用 `--source` / `--file` 显式修复一条记录 |
| `--project` | 无 | `--context` / `--find` 时按项目名过滤 |
| `--top` | 3 | `--find` 返回几条 |
| `--base-url` / `--api-key` / `--model` | 环境变量 | 模型配置 |
| `--api-format` | `anthropic` | `anthropic`（/v1/messages）或 `openai`（/chat/completions） |
| `--min-turns` | 8 | 新会话少于 N 条消息时跳过 |
| `--min-age-hours` | 6 | 刚结束的会话先不动 |
| `--max-sessions` | 20 | 单次上限，控制花费 |
| `--merge-after` | 3 | 续段达到 N 段后合并整理 |
| `--min-delta-turns` | 4 | 新增少于 N 条消息且字数不足时跳过 |
| `--min-delta-chars` | 500 | 新增少于 N 字符且消息不足时跳过 |
| `--force` | 关 | 忽略状态，全部重跑 |
| `--no-verify` | 关 | 不把标识符与原文比对，不加 `⚠` |
| `--redact` | `strict` | 脱敏档位：`off` / `safe` / `strict`（默认把用户名换成 `~`） |
| `--retry-failed` | 关 | 只重试失败队列 |
| `--dry-run` | 关 | 只规划、估算，不调用模型不写文件；`--migrate` 时快照不变 |
| `--verbose` | 关 | 失败时打印完整堆栈 |

> “消息数”指用户消息与助手消息各计一条。所以 `--min-turns 8` 大约是 4 个来回。

### 项目名映射

Claude Code 的目录名是路径编码（如 `c--Users-me-projects-shop`），不好读。
可以给一个 JSON，把目录名或工作目录名映射成展示名：

```json
{
  "shop": "库存系统",
  "gen": "上班猫桌宠"
}
```

未匹配的项目会自动使用工作目录名。

## 失败队列

调用失败的会话会写进 `failures.json`，不推进偏移量。下次 `--nightly` 会先自动重试，
也可以单独跑：

```bash
python session_digest.py --retry-failed --dry-run   # 看看有哪些
python session_digest.py --retry-failed             # 逐个重试
```

同一项连续失败 3 次后不再自动重试，避免死循环。

## 并发与锁

`--nightly`、`--export` 和 `--retry-failed` 启动时会先拿一个进程锁（默认是
state 文件旁的 `state.json.lock`）。定时任务和手动运行撞上时，第二个进程会
直接退出并提示，不会两个进程同时改 state、索引和摘要文件。

锁用的是操作系统文件锁：进程正常退出或崩溃，系统都会自动释放，不会留下
需要手工删除的死锁。`--dry-run` 只读不加锁。

## 定时任务

### Windows

`run_nightly.bat`：

```bat
@echo off
chcp 65001 >nul
set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%session_digest.py" --nightly --obsidian-dir D:\vault\AI摘要 %*
```

任务计划程序 → 新建任务 → 每天凌晨执行，起始于脚本目录。

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
    └── 2026-11-01_1a2b3c4d.md   # 续段追加在同一文件末尾

<memory-dir>/                     # --nightly
├── sessions/2026-11.md           # 按月追加，同一会话重写块
└── MEMORY.md                     # 自动插入一行索引
```

## 从 v0.1 升级

v0.1 的 state 只有 session id。v0.2 第一次运行会给旧条目补上偏移量，**不重跑历史**。
升级前已经续写过的部分会漏掉；如果需要，用 `--force` 整场重跑一次。

## 测试

```bash
python -m unittest discover -s tests -v
```

104 个用例，覆盖两个解析器、命令事件关联、严格 argv 比对与证据等级、
增量读取、前缀哈希、事实校验（含 `--no-verify`）、脱敏（密钥/邮箱/内网 IP/
主目录路径、发送前与落盘前各一道）、v2 路径引用与旧数据迁移、读取与异常输出的
二次脱敏、月度块替换、规划判定、索引、失败队列、`--context`/`--find` 过滤，
以及进程锁（含跨进程争抢）、原子写入中断、HTTP 错误、超时、非 JSON 响应、
模型返回被截断等异常恢复场景。
另有一组哨兵扫描测试，把 state/index/failures/摘要/导出/终端/异常栈/临时文件/
未跟踪文件等 17 类产物集中检查，确认已知哨兵没有漏出。接口测试用本地起的小
HTTP 服务，不访问外网。

## 常见问题

**摘要会不会写错？**
会。它是模型归纳的，不是程序抽取的。路径、命令、报错原文在 prompt 里要求逐字抄录，但叙述部分仍可能有偏差；标 `⚠` 的条目是程序没能在原文里匹配到的，优先核对它们。关键事实以原文为准。

**会花多少钱？**
取决于会话长度和模型。先用 `--dry-run` 看估算。一场典型会话的摘要在 1–2k output token 左右。`--max-sessions` 是硬上限。增量续写只花新增部分的钱。

**同一场会话继续聊会怎样？**
只摘要新增部分，追加「续」段，不重跑整场。见上文「增量续写」。

**Claude Code 升级后格式变了怎么办？**
解析失败不会崩溃，只会少解析内容。用 `--verbose` 看异常堆栈，然后提 issue 附上一段脱敏后的 jsonl。

**和 claude-mem / claude-code-log 这些有什么区别？**
见上文「和同类工具比」。简单说：它们多数靠 Hook 自动跑并注入上下文，
本项目零配置、手动控制、带增量续写和事实校验，定位不同，可以同时用。

**支持 OpenAI 兼容接口吗？**
支持。加 `--api-format openai`，并显式给 `--model`。见上文「用 OpenAI 兼容接口」。

**`--find` 和 `--context` 有什么区别？**
`--context N` 取最近 N 份，适合“接上刚才的活”；`--find 关键词` 按相关度找，
适合“以前那件事是怎么处理的”。后者是关键词匹配，不是语义检索。

**支持别的 agent 吗？**
解析器在 `parse_claude` / `parse_codex` 里，加一个 `parse_xxx` 并接进 `parse_session` 即可。欢迎 PR，附一段样本格式。

## License

[MIT](LICENSE)
