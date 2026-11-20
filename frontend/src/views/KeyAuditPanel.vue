<template>
  <section class="audit-panel" :data-testid="`audit-${mode}`">
    <header class="audit-head">
      <div>
        <h3>{{ mode === 'conform_dim' ? '实际键字段比对' : '模型校验键证据' }}</h3>
        <p class="audit-hint">
          按实际主键字段与标准键字段（含标准版本）比对，展示名称不参与判断；
          同范围只保留一项当前有效判定，历史误判与更正留痕可核对。
        </p>
      </div>
      <button class="btn" type="button" data-testid="audit-refresh" @click="reload">刷新比对</button>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="mode === 'conform_dim'">
      <table class="data-table">
        <thead>
          <tr>
            <th>维度</th>
            <th>实际主键字段</th>
            <th>标准键字段</th>
            <th>标准版本</th>
            <th>逐键比对</th>
            <th>当前判定</th>
            <th>误判 / 更正</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in conformComparisons"
            :key="item.dim_code"
            :data-testid="`conform-compare-${item.dim_code}`"
            :data-verdict="item.verdict"
            :data-keys="item.actual_key_fields.join('.')"
          >
            <td>{{ item.dim_code }} / {{ item.dim_name }}</td>
            <td>{{ item.actual_key_fields.join('.') || '未定位' }}</td>
            <td>{{ item.standard_key_fields.join('.') || '未定位' }}</td>
            <td>{{ item.standard_name }} {{ item.standard_version }}</td>
            <td>
              <span v-for="key in item.compared_keys" :key="key.position" class="key-chip" :class="key.match ? 'ok' : 'bad'">
                {{ key.actual || '—' }}{{ key.match ? '=' : '≠' }}{{ key.standard || '—' }}
              </span>
            </td>
            <td>
              <span class="verdict" :class="verdictClass(item.verdict)">{{ item.verdict }}</span>
              <small v-if="item.same_key_different_name">同键异名</small>
              <small v-if="item.same_name_different_key">同名异键</small>
            </td>
            <td>
              <span v-if="item.prior_verdict" class="correction" :data-testid="`conform-correction-${item.dim_code}`">
                旧判定 {{ item.prior_verdict.verdict }}（{{ item.prior_verdict.verdict_id }}）
                → {{ item.verdict }}（{{ item.verdict_id }}）
                <em v-if="item.used_for_delivery">曾用于 {{ item.delivery_ref }}</em>
              </span>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <details class="audit-history" data-testid="conform-verdict-history">
        <summary>判定沿革（{{ conformHistory.length }} 条）</summary>
        <table class="data-table">
          <thead>
            <tr><th>判定编号</th><th>范围</th><th>结论</th><th>有效</th><th>被谁更正</th><th>来源规则</th><th>交付依据</th></tr>
          </thead>
          <tbody>
            <tr v-for="record in conformHistory" :key="record.verdict_id" :data-verdict-id="record.verdict_id">
              <td>{{ record.verdict_id }}</td>
              <td>{{ record.scope }}</td>
              <td>{{ record.verdict }}</td>
              <td>{{ record.effective ? '当前有效' : '已更正' }}</td>
              <td>{{ record.superseded_by ?? '—' }}</td>
              <td>{{ (record.basis as Record<string, unknown>).rule }}</td>
              <td>{{ record.used_for_delivery ? record.delivery_ref : '—' }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </template>

    <template v-else>
      <table class="data-table">
        <thead>
          <tr>
            <th>目标模型</th>
            <th>实际主键字段 / 标准键字段</th>
            <th>逐键比对</th>
            <th>当前比对结论</th>
            <th>分区登记</th>
            <th>模型凭证</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="model in modelAudits"
            :key="model.target_model"
            :data-testid="`model-compare-${model.target_model}`"
            :data-verdict="model.current_verdict"
            :data-keys="model.actual_key_fields.join('.')"
          >
            <td>{{ model.target_model }}</td>
            <td>
              <div>{{ model.actual_key_fields.join('.') || '未定位' }}</div>
              <small class="muted">标准：{{ model.standard_key_fields.join('.') || '未定位' }}</small>
            </td>
            <td>
              <span v-for="(label, index) in model.compared_key_labels" :key="index" class="key-chip" :class="label.split('↔')[0] === label.split('↔')[1] ? 'ok' : 'bad'">
                {{ label }}
              </span>
              <ul v-if="model.mismatched_keys.length" class="mismatch-list" data-testid="model-mismatch-keys">
                <li v-for="(key, index) in model.mismatched_keys" :key="index">
                  {{ key.dim_code }}：<code>{{ key.actual }}</code> ≠ <code>{{ key.standard }}</code>
                </li>
              </ul>
            </td>
            <td>
              <span class="verdict" :class="verdictClass(model.current_verdict)">{{ model.current_verdict }}</span>
            </td>
            <td>
              <div v-for="partition in model.partitions" :key="partition.partition">
                {{ partition.partition }}：{{ partition.passed ? '通过' : partition.verdict }}
              </div>
            </td>
            <td>
              <div v-for="credential in model.credentials" :key="credential.credential_id" :data-credential-id="credential.credential_id">
                {{ credential.credential_id }} {{ credential.status }}
                <em v-if="credential.used_for_delivery">（{{ credential.delivery_ref }}）</em>
              </div>
              <span v-if="!model.credentials.length" class="muted">未签发</span>
            </td>
          </tr>
        </tbody>
      </table>

      <details class="audit-history" data-testid="model-verdict-history">
        <summary>判定与更正记录（{{ modelHistory.length }} 条）</summary>
        <table class="data-table">
          <thead>
            <tr><th>判定编号</th><th>范围</th><th>结论</th><th>有效</th><th>更正自</th><th>被谁更正</th><th>交付依据</th></tr>
          </thead>
          <tbody>
            <tr v-for="record in modelHistory" :key="record.verdict_id" :data-verdict-id="record.verdict_id">
              <td>{{ record.verdict_id }}</td>
              <td>{{ record.scope }}</td>
              <td>{{ record.verdict }}</td>
              <td>{{ record.effective ? '当前有效' : '已更正' }}</td>
              <td>{{ record.correction_of ?? '—' }}</td>
              <td>{{ record.superseded_by ?? '—' }}</td>
              <td>{{ record.used_for_delivery ? record.delivery_ref : '—' }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  loadConformAudit,
  loadModelAudit,
  type Credential,
  type KeyComparison,
  type ModelKeyAudit,
  type VerdictRecord,
} from '@/api/backend-service'

const props = defineProps<{ mode: string }>()

const errorMessage = ref('')
const conformComparisons = ref<KeyComparison[]>([])
const conformHistory = ref<VerdictRecord[]>([])
const modelAudits = ref<ModelKeyAudit[]>([])
const modelHistory = ref<VerdictRecord[]>([])
const credentials = ref<Credential[]>([])

const mode = computed(() => props.mode)

function verdictClass(verdict: string) {
  if (verdict === '通过') return 'verdict-pass'
  if (verdict === '未通过') return 'verdict-fail'
  return 'verdict-pending'
}

async function reload() {
  errorMessage.value = ''
  try {
    if (props.mode === 'conform_dim') {
      const view = await loadConformAudit()
      conformComparisons.value = view.key_comparisons
      conformHistory.value = view.verdict_history
    } else {
      const payload = await loadModelAudit()
      modelAudits.value = payload.models
      credentials.value = payload.credentials
      const seen = new Map<string, VerdictRecord>()
      for (const model of payload.models) {
        for (const record of model.history) seen.set(record.verdict_id, record)
      }
      modelHistory.value = [...seen.values()].sort((a, b) => a.verdict_id.localeCompare(b.verdict_id))
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '比对结果读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.audit-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.audit-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 8px; }
.audit-head h3 { margin: 0 0 4px; font-size: 15px; }
.audit-hint { margin: 0; color: var(--muted); font-size: 12px; }
.audit-history { margin-top: 12px; font-size: 12px; }
.audit-history summary { cursor: pointer; color: var(--brand); margin-bottom: 6px; }
.key-chip { display: inline-block; margin: 0 4px 4px 0; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.key-chip.ok { background: #e8f7ee; color: #157347; }
.key-chip.bad { background: #fdecec; color: #b42318; }
.verdict { font-weight: 600; }
.verdict-pass { color: #157347; }
.verdict-fail { color: #b42318; }
.verdict-pending { color: #b26a00; }
.correction { display: block; font-size: 12px; color: #b26a00; }
.correction em { color: var(--muted); font-style: normal; }
.muted { color: var(--muted); }
.mismatch-list { margin: 4px 0 0; padding-left: 16px; font-size: 12px; }
</style>
