-- gen_ai_db.searchtermreport
-- Amazon Ads saved report "search_term_Automation" (Sponsored ads, Search term,
-- Hourly, All accounts), one row per date / hour / campaign / ad group / target /
-- search term.  Loaded every hour by Amazon_ad_search_term_automation_cron.
--
-- Create it once (needs CREATE on gen_ai_db):
--   python run.py --create-table
-- or run this statement yourself in MySQL Workbench / the mysql client.
CREATE TABLE IF NOT EXISTS searchtermreport (
  id                        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  report_date               DATE            NOT NULL COMMENT 'Date (each account''s own timezone)',
  hour                      TINYINT UNSIGNED NOT NULL COMMENT 'Hour 0-23',
  target_id                 BIGINT          NULL,
  target_bid                DECIMAL(12,2)   NULL,
  target_type               VARCHAR(32)     NULL,
  target_status             VARCHAR(32)     NULL,
  targeting                 VARCHAR(512)    NULL,
  targeting_match_type      VARCHAR(64)     NULL,
  campaign_id               BIGINT          NOT NULL,
  campaign_name             VARCHAR(512)    NOT NULL,
  campaign_budget_amount    DECIMAL(14,2)   NULL,
  campaign_delivery_status  VARCHAR(32)     NULL,
  campaign_country          VARCHAR(8)      NOT NULL COMMENT 'Marketplace: US, CA, MX, GB, DE, FR, IT ...',
  budget_currency           VARCHAR(8)      NULL,
  ad_group_id               BIGINT          NULL,
  ad_group_name             VARCHAR(512)    NULL,
  portfolio_id              BIGINT          NULL COMMENT '-1 = No Portfolio',
  portfolio_name            VARCHAR(255)    NULL,
  country                   VARCHAR(64)     NULL,
  country_code              VARCHAR(8)      NULL,
  search_term               VARCHAR(512)    NULL,
  impressions               INT             NOT NULL DEFAULT 0,
  clicks                    INT             NOT NULL DEFAULT 0,
  total_cost                DECIMAL(14,5)   NOT NULL DEFAULT 0,
  purchases                 INT             NOT NULL DEFAULT 0,
  purchases_multi_touch     DECIMAL(14,5)   NOT NULL DEFAULT 0,
  sales                     DECIMAL(14,2)   NOT NULL DEFAULT 0,
  sales_multi_touch         DECIMAL(14,5)   NOT NULL DEFAULT 0,
  units_sold                INT             NOT NULL DEFAULT 0,
  units_sold_multi_touch    DECIMAL(14,5)   NOT NULL DEFAULT 0,
  cpc                       DECIMAL(14,5)   NULL,
  ctr                       DECIMAL(9,4)    NULL COMMENT 'Percent: 2.5 = 2.5%',
  report_id                 CHAR(36)        NULL COMMENT 'Amazon saved report the row came from',
  loaded_at                 DATETIME        NOT NULL COMMENT 'UTC time of the load',
  row_key                   CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT 'SHA-256 of date|hour|country|campaign|ad group|target|search term (upsert key)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_row_key (row_key),
  KEY ix_date_country (report_date, campaign_country),
  KEY ix_campaign_date (campaign_id, report_date),
  KEY ix_search_term (search_term(100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
