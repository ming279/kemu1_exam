-- =============================================================
-- migration_v4.sql - 题目维护功能：软删除标记
-- 执行：mysql -u root -p kemu1_exam < migration_v4.sql
-- 重复执行报 Duplicate column 可忽略
-- =============================================================
USE kemu1_exam;

-- 题目软删除：删除只打标记，历史成绩(exam_detail)/练习/错题本/任务/PK 全部不受影响；
-- 抽题/组卷/列表/统计处按 is_deleted=0 过滤
ALTER TABLE question
  ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0
    COMMENT '软删除标记：1=已删除（不出现在抽题/列表，历史数据不受影响）',
  ADD INDEX idx_q_deleted (is_deleted);

-- 每题统计视图排除已删题
CREATE OR REPLACE VIEW v_question_stat AS
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
) s ON s.question_id = q.id
WHERE q.is_deleted = 0;
