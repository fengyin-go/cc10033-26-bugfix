"""内存数据仓库：跨模块数据通道、失败恢复、快照引擎和产物注册。

各业务模块产生的数据产物通过 DataChannel 发布，下游模块消费；
FailureRecovery 管理断点恢复；SnapshotEngine 提供版本快照能力。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.seed import SEED_CREDENTIALS, SEED_ROWS, SEED_VERDICTS


class DataChannel:
    """跨模块数据通道：上游模块发布数据产物，下游模块通过 topic 消费。"""

    def __init__(self) -> None:
        self._channels: dict[str, list[dict[str, Any]]] = {}
        self._consumers: dict[str, list[str]] = {}

    def publish(self, topic: str, payload: dict[str, Any]) -> str:
        """将数据产物发布到指定 topic，返回消息ID。"""
        msg_id = str(uuid.uuid4())[:12]
        entry = {
            "msg_id": msg_id,
            "topic": topic,
            "payload": payload,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "consumed_by": [],
            "expired": False,
        }
        self._channels.setdefault(topic, []).append(entry)
        return msg_id

    def consume(self, topic: str, consumer: str) -> list[dict[str, Any]]:
        """消费指定 topic 上未被当前消费者消费过的消息，标记已消费。"""
        channel = self._channels.get(topic, [])
        results: list[dict[str, Any]] = []
        for entry in channel:
            if consumer not in entry["consumed_by"] and not entry["expired"]:
                entry["consumed_by"].append(consumer)
                results.append(entry)
        return results

    def consume_latest(self, topic: str, consumer: str) -> dict[str, Any] | None:
        """消费指定 topic 上最新一条未被消费的消息。"""
        channel = self._channels.get(topic, [])
        for entry in reversed(channel):
            if consumer not in entry["consumed_by"] and not entry["expired"]:
                entry["consumed_by"].append(consumer)
                return entry
        return None

    def topic_snapshot(self, topic: str) -> list[dict[str, Any]]:
        """获取 topic 上所有消息的快照。"""
        return list(self._channels.get(topic, []))

    def expire_topic(self, topic: str) -> int:
        """过期指定 topic 上所有未消费消息，返回过期数量。"""
        count = 0
        for entry in self._channels.get(topic, []):
            if not entry["expired"]:
                entry["expired"] = True
                count += 1
        return count

    def list_topics(self) -> list[str]:
        return sorted(self._channels)

    def topic_stats(self) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = {}
        for topic, entries in self._channels.items():
            consumed = sum(1 for e in entries if len(e["consumed_by"]) > 0)
            pending = len(entries) - consumed
            expired = sum(1 for e in entries if e["expired"])
            stats[topic] = {"total": len(entries), "consumed": consumed, "pending": pending, "expired": expired}
        return stats


class FailureRecovery:
    """失败恢复管理器：检查点管理、重试计数和失败链路追踪。"""

    def __init__(self) -> None:
        self._checkpoints: dict[str, dict[str, Any]] = {}
        self._retry_logs: dict[str, list[dict[str, Any]]] = {}
        self._failure_chains: dict[str, list[dict[str, Any]]] = {}

    def create_checkpoint(self, module: str, task_id: str, state: dict[str, Any]) -> str:
        """创建处理检查点，返回检查点ID。"""
        cp_id = f"{module}-{task_id}-{uuid.uuid4().hex[:8]}"
        self._checkpoints[cp_id] = {
            "cp_id": cp_id,
            "module": module,
            "task_id": task_id,
            "state": dict(state),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "restored_at": None,
            "restore_count": 0,
            "completed": False,
        }
        return cp_id

    def get_checkpoint(self, cp_id: str) -> dict[str, Any] | None:
        return self._checkpoints.get(cp_id)

    def update_checkpoint(self, cp_id: str, state: dict[str, Any]) -> bool:
        cp = self._checkpoints.get(cp_id)
        if cp is None:
            return False
        cp["state"].update(state)
        return True

    def mark_completed(self, cp_id: str) -> bool:
        cp = self._checkpoints.get(cp_id)
        if cp is None:
            return False
        cp["completed"] = True
        return True

    def restore_checkpoint(self, cp_id: str) -> dict[str, Any] | None:
        cp = self._checkpoints.get(cp_id)
        if cp is None:
            return None
        cp["restored_at"] = datetime.now(timezone.utc).isoformat()
        cp["restore_count"] += 1
        return dict(cp)

    def log_retry(self, module: str, task_id: str, attempt: int, reason: str, success: bool) -> None:
        self._retry_logs.setdefault(module, [])
        self._retry_logs[module].append({
            "task_id": task_id,
            "attempt": attempt,
            "reason": reason,
            "success": success,
            "logged_at": datetime.now(timezone.utc).isoformat(),
        })

    def retry_history(self, module: str) -> list[dict[str, Any]]:
        return self._retry_logs.get(module, [])

    def log_failure_chain(self, chain_id: str, step: str, module: str, error: str, upstream: str | None = None) -> None:
        self._failure_chains.setdefault(chain_id, [])
        self._failure_chains[chain_id].append({
            "step": step,
            "module": module,
            "error": error,
            "upstream": upstream,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def chain_summary(self, chain_id: str) -> list[dict[str, Any]]:
        return self._failure_chains.get(chain_id, [])

    def recovery_summary(self) -> dict[str, Any]:
        completed = sum(1 for cp in self._checkpoints.values() if cp["completed"])
        pending = len(self._checkpoints) - completed
        total_retries = sum(len(logs) for logs in self._retry_logs.values())
        total_chains = len(self._failure_chains)
        return {
            "checkpoints": {"total": len(self._checkpoints), "completed": completed, "pending": pending},
            "retries": total_retries,
            "failure_chains": total_chains,
        }


class SnapshotEngine:
    """版本快照引擎：创建、比较和管理数据快照。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._diff_logs: dict[str, dict[str, Any]] = {}

    def create_snapshot(self, module: str, entry_id: int, data: dict[str, Any], version_label: str = "") -> str:
        """创建数据快照，返回快照ID。"""
        data_str = str(sorted(data.items()))
        content_hash = hashlib.sha256(data_str.encode()).hexdigest()[:16]
        snap_id = f"{module}-{entry_id}-v{version_label}-{uuid.uuid4().hex[:8]}"
        self._snapshots[snap_id] = {
            "snap_id": snap_id,
            "module": module,
            "entry_id": entry_id,
            "version_label": version_label,
            "data": dict(data),
            "content_hash": content_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "parent_snap_id": None,
            "retention_policy": "keep_forever",
        }
        return snap_id

    def create_version_snapshot(self, module: str, entry_id: int, data: dict[str, Any],
                                version_label: str, parent_snap_id: str | None = None) -> str:
        """创建带父子关系的版本快照，用于模型发布版本管理。"""
        data_str = str(sorted(data.items()))
        content_hash = hashlib.sha256(data_str.encode()).hexdigest()[:16]
        snap_id = f"{module}-{entry_id}-{version_label}-{uuid.uuid4().hex[:8]}"
        self._snapshots[snap_id] = {
            "snap_id": snap_id,
            "module": module,
            "entry_id": entry_id,
            "version_label": version_label,
            "data": dict(data),
            "content_hash": content_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "parent_snap_id": parent_snap_id,
            "retention_policy": "keep_forever",
        }
        return snap_id

    def get_snapshot(self, snap_id: str) -> dict[str, Any] | None:
        return self._snapshots.get(snap_id)

    def list_snapshots(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        return [
            s for s in self._snapshots.values()
            if s["module"] == module and s["entry_id"] == entry_id
        ]

    def diff_snapshots(self, snap_id_a: str, snap_id_b: str) -> dict[str, Any] | None:
        snap_a = self._snapshots.get(snap_id_a)
        snap_b = self._snapshots.get(snap_id_b)
        if not snap_a or not snap_b:
            return None
        keys_a = set(snap_a["data"])
        keys_b = set(snap_b["data"])
        added = {k: snap_b["data"][k] for k in keys_b - keys_a}
        removed = {k: snap_a["data"][k] for k in keys_a - keys_b}
        modified = {}
        for k in keys_a & keys_b:
            if snap_a["data"][k] != snap_b["data"][k]:
                modified[k] = {"old": snap_a["data"][k], "new": snap_b["data"][k]}
        diff_id = f"diff-{uuid.uuid4().hex[:8]}"
        result = {
            "diff_id": diff_id,
            "snap_a": snap_id_a,
            "snap_b": snap_id_b,
            "added_fields": added,
            "removed_fields": removed,
            "modified_fields": modified,
            "total_changes": len(added) + len(removed) + len(modified),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._diff_logs[diff_id] = result
        return result

    def rollback_to_snapshot(self, module: str, entry_id: int, snap_id: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """将数据回滚到指定快照版本。"""
        snap = self._snapshots.get(snap_id)
        if not snap:
            return None
        for row in rows:
            if row.get("id") == entry_id:
                for k, v in snap["data"].items():
                    row[k] = v
                row["_rollback_to"] = snap_id
                row["_rollback_at"] = datetime.now(timezone.utc).isoformat()
                return row
        return None

    def snapshot_summary(self) -> dict[str, Any]:
        by_module: dict[str, int] = {}
        for s in self._snapshots.values():
            by_module[s["module"]] = by_module.get(s["module"], 0) + 1
        return {"total_snapshots": len(self._snapshots), "by_module": by_module, "total_diffs": len(self._diff_logs)}


class ArtifactRegistry:
    """数据产物注册表：追踪各模块产出的数据工件。"""

    def __init__(self) -> None:
        self._artifacts: dict[str, dict[str, Any]] = {}

    def register(self, module: str, artifact_type: str, artifact_id: str,
                 metadata: dict[str, Any], source_modules: list[str] | None = None) -> str:
        """注册数据产物，返回注册ID。"""
        reg_id = f"{module}-{artifact_type}-{artifact_id}"
        self._artifacts[reg_id] = {
            "reg_id": reg_id,
            "module": module,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "metadata": dict(metadata),
            "source_modules": source_modules or [module],
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "consumed_by": [],
        }
        return reg_id

    def get_artifact(self, reg_id: str) -> dict[str, Any] | None:
        return self._artifacts.get(reg_id)

    def list_by_module(self, module: str) -> list[dict[str, Any]]:
        return [a for a in self._artifacts.values() if a["module"] == module]

    def mark_consumed(self, reg_id: str, consumer: str) -> bool:
        a = self._artifacts.get(reg_id)
        if a is None:
            return False
        a["consumed_by"].append(consumer)
        return True

    def list_all(self) -> list[dict[str, Any]]:
        return list(self._artifacts.values())

    def by_type(self, artifact_type: str) -> list[dict[str, Any]]:
        return [a for a in self._artifacts.values() if a["artifact_type"] == artifact_type]

    def artifact_summary(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        by_module: dict[str, int] = {}
        for a in self._artifacts.values():
            by_type[a["artifact_type"]] = by_type.get(a["artifact_type"], 0) + 1
            by_module[a["module"]] = by_module.get(a["module"], 0) + 1
        return {"total": len(self._artifacts), "by_type": by_type, "by_module": by_module}


class VerdictLedger:
    """判定台账：同一范围只保留一项当前有效判定，并保留历史判定与更正留痕。

    每次 record 都追加一条不可变判定；若该范围已有有效判定，旧判定被标记为失效
    （effective=False, superseded_by=新判定），但记录本身、比较依据、是否曾作为
    交付依据以及被哪条新判定更正都能回溯。
    """

    def __init__(self) -> None:
        self._verdicts: list[dict[str, Any]] = []
        self._credentials: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def record(
        self,
        *,
        scope: str,
        subject: str,
        verdict: str,
        basis: dict[str, Any],
        source: str,
        entry_id: int | None = None,
        subject_kind: str = "",
        used_for_delivery: bool | None = None,
        delivery_ref: str | None = None,
        legacy: bool = False,
        correction_note: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        verdict_id = self._next_id("VD")
        prior = self.effective(scope)
        # 未显式声明时沿用前一条判定的交付留痕，保证更正后仍能核对交付依据。
        if used_for_delivery is None:
            used_for_delivery = bool(prior.get("used_for_delivery")) if prior else False
        if delivery_ref is None:
            delivery_ref = str(prior.get("delivery_ref", "")) if prior else ""
        record: dict[str, Any] = {
            "verdict_id": verdict_id,
            "scope": scope,
            "subject": subject,
            "subject_kind": subject_kind,
            "verdict": verdict,
            "effective": True,
            "legacy_rule": legacy,
            "basis": dict(basis),
            "source": source,
            "entry_id": entry_id,
            "used_for_delivery": bool(used_for_delivery),
            "delivery_ref": delivery_ref,
            "created_at": now,
            "supersedes": None,
            "superseded_by": None,
            "superseded_at": None,
            "correction_of": None,
            "prior_verdict": None,
            "correction_note": correction_note,
        }
        if prior is not None:
            record["supersedes"] = prior["verdict_id"]
            record["correction_of"] = prior["verdict_id"]
            record["prior_verdict"] = {
                "verdict_id": prior["verdict_id"],
                "verdict": prior["verdict"],
                "rule": prior.get("basis", {}).get("rule", ""),
                "actual_key_fields": prior.get("basis", {}).get("actual_key_fields", []),
                "standard_key_fields": prior.get("basis", {}).get("standard_key_fields", []),
                "compared_keys": prior.get("basis", {}).get("compared_keys", []),
                "legacy_compared_fields": prior.get("basis", {}).get("legacy_compared_fields", []),
                "used_for_delivery": prior.get("used_for_delivery", False),
                "delivery_ref": prior.get("delivery_ref", ""),
                "created_at": prior.get("created_at"),
            }
            if not record["correction_note"]:
                record["correction_note"] = (
                    f"更正判定 {prior['verdict']} → {verdict}："
                    f"旧规则「{prior.get('basis', {}).get('rule', '')}」，"
                    f"新规则「{basis.get('rule', '')}」"
                )
            prior["effective"] = False
            prior["superseded_by"] = verdict_id
            prior["superseded_at"] = now
            # 旧判定签发的模型凭证随判定失效一并撤销
            self.revoke_for_scope(scope, verdict_id, f"被判定 {verdict_id} 取代")

        # 防御性收口：同一范围不允许存在第二条有效判定
        for existing in self._verdicts:
            if existing["scope"] == scope and existing["effective"]:
                existing["effective"] = False
                existing["superseded_by"] = verdict_id
                existing["superseded_at"] = now

        self._verdicts.append(record)
        return record

    def load_seed(self, record: dict[str, Any]) -> None:
        """载入历史判定（可能曾作为交付依据），保留原始依据和判定编号。"""
        seeded = dict(record)
        seeded.setdefault("effective", True)
        seeded.setdefault("legacy_rule", True)
        seeded.setdefault("used_for_delivery", False)
        seeded.setdefault("delivery_ref", "")
        seeded.setdefault("supersedes", None)
        seeded.setdefault("superseded_by", None)
        seeded.setdefault("superseded_at", None)
        seeded.setdefault("correction_of", None)
        seeded.setdefault("prior_verdict", None)
        seeded.setdefault("correction_note", "")
        seeded.setdefault("basis", {})
        seeded.setdefault("scope", "")
        seeded.setdefault("subject", "")
        seeded.setdefault("subject_kind", "")
        seeded.setdefault("source", "seed")
        seeded.setdefault("entry_id", None)
        self._verdicts.append(seeded)
        match = re.match(r"^[A-Z]+-(\d+)$", str(seeded.get("verdict_id", "")))
        if match:
            self._seq = max(self._seq, int(match.group(1)))

    def load_seed_credential(self, credential: dict[str, Any]) -> None:
        seeded = dict(credential)
        seeded.setdefault("status", "有效")
        seeded.setdefault("partition", "")
        seeded.setdefault("legacy_rule", True)
        seeded.setdefault("used_for_delivery", False)
        seeded.setdefault("delivery_ref", "")
        seeded.setdefault("revoked_by", None)
        seeded.setdefault("revoked_reason", "")
        seeded.setdefault("revoked_at", None)
        seeded.setdefault("entry_id", None)
        self._credentials[seeded["credential_id"]] = seeded
        match = re.match(r"^[A-Z]+-(\d+)$", str(seeded.get("credential_id", "")))
        if match:
            self._seq = max(self._seq, int(match.group(1)))

    def effective(self, scope: str) -> dict[str, Any] | None:
        for record in reversed(self._verdicts):
            if record["scope"] == scope and record["effective"]:
                return record
        return None

    def history(self, scope: str) -> list[dict[str, Any]]:
        return [record for record in self._verdicts if record["scope"] == scope]

    def all_verdicts(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._verdicts]

    def issue_credential(
        self,
        *,
        target_model: str,
        scope: str,
        rule_name: str,
        verdict_record: dict[str, Any],
        partition: str = "",
        entry_id: int | None = None,
        legacy: bool = False,
        used_for_delivery: bool = False,
        delivery_ref: str = "",
    ) -> dict[str, Any] | None:
        """只有「通过」且实际主键全部定位的判定才能签发模型凭证。"""
        if verdict_record.get("verdict") != "通过":
            return None
        if not verdict_record.get("basis", {}).get("all_keys_located", False):
            return None
        now = datetime.now(timezone.utc).isoformat()
        credential_id = self._next_id("CRED")
        for existing in self._credentials.values():
            if (
                existing["target_model"] == target_model
                and existing["scope"] == scope
                and existing["rule_name"] == rule_name
                and existing["status"] == "有效"
            ):
                existing["status"] = "已撤销"
                existing["revoked_by"] = credential_id
                existing["revoked_reason"] = f"被新凭证 {credential_id} 取代"
                existing["revoked_at"] = now
        credential = {
            "credential_id": credential_id,
            "target_model": target_model,
            "scope": scope,
            "rule_name": rule_name,
            "partition": partition,
            "verdict_id": verdict_record["verdict_id"],
            "verdict": verdict_record["verdict"],
            "status": "有效",
            "legacy_rule": legacy,
            "used_for_delivery": bool(used_for_delivery),
            "delivery_ref": delivery_ref,
            "issued_at": now,
            "revoked_by": None,
            "revoked_reason": "",
            "revoked_at": None,
            "entry_id": entry_id,
        }
        self._credentials[credential_id] = credential
        return dict(credential)

    def revoke_for_scope(self, scope: str, new_verdict_id: str, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for credential in self._credentials.values():
            if credential["scope"] == scope and credential["status"] == "有效":
                credential["status"] = "已撤销"
                credential["revoked_by"] = new_verdict_id
                credential["revoked_reason"] = reason
                credential["revoked_at"] = now

    def credentials(self, target_model: str | None = None) -> list[dict[str, Any]]:
        values = list(self._credentials.values())
        if target_model is not None:
            values = [c for c in values if c["target_model"] == target_model]
        return [dict(value) for value in values]

    def effective_credential(self, target_model: str, scope: str, rule_name: str) -> dict[str, Any] | None:
        for credential in reversed(list(self._credentials.values())):
            if (
                credential["target_model"] == target_model
                and credential["scope"] == scope
                and credential["rule_name"] == rule_name
                and credential["status"] == "有效"
            ):
                return dict(credential)
        return None


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self.channel = DataChannel()
        self.recovery = FailureRecovery()
        self.snapshot = SnapshotEngine()
        self.artifacts = ArtifactRegistry()
        self.verdicts = VerdictLedger()
        for verdict in SEED_VERDICTS:
            self.verdicts.load_seed(verdict)
        for credential in SEED_CREDENTIALS:
            self.verdicts.load_seed_credential(credential)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def insert(self, module: str, entry: dict[str, Any]) -> None:
        self.rows(module).append(entry)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        channel_stats = self.channel.topic_stats()
        recovery = self.recovery.recovery_summary()
        snapshot_info = self.snapshot.snapshot_summary()
        artifact_info = self.artifacts.artifact_summary()
        return {
            "cards": cards,
            "modules": modules,
            "data_channels": {"topics": len(channel_stats), "stats": channel_stats},
            "recovery": recovery,
            "snapshots": snapshot_info,
            "artifacts": artifact_info,
        }


store = Store()