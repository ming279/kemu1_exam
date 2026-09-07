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
                   render_template, flash, Response, abort, jsonify)
from flask_socketio import SocketIO, join_room, emit, leave_room

app = Flask(__name__)
app.secret_key = 'kemu1-exam-2026-sec'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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
    rows = q(f"SELECT id, stem, qtype, explanation FROM question WHERE id IN ({ph})", qids)
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
    # 学生：查询待完成任务（published 且未完成）
    my_tasks = []
    me = current_user()
    if me and me['role'] == 'student':
        my_tasks = q(
            "SELECT t.id, t.title, t.judge_count, t.single_count, "
            "t.time_limit_sec, t.mode, t.purpose, t.created_at, "
            "tr.status AS my_status "
            "FROM task t LEFT JOIN task_record tr "
            "ON tr.task_id=t.id AND tr.uid=%s "
            "WHERE t.status='published' "
            "AND (tr.status IS NULL OR tr.status='in_progress') "
            "ORDER BY t.id DESC",
            (session['uid'],))
    return render_template('index.html', stats=stats, my_tasks=my_tasks)


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

    # 任务模式：加载 task 信息和 task_record 状态
    task = None
    task_record = None
    remain_sec = None        # 考试模式倒计时剩余秒数
    is_practice = False      # 是否为练习模式任务
    if paper.get('task_id'):
        task = q("SELECT * FROM task WHERE id=%s", (paper['task_id'],), one=True)
        task_record = q("SELECT * FROM task_record WHERE paper_id=%s",
                        (pid,), one=True)
        if task and task['mode'] == 'practice':
            is_practice = True
        # 考试模式倒计时：剩余 = time_limit_sec - 已用时
        if task and task['time_limit_sec'] and task['mode'] == 'exam':
            elapsed = 0
            if task_record:
                # 已用时 = now - start_time + 之前累计的 elapsed_sec
                # （考试模式不暂停，elapsed_sec 通常为0）
                elapsed_row = q(
                    "SELECT TIMESTAMPDIFF(SECOND, start_time, NOW()) AS e "
                    "FROM task_record WHERE id=%s", (task_record['id'],), one=True)
                elapsed = elapsed_row['e'] if elapsed_row else 0
            remain_sec = task['time_limit_sec'] - (elapsed or 0)
            if remain_sec < 0:
                remain_sec = 0

    return render_template('exam.html', pid=pid, judges=judges, singles=singles,
                           task=task, task_record=task_record,
                           remain_sec=remain_sec, is_practice=is_practice)


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
    final_score = round(score * 100.0 / total, 2)
    execute("UPDATE exam_paper SET score=%s, status='finished', "
            "submitted_at=CURRENT_TIMESTAMP WHERE id=%s",
            (final_score, pid))
    # 若是任务考试，更新 task_record 状态为 completed
    if paper.get('task_id'):
        execute("UPDATE task_record SET status='completed', "
                "submit_time=CURRENT_TIMESTAMP, score=%s "
                "WHERE paper_id=%s", (final_score, pid))
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


# ---------------- 功能⑥：题目管理（答案解析）----------------
@app.route('/admin/questions')
@login_required
def admin_questions():
    me, err = _admin_or_back()
    if err:
        return err
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 20
    search = request.args.get('q', '').strip()
    where = "WHERE 1=1"
    params = []
    if search:
        where += " AND stem LIKE %s"
        params.append(f'%{search}%')
    total = q(f"SELECT COUNT(*) c FROM question {where}", params, one=True)['c']
    pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    rows = q(f"SELECT id, LEFT(stem, 50) stem_short, qtype, explanation "
             f"FROM question {where} ORDER BY id LIMIT %s OFFSET %s",
             params + [per_page, offset])
    return render_template('admin_questions.html', rows=rows, page=page,
                           pages=pages, search=search, total=total)


@app.route('/admin/questions/<int:qid>/explanation', methods=['POST'])
@login_required
def admin_save_explanation(qid):
    me, err = _admin_or_back()
    if err:
        return err
    text = request.form.get('explanation', '').strip()
    execute("UPDATE question SET explanation=%s WHERE id=%s", (text or None, qid))
    return jsonify(ok=True)


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


# ---------------- 功能①：教师发布任务 ----------------
def _generate_questions_for_task(judge_count, single_count):
    """教师发布时调用：分别抽判断题和单选题，合并存 question_ids
    判断题在前，单选题在后（打乱时各区内打乱，不混在一起）"""
    judges = [r['id'] for r in
              q(f"SELECT id FROM question WHERE qtype='judge' "
                f"ORDER BY RAND() LIMIT {int(judge_count)}")]
    singles = [r['id'] for r in
               q(f"SELECT id FROM question WHERE qtype='single' "
                 f"ORDER BY RAND() LIMIT {int(single_count)}")]
    # 判断题始终在前，单选题在后
    return judges + singles


def _get_questions_for_student(task, uid):
    """学生进入任务时调用：读 question_ids，按 purpose 决定顺序
    - purpose=exam 且 shuffle_order=True：用 uid 做种子打乱（同生一致，异生不同）
    - purpose=review 或不打乱：保持原序，全班一致便于讲评
    - 判断题始终在前，单选题在后（不混合）
    """
    if not task['question_ids']:
        return []
    raw_ids = [int(x) for x in task['question_ids'].split(',') if x.strip()]
    # 加载题型信息用于分区
    ph = ','.join(['%s'] * len(raw_ids))
    type_rows = q(f"SELECT id, qtype FROM question WHERE id IN ({ph})", raw_ids)
    type_map = {r['id']: r['qtype'] for r in type_rows}
    judges = [i for i in raw_ids if type_map.get(i) == 'judge']
    singles = [i for i in raw_ids if type_map.get(i) == 'single']

    if task['purpose'] == 'exam' and task['shuffle_order']:
        # 用 uid 做种子打乱，同一学生顺序一致，不同学生不同
        rng = random.Random(uid)
        rng.shuffle(judges)
        rng.shuffle(singles)
    # 判断题始终在前，单选题在后
    return judges + singles


@app.route('/admin/tasks')
@login_required
def admin_tasks():
    """任务管理页：已发布任务列表 + 新建表单"""
    me, err = _admin_or_back()
    if err:
        return err
    rows = q(
        "SELECT t.id, t.title, t.judge_count, t.single_count, "
        "t.time_limit_sec, t.mode, t.purpose, t.shuffle_order, "
        "t.status, t.created_at, t.closed_at, "
        "(SELECT COUNT(*) FROM task_record tr WHERE tr.task_id=t.id) AS joined, "
        "(SELECT COUNT(*) FROM task_record tr WHERE tr.task_id=t.id "
        "  AND tr.status='completed') AS finished "
        "FROM task t ORDER BY t.id DESC")
    return render_template('admin_tasks.html', rows=rows)


@app.route('/admin/tasks/create', methods=['POST'])
@login_required
def admin_tasks_create():
    """创建任务：预生成固定题目列表，所有学生题目内容相同"""
    me, err = _admin_or_back()
    if err:
        return err
    title = request.form.get('title', '').strip()
    judge_count = request.form.get('judge_count', 40, type=int)
    single_count = request.form.get('single_count', 60, type=int)
    minutes = request.form.get('time_limit', '').strip()
    mode = request.form.get('mode', 'exam')
    purpose = request.form.get('purpose', 'exam')
    shuffle_order = request.form.get('shuffle_order') == 'on'

    if not title:
        flash('任务标题不能为空', 'danger')
        return redirect(url_for('admin_tasks'))
    if judge_count < 0 or single_count < 0 or (judge_count + single_count) == 0:
        flash('题目数量必须大于 0', 'danger')
        return redirect(url_for('admin_tasks'))

    # 校验题库存量
    avail_judge = q("SELECT COUNT(*) c FROM question WHERE qtype='judge'",
                    one=True)['c']
    avail_single = q("SELECT COUNT(*) c FROM question WHERE qtype='single'",
                     one=True)['c']
    if judge_count > avail_judge:
        flash(f'判断题库存不足（现有 {avail_judge} 题，需要 {judge_count} 题）', 'danger')
        return redirect(url_for('admin_tasks'))
    if single_count > avail_single:
        flash(f'单选题库存不足（现有 {avail_single} 题，需要 {single_count} 题）', 'danger')
        return redirect(url_for('admin_tasks'))

    # 时限：空=不限时，否则分钟×60
    time_limit_sec = None
    if minutes:
        try:
            mins = int(minutes)
            time_limit_sec = mins * 60 if mins > 0 else None
        except ValueError:
            flash('时限必须为整数分钟', 'danger')
            return redirect(url_for('admin_tasks'))

    # 预生成固定题目列表（所有学生同一套题）
    qids = _generate_questions_for_task(judge_count, single_count)
    if not qids:
        flash('组卷失败，请检查题库', 'danger')
        return redirect(url_for('admin_tasks'))

    execute(
        "INSERT INTO task (title, creator_uid, judge_count, single_count, "
        "time_limit_sec, mode, purpose, question_ids, shuffle_order, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'published')",
        (title, session['uid'], judge_count, single_count, time_limit_sec,
         mode, purpose, ','.join(map(str, qids)), int(shuffle_order)))
    flash(f'任务「{title}」已发布（判断 {judge_count} + 单选 {single_count} = '
          f'{judge_count + single_count} 题）', 'success')
    return redirect(url_for('admin_tasks'))


@app.route('/admin/tasks/<int:tid>/close', methods=['POST'])
@login_required
def admin_tasks_close(tid):
    """关闭任务：不再接受学生参加"""
    me, err = _admin_or_back()
    if err:
        return err
    execute("UPDATE task SET status='closed', closed_at=CURRENT_TIMESTAMP "
            "WHERE id=%s", (tid,))
    flash(f'任务 #{tid} 已关闭', 'success')
    return redirect(url_for('admin_tasks'))


@app.route('/admin/tasks/<int:tid>/reopen', methods=['POST'])
@login_required
def admin_tasks_reopen(tid):
    """重新开放已关闭的任务"""
    me, err = _admin_or_back()
    if err:
        return err
    execute("UPDATE task SET status='published', closed_at=NULL WHERE id=%s", (tid,))
    flash(f'任务 #{tid} 已重新开放', 'success')
    return redirect(url_for('admin_tasks'))


@app.route('/admin/tasks/<int:tid>/delete', methods=['POST'])
@login_required
def admin_tasks_delete(tid):
    """删除任务：先删 task_record（如有），再删 task"""
    me, err = _admin_or_back()
    if err:
        return err
    # 关联的 task_record 先删（exam_paper 由 task_record.paper_id 关联，不在此级联）
    n_rec = q("SELECT COUNT(*) c FROM task_record WHERE task_id=%s",
              (tid,), one=True)['c']
    execute("DELETE FROM task_record WHERE task_id=%s", (tid,))
    execute("DELETE FROM task WHERE id=%s", (tid,))
    flash(f'已删除任务 #{tid}（含 {n_rec} 条参加记录）', 'success')
    return redirect(url_for('admin_tasks'))


@app.route('/admin/tasks/<int:tid>/edit', methods=['POST'])
@login_required
def admin_tasks_edit(tid):
    """修改任务标题/时限（题目不可改，已确认）"""
    me, err = _admin_or_back()
    if err:
        return err
    title = request.form.get('title', '').strip()
    minutes = request.form.get('time_limit', '').strip()
    if not title:
        flash('任务标题不能为空', 'danger')
        return redirect(url_for('admin_tasks'))

    time_limit_sec = None
    if minutes:
        try:
            mins = int(minutes)
            time_limit_sec = mins * 60 if mins > 0 else None
        except ValueError:
            flash('时限必须为整数分钟', 'danger')
            return redirect(url_for('admin_tasks'))

    execute("UPDATE task SET title=%s, time_limit_sec=%s WHERE id=%s",
            (title, time_limit_sec, tid))
    flash(f'任务 #{tid} 已更新', 'success')
    return redirect(url_for('admin_tasks'))


# ---------------- 功能②：学生参加任务（骨架，步骤4完善计时） ----------------
@app.route('/task/<int:tid>/start', methods=['POST'])
@login_required
def task_start(tid):
    """学生开始/继续任务：创建 task_record + exam_paper，复用考试作答页"""
    task = q("SELECT * FROM task WHERE id=%s", (tid,), one=True)
    if not task or task['status'] != 'published':
        flash('任务不存在或已关闭', 'danger')
        return redirect(url_for('index'))

    uid = session['uid']
    # 查询是否已有记录
    rec = q("SELECT * FROM task_record WHERE task_id=%s AND uid=%s",
            (tid, uid), one=True)
    if rec and rec['status'] == 'completed':
        flash('该任务已完成', 'info')
        return redirect(url_for('exam_result', pid=rec['paper_id']))
    if rec and rec['paper_id']:
        # 继续进行中的任务
        return redirect(url_for('exam_page', pid=rec['paper_id']))

    # 首次开始：获取题目顺序（按 purpose 决定是否打乱）
    qids = _get_questions_for_student(task, uid)
    if not qids:
        flash('任务题目加载失败', 'danger')
        return redirect(url_for('index'))

    # 创建试卷（关联 task_id）
    pid = execute(
        "INSERT INTO exam_paper (user_id, task_id, total_count) VALUES (%s, %s, %s)",
        (uid, tid, len(qids)))
    for seq, qid in enumerate(qids, 1):
        execute("INSERT INTO exam_detail (paper_id, question_id, seq_no) "
                "VALUES (%s, %s, %s)", (pid, qid, seq))

    # 创建/更新任务记录
    if rec:
        execute("UPDATE task_record SET paper_id=%s, status='in_progress', "
                "start_time=CURRENT_TIMESTAMP WHERE id=%s", (pid, rec['id']))
    else:
        execute("INSERT INTO task_record (task_id, uid, paper_id, status, "
                "start_time) VALUES (%s, %s, %s, 'in_progress', CURRENT_TIMESTAMP)",
                (tid, uid, pid))

    return redirect(url_for('exam_page', pid=pid))


@app.route('/task/<int:tid>/pause', methods=['POST'])
@login_required
def task_pause(tid):
    """练习模式暂停：记录 pause_time，累加已用时"""
    rec = q("SELECT tr.*, t.mode FROM task_record tr "
            "JOIN task t ON t.id=tr.task_id "
            "WHERE tr.task_id=%s AND tr.uid=%s",
            (tid, session['uid']), one=True)
    if not rec:
        return jsonify(ok=False, msg='记录不存在'), 404
    if rec['mode'] != 'practice':
        return jsonify(ok=False, msg='仅练习模式可暂停'), 400
    if rec['status'] != 'in_progress' or rec['paused']:
        return jsonify(ok=False, msg='当前状态不可暂停'), 400

    # 累加从 start_time（或上次 resume）到现在的秒数
    elapsed_row = q(
        "SELECT TIMESTAMPDIFF(SECOND, "
        "COALESCE(pause_time, start_time), NOW()) AS e "
        "FROM task_record WHERE id=%s", (rec['id'],), one=True)
    add = elapsed_row['e'] if elapsed_row else 0
    execute("UPDATE task_record SET paused=1, pause_time=NOW(), "
            "elapsed_sec=elapsed_sec+%s WHERE id=%s",
            (add, rec['id']))
    return jsonify(ok=True, elapsed=rec['elapsed_sec'] + add)


@app.route('/task/<int:tid>/resume', methods=['POST'])
@login_required
def task_resume(tid):
    """练习模式继续：清除 pause_time，从当前时刻重新计时"""
    rec = q("SELECT tr.*, t.mode FROM task_record tr "
            "JOIN task t ON t.id=tr.task_id "
            "WHERE tr.task_id=%s AND tr.uid=%s",
            (tid, session['uid']), one=True)
    if not rec:
        return jsonify(ok=False, msg='记录不存在'), 404
    if rec['mode'] != 'practice':
        return jsonify(ok=False, msg='仅练习模式可继续'), 400
    if not rec['paused']:
        return jsonify(ok=False, msg='当前未暂停'), 400

    # pause_time 设为 NOW()，表示从这里继续计时（下次暂停时计算从这里到暂停的差）
    execute("UPDATE task_record SET paused=0, pause_time=NOW() WHERE id=%s",
            (rec['id'],))
    return jsonify(ok=True, elapsed=rec['elapsed_sec'])


@app.route('/task/<int:tid>/status', methods=['GET'])
@login_required
def task_status(tid):
    """前端轮询：返回当前计时状态（练习模式已用时，考试模式剩余秒）"""
    rec = q("SELECT tr.*, t.mode, t.time_limit_sec FROM task_record tr "
            "JOIN task t ON t.id=tr.task_id "
            "WHERE tr.task_id=%s AND tr.uid=%s",
            (tid, session['uid']), one=True)
    if not rec:
        return jsonify(ok=False), 404
    if rec['mode'] == 'practice':
        # 练习模式：返回累计已用时 + 是否暂停中 + 当前段（从 pause_time 或 start_time 到 now）
        now_seg = 0
        if rec['status'] == 'in_progress' and not rec['paused']:
            row = q(
                "SELECT TIMESTAMPDIFF(SECOND, "
                "COALESCE(pause_time, start_time), NOW()) AS e "
                "FROM task_record WHERE id=%s", (rec['id'],), one=True)
            now_seg = row['e'] if row and row['e'] is not None else 0
        return jsonify(ok=True, mode='practice',
                       elapsed=(rec['elapsed_sec'] or 0) + now_seg,
                       paused=bool(rec['paused']))
    else:
        # 考试模式：返回剩余秒数
        if rec['time_limit_sec'] and rec['status'] == 'in_progress':
            row = q(
                "SELECT TIMESTAMPDIFF(SECOND, start_time, NOW()) AS e "
                "FROM task_record WHERE id=%s", (rec['id'],), one=True)
            elapsed = row['e'] if row else 0
            remain = max(0, rec['time_limit_sec'] - elapsed)
            return jsonify(ok=True, mode='exam', remain=remain)
        return jsonify(ok=True, mode='exam', remain=0)


# ---------------- 功能④：错题排行榜 ----------------
@app.route('/admin/wrong_rank')
@login_required
def admin_wrong_rank():
    """错题排行榜：默认全局，可按任务筛选"""
    me, err = _admin_or_back()
    if err:
        return err
    # 任务下拉选项
    tasks = q("SELECT id, title FROM task WHERE status IN ('published','closed') "
              "ORDER BY id DESC")
    task_filter = request.args.get('task', '', type=str)

    # 构建查询：全局或按任务
    if task_filter:
        # 按任务筛选：通过 task_record.task_id 关联 paper_id
        rows = q(
            "SELECT q.id, LEFT(q.stem, 60) stem_short, q.qtype, q.explanation, "
            "COUNT(*) AS wrong_count, "
            "COUNT(DISTINCT ed.paper_id) AS total_attempts, "
            "ROUND(COUNT(*) * 100.0 / GREATEST(COUNT(DISTINCT ed.paper_id),1), 1) AS wrong_rate, "
            "GROUP_CONCAT(DISTINCT ed.user_answer) AS common_wrong "
            "FROM exam_detail ed "
            "JOIN question q ON q.id = ed.question_id "
            "JOIN task_record tr ON tr.paper_id = ed.paper_id "
            "WHERE tr.task_id = %s AND ed.is_correct = 0 "
            "GROUP BY q.id ORDER BY wrong_count DESC LIMIT 50",
            (task_filter,))
    else:
        # 全局：所有 exam_detail 汇总
        rows = q(
            "SELECT q.id, LEFT(q.stem, 60) stem_short, q.qtype, q.explanation, "
            "COUNT(*) AS wrong_count, "
            "COUNT(DISTINCT ed.paper_id) AS total_attempts, "
            "ROUND(COUNT(*) * 100.0 / GREATEST(COUNT(DISTINCT ed.paper_id),1), 1) AS wrong_rate, "
            "GROUP_CONCAT(DISTINCT ed.user_answer) AS common_wrong "
            "FROM exam_detail ed "
            "JOIN question q ON q.id = ed.question_id "
            "WHERE ed.is_correct = 0 "
            "GROUP BY q.id ORDER BY wrong_count DESC LIMIT 50")

    return render_template('admin_wrong_rank.html', rows=rows, tasks=tasks,
                           task_filter=task_filter)


# ---------------- 功能③：答题数据导出（5场景 + CSV/Excel）----------------
import io
import csv
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

EXPORT_SCENARIOS = {
    'review': '课堂讲评',
    'scores': '成绩公告',
    'tutor': '个别辅导',
    'reflect': '教学反思',
    'archive': '全量存档',
}


def _export_csv(headers, rows, filename):
    """CSV 导出（带 BOM，Excel 友好）"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([str(c) if c is not None else '' for c in row])
    from urllib.parse import quote
    ascii_name = 'export_' + str(int(__import__('time').time())) + '.csv'
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition':
                 f"attachment; filename=\"{ascii_name}\"; "
                 f"filename*=UTF-8''{quote(filename + '.csv')}"})


def _export_excel(headers, rows, filename, wrong_col_idx=None, header_bg='4472C4'):
    """Excel 导出（表头加粗白字、错题行标红、列宽自适应）"""
    wb = Workbook()
    ws = wb.active
    ws.title = '答题数据'
    hdr_font = Font(bold=True, color='FFFFFF')
    hdr_fill = PatternFill('solid', fgColor=header_bg)
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal='center')
    wrong_fill = PatternFill('solid', fgColor='FEE2E2')
    for r, row in enumerate(rows, 2):
        for c, val in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=val if val is not None else '')
            if wrong_col_idx and c == wrong_col_idx and str(val) in ('错', '×', '0', 'False'):
                for cc in range(1, len(headers) + 1):
                    ws.cell(row=r, column=cc).fill = wrong_fill
    for c in range(1, len(headers) + 1):
        max_len = len(str(headers[c - 1]))
        for r in range(2, min(len(rows) + 2, 50)):
            v = ws.cell(row=r, column=c).value
            if v is not None:
                max_len = max(max_len, len(str(v)[:40]))
        ws.column_dimensions[get_column_letter(c)].width = min(max_len + 4, 60)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from urllib.parse import quote
    ascii_name = 'export_' + str(int(__import__('time').time())) + '.xlsx'
    return Response(
        buf.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition':
                 f"attachment; filename=\"{ascii_name}\"; "
                 f"filename*=UTF-8''{quote(filename + '.xlsx')}"})


def _build_export_data(scenario, task_id, student_id, fmt):
    """根据场景构建导出数据，返回 (headers, rows, filename)"""

    if scenario == 'review':
        # 1. 课堂讲评：按题号，含错误率+错的人名
        if task_id:
            rows_data = q(
                "SELECT q.id, LEFT(q.stem,80) AS stem, q.qtype, "
                "GROUP_CONCAT(DISTINCT o.label) AS correct_answer, "
                "COUNT(*) AS wrong_count, "
                "COUNT(DISTINCT ep.id) AS total_attempts, "
                "ROUND(COUNT(*)*100.0/GREATEST(COUNT(DISTINCT ep.id),1),1) AS wrong_rate, "
                "GROUP_CONCAT(DISTINCT ed.user_answer) AS common_wrong, "
                "GROUP_CONCAT(DISTINCT u.real_name) AS wrong_students "
                "FROM exam_detail ed "
                "JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN task_record tr ON tr.paper_id=ep.id "
                "JOIN question q ON q.id=ed.question_id "
                "LEFT JOIN `user` u ON u.id=ep.user_id "
                "LEFT JOIN `option` o ON o.question_id=q.id AND o.is_correct=1 "
                "WHERE tr.task_id=%s AND ed.is_correct=0 "
                "GROUP BY q.id ORDER BY q.id", (task_id,))
        else:
            rows_data = q(
                "SELECT q.id, LEFT(q.stem,80) AS stem, q.qtype, "
                "GROUP_CONCAT(DISTINCT o.label) AS correct_answer, "
                "COUNT(*) AS wrong_count, "
                "COUNT(DISTINCT ep.id) AS total_attempts, "
                "ROUND(COUNT(*)*100.0/GREATEST(COUNT(DISTINCT ep.id),1),1) AS wrong_rate, "
                "GROUP_CONCAT(DISTINCT ed.user_answer) AS common_wrong, "
                "GROUP_CONCAT(DISTINCT u.real_name) AS wrong_students "
                "FROM exam_detail ed "
                "JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN question q ON q.id=ed.question_id "
                "LEFT JOIN `user` u ON u.id=ep.user_id "
                "LEFT JOIN `option` o ON o.question_id=q.id AND o.is_correct=1 "
                "WHERE ed.is_correct=0 "
                "GROUP BY q.id ORDER BY q.id")
        headers = ['题号', '题干', '题型', '正确答案', '错次', '错误率(%)', '常见错误答案', '错的学生']
        rows = [(r['id'], r['stem'], r['qtype'], r['correct_answer'],
                 r['wrong_count'], r['wrong_rate'], r['common_wrong'],
                 r['wrong_students']) for r in rows_data]
        return headers, rows, '课堂讲评'

    elif scenario == 'scores':
        # 2. 成绩公告：排名、姓名、分数、通过/未通过、用时
        if task_id:
            rows_data = q(
                "SELECT u.id, u.username, u.real_name, ep.score, ep.total_count, "
                "tr.elapsed_sec, CASE WHEN ep.score>=90 THEN '通过' ELSE '未通过' END AS pass, "
                "ep.submitted_at "
                "FROM exam_paper ep JOIN `user` u ON u.id=ep.user_id "
                "JOIN task_record tr ON tr.paper_id=ep.id "
                "WHERE ep.status='finished' AND tr.task_id=%s "
                "ORDER BY ep.score DESC", (task_id,))
        else:
            rows_data = q(
                "SELECT u.id, u.username, u.real_name, ep.score, ep.total_count, "
                "tr.elapsed_sec, CASE WHEN ep.score>=90 THEN '通过' ELSE '未通过' END AS pass, "
                "ep.submitted_at "
                "FROM exam_paper ep JOIN `user` u ON u.id=ep.user_id "
                "LEFT JOIN task_record tr ON tr.paper_id=ep.id "
                "WHERE ep.status='finished' ORDER BY ep.score DESC")
        headers = ['排名', '学号', '账号', '姓名', '分数', '题数', '通过状态', '用时(秒)', '提交时间']
        rows = [(i+1, r['id'], r['username'], r['real_name'] or '', r['score'],
                 r['total_count'], r['pass'], r['elapsed_sec'] or '',
                 str(r['submitted_at']) if r['submitted_at'] else '')
                for i, r in enumerate(rows_data)]
        return headers, rows, '成绩公告'

    elif scenario == 'tutor':
        # 3. 个别辅导：单学生全部答题明细
        sid = int(student_id) if student_id else 0
        if task_id:
            rows_data = q(
                "SELECT q.id, LEFT(q.stem,80) AS stem, q.qtype, ed.user_answer, "
                "(SELECT GROUP_CONCAT(o.label SEPARATOR '') FROM `option` o "
                "WHERE o.question_id=q.id AND o.is_correct=1) AS correct_answer, "
                "ed.is_correct, tr.elapsed_sec "
                "FROM exam_detail ed JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN question q ON q.id=ed.question_id "
                "JOIN task_record tr ON tr.paper_id=ep.id "
                "WHERE ep.user_id=%s AND tr.task_id=%s ORDER BY q.id", (sid, task_id))
        else:
            rows_data = q(
                "SELECT q.id, LEFT(q.stem,80) AS stem, q.qtype, ed.user_answer, "
                "(SELECT GROUP_CONCAT(o.label SEPARATOR '') FROM `option` o "
                "WHERE o.question_id=q.id AND o.is_correct=1) AS correct_answer, "
                "ed.is_correct, tr.elapsed_sec "
                "FROM exam_detail ed JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN question q ON q.id=ed.question_id "
                "LEFT JOIN task_record tr ON tr.paper_id=ep.id "
                "WHERE ep.user_id=%s ORDER BY q.id", (sid,))
        headers = ['题号', '题干', '题型', '学生答案', '正确答案', '对错', '用时(秒)']
        rows = [(r['id'], r['stem'], r['qtype'], r['user_answer'] or '未答',
                 r['correct_answer'], '对' if r['is_correct'] else '错',
                 r['elapsed_sec'] or '') for r in rows_data]
        return headers, rows, f'学生{sid}_答题明细'

    elif scenario == 'reflect':
        # 4. 教学反思：按分类聚合错误率
        if task_id:
            rows_data = q(
                "SELECT c.name AS cat_name, COUNT(DISTINCT q.id) AS q_count, "
                "COUNT(ed.id) AS wrong_count, COUNT(DISTINCT ep.id) AS total_attempts, "
                "ROUND(COUNT(ed.id)*100.0/GREATEST(COUNT(DISTINCT ep.id),1),1) AS avg_wrong_rate "
                "FROM question q LEFT JOIN category c ON c.id=q.category_id "
                "LEFT JOIN exam_detail ed ON ed.question_id=q.id AND ed.is_correct=0 "
                "LEFT JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN task_record tr ON tr.paper_id=ep.id "
                "WHERE tr.task_id=%s "
                "GROUP BY c.id, c.name ORDER BY avg_wrong_rate DESC", (task_id,))
        else:
            rows_data = q(
                "SELECT c.name AS cat_name, COUNT(DISTINCT q.id) AS q_count, "
                "COUNT(ed.id) AS wrong_count, COUNT(DISTINCT ep.id) AS total_attempts, "
                "ROUND(COUNT(ed.id)*100.0/GREATEST(COUNT(DISTINCT ep.id),1),1) AS avg_wrong_rate "
                "FROM question q LEFT JOIN category c ON c.id=q.category_id "
                "LEFT JOIN exam_detail ed ON ed.question_id=q.id AND ed.is_correct=0 "
                "LEFT JOIN exam_paper ep ON ep.id=ed.paper_id "
                "GROUP BY c.id, c.name ORDER BY avg_wrong_rate DESC")
        headers = ['分类名', '该分类题数', '错次', '总答题人次', '平均错误率(%)']
        rows = [(r['cat_name'] or '未分类', r['q_count'], r['wrong_count'],
                 r['total_attempts'], r['avg_wrong_rate']) for r in rows_data]
        return headers, rows, '教学反思'

    else:  # archive
        # 5. 全量存档：所有原始数据
        if task_id:
            rows_data = q(
                "SELECT ep.submitted_at, u.username, u.real_name, t.title, "
                "q.id, LEFT(q.stem,60) stem, q.qtype, ed.user_answer, "
                "(SELECT GROUP_CONCAT(o.label SEPARATOR '') FROM `option` o "
                "WHERE o.question_id=q.id AND o.is_correct=1) AS correct_answer, "
                "ed.is_correct, tr.elapsed_sec "
                "FROM exam_detail ed JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN `user` u ON u.id=ep.user_id JOIN question q ON q.id=ed.question_id "
                "LEFT JOIN task t ON t.id=ep.task_id "
                "JOIN task_record tr ON tr.paper_id=ep.id "
                "WHERE tr.task_id=%s ORDER BY ep.id, ed.seq_no", (task_id,))
        else:
            rows_data = q(
                "SELECT ep.submitted_at, u.username, u.real_name, t.title, "
                "q.id, LEFT(q.stem,60) stem, q.qtype, ed.user_answer, "
                "(SELECT GROUP_CONCAT(o.label SEPARATOR '') FROM `option` o "
                "WHERE o.question_id=q.id AND o.is_correct=1) AS correct_answer, "
                "ed.is_correct, tr.elapsed_sec "
                "FROM exam_detail ed JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN `user` u ON u.id=ep.user_id JOIN question q ON q.id=ed.question_id "
                "LEFT JOIN task t ON t.id=ep.task_id "
                "LEFT JOIN task_record tr ON tr.paper_id=ep.id "
                "ORDER BY ep.id, ed.seq_no")
        headers = ['提交时间', '账号', '姓名', '任务', '题号', '题干', '题型',
                   '学生答案', '正确答案', '对错', '用时(秒)']
        rows = [(str(r['submitted_at']) if r['submitted_at'] else '', r['username'],
                 r['real_name'] or '', r['title'] or '模拟考试', r['id'],
                 r['stem'], r['qtype'], r['user_answer'] or '未答',
                 r['correct_answer'], '对' if r['is_correct'] else '错',
                 r['elapsed_sec'] or '') for r in rows_data]
        return headers, rows, '全量明细'


@app.route('/admin/answer_data')
@login_required
def admin_answer_data():
    """答题数据页：筛选+预览表格+导出"""
    me, err = _admin_or_back()
    if err:
        return err
    tasks = q("SELECT id, title FROM task ORDER BY id DESC")
    students = q("SELECT id, username, real_name FROM `user` WHERE role='student' ORDER BY id")
    # 预览前 20 条全量数据
    preview = q(
        "SELECT ep.submitted_at, u.username, u.real_name, t.title, "
        "q.id, LEFT(q.stem,40) stem, q.qtype, ed.user_answer, "
        "ed.is_correct, tr.elapsed_sec "
        "FROM exam_detail ed "
        "JOIN exam_paper ep ON ep.id=ed.paper_id "
        "JOIN `user` u ON u.id=ep.user_id "
        "JOIN question q ON q.id=ed.question_id "
        "LEFT JOIN task t ON t.id=ep.task_id "
        "LEFT JOIN task_record tr ON tr.paper_id=ep.id "
        "ORDER BY ep.id DESC, ed.seq_no LIMIT 20")
    return render_template('admin_answer_data.html', tasks=tasks, students=students,
                           preview=preview, scenarios=EXPORT_SCENARIOS)


@app.route('/admin/answer_data/export')
@login_required
def admin_answer_data_export():
    """导出 CSV/Excel"""
    me, err = _admin_or_back()
    if err:
        return err
    scenario = request.args.get('scenario', 'archive')
    fmt = request.args.get('fmt', 'csv')
    task_id = request.args.get('task', '', type=str)
    student_id = request.args.get('student', '', type=str)

    if scenario not in EXPORT_SCENARIOS:
        flash('未知导出场景', 'danger')
        return redirect(url_for('admin_answer_data'))

    headers, rows, name_prefix = _build_export_data(
        scenario, task_id or None, student_id or None, fmt)
    suffix = f"_{task_id}" if task_id else ''
    filename = f"{name_prefix}{suffix}"

    if fmt == 'excel':
        # 个别辅导和全量存档有"对错"列，标红错题行
        wrong_col = None
        if scenario in ('tutor', 'archive'):
            wrong_col = headers.index('对错') + 1
        return _export_excel(headers, rows, filename, wrong_col_idx=wrong_col)
    else:
        return _export_csv(headers, rows, filename)


# ---------------- 功能⑤：学生排名 ----------------
def _pk_badge(wins):
    """根据 PK 胜场返回段位徽章 (emoji, 名称)"""
    if wins >= 30:
        return ('💎', '铂金')
    if wins >= 15:
        return ('🥇', '黄金')
    if wins >= 5:
        return ('🥈', '白银')
    return ('🥉', '青铜')


@app.route('/ranking')
@login_required
def ranking():
    """学生排名：考试排名（按平均分）+ 练习排名（按总正确题数）"""
    me = current_user()
    # 考试排名：按平均分降序（只统计已完成的试卷）
    exam_rank = q(
        "SELECT u.id, u.username, u.real_name, u.pk_wins, u.win_streak, "
        "COUNT(ep.id) AS exam_count, "
        "ROUND(AVG(ep.score),1) AS avg_score, "
        "SUM(CASE WHEN ep.score>=90 THEN 1 ELSE 0 END) AS pass_count, "
        "MAX(ep.score) AS best_score "
        "FROM `user` u "
        "LEFT JOIN exam_paper ep ON ep.user_id=u.id AND ep.status='finished' "
        "WHERE u.role='student' "
        "GROUP BY u.id, u.username, u.real_name, u.pk_wins, u.win_streak "
        "ORDER BY avg_score DESC, exam_count DESC")

    # 练习排名：按总正确题数降序
    practice_rank = q(
        "SELECT u.id, u.username, u.real_name, u.pk_wins, u.win_streak, "
        "COUNT(pr.id) AS practice_count, "
        "SUM(pr.is_correct) AS correct_count, "
        "ROUND(SUM(pr.is_correct)*100.0/GREATEST(COUNT(pr.id),1),1) AS correct_rate "
        "FROM `user` u "
        "LEFT JOIN practice pr ON pr.user_id=u.id "
        "WHERE u.role='student' "
        "GROUP BY u.id, u.username, u.real_name, u.pk_wins, u.win_streak "
        "ORDER BY correct_count DESC, practice_count DESC")

    # 为每行附加排名和徽章
    for i, r in enumerate(exam_rank, 1):
        r['rank'] = i
        r['badge'] = _pk_badge(r['pk_wins'] or 0)
    for i, r in enumerate(practice_rank, 1):
        r['rank'] = i
        r['badge'] = _pk_badge(r['pk_wins'] or 0)

    # 当前用户在两个榜单中的排名
    my_exam_rank = next((r['rank'] for r in exam_rank if r['id'] == me['id']), None)
    my_prac_rank = next((r['rank'] for r in practice_rank if r['id'] == me['id']), None)

    return render_template('ranking.html',
                           exam_rank=exam_rank, practice_rank=practice_rank,
                           my_exam_rank=my_exam_rank, my_prac_rank=my_prac_rank,
                           me=me)


# ---------------- 功能⑦：双人 PK 赛车（socketio）----------------
# 内存房间状态：room_key -> dict
# {challenger, opponent, status, questions, current_q, scores, answers, ready, sids}
PK_ROOMS = {}
ONLINE_SIDS = {}   # sid -> uid
PK_QUESTION_COUNT = 10
PK_Q_TIME = 15     # 每题秒数


def _pk_room_key(pid):
    return f'pk_{pid}'


@app.route('/pk')
@login_required
def pk_lobby():
    """PK 大厅：在线学生 + 我的战绩 + 对战记录"""
    me = current_user()
    # 所有学生（在线状态由前端 socket 上报，这里列全部）
    students = q(
        "SELECT id, username, real_name, pk_wins, pk_losses, win_streak "
        "FROM `user` WHERE role='student' AND id<>%s ORDER BY pk_wins DESC",
        (session['uid'],))
    # 我的战绩
    my_stat = q("SELECT pk_wins, pk_losses, win_streak FROM `user` WHERE id=%s",
                (session['uid'],), one=True)
    # 最近对战记录
    records = q(
        "SELECT p.*, uc.username AS c_name, uo.username AS o_name "
        "FROM pk_challenge p "
        "JOIN `user` uc ON uc.id=p.challenger_uid "
        "JOIN `user` uo ON uo.id=p.opponent_uid "
        "WHERE p.challenger_uid=%s OR p.opponent_uid=%s "
        "ORDER BY p.id DESC LIMIT 10",
        (session['uid'], session['uid']))
    badge = _pk_badge(my_stat['pk_wins'] or 0)
    return render_template('pk_lobby.html', students=students,
                           my_stat=my_stat, badge=badge, records=records, me=me)


@app.route('/pk/challenge', methods=['POST'])
@login_required
def pk_challenge():
    """发起挑战：选对手，抽10道判断题，创建房间"""
    opponent_id = request.form.get('opponent', type=int)
    if not opponent_id or opponent_id == session['uid']:
        flash('请选择有效的对手', 'danger')
        return redirect(url_for('pk_lobby'))
    opp = q("SELECT id, username FROM `user` WHERE id=%s AND role='student'",
            (opponent_id,), one=True)
    if not opp:
        flash('对手不存在', 'danger')
        return redirect(url_for('pk_lobby'))

    # 抽10道判断题（PK 用判断题，抢答快）
    qids = [r['id'] for r in
            q(f"SELECT id FROM question WHERE qtype='judge' "
              f"ORDER BY RAND() LIMIT {PK_QUESTION_COUNT}")]
    if len(qids) < PK_QUESTION_COUNT:
        flash('题库判断题不足10道，无法PK', 'danger')
        return redirect(url_for('pk_lobby'))

    pid = execute(
        "INSERT INTO pk_challenge (challenger_uid, opponent_uid, question_ids, status) "
        "VALUES (%s, %s, %s, 'waiting')",
        (session['uid'], opponent_id, ','.join(map(str, qids))))

    # 初始化内存房间
    key = _pk_room_key(pid)
    PK_ROOMS[key] = {
        'challenger': session['uid'],
        'opponent': opponent_id,
        'status': 'waiting',
        'questions': qids,
        'current_q': -1,
        'scores': {session['uid']: 0, opponent_id: 0},
        'answers': {},   # q_idx -> {uid: answer_label}
        'answered': {},  # q_idx -> set of uid who got it right (locked)
        'ready': set(),
        'sids': {},
    }
    return redirect(url_for('pk_room', pid=pid))


@app.route('/pk/<int:pid>')
@login_required
def pk_room(pid):
    """PK 房间页"""
    rec = q(
        "SELECT p.*, uc.username AS c_name, uc.real_name AS c_real, "
        "uo.username AS o_name, uo.real_name AS o_real, "
        "uc.pk_wins AS c_wins, uo.pk_wins AS o_wins, "
        "uc.win_streak AS c_streak, uo.win_streak AS o_streak "
        "FROM pk_challenge p "
        "JOIN `user` uc ON uc.id=p.challenger_uid "
        "JOIN `user` uo ON uo.id=p.opponent_uid "
        "WHERE p.id=%s", (pid,), one=True)
    if not rec:
        abort(404)
    if session['uid'] not in (rec['challenger_uid'], rec['opponent_uid']):
        flash('你不是本局玩家', 'danger')
        return redirect(url_for('pk_lobby'))

    # 加载题目内容（发给前端）
    qids = [int(x) for x in rec['question_ids'].split(',')]
    questions = load_questions(qids)
    # 序列化题目（选项只保留 label/content/is_correct 不发给客户端正确答案防作弊——
    # 但判断题答案在客户端判分有风险，这里由服务端判分，不发 is_correct）
    qs = []
    for i, qq in enumerate(questions):
        qs.append({
            'idx': i,
            'id': qq['id'],
            'stem': qq['stem'],
            'options': [{'label': o['label'], 'content': o['content']}
                        for o in qq['options']],
        })

    my_role = 'challenger' if session['uid'] == rec['challenger_uid'] else 'opponent'
    my_name = rec['c_name'] if my_role == 'challenger' else rec['o_name']
    opp_name = rec['o_name'] if my_role == 'challenger' else rec['c_name']
    my_wins = rec['c_wins'] if my_role == 'challenger' else rec['o_wins']
    opp_wins = rec['o_wins'] if my_role == 'challenger' else rec['c_wins']
    my_streak = rec['c_streak'] if my_role == 'challenger' else rec['o_streak']
    opp_streak = rec['o_streak'] if my_role == 'challenger' else rec['c_streak']

    return render_template('pk_room.html', pid=pid, rec=rec, questions=qs,
                           my_role=my_role, my_name=my_name, opp_name=opp_name,
                           my_wins=my_wins, opp_wins=opp_wins,
                           my_streak=my_streak, opp_streak=opp_streak,
                           q_time=PK_Q_TIME)


# ---- WebSocket 事件 ----
@socketio.on('connect')
def socket_connect():
    if 'uid' in session:
        ONLINE_SIDS[request.sid] = session['uid']


@socketio.on('disconnect')
def socket_disconnect():
    uid = ONLINE_SIDS.pop(request.sid, None)
    # 通知所在房间的对手
    for key, room in PK_ROOMS.items():
        if uid in (room['challenger'], room['opponent']):
            other = room['opponent'] if uid == room['challenger'] else room['challenger']
            emit('opponent_left', {'uid': uid}, room=key)


@socketio.on('pk_join')
def pk_join(data):
    """玩家加入房间"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        return
    uid = session.get('uid')
    if uid not in (room['challenger'], room['opponent']):
        return
    join_room(key)
    room['sids'][uid] = request.sid

    # 通知房间内双方当前状态
    players = [
        {'uid': room['challenger'], 'ready': room['challenger'] in room['ready']},
        {'uid': room['opponent'], 'ready': room['opponent'] in room['ready']},
    ]
    emit('room_state', {'players': players, 'status': room['status']}, room=key)


@socketio.on('pk_ready')
def pk_ready(data):
    """玩家准备"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        return
    uid = session.get('uid')
    if uid not in (room['challenger'], room['opponent']):
        return
    room['ready'].add(uid)
    emit('player_ready', {'uid': uid}, room=key)

    # 双方都准备 -> 开始倒计时
    if room['challenger'] in room['ready'] and room['opponent'] in room['ready']:
        room['status'] = 'playing'
        room['current_q'] = -1
        socketio.sleep(1)
        emit('start_countdown', {}, room=key)
        # 3-2-1-GO
        for n in [3, 2, 1]:
            socketio.sleep(1)
            emit('countdown', {'n': n}, room=key)
        socketio.sleep(1)
        emit('go', {}, room=key)
        socketio.sleep(0.5)
        _pk_next_question(key, room)


def _pk_next_question(key, room):
    """推送下一题"""
    room['current_q'] += 1
    idx = room['current_q']
    if idx >= PK_QUESTION_COUNT:
        _pk_finish(key, room)
        return
    qids = room['questions']
    qq = load_questions([qids[idx]])[0]
    payload = {
        'idx': idx,
        'total': PK_QUESTION_COUNT,
        'stem': qq['stem'],
        'options': [{'label': o['label'], 'content': o['content']}
                    for o in qq['options']],
        'q_time': PK_Q_TIME,
        'scores': {str(k): v for k, v in room['scores'].items()},
    }
    room['answers'][idx] = {}
    room['answered'][idx] = set()
    room['locked_q'] = -1
    emit('next_question', payload, room=key)

    # 每题倒计时；有人答对锁定后提前进入下一题
    for _ in range(PK_Q_TIME):
        socketio.sleep(1)
        if room['status'] != 'playing' or room['current_q'] != idx:
            return
        if room.get('locked_q') == idx:
            break
    if room['status'] != 'playing' or room['current_q'] != idx:
        return
    # 公布正确答案
    correct = ''.join(o['label'] for o in qq['options'] if o['is_correct'])
    emit('question_timeout', {'idx': idx, 'correct': correct}, room=key)
    socketio.sleep(2)
    _pk_next_question(key, room)


@socketio.on('pk_answer')
def pk_answer(data):
    """玩家提交答案"""
    pid = data.get('pid')
    idx = data.get('idx')
    answer = data.get('answer')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room or room['status'] != 'playing':
        return
    uid = session.get('uid')
    if uid not in (room['challenger'], room['opponent']):
        return
    if idx != room['current_q']:
        return

    # 该题已被此人答对锁定 -> 不能重复答
    if uid in room['answered'].get(idx, set()):
        return

    # 服务端判分
    qids = room['questions']
    qq = load_questions([qids[idx]])[0]
    correct_labels = sorted(o['label'] for o in qq['options'] if o['is_correct'])
    ok = sorted([answer]) == correct_labels

    other = room['opponent'] if uid == room['challenger'] else room['challenger']
    correct_str = ''.join(correct_labels)

    if ok:
        # 答对：得分，锁定该题（对方不能再答）
        room['scores'][uid] += 1
        room['answered'][idx].add(uid)
        room['locked_q'] = idx   # 通知定时器提前推进
        emit('answer_result', {
            'uid': uid, 'correct': True, 'answer': answer,
            'correct_answer': correct_str,
            'scores': {str(k): v for k, v in room['scores'].items()},
            'locked': True,
        }, room=key)
    else:
        # 答错：不加分，但对方还可以答
        room['answers'].setdefault(idx, {})[uid] = answer
        emit('answer_result', {
            'uid': uid, 'correct': False, 'answer': answer,
            'scores': {str(k): v for k, v in room['scores'].items()},
            'locked': False,
        }, room=request.sid)
        # 通知对方"对手答了但错了，你可以继续答"
        other_sid = room['sids'].get(other)
        if other_sid:
            emit('opponent_wrong', {'uid': uid}, room=other_sid)


@socketio.on('pk_emoji')
def pk_emoji(data):
    """快捷表情"""
    pid = data.get('pid')
    emoji = data.get('emoji', '')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        return
    uid = session.get('uid')
    if uid not in (room['challenger'], room['opponent']):
        return
    other = room['opponent'] if uid == room['challenger'] else room['challenger']
    sid = room['sids'].get(other)
    if sid:
        emit('emoji', {'from': uid, 'emoji': emoji}, room=sid)


def _pk_finish(key, room):
    """游戏结束：判定胜负，更新战绩"""
    room['status'] = 'finished'
    cs = room['scores'][room['challenger']]
    os_ = room['scores'][room['opponent']]
    if cs > os_:
        winner = room['challenger']
    elif os_ > cs:
        winner = room['opponent']
    else:
        winner = None  # 平局

    # 更新数据库
    execute("UPDATE pk_challenge SET status='finished', "
            "challenger_score=%s, opponent_score=%s, winner_uid=%s, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=%s",
            (cs, os_, winner, int(key.split('_')[1])))

    if winner:
        # 胜方：胜场+1，连胜+1
        execute("UPDATE `user` SET pk_wins=pk_wins+1, win_streak=win_streak+1 "
                "WHERE id=%s", (winner,))
        # 负方：负场+1，连胜清零
        loser = room['opponent'] if winner == room['challenger'] else room['challenger']
        execute("UPDATE `user` SET pk_losses=pk_losses+1, win_streak=0 "
                "WHERE id=%s", (loser,))

    emit('game_over', {
        'scores': {str(k): v for k, v in room['scores'].items()},
        'winner': winner,
        'challenger': room['challenger'],
        'opponent': room['opponent'],
    }, room=key)


# ---------------- image 服务 ----------------
@app.route('/image/<int:iid>')
def image(iid):
    row = q("SELECT mime_type, data FROM image WHERE id=%s", (iid,), one=True)
    if not row:
        abort(404)
    return Response(row['data'], mimetype=row['mime_type'])


# ---------------- crawler 模块（加分项④：网上爬题增量导入） ----------------
import crawler as crawl_mod
import trend as trend_mod   # 加分项⑤：历年题库对比（采集页实时展示）


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
    trend = trend_mod.trend_data()
    return render_template('crawl.html', batches=batches,
                           sources=crawl_mod.SOURCES, year_dist=year_dist,
                           trend=trend)


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


@app.route('/admin/crawl/delete_year/<year>', methods=['POST'])
@login_required
@admin_required
def crawl_delete_year(year):
    if year == '2026':
        flash('2026 为原始题库（题库_2026.docx 导入），禁止删除', 'danger')
        return redirect(url_for('crawl_page'))
    try:
        n = crawl_mod.delete_year(year)
    except Exception as e:
        flash(f'删除失败：{e}', 'danger')
        return redirect(url_for('crawl_page'))
    if n == 0:
        flash(f'{year} 年版没有题目，无需删除', 'warning')
    else:
        flash(f'已删除 {year} 年版 {n} 道题目及其考试/练习/错题/AI 验证关联记录', 'success')
    return redirect(url_for('crawl_page'))


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

VERDICT_NAME = {'correct': '盲答一致', 'kept': '仲裁维持', 'wrong': '疑似错题',
                'uncertain': '无法判定', 'skipped': '带图跳过'}


@app.route('/admin/ai')
@login_required
@admin_required
def ai_page():
    llm_mod.mark_zombie()          # 服务重启遗留的僵死批次自动标记
    cfg = llm_mod.get_config()
    stats, verdicts, pending_text, pending_img = llm_mod.verify_stats()
    verdict_filter = request.args.get('verdict')
    batch_filter = request.args.get('batch', type=int)
    results = llm_mod.recent_results(50, verdict_filter, batch_filter)
    return render_template('ai.html', cfg=cfg, providers=llm_mod.PROVIDERS,
                           stats=stats, verdicts=verdicts,
                           pending_text=pending_text, pending_img=pending_img,
                           results=results, verdict_filter=verdict_filter,
                           batch_filter=batch_filter,
                           batches=llm_mod.recent_batches(8),
                           VERDICT_NAME=VERDICT_NAME)


@app.route('/admin/ai/save', methods=['POST'])
@login_required
@admin_required
def ai_save():
    provider = request.form['provider']
    base_url = request.form['base_url'].strip()
    api_key = request.form['api_key'].strip()
    model = request.form['model'].strip()
    vl_model = request.form.get('vl_model', '').strip()
    if not (base_url and api_key and model):
        flash('接口地址、API Key、模型名均不能为空', 'danger')
    else:
        llm_mod.save_config(provider, base_url, api_key, model, vl_model)
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
    raw = request.form.get('limit', '').strip()
    limit = None                    # 留空 / 0 = 全部待验证题
    if raw:
        try:
            limit = max(1, min(50000, int(raw)))
        except ValueError:
            limit = 50
    bid, err = llm_mod.start_verify(scope, limit,
                                    include_images=request.form.get('include_images') == 'on')
    if err:
        flash(err, 'danger')
        return redirect(url_for('ai_page'))
    flash(f'验证任务 #{bid} 已启动（{"全量" if limit is None else f"前 {limit} 题"}'
          f'{"，含图片题" if request.form.get("include_images") == "on" else ""}）', 'success')
    return redirect(url_for('ai_page', watch=bid))


@app.route('/admin/ai/clear', methods=['POST'])
@login_required
@admin_required
def ai_clear():
    n1, n2, n3 = llm_mod.clear_all()
    flash(f'已清空 {n3} 条批次明细、{n1} 条验证记录与 {n2} 个批次，'
          f'全部题目回到待验证状态', 'success')
    return redirect(url_for('ai_page'))


@app.route('/admin/ai/report', methods=['POST'])
@login_required
@admin_required
def ai_report():
    path, n_wrong = llm_mod.export_report()
    flash(f'已生成 answer_report.md（疑似错题 {n_wrong} 道）→ {path}', 'success')
    return redirect(url_for('ai_page'))


@app.route('/admin/ai/verify/status/<int:bid>')
@login_required
@admin_required
def ai_verify_status(bid):
    row = q("SELECT * FROM verify_batch WHERE id=%s", (bid,), one=True)
    if not row:
        return {'error': 'not found'}, 404
    return dict(row)


if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000,
                 allow_unsafe_werkzeug=True)
