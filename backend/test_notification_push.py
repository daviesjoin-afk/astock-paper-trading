# -*- coding: utf-8 -*-
"""WxPusher 日报/周报推送的离线测试。

覆盖：token 掩码与校验、报告内容计算、未启用/未配置的短路、
发送窗口判定。全部离线，不发起任何网络请求。
"""
import datetime as dt
import sqlite3
import unittest

import notify_wxpusher as NOTIFY
import push_report
import runtime_settings as RSET


def _paper_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE paper_nav(account_id TEXT, nav_date TEXT, nav REAL, benchmark REAL);
        CREATE TABLE paper_fills(account_id TEXT, fill_date TEXT);
        """
    )
    return conn


class TokenMaskTests(unittest.TestCase):
    def test_mask_never_reveals_the_full_token(self):
        token = "AT_abcdefghijklmnop"
        masked = NOTIFY.mask_token(token)
        self.assertNotEqual(token, masked)
        self.assertNotIn("abcdefghij", masked)
        self.assertTrue(masked.startswith("AT_a"))
        self.assertTrue(masked.endswith("mnop"))

    def test_short_and_empty_tokens_degrade_safely(self):
        self.assertEqual("", NOTIFY.mask_token(""))
        self.assertEqual("", NOTIFY.mask_token(None))
        self.assertEqual("****", NOTIFY.mask_token("AT_1"))


class SettingsValidationTests(unittest.TestCase):
    def test_token_must_start_with_at(self):
        with self.assertRaises(ValueError):
            RSET.validate({"wxpusher_app_token": "wrong-token"})

    def test_valid_token_is_accepted(self):
        checked = RSET.validate({"wxpusher_app_token": "AT_1234567890abcdef"})
        self.assertEqual("AT_1234567890abcdef", checked["wxpusher_app_token"])

    def test_empty_token_is_allowed_so_blank_means_keep(self):
        checked = RSET.validate({"wxpusher_app_token": "  "})
        self.assertEqual("", checked["wxpusher_app_token"])

    def test_time_format_is_enforced(self):
        for bad in ("25:00", "1540", "", "15-40"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                RSET.validate({"wxpusher_daily_time": bad})

    def test_valid_time_is_accepted(self):
        checked = RSET.validate({"wxpusher_daily_time": "09:05"})
        self.assertEqual("09:05", checked["wxpusher_daily_time"])

    def test_weekday_bounds_are_one_to_seven(self):
        checked = RSET.validate({"wxpusher_weekly_weekday": 1})
        self.assertEqual(1, checked["wxpusher_weekly_weekday"])
        with self.assertRaises(ValueError):
            RSET.validate({"wxpusher_weekly_weekday": 8})

    def test_topic_id_must_be_numeric_or_blank(self):
        self.assertEqual("", RSET.validate({"wxpusher_topic_id": ""})["wxpusher_topic_id"])
        self.assertEqual("123", RSET.validate({"wxpusher_topic_id": "123"})["wxpusher_topic_id"])
        with self.assertRaises(ValueError):
            RSET.validate({"wxpusher_topic_id": "abc"})

    def test_switches_accept_booleans(self):
        checked = RSET.validate({"wxpusher_enabled": True, "wxpusher_daily_enabled": 0})
        self.assertTrue(checked["wxpusher_enabled"])
        self.assertFalse(checked["wxpusher_daily_enabled"])


class ReportContentTests(unittest.TestCase):
    def _seed(self, conn):
        conn.executescript(
            """
            INSERT INTO paper_nav VALUES('tq_breakout','2026-10-20',60000.0,4544.59);
            INSERT INTO paper_nav VALUES('tq_breakout','2026-10-21',57000.0,4152.11);
            INSERT INTO paper_fills VALUES('tq_breakout','2026-10-21');
            """
        )

    def test_daily_report_computes_change_and_excess(self):
        conn = _paper_db()
        self._seed(conn)
        summary, content = NOTIFY.daily_report(conn, "2026-10-21")
        self.assertIn("模拟盘日报 2026-10-21", summary)
        # 净值 -5.00%，基准 -8.64% → 超额 +3.64%
        self.assertIn("-5.00%", content)
        self.assertIn("+3.64%", content)
        self.assertIn("当日成交合计 1 笔", content)
        self.assertIn("短线日内做T", content)

    def test_daily_report_without_nav_says_so_instead_of_zero(self):
        conn = _paper_db()
        summary, content = NOTIFY.daily_report(conn, "2026-10-21")
        self.assertIn("没有可用的净值记录", content)

    def test_weekly_report_uses_the_week_window(self):
        conn = _paper_db()
        conn.executescript(
            """
            INSERT INTO paper_nav VALUES('trend_pullback','2026-10-15',60000.0,4500.0);
            INSERT INTO paper_nav VALUES('trend_pullback','2026-10-21',63000.0,4200.0);
            INSERT INTO paper_fills VALUES('trend_pullback','2026-10-16');
            """
        )
        summary, content = NOTIFY.weekly_report(conn, "2026-10-21")
        self.assertIn("模拟盘周报 2026-10-21", summary)
        self.assertIn("+5.00%", content)
        self.assertIn("本周成交合计 1 笔", content)

    def test_weekly_report_measures_from_the_close_before_the_window(self):
        # 周五出报：窗口是上周六~本周五，窗口内第一个净值日是周一。
        # 若拿周一收盘当分母，周一的 +10% 会被整段漏掉（报成 +0.00%）。
        conn = _paper_db()
        conn.executescript(
            """
            INSERT INTO paper_nav VALUES('acct','2026-10-09',100.0,4000.0);
            INSERT INTO paper_nav VALUES('acct','2026-10-12',110.0,4040.0);
            INSERT INTO paper_nav VALUES('acct','2026-10-16',110.0,4080.0);
            """
        )
        _, content = NOTIFY.weekly_report(conn, "2026-10-16")
        self.assertIn("+10.00%", content)
        self.assertNotIn("+0.00%", content)

    def test_weekly_report_does_not_use_nav_rows_after_the_as_of_day(self):
        # 净值表里可能已经有 asof 之后的行：报告写的是"截至 asof"，不能拿它当收盘。
        conn = _paper_db()
        conn.executescript(
            """
            INSERT INTO paper_nav VALUES('acct','2026-10-09',100.0,4000.0);
            INSERT INTO paper_nav VALUES('acct','2026-10-16',120.0,4400.0);
            INSERT INTO paper_nav VALUES('acct','2026-10-19',200.0,4800.0);
            """
        )
        _, content = NOTIFY.weekly_report(conn, "2026-10-16")
        self.assertIn("+20.00%", content)

    def test_weekly_report_without_a_prior_close_starts_from_the_window(self):
        # 账户本周才有第一条净值：没有窗口前基准，退回窗口内第一天且不报错。
        conn = _paper_db()
        conn.executescript(
            """
            INSERT INTO paper_nav VALUES('acct','2026-10-12',100.0,4000.0);
            INSERT INTO paper_nav VALUES('acct','2026-10-16',105.0,4040.0);
            """
        )
        _, content = NOTIFY.weekly_report(conn, "2026-10-16")
        self.assertIn("+5.00%", content)


class DispatchTests(unittest.TestCase):
    def _settings(self, **overrides):
        base = {
            "wxpusher_enabled": True, "wxpusher_app_token": "AT_1234567890",
            "wxpusher_topic_id": "", "wxpusher_daily_enabled": True,
            "wxpusher_weekly_enabled": True,
        }
        base.update(overrides)
        return base

    def test_master_switch_blocks_everything(self):
        result = NOTIFY.dispatch(_paper_db(), "daily", self._settings(wxpusher_enabled=False))
        self.assertFalse(result["sent"])
        self.assertIn("总开关", result["reason"])

    def test_missing_token_blocks_instead_of_sending(self):
        result = NOTIFY.dispatch(_paper_db(), "daily", self._settings(wxpusher_app_token=""))
        self.assertFalse(result["sent"])
        self.assertIn("appToken", result["reason"])

    def test_disabled_kind_blocks(self):
        result = NOTIFY.dispatch(_paper_db(), "weekly", self._settings(wxpusher_weekly_enabled=False))
        self.assertFalse(result["sent"])
        self.assertIn("未启用", result["reason"])

    def test_sender_receives_markdown_content(self):
        conn = _paper_db()
        conn.executescript(
            "INSERT INTO paper_nav VALUES('tq_breakout','2026-10-21',57000.0,4152.11);"
        )
        captured = {}

        def sender(token, content, summary, topic_id):
            captured.update(token=token, content=content, summary=summary, topic=topic_id)

        result = NOTIFY.dispatch(conn, "daily", self._settings(), "2026-10-21", sender=sender)
        self.assertTrue(result["sent"])
        self.assertEqual("AT_1234567890", captured["token"])
        self.assertIn("模拟盘日报", captured["summary"])
        self.assertIn("## 模拟盘日报", captured["content"])

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            NOTIFY.dispatch(_paper_db(), "monthly", self._settings())


class AuditMaskingTests(unittest.TestCase):
    """设置审计随快照一起返回前端，凭据必须以掩码写入而不能落明文。"""

    def test_secret_audit_value_is_masked(self):
        raw = RSET._audit_json("wxpusher_app_token", "AT_verysecretvalue123")
        self.assertNotIn("verysecret", raw)
        self.assertIn("AT_v", raw)

    def test_non_secret_audit_value_is_verbatim(self):
        self.assertEqual('"15:40"', RSET._audit_json("wxpusher_daily_time", "15:40"))

    def test_secret_mask_handles_empty_and_short_values(self):
        self.assertEqual('""', RSET._audit_json("wxpusher_app_token", ""))
        self.assertEqual('"****"', RSET._audit_json("wxpusher_app_token", "AT_1"))


class ScheduleWindowTests(unittest.TestCase):
    def test_window_covers_ten_minutes_from_target(self):
        now = dt.datetime(2026, 10, 21, 15, 40, tzinfo=NOTIFY.TZ)
        self.assertTrue(push_report._in_window(now, "15:40"))
        self.assertTrue(push_report._in_window(now + dt.timedelta(minutes=9), "15:40"))
        self.assertFalse(push_report._in_window(now + dt.timedelta(minutes=10), "15:40"))
        self.assertFalse(push_report._in_window(now - dt.timedelta(minutes=1), "15:40"))

    def test_unparsable_target_is_never_in_window(self):
        now = dt.datetime(2026, 10, 21, 15, 40, tzinfo=NOTIFY.TZ)
        self.assertFalse(push_report._in_window(now, "bad"))
        self.assertFalse(push_report._in_window(now, ""))


if __name__ == "__main__":
    unittest.main()
