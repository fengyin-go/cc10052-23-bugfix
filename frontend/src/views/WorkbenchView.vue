<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="meta.key === 'exposure_log'" class="receipt-panel" data-testid="receipt-panel">
      <form class="receipt-query" @submit.prevent="loadReceipt()">
        <span class="receipt-title">批次确认回执</span>
        <input
          v-model="receiptBatch"
          data-testid="receipt-batch"
          placeholder="输入日志批次复查更正回执"
        />
        <button class="btn" data-testid="receipt-query" type="submit">查询回执</button>
      </form>
      <p v-if="receiptError" class="error-text">{{ receiptError }}</p>
      <div v-if="receiptData" class="receipt-body">
        <p v-if="receiptData.最新回执" class="receipt-summary">
          第 {{ receiptData.最新回执.轮次 }} 轮确认：确认数 {{ receiptData.最新回执.确认数 }}，
          触发来源 {{ receiptData.最新回执.触发来源 }}，
          受影响窗口
          <template v-if="receiptData.最新回执.受影响窗口.length">
            <span v-for="window in receiptData.最新回执.受影响窗口" :key="window" class="window-chip">
              {{ window }}
            </span>
          </template>
          <span v-else>无</span>
        </p>
        <p v-else class="receipt-summary">该批次尚未产生确认回执，请先执行去重。</p>
        <div class="receipt-columns">
          <div>
            <h4>实际保留（{{ receiptData.当前保留.length }}）</h4>
            <table class="data-table compact">
              <thead>
                <tr><th>id</th><th>用户标识</th><th>曝光时间</th><th>计入窗口</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in receiptData.当前保留" :key="`keep-${row.id}`" data-testid="receipt-kept">
                  <td>{{ row.id }}</td>
                  <td>{{ row.用户标识 }}</td>
                  <td>{{ row.曝光时间 }}</td>
                  <td>{{ row.计入窗口 || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div>
            <h4>重复项（{{ receiptData.当前重复.length }}）</h4>
            <table class="data-table compact">
              <thead>
                <tr><th>id</th><th>用户标识</th><th>曝光时间</th><th>丢弃原因</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in receiptData.当前重复" :key="`drop-${row.id}`" data-testid="receipt-dropped">
                  <td>{{ row.id }}</td>
                  <td>{{ row.用户标识 }}</td>
                  <td>{{ row.曝光时间 }}</td>
                  <td>{{ row.丢弃原因 || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <p v-if="receiptData.轮次历史.length > 1" class="receipt-history">
          轮次历史：
          <span v-for="round in receiptData.轮次历史" :key="round.轮次" class="window-chip">
            第{{ round.轮次 }}轮 · 确认数 {{ round.确认数 }} · {{ round.触发来源 }}
          </span>
        </p>
      </div>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">
              详情
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in editableFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="detailRow" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal">
        <h3>{{ meta.entity }}详情 #{{ detailRow.id }}</h3>
        <dl class="detail-list">
          <template v-for="[key, value] in detailEntries" :key="key">
            <dt>{{ key }}</dt>
            <dd>
              <pre v-if="isStructured(value)" class="detail-json">{{ formatValue(value) }}</pre>
              <span v-else>{{ formatValue(value) }}</span>
            </dd>
          </template>
        </dl>
        <section v-if="jobBasis" class="basis-panel" data-testid="job-basis">
          <h4>计算依据与分母对账</h4>
          <p>
            分母状态 {{ jobBasis.分母状态 }} · 触发来源 {{ jobBasis.触发来源 || '—' }} ·
            确认轮次 {{ jobBasis.确认轮次 }} · 分母数 {{ jobBasis.分母数 }}
          </p>
          <table v-if="jobBasis.分母对账.length" class="data-table compact">
            <thead>
              <tr><th>日志批次</th><th>作业分母</th><th>回执确认数</th><th>回执轮次</th><th>是否一致</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in jobBasis.分母对账" :key="item.日志批次" data-testid="basis-recon">
                <td>{{ item.日志批次 }}</td>
                <td>{{ item.作业分母 }}</td>
                <td>{{ item.回执确认数 }}</td>
                <td>{{ item.回执轮次 }}</td>
                <td>{{ item.是否一致 === true ? '一致' : item.是否一致 === false ? '不一致' : item.是否一致 }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="jobBasis.修订理由" class="revision-reason">修订理由：{{ jobBasis.修订理由 }}</p>
        </section>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-detail" @click="closeDetail">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  getJobBasis,
  listBatchReceipts,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { BatchReceiptResult, EntryRow, JobBasisResult } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const editableFields = meta.fields.filter((field) => !(meta.computedFields ?? []).includes(field))
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const detailRow = ref<EntryRow | null>(null)
const jobBasis = ref<JobBasisResult | null>(null)
const receiptBatch = ref('')
const receiptData = ref<BatchReceiptResult | null>(null)
const receiptError = ref('')
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)
const detailEntries = computed(() => (detailRow.value ? Object.entries(detailRow.value) : []))

function isStructured(value: unknown): boolean {
  return typeof value === 'object' && value !== null
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(editableFields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    if (meta.key === 'exposure_log' && action === '执行去重') {
      receiptBatch.value = String(row['日志批次'] ?? '')
      await loadReceipt()
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function openDetail(row: EntryRow) {
  detailRow.value = row
  jobBasis.value = null
  if (meta.key === 'metric_compute') {
    try {
      jobBasis.value = await getJobBasis(Number(row.id))
    } catch {
      jobBasis.value = null
    }
  }
}

function closeDetail() {
  detailRow.value = null
  jobBasis.value = null
}

async function loadReceipt(batchId?: string) {
  const target = (batchId ?? receiptBatch.value).trim()
  receiptError.value = ''
  if (!target) {
    receiptData.value = null
    return
  }
  try {
    receiptData.value = await listBatchReceipts(target)
  } catch (error) {
    receiptData.value = null
    receiptError.value = error instanceof Error ? error.message : '批次确认回执读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
