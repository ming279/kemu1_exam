# -*- coding: utf-8 -*-
"""
trend.py - 加分项⑤：历年题库对比趋势分析（共享数据层）

网页端（题库采集页）与命令行脚本（trend_analysis.py 生成 md 报告）共用本模块。
数据基础：
  - question.year_version 标记题库年份（采集增量与原库 docx）
  - question.category_id 为加分项①的自动归类结果
"""
from collections import Counter, defaultdict

import pymysql

DB = dict(host='localhost', user='root', password='123456',
          database='kemu1_exam', charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor)

QTYPES = ['judge', 'single', 'multi']
QT = {'judge': '判断题', 'single': '单选题', 'multi': '多选题'}


def pct(n, d):
    return round(n * 100.0 / d, 1) if d else 0.0


def trend_data():
    """返回历年对比的结构化数据：年份×题型矩阵、年份×分类矩阵、自动结论、样例题。"""
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            # 1. 年份 × 题型
            cur.execute("SELECT year_version y, qtype, COUNT(*) c FROM question "
                        "GROUP BY year_version, qtype")
            year_qtype = defaultdict(Counter)
            for r in cur.fetchall():
                year_qtype[r['y']][r['qtype']] += r['c']
            years = sorted(year_qtype)
            if not years:
                return None

            # 2. 年份 × 分类（LEFT JOIN：未归类题计入"未分类"，避免被 INNER JOIN 丢弃）
            cur.execute(
                "SELECT q.year_version y, COALESCE(c.name, '未分类') cat, COUNT(*) c "
                "FROM question q LEFT JOIN category c ON c.id = q.category_id "
                "GROUP BY q.year_version, COALESCE(c.name, '未分类')")
            year_cat = defaultdict(Counter)
            for r in cur.fetchall():
                year_cat[r['y']][r['cat']] += r['c']
            latest = years[-1]
            growth = years[:-1]
            cats = sorted({c for yc in year_cat.values() for c in yc},
                          key=lambda c: -year_cat[latest][c])

            # 3. 各爬取年份的典型新增题样例
            samples = {}
            for y in growth:
                cur.execute(
                    "SELECT q.stem, COALESCE(c.name, '未分类') cat FROM question q "
                    "LEFT JOIN category c ON c.id = q.category_id "
                    "WHERE q.year_version=%s ORDER BY RAND() LIMIT 3", (y,))
                samples[y] = [{'stem': s['stem'][:60], 'cat': s['cat']}
                              for s in cur.fetchall()]
    finally:
        conn.close()

    totals = {y: sum(year_qtype[y].values()) for y in years}

    # 题型矩阵（含占比）
    qtype_rows = []
    for y in years:
        c = year_qtype[y]
        t = totals[y]
        qtype_rows.append({
            'year': y, 'total': t,
            'judge': c['judge'], 'single': c['single'], 'multi': c['multi'],
            'judge_pct': pct(c['judge'], t),
        })

    # 分类矩阵（数量 + 占比）
    cat_rows = []
    for cat in cats:
        row = {'cat': cat, 'cells': []}
        for y in years:
            n = year_cat[y][cat]
            row['cells'].append({'n': n, 'pct': pct(n, totals[y])})
        cat_rows.append(row)

    # 自动趋势结论
    conclusions = []
    if growth:
        conclusions.append(
            f'题库规模：主库 {totals[latest]} 题（{latest} 年版）；'
            + '、'.join(f'{y} 年版采集新增有效题 {totals[y]} 题' for y in growth)
            + '，历年题库持续扩充更新。')
        diffs = []
        base_t = totals[latest] or 1
        for cat in cats:
            b = year_cat[latest][cat] / base_t
            for y in growth:
                a = year_cat[y][cat] / (totals[y] or 1)
                diffs.append((abs(a - b), y, cat, a - b))
        diffs.sort(reverse=True)
        for d, y, cat, delta in diffs[:4]:
            conclusions.append(
                f'{cat}：{y} 年版占比 {pct(year_cat[y][cat], totals[y])}% '
                f'→ {latest} 年版 {pct(year_cat[latest][cat], base_t)}%，'
                f'最新题库中占比{"上升" if delta < 0 else "下降"}约 {d * 100:.1f} 个百分点。')
    else:
        conclusions.append(f'当前仅 {latest} 年版题库；使用"题库采集"获取历年题目后，'
                           '本页将自动展示年份 × 题型/分类对比与趋势结论。')

    return {'years': years, 'latest': latest, 'growth': growth,
            'totals': totals, 'qtype_rows': qtype_rows,
            'cat_rows': cat_rows, 'conclusions': conclusions, 'samples': samples}
