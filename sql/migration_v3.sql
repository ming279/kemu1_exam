-- =============================================================
-- migration_v3.sql  功能改进批次（2026-09）
-- 在 kemu1_exam 库执行；可重复执行（用 IF NOT EXISTS / 先查后加）说明：
--   MySQL 8.0 的 ALTER TABLE ADD COLUMN 尚不支持 IF NOT EXISTS，
--   重复执行报 Duplicate column 错误可忽略。
-- =============================================================
USE kemu1_exam;

-- -------------------------------------------------------------
-- 补漏：早期通过手动 ALTER 加的列，一并列出防止环境缺列
-- （重复执行报 Duplicate column 错误可忽略）
-- -------------------------------------------------------------
ALTER TABLE task
    ADD COLUMN target_uids TEXT NULL COMMENT '定向名单（NULL=全员，逗号分隔uid）';
ALTER TABLE `user`
    ADD COLUMN login_token VARCHAR(32) DEFAULT NULL COMMENT '最新登录令牌（互踢）';
ALTER TABLE wrong_book
    ADD COLUMN correct_streak INT NOT NULL DEFAULT 0 COMMENT '连续答对次数（连对2次自动掌握）';

-- C2 切屏检测：试卷记录考试期间切屏次数
ALTER TABLE exam_paper
    ADD COLUMN switch_count INT NOT NULL DEFAULT 0 COMMENT '考试期间切屏次数';

-- C24 自由模拟考限时：45 分钟贴近真实考试，NULL=不限时
ALTER TABLE exam_paper
    ADD COLUMN time_limit_sec INT NULL COMMENT '自由模拟考限时秒数（45分钟=2700，NULL不限时）';

-- C16 赛道皮肤：发起方创建对局时选择的皮肤（day/night/rain/desert）
ALTER TABLE pk_challenge
    ADD COLUMN theme VARCHAR(20) NULL COMMENT 'PK赛道皮肤';

-- C20 题目纠错上报（学生举报 -> 管理员审核）
CREATE TABLE question_report (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    question_id INT NOT NULL COMMENT '被举报的题目',
    uid         INT NOT NULL COMMENT '举报人',
    reason      VARCHAR(500) NOT NULL COMMENT '举报原因',
    status      ENUM('pending','resolved') NOT NULL DEFAULT 'pending' COMMENT '处理状态',
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_status (status),
    CONSTRAINT fk_rp_q FOREIGN KEY (question_id) REFERENCES question (id) ON DELETE CASCADE,
    CONSTRAINT fk_rp_u FOREIGN KEY (uid) REFERENCES `user` (id) ON DELETE CASCADE
) COMMENT '题目纠错上报';

-- C22 站内通知中心（任务发布 / 被 PK 挑战）
CREATE TABLE notification (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    uid        INT NOT NULL COMMENT '接收人',
    content    VARCHAR(200) NOT NULL COMMENT '通知内容',
    url        VARCHAR(200) NULL COMMENT '点击跳转地址',
    is_read    BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否已读',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_un (uid, is_read),
    CONSTRAINT fk_nt_u FOREIGN KEY (uid) REFERENCES `user` (id) ON DELETE CASCADE
) COMMENT '站内通知';
