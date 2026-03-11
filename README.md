# Mortimer

> 以 Mortimer J. Adler 命名——《如何阅读一本书》作者，提出阅读四层次：基础阅读→检视阅读→分析阅读→主题阅读。本项目的四阶段流程（概览→粗读→精读→深度探索）正是这一方法论的自动化实现。

![mortimer-reading-pipeline-cover](https://github.com/user-attachments/assets/9ebfebea-e210-4bba-94fb-a9e44136554a)
自动获取书籍全文，利用 Gemini 3 Flash 的 1M context window 进行全书分析，生成结构化读书笔记到 Obsidian。

支持两种交互方式：**Claude Code 终端**（完整功能）和 **Claudian（Obsidian 插件）**（笔记上下文感知）。

## 架构

```
用户 ──→ Claude Code (/read 技能编排)
         │
         ├─ 终端模式 (CLI)
         │   └─ 完整功能：单本 + 批量 + 自由探索
         │
         └─ Claudian 模式 (Obsidian 内嵌)
             └─ 同一 /read 技能，额外感知当前编辑的笔记
         │
         ├─ 单本模式: 交互式四阶段阅读
         │   书名确认 → 获取资源 → 概览+粗读+精读 → 自由探索
         │
         └─ 批量模式: 书单驱动全自动
             书单准备 → 批量搜索下载 → 并行分析 → 验证完整性
         │
         ├── Anna's Archive ─── 书籍搜索与下载
         ├── extract_book.py ── EPUB/PDF/TXT 文本提取
         ├── Gemini 3 Flash ─── 全书分析 (1M context)
         ├── Obsidian ───────── 笔记写入
         └── Google Drive ──── 备份 (可选)
```

**分工**: Claude Code 负责编排和用户交互，Gemini 3 Flash 负责全书文本分析。Claude 从不直接读取书籍原文——所有分析通过 `gemini_analyzer.py` 完成。

**两种模式对比**:

| | 终端 (CLI) | Claudian (Obsidian) |
|---|---|---|
| 触发方式 | 终端输入 `/read 书名` | Obsidian 内输入 `/read 书名` |
| 单本模式 | ✅ | ✅ |
| 批量模式 | ✅ | ✅ |
| 笔记上下文感知 | ❌ | ✅ 自动注入当前编辑的笔记 |
| 额外进程 | 无 | 独立 Claude Code 进程 (~300MB) |
| 适用场景 | 首次阅读、批量处理 | 在 Obsidian 中浏览笔记时追问、深度探索 |

## 功能

### 单本模式

| 能力 | 说明 |
|------|------|
| 一键读书 | 输入书名，自动完成搜索、下载、分析、笔记全流程 |
| 四阶段分析 | 概览 → 粗读 → 精读 → 自由探索 |
| 10 类专业模板 | biography, business, psychology, self-growth, technology, history, philosophy, finance, literature, science |
| 断点续读 | 检测 Obsidian 已有笔记，从断点继续 |

精读完成后进入**自由探索**阶段：

| 操作 | 说明 |
|------|------|
| 深度探索 | 选择推荐视角或自定义主题，生成结构化深度笔记（`03-深度-{主题}.md`） |
| 全部探索 | 一次性并行生成 5 个推荐视角的深度笔记 |
| 自由提问 | 基于全文回答任意问题，直接展示答案，不写入笔记 |
| 结束阅读 | 标记阅读状态为 completed |

### 批量模式

| 能力 | 说明 |
|------|------|
| 书单驱动 | 提供 Markdown 书单、JSON 文件或直接粘贴书目 |
| 批量搜索下载 | Anna's Archive 评分选择最佳候选 |
| TPM 感知并行 | 动态令牌桶调度，最多 5 并行，适配速率限制 |
| 完整性验证 | 检查每本书的 3 个核心笔记是否齐全 |
| 失败处理 | 重试或降级到单本模式逐个处理 |

### 分析引擎

- **Map-Reduce**: 超长书籍（>800K tokens）分片分析再合并
- **Context Cache**: 同一书籍多次调用复用 Gemini 上下文缓存
- **结果缓存**: 本地缓存 24 小时，避免重复调用
- **分类模板**: 10 类定制化提示词，按 `--category` 切换

## 安装

### 前置条件

- Python 3.10+
- [pdftotext](https://poppler.freedesktop.org/) — PDF 文本提取
- [Claude Code](https://claude.com/claude-code) — 流程编排
- Obsidian + `obsidian` CLI — 笔记写入
- [gws](https://github.com/nicholasgasior/gws) CLI — Google Drive 备份（可选）

```bash
# macOS
brew install poppler

# 克隆
git clone https://github.com/anon019/Mortimer.git
cd Mortimer

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 GEMINI_API_KEY
```

### 安装 /read 技能

```bash
mkdir -p ~/.claude/skills/read
cp skill/SKILL.md ~/.claude/skills/read/SKILL.md

# 编辑 SKILL.md，将 ~/coding/read 替换为你的实际克隆路径
```

### Claudian 集成（可选）

[Claudian](https://github.com/YishenTu/claudian) 是一个 Obsidian 插件，在 vault 内嵌入 Claude Code 作为 AI 协作者。安装后可直接在 Obsidian 中使用 `/read` 技能。

**原理**：Claudian 通过 Claude Agent SDK 启动本机的 `claude` CLI 作为子进程，工作目录设为 Obsidian vault。它加载 user 级 skill（`~/.claude/skills/`），因此已安装的 `/read` 技能可直接使用。

**配置步骤**：

1. 在 Obsidian 中安装 Claudian 插件
2. 确保 Claudian 设置中 `loadUserClaudeSettings` 为 `true`（默认已开启）
3. 开放工具链路径——编辑 Claudian 设置（Obsidian 设置 → Claudian，或直接编辑 `.claude/claudian-settings.json`）：

```json
{
  "persistentExternalContextPaths": [
    "~/coding/read",
    "/tmp"
  ]
}
```

> **为什么需要这一步？** Claudian 有 vault restriction hook，默认拒绝访问 vault 外路径。`~/coding/read` 是工具链目录，`/tmp` 是 Gemini 分析器的临时文件路径，两者都需要开放。

4. 重启 Obsidian 或新建 Claudian 会话使配置生效

**验证**：在 Claudian 中输入 `/read Atomic Habits`，应能正常启动阅读流程。

**Claudian 特有优势**：

在 Obsidian 中浏览已生成的读书笔记时，Claudian 会自动将当前笔记内容注入上下文。这意味着：

- 打开 `02-精读.md` 后直接说"探索第 3 个视角"，无需指定书名
- 选中一段笔记内容，问"展开讲讲这个观点"
- 打开两本书的笔记，问"对比这两本书的观点"

### 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `GEMINI_API_KEY` | 是 | [Google AI Studio](https://aistudio.google.com/apikey) API key |
| `GEMINI_MODEL` | 否 | 覆盖默认模型（默认 `gemini-3-flash-preview`） |
| `ANNAS_ARCHIVE_KEY` | 否 | [Anna's Archive](https://annas-archive.gl/donate) 会员 key，搜索免费，下载需要 |
| `OBSIDIAN_VAULT_PATH` | 否 | Obsidian vault 路径（默认 `~/Documents/Obsidian Vault`） |
| `GDRIVE_BOOKS_FOLDER_ID` | 否 | Google Drive Books 文件夹 ID |

## 使用

### /read 技能（推荐）

**单本：**
```
/read 史蒂夫·乔布斯传
/read Atomic Habits
帮我读一下穷查理宝典
```

**批量：**
```
/read booklists/my-list.md
/read booklists/my-list.json
帮我批量读这 10 本书：<粘贴书单>
```

### 命令行

```bash
# 搜索
python3 tools/annas-archive/annas.py search "Steve Jobs Walter Isaacson" --format epub --limit 5

# 下载
python3 tools/annas-archive/annas.py download <md5> --output books/

# 提取文本
python3 tools/extract_book.py books/steve-jobs.epub -o /tmp/book_stevejobs.txt

# 分析 (4 个命令)
python3 tools/gemini_analyzer.py overview-skim --book /tmp/book.txt --title "书名" --author "作者" --category biography
python3 tools/gemini_analyzer.py deep-read    --book /tmp/book.txt --title "书名" --author "作者" --category biography
python3 tools/gemini_analyzer.py deep-dive    --book /tmp/book.txt --title "书名" --author "作者" --category biography --topic "主题"
python3 tools/gemini_analyzer.py ask          --book /tmp/book.txt --title "书名" --author "作者" --question "问题"

# 单本全流程 (提取 → 分析 → Obsidian)
bash tools/process_book.sh "books/book.epub" "书名" "作者" "category" "年份" "prefix"

# 批量流程
python3 tools/booklist_to_json.py booklists/list.md          # MD → JSON
python3 tools/batch_download.py booklists/list.json           # 搜索候选
python3 tools/batch_download.py booklists/list.json --confirm # 下载
python3 tools/batch_read.py booklists/list.json               # 并行分析
python3 tools/batch_read.py booklists/list.json --dry-run     # 预览计划
python3 tools/batch_verify.py booklists/list.json             # 验证完整性
```

## Obsidian 输出结构

```
Reading/{category}/{书名} ({作者}, {年份})/
├── 00-概览.md      ← 书籍概要、核心主题、作者背景
├── 01-粗读.md      ← 章节脉络、关键论点梳理
├── 02-精读.md      ← 跨章主题分析 + 5 个推荐探索视角
├── 03-深度-主题A.md ← 深度探索笔记（按需生成）
└── 03-深度-主题B.md
```

10 个分类：biography, business, psychology, self-growth, technology, history, philosophy, finance, literature, science

## 书单格式

```markdown
## 传记 (Biography)

**1. 史蒂夫·乔布斯传** — Walter Isaacson
`steve jobs walter isaacson|epub`

**2. 埃隆·马斯克传** — Walter Isaacson
`elon musk walter isaacson|epub`

## 商业 (Business)

**3. 从零到一** — Peter Thiel
`zero to one peter thiel|epub`
```

## 工具一览

| 工具 | 用途 |
|------|------|
| `tools/gemini_analyzer.py` | Gemini 3 Flash 全书分析（context cache, map-reduce, 10 类模板） |
| `tools/extract_book.py` | EPUB/PDF/TXT → 纯文本 + 目录索引 |
| `tools/annas-archive/annas.py` | Anna's Archive 搜索与下载（mirror 自动切换） |
| `tools/process_book.sh` | 单本全流程（提取 → 分析 → Obsidian） |
| `tools/batch_download.py` | 批量搜索 + 下载（评分选择、断点续传） |
| `tools/batch_read.py` | TPM 感知并行分析（令牌桶、checkpoint） |
| `tools/batch_verify.py` | 笔记完整性验证（模糊匹配、frontmatter 检查） |
| `tools/booklist_to_json.py` | Markdown 书单 → JSON |
| `tools/gdrive_upload.sh` | Google Drive 备份 |

## 兼容性

| 平台 | 支持 | 说明 |
|------|------|------|
| Claude Code (终端) | 完整 | 安装 SKILL.md 后 `/read` 即用，单本+批量全支持 |
| Claudian (Obsidian) | 完整 | 需配置 `persistentExternalContextPaths`，额外支持笔记上下文感知 |
| 其他编程工具 | 工具层 | Python 脚本独立可用，SKILL.md 可作为指令文档参考 |
| 纯命令行 | 完全 | 所有工具都是标准 CLI，可手动使用 |

## 技术特点

- **零 pip 依赖** — 全部 Python stdlib（urllib, json, zipfile, xml.etree）
- **系统依赖仅 pdftotext** — `brew install poppler`（处理 PDF 时需要）

## License

MIT
