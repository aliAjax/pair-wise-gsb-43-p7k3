import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class PerformanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test_perf.db")
        self.vendor = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "vendor-demo")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        tender = self.service.create_tender(
            "proc1", "procurement", "T-101", "机房改造",
            (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(), criteria,
        )
        tender = self.service.publish_tender("proc1", "procurement", tender["id"], tender["version"])
        bid = self.service.submit_bid("vendor-demo", "vendor", tender["id"], self.vendor["id"],
                                      {"报价": 800000, "质量": 90}, 800000)
        time.sleep(5.2)
        opened = self.service.open_bids("proc1", "procurement", tender["id"], tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        current = self.service.get_tender("sup1", "supervisor", tender["id"])["tender"]
        self.award = self.service.award_tender("sup1", "supervisor", tender["id"], current["version"])
        self.tender_id = tender["id"]
        self.bid = bid

    def tearDown(self):
        self.tmp.cleanup()

    def make_contract(self, amounts=(300000, 500000), nos=("C-101",)):
        nodes = [{"seq": i + 1, "name": "节点%d" % (i + 1), "amount": a} for i, a in enumerate(amounts)]
        result = self.service.create_contract("sup1", "supervisor", self.tender_id, nos[0], nodes)
        return result

    def test_contract_node_split_and_happy_path_payment(self):
        detail = self.make_contract()
        self.assertEqual(800000, detail["contract"]["total_amount"])
        n1, n2 = detail["nodes"][0]["id"], detail["nodes"][1]["id"]

        # 供应商报量 -> 采购人按匹配金额验收 -> 形成应付 -> 监督员登记付款
        self.service.report_completion("vendor-demo", "vendor", n1, 100)
        accepted = self.service.accept_completion("proc2", "procurement", n1, 300000)
        self.assertEqual("accepted", accepted["acceptances"][0]["status"])
        self.assertEqual(300000, accepted["payables"][0]["amount"])

        paid = self.service.register_payment("sup1", "supervisor", accepted["payables"][0]["id"], 300000)
        self.assertEqual("posted", paid["payments"][0]["status"])
        self.assertEqual(300000, paid["paid_total"])
        self.assertEqual("done", paid["nodes"][0]["status"])

        # 节点2全额完成、验收、付款后合同结清
        self.service.report_completion("vendor-demo", "vendor", n2, 100)
        d2 = self.service.accept_completion("proc2", "procurement", n2, 500000)
        d2 = self.service.register_payment("sup1", "supervisor", d2["payables"][-1]["id"], 500000)
        self.assertEqual("completed", d2["contract"]["status"])
        self.assertEqual(800000, d2["paid_total"])

    def test_node_amount_mismatch_parks_in_review_and_duplicate_conflicts(self):
        detail = self.make_contract()
        n1 = detail["nodes"][0]["id"]
        self.service.report_completion("vendor-demo", "vendor", n1, 50)  # 匹配额应为 150000

        # 金额与报量不匹配 -> 待复核，不形成应付
        mismatched = self.service.accept_completion("proc2", "procurement", n1, 120000)
        self.assertEqual("review", mismatched["acceptances"][0]["status"])
        self.assertEqual([], mismatched["payables"])

        # 同一节点重复提交验收 -> 409 冲突
        with self.assertRaises(DomainError) as ctx:
            self.service.accept_completion("proc2", "procurement", n1, 150000)
        self.assertEqual(409, ctx.exception.status)

        # 监督员驳回后才能重新验收
        self.service.resolve_acceptance("sup1", "supervisor", mismatched["acceptances"][0]["id"], "reject", note="金额不符")
        redone = self.service.accept_completion("proc2", "procurement", n1, 150000)
        self.assertEqual("accepted", redone["acceptances"][-1]["status"])
        self.assertEqual(150000, redone["payables"][-1]["amount"])
        # 部分验收节点状态
        self.assertEqual("partial", redone["nodes"][0]["status"])

    def test_supervisor_approves_review_with_corrected_amount(self):
        detail = self.make_contract()
        n1 = detail["nodes"][0]["id"]
        self.service.report_completion("vendor-demo", "vendor", n1, 100)
        review = self.service.accept_completion("proc2", "procurement", n1, 280000)
        self.assertEqual("review", review["acceptances"][0]["status"])
        approved = self.service.resolve_acceptance(
            "sup1", "supervisor", review["acceptances"][0]["id"], "approve", amount=300000
        )
        self.assertEqual("accepted", approved["acceptances"][0]["status"])
        self.assertEqual(300000, approved["payables"][-1]["amount"])
        self.assertEqual("done", approved["nodes"][0]["status"])

    def test_cumulative_acceptance_cannot_exceed_contract(self):
        detail = self.make_contract()
        n1, n2 = detail["nodes"][0]["id"], detail["nodes"][1]["id"]
        self.service.report_completion("vendor-demo", "vendor", n1, 100)
        d = self.service.accept_completion("proc2", "procurement", n1, 300000)
        self.service.report_completion("vendor-demo", "vendor", n2, 100)
        # 节点2最多还能验收 500000（合同额 800000）
        with self.assertRaises(DomainError) as ctx:
            self.service.accept_completion("proc2", "procurement", n2, 500001)
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(300000, d["accepted_total"])

    def test_node_overpay_parks_and_hard_caps_block(self):
        detail = self.make_contract(amounts=(200000, 600000))
        n1 = detail["nodes"][0]["id"]
        self.service.report_completion("vendor-demo", "vendor", n1, 100)
        d = self.service.accept_completion("proc2", "procurement", n1, 200000)
        payable_id = d["payables"][0]["id"]

        # 超付：登记成功但停在待复核，不生效
        parked = self.service.register_payment("sup1", "supervisor", payable_id, 250000)
        self.assertEqual("review", parked["payments"][0]["status"])
        self.assertEqual(0, parked["paid_total"])
        self.assertEqual("payable", parked["payables"][0]["status"])

        # 待复核期间正常付款也不能让累计超合同额；按可付额度内登记则直接生效
        ok = self.service.register_payment("sup1", "supervisor", payable_id, 200000)
        self.assertEqual("posted", ok["payments"][-1]["status"])
        self.assertEqual(200000, ok["paid_total"])

        # 应付已结清
        with self.assertRaises(DomainError) as ctx:
            self.service.register_payment("sup1", "supervisor", payable_id, 1)
        self.assertEqual(409, ctx.exception.status)

        # 原超付记录复核时即使原样确认仍被拒绝，驳回即可
        with self.assertRaises(DomainError):
            self.service.resolve_payment("sup1", "supervisor", parked["payments"][0]["id"], "approve")
        rejected = self.service.resolve_payment(
            "sup1", "supervisor", parked["payments"][0]["id"], "reject", note="超付"
        )
        self.assertEqual("rejected", rejected["payments"][0]["status"])

    def test_roles_permissions_and_public_view_hides_money(self):
        detail = self.make_contract()
        # 只有监督员能建合同
        with self.assertRaises(DomainError) as ctx:
            self.service.create_contract("proc2", "procurement", self.tender_id, "C-X",
                                         [{"seq": 1, "name": "x", "amount": 800000}])
        self.assertEqual(403, ctx.exception.status)
        # 同一授标项目不能重复建合同
        with self.assertRaises(DomainError) as ctx2:
            self.service.create_contract("sup1", "supervisor", self.tender_id, "C-102",
                                         [{"seq": 1, "name": "x", "amount": 800000}])
        self.assertEqual(409, ctx2.exception.status)

        n1 = detail["nodes"][0]["id"]
        # 未授标项目不能建合同（另建项目）
        with self.assertRaises(DomainError):
            self.service.create_contract("sup1", "supervisor", 999999, "C-NOPE",
                                         [{"seq": 1, "name": "x", "amount": 1}])

        self.service.report_completion("vendor-demo", "vendor", n1, 100)
        self.service.accept_completion("proc2", "procurement", n1, 300000)

        # 公开视图：只有状态，无任何金额明细
        pub = self.service.get_contract("anon", "public", detail["contract"]["id"])
        self.assertNotIn("total_amount", pub["contract"])
        self.assertNotIn("amount", pub["nodes"][0])
        self.assertNotIn("accepted_amount", pub["nodes"][0])
        self.assertEqual("done", pub["nodes"][0]["status"])
        self.assertEqual("accepted", pub["acceptances"][0]["status"])
        self.assertEqual([], pub["payments"])
        overview = self.service.contract_overview("anon", "public")
        self.assertNotIn("total_amount", overview["contracts"][0])

        # 待办按角色分发
        self.service.report_completion("vendor-demo", "vendor", detail["nodes"][1]["id"], 100)
        proc_todos = self.service.contract_overview("proc2", "procurement")["todos"]
        self.assertIn("acceptance_pending", {t["type"] for t in proc_todos})
        sup = self.service.contract_overview("sup1", "supervisor")["todos"]
        self.assertEqual([], sup)  # 匹配金额，无待复核

    def test_node_split_total_must_match_award_price(self):
        with self.assertRaises(DomainError) as ctx:
            self.make_contract(amounts=(300000, 499999))
        self.assertEqual(400, ctx.exception.status)

    def test_versions_are_stored_separately(self):
        detail = self.make_contract()
        cid = detail["contract"]["id"]
        n1 = detail["nodes"][0]["id"]
        self.service.report_completion("vendor-demo", "vendor", n1, 100)
        d = self.service.accept_completion("proc2", "procurement", n1, 300000)
        versions = self.service.list_versions("sup1", "supervisor", "node", n1)
        self.assertGreaterEqual(len(versions["versions"]), 2)
        contract_versions = self.service.list_versions("auditor", "auditor", "contract", cid)
        self.assertEqual(1, contract_versions["versions"][0]["version"])
        acceptance_id = d["acceptances"][0]["id"]
        av = self.service.list_versions("sup1", "supervisor", "acceptance", acceptance_id)
        self.assertEqual(1, len(av["versions"]))
        with self.assertRaises(DomainError) as ctx:
            self.service.list_versions("vendor-demo", "vendor", "node", n1)
        self.assertEqual(403, ctx.exception.status)


if __name__ == "__main__":
    unittest.main()
