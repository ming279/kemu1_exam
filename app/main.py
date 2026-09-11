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
import re
import time
import random
import hashlib
import secrets
from contextlib import contextmanager
from datetime import date, timedelta
from functools import wraps

# 云服务器生产部署用 gunicorn + gevent 提供 WebSocket；
# 此处仅当本机开发装了 eventlet 时才启用其 monkey patch（可选加速）。
# 未装 eventlet（如云服务器）则自动跳过，由 gunicorn 的 gevent worker 完成补丁。
try:
    import eventlet
    eventlet.monkey_patch()
except ImportError:
    pass

import pymysql
from flask import (Flask, g, session, request, redirect, url_for,
                   render_template, flash, Response, abort, jsonify)
from flask_socketio import SocketIO, join_room, emit, leave_room

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kemu1-exam-2026-sec-dev')
# async_mode 不写死：服务器装了 eventlet 自动用 eventlet（gunicorn 部署），
# 本机 Windows 未装则自动 threading（socketio.run 开发模式）
socketio = SocketIO(app, cors_allowed_origins="*")

# 数据库连接：优先读环境变量（云部署），本机默认值保持不变
DB = dict(host=os.environ.get('DB_HOST', 'localhost'),
          user=os.environ.get('DB_USER', 'root'),
          password=os.environ.get('DB_PASSWORD',
                                  os.environ.get('MYSQL_PASSWORD', '123456')),
          database=os.environ.get('DB_NAME', 'kemu1_exam'),
          charset='utf8mb4',
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


@contextmanager
def _tx():
    """多步写入事务包裹（question+option 等关联写入需原子性）：异常自动回滚"""
    db = get_db()
    db.begin()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


# ---------------- 登录控制 ----------------
# 登录互踢：同一账号每次登录生成新 token 写入 user.login_token，
# 旧浏览器 session 中 token 与之不匹配即视为被挤下线（防同账号多人登录串号）
def _login_valid(uid):
    """校验当前会话 token 是否仍为该账号最新登录，返回 bool"""
    row = q("SELECT login_token FROM `user` WHERE id=%s", (uid,), one=True)
    return not (row and row['login_token']
                and row['login_token'] != session.get('token'))


def _kick_session():
    """被挤下线时给出提示（去重，避免每次跳转重复 flash）"""
    if not session.get('login_kicked'):
        session['login_kicked'] = True
        flash('您的账号已在其他设备登录，当前会话已下线', 'warning')


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'uid' not in session:
            return redirect(url_for('login'))
        if not _login_valid(session['uid']):
            _kick_session()
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    if 'uid' not in session:
        return None
    if not _login_valid(session['uid']):
        return None
    return q("SELECT id, username, real_name, role FROM `user` WHERE id=%s",
             (session['uid'],), one=True)


def _study_streak(uid):
    """连续学习天数（练习+考试按天并集，从今天/昨天往前数，C13 每日打卡）"""
    days = set()
    for r in q("SELECT DISTINCT DATE(practiced_at) d FROM practice WHERE user_id=%s "
               "AND practiced_at >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)", (uid,)):
        days.add(r['d'])
    for r in q("SELECT DISTINCT DATE(submitted_at) d FROM exam_paper WHERE user_id=%s "
               "AND status='finished' AND submitted_at >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)",
               (uid,)):
        days.add(r['d'])
    streak, d = 0, date.today()
    if d not in days:                       # 今天还没学，从昨天起算不断签
        d -= timedelta(days=1)
    while d in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


def _ongoing_exam(uid):
    """该学生最近一场未完成考试（自由卷/任务卷），用于全站顶部续考横幅。
    返回 dict 或 None：remain_sec=None 表示不限时/练习；paused 表示任务练习已暂停。"""
    r = q(
        "SELECT p.id, p.task_id, p.started_at, p.time_limit_sec AS free_tl, "
        "t.title AS task_title, t.mode AS task_mode, t.time_limit_sec AS task_tl, "
        "tr.status AS tr_status, tr.paused, "
        "(SELECT COUNT(*) FROM exam_detail d WHERE d.paper_id=p.id "
        " AND d.user_answer IS NOT NULL) AS ans_n, "
        "(SELECT COUNT(*) FROM exam_detail d WHERE d.paper_id=p.id) AS total_n "
        "FROM exam_paper p LEFT JOIN task t ON t.id=p.task_id "
        "LEFT JOIN task_record tr ON tr.paper_id=p.id "
        "WHERE p.user_id=%s AND p.status='in_progress' "
        "ORDER BY p.id DESC LIMIT 1", (uid,), one=True)
    if not r:
        return None
    info = dict(pid=r['id'], name=r['task_title'] or '模拟考试',
                ans_n=r['ans_n'], total_n=r['total_n'],
                remain_sec=None, paused=bool(r['paused']))
    # 限时：自由卷按 started_at；任务考试按 task_record.start_time（任务无暂停）
    tl = r['task_tl'] if r['task_id'] else r['free_tl']
    if r['task_id'] and not (r['task_mode'] == 'exam' and r['task_tl']):
        tl = None
    if tl and not r['paused']:
        base = 'tr.start_time' if r['task_id'] else 'p.started_at'
        e = q(f"SELECT TIMESTAMPDIFF(SECOND, {base}, NOW()) e FROM exam_paper p "
              f"LEFT JOIN task_record tr ON tr.paper_id=p.id "
              f"WHERE p.id=%s", (r['id'],), one=True)
        info['remain_sec'] = max(0, int(tl) - (e['e'] if e else 0))
    return info


@app.context_processor
def inject_user():
    u = current_user()
    extra = {}
    if u:
        extra['study_streak'] = _study_streak(u['id'])
        extra['notif_unread'] = q(
            "SELECT COUNT(*) c FROM notification WHERE uid=%s AND is_read=0",
            (u['id'],), one=True)['c']
        # 管理员：待审核纠错上报数（导航"题目管理"角标）
        if u['role'] == 'admin':
            extra['pending_reports'] = q(
                "SELECT COUNT(*) c FROM question_report WHERE status='pending'",
                one=True)['c']
        # 学生：未完成考试（顶部续考横幅；考试答题页自身不显示）
        if request.endpoint != 'exam_page':
            extra['ongoing_exam'] = _ongoing_exam(u['id'])
    return dict(cur_user=u, QTYPE_NAME=QTYPE_NAME, **extra)


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
        real_name = request.form.get('real_name', '').strip()
        password = request.form['password']
        if not username or not password or not real_name:
            flash('账号、姓名和密码不能为空', 'danger')
        elif q("SELECT id FROM `user` WHERE username=%s", (username,), one=True):
            flash('该账号已被注册', 'danger')
        else:
            execute("INSERT INTO `user` (username, password_hash, real_name, role) "
                    "VALUES (%s, %s, %s, 'student')",
                    (username, sha256(password), real_name))
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
            # 登录互踢：生成新 token，旧会话将因 token 不匹配被下线
            token = secrets.token_hex(16)
            execute("UPDATE `user` SET login_token=%s WHERE id=%s",
                    (token, row['id']))
            session['uid'] = row['id']
            session['token'] = token
            session.pop('login_kicked', None)
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
    # 学生：我的任务（待完成 + 已完成两块）
    my_tasks = []
    my_done_tasks = []
    me = current_user()
    if me and me['role'] == 'student':
        my_tasks, my_done_tasks = _my_task_data(session['uid'])
    # 学生：检查是否有进行中的自由模拟卷（用于前端弹确认）
    has_in_progress = False
    in_progress_info = None
    if me and me['role'] == 'student':
        r = q(
            "SELECT id, started_at FROM exam_paper WHERE user_id=%s "
            "AND task_id IS NULL AND status='in_progress' "
            "ORDER BY id DESC LIMIT 1",
            (session['uid'],), one=True)
        if r:
            has_in_progress = True
            ans = q("SELECT COUNT(*) c FROM exam_detail WHERE paper_id=%s "
                    "AND user_answer IS NOT NULL", (r['id'],), one=True)['c']
            in_progress_info = {'id': r['id'], 'started_at': r['started_at'],
                               'ans_count': ans}
    return render_template('index.html', stats=stats,
                           my_tasks=my_tasks, my_done_tasks=my_done_tasks,
                           has_in_progress=has_in_progress,
                           in_progress_info=in_progress_info)


def _my_task_data(uid):
    """返回学生的任务数据：(待完成/进行中列表, 已完成列表)。
    已完成记录不受任务关闭影响，始终可回看成绩单。"""
    pending = q(
        "SELECT t.id, t.title, t.judge_count, t.single_count, "
        "t.time_limit_sec, t.mode, t.purpose, t.created_at, "
        "tr.status AS my_status "
        "FROM task t LEFT JOIN task_record tr "
        "ON tr.task_id=t.id AND tr.uid=%s "
        "WHERE t.status='published' "
        "AND (t.target_uids IS NULL OR t.target_uids='' "
        "     OR FIND_IN_SET(%s, t.target_uids)) "
        "AND (tr.status IS NULL OR tr.status='in_progress') "
        "ORDER BY t.id DESC",
        (uid, uid))
    done = q(
        "SELECT t.id, t.title, t.judge_count, t.single_count, "
        "t.mode, t.purpose, t.status AS task_status, "
        "tr.paper_id, tr.submit_time, "
        "p.score, p.total_count, "
        "COALESCE(NULLIF(tr.elapsed_sec, 0), "
        "  TIMESTAMPDIFF(SECOND, tr.start_time, tr.submit_time)) AS used_sec "
        "FROM task_record tr "
        "JOIN task t ON t.id = tr.task_id "
        "LEFT JOIN exam_paper p ON p.id = tr.paper_id "
        "WHERE tr.uid=%s AND tr.status='completed' "
        "ORDER BY tr.submit_time DESC",
        (uid,))
    return pending, done


@app.route('/my-tasks')
@login_required
def my_tasks_page():
    """学生"我的任务"独立页：待完成/进行中 + 已完成（可回看成绩单）"""
    pending, done = _my_task_data(session['uid'])
    return render_template('my_tasks.html', pending=pending, done=done)


# ---------------- exam 模块 ----------------
@app.route('/exam/start', methods=['POST'])
@login_required
def exam_start():
    """随机组卷：判断题 1-40 在前，单选题 41-100 在后（按题型分区展示）。
    已有进行中的自由模拟考时直接恢复；用户也可选择放弃旧卷开新卷。"""
    # 方案5：限时卷超时自动关闭（过了时限+5分钟还没交卷，直接判0分 finished）
    execute(
        "UPDATE exam_paper SET status='finished', score=0, "
        "submitted_at=NOW() "
        "WHERE user_id=%s AND task_id IS NULL AND status='in_progress' "
        "AND time_limit_sec IS NOT NULL "
        "AND TIMESTAMPDIFF(SECOND, started_at, NOW()) > time_limit_sec + 300",
        (session['uid'],))

    # 方案3：如果用户点了"放弃旧卷开新卷"，先关闭旧卷
    abandon = request.form.get('abandon')
    if abandon:
        execute(
            "UPDATE exam_paper SET status='finished', score=0, "
            "submitted_at=NOW() "
            "WHERE user_id=%s AND task_id IS NULL AND status='in_progress'",
            (session['uid'],))

    existing = q(
        "SELECT id, started_at, time_limit_sec FROM exam_paper WHERE user_id=%s "
        "AND task_id IS NULL AND status='in_progress' ORDER BY id DESC LIMIT 1",
        (session['uid'],), one=True)
    if existing and not abandon:
        # 统计已答题数，给用户明确提示
        ans_count = q(
            "SELECT COUNT(*) c FROM exam_detail WHERE paper_id=%s AND user_answer IS NOT NULL",
            (existing['id'],), one=True)['c']
        old_tl = '限时 ' + str(existing['time_limit_sec'] // 60) + ' 分钟' \
            if existing['time_limit_sec'] else '不限时'
        flash('已恢复你 ' + existing['started_at'].strftime('%m-%d %H:%M') +
              ' 的模拟考试（已答 ' + str(ans_count) + ' 题，' + old_tl +
              '）。如需更换设置请放弃此卷开新卷', 'info')
        return redirect(url_for('exam_page', pid=existing['id']))

    judges = [r['id'] for r in
              q(f"SELECT id FROM question WHERE qtype='judge' AND is_deleted=0 "
                f"ORDER BY RAND() LIMIT {EXAM_JUDGE_COUNT}")]
    singles = [r['id'] for r in
               q(f"SELECT id FROM question WHERE qtype='single' AND is_deleted=0 "
                 f"ORDER BY RAND() LIMIT {EXAM_SINGLE_COUNT}")]
    ids = judges + singles

    # 限时模式：45 分钟 = 2700 秒，不限时则 NULL
    time_limit = request.form.get('time_limit')
    tl_sec = 2700 if time_limit == '45' else None

    pid = execute("INSERT INTO exam_paper (user_id, total_count, time_limit_sec) "
                  "VALUES (%s, %s, %s)",
                  (session['uid'], len(ids), tl_sec))
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
    details = q("SELECT question_id, seq_no, user_answer FROM exam_detail "
                "WHERE paper_id=%s ORDER BY seq_no", (pid,))
    questions = load_questions([d['question_id'] for d in details])
    for r, d in zip(questions, details):
        r['seq'] = d['seq_no']
        r['user_answer'] = d['user_answer']   # 已自动保存的答案，用于回显勾选
    judges = [r for r in questions if r['qtype'] == 'judge']
    singles = [r for r in questions if r['qtype'] != 'judge']

    # 倒计时剩余秒数：任务考试模式 / 自由模拟考限时 两种来源
    task = None
    task_record = None
    remain_sec = None
    is_practice = False
    if paper.get('task_id'):
        task = q("SELECT * FROM task WHERE id=%s", (paper['task_id'],), one=True)
        task_record = q("SELECT * FROM task_record WHERE paper_id=%s",
                        (pid,), one=True)
        if task and task['mode'] == 'practice':
            is_practice = True
        if task and task['time_limit_sec'] and task['mode'] == 'exam':
            elapsed_row = q(
                "SELECT TIMESTAMPDIFF(SECOND, start_time, NOW()) AS e "
                "FROM task_record WHERE id=%s", (task_record['id'],), one=True)
            remain_sec = task['time_limit_sec'] - (elapsed_row['e'] if elapsed_row else 0)
    elif paper.get('time_limit_sec'):
        # 自由模拟考限时：剩余 = time_limit_sec - (NOW - started_at)
        elapsed = q(
            "SELECT TIMESTAMPDIFF(SECOND, started_at, NOW()) AS e "
            "FROM exam_paper WHERE id=%s", (pid,), one=True)
        remain_sec = paper['time_limit_sec'] - (elapsed['e'] if elapsed else 0)
    if remain_sec is not None and remain_sec < 0:
        remain_sec = 0

    return render_template('exam.html', pid=pid, judges=judges, singles=singles,
                           task=task, task_record=task_record,
                           started_at=paper['started_at'],
                           remain_sec=remain_sec, is_practice=is_practice)


@app.route('/exam/<int:pid>/save', methods=['POST'])
@login_required
def exam_save(pid):
    """作答自动保存：只写 user_answer，不判分、不计错题，交卷时才统一判分"""
    paper = q("SELECT id, status FROM exam_paper WHERE id=%s AND user_id=%s",
              (pid, session['uid']), one=True)
    if not paper or paper['status'] == 'finished':
        return jsonify(ok=False), 404
    details = q("SELECT id, question_id FROM exam_detail WHERE paper_id=%s",
                (pid,))
    for d in details:
        labels = request.form.getlist(f"q_{d['question_id']}")
        execute("UPDATE exam_detail SET user_answer=%s WHERE id=%s",
                (''.join(sorted(labels)) if labels else None, d['id']))
    return jsonify(ok=True)


@app.route('/exam/<int:pid>/blur', methods=['POST'])
@login_required
def exam_blur(pid):
    """切屏记录实时上报：次数(switch_count)与累计离开秒(blur_sec)，只记录不拦截。
    客户端自报数据只取 GREATEST（只能往大走，防篡改改小）。"""
    paper = q("SELECT id, status FROM exam_paper WHERE id=%s AND user_id=%s",
              (pid, session['uid']), one=True)
    if not paper or paper['status'] == 'finished':
        return jsonify(ok=False), 404
    data = request.get_json(silent=True) or {}
    try:
        count = max(0, min(9999, int(data.get('count', 0))))
        sec = max(0, min(999999, int(data.get('sec', 0))))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg='参数无效'), 400
    execute("UPDATE exam_paper SET switch_count=GREATEST(switch_count,%s), "
            "blur_sec=GREATEST(blur_sec,%s) WHERE id=%s", (count, sec, pid))
    return jsonify(ok=True)


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
    # C2 切屏检测：前端统计 visibilitychange+blur 次数随卷提交；秒数取较大值兜底
    switch_n = request.form.get('switch_count', 0, type=int) or 0
    blur_sec = request.form.get('blur_sec', 0, type=int) or 0
    execute("UPDATE exam_paper SET score=%s, status='finished', "
            "submitted_at=CURRENT_TIMESTAMP, switch_count=GREATEST(switch_count,%s), "
            "blur_sec=GREATEST(blur_sec,%s) WHERE id=%s",
            (final_score, switch_n, blur_sec, pid))
    # 若是任务考试，更新 task_record 状态为 completed
    if paper.get('task_id'):
        execute("UPDATE task_record SET status='completed', "
                "submit_time=CURRENT_TIMESTAMP, score=%s "
                "WHERE paper_id=%s", (final_score, pid))
    return redirect(url_for('exam_result', pid=pid))


@app.route('/exam/<int:pid>/result')
@login_required
def exam_result(pid):
    me = current_user()
    is_admin = bool(me and me['role'] == 'admin')
    paper = q("SELECT * FROM exam_paper WHERE id=%s AND (%s=1 OR user_id=%s)",
              (pid, int(is_admin), session['uid']), one=True)
    if not paper:
        abort(404)
    if paper['status'] != 'finished':
        # 未交卷的试卷没有成绩，回到答题页
        return redirect(url_for('exam_page', pid=pid))
    details = q("SELECT d.question_id, d.user_answer, d.is_correct, d.seq_no "
                "FROM exam_detail d WHERE d.paper_id=%s ORDER BY d.seq_no", (pid,))
    questions = load_questions([d['question_id'] for d in details])
    for d, qq in zip(details, questions):
        qq['user_answer'] = d['user_answer']
        qq['is_correct'] = d['is_correct']
        qq['seq'] = d['seq_no']
    judges = [r for r in questions if r['qtype'] == 'judge']
    singles = [r for r in questions if r['qtype'] != 'judge']
    fav_ids = _fav_ids(session['uid'], [r['question_id'] for r in details])
    return render_template('exam_result.html', paper=paper,
                           judges=judges, singles=singles, fav_ids=fav_ids)


# ---------------- practice 模块 ----------------
# 练习模式枚举全链路统一英文码，中文名仅用于页面展示
PRACTICE_MODES = {'random': '随机练习', 'category': '专项练习',
                  'wrong': '错题练习', 'favorite': '收藏练习'}


@app.route('/practice', methods=['GET'])
@login_required
def practice_home():
    """练习中心：随机练习 / 专项练习（分类+题型）/ 错题练习"""
    cats = q("SELECT id, name, parent_id FROM category ORDER BY parent_id, id")
    name_map = {c['id']: c['name'] for c in cats}
    cat_options = [{
        'id': c['id'],
        'label': (name_map[c['parent_id']] + ' / ' if c['parent_id'] else '')
                 + c['name'],
    } for c in cats]
    wrong_n = q("SELECT COUNT(*) c FROM wrong_book WHERE user_id=%s AND mastered=0",
                (session['uid'],), one=True)['c']
    fav_n = q("SELECT COUNT(*) c FROM favorite f JOIN question q ON q.id=f.question_id "
              "WHERE f.user_id=%s AND q.is_deleted=0",
              (session['uid'],), one=True)['c']
    return render_template('practice_home.html', cat_options=cat_options,
                           wrong_n=wrong_n, fav_n=fav_n)


@app.route('/practice/start', methods=['POST'])
@login_required
def practice_start():
    """按模式生成练习题卡：模式只决定题目来源，答题/判分链路共用（题卡驱动）"""
    mode = request.form.get('mode', 'random')
    if mode not in PRACTICE_MODES:
        mode = 'random'
    # C7 背题模式：立即显示答案+解析（不记作答记录）
    session['p_reveal'] = request.form.get('reveal') == '1'
    try:
        n = max(1, min(500, int(request.form.get('num', 10))))
    except (TypeError, ValueError):
        n = 10
    label_parts = []

    if mode == 'wrong':
        rows = q("SELECT wb.question_id FROM wrong_book wb "
                 "JOIN question q ON q.id=wb.question_id "
                 "WHERE wb.user_id=%s AND wb.mastered=0 AND q.is_deleted=0 "
                 "ORDER BY RAND() LIMIT %s", (session['uid'], n))
        ids = [r['question_id'] for r in rows]
        if not ids:
            flash('错题本里还没有需要练习的错题，先去随机练习吧！', 'info')
            return redirect(url_for('practice_home'))
        label_parts.append('错题练习')

    elif mode == 'favorite':
        rows = q("SELECT f.question_id FROM favorite f "
                 "JOIN question q ON q.id=f.question_id "
                 "WHERE f.user_id=%s AND q.is_deleted=0 "
                 "ORDER BY RAND() LIMIT %s", (session['uid'], n))
        ids = [r['question_id'] for r in rows]
        if not ids:
            flash('收藏夹还是空的，做题时点 ☆ 即可收藏题目', 'info')
            return redirect(url_for('practice_home'))
        label_parts.append('收藏练习')

    elif mode == 'category':
        cat_raw = request.form.get('category_id', 'all')
        qtype = request.form.get('qtype', '')
        where, params = ["is_deleted=0"], []
        if cat_raw == '0':
            where.append("category_id IS NULL")
            label_parts.append('未分类')
        elif cat_raw == 'all':
            label_parts.append('全部分类')
        else:
            cid = int(cat_raw)
            where.append("category_id=%s")
            params.append(cid)
            cname = q("SELECT name FROM category WHERE id=%s", (cid,), one=True)
            label_parts.append(cname['name'] if cname else '未知分类')
        if qtype in ('judge', 'single', 'multi'):
            where.append("qtype=%s")
            params.append(qtype)
            label_parts.append(QTYPE_NAME[qtype])
        sql = "SELECT id FROM question"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY RAND() LIMIT %s"
        params.append(n)
        ids = [r['id'] for r in q(sql, tuple(params))]
        if not ids:
            flash('该条件下没有可练习的题目', 'warning')
            return redirect(url_for('practice_home'))

    else:  # random
        ids = [r['id'] for r in
               q("SELECT id FROM question WHERE is_deleted=0 "
                 "ORDER BY RAND() LIMIT %s", (n,))]

    session['p_ids'] = ids
    session['p_mode'] = mode
    session['p_mode_label'] = PRACTICE_MODES[mode] if mode != 'category' \
        else '专项练习 · ' + ' · '.join(label_parts)
    if mode == 'wrong':
        session['p_mode_label'] = '错题练习'
    return redirect(url_for('practice_page', idx=0))


@app.route('/practice/<int:idx>', methods=['GET'])
@login_required
def practice_page(idx):
    p_ids = session.get('p_ids', [])
    if idx >= len(p_ids):
        return redirect(url_for('practice_summary'))
    loaded = load_questions([p_ids[idx]])
    if not loaded:
        # 题目已被管理员删除：跳过本题继续（防 IndexError 500）
        return redirect(url_for('practice_page', idx=idx + 1))
    question = loaded[0]
    fb = session.pop('p_feedback', None)      # 上一题的判分反馈
    is_fav = bool(q("SELECT 1 ok FROM favorite WHERE user_id=%s AND question_id=%s",
                    (session['uid'], question['id']), one=True))
    return render_template('practice.html', idx=idx, total=len(p_ids),
                           question=question, feedback=fb,
                           reveal=session.get('p_reveal', False),
                           mode_label=session.get('p_mode_label', '练习'),
                           is_fav=is_fav)


@app.route('/practice/answer', methods=['POST'])
@login_required
def practice_answer():
    idx = int(request.form['idx'])
    p_ids = session.get('p_ids', [])
    qid = p_ids[idx]
    loaded = load_questions([qid])
    if not loaded:
        # 题目已被管理员删除：跳过本题继续
        return redirect(url_for('practice_page', idx=idx + 1))
    question = loaded[0]
    labels = request.form.getlist('q_%d' % qid)
    ok = bool(labels) and judge_answer(question, labels)

    # 记录练习
    execute("INSERT INTO practice (user_id, question_id, user_answer, is_correct) "
            "VALUES (%s, %s, %s, %s)",
            (session['uid'], qid, ''.join(sorted(labels)) or None, int(ok)))
    # 维护错题本：连对 2 次自动标记"已掌握"移出；答错重置连对计数并累计错次
    if ok:
        # 注意 SET 左到右求值：先判 mastered（此时 correct_streak 为旧值），再自增
        execute(
            "UPDATE wrong_book SET "
            "mastered = CASE WHEN correct_streak + 1 >= 2 THEN 1 ELSE mastered END, "
            "correct_streak = correct_streak + 1 "
            "WHERE user_id=%s AND question_id=%s",
            (session['uid'], qid))
    else:
        execute(
            "INSERT INTO wrong_book (user_id, question_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE wrong_count = wrong_count + 1, "
            "last_wrong_at = CURRENT_TIMESTAMP, mastered = 0, correct_streak = 0",
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
    mode_label = session.pop('p_mode_label', '练习')
    session.pop('p_mode', None)
    session.pop('p_reveal', None)
    return render_template('practice_summary.html', total=len(p_ids),
                           mode_label=mode_label)


# ---------------- wrongbook 模块 ----------------
_STEM_PUNCT = re.compile(r'[\s，。、,.：:；;？！?!（）()\-—~～]')


def _stem_grams(s):
    """题干 2-gram 集合（去标点空白），用于相似题 Jaccard 相似度"""
    s = _STEM_PUNCT.sub('', s or '')
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar_questions(qid, n=3):
    """C19 相似题推荐：同题型优先同分类，2-gram Jaccard 取 top n。
    只返回题目 id 列表，具体选项由调用方用 load_questions 取。"""
    base = q("SELECT id, stem, qtype, category_id FROM question WHERE id=%s",
             (qid,), one=True)
    if not base:
        return []
    cands = q("SELECT id, stem FROM question WHERE id<>%s AND qtype=%s "
              "AND is_deleted=0 "
              "ORDER BY (category_id=%s) DESC, RAND() LIMIT 400",
              (qid, base['qtype'], base['category_id']))
    grams = _stem_grams(base['stem'])
    scored = []
    for c in cands:
        g2 = _stem_grams(c['stem'])
        union = grams | g2
        if not union:
            continue
        score = len(grams & g2) / len(union)
        if score >= 0.12:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [{'id': c['id'], 'sim': round(sc, 2)} for sc, c in scored[:n]]


@app.route('/wrongbook')
@login_required
def wrongbook():
    rows = q(
        "SELECT wb.id, wb.wrong_count, wb.correct_streak, wb.last_wrong_at, "
        "q.id AS qid, q.stem, q.qtype FROM wrong_book wb "
        "JOIN question q ON q.id = wb.question_id "
        "WHERE wb.user_id=%s AND wb.mastered=0 ORDER BY wb.last_wrong_at DESC",
        (session['uid'],))
    items = load_questions([r['qid'] for r in rows])
    for r, it in zip(rows, items):
        it['wrong_count'] = r['wrong_count']
        it['correct_streak'] = r['correct_streak']
        it['last_wrong_at'] = r['last_wrong_at']
    # C19 相似题推荐（前 20 题计算，避免长列表过慢）
    sim_map = {it['id']: _similar_questions(it['id']) for it in items[:20]}
    fav_ids = _fav_ids(session['uid'], [it['id'] for it in items])
    return render_template('wrongbook.html', questions=items, sim_map=sim_map,
                           fav_ids=fav_ids)


@app.route('/wrongbook/master/<int:qid>', methods=['POST'])
@login_required
def wrongbook_master(qid):
    execute("UPDATE wrong_book SET mastered=1 WHERE user_id=%s AND question_id=%s",
            (session['uid'], qid))
    flash('已标记为掌握，移出错题本', 'success')
    return redirect(url_for('wrongbook'))


@app.route('/wrongbook/sim/<int:qid>')
@login_required
def sim_practice(qid):
    """C19 相似题专项练习：从错题本入口跳转，3 道相似题逐题作答，答完汇总"""
    base = q("SELECT stem FROM question WHERE id=%s", (qid,), one=True)
    if not base:
        abort(404)
    sims = _similar_questions(qid, n=3)
    if not sims:
        flash('这道题暂时找不到足够的相似题可以练习', 'warning')
        return redirect(url_for('wrongbook'))
    ids = [s['id'] for s in sims]
    questions = load_questions(ids)
    # 保留原推荐顺序（sim 里的 sim 值）
    sim_map = {s['id']: s['sim'] for s in sims}
    for qq in questions:
        qq['sim'] = sim_map.get(qq['id'], 0)
    questions.sort(key=lambda x: -x['sim'])
    fav_ids = _fav_ids(session['uid'], [qq['id'] for qq in questions] + [qid])
    return render_template('sim_practice.html', questions=questions,
                           base_stem=base['stem'][:60], base_qid=qid,
                           fav_ids=fav_ids)


@app.route('/practice/sim-wrong', methods=['POST'])
@login_required
def sim_wrong_record():
    """C19 相似题练习答错：写 practice 记录 + wrong_book（不影响掌握状态）"""
    data = request.get_json(silent=True) or {}
    qid = data.get('qid')
    if not qid:
        return jsonify(ok=False), 400
    execute("INSERT INTO practice (user_id, question_id, user_answer, is_correct) "
            "VALUES (%s, %s, NULL, 0)", (session['uid'], qid))
    execute(
        "INSERT INTO wrong_book (user_id, question_id) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE wrong_count = wrong_count + 1, "
        "last_wrong_at = CURRENT_TIMESTAMP, mastered = 0, correct_streak = 0",
        (session['uid'], qid))
    return jsonify(ok=True)


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


# ---------------- 题目收藏夹 ----------------
def _fav_ids(uid, qids):
    """批量返回该用户已收藏的题目 id 集合，供列表页渲染星星状态"""
    if not qids:
        return set()
    ph = ','.join(['%s'] * len(qids))
    return {r['question_id'] for r in
            q(f"SELECT question_id FROM favorite WHERE user_id=%s "
              f"AND question_id IN ({ph})", (uid, *qids))}


@app.route('/favorites')
@login_required
def favorites_page():
    """收藏夹列表：学生自主收藏的题，可直接开始收藏练习或逐题移除"""
    rows = q(
        "SELECT f.id AS fav_id, f.created_at, q.id AS qid, q.stem, q.qtype "
        "FROM favorite f JOIN question q ON q.id=f.question_id "
        "WHERE f.user_id=%s AND q.is_deleted=0 "
        "ORDER BY f.id DESC", (session['uid'],))
    items = load_questions([r['qid'] for r in rows])
    for r, it in zip(rows, items):
        it['fav_id'] = r['fav_id']
        it['fav_at'] = r['created_at']
    return render_template('favorites.html', questions=items, total=len(items))


@app.route('/favorite/<int:qid>/toggle', methods=['POST'])
@login_required
def favorite_toggle(qid):
    """收藏/取消收藏（同一接口切换）。已删除题目不可收藏。"""
    qrow = q("SELECT id, is_deleted FROM question WHERE id=%s",
             (qid,), one=True)
    if not qrow:
        return jsonify(ok=False, msg='题目不存在'), 404
    row = q("SELECT id FROM favorite WHERE user_id=%s AND question_id=%s",
            (session['uid'], qid), one=True)
    if row:
        execute("DELETE FROM favorite WHERE user_id=%s AND question_id=%s",
                (session['uid'], qid))
        return jsonify(ok=True, fav=False)
    if qrow['is_deleted']:
        return jsonify(ok=False, msg='题目已删除，无法收藏'), 400
    execute("INSERT INTO favorite (user_id, question_id) VALUES (%s, %s)",
            (session['uid'], qid))
    return jsonify(ok=True, fav=True)


@app.route('/favorite/<int:qid>/remove', methods=['POST'])
@login_required
def favorite_remove(qid):
    """收藏夹列表页表单式移除（非 AJAX，移除后回到列表）"""
    execute("DELETE FROM favorite WHERE user_id=%s AND question_id=%s",
            (session['uid'], qid))
    flash(f'已移出题 # {qid} 的收藏', 'success')
    return redirect(url_for('favorites_page'))


# ---------------- C18 AI 错题讲解 / C20 纠错上报 ----------------
PROMPT_EXPLAIN = (
    '你是机动车驾驶人科目一考试的资深教练。请用通俗易懂的语言讲解下面这道题：'
    '先点明正确答案，再解释其中的法规原理和易错点，最后给一句好记的口诀。'
    '全文不超过150字。\n题目：{stem}\n选项：\n{options}\n正确答案：{answer}'
)


@app.route('/ai/explain/<int:qid>', methods=['POST'])
@login_required
def ai_explain(qid):
    """AI 通俗讲解：优先返回已缓存的解析，否则调 LLM 生成并缓存入 explanation"""
    row = q("SELECT explanation FROM question WHERE id=%s", (qid,), one=True)
    if not row:
        return jsonify(ok=False, msg='题目不存在'), 404
    if row['explanation']:
        return jsonify(ok=True, explanation=row['explanation'], cached=True)
    cfg = llm_mod.get_config()
    if not cfg:
        return jsonify(ok=False, msg='管理员尚未配置 AI 接口，无法生成讲解')
    qq = load_questions([qid])[0]
    opt_lines = '\n'.join(f"{o['label']}. {o['content']}" for o in qq['options'])
    answer = ''.join(o['label'] for o in qq['options'] if o['is_correct'])
    try:
        content, pt, ct, ms = llm_mod._chat(
            cfg, PROMPT_EXPLAIN.format(stem=qq['stem'], options=opt_lines,
                                       answer=answer), 300)
    except Exception as e:
        return jsonify(ok=False, msg='AI 调用失败：' + str(e)[:200])
    text = (content or '').strip()
    if not text:
        return jsonify(ok=False, msg='AI 未返回内容，请稍后重试')
    execute("UPDATE question SET explanation=%s WHERE id=%s", (text, qid))
    return jsonify(ok=True, explanation=text)


@app.route('/report/question/<int:qid>', methods=['POST'])
@login_required
def report_question(qid):
    """C20 学生纠错上报（练习/错题本页调用，fetch JSON）"""
    row = q("SELECT id, LEFT(stem, 30) stem_short FROM question WHERE id=%s",
            (qid,), one=True)
    if not row:
        return jsonify(ok=False, msg='题目不存在'), 404
    if request.is_json:
        data = request.get_json(silent=True) or {}
        reason = data.get('reason', '')
    else:
        reason = request.form.get('reason', '')
    reason = (reason or '').strip() or '未填写具体原因'
    execute("INSERT INTO question_report (question_id, uid, reason) "
            "VALUES (%s, %s, %s)", (qid, session['uid'], reason[:500]))
    # 通知所有管理员：站内通知 + 实时推送（铃铛亮红点），确保举报不被漏看
    me = current_user()
    reporter = (me and (me['real_name'] or me['username'])) or '学生'
    content = (f'{reporter} 举报了题目 #{qid}（{row["stem_short"]}…）：'
               f'{reason[:60]}')
    admins = q("SELECT id FROM `user` WHERE role='admin'")
    online_sids = [sid for sid, suid in ONLINE_SIDS.items()
                   if any(a['id'] == suid for a in admins)]
    for a in admins:
        _notify(a['id'], content, url_for('admin_questions'))
    for sid in online_sids:
        socketio.emit('new_notification', {'content': content[:60]}, room=sid)
    return jsonify(ok=True, msg='已收到反馈，感谢纠错！管理员会尽快核实')


# ---------------- C22 站内通知中心 ----------------
def _notify(uid, content, url=None):
    """写一条站内通知"""
    execute("INSERT INTO notification (uid, content, url) VALUES (%s, %s, %s)",
            (uid, content[:200], url))


@app.route('/notifications')
@login_required
def notifications_page():
    """通知列表（进入即全部标记已读）。管理员额外看到发通知表单。"""
    rows = q("SELECT * FROM notification WHERE uid=%s ORDER BY id DESC LIMIT 50",
             (session['uid'],))
    execute("UPDATE notification SET is_read=1 WHERE uid=%s AND is_read=0",
            (session['uid'],))
    students = None
    me = current_user()
    if me and me['role'] == 'admin':
        students = q("SELECT id, username, real_name FROM `user` "
                     "WHERE role='student' ORDER BY id")
    return render_template('notifications.html', notis=rows, students=students)


NOTI_LINKS = {
    'exam': 'exam_start', 'practice': 'practice_home',
    'tasks': 'my_tasks_page', 'pk': 'pk_lobby',
}


@app.route('/notifications/send', methods=['POST'])
@login_required
def notification_send():
    """管理员群发/单发站内通知，并向在线学生实时推送铃铛提醒"""
    me = current_user()
    if not me or me['role'] != 'admin':
        abort(403)
    content = request.form.get('content', '').strip()
    if not content:
        flash('通知内容不能为空', 'danger')
        return redirect(url_for('notifications_page'))
    if request.form.get('target') == 'all':
        uids = [r['id'] for r in q("SELECT id FROM `user` WHERE role='student'")]
    else:
        uids = [int(x) for x in request.form.getlist('uids')]
    uids = [u for u in uids if u]
    if not uids:
        flash('请选择接收学生', 'danger')
        return redirect(url_for('notifications_page'))
    link_key = request.form.get('link', '')
    link = url_for(NOTI_LINKS[link_key]) if link_key in NOTI_LINKS else None
    online_sids = [sid for sid, suid in ONLINE_SIDS.items() if suid in uids]
    for uid in uids:
        _notify(uid, content, link)
    for sid in online_sids:
        socketio.emit('new_notification', {'content': content[:60]}, room=sid)
    flash(f'通知已发送给 {len(uids)} 名学生', 'success')
    return redirect(url_for('notifications_page'))


# ---------------- stats 模块 ----------------
def _pass_probability(scores):
    """C10 过考概率：最近 5 场均分 -> 概率映射"""
    if not scores:
        return None
    avg5 = round(sum(scores[:5]) / min(5, len(scores)), 1)
    if avg5 >= 95:
        p, text = 98, '稳了，放心上考场！'
    elif avg5 >= 90:
        p, text = 85, '已过及格线，保持手感更稳'
    elif avg5 >= 85:
        p, text = 60, '就差一点点，再刷两轮错题'
    elif avg5 >= 80:
        p, text = 40, '继续加油，重点攻克易错题'
    elif avg5 >= 70:
        p, text = 20, '还有差距，建议多做模拟卷'
    else:
        p, text = 10, '先从背题模式开始打基础吧'
    return {'p': p, 'avg5': avg5, 'text': text}


def _achievements(uid, streak):
    """C12 成就徽章：全部由现有数据动态计算，无新表"""
    full = q("SELECT COUNT(*) c FROM exam_paper WHERE user_id=%s AND score=100",
             (uid,), one=True)['c']
    prac_ok = q("SELECT COALESCE(SUM(is_correct),0) c FROM practice "
                "WHERE user_id=%s", (uid,), one=True)['c']
    exam_ok = q("SELECT COALESCE(SUM(ed.is_correct),0) c FROM exam_detail ed "
                "JOIN exam_paper ep ON ep.id=ed.paper_id WHERE ep.user_id=%s",
                (uid,), one=True)['c']
    mastered = q("SELECT COUNT(*) c FROM wrong_book WHERE user_id=%s AND mastered=1",
                 (uid,), one=True)['c']
    wins = q("SELECT pk_wins c FROM `user` WHERE id=%s", (uid,), one=True)['c'] or 0
    covered = q("SELECT COUNT(DISTINCT question_id) c FROM practice "
                "WHERE user_id=%s", (uid,), one=True)['c']
    passed = q("SELECT COUNT(*) c FROM exam_paper WHERE user_id=%s AND score>=90",
               (uid,), one=True)['c']
    return [
        ('💯', '首次满分', '模拟考试拿到 100 分', full >= 1),
        ('🎯', '百发百中', '累计答对 200 题', (prac_ok + exam_ok) >= 200),
        ('📚', '错题克星', '掌握错题 50 道', mastered >= 50),
        ('⚔️', '初次凯旋', '赢下第一场 PK 对战', wins >= 1),
        ('👑', '十战十胜', 'PK 累计获胜 10 场', wins >= 10),
        ('🔥', '七日之约', '连续学习 7 天', streak >= 7),
        ('🗺️', '题海遨游', '练习覆盖 500 道不同题目', covered >= 500),
        ('✅', '及格到手', '考试达到 90 分及格线', passed >= 1),
    ]


@app.route('/stats')
@login_required
def stats():
    me = q("SELECT * FROM v_user_stat WHERE user_id=%s",
           (session['uid'],), one=True)
    # 全部试卷（成绩走势图与概览统计用；明细列表在 /my-papers 页）
    my_papers = q("SELECT p.id, p.total_count, p.score, p.status, "
                  "p.started_at, p.submitted_at, t.title AS task_title "
                  "FROM exam_paper p LEFT JOIN task t ON t.id = p.task_id "
                  "WHERE p.user_id=%s ORDER BY p.id DESC",
                  (session['uid'],))
    done = [p for p in my_papers if p['status'] == 'finished' and p['score'] is not None]
    paper_summary = {
        'total': len(my_papers),
        'done': len(done),
        'ongoing': len(my_papers) - len(done),
        'avg': round(sum(p['score'] for p in done) / len(done), 1) if done else None,
    }
    # 我的易错题 TOP10（按错误次数）
    my_weak = q(
        "SELECT q.id, q.stem, wb.wrong_count FROM wrong_book wb "
        "JOIN question q ON q.id = wb.question_id "
        "WHERE wb.user_id=%s ORDER BY wb.wrong_count DESC LIMIT 10",
        (session['uid'],))

    # C10 过考概率（最近 5 场均分）
    prob = _pass_probability([float(p['score']) for p in done])
    # C9 能力雷达：练习正确率按一级分类聚合（全部分类都显示，未练习=0）
    radar_rows = q(
        "SELECT rc.name AS root_name, "
        "COALESCE(s.attempts, 0) AS attempts, COALESCE(s.corrects, 0) AS corrects "
        "FROM category rc "
        "LEFT JOIN ( "
        "  SELECT COALESCE(c2.id, c1.id) AS root_id, "
        "         COUNT(*) AS attempts, SUM(pr.is_correct) AS corrects "
        "  FROM practice pr "
        "  JOIN question q ON q.id=pr.question_id "
        "  JOIN category c1 ON c1.id=q.category_id "
        "  LEFT JOIN category c2 ON c2.id=c1.parent_id "
        "  WHERE pr.user_id=%s GROUP BY root_id "
        ") s ON s.root_id=rc.id "
        "WHERE rc.parent_id IS NULL ORDER BY rc.id",
        (session['uid'],))
    radar_names = [r['root_name'] for r in radar_rows]
    radar_values = [round(float(r['corrects']) * 100.0 / r['attempts'], 1)
                    if r['attempts'] else 0 for r in radar_rows]
    # C11 学习热力图（近 12 个月每天练习作答量）
    heat_raw = q("SELECT DATE(practiced_at) d, COUNT(*) c FROM practice "
                 "WHERE user_id=%s AND practiced_at >= "
                 "DATE_SUB(CURDATE(), INTERVAL 364 DAY) GROUP BY DATE(practiced_at)",
                 (session['uid'],))
    hmap = {str(r['d']): int(r['c']) for r in heat_raw}
    today = date.today()
    heat_list = [
        [(today - timedelta(days=364 - i)).isoformat(),
         hmap.get((today - timedelta(days=364 - i)).isoformat(), 0)]
        for i in range(365)]
    # C12 成就徽章（C13 连续天数由上下文处理器注入）
    achievements = _achievements(session['uid'], _study_streak(session['uid']))
    ach_unlocked = sum(1 for a in achievements if a[3])
    return render_template('stats.html', me=me, papers=my_papers,
                           psum=paper_summary, weak=my_weak, prob=prob,
                           radar_names=radar_names, radar_values=radar_values,
                           heat_list=heat_list, ach_unlocked=ach_unlocked,
                           achievements=achievements)


def _paper_sources(papers):
    """来源大类统计：模拟考试 / 任务（具体任务名在来源列显示）"""
    n_mock = sum(1 for p in papers if not p['task_title'])
    n_task = len(papers) - n_mock
    sources = []
    if n_mock:
        sources.append(('模拟考试', n_mock))
    if n_task:
        sources.append(('任务', n_task))
    return sources


@app.route('/my-papers')
@login_required
def my_papers():
    """我的全部考试记录（含进行中与已完成，可继续作答或回看成绩单）"""
    papers = q(
        "SELECT p.id, p.total_count, p.score, p.status, "
        "p.started_at, p.submitted_at, t.title AS task_title "
        "FROM exam_paper p LEFT JOIN task t ON t.id = p.task_id "
        "WHERE p.user_id=%s ORDER BY p.id DESC",
        (session['uid'],))
    return render_template('my_papers.html', papers=papers,
                           sources=_paper_sources(papers))


@app.route('/stats/clear/papers', methods=['POST'])
@login_required
def stats_clear_papers():
    """删除当前用户全部考试记录（exam_detail 随外键级联删除）"""
    n = q("SELECT COUNT(*) c FROM exam_paper WHERE user_id=%s",
          (session['uid'],), one=True)['c']
    execute("DELETE FROM exam_paper WHERE user_id=%s", (session['uid'],))
    flash(f'已删除 {n} 份试卷（含全部答题明细）', 'success')
    return redirect(url_for('stats'))


@app.route('/exam_paper/<int:pid>/delete', methods=['POST'])
@login_required
def exam_paper_delete(pid):
    """删除单份试卷：学生只能删自己已完成的；admin 能删任何"""
    me = current_user()
    paper = q("SELECT id, user_id, status FROM exam_paper WHERE id=%s",
              (pid,), one=True)
    if not paper:
        return jsonify(ok=False, msg='试卷不存在'), 404
    is_admin = me.role == 'admin'
    if not is_admin and paper['user_id'] != me['uid']:
        return jsonify(ok=False, msg='无权删除此试卷'), 403
    if not is_admin and paper['status'] != 'finished':
        return jsonify(ok=False, msg='只能删除已完成的试卷（进行中请交卷或等限时结束）'), 400
    # 清 task_record 悬空引用
    execute("UPDATE task_record SET paper_id=NULL WHERE paper_id=%s", (pid,))
    execute("DELETE FROM exam_paper WHERE id=%s", (pid,))
    return jsonify(ok=True)


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
        questions=q("SELECT COUNT(*) c FROM question WHERE is_deleted=0",
                    one=True)['c'],
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
    qtype_dist = q("SELECT qtype, COUNT(*) c FROM question "
                   "WHERE is_deleted=0 GROUP BY qtype")
    # 题库分类分布（加分项①自动归类结果）
    cat_dist = q(
        "SELECT c.name, COUNT(q.id) cnt FROM category c "
        "LEFT JOIN question q ON q.category_id = c.id AND q.is_deleted=0 "
        "GROUP BY c.id, c.name ORDER BY cnt DESC")
    cat_total = sum(r['cnt'] for r in cat_dist) or 1
    # 各用户记录数（供"按用户删除"使用）
    user_rows = q(
        "SELECT u.id, u.username, u.real_name, "
        "(SELECT COUNT(*) FROM exam_paper ep WHERE ep.user_id=u.id) papers, "
        "(SELECT COUNT(*) FROM practice p WHERE p.user_id=u.id) practices, "
        "(SELECT COUNT(*) FROM wrong_book wb WHERE wb.user_id=u.id) wrongs "
        "FROM `user` u ORDER BY u.id")
    # ---- 图表数据 ----
    import json
    # 近14天考试/练习趋势
    trend = q(
        "SELECT d.dt, "
        "(SELECT COUNT(*) FROM exam_paper ep WHERE DATE(ep.submitted_at)=d.dt "
        " AND ep.status='finished') papers, "
        "(SELECT COUNT(*) FROM practice p WHERE DATE(p.practiced_at)=d.dt) practices "
        "FROM (SELECT DATE_SUB(CURDATE(), INTERVAL n DAY) dt FROM "
        "(SELECT 0 n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 "
        "UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9 "
        "UNION SELECT 10 UNION SELECT 11 UNION SELECT 12 UNION SELECT 13) t) d "
        "ORDER BY d.dt")
    # 整体答题正确率（仪表盘）
    acc = q("SELECT COALESCE(SUM(is_correct),0) ok, COUNT(*) total FROM exam_detail",
            one=True)
    acc_total = int(acc['total'] or 0)
    acc_rate = round(float(acc['ok']) * 100.0 / acc_total, 1) if acc_total else 0
    # 用户活跃 TOP8（柱状图）
    active = [{'name': (r['real_name'] or r['username']),
               'papers': r['papers'], 'practices': r['practices']}
              for r in sorted(user_rows, key=lambda x: x['papers'] + x['practices'],
                              reverse=True)[:8]]
    charts = dict(
        trend=json.dumps({
            'dates': [str(r['dt']) for r in trend],
            'papers': [r['papers'] for r in trend],
            'practices': [r['practices'] for r in trend],
        }, ensure_ascii=False),
        qtype=json.dumps([
            {'name': QTYPE_NAME.get(r['qtype'], r['qtype']), 'value': r['c']}
            for r in qtype_dist if r['qtype'] in QTYPE_NAME], ensure_ascii=False),
        cats=json.dumps([
            {'name': r['name'], 'value': r['cnt']}
            for r in cat_dist if r['cnt'] > 0][:10], ensure_ascii=False),
        active=json.dumps(active, ensure_ascii=False),
        gauge=acc_rate,
    )
    return render_template('admin_stats.html', overview=overview,
                           by_question=by_question, qtype_dist=qtype_dist,
                           cat_dist=cat_dist, cat_total=cat_total,
                           user_rows=user_rows, charts=charts)


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
    qtype_f = request.args.get('qtype', '').strip()
    cat_f = request.args.get('cat', '').strip()
    del_f = request.args.get('del', '').strip()   # 1=查看回收站（已删题）
    where, params = ("WHERE is_deleted=1" if del_f == '1' else "WHERE is_deleted=0"), []
    if search:
        # C21 支持题干关键词与题号两种搜索方式
        if search.isdigit():
            where += " AND (stem LIKE %s OR id=%s)"
            params.extend([f'%{search}%', int(search)])
        else:
            where += " AND stem LIKE %s"
            params.append(f'%{search}%')
    if qtype_f in ('judge', 'single', 'multi'):
        where += " AND qtype=%s"
        params.append(qtype_f)
    if cat_f == '0':
        where += " AND category_id IS NULL"
    elif cat_f.isdigit():
        where += " AND category_id=%s"
        params.append(int(cat_f))
    total = q(f"SELECT COUNT(*) c FROM question {where}", params, one=True)['c']
    pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    rows = q(f"SELECT id, LEFT(stem, 50) stem_short, qtype, explanation, "
             f"(SELECT COUNT(*) FROM question_image qi "
             f"WHERE qi.question_id=question.id) img_n "
             f"FROM question {where} ORDER BY id LIMIT %s OFFSET %s",
             params + [per_page, offset])
    # 筛选下拉选项
    cat_options = _cat_options()
    # C20 待审核纠错上报（置顶展示：完整计数 + 最新 20 条）
    pending_total = q("SELECT COUNT(*) c FROM question_report "
                      "WHERE status='pending'", one=True)['c']
    reports = q(
        "SELECT r.id, r.reason, r.created_at, u.username, u.real_name, "
        "q.id AS qid, LEFT(q.stem, 40) stem_short FROM question_report r "
        "JOIN `user` u ON u.id=r.uid JOIN question q ON q.id=r.question_id "
        "WHERE r.status='pending' ORDER BY r.id DESC LIMIT 20")
    return render_template('admin_questions.html', rows=rows, page=page,
                           pages=pages, search=search, total=total,
                           qtype_f=qtype_f, cat_f=cat_f, del_f=del_f,
                           cat_options=cat_options, reports=reports,
                           pending_total=pending_total)


@app.route('/admin/questions/report/<int:rid>/resolve', methods=['POST'])
@login_required
def admin_report_resolve(rid):
    """C20 标记纠错上报为已处理，并给举报人发回执通知"""
    me, err = _admin_or_back()
    if err:
        return err
    r = q("SELECT r.uid, r.question_id FROM question_report r WHERE r.id=%s",
          (rid,), one=True)
    execute("UPDATE question_report SET status='resolved' WHERE id=%s", (rid,))
    if r:
        _notify(r['uid'], f'你举报的题目 #{r["question_id"]} 已核实处理，'
                          f'感谢纠错，题库因你更准确！',
                url_for('wrongbook'))
    flash(f'纠错上报 #{rid} 已标记处理完成（已通知举报人）', 'success')
    return redirect(url_for('admin_questions'))


@app.route('/admin/questions/<int:qid>/explanation', methods=['POST'])
@login_required
def admin_save_explanation(qid):
    me, err = _admin_or_back()
    if err:
        return err
    text = request.form.get('explanation', '').strip()
    execute("UPDATE question SET explanation=%s WHERE id=%s", (text or None, qid))
    return jsonify(ok=True)


# ---------------- 题目维护：新增/编辑/删除（软删除）----------------
def _cat_options():
    """分类下拉选项（父/子级联展示名）"""
    cats = q("SELECT id, name, parent_id FROM category ORDER BY parent_id, id")
    name_map = {c['id']: c['name'] for c in cats}
    return [{'id': c['id'],
             'label': (name_map[c['parent_id']] + ' / ' if c['parent_id'] else '')
                      + c['name']}
            for c in cats]


def _validate_question_form():
    """解析并校验题目表单。返回 (data, err_msg)。
    data.options 为 [(label, content, is_correct)]，label 由服务端按提交顺序重排。"""
    stem = (request.form.get('stem') or '').strip()
    qtype = request.form.get('qtype', '')
    explanation = (request.form.get('explanation') or '').strip() or None
    cat_raw = request.form.get('category_id', '')
    category_id = int(cat_raw) if cat_raw.isdigit() and cat_raw != '0' else None

    if not stem:
        return None, '题干不能为空'
    if qtype not in QTYPE_NAME:
        return None, '题型无效'
    if category_id is not None and not q(
            "SELECT id FROM category WHERE id=%s", (category_id,), one=True):
        return None, '所选分类不存在'

    contents = request.form.getlist('opt_content')
    corrects = {int(c) for c in request.form.getlist('opt_correct') if c.isdigit()}
    options = []
    for i, content in enumerate(contents):
        content = (content or '').strip()
        if not content:
            return None, f'第 {i + 1} 个选项内容不能为空'
        options.append((chr(ord('A') + i), content, i in corrects))

    n_opts, n_correct = len(options), len(corrects & set(range(n_opts)))
    if qtype == 'judge':
        if n_opts != 2:
            return None, '判断题必须恰好两个选项（正确/错误）'
        if n_correct != 1:
            return None, '判断题必须勾选一个正确答案'
    else:
        if not (2 <= n_opts <= 6):
            return None, '选项数量须为 2-6 个'
        if qtype == 'single' and n_correct != 1:
            return None, '单选题必须恰好一个正确答案'
        if qtype == 'multi' and n_correct < 2:
            return None, '多选题正确答案至少两个'
    return dict(stem=stem, qtype=qtype, category_id=category_id,
                explanation=explanation, options=options), None


def _question_form_context(qid=None):
    """新增/编辑页共用数据：题目（编辑时含选项/配图）、分类选项、作答次数警示"""
    cats = _cat_options()
    if qid is None:
        blank = dict(id=None, stem='', qtype='single', category_id=None,
                     explanation='', is_deleted=0)
        return render_template('admin_question_form.html', q=blank,
                               options=[('', '', False)] * 4, images=[],
                               answered=0, cats=cats)
    row = q("SELECT * FROM question WHERE id=%s", (qid,), one=True)
    if not row:
        abort(404)
    opts = q("SELECT label, content, is_correct FROM `option` "
             "WHERE question_id=%s ORDER BY label", (qid,))
    imgs = q("SELECT qi.image_id, qi.position, i.mime_type FROM question_image qi "
             "JOIN image i ON i.id=qi.image_id "
             "WHERE qi.question_id=%s ORDER BY qi.position, qi.image_id", (qid,))
    answered = q("SELECT COUNT(*) c FROM exam_detail WHERE question_id=%s "
                 "AND is_correct IS NOT NULL", (qid,), one=True)['c'] \
        + q("SELECT COUNT(*) c FROM practice WHERE question_id=%s",
            (qid,), one=True)['c']
    return render_template('admin_question_form.html', q=row,
                           options=[(o['label'], o['content'],
                                     bool(o['is_correct'])) for o in opts],
                           images=imgs, answered=answered, cats=cats)


def _save_question_options(qid, options):
    """选项全删全插（label 按服务端重排结果落库，历史判分已固化不受影响）"""
    execute("DELETE FROM `option` WHERE question_id=%s", (qid,))
    for label, content, ok in options:
        execute("INSERT INTO `option` (question_id, label, content, is_correct) "
                "VALUES (%s, %s, %s, %s)", (qid, label, content, int(ok)))


@app.route('/admin/question/new')
@login_required
def admin_question_new():
    me, err = _admin_or_back()
    if err:
        return err
    return _question_form_context()


@app.route('/admin/question/create', methods=['POST'])
@login_required
def admin_question_create():
    me, err = _admin_or_back()
    if err:
        return err
    data, verr = _validate_question_form()
    if verr:
        flash(verr, 'danger')
        return redirect(url_for('admin_question_new'))
    with _tx():
        qid = execute(
            "INSERT INTO question (stem, qtype, category_id, explanation, "
            "year_version) VALUES (%s, %s, %s, %s, '2026')",
            (data['stem'], data['qtype'], data['category_id'],
             data['explanation']))
        _save_question_options(qid, data['options'])
    flash(f'已新增题目 #{qid}', 'success')
    return redirect(url_for('admin_question_edit', qid=qid))


@app.route('/admin/question/<int:qid>/edit')
@login_required
def admin_question_edit(qid):
    me, err = _admin_or_back()
    if err:
        return err
    return _question_form_context(qid)


@app.route('/admin/question/<int:qid>/update', methods=['POST'])
@login_required
def admin_question_update(qid):
    me, err = _admin_or_back()
    if err:
        return err
    if not q("SELECT id FROM question WHERE id=%s", (qid,), one=True):
        abort(404)
    data, verr = _validate_question_form()
    if verr:
        flash(verr, 'danger')
        return redirect(url_for('admin_question_edit', qid=qid))
    with _tx():
        execute("UPDATE question SET stem=%s, qtype=%s, category_id=%s, "
                "explanation=%s WHERE id=%s",
                (data['stem'], data['qtype'], data['category_id'],
                 data['explanation'], qid))
        _save_question_options(qid, data['options'])
    flash(f'题目 #{qid} 已更新', 'success')
    return redirect(url_for('admin_questions'))


@app.route('/admin/question/<int:qid>/delete', methods=['POST'])
@login_required
def admin_question_delete(qid):
    """软删除：不出现在抽题/列表/统计，历史成绩与进行中对局不受影响"""
    me, err = _admin_or_back()
    if err:
        return err
    row = q("SELECT id, is_deleted FROM question WHERE id=%s", (qid,), one=True)
    if not row:
        abort(404)
    if row['is_deleted']:
        flash('该题已在回收站中', 'info')
    else:
        execute("UPDATE question SET is_deleted=1 WHERE id=%s", (qid,))
        flash(f'题目 #{qid} 已删除（可从回收站恢复；历史成绩与进行中对局不受影响）',
              'success')
    return redirect(url_for('admin_questions'))


@app.route('/admin/question/<int:qid>/restore', methods=['POST'])
@login_required
def admin_question_restore(qid):
    """从回收站恢复已删题"""
    me, err = _admin_or_back()
    if err:
        return err
    execute("UPDATE question SET is_deleted=0 WHERE id=%s "
            "AND is_deleted=1", (qid,))
    flash(f'题目 #{qid} 已恢复', 'success')
    return redirect(url_for('admin_questions', **{'del': 1})
                    if request.form.get('from_trash') else url_for('admin_questions'))


# ---------------- 题目维护：配图管理 ----------------
ALLOWED_IMG_MIME = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
MAX_IMG_SIZE = 2 * 1024 * 1024   # 单张 2MB


def _q_images(qid):
    """题目配图列表（含引用计数，供前端渲染与 ref_count 维护）"""
    return q("SELECT qi.image_id, qi.position, i.mime_type, i.ref_count "
             "FROM question_image qi JOIN image i ON i.id=qi.image_id "
             "WHERE qi.question_id=%s ORDER BY qi.position, qi.image_id", (qid,))


@app.route('/admin/question/<int:qid>/images', methods=['POST'])
@login_required
def admin_question_images(qid):
    """AJAX 上传配图（可多选）：SHA-256 内容哈希去重，命中复用 image 行；
    question_image 关联落库并维护 ref_count 引用计数"""
    me, err = _admin_or_back()
    if err:
        return jsonify(ok=False, msg='仅管理员可操作'), 403
    if not q("SELECT id FROM question WHERE id=%s", (qid,), one=True):
        return jsonify(ok=False, msg='题目不存在'), 404

    saved = []
    for f in request.files.getlist('file'):
        data = f.read()
        if not data:
            continue
        if f.mimetype not in ALLOWED_IMG_MIME:
            return jsonify(ok=False,
                           msg=f'不支持的图片格式：{f.filename}（{f.mimetype}）'), 400
        if len(data) > MAX_IMG_SIZE:
            return jsonify(ok=False, msg=f'图片超过 2MB：{f.filename}'), 400
        h = hashlib.sha256(data).hexdigest()
        row = q("SELECT id FROM image WHERE content_hash=%s", (h,), one=True)
        img_id = row['id'] if row else execute(
            "INSERT INTO image (content_hash, mime_type, file_size, data, ref_count) "
            "VALUES (%s, %s, %s, %s, 0)", (h, f.mimetype, len(data), data))
        # 建关联（同题同图不重复关联）；每新增一处引用 ref_count+1
        already = q("SELECT 1 ok FROM question_image WHERE question_id=%s "
                    "AND image_id=%s", (qid, img_id), one=True)
        if already:
            continue
        pos = q("SELECT COALESCE(MAX(position), -1) + 1 p FROM question_image "
                "WHERE question_id=%s", (qid,), one=True)['p']
        with _tx():
            execute("INSERT INTO question_image (question_id, image_id, position) "
                    "VALUES (%s, %s, %s)", (qid, img_id, pos))
            execute("UPDATE image SET ref_count=ref_count+1 WHERE id=%s", (img_id,))
        saved.append({'image_id': img_id, 'position': pos})
    if not saved:
        return jsonify(ok=False, msg='未选择图片（或所选图已在本题配图中）'), 400
    return jsonify(ok=True, images=saved)


@app.route('/admin/question/<int:qid>/image/<int:image_id>/delete', methods=['POST'])
@login_required
def admin_question_image_delete(qid, image_id):
    """删除本题某配图：解除关联并 ref_count-1；引用归零清理物理 BLOB"""
    me, err = _admin_or_back()
    if err:
        return jsonify(ok=False, msg='仅管理员可操作'), 403
    n = q("SELECT COUNT(*) c FROM question_image WHERE question_id=%s "
          "AND image_id=%s", (qid, image_id), one=True)['c']
    if not n:
        return jsonify(ok=False, msg='关联不存在'), 404
    with _tx():
        execute("DELETE FROM question_image WHERE question_id=%s AND image_id=%s",
                (qid, image_id))
        execute("UPDATE image SET ref_count=ref_count-1 WHERE id=%s", (image_id,))
        if q("SELECT ref_count c FROM image WHERE id=%s",
             (image_id,), one=True)['c'] <= 0:
            execute("DELETE FROM image WHERE id=%s AND ref_count<=0", (image_id,))
    return jsonify(ok=True)


@app.route('/admin/question/<int:qid>/image/<int:image_id>/move', methods=['POST'])
@login_required
def admin_question_image_move(qid, image_id):
    """配图排序：dir=up/down 与相邻图交换 position"""
    me, err = _admin_or_back()
    if err:
        return jsonify(ok=False, msg='仅管理员可操作'), 403
    ids = [r['image_id'] for r in _q_images(qid)]
    if image_id not in ids:
        return jsonify(ok=False, msg='关联不存在'), 404
    i = ids.index(image_id)
    j = i - 1 if request.form.get('dir') == 'up' else i + 1
    if 0 <= j < len(ids):
        other = ids[j]
        # 三步交换避开 (question_id, image_id) 无冲突写：临时值 127 在 TINYINT 范围内
        execute("UPDATE question_image SET position=127 "
                "WHERE question_id=%s AND image_id=%s", (qid, image_id))
        execute("UPDATE question_image SET position=%s "
                "WHERE question_id=%s AND image_id=%s", (i, qid, other))
        execute("UPDATE question_image SET position=%s "
                "WHERE question_id=%s AND image_id=%s", (j, qid, image_id))
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


# ---------------- 功能⑧：注册用户管理 ----------------
@app.route('/admin/users')
@login_required
def admin_users():
    """用户管理：账号列表 + 改名 / 重置密码 / 删除"""
    me, back = _admin_or_back()
    if back:
        return back
    rows = q(
        "SELECT u.id, u.username, u.real_name, u.role, "
        "u.pk_wins, u.pk_losses, u.win_streak, u.created_at, "
        "COALESCE(p.total_answered, 0) AS answered, "
        "COALESCE(p.total_correct, 0) AS correct_n "
        "FROM `user` u LEFT JOIN ("
        "  SELECT user_id, COUNT(*) AS total_answered, SUM(is_correct) AS total_correct "
        "  FROM practice GROUP BY user_id"
        ") p ON p.user_id = u.id "
        "ORDER BY u.role DESC, u.id")
    return render_template('admin_users.html', users=rows)


def _target_student(uid):
    """取待管理的学生；用户不存在或为管理员账号时返回 None（已 flash 提示）"""
    u = q("SELECT id, username, role FROM `user` WHERE id=%s", (uid,), one=True)
    if not u:
        flash('用户不存在', 'danger')
    elif u['role'] != 'student':
        flash('管理员账号不可在此操作', 'danger')
    else:
        return u
    return None


@app.route('/admin/users/<int:uid>/delete', methods=['POST'])
@login_required
def admin_user_delete(uid):
    """删除学生账号及其全部数据（考试/练习/错题/任务记录/PK对战）"""
    me, back = _admin_or_back()
    if back:
        return back
    u = _target_student(uid)
    if not u:
        return redirect(url_for('admin_users'))
    # 有外键引用的数据按序删除：task_record（引用试卷）→ 试卷（明细随外键级联）→ 其余
    execute("DELETE FROM task_record WHERE uid=%s", (uid,))
    execute("DELETE FROM exam_paper WHERE user_id=%s", (uid,))
    execute("DELETE FROM practice WHERE user_id=%s", (uid,))
    execute("DELETE FROM wrong_book WHERE user_id=%s", (uid,))
    execute("DELETE FROM pk_challenge WHERE challenger_uid=%s OR opponent_uid=%s",
            (uid, uid))
    execute("DELETE FROM `user` WHERE id=%s", (uid,))
    # 清理内存中该用户所在的 PK 房间并通知对手
    for key, room in list(PK_ROOMS.items()):
        if uid in (room['challenger'], room['opponent']):
            socketio.emit('room_closed', room=key)
            PK_ROOMS.pop(key, None)
    flash(f"已删除用户 {u['username']} 及其全部答题与对战数据", 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:uid>/reset_password', methods=['POST'])
@login_required
def admin_user_reset_password(uid):
    """重置学生密码为 123456 并踢下线（清 login_token 强制重新登录）"""
    me, back = _admin_or_back()
    if back:
        return back
    u = _target_student(uid)
    if not u:
        return redirect(url_for('admin_users'))
    execute("UPDATE `user` SET password_hash=%s, login_token=NULL WHERE id=%s",
            (sha256('123456'), uid))
    flash(f"用户 {u['username']} 的密码已重置为 123456", 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:uid>/rename', methods=['POST'])
@login_required
def admin_user_rename(uid):
    """更正学生姓名（注册时填写的真实姓名）"""
    me, back = _admin_or_back()
    if back:
        return back
    u = _target_student(uid)
    if not u:
        return redirect(url_for('admin_users'))
    real_name = request.form.get('real_name', '').strip()
    if not real_name:
        flash('姓名不能为空', 'danger')
    else:
        execute("UPDATE `user` SET real_name=%s WHERE id=%s", (real_name, uid))
        flash(f"用户 {u['username']} 的姓名已更新为 {real_name}", 'success')
    return redirect(url_for('admin_users'))


# ---------------- 功能①：教师发布任务 ----------------
def _generate_questions_for_task(judge_count, single_count):
    """教师发布时调用：分别抽判断题和单选题，合并存 question_ids
    判断题在前，单选题在后（打乱时各区内打乱，不混在一起）"""
    judges = [r['id'] for r in
              q(f"SELECT id FROM question WHERE qtype='judge' AND is_deleted=0 "
                f"ORDER BY RAND() LIMIT {int(judge_count)}")]
    singles = [r['id'] for r in
               q(f"SELECT id FROM question WHERE qtype='single' AND is_deleted=0 "
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
        "t.target_uids, t.status, t.created_at, t.closed_at, "
        "(SELECT COUNT(*) FROM task_record tr WHERE tr.task_id=t.id) AS joined, "
        "(SELECT COUNT(*) FROM task_record tr WHERE tr.task_id=t.id "
        "  AND tr.status='completed') AS finished "
        "FROM task t ORDER BY t.id DESC")
    students = q("SELECT id, username, real_name FROM `user` "
                 "WHERE role='student' ORDER BY id")
    student_ids = {s['id'] for s in students}
    for r in rows:
        # 解析名单：空=全员；仅保留仍存在的学生 id
        raw = [x for x in (r['target_uids'] or '').split(',') if x]
        targets = [int(x) for x in raw if x.isdigit() and int(x) in student_ids]
        r['target_all'] = not targets
        r['target_list'] = targets
    return render_template('admin_tasks.html', rows=rows, students=students)


def _form_target_uids():
    """从发布/编辑表单读取接收名单：(target_uids 逗号串或 None=全员, 错误消息或 None)"""
    if request.form.get('target_scope', 'all') == 'all':
        return None, None
    valid = {r['id'] for r in q("SELECT id FROM `user` WHERE role='student'")}
    uids = [int(x) for x in request.form.getlist('target_uids') if x.isdigit()]
    uids = [u for u in uids if u in valid]
    if not uids:
        return None, '请至少勾选一名接收学生（或选择"全体学生"）'
    return ','.join(map(str, uids)), None


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

    # C22/定向：读取接收名单（None=全员，逗号串=指定学生）
    target_uids, terr = _form_target_uids()
    if terr:
        flash(terr, 'danger')
        return redirect(url_for('admin_tasks'))

    execute(
        "INSERT INTO task (title, creator_uid, judge_count, single_count, "
        "time_limit_sec, mode, purpose, question_ids, shuffle_order, "
        "target_uids, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'published')",
        (title, session['uid'], judge_count, single_count, time_limit_sec,
         mode, purpose, ','.join(map(str, qids)), int(shuffle_order),
         target_uids))
    # C22 站内通知：推送给接收名单内的学生（NULL=全体学生）
    if target_uids:
        dest = [u for u in target_uids.split(',') if u]
    else:
        dest = None
    for s in q("SELECT id FROM `user` WHERE role='student'"):
        if dest is not None and str(s['id']) not in dest:
            continue
        _notify(s['id'], f'新任务「{title}」已发布，共 {judge_count + single_count} 题',
                url_for('my_tasks_page'))
    flash(f'任务「{title}」已发布（判断 {judge_count} + 单选 {single_count} = '
          f'{judge_count + single_count} 题）', 'success')
    return redirect(url_for('admin_tasks'))


@app.route('/admin/tasks/<int:tid>/records')
@login_required
def admin_task_records(tid):
    """教师查看任务成绩明细：分数/用时/切屏次数与离开秒数，可点进原卷讲评"""
    me, err = _admin_or_back()
    if err:
        return err
    task = q("SELECT * FROM task WHERE id=%s", (tid,), one=True)
    if not task:
        abort(404)
    records = q(
        "SELECT u.id AS uid, u.real_name, u.username, tr.status, tr.paper_id, "
        "tr.start_time, tr.submit_time, tr.paused, "
        "p.score, p.total_count, p.switch_count, p.blur_sec, "
        "COALESCE(NULLIF(tr.elapsed_sec, 0), "
        "  TIMESTAMPDIFF(SECOND, tr.start_time, tr.submit_time)) AS used_sec "
        "FROM task_record tr JOIN `user` u ON u.id=tr.uid "
        "LEFT JOIN exam_paper p ON p.id=tr.paper_id "
        "WHERE tr.task_id=%s "
        "ORDER BY (tr.status='completed') DESC, p.score DESC, u.id", (tid,))
    return render_template('admin_task_records.html', task=task, records=records)


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

    target_uids, err = _form_target_uids()
    if err:
        flash(err, 'danger')
        return redirect(url_for('admin_tasks'))

    execute("UPDATE task SET title=%s, time_limit_sec=%s, target_uids=%s "
            "WHERE id=%s", (title, time_limit_sec, target_uids, tid))
    flash(f'任务 #{tid} 已更新（标题/时限/接收名单）', 'success')
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
    # 定向任务校验：不在接收名单内禁止进入（防手敲 URL）
    targets = [x for x in (task['target_uids'] or '').split(',') if x]
    if targets and str(uid) not in targets:
        flash('该任务未向你发布', 'danger')
        return redirect(url_for('index'))

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
        # 题型错次分布（按任务）
        qtype_dist = q(
            "SELECT q.qtype, COUNT(*) AS cnt FROM exam_detail ed "
            "JOIN question q ON q.id=ed.question_id "
            "JOIN task_record tr ON tr.paper_id=ed.paper_id "
            "WHERE tr.task_id=%s AND ed.is_correct=0 GROUP BY q.qtype",
            (task_filter,))
        # 错误答案分布
        ans_dist = q(
            "SELECT ed.user_answer AS ans, COUNT(*) AS cnt FROM exam_detail ed "
            "JOIN task_record tr ON tr.paper_id=ed.paper_id "
            "WHERE tr.task_id=%s AND ed.is_correct=0 AND ed.user_answer IS NOT NULL "
            "GROUP BY ed.user_answer ORDER BY cnt DESC LIMIT 8",
            (task_filter,))
        # 错误率区间分布
        rate_dist = q(
            "SELECT CASE "
            "WHEN COUNT(*)*100.0/GREATEST(COUNT(DISTINCT ed.paper_id),1) >= 70 THEN '高错误率(≥70%%)' "
            "WHEN COUNT(*)*100.0/GREATEST(COUNT(DISTINCT ed.paper_id),1) >= 40 THEN '中错误率(40-70%%)' "
            "ELSE '低错误率(<40%%)' END AS bucket, COUNT(*) AS cnt "
            "FROM exam_detail ed JOIN question q ON q.id=ed.question_id "
            "JOIN task_record tr ON tr.paper_id=ed.paper_id "
            "WHERE tr.task_id=%s AND ed.is_correct=0 GROUP BY q.id",
            (task_filter,))
    else:
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
        qtype_dist = q(
            "SELECT q.qtype, COUNT(*) AS cnt FROM exam_detail ed "
            "JOIN question q ON q.id=ed.question_id "
            "WHERE ed.is_correct=0 GROUP BY q.qtype")
        ans_dist = q(
            "SELECT user_answer AS ans, COUNT(*) AS cnt FROM exam_detail "
            "WHERE is_correct=0 AND user_answer IS NOT NULL "
            "GROUP BY user_answer ORDER BY cnt DESC LIMIT 8")
        rate_dist = q(
            "SELECT bucket, COUNT(*) AS cnt FROM ( "
            "SELECT CASE "
            "WHEN COUNT(*)*100.0/GREATEST(COUNT(DISTINCT ed.paper_id),1) >= 70 THEN '高错误率(≥70%%)' "
            "WHEN COUNT(*)*100.0/GREATEST(COUNT(DISTINCT ed.paper_id),1) >= 40 THEN '中错误率(40-70%%)' "
            "ELSE '低错误率(<40%%)' END AS bucket "
            "FROM exam_detail ed JOIN question q ON q.id=ed.question_id "
            "WHERE ed.is_correct=0 GROUP BY q.id) t GROUP BY bucket")

    import json
    charts = dict(
        top10=json.dumps([
            {'name': (r['stem_short'] or '')[:18], 'rate': float(r['wrong_rate'] or 0),
             'cnt': r['wrong_count']} for r in rows[:10]], ensure_ascii=False),
        qtype=json.dumps([
            {'name': QTYPE_NAME.get(r['qtype'], r['qtype']), 'value': r['cnt']}
            for r in qtype_dist], ensure_ascii=False),
        answers=json.dumps([
            {'name': r['ans'] or '未作答', 'value': r['cnt']} for r in ans_dist],
            ensure_ascii=False),
        rates=json.dumps([
            {'name': r['bucket'], 'value': r['cnt']} for r in rate_dist],
            ensure_ascii=False),
    )

    return render_template('admin_wrong_rank.html', rows=rows, tasks=tasks,
                           task_filter=task_filter, charts=charts)


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


def _in_clause(ids):
    """把 ID 列表转成 (sql_fragment, params_tuple) 用于 WHERE col IN (...)"""
    if not ids:
        return None, ()
    placeholders = ','.join(['%s'] * len(ids))
    return f"IN ({placeholders})", tuple(ids)


def _build_export_data(scenario, task_ids, student_ids, fmt):
    """根据场景构建导出数据，返回 (headers, rows, filename)"""

    if scenario == 'review':
        # 1. 课堂讲评：按题号，含错误率+错的人名
        task_clause, task_params = _in_clause(task_ids)
        sid_clause, sid_params = _in_clause(student_ids)
        extra_where = ["ed.is_correct=0"]
        params = []
        if task_clause:
            extra_where.append(f"tr.task_id {task_clause}")
            params.extend(task_params)
        if sid_clause:
            extra_where.append(f"ep.user_id {sid_clause}")
            params.extend(sid_params)
        where_sql = " AND ".join(extra_where)
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
            f"WHERE {where_sql} "
            "GROUP BY q.id ORDER BY q.id", tuple(params))
        headers = ['题号', '题干', '题型', '正确答案', '错次', '错误率(%)', '常见错误答案', '错的学生']
        rows = [(r['id'], r['stem'], r['qtype'], r['correct_answer'],
                 r['wrong_count'], r['wrong_rate'], r['common_wrong'],
                 r['wrong_students']) for r in rows_data]
        return headers, rows, '课堂讲评'

    elif scenario == 'scores':
        # 2. 成绩公告：排名、姓名、分数、通过/未通过、用时
        task_clause, task_params = _in_clause(task_ids)
        sid_clause, sid_params = _in_clause(student_ids)
        params = []
        where_parts = ["ep.status='finished'"]
        join_task = False
        join_student = False
        if task_clause:
            where_parts.append(f"tr.task_id {task_clause}")
            params.extend(task_params)
            join_task = True
        if sid_clause:
            where_parts.append(f"ep.user_id {sid_clause}")
            params.extend(sid_params)
            join_student = True
        tr_join = "JOIN task_record tr ON tr.paper_id=ep.id" if join_task or task_ids else "LEFT JOIN task_record tr ON tr.paper_id=ep.id"
        rows_data = q(
            "SELECT u.id, u.username, u.real_name, ep.score, ep.total_count, "
            "tr.elapsed_sec, ep.switch_count, ep.blur_sec, "
            "CASE WHEN ep.score>=90 THEN '通过' ELSE '未通过' END AS pass, "
            "ep.submitted_at "
            f"FROM exam_paper ep JOIN `user` u ON u.id=ep.user_id {tr_join} "
            f"WHERE {' AND '.join(where_parts)} "
            "ORDER BY ep.score DESC", tuple(params))
        headers = ['排名', '学号', '账号', '姓名', '分数', '题数', '通过状态',
                   '用时(秒)', '切屏次数', '离开秒数', '提交时间']
        rows = [(i+1, r['id'], r['username'], r['real_name'] or '', r['score'],
                 r['total_count'], r['pass'], r['elapsed_sec'] or '',
                 r['switch_count'] or 0, r['blur_sec'] or 0,
                 str(r['submitted_at']) if r['submitted_at'] else '')
                for i, r in enumerate(rows_data)]
        return headers, rows, '成绩公告'

    elif scenario == 'tutor':
        # 3. 个别辅导：学生答题明细（支持多学生）
        task_clause, task_params = _in_clause(task_ids)
        sid_clause, sid_params = _in_clause(student_ids)
        params = []
        where_parts = []
        join_task = False
        if task_clause:
            where_parts.append(f"tr.task_id {task_clause}")
            params.extend(task_params)
            join_task = True
        if sid_clause:
            where_parts.append(f"ep.user_id {sid_clause}")
            params.extend(sid_params)
        tr_join = "JOIN task_record tr ON tr.paper_id=ep.id" if join_task else "LEFT JOIN task_record tr ON tr.paper_id=ep.id"
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows_data = q(
            "SELECT q.id, LEFT(q.stem,80) AS stem, q.qtype, ed.user_answer, "
            "(SELECT GROUP_CONCAT(o.label SEPARATOR '') FROM `option` o "
            "WHERE o.question_id=q.id AND o.is_correct=1) AS correct_answer, "
            "ed.is_correct, tr.elapsed_sec, u.username "
            f"FROM exam_detail ed JOIN exam_paper ep ON ep.id=ed.paper_id "
            f"JOIN question q ON q.id=ed.question_id "
            f"JOIN `user` u ON u.id=ep.user_id "
            f"{tr_join} {where_sql} ORDER BY u.id, q.id", tuple(params))
        headers = ['账号', '题号', '题干', '题型', '学生答案', '正确答案', '对错', '用时(秒)']
        rows = [(r['username'], r['id'], r['stem'], r['qtype'],
                 r['user_answer'] or '未答', r['correct_answer'],
                 '对' if r['is_correct'] else '错', r['elapsed_sec'] or '')
                for r in rows_data]
        sid_label = '多学生' if (student_ids and len(student_ids) > 1) else ('学生' + (student_ids[0] if student_ids else '全部'))
        return headers, rows, f'{sid_label}_答题明细'

    elif scenario == 'reflect':
        # 4. 教学反思：按分类聚合错误率
        task_clause, task_params = _in_clause(task_ids)
        params = []
        if task_clause:
            rows_data = q(
                "SELECT c.name AS cat_name, COUNT(DISTINCT q.id) AS q_count, "
                "COUNT(ed.id) AS wrong_count, COUNT(DISTINCT ep.id) AS total_attempts, "
                "ROUND(COUNT(ed.id)*100.0/GREATEST(COUNT(DISTINCT ep.id),1),1) AS avg_wrong_rate "
                "FROM question q LEFT JOIN category c ON c.id=q.category_id "
                "LEFT JOIN exam_detail ed ON ed.question_id=q.id AND ed.is_correct=0 "
                "LEFT JOIN exam_paper ep ON ep.id=ed.paper_id "
                "JOIN task_record tr ON tr.paper_id=ep.id "
                f"WHERE tr.task_id {task_clause} "
                "GROUP BY c.id, c.name ORDER BY avg_wrong_rate DESC", tuple(task_params))
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
        task_clause, task_params = _in_clause(task_ids)
        sid_clause, sid_params = _in_clause(student_ids)
        params = []
        where_parts = []
        join_task = False
        if task_clause:
            where_parts.append(f"tr.task_id {task_clause}")
            params.extend(task_params)
            join_task = True
        if sid_clause:
            where_parts.append(f"ep.user_id {sid_clause}")
            params.extend(sid_params)
        tr_join = "JOIN task_record tr ON tr.paper_id=ep.id" if join_task else "LEFT JOIN task_record tr ON tr.paper_id=ep.id"
        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows_data = q(
            "SELECT ep.submitted_at, u.username, u.real_name, t.title, "
            "q.id, LEFT(q.stem,60) stem, q.qtype, ed.user_answer, "
            "(SELECT GROUP_CONCAT(o.label SEPARATOR '') FROM `option` o "
            "WHERE o.question_id=q.id AND o.is_correct=1) AS correct_answer, "
            "ed.is_correct, tr.elapsed_sec "
            f"FROM exam_detail ed JOIN exam_paper ep ON ep.id=ed.paper_id "
            f"JOIN `user` u ON u.id=ep.user_id JOIN question q ON q.id=ed.question_id "
            f"LEFT JOIN task t ON t.id=ep.task_id "
            f"{tr_join} {where_sql} ORDER BY ep.id, ed.seq_no", tuple(params))
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
    """导出 CSV/Excel（支持多选任务/学生）"""
    me, err = _admin_or_back()
    if err:
        return err
    scenario = request.args.get('scenario', 'archive')
    fmt = request.args.get('fmt', 'csv')
    # 多选下拉返回多个同 key 的参数；多选+空选项时需过滤掉空字符串
    task_ids = [x for x in request.args.getlist('task', type=str) if x]
    student_ids = [x for x in request.args.getlist('student', type=str) if x]
    # 多选全部选了的话也不过滤（返回空列表）

    if scenario not in EXPORT_SCENARIOS:
        flash('未知导出场景', 'danger')
        return redirect(url_for('admin_answer_data'))

    headers, rows, name_prefix = _build_export_data(
        scenario, task_ids or None, student_ids or None, fmt)
    filename = name_prefix
    if fmt == 'excel':
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
    """学生排名：考试排名（按平均分）+ 练习排名（按总正确题数）
    上榜门槛：考试至少完成1场；练习至少答对1题。同分并列（1,1,3 奥运式）。"""
    me = current_user()
    # 考试排名：按平均分降序 → 最高分 → 场次（只统计已交卷）
    exam_all = q(
        "SELECT u.id, u.username, u.real_name, "
        "COUNT(ep.id) AS exam_count, "
        "ROUND(AVG(ep.score),1) AS avg_score, "
        "SUM(CASE WHEN ep.score>=90 THEN 1 ELSE 0 END) AS pass_count, "
        "MAX(ep.score) AS best_score "
        "FROM `user` u "
        "LEFT JOIN exam_paper ep ON ep.user_id=u.id AND ep.status='finished' "
        "WHERE u.role='student' "
        "GROUP BY u.id, u.username, u.real_name "
        "ORDER BY avg_score DESC, best_score DESC, exam_count DESC")

    # 练习排名：按总正确题数降序 → 正确率 → 练习题数
    prac_all = q(
        "SELECT u.id, u.username, u.real_name, "
        "COUNT(pr.id) AS practice_count, "
        "SUM(pr.is_correct) AS correct_count, "
        "ROUND(SUM(pr.is_correct)*100.0/GREATEST(COUNT(pr.id),1),1) AS correct_rate "
        "FROM `user` u "
        "LEFT JOIN practice pr ON pr.user_id=u.id "
        "WHERE u.role='student' "
        "GROUP BY u.id, u.username, u.real_name "
        "ORDER BY correct_count DESC, correct_rate DESC, practice_count DESC")

    # 上榜门槛拆分
    exam_rank = [r for r in exam_all if (r['exam_count'] or 0) >= 1]
    exam_unranked = [r for r in exam_all if (r['exam_count'] or 0) < 1]
    prac_rank = [r for r in prac_all if (r['correct_count'] or 0) >= 1]
    prac_unranked = [r for r in prac_all if (r['correct_count'] or 0) < 1]

    # 同分并列名次：按主指标（平均分/正确题数）判定，兜底键只决定展示顺序
    def assign_ties(rows, keyfn):
        last_key, rank = None, 0
        for i, r in enumerate(rows, 1):
            k = keyfn(r)
            if k != last_key:
                rank, last_key = i, k
            r['rank'] = rank
    assign_ties(exam_rank, lambda r: r['avg_score'])
    assign_ties(prac_rank, lambda r: r['correct_count'])

    # 当前用户在两个榜单中的排名（未上榜为 None）
    my_exam_rank = next((r['rank'] for r in exam_rank if r['id'] == me['id']), None)
    my_prac_rank = next((r['rank'] for r in prac_rank if r['id'] == me['id']), None)

    # ---- 图表数据：考试分数段分布 ----
    import json
    seg = q(
        "SELECT bucket, COUNT(*) cnt FROM ( "
        "SELECT CASE WHEN score<90 THEN '不及格(<90)' "
        "WHEN score<95 THEN '及格(90-94)' "
        "WHEN score<100 THEN '优秀(95-99)' "
        "ELSE '满分(100)' END AS bucket, score "
        "FROM exam_paper WHERE status='finished') t GROUP BY bucket")
    seg_order = ['不及格(<90)', '及格(90-94)', '优秀(95-99)', '满分(100)']
    seg_map = {r['bucket']: r['cnt'] for r in seg}
    # PK 战绩饼图：当前登录用户自己的战绩（全局统计 wins==losses 永远 50:50 无意义）
    me_pk = q("SELECT pk_wins, pk_losses FROM `user` WHERE id=%s",
              (me['id'],), one=True)
    charts = dict(
        seg=json.dumps({'names': seg_order,
                        'values': [seg_map.get(k, 0) for k in seg_order]},
                       ensure_ascii=False),
        pk=json.dumps([
            {'name': '获胜场次', 'value': int(me_pk['pk_wins'] or 0)},
            {'name': '失败场次', 'value': int(me_pk['pk_losses'] or 0)},
        ], ensure_ascii=False),
    )

    return render_template('ranking.html',
                           exam_rank=exam_rank, exam_unranked=exam_unranked,
                           practice_rank=prac_rank, prac_unranked=prac_unranked,
                           my_exam_rank=my_exam_rank, my_prac_rank=my_prac_rank,
                           me=me, charts=charts)


# ---------------- 功能⑦：双人 PK 赛车（socketio）----------------
# 内存房间状态：room_key -> dict
# {challenger, opponent, status, questions, current_q, scores, answers, ready, sids}
PK_ROOMS = {}
ONLINE_SIDS = {}   # sid -> uid
PK_QUESTION_COUNT = 10
PK_Q_TIME = 15     # 每题秒数


def _pk_room_key(pid):
    return f'pk_{pid}'


def _pk_emit_state(key, room):
    """向全房间推送双方准备状态（客户端以此为准渲染按钮，可自愈重连丢状态）"""
    players = [
        {'uid': room['challenger'], 'ready': room['challenger'] in room['ready']},
        {'uid': room['opponent'], 'ready': room['opponent'] in room['ready']},
    ]
    emit('room_state', {'players': players, 'status': room['status']}, room=key)
    return players


def _pk_recent_qids(uids, games=3):
    """两名玩家最近 games 场 PK 出过的题（抽题去重用）。
    每人最多 games 场，故取 games*2 行覆盖双方。"""
    recent = set()
    rows = q(
        "SELECT question_ids FROM pk_challenge "
        "WHERE (challenger_uid=%s OR opponent_uid=%s "
        "       OR challenger_uid=%s OR opponent_uid=%s) "
        "ORDER BY id DESC LIMIT %s",
        (uids[0], uids[0], uids[1], uids[1], games * 2))
    for r in rows:
        recent.update(int(x) for x in (r['question_ids'] or '').split(',') if x.strip())
    return recent


def _pk_pick_questions(judge_n, single_n, exclude=None):
    """按题型随机抽题；exclude 中的近期题不抽。
    排除后题池不足时自动放宽为全池随机，保证总能抽满。"""
    exclude = set(exclude or ())

    def _pick(qtype, n):
        if n <= 0:
            return []
        if exclude:
            ex = ','.join(str(i) for i in exclude)
            ids = [r['id'] for r in q(
                f"SELECT id FROM question WHERE qtype=%s AND is_deleted=0 "
                f"AND id NOT IN ({ex}) "
                f"ORDER BY RAND() LIMIT %s", (qtype, n))]
            if len(ids) < n:    # 排除后不够 -> 放宽到全池
                ids = [r['id'] for r in q(
                    f"SELECT id FROM question WHERE qtype=%s AND is_deleted=0 "
                    f"ORDER BY RAND() LIMIT %s", (qtype, n))]
        else:
            ids = [r['id'] for r in q(
                f"SELECT id FROM question WHERE qtype=%s AND is_deleted=0 "
                f"ORDER BY RAND() LIMIT %s", (qtype, n))]
        return ids

    return _pick('judge', judge_n) + _pick('single', single_n)


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
    # 收到的对战邀请（10 分钟内有效，防止陈旧邀请堆积）
    invitations = q(
        "SELECT p.id, uc.username AS c_name, uc.real_name AS c_real, "
        "uc.pk_wins AS c_wins, p.created_at "
        "FROM pk_challenge p JOIN `user` uc ON uc.id=p.challenger_uid "
        "WHERE p.opponent_uid=%s AND p.status='waiting' "
        "AND p.created_at >= NOW() - INTERVAL 10 MINUTE ORDER BY p.id DESC",
        (session['uid'],))
    for inv in invitations:
        inv['created_at'] = str(inv['created_at'])[:19]
    return render_template('pk_lobby.html', students=students,
                           my_stat=my_stat, badge=badge, records=records, me=me,
                           invitations=invitations)


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

    # 题型配置：判断题/单选题数量由发起方指定，总数须等于 10
    judge_n = request.form.get('judge_count', type=int)
    single_n = request.form.get('single_count', type=int)
    if judge_n is None or single_n is None or judge_n < 0 or single_n < 0 \
            or judge_n + single_n != PK_QUESTION_COUNT:
        flash('题型配置无效：判断题 + 单选题数量之和须等于 10', 'danger')
        return redirect(url_for('pk_lobby'))

    # 抽题：随机抽判断题/单选题，避开双方最近 3 局出过的题
    recent = _pk_recent_qids((session['uid'], opponent_id))
    qids = _pk_pick_questions(judge_n, single_n, recent)
    if len(qids) < PK_QUESTION_COUNT:
        flash('题库对应题型数量不足，无法PK', 'danger')
        return redirect(url_for('pk_lobby'))
    random.shuffle(qids)  # 判断/单选交错出场

    # C16 赛道皮肤（发起方选择）
    theme = request.form.get('theme', 'day')
    if theme not in ('day', 'night', 'rain', 'desert'):
        theme = 'day'

    pid = execute(
        "INSERT INTO pk_challenge (challenger_uid, opponent_uid, question_ids, "
        "theme, status) VALUES (%s, %s, %s, %s, 'waiting')",
        (session['uid'], opponent_id, ','.join(map(str, qids)), theme))

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
        'seq': {},       # C15 回放：q_idx -> {uid: answer_label}（含答错与未答）
        'ready': set(),
        'sids': {},
        'watchers': set(),   # C14 观战者 sid 集合
        'theme': theme,
    }
    # C22 站内通知：告知被挑战方
    me_row = q("SELECT username FROM `user` WHERE id=%s", (session['uid'],), one=True)
    _notify(opponent_id, f'{me_row["username"]} 向你发起 PK 挑战，快去应战！',
            url_for('pk_lobby'))
    # 对手在线则实时推送邀请（大厅轮询作兜底）
    for sid, uid in list(ONLINE_SIDS.items()):
        if uid == opponent_id:
            socketio.emit('pk_invited',
                          {'pid': pid, 'from': me_row['username']}, to=sid)
    return redirect(url_for('pk_room', pid=pid))


@app.route('/pk/join_code', methods=['POST'])
@login_required
def pk_join_code():
    """C17 房间码快速加入：code = 100000 + challenge_id；
    非本局玩家输入房间码自动转为观战"""
    code = request.form.get('code', '').strip()
    pid = int(code) - 100000 if code.isdigit() and len(code) == 6 else None
    rec = q("SELECT * FROM pk_challenge WHERE id=%s", (pid,), one=True) \
        if pid and pid > 0 else None
    if not rec or rec['status'] not in ('waiting', 'ready', 'playing'):
        flash('房间码无效或该对局已结束', 'danger')
        return redirect(url_for('pk_lobby'))
    if session['uid'] in (rec['challenger_uid'], rec['opponent_uid']):
        return redirect(url_for('pk_room', pid=pid))
    return redirect(url_for('pk_watch', pid=pid))


@app.route('/pk/invitations')
@login_required
def pk_invitations():
    """大厅轮询：我收到的待接受邀请"""
    rows = q(
        "SELECT p.id, uc.username AS c_name, uc.real_name AS c_real, "
        "p.created_at FROM pk_challenge p "
        "JOIN `user` uc ON uc.id=p.challenger_uid "
        "WHERE p.opponent_uid=%s AND p.status='waiting' "
        "AND p.created_at >= NOW() - INTERVAL 10 MINUTE ORDER BY p.id DESC",
        (session['uid'],))
    for r in rows:
        r['created_at'] = str(r['created_at'])[:19]
    return {'invitations': [dict(r) for r in rows]}


@app.route('/pk/<int:pid>/decline', methods=['POST'])
@login_required
def pk_decline(pid):
    """拒绝邀请"""
    rec = q("SELECT id FROM pk_challenge WHERE id=%s AND opponent_uid=%s "
            "AND status='waiting'", (pid, session['uid']), one=True)
    if rec:
        execute("UPDATE pk_challenge SET status='declined' WHERE id=%s", (pid,))
        key = _pk_room_key(pid)
        room = PK_ROOMS.pop(key, None)
        if room:
            socketio.emit('pk_cancelled', room=key)
        flash('已拒绝该对战邀请', 'warning')
    return redirect(url_for('pk_lobby'))


@app.route('/pk/<int:pid>')
@login_required
def pk_room(pid):
    """PK 房间页（玩家）"""
    rec = _pk_rec(pid)
    if not rec:
        abort(404)
    if session['uid'] not in (rec['challenger_uid'], rec['opponent_uid']):
        flash('你不是本局玩家，可从大厅观战', 'danger')
        return redirect(url_for('pk_lobby'))
    return _render_pk_room(rec, is_watcher=False)


def _pk_rec(pid):
    return q(
        "SELECT p.*, uc.username AS c_name, uc.real_name AS c_real, "
        "uo.username AS o_name, uo.real_name AS o_real, "
        "uc.pk_wins AS c_wins, uo.pk_wins AS o_wins, "
        "uc.win_streak AS c_streak, uo.win_streak AS o_streak "
        "FROM pk_challenge p "
        "JOIN `user` uc ON uc.id=p.challenger_uid "
        "JOIN `user` uo ON uo.id=p.opponent_uid "
        "WHERE p.id=%s", (pid,), one=True)


def _render_pk_room(rec, is_watcher):
    """渲染 PK 房间页（玩家与观战者共用模板）"""
    pid = rec['id']
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

    if is_watcher:
        my_name, opp_name = rec['c_name'], rec['o_name']
        my_wins, opp_wins = rec['c_wins'], rec['o_wins']
        my_streak, opp_streak = rec['c_streak'], rec['o_streak']
    else:
        my_role = 'challenger' if session['uid'] == rec['challenger_uid'] else 'opponent'
        my_name = rec['c_name'] if my_role == 'challenger' else rec['o_name']
        opp_name = rec['o_name'] if my_role == 'challenger' else rec['c_name']
        my_wins = rec['c_wins'] if my_role == 'challenger' else rec['o_wins']
        opp_wins = rec['o_wins'] if my_role == 'challenger' else rec['c_wins']
        my_streak = rec['c_streak'] if my_role == 'challenger' else rec['o_streak']
        opp_streak = rec['o_streak'] if my_role == 'challenger' else rec['c_streak']

    return render_template('pk_room.html', pid=pid, rec=rec, questions=qs,
                           my_role='watcher' if is_watcher else my_role,
                           my_name=my_name, opp_name=opp_name,
                           my_wins=my_wins, opp_wins=opp_wins,
                           my_streak=my_streak, opp_streak=opp_streak,
                           is_watcher=is_watcher,
                           code=100000 + pid,
                           theme=rec.get('theme') or 'day',
                           q_time=PK_Q_TIME)


@app.route('/pk/<int:pid>/watch')
@login_required
def pk_watch(pid):
    """C14 观战：第三人以只读方式进入房间"""
    rec = _pk_rec(pid)
    if not rec:
        abort(404)
    if session['uid'] in (rec['challenger_uid'], rec['opponent_uid']):
        return redirect(url_for('pk_room', pid=pid))
    return _render_pk_room(rec, is_watcher=True)


@app.route('/pk/<int:pid>/replay')
@login_required
def pk_replay(pid):
    """C15 对局回放：按题目顺序逐步重演双方作答"""
    rec = _pk_rec(pid)
    if not rec:
        abort(404)
    if rec['status'] != 'finished':
        flash('该对局尚未结束，无法回放', 'warning')
        return redirect(url_for('pk_lobby'))
    # 回放为对局结束后的只读内容，所有登录用户均可观看（与观战权限一致）
    qids = [int(x) for x in rec['question_ids'].split(',')]
    questions = load_questions(qids)
    qa = rec['challenger_answers'] or ''
    ob = rec['opponent_answers'] or ''
    steps = []
    for i, qq in enumerate(questions):
        correct = ''.join(o['label'] for o in qq['options'] if o['is_correct'])
        ca = qa[i] if i < len(qa) and qa[i] != '_' else None
        oa = ob[i] if i < len(ob) and ob[i] != '_' else None
        steps.append({
            'idx': i,
            'stem': qq['stem'],
            'images': qq['images'],
            'options': [{'label': o['label'], 'content': o['content'],
                         'correct': bool(o['is_correct'])} for o in qq['options']],
            'correct': correct,
            'c_answer': ca, 'o_answer': oa,
            'c_ok': ca == correct, 'o_ok': oa == correct,
        })
    return render_template('pk_replay.html', rec=rec, steps=steps,
                           code=100000 + pid)


# ---- WebSocket 事件 ----
def _session_uid():
    """socket 事件专用的会话校验：账号在其他设备登录后，旧会话无效"""
    uid = session.get('uid')
    if uid is None:
        return None
    return uid if _login_valid(uid) else None


@socketio.on('connect')
def socket_connect():
    if _session_uid() is not None:
        ONLINE_SIDS[request.sid] = session['uid']


@socketio.on('disconnect')
def socket_disconnect():
    uid = ONLINE_SIDS.pop(request.sid, None)
    # C14 观战者断开：更新房间旁观人数
    for key, room in list(PK_ROOMS.items()):
        if request.sid in room.get('watchers', set()):
            room['watchers'].discard(request.sid)
            emit('watch_count', {'n': len(room.get('watchers', set()))}, room=key)
    if uid is None:
        return
    # 通知所在房间的对手（仅当断开的是该用户当前绑定的连接才算真正离开；
    # 多页签旧连接、重连后的僵尸连接不计入，避免误报"对方已离开"）
    for key, room in list(PK_ROOMS.items()):
        if uid in (room['challenger'], room['opponent']) \
                and room['sids'].get(uid) == request.sid:
            room['sids'].pop(uid, None)
            # 不清理 ready 状态：手机休眠/切网导致的断线很常见，
            # 清掉 ready 会导致对方上线后准备无法凑齐开局。
            # ready 只在显式离开或再战重置时清。
            other = room['opponent'] if uid == room['challenger'] else room['challenger']
            emit('opponent_left', {'uid': uid}, room=key)


@socketio.on('pk_join')
def pk_join(data):
    """玩家加入房间（服务重启后可从数据库恢复未开始的房间）"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        rec = q("SELECT * FROM pk_challenge WHERE id=%s", (pid,), one=True)
        if not rec:
            return
        if rec['status'] == 'playing':
            # B 重启兜底：服务重启丢失内存房间，进行中对局比分/进度无法恢复，
            # 判平局收场（不动 pk_wins/losses），避免玩家永远卡在进不去的对局。
            # 仅参赛玩家触发，防止无关用户的 pk_join 误杀对局
            if _session_uid() in (rec['challenger_uid'], rec['opponent_uid']):
                execute("UPDATE pk_challenge SET status='finished', winner=NULL "
                        "WHERE id=%s AND status='playing'", (pid,))
                emit('room_closed', {'msg': '服务器重启导致对局中断，已按平局处理'})
            return
        if rec['status'] != 'waiting':
            return
        room = PK_ROOMS[key] = {
            'challenger': rec['challenger_uid'],
            'opponent': rec['opponent_uid'],
            'status': 'waiting',
            'questions': [int(x) for x in rec['question_ids'].split(',')],
            'current_q': -1,
            'scores': {rec['challenger_uid']: 0, rec['opponent_uid']: 0},
            'answers': {}, 'answered': {}, 'seq': {},
            'ready': set(), 'sids': {}, 'watchers': set(),
            'theme': rec.get('theme') or 'day',
        }
    uid = _session_uid()
    if uid not in (room['challenger'], room['opponent']):
        return
    join_room(key)
    room['sids'][uid] = request.sid

    # 通知房间内双方当前状态（重连方据此自动恢复"已准备"按钮/补报）
    _pk_emit_state(key, room)

    # 自动开局检查：对方已准备 + 我方之前也准备过 + 双方都在线 → 开局
    # 解决"A先准备→A掉线→B上线准备→A重连"场景：A 重连后无需再点准备
    _pk_try_start(key, room)

    # A 主修复：对局进行中重连（退出重登/F5/切后台）→ 单发对局快照，
    # 恢复当前题/比分/剩余秒。数据口径与观战 watch_sync 一致但不泄露对方选择
    if room['status'] == 'playing' and room.get('current_q', -1) >= 0:
        idx = room['current_q']
        qq = load_questions([room['questions'][idx]])[0]
        remain = max(0, round(room.get('q_deadline', time.time()) - time.time()))
        ans = room.get('answers', {}).get(idx, {}) or {}
        answered = room.get('answered', {}).get(idx, set())
        opp = room['opponent'] if uid == room['challenger'] else room['challenger']
        emit('pk_rejoin', {
            'idx': idx, 'total': PK_QUESTION_COUNT,
            'stem': qq['stem'], 'images': qq.get('images', []),
            'options': [{'label': o['label'], 'content': o['content']}
                        for o in qq['options']],
            'q_time': PK_Q_TIME, 'remain': remain,
            'scores': {str(k): v for k, v in room['scores'].items()},
            'my_answered': uid in answered, 'my_choice': ans.get(uid),
            'opp_answered': opp in answered,
        })


@socketio.on('watch_join')
def watch_join(data):
    """C14 观战者加入：只读同步题目与比分"""
    pid = data.get('pid')
    if _session_uid() is None:
        return
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        rec = q("SELECT * FROM pk_challenge WHERE id=%s", (pid,), one=True)
        if not rec or rec['status'] not in ('waiting', 'ready', 'playing', 'finished'):
            return
        room = PK_ROOMS[key] = {
            'challenger': rec['challenger_uid'],
            'opponent': rec['opponent_uid'],
            'status': rec['status'],
            'questions': [int(x) for x in rec['question_ids'].split(',')],
            'current_q': -1,
            'scores': {rec['challenger_uid']: 0, rec['opponent_uid']: 0},
            'answers': {}, 'answered': {}, 'seq': {},
            'ready': set(), 'sids': {}, 'watchers': set(),
            'theme': rec.get('theme') or 'day',
        }
    join_room(key)
    room.setdefault('watchers', set()).add(request.sid)
    emit('watch_count', {'n': len(room['watchers'])}, room=key)
    players = [
        {'uid': room['challenger'], 'ready': room['challenger'] in room['ready']},
        {'uid': room['opponent'], 'ready': room['opponent'] in room['ready']},
    ]
    emit('room_state', {'players': players, 'status': room['status']})

    # 对局进行中：补发当前题/比分/剩余时间/双方已选答案，观战者即时同步
    if room['status'] == 'playing' and room.get('current_q', -1) >= 0:
        idx = room['current_q']
        qq = load_questions([room['questions'][idx]])[0]
        remain = max(0, round(room.get('q_deadline', time.time()) - time.time()))
        ans = room.get('answers', {}).get(idx, {}) or {}
        emit('watch_sync', {
            'idx': idx, 'total': PK_QUESTION_COUNT,
            'stem': qq['stem'], 'images': qq.get('images', []),
            'options': [{'label': o['label'], 'content': o['content']}
                        for o in qq['options']],
            'q_time': PK_Q_TIME, 'remain': remain,
            'scores': {str(k): v for k, v in room['scores'].items()},
            'answers': {str(k): v for k, v in ans.items()},
            'answered': [u for u in room.get('answered', {}).get(idx, set())],
        })
    elif room['status'] == 'finished':
        emit('watch_finished', {'pid': pid})


def _pk_try_start(key, room):
    """检查双方都已准备且都在线，满足则开局。供 pk_ready / pk_join 调用。"""
    if room['status'] != 'waiting':
        return
    both_ready = room['challenger'] in room['ready'] and room['opponent'] in room['ready']
    both_online = room['challenger'] in room['sids'] and room['opponent'] in room['sids']
    if not (both_ready and both_online):
        return
    room['status'] = 'playing'
    room['current_q'] = -1
    socketio.sleep(1)
    emit('start_countdown', {}, room=key)
    for n in [3, 2, 1]:
        socketio.sleep(1)
        emit('countdown', {'n': n}, room=key)
    socketio.sleep(1)
    emit('go', {}, room=key)
    socketio.sleep(0.5)
    _pk_next_question(key, room)


@socketio.on('pk_ready')
def pk_ready(data):
    """玩家准备"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        return
    uid = _session_uid()
    if uid not in (room['challenger'], room['opponent']):
        return
    room['ready'].add(uid)
    _pk_emit_state(key, room)

    # 双方都准备且都在线 -> 开始倒计时
    _pk_try_start(key, room)


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
        'images': qq.get('images', []),
        'options': [{'label': o['label'], 'content': o['content']}
                    for o in qq['options']],
        'q_time': PK_Q_TIME,
        'scores': {str(k): v for k, v in room['scores'].items()},
    }
    room['answers'][idx] = {}
    room['answered'][idx] = set()
    room['locked_q'] = -1
    room['q_deadline'] = time.time() + PK_Q_TIME   # 观战者中途进入时补发剩余秒数
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
    uid = _session_uid()
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
        room['answers'].setdefault(idx, {})[uid] = answer
        room['locked_q'] = idx   # 通知定时器提前推进
        emit('answer_result', {
            'uid': uid, 'correct': True, 'answer': answer,
            'correct_answer': correct_str,
            'scores': {str(k): v for k, v in room['scores'].items()},
            'locked': True,
        }, room=key)
    else:
        # 抢答错误：直接送对方 1 分并锁定本题，双方立即进入下一题
        room['scores'][other] += 1
        room['answers'].setdefault(idx, {})[uid] = answer
        room['locked_q'] = idx
        emit('answer_result', {
            'uid': uid, 'correct': False, 'answer': answer,
            'correct_answer': correct_str,
            'scores': {str(k): v for k, v in room['scores'].items()},
            'locked': True,
        }, room=key)


PK_CHAT_EMOJIS = ('😊', '😂', '😤', '👍', '🎉')
PK_CHAT_TAUNTS = ('我要超你了，小心', '就这还想超我', '再练练吧', '等等我', '加油')
# 观战者专属助威弹幕：渲染成赛道飞入弹幕而非聊天气泡
PK_CHAT_CHEERS = ('666666', '这波操作可以', '红队冲！', '蓝队稳住啊',
                  '围观大佬', '太刺激了', '加油加油', '神仙打架')
# 弹幕服务端限流：(room_key, sid) -> 上次发送时间戳，同 sid 间隔 1.5 秒
_PK_CHAT_CD = {}
_PK_CHEER_INTERVAL = 1.5


@socketio.on('pk_chat')
def pk_chat(data):
    """快捷互动（表情/预设喊话/观战助威弹幕）：玩家与观战者均可发，全房间广播可见。
    文字只允许白名单预设句，表情只允许白名单表情，防刷屏/灌水。"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        return
    uid = _session_uid()
    is_watcher = request.sid in room.get('watchers', set())
    if not is_watcher and uid not in (room['challenger'], room['opponent']):
        return
    kind = data.get('kind', '')
    value = (data.get('value') or '').strip()
    if kind == 'emoji':
        if value not in PK_CHAT_EMOJIS:
            return
    elif kind == 'text':
        if value not in PK_CHAT_TAUNTS:
            return
    elif kind == 'cheer':
        # 助威弹幕仅观战者可发，且服务端 1.5 秒限流
        if not is_watcher or value not in PK_CHAT_CHEERS:
            return
        import time as _time
        now = _time.time()
        if len(_PK_CHAT_CD) > 1000:  # 顺手清理过期限流记录，防长期运行堆积
            expired = [k for k, t in _PK_CHAT_CD.items() if now - t > 300]
            for k in expired:
                _PK_CHAT_CD.pop(k, None)
        cd_key = (key, request.sid)
        if now - _PK_CHAT_CD.get(cd_key, 0) < _PK_CHEER_INTERVAL:
            return
        _PK_CHAT_CD[cd_key] = now
    else:
        return
    u = q("SELECT username, real_name FROM `user` WHERE id=%s", (uid,), one=True)
    name = (u['real_name'] or u['username']) if u else ''
    if is_watcher:
        role = 'watcher'
    elif uid == room['challenger']:
        role = 'c'
    else:
        role = 'o'
    emit('pk_chat', {'kind': kind, 'value': value, 'from': uid,
                     'role': role, 'name': name}, room=key)


def _pk_seq_str(room, uid):
    """C15 回放：按题目顺序拼合作答串，未作答记 '_'"""
    seq = room.get('seq', {})
    return ''.join(seq.get(i, {}).get(uid, '_')
                   for i in range(PK_QUESTION_COUNT))[:20]


def _pk_finish(key, room, force_winner=None):
    """游戏结束：判定胜负，更新战绩（force_winner 用于认输/中途退出判负）"""
    room['status'] = 'finished'
    cs = room['scores'][room['challenger']]
    os_ = room['scores'][room['opponent']]
    if force_winner is not None:
        winner = force_winner
    elif cs > os_:
        winner = room['challenger']
    elif os_ > cs:
        winner = room['opponent']
    else:
        winner = None  # 平局

    # 更新数据库（含双方作答序列，供对局回放）
    execute("UPDATE pk_challenge SET status='finished', "
            "challenger_score=%s, opponent_score=%s, winner_uid=%s, "
            "challenger_answers=%s, opponent_answers=%s, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=%s",
            (cs, os_, winner, _pk_seq_str(room, room['challenger']),
             _pk_seq_str(room, room['opponent']), int(key.split('_')[1])))

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


@socketio.on('pk_concede')
def pk_concede(data):
    """认输：对局中主动放弃，对手获胜，自己留在房间看结算"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room or room['status'] != 'playing':
        return
    uid = _session_uid()
    if uid not in (room['challenger'], room['opponent']):
        return
    other = room['opponent'] if uid == room['challenger'] else room['challenger']
    _pk_finish(key, room, force_winner=other)
    PK_ROOMS.pop(key, None)


@socketio.on('pk_leave')
def pk_leave(data):
    """退出房间：等待期作废房间；对局中按认输处理并退出"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        emit('room_closed', room=request.sid)
        return
    uid = _session_uid()
    if uid not in (room['challenger'], room['opponent']):
        return
    if room['status'] == 'playing':
        other = room['opponent'] if uid == room['challenger'] else room['challenger']
        _pk_finish(key, room, force_winner=other)   # 对手收到结算
        emit('room_closed', room=request.sid)       # 退出者直接回大厅
        PK_ROOMS.pop(key, None)
        return
    # 等待/准备阶段：房间作废，双方回大厅
    execute("UPDATE pk_challenge SET status='declined' WHERE id=%s", (pid,))
    emit('room_closed', room=key)
    PK_ROOMS.pop(key, None)


@socketio.on('pk_rematch')
def pk_rematch(data):
    """C1 再战一局：双方点击后用相同题型配置直接开新局（新房间码）"""
    pid = data.get('pid')
    key = _pk_room_key(pid)
    room = PK_ROOMS.get(key)
    if not room:
        rec = q("SELECT * FROM pk_challenge WHERE id=%s AND status='finished'",
                (pid,), one=True)
        if not rec:
            return
        room = PK_ROOMS[key] = {
            'challenger': rec['challenger_uid'],
            'opponent': rec['opponent_uid'],
            'status': 'finished',
            'questions': [int(x) for x in rec['question_ids'].split(',')],
            'current_q': -1,
            'scores': {rec['challenger_uid']: 0, rec['opponent_uid']: 0},
            'answers': {}, 'answered': {}, 'seq': {},
            'ready': set(), 'sids': {}, 'watchers': set(),
            'theme': rec.get('theme') or 'day',
        }
    uid = _session_uid()
    if uid not in (room['challenger'], room['opponent']):
        return
    rematch = room.setdefault('rematch', set())
    rematch.add(uid)
    emit('rematch_wait', {'n': len(rematch)}, room=key)
    if len(rematch) < 2:
        return
    # 双方都同意：按相同题型配置重新抽新题（避开近 3 局 + 上一局原题），开新局
    old = list(room['questions'])
    type_rows = q(
        "SELECT qtype, COUNT(*) c FROM question WHERE id IN (%s) GROUP BY qtype"
        % ','.join(['%s'] * len(old)), old)
    tcount = {r['qtype']: r['c'] for r in type_rows}
    jn, sn = tcount.get('judge', 0), tcount.get('single', 0)
    exclude = _pk_recent_qids((room['challenger'], room['opponent']))
    exclude.update(old)   # 保证再战不与上一局重复
    new_qids = _pk_pick_questions(jn, sn, exclude)
    if len(new_qids) < len(old):    # 极端情况题池不足，退回旧题重洗
        new_qids = list(old)
    random.shuffle(new_qids)
    new_pid = execute(
        "INSERT INTO pk_challenge (challenger_uid, opponent_uid, question_ids, "
        "theme, status) VALUES (%s, %s, %s, %s, 'waiting')",
        (room['challenger'], room['opponent'],
         ','.join(map(str, new_qids)), room.get('theme') or 'day'))
    PK_ROOMS[_pk_room_key(new_pid)] = {
        'challenger': room['challenger'],
        'opponent': room['opponent'],
        'status': 'waiting',
        'questions': new_qids,
        'current_q': -1,
        'scores': {room['challenger']: 0, room['opponent']: 0},
        'answers': {}, 'answered': {}, 'seq': {},
        'ready': set(), 'sids': {}, 'watchers': set(),
        'theme': room.get('theme') or 'day',
    }
    emit('rematch_go', {'pid': new_pid}, room=key)
    PK_ROOMS.pop(key, None)


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


# 生产部署（Linux）用 gunicorn + gevent，不执行本块：
#   gunicorn --worker-class gevent -w 1 --bind 0.0.0.0:5000 main:app
# 本机 Windows 开发直接 python main.py（threading 模式）
if __name__ == '__main__':
    _port = int(os.environ.get('PORT', 5000))
    _debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    socketio.run(app, debug=_debug, host='0.0.0.0', port=_port,
                 allow_unsafe_werkzeug=True)
