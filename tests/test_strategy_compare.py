"""策略版本比较：顺序变化、依赖变化、受影响案件、只读结果、缺失版本错误。"""
import dataclasses
import unittest

from service_09252_006.domain.enums import Role
from service_09252_006.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from tests.support import Harness


class StrategyCompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.owner = self.h.user("rule-owner", Role.INSTITUTION_ADMIN)
        self.service = self.h.ctx.strategies
        created = self.service.create_strategy(self.owner, name="录取规则")
        self.strategy_id = created["strategy_id"]

    def tearDown(self) -> None:
        self.h.close()

    def _publish(self, rules) -> dict:
        return self.service.publish_version(
            self.owner, strategy_id=self.strategy_id, rules=rules
        )

    def _compare(self, from_version_id, to_version_id, actor=None):
        return self.service.compare_versions(
            actor or self.owner,
            strategy_id=self.strategy_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
        )

    # ---------------------------------------------------- 顺序与内容变化
    def test_order_content_and_dependency_changes(self) -> None:
        v1 = self._publish([
            {"rule_key": "r-lang", "content": {"min": 6.5}},
            {"rule_key": "r-gpa", "content": {"min": 3.0},
             "depends_on": ["r-lang"]},
            {"rule_key": "r-interview", "content": {"required": True}},
        ])
        v2 = self._publish([
            {"rule_key": "r-interview", "content": {"required": True}},
            {"rule_key": "r-lang", "content": {"min": 7.0}},
            {"rule_key": "r-gpa", "content": {"min": 3.0}},
            {"rule_key": "r-essay", "content": {"required": True}},
        ])

        report = self._compare(v1["version_id"], v2["version_id"])

        # 新增/移除
        self.assertEqual(report.added_rules, ("r-essay",))
        self.assertEqual(report.removed_rules, ())
        # 内容修改
        self.assertEqual(report.modified_rules, ("r-lang",))
        # 顺序变化：r-interview 2→0，r-lang 0→1，r-gpa 1→2
        moved = {m.rule_key: m for m in report.moved_rules}
        self.assertEqual(
            {k: (m.from_position, m.to_position) for k, m in moved.items()},
            {"r-interview": (2, 0), "r-lang": (0, 1), "r-gpa": (1, 2)},
        )
        # 依赖变化：r-gpa 去掉对 r-lang 的依赖
        deps = {d.rule_key: d for d in report.dependency_changes}
        self.assertEqual(deps["r-gpa"].removed, ("r-lang",))
        self.assertEqual(deps["r-gpa"].added, ())

    def test_removed_rules_reported(self) -> None:
        v1 = self._publish([
            {"rule_key": "r-a", "content": {}},
            {"rule_key": "r-b", "content": {}},
        ])
        v2 = self._publish([{"rule_key": "r-a", "content": {}}])
        report = self._compare(v1["version_id"], v2["version_id"])
        self.assertEqual(report.removed_rules, ("r-b",))
        self.assertEqual(report.moved_rules, ())
        self.assertEqual(report.changed_rule_keys, ("r-b",))

    # -------------------------------------------------------- 受影响案件
    def test_affected_cases_listed(self) -> None:
        v1 = self._publish([
            {"rule_key": "r-lang", "content": {"min": 6.5}},
            {"rule_key": "r-gpa", "content": {"min": 3.0}},
            {"rule_key": "r-interview", "content": {"required": True}},
        ])
        vid1 = v1["version_id"]
        self.service.register_case(
            self.owner, strategy_id=self.strategy_id, version_id=vid1,
            matched_rule_keys=["r-lang", "r-gpa"], case_id="case-1",
        )
        self.service.register_case(
            self.owner, strategy_id=self.strategy_id, version_id=vid1,
            matched_rule_keys=["r-interview"], case_id="case-2",
        )
        self.service.register_case(
            self.owner, strategy_id=self.strategy_id, version_id=vid1,
            matched_rule_keys=["r-gpa"], case_id="case-3",
        )
        v2 = self._publish([
            {"rule_key": "r-lang", "content": {"min": 7.0}},   # 修改
            {"rule_key": "r-gpa", "content": {"min": 3.0}},    # 未变
            # r-interview 被移除
        ])

        report = self._compare(vid1, v2["version_id"])

        affected = {a.case_id: a for a in report.affected_cases}
        # case-1 命中了被修改的 r-lang；case-2 命中了被移除的 r-interview
        self.assertEqual(affected["case-1"].changed_rule_keys, ("r-lang",))
        self.assertEqual(affected["case-2"].changed_rule_keys, ("r-interview",))
        # case-3 只命中未变化的 r-gpa，不受影响
        self.assertNotIn("case-3", affected)

    def test_identical_versions_have_no_changes(self) -> None:
        v1 = self._publish([
            {"rule_key": "r-a", "content": {"x": 1}},
            {"rule_key": "r-b", "content": {}, "depends_on": ["r-a"]},
        ])
        self.service.register_case(
            self.owner, strategy_id=self.strategy_id,
            version_id=v1["version_id"], matched_rule_keys=["r-a"],
            case_id="case-1",
        )
        report = self._compare(v1["version_id"], v1["version_id"])
        self.assertEqual(report.changed_rule_keys, ())
        self.assertEqual(report.affected_cases, ())

    # ---------------------------------------------------------- 只读性
    def test_comparison_result_is_read_only(self) -> None:
        v1 = self._publish([{"rule_key": "r-a", "content": {"x": 1}}])
        v2 = self._publish([{"rule_key": "r-a", "content": {"x": 2}}])

        report = self._compare(v1["version_id"], v2["version_id"])

        # frozen 结构：字段不可改
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.strategy_id = "other"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.moved_rules = ()  # type: ignore[misc]
        # 集合为 tuple，无 append/remove
        self.assertIsInstance(report.modified_rules, tuple)
        self.assertIsInstance(report.moved_rules, tuple)
        self.assertIsInstance(report.affected_cases, tuple)
        # 可序列化为普通 dict 供展示
        as_dict = report.to_dict()
        self.assertEqual(as_dict["from_version_id"], v1["version_id"])
        self.assertEqual(as_dict["modified_rules"], ["r-a"])

    def test_comparison_does_not_write_anything(self) -> None:
        v1 = self._publish([{"rule_key": "r-a", "content": {"x": 1}}])
        v2 = self._publish([{"rule_key": "r-a", "content": {"x": 2}}])
        audits_before = sorted(
            a.audit_id for a in self.h.repo.list_audit(limit=1000)
        )

        first = self._compare(v1["version_id"], v2["version_id"])
        second = self._compare(v1["version_id"], v2["version_id"])

        # 纯函数：重复比较结果一致
        self.assertEqual(first, second)
        # 只读：不产生任何审计/状态变化
        audits_after = sorted(
            a.audit_id for a in self.h.repo.list_audit(limit=1000)
        )
        self.assertEqual(audits_before, audits_after)

    # -------------------------------------------- SQLite 保留顺序与依赖
    def test_version_roundtrip_preserves_order_and_dependencies(self) -> None:
        published = self._publish([
            {"rule_key": "r-a", "content": {"x": 1}},
            {"rule_key": "r-b", "content": {"y": [1, 2]},
             "depends_on": ["r-a"]},
            {"rule_key": "r-c", "content": {}, "depends_on": ["r-a", "r-b"]},
        ])

        loaded = self.h.repo.get_strategy_version(published["version_id"])

        self.assertIsNotNone(loaded)
        self.assertEqual(
            [r.rule_key for r in loaded.rules], ["r-a", "r-b", "r-c"]
        )
        self.assertEqual([r.position for r in loaded.rules], [0, 1, 2])
        deps = {r.rule_key: r.depends_on for r in loaded.rules}
        self.assertEqual(deps["r-b"], ("r-a",))
        self.assertEqual(deps["r-c"], ("r-a", "r-b"))
        self.assertEqual(loaded.rules[1].content, {"y": [1, 2]})
        # 版本链：v2 指向 v1
        v2 = self._publish([{"rule_key": "r-a", "content": {"x": 2}}])
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["supersedes_version_id"], published["version_id"])

    # ---------------------------------------------------- 缺失版本错误
    def test_missing_from_version_raises_clear_error(self) -> None:
        v1 = self._publish([{"rule_key": "r-a", "content": {}}])
        with self.assertRaises(NotFoundError) as ctx:
            self._compare("sver_missing", v1["version_id"])
        exc = ctx.exception
        self.assertIn("策略版本不存在", exc.message)
        self.assertEqual(exc.details["version_id"], "sver_missing")
        self.assertEqual(exc.details["side"], "from")
        self.assertEqual(exc.details["strategy_id"], self.strategy_id)

    def test_missing_to_version_raises_clear_error(self) -> None:
        v1 = self._publish([{"rule_key": "r-a", "content": {}}])
        with self.assertRaises(NotFoundError) as ctx:
            self._compare(v1["version_id"], "sver_missing")
        exc = ctx.exception
        self.assertIn("策略版本不存在", exc.message)
        self.assertEqual(exc.details["version_id"], "sver_missing")
        self.assertEqual(exc.details["side"], "to")

    def test_version_of_other_strategy_is_not_found(self) -> None:
        other = self.service.create_strategy(self.owner, name="另一套规则")
        foreign = self.service.publish_version(
            self.owner, strategy_id=other["strategy_id"],
            rules=[{"rule_key": "r-x", "content": {}}],
        )
        v1 = self._publish([{"rule_key": "r-a", "content": {}}])
        # 其他策略的版本拿来比较：按“不存在”处理，不泄露跨策略信息
        with self.assertRaises(NotFoundError) as ctx:
            self._compare(v1["version_id"], foreign["version_id"])
        self.assertEqual(ctx.exception.details["side"], "to")

    def test_missing_strategy_raises_clear_error(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.service.compare_versions(
                self.owner,
                strategy_id="str_missing",
                from_version_id="a",
                to_version_id="b",
            )
        self.assertIn("策略不存在", ctx.exception.message)
        self.assertEqual(ctx.exception.details["strategy_id"], "str_missing")

    # ------------------------------------------------------------ 校验
    def test_invalid_rule_sets_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._publish([])  # 空规则集
        with self.assertRaises(ValidationError):
            self._publish([
                {"rule_key": "r-a", "content": {}},
                {"rule_key": "r-a", "content": {}},
            ])  # 键重复
        with self.assertRaises(ValidationError):
            self._publish([
                {"rule_key": "r-a", "content": {}, "depends_on": ["r-ghost"]},
            ])  # 依赖未知规则
        with self.assertRaises(ValidationError):
            self._publish([
                {"rule_key": "r-a", "content": {}, "depends_on": ["r-b"]},
                {"rule_key": "r-b", "content": {}, "depends_on": ["r-a"]},
            ])  # 依赖循环

    def test_case_with_unknown_rule_rejected(self) -> None:
        v1 = self._publish([{"rule_key": "r-a", "content": {}}])
        with self.assertRaises(ValidationError):
            self.service.register_case(
                self.owner, strategy_id=self.strategy_id,
                version_id=v1["version_id"], matched_rule_keys=["r-ghost"],
            )

    # ------------------------------------------------------------ 权限
    def test_cross_institution_compare_denied(self) -> None:
        outsider = self.h.user("outsider", Role.INSTITUTION_ADMIN,
                               institution_id="inst-b")
        v1 = self._publish([{"rule_key": "r-a", "content": {}}])
        with self.assertRaises(PermissionDeniedError):
            self._compare(v1["version_id"], v1["version_id"], actor=outsider)

    def test_auditor_can_compare_across_institutions(self) -> None:
        auditor = self.h.user("auditor", Role.AUDITOR, institution_id=None)
        v1 = self._publish([{"rule_key": "r-a", "content": {"x": 1}}])
        v2 = self._publish([{"rule_key": "r-a", "content": {"x": 2}}])
        report = self._compare(v1["version_id"], v2["version_id"], actor=auditor)
        self.assertEqual(report.modified_rules, ("r-a",))


if __name__ == "__main__":
    unittest.main()
