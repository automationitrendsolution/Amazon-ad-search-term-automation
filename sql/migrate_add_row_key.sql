-- One-time migration: add row_key to a gen_ai_db.searchtermreport table
-- created before it existed, so DB_LOAD_MODE=upsert / insert can be used.
--
-- Not needed for a table created with the current
-- sql/create_searchtermreport.sql (it already has row_key).
--
-- Run as a MySQL admin (needs ALTER), with the hourly job paused, e.g.
--   mysql -h <host> -u <admin> -p gen_ai_db < sql/migrate_add_row_key.sql
--
-- row_key must be computed exactly as app/search_term_processor.py:row_key():
-- SHA-256 hex of "date|hour|country|campaign|ad group|target|search term",
-- with NULL written as ''.

USE gen_ai_db;

-- 1. Add the column (nullable while it is filled in).
ALTER TABLE searchtermreport
  ADD COLUMN row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL
    COMMENT 'SHA-256 of date|hour|country|campaign|ad group|target|search term (upsert key)'
    AFTER loaded_at;

-- 2. Fill it for the existing rows.
UPDATE searchtermreport
   SET row_key = SHA2(CONCAT(
         report_date, '|', hour, '|', campaign_country, '|', campaign_id, '|',
         COALESCE(ad_group_id, ''), '|', COALESCE(target_id, ''), '|',
         COALESCE(search_term, '')), 256);

-- 3. Must return no rows. If it does, the same row is stored twice: delete the
--    older copies (lower id) before step 4, or step 4 fails.
SELECT row_key, COUNT(*) AS copies, MIN(id) AS oldest_id, MAX(id) AS newest_id
  FROM searchtermreport
 GROUP BY row_key
HAVING COUNT(*) > 1
 LIMIT 20;

-- 4. Make it required and unique.
ALTER TABLE searchtermreport
  MODIFY row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL
    COMMENT 'SHA-256 of date|hour|country|campaign|ad group|target|search term (upsert key)',
  ADD UNIQUE KEY uk_row_key (row_key);
