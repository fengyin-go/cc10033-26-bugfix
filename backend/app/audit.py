"""一致性审计与历史判定更正：让旧误判可追溯，并保证同范围只有一项有效判定。

- conform_dim：按模型/维度实际主键字段与标准键字段（含标准版本）重算结论
- model_validate：按模型分区重算键完整性结论，只有键定位成功的通过才签发凭证
- 旧判定不会被删除：保留原依据、曾用作交付依据的引用和更正指向
"""
from __future__ import annotations

from typing import Any

from app.key_compare import (
    compare_dim_to_standard,
    compare_model_keys,
    conform_scope,
    model_validate_scope,
    record_conform_verdict,
    standard_for_dim,
)


def _find_dim(store: Any, dim_code: str) -> dict[str, Any] | None:
    for dim in store.rows("dim_definition"):
        if str(dim.get("维度编码", "")) == dim_code:
            return dim
    return None


def _find_standard(store: Any, standard_name: str, version: str = "") -> dict[str, Any] | None:
    candidates = [
        standard for standard in store.rows("conform_dim")
        if str(standard.get("标准名称", "")) == standard_name
    ]
    if version:
        for standard in candidates:
            if str(standard.get("版本号", "")) == version:
                return standard
    if not candidates:
        return None
    return candidates[0]


def reconcile_conform_dim(store: Any) -> list[dict[str, Any]]:
    """重算一致性维度的历史判定，并生成更正记录。"""
    corrections: list[dict[str, Any]] = []
    legacy_records = [
        record for record in store.verdicts.all_verdicts()
        if record.get("subject_kind") == "conform_dim" and record.get("legacy_rule")
    ]
    for prior in legacy_records:
        if not prior.get("effective"):
            continue
        dim = _find_dim(store, str(prior.get("subject", "")))
        if dim is None:
            continue
        scope = str(prior.get("scope", ""))
        standard_name = scope.rsplit("|", 1)[-1] if "|" in scope else ""
        standard = _find_standard(store, standard_name)
        if standard is None:
            standard = standard_for_dim(dim, store.rows("conform_dim"))
        if standard is None:
            continue
        comparison = compare_dim_to_standard(dim, standard)
        corrected = record_conform_verdict(
            store, dim, standard, comparison,
            source="reconcile_conform_dim",
            used_for_delivery=bool(prior.get("used_for_delivery")),
            delivery_ref=str(prior.get("delivery_ref", "")),
            legacy=False,
        )
        if corrected.get("verdict_id") != prior.get("verdict_id"):
            corrections.append({
                "scope": scope,
                "prior_verdict_id": prior.get("verdict_id"),
                "prior_verdict": prior.get("verdict"),
                "verdict_id": corrected.get("verdict_id"),
                "verdict": corrected.get("verdict"),
                "compared_keys": comparison["compared_keys"],
                "actual_key_fields": comparison["actual_key_fields"],
                "standard_key_fields": comparison["standard_key_fields"],
                "standard_version": comparison["standard_version"],
                "false_positive": prior.get("verdict") == "未通过" and corrected.get("verdict") == "通过",
            })
    return corrections


def model_audit(store: Any, target_model: str | None = None) -> dict[str, Any]:
    """模型校验入口的审计视图：实际键字段、分区结论、有效判定和凭证。"""
    models: dict[str, dict[str, Any]] = {}
    rules = store.rows("model_validate")
    for rule in rules:
        model = str(rule.get("目标模型", ""))
        if target_model and model != target_model:
            continue
        validation_type = str(rule.get("校验类型", ""))
        if validation_type not in ("维度键完整性", "主键唯一性"):
            continue
        comparison = compare_model_keys(model, store)
        scope = model_validate_scope(model, validation_type)
        effective = store.verdicts.effective(scope)
        history = store.verdicts.history(scope)
        credentials = store.verdicts.credentials(model)
        models.setdefault(model, {
            "target_model": model,
            "actual_key_fields": comparison["actual_key_fields"],
            "standard_key_fields": comparison["standard_key_fields"],
            "compared_key_labels": comparison["compared_key_labels"],
            "compared_keys": [
                {"dim_code": c["dim_code"], **item}
                for c in comparison["comparisons"] for item in c["compared_keys"]
            ],
            "comparisons": comparison["comparisons"],
            "unresolved": comparison["unresolved"],
            "mismatched_keys": comparison["mismatched_keys"],
            "partitions": comparison["partition_results"],
            "current_verdict": comparison["verdict"],
            "verdict": effective,
            "history": history,
            "credentials": credentials,
            "rules": [],
        })
        models[model]["rules"].append({
            "entry_id": rule.get("id"),
            "rule_name": rule.get("规则名称"),
            "validation_type": validation_type,
            "status": rule.get("status"),
            "verification_state": rule.get("verification_state", ""),
            "compared_key_fields": rule.get("compared_key_fields", []),
            "partition_results": rule.get("partition_results", []),
        })
    return {"models": list(models.values()), "total": len(models)}


def current_key_evidence(store: Any, target_model: str) -> dict[str, Any]:
    """模型校验与一致性列表共用的同一份实际键字段证据。"""
    comparison = compare_model_keys(target_model, store)
    return {
        "target_model": target_model,
        "actual_key_fields": comparison["actual_key_fields"],
        "standard_key_fields": comparison["standard_key_fields"],
        "compared_key_labels": comparison["compared_key_labels"],
        "compared_keys": [
            {"dim_code": c["dim_code"], **item}
            for c in comparison["comparisons"] for item in c["compared_keys"]
        ],
        "verdict": comparison["verdict"],
        "unresolved": comparison["unresolved"],
        "mismatched_keys": comparison["mismatched_keys"],
        "rule": comparison["rule"],
    }


def reconcile_model_validate(store: Any) -> list[dict[str, Any]]:
    """重算模型校验的历史判定，撤销键未定位的旧凭证，并按分区登记结论。"""
    from app.services.model_validate import KEY_VALIDATION_TYPES, _status_for_verdict

    corrections: list[dict[str, Any]] = []
    legacy_records = [
        record for record in store.verdicts.all_verdicts()
        if record.get("subject_kind") == "model_validate" and record.get("legacy_rule")
    ]
    for prior in legacy_records:
        if not prior.get("effective"):
            continue
        scope = str(prior.get("scope", ""))
        parts = scope.split("|")
        if len(parts) != 3:
            continue
        _, target_model, validation_type = parts
        if validation_type not in KEY_VALIDATION_TYPES:
            # 非键类校验（关联率/非空/日期）不因键字段重算而改判，保留原判定依据。
            continue
        comparison = compare_model_keys(target_model, store)
        verdict = comparison["verdict"]
        basis = {
            "rule": comparison["rule"],
            "legacy_rule": prior.get("basis", {}).get("legacy_rule", ""),
            "actual_key_fields": comparison["actual_key_fields"],
            "standard_key_fields": comparison["standard_key_fields"],
            "compared_keys": [
                {"dim_code": c["dim_code"], **item}
                for c in comparison["comparisons"] for item in c["compared_keys"]
            ],
            "dimensions": [c["dim_code"] for c in comparison["comparisons"]],
            "unresolved": comparison["unresolved"],
            "mismatched_keys": comparison["mismatched_keys"],
            "partitions": comparison["partitions"],
            "partition_results": comparison["partition_results"],
            "all_keys_located": comparison["all_keys_located"],
            "standard_versions": sorted({
                f"{c['standard_name']}@{c['standard_version']}" for c in comparison["comparisons"]
            }),
        }
        record = store.verdicts.record(
            scope=scope,
            subject=target_model,
            subject_kind="model_validate",
            verdict=verdict,
            basis=basis,
            source="reconcile_model_validate",
            entry_id=prior.get("entry_id"),
            used_for_delivery=bool(prior.get("used_for_delivery")),
            delivery_ref=str(prior.get("delivery_ref", "")),
            correction_note=(
                f"更正判定 {prior.get('verdict')} → {verdict}："
                f"旧规则「{prior.get('basis', {}).get('rule', '')}」未定位实际主键，"
                f"新规则按实际主键字段 {'/'.join(comparison['actual_key_fields']) or '未定位'} 重算"
            ),
        )

        credential = None
        if validation_type in KEY_VALIDATION_TYPES:
            credential = store.verdicts.issue_credential(                target_model=target_model,
                scope=scope,
                rule_name=(prior.get("basis", {}).get("rule_name", "")
                           or _rule_name_for(store, scope)
                           or str(prior.get("subject", ""))),
                verdict_record=record,
                entry_id=prior.get("entry_id"),
                used_for_delivery=bool(prior.get("used_for_delivery")),
                delivery_ref=str(prior.get("delivery_ref", "")),
            )

        _sync_rule_row(store, target_model, validation_type, record, credential, verdict,
                       comparison, _status_for_verdict)
        corrections.append({
            "scope": scope,
            "target_model": target_model,
            "prior_verdict_id": prior.get("verdict_id"),
            "prior_verdict": prior.get("verdict"),
            "verdict_id": record.get("verdict_id"),
            "verdict": verdict,
            "revoked_credential": next(
                (c["credential_id"] for c in store.verdicts.credentials(target_model)
                 if c.get("verdict_id") == prior.get("verdict_id") and c.get("status") == "已撤销"),
                "",
            ),
            "credential_id": credential["credential_id"] if credential else "",
            "compared_keys": basis["compared_keys"],
            "partition_results": comparison["partition_results"],
            "false_positive": prior.get("verdict") == "通过" and verdict != "通过",
        })
    return corrections


def _rule_name_for(store: Any, scope: str) -> str:
    for rule in store.rows("model_validate"):
        if model_validate_scope(str(rule.get("目标模型", "")), str(rule.get("校验类型", ""))) == scope:
            return str(rule.get("规则名称", ""))
    return ""


def _sync_rule_row(
    store: Any,
    target_model: str,
    validation_type: str,
    record: dict[str, Any],
    credential: dict[str, Any] | None,
    verdict: str,
    comparison: dict[str, Any],
    status_for_verdict: Any,
) -> None:
    """把重新比对结论同步到对应规则行，避免旧“通过”继续展示成当前结论。"""
    for rule in store.rows("model_validate"):
        if str(rule.get("目标模型", "")) != target_model:
            continue
        if str(rule.get("校验类型", "")) != validation_type:
            continue
        rule["status"] = status_for_verdict(verdict)
        rule["pending"] = verdict != "通过"
        rule["abnormal"] = verdict == "未通过"
        rule["verification_state"] = verdict
        rule["verdict_id"] = record["verdict_id"]
        rule["credential_id"] = credential["credential_id"] if credential else ""
        rule["compared_key_fields"] = comparison["compared_key_labels"]
        rule["partition_results"] = comparison["partition_results"]
        if verdict == "未通过":
            rule["实际结果"] = f"键字段比对未通过（{len(comparison['mismatches'])}个维度不一致）"
            rule["差异说明"] = "；".join(
                f"{m['dim_code']} {('/'.join(m['actual_key_fields']) or '未定位')} ≠ "
                f"{('/'.join(m['standard_key_fields']) or '未定位')}"
                for m in comparison["mismatches"]
            )
        elif verdict == "待验证":
            rule["实际结果"] = "待验证：无法定位实际主键字段"
            rule["差异说明"] = "；".join(
                item.get("reason", "") for item in comparison["unresolved"]
            ) or "；".join(comparison["locate_notes"])
        else:
            rule["实际结果"] = f"{len(comparison['comparisons'])}/{len(comparison['comparisons'])}维度键字段比对通过"
            rule["差异说明"] = ""
        rule["执行时间"] = record.get("created_at", rule.get("执行时间", ""))


def reconcile_all(store: Any) -> dict[str, Any]:
    conform_corrections = reconcile_conform_dim(store)
    model_corrections = reconcile_model_validate(store)
    return {
        "conform_dim_corrections": conform_corrections,
        "model_validate_corrections": model_corrections,
    }
