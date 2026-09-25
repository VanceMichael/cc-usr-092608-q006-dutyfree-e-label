"""离岛免税电子标签监管服务的全流程测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.event_store import EventStore
from src.regulatory_service import (
    DOC_COMPOSITION,
    DOC_CUSTOMS,
    DOC_INSTRUCTIONS,
    DOC_ORIGIN,
    Actor,
    RegulatoryService,
    Violation,
)

ENTERPRISE = Actor("免税经营企业", "E-01")
REVIEWER_A = Actor("资料审核", "R-01")
REVIEWER_B = Actor("资料审核", "R-02")
ISSUER_A = Actor("标签签发", "I-01")
WAREHOUSE = Actor("仓库人员", "W-01")
CUSTOMS = Actor("海关监管人员", "C-01")
DRUG = Actor("药品监管人员", "D-01")


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.svc = RegulatoryService(EventStore(self.dir / "events.jsonl"))

    def tearDown(self) -> None:
        self.svc.store.close()
        self._tmp.cleanup()

    def reopen(self) -> RegulatoryService:
        """模拟应用重启：重新打开事件日志并回放。"""
        self.svc.store.close()
        store = EventStore(self.dir / "events.jsonl")
        self.svc = RegulatoryService(store)
        return self.svc

    # -- 场景搭建 --------------------------------------------------------------

    def seed_product(
        self,
        model: str = "口红-301",
        *,
        comp: str = "comp-1",
        origin: str = "origin-1",
        instr: str = "instr-1",
        customs: str = "customs-1",
        confidential: str | None = "配方工艺附件://internal/formula-301.pdf",
        version: str = "v1",
    ) -> None:
        svc = self.svc
        svc.register_product(model, "丝绒口红 3.5g", "示例品牌", actor=ENTERPRISE)
        svc.submit_document(
            comp, model, DOC_COMPOSITION, "成分声明",
            {"ingredients": ["氢化聚异丁烯", "聚乙烯", "云母"]},
            confidential_attachment=confidential, actor=ENTERPRISE,
        )
        svc.submit_document(
            origin, model, DOC_ORIGIN, "原产地证明",
            {"origin": "法国", "certificate_no": "CO-FR-0001"}, actor=ENTERPRISE,
        )
        svc.submit_document(
            instr, model, DOC_INSTRUCTIONS, "中文说明书",
            {"chinese_name": "丝绒口红 3.5g", "instructions": "避开眼周；如有不适停用。"},
            actor=ENTERPRISE,
        )
        svc.submit_document(
            customs, model, DOC_CUSTOMS, "进境货物报关单",
            {"declaration_no": "HK-2026-0001", "summary": "一般贸易进境，转离岛免税监管仓"},
            actor=ENTERPRISE,
        )
        for doc in (comp, origin, instr, customs):
            svc.confirm_document(doc, actor=REVIEWER_A)
        svc.create_draft(
            f"draft-{version}", model,
            {DOC_COMPOSITION: comp, DOC_ORIGIN: origin, DOC_INSTRUCTIONS: instr, DOC_CUSTOMS: customs},
            actor=ENTERPRISE,
        )
        svc.review_draft(f"draft-{version}", "通过", actor=REVIEWER_A)
        svc.issue_label(f"draft-{version}", version_id=version, actor=ISSUER_A)

    def receive_batch(self, batch_id: str, codes: list[str], *, model: str = "口红-301",
                      customs: str = "customs-1", location: str = "A仓-01") -> None:
        for code in codes:
            self.svc.register_carrier(code, model, actor=ENTERPRISE)
        self.svc.receive_import(batch_id, model, customs, location, codes, actor=WAREHOUSE)

    @staticmethod
    def codes(prefix: str, n: int) -> list[str]:
        return [f"{prefix}-{i:03d}" for i in range(1, n + 1)]


class ApprovalTest(ServiceCase):
    def test_review_and_issuance_are_separate_duties(self) -> None:
        svc = self.svc
        svc.register_product("口红-301", "丝绒口红 3.5g", "示例品牌", actor=ENTERPRISE)
        svc.submit_document(
            "comp-1", "口红-301", DOC_COMPOSITION, "成分声明",
            {"ingredients": ["云母"]}, confidential_attachment="internal://f.pdf", actor=ENTERPRISE,
        )
        # 企业与签发人都不能确认资料，确认只能由资料审核岗完成。
        with self.assertRaisesRegex(Violation, "仅允许资料审核"):
            svc.confirm_document("comp-1", actor=ENTERPRISE)
        with self.assertRaisesRegex(Violation, "仅允许资料审核"):
            svc.confirm_document("comp-1", actor=ISSUER_A)
        svc.confirm_document("comp-1", actor=REVIEWER_A)
        self.assertEqual(svc.p.documents["comp-1"]["status"], "已确认")

    def test_issuer_can_only_reference_confirmed_documents(self) -> None:
        svc = self.svc
        svc.register_product("口红-301", "丝绒口红 3.5g", "示例品牌", actor=ENTERPRISE)
        svc.submit_document("comp-1", "口红-301", DOC_COMPOSITION, "成分",
                            {"ingredients": ["云母"]}, actor=ENTERPRISE)
        svc.submit_document("origin-1", "口红-301", DOC_ORIGIN, "原产地",
                            {"origin": "法国"}, actor=ENTERPRISE)
        svc.submit_document("instr-1", "口红-301", DOC_INSTRUCTIONS, "中文",
                            {"chinese_name": "口红", "instructions": ""}, actor=ENTERPRISE)
        svc.submit_document("customs-1", "口红-301", DOC_CUSTOMS, "报关单",
                            {"declaration_no": "HK-1"}, actor=ENTERPRISE)
        # 只确认三份，成分声明未确认。
        for doc in ("origin-1", "instr-1", "customs-1"):
            svc.confirm_document(doc, actor=REVIEWER_A)
        svc.create_draft("draft-1", "口红-301",
                         {DOC_COMPOSITION: "comp-1", DOC_ORIGIN: "origin-1",
                          DOC_INSTRUCTIONS: "instr-1", DOC_CUSTOMS: "customs-1"}, actor=ENTERPRISE)
        with self.assertRaisesRegex(Violation, "未经确认"):
            svc.review_draft("draft-1", "通过", actor=REVIEWER_A)
        # 驳回允许，审核员不能越权直接签发。
        svc.review_draft("draft-1", "驳回", actor=REVIEWER_A)
        with self.assertRaisesRegex(Violation, "未经资料审核通过"):
            svc.issue_label("draft-1", version_id="v1", actor=ISSUER_A)

    def test_same_officer_cannot_review_and_issue(self) -> None:
        self.seed_product()
        svc = self.svc
        svc.create_draft("draft-x", "口红-301",
                         {DOC_COMPOSITION: "comp-1", DOC_ORIGIN: "origin-1",
                          DOC_INSTRUCTIONS: "instr-1", DOC_CUSTOMS: "customs-1"}, actor=ENTERPRISE)
        svc.review_draft("draft-x", "通过", actor=REVIEWER_B)
        # 以审核人编号担任签发人即同人两职，必须拒绝。
        same = Actor("标签签发", "R-02")
        with self.assertRaisesRegex(Violation, "不同人员"):
            svc.issue_label("draft-x", version_id="vX", actor=same)
        # 换一名签发人即可签发。
        svc.issue_label("draft-x", version_id="vX", actor=ISSUER_A)


class ConsumerViewTest(ServiceCase):
    def test_scan_shows_only_public_whitelist(self) -> None:
        self.seed_product()
        self.receive_batch("B-1", self.codes("QR", 2))
        view = self.svc.consumer_scan("QR-001")
        self.assertEqual(view["商品名称"], "丝绒口红 3.5g")
        self.assertEqual(view["成分"], ["氢化聚异丁烯", "聚乙烯", "云母"])
        self.assertEqual(view["原产地"], "法国")
        self.assertIn("HK-2026-0001", view["通关信息"]["declaration_no"])
        # 企业配方附件等内部信息绝不公开。
        rendered = repr(view)
        self.assertNotIn("formula-301", rendered)
        self.assertNotIn("confidential", rendered)
        self.assertNotIn("doc_id", rendered)

    def test_unknown_and_retired_codes(self) -> None:
        self.seed_product()
        self.receive_batch("B-1", ["QR-1"])
        unknown = self.svc.consumer_scan("NOPE")
        self.assertEqual(unknown["status"], "无效码")
        serial = next(iter(self.svc.p.units))
        self.svc.relabel_unit(serial, "QR-NEW", actor=WAREHOUSE)
        old = self.svc.consumer_scan("QR-1")
        self.assertEqual(old["status"], "标签载体已停用")
        new = self.svc.consumer_scan("QR-NEW")
        self.assertEqual(new["标签版本"], "v1")


class CarrierReplacementTest(ServiceCase):
    def test_replacement_keeps_serial_history_and_blocks_old_carrier(self) -> None:
        self.seed_product()
        self.receive_batch("B-1", ["QR-1", "QR-2"])
        serial = next(iter(self.svc.p.units))
        self.svc.relabel_unit(serial, "QR-1R", actor=WAREHOUSE)
        unit = self.svc.p.units[serial]
        self.assertEqual(unit.code, "QR-1R")
        self.assertEqual(unit.status, "在区")
        actions = [h["action"] for h in unit.history]
        self.assertIn("入区", actions)
        self.assertIn("破损补发", actions)
        self.assertEqual(self.svc.p.carriers["QR-1"]["status"], "已停用")
        self.assertEqual(self.svc.p.carriers["QR-1"]["replaced_by"], "QR-1R")
        # 旧码不得再次用于入区。
        with self.assertRaisesRegex(Violation, "已失效"):
            self.svc.receive_import("B-X", "口红-301", "customs-1", "A仓-01", ["QR-1"], actor=WAREHOUSE)


class StockConservationTest(ServiceCase):
    def test_repack_transfer_partial_release_return_destroy(self) -> None:
        svc = self.svc
        self.seed_product()
        codes = self.codes("QR", 6)
        self.receive_batch("B-1", codes)
        self.assertEqual(svc.stock("B-1"), {"在区": 6})

        # 分装：2 件进入子批次，母批剩 4，件数守恒。
        svc.repack("B-1", [("B-1A", codes[:2])], location="A仓-02", actor=WAREHOUSE)
        self.assertEqual(svc.stock("B-1"), {"在区": 4})
        self.assertEqual(svc.stock("B-1A"), {"在区": 2})

        # 换仓移动母批两件。
        serials = [u.serial for u in svc._batch_units("B-1")][:2]
        svc.transfer(serials, "B仓-09", actor=WAREHOUSE)
        self.assertTrue(all(svc.p.units[s].location == "B仓-09" for s in serials))

        # 部分放行：子批两件核放出区。
        svc.declare_release("REL-1", codes[:2], "customs-1", "RESULT-1", actor=CUSTOMS)
        svc.customs_clear("REL-1", actor=CUSTOMS)
        self.assertEqual(svc.stock("B-1A"), {"已出区": 2})
        self.assertEqual(svc.stock("B-1"), {"在区": 4})

        # 退运一件、销毁一件，均只改变对应件。
        remain = [u.serial for u in svc._batch_units("B-1")]
        svc.return_units([remain[0]], "客户订单取消", actor=WAREHOUSE)
        svc.destroy_units([remain[1]], "外观破损", actor=WAREHOUSE)
        self.assertEqual(svc.stock("B-1"), {"在区": 2, "退运": 1, "销毁": 1})

        # 总数始终为 6。
        total = sum(sum(svc.stock(b).values()) for b in ("B-1", "B-1A"))
        self.assertEqual(total, 6)


class FreezeIsolationTest(ServiceCase):
    def test_composition_correction_freezes_only_related_batch(self) -> None:
        svc = self.svc
        self.seed_product()
        self.receive_batch("B-OLD", self.codes("OLD", 3))
        # 另一商品完全不受牵连。
        self.seed_product(model="香水-202", comp="p-comp", origin="p-origin",
                          instr="p-instr", customs="p-customs", version="p1")
        for c in self.codes("PQ", 2):
            svc.register_carrier(c, "香水-202", actor=ENTERPRISE)
        svc.receive_import("P-1", "香水-202", "p-customs", "C仓-01",
                           self.codes("PQ", 2), actor=WAREHOUSE)

        # 成分更正：新成分声明替代旧声明并签发 v2。
        svc.submit_document("comp-2", "口红-301", DOC_COMPOSITION, "成分声明(修订)",
                            {"ingredients": ["氢化聚异丁烯", "聚乙烯", "云母", "生育酚乙酸酯"]},
                            replaces="comp-1", actor=ENTERPRISE)
        svc.confirm_document("comp-2", actor=REVIEWER_B)
        svc.create_draft("draft-2", "口红-301",
                         {DOC_COMPOSITION: "comp-2", DOC_ORIGIN: "origin-1",
                          DOC_INSTRUCTIONS: "instr-1", DOC_CUSTOMS: "customs-1"}, actor=ENTERPRISE)
        svc.review_draft("draft-2", "通过", actor=REVIEWER_B)
        svc.issue_label("draft-2", version_id="v2", actor=ISSUER_A)

        # 挂旧版本的在区批次被冻结，新品批次不动。
        self.assertTrue(svc.p.batches["B-OLD"]["frozen"])
        self.assertFalse(svc.p.batches["P-1"]["frozen"])
        frozen_serial = next(iter(svc._batch_units("B-OLD"))).serial
        with self.assertRaisesRegex(Violation, "冻结"):
            svc.transfer([frozen_serial], "B仓-01", actor=WAREHOUSE)
        # 香水正常换仓。
        p_serial = next(iter(svc._batch_units("P-1"))).serial
        svc.transfer([p_serial], "C仓-02", actor=WAREHOUSE)

        # 抽检异常冻结同理：只冻指定批次。
        svc.freeze_batch("P-1", "抽检标签信息异常", actor=DRUG)
        self.assertTrue(svc.p.batches["P-1"]["frozen"])
        # 已经出区的商品不在冻结范围（召回另行处理）。


class CustomsClearanceTest(ServiceCase):
    def test_lock_version_and_idempotency(self) -> None:
        svc = self.svc
        self.seed_product()
        codes = self.codes("QR", 3)
        self.receive_batch("B-1", codes)
        rel = svc.declare_release("REL-1", codes[:2], "customs-1", "RESULT-1", actor=CUSTOMS)
        serials = sorted(rel["serials"])
        self.assertTrue(all(rel["locked_versions"][s] == "v1" for s in serials))

        svc.customs_clear("REL-1", actor=CUSTOMS)
        # 重复提交同一核验结果：幂等返回，不再次扣减（第三件仍在区）。
        again = svc.declare_release("REL-1", codes[:2], "customs-1", "RESULT-1", actor=CUSTOMS)
        self.assertIs(again, rel)
        self.assertEqual(svc.stock("B-1")["已出区"], 2)
        self.assertEqual(svc.stock("B-1")["在区"], 1)
        # 重复核放也不报错、不重复扣。
        svc.customs_clear("REL-1", actor=CUSTOMS)
        self.assertEqual(svc.stock("B-1")["已出区"], 2)

    def test_conflicting_documents_go_to_manual_handling(self) -> None:
        svc = self.svc
        self.seed_product()
        codes = self.codes("QR", 2)
        self.receive_batch("B-1", codes)
        svc.declare_release("REL-1", codes, "customs-1", "RESULT-1", actor=CUSTOMS)
        # 用另一份单证申报核放 → 矛盾，停人工处理，库存不动。
        svc.submit_document("customs-2", "口红-301", DOC_CUSTOMS, "报关单(更正)",
                            {"declaration_no": "HK-2026-0002", "summary": "单证号不一致"},
                            replaces="customs-1", actor=ENTERPRISE)
        svc.confirm_document("customs-2", actor=REVIEWER_A)
        # 直接构造一个引用矛盾单证的核放单。
        svc.declare_release("REL-2", codes, "customs-2", "RESULT-2", actor=CUSTOMS)
        result = svc.customs_clear("REL-2", actor=CUSTOMS)
        self.assertEqual(result["status"], "人工处理")
        self.assertIn("通关单证矛盾", result["block_reason"])
        self.assertEqual(svc.stock("B-1").get("已出区", 0), 0)

    def test_version_change_after_lock_blocks(self) -> None:
        svc = self.svc
        self.seed_product()
        self.receive_batch("B-1", ["QR-9"])
        svc.declare_release("REL-1", ["QR-9"], "customs-1", "RESULT-1", actor=CUSTOMS)
        # 签发 v2 后对在区实物补发新码（模拟核验时码已指向新版本），锁定版本不再匹配。
        svc.submit_document("comp-2", "口红-301", DOC_COMPOSITION, "成分(修订)",
                            {"ingredients": ["云母", "生育酚"]}, replaces="comp-1", actor=ENTERPRISE)
        svc.confirm_document("comp-2", actor=REVIEWER_B)
        svc.create_draft("draft-2", "口红-301",
                         {DOC_COMPOSITION: "comp-2", DOC_ORIGIN: "origin-1",
                          DOC_INSTRUCTIONS: "instr-1", DOC_CUSTOMS: "customs-1"}, actor=ENTERPRISE)
        svc.review_draft("draft-2", "通过", actor=REVIEWER_B)
        svc.issue_label("draft-2", version_id="v2", actor=ISSUER_A)
        # 成分更正已冻结 B-1；先解冻才能演示版本不匹配拦截。
        svc.unfreeze_batch("B-1", actor=DRUG)
        serial = next(iter(svc.p.units))
        svc.relabel_unit(serial, "QR-9R", actor=WAREHOUSE)
        result = svc.customs_clear("REL-1", actor=CUSTOMS)
        self.assertEqual(result["status"], "人工处理")
        self.assertIn("标签版本已变化", result["block_reason"])


class RecallTest(ServiceCase):
    def test_recall_locates_sales_status_and_notification_progress(self) -> None:
        svc = self.svc
        self.seed_product()
        codes = self.codes("QR", 4)
        self.receive_batch("B-1", codes)
        svc.declare_release("REL-1", codes, "customs-1", "RESULT-1", actor=CUSTOMS)
        svc.customs_clear("REL-1", actor=CUSTOMS)
        # 两件已售、两件待售。
        sold = [u.serial for u in svc._batch_units("B-1")][:2]
        for s in sold:
            svc.sell_unit(s, actor=ENTERPRISE)

        svc.start_recall("RC-1", {"batch_id": "B-1"}, "某批次成分标注更正召回", actor=DRUG)
        progress = svc.recall_progress("RC-1")
        self.assertEqual(progress["总数"], 4)
        self.assertEqual(progress["待通知"], 4)
        statuses = {row["serial"]: row["销售状态"] for row in progress["明细"]}
        self.assertEqual({statuses[s] for s in sold}, {"已售"})

        svc.notify_consumer("RC-1", sold[0], actor=DRUG)
        progress = svc.recall_progress("RC-1")
        self.assertEqual((progress["已通知"], progress["待通知"]), (1, 3))
        with self.assertRaisesRegex(Violation, "不能重复通知"):
            svc.notify_consumer("RC-1", sold[0], actor=DRUG)
        # 消费者扫码可见召回提示。
        code = svc.p.units[sold[0]].code
        self.assertEqual(svc.consumer_scan(code)["召回提示"], "某批次成分标注更正召回")


class RecoveryAndQueryTest(ServiceCase):
    def test_restart_recovers_pending_release_and_recall_notifications(self) -> None:
        svc = self.svc
        self.seed_product()
        codes = self.codes("QR", 3)
        self.receive_batch("B-1", codes)
        # 一件已申报未核放；另两件已出区并进入召回，其中一件尚未通知。
        svc.declare_release("REL-OPEN", codes[:1], "customs-1", "RESULT-1", actor=CUSTOMS)
        svc.declare_release("REL-2", codes[1:], "customs-1", "RESULT-2", actor=CUSTOMS)
        svc.customs_clear("REL-2", actor=CUSTOMS)
        svc.start_recall("RC-1", {"batch_id": "B-1"}, "召回测试", actor=DRUG)
        out_serials = list(svc.p.releases["REL-2"]["serials"])
        svc.notify_consumer("RC-1", out_serials[0], actor=DRUG)

        svc = self.reopen()
        recovery = svc.pending_recovery()
        self.assertIn("REL-OPEN", recovery["未完成出区核放"])
        self.assertNotIn("REL-2", recovery["未完成出区核放"])
        self.assertEqual(
            sorted(recovery["待召回通知"]),
            [f"RC-1:{out_serials[1]}"],
        )
        # 恢复后继续核放与通知，状态衔接无误。
        svc.customs_clear("REL-OPEN", actor=CUSTOMS)
        svc.notify_consumer("RC-1", out_serials[1], actor=DRUG)
        self.assertEqual(svc.pending_recovery(), {"未完成出区核放": [], "待召回通知": []})

    def test_serial_query_shows_approval_code_chain_stock_and_views(self) -> None:
        svc = self.svc
        self.seed_product()
        self.receive_batch("B-1", ["QR-1"])
        serial = next(iter(svc.p.units))
        svc.transfer([serial], "A仓-05", actor=WAREHOUSE)
        svc.relabel_unit(serial, "QR-1R", actor=WAREHOUSE)
        svc.declare_release("REL-1", ["QR-1R"], "customs-1", "RESULT-1", actor=CUSTOMS)
        svc.customs_clear("REL-1", actor=CUSTOMS)

        report = svc.serial_query(serial)
        self.assertEqual(report["实物状态"], "已出区")
        self.assertEqual(report["当前仓位"], "A仓-05")
        # 审批依据：v1 的审核/签发人与四份资料。
        approval = report["审批依据"][0]
        self.assertEqual(approval["审核人"], str(REVIEWER_A))
        self.assertEqual(approval["签发人"], str(ISSUER_A))
        doc_types = {row["类型"] for row in approval["依据资料"]}
        self.assertEqual(doc_types, {DOC_COMPOSITION, DOC_ORIGIN, DOC_INSTRUCTIONS, DOC_CUSTOMS})
        # 码值替换：旧码停用、新码当前。
        chain = report["码值替换"]
        self.assertEqual(chain[0]["code"], "QR-1")
        self.assertIn("QR-1R", chain[0]["去向"])
        self.assertEqual(chain[-1]["code"], "QR-1R")
        # 库存去向按时间排列。
        actions = [h["action"] for h in report["库存去向"]]
        self.assertEqual(actions, ["入区", "换仓", "破损补发", "海关核放出区"])
        # 每个时点消费者可见信息均为白名单内容且可回溯。
        for point in report["各时点消费者可见信息"]:
            self.assertIn("成分", point["消费者可见"])
            self.assertNotIn("confidential", repr(point))
        # 入区时点看到的是旧码承载的 v1 内容。
        self.assertEqual(report["各时点消费者可见信息"][0]["消费者可见"]["标签版本"], "v1")


if __name__ == "__main__":
    unittest.main()
