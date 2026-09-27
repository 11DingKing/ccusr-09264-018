"""规则策略实体与策略版本比较（纯领域逻辑）。

- 策略版本是不可变的【有序规则集】：每条规则带位置（position，即顺序）
  与同版本内的依赖（depends_on），发布落库后不可改；
- compare_strategy_versions 是纯函数：输入两个版本的规则集与在 from
  版本下决定的案件，输出只读比较报告（frozen dataclass + tuple），
  不触碰任何持久化状态；
- 受影响案件：在 from 版本下决定、且其命中规则在 to 版本中发生变化
  （被移除/内容修改/顺序移动/依赖变化）的案件。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import ValidationError
from .fingerprint import canonical_json


# ---------------------------------------------------------------- 实体
@dataclass
class RuleStrategy:
    """一组有序规则的逻辑载体，由规则负责人维护。"""

    strategy_id: str
    institution_id: str
    name: str
    created_by: str
    created_at: str


@dataclass
class StrategyRule:
    """策略版本内的一条规则：position 即评估顺序，depends_on 为同版本内依赖。"""

    rule_key: str
    position: int
    content: dict
    depends_on: tuple[str, ...] = ()


@dataclass
class StrategyVersion:
    """策略的一次不可变版本；rules 按 position 升序。"""

    version_id: str
    strategy_id: str
    institution_id: str
    version_no: int
    supersedes_version_id: str | None
    created_by: str
    created_at: str
    rules: tuple[StrategyRule, ...] = ()

    def rule_map(self) -> dict[str, StrategyRule]:
        return {r.rule_key: r for r in self.rules}


@dataclass
class StrategyCase:
    """在某个策略版本下决定的案件及其命中的规则。"""

    case_id: str
    strategy_id: str
    institution_id: str
    version_id: str
    matched_rule_keys: tuple[str, ...]
    decided_by: str
    decided_at: str


# ------------------------------------------------------------ 比较结果
@dataclass(frozen=True)
class RuleOrderChange:
    """同一条规则在两个版本间的位置变化。"""

    rule_key: str
    from_position: int
    to_position: int


@dataclass(frozen=True)
class RuleDependencyChange:
    """同一条规则的依赖集合变化。"""

    rule_key: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class AffectedCase:
    """受版本差异影响的案件：命中规则中哪些发生了变化。"""

    case_id: str
    changed_rule_keys: tuple[str, ...]


@dataclass(frozen=True)
class StrategyComparison:
    """只读比较报告：frozen 结构 + tuple 集合，调用方无法改动。"""

    strategy_id: str
    from_version_id: str
    to_version_id: str
    added_rules: tuple[str, ...]
    removed_rules: tuple[str, ...]
    modified_rules: tuple[str, ...]
    moved_rules: tuple[RuleOrderChange, ...]
    dependency_changes: tuple[RuleDependencyChange, ...]
    affected_cases: tuple[AffectedCase, ...]

    @property
    def changed_rule_keys(self) -> tuple[str, ...]:
        """发生任意变化的规则键（去重，稳定顺序）。"""
        ordered = list(self.removed_rules) + list(self.modified_rules)
        ordered += [m.rule_key for m in self.moved_rules]
        ordered += [d.rule_key for d in self.dependency_changes]
        return tuple(dict.fromkeys(ordered))

    def to_dict(self) -> dict:
        """序列化为普通 dict（新建对象，不影响只读报告本身）。"""
        return {
            "strategy_id": self.strategy_id,
            "from_version_id": self.from_version_id,
            "to_version_id": self.to_version_id,
            "added_rules": list(self.added_rules),
            "removed_rules": list(self.removed_rules),
            "modified_rules": list(self.modified_rules),
            "moved_rules": [
                {
                    "rule_key": m.rule_key,
                    "from_position": m.from_position,
                    "to_position": m.to_position,
                }
                for m in self.moved_rules
            ],
            "dependency_changes": [
                {
                    "rule_key": d.rule_key,
                    "added": list(d.added),
                    "removed": list(d.removed),
                }
                for d in self.dependency_changes
            ],
            "affected_cases": [
                {"case_id": a.case_id, "changed_rule_keys": list(a.changed_rule_keys)}
                for a in self.affected_cases
            ],
            "changed_rule_keys": list(self.changed_rule_keys),
        }


# ------------------------------------------------------------ 发布校验
def validate_rules(rules: tuple[StrategyRule, ...]) -> None:
    """发布前校验：键唯一、依赖指向同版本规则、依赖无循环。"""
    keys = [r.rule_key for r in rules]
    if len(set(keys)) != len(keys):
        duplicated = sorted({k for k in keys if keys.count(k) > 1})
        raise ValidationError("规则键重复", details={"rule_keys": duplicated})
    known = set(keys)
    for rule in rules:
        unknown = [d for d in rule.depends_on if d not in known]
        if unknown:
            raise ValidationError(
                "规则依赖指向不存在的规则",
                details={"rule_key": rule.rule_key, "depends_on": unknown},
            )
    deps = {r.rule_key: r.depends_on for r in rules}
    cycle = _find_cycle(keys, deps)
    if cycle:
        raise ValidationError("规则依赖存在循环", details={"cycle": cycle})


def _find_cycle(keys: list[str], deps: dict[str, tuple[str, ...]]) -> list[str] | None:
    """深度优先找依赖环；找到返回环上的键序列，否则 None。"""
    visiting, done = 1, 2
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        state[node] = visiting
        stack.append(node)
        for dep in deps.get(node, ()):
            if state.get(dep) == visiting:
                return stack[stack.index(dep):] + [dep]
            if state.get(dep) is None:
                found = dfs(dep)
                if found:
                    return found
        stack.pop()
        state[node] = done
        return None

    for key in keys:
        if state.get(key) is None:
            found = dfs(key)
            if found:
                return found
    return None


# ------------------------------------------------------------ 版本比较
def compare_strategy_versions(
    from_version: StrategyVersion,
    to_version: StrategyVersion,
    cases: Iterable[StrategyCase],
) -> StrategyComparison:
    """纯函数：比较同一策略的两个版本。

    cases 为在 from 版本下决定的案件；命中规则发生变化的案件被标记为
    受影响案件。新增规则不影响既有案件（案件决定时该规则尚不存在）。
    """
    if from_version.strategy_id != to_version.strategy_id:
        raise ValidationError("只能比较同一策略的两个版本")
    from_map = from_version.rule_map()
    to_map = to_version.rule_map()

    added = tuple(r.rule_key for r in to_version.rules if r.rule_key not in from_map)
    removed = tuple(r.rule_key for r in from_version.rules if r.rule_key not in to_map)

    modified: list[str] = []
    moved: list[RuleOrderChange] = []
    dep_changes: list[RuleDependencyChange] = []
    for rule in from_version.rules:
        other = to_map.get(rule.rule_key)
        if other is None:
            continue
        if canonical_json(rule.content) != canonical_json(other.content):
            modified.append(rule.rule_key)
        if rule.position != other.position:
            moved.append(
                RuleOrderChange(
                    rule_key=rule.rule_key,
                    from_position=rule.position,
                    to_position=other.position,
                )
            )
        added_deps = tuple(d for d in other.depends_on if d not in rule.depends_on)
        removed_deps = tuple(d for d in rule.depends_on if d not in other.depends_on)
        if added_deps or removed_deps:
            dep_changes.append(
                RuleDependencyChange(
                    rule_key=rule.rule_key,
                    added=added_deps,
                    removed=removed_deps,
                )
            )

    changed = (
        set(removed)
        | set(modified)
        | {m.rule_key for m in moved}
        | {d.rule_key for d in dep_changes}
    )
    position_of = {r.rule_key: r.position for r in from_version.rules}
    affected: list[AffectedCase] = []
    for case in cases:
        hits = sorted(
            {k for k in case.matched_rule_keys if k in changed},
            key=lambda k: position_of.get(k, len(position_of)),
        )
        if hits:
            affected.append(
                AffectedCase(case_id=case.case_id, changed_rule_keys=tuple(hits))
            )
    affected.sort(key=lambda a: a.case_id)

    return StrategyComparison(
        strategy_id=from_version.strategy_id,
        from_version_id=from_version.version_id,
        to_version_id=to_version.version_id,
        added_rules=added,
        removed_rules=removed,
        modified_rules=tuple(modified),
        moved_rules=tuple(moved),
        dependency_changes=tuple(dep_changes),
        affected_cases=tuple(affected),
    )
