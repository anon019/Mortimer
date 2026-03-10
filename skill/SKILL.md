---
name: read
description: Use when the user wants to read, study, or learn about a book. Triggers on book names, author names, reading-related keywords, or explicit "/read" commands. Also handles batch reading when user provides a booklist file or multiple books. Examples - "/read 史蒂夫·乔布斯传", "帮我读一下穷查理宝典", "Atomic Habits 这本书讲什么", "/read booklists/my-list.md", "帮我批量读这些书"
---

# 读书技能 /read (v3)

## 安装配置

1. 将此文件复制到 `~/.claude/skills/read/SKILL.md`
2. 根据你的环境替换以下路径：
   - `~/coding/read` → 项目克隆路径
   - Obsidian vault 路径（默认 `~/Documents/Obsidian Vault`）
3. 确保已安装必要工具（见项目 README）

## 概述

系统性阅读和学习书籍核心内容。支持两种模式：

- **单本模式**：交互式四阶段阅读（概览 → 粗读 → 精读 → 深度探索）
- **批量模式**：从书单自动批量下载、并行分析、验证完整性

**架构：Claude 编排 + Gemini 分析**
- Claude（主模型）：流程编排、用户交互、Obsidian 写入
- Gemini 3 Flash（1M context）：书籍全文分析、内容提炼
- Claude **从不读取** `/tmp/book_{prefix}.txt`，所有文本分析通过 `gemini_analyzer.py` 完成
- `{prefix}` = 书名前 3 个汉字或英文单词的拼音/缩写（如 `elonmusk`、`qiongchali`），避免多书并行时 /tmp 冲突

## 模式检测

解析用户输入，判断进入哪种模式：

| 输入 | 模式 | 示例 |
|------|------|------|
| 书名/作者 | 单本模式 → Phase 1 | `/read 穷查理宝典`、`帮我读 Atomic Habits` |
| `.md` 或 `.json` 文件路径 | 批量模式 → Batch Phase 1 | `/read booklists/my-list.md` |
| 「批量」「batch」关键词 | 批量模式 → Batch Phase 1 | `批量读这些书`、`帮我批量处理` |
| 多本书列表（3+ 本） | 批量模式 → Batch Phase 1 | 用户粘贴了一个书单 |

## 目录结构

```
Reading/
├── biography/
│   ├── 埃隆·马斯克传 (Walter Isaacson, 2023)/
│   │   ├── 00-概览.md
│   │   ├── 01-粗读.md
│   │   ├── 02-精读.md
│   │   ├── 03-深度-五步工作法.md
│   │   └── ...
│   └── 张忠谋自传 (张忠谋, 2024)/
├── business/
├── psychology/
├── self-growth/
├── technology/
├── history/
├── philosophy/
├── finance/
├── literature/
└── science/
```

**命名规则：** `Reading/<category>/<书名> (<作者>, <年份>)/`

## 流程

### Phase 1: 确认书名

1. 解析用户输入（书名/作者/关键词）
2. 用 WebSearch 搜索 `"<输入>" book author year` 获取候选
3. 如果输入模糊或有重名，展示候选列表供用户选择：
   ```
   你要读的是哪本？
   1. 《活着》余华 (1993) — 小说，中国当代文学经典
   2. 《活着》王蒙 (2001) — 散文集
   请选择序号。
   ```
4. 如果明确匹配一本，直接确认：
   ```
   确认：《史蒂夫·乔布斯传》Walter Isaacson (2011)
   ```
5. 确定分类 category（从以下选择）：
   `biography` `business` `psychology` `self-growth` `technology` `history` `philosophy` `finance` `literature` `science`
6. 确定目录路径变量（后续使用）：
   ```
   BOOK_DIR="Reading/<category>/<书名> (<作者>, <年份>)"
   ```

### Phase 1.5: 检测已读进度（自动）

Phase 1 确认书名后，立即检查 Obsidian 是否已有该书的笔记：

```bash
# 检查 BOOK_DIR 下是否已有 3 个核心文件
ls "$HOME/Documents/Obsidian Vault/<BOOK_DIR>/"
```

**情况 A：已有概览+粗读+精读（3 个文件都存在且非空）→ 跳到 Phase 5（恢复模式）**

恢复模式步骤：
1. 在 `books/` 找到原书文件：
```bash
ls ~/coding/read/books/ | grep -i "<关键词>"
```

2. 确保提取文本可用（如果 `/tmp/book_{prefix}.txt` 不存在或内容不匹配）：
```bash
cd ~/coding/read && python3 tools/extract_book.py books/<file> -o /tmp/book_{prefix}.txt
```

3. 读取 02-精读.md 末尾的「推荐深度探索」获取 5 个视角
4. 直接进入 Phase 5 自由探索

**情况 B：部分完成（如有概览+粗读但缺精读）→ 从缺失的阶段继续**

- 缺 01-粗读 + 02-精读 → 从 Phase 3 开始（需要恢复提取文本）
- 缺 02-精读 → 从 Phase 4 开始（需要恢复提取文本）

**情况 C：无任何笔记 → 正常从 Phase 2 开始**

### Phase 2: 获取资源

按优先级自动执行，用户无需介入：

**Step 2.1: 检查本地**
```bash
ls ~/coding/read/books/ | grep -i "<关键词>"
```
有匹配文件 → 跳到 Phase 2.5。

**Step 2.2: Anna's Archive API 搜索+下载**
```bash
# 搜索（优先英文原版，覆盖率更高）
cd ~/coding/read && python3 tools/annas-archive/annas.py search "<书名英文> <作者>" --format epub --limit 5 --json

# 如果英文无结果，搜中文
cd ~/coding/read && python3 tools/annas-archive/annas.py search "<书名中文>" --format epub --limit 5 --json

# 下载（需要 ANNAS_ARCHIVE_KEY 环境变量）
cd ~/coding/read && python3 tools/annas-archive/annas.py download <md5> --output books/
```

**如果 annas.py download 失败（SSL 错误等）**，用以下方式获取下载 URL 后用 curl：
```bash
cd ~/coding/read && python3 -c "
import json, urllib.request, urllib.parse, sys, os
sys.path.insert(0, 'tools/annas-archive')
from annas import get_base_url, fetch_url
base_url = get_base_url()
params = {'md5': '<MD5>', 'key': os.environ['ANNAS_ARCHIVE_KEY'], 'path_index': 0, 'domain_index': 0}
api_url = f'{base_url}/dyn/api/fast_download.json?{urllib.parse.urlencode(params)}'
response = fetch_url(api_url)
data = json.loads(response)
print(data.get('download_url', ''))
" 2>&1 | tail -1
# 然后用 curl 下载
curl -sL -k -o ~/coding/read/books/<filename> '<download_url>' --max-time 120
```

**Step 2.3: WebSearch 直链**
```
WebSearch: "<书名> <作者> filetype:pdf free download"
```
找到直链后用 curl 下载，然后验证文件类型和大小。

**Step 2.4: 降级处理**
如果以上全部失败：
- 告知用户："未找到完整资源，将基于公开资料和 AI 知识生成笔记"
- 用 WebSearch 聚合多源书评、章节摘要、作者访谈
- 后续笔记标注 `source: partial`
- 提示用户可手动获取原书放到 `~/coding/read/books/` 后重新运行

### Phase 2.5: 上传到 Google Drive（下载成功后立即备份）

```bash
cd ~/coding/read && bash tools/gdrive_upload.sh books/<filename> <category>
```

- Google Drive 目录结构：`Books/<category>/<filename>`
- 脚本自动创建分类子文件夹（如 `Books/biography/`）
- 使用 `gws` CLI（Google Workspace CLI），已配置认证

### Phase 2.6: 提取文本内容

根据文件格式提取全文（供 Gemini 分析用）：

**EPUB / PDF:**
```bash
cd ~/coding/read && python3 tools/extract_book.py books/<file>.epub -o /tmp/book_{prefix}.txt
```

**TXT:** 直接复制到 `/tmp/book_{prefix}.txt`：
```bash
cp ~/coding/read/books/<file>.txt /tmp/book_{prefix}.txt
```

**重要：Claude 不读取 /tmp/book_{prefix}.txt。** 此文件仅供 `gemini_analyzer.py` 使用。

### Phase 3: 概览 + 粗读（1 次 Gemini 调用，自动）

1. 调用 Gemini 合并分析（概览 + 粗读一次完成，节省 tokens）：
```bash
cd ~/coding/read && python3 tools/gemini_analyzer.py overview-skim \
  --book /tmp/book_{prefix}.txt \
  --title "<书名>" \
  --author "<作者>" \
  --category <category> 2>/dev/null | tee /tmp/gemini_{prefix}_overview_skim.txt
```

2. 输出以 `====SPLIT====` 分隔为两部分。用 Bash 拆分：
```bash
# 提取概览部分（SPLIT 之前）
sed -n '1,/====SPLIT====/p' /tmp/gemini_{prefix}_overview_skim.txt | sed '$d' > /tmp/{prefix}_overview_content.txt

# 提取粗读部分（SPLIT 之后）
sed -n '/====SPLIT====/,$p' /tmp/gemini_{prefix}_overview_skim.txt | sed '1d' > /tmp/{prefix}_skim_content.txt
```

3. 提取 themes 并写入 Obsidian — 00-概览：

Gemini 输出的概览第一行格式为 `themes: 主题1, 主题2, 主题3`，提取后写入 frontmatter：
```bash
# 提取 themes 行（第一行）和正文（第二行起）
THEMES=$(head -1 /tmp/{prefix}_overview_content.txt | sed 's/^themes: //')
OVERVIEW=$(tail -n +2 /tmp/{prefix}_overview_content.txt)
obsidian create path="<BOOK_DIR>/00-概览.md" content="---
title: <书名>
author: <作者>
category: <分类>
date: <今天日期>
status: reading
source: full
themes: [${THEMES}]
---

${OVERVIEW}"
```

4. 写入 Obsidian — 01-粗读：
```bash
SKIM=$(cat /tmp/{prefix}_skim_content.txt)
obsidian create path="<BOOK_DIR>/01-粗读.md" content="---
title: <书名> - 粗读
parent: \"[[00-概览]]\"
---

${SKIM}"
```

**处理 Obsidian 重名：** 如果 `obsidian create` 生成了带数字后缀的文件（如 "xxx 1.md"），说明已有同名旧文件。删除旧文件后重命名新文件。

### Phase 4: 精读（1 次 Gemini 调用，自动）

1. 调用 Gemini 精读分析：
```bash
cd ~/coding/read && python3 tools/gemini_analyzer.py deep-read \
  --book /tmp/book_{prefix}.txt \
  --title "<书名>" \
  --author "<作者>" \
  --category <category> 2>/dev/null
```

2. 将输出写入 Obsidian：
```bash
obsidian create path="<BOOK_DIR>/02-精读.md" content="---
title: <书名> - 精读
parent: \"[[00-概览]]\"
---

<Gemini 输出的精读内容>"
```

输出末尾包含费曼检验 + 5 个推荐深度探索视角。

3. 自动更新状态为 `read`（表示核心阅读完成，可选深度探索）：
```bash
obsidian property:set path="<BOOK_DIR>/00-概览.md" name="status" value="read"
```

### Phase 5: 进入自由探索

输出提示：
```
概览、粗读和精读已完成，笔记已保存到 Obsidian <BOOK_DIR>/ 目录。

精读末尾已生成 5 个推荐深度探索视角，你可以：
- 输入序号（如 "1" 或 "3"）探索对应视角
- 输入「全部探索」一次性生成所有深度笔记
- 自由提问（如 "这本书对XXX的看法是什么？"）— 直接基于全文回答，不生成笔记
- 输入「结束阅读」将状态标记为 completed
```

### 自由提问处理

如果用户输入的是问题（非序号、非「全部探索」、非「结束阅读」），使用 `ask` 命令直接回答：
```bash
cd ~/coding/read && python3 tools/gemini_analyzer.py ask \
  --book /tmp/book_{prefix}.txt \
  --title "<书名>" \
  --author "<作者>" \
  --question "<用户问题>" 2>/dev/null
```
直接将 Gemini 回答展示给用户，不写入 Obsidian。

### 深度探索处理

对于序号选择或自定义深度主题：

0. **确保提取文本可用**（恢复模式或新会话时 `/tmp/book_{prefix}.txt` 可能不存在）：
```bash
# 检查提取文本是否存在
if [ ! -f /tmp/book_{prefix}.txt ] || [ ! -s /tmp/book_{prefix}.txt ]; then
  # 在 books/ 中找到原书文件
  ls ~/coding/read/books/ | grep -i "<关键词>"
  # 重新提取
  cd ~/coding/read && python3 tools/extract_book.py books/<file> -o /tmp/book_{prefix}.txt
fi
```

1. 调用 Gemini 深度分析：
```bash
cd ~/coding/read && python3 tools/gemini_analyzer.py deep-dive \
  --book /tmp/book_{prefix}.txt \
  --title "<书名>" \
  --author "<作者>" \
  --category <category> \
  --topic "<探索主题>" 2>/dev/null
```

2. 写入 Obsidian：
```bash
obsidian create path="<BOOK_DIR>/03-深度-<主题>.md" content="---
title: <书名> - <主题>
parent: \"[[02-精读]]\"
type: deep-dive
---

<Gemini 输出的深度内容>"
```

### 「全部探索」并行模式

当用户输入「全部探索」时：
1. 从 02-精读.md 读取 5 个推荐视角
2. 为每个视角启动并行 Agent：

```
Agent(
  prompt="你是读书笔记助手。为《<书名>》生成关于「<视角名>」的深度笔记。

  步骤：
  0. 确保提取文本可用：
     检查 /tmp/book_{prefix}.txt 是否存在且非空。
     如果不存在，在 ~/coding/read/books/ 中找到原书文件，然后：
     cd ~/coding/read && python3 tools/extract_book.py books/<file> -o /tmp/book_{prefix}.txt

  1. 调用 Gemini 分析工具：
     cd ~/coding/read && python3 tools/gemini_analyzer.py deep-dive \
       --book /tmp/book_{prefix}.txt \
       --title '<书名>' \
       --author '<作者>' \
       --category <category> \
       --topic '<视角名>'

  2. 用 obsidian create 写入：
     obsidian create path=\"<BOOK_DIR>/03-深度-<主题>.md\" content=\"---
     title: <书名> - <主题>
     parent: \\\"[[02-精读]]\\\"
     type: deep-dive
     ---

     <Gemini 输出内容>\"
  ",
  description="深度探索: <视角名>",
  mode="auto"
)
```

3. 等待所有 Agent 完成后汇总结果
4. 更新概览中的阅读进度

### 结束阅读

当用户说「结束阅读」时：
```bash
obsidian property:set path="<BOOK_DIR>/00-概览.md" name="status" value="completed"
```

---

## 批量模式

当模式检测判定为批量模式时，执行以下流程。

### Batch Phase 1: 准备书单

**情况 A：用户提供了 `.json` 文件**
直接使用，跳到 Batch Phase 2。

**情况 B：用户提供了 `.md` 文件**
```bash
cd ~/coding/read && python3 tools/booklist_to_json.py <file.md>
```
输出 JSON 文件路径，供后续步骤使用。

**情况 C：用户粘贴了书单文本或口述多本书**
1. 为用户生成 Markdown 书单文件，格式如下：
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

规则：
- 按 10 个分类归类（biography, business, psychology, self-growth, technology, history, philosophy, finance, literature, science）
- 每本书需要编号、中文书名、作者
- 搜索行格式：`` `英文搜索词|格式` ``，格式优先 epub，其次 pdf
- 保存到 `~/coding/read/booklists/YYYY-MM-DD.md`

2. 展示书单让用户确认（可调整分类、增删书目）
3. 确认后转换为 JSON：
```bash
cd ~/coding/read && python3 tools/booklist_to_json.py booklists/YYYY-MM-DD.md
```

### Batch Phase 2: 批量搜索下载

**Step 2.1: 搜索候选**
```bash
cd ~/coding/read && python3 tools/batch_download.py <booklist.json>
```
- 自动在 Anna's Archive 搜索每本书的候选
- 评分并自动选择最佳匹配
- 结果保存到 `booklists/YYYY-MM-DD-candidates.json`
- 支持断点续传：重新运行跳过已搜索的书

搜索完成后，向用户汇报结果：
```
搜索完成：N 本找到候选，M 本未找到。
未找到的书：<列表>
是否开始下载？
```

**Step 2.2: 确认并下载**
```bash
cd ~/coding/read && python3 tools/batch_download.py <booklist.json> --confirm
```
- 下载所有选中的候选
- 自动重命名为 `{书名} - {作者}.{ext}`
- 如果配置了 `GDRIVE_BOOKS_FOLDER_ID`，自动上传 Google Drive 备份
- 失败的记录到 `booklists/YYYY-MM-DD-download-failed.json`

下载完成后汇报：
```
下载完成：X 本成功，Y 本失败。
失败的书：<列表>
是否开始批量分析？
```

### Batch Phase 3: 批量分析

**Step 3.1: 预览计划（可选）**
```bash
cd ~/coding/read && python3 tools/batch_read.py <booklist.json> --dry-run
```
展示预估 token 消耗、并行计划，让用户了解规模。

**Step 3.2: 执行并行分析**
```bash
cd ~/coding/read && python3 tools/batch_read.py <booklist.json>
```
- TPM 感知的动态令牌桶调度（900K TPM 限制）
- 最多 5 个并行 `process_book.sh` 进程
- 小书优先调度，最大化吞吐量
- 自动跳过 Obsidian 中已完成的书
- 失败的书自动重试 1 轮
- 每本书完成后实时输出进度

**重要**：此命令可能运行较长时间（取决于书单大小）。运行后告知用户：
```
批量分析已启动，共 N 本书。
预计耗时视书籍大小而定，小书约 2-3 分钟/本，大书约 5-10 分钟/本。
进度会实时输出到终端。
```

### Batch Phase 4: 验证完整性

分析完成后自动验证：
```bash
cd ~/coding/read && python3 tools/batch_verify.py <booklist.json> --verbose
```
- 检查每本书的 3 个核心文件是否存在且非空
- 检查 frontmatter 字段完整性
- 模糊匹配目录名（处理标点差异）

向用户汇报最终结果：
```
验证完成：
✓ X 本书笔记完整
✗ Y 本书有问题：
  - 《书名》: 缺少 02-精读.md
  - 《书名》: 目录未找到

是否对失败的书逐本重试？（将切换到单本模式逐个处理）
```

### Batch Phase 5: 失败处理

如果有失败的书，提供两个选项：

**选项 A：自动重试失败的书**
对每本失败的书，切换到单本模式（Phase 1-4）逐个处理。优势是可以走完整的降级路径（Anna's Archive → WebSearch → partial）。

**选项 B：跳过，完成批量任务**
标记批量任务完成，告知用户可以后续单独 `/read <书名>` 处理失败的书。

## 关键规则

1. **所有笔记内容使用中文**，无论原书语言
2. **获取资源时优先英文原版**（覆盖率更高），中文版作为备选
3. **不卡住流程** — 获取不到全文时降级继续，标注 source: partial
4. **通过 obsidian CLI 操作** — 使用 `obsidian create`（frontmatter 内嵌）减少 API 调用
5. **书籍文件保存到** `~/coding/read/books/`
6. **Anna's Archive 工具路径** `~/coding/read/tools/annas-archive/annas.py`
7. **文本提取工具路径** `~/coding/read/tools/extract_book.py`
8. **Gemini 分析工具路径** `~/coding/read/tools/gemini_analyzer.py`
9. **内容验证** — 下载后检查文件类型、大小，排除摘要版/损坏文件
10. **下载成功后立即上传 Google Drive** — `bash tools/gdrive_upload.sh <file> <category>`（Phase 2.5）
11. **Claude 不读取书籍正文** — `/tmp/book_{prefix}.txt` 仅供 Gemini 分析工具使用，Claude 从不直接 Read 此文件
12. **{prefix} 命名** — 取书名前几个字的拼音或缩写（如 `elonmusk`、`qiongchali`），确保 /tmp 下多书不冲突
13. **Frontmatter 内嵌** — 所有笔记创建时将属性写入 content，不逐个 property:set（唯一例外：Phase 4 完成后用 `property:set` 更新 status 为 read）
14. **Gemini 环境变量** — 需要 `GEMINI_API_KEY`（配置在 `.env` 或 shell profile 中）
15. **目录路径** — `Reading/<category>/<书名> (<作者>, <年份>)/`，按类别物理分目录
16. **Obsidian 重名处理** — 如果 create 生成了 "xxx 1.md"，删除旧文件后重命名
17. **分类自适应** — `gemini_analyzer.py` 会根据 `--category` 自动调整分析提示词（如 biography 侧重人物心理，business 侧重商业模型），无需手动适配
