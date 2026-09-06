# -*- coding: utf-8 -*-
"""
trend_analysis.py - 加分项⑤：历年题库对比趋势分析

数据基础：
  - question.year_version 标记题库年份（2021/2022 来自加分项④爬取增量，2026 为原库）
  - question.category_id 为加分项①的自动归类结果

分析维度：
  1. 各年份题量与题型结构
  2. 各年份 × 知识分类的占比矩阵与变化趋势
  3. 增量题（2021/2022 相对 2026 库未重复的部分）的分布特征与典型样例

输出：trend_report.md
"""
import sys
from pathlib import Path
from collections import Counter, defaultdict

import pymysql

BASE = Path(__file__).parent
REPORT = BASE / 'trend_report.md'
DB = dict(host='localhost', user='root', password='123456',
          database='kemu1_exam', charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor)

QT = {'judge': '判断题', 'single': '单选题', 'multi': '多选题'}


def pct(n, d):
    return f'{n * 100.0 / d:.1f}%' if d else '—'


def main():
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            # ---- 1. 年份 × 题型 ----
            cur.execute("SELECT year_version, qtype, COUNT(*) c FROM question "
                        "GROUP BY year_version, qtype")
            year_qtype = defaultdict(Counter)
            for r in cur.fetchall():
                year_qtype[r['year_version']][r['qtype']] += r['c']
            years = sorted(year_qtype)

            # ---- 2. 年份 × 分类 ----
            cur.execute(
                "SELECT q.year_version y, c.name cat, COUNT(*) c FROM question q "
                "JOIN category c ON c.id = q.category_id "
                "GROUP BY q.year_version, c.name")
            year_cat = defaultdict(Counter)
            for r in cur.fetchall():
                year_cat[r['y']][r['cat']] += r['c']
            cats = sorted({c for yc in year_cat.values() for c in yc},
                          key=lambda c: -year_cat[years[-1]][c])

            # ---- 3. 增量题典型样例（爬取年份中随机挑） ----
            samples = {}
            for y in years:
                if y == years[-1]:
                    continue
                cur.execute(
                    "SELECT q.stem, c.name cat FROM question q "
                    "JOIN category c ON c.id = q.category_id "
                    "WHERE q.year_version=%s ORDER BY RAND() LIMIT 3", (y,))
                samples[y] = cur.fetchall()
    finally:
        conn.close()

    lines = ['# 历年题库对比趋势分析报告', '',
             '> 数据来源：question 表按 year_version 分组；',
             f'> 2026 为原库（题库_2026.docx），'
             f'{"、".join(y for y in years if y != years[-1])} 为加分项④从网上爬取并去重后增量入库的版本。',
             '']
    total_by_year = {y: sum(year_qtype[y].values()) for y in years}

    # ---- 1. 题量与题型结构 ----
    lines += ['## 一、各年份题量与题型结构', '',
              '| 年份版本 | 题量 | 判断题 | 单选题 | 多选题 | 判断题占比 |',
              '|---|---|---|---|---|---|']
    for y in years:
        c = year_qtype[y]
        t = total_by_year[y]
        lines.append(f"| {y} | {t} | {c['judge']} | {c['single']} | {c['multi']} "
                     f"| {pct(c['judge'], t)} |")
    lines.append('')

    # ---- 2. 分类占比矩阵 ----
    lines += ['## 二、知识分类占比对比（按 2026 版占比降序）', '']
    header = '| 知识分类 | ' + ' | '.join(years) + ' |'
    sep = '|---|' + '---|' * len(years)
    lines += [header, sep]
    base_t = total_by_year[years[-1]]
    for cat in cats:
        row = [f'{cat}']
        for y in years:
            n = year_cat[y][cat]
            row.append(f'{n}（{pct(n, total_by_year[y])}）')
        lines.append('| ' + ' | '.join(row) + ' |')
    lines.append('')

    # ---- 3. 趋势结论（自动生成） ----
    lines += ['## 三、趋势观察', '']
    # 题量变化
    growth = [y for y in years if y != years[-1]]
    lines.append(f'1. **题库规模**：主库为 {total_by_year[years[-1]]} 题（{years[-1]} 年版）；'
                 f'爬取的历史版本中，'
                 + '、'.join(f'{y} 年版新增有效题 {total_by_year[y]} 题'
                             for y in growth)
                 + '，可见历年题库在持续扩充与更新。')
    # 分类占比变化 top：与 2026 对比差值最大的分类
    diffs = []
    for cat in cats:
        b = year_cat[years[-1]][cat] / (base_t or 1)
        for y in growth:
            a = year_cat[y][cat] / (total_by_year[y] or 1)
            diffs.append((abs(a - b), y, cat, a - b))
    diffs.sort(reverse=True)
    for i, (d, y, cat, delta) in enumerate(diffs[:4], len(lines) + 1):
        arrow = '上升' if delta < 0 else '下降'   # 历史年份占比更高 -> 在新库中下降
        lines.append(f'{i}. **{cat}**：{y} 年版占比 {pct(year_cat[y][cat], total_by_year[y])}'
                     f' → {years[-1]} 年版 {pct(year_cat[years[-1]][cat], base_t)}，'
                     f'在最新题库中占比{"上升" if delta < 0 else "下降"}约 {abs(delta) * 100:.1f} 个百分点。')
    lines.append('')
    # ---- 4. 典型新增题样例 ----
    lines += ['## 四、各爬取年份典型新增题样例', '']
    for y in growth:
        lines.append(f'**{y} 年版样例**：')
        for s in samples[y]:
            lines.append(f'- [{s["cat"]}] {s["stem"][:60]}')
        lines.append('')

    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print(f'报告已写入: {REPORT}')
    print('\n'.join(lines[:24]))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
