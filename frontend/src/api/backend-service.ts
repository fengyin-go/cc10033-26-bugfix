import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type KeyComparison = {
  dim_code: string
  dim_name: string
  standard_name: string
  standard_version: string
  actual_key_fields: string[]
  standard_key_fields: string[]
  compared_keys: { position: number; actual: string; standard: string; match: boolean }[]
  same_key_different_name: boolean
  same_name_different_key: boolean
  verdict: string
  verdict_id: string
  correction_of: string | null
  prior_verdict: Record<string, unknown> | null
  correction_note: string
  used_for_delivery: boolean
  delivery_ref: string
}

export type VerdictRecord = {
  verdict_id: string
  scope: string
  subject: string
  verdict: string
  effective: boolean
  supersedes: string | null
  superseded_by: string | null
  correction_of: string | null
  correction_note: string
  legacy_rule: boolean
  used_for_delivery: boolean
  delivery_ref: string
  basis: Record<string, unknown>
  prior_verdict: Record<string, unknown> | null
  created_at: string
}

export type Credential = {
  credential_id: string
  target_model: string
  scope: string
  rule_name: string
  partition: string
  verdict_id: string
  status: string
  legacy_rule: boolean
  used_for_delivery: boolean
  delivery_ref: string
  revoked_by: string | null
  revoked_reason: string
}

export type ModelKeyAudit = {
  target_model: string
  actual_key_fields: string[]
  standard_key_fields: string[]
  compared_key_labels: string[]
  compared_keys: { dim_code: string; position: number; actual: string; standard: string; match: boolean }[]
  unresolved: { dim_code: string; dim_name: string; reason: string; actual_key_fields: string[]; standard_key_fields: string[] }[]
  mismatched_keys: { dim_code: string; actual: string; standard: string; position: number }[]
  partitions: { partition: string; passed: boolean; verdict: string; mismatched_keys: unknown[]; unresolved: unknown[] }[]
  current_verdict: string
  verdict: VerdictRecord | null
  history: VerdictRecord[]
  credentials: Credential[]
  rules: { entry_id: number; rule_name: string; validation_type: string; status: string }[]
}

export async function loadConformAudit(): Promise<{ key_comparisons: KeyComparison[]; standards: Record<string, unknown>[]; verdict_history: VerdictRecord[] }> {
  const response = await request('/api/conform_dim/audit')
  if (!response.ok) throw new Error('一致性维度键比对读取失败')
  return (await response.json()).view
}

export async function loadModelAudit(targetModel?: string): Promise<{ models: ModelKeyAudit[]; credentials: Credential[] }> {
  const query = targetModel ? `?target_model=${encodeURIComponent(targetModel)}` : ''
  const response = await request(`/api/model_validate/audit${query}`)
  if (!response.ok) throw new Error('模型校验键比对读取失败')
  const payload = await response.json()
  return { models: payload.audit.models, credentials: payload.credentials }
}
