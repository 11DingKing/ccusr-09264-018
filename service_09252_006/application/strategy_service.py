"""规则策略服务：策略/版本/案件的写入，以及【只读】的策略版本比较。

compare_versions 是纯读接口：
- 不开启写事务、不落审计、不改任何持久化状态，可安全并发调用；
- 返回 frozen 的 StrategyComparison（tuple 集合），比较结果只读；
- 输入版本不存在时抛 NotFoundError，details 指明 strategy_id、
  version_id 与 side（from/to），错误明确可定位。
"""
from __future__ import annotations

from ..domain.enums import Role
from ..domain.errors import NotFoundError, PermissionDeniedError, ValidationError
from ..domain.models import User
from ..domain.strategy import (
    RuleStrategy,
    StrategyCase,
    StrategyComparison,
    StrategyRule,
    StrategyVersion,
    compare_strategy_versions,
    validate_rules,
)
from .base import Service, require_roles, require_user


class StrategyService(Service):
    # ------------------------------------------------------------- 策略
    def create_strategy(
        self,
        actor: User,
        *,
        name: str,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)
        if not name.strip():
            raise ValidationError("策略名称不能为空")

        def work() -> dict:
            strategy = RuleStrategy(
                strategy_id=self.ids.new_id("str"),
                institution_id=actor.institution_id or "",
                name=name.strip(),
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
            )
            self.repo.insert_strategy(strategy)
            self.audit(
                actor.user_id, "strategy.created",
                institution_id=strategy.institution_id,
                detail={"strategy_id": strategy.strategy_id},
            )
            return self._strategy_dict(strategy)

        return self.idempotent(idempotency_key, work)

    # ------------------------------------------------------------- 版本
    def publish_version(
        self,
        actor: User,
        *,
        strategy_id: str,
        rules: list[dict],
        idempotency_key: str | None = None,
    ) -> dict:
        """发布不可变版本；rules 的列表顺序即规则评估顺序。"""
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)
        built = _build_rules(rules)  # 纯校验，失败不进事务

        def work() -> dict:
            strategy = self._require_strategy(strategy_id)
            self._check_write_permission(actor, strategy)
            prior = self.repo.list_strategy_versions(strategy_id)
            version = StrategyVersion(
                version_id=self.ids.new_id("sver"),
                strategy_id=strategy_id,
                institution_id=strategy.institution_id,
                version_no=len(prior) + 1,
                supersedes_version_id=prior[-1].version_id if prior else None,
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
                rules=built,
            )
            self.repo.insert_strategy_version(version)
            self.audit(
                actor.user_id, "strategy_version.published",
                institution_id=strategy.institution_id,
                detail={
                    "strategy_id": strategy_id,
                    "version_id": version.version_id,
                    "version_no": version.version_no,
                    "rule_count": len(built),
                },
            )
            return self._version_dict(version)

        return self.idempotent(idempotency_key, work)

    # ------------------------------------------------------------- 案件
    def register_case(
        self,
        actor: User,
        *,
        strategy_id: str,
        version_id: str,
        matched_rule_keys: list[str],
        case_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """登记在某版本下决定的案件及其命中规则（受影响案件的数据来源）。"""
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)
        keys = tuple(dict.fromkeys(str(k) for k in matched_rule_keys))

        def work() -> dict:
            strategy = self._require_strategy(strategy_id)
            self._check_write_permission(actor, strategy)
            version = self.repo.get_strategy_version(version_id)
            if version is None or version.strategy_id != strategy_id:
                raise NotFoundError(
                    "策略版本不存在",
                    details={"strategy_id": strategy_id, "version_id": version_id},
                )
            cid = case_id or self.ids.new_id("case")
            existing = self.repo.get_strategy_case(cid)
            if existing is not None:
                # 客户端指定 id 的重复提交：回放，不报错
                return self._case_dict(existing, replayed=True)
            known = {r.rule_key for r in version.rules}
            unknown = [k for k in keys if k not in known]
            if unknown:
                raise ValidationError(
                    "案件命中了版本中不存在的规则",
                    details={"version_id": version_id, "rule_keys": unknown},
                )
            case = StrategyCase(
                case_id=cid,
                strategy_id=strategy_id,
                institution_id=strategy.institution_id,
                version_id=version_id,
                matched_rule_keys=keys,
                decided_by=actor.user_id,
                decided_at=self.clock.now_iso(),
            )
            self.repo.insert_strategy_case(case)
            self.audit(
                actor.user_id, "strategy_case.registered",
                institution_id=strategy.institution_id,
                detail={
                    "strategy_id": strategy_id,
                    "version_id": version_id,
                    "case_id": cid,
                },
            )
            return self._case_dict(case)

        return self.idempotent(idempotency_key, work)

    # --------------------------------------------------------- 只读比较
    def compare_versions(
        self,
        actor: User,
        *,
        strategy_id: str,
        from_version_id: str,
        to_version_id: str,
    ) -> StrategyComparison:
        """只读比较两个版本：规则顺序变化 + 受影响案件。

        不开启写事务、不写审计、不改任何状态；返回 frozen 报告。
        """
        require_user(actor)
        strategy = self.repo.get_strategy(strategy_id)
        if strategy is None:
            raise NotFoundError("策略不存在", details={"strategy_id": strategy_id})
        if (
            actor.institution_id != strategy.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
        ):
            raise PermissionDeniedError("不能查看其他机构的策略")
        from_version = self._require_version(
            strategy_id, from_version_id, side="from"
        )
        to_version = self._require_version(strategy_id, to_version_id, side="to")
        cases = self.repo.list_cases_by_version(from_version_id)
        return compare_strategy_versions(from_version, to_version, cases)

    # ------------------------------------------------------------- 辅助
    def _require_strategy(self, strategy_id: str) -> RuleStrategy:
        strategy = self.repo.get_strategy(strategy_id)
        if strategy is None:
            raise NotFoundError("策略不存在", details={"strategy_id": strategy_id})
        return strategy

    def _require_version(
        self, strategy_id: str, version_id: str, *, side: str
    ) -> StrategyVersion:
        version = (
            self.repo.get_strategy_version(version_id) if version_id else None
        )
        # 属于其他策略的版本同样按“不存在”处理，不泄露跨策略信息
        if version is None or version.strategy_id != strategy_id:
            raise NotFoundError(
                "策略版本不存在",
                details={
                    "strategy_id": strategy_id,
                    "version_id": version_id,
                    "side": side,
                },
            )
        return version

    @staticmethod
    def _check_write_permission(actor: User, strategy: RuleStrategy) -> None:
        if (
            strategy.institution_id != actor.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
        ):
            raise PermissionDeniedError("只能维护本机构的策略")

    @staticmethod
    def _strategy_dict(s: RuleStrategy) -> dict:
        return {
            "strategy_id": s.strategy_id,
            "institution_id": s.institution_id,
            "name": s.name,
            "created_by": s.created_by,
            "created_at": s.created_at,
        }

    @staticmethod
    def _version_dict(v: StrategyVersion) -> dict:
        return {
            "version_id": v.version_id,
            "strategy_id": v.strategy_id,
            "institution_id": v.institution_id,
            "version_no": v.version_no,
            "supersedes_version_id": v.supersedes_version_id,
            "created_by": v.created_by,
            "created_at": v.created_at,
            "rules": [
                {
                    "rule_key": r.rule_key,
                    "position": r.position,
                    "content": r.content,
                    "depends_on": list(r.depends_on),
                }
                for r in v.rules
            ],
        }

    @staticmethod
    def _case_dict(c: StrategyCase, *, replayed: bool = False) -> dict:
        return {
            "case_id": c.case_id,
            "strategy_id": c.strategy_id,
            "version_id": c.version_id,
            "matched_rule_keys": list(c.matched_rule_keys),
            "decided_by": c.decided_by,
            "decided_at": c.decided_at,
            "replayed": replayed,
        }


def _build_rules(specs: list[dict]) -> tuple[StrategyRule, ...]:
    """把输入规格规整为有序规则元组并做发布校验。"""
    if not specs:
        raise ValidationError("策略版本至少包含一条规则")
    built: list[StrategyRule] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ValidationError("规则必须为对象", details={"index": index})
        key = str(spec.get("rule_key") or "").strip()
        if not key:
            raise ValidationError("规则键不能为空", details={"index": index})
        content = spec.get("content") or {}
        if not isinstance(content, dict):
            raise ValidationError(
                "规则内容必须为对象", details={"rule_key": key}
            )
        depends_on = tuple(str(d) for d in (spec.get("depends_on") or ()))
        built.append(
            StrategyRule(
                rule_key=key,
                position=index,
                content=content,
                depends_on=depends_on,
            )
        )
    result = tuple(built)
    validate_rules(result)
    return result
