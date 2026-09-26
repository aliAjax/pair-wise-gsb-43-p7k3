import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiHandler, DomainError, ProcurementService  # noqa: E402


class PerformanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        criteria = [{"name": "报价", "weight": 100, "kind": "cost", "max_value": 1000000}]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def award(self, price=300000):
        bid = self.service.submit_bid("vendor1", "vendor", self.tender["id"], self.vendor["id"], {"报价": price}, price)
        time.sleep(1.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": price})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.service.award_tender("sup1", "supervisor", self.tender["id"], current["version"])

    def create_contract(self):
        return self.service.create_contract(
            "sup1", "supervisor", self.tender["id"], "HT-001", "数据中心设备合同",
            [{"name": "设备到货", "amount": 120000},
             {"name": "安装调试", "amount": 100000},
             {"name": "验收上线", "amount": 80000}]
        )

    def test_full_performance_flow(self):
        self.award()
        view = self.create_contract()
        self.assertEqual("active", view["contract_status"])
        self.assertEqual(300000, view["total_amount"])
        self.assertEqual(1, view["current_milestone"]["seq"])
        self.assertIn("待供应商报量", view["todos"][0]["text"])
        for milestone in view["milestones"]:
            mid, amount = milestone["id"], milestone["amount"]
            reported = self.service.report_progress("vendor1", "vendor", mid, 100)
            self.assertEqual("reported", reported["status"])
            acceptance = self.service.accept_progress("proc1", "procurement", mid, amount)
            self.assertEqual("effective", acceptance["status"])
            payment = self.service.register_payment("sup1", "supervisor", mid, amount)
            self.assertEqual("effective", payment["status"])
        final = self.service.get_performance("sup1", "supervisor", self.tender["id"])
        self.assertEqual("completed", final["contract_status"])
        self.assertIsNone(final["current_milestone"])
        self.assertEqual([], final["todos"])
        self.assertEqual(300000, final["totals"]["paid"])
        self.assertEqual(10, final["contract_version"])
        self.assertEqual(10, len(final["versions"]))
        self.assertEqual(list(range(10, 0, -1)), [v["version"] for v in final["versions"]])

    def test_milestone_split_must_equal_contract_amount(self):
        self.award()
        with self.assertRaises(DomainError) as ctx:
            self.service.create_contract("sup1", "supervisor", self.tender["id"], "HT-001", "合同",
                                         [{"name": "一期", "amount": 100000}])
        self.assertIn("节点金额合计", str(ctx.exception))
        view = self.service.create_contract("sup1", "supervisor", self.tender["id"], "HT-001", "合同",
                                            [{"name": "一期", "amount": 150000}], total_amount=150000)
        self.assertEqual(150000, view["total_amount"])

    def test_contract_requires_awarded_tender_and_single_contract(self):
        with self.assertRaises(DomainError) as ctx:
            self.create_contract()
        self.assertEqual(409, ctx.exception.status)
        self.award()
        self.create_contract()
        with self.assertRaises(DomainError) as ctx2:
            self.create_contract()
        self.assertEqual(409, ctx2.exception.status)

    def test_duplicate_acceptance_returns_conflict(self):
        self.award()
        view = self.create_contract()
        mid = view["milestones"][0]["id"]
        with self.assertRaises(DomainError) as ctx:
            self.service.accept_progress("proc1", "procurement", mid, 120000)
        self.assertEqual(409, ctx.exception.status)
        self.service.report_progress("vendor1", "vendor", mid, 100)
        with self.assertRaises(DomainError) as ctx2:
            self.service.report_progress("vendor1", "vendor", mid, 100)
        self.assertEqual(409, ctx2.exception.status)
        acceptance = self.service.accept_progress("proc1", "procurement", mid, 120000)
        self.assertEqual("effective", acceptance["status"])
        with self.assertRaises(DomainError) as ctx3:
            self.service.accept_progress("proc1", "procurement", mid, 120000)
        self.assertEqual(409, ctx3.exception.status)

    def test_amount_mismatch_stops_at_pending_review(self):
        self.award()
        view = self.create_contract()
        mid = view["milestones"][0]["id"]
        self.service.report_progress("vendor1", "vendor", mid, 100)
        over = self.service.accept_progress("proc1", "procurement", mid, 130000)
        self.assertEqual("pending_review", over["status"])
        with self.assertRaises(DomainError) as ctx:
            self.service.accept_progress("proc1", "procurement", mid, 120000)
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.review_record("sup1", "supervisor", "acceptance", over["id"], "confirm")
        self.assertEqual(409, ctx2.exception.status)
        voided = self.service.review_record("sup1", "supervisor", "acceptance", over["id"], "void")
        self.assertEqual("void", voided["status"])
        mismatched = self.service.accept_progress("proc1", "procurement", mid, 110000)
        self.assertEqual("pending_review", mismatched["status"])
        confirmed = self.service.review_record("sup1", "supervisor", "acceptance", mismatched["id"], "confirm")
        self.assertEqual("effective", confirmed["status"])
        perf = self.service.get_performance("sup1", "supervisor", self.tender["id"])
        self.assertEqual("accepted", perf["milestones"][0]["status"])
        self.assertEqual(110000, perf["milestones"][0]["accepted_amount"])

    def test_overpayment_stops_at_pending_review(self):
        self.award()
        view = self.create_contract()
        mid = view["milestones"][0]["id"]
        early = self.service.register_payment("sup1", "supervisor", mid, 1000)
        self.assertEqual("pending_review", early["status"])
        self.service.review_record("sup1", "supervisor", "payment", early["id"], "void")
        self.service.report_progress("vendor1", "vendor", mid, 100)
        self.service.accept_progress("proc1", "procurement", mid, 120000)
        over = self.service.register_payment("sup1", "supervisor", mid, 130000)
        self.assertEqual("pending_review", over["status"])
        with self.assertRaises(DomainError) as ctx:
            self.service.review_record("sup1", "supervisor", "payment", over["id"], "confirm")
        self.assertEqual(409, ctx.exception.status)
        self.service.review_record("sup1", "supervisor", "payment", over["id"], "void")
        ok = self.service.register_payment("sup1", "supervisor", mid, 120000)
        self.assertEqual("effective", ok["status"])
        perf = self.service.get_performance("sup1", "supervisor", self.tender["id"])
        self.assertEqual("paid", perf["milestones"][0]["status"])

    def test_cumulative_payment_cannot_exceed_contract_amount(self):
        self.award()
        view = self.create_contract()
        for milestone in view["milestones"]:
            self.service.report_progress("vendor1", "vendor", milestone["id"], 100)
            self.service.accept_progress("proc1", "procurement", milestone["id"], milestone["amount"])
        self.service.register_payment("sup1", "supervisor", view["milestones"][0]["id"], 120000)
        self.service.register_payment("sup1", "supervisor", view["milestones"][1]["id"], 100000)
        over = self.service.register_payment("sup1", "supervisor", view["milestones"][2]["id"], 80001)
        self.assertEqual("pending_review", over["status"])
        with self.assertRaises(DomainError) as ctx:
            self.service.review_record("sup1", "supervisor", "payment", over["id"], "confirm")
        self.assertEqual(409, ctx.exception.status)
        perf = self.service.get_performance("sup1", "supervisor", self.tender["id"])
        self.assertEqual(220000, perf["totals"]["paid"])
        self.assertTrue(any("待复核付款" in t["text"] for t in perf["todos"]))

    def test_role_permissions(self):
        self.award()
        with self.assertRaises(DomainError) as ctx:
            self.service.create_contract("proc1", "procurement", self.tender["id"], "HT-001", "合同",
                                         [{"name": "一期", "amount": 300000}])
        self.assertEqual(403, ctx.exception.status)
        view = self.create_contract()
        mid = view["milestones"][0]["id"]
        with self.assertRaises(DomainError) as ctx2:
            self.service.report_progress("proc1", "procurement", mid, 100)
        self.assertEqual(403, ctx2.exception.status)
        with self.assertRaises(DomainError) as ctx3:
            self.service.report_progress("vendor2", "vendor", mid, 100)
        self.assertEqual(403, ctx3.exception.status)
        self.service.report_progress("vendor1", "vendor", mid, 100)
        with self.assertRaises(DomainError) as ctx4:
            self.service.accept_progress("vendor1", "vendor", mid, 120000)
        self.assertEqual(403, ctx4.exception.status)
        self.service.accept_progress("proc1", "procurement", mid, 120000)
        with self.assertRaises(DomainError) as ctx5:
            self.service.register_payment("proc1", "procurement", mid, 120000)
        self.assertEqual(403, ctx5.exception.status)
        with self.assertRaises(DomainError) as ctx6:
            self.service.review_record("proc1", "procurement", "payment", 1, "confirm")
        self.assertEqual(403, ctx6.exception.status)

    def test_public_view_hides_amounts(self):
        self.award()
        view = self.create_contract()
        mid = view["milestones"][0]["id"]
        self.service.report_progress("vendor1", "vendor", mid, 100)
        self.service.accept_progress("proc1", "procurement", mid, 120000)
        public = self.service.get_performance("", "public", self.tender["id"])
        self.assertNotIn("total_amount", public)
        self.assertNotIn("acceptances", public)
        self.assertNotIn("payments", public)
        self.assertNotIn("todos", public)
        self.assertEqual("accepted", public["milestones"][0]["status"])
        self.assertNotIn("amount", public["milestones"][0])
        self.assertEqual(1, public["progress"]["accepted"])
        state = self.service.state("", "public")["performance"]
        self.assertEqual(1, len(state))
        self.assertNotIn("total_amount", state[0])
        full = self.service.state("sup1", "supervisor")["performance"][0]
        self.assertIn("total_amount", full)
        self.assertIn("todos", full)
        vendor_state = self.service.state("vendor1", "vendor")["performance"]
        self.assertEqual(1, len(vendor_state))
        self.assertIn("total_amount", vendor_state[0])
        outsider = self.service.state("vendor2", "vendor")["performance"]
        self.assertEqual(0, len(outsider))
        with self.assertRaises(DomainError) as ctx:
            self.service.get_performance("vendor2", "vendor", self.tender["id"])
        self.assertEqual(403, ctx.exception.status)

    def test_current_milestone_and_todos_follow_progress(self):
        self.award()
        view = self.create_contract()
        mid = view["milestones"][0]["id"]
        self.service.report_progress("vendor1", "vendor", mid, 100)
        perf = self.service.get_performance("sup1", "supervisor", self.tender["id"])
        self.assertTrue(any("待采购人验收" in t["text"] for t in perf["todos"]))
        self.service.accept_progress("proc1", "procurement", mid, 120000)
        perf = self.service.get_performance("sup1", "supervisor", self.tender["id"])
        self.assertTrue(any("待监督员付款" in t["text"] for t in perf["todos"]))
        self.service.register_payment("sup1", "supervisor", mid, 120000)
        perf = self.service.get_performance("sup1", "supervisor", self.tender["id"])
        self.assertEqual(2, perf["current_milestone"]["seq"])
        self.assertTrue(any("待供应商报量" in t["text"] for t in perf["todos"]))


class PerformanceApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ApiHandler.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, user="", role="public"):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-User": user, "X-Role": role},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_performance_routes_and_errors(self):
        status, body = self.request("POST", "/api/contracts", {
            "tender_id": 1, "contract_no": "HT-1", "title": "合同",
            "milestones": [{"name": "一期", "amount": 1}],
        }, "sup1", "supervisor")
        self.assertEqual(404, status)
        self.assertIn("error", body)
        status, _ = self.request("POST", "/api/contracts", {
            "tender_id": 1, "contract_no": "HT-1", "title": "合同",
            "milestones": [{"name": "一期", "amount": 1}],
        }, "proc1", "procurement")
        self.assertEqual(403, status)
        status, body = self.request("GET", "/api/tenders/1/performance")
        self.assertEqual(404, status)
        status, _ = self.request("POST", "/api/performance/review",
                                 {"kind": "payment", "record_id": 1, "decision": "confirm"}, "proc1", "procurement")
        self.assertEqual(403, status)
        status, body = self.request("POST", "/api/performance/report",
                                    {"milestone_id": 1, "quantity": 50}, "vendor1", "vendor")
        self.assertEqual(404, status)
        status, body = self.request("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertIn("performance", body)


if __name__ == "__main__":
    unittest.main()
