"""策略版本比较（纯函数，不触碰持久化）。

输入两个策略版本与相关案件，输出：
- 规则顺序变化（新增/移除/移动/修改，含位置前后对照）；
- 受影响案件：直接绑定变化规则的案件，以及经规则依赖链
  （取两个版本依赖边的并集）间接受影响的案件。

比较结果为冻结数据，仅派生自输入，不产生任何副作用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import PolicyCase, PolicyVersion

# 规则变化种类
ADDED = "added"
REMOVED = "removed"
MOVED = "moved"
MODIFIED = "modified"

# 案件受影响原因
RULE_CHANGED = "rule_changed"
DEPENDENCY_CHANGED = "dependency_changed"


@dataclass(frozen=True)
class RuleChange:
    rule_id: str
    change: str                  # added / removed / moved / modified
    from_position: int | None    # removed 时为旧位置；added 时为 None
    to_position: int | None      # added 时为新位置；removed 时为 None
    content_changed: bool = False
    depends_changed: bool = False


@dataclass(frozen=True)
class AffectedCase:
    case_id: str
    rule_id: str                 # 案件绑定的规则
    reason: str                  # rule_changed / dependency_changed
    via_rule_ids: tuple[str, ...] = ()  # 依赖传播时，发生变化的上游规则


@dataclass(frozen=True)
class PolicyComparison:
    policy_id: str
    from_version_id: str
    from_version_no: int
    to_version_id: str
    to_version_no: int
    rule_changes: tuple[RuleChange, ...]
    affected_cases: tuple[AffectedCase, ...]

    def to_dict(self) -> dict:
        summary = {"added": 0, "removed": 0, "moved": 0, "modified": 0}
        for change in self.rule_changes:
            summary[change.change] += 1
        return {
            "policy_id": self.policy_id,
            "from_version": {
                "version_id": self.from_version_id,
                "version_no": self.from_version_no,
            },
            "to_version": {
                "version_id": self.to_version_id,
                "version_no": self.to_version_no,
            },
            "rule_changes": [
                {
                    "rule_id": c.rule_id,
                    "change": c.change,
                    "from_position": c.from_position,
                    "to_position": c.to_position,
                    "content_changed": c.content_changed,
                    "depends_changed": c.depends_changed,
                }
                for c in self.rule_changes
            ],
            "affected_cases": [
                {
                    "case_id": a.case_id,
                    "rule_id": a.rule_id,
                    "reason": a.reason,
                    "via_rule_ids": list(a.via_rule_ids),
                }
                for a in self.affected_cases
            ],
            "summary": {**summary, "affected_cases": len(self.affected_cases)},
        }


def compare_policy_versions(
    old: PolicyVersion,
    new: PolicyVersion,
    cases: Iterable[PolicyCase],
) -> PolicyComparison:
    """比较同一策略的两个版本，返回只读的比较结果。"""
    old_rules = {r.rule_id: r for r in old.rules}
    new_rules = {r.rule_id: r for r in new.rules}

    changes: list[RuleChange] = []
    for rule in old.rules:  # 旧版本顺序遍历，输出确定
        current = new_rules.get(rule.rule_id)
        if current is None:
            changes.append(
                RuleChange(rule.rule_id, REMOVED, rule.position, None)
            )
            continue
        content_changed = current.content != rule.content
        depends_changed = current.depends_on != rule.depends_on
        if current.position != rule.position:
            changes.append(
                RuleChange(
                    rule.rule_id, MOVED, rule.position, current.position,
                    content_changed=content_changed,
                    depends_changed=depends_changed,
                )
            )
        elif content_changed or depends_changed:
            changes.append(
                RuleChange(
                    rule.rule_id, MODIFIED, rule.position, current.position,
                    content_changed=content_changed,
                    depends_changed=depends_changed,
                )
            )
    for rule in new.rules:
        if rule.rule_id not in old_rules:
            changes.append(RuleChange(rule.rule_id, ADDED, None, rule.position))

    changed_ids = {c.rule_id for c in changes}
    affected_cases = _affected_cases(old, new, cases, changed_ids)
    return PolicyComparison(
        policy_id=new.policy_id,
        from_version_id=old.version_id,
        from_version_no=old.version_no,
        to_version_id=new.version_id,
        to_version_no=new.version_no,
        rule_changes=tuple(changes),
        affected_cases=affected_cases,
    )


def _affected_cases(
    old: PolicyVersion,
    new: PolicyVersion,
    cases: Iterable[PolicyCase],
    changed_ids: set[str],
) -> tuple[AffectedCase, ...]:
    """受影响案件 = 直接绑定变化规则 + 依赖链下游（两版本依赖边并集）。"""
    dependents: dict[str, set[str]] = {}  # 被依赖规则 -> 依赖它的规则集合
    for version in (old, new):
        for rule in version.rules:
            for upstream in rule.depends_on:
                dependents.setdefault(upstream, set()).add(rule.rule_id)

    # 从全部变化规则出发沿依赖边反向可达的规则均受影响
    reached: dict[str, set[str]] = {}  # 间接受影响规则 -> 其变化上游集合
    stack = [(rid, rid) for rid in sorted(changed_ids)]
    while stack:
        upstream, origin = stack.pop()
        for dependent in sorted(dependents.get(upstream, ())):
            if dependent in changed_ids:
                continue  # 直接变化优先，不重复标记为间接受影响
            origins = reached.setdefault(dependent, set())
            if origin in origins:
                continue
            origins.add(origin)
            stack.append((dependent, origin))

    affected: list[AffectedCase] = []
    for case in sorted(cases, key=lambda c: c.case_id):
        if case.rule_id in changed_ids:
            affected.append(AffectedCase(case.case_id, case.rule_id, RULE_CHANGED))
        elif case.rule_id in reached:
            affected.append(
                AffectedCase(
                    case.case_id,
                    case.rule_id,
                    DEPENDENCY_CHANGED,
                    via_rule_ids=tuple(sorted(reached[case.rule_id])),
                )
            )
    return tuple(affected)
