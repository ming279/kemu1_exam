# -*- coding: utf-8 -*-
"""
llm.py - 加分项③⑥：LLM 答案正确性验证 + token 成本统计

- 配置由管理员在"AI 验证"页面自填（openai 兼容接口），存 llm_config 表
- 验证任务后台线程执行，逐题让 AI 作答并与标准答案比对，结果与 token 用量落库
  （ai_verification：verdict / ai_answer / reason / prompt_tokens / completion_tokens / 耗时）
- 成本统计：按 token 用量与服务商单价（估算）折算费用
"""
import re
import json
import time
import threading
import traceback

from openai import OpenAI
import pymysql

DB = dict(host='localhost', user='root', password='123456',
          database='kemu1_exam', charset='utf8mb4',
          cursorclass=pymysql.cursors.DictCursor, autocommit=True)

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

PROMPT_TMPL = (
    '你是驾考科目一题目审核专家。请独立作答下面这道题目，并判断给定答案是否正确。\n'
    '题目：{stem}\n选项：\n{options}\n给定答案：{answer}\n\n'
    '要求：只输出 JSON，格式为 {{"verdict": "correct|wrong|uncertain", '
    '"ai_answer": "你作答的答案（判断题用 T/F，选择题用字母，多选用字母连写）", '
    '"reason": "一句话理由"}}。若题目信息不足无法判断则 verdict 用 uncertain。'
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


def save_config(provider, base_url, api_key, model):
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM llm_config")
            cur.execute("INSERT INTO llm_config (provider, base_url, api_key, model) "
                        "VALUES (%s, %s, %s, %s)",
                        (provider, base_url, api_key, model))
        conn.commit()
    finally:
        conn.close()


def _client(cfg):
    return OpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'], timeout=30)


# ---------------------------------------------------------------
# 连接测试
# ---------------------------------------------------------------
def test_connection():
    """同步发一条测试消息，返回 (ok, message)"""
    cfg = get_config()
    if not cfg:
        return False, '请先保存配置'
    try:
        t0 = time.time()
        resp = _client(cfg).chat.completions.create(
            model=cfg['model'],
            messages=[{'role': 'user', 'content': '请只回复两个字：成功'}],
            max_tokens=8)
        usage = resp.usage
        ms = int((time.time() - t0) * 1000)
        return True, (f'连接成功：模型 {cfg["model"]} 回复'
                      f'「{resp.choices[0].message.content}」，'
                      f'耗时 {ms}ms，tokens={usage.prompt_tokens}+{usage.completion_tokens}')
    except Exception as e:
        return False, '连接失败：' + str(e)[:300]


# ---------------------------------------------------------------
# 答案验证
# ---------------------------------------------------------------
def start_verify(scope, limit):
    cfg = get_config()
    if not cfg:
        return None, '请先保存 API 配置'
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            where = '' if scope == 'all' else 'WHERE q.qtype=%s'
            args = () if scope == 'all' else (scope,)
            cur.execute(
                f"SELECT COUNT(*) c FROM question q LEFT JOIN ai_verification av "
                f"ON av.question_id=q.id AND av.model=%s {where.replace('q.qtype', 'q.qtype')}",
                (cfg['model'],) + args if args else (cfg['model'],))
            pending = cur.fetchone()['c']
            cur.execute(
                "INSERT INTO verify_batch (model, scope, total, status, message) "
                "VALUES (%s, %s, %s, 'running', %s)",
                (cfg['model'], scope, min(limit, pending),
                 f'待验证 {pending} 题，本批计划 {min(limit, pending)} 题'))
            bid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    threading.Thread(target=_verify_run, args=(bid, scope, min(limit, pending)),
                     daemon=True).start()
    return bid, None


def _ask_ai(cfg, stem, options, answer):
    """单题调用，返回 (verdict, ai_answer, reason, pt, ct, ms)"""
    def opt_items():
        for o in options:                     # 兼容 dict(DictCursor) 与 tuple
            if isinstance(o, dict):
                yield o['label'], o['content']
            else:
                yield o[0], o[1]
    opt_lines = '\n'.join(f'{l}. {c}' for l, c in opt_items())
    prompt = PROMPT_TMPL.format(stem=stem, options=opt_lines, answer=answer)
    t0 = time.time()
    kw = dict(model=cfg['model'],
              messages=[{'role': 'user', 'content': prompt}],
              temperature=0, max_tokens=300)
    try:                                      # qwen3 系列关闭思考模式，提速降耗
        resp = _client(cfg).chat.completions.create(
            **kw, extra_body={'enable_thinking': False})
    except Exception:
        resp = _client(cfg).chat.completions.create(**kw)
    ms = int((time.time() - t0) * 1000)
    content = resp.choices[0].message.content or ''
    usage = resp.usage
    # 容错解析 JSON（允许 ```json 包裹）
    m = re.search(r'\{.*\}', content, re.S)
    verdict, ai_answer, reason = 'uncertain', '', (content or '')[:200]
    if m:
        try:
            d = json.loads(m.group())
            verdict = d.get('verdict', 'uncertain')
            ai_answer = str(d.get('ai_answer', '')).strip().upper()
            reason = str(d.get('reason', ''))[:200]
        except Exception:
            pass
    return (verdict if verdict in ('correct', 'wrong', 'uncertain') else 'uncertain',
            ai_answer, reason,
            usage.prompt_tokens, usage.completion_tokens, ms)


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


def _verify_run(bid, scope, limit):
    def upd(**kw):
        conn = pymysql.connect(**DB)
        try:
            with conn.cursor() as cur:
                sets = ', '.join(f'{k}=%s' for k in kw)
                cur.execute(f"UPDATE verify_batch SET {sets} WHERE id=%s",
                            (*kw.values(), bid))
        finally:
            conn.close()

    try:
        cfg = get_config()
        model = cfg['model']
        conn = pymysql.connect(**DB)
        try:
            with conn.cursor() as cur:
                where = '' if scope == 'all' else 'WHERE q.qtype=%s'
                args = () if scope == 'all' else (scope,)
                cur.execute(
                    f"SELECT q.id, q.stem, q.qtype FROM question q "
                    f"LEFT JOIN ai_verification av ON av.question_id=q.id "
                    f"AND av.model=%s {where.replace('q.qtype', 'q.qtype') if args else ''}"
                    f"{' WHERE ' if not args else ' AND '}av.id IS NULL "
                    f"ORDER BY RAND() LIMIT {int(limit)}",
                    (model,) + args if args else (model,))
                questions = cur.fetchall()
                # judge 题把"正确/错误"选项换算为 T/F 标准答案
                for qq in questions:
                    cur.execute(
                        "SELECT label, content, is_correct FROM `option` "
                        "WHERE question_id=%s ORDER BY label", (qq['id'],))
                    qq['options'] = cur.fetchall()
                    correct = [o['label'] for o in qq['options'] if o['is_correct']]
                    if qq['qtype'] == 'judge' or _bool_pair(qq['options']):
                        # 正确选项的"内容"决定 T/F（以 is_correct 为准）
                        content = {o['label']: o['content'] for o in qq['options']}
                        lab = correct[0] if correct else 'A'
                        c = content.get(lab, '')
                        qq['answer'] = ('T' if c.startswith('正确') or '对' in c
                                        else ('F' if c.startswith('错误') or '错' in c
                                              else ('T' if lab == 'A' else 'F')))
                    else:
                        qq['answer'] = ''.join(sorted(correct))
        finally:
            conn.close()

        upd(total=len(questions),
            message=f'开始验证（模型 {model}）')
        done = correct_n = wrong_n = uncertain_n = 0
        for i, qq in enumerate(questions, 1):
            try:
                verdict, ai_answer, reason, pt, ct, ms = _ask_ai(
                    cfg, qq['stem'], qq['options'], qq['answer'])
                # 二次校验：以 AI 答案与标准答案比对为准，verdict 仅作参考
                norm = _normalize_answer(ai_answer, qq['qtype'], qq['options'])
                if not norm:
                    verdict = 'uncertain'
                    reason = ((reason or '') + '；AI 未给出可解析的答案')[:200]
                else:
                    verdict = 'correct' if norm == qq['answer'] else 'wrong'
                conn = pymysql.connect(**DB)
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO ai_verification (question_id, model, verdict, "
                            "ai_answer, reason, prompt_tokens, completion_tokens, latency_ms) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                            "ON DUPLICATE KEY UPDATE verdict=VALUES(verdict), "
                            "ai_answer=VALUES(ai_answer), reason=VALUES(reason), "
                            "prompt_tokens=VALUES(prompt_tokens), "
                            "completion_tokens=VALUES(completion_tokens), "
                            "latency_ms=VALUES(latency_ms)",
                            (qq['id'], model, verdict, norm or ai_answer[:40],
                             reason, pt, ct, ms))
                    conn.commit()
                finally:
                    conn.close()
                done += 1
                if verdict == 'correct':
                    correct_n += 1
                elif verdict == 'wrong':
                    wrong_n += 1
                else:
                    uncertain_n += 1
            except Exception as e:
                done += 1
                uncertain_n += 1
                upd(message=f'第 {i} 题异常：{str(e)[:150]}')
            upd(done=done, correct_n=correct_n, wrong_n=wrong_n,
                uncertain_n=uncertain_n)
            time.sleep(0.15)                     # 温和请求，防 QPS 限制
        upd(status='done',
            message=f'完成：验证 {done} 题（AI 与标准答案一致 {correct_n}、'
                    f'不一致 {wrong_n}、无法判定 {uncertain_n}）')
    except Exception as e:
        upd(status='failed', message='失败：' + ''.join(
            traceback.format_exception_only(e)).strip()[:400])


# ---------------------------------------------------------------
# ⑥ token 成本统计
# ---------------------------------------------------------------
def unit_price(model):
    for p in PROVIDERS.values():
        if model in p['price']:
            return p['price'][model]
    return DEFAULT_PRICE


def verify_stats():
    """返回 (汇总行列表, verdict 计数, 待验证数)"""
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT model, COUNT(*) calls, SUM(prompt_tokens) pt, "
                "SUM(completion_tokens) ct, ROUND(AVG(latency_ms)) avg_ms "
                "FROM ai_verification GROUP BY model")
            rows = cur.fetchall()
            for r in rows:
                inp, outp = unit_price(r['model'])
                r['cost'] = round(float(r['pt'] or 0) / 1e6 * inp +
                                  float(r['ct'] or 0) / 1e6 * outp, 4)
            cur.execute("SELECT verdict, COUNT(*) c FROM ai_verification "
                        "GROUP BY verdict")
            verdicts = {r['verdict']: r['c'] for r in cur.fetchall()}
            cur.execute(
                "SELECT COUNT(*) c FROM question q LEFT JOIN ai_verification av "
                "ON av.question_id=q.id AND av.model=(SELECT model FROM llm_config "
                "ORDER BY id DESC LIMIT 1) WHERE av.id IS NULL")
            pending = cur.fetchone()['c']
            return rows, verdicts, pending
    finally:
        conn.close()


def recent_results(limit=50, verdict=None):
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            where = 'WHERE av.verdict=%s' if verdict else ''
            args = (verdict,) if verdict else ()
            cur.execute(
                f"SELECT av.*, LEFT(q.stem, 44) stem, q.qtype, "
                f"(SELECT GROUP_CONCAT(o.label ORDER BY o.label) FROM `option` o "
                f" WHERE o.question_id=av.question_id AND o.is_correct) correct_labels "
                f"FROM ai_verification av "
                f"JOIN question q ON q.id=av.question_id {where} "
                f"ORDER BY av.id DESC LIMIT {int(limit)}", args)
            return cur.fetchall()
    finally:
        conn.close()
