-- =============================================================
-- migration_v6：错题本间隔重复（SRS）
-- 新增 next_review（下次复习日期），把错题本从"仓库"变成"队列"
-- 执行：mysql -u root -p kemu1 < sql/migration_v6.sql
-- 未执行也不影响系统：代码会自动降级为旧逻辑（见 app/main.py _srs_enabled）
-- =============================================================

ALTER TABLE wrong_book
    ADD COLUMN next_review DATE NULL DEFAULT NULL
        COMMENT '下次复习日期（SRS）；NULL 视为已到期' AFTER last_wrong_at,
    ADD INDEX idx_wb_review (user_id, mastered, next_review);

-- 存量数据回填：以最后一次答错日期作为复习起点（基本都是"已到期"）
UPDATE wrong_book SET next_review = DATE(last_wrong_at) WHERE next_review IS NULL;

-- 复习节奏（写死在 app/main.py）：
--   答错           -> next_review = 今天（当天即可再练）
--   连对 1 次      -> next_review = 今天 + 3 天
--   连对 2 次      -> mastered = 1（移出错题本）
