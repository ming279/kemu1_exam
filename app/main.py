# -*- coding: utf-8 -*-
"""
main.py - 驾照科目一题库管理与模拟考试系统（B/S 架构，Flask）

模块划分：
  auth      注册/登录/登出
  exam      随机组卷 -> 模拟考试 -> 判分 -> 成绩单
  practice  顺序练习（逐题反馈，自动维护错题本）
  wrongbook 错题本（查看/标记掌握）
  stats     个人统计 / 管理员全局统计
  image     图片服务（从数据库 BLOB 输出）
"""
import os
import random
import hashlib
from functools import wraps

import pymysql
from flask import (Flask, g, session, request, redirect, url_for,
                   render_template, flash, Response, abort)

app = Flask(__name__)
app.secret_key = 'kemu1-exam-2026-sec'

DB = dict(host='localhost', user='root',
          password=os.environ.get('MYSQL_PASSWORD', '123456'),
          database='kemu1_exam', charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor, autocommit=True)

# 模拟考试组卷参数（科目一：100 题）
EXAM_SINGLE_COUNT = 60     # 单选题数
EXAM_JUDGE_COUNT = 40      # 判断题数

QTYPE_NAME = {'judge': '判断题', 'single': '单选题', 'multi': '多选题'}


# ---------------- 数据库访问 ----------------
def get_db():
    if 'db' not in g:
        g.db = pymysql.connect(**DB)
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def q(sql, args=(), one=False):
    """查询：返回列表或单行"""
    with get_db().cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    """写入"""
    with get_db().cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


# ---------------- 登录控制 ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'uid' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    if 'uid' not in session:
        return None
    return q("SELECT id, username, real_name, role FROM `user` WHERE id=%s",
             (session['uid'],), one=True)


@app.context_processor
def inject_user():
    return dict(cur_user=current_user(), QTYPE_NAME=QTYPE_NAME)


def sha256(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def load_questions(qids):
    """批量加载题目（题干/选项/图片），按给定顺序返回"""
    if not qids:
        return []
    ph = ','.join(['%s'] * len(qids))
    rows = q(f"SELECT id, stem, qtype FROM question WHERE id IN ({ph})", qids)
    opt_rows = q(f"SELECT question_id, label, content, is_correct FROM `option` "
                 f"WHERE question_id IN ({ph}) ORDER BY label", qids)
    img_rows = q(f"SELECT question_id, image_id FROM question_image "
                 f"WHERE question_id IN ({ph}) ORDER BY position", qids)
    by_id = {r['id']: dict(r, options=[], images=[]) for r in rows}
    for r in opt_rows:
        by_id[r['question_id']]['options'].append(r)
    for r in img_rows:
        by_id[r['question_id']]['images'].append(r['image_id'])
    return [by_id[i] for i in qids if i in by_id]


def judge_answer(question, user_labels):
    """判分：user_labels 为用户提交的 label 列表"""
    correct_labels = sorted(o['label'] for o in question['options'] if o['is_correct'])
    return sorted(user_labels) == correct_labels


# ---------------- auth 模块 ----------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        if not username or not password:
            flash('账号和密码不能为空', 'danger')
        elif q("SELECT id FROM `user` WHERE username=%s", (username,), one=True):
            flash('该账号已被注册', 'danger')
        else:
            execute("INSERT INTO `user` (username, password_hash, role) "
                    "VALUES (%s, %s, 'student')", (username, sha256(password)))
            flash('注册成功，请登录', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        row = q("SELECT id, password_hash FROM `user` WHERE username=%s",
                (username,), one=True)
        if row and row['password_hash'] == sha256(request.form['password']):
            session['uid'] = row['id']
            return redirect(url_for('index'))
        flash('账号或密码错误', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    stats = dict(
        total=q("SELECT COUNT(*) c FROM question", one=True)['c'],
        users=q("SELECT COUNT(*) c FROM `user`", one=True)['c'],
        papers=q("SELECT COUNT(*) c FROM exam_paper WHERE status='finished'",
                 one=True)['c'],
        my_wrong=q("SELECT COUNT(*) c FROM wrong_book WHERE user_id=%s "
                   "AND mastered=0", (session['uid'],), one=True)['c'],
    )
    return render_template('index.html', stats=stats)


# ---------------- exam 模块 ----------------
@app.route('/exam/start', methods=['POST'])
@login_required
def exam_start():
    """随机组卷：判断题 1-40 在前，单选题 41-100 在后（按题型分区展示）"""
    judges = [r['id'] for r in
              q(f"SELECT id FROM question WHERE qtype='judge' "
                f"ORDER BY RAND() LIMIT {EXAM_JUDGE_COUNT}")]
    singles = [r['id'] for r in
               q(f"SELECT id FROM question WHERE qtype='single' "
                 f"ORDER BY RAND() LIMIT {EXAM_SINGLE_COUNT}")]
    ids = judges + singles

    pid = execute("INSERT INTO exam_paper (user_id, total_count) VALUES (%s, %s)",
                  (session['uid'], len(ids)))
    for seq, qid in enumerate(ids, 1):
        execute("INSERT INTO exam_detail (paper_id, question_id, seq_no) "
                "VALUES (%s, %s, %s)", (pid, qid, seq))
    return redirect(url_for('exam_page', pid=pid))


@app.route('/exam/<int:pid>')
@login_required
def exam_page(pid):
    paper = q("SELECT * FROM exam_paper WHERE id=%s AND user_id=%s",
              (pid, session['uid']), one=True)
    if not paper:
        abort(404)
    if paper['status'] == 'finished':
        return redirect(url_for('exam_result', pid=pid))
    details = q("SELECT question_id, seq_no FROM exam_detail WHERE paper_id=%s "
                "ORDER BY seq_no", (pid,))
    questions = load_questions([d['question_id'] for d in details])
    for r, d in zip(questions, details):
        r['seq'] = d['seq_no']
    judges = [r for r in questions if r['qtype'] == 'judge']
    singles = [r for r in questions if r['qtype'] != 'judge']
    return render_template('exam.html', pid=pid, judges=judges, singles=singles)


@app.route('/exam/<int:pid>/submit', methods=['POST'])
@login_required
def exam_submit(pid):
    paper = q("SELECT * FROM exam_paper WHERE id=%s AND user_id=%s",
              (pid, session['uid']), one=True)
    if not paper or paper['status'] == 'finished':
        return redirect(url_for('exam_result', pid=pid))

    details = q("SELECT id, question_id FROM exam_detail WHERE paper_id=%s "
                "ORDER BY seq_no", (pid,))
    questions = {qq['id']: qq for qq in
                 load_questions([d['question_id'] for d in details])}

    score = 0
    for d in details:
        question = questions[d['question_id']]
        labels = request.form.getlist(f"q_{d['question_id']}")
        ok = bool(labels) and judge_answer(question, labels)
        execute("UPDATE exam_detail SET user_answer=%s, is_correct=%s "
                "WHERE id=%s",
                (''.join(sorted(labels)) if labels else None, int(ok), d['id']))
        score += ok
        # 考试答错 -> 写入错题本
        if not ok:
            execute(
                "INSERT INTO wrong_book (user_id, question_id) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE wrong_count = wrong_count + 1, "
                "last_wrong_at = CURRENT_TIMESTAMP, mastered = 0",
                (session['uid'], d['question_id']))

    total = paper['total_count']
    execute("UPDATE exam_paper SET score=%s, status='finished', "
            "submitted_at=CURRENT_TIMESTAMP WHERE id=%s",
            (round(score * 100.0 / total, 2), pid))
    return redirect(url_for('exam_result', pid=pid))


@app.route('/exam/<int:pid>/result')
@login_required
def exam_result(pid):
    paper = q("SELECT * FROM exam_paper WHERE id=%s AND user_id=%s",
              (pid, session['uid']), one=True)
    if not paper:
        abort(404)
    details = q("SELECT d.question_id, d.user_answer, d.is_correct, d.seq_no "
                "FROM exam_detail d WHERE d.paper_id=%s ORDER BY d.seq_no", (pid,))
    questions = load_questions([d['question_id'] for d in details])
    for d, qq in zip(details, questions):
        qq['user_answer'] = d['user_answer']
        qq['is_correct'] = d['is_correct']
        qq['seq'] = d['seq_no']
    judges = [r for r in questions if r['qtype'] == 'judge']
    singles = [r for r in questions if r['qtype'] != 'judge']
    return render_template('exam_result.html', paper=paper,
                           judges=judges, singles=singles)


# ---------------- practice 模块 ----------------
@app.route('/practice', methods=['POST'])
@login_required
def practice_start():
    """随机抽 10 题开始一轮练习"""
    ids = [r['id'] for r in
           q("SELECT id FROM question ORDER BY RAND() LIMIT 10")]
    session['p_ids'] = ids
    return redirect(url_for('practice_page', idx=0))


@app.route('/practice/<int:idx>', methods=['GET'])
@login_required
def practice_page(idx):
    p_ids = session.get('p_ids', [])
    if idx >= len(p_ids):
        return redirect(url_for('practice_summary'))
    question = load_questions([p_ids[idx]])[0]
    fb = session.pop('p_feedback', None)      # 上一题的判分反馈
    return render_template('practice.html', idx=idx, total=len(p_ids),
                           question=question, feedback=fb)


@app.route('/practice/answer', methods=['POST'])
@login_required
def practice_answer():
    idx = int(request.form['idx'])
    p_ids = session.get('p_ids', [])
    qid = p_ids[idx]
    question = load_questions([qid])[0]
    labels = request.form.getlist('q_%d' % qid)
    ok = bool(labels) and judge_answer(question, labels)

    # 记录练习
    execute("INSERT INTO practice (user_id, question_id, user_answer, is_correct) "
            "VALUES (%s, %s, %s, %s)",
            (session['uid'], qid, ''.join(sorted(labels)) or None, int(ok)))
    # 维护错题本：答错累计，答对标记已掌握
    if ok:
        execute("UPDATE wrong_book SET mastered=1 WHERE user_id=%s AND question_id=%s",
                (session['uid'], qid))
    else:
        execute(
            "INSERT INTO wrong_book (user_id, question_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE wrong_count = wrong_count + 1, "
            "last_wrong_at = CURRENT_TIMESTAMP, mastered = 0",
            (session['uid'], qid))

    session['p_feedback'] = dict(
        ok=ok,
        user_answer=''.join(sorted(labels)) or '未作答',
        correct=''.join(o['label'] for o in question['options'] if o['is_correct']))
    return redirect(url_for('practice_page', idx=idx))


@app.route('/practice/summary')
@login_required
def practice_summary():
    p_ids = session.pop('p_ids', None) or []
    return render_template('practice_summary.html', total=len(p_ids))


# ---------------- wrongbook 模块 ----------------
@app.route('/wrongbook')
@login_required
def wrongbook():
    rows = q(
        "SELECT wb.id, wb.wrong_count, wb.last_wrong_at, q.id AS qid, "
        "q.stem, q.qtype FROM wrong_book wb JOIN question q ON q.id = wb.question_id "
        "WHERE wb.user_id=%s AND wb.mastered=0 ORDER BY wb.last_wrong_at DESC",
        (session['uid'],))
    items = load_questions([r['qid'] for r in rows])
    for r, it in zip(rows, items):
        it['wrong_count'] = r['wrong_count']
        it['last_wrong_at'] = r['last_wrong_at']
    return render_template('wrongbook.html', questions=items)


@app.route('/wrongbook/master/<int:qid>', methods=['POST'])
@login_required
def wrongbook_master(qid):
    execute("UPDATE wrong_book SET mastered=1 WHERE user_id=%s AND question_id=%s",
            (session['uid'], qid))
    flash('已标记为掌握，移出错题本', 'success')
    return redirect(url_for('wrongbook'))


@app.route('/wrongbook/clear', methods=['POST'])
@login_required
def wrongbook_clear():
    """清空当前用户错题本（错题本页与"我的统计"页共用）"""
    n = q("SELECT COUNT(*) c FROM wrong_book WHERE user_id=%s",
          (session['uid'],), one=True)['c']
    execute("DELETE FROM wrong_book WHERE user_id=%s", (session['uid'],))
    flash(f'已清空错题本（{n} 条记录）', 'success')
    if request.form.get('from') == 'stats':
        return redirect(url_for('stats'))
    return redirect(url_for('wrongbook'))


# ---------------- stats 模块 ----------------
@app.route('/stats')
@login_required
def stats():
    me = q("SELECT * FROM v_user_stat WHERE user_id=%s",
           (session['uid'],), one=True)
    my_papers = q("SELECT id, total_count, score, status, started_at, submitted_at "
                  "FROM exam_paper WHERE user_id=%s ORDER BY id DESC LIMIT 10",
                  (session['uid'],))
    # 我的易错题 TOP10（按错误次数）
    my_weak = q(
        "SELECT q.id, q.stem, wb.wrong_count FROM wrong_book wb "
        "JOIN question q ON q.id = wb.question_id "
        "WHERE wb.user_id=%s ORDER BY wb.wrong_count DESC LIMIT 10",
        (session['uid'],))
    return render_template('stats.html', me=me, papers=my_papers, weak=my_weak)


@app.route('/stats/clear/papers', methods=['POST'])
@login_required
def stats_clear_papers():
    """删除当前用户全部考试记录（exam_detail 随外键级联删除）"""
    n = q("SELECT COUNT(*) c FROM exam_paper WHERE user_id=%s",
          (session['uid'],), one=True)['c']
    execute("DELETE FROM exam_paper WHERE user_id=%s", (session['uid'],))
    flash(f'已删除 {n} 份试卷（含全部答题明细）', 'success')
    return redirect(url_for('stats'))


@app.route('/stats/clear/practice', methods=['POST'])
@login_required
def stats_clear_practice():
    """删除当前用户全部顺序练习记录"""
    n = q("SELECT COUNT(*) c FROM practice WHERE user_id=%s",
          (session['uid'],), one=True)['c']
    execute("DELETE FROM practice WHERE user_id=%s", (session['uid'],))
    session.pop('p_ids', None)            # 进行中的练习进度一并清掉
    flash(f'已删除 {n} 条练习记录', 'success')
    return redirect(url_for('stats'))


@app.route('/admin/stats')
@login_required
def admin_stats():
    me = current_user()
    if not me or me['role'] != 'admin':
        flash('仅管理员可访问', 'danger')
        return redirect(url_for('index'))
    overview = dict(
        questions=q("SELECT COUNT(*) c FROM question", one=True)['c'],
        images=q("SELECT COUNT(*) c FROM image", one=True)['c'],
        users=q("SELECT COUNT(*) c FROM `user`", one=True)['c'],
        papers=q("SELECT COUNT(*) c FROM exam_paper WHERE status='finished'",
                 one=True)['c'],
        practices=q("SELECT COUNT(*) c FROM practice", one=True)['c'],
    )
    # 全库每题统计（按答题量倒序，前 100）
    by_question = q(
        "SELECT question_id, LEFT(stem, 36) stem, qtype, attempt_count, "
        "correct_count, correct_rate FROM v_question_stat "
        "ORDER BY attempt_count DESC, question_id LIMIT 100")
    qtype_dist = q("SELECT qtype, COUNT(*) c FROM question GROUP BY qtype")
    # 题库分类分布（加分项①自动归类结果）
    cat_dist = q(
        "SELECT c.name, COUNT(q.id) cnt FROM category c "
        "LEFT JOIN question q ON q.category_id = c.id "
        "GROUP BY c.id, c.name ORDER BY cnt DESC")
    cat_total = sum(r['cnt'] for r in cat_dist) or 1
    # 各用户记录数（供"按用户删除"使用）
    user_rows = q(
        "SELECT u.id, u.username, u.real_name, "
        "(SELECT COUNT(*) FROM exam_paper ep WHERE ep.user_id=u.id) papers, "
        "(SELECT COUNT(*) FROM practice p WHERE p.user_id=u.id) practices, "
        "(SELECT COUNT(*) FROM wrong_book wb WHERE wb.user_id=u.id) wrongs "
        "FROM `user` u ORDER BY u.id")
    return render_template('admin_stats.html', overview=overview,
                           by_question=by_question, qtype_dist=qtype_dist,
                           cat_dist=cat_dist, cat_total=cat_total,
                           user_rows=user_rows)


def _admin_or_back():
    """管理员校验，非管理员返回 (None, 重定向响应)"""
    me = current_user()
    if not me or me['role'] != 'admin':
        flash('仅管理员可操作', 'danger')
        return None, redirect(url_for('index'))
    return me, None


@app.route('/admin/stats/clear/all', methods=['POST'])
@login_required
def admin_clear_all():
    """清空全体用户的考试/练习/错题记录"""
    me, back = _admin_or_back()
    if back:
        return back
    n_paper = q("SELECT COUNT(*) c FROM exam_paper", one=True)['c']
    n_prac = q("SELECT COUNT(*) c FROM practice", one=True)['c']
    n_wrong = q("SELECT COUNT(*) c FROM wrong_book", one=True)['c']
    execute("DELETE FROM exam_paper")     # exam_detail 随外键级联删除
    execute("DELETE FROM practice")
    execute("DELETE FROM wrong_book")
    flash(f'已清空全体用户记录：试卷 {n_paper} 份（含答题明细）、'
          f'练习 {n_prac} 条、错题 {n_wrong} 条', 'success')
    return redirect(url_for('admin_stats'))


@app.route('/admin/stats/clear/user/<int:uid>', methods=['POST'])
@login_required
def admin_clear_user(uid):
    """清除指定用户的全部考试/练习/错题记录"""
    me, back = _admin_or_back()
    if back:
        return back
    u = q("SELECT username FROM `user` WHERE id=%s", (uid,), one=True)
    if not u:
        flash('用户不存在', 'danger')
        return redirect(url_for('admin_stats'))
    n_paper = q("SELECT COUNT(*) c FROM exam_paper WHERE user_id=%s",
                (uid,), one=True)['c']
    n_prac = q("SELECT COUNT(*) c FROM practice WHERE user_id=%s",
               (uid,), one=True)['c']
    n_wrong = q("SELECT COUNT(*) c FROM wrong_book WHERE user_id=%s",
                (uid,), one=True)['c']
    execute("DELETE FROM exam_paper WHERE user_id=%s", (uid,))
    execute("DELETE FROM practice WHERE user_id=%s", (uid,))
    execute("DELETE FROM wrong_book WHERE user_id=%s", (uid,))
    flash(f"已清除用户 {u['username']} 的记录：试卷 {n_paper} 份、"
          f"练习 {n_prac} 条、错题 {n_wrong} 条", 'success')
    return redirect(url_for('admin_stats'))


# ---------------- image 服务 ----------------
@app.route('/image/<int:iid>')
def image(iid):
    row = q("SELECT mime_type, data FROM image WHERE id=%s", (iid,), one=True)
    if not row:
        abort(404)
    return Response(row['data'], mimetype=row['mime_type'])


# ---------------- crawler 模块（加分项④：网上爬题增量导入） ----------------
import crawler as crawl_mod


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        me = current_user()
        if not me or me['role'] != 'admin':
            flash('仅管理员可访问', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return wrapper


@app.route('/admin/crawl')
@login_required
@admin_required
def crawl_page():
    batches = q("SELECT * FROM import_batch ORDER BY id DESC LIMIT 20")
    year_dist = q("SELECT year_version, COUNT(*) cnt FROM question "
                  "GROUP BY year_version ORDER BY year_version")
    return render_template('crawl.html', batches=batches,
                           sources=crawl_mod.SOURCES, year_dist=year_dist)


@app.route('/admin/crawl/start', methods=['POST'])
@login_required
@admin_required
def crawl_start():
    mode = request.form['mode']
    year = request.form.get('year_version', '').strip() or None
    if mode == 'public':
        key = request.form['source']
        if key not in crawl_mod.SOURCES:
            flash('未知公开源', 'danger')
            return redirect(url_for('crawl_page'))
        year = year or crawl_mod.SOURCES[key]['default_year']
        bid = crawl_mod.start_batch('public', source_key=key, year_version=year)
    else:
        url = request.form.get('url', '').strip()
        if not url.startswith(('http://', 'https://')):
            flash('请输入合法的网址（http/https）', 'danger')
            return redirect(url_for('crawl_page'))
        year = year or '2025'
        bid = crawl_mod.start_batch('url', url=url, year_version=year)
    flash(f'采集任务 #{bid} 已启动，页面将自动刷新进度', 'success')
    return redirect(url_for('crawl_page', watch=bid))


@app.route('/admin/crawl/status/<int:bid>')
@login_required
@admin_required
def crawl_status(bid):
    row = q("SELECT id, status, fetched, imported, duplicates, message "
            "FROM import_batch WHERE id=%s", (bid,), one=True)
    if not row:
        return {'error': 'not found'}, 404
    return dict(row)


# ---------------- llm 模块（加分项③⑥：AI 答案验证 + token 成本统计） ----------------
import llm as llm_mod

VERDICT_NAME = {'correct': '与标准答案一致', 'wrong': '与标准答案不一致', 'uncertain': '无法判定'}


@app.route('/admin/ai')
@login_required
@admin_required
def ai_page():
    cfg = llm_mod.get_config()
    stats, verdicts, pending = llm_mod.verify_stats()
    verdict_filter = request.args.get('verdict')
    results = llm_mod.recent_results(50, verdict_filter)
    return render_template('ai.html', cfg=cfg, providers=llm_mod.PROVIDERS,
                           stats=stats, verdicts=verdicts, pending=pending,
                           results=results, verdict_filter=verdict_filter,
                           VERDICT_NAME=VERDICT_NAME)


@app.route('/admin/ai/save', methods=['POST'])
@login_required
@admin_required
def ai_save():
    provider = request.form['provider']
    base_url = request.form['base_url'].strip()
    api_key = request.form['api_key'].strip()
    model = request.form['model'].strip()
    if not (base_url and api_key and model):
        flash('接口地址、API Key、模型名均不能为空', 'danger')
    else:
        llm_mod.save_config(provider, base_url, api_key, model)
        flash('API 配置已保存', 'success')
    return redirect(url_for('ai_page'))


@app.route('/admin/ai/test', methods=['POST'])
@login_required
@admin_required
def ai_test():
    ok, msg = llm_mod.test_connection()
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('ai_page'))


@app.route('/admin/ai/verify/start', methods=['POST'])
@login_required
@admin_required
def ai_verify_start():
    scope = request.form.get('scope', 'all')
    try:
        limit = max(1, min(5000, int(request.form.get('limit', 50))))
    except ValueError:
        limit = 50
    bid, err = llm_mod.start_verify(scope, limit)
    if err:
        flash(err, 'danger')
        return redirect(url_for('ai_page'))
    flash(f'验证任务 #{bid} 已启动', 'success')
    return redirect(url_for('ai_page', watch=bid))


@app.route('/admin/ai/verify/status/<int:bid>')
@login_required
@admin_required
def ai_verify_status(bid):
    row = q("SELECT * FROM verify_batch WHERE id=%s", (bid,), one=True)
    if not row:
        return {'error': 'not found'}, 404
    return dict(row)


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
