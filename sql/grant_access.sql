-- Run as a MySQL admin once the database access is approved.
-- A dedicated login that can only read and replace rows in this one table.
-- Replace the password, then put the same user/password in .env.

CREATE USER IF NOT EXISTS 'search_term_loader'@'%' IDENTIFIED BY 'change-me';

-- Hourly load. DB_LOAD_MODE=replace: DELETE the dates being replaced, INSERT the new rows.
-- DB_LOAD_MODE=upsert / insert: INSERT ... ON DUPLICATE KEY UPDATE, which needs UPDATE
-- (insert mode's no-op "id = id" update needs it too).
GRANT SELECT, INSERT, UPDATE, DELETE ON gen_ai_db.searchtermreport TO 'search_term_loader'@'%';

-- Only for the one-time `python run.py --create-table` (or create the table as admin
-- with sql/create_searchtermreport.sql and skip this line):
-- GRANT CREATE ON gen_ai_db.* TO 'search_term_loader'@'%';

-- Read-only access for Power BI / analysts:
-- GRANT SELECT ON gen_ai_db.searchtermreport TO 'powerbi_reader'@'%';
