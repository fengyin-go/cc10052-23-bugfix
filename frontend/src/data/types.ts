/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
  /** 服务端计算字段：列表展示但不出现在登记表单里 */
  computedFields?: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type ReceiptRow = {
  id: number
  用户标识: string
  实验编码: string
  曝光时间: string
  计入窗口: string
  丢弃原因?: string
}

export type BatchReceipt = {
  日志批次: string
  轮次: number
  确认数: number
  保留记录: ReceiptRow[]
  丢弃重复: ReceiptRow[]
  受影响窗口: string[]
  触发来源: string
  确认时间: string
}

export type BatchReceiptResult = {
  日志批次: string
  最新回执: BatchReceipt | null
  轮次历史: BatchReceipt[]
  当前保留: ReceiptRow[]
  当前重复: ReceiptRow[]
}

export type JobBasisResult = {
  作业: Record<string, string | number>
  分母状态: string
  触发来源: string
  确认轮次: string
  分母数: string
  采用记录: number[]
  分母对账: {
    日志批次: string
    作业分母: number
    回执确认数: number | string
    回执轮次: number | string
    是否一致: boolean | string
  }[]
  修订理由: string
  依据历史: Record<string, unknown>[]
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
