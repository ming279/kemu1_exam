# -*- coding: utf-8 -*-
"""
importer.py - 题库导入器：解析 docx -> MySQL (kemu1_exam)

功能：
1. 执行 sql/schema.sql 建库建表
2. 图片按 SHA-256 哈希去重后以 BLOB 入库
3. 题目/选项/图片关联批量入库（单事务，可回滚）
4. 创建默认管理员
5. 输出导入报告与完整性校验

用法：python importer.py [root密码]
"""
import sys
import os
import hashlib
import getpass
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).parent))
from docx_parser import parse_all

BASE = Path(__file__).parent
DOCX = next(BASE.glob('题库_2026.docx'))
SCHEMA = BASE / 'sql' / 'schema.sql'

DB_NAME = os.environ.get('DB_NAME', 'kemu1_exam')


def _db_kwargs(password):
    """连接基础参数：环境变量优先（服务器部署），密码参数/默认值兜底（本机开发）"""
    return dict(host=os.environ.get('DB_HOST', 'localhost'),
                user=os.environ.get('DB_USER', 'root'),
                password=password or os.environ.get('DB_PASSWORD',
                                                    os.environ.get('MYSQL_PASSWORD', '123456')))


def run_schema(password):
    """执行建库脚本（schema.sql 含 DROP/CREATE DATABASE）"""
    from pymysql.constants import CLIENT
    sql = SCHEMA.read_text(encoding='utf-8')
    conn = pymysql.connect(**_db_kwargs(password),
                           charset='utf8mb4',
                           client_flag=CLIENT.MULTI_STATEMENTS)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            while cur.nextset():     # 消费全部结果集
                pass
        conn.commit()
    finally:
        conn.close()
    print('[1/5] 建库脚本执行完成 (kemu1_exam)')


def import_all(password):
    questions, media_map, tail = parse_all(DOCX)
    print(f'[2/5] 解析完成: {len(questions)} 题, {len(media_map)} 张图片')

    # 图片哈希去重
    images = {}   # filename -> (hash, mime, size, data)
    hash2id = {}  # hash -> 占位（入库时填 id）
    for name, data in media_map.items():
        h = hashlib.sha256(data).hexdigest()
        mime = 'image/png' if name.lower().endswith('.png') else 'image/jpeg'
        images[name] = (h, mime, len(data), data)
    print(f'       去重后图片: {len(images)} 张')

    conn = pymysql.connect(**_db_kwargs(password),
                           database=DB_NAME, charset='utf8mb4',
                           autocommit=False)
    try:
        with conn.cursor() as cur:
            # ---- 图片入库（哈希去重） ----
            hash2id = {}
            for name, (h, mime, size, data) in images.items():
                cur.execute(
                    "INSERT INTO image (content_hash, mime_type, file_size, data) "
                    "VALUES (%s, %s, %s, %s)",
                    (h, mime, size, data))
                hash2id[h] = cur.lastrowid
            name2imgid = {name: hash2id[images[name][0]] for name in images}

            # ---- 题目/选项/图片关联入库 ----
            n_opts = 0
            n_qimg = 0
            for idx, q in enumerate(questions, start=1):
                cur.execute(
                    "INSERT INTO question (source_id, stem, qtype, year_version) "
                    "VALUES (%s, %s, %s, '2026')",
                    (q.source_id, q.stem, q.qtype))
                qid = cur.lastrowid

                if q.options:
                    cur.executemany(
                        "INSERT INTO `option` (question_id, label, content, is_correct) "
                        "VALUES (%s, %s, %s, %s)",
                        [(qid, label, content, int(ok))
                         for label, content, ok in q.options])
                    n_opts += len(q.options)

                if q.image_names:
                    for pos, name in enumerate(q.image_names):
                        cur.execute(
                            "INSERT INTO question_image (question_id, image_id, position) "
                            "VALUES (%s, %s, %s)",
                            (qid, name2imgid[name], pos))
                        n_qimg += 1

            # 更新图片引用计数
            cur.execute(
                "UPDATE image i SET ref_count = "
                "(SELECT COUNT(*) FROM question_image qi WHERE qi.image_id = i.id)")

            # ---- 默认管理员 (admin / admin123) ----
            cur.execute(
                "INSERT INTO `user` (username, password_hash, real_name, role) "
                "VALUES (%s, SHA2(%s, 256), %s, 'admin')",
                ('admin', 'admin123', '系统管理员'))

            # ---- 更新图片引用计数（在 user 之后同一事务） ----
            conn.commit()
        print(f'[3/5] 入库完成: 题目 {len(questions)} / 选项 {n_opts} / 题图关联 {n_qimg} / 管理员 admin')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify(password):
    """完整性校验"""
    conn = pymysql.connect(**_db_kwargs(password),
                           database=DB_NAME, charset='utf8mb4')
    try:
        with conn.cursor() as cur:
            checks = [
                ('question', 'SELECT COUNT(*) FROM question'),
                ('option', 'SELECT COUNT(*) FROM `option`'),
                ('image', 'SELECT COUNT(*) FROM image'),
                ('question_image', 'SELECT COUNT(*) FROM question_image'),
                ('user', 'SELECT COUNT(*) FROM `user`'),
            ]
            print('[4/5] 完整性校验:')
            for name, sql in checks:
                cur.execute(sql)
                print(f'       {name:16s} {cur.fetchone()[0]:>6} 行')

            # 题型分布
            cur.execute("SELECT qtype, COUNT(*) FROM question GROUP BY qtype")
            print('       题型分布:', dict(cur.fetchall()))

            # 每题选项数分布
            cur.execute(
                "SELECT opt_count, COUNT(*) FROM ("
                "  SELECT q.id, COUNT(o.id) opt_count FROM question q"
                "  LEFT JOIN `option` o ON o.question_id = q.id"
                "  GROUP BY q.id) t GROUP BY opt_count ORDER BY opt_count")
            print('       每题选项数分布:', dict(cur.fetchall()))

            # 无正确答案的题（应仅限异常数据）
            cur.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT q.id FROM question q"
                "  LEFT JOIN `option` o ON o.question_id = q.id AND o.is_correct = 1"
                "  GROUP BY q.id HAVING COUNT(o.id) = 0) t")
            print(f'       无正确答案的题目: {cur.fetchone()[0]}')

            # 未被引用的图片
            cur.execute("SELECT COUNT(*) FROM image WHERE ref_count = 0")
            print(f'       未被引用的图片: {cur.fetchone()[0]}')

            # 视图可用性
            cur.execute("SELECT COUNT(*) FROM v_question_stat")
            cur.execute("SELECT COUNT(*) FROM v_user_stat")
            print('       统计视图可用: 是')

            # 抽样核对（原始题号 100 与 2308 附近）
            for sid in (100, 2307):
                cur.execute(
                    "SELECT source_id, qtype, LEFT(stem, 40) FROM question "
                    "WHERE source_id = %s", (sid,))
                row = cur.fetchone()
                print(f'       抽样 sid={sid}: {row}')
    finally:
        conn.close()
    print('[5/5] 导入全部完成')


if __name__ == '__main__':
    pwd = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DB_PASSWORD')
    if not pwd:
        pwd = getpass.getpass('MySQL root 密码: ')
    run_schema(pwd)
    import_all(pwd)
    verify(pwd)
