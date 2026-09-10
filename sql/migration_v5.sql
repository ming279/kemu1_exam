-- =============================================================
-- migration_v5.sql - 收藏夹 + 考试切屏记录
-- 执行：sudo mysql kemu1_exam < migration_v5.sql
-- 重复执行报 Duplicate column/table 可忽略
-- =============================================================
USE kemu1_exam;

-- 题目收藏夹（学生自主标记，与系统判定的错题本互补）
CREATE TABLE IF NOT EXISTS favorite (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    question_id INT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_fav_uq (user_id, question_id),
    CONSTRAINT fk_fav_u FOREIGN KEY (user_id) REFERENCES `user` (id) ON DELETE CASCADE,
    CONSTRAINT fk_fav_q FOREIGN KEY (question_id) REFERENCES question (id) ON DELETE CASCADE
) COMMENT '题目收藏夹';

-- 考试防作弊增强：累计离开秒数（切屏次数 switch_count 已在 v3 迁移中）
-- 只记录不拦截，不影响成绩
ALTER TABLE exam_paper
  ADD COLUMN blur_sec INT NOT NULL DEFAULT 0 COMMENT '离开答题页累计秒数（切屏/切标签）';
