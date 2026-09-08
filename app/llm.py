# -*- coding: utf-8 -*-
"""
llm.py - 加分项③⑥：LLM 答案正确性验证（盲测 + 仲裁）+ token 成本统计

验证算法（三层漏斗，省钱省时的关键是后面两层只处理前面筛不掉的题）：
1. 规则层：带图题纯文本模型无法作答，直接落 skipped，不发起调用
2. 盲测层：AI 只看题目与选项独立作答（看不到标准答案，无锚定偏差），
   代码把 AI 答案归一化后与标准答案精确比对——一致即判"可信"（约九成题到这就结束）
3. 仲裁层：仅对盲测分歧的题做第二次调用——把题目、标准答案、AI 盲答一起给 AI，
   让它依据现行法规仲裁：维持=标准答案可信（AI 误答），推翻=疑似错题（人工复核）

- 配置由管理员在"AI 验证"页面自填（openai 兼容接口），存 llm_config 表
- 验证任务后台线程并发执行，进度实时写 verify_batch；服务重启后僵死批次自动标记
- 成本统计：每次调用的 prompt/completion tokens 与耗时落库，按服务商单价折算费用
"""
import os
import re
import json
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI
import pymysql

DB = dict(host=os.environ.get('DB_HOST', 'localhost'),
          user=os.environ.get('DB_USER', 'root'),
          password=os.environ.get('DB_PASSWORD',
                                  os.environ.get('MYSQL_PASSWORD', '123456')),
          database=os.environ.get('DB_NAME', 'kemu1_exam'), charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor, autocommit=True)

WORKERS = 4              # 并发验证线程数

# 服务商预设（openai 兼容接口；单价为常见公开价的估算，元 / 百万 tokens）
PROVIDERS = {
    'zhipu':    {'name': '智谱 GLM（glm-4-flash 免费）',
                 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                 'models': ['glm-4-flash', 'glm-4-flash-250414', 'glm-4-plus'],
                 'price': {'glm-4-flash': (0, 0)}},
    'deepseek': {'name': 'DeepSeek（deepseek-chat）',
                 'base_url': 'https://api.deepseek.com/v1',
                 'models': ['deepseek-chat', 'deepseek-reasoner'],
                 'price': {'deepseek-chat': (2, 8), 'deepseek-reasoner': (4, 16)}},
    'moonshot': {'name': '月之暗面 Kimi',
                 'base_url': 'https://api.moonshot.cn/v1',
                 'models': ['moonshot-v1-8k', 'moonshot-v1-32k'],
                 'price': {}},
    'qwen':     {'name': '阿里通义千问',
                 'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                 'models': ['qwen-turbo', 'qwen-plus', 'qwen-max'],
                 'price': {}},
    'openai':   {'name': 'OpenAI',
                 'base_url': 'https://api.openai.com/v1',
                 'models': ['gpt-4o-mini', 'gpt-4o'],
                 'price': {}},
}
DEFAULT_PRICE = (1.0, 2.0)          # 未收录模型的估算单价（元/百万tokens）
VL_PRICES = {                        # 视觉模型单价（元/百万tokens，估算）
    'qwen-vl-plus': (1.5, 4.5),
    'qwen-vl-max': (3.0, 9.0),
    'qwen-vl-ocr': (0.5, 1.5),
    'glm-4v-flash': (0, 0),
}

# 盲测 prompt：只给题目与选项，不给标准答案（独立作答，无锚定）
PROMPT_BLIND = (
    '你是机动车驾驶人科目一考试的资深教练。请独立作答下面这道题目。\n'
    '题目：{stem}\n选项：\n{options}\n\n'
    '要求：只输出 JSON，格式为 {{"ai_answer": "你的作答（判断题用 T/F，'
    '选择题用字母，多选用字母连写）", "reason": "一句话理由，不超过20字"}}。'
)

# 仲裁 prompt：仅用于盲测分歧的题，把标准答案与 AI 盲答一起给 AI 评审
PROMPT_ARBITER = (
    '你是驾考科目一题库审核专家。下面这道题目的标准答案与大模型的盲测作答不一致，'
    '请你依据现行道路交通安全法律法规仲裁。\n'
    '题目：{stem}\n选项：\n{options}\n标准答案：{answer}\n大模型盲测作答：{ai_answer}\n\n'
    '只输出 JSON：{{"verdict": "keep|overturn|uncertain", '
    '"reason": "一句话仲裁理由"}}。'
    'verdict 含义：keep=标准答案正确（大模型答错），overturn=标准答案错误，uncertain=无法判定。'
)


# ---------------------------------------------------------------
# 配置存取
# ---------------------------------------------------------------
def get_config():
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_config ORDER BY id DESC LIMIT 1")
            return cur.fetchone()
    finally:
        conn.close()


def save_config(provider, base_url, api_key, model, vl_model=''):
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM llm_config")
            cur.execute("INSERT INTO llm_config (provider, base_url, api_key, model, vl_model) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (provider, base_url, api_key, model, vl_model or None))
        conn.commit()
    finally:
        conn.close()


def _client(cfg):
    return OpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'], timeout=60)


def _chat(cfg, prompt, max_tokens, image_uri=None):
    """单次对话调用（含限流退避重试），返回 (回复文本, pt, ct, ms)。
    传入 image_uri 且配置了视觉模型时，走视觉模型看图作答。"""
    last = None
    for attempt in range(3):
        try:
            t0 = time.time()
            if image_uri and (cfg.get('vl_model') or '').strip():
                content = [{'type': 'text', 'text': prompt},
                           {'type': 'image_url', 'image_url': {'url': image_uri}}]
                kw = dict(model=cfg['vl_model'],
                          messages=[{'role': 'user', 'content': content}],
                          temperature=0, max_tokens=max_tokens)
            else:
                kw = dict(model=cfg['model'],
                          messages=[{'role': 'user', 'content': prompt}],
                          temperature=0, max_tokens=max_tokens)
            try:                      # qwen3 系列关闭思考模式，提速降耗
                resp = _client(cfg).chat.completions.create(
                    **kw, extra_body={'enable_thinking': False})
            except Exception:
                resp = _client(cfg).chat.completions.create(**kw)
            ms = int((time.time() - t0) * 1000)
            return (resp.choices[0].message.content or '',
                    resp.usage.prompt_tokens, resp.usage.completion_tokens, ms)
        except Exception as e:        # 429/网络抖动：退避重试
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def _parse_json(content):
    m = re.search(r'\{.*\}', content or '', re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _image_data_uri(data, mime=None):
    """图片 BLOB -> base64 data URI（openai 兼容视觉接口格式）"""
    import base64
    if not mime:
        mime = 'image/png' if data[:4] == b'\x89PNG' else 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(data).decode()


# ---------------------------------------------------------------
# 连接测试
# ---------------------------------------------------------------
def test_connection():
    """同步发一条测试消息，返回 (ok, message)"""
    cfg = get_config()
    if not cfg:
        return False, '请先保存配置'
    try:
        content, pt, ct, ms = _chat(cfg, '请只回复两个字：成功', 8)
        return True, (f'连接成功：模型 {cfg["model"]} 回复「{content.strip()}」，'
                      f'耗时 {ms}ms，tokens={pt}+{ct}')
    except Exception as e:
        return False, '连接失败：' + str(e)[:300]


# ---------------------------------------------------------------
# 答案归一化
# ---------------------------------------------------------------
def _bool_pair(options):
    """选项是否为"正确/错误"两选项对（原库部分 single 题实为判断题，按内容判断）"""
    content = [((o.get('content') or '') if isinstance(o, dict) else (o[1] or ''))
               for o in (options or [])]
    if len(content) != 2:
        return False
    has_true = any(('正确' in c) or ('对' in c) or c.strip().upper() == 'T' for c in content)
    has_false = any(('错' in c) or c.strip().upper() == 'F' for c in content)
    return has_true and has_false


def _normalize_answer(ai_answer, qtype, options=None):
    """AI 答案归一化：正确/错误对 -> T/F（兼容 T/F、正确/错误、选项字母）；选择题 -> 字母"""
    opts = options or []
    content = {o['label']: (o['content'] or '') for o in opts if isinstance(o, dict)}
    a = re.sub(r'[^A-DTFTF对错误正]', '', (ai_answer or '').upper())
    if qtype == 'judge' or _bool_pair(opts):
        if a and ('T' in a or '正' in a or '对' in a):
            return 'T'
        if a and ('F' in a or '错' in a):
            return 'F'
        if a in ('A', 'B'):                      # AI 以选项字母作答
            c = content.get(a, '')
            return ('T' if '正确' in c or '对' in c
                    else 'F' if '错' in c else '')
        return ''
    letters = ''.join(sorted(set(re.findall(r'[A-D]', a))))
    if letters:
        return letters
    # AI 输出答案文字时按选项内容反查标号
    raw = (ai_answer or '').strip()
    if raw:
        for o in opts:
            if not isinstance(o, dict):
                continue
            c = (o['content'] or '').strip()
            if c and (c == raw or (len(c) <= 10 and (c in raw or raw in c))):
                return o['label']
    return ''


# ---------------------------------------------------------------
# 验证任务
# ---------------------------------------------------------------
def _pending_cond(include_images):
    """待处理题条件：不勾图片题=未验证的纯文本题；勾选=未验证或曾跳过（含带图题）"""
    if include_images:
        return "(av.id IS NULL OR av.verdict='skipped')"
    return ("av.id IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM question_image qi WHERE qi.question_id = q.id)")


def start_verify(scope, limit, include_images=False):
    """启动验证任务。scope: all/judge/single/multi；limit: None/0 表示全部待验证题；
    include_images: 包含图片题（需已配置视觉模型，此前跳过的带图题会被重新纳入）"""
    cfg = get_config()
    if not cfg:
        return None, '请先保存 API 配置'
    if scope not in ('all', 'judge', 'single', 'multi'):
        scope = 'all'
    if include_images and not (cfg.get('vl_model') or '').strip():
        return None, '包含图片题需先在配置里填写视觉模型（如 qwen-vl-plus）'
    cond = _pending_cond(include_images)
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            where = 'WHERE q.qtype=%s' if scope != 'all' else ''
            args = (scope,) if scope != 'all' else ()
            cur.execute(
                f"SELECT COUNT(*) c FROM question q "
                f"LEFT JOIN ai_verification av ON av.question_id=q.id AND av.model=%s "
                f"{where} {'AND' if where else 'WHERE'} {cond}",
                (cfg['model'],) + args)
            pending = cur.fetchone()['c']
            plan = pending if not limit else min(int(limit), pending)
            if plan == 0:
                return None, '当前没有待验证的题目（可换模型、勾选图片题，或先清空结果）'
            cur.execute(
                "INSERT INTO verify_batch (model, scope, total, status, message) "
                "VALUES (%s, %s, %s, 'running', %s)",
                (cfg['model'], scope, plan,
                 f'待验证 {pending} 题，本批 {plan} 题（盲测+仲裁，并发 {WORKERS}'
                 f'{"，含图片题" if include_images else ""}）'))
            bid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    threading.Thread(target=_verify_run, args=(bid, scope, plan, include_images),
                     daemon=True).start()
    return bid, None


def mark_zombie():
    """服务重启会杀掉后台线程：把长时间无心跳的 running 批次标记为 interrupted"""
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            n = cur.execute(
                "UPDATE verify_batch SET status='interrupted', "
                "message=CONCAT(IFNULL(message,''),'；服务重启任务中断，重新发起即可续验') "
                "WHERE status='running' AND updated_at < NOW() - INTERVAL 3 MINUTE")
        conn.commit()
        return n
    finally:
        conn.close()


def _std_answer_of(qq, cur):
    """给题目附上选项与标准答案（judge/正确错误对换算为 T/F），返回带图标记"""
    cur.execute("SELECT label, content, is_correct FROM `option` "
                "WHERE question_id=%s ORDER BY label", (qq['id'],))
    opts = cur.fetchall()
    correct = [o['label'] for o in opts if o['is_correct']]
    if qq['qtype'] == 'judge' or _bool_pair(opts):
        content = {o['label']: o['content'] for o in opts}
        lab = correct[0] if correct else 'A'
        c = content.get(lab, '')
        qq['answer'] = ('T' if c.startswith('正确') or '对' in c
                        else ('F' if c.startswith('错误') or '错' in c
                              else ('T' if lab == 'A' else 'F')))
    else:
        qq['answer'] = ''.join(sorted(correct))
    qq['options'] = opts


def _verify_run(bid, scope, limit, include_images=False):
    def upd(**kw):
        conn = pymysql.connect(**DB)
        try:
            with conn.cursor() as cur:
                sets = ', '.join(f'{k}=%s' for k in kw)
                cur.execute(f"UPDATE verify_batch SET {sets} WHERE id=%s",
                            (*kw.values(), bid))
        finally:
            conn.close()

    KEY_OF = {'correct': 'correct_n', 'kept': 'kept_n', 'wrong': 'wrong_n',
              'skipped': 'skipped_n'}
    try:
        cfg = get_config()
        model = cfg['model']
        vl_model = (cfg.get('vl_model') or '').strip()
        conn = pymysql.connect(**DB)
        try:
            with conn.cursor() as cur:
                where = 'WHERE q.qtype=%s' if scope != 'all' else ''
                args = (scope,) if scope != 'all' else ()
                cur.execute(
                    f"SELECT q.id, q.stem, q.qtype FROM question q "
                    f"LEFT JOIN ai_verification av ON av.question_id=q.id AND av.model=%s "
                    f"{where} {'AND' if where else 'WHERE'} {_pending_cond(include_images)} "
                    f"ORDER BY RAND()",
                    (model,) + args)
                rows = cur.fetchall()
                picked = rows if not limit else rows[:limit]
                qids = [r['id'] for r in picked]
                img_ids = set()
                if qids:                      # 图片题跳过，不发起调用
                    cur.execute(
                        "SELECT DISTINCT question_id FROM question_image "
                        "WHERE question_id IN (%s)" % ','.join(['%s'] * len(qids)),
                        qids)
                    img_ids = {r['question_id'] for r in cur.fetchall()}
                for qq in picked:
                    _std_answer_of(qq, cur)
                    qq['has_image'] = qq['id'] in img_ids
        finally:
            conn.close()

        upd(total=len(picked),
            message=f'开始验证（模型 {model}｜并发 {WORKERS}｜盲测+仲裁）')
        counters = dict(done=0, correct_n=0, kept_n=0, wrong_n=0,
                        uncertain_n=0, skipped_n=0)
        lock = threading.Lock()

        def record(qq, actual_model, verdict, ai_answer, reason, pt, ct, ms):
            conn = pymysql.connect(**DB)
            try:
                with conn.cursor() as cur:
                    # 历史档案：model=实际执行模型（视觉题记 VL 模型，成本按真实模型核算）
                    cur.execute(
                        "INSERT INTO verify_result (batch_id, question_id, model, verdict, "
                        "ai_answer, reason, prompt_tokens, completion_tokens, latency_ms) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (bid, qq['id'], actual_model, verdict, (ai_answer or '')[:50],
                         (reason or '')[:500], pt, ct, ms))
                    # 最新状态：model 恒为主模型（保证选题与待验证数口径一致）
                    cur.execute(
                        "INSERT INTO ai_verification (question_id, model, batch_id, verdict, "
                        "ai_answer, reason, prompt_tokens, completion_tokens, latency_ms) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE batch_id=VALUES(batch_id), "
                        "verdict=VALUES(verdict), "
                        "ai_answer=VALUES(ai_answer), reason=VALUES(reason), "
                        "prompt_tokens=VALUES(prompt_tokens), "
                        "completion_tokens=VALUES(completion_tokens), "
                        "latency_ms=VALUES(latency_ms)",
                        (qq['id'], model, bid, verdict, (ai_answer or '')[:50],
                         (reason or '')[:500], pt, ct, ms))
                conn.commit()
            finally:
                conn.close()

        def _load_image(qid):
            conn = pymysql.connect(**DB)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT i.data, i.mime_type FROM question_image qi "
                        "JOIN image i ON i.id = qi.image_id "
                        "WHERE qi.question_id = %s ORDER BY qi.position LIMIT 1",
                        (qid,))
                    r = cur.fetchone()
                    return (r['data'], r['mime_type']) if r else (None, None)
            finally:
                conn.close()

        def _verify_one(qq, image_uri=None):
            """盲测→比对→仲裁（文本题与带图题共用，带图时走视觉模型）"""
            raw, reason, pt, ct, ms = _blind_answer(cfg, qq['stem'], qq['options'],
                                                    image_uri=image_uri)
            norm = _normalize_answer(raw, qq['qtype'], qq['options'])
            if not norm:
                return 'uncertain', '', ('AI 答案无法解析：' + (reason or ''))[:200], pt, ct, ms
            if norm == qq['answer']:
                return 'correct', norm, (reason or ''), pt, ct, ms
            av, ar, pt2, ct2, ms2 = _arbitrate(cfg, qq['stem'], qq['options'],
                                               qq['answer'], norm, image_uri=image_uri)
            pt, ct, ms = pt + pt2, ct + ct2, ms + ms2
            if av == 'overturn':
                return 'wrong', norm, ('仲裁推翻标准答案：' + (ar or ''))[:200], pt, ct, ms
            if av == 'keep':
                return 'kept', norm, ('仲裁维持标准答案（AI 误答）：' + (ar or ''))[:200], pt, ct, ms
            return 'uncertain', norm, ('仲裁无法判定：' + (ar or ''))[:200], pt, ct, ms

        def work(qq):
            try:
                if qq['has_image'] and not (include_images and vl_model):
                    # 第1层：带图题跳过（未勾选图片题，或未配置视觉模型）
                    verdict, ai_answer = 'skipped', ''
                    reason, pt, ct, ms = '带图题，纯文本模型无法作答', 0, 0, 0
                    actual = model
                elif qq['has_image']:           # 勾选且已配视觉模型：看图盲测+仲裁
                    data, mime = _load_image(qq['id'])
                    if not data:
                        verdict, ai_answer = 'skipped', ''
                        reason, pt, ct, ms = '图片缺失，无法读取', 0, 0, 0
                        actual = model
                    else:
                        verdict, ai_answer, reason, pt, ct, ms = _verify_one(
                            qq, _image_data_uri(data, mime))
                        actual = vl_model
                else:                            # 纯文本题：文本模型盲测+仲裁
                    verdict, ai_answer, reason, pt, ct, ms = _verify_one(qq)
                    actual = model
                record(qq, actual, verdict, ai_answer, reason, pt, ct, ms)
                with lock:
                    counters['done'] += 1
                    counters[KEY_OF.get(verdict, 'uncertain_n')] += 1
                    upd(done=counters['done'],
                        correct_n=counters['correct_n'], kept_n=counters['kept_n'],
                        wrong_n=counters['wrong_n'],
                        uncertain_n=counters['uncertain_n'],
                        skipped_n=counters['skipped_n'])
                time.sleep(0.05)
            except Exception as e:
                with lock:
                    counters['done'] += 1
                    counters['uncertain_n'] += 1
                    upd(done=counters['done'],
                        correct_n=counters['correct_n'], kept_n=counters['kept_n'],
                        wrong_n=counters['wrong_n'],
                        uncertain_n=counters['uncertain_n'],
                        skipped_n=counters['skipped_n'],
                        message=f"题 #{qq['id']} 异常：{str(e)[:120]}")

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(work, picked))

        c = counters
        upd(status='done',
            message=f"完成：验证 {c['done']} 题（盲答一致 {c['correct_n']}｜"
                    f"仲裁维持 {c['kept_n']}｜疑似错题 {c['wrong_n']}｜"
                    f"无法判定 {c['uncertain_n']}｜带图跳过 {c['skipped_n']}）")
    except Exception as e:
        upd(status='failed', message='失败：' + ''.join(
            traceback.format_exception_only(e)).strip()[:400])


def _blind_answer(cfg, stem, options, image_uri=None):
    """第2层盲测：只给题目与选项（可含图片），AI 独立作答。返回 (原始答案, 理由, pt, ct, ms)"""
    opt_lines = '\n'.join(f"{o['label']}. {o['content']}" for o in options)
    prompt = PROMPT_BLIND.format(stem=stem, options=opt_lines)
    if image_uri:
        prompt = ('题目配有示意图，请结合图片内容作答。\n' + prompt)
    content, pt, ct, ms = _chat(cfg, prompt, 150, image_uri=image_uri)
    d = _parse_json(content)
    if d and d.get('ai_answer') is not None:
        return str(d.get('ai_answer', '')), str(d.get('reason', '')), pt, ct, ms
    return '', ('AI 未按 JSON 输出：' + content[:150]), pt, ct, ms


def _arbitrate(cfg, stem, options, std_answer, ai_answer, image_uri=None):
    """第3层仲裁：题目+标准答案+盲答一起给 AI 评（可含图片）。返回 (verdict, 理由, pt, ct, ms)"""
    opt_lines = '\n'.join(f"{o['label']}. {o['content']}" for o in options)
    prompt = PROMPT_ARBITER.format(stem=stem, options=opt_lines,
                                   answer=std_answer, ai_answer=ai_answer)
    if image_uri:
        prompt = ('题目配有示意图，请结合图片内容仲裁。\n' + prompt)
    content, pt, ct, ms = _chat(cfg, prompt, 200, image_uri=image_uri)
    d = _parse_json(content) or {}
    verdict = d.get('verdict', 'uncertain')
    if verdict not in ('keep', 'overturn', 'uncertain'):
        verdict = 'uncertain'
    return verdict, str(d.get('reason', content[:120])), pt, ct, ms


# ---------------------------------------------------------------
# ⑥ token 成本统计
# ---------------------------------------------------------------
def unit_price(model):
    if model in VL_PRICES:
        return VL_PRICES[model]
    for p in PROVIDERS.values():
        if model in p['price']:
            return p['price'][model]
    return DEFAULT_PRICE


def verify_stats():
    """返回 (累计成本汇总[按实际调用, 取自 verify_result], 最新判定分布, 待验证数)"""
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            # 真实累计：verify_result 是历史档案，不会被后续验证覆盖
            cur.execute(
                "SELECT model, COUNT(*) calls, SUM(prompt_tokens) pt, "
                "SUM(completion_tokens) ct, ROUND(AVG(latency_ms)) avg_ms "
                "FROM verify_result GROUP BY model")
            rows = cur.fetchall()
            for r in rows:
                inp, outp = unit_price(r['model'])
                r['cost'] = round(float(r['pt'] or 0) / 1e6 * inp +
                                  float(r['ct'] or 0) / 1e6 * outp, 4)
            # 最新状态分布：每题每模型一条
            cur.execute("SELECT verdict, COUNT(*) c FROM ai_verification "
                        "GROUP BY verdict")
            verdicts = {r['verdict']: r['c'] for r in cur.fetchall()}
            # 待验证数拆分：纯文本题 / 图片题（含此前仅跳过的带图题，均可发起验证）
            cfg2 = get_config()
            m = cfg2['model'] if cfg2 else '\x00none'
            cur.execute(
                "SELECT COALESCE(SUM(has_img = 0), 0) t, COALESCE(SUM(has_img), 0) i "
                "FROM (SELECT EXISTS(SELECT 1 FROM question_image qi "
                "WHERE qi.question_id = q.id) has_img "
                "FROM question q LEFT JOIN ai_verification av "
                "ON av.question_id = q.id AND av.model = %s "
                "WHERE av.id IS NULL OR av.verdict = 'skipped') x", (m,))
            row = cur.fetchone()
            pending_text, pending_img = int(row['t']), int(row['i'])
            return rows, verdicts, pending_text, pending_img
    finally:
        conn.close()


def clear_all():
    """清空全部验证记录与批次历史（重新验证从零开始），返回 (最新结果数, 批次数, 明细数)"""
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            n1 = cur.execute("DELETE FROM ai_verification")
            n2 = cur.execute("DELETE FROM verify_batch")
            n3 = cur.execute("DELETE FROM verify_result")
            for t in ('ai_verification', 'verify_batch', 'verify_result'):
                cur.execute(f"ALTER TABLE {t} AUTO_INCREMENT = 1")
        conn.commit()
        return n1, n2, n3
    finally:
        conn.close()


def recent_batches(n=8):
    """最近 n 个验证批次，附各批次真实 token 花费（取自 verify_result）"""
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT b.*, COALESCE(v.pt, 0) pt, COALESCE(v.ct, 0) ct "
                "FROM verify_batch b "
                "LEFT JOIN (SELECT batch_id, SUM(prompt_tokens) pt, "
                "SUM(completion_tokens) ct FROM verify_result GROUP BY batch_id) "
                "v ON v.batch_id = b.id "
                "ORDER BY b.id DESC LIMIT %s", (int(n),))
            rows = cur.fetchall()
            for r in rows:
                inp, outp = unit_price(r['model'])
                r['cost'] = round(float(r['pt']) / 1e6 * inp +
                                  float(r['ct']) / 1e6 * outp, 4)
            return rows
    finally:
        conn.close()


def recent_results(limit=50, verdict=None, batch_id=None):
    """batch_id 有值时查该批次当次的明细（历史档案）；否则查各题最新状态"""
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cols = ("LEFT(q.stem, 44) stem, q.qtype, "
                    "(SELECT GROUP_CONCAT(o.label ORDER BY o.label) FROM `option` o "
                    " WHERE o.question_id={a}.question_id AND o.is_correct) correct_labels ")
            if batch_id:      # 历史明细：该批次当次的判定，不受后续重验覆盖
                conds, args = ['vr.batch_id=%s'], [batch_id]
                if verdict:
                    conds.append('vr.verdict=%s')
                    args.append(verdict)
                where = 'WHERE ' + ' AND '.join(conds)
                cur.execute(
                    f"SELECT vr.*, {cols.format(a='vr')} FROM verify_result vr "
                    f"JOIN question q ON q.id=vr.question_id {where} "
                    f"ORDER BY vr.id DESC LIMIT {int(limit)}", args)
            else:             # 最新状态：每题每模型一条
                conds, args = [], []
                if verdict:
                    conds.append('av.verdict=%s')
                    args.append(verdict)
                where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
                cur.execute(
                    f"SELECT av.*, {cols.format(a='av')} FROM ai_verification av "
                    f"JOIN question q ON q.id=av.question_id {where} "
                    f"ORDER BY av.id DESC LIMIT {int(limit)}", args)
            return cur.fetchall()
    finally:
        conn.close()


def export_report():
    """生成 answer_report.md（验证汇总 + 疑似错题清单），返回 (路径, 疑似错题数)"""
    import datetime
    rows, verdicts, pending = verify_stats()
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT av.question_id, av.ai_answer, av.reason, av.prompt_tokens, "
                "av.completion_tokens, q.stem, q.qtype, "
                "(SELECT GROUP_CONCAT(o.label ORDER BY o.label) FROM `option` o "
                " WHERE o.question_id=av.question_id AND o.is_correct) correct_labels "
                "FROM ai_verification av JOIN question q ON q.id=av.question_id "
                "WHERE av.verdict='wrong' ORDER BY av.question_id")
            wrongs = cur.fetchall()
            qids = [w['question_id'] for w in wrongs]
            opts_map = {}
            if qids:
                cur.execute(
                    "SELECT question_id, label, content FROM `option` "
                    "WHERE question_id IN (%s) ORDER BY question_id, label"
                    % ','.join(['%s'] * len(qids)), qids)
                for r in cur.fetchall():
                    opts_map.setdefault(r['question_id'], []).append(r)
    finally:
        conn.close()

    total = sum(verdicts.values())
    lines = [
        '# AI 答案验证报告（盲测 + 仲裁）',
        '',
        f'生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}',
        '',
        '## 验证方法',
        '',
        '采用"盲测 + 仲裁"三层漏斗算法，避免让模型看到标准答案产生附和偏差：',
        '',
        '1. **规则层**：带图题纯文本模型无法作答，直接跳过（skipped）；',
        '2. **盲测层**：模型只看题干与选项独立作答（看不到标准答案），'
        '代码将其答案归一化后与标准答案精确比对，一致即判"可信"；',
        '3. **仲裁层**：仅对盲测分歧的题发起第二次调用，把题目、标准答案、'
        '盲测作答一起交模型依据现行法规仲裁——维持=可信（模型误答），推翻=疑似错题。',
        '',
        '## 验证汇总',
        '',
    ]
    for r in rows:
        lines.append(f"- 模型 `{r['model']}`：验证 {r['calls']} 题，"
                     f"输入 {r['pt'] or 0} + 输出 {r['ct'] or 0} tokens，"
                     f"平均耗时 {r['avg_ms']} ms，估算费用 {r['cost']} 元")
    lines += [
        f"- 判定分布：盲答一致 **{verdicts.get('correct', 0)}** ｜ "
        f"仲裁维持 {verdicts.get('kept', 0)} ｜ 疑似错题 "
        f"**{verdicts.get('wrong', 0)}** ｜ 无法判定 {verdicts.get('uncertain', 0)} ｜ "
        f"带图跳过 {verdicts.get('skipped', 0)}",
        f"- 当前配置模型待验证：{pending} 题",
        '',
        f'## 疑似错题清单（{len(wrongs)} 道，需人工复核）',
        '',
    ]
    for w in wrongs:
        std = w['correct_labels'] or '—'
        lines.append(f"### 题 #{w['question_id']}（{w['qtype']}）")
        lines.append(f"- 题干：{w['stem']}")
        for o in opts_map.get(w['question_id'], []):
            mark = ' ←标准答案' if o['label'] in (w['correct_labels'] or '') else ''
            lines.append(f"  - {o['label']}. {o['content']}{mark}")
        lines.append(f"- 标准答案：`{std}` ｜ AI 盲测作答：`{w['ai_answer']}`")
        lines.append(f"- 仲裁理由：{w['reason']}")
        lines.append('')
    path = r"C:\Users\w'j'm\Desktop\新建文件夹\answer_report.md"
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path, len(wrongs)
