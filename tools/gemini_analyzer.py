#!/usr/bin/env python3
"""
Gemini-powered book analysis tool (v3).

Offloads heavy text analysis to Gemini 3 Flash (1M context window),
keeping the main Claude context clean for orchestration.

Commands:
  overview-skim  - Combined overview + skim read (1 API call, saves tokens)
  deep-read      - Cross-chapter thematic analysis
  deep-dive      - Single-topic deep exploration
  ask            - Free-form question about a book

Usage:
  python3 gemini_analyzer.py overview-skim --book /tmp/book_full.txt --title "书名" --author "作者" --category biography
  python3 gemini_analyzer.py deep-read    --book /tmp/book_full.txt --title "书名" --author "作者" --category biography
  python3 gemini_analyzer.py deep-dive    --book /tmp/book_full.txt --title "书名" --author "作者" --category biography --topic "主题"
  python3 gemini_analyzer.py ask          --book /tmp/book_full.txt --title "书名" --author "作者" --question "问题"

Environment:
  GEMINI_API_KEY - Required. Google AI API key.
  GEMINI_MODEL   - Optional. Default: gemini-3-flash-preview
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_MODEL = "gemini-3-flash-preview"
CACHE_MAX_AGE_HOURS = 24
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
API_URL_TEMPLATE = API_BASE + "/models/{}:generateContent"
CACHED_CONTENTS_URL = API_BASE + "/cachedContents"

# Max tokens for book text in a single Gemini call (leave margin for prompt overhead)
BOOK_TOKEN_LIMIT = 800_000

# ---------------------------------------------------------------------------
# Gemini Context Caching (REST API)
# ---------------------------------------------------------------------------

def _gemini_cache_file(book_path: str) -> str:
    """Return the /tmp path for storing a Gemini cached content name."""
    path_hash = hashlib.md5(os.path.abspath(book_path).encode()).hexdigest()[:12]
    return f"/tmp/gemini_cache_{path_hash}.json"


def _create_gemini_cache(book_text: str, model_name: str, api_key: str, display_name: str) -> dict | None:
    """Create a Gemini cached content via REST API. Returns cache metadata or None on failure."""
    payload = {
        "model": f"models/{model_name}",
        "displayName": display_name,
        "contents": [{"role": "user", "parts": [{"text": book_text}]}],
        "ttl": "3600s",
    }
    data = json.dumps(payload).encode("utf-8")
    url = f"{CACHED_CONTENTS_URL}?key={api_key}"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result
    except Exception as e:
        print(f"[Cache] Failed to create cached content: {e}", file=sys.stderr)
        return None


def _get_gemini_cache(cache_name: str, api_key: str) -> dict | None:
    """Retrieve a Gemini cached content by name. Returns metadata or None if expired/missing."""
    url = f"{API_BASE}/{cache_name}?key={api_key}"
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[Cache] Cached content expired or not found: {cache_name}", file=sys.stderr)
        else:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            print(f"[Cache] Error retrieving cache ({e.code}): {body[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[Cache] Error retrieving cache: {e}", file=sys.stderr)
        return None


def get_or_create_gemini_cache(book_path: str, book_text: str) -> str | None:
    """Get or create a Gemini cached content for the book. Returns cache name or None.

    The cache name is persisted to a local file so subsequent commands (deep-read,
    deep-dive) can reuse the same cached content within the 1-hour TTL.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    model_name = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    cache_file = _gemini_cache_file(book_path)

    # Try to load existing cache name from file
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                info = json.load(f)
            cache_name = info.get("name")
            if cache_name:
                # Verify it still exists on the server
                remote = _get_gemini_cache(cache_name, api_key)
                if remote:
                    print(f"[Cache] Reusing cached content: {cache_name}", file=sys.stderr)
                    return cache_name
                else:
                    print(f"[Cache] Stored cache expired, creating new one...", file=sys.stderr)
        except (json.JSONDecodeError, KeyError):
            pass

    # Create new cache
    prefix = _book_prefix(book_path)
    print(f"[Cache] Creating Gemini cached content for '{prefix}'...", file=sys.stderr)
    result = _create_gemini_cache(book_text, model_name, api_key, f"book-{prefix}")
    if not result or "name" not in result:
        print(f"[Cache] Could not create cache, will send full text.", file=sys.stderr)
        return None

    cache_name = result["name"]
    # Persist cache name
    with open(cache_file, "w") as f:
        json.dump({"name": cache_name, "book_path": os.path.abspath(book_path)}, f)
    print(f"[Cache] Created cached content: {cache_name}", file=sys.stderr)
    return cache_name


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

def call_gemini(prompt: str, temperature: float = 0.4, max_tokens: int = 65536,
                cached_content: str | None = None) -> str:
    """Send prompt to Gemini and return response text.

    Args:
        prompt: The prompt text to send.
        temperature: Sampling temperature.
        max_tokens: Maximum output tokens.
        cached_content: Optional Gemini cached content name (e.g. "cachedContents/abc123").
            If provided, the prompt should NOT include the book text (it's in the cache).
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    url = f"{API_URL_TEMPLATE.format(model)}?key={api_key}"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    if cached_content:
        payload["cachedContent"] = cached_content

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )

    max_retries = 5
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            if e.code == 429 and attempt < max_retries - 1:
                # Parse retry delay from error message, or use exponential backoff
                wait = 30 * (2 ** attempt) + random.uniform(0, 10)
                print(f"Rate limited (429). Retry {attempt+1}/{max_retries} in {wait:.0f}s...",
                      file=sys.stderr)
                time.sleep(wait)
                # Rebuild request (urlopen consumes it)
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"}
                )
                continue
            # If cached_content caused an error, mention it
            if cached_content and e.code in (400, 404):
                print(f"[Cache] Cached content may be invalid ({e.code}). "
                      f"Consider using --no-cache.", file=sys.stderr)
            print(f"Gemini API HTTP {e.code}: {body[:500]}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 15 * (2 ** attempt) + random.uniform(0, 5)
                print(f"Connection error: {e}. Retry {attempt+1}/{max_retries} in {wait:.0f}s...",
                      file=sys.stderr)
                time.sleep(wait)
                # Rebuild request (urlopen consumes it)
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"}
                )
                continue
            print(f"Gemini API error: {e}", file=sys.stderr)
            sys.exit(1)

    candidates = result.get("candidates", [])
    if not candidates:
        block_reason = result.get("promptFeedback", {}).get("blockReason", "unknown")
        print(f"Gemini returned no candidates. Block reason: {block_reason}", file=sys.stderr)
        sys.exit(1)

    parts = candidates[0].get("content", {}).get("parts", [])
    text = parts[0].get("text", "") if parts else ""

    # Print token usage to stderr for debugging
    usage = result.get("usageMetadata", {})
    if usage:
        cached_tokens = usage.get("cachedContentTokenCount")
        cache_info = f", cached: {cached_tokens}" if cached_tokens else ""
        print(
            f"Tokens - input: {usage.get('promptTokenCount', '?')}, "
            f"output: {usage.get('candidatesTokenCount', '?')}, "
            f"total: {usage.get('totalTokenCount', '?')}{cache_info}",
            file=sys.stderr,
        )

    return text


def estimate_tokens(text: str) -> int:
    """Estimate token count. Chinese ~1.5 chars/token, English ~4 chars/token."""
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    non_cjk = len(text) - cjk
    return int(cjk / 1.5 + non_cjk / 4)


def split_by_chapters(text: str, max_tokens: int = BOOK_TOKEN_LIMIT) -> list[str]:
    """Split book text at chapter boundaries into parts under max_tokens each."""
    # Split at chapter markers: ===== Chapter N: title =====
    chapters = re.split(r'(?=={5} Chapter \d+:)', text)
    chapters = [c for c in chapters if c.strip()]

    if not chapters:
        # No chapter markers — split by estimated character count
        chars_per_part = int(max_tokens * 1.8)
        return [text[i:i + chars_per_part] for i in range(0, len(text), chars_per_part)]

    parts = []
    current_part = []
    current_tokens = 0

    for chapter in chapters:
        ch_tokens = estimate_tokens(chapter)
        if current_tokens + ch_tokens > max_tokens and current_part:
            parts.append(''.join(current_part))
            current_part = [chapter]
            current_tokens = ch_tokens
        else:
            current_part.append(chapter)
            current_tokens += ch_tokens

    if current_part:
        parts.append(''.join(current_part))

    return parts


def read_book(path: str) -> str:
    """Read book text file, with size warning."""
    if not os.path.exists(path):
        print(f"Error: Book file not found: {path}", file=sys.stderr)
        sys.exit(1)

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > 4:
        print(
            f"Warning: Book text is {size_mb:.1f}MB. "
            f"May approach Gemini's 1M token limit.",
            file=sys.stderr,
        )

    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Prompt: Overview + Skim (merged, 1 API call)
# ---------------------------------------------------------------------------

OVERVIEW_SKIM_PROMPT = """\
你是一位专业的阅读分析师。根据以下书籍全文，完成两项分析任务。

书名：{title}
作者：{author}
分类：{category}

## 任务一：书籍概览（约 1500 字）

在概览输出的第一行，输出 themes: 后跟 3-5 个本书核心主题词，用逗号分隔。这行之后再开始正式的概览内容。

例如：
themes: 主题1, 主题2, 主题3, 主题4, 主题5

请按以下结构输出（themes 行之后）：

## 基本信息

- **作者**：（全名 + 一句话介绍）
- **出版年**：
- **页数**：（如能推断，否则写"—"）
- **原书语言**：

## 作者背景

（2-3 句话：为什么这个人有资格写这本书？与书中主题/人物的关系，此前的代表作）

## 研究方法与信息来源

（2-3 句话：作者的研究方法、信息来源、写作视角。如传记类：采访了谁、跟踪多久；如学术类：基于什么研究/实验；如商业类：数据来源和案例选取方法）

## 一句话概括

（一句话，不超过 50 字）

## 为什么值得读

（2-3 句话，具体说明对读者的价值）

## 全书结构

（描述这本书分几大部分、按什么逻辑组织，帮助读者建立全局地图）

## 核心主题

1. （主题名 + 一句话说明）
2.
3.
4.
5.

## 适合谁读

- **适合**：（什么样的读者会从中获益最多）
- **不适合**：（什么样的读者可能觉得不值得）

## 争议与评价

（这本书受到的主要赞誉和批评，保持客观）

## 推荐阅读策略

（具体建议：哪些章节重点读，哪些可以略读，用什么方法读效果最好）

====SPLIT====

## 任务二：粗读笔记（约 8000-10000 字）

将全书按自然段落/阶段分成 5-8 个大部分（根据书的结构，可以是时间线、主题线、或作者的章节分组）。

对每个部分，按以下结构输出：

## [部分标题]（涵盖的章节范围或时间范围）

### 背景与设定
（这部分的时代背景、起因、前情）

### 核心事件与发展
（按重要性列出这部分最关键的事件、论点或情节，每个事件 2-4 句话描述，包含具体细节、数据或人名）

### 关键人物
（这部分新出现或起关键作用的人物，及其角色）

### 结果与影响
（这部分的结局、对后续的影响）

### 金句摘录
> "原文引用"（标注章节）

每个部分写 800-1500 字，覆盖全书内容，不要遗漏重要章节。

最后输出：

---

## 全书脉络

（300 字以内，用一段话串联各部分的逻辑关系，帮助读者看清全书的主线和走向）

---

要求：
- 所有内容使用中文
- 输出纯 Markdown 格式（不要用代码块包裹）
- 两个任务之间用 ====SPLIT==== 分隔
- 粗读部分要足够详细，让没读过原书的人也能了解每个阶段发生了什么
- 不要在输出中包含"任务一""任务二"等字样，直接从 themes: 行开始输出，然后是"## 基本信息"开始概览，从"## [部分标题]"开始输出粗读

<book>
{book_text}
</book>
"""

# ---------------------------------------------------------------------------
# Prompt: Deep Read (cross-chapter thematic analysis)
# ---------------------------------------------------------------------------

DEEP_READ_BIOGRAPHY = """\
对这本传记进行跨章节的主题提炼。不要按时间线组织（粗读已经做了），而是跨章节提炼核心洞察。

请从以下维度中选择 6-8 个最有价值的主题：
性格密码、决策模型、管理哲学、创新方法论、人际模式、失败与危机应对、成长曲线、核心矛盾、历史定位。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"父亲的阴影与共生依赖"而非"核心洞察"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的具体事件、数据、人物言行（标注章节或时间）
  🔍 深层分析：模式识别、因果推理、与其他案例的对比、批判性思考
- 避免纯叙事——每一段事件描述后，必须跟随你的分析判断\
"""

DEEP_READ_BUSINESS = """\
对这本商业书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心商业洞察。

识别 6-8 个核心商业主题。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"飞轮效应的冷启动困境"而非"核心框架"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的具体案例、数据、商业场景
  🔍 深层分析：底层逻辑、适用边界、可迁移性、批判性思考
- 避免纯叙事——每一段案例描述后，必须跟随你的分析判断\
"""

DEEP_READ_PSYCHOLOGY = """\
对这本心理学书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心理论和效应。

识别 6-8 个核心理论/效应。

输出格式要求：
- 主题用编号标题：## 1. [理论/效应名]、## 2. [理论/效应名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"锚定效应在薪资谈判中的陷阱"而非"核心机制"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的关键实验、数据、研究场景
  🔍 深层分析：机制解释、适用边界、理论争议、生活应用
- 避免纯叙事——每一段实验描述后，必须跟随你的分析判断\
"""

DEEP_READ_SELF_GROWTH = """\
对这本自我成长书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心方法论和原则。

请从以下维度中选择 6-8 个最有价值的主题：
核心原则、行为系统、习惯机制、心智模型、实践方法论、常见陷阱、科学依据、身份转变、环境设计、反馈循环。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"身份认同驱动行为改变"而非"底层原理"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的案例、研究、数据
  🔍 深层分析：底层原理、适用边界、常见陷阱、与其他方法论的对比
- 避免纯叙事——每一段案例描述后，必须跟随你的分析判断\
"""

DEEP_READ_LITERATURE = """\
对这部文学作品进行跨章节的主题提炼。不要按情节顺序组织（粗读已经做了），而是跨章节提炼核心文学主题。

请从以下维度中选择 5-8 个最有价值的主题：
人物命运、核心隐喻、叙事结构、时代映射、情感母题、道德困境、身份认同、权力关系、语言风格、文化符号。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"福贵的牛与生命的隐喻"而非"主题阐释"）
- 每个主题必须同时包含：
  📖 具体证据：引用原文关键场景、意象、人物言行
  🔍 深层分析：写作技法、文化/哲学解读、与其他作品的对比
- 避免纯叙事——每一段情节描述后，必须跟随你的分析判断\
"""

DEEP_READ_TECHNOLOGY = """\
对这本技术书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心技术洞察。

请从以下维度中选择 6-8 个最有价值的主题：
技术演进路径、创新模式与突破点、对社会的影响与重塑、与竞争技术的对比分析、工程实践方法论、技术债务与妥协、未来趋势与前瞻、底层架构设计哲学。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"从单体到微服务的范式转移"而非"技术趋势"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的具体技术细节、架构决策、数据对比
  🔍 深层分析：技术演进逻辑、设计权衡、对行业格局的影响、批判性思考
- 避免纯叙事——每一段技术描述后，必须跟随你的分析判断\
"""

DEEP_READ_HISTORY = """\
对这本历史书进行跨章节的主题提炼。不要按时间线组织（粗读已经做了），而是跨章节提炼核心历史洞察。

请从以下维度中选择 6-8 个最有价值的主题：
因果链条与关键转折、多元视角下的事件解读、一手史料与证据分析、历史对当下的启示、史学争论与不同叙事、权力结构与制度演变、个人与时代的互动、文化与观念的长期变迁。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"威斯特伐利亚体系的连锁反应"而非"历史影响"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的史料、数据、人物言行（标注时间与出处）
  🔍 深层分析：因果推理、不同立场的比较、对现代的映射、批判性思考
- 避免纯叙事——每一段历史描述后，必须跟随你的分析判断\
"""

DEEP_READ_PHILOSOPHY = """\
对这本哲学书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心哲学论证。

请从以下维度中选择 6-8 个最有价值的主题：
论证结构与逻辑链、核心前提的检验、与其他哲学传统的对比、实践意义与现实应用、思想实验与直觉泵、概念创新与定义辨析、潜在反驳与回应、对后世思想的影响。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"自由意志与决定论的调和尝试"而非"核心论点"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的论证、思想实验、关键定义
  🔍 深层分析：逻辑有效性评估、前提合理性、与其他哲学家的对照、批判性思考
- 避免纯叙事——每一段论证描述后，必须跟随你的分析判断\
"""

DEEP_READ_FINANCE = """\
对这本金融书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心金融洞察。

请从以下维度中选择 6-8 个最有价值的主题：
市场机制与价格发现、风险收益框架、历史先例与周期规律、量化推理与数据分析、行为偏差与非理性因素、监管博弈与制度设计、资产配置方法论、金融创新与系统性风险。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"杠杆周期中的明斯基时刻"而非"市场分析"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的市场数据、案例、交易场景
  🔍 深层分析：底层机制、风险评估、历史类比、批判性思考
- 避免纯叙事——每一段金融案例描述后，必须跟随你的分析判断\
"""

DEEP_READ_SCIENCE = """\
对这本科学书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心科学洞察。

请从以下维度中选择 6-8 个最有价值的主题：
实验方法论与研究设计、证据评估与可信度、范式转换与科学革命、跨学科关联与启发、当前认知的局限性、科学争议与未解之谜、从理论到应用的路径、科学社群与同行评审。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（如"双缝实验对实在论的挑战"而非"实验分析"）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的实验设计、数据、研究结论
  🔍 深层分析：方法论评估、证据强度、替代解释、批判性思考
- 避免纯叙事——每一段实验描述后，必须跟随你的分析判断\
"""

DEEP_READ_DEFAULT = """\
对这本书进行跨章节的主题提炼。不要按章节顺序组织（粗读已经做了），而是跨章节提炼核心洞察。

请从书中内容选择 6-8 个最有价值的主题进行深度分析。

输出格式要求：
- 主题用编号标题：## 1. [主题名]、## 2. [主题名]...
- 每个主题 600-1000 字，包含 2-3 个子标题
- 子标题必须是具体的、与内容相关的（不要用"核心论点""深层洞察"这类通用标题）
- 每个主题必须同时包含：
  📖 具体证据：引用书中的具体案例、数据、论述
  🔍 深层分析：论证逻辑、适用边界、批判性思考、与其他观点的对比
- 避免纯叙事——每一段描述后，必须跟随你的分析判断\
"""

DEEP_READ_TEMPLATES = {
    "biography": DEEP_READ_BIOGRAPHY,
    "business": DEEP_READ_BUSINESS,
    "psychology": DEEP_READ_PSYCHOLOGY,
    "self-growth": DEEP_READ_SELF_GROWTH,
    "literature": DEEP_READ_LITERATURE,
    "technology": DEEP_READ_TECHNOLOGY,
    "history": DEEP_READ_HISTORY,
    "philosophy": DEEP_READ_PHILOSOPHY,
    "finance": DEEP_READ_FINANCE,
    "science": DEEP_READ_SCIENCE,
}

PERSPECTIVES_GUIDE = {
    "biography": "根据传记内容，从以下方向生成 5 个视角：人物转折点 / 决策分析 / 人际关系 / 管理哲学 / 与同时代人物对比",
    "business": "根据商业内容，从以下方向生成 5 个视角：商业模式拆解 / 竞争策略 / 增长飞轮 / 失败案例 / 可迁移方法论",
    "psychology": "根据心理学内容，从以下方向生成 5 个视角：核心实验 / 生活应用 / 理论对比 / 反对观点 / 自我诊断",
    "self-growth": "根据自我成长内容，从以下方向生成 5 个视角：行动清单 / 习惯系统 / 实践计划 / 方法论对比 / 常见误区",
    "literature": "根据文学作品内容，从以下方向生成 5 个视角：叙事结构 / 人物分析 / 主题解读 / 写作技法 / 时代背景",
    "technology": "根据技术内容，从以下方向生成 5 个视角：技术演进路径 / 架构设计哲学 / 与竞品技术对比 / 对行业格局的影响 / 未来趋势预判",
    "history": "根据历史内容，从以下方向生成 5 个视角：关键转折点 / 多元视角对比 / 制度演变 / 历史对当下的启示 / 史学争议",
    "philosophy": "根据哲学内容，从以下方向生成 5 个视角：核心论证结构 / 与其他哲学传统对比 / 思想实验分析 / 现实应用 / 主要反驳与回应",
    "finance": "根据金融内容，从以下方向生成 5 个视角：市场机制拆解 / 风险管理框架 / 历史周期对比 / 行为金融偏差 / 投资策略验证",
    "science": "根据科学内容，从以下方向生成 5 个视角：核心理论 / 实验设计 / 科学争议 / 跨学科关联 / 前沿进展",
}

DEEP_READ_PROMPT = """\
你是一位专业的阅读分析师。请对以下书籍进行跨章节的深度主题提炼。

书名：{title}
作者：{author}
分类：{category}

## 任务

{category_template}

完成主题提炼后，还需要生成以下三个部分：

---

## 全书总结

### 核心收获

1.（最重要的收获，2-3 句话展开）
2.（第二重要的收获，2-3 句话展开）
3.（第三重要的收获，2-3 句话展开）

---

## 费曼检验

如果要向一个完全不了解这个领域的朋友解释这本书，你可以这样说：

「（200字以内，零术语，用日常语言讲清楚核心观点。想象你在跟一个聪明但完全不了解这个领域的朋友聊天）」

---

## 推荐深度探索

以下 5 个视角基于本书内容生成，按认知价值排序：

{perspectives_guide}

对每个视角：
1. 🔬 **[视角名]** — [为什么值得深入，一句话]

要求：
- 视角必须来源于书中实际内容，不要泛泛而谈
- 至少 1 个跨书/跨领域对比视角
- 至少 1 个批判/反思视角

---

请输出纯 Markdown 格式（不要用代码块包裹）。所有内容使用中文。

<book>
{book_text}
</book>
"""

# ---------------------------------------------------------------------------
# Prompt: Deep Dive (single topic)
# ---------------------------------------------------------------------------

DEEP_DIVE_PROMPT = """\
你是一位专业的阅读分析师。请针对以下书籍的特定视角，进行深度分析。

书名：{title}
作者：{author}
分类：{category}
探索视角：{topic}

## 任务

围绕「{topic}」这个视角，对书中相关内容进行深度分析。

要求：
1. 找到书中所有与该视角相关的内容（具体章节、事件、论述）
2. 进行有深度的分析，不要停留在表面总结
3. 提供独到的见解和批判性思考
4. 如果有跨领域的关联，请指出
5. 总字数 2000-4000 字
6. 所有内容使用中文
7. 输出纯 Markdown 格式（不要用代码块包裹）

建议的结构（可根据具体内容调整）：
- 核心发现/论点
- 具体案例和证据（引用书中内容）
- 深层分析和独到见解
- 跨领域联想
- 批判与反思

<book>
{book_text}
</book>
"""

# ---------------------------------------------------------------------------
# Prompt: Ask (free-form question about a book)
# ---------------------------------------------------------------------------

ASK_PROMPT = """\
你是一个读书助手。以下是《{title}》({author}) 的全文。

用户问题：{question}

请基于书籍内容准确回答这个问题。要求：
- 引用书中的具体内容来支撑回答
- 如果书中没有直接涉及这个问题，诚实说明并提供相关的间接信息
- 用中文回答
- 回答要有深度，不要泛泛而谈

<book>
{book_text}
</book>
"""

ASK_PROMPT_CACHED = """\
你是一个读书助手。之前提供的内容是《{title}》({author}) 的全文。

用户问题：{question}

请基于书籍内容准确回答这个问题。要求：
- 引用书中的具体内容来支撑回答
- 如果书中没有直接涉及这个问题，诚实说明并提供相关的间接信息
- 用中文回答
- 回答要有深度，不要泛泛而谈
"""

# ---------------------------------------------------------------------------
# Cached prompt helper
# ---------------------------------------------------------------------------

def strip_book_text_from_prompt(prompt: str) -> str:
    """Remove the <book>...</book> section from a prompt for use with cached content.

    When using Gemini context caching, the book text is already in the cache,
    so we replace the <book> block with a reference note.
    """
    return re.sub(
        r'\n*<book>\n.*?\n</book>\n*',
        '\n\n（书籍全文已通过缓存提供，请基于缓存中的书籍内容进行分析。）\n',
        prompt,
        flags=re.DOTALL,
    )


# ---------------------------------------------------------------------------
# Map-Reduce prompts (for books exceeding context window)
# ---------------------------------------------------------------------------

MAP_ANALYSIS_PROMPT = """\
你是一位专业的阅读分析师。以下是《{title}》({author}) 的第 {part_num}/{total_parts} 部分。

请对这部分内容进行详尽分析，输出以下内容（约 4000-6000 字）：

## 内容概述
（这部分讲了什么，核心事件/论点/发展脉络）

## 关键事件与细节
（按重要性列出这部分最关键的事件、论点或情节，每个 3-5 句话，包含具体数据、人名、时间）

## 关键人物
（这部分出现的重要人物及其角色、行为、影响）

## 核心洞察
（这部分最有价值的观点、方法论、模式）

## 金句摘录
（3-5 条原文引用，标注章节）

## 主题标签
（列出 5-8 个这部分涉及的主题关键词）

要求：
- 使用中文输出
- 尽可能详细，不要遗漏重要信息
- 这是分段分析，后续会与其他部分合成为完整笔记

<book_part>
{book_text}
</book_part>
"""

REDUCE_OVERVIEW_SKIM_PROMPT = """\
你是一位专业的阅读分析师。《{title}》({author}) 因篇幅过大，已分 {total_parts} 段分析。以下是各段的详细分析结果。

请基于这些分析，生成完整的概览+粗读笔记。格式和要求与直接分析完全一致。

书名：{title}
作者：{author}
分类：{category}

## 任务一：书籍概览（约 1500 字）

在概览输出的第一行，输出 themes: 后跟 3-5 个本书核心主题词，用逗号分隔。这行之后再开始正式的概览内容。

例如：
themes: 主题1, 主题2, 主题3, 主题4, 主题5

请按以下结构输出（themes 行之后）：

## 基本信息

- **作者**：（全名 + 一句话介绍）
- **出版年**：
- **页数**：（如能推断，否则写"—"）
- **原书语言**：

## 作者背景

（2-3 句话：为什么这个人有资格写这本书？与书中主题/人物的关系，此前的代表作）

## 研究方法与信息来源

（2-3 句话：作者的研究方法、信息来源、写作视角。如传记类：采访了谁、跟踪多久；如学术类：基于什么研究/实验；如商业类：数据来源和案例选取方法）

## 一句话概括

（一句话，不超过 50 字）

## 为什么值得读

（2-3 句话，具体说明对读者的价值）

## 全书结构

（描述这本书分几大部分、按什么逻辑组织，帮助读者建立全局地图）

## 核心主题

1. （主题名 + 一句话说明）
2.
3.
4.
5.

## 适合谁读

- **适合**：（什么样的读者会从中获益最多）
- **不适合**：（什么样的读者可能觉得不值得）

## 争议与评价

（这本书受到的主要赞誉和批评，保持客观）

## 推荐阅读策略

（具体建议：哪些章节重点读，哪些可以略读，用什么方法读效果最好）

====SPLIT====

## 任务二：粗读笔记（约 8000-10000 字）

将全书按自然段落/阶段分成 5-8 个大部分（根据书的结构，可以是时间线、主题线、或作者的章节分组）。

对每个部分，按以下结构输出：

## [部分标题]（涵盖的章节范围或时间范围）

### 背景与设定
（这部分的时代背景、起因、前情）

### 核心事件与发展
（按重要性列出这部分最关键的事件、论点或情节，每个事件 2-4 句话描述，包含具体细节、数据或人名）

### 关键人物
（这部分新出现或起关键作用的人物，及其角色）

### 结果与影响
（这部分的结局、对后续的影响）

### 金句摘录
> "原文引用"（标注章节）

每个部分写 800-1500 字，覆盖全书内容，不要遗漏重要章节。

最后输出：

---

## 全书脉络

（300 字以内，用一段话串联各部分的逻辑关系，帮助读者看清全书的主线和走向）

---

要求：
- 所有内容使用中文
- 输出纯 Markdown 格式（不要用代码块包裹）
- 两个任务之间用 ====SPLIT==== 分隔
- 粗读部分要足够详细，让没读过原书的人也能了解每个阶段发生了什么
- 不要在输出中包含"任务一""任务二"等字样，直接从 themes: 行开始输出，然后是"## 基本信息"开始概览，从"## [部分标题]"开始输出粗读

<analyses>
{analyses}
</analyses>
"""

REDUCE_DEEP_READ_PROMPT = """\
你是一位专业的阅读分析师。《{title}》({author}) 因篇幅过大，已分 {total_parts} 段分析。以下是各段的详细分析结果。

请基于这些分析，进行跨章节的深度主题提炼。

书名：{title}
作者：{author}
分类：{category}

## 任务

{category_template}

完成主题提炼后，还需要生成以下三个部分：

---

## 全书总结

### 核心收获

1.（最重要的收获，2-3 句话展开）
2.（第二重要的收获，2-3 句话展开）
3.（第三重要的收获，2-3 句话展开）

---

## 费曼检验

如果要向一个完全不了解这个领域的朋友解释这本书，你可以这样说：

「（200字以内，零术语，用日常语言讲清楚核心观点。想象你在跟一个聪明但完全不了解这个领域的朋友聊天）」

---

## 推荐深度探索

以下 5 个视角基于本书内容生成，按认知价值排序：

{perspectives_guide}

对每个视角：
1. 🔬 **[视角名]** — [为什么值得深入，一句话]

要求：
- 视角必须来源于书中实际内容，不要泛泛而谈
- 至少 1 个跨书/跨领域对比视角
- 至少 1 个批判/反思视角

---

请输出纯 Markdown 格式（不要用代码块包裹）。所有内容使用中文。

<analyses>
{analyses}
</analyses>
"""


def map_reduce_analyze(book_text: str, title: str, author: str, category: str,
                       build_reduce_prompt, phase_name: str) -> str:
    """Run Map-Reduce analysis for books exceeding context window.

    Args:
        book_text: Full book text.
        build_reduce_prompt: Callable(analyses_text, total_parts) -> reduce prompt string.
        phase_name: Name for logging (e.g. "overview-skim").
    Returns:
        Final synthesized analysis text.
    """
    parts = split_by_chapters(book_text)
    total = len(parts)
    print(f"[Map-Reduce] Book exceeds token limit. Split into {total} parts for {phase_name}.",
          file=sys.stderr)

    # Map phase: analyze each part in parallel (max 3 concurrent workers)
    max_workers = min(3, total)
    results = [None] * total

    def _process_part(i: int, part: str) -> tuple[int, str]:
        part_tokens = estimate_tokens(part)
        print(f"[Map {i+1}/{total}] Processing ~{part_tokens:,} tokens...", file=sys.stderr)
        prompt = MAP_ANALYSIS_PROMPT.format(
            title=title, author=author,
            part_num=i + 1, total_parts=total,
            book_text=part,
        )
        result = call_gemini(prompt, temperature=0.3, max_tokens=32768)
        return i, result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_process_part, i, part): i
            for i, part in enumerate(parts)
        }
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = f"== 第 {idx+1}/{total} 部分分析 ==\n\n{result}"
            print(f"[Map {idx+1}/{total}] Done.", file=sys.stderr)

    # Reduce phase: synthesize (sequential — needs all map results)
    analyses_text = "\n\n---\n\n".join(results)
    reduce_prompt = build_reduce_prompt(analyses_text, total)
    reduce_tokens = estimate_tokens(reduce_prompt)
    print(f"[Reduce] Synthesizing from {total} analyses (~{reduce_tokens:,} tokens)...",
          file=sys.stderr)
    final = call_gemini(reduce_prompt, temperature=0.4, max_tokens=65536)
    return final


# ---------------------------------------------------------------------------
# Cache helpers (intermediate artifact persistence)
# ---------------------------------------------------------------------------

def _book_prefix(book_path: str) -> str:
    """Derive a cache prefix from the book file path."""
    return os.path.splitext(os.path.basename(book_path))[0]


def get_cache_path(book_path: str, stage: str, topic: str | None = None) -> str:
    """Return the /tmp cache file path for a given stage."""
    prefix = _book_prefix(book_path)
    if topic:
        # Sanitize topic: first 20 chars, replace non-alphanumeric with underscore
        slug = re.sub(r'[^\w]', '_', topic[:20]).strip('_')
        return f"/tmp/{prefix}_{stage}_{slug}.txt"
    return f"/tmp/{prefix}_{stage}_raw.txt"


def save_cache(path: str, content: str) -> None:
    """Save content to cache file."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Cache saved: {path}", file=sys.stderr)


def load_cache(path: str) -> str | None:
    """Load content from cache if it exists, is non-empty, and is less than 24 hours old."""
    if not os.path.exists(path):
        return None
    if os.path.getsize(path) == 0:
        return None
    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    if age_hours > CACHE_MAX_AGE_HOURS:
        print(f"Cache expired ({age_hours:.1f}h old): {path}", file=sys.stderr)
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    print(f"Cache hit ({age_hours:.1f}h old): {path}", file=sys.stderr)
    return content


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_overview_skim(args):
    cache_path = get_cache_path(args.book, "overview_skim")

    # Check cache first (unless --no-cache)
    if not args.no_cache:
        cached = load_cache(cache_path)
        if cached is not None:
            print(cached)
            return

    book_text = read_book(args.book)
    tokens = estimate_tokens(book_text)
    print(f"Book text: ~{tokens:,} estimated tokens", file=sys.stderr)

    if tokens > BOOK_TOKEN_LIMIT:
        # Map-Reduce path (no context caching — each chunk is different)
        def build_reduce(analyses_text, total_parts):
            return REDUCE_OVERVIEW_SKIM_PROMPT.format(
                title=args.title, author=args.author, category=args.category,
                total_parts=total_parts, analyses=analyses_text,
            )
        result = map_reduce_analyze(
            book_text, args.title, args.author, args.category,
            build_reduce, "overview-skim",
        )
    else:
        # Direct path — try context caching
        gemini_cache_name = None
        if not args.no_cache:
            gemini_cache_name = get_or_create_gemini_cache(args.book, book_text)

        prompt = OVERVIEW_SKIM_PROMPT.format(
            title=args.title, author=args.author,
            category=args.category, book_text=book_text,
        )

        if gemini_cache_name:
            prompt = strip_book_text_from_prompt(prompt)

        print("Calling Gemini for overview + skim analysis...", file=sys.stderr)
        result = call_gemini(prompt, temperature=0.3, max_tokens=65536,
                             cached_content=gemini_cache_name)

    # Strip leaked prompt headers
    result = re.sub(r'^## 任务[一二].*\n+', '', result)

    save_cache(cache_path, result)
    print(result)


def cmd_deep_read(args):
    cache_path = get_cache_path(args.book, "deep_read")

    # Check cache first (unless --no-cache)
    if not args.no_cache:
        cached = load_cache(cache_path)
        if cached is not None:
            print(cached)
            return

    book_text = read_book(args.book)
    tokens = estimate_tokens(book_text)
    print(f"Book text: ~{tokens:,} estimated tokens", file=sys.stderr)

    category_template = DEEP_READ_TEMPLATES.get(args.category, DEEP_READ_DEFAULT)
    perspectives = PERSPECTIVES_GUIDE.get(
        args.category,
        "根据书中内容，从以下方向生成 5 个视角：核心框架 / 案例验证 / 跨领域对比 / 批判视角 / 实践指南",
    )

    if tokens > BOOK_TOKEN_LIMIT:
        # Map-Reduce path (no context caching — each chunk is different)
        def build_reduce(analyses_text, total_parts):
            return REDUCE_DEEP_READ_PROMPT.format(
                title=args.title, author=args.author, category=args.category,
                total_parts=total_parts, category_template=category_template,
                perspectives_guide=perspectives, analyses=analyses_text,
            )
        result = map_reduce_analyze(
            book_text, args.title, args.author, args.category,
            build_reduce, "deep-read",
        )
    else:
        # Direct path — try context caching
        gemini_cache_name = None
        if not args.no_cache:
            gemini_cache_name = get_or_create_gemini_cache(args.book, book_text)

        prompt = DEEP_READ_PROMPT.format(
            title=args.title, author=args.author,
            category=args.category, category_template=category_template,
            perspectives_guide=perspectives, book_text=book_text,
        )

        if gemini_cache_name:
            prompt = strip_book_text_from_prompt(prompt)

        print("Calling Gemini for deep-read analysis...", file=sys.stderr)
        result = call_gemini(prompt, temperature=0.4, max_tokens=65536,
                             cached_content=gemini_cache_name)

    # Strip leaked prompt headers
    result = re.sub(r'^## 任务.*\n+', '', result)

    save_cache(cache_path, result)
    print(result)


def cmd_deep_dive(args):
    if not args.topic:
        print("Error: --topic is required for deep-dive", file=sys.stderr)
        sys.exit(1)

    cache_path = get_cache_path(args.book, "deep_dive", topic=args.topic)

    # Check cache first (unless --no-cache)
    if not args.no_cache:
        cached = load_cache(cache_path)
        if cached is not None:
            print(cached)
            return

    book_text = read_book(args.book)

    # Try context caching
    gemini_cache_name = None
    if not args.no_cache:
        gemini_cache_name = get_or_create_gemini_cache(args.book, book_text)

    prompt = DEEP_DIVE_PROMPT.format(
        title=args.title,
        author=args.author,
        category=args.category,
        topic=args.topic,
        book_text=book_text,
    )

    if gemini_cache_name:
        prompt = strip_book_text_from_prompt(prompt)

    print(f"Calling Gemini for deep-dive on '{args.topic}'...", file=sys.stderr)
    result = call_gemini(prompt, temperature=0.5, cached_content=gemini_cache_name)

    save_cache(cache_path, result)
    print(result)


def cmd_ask(args):
    """Handle free-form questions about a book."""
    book_text = read_book(args.book)

    # Try context caching
    gemini_cache_name = None
    if not args.no_cache:
        gemini_cache_name = get_or_create_gemini_cache(args.book, book_text)

    if gemini_cache_name:
        prompt = ASK_PROMPT_CACHED.format(
            title=args.title,
            author=args.author,
            question=args.question,
        )
    else:
        prompt = ASK_PROMPT.format(
            title=args.title,
            author=args.author,
            question=args.question,
            book_text=book_text,
        )

    print(f"Calling Gemini to answer: '{args.question}'...", file=sys.stderr)
    result = call_gemini(prompt, temperature=0.3, cached_content=gemini_cache_name)

    print(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Gemini-powered book analysis (v3)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Common arguments
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--book", required=True, help="Path to extracted book text")
    common.add_argument("--title", required=True, help="Book title")
    common.add_argument("--author", required=True, help="Book author")
    common.add_argument(
        "--category",
        required=True,
        choices=[
            "biography", "business", "psychology", "self-growth",
            "technology", "history", "philosophy", "finance",
            "literature", "science",
        ],
    )
    common.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Bypass cache and force a fresh Gemini API call",
    )

    # overview-skim (merged)
    subparsers.add_parser("overview-skim", parents=[common],
                          help="Generate overview + skim read (merged, 1 call)")

    # deep-read
    subparsers.add_parser("deep-read", parents=[common],
                          help="Generate cross-chapter thematic analysis")

    # deep-dive
    dd = subparsers.add_parser("deep-dive", parents=[common],
                               help="Deep-dive on a specific topic")
    dd.add_argument("--topic", required=True, help="Topic to explore")

    # ask (free-form question) — uses its own argument set (no --category required)
    ask_common = argparse.ArgumentParser(add_help=False)
    ask_common.add_argument("--book", required=True, help="Path to extracted book text")
    ask_common.add_argument("--title", required=True, help="Book title")
    ask_common.add_argument("--author", required=True, help="Book author")
    ask_common.add_argument("--question", required=True, help="Question to ask about the book")
    ask_common.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Bypass cache and force a fresh Gemini API call",
    )
    subparsers.add_parser("ask", parents=[ask_common],
                          help="Ask a free-form question about a book")

    args = parser.parse_args()

    commands = {
        "overview-skim": cmd_overview_skim,
        "deep-read": cmd_deep_read,
        "deep-dive": cmd_deep_dive,
        "ask": cmd_ask,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
