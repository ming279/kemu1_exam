# -*- coding: utf-8 -*-
"""
duplicate_detector.py - 加分项②：题库重复题检测

流程：
  1. 文本规范化：题干+选项去标点/空白（判断题选项为固定"正确/错误"，仅用题干）
  2. 倒排索引筛候选对：字符 2-gram 建倒排，过滤文档频率过高（>60）的无区分度片段，
        共同 2-gram 数 >= 8 的题对才进入精算，避免 O(n^2) 全量比较
  3. 相似度精算：编辑距离相似度 + 词级 Jaccard，取较大者；>= 0.85 判为疑似重复
  4. 并查集聚簇 -> 输出报告 duplicate_report.md

用法：python duplicate_detector.py [root密码]
"""
import sys
import re
import getpass
from collections import Counter, defaultdict
from pathlib import Path

import jieba
import pymysql

BASE = Path(__file__).parent
DB_NAME = 'kemu1_exam'
REPORT = BASE / 'duplicate_report.md'

MIN_SHARED_SHINGLE = 8     # 候选对最少共同 2-gram 数
MAX_DF = 60                # 2-gram 出现在超过 60 题中则无区分度，跳过
SIM_THRESHOLD = 0.85       # 疑似重复阈值

PUNCT_RE = re.compile(r'[\s\W_]+', re.UNICODE)


def normalize(text):
    return PUNCT_RE.sub('', text)


def shingles(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def edit_sim(a, b):
    """归一化编辑距离相似度 = 1 - dist/max(len)"""
    if a == b:
        return 1.0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))


def word_jaccard(a, b):
    wa = {t for t in jieba.lcut(a) if len(t) >= 2}
    wb = {t for t in jieba.lcut(b) if len(t) >= 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def main(password):
    conn = pymysql.connect(host='localhost', user='root', password=password,
                           database=DB_NAME, charset='utf8mb4',
                           cursorclass=pymysql.cursors.DictCursor)
    with conn.cursor() as cur:
        cur.execute("SELECT id, source_id, stem, qtype FROM question ORDER BY id")
        questions = cur.fetchall()
        cur.execute("SELECT question_id, label, content, is_correct FROM `option`")
        opts = cur.fetchall()
    conn.close()

    # 组织选项（judge 题选项固定"正确/错误"，不参与文本比对）
    opt_map = defaultdict(list)
    opt_all = defaultdict(list)
    for o in opts:
        opt_all[o['question_id']].append(o)
        if o['is_correct'] and o['content'] not in ('正确', '错误'):
            opt_map[o['question_id']].append(f"{o['label']}{o['content']}")

    docs = []      # 规范化全文（题干+正确选项内容）
    for qq in questions:
        ans_text = ''.join(opt_map.get(qq['id'], [])) if qq['qtype'] != 'judge' else ''
        docs.append(normalize(qq['stem'] + ans_text))

    # 倒排索引筛候选对
    inv = defaultdict(list)
    for i, d in enumerate(docs):
        for sh in shingles(d):
            inv[sh].append(i)
    pair_counter = Counter()
    for sh, doc_ids in inv.items():
        if len(doc_ids) > MAX_DF or len(doc_ids) < 2:
            continue
        for a in range(len(doc_ids)):
            for b in range(a + 1, len(doc_ids)):
                pair_counter[(doc_ids[a], doc_ids[b])] += 1
    candidates = [p for p, c in pair_counter.items() if c >= MIN_SHARED_SHINGLE]
    print(f'候选题对（共同 2-gram >= {MIN_SHARED_SHINGLE}）: {len(candidates)} 对')

    # 精算相似度
    pairs = []
    for i, j in candidates:
        sim = max(edit_sim(docs[i], docs[j]), word_jaccard(docs[i], docs[j]))
        if sim >= SIM_THRESHOLD:
            pairs.append((i, j, sim))
    pairs.sort(key=lambda x: -x[2])
    print(f'疑似重复题对（相似度 >= {SIM_THRESHOLD}）: {len(pairs)} 对')

    # 并查集聚簇
    dsu = DSU(len(questions))
    for i, j, _ in pairs:
        dsu.union(i, j)
    clusters = defaultdict(list)
    for i in range(len(questions)):
        clusters[dsu.find(i)].append(i)
    dup_clusters = sorted((sorted(m) for m in clusters.values() if len(m) > 1),
                          key=len, reverse=True)
    print(f'重复题簇: {len(dup_clusters)} 簇')

    # ---- 输出报告 ----
    with open(REPORT, 'w', encoding='utf-8') as f:
        f.write('# 题库重复题检测报告\n\n')
        f.write(f'- 检测范围：{len(questions)} 题（question 表全量）\n')
        f.write(f'- 方法：字符 2-gram 倒排索引筛候选（共同片段 ≥ {MIN_SHARED_SHINGLE}，'
                f'文档频率 ≤ {MAX_DF}），编辑距离相似度与词级 Jaccard 取最大值\n')
        f.write(f'- 判定阈值：相似度 ≥ {SIM_THRESHOLD}；并查集聚簇\n')
        f.write(f'- 结果：疑似重复题对 {len(pairs)} 对，'
                f'涉及 {sum(len(c) for c in dup_clusters)} 题 / {len(dup_clusters)} 簇\n\n')
        f.write('## 重复题簇明细（按簇内题数、相似度排序）\n\n')
        for rank, cluster in enumerate(dup_clusters, 1):
            f.write(f'### 簇 {rank}（{len(cluster)} 题）\n\n')
            for i in cluster:
                qq = questions[i]
                f.write(f"- **题目 #{qq['id']}**（原题号 {qq['source_id']}，"
                        f"题型 {qq['qtype']}）：{qq['stem']}\n")
                for o in opt_all.get(qq['id'], []):
                    f.write(f"  - {o['label']}. {o['content']}"
                            f"{' ✔' if o['is_correct'] else ''}\n")
            sims_in = [(s, i, j) for i, j, s in pairs
                       if i in cluster and j in cluster]
            if sims_in:
                f.write(f"  - 簇内相似度: {', '.join(f'#{questions[i]['id']}~#{questions[j]['id']}={s:.3f}' for s, i, j in sims_in)}\n")
            f.write('\n')

        if pairs:
            f.write('## 全部疑似重复题对\n\n| 题目A | 题目B | 相似度 |\n|---|---|---|\n')
            for i, j, s in pairs:
                f.write(f"| #{questions[i]['id']} {questions[i]['stem'][:30]}… "
                        f"| #{questions[j]['id']} {questions[j]['stem'][:30]}… "
                        f"| {s:.3f} |\n")

    print(f'报告已写入: {REPORT}')
    for c in dup_clusters[:10]:
        print('簇:', [f"#{questions[i]['id']}(原{questions[i]['source_id']})" for i in c])


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    pwd = sys.argv[1] if len(sys.argv) > 1 else getpass.getpass('MySQL root 密码: ')
    main(pwd)
