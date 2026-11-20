"""一致性键比对与判定台账回归测试。

覆盖：
- 同键异名不再被误报为键冲突（展示名称不参与比对）
- 同名异键会按实际主键字段报出冲突
- 标准版本参与对照，不同版本可以共存
- 无法定位实际主键的记录列为待验证，不签发模型凭证
- 历史误判与新结果冲突时，同范围只保留一项有效判定，旧判定和交付依据可追溯
"""
from __future__ import annotations

import pytest

from app.audit import model_audit, reconcile_all
from app.key_compare import (
    compare_dim_to_standard,
    compare_model_keys,
    parse_key_fields,
    standard_for_dim,
)
from app.services import service_for
from app.services.pipeline import conform_dim_constrain
from app.store import Store


@pytest.fixture
def store() -> Store:
    """把各服务共用的全局仓库就地重置，保证用例之间互不影响。"""
    from app.store import store as global_store

    fresh = Store()
    global_store._tables = fresh._tables
    global_store.channel = fresh.channel
    global_store.recovery = fresh.recovery
    global_store.snapshot = fresh.snapshot
    global_store.artifacts = fresh.artifacts
    global_store.verdicts = fresh.verdicts
    return global_store


def _dim(store: Store, code: str) -> dict:
    return next(row for row in store.rows("dim_definition") if row["维度编码"] == code)


def _standard(store: Store, name: str) -> dict:
    return next(row for row in store.rows("conform_dim") if row["标准名称"] == name)


class TestKeyParsing:
    def test_parse_key_fields_strips_type(self) -> None:
        assert parse_key_fields("product_id VARCHAR(32)") == ["product_id"]
        assert parse_key_fields("supplier_id INT") == ["supplier_id"]
        assert parse_key_fields("store_id, product_id") == ["store_id", "product_id"]
        assert parse_key_fields("") == []


class TestConformKeyComparison:
    def test_same_key_different_display_name_is_not_a_conflict(self, store: Store) -> None:
        """产品维度 product_id 对标准 product_id，展示名不同不算键冲突。"""
        dim = _dim(store, "DIM_PRODUCT")
        standard = _standard(store, "标准产品维度")
        assert dim["维度名称"] != standard["标准键值"]

        comparison = compare_dim_to_standard(dim, standard)
        assert comparison["matched"] is True
        assert comparison["same_key_different_name"] is True
        assert comparison["display_name_used_for_key_match"] is False
        assert comparison["actual_key_fields"] == ["product_id"]
        assert comparison["standard_key_fields"] == ["product_id"]

        result = conform_dim_constrain("标准产品维度", ["DIM_PRODUCT"], store=store)
        assert result["violations"] == 0
        assert result["passed_dims"] == ["DIM_PRODUCT"]
        assert result["pending"] == 0

    def test_same_name_different_key_reports_the_real_keys(self, store: Store) -> None:
        """同名异键：名称与标准一致，但主键字段不同必须报错并给出两侧字段。"""
        dim = _dim(store, "DIM_STORE_LEGACY")
        standard = _standard(store, "标准门店维度")
        assert dim["维度名称"] == standard["标准名称"]

        comparison = compare_dim_to_standard(dim, standard)
        assert comparison["matched"] is False
        assert comparison["same_name_different_key"] is True
        assert comparison["same_name"] is True
        assert comparison["actual_key_fields"] == ["store_sk"]
        assert comparison["standard_key_fields"] == ["store_id"]

        # 名称相同不能证明键兼容：显式校验该维度时必须报出键差异。
        legacy_result = conform_dim_constrain("标准门店维度", ["DIM_STORE_LEGACY"], store=store)
        assert legacy_result["violations"] == 1
        violation = legacy_result["violation_details"][0]
        assert violation["actual_key_fields"] == ["store_sk"]
        assert violation["expected_key_fields"] == ["store_id"]
        assert any(not key["match"] for key in violation["compared_keys"])

    def test_standard_version_is_selected_and_reported(self, store: Store) -> None:
        dim = _dim(store, "DIM_TIME")
        standard = standard_for_dim(dim, store.rows("conform_dim"))
        assert standard["版本号"] == "v2.1"
        # 历史误判记录的是 v2.1，新结论同样按 v2.1 对照，结论可比较。
        result = conform_dim_constrain("标准日期维度", ["DIM_TIME"], store=store, standard_version="v2.1")
        assert result["standard_version"] == "v2.1"
        assert result["comparisons"][0]["standard_version"] == "v2.1"

    def test_unknown_standard_version_is_rejected(self, store: Store) -> None:
        result = conform_dim_constrain("标准日期维度", ["DIM_TIME"], store=store, standard_version="v9.9")
        assert "error" in result

    def test_missing_definition_is_pending_not_passed(self, store: Store) -> None:
        result = conform_dim_constrain("标准产品维度", ["DIM_NOT_EXIST"], store=store)
        assert result["passed"] == 0
        assert result["pending"] == 1
        assert result["pending_details"][0]["dim_key"] == "DIM_NOT_EXIST"
        assert result["violations"] == 0


class TestHistoricalVerdictCorrection:
    def test_legacy_false_positive_is_corrected_and_traceable(self, store: Store) -> None:
        corrections = reconcile_all(store)["conform_dim_corrections"]
        product = next(item for item in corrections if item["scope"].endswith("标准产品维度"))
        assert product["prior_verdict"] == "未通过"
        assert product["verdict"] == "通过"
        assert product["false_positive"] is True
        assert product["compared_keys"] == [
            {"position": 1, "actual": "product_id", "standard": "product_id", "match": True}
        ]

        history = store.verdicts.history("conform_dim|DIM_PRODUCT|标准产品维度")
        assert len(history) == 2
        prior, current = history
        assert prior["verdict_id"] == "VD-0001"
        assert prior["effective"] is False
        assert prior["superseded_by"] == current["verdict_id"]
        assert prior["used_for_delivery"] is True
        assert prior["delivery_ref"] == "部署流水线-DIM_PRODUCT生产发布"
        assert current["effective"] is True
        assert current["correction_of"] == "VD-0001"
        assert current["prior_verdict"]["verdict"] == "未通过"
        assert "更正判定" in current["correction_note"]

    def test_only_one_effective_verdict_per_scope(self, store: Store) -> None:
        reconcile_all(store)
        service_for("conform_dim").domain_snapshot()
        service_for("model_validate").domain_snapshot()

        effective_by_scope: dict[str, list[str]] = {}
        for record in store.verdicts.all_verdicts():
            if record["effective"]:
                effective_by_scope.setdefault(record["scope"], []).append(record["verdict_id"])
        assert all(len(ids) == 1 for ids in effective_by_scope.values()), effective_by_scope

    def test_repeated_reconciliation_does_not_multiply_verdicts(self, store: Store) -> None:
        def full_pass() -> int:
            reconcile_all(store)
            service_for("conform_dim").domain_snapshot()
            service_for("model_validate").domain_snapshot()
            return len(store.verdicts.all_verdicts())

        before = full_pass()
        assert full_pass() == before


class TestModelKeyValidation:
    def test_model_compares_same_actual_key_fields_as_conform_list(self, store: Store) -> None:
        """模型校验入口与一致性标准列表核对的是同一份实际键字段。"""
        comparison = compare_model_keys("dwd_sales_detail", store)
        assert comparison["actual_key_fields"] == ["product_id", "store_id", "date_key"]
        assert comparison["standard_key_fields"] == ["product_id", "store_id", "date_key"]
        assert comparison["verdict"] == "通过"

        conform_view = service_for("conform_dim").consistency_view()
        product_row = next(item for item in conform_view["key_comparisons"] if item["dim_code"] == "DIM_PRODUCT")
        assert product_row["actual_key_fields"] == ["product_id"]
        assert "product_id" in comparison["actual_key_fields"]

    def test_same_name_different_key_model_is_not_passed(self, store: Store) -> None:
        rule = next(row for row in store.rows("model_validate") if row["目标模型"] == "dim_store_legacy")
        entry, message = service_for("model_validate").run_action(rule["id"], "执行校验")
        assert entry is not None
        assert entry["status"] == "未通过"
        assert entry["verification_state"] == "未通过"
        assert "store_sk" in entry["差异说明"]
        assert "store_id" in entry["差异说明"]
        assert "credential_id" not in {key for key, value in entry.items() if value}
        assert store.verdicts.credentials("dim_store_legacy") == []

    def test_unlocatable_primary_key_is_pending_and_gets_no_credential(self, store: Store) -> None:
        rule = next(row for row in store.rows("model_validate") if row["目标模型"] == "dim_legacy_x")
        entry, _ = service_for("model_validate").run_action(rule["id"], "执行校验")
        assert entry is not None
        assert entry["status"] == "待校验"
        assert entry["verification_state"] == "待验证"
        assert entry["pending"] is True
        assert store.verdicts.credentials("dim_legacy_x") == []

        audit = model_audit(store, "dim_legacy_x")["models"][0]
        assert audit["current_verdict"] == "待验证"
        assert audit["actual_key_fields"] == []

    def test_partition_results_follow_key_verdict(self, store: Store) -> None:
        rule = next(row for row in store.rows("model_validate") if row["目标模型"] == "dwd_sales_detail")
        entry, _ = service_for("model_validate").run_action(rule["id"], "执行校验")
        assert entry["partition_results"]
        assert all(item["passed"] for item in entry["partition_results"])
        assert entry["credential_id"]

        # 分区结论必须跟随键比对：把标准键改成与维度不一致后，同一分区不能仍显示通过。
        standard = next(row for row in store.rows("conform_dim") if row["标准名称"] == "标准产品维度")
        standard["标准键值"] = "product_sku"
        failing, _ = service_for("model_validate").run_action(rule["id"], "执行校验")
        assert failing["status"] == "未通过"
        assert failing["partition_results"]
        assert all(not item["passed"] for item in failing["partition_results"])
        assert any(
            key["actual"] == "product_id" and key["standard"] == "product_sku"
            for item in failing["partition_results"]
            for key in item["mismatched_keys"]
        )
        assert not failing["credential_id"]
        assert all(
            item["status"] != "有效" or item["verdict_id"] == failing["verdict_id"]
            for item in store.verdicts.credentials("dwd_sales_detail")
        )

    def test_repeated_execution_reuses_current_verdict(self, store: Store) -> None:
        rule = next(row for row in store.rows("model_validate") if row["目标模型"] == "dwd_sales_detail")
        first, _ = service_for("model_validate").run_action(rule["id"], "执行校验")
        second, _ = service_for("model_validate").run_action(rule["id"], "执行校验")
        assert first["verdict_id"] == second["verdict_id"]
        assert first["credential_id"] == second["credential_id"]
        history = store.verdicts.history("model_validate|dwd_sales_detail|维度键完整性")
        assert len(history) == 2  # 历史误判 + 当前有效判定
        assert sum(1 for record in history if record["effective"]) == 1

    def test_legacy_credential_is_revoked_and_replaced(self, store: Store) -> None:
        reconcile_all(store)
        credentials = store.verdicts.credentials("dwd_sales_detail")
        legacy = next(item for item in credentials if item["credential_id"] == "CRED-0001")
        assert legacy["status"] == "已撤销"
        assert legacy["revoked_reason"]
        assert legacy["used_for_delivery"] is True
        active = [item for item in credentials if item["status"] == "有效"]
        assert len(active) == 1
        assert active[0]["credential_id"] != "CRED-0001"


class TestAuditEndpoints:
    def test_conform_audit_returns_verdict_history(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.routers.resource as resource

        monkeypatch.setattr(resource, "data_store", store)
        payload = resource.conform_dim_audit()
        assert payload["ok"] is True
        product = next(
            item for item in payload["view"]["key_comparisons"] if item["dim_code"] == "DIM_PRODUCT"
        )
        assert product["verdict"] == "通过"
        assert product["prior_verdict"]["verdict_id"] == "VD-0001"
        verdict_ids = [record["verdict_id"] for record in payload["verdicts"]]
        assert "VD-0001" in verdict_ids

    def test_model_audit_lists_mismatched_keys(self) -> None:
        from app.store import Store

        local = Store()
        audit = model_audit(local, "dim_store_legacy")["models"][0]
        assert audit["current_verdict"] == "未通过"
        assert audit["mismatched_keys"] == [
            {"dim_code": "DIM_STORE_LEGACY", "actual": "store_sk", "standard": "store_id", "position": 1}
        ]
