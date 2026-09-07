# -*- coding: utf-8 -*-
"""
trend_analysis.py - 加分项⑤：历年题库对比趋势分析（命令行报告生成）

数据逻辑在 app/trend.py（网页端"题库采集"页共用），本脚本将其渲染为
trend_report.md 交付报告。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'app'))
from trend import trend_data, QT  # noqa: E402

BASE = Path(__file__).parent
REPORT = BASE / 'trend_report.md'


def main():
    d = trend_data()
    if not d:
        print('题库为空，无法生成报告')
        return
    years, latest, growth = d['years'], d['latest'], d['growth']
    totals = d['totals']

    def pp(n, den):
        return f'{n * 100.0 / den:.1f}%' if den else '—'

    src_line = (f'> {latest} 为原库（题库_2026.docx）'
                + (f'；{"、".join(growth)} 为加分项④从网上爬取并去重后增量入库的版本。'
                   if growth else '；当前暂无采集年份，使用题库采集功能获取历年题目后本报告自动扩展。'))
    lines = ['# 历年题库对比趋势分析报告', '',
             '> 数据来源：question 表按 year_version 分组；', src_line, '']

    # 一、题量与题型结构
    lines += ['## 一、各年份题量与题型结构', '',
              '| 年份版本 | 题量 | 判断题 | 单选题 | 多选题 | 判断题占比 |',
              '|---|---|---|---|---|---|']
    for r in d['qtype_rows']:
        lines.append(f"| {r['year']} | {r['total']} | {r['judge']} | {r['single']} "
                     f"| {r['multi']} | {r['judge_pct']}% |")
    lines.append('')

    # 二、分类占比矩阵
    lines += ['## 二、知识分类占比对比（按最新版占比降序）', '',
              '| 知识分类 | ' + ' | '.join(years) + ' |',
              '|---|' + '---|' * len(years)]
    for row in d['cat_rows']:
        cells = ' | '.join(f"{c['n']}（{c['pct']}%）" for c in row['cells'])
        lines.append(f"| {row['cat']} | {cells} |")
    lines.append('')

    # 三、趋势结论
    lines += ['## 三、趋势观察', '']
    for i, c in enumerate(d['conclusions'], 1):
        lines.append(f'{i}. **{c.split("：", 1)[0]}**：{c.split("：", 1)[1]}'
                     if '：' in c else f'{i}. {c}')
    lines.append('')

    # 四、典型新增题样例
    if growth:
        lines += ['## 四、各采集年份典型新增题样例', '']
        for y in growth:
            lines.append(f'**{y} 年版样例**：')
            for s in d['samples'][y]:
                lines.append(f'- [{s["cat"]}] {s["stem"]}')
            lines.append('')

    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print(f'报告已写入: {REPORT}')
    print('\n'.join(lines[:24]))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
