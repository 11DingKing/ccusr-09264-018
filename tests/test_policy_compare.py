"""策略版本比较：顺序变化、受影响案件、只读性与缺失版本错误。"""
import unittest

from service_09252_006.domain.enums import Role
from service_09252_006.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from tests.support import Harness


def rule(rule_id, content=None, depends_on=()):
    return {
        "rule_id": rule_id,
        "content": content if content is not None else f"规则{rule_id}",
        "depends_on": list(depends_on),
    }


class PolicyCompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.auditor = self.h.user(
            "aud", Role.AUDITOR, institution_id=None
        )
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)

    def tearDown(self) -> None:
        self.h.close()

    # ------------------------------------------------------------ 辅助
    def make_version(self, rules, policy_id=None):
        return self.h.ctx.policies.create_policy_version(
            self.authority, rules=rules, policy_id=policy_id
        )

    def make_policy(self, versions):
        """versions: [ [rule, ...], ... ]，返回 (policy_id, [version_dict])。"""
        made = []
        policy_id = None
        for rules in versions:
            v = self.make_version(rules, policy_id=policy_id)
            policy_id = v["policy_id"]
            made.append(v)
        return policy_id, made

    # ---------------------------------------------------- 缺失版本错误
    def test_missing_version_returns_clear_error(self) -> None:
        policy_id, (v1,) = self.make_policy([[rule("a")]])

        with self.assertRaises(NotFoundError) as ctx:
            self.h.ctx.policies.compare_versions(
                self.authority,
                from_version_id=v1["version_id"],
                to_version_id="pver_missing",
            )
        err = ctx.exception
        self.assertEqual(err.code, "not_found")
        self.assertEqual(err.http_status, 404)
        self.assertIn("策略版本不存在", err.message)
        self.assertEqual(err.details, {"version_id": "pver_missing"})

        # 基准版本缺失同样明确报错
        with self.assertRaises(NotFoundError) as ctx:
            self.h.ctx.policies.compare_versions(
                self.authority,
                from_version_id="pver_nothing",
                to_version_id=v1["version_id"],
            )
        self.assertIn("比较基准", ctx.exception.message)

        # 用 policy_id + version_no 定位时，缺失也给明确错误
        with self.assertRaises(NotFoundError) as ctx:
            self.h.ctx.policies.compare_versions(
                self.authority,
                policy_id=policy_id,
                from_version_no=1,
                to_version_no=99,
            )
        self.assertEqual(
            ctx.exception.details,
            {"policy_id": policy_id, "version_no": 99},
        )

        # 未提供任何版本标识：参数错误而非静默
        with self.assertRaises(ValidationError):
            self.h.ctx.policies.compare_versions(self.authority)

    # -------------------------------------------------------- 顺序变化
    def test_order_changes_are_reported(self) -> None:
        policy_id, (v1, v2) = self.make_policy([
            [rule("a"), rule("b"), rule("c")],
            [rule("c"), rule("a"), rule("d")],
        ])
        result = self.h.ctx.policies.compare_versions(
            self.authority,
            from_version_id=v1["version_id"],
            to_version_id=v2["version_id"],
        )
        self.assertEqual(result["policy_id"], policy_id)
        self.assertEqual(result["from_version"]["version_no"], 1)
        self.assertEqual(result["to_version"]["version_no"], 2)

        changes = {c["rule_id"]: c for c in result["rule_changes"]}
        self.assertEqual(set(changes), {"a", "b", "c", "d"})
        self.assertEqual(
            (changes["a"]["change"], changes["a"]["from_position"],
             changes["a"]["to_position"]),
            ("moved", 1, 2),
        )
        self.assertEqual(
            (changes["c"]["change"], changes["c"]["from_position"],
             changes["c"]["to_position"]),
            ("moved", 3, 1),
        )
        self.assertEqual(
            (changes["b"]["change"], changes["b"]["from_position"],
             changes["b"]["to_position"]),
            ("removed", 2, None),
        )
        self.assertEqual(
            (changes["d"]["change"], changes["d"]["from_position"],
             changes["d"]["to_position"]),
            ("added", None, 3),
        )
        self.assertEqual(
            result["summary"],
            {"added": 1, "removed": 1, "moved": 2, "modified": 0,
             "affected_cases": 0},
        )

    def test_content_and_dependency_changes_are_reported(self) -> None:
        policy_id, (v1, v2) = self.make_policy([
            [rule("a"), rule("b", depends_on=["a"])],
            [rule("a", content="规则a-修订"), rule("b")],
        ])
        result = self.h.ctx.policies.compare_versions(
            self.authority,
            policy_id=policy_id,
            from_version_no=1,
            to_version_no=2,
        )
        changes = {c["rule_id"]: c for c in result["rule_changes"]}
        self.assertEqual(changes["a"]["change"], "modified")
        self.assertTrue(changes["a"]["content_changed"])
        self.assertFalse(changes["a"]["depends_changed"])
        self.assertEqual(changes["b"]["change"], "modified")
        self.assertTrue(changes["b"]["depends_changed"])

    def test_identical_versions_have_no_changes(self) -> None:
        _, (v1,) = self.make_policy([[rule("a"), rule("b")]])
        result = self.h.ctx.policies.compare_versions(
            self.authority,
            from_version_id=v1["version_id"],
            to_version_id=v1["version_id"],
        )
        self.assertEqual(result["rule_changes"], [])
        self.assertEqual(result["affected_cases"], [])

    # -------------------------------------------------------- 受影响案件
    def test_affected_cases_include_dependency_propagation(self) -> None:
        policy_id, (v1, v2) = self.make_policy([
            [rule("a"), rule("b", depends_on=["a"]), rule("c")],
            [rule("a", content="规则a-修订"), rule("b", depends_on=["a"]),
             rule("c")],
        ])
        cases = {}
        for rule_id in ("a", "b", "c"):
            cases[rule_id] = self.h.ctx.policies.register_case(
                self.admin, policy_id=policy_id, rule_id=rule_id
            )["case_id"]

        result = self.h.ctx.policies.compare_versions(
            self.authority,
            from_version_id=v1["version_id"],
            to_version_id=v2["version_id"],
        )
        affected = {a["case_id"]: a for a in result["affected_cases"]}
        # 直接绑定变化规则的案件
        self.assertEqual(affected[cases["a"]]["reason"], "rule_changed")
        # 依赖链下游案件：b 依赖被修改的 a
        self.assertEqual(affected[cases["b"]]["reason"], "dependency_changed")
        self.assertEqual(affected[cases["b"]]["via_rule_ids"], ["a"])
        # 与变化无关的规则案件不受影响
        self.assertNotIn(cases["c"], affected)
        self.assertEqual(result["summary"]["affected_cases"], 2)

    def test_removed_rule_cases_are_affected(self) -> None:
        # 案件在 v1 生效期间绑定规则 b；v2 移除 b 后该案件受影响
        v1 = self.make_version([rule("a"), rule("b")])
        policy_id = v1["policy_id"]
        case_id = self.h.ctx.policies.register_case(
            self.admin, policy_id=policy_id, rule_id="b"
        )["case_id"]
        v2 = self.make_version([rule("a")], policy_id=policy_id)
        result = self.h.ctx.policies.compare_versions(
            self.auditor,
            from_version_id=v1["version_id"],
            to_version_id=v2["version_id"],
        )
        affected = {a["case_id"]: a for a in result["affected_cases"]}
        self.assertEqual(affected[case_id]["reason"], "rule_changed")
        self.assertEqual(affected[case_id]["rule_id"], "b")

    # ------------------------------------------------------------ 只读
    def test_compare_is_readonly(self) -> None:
        policy_id, (v1, v2) = self.make_policy([
            [rule("a"), rule("b")],
            [rule("b"), rule("a")],
        ])
        self.h.ctx.policies.register_case(
            self.admin, policy_id=policy_id, rule_id="a"
        )
        audits_before = self.h.repo.list_audit(limit=1000)
        versions_before = self.h.repo.list_policy_versions(policy_id)
        cases_before = self.h.repo.list_cases_by_policy(policy_id)

        first = self.h.ctx.policies.compare_versions(
            self.authority,
            from_version_id=v1["version_id"],
            to_version_id=v2["version_id"],
        )
        second = self.h.ctx.policies.compare_versions(
            self.auditor,
            from_version_id=v1["version_id"],
            to_version_id=v2["version_id"],
        )

        # 重复比较结果一致，且不留下任何写入痕迹（含审计）
        self.assertEqual(first, second)
        self.assertEqual(self.h.repo.list_audit(limit=1000), audits_before)
        self.assertEqual(
            self.h.repo.list_policy_versions(policy_id), versions_before
        )
        self.assertEqual(
            self.h.repo.list_cases_by_policy(policy_id), cases_before
        )

    # ---------------------------------------------------- 存储与校验
    def test_storage_preserves_order_and_dependencies(self) -> None:
        v = self.make_version([
            rule("a"),
            rule("b", depends_on=["a"]),
            rule("c", depends_on=["a", "b"]),
        ])
        stored = self.h.repo.get_policy_version(v["version_id"])
        self.assertEqual(
            [(r.rule_id, r.position) for r in stored.rules],
            [("a", 1), ("b", 2), ("c", 3)],
        )
        self.assertEqual(stored.rules[1].depends_on, ("a",))
        self.assertEqual(stored.rules[2].depends_on, ("a", "b"))

        # 同一策略再登记，版本号递增且各自顺序独立保存
        v2 = self.make_version([rule("c"), rule("a")], policy_id=v["policy_id"])
        self.assertEqual(v2["version_no"], 2)
        versions = self.h.repo.list_policy_versions(v["policy_id"])
        self.assertEqual([x.version_no for x in versions], [1, 2])
        self.assertEqual(
            [r.rule_id for r in versions[1].rules], ["c", "a"]
        )

    def test_invalid_dependencies_rejected(self) -> None:
        # 悬空依赖
        with self.assertRaises(ValidationError) as ctx:
            self.make_version([rule("a", depends_on=["ghost"])])
        self.assertIn("依赖不存在", ctx.exception.message)
        # 环依赖
        with self.assertRaises(ValidationError) as ctx:
            self.make_version([
                rule("a", depends_on=["b"]),
                rule("b", depends_on=["a"]),
            ])
        self.assertIn("环", ctx.exception.message)
        # 自依赖
        with self.assertRaises(ValidationError):
            self.make_version([rule("a", depends_on=["a"])])
        # 重复规则标识 / 空内容
        with self.assertRaises(ValidationError):
            self.make_version([rule("a"), rule("a")])
        with self.assertRaises(ValidationError):
            self.make_version([rule("a", content="  ")])

    def test_cross_policy_compare_rejected(self) -> None:
        v1 = self.make_version([rule("a")])
        v2 = self.make_version([rule("a")])
        with self.assertRaises(ValidationError) as ctx:
            self.h.ctx.policies.compare_versions(
                self.authority,
                from_version_id=v1["version_id"],
                to_version_id=v2["version_id"],
            )
        self.assertIn("同一策略", ctx.exception.message)

    def test_compare_requires_privileged_role(self) -> None:
        _, (v1, v2) = self.make_policy([[rule("a")], [rule("a"), rule("b")]])
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.policies.compare_versions(
                self.admin,
                from_version_id=v1["version_id"],
                to_version_id=v2["version_id"],
            )

    def test_register_case_validates_policy_and_rule(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.h.ctx.policies.register_case(
                self.admin, policy_id="pol_missing", rule_id="a"
            )
        self.assertIn("策略不存在", ctx.exception.message)

        policy_id, _ = self.make_policy([[rule("a")]])
        with self.assertRaises(NotFoundError) as ctx:
            self.h.ctx.policies.register_case(
                self.admin, policy_id=policy_id, rule_id="ghost"
            )
        self.assertIn("规则不存在于当前策略版本", ctx.exception.message)

    def test_create_version_requires_rule_owner(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.policies.create_policy_version(
                self.admin, rules=[rule("a")]
            )


if __name__ == "__main__":
    unittest.main()
