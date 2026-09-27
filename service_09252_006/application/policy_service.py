"""策略版本服务：规则版本登记、案件绑定与只读的版本比较。

- 规则负责人（quality_authority）登记策略版本；规则顺序由提交列表
  顺序固定为 position，依赖（depends_on）必须指向同版本内规则且无环；
- 案件由机构登记并绑定到当前版本的规则；
- compare_versions 为只读接口：不开启写事务、不写审计，仅查询并
  调用领域纯函数得到比较结果；输入版本不存在时抛出带定位信息的
  NotFoundError。
"""
from __future__ import annotations

from ..domain.enums import Role
from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import PolicyCase, PolicyRule, PolicyVersion, User
from ..domain.policy_compare import compare_policy_versions
from .base import Service, require_roles


class PolicyService(Service):
    # -------------------------------------------------------- 登记策略版本
    def create_policy_version(
        self,
        actor: User,
        *,
        rules: list[dict],
        policy_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """登记新的策略版本；rules 按列表顺序固定为版本内顺序。

        每个 rule: {"rule_id": str, "content": str, "depends_on": [rule_id]}
        """
        require_roles(actor, Role.QUALITY_AUTHORITY)
        ordered_rules = self._validate_rules(rules)

        def work() -> dict:
            pid = policy_id or self.ids.new_id("pol")
            latest = self.repo.latest_policy_version(pid)
            version_no = 1 if latest is None else latest.version_no + 1
            version = PolicyVersion(
                version_id=self.ids.new_id("pver"),
                policy_id=pid,
                version_no=version_no,
                rules=tuple(ordered_rules),
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
            )
            self.repo.insert_policy_version(version)
            self.audit(
                actor.user_id, "policy.version_created",
                detail={"policy_id": pid, "version_no": version_no},
            )
            return self._version_dict(version)

        return self.idempotent(idempotency_key, work)

    # ------------------------------------------------------------ 登记案件
    def register_case(
        self,
        actor: User,
        *,
        policy_id: str,
        rule_id: str,
        case_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """把案件绑定到策略当前版本的某条规则。"""
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.INSTITUTION_SUBMITTER)

        def work() -> dict:
            latest = self.repo.latest_policy_version(policy_id)
            if latest is None:
                raise NotFoundError("策略不存在", details={"policy_id": policy_id})
            if all(r.rule_id != rule_id for r in latest.rules):
                raise NotFoundError(
                    "规则不存在于当前策略版本",
                    details={
                        "policy_id": policy_id,
                        "rule_id": rule_id,
                        "version_no": latest.version_no,
                    },
                )
            cid = case_id or self.ids.new_id("case")
            existing = self.repo.get_case(cid)
            if existing is not None:
                return self._case_dict(existing, replayed=True)
            case = PolicyCase(
                case_id=cid,
                policy_id=policy_id,
                rule_id=rule_id,
                institution_id=actor.institution_id or "",
                status="open",
                created_at=self.clock.now_iso(),
            )
            self.repo.insert_case(case)
            self.audit(
                actor.user_id, "policy.case_registered",
                institution_id=case.institution_id,
                detail={"case_id": cid, "policy_id": policy_id, "rule_id": rule_id},
            )
            return self._case_dict(case)

        return self.idempotent(idempotency_key, work)

    # -------------------------------------------------------- 只读版本比较
    def compare_versions(
        self,
        actor: User,
        *,
        from_version_id: str | None = None,
        to_version_id: str | None = None,
        policy_id: str | None = None,
        from_version_no: int | None = None,
        to_version_no: int | None = None,
    ) -> dict:
        """比较两个策略版本：规则顺序变化与受影响案件。

        只读：不开启写事务、不写入任何记录。版本可用 version_id 直接
        指定，或用 policy_id + version_no 定位。
        """
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.AUDITOR)
        old = self._resolve_version(
            label="比较基准",
            version_id=from_version_id,
            policy_id=policy_id,
            version_no=from_version_no,
        )
        new = self._resolve_version(
            label="比较目标",
            version_id=to_version_id,
            policy_id=policy_id,
            version_no=to_version_no,
        )
        if old.policy_id != new.policy_id:
            raise ValidationError(
                "只能比较同一策略的版本",
                details={
                    "from_policy_id": old.policy_id,
                    "to_policy_id": new.policy_id,
                },
            )
        cases = self.repo.list_cases_by_policy(old.policy_id)
        return compare_policy_versions(old, new, cases).to_dict()

    # ------------------------------------------------------------- 辅助
    def _resolve_version(
        self,
        *,
        label: str,
        version_id: str | None,
        policy_id: str | None,
        version_no: int | None,
    ) -> PolicyVersion:
        if version_id is not None:
            version = self.repo.get_policy_version(version_id)
            ref = {"version_id": version_id}
        else:
            if policy_id is None or version_no is None:
                raise ValidationError(
                    f"缺少{label}版本标识：需 version_id 或 policy_id + version_no"
                )
            version = self.repo.get_policy_version_by_no(policy_id, version_no)
            ref = {"policy_id": policy_id, "version_no": version_no}
        if version is None:
            raise NotFoundError(f"策略版本不存在（{label}）", details=ref)
        return version

    @staticmethod
    def _validate_rules(rules: list[dict]) -> list[PolicyRule]:
        if not isinstance(rules, list) or not rules:
            raise ValidationError("策略版本至少包含一条规则")
        ordered: list[PolicyRule] = []
        seen: set[str] = set()
        for index, raw in enumerate(rules, start=1):
            if not isinstance(raw, dict):
                raise ValidationError("规则格式非法", details={"index": index})
            rule_id = str(raw.get("rule_id") or "").strip()
            content = str(raw.get("content") or "").strip()
            depends_raw = raw.get("depends_on") or ()
            if isinstance(depends_raw, str) or not isinstance(
                depends_raw, (list, tuple)
            ):
                raise ValidationError(
                    "规则依赖必须为规则标识列表", details={"rule_id": rule_id}
                )
            depends_on = tuple(depends_raw)
            if not rule_id:
                raise ValidationError("规则标识不能为空", details={"index": index})
            if rule_id in seen:
                raise ValidationError(
                    "规则标识重复", details={"rule_id": rule_id}
                )
            if not content:
                raise ValidationError(
                    "规则内容不能为空", details={"rule_id": rule_id}
                )
            if not all(isinstance(d, str) and d for d in depends_on):
                raise ValidationError(
                    "规则依赖必须为非空规则标识", details={"rule_id": rule_id}
                )
            seen.add(rule_id)
            ordered.append(
                PolicyRule(
                    rule_id=rule_id,
                    position=index,
                    content=content,
                    depends_on=depends_on,
                )
            )
        known = {r.rule_id for r in ordered}
        for rule in ordered:
            missing = [d for d in rule.depends_on if d not in known]
            if missing:
                raise ValidationError(
                    "规则依赖不存在于本版本",
                    details={"rule_id": rule.rule_id, "missing": missing},
                )
        PolicyService._require_acyclic(ordered)
        return ordered

    @staticmethod
    def _require_acyclic(rules: list[PolicyRule]) -> None:
        """拓扑检查：依赖链存在环时拒绝登记。"""
        deps = {r.rule_id: r.depends_on for r in rules}
        state: dict[str, int] = {}  # 0=未访问 1=访问中 2=已完成

        def visit(rule_id: str, path: list[str]) -> None:
            mark = state.get(rule_id, 0)
            if mark == 2:
                return
            if mark == 1:
                cycle = path[path.index(rule_id):] + [rule_id]
                raise ValidationError(
                    "规则依赖存在环", details={"cycle": cycle}
                )
            state[rule_id] = 1
            for upstream in deps[rule_id]:
                visit(upstream, path + [upstream])
            state[rule_id] = 2

        for rule_id in deps:
            visit(rule_id, [rule_id])

    @staticmethod
    def _version_dict(v: PolicyVersion) -> dict:
        return {
            "policy_id": v.policy_id,
            "version_id": v.version_id,
            "version_no": v.version_no,
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "position": r.position,
                    "content": r.content,
                    "depends_on": list(r.depends_on),
                }
                for r in v.rules
            ],
            "created_by": v.created_by,
            "created_at": v.created_at,
        }

    @staticmethod
    def _case_dict(c: PolicyCase, *, replayed: bool = False) -> dict:
        return {
            "case_id": c.case_id,
            "policy_id": c.policy_id,
            "rule_id": c.rule_id,
            "institution_id": c.institution_id,
            "status": c.status,
            "created_at": c.created_at,
            "replayed": replayed,
        }
