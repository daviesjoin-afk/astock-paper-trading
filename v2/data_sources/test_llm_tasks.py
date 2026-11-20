#!/usr/bin/env python3
"""Offline tests for the LLM task contracts.

No network, no model, no API key: these check the parts that must be correct
before a model is ever called — that a job refuses to run without its data,
that malformed answers are rejected rather than repaired, and that a failure is
reported as a failure.
"""
from __future__ import annotations

import json
import unittest

import llm_tasks
from llm_runner import Failure, run, strip_code_fence


class TaskRegistryTests(unittest.TestCase):
    def test_every_task_declares_its_boundaries(self):
        for name, task in llm_tasks.TASKS.items():
            with self.subTest(task=name):
                self.assertTrue(task.system_prompt.strip(), f"{name} has no system prompt")
                self.assertTrue(task.user_template.strip(), f"{name} has no user template")
                self.assertTrue(task.required_inputs, f"{name} declares no required inputs")
                self.assertTrue(callable(task.validator))
                self.assertTrue(task.notes, f"{name} has no note about when it may run")
                # Every prompt must carry the no-fabrication rule.
                self.assertIn("严禁编造", task.system_prompt)

    def test_an_unknown_task_is_a_structured_failure(self):
        with self.assertRaises(llm_tasks.TaskError) as caught:
            llm_tasks.get("does_not_exist")
        self.assertEqual(caught.exception.code, "unknown_task")

    def test_a_job_without_its_data_refuses_to_run(self):
        task = llm_tasks.get("flow_reading")
        missing = llm_tasks.missing_inputs(task, {"symbol": "600000.SH"})
        self.assertEqual(missing, ["flows"])
        with self.assertRaises(llm_tasks.TaskError) as caught:
            llm_tasks.render(task, {"symbol": "600000.SH"})
        self.assertEqual(caught.exception.code, "data_unavailable")
        self.assertIn("flows", caught.exception.detail)

    def test_an_empty_value_counts_as_missing(self):
        # An empty list is not "no flows today", it is "no data supplied".
        task = llm_tasks.get("flow_reading")
        self.assertEqual(
            llm_tasks.missing_inputs(task, {"symbol": "x", "flows": []}), ["flows"]
        )
        self.assertEqual(
            llm_tasks.missing_inputs(task, {"symbol": "x", "flows": {}}), ["flows"]
        )

    def test_rendering_injects_the_payload_as_json(self):
        task = llm_tasks.get("signal_review")
        _, user = llm_tasks.render(
            task,
            {
                "symbol": "600000.SH",
                "signal": "buy",
                "basis": {"score": 72, "reason": "fresh_breakout"},
                "context": {"positions": 2, "regime": "trending"},
            },
        )
        self.assertIn("600000.SH", user)
        self.assertIn("fresh_breakout", user)
        self.assertIn('"score": 72', user)


class ContractTests(unittest.TestCase):
    def test_news_items_must_cite_a_reason(self):
        base = {"ref": "a1", "sentiment": "positive", "impact": "high", "reason": "提价"}
        self.assertEqual(
            llm_tasks.validate_news_impact({"items": [base]}), {"items": [base]}
        )
        without = dict(base, reason="")
        with self.assertRaises(llm_tasks.TaskError) as caught:
            llm_tasks.validate_news_impact({"items": [without]})
        self.assertEqual(caught.exception.code, "schema_validation_failed")

    def test_news_rejects_an_unknown_sentiment_label(self):
        bad = {"ref": "a1", "sentiment": "bullish", "impact": "high", "reason": "x"}
        with self.assertRaises(llm_tasks.TaskError):
            llm_tasks.validate_news_impact({"items": [bad]})

    def test_pnl_attribution_must_sum_to_the_reported_total(self):
        good = {
            "total_bps": 150,
            "attribution": [
                {"source": "选股", "bps": 100, "evidence": "前 20 只涨幅领先"},
                {"source": "成本", "bps": -40, "evidence": "手续费 3.8 万"},
                {"source": "运气", "bps": 90, "evidence": "无法归因残差"},
            ],
            "notes": "x",
        }
        llm_tasks.validate_pnl_review(good)

        # 100 - 40 + 10 = 70, but the total says 150: a story, not an
        # explanation. This is the check that makes attribution honest.
        lying = {
            "total_bps": 150,
            "attribution": [
                {"source": "选股", "bps": 100, "evidence": "a"},
                {"source": "成本", "bps": -40, "evidence": "b"},
                {"source": "运气", "bps": 10, "evidence": "c"},
            ],
            "notes": "x",
        }
        with self.assertRaises(llm_tasks.TaskError) as caught:
            llm_tasks.validate_pnl_review(lying)
        self.assertIn("sums to", caught.exception.detail)

    def test_pnl_allows_rounding_slack_but_not_more(self):
        def build(total: int) -> dict:
            return {
                "total_bps": total,
                "attribution": [{"source": "选股", "bps": 100, "evidence": "e"}],
                "notes": "n",
            }

        llm_tasks.validate_pnl_review(build(103))  # inside 5 bps
        with self.assertRaises(llm_tasks.TaskError):
            llm_tasks.validate_pnl_review(build(130))

    def test_pnl_rejects_a_numeric_string(self):
        payload = {
            "total_bps": "150",
            "attribution": [{"source": "选股", "bps": 150, "evidence": "e"}],
            "notes": "n",
        }
        with self.assertRaises(llm_tasks.TaskError) as caught:
            llm_tasks.validate_pnl_review(payload)
        self.assertIn("must be numeric", caught.exception.detail)

    def test_signal_review_verdict_vocabulary_is_closed(self):
        ok = {"verdict": "object", "reasons": ["资金流出与突破不符"]}
        llm_tasks.validate_signal_review(ok)
        for bad in ("buy", "strong_sell", "yes"):
            with self.assertRaises(llm_tasks.TaskError):
                llm_tasks.validate_signal_review({"verdict": bad, "reasons": ["x"]})
        with self.assertRaises(llm_tasks.TaskError):
            llm_tasks.validate_signal_review({"verdict": "agree", "reasons": []})

    def test_evolution_may_not_invent_a_parameter(self):
        good = {
            "proposals": [
                {
                    "parameter": "stop_loss_bps",
                    "current": 700,
                    "proposed": 900,
                    "hypothesis": "止损过紧",
                    "test_plan": "在 2023-2024 训练、2025 验证",
                }
            ]
        }
        llm_tasks.validate_evolution_proposal(good)

        invented = json.loads(json.dumps(good))
        invented["proposals"][0]["parameter"] = "leverage"
        with self.assertRaises(llm_tasks.TaskError) as caught:
            llm_tasks.validate_evolution_proposal(invented)
        self.assertIn("not allowed", caught.exception.detail)

    def test_evolution_rejects_a_no_op_and_a_missing_test_plan(self):
        base = {
            "parameter": "entry_score",
            "current": 40,
            "proposed": 40,
            "hypothesis": "h",
            "test_plan": "p",
        }
        with self.assertRaises(llm_tasks.TaskError):
            llm_tasks.validate_evolution_proposal({"proposals": [base]})
        without = dict(base, proposed=50, test_plan="  ")
        with self.assertRaises(llm_tasks.TaskError):
            llm_tasks.validate_evolution_proposal({"proposals": [without]})


class RunnerTests(unittest.TestCase):
    def test_a_fenced_block_is_unwrapped(self):
        self.assertEqual(strip_code_fence('```json\n{"a":1}\n```'), '{"a":1}')
        self.assertEqual(strip_code_fence('```\n{"a":1}\n```'), '{"a":1}')
        self.assertEqual(strip_code_fence('{"a":1}'), '{"a":1}')

    def test_a_missing_input_is_reported_not_improvised(self):
        record = run(
            "flow_reading",
            {"symbol": "600000.SH"},
            base_url="http://127.0.0.1:1/v1",
            api_key="unused",
            model="test",
        )
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["code"], "data_unavailable")
        self.assertIn("flows", record["detail"])

    def test_an_unreachable_backend_is_a_network_failure(self):
        record = run(
            "news_impact",
            {"symbol": "600000.SH", "name": "浦发银行", "materials": [{"id": "a1", "text": "提价"}]},
            base_url="http://127.0.0.1:1/v1",
            api_key="unused",
            model="test",
            timeout=2.0,
        )
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["code"], "network_error")

    def test_a_failed_job_never_raises(self):
        # The pipeline must be able to run every job in a batch and report the
        # whole picture, so a failure is a record, not an exception.
        for task in ("news_impact", "pnl_review", "evolution_proposal"):
            record = run(task, {}, base_url="http://127.0.0.1:1/v1",
                         api_key="x", model="m", timeout=1.0)
            self.assertEqual(record["status"], "failed")
            self.assertIn("code", record)
            self.assertNotIn("result", record)


if __name__ == "__main__":
    unittest.main()

class TestFlowReadingAvailability(unittest.TestCase):
    """The order-flow task must state what it needs, not fake a substitute."""

    def test_flow_task_names_the_fields_it_needs(self):
        task = llm_tasks.TASKS["flow_reading"]
        self.assertTrue(task.required_fields, "flow_reading must declare required_fields")
        for field in task.required_fields:
            self.assertIn("inflow", field)

    def test_flow_task_explains_why_the_data_is_unavailable(self):
        task = llm_tasks.TASKS["flow_reading"]
        self.assertIn("Level-2", task.unavailable_reason)
        self.assertIn("成交量", task.unavailable_reason)

    def test_flow_task_forbids_substituting_volume(self):
        prompt = llm_tasks.TASKS["flow_reading"].system_prompt
        self.assertIn("data_unavailable", prompt)
        self.assertIn("成交量", prompt)

    def test_other_tasks_do_not_declare_phantom_fields(self):
        for name, task in llm_tasks.TASKS.items():
            if name == "flow_reading":
                continue
            self.assertEqual(task.required_fields, (),
                             f"{name} should not declare unavailable fields")
