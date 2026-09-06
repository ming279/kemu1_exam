-- =============================================================
-- 驾照科目一题库管理与模拟考试系统 - 数据库建库脚本
-- MySQL 8.0  |  字符集 utf8mb4  |  引擎 InnoDB
-- =============================================================

DROP DATABASE IF EXISTS kemu1_exam;
CREATE DATABASE kemu1_exam DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE kemu1_exam;

-- -------------------------------------------------------------
-- 1. 题目分类表（支持层级分类，供加分项①自动归类写入）
-- -------------------------------------------------------------
CREATE TABLE category (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(100) NOT NULL UNIQUE COMMENT '分类名称',
    parent_id   INT NULL COMMENT '父分类ID，NULL为顶级',
    CONSTRAINT fk_cat_parent FOREIGN KEY (parent_id) REFERENCES category (id)
) COMMENT '题目分类';

-- -------------------------------------------------------------
-- 2. 图片表（BLOB 存储，SHA-256 内容哈希去重，解决图片存储问题）
-- -------------------------------------------------------------
CREATE TABLE image (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    content_hash CHAR(64) NOT NULL UNIQUE COMMENT 'SHA-256内容哈希，去重键',
    mime_type    VARCHAR(50) NOT NULL COMMENT 'MIME类型',
    file_size    INT NOT NULL COMMENT '字节数',
    data         LONGBLOB NOT NULL COMMENT '图片二进制数据',
    ref_count    INT NOT NULL DEFAULT 0 COMMENT '被题目引用次数'
) COMMENT '题库图片';

-- -------------------------------------------------------------
-- 3. 题目表
-- -------------------------------------------------------------
CREATE TABLE question (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    source_id    INT NULL COMMENT '原始文档题号（供核对）',
    stem         TEXT NOT NULL COMMENT '题干',
    qtype        ENUM('judge','single','multi') NOT NULL COMMENT '题型：判断/单选/多选',
    category_id  INT NULL COMMENT '所属分类',
    year_version VARCHAR(20) NOT NULL DEFAULT '2026' COMMENT '题库年份版本（增量导入用）',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_q_cat FOREIGN KEY (category_id) REFERENCES category (id),
    FULLTEXT KEY ft_stem (stem) WITH PARSER ngram COMMENT '全文索引，供重复题检索'
) COMMENT '题目';

-- -------------------------------------------------------------
-- 4. 选项表（判断题归一化为"正确/错误"两个选项）
-- -------------------------------------------------------------
CREATE TABLE `option` (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    question_id INT NOT NULL,
    label       CHAR(1) NOT NULL COMMENT '选项标号 A/B/C/D',
    content     TEXT NOT NULL COMMENT '选项内容',
    is_correct  BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否为正确答案',
    UNIQUE KEY uq_qo (question_id, label),
    CONSTRAINT fk_opt_q FOREIGN KEY (question_id) REFERENCES question (id) ON DELETE CASCADE
) COMMENT '题目选项';

-- -------------------------------------------------------------
-- 5. 题目-图片关联表（一对多）
-- -------------------------------------------------------------
CREATE TABLE question_image (
    question_id INT NOT NULL,
    image_id    INT NOT NULL,
    position    TINYINT NOT NULL DEFAULT 0 COMMENT '图在题内出现顺序',
    PRIMARY KEY (question_id, image_id),
    CONSTRAINT fk_qi_q FOREIGN KEY (question_id) REFERENCES question (id) ON DELETE CASCADE,
    CONSTRAINT fk_qi_i FOREIGN KEY (image_id) REFERENCES image (id) ON DELETE CASCADE
) COMMENT '题目与图片关联';

-- -------------------------------------------------------------
-- 6. 用户表
-- -------------------------------------------------------------
CREATE TABLE `user` (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(50) NOT NULL UNIQUE COMMENT '登录账号',
    password_hash CHAR(64) NOT NULL COMMENT 'SHA-256密码摘要',
    real_name     VARCHAR(50) NULL COMMENT '姓名',
    role          ENUM('admin','student') NOT NULL DEFAULT 'student' COMMENT '角色',
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) COMMENT '用户';

-- -------------------------------------------------------------
-- 7. 试卷表（模拟考试）
-- -------------------------------------------------------------
CREATE TABLE exam_paper (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT NOT NULL,
    total_count  INT NOT NULL COMMENT '组卷题目数',
    score        DECIMAL(5,2) NULL COMMENT '得分',
    status       ENUM('in_progress','finished') NOT NULL DEFAULT 'in_progress',
    started_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    submitted_at TIMESTAMP NULL,
    CONSTRAINT fk_ep_u FOREIGN KEY (user_id) REFERENCES `user` (id)
) COMMENT '模拟考试试卷';

-- -------------------------------------------------------------
-- 8. 试卷明细表（每题作答与判分）
-- -------------------------------------------------------------
CREATE TABLE exam_detail (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    paper_id    INT NOT NULL,
    question_id INT NOT NULL,
    seq_no      INT NOT NULL COMMENT '卷内题号',
    user_answer VARCHAR(10) NULL COMMENT '用户答案，如 A / √ / ABD',
    is_correct  BOOLEAN NULL COMMENT '判分结果，未答为NULL',
    UNIQUE KEY uq_pq (paper_id, seq_no),
    UNIQUE KEY uq_pqq (paper_id, question_id),
    CONSTRAINT fk_ed_p FOREIGN KEY (paper_id) REFERENCES exam_paper (id) ON DELETE CASCADE,
    CONSTRAINT fk_ed_q FOREIGN KEY (question_id) REFERENCES question (id)
) COMMENT '试卷明细';

-- -------------------------------------------------------------
-- 9. 练习记录表（顺序练习/错题重练均记录于此）
-- -------------------------------------------------------------
CREATE TABLE practice (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT NOT NULL,
    question_id  INT NOT NULL,
    user_answer  VARCHAR(10) NULL,
    is_correct   BOOLEAN NOT NULL,
    practiced_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_uq (user_id, question_id),
    INDEX idx_time (practiced_at),
    CONSTRAINT fk_pr_u FOREIGN KEY (user_id) REFERENCES `user` (id),
    CONSTRAINT fk_pr_q FOREIGN KEY (question_id) REFERENCES question (id)
) COMMENT '练习记录';

-- -------------------------------------------------------------
-- 10. 错题本表
-- -------------------------------------------------------------
CREATE TABLE wrong_book (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    question_id   INT NOT NULL,
    wrong_count   INT NOT NULL DEFAULT 1 COMMENT '累计错误次数',
    last_wrong_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    mastered      BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否已掌握（移出错题本）',
    UNIQUE KEY uq_uq (user_id, question_id),
    CONSTRAINT fk_wb_u FOREIGN KEY (user_id) REFERENCES `user` (id),
    CONSTRAINT fk_wb_q FOREIGN KEY (question_id) REFERENCES question (id)
) COMMENT '错题本';

-- -------------------------------------------------------------
-- 11. 题库采集批次表（加分项④：网上爬题增量导入的过程记录）
-- -------------------------------------------------------------
CREATE TABLE import_batch (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    source_type  ENUM('public_source','web_url') NOT NULL COMMENT '采集方式：公开源/指定网站',
    source_name  VARCHAR(200) NOT NULL COMMENT '来源名称',
    source_url   VARCHAR(500) NULL COMMENT '来源地址',
    year_version VARCHAR(20) NOT NULL COMMENT '导入题目标记的年份版本',
    fetched      INT NOT NULL DEFAULT 0 COMMENT '抓取到的题目数',
    imported     INT NOT NULL DEFAULT 0 COMMENT '去重后新入库题数',
    duplicates   INT NOT NULL DEFAULT 0 COMMENT '判定重复跳过数',
    status       ENUM('running','done','failed') NOT NULL DEFAULT 'running',
    message      TEXT NULL COMMENT '进度/结果日志',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) COMMENT '题库采集批次';

-- -------------------------------------------------------------
-- 12. LLM 配置表（加分项③⑥：用户自填 openai 兼容接口与模型）
-- -------------------------------------------------------------
CREATE TABLE llm_config (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    provider    VARCHAR(50) NOT NULL COMMENT '服务商标识',
    base_url    VARCHAR(200) NOT NULL COMMENT 'API 地址（openai 兼容）',
    api_key     VARCHAR(200) NOT NULL COMMENT 'API Key',
    model       VARCHAR(100) NOT NULL COMMENT '模型名',
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) COMMENT 'LLM 接口配置';

-- -------------------------------------------------------------
-- 13. AI 答案验证结果表（加分项③：逐题验证 + ⑥：token 用量）
-- -------------------------------------------------------------
CREATE TABLE ai_verification (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    question_id       INT NOT NULL,
    model             VARCHAR(100) NOT NULL,
    verdict           ENUM('correct','wrong','uncertain') NOT NULL COMMENT 'AI 判定：正确/错误/存疑',
    ai_answer         VARCHAR(50) NOT NULL COMMENT 'AI 给出的答案',
    reason            VARCHAR(500) NULL COMMENT '判定理由',
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    latency_ms        INT NOT NULL DEFAULT 0,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_qm (question_id, model),
    CONSTRAINT fk_av_q FOREIGN KEY (question_id) REFERENCES question (id) ON DELETE CASCADE
) COMMENT 'AI 答案验证结果与 token 用量';

-- -------------------------------------------------------------
-- 14. AI 验证批次表（后台任务进度）
-- -------------------------------------------------------------
CREATE TABLE verify_batch (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    model     VARCHAR(100) NOT NULL,
    scope     VARCHAR(20) NOT NULL COMMENT '验证范围：all/judge/single/multi',
    total     INT NOT NULL DEFAULT 0 COMMENT '本批计划验证题数',
    done      INT NOT NULL DEFAULT 0,
    correct_n INT NOT NULL DEFAULT 0,
    wrong_n   INT NOT NULL DEFAULT 0,
    uncertain_n INT NOT NULL DEFAULT 0,
    status    ENUM('running','done','failed') NOT NULL DEFAULT 'running',
    message   TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) COMMENT 'AI 验证批次';

-- =============================================================
-- 统计视图
-- =============================================================

-- 每题答题统计（管理员全局视角，含考试与练习两个来源）
CREATE VIEW v_question_stat AS
SELECT
    q.id            AS question_id,
    q.stem          AS stem,
    q.qtype         AS qtype,
    COALESCE(s.attempt_count, 0)  AS attempt_count,
    COALESCE(s.correct_count, 0)  AS correct_count,
    ROUND(COALESCE(s.correct_count, 0) * 100.0 / NULLIF(s.attempt_count, 0), 2) AS correct_rate
FROM question q
LEFT JOIN (
    SELECT question_id,
           COUNT(*)            AS attempt_count,
           SUM(is_correct)     AS correct_count
    FROM (
        SELECT question_id, is_correct FROM exam_detail WHERE is_correct IS NOT NULL
        UNION ALL
        SELECT question_id, is_correct FROM practice
    ) t
    GROUP BY question_id
) s ON s.question_id = q.id;

-- 用户个人统计（答题量/正确率/错题数/掌握度）
CREATE VIEW v_user_stat AS
SELECT
    u.id   AS user_id,
    u.username AS username,
    COALESCE(p.total_answered, 0)  AS total_answered,
    COALESCE(p.total_correct, 0)   AS total_correct,
    ROUND(COALESCE(p.total_correct, 0) * 100.0 / NULLIF(p.total_answered, 0), 2) AS accuracy,
    COALESCE(w.wrong_total, 0)     AS wrong_total,
    COALESCE(w.wrong_active, 0)    AS wrong_active
FROM `user` u
LEFT JOIN (
    SELECT user_id,
           COUNT(*)        AS total_answered,
           SUM(is_correct) AS total_correct
    FROM practice
    GROUP BY user_id
) p ON p.user_id = u.id
LEFT JOIN (
    SELECT user_id,
           COUNT(*)               AS wrong_total,
           SUM(NOT mastered)      AS wrong_active
    FROM wrong_book
    GROUP BY user_id
) w ON w.user_id = u.id;
