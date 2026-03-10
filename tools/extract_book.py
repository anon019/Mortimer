#!/usr/bin/env python3
"""
extract_book.py - 一次性提取书籍全文到纯文本

支持 EPUB 和 PDF 格式。输出带章节标记的纯文本文件和目录索引文件。

用法:
  python3 extract_book.py <book_file> [--output /tmp/book_full.txt]

输出:
  - <output>.txt: 带 ===== Chapter N: <标题> ===== 标记的全文
  - <output>_toc.txt: 目录索引（章节名 + 起始行号）
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from xml.etree import ElementTree as ET


def strip_html(text: str) -> str:
    """去除 HTML 标签，解码实体，清理空白。"""
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = text.replace('&#13;', '')
    text = re.sub(r'\r\n?', '\n', text)
    # 合并连续空行为单个空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 去除每行首尾空白但保留缩进结构
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    # 去除首尾空白
    return text.strip()


def get_epub_spine_order(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """从 OPF 文件获取 spine 顺序的内容文件列表。返回 [(filepath, title), ...]"""
    # 找到 OPF 文件
    container_path = 'META-INF/container.xml'
    try:
        container_xml = zf.read(container_path).decode('utf-8')
        root = ET.fromstring(container_xml)
        ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
        rootfile = root.find('.//c:rootfile', ns)
        if rootfile is not None:
            opf_path = rootfile.get('full-path', '')
        else:
            opf_path = ''
    except (KeyError, ET.ParseError):
        opf_path = ''

    if not opf_path:
        # 备选：搜索 .opf 文件
        opf_files = [n for n in zf.namelist() if n.endswith('.opf')]
        if opf_files:
            opf_path = opf_files[0]
        else:
            return []

    opf_dir = os.path.dirname(opf_path)

    try:
        opf_xml = zf.read(opf_path).decode('utf-8')
        root = ET.fromstring(opf_xml)
    except (KeyError, ET.ParseError):
        return []

    # 解析命名空间
    ns_opf = 'http://www.idpf.org/2007/opf'
    ns_dc = 'http://purl.org/dc/elements/1.1/'

    # 构建 manifest: id -> (href, media-type)
    manifest = {}
    for item in root.findall(f'.//{{{ns_opf}}}item'):
        item_id = item.get('id', '')
        href = item.get('href', '')
        media_type = item.get('media-type', '')
        if item_id and href:
            # 解析相对路径
            full_path = os.path.normpath(os.path.join(opf_dir, href)) if opf_dir else href
            manifest[item_id] = (full_path, media_type)

    # 从 spine 获取顺序
    spine_items = []
    for itemref in root.findall(f'.//{{{ns_opf}}}itemref'):
        idref = itemref.get('idref', '')
        if idref in manifest:
            path, media_type = manifest[idref]
            if 'html' in media_type or 'xml' in media_type:
                spine_items.append(path)

    # 尝试从 NCX 获取章节标题
    titles = {}
    ncx_id = None
    toc_attr = root.find(f'.//{{{ns_opf}}}spine')
    if toc_attr is not None:
        ncx_id = toc_attr.get('toc', '')

    if ncx_id and ncx_id in manifest:
        ncx_path = manifest[ncx_id][0]
        try:
            ncx_xml = zf.read(ncx_path).decode('utf-8')
            ncx_root = ET.fromstring(ncx_xml)
            ns_ncx = 'http://www.daisy.org/z3986/2005/ncx/'
            for nav_point in ncx_root.findall(f'.//{{{ns_ncx}}}navPoint'):
                text_el = nav_point.find(f'{{{ns_ncx}}}navLabel/{{{ns_ncx}}}text')
                content_el = nav_point.find(f'{{{ns_ncx}}}content')
                if text_el is not None and content_el is not None:
                    src = content_el.get('src', '')
                    # 去除锚点
                    src_clean = src.split('#')[0]
                    full_src = os.path.normpath(os.path.join(os.path.dirname(ncx_path), src_clean))
                    title = text_el.text or ''
                    if full_src not in titles:
                        titles[full_src] = title.strip()
        except (KeyError, ET.ParseError):
            pass

    result = []
    for path in spine_items:
        title = titles.get(path, '')
        result.append((path, title))

    return result


SKIP_PATTERNS = [
    r'^(封面|版權頁|版权页|版权信息|Copyright|Cover|Title Page|封底)$',
    r'^目[錄录]$',
    r'^(Table of Contents|Contents)$',
]


def extract_epub(epub_path: str, output_path: str, min_length: int = 100) -> dict:
    """提取 EPUB 全文到纯文本文件。"""
    with zipfile.ZipFile(epub_path, 'r') as zf:
        # 获取 spine 顺序
        spine = get_epub_spine_order(zf)

        if not spine:
            # 备选：按文件名排序所有 HTML 文件
            html_files = sorted([
                n for n in zf.namelist()
                if re.search(r'\.(x?html?)$', n, re.I)
            ])
            spine = [(f, '') for f in html_files]

        if not spine:
            print("错误：未找到任何内容文件", file=sys.stderr)
            sys.exit(1)

        chapters = []
        for filepath, title in spine:
            try:
                raw = zf.read(filepath).decode('utf-8', errors='replace')
            except KeyError:
                continue

            # 如果没有从 NCX 获取到标题，尝试从 HTML 的 h1/h2 提取
            if not title:
                h_match = re.search(r'<h[12][^>]*>(.*?)</h[12]>', raw, re.I | re.DOTALL)
                if h_match:
                    title = strip_html(h_match.group(1))[:80]

            text = strip_html(raw)

            # 跳过太短的内容（封面、版权页等）
            if len(text) < min_length:
                continue

            # 根据标题过滤非内容章节
            skip = False
            for pattern in SKIP_PATTERNS:
                if re.match(pattern, title.strip(), re.I):
                    skip = True
                    break
            if skip:
                continue

            # 如果仍没有标题，从文件名或内容推断
            if not title:
                basename = os.path.basename(filepath)
                name_match = re.match(r'(?:Chapter|ch|part)[-_]?(\d+)', basename, re.I)
                if name_match:
                    title = f"Chapter {name_match.group(1)}"
                else:
                    first_line = text.split('\n')[0][:80]
                    title = first_line if first_line else basename

            chapters.append((title, text))

        # 写入文件
        toc_entries = []
        line_num = 1

        with open(output_path, 'w', encoding='utf-8') as f:
            for i, (title, text) in enumerate(chapters, 1):
                header = f"===== Chapter {i}: {title} ====="
                toc_entries.append((i, title, line_num))

                f.write(header + '\n')
                line_num += 1

                f.write(text + '\n\n')
                line_num += text.count('\n') + 2

        # 写入 TOC 文件
        toc_path = output_path.replace('.txt', '_toc.txt')
        with open(toc_path, 'w', encoding='utf-8') as f:
            f.write(f"# 目录索引 - {os.path.basename(epub_path)}\n")
            f.write(f"# 总章节数: {len(chapters)}\n\n")
            for num, title, start_line in toc_entries:
                f.write(f"Chapter {num:3d} | Line {start_line:6d} | {title}\n")

        return {
            'chapters': len(chapters),
            'output': output_path,
            'toc': toc_path,
            'total_lines': line_num - 1,
        }


def extract_pdf(pdf_path: str, output_path: str) -> dict:
    """提取 PDF 全文到纯文本文件。"""
    result = subprocess.run(
        ['pdftotext', pdf_path, output_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"pdftotext 错误: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # 读取并统计
    with open(output_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    total_lines = content.count('\n') + 1

    # 尝试检测章节标题生成 TOC
    toc_entries = []
    for i, line in enumerate(content.split('\n'), 1):
        # 常见章节标题模式
        if re.match(r'^(Chapter|第[一二三四五六七八九十百\d]+[章节篇]|Part|PART)\s', line.strip()):
            title = line.strip()[:80]
            toc_entries.append((len(toc_entries) + 1, title, i))

    toc_path = output_path.replace('.txt', '_toc.txt')
    with open(toc_path, 'w', encoding='utf-8') as f:
        f.write(f"# 目录索引 - {os.path.basename(pdf_path)}\n")
        f.write(f"# 检测到章节数: {len(toc_entries)}\n\n")
        for num, title, start_line in toc_entries:
            f.write(f"Chapter {num:3d} | Line {start_line:6d} | {title}\n")

    return {
        'chapters': len(toc_entries),
        'output': output_path,
        'toc': toc_path,
        'total_lines': total_lines,
    }


def main():
    parser = argparse.ArgumentParser(description='提取书籍全文到纯文本')
    parser.add_argument('book', help='书籍文件路径 (EPUB 或 PDF)')
    parser.add_argument('--output', '-o', default='/tmp/book_full.txt',
                        help='输出文件路径 (默认: /tmp/book_full.txt)')
    parser.add_argument('--min-length', type=int, default=100,
                        help='最小内容长度，低于此值的章节被跳过 (默认: 100)')
    args = parser.parse_args()

    book_path = os.path.expanduser(args.book)
    if not os.path.exists(book_path):
        print(f"文件不存在: {book_path}", file=sys.stderr)
        sys.exit(1)

    ext = os.path.splitext(book_path)[1].lower()

    if ext == '.epub':
        info = extract_epub(book_path, args.output, min_length=args.min_length)
    elif ext == '.pdf':
        info = extract_pdf(book_path, args.output)
    elif ext == '.txt':
        shutil.copy2(book_path, args.output)
        with open(book_path, 'r', encoding='utf-8', errors='replace') as f:
            total_lines = sum(1 for _ in f)
        info = {'chapters': 0, 'output': args.output, 'toc': '', 'total_lines': total_lines}
    else:
        print(f"不支持的格式: {ext}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(info, ensure_ascii=False))


if __name__ == '__main__':
    main()
