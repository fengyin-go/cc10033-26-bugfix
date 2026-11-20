"""一致性键比对：以实际主键字段和标准版本为准，展示名称不参与键兼容判断。

一致性维度的历史实现直接拿 `标准键值` 和维度 `维度名称` 比较，导致“同键异名”
被误报为键冲突。本模块把比对收敛成一条规则：

1. 标准侧取 `标准键值` 解析出的标准键字段；
2. 维度侧取 `主键定义` 解析出的实际主键字段；
3. 两侧都归一到“字段名”级别后比较，类型声明、大小写和展示名称都不影响结果；
4. 标准按主题 + 版本选择，默认取已发布的最新版本，也可由维度的 `标准版本` 指定。

比对结果同时记录参与比较的键、标准版本、名称是否相同，供判定台账追溯。
"""
from __future__ import annotations

import re
from typing import Any

# 维度编码后缀 / 标准主题之间的领域对应关系。
_THEME_MARKERS: list[tuple[str, str]] = [
    ("PRODUCT", "商品"),
    ("CUSTOMER", "客户"),
    ("SUPPLIER", "供应商"),
    ("CHANNEL", "渠道"),
    ("STORE", "门店"),
    ("TIME", "时间"),
    ("DATE", "时间"),
    ("ORG", "组织"),
]

_TYPE_TOKENS = {
    "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "DECIMAL", "NUMERIC",
    "VARCHAR", "CHAR", "STRING", "TEXT", "DATE", "DATETIME", "TIMESTAMP",
    "BOOLEAN", "BOOL", "FLOAT", "DOUBLE", "NUMBER", "PRIMARY", "KEY", "NOT",
    "NULL", "DEFAULT", "UNSIGNED", "COMMENT",
}

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def normalize_key_field(field: str) -> str:
    """把字段名归一到可比较形式：去引号、去空白、忽略大小写。"""
    value = str(field or "").strip()
    value = value.strip("`'\"")
    value = value.replace(" ", "").replace("\t", "")
    return value.casefold()


def parse_key_fields(definition: Any) -> list[str]:
    """从主键定义 / 标准键值中解析出字段名列表。

    - ``product_id VARCHAR(32)`` → ``["product_id"]``
    - ``store_id, product_id`` → ``["store_id", "product_id"]``
    - ``date_key`` → ``["date_key"]``
    """
    text = str(definition or "").strip()
    if not text:
        return []
    text = text.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    fields: list[str] = []
    for chunk in text.split(","):
        tokens = [token for token in re.split(r"\s+", chunk.strip()) if token]
        for token in tokens:
            cleaned = token.strip("`'\"")
            if not cleaned:
                continue
            if cleaned.upper() in _TYPE_TOKENS:
                continue
            if re.match(r"^[A-Za-z_][\w$#]*$", cleaned):
                if cleaned not in fields:
                    fields.append(cleaned)
                break  # 每个片段只取第一个字段名，后面的都是类型/约束
    return fields


def parse_version(text: Any) -> tuple[int, ...]:
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def theme_for_dim(dim: dict[str, Any]) -> str | None:
    code = str(dim.get("维度编码", "")).upper()
    for marker, theme in _THEME_MARKERS:
        if marker in code:
            return theme
    return None


def standard_for_dim(dim: dict[str, Any], standards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """为维度选择要对照的一致性标准：主题匹配 + 版本优先。

    默认取已发布的最新版本；维度显式声明 `标准版本` 时按该版本对照。
    已废止的标准只有在被版本明确指定时才可用，且会在结果里标记为未生效。
    """
    theme = theme_for_dim(dim)
    candidates = [s for s in standards if theme and str(s.get("维度主题", "")) == theme]
    if not candidates:
        return None

    version_hint = str(dim.get("标准版本", "")).strip()
    if version_hint:
        for standard in candidates:
            if str(standard.get("版本号", "")) == version_hint:
                return standard
        return None

    published = [s for s in candidates if s.get("status") == "已发布"]
    pool = published or [s for s in candidates if s.get("status") != "已废止"]
    if not pool:
        # 只剩已废止标准：仍按最近版本对照，但结果会标记“标准未生效”并列为待验证。
        pool = candidates
    return max(pool, key=lambda s: parse_version(s.get("版本号", "")))


def compare_dim_to_standard(dim: dict[str, Any], standard: dict[str, Any]) -> dict[str, Any]:
    """按实际主键字段与标准键字段比对，返回可追溯的比对明细。"""
    actual_fields = parse_key_fields(dim.get("主键定义"))
    standard_fields = parse_key_fields(standard.get("标准键值"))

    actual_norm = {normalize_key_field(field) for field in actual_fields}
    standard_norm = {normalize_key_field(field) for field in standard_fields}

    compared_keys: list[dict[str, Any]] = []
    for index in range(max(len(actual_fields), len(standard_fields))):
        actual = actual_fields[index] if index < len(actual_fields) else ""
        expected = standard_fields[index] if index < len(standard_fields) else ""
        compared_keys.append({
            "position": index + 1,
            "actual": actual,
            "standard": expected,
            "match": bool(actual and expected and normalize_key_field(actual) == normalize_key_field(expected)),
        })

    missing_in_dimension = [f for f in standard_fields if normalize_key_field(f) not in actual_norm]
    extra_in_dimension = [f for f in actual_fields if normalize_key_field(f) not in standard_norm]

    located = bool(actual_fields) and bool(standard_fields)
    matched = located and actual_norm == standard_norm

    dim_name = str(dim.get("维度名称", ""))
    standard_name = str(standard.get("标准名称", ""))
    same_name = bool(dim_name and standard_name and dim_name == standard_name)

    return {
        "dim_code": dim.get("维度编码", ""),
        "dim_name": dim_name,
        "standard_id": standard.get("id"),
        "standard_name": standard_name,
        "standard_version": str(standard.get("版本号", "")),
        "standard_status": str(standard.get("status", "")),
        "standard_active": standard.get("status") == "已发布",
        "actual_key_fields": actual_fields,
        "standard_key_fields": standard_fields,
        "actual_primary_key_definition": str(dim.get("主键定义", "")),
        "standard_key_value": str(standard.get("标准键值", "")),
        "compared_keys": compared_keys,
        "missing_in_dimension": missing_in_dimension,
        "extra_in_dimension": extra_in_dimension,
        "keys_located": located,
        "matched": matched,
        "same_name": same_name,
        # 同键异名：键一致但展示名称不同；同名异键：名称一致但键不同。
        "same_key_different_name": matched and not same_name,
        "same_name_different_key": same_name and not matched,
        "rule": "实际主键字段 vs 标准键字段（按标准版本）",
        "display_name_used_for_key_match": False,
    }


def comparison_label(comparison: dict[str, Any]) -> str:
    actual = ".".join(comparison.get("actual_key_fields", [])) or "未定位"
    standard = ".".join(comparison.get("standard_key_fields", [])) or "未定位"
    return f"{actual}↔{standard}"


# 目标模型到事实表的业务映射：模型校验需要用事实的分区字段登记分区结果。
MODEL_FACT_MAP = {
    "dwd_sales_detail": "销售明细事实",
    "dwd_warehouse_change": "仓储变动事实",
    "dwd_purchase_order": "采购订单事实",
    "dwd_customer_behavior": "客户行为事实",
}

MODEL_DIM_HINTS = {
    "dwd_sales_detail": ["DIM_TIME", "DIM_PRODUCT", "DIM_STORE"],
    "dwd_warehouse_change": ["DIM_TIME", "DIM_PRODUCT", "DIM_STORE"],
    "dwd_purchase_order": ["DIM_TIME", "DIM_PRODUCT", "DIM_SUPPLIER"],
    "dwd_customer_behavior": ["DIM_TIME", "DIM_CUSTOMER", "DIM_CHANNEL"],
}


def locate_model_dimensions(target_model: str, store: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """定位模型关联的维度定义，返回 (维度行, 定位说明)。

    优先使用模型血缘里的维度上游，再补充事实设计声明的关联维度；
    目标模型本身是维度时直接按维度编码匹配。定位不到就返回空列表，
    调用方必须把这类记录列为待验证，而不能当成通过。
    """
    target = str(target_model or "").strip()
    target_lower = target.lower()
    notes: list[str] = []
    dim_table = store.rows("dim_definition")
    located: dict[str, dict[str, Any]] = {}

    if target_lower.startswith("dim_"):
        direct_code = ("DIM_" + target_lower[len("dim_"):]).upper()
        for dim in dim_table:
            code = str(dim.get("维度编码", "")).upper()
            if code == direct_code or code.startswith(direct_code):
                located[code] = dim
        if not located:
            for dim in dim_table:
                code = str(dim.get("维度编码", "")).upper()
                suffix = target_lower[len("dim_"):].upper()
                if suffix and suffix in code:
                    located[code] = dim
        notes.append(f"按维度编码定位 {direct_code}" if located else f"未找到维度编码 {direct_code}")

    # 模型血缘中的维度上游
    for relation in store.rows("model_lineage"):
        if str(relation.get("下游模型", "")).strip().lower() != target_lower:
            continue
        upstream = str(relation.get("上游模型", "")).strip().upper()
        if not upstream.startswith("DIM_"):
            continue
        for dim in dim_table:
            if str(dim.get("维度编码", "")).upper() == upstream:
                located[upstream] = dim
                notes.append(f"血缘定位维度 {upstream}（依赖字段 {relation.get('依赖字段', '')}）")

    # 事实设计声明的关联维度
    fact_name = MODEL_FACT_MAP.get(target_lower, "")
    if not fact_name:
        for fact in store.rows("fact_design"):
            if str(fact.get("事实名称", "")).lower() == target_lower:
                fact_name = str(fact.get("事实名称", ""))
                break
    if fact_name:
        for fact in store.rows("fact_design"):
            if str(fact.get("事实名称", "")) != fact_name:
                continue
            for code in str(fact.get("关联维度", "")).split("."):
                code = code.strip().upper()
                if not code:
                    continue
                for dim in dim_table:
                    if str(dim.get("维度编码", "")).upper() == code:
                        located.setdefault(code, dim)
                        notes.append(f"事实 {fact_name} 关联维度 {code}")
            break

    if not located and not notes:
        notes.append("模型未关联任何维度，无法定位实际主键字段")

    ordered = [located[key] for key in sorted(located)]
    return ordered, notes


def locate_model_partitions(target_model: str, store: Any) -> list[str]:
    """定位目标模型的分区字段及已登记的分区值。

    分区值优先取事实表的分区列，其次取快照计划中对目标表声明的分区格式，
    这样“按分区登记是否通过”至少有可核对的粒度，而不是笼统的模型级结论。
    """
    target_lower = str(target_model or "").strip().lower()
    fact_name = MODEL_FACT_MAP.get(target_lower, "")
    if not fact_name:
        for fact in store.rows("fact_design"):
            if str(fact.get("事实名称", "")).lower() == target_lower:
                fact_name = str(fact.get("事实名称", ""))
                break

    partitions: list[str] = []
    partition_field = ""
    if fact_name:
        for fact in store.rows("fact_design"):
            if str(fact.get("事实名称", "")) != fact_name:
                continue
            fields = parse_key_fields(fact.get("分区字段", ""))
            partition_field = fields[0] if fields else ""
            value = str(fact.get(partition_field, "") or "").strip() if partition_field else ""
            if value:
                partitions.append(value)
            elif partition_field:
                partitions.append(f"{partition_field}（已声明，暂无分区值）")

    for plan in store.rows("snapshot_plan"):
        if str(plan.get("目标表", "")).strip().lower() != target_lower:
            continue
        fmt = str(plan.get("分区格式", "")).strip()
        if fmt and fmt not in partitions:
            partitions.append(fmt)

    if partitions:
        return partitions
    return ["未定位分区"]


def compare_model_keys(target_model: str, store: Any) -> dict[str, Any]:
    """模型级的实际键比对：逐个维度比对主键字段与对应标准键字段。"""
    dims, locate_notes = locate_model_dimensions(target_model, store)
    standards = store.rows("conform_dim")
    comparisons: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []

    for dim in dims:
        standard = standard_for_dim(dim, standards)
        if standard is None:
            unresolved.append({
                "dim_code": dim.get("维度编码", ""),
                "dim_name": dim.get("维度名称", ""),
                "reason": "未找到与维度主题匹配的一致性标准" if theme_for_dim(dim) else "无法从维度编码推断主题",
                "actual_key_fields": parse_key_fields(dim.get("主键定义")),
                "standard_key_fields": [],
            })
            continue
        comparison = compare_dim_to_standard(dim, standard)
        comparisons.append(comparison)
        if not comparison["matched"]:
            mismatches.append(comparison)
        if not comparison["standard_active"]:
            unresolved.append({
                "dim_code": comparison["dim_code"],
                "dim_name": comparison["dim_name"],
                "reason": f"对照标准 {comparison['standard_name']} {comparison['standard_version']} 未发布生效",
                "actual_key_fields": comparison["actual_key_fields"],
                "standard_key_fields": comparison["standard_key_fields"],
            })

    if mismatches:
        verdict = "未通过"
    elif unresolved or not comparisons:
        verdict = "待验证"
    else:
        verdict = "通过"

    all_keys_located = bool(comparisons) and not unresolved and all(
        comparison["keys_located"] for comparison in comparisons
    )
    # 维度模型按表级登记，不做分区结论；事实模型按分区字段逐个登记。
    if str(target_model).strip().lower().startswith("dim_"):
        partitions: list[str] = []
    else:
        partitions = locate_model_partitions(target_model, store)

    mismatched_keys: list[dict[str, Any]] = []
    for comparison in mismatches:
        for item in comparison["compared_keys"]:
            if not item["match"]:
                mismatched_keys.append({
                    "dim_code": comparison["dim_code"],
                    "actual": item["actual"],
                    "standard": item["standard"],
                    "position": item["position"],
                })

    partition_results = [
        {
            "partition": partition,
            "passed": verdict == "通过",
            "verdict": verdict,
            "mismatched_keys": mismatched_keys,
            "unresolved": unresolved,
        }
        for partition in partitions
    ]

    return {
        "target_model": target_model,
        "dimensions": dims,
        "locate_notes": locate_notes,
        "comparisons": comparisons,
        "unresolved": unresolved,
        "mismatches": mismatches,
        "mismatched_keys": mismatched_keys,
        "verdict": verdict,
        "all_keys_located": all_keys_located,
        "partitions": partitions,
        "partition_results": partition_results,
        "actual_key_fields": [field for c in comparisons for field in c["actual_key_fields"]],
        "standard_key_fields": [field for c in comparisons for field in c["standard_key_fields"]],
        "compared_key_labels": [comparison_label(c) for c in comparisons],
        "rule": "模型维度实际主键字段 vs 一致性标准键字段（按标准版本）",
    }


def conform_scope(dim_code: str, standard_name: str) -> str:
    return f"conform_dim|{dim_code}|{standard_name}"


def model_validate_scope(target_model: str, validation_type: str) -> str:
    return f"model_validate|{target_model}|{validation_type}"


def record_conform_verdict(
    store: Any,
    dim: dict[str, Any],
    standard: dict[str, Any],
    comparison: dict[str, Any],
    *,
    source: str,
    used_for_delivery: bool | None = None,
    delivery_ref: str | None = None,
    legacy: bool = False,
) -> dict[str, Any]:
    """把一致性比对结论登记到判定台账，同一维度范围只保留一项有效判定。"""
    scope = conform_scope(str(dim.get("维度编码", "")), str(standard.get("标准名称", "")))
    if not comparison.get("standard_active", True):
        verdict = "待验证"
    else:
        verdict = "通过" if comparison["matched"] else "未通过"
    basis = {
        "rule": comparison["rule"],
        "legacy_rule": "标准键值 vs 维度名称（展示名直接比较）" if legacy else "",
        "actual_key_fields": comparison["actual_key_fields"],
        "standard_key_fields": comparison["standard_key_fields"],
        "compared_keys": comparison["compared_keys"],
        "standard_name": comparison["standard_name"],
        "standard_version": comparison["standard_version"],
        "standard_id": comparison["standard_id"],
        "standard_status": comparison["standard_status"],
        "standard_active": comparison.get("standard_active", True),
        "dim_name": comparison["dim_name"],
        "same_name": comparison["same_name"],
        "same_key_different_name": comparison["same_key_different_name"],
        "same_name_different_key": comparison["same_name_different_key"],
        "all_keys_located": comparison["keys_located"],
    }
    current = store.verdicts.effective(scope)
    if (
        current
        and not current.get("legacy_rule")
        and current.get("basis", {}).get("rule") == basis["rule"]
        and current.get("verdict") == verdict
        and current.get("basis", {}).get("actual_key_fields") == basis["actual_key_fields"]
        and current.get("basis", {}).get("standard_key_fields") == basis["standard_key_fields"]
        and current.get("basis", {}).get("standard_version") == basis["standard_version"]
        and (not used_for_delivery or current.get("used_for_delivery"))
    ):
        return current
    return store.verdicts.record(
        scope=scope,
        subject=str(dim.get("维度编码", "")),
        subject_kind="conform_dim",
        verdict=verdict,
        basis=basis,
        source=source,
        entry_id=standard.get("id"),
        used_for_delivery=used_for_delivery,
        delivery_ref=delivery_ref,
        legacy=legacy,
    )
