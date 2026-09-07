-- =============================================================
-- 功能扩展迁移脚本
-- 执行前确保已运行 schema.sql 建好基础表
-- =============================================================
USE kemu1_exam;

-- -------------------------------------------------------------
-- 功能⑥：question 加答案解析字段
-- -------------------------------------------------------------
ALTER TABLE question ADD COLUMN explanation TEXT NULL COMMENT '答案解析（学生交卷后可见）';

-- -------------------------------------------------------------
-- 功能①②：教师发布任务 + 学生参加任务
-- -------------------------------------------------------------
CREATE TABLE task (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    title         VARCHAR(200) NOT NULL COMMENT '任务标题',
    creator_uid   INT NOT NULL COMMENT '发布教师',
    judge_count   INT NOT NULL DEFAULT 40 COMMENT '判断题数量',
    single_count  INT NOT NULL DEFAULT 60 COMMENT '单选题数量',
    time_limit_sec INT NULL COMMENT '时限秒数（NULL=不限时）',
    mode          ENUM('exam','practice') NOT NULL COMMENT '考试/练习',
    purpose       ENUM('exam','review') NOT NULL DEFAULT 'exam' COMMENT '用途：考试（防作弊打乱）/讲解（不打乱）',
    question_ids  TEXT NULL COMMENT '预生成题目ID列表（逗号分隔）',
    shuffle_order BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否打乱顺序',
    status        ENUM('draft','published','closed') NOT NULL DEFAULT 'published',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at     DATETIME NULL,
    CONSTRAINT fk_task_creator FOREIGN KEY (creator_uid) REFERENCES `user` (id)
) COMMENT '教师发布的任务';

CREATE TABLE task_record (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    task_id       INT NOT NULL,
    uid           INT NOT NULL COMMENT '学生',
    paper_id      INT NULL COMMENT '关联试卷',
    status        ENUM('not_started','in_progress','completed','expired') NOT NULL DEFAULT 'not_started',
    start_time    DATETIME NULL COMMENT '开始作答时间',
    submit_time   DATETIME NULL COMMENT '交卷时间',
    elapsed_sec   INT NOT NULL DEFAULT 0 COMMENT '实际用时（练习含暂停）',
    score         DECIMAL(5,2) NULL,
    paused        BOOLEAN NOT NULL DEFAULT FALSE COMMENT '练习模式暂停中',
    pause_time    DATETIME NULL COMMENT '暂停时刻',
    UNIQUE KEY uq_task_user (task_id, uid),
    CONSTRAINT fk_tr_task FOREIGN KEY (task_id) REFERENCES task (id),
    CONSTRAINT fk_tr_u FOREIGN KEY (uid) REFERENCES `user` (id),
    CONSTRAINT fk_tr_paper FOREIGN KEY (paper_id) REFERENCES exam_paper (id)
) COMMENT '学生参加任务记录';

-- exam_paper 加 task_id 关联
ALTER TABLE exam_paper ADD COLUMN task_id INT NULL COMMENT '关联任务（NULL=默认模拟考）';

-- -------------------------------------------------------------
-- 功能⑦：双人PK
-- -------------------------------------------------------------
CREATE TABLE pk_challenge (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    challenger_uid  INT NOT NULL COMMENT '发起方',
    opponent_uid    INT NOT NULL COMMENT '被挑战方',
    question_ids    VARCHAR(500) NOT NULL COMMENT '10题ID逗号分隔',
    status          ENUM('waiting','ready','playing','finished','declined') NOT NULL DEFAULT 'waiting',
    current_q       INT NOT NULL DEFAULT 0 COMMENT '当前第几题（0-9）',
    challenger_score DECIMAL(5,2) NOT NULL DEFAULT 0,
    opponent_score  DECIMAL(5,2) NOT NULL DEFAULT 0,
    challenger_answers VARCHAR(20) NULL COMMENT '答题记录（逐题A/B/C/D或0=超时）',
    opponent_answers  VARCHAR(20) NULL,
    winner_uid      INT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     DATETIME NULL,
    CONSTRAINT fk_pk_c FOREIGN KEY (challenger_uid) REFERENCES `user` (id),
    CONSTRAINT fk_pk_o FOREIGN KEY (opponent_uid) REFERENCES `user` (id)
) COMMENT '双人PK对战';

-- user 加段位字段
ALTER TABLE `user` ADD COLUMN pk_wins INT NOT NULL DEFAULT 0 COMMENT 'PK胜场';
ALTER TABLE `user` ADD COLUMN pk_losses INT NOT NULL DEFAULT 0 COMMENT 'PK负场';
ALTER TABLE `user` ADD COLUMN win_streak INT NOT NULL DEFAULT 0 COMMENT '连胜场次';
