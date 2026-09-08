# -*- coding: utf-8 -*-
"""
classifier.py - 加分项①：题目自动归类

两阶段分类（无监督+弱监督，无需人工标注全量数据）：
  阶段1  规则匹配：为每个分类人工配置带权关键词，按命中加权计分，取最高分且超过阈值的分类
  阶段2  TF-IDF 最近质心：以阶段1的结果作为弱标注语料，构建各分类的 TF-IDF 质心向量，
         对规则未命中的题目计算余弦相似度，取最相似分类；相似度过低则归入"综合知识"

结果写入 category 表并回填 question.category_id。

用法：python classifier.py [root密码]
"""
import sys
import os
import math
import getpass
from pathlib import Path
from collections import Counter

import jieba
import pymysql

BASE = Path(__file__).parent
DB_NAME = os.environ.get('DB_NAME', 'kemu1_exam')
sys.path.insert(0, str(BASE))

# ---------------------------------------------------------------
# 分类体系与关键词规则 (关键词, 权重)
# ---------------------------------------------------------------
CATEGORIES = [
    ('驾驶证申领与使用', [
        ('驾驶证', 3), ('申领', 4), ('换证', 4), ('补证', 4), ('审验', 4),
        ('实习期', 4), ('准驾车型', 4), ('有效期', 2), ('身体条件', 2),
        ('报考', 3), ('考试合格', 2), ('满分学习', 4), ('注销', 2), ('降级', 3),
        ('驾驶技能准考', 4), ('申请人', 2), ('核发', 2),
    ]),
    ('机动车登记与检验', [
        ('登记', 4), ('注册登记', 4), ('检验', 3), ('年检', 4), ('报废', 4),
        ('保险', 3), ('交强险', 4), ('号牌', 4), ('悬挂', 2), ('行驶证', 4),
        ('改装', 3), ('转移', 2), ('变更登记', 4), ('安检', 3),
    ]),
    ('道路通行规定', [
        ('车道', 2), ('限速', 3), ('超车', 4), ('会车', 4), ('掉头', 4),
        ('倒车', 4), ('变道', 3), ('通行', 2), ('让行', 3), ('超速', 3),
        ('超载', 3), ('超员', 3), ('停放', 3), ('停车', 2), ('路口', 2),
        ('环岛', 3), ('单行道', 3), ('载物', 3), ('载人', 2), ('逆行', 4),
        ('驶入', 1), ('驶离', 1),
    ]),
    ('交通信号', [
        ('信号灯', 5), ('红灯', 4), ('绿灯', 4), ('黄灯', 4), ('标志', 4),
        ('标线', 4), ('手势', 5), ('指示', 2), ('警告标志', 4), ('禁令标志', 4),
        ('可变导向', 4), ('导向箭头', 4), ('交通信号', 5), ('闪烁', 2),
    ]),
    ('违法记分与处罚', [
        ('记分', 4), ('罚款', 4), ('拘留', 4), ('吊销', 5), ('醉驾', 5),
        ('酒驾', 5), ('饮酒', 3), ('无证驾驶', 4), ('刑事责任', 4),
        ('违法', 3), ('违章', 3), ('处罚', 3), ('扣留', 3), ('暂扣', 4),
        ('伪造', 3), ('变造', 3), ('逃逸', 2), ('拘役', 4),
    ]),
    ('安全文明行车', [
        ('礼让', 5), ('文明', 2), ('喇叭', 4), ('鸣笛', 4), ('安全带', 5),
        ('头枕', 4), ('疲劳驾驶', 4), ('接打', 4), ('远光灯', 4), ('近光灯', 4),
        ('雾灯', 4), ('转向灯', 4), ('灯光', 2), ('争道', 4), ('安全距离', 3),
        ('跟车', 2), ('观察', 1), ('缓慢', 1),
    ]),
    ('特殊路段与恶劣天气驾驶', [
        ('高速公路', 5), ('隧道', 5), ('桥梁', 4), ('涵洞', 4), ('夜间', 4),
        ('雾', 3), ('雨', 2), ('冰雪', 4), ('泥泞', 4), ('涉水', 4),
        ('山区', 4), ('弯道', 3), ('陡坡', 4), ('连续下坡', 4), ('铁道路口', 5),
        ('渡口', 4), ('结冰', 4), ('雾天', 4), ('大风', 3), ('低能见度', 4),
    ]),
    ('紧急情况临危处置', [
        ('爆胎', 5), ('制动失灵', 5), ('刹车失灵', 5), ('转向失控', 5),
        ('起火', 5), ('失火', 5), ('落水', 5), ('侧滑', 4), ('翻车', 5),
        ('碰撞', 4), ('熄火', 3), ('抱死', 4), ('紧急制动', 3), ('躲避', 2),
        ('应急', 2), ('爆震', 3),
    ]),
    ('交通事故处理', [
        ('交通事故', 5), ('事故', 3), ('报警', 4), ('撤离', 4), ('现场', 3),
        ('责任认定', 4), ('逃逸', 3), ('快处', 4), ('协商', 3), ('保护现场', 5),
        ('抢救伤员', 4), ('122', 4), ('110', 3),
    ]),
    ('伤员自救与急救', [
        ('急救', 5), ('止血', 5), ('包扎', 5), ('骨折', 5), ('心肺复苏', 5),
        ('人工呼吸', 5), ('烧伤', 4), ('烫伤', 4), ('中毒', 4), ('伤员', 4),
        ('自救', 4), ('救护', 4), ('动脉', 4), ('止血带', 5), ('昏迷', 4),
    ]),
    ('车辆构造与日常维护', [
        ('轮胎', 4), ('气压', 4), ('发动机', 4), ('仪表', 4), ('冷却液', 5),
        ('机油', 5), ('蓄电池', 4), ('制动系统', 4), ('防抱死', 5), ('ABS', 5),
        ('安全气囊', 5), ('后视镜', 4), ('日常检查', 4), ('维护', 3),
        ('保养', 4), ('燃油', 3), ('冷却', 2), ('风扇', 3), ('节温器', 5),
    ]),
]
FALLBACK_CATEGORY = '综合知识'
RULE_THRESHOLD = 3          # 规则阶段最低得分
TFIDF_MIN_SIM = 0.08        # 质心相似度下限，低于则归入兜底分类


def tokenize(text):
    """jieba 分词，保留长度 >= 2 的词"""
    return [t for t in jieba.lcut(text) if len(t) >= 2]


def rule_classify(stem):
    """阶段1：关键词加权计分 -> (分类, 得分) 或 (None, 0)"""
    scores = {}
    for name, kws in CATEGORIES:
        s = 0
        for kw, w in kws:
            n = stem.count(kw)
            if n:
                s += w * min(n, 2)      # 同一关键词最多计 2 次
        if s:
            scores[name] = s
    if not scores:
        return None, 0
    best = max(scores, key=scores.get)
    return (best, scores[best]) if scores[best] >= RULE_THRESHOLD else (None, 0)


def build_tfidf(tokens_list):
    """构建 TF-IDF 向量（dict: token -> weight）与 DF 表"""
    n_docs = len(tokens_list)
    df = Counter()
    for toks in tokens_list:
        df.update(set(toks))
    idf = {t: math.log((1 + n_docs) / (1 + d)) + 1 for t, d in df.items()}
    vectors = []
    for toks in tokens_list:
        tf = Counter(toks)
        norm = sum(tf.values()) or 1
        vectors.append({t: c / norm * idf[t] for t, c in tf.items()})
    return vectors, idf


def cosine(v1, v2):
    if len(v2) < len(v1):
        v1, v2 = v2, v1
    dot = sum(w * v2.get(t, 0.0) for t, w in v1.items())
    n1 = math.sqrt(sum(w * w for w in v1.values()))
    n2 = math.sqrt(sum(w * w for w in v2.values()))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def main(password=None):
    conn = pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        user=os.environ.get('DB_USER', 'root'),
        password=password or os.environ.get('DB_PASSWORD',
                                            os.environ.get('MYSQL_PASSWORD', '123456')),
        database=DB_NAME, charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, stem FROM question")
            rows = cur.fetchall()
            stems = [r['stem'] for r in rows]
            print(f'[1/4] 读取题目 {len(rows)} 条')

            # ---- 阶段1：规则分类 ----
            labels = []              # 每题 -> 分类名或 None
            rule_n = 0
            for stem in stems:
                cat, score = rule_classify(stem)
                labels.append(cat)
                if cat:
                    rule_n += 1
            print(f'[2/4] 规则分类完成: 命中 {rule_n} 题')

            # ---- 阶段2：TF-IDF 最近质心 ----
            tokens_list = [tokenize(s) for s in stems]
            vectors, idf = build_tfidf(tokens_list)
            # 以规则命中题目作为弱标注语料，计算各分类质心
            centroids = {}
            for i, cat in enumerate(labels):
                if not cat:
                    continue
                if cat not in centroids:
                    centroids[cat] = dict(vectors[i])
                else:
                    for t, w in vectors[i].items():
                        centroids[cat][t] = centroids[cat].get(t, 0.0) + w
            for cat in centroids:
                cw = centroids[cat]
                centroids[cat] = {t: w / max(sum(cw.values()), 1) for t, w in cw.items()}

            tfidf_n = 0
            for i, cat in enumerate(labels):
                if cat:
                    continue
                sims = {c: cosine(vectors[i], cv) for c, cv in centroids.items()}
                if not sims:
                    labels[i] = FALLBACK_CATEGORY
                    continue
                best = max(sims, key=sims.get)
                if sims[best] >= TFIDF_MIN_SIM:
                    labels[i] = best
                    tfidf_n += 1
                else:
                    labels[i] = FALLBACK_CATEGORY
            print(f'[3/4] TF-IDF 质心分类完成: 补充归类 {tfidf_n} 题')

            # ---- 写库 ----
            names = [name for name, _ in CATEGORIES] + [FALLBACK_CATEGORY]
            for name in names:
                cur.execute(
                    "INSERT INTO category (name) VALUES (%s) "
                    "ON DUPLICATE KEY UPDATE name=name", (name,))
            cur.execute("SELECT id, name FROM category")
            cat_id = {r['name']: r['id'] for r in cur.fetchall()}

            cur.execute("UPDATE question SET category_id=NULL")
            by_cat = {}
            for r, cat in zip(rows, labels):
                by_cat.setdefault(cat, []).append(r['id'])
            for cat, qids in by_cat.items():
                ph = ','.join(['%s'] * len(qids))
                cur.execute(f"UPDATE question SET category_id=%s WHERE id IN ({ph})",
                            [cat_id[cat]] + qids)
            conn.commit()

            print('[4/4] 已写库: category 表 + question.category_id 回填\n')
            print(f"{'分类':<14}{'题数':>6}")
            print('-' * 24)
            for name in names:
                print(f"{name:<14}{len(by_cat.get(name, [])):>6}")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    pwd = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DB_PASSWORD')
    if not pwd:
        pwd = getpass.getpass('MySQL root 密码: ')
    main(pwd)
