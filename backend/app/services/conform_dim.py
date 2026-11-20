"""一致性维度服务：管理跨业务过程的一致性维度标准定义。

增强能力：
- 消费 dim.published 通道校验一致性
- 标准键冲突检测：按主题 + 标准键字段判重，不同版本可共存
- 版本号管理：递增版本号
- 适用范围标准化
- 标准列表直接给出实际主键字段比对结果与判定沿革（旧误判不被静默覆盖）
"""
from __future__ import annotations

from typing import Any

from app.key_compare import (
    compare_dim_to_standard,
    parse_key_fields,
    record_conform_verdict,
    standard_for_dim,
)
from app.services.base import BaseModuleService
from app.store import store

SCOPES = {"全仓", "零售域", "供应链域", "财务域", "营销域", "HR域"}


class ConformDimService(BaseModuleService):
    MODULE = "conform_dim"
    ENTITY = "标准维度"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["标准名称", "维度主题", "标准键值", "标准属性", "适用范围", "版本号", "发布状态"]
    REQUIRED_FIELDS = ["标准名称", "维度主题", "标准键值"]
    STATUS_ORDER = ["草稿", "已发布", "已废止"]
    ACTION_RULES = {"发布标准": "草稿", "废止标准": "已发布"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        scope = str(values.get("适用范围", "")).strip()
        if scope not in SCOPES:
            scope = "全仓"
            values["适用范围"] = scope

        # 标准键冲突检测：同一主题、同一版本下键字段集合不能重复。
        # 展示名称不参与判断，不同版本允许键相同（用于版本演进）。
        std_fields = {field.casefold() for field in parse_key_fields(values.get("标准键值"))}
        version = str(values.get("版本号", "") or "v1.0").strip()
        name = str(values.get("标准名称", "")).strip()
        for r in store.rows(self.MODULE):
            if (str(r.get("标准名称", "")).strip() == name
                    and str(r.get("版本号", "")) == version
                    and r.get("status") in ("草稿", "已发布")):
                return None, [f"标准 {name} 版本 {version} 已存在，请递增版本号"]
            if r.get("维度主题") != values.get("维度主题"):
                continue
            if str(r.get("版本号", "")) != version:
                continue
            existing_fields = {field.casefold() for field in parse_key_fields(r.get("标准键值"))}
            if std_fields and existing_fields and std_fields == existing_fields:
                if r.get("status") in ("草稿", "已发布"):
                    return None, [
                        f"标准键字段 {'/'.join(sorted(std_fields))} 在主题 "
                        f"{values.get('维度主题')} 版本 {version} 下已存在"
                    ]

        if not values.get("版本号"):
            values["版本号"] = version

        # 消费维度发布
        store.channel.consume("dim.published", "conform_dim")
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("conform_dim.defined", {
            "std_name": entry["标准名称"],
            "theme": entry["维度主题"],
            "std_key": entry["标准键值"],
            "std_key_fields": parse_key_fields(entry["标准键值"]),
            "version": entry.get("版本号", version),
            "scope": scope,
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "conform_dim", "conform_dim_standard",
            f"conform_{entry['id']}",
            {"std_name": entry["标准名称"], "theme": entry["维度主题"], "std_key": entry["标准键值"]},
            ["dim_definition", "conform_dim"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "发布标准":
            entry["发布状态"] = "已发布"
            store.channel.publish("conform_dim.published", {
                "std_name": entry["标准名称"],
                "std_key_fields": parse_key_fields(entry["标准键值"]),
                "version": entry.get("版本号", ""),
                "entry_id": entry_id,
            })
        elif action == "废止标准":
            entry["发布状态"] = "已废止"
            store.channel.publish("conform_dim.deprecated", {
                "std_name": entry["标准名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        published = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已发布")
        base["published_standards"] = published

        # 逐条维度按实际主键字段重算，并登记到判定台账。
        key_results = self.domain_key_records()
        base["key_field_comparisons"] = key_results
        base["verdict_history"] = [
            record for record in store.verdicts.all_verdicts()
            if record.get("subject_kind") == "conform_dim"
        ]
        base["consistency_view"] = self.consistency_view()
        return base

    def consistency_view(self) -> dict[str, Any]:
        """一致性标准列表与模型校验入口共享的同一份键字段证据。"""
        records = self.domain_key_records()
        standards = [
            {
                "id": row.get("id"),
                "standard_name": row.get("标准名称"),
                "theme": row.get("维度主题"),
                "standard_key_value": row.get("标准键值"),
                "standard_key_fields": parse_key_fields(row.get("标准键值")),
                "version": row.get("版本号"),
                "status": row.get("status"),
                "published_status": row.get("发布状态"),
            }
            for row in store.rows(self.MODULE)
        ]
        return {
            "module": "conform_dim",
            "rule": "实际主键字段 vs 标准键字段（按标准版本）",
            "display_name_used_for_key_match": False,
            "standards": standards,
            "key_comparisons": records,
            "verdict_history": [
                record for record in store.verdicts.all_verdicts()
                if record.get("subject_kind") == "conform_dim"
            ],
        }

    def domain_key_records(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for dim in store.rows("dim_definition"):
            standard = standard_for_dim(dim, store.rows(self.MODULE))
            if standard is None:
                rows.append({
                    "dim_code": dim.get("维度编码", ""),
                    "dim_name": dim.get("维度名称", ""),
                    "standard_name": "",
                    "standard_version": "",
                    "actual_key_fields": parse_key_fields(dim.get("主键定义")),
                    "standard_key_fields": [],
                    "compared_keys": [],
                    "same_key_different_name": False,
                    "same_name_different_key": False,
                    "verdict": "待验证",
                    "verdict_id": "",
                    "effective": False,
                    "correction_of": None,
                    "prior_verdict": None,
                    "correction_note": "未找到与维度主题匹配的一致性标准",
                    "used_for_delivery": False,
                    "delivery_ref": "",
                })
                continue
            comparison = compare_dim_to_standard(dim, standard)
            record = record_conform_verdict(store, dim, standard, comparison, source="conform_dim.list")
            rows.append({
                "dim_code": comparison["dim_code"],
                "dim_name": comparison["dim_name"],
                "standard_name": comparison["standard_name"],
                "standard_version": comparison["standard_version"],
                "actual_key_fields": comparison["actual_key_fields"],
                "standard_key_fields": comparison["standard_key_fields"],
                "compared_keys": comparison["compared_keys"],
                "same_key_different_name": comparison["same_key_different_name"],
                "same_name_different_key": comparison["same_name_different_key"],
                "verdict": record["verdict"],
                "verdict_id": record["verdict_id"],
                "effective": record["effective"],
                "correction_of": record.get("correction_of"),
                "prior_verdict": record.get("prior_verdict"),
                "correction_note": record.get("correction_note"),
                "used_for_delivery": record.get("used_for_delivery", False),
                "delivery_ref": record.get("delivery_ref", ""),
            })
        return rows
