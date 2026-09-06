# -*- coding: utf-8 -*-
"""
docx_parser.py - 题库 docx 解析模块

核心策略（基于格式分析结论）：
1. 按文档顺序交错提取"文本 + 图片占位符"，保证图片归属题目的位置准确
2. 以 `答案：` 为切题锚点（题号缺失/粘连不影响切题），而非行首题号
3. 每块开头为上一题答案文本（含粘连场景），剥离后即本题内容
4. 题型：judge(判断) / single(单选) / multi(多选)；判断题无选项时自动补"正确/错误"
"""
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
V = 'urn:schemas-microsoft-com:vml'

# 切题锚点与答案前缀
ANSWER_SPLIT_RE = re.compile(r'答\s*案\s*[:：]')
ANSWER_LEAD_RE = re.compile(
    r'^[\s\u00a0\u3000]*((?:正确|错误|√|\u2713|\u2717|×|X|Y|(?:[A-D][\s,，、]?)+))'
)
# 题号（允许行中，前面不是数字/字母，防止匹配 "AB1." 之类）
QNUM_RE = re.compile(r'(?<![\da-zA-Z])(\d{1,4})\s*[.、．]\s*')
# 题号变体：行首 "1147 这个…"（数字+空格+汉字，无点号）
QNUM_SP_RE = re.compile(r'(?m)^\s*(\d{3,4})\s+(?=[\u4e00-\u9fff])')
# 题号变体：行首 "788驾驶…"（数字+汉字，无任何分隔）
QNUM_NOSP_RE = re.compile(r'(?m)^\s*(\d{3,4})(?=[\u4e00-\u9fff])')
# 解析注释行 "(解析：…)" —— 非题干内容
NOTE_LINE_RE = re.compile(r'(?m)^\s*[（(]\s*解析[：:)].*$\n?')
# 选项标记
OPT_RE = re.compile(r'([A-D])\s*[、.．]\s*')
# 带标号的选项行（fallback 用）
OPT_LINE_RE = re.compile(r'^([A-D])\s*[、.．]\s*(.+)$')
# 判断题无分隔符变体：独立行 "A正确 / B错误"
JUDGE_VARIANT_RE = re.compile(r'(?m)^\s*A\s*正确\s*\n\s*B\s*错误\s*$')
# 562 特殊残留行 "N:错误 / Y:正确"
YN_LINE_RE = re.compile(r'(?m)^\s*[NY]\s*[:：]\s*(正确|错误)\s*\n?')
IMG_PH_RE = re.compile(r'\x00IMG([^\x00]+)\x00')


@dataclass
class ParsedQuestion:
    source_id: int | None = None
    stem: str = ''
    qtype: str = 'unknown'          # judge / single / multi / unknown
    answer: str = ''                # 规范化答案：'A'/'ABD' 或 'T'/'F'
    options: list = field(default_factory=list)     # [(label, content, is_correct)]
    image_names: list = field(default_factory=list) # 关联的 media 文件名（按出现顺序）
    raw: str = ''                   # 原始块文本（异常排查用）


def extract_stream(docx_path):
    """提取交错文本流与 media 映射。

    返回 (stream, media_map)；stream 中图片以 \\x00IMG<文件名>\\x00 占位，
    media_map: {文件名: bytes}
    """
    z = zipfile.ZipFile(docx_path)

    # rId -> media 文件名
    rels = etree.fromstring(z.read('word/_rels/document.xml.rels'))
    rid2name = {}
    for rel in rels:
        target = rel.get('Target', '')
        if 'media/' in target:
            rid2name[rel.get('Id')] = target.split('/')[-1]

    root = etree.fromstring(z.read('word/document.xml'))
    paras = root.xpath('//w:body/w:p', namespaces={'w': W})

    lines = []
    for p in paras:
        buf = []
        for node in p.iter():
            tag = node.tag
            if tag == f'{{{W}}}t':
                buf.append(node.text or '')
            elif tag == f'{{{W}}}br':
                buf.append('\n')
            elif tag == f'{{{W}}}tab':
                buf.append('\t')
            elif tag == f'{{{A}}}blip':
                rid = node.get(f'{{{R}}}embed')
                name = rid2name.get(rid)
                if name:
                    buf.append(f'\x00IMG{name}\x00')
            elif tag == f'{{{V}}}imagedata':
                rid = node.get(f'{{{R}}}id')
                name = rid2name.get(rid)
                if name:
                    buf.append(f'\x00IMG{name}\x00')
        lines.append(''.join(buf))

    # media 字节
    media_map = {}
    for n in z.namelist():
        if n.startswith('word/media/'):
            media_map[n.split('/')[-1]] = z.read(n)

    return '\n'.join(lines), media_map


def clean_text(s):
    """文本清洗：去占位符、全角空格、多余空白。"""
    s = IMG_PH_RE.sub('', s)
    s = s.replace('\u00a0', ' ').replace('\u3000', ' ').replace('\t', ' ')
    s = re.sub(r' {2,}', ' ', s)
    s = re.sub(r' ?\n ?', '\n', s)
    return s.strip()


def _extract_options(text):
    """提取严格递增的选项序列（A→B→C→D 链），返回 (选项列表, 首个选项标记位置)。

    选项内容截断到下一个选项标记处；采用递增链扫描，避免题干正文误匹配。
    """
    cands = [(m.start(), m.group(1), m.end()) for m in OPT_RE.finditer(text)]
    accepted = []
    expect_idx = 0
    labels = 'ABCD'
    for pos, label, end in cands:
        if label == labels[expect_idx]:
            accepted.append((pos, label, end))
            expect_idx += 1
            if expect_idx >= 4:
                break
    chain = []
    for i, (pos, label, end) in enumerate(accepted):
        nxt = accepted[i + 1][0] if i + 1 < len(accepted) else len(text)
        chain.append((label, text[end:nxt].strip()))
    first_pos = accepted[0][0] if accepted else None
    return chain, first_pos


def _fallback_line_options(text):
    """无标号选项的按行拆分：题干=首行，其余非空行按序补标号 A/B/C/D。

    已带标号的行（如 300 题的 "C、不得左转…"）沿用其标号。
    返回 (题干, [(label, content)]) 或 None（无法可靠拆分时）。
    """
    lines = [ln.strip() for ln in text.split('\n')]
    lines = [ln for ln in lines if ln]
    if len(lines) < 3:            # 题干 + 至少2个选项
        return None
    stem, opts = lines[0], []
    labels = 'ABCD'
    next_idx = 0
    for ln in lines[1:]:
        m = OPT_LINE_RE.match(ln)
        if m:
            label, content = m.group(1), m.group(2)
            next_idx = labels.index(label) + 1
        elif next_idx < 4:
            label, content = labels[next_idx], ln
            next_idx += 1
        else:
            return None
        opts.append((label, content))
    if 2 <= len(opts) <= 4:
        return stem, opts
    return None


def parse_one(content, answer_raw):
    """解析单个题目块 -> ParsedQuestion"""
    q = ParsedQuestion(raw=content[:200])

    # 1. 收集并移除图片占位符（保留出现顺序）
    q.image_names = IMG_PH_RE.findall(content)
    text = clean_text(content)

    # 2. 规范化答案
    ans = (answer_raw or '').strip()
    ans = ans.replace('，', '').replace(',', '').replace('、', '').replace(' ', '')
    if ans in ('正确', '√', 'Y', '\u2713'):
        q.qtype, q.answer = 'judge', 'T'
    elif ans in ('错误', '×', 'X', '\u2717'):
        q.qtype, q.answer = 'judge', 'F'
    elif re.fullmatch(r'[A-D]', ans):
        q.qtype, q.answer = 'single', ans
    elif re.fullmatch(r'[A-D]{2,4}', ans):
        q.qtype, q.answer = 'multi', ans

    # 3. 提取原始题号（三种格式：带点号 / 数字+空格 / 数字+汉字）
    text = NOTE_LINE_RE.sub('', text).strip()
    m = (QNUM_RE.search(text) or QNUM_SP_RE.search(text)
         or QNUM_NOSP_RE.search(text))
    if m:
        q.source_id = int(m.group(1))
        text = text[m.end():].strip()

    # 4. 清除 Y:/N: 残留行（562 特殊格式）
    text = YN_LINE_RE.sub('', text).strip()

    # 5. 判断题无分隔符变体（独立行 "A正确/B错误"）-> 转为 judge
    if q.qtype == 'single' and JUDGE_VARIANT_RE.search(text):
        q.qtype = 'judge'
        ans_letter = q.answer
        q.answer = 'T' if ans_letter == 'A' else 'F'
        text = JUDGE_VARIANT_RE.sub('', text).strip()

    # 6. 提取选项（递增链，有标号）
    chain, first_pos = _extract_options(text)
    if first_pos is not None:
        q.stem = clean_text(text[:first_pos]).replace('\n', '')
        q.options = [(label, clean_text(c).replace('\n', ''), False)
                     for label, c in chain]
    else:
        # 无标号选项：按行拆分
        fb = _fallback_line_options(text)
        if fb:
            q.stem, pair_list = fb
            q.options = [(label, c, False) for label, c in pair_list]
        else:
            q.stem = text.replace('\n', '')

    # 7. 判断题选项补全 / 正确选项设置
    if q.qtype == 'judge':
        labels = {label for label, _, _ in q.options}
        if not labels:
            q.options = [('A', '正确', q.answer == 'T'), ('B', '错误', q.answer == 'F')]
        else:
            for i, (label, c, _) in enumerate(q.options):
                q.options[i] = (label, c,
                                (label == 'A' and q.answer == 'T') or
                                (label == 'B' and q.answer == 'F'))
    elif q.qtype in ('single', 'multi'):
        q.options = [(label, c, label in q.answer) for label, c, _ in q.options]

    return q


def parse_all(docx_path):
    """解析整份题库。返回 (questions, media_map, tail_text)"""
    stream, media_map = extract_stream(docx_path)

    # 预清洗：562 特殊格式 "参考答案N:错误" -> 标准答案行，使粘连的 563 独立成块
    stream = re.sub(r'参考答案\s*N\s*[:：]\s*(错误|×)', r'答案：\1', stream)

    parts = ANSWER_SPLIT_RE.split(stream)

    n = len(parts)
    answers = [None] * n
    for k in range(1, n):
        m = ANSWER_LEAD_RE.match(parts[k])
        answers[k] = m.group(1).strip() if m else None
        parts[k] = parts[k][m.end():] if m else parts[k]

    questions = []
    for k in range(1, n):          # Q_k: content=parts[k-1], answer=answers[k]
        questions.append(parse_one(parts[k - 1], answers[k]))

    tail = parts[n - 1].strip()    # 最后一块剥离答案后的剩余（应为空或杂讯）
    return questions, media_map, tail


if __name__ == '__main__':
    import sys, collections
    sys.stdout.reconfigure(encoding='utf-8')
    base = Path(r"C:\Users\w'j'm\Desktop\新建文件夹")
    docx = next(base.glob('题库_2026.docx'))

    questions, media_map, tail = parse_all(docx)
    print(f'解析题数: {len(questions)}')
    print(f'题型分布: {collections.Counter(q.qtype for q in questions)}')
    print(f'带图题数: {sum(1 for q in questions if q.image_names)}')
    print(f'图片引用总数: {sum(len(q.image_names) for q in questions)}')
    print(f'有原始题号: {sum(1 for q in questions if q.source_id)}')
    print(f'尾部剩余: {tail[:100]!r}')

    # 异常题目筛查
    print('\n=== 异常题目 ===')
    bad = [q for q in questions
           if q.qtype == 'unknown' or not q.stem
           or (q.qtype in ('single', 'multi') and len(q.options) < 2)
           or (q.qtype == 'single' and not any(c for _, _, c in q.options))]
    for q in bad[:15]:
        print(f'  [type={q.qtype} ans={q.answer!r} opts={len(q.options)} sid={q.source_id}] {q.stem[:40]}')
    print(f'异常总数: {len(bad)}')

    # 原始题号缺口统计（对照标称 2309 题）
    sids = sorted(q.source_id for q in questions if q.source_id)
    missing = [n for n in range(1, 2310) if n not in set(sids)]
    print(f'\n有题号: {len(sids)}  无题号: {sum(1 for q in questions if not q.source_id)}')
    print(f'1-2309 中缺失的原始题号 ({len(missing)}):', missing[:30])

    # 样例展示
    print('\n=== 样例（含图单选/判断/多选各1） ===')
    shown = set()
    for q in questions:
        key = (q.qtype, bool(q.image_names))
        if key not in shown and (q.image_names if key[1] else True):
            shown.add(key)
            print(f'[{q.qtype} sid={q.source_id} ans={q.answer}] {q.stem[:50]}')
            for label, c, ok in q.options:
                print(f'   {label}({"√" if ok else " "}) {c[:40]}')
            if q.image_names:
                print(f'   图片: {q.image_names}')
        if len(shown) >= 3:
            break
