"""Amazon Ads Search term report automation.

Playwright runs the saved ``search_term_Automation`` report on the Amazon Ads
reporting/history page every hour, waits for it to finish, downloads the CSV,
validates it with pandas, and replaces its dates/marketplaces in
``gen_ai_db.searchtermreport``.

Built from Amazon_ad_targeting_report_cron: the browser session, sign-in,
selector, retry, lock and logging modules are reused from it.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
