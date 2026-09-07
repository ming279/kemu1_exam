# -*- coding: utf-8 -*-
"""
crawler.py - 加分项④：网上爬题采集模块

两种采集方式：
  A. 公开题库源抓取（内置源注册表，requests 拉 JSON 规范化）
     - drivereasy : GitHub 开源题库 2022-07 版（科目一 2545 题）
     - juhe       : 聚合数据 C1 科目一镜像（1229 题，版本较早）
  B. 指定网站爬取（输入 URL，通用文本解析：答案锚点切块 + 递增选项链提取）

导入流程：规范化 -> 与库内题/批次内题做 2-gram Jaccard 去重 -> 增量入库
         （question.year_version 标记来源年份，import_batch 记录批次）
采集任务在后台线程执行，状态写 import_batch 表供前端轮询。
"""
import re
import os
import json
import time
import html as html_mod
import threading
import traceback

import requests
import pymysql

from pathlib import Path

BASE = Path(__file__).parent
CACHE = BASE.parent / 'data_cache'          # 抓取结果缓存（首次在线抓取后落盘）
DB = dict(host='localhost', user='root',
          password=os.environ.get('MYSQL_PASSWORD', '123456'),
          database='kemu1_exam', charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor, autocommit=True)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'}

# ---------------------------------------------------------------
# 公开源注册表（raw 直连失败时依次回退镜像地址）
# ---------------------------------------------------------------
SOURCES = {
    'drivereasy': {
        'name': 'DriverEasy 开源题库（2022-07 版，科目一 2545 题）',
        'default_year': '2022',
        'urls': ['https://raw.githubusercontent.com/icecreamZeng/DriverEasy/main/questions.json',
                 'https://gh-proxy.com/raw.githubusercontent.com/icecreamZeng/DriverEasy/main/questions.json'],
        'fetch': '_fetch_drivereasy',
    },
    'juhe': {
        'name': '聚合数据 C1 科目一镜像（早期版本，1229 题）',
        'default_year': '2021',
        'urls': ['https://raw.githubusercontent.com/tjlizz/driver-data/master/data/question/1/c1/1-c1.json',
                 'https://gh-proxy.com/raw.githubusercontent.com/tjlizz/driver-data/master/data/question/1/c1/1-c1.json'],
        'fetch': '_fetch_juhe',
    },
}

PUNCT_RE = re.compile(r'[\s\W_]+', re.UNICODE)
OPT_RE = re.compile(r'([A-D])\s*[、.．)）]\s*')
ANS_SPLIT_RE = re.compile(r'(?:正确)?\s*答\s*案\s*[:：为]')
ANS_LEAD_RE = re.compile(r'^\s*((?:[A-D][\s,，、]?)+|正确|错误|对|错|√|×|X|Y)')
NOTE_RE = re.compile(r'(?m)^\s*[（(]\s*解析[：:)].*$\n?')


def normalize(text):
    return PUNCT_RE.sub('', text or '')


def shingles(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


# ---------------------------------------------------------------
# 抓取器：公开源 -> 规范化题目 dict 列表
#   {stem, qtype, options: [(label, content, is_correct)]}
# ---------------------------------------------------------------
def _http_get(urls):
    last_err = None
    for u in urls:
        for attempt in range(2):          # 每个地址尝试 2 次
            try:
                r = requests.get(u, headers=UA, timeout=30)
                r.raise_for_status()
                return r
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def _cached_get(cache_key, urls):
    """优先读 data_cache/<cache_key>.json；无缓存则在线抓取并落盘。

    返回 (bytes, from_cache)
    """
    cache_file = CACHE / f'{cache_key}.json'
    if cache_file.exists():
        return cache_file.read_bytes(), True
    content = _http_get(urls).content
    CACHE.mkdir(exist_ok=True)
    cache_file.write_bytes(content)
    return content, False


def _fetch_drivereasy(year):
    content, _ = _cached_get('drivereasy', SOURCES['drivereasy']['urls'])
    data = json.loads(content)
    items = []
    for q in data['questions']:
        if q.get('subject') != 1:         # 只要科目一
            continue
        opts = list(q.get('options') or [])
        ans = (q.get('answer') or '').replace(' ', '')
        if q['type'] == 3:                # 判断题
            items.append(dict(stem=q['question'], qtype='judge',
                              options=[('A', '正确', ans == 'A'),
                                       ('B', '错误', ans != 'A')]))
        elif q['type'] == 1:              # 单选
            items.append(dict(stem=q['question'], qtype='single',
                              options=[(chr(65 + i), c, chr(65 + i) == ans)
                                       for i, c in enumerate(opts[:4])]))
        elif q['type'] == 2:              # 多选，答案形如 'A,B,C'
            answers = set(ans.split(','))
            items.append(dict(stem=q['question'], qtype='multi',
                              options=[(chr(65 + i), c, chr(65 + i) in answers)
                                       for i, c in enumerate(opts[:4])]))
    return items


def _fetch_juhe(year):
    content, _ = _cached_get('juhe', SOURCES['juhe']['urls'])
    data = json.loads(content)['result']
    items = []
    for q in data:
        opts = [q.get(k) for k in ('item1', 'item2', 'item3', 'item4')]
        opts = [o for o in opts if o]
        ans_idx = int(q.get('answer') or 0) - 1
        if len(opts) >= 3:                # 单选，answer 数字即正确选项序号
            items.append(dict(stem=q['question'], qtype='single',
                              options=[(chr(65 + i), c, i == ans_idx)
                                       for i, c in enumerate(opts[:4])]))
        elif len(opts) == 2:              # 两选项 -> 判断题，归一化为"正确/错误"
            a, b = opts[0], opts[1]
            if _is_true(a) != _is_true(b):
                a = '正确' if _is_true(a) else '错误'
                b = '正确' if _is_true(b) else '错误'
            items.append(dict(stem=q['question'], qtype='judge',
                              options=[('A', a, ans_idx == 0),
                                       ('B', b, ans_idx == 1)]))
        # 其余（无选项题）跳过，由批次统计体现
    return items


def _is_true(s):
    s = (s or '').strip()
    return ('正确' in s) or (s in ('对', '是', 'T'))


# ---------------------------------------------------------------
# 指定网站爬取：HTML -> 文本 -> 按答案锚点切块解析
# ---------------------------------------------------------------
def _fetch_url(url, year):
    r = _http_get([url])
    if not r.encoding or r.encoding.lower() in ('iso-8859-1',):
        r.encoding = r.apparent_encoding
    text = re.sub(r'<script\b.*?</script>|<style\b.*?</style>', '', r.text, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>|</p>|</li>|</div>|</tr>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = NOTE_RE.sub('', text)

    parts = ANS_SPLIT_RE.split(text)
    items = []
    for k in range(1, len(parts)):
        m = ANS_LEAD_RE.match(parts[k])
        if not m:
            continue
        ans_raw = m.group(1).strip()
        body = parts[k - 1]
        # 块内答案残留剥离（上一题答案文本粘连）
        lead = re.match(r'^\s*(?:正确|错误|对|错|√|×|[A-D][\s,，、]?)+', body)
        if lead and k > 1:
            body = body[lead.end():]
        q = _parse_block(body, ans_raw)
        if q:
            items.append(q)
    return items


def _parse_block(block, ans_raw):
    """单块解析：题干 + 递增选项链（A->B->C->D），与 docx_parser 同思路"""
    ans = re.sub(r'[\s,，、]', '', ans_raw or '')
    if ans in ('正确', '对', '√', 'Y'):
        qtype, ans = 'judge', 'T'
    elif ans in ('错误', '错', '×', 'X'):
        qtype, ans = 'judge', 'F'
    elif re.fullmatch(r'[A-D]', ans):
        qtype = 'single'
    elif re.fullmatch(r'[A-D]{2,4}', ans):
        qtype = 'multi'
    else:
        return None

    # 行首题号截断：丢弃页头导航等噪声（题号前缀）
    num = re.search(r'(?m)^\s*(\d{1,4})\s*[.、．)]\s*', block)
    if num:
        block = block[num.end():]

    text = re.sub(r'\s+', ' ', block).strip()
    cands = [(m.start(), m.group(1), m.end()) for m in OPT_RE.finditer(text)]
    accepted, expect = [], 0
    for pos, label, end in cands:
        if label == 'ABCD'[expect]:
            accepted.append((pos, label, end))
            expect += 1
            if expect >= 4:
                break
    if qtype == 'judge':
        stem = text[:accepted[0][0]] if accepted else text
        if len(normalize(stem)) < 6:
            return None
        return dict(stem=stem.strip(), qtype='judge',
                    options=[('A', '正确', ans == 'T'), ('B', '错误', ans == 'F')])
    if len(accepted) < 2:
        return None
    stem = text[:accepted[0][0]].strip()
    if len(normalize(stem)) < 6:
        return None
    opts = []
    for i, (pos, label, end) in enumerate(accepted):
        nxt = accepted[i + 1][0] if i + 1 < len(accepted) else len(text)
        opts.append((label, text[end:nxt].strip(), label in ans))
    return dict(stem=stem, qtype=qtype, options=opts)


# ---------------------------------------------------------------
# 去重与增量导入
# ---------------------------------------------------------------
def load_existing_docs(cur):
    """库内现有题 -> [(qid, doc_text)]（题干+正确选项内容，规范化）"""
    cur.execute("SELECT q.id, q.stem, q.qtype FROM question q")
    qs = cur.fetchall()
    cur.execute("SELECT question_id, label, content FROM `option` WHERE is_correct=1")
    correct = {}
    for o in cur.fetchall():
        correct.setdefault(o['question_id'], []).append(f"{o['label']}{o['content']}")
    docs = []
    for qq in qs:
        tail = '' if qq['qtype'] == 'judge' else ''.join(sorted(correct.get(qq['id'], [])))
        docs.append((qq['id'], normalize(qq['stem'] + tail)))
    return docs


def dedup_import(cur, items, year_version, batch_id):
    """去重 + 入库。items: 规范化题目列表"""
    existing = load_existing_docs(cur)
    exist_shingles = [(qid, shingles(d)) for qid, d in existing]
    exist_norm = set(d for _, d in existing)

    imported = duplicates = 0
    batch_norm = []                        # 本批次已入库题（规范化文本），用于批内去重
    batch_shingles = []
    for idx, it in enumerate(items, 1):
        if idx % 100 == 0:
            _update_batch(batch_id, fetched=len(items), imported=imported,
                          duplicates=duplicates,
                          message=f'去重入库中 {idx}/{len(items)}（新增 {imported}，重复 {duplicates}）')
        text = normalize(it['stem'] +
                         ('' if it['qtype'] == 'judge'
                          else ''.join(sorted(l + c for l, c, ok in it['options'] if ok))))
        if not text:
            continue
        # 1) 规范化后完全一致 -> 重复
        if text in exist_norm or any(text == bt for _, bt in batch_norm):
            duplicates += 1
            continue
        # 2) 2-gram Jaccard >= 0.9 -> 重复
        sh = shingles(text)
        if _is_dup(sh, exist_shingles) or _is_dup(sh, batch_shingles):
            duplicates += 1
            continue
        # 3) 入库
        cur.execute(
            "INSERT INTO question (stem, qtype, year_version) VALUES (%s, %s, %s)",
            (it['stem'].strip(), it['qtype'], year_version))
        qid = cur.lastrowid
        cur.executemany(
            "INSERT INTO `option` (question_id, label, content, is_correct) "
            "VALUES (%s, %s, %s, %s)",
            [(qid, l, c, int(ok)) for l, c, ok in it['options'] if c])
        imported += 1
        batch_norm.append((qid, text))
        batch_shingles.append((qid, sh))
    return imported, duplicates


def _is_dup(sh, pool):
    """pool: [(id, shingle_set)]，Jaccard 阈值 0.9（先快速剪枝再精算）"""
    for _, other in pool:
        if not sh or not other:
            continue
        inter = len(sh & other)
        if inter * 10 >= 9 * max(len(sh), len(other)):   # 剪枝：jaccard>=0.9 必然满足
            if inter / (len(sh) + len(other) - inter) >= 0.9:
                return True
    return False


def _update_batch(batch_id, **kw):
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            sets = ', '.join(f'{k}=%s' for k in kw)
            cur.execute(f"UPDATE import_batch SET {sets} WHERE id=%s",
                        (*kw.values(), batch_id))
    finally:
        conn.close()


# ---------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------
def start_batch(mode, source_key=None, url=None, year_version='2025'):
    """创建批次记录并启动后台线程，返回批次 id"""
    if mode == 'public':
        src = SOURCES[source_key]
        name, url_used = src['name'], src['urls'][0]
    else:
        name, url_used = '指定网站爬取', url
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO import_batch (source_type, source_name, source_url, "
                "year_version, status, message) VALUES (%s, %s, %s, %s, 'running', %s)",
                ('public_source' if mode == 'public' else 'web_url',
                 name, url_used, year_version, '任务排队中…'))
            batch_id = cur.lastrowid
    finally:
        conn.close()
    threading.Thread(target=_run, args=(batch_id, mode, source_key, url, year_version),
                     daemon=True).start()
    return batch_id


def _run(batch_id, mode, source_key, url, year_version):
    try:
        if mode == 'public':
            src = SOURCES[source_key]
            items = globals()[src['fetch']](year_version)
        else:
            items = _fetch_url(url, year_version)
        _update_batch(batch_id, fetched=len(items),
                      message=f'抓取完成，共 {len(items)} 题，开始去重入库…')

        conn = pymysql.connect(**DB)
        try:
            with conn.cursor() as cur:
                imported, duplicates = dedup_import(cur, items, year_version, batch_id)
        finally:
            conn.close()
        msg = (f'完成：抓取 {len(items)} 题，去重跳过 {duplicates} 题，'
               f'新增入库 {imported} 题（year_version={year_version}）')
        if imported > 0:
            # 入库后自动对新题归类（复用加分项①分类器，全量重跑保证质心一致）
            _update_batch(batch_id, status='done',
                          message=msg + '；正在自动归类…')
            try:
                import sys
                sys.path.insert(0, str(BASE.parent))
                import classifier
                classifier.main(DB['password'])
                msg += '；新题已自动归类'
            except Exception as ce:
                msg += f'；自动归类失败（可手动运行 classifier.py）：{ce}'
        _update_batch(batch_id, imported=imported, duplicates=duplicates,
                      status='done', message=msg)
    except Exception as e:
        _update_batch(batch_id, status='failed',
                      message='失败：' + ''.join(traceback.format_exception_only(e)).strip())


def delete_year(year_version):
    """删除某采集年份的全部题目及关联数据（2026 原始题库由路由层拦截）。
    事务执行，返回删除题数（0 表示该年份无数据）。"""
    conn = pymysql.connect(**dict(DB, autocommit=False))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) c FROM question WHERE year_version=%s",
                        (year_version,))
            n = cur.fetchone()['c']
            if n == 0:
                return 0
            # 按外键依赖顺序删关联数据（子查询定位该年份题目）
            for tbl, col in (('ai_verification', 'question_id'),
                             ('verify_result', 'question_id'),
                             ('wrong_book', 'question_id'),
                             ('practice', 'question_id'),
                             ('exam_detail', 'question_id'),
                             ('question_image', 'question_id'),
                             ('`option`', 'question_id')):
                try:
                    cur.execute(
                        f"DELETE FROM {tbl} WHERE {col} IN "
                        f"(SELECT id FROM question WHERE year_version=%s)",
                        (year_version,))
                except pymysql.Error:
                    pass            # 表无该列等情况跳过
            cur.execute("DELETE FROM question WHERE year_version=%s", (year_version,))
            cur.execute("DELETE FROM import_batch WHERE year_version=%s", (year_version,))
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
