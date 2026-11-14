<template>
  <div class="space-y-3">
    <header class="flex flex-wrap items-end gap-2">
      <div>
        <h1 class="text-[16px] font-semibold text-slate-100">Trades & Performance</h1>
        <p class="text-[11px] text-muted">วัดผลจากไม้ที่ปิดแล้วจริง — ไม่มีการปรับตัวเลขย้อนหลัง</p>
      </div>
      <div class="ml-auto flex gap-1.5">
        <button class="btn py-1.5" @click="load">Refresh</button>
        <button class="btn btn-danger py-1.5" :disabled="!perms.canTrade" :title="perms.reason" @click="closeAll">Close all</button>
      </div>
    </header>

    <div class="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
      <Stat label="Net P&L" :value="money(m?.net_profit)" :tone="(m?.net_profit || 0) >= 0 ? 'text-up' : 'text-down'" />
      <Stat label="Profit factor" :value="fmtFactor(m?.profit_factor)" :hint="'target > 1.30'" />
      <Stat label="Expectancy" :value="money(m?.expectancy)" :hint="`${fmt(m?.expectancy_r, 2)}R avg`" />
      <Stat label="Win rate" :value="pct((m?.win_rate || 0) / 100, 1)" :hint="`${m?.wins || 0}W / ${m?.losses || 0}L`" />
      <Stat label="Max drawdown" :value="`${fmt(m?.max_drawdown_pct, 2)}%`" tone="text-down" />
      <Stat label="Sharpe" :value="fmt(m?.sharpe, 2)" />
      <Stat label="TP reached first" :value="pct((m?.tp_before_sl_rate || 0) / 100, 0)" hint="any target before the stop" />
      <Stat label="Trades" :value="m?.trades ?? 0" :hint="`costs ${money(m?.total_costs)} · swap ${money(m?.total_swap)}`" />
    </div>

    <div class="grid gap-3 xl:grid-cols-[minmax(0,1fr)_330px]">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">Equity curve</div>
          <span class="chip">live account · {{ curve.length }} points</span>
        </div>
        <div class="px-2 py-2">
          <EquityChart :curve="curve" :baseline="startingBalance" />
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><div class="panel-title">Exit distribution</div></div>
        <div class="space-y-1.5 px-4 py-3">
          <div v-for="(v, k) in byExit" :key="k" class="flex items-center justify-between text-[11px]">
            <span class="capitalize text-slate-300">{{ k.replaceAll('_', ' ') }}</span>
            <span class="flex items-center gap-2">
              <span class="chip">{{ v.count }}</span>
              <span class="num" :class="v.pnl >= 0 ? 'text-up' : 'text-down'">{{ money(v.pnl) }}</span>
            </span>
          </div>
          <p v-if="!Object.keys(byExit).length" class="py-4 text-center text-[11px] text-muted">ยังไม่มีข้อมูล</p>
        </div>
        <div class="border-t border-ink-700/60 px-4 py-3 text-[10px] leading-relaxed text-muted">
          {{ payload?.note }}
        </div>
      </section>
    </div>

    <PositionsTable />

    <section v-if="positions.length" class="panel">
      <div class="panel-head"><div class="panel-title">ตรวจเส้นแผนเดิมของไม้ที่ยังเปิดอยู่</div></div>
      <div class="flex flex-wrap gap-2 p-3">
        <button v-for="position in positions" :key="position.ticket" type="button" class="btn"
                :aria-pressed="selectedOpen === position.ticket" @click="reviewOpen(position)">
          {{ position.symbol }} · #{{ position.ticket }} · ดูเส้นก่อนส่งออเดอร์
        </button>
      </div>
      <p v-if="openError" class="px-3 pb-3 text-xs text-amber-300">{{ openError }}</p>
      <p v-if="openLoading" class="px-3 pb-3 text-xs text-muted">กำลังอ่านหลักฐานจากโบรกเกอร์…</p>
      <div v-if="openReview" class="space-y-3 p-3">
        <ArchivedTradeChart :key="`open-${openReview.position.ticket}`" :trade="openReview.position"
                            :snapshot="openReview.original_plan_snapshot?.original_signal"
                            :analysis="openReview.original_plan_snapshot?.analysis"
                            :post-entry="openReview.post_entry_candles" />
        <ArchivedCandleContinuity :archive="openReview.post_entry_candles" />
        <TradeLevels :key="`open-levels-${openReview.position.ticket}`" :trade="openReview.position"
                     :snapshot="openReview.original_plan_snapshot?.original_signal"
                     :analysis="openReview.original_plan_snapshot?.analysis" />
        <div class="rounded-lg border border-ink-700 p-3">
          <h3 class="text-xs font-semibold text-slate-200">บันทึกการจัดการออเดอร์ที่ยังเปิด</h3>
          <p class="mt-1 text-[10px] text-muted">{{ openReview.events?.length || 0 }} เหตุการณ์ · {{ openReview.deals?.length || 0 }} broker deals · {{ openReview.deal_attribution }}</p>
          <p v-for="event in openReview.events || []" :key="event.event_id" class="mt-1 text-xs text-slate-300">
            {{ datetime(event.created_at) }} · {{ event.kind }} · {{ event.status }} · {{ event.detail?.reason || event.actor }}
            <span v-if="event.kind === 'broker_observation'" class="text-amber-300">
              · ข้อมูลจากโบรกเกอร์ต่างจากบันทึก ({{ event.detail?.changed_fields?.join(', ') || 'ไม่ทราบรายละเอียด' }})
              · ยังไม่ยืนยันผู้แก้หรือการปิดไม้
            </span>
          </p>
          <p v-if="!openReview.events?.length" class="mt-2 text-xs text-muted">ยังไม่มีเหตุการณ์แก้ไข/ปิดบางส่วนที่ระบบบันทึกไว้</p>
        </div>
        <p class="text-[10px] text-muted">คำสั่งนี้แสดงหลักฐานอย่างเดียว ไม่สามารถเปิด/ปิดไม้จากหน้าดูเส้นนี้</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div class="panel-title">Closed trades</div>
        <span class="chip">{{ trades.length }} records</span>
      </div>
      <div class="overflow-x-auto">
        <table class="table-base">
          <thead>
            <tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Lot</th><th>Entry</th><th>Exit</th><th>Points</th>
              <th>P&L</th><th>R</th><th>Exit reason</th><th>Strategy</th><th>Mode</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in trades" :key="t.ticket" tabindex="0" role="button"
                :class="selectedTrade?.ticket === t.ticket ? 'bg-ink-800/70 ring-1 ring-gold/50' : ''"
                :aria-label="`เปิดทบทวนไม้ ${t.symbol} ${t.direction === 'up' ? 'ซื้อ' : 'ขาย'} หมายเลข ${t.ticket}`"
                :aria-expanded="selectedTrade?.ticket === t.ticket" class="cursor-pointer hover:bg-ink-800/40"
                @click="selectTrade(t)" @keydown.enter.prevent="selectTrade(t)" @keydown.space.prevent="selectTrade(t)">
              <td class="num text-[10.5px] text-muted">{{ datetime(t.closed_at) }}</td>
              <td>{{ t.symbol }}</td>
              <td><span class="chip" :class="t.direction === 'up' ? 'chip-up' : 'chip-down'">{{ t.direction === 'up' ? 'BUY' : 'SELL' }}</span></td>
              <td class="num">{{ fmt(t.volume, 2) }}</td>
              <td class="num">{{ fmt(t.entry, 2) }}</td>
              <td class="num">{{ fmt(t.exit, 2) }}</td>
              <td class="num" :class="t.points >= 0 ? 'text-up' : 'text-down'">{{ fmt(t.points, 0) }}</td>
              <td class="num" :class="t.pnl >= 0 ? 'text-up' : 'text-down'">{{ money(t.pnl) }}</td>
              <td class="num">{{ fmt(t.r_multiple, 2) }}</td>
              <td class="text-[10.5px] capitalize text-muted">{{ (t.exit_reason || '').replaceAll('_', ' ') }}</td>
              <td class="text-[10.5px] text-muted">{{ (t.strategy || '—').replaceAll('_', ' ') }}</td>
              <td class="text-[10.5px] text-muted">{{ t.mode }}</td>
            </tr>
            <tr v-if="!trades.length"><td colspan="12" class="py-6 text-center text-[11px] text-muted">ยังไม่มีไม้ที่ปิดแล้ว</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="selectedTrade" class="panel" aria-live="polite">
      <div class="panel-head">
        <div><div class="panel-title">ทบทวนไม้ {{ selectedTrade.symbol }} · {{ selectedTrade.direction === 'up' ? 'ซื้อ' : 'ขาย' }}</div>
          <p class="text-[10px] text-muted">{{ datetime(selectedTrade.opened_at) }} → {{ datetime(selectedTrade.closed_at) }} · ticket {{ selectedTrade.ticket }}</p></div>
        <button class="btn py-1.5" @click="selectedTrade = null">ปิดรายละเอียด</button>
      </div>
      <div v-if="reviewLoading" class="px-4 py-5 text-sm text-muted">กำลังอ่านบันทึกไม้…</div>
      <p v-else-if="reviewError" class="px-4 py-5 text-sm text-down">{{ reviewError }} <button class="btn ml-2" @click="selectTrade(selectedTrade)">ลองอีกครั้ง</button></p>
      <div v-else-if="review" class="grid gap-3 p-4 md:grid-cols-2">
        <ArchivedTradeChart class="md:col-span-2" :key="`chart-${selectedTrade.ticket}`"
                            :trade="review.trade" :snapshot="review.entry_snapshot"
                            :analysis="review.original_plan_snapshot?.analysis"
                            :post-entry="review.post_entry_candles" />
        <ArchivedCandleContinuity class="md:col-span-2" :archive="review.post_entry_candles" />
        <TradeLevels class="md:col-span-2" :key="selectedTrade.ticket"
                     :trade="review.trade" :snapshot="review.entry_snapshot"
                     :analysis="review.original_plan_snapshot?.analysis" />
        <article class="rounded-lg border border-ink-700 p-3">
          <h2 class="text-sm font-semibold">ตอนเข้าไม้ · ข้อมูลที่บันทึกไว้</h2>
          <p v-if="review.original_plan_snapshot?.evidence_level === 'original_plan'" class="mt-2 text-xs text-emerald-300">
            ✓ มีหลักฐานแผนก่อนส่งออเดอร์ (แก้ย้อนหลังไม่ได้)
            <span class="text-muted">· {{ review.original_plan_snapshot.analysis_provenance === 'decision_time' ? 'รวมโซน ณ เวลาตัดสินใจ' : 'ไม่มีข้อมูลโซนเดิม' }}</span>
          </p>
          <p v-else class="mt-2 text-xs text-amber-300">ไม้เก่าหรือคำสั่งที่ไม่มีหลักฐานแผนครบถ้วน · ระวังการตีความเส้นย้อนหลัง</p>
          <p v-if="review.entry_snapshot" class="mt-2 text-xs leading-relaxed text-slate-300">สัญญาณ {{ review.entry_snapshot.action }} · คะแนน {{ fmt(review.entry_snapshot.score, 0) }} · กลยุทธ์ {{ review.entry_snapshot.strategy }} · ภาวะตลาด {{ review.entry_snapshot.regime }}</p>
          <p v-else class="mt-2 text-xs text-amber-300">ไม่มี snapshot สัญญาณที่เชื่อมกับไม้ จึงไม่สรุปเหตุผลตอนเข้า</p>
          <p v-if="review.entry_snapshot?.reasons?.length" class="mt-2 text-xs text-muted">เหตุผล: {{ review.entry_snapshot.reasons.join(' · ') }}</p>
          <p v-if="review.entry_snapshot?.plan" class="mt-2 text-xs text-muted">แผนเดิม: SL {{ fmt(review.entry_snapshot.plan.sl) }} · เป้าหมาย {{ fmt(review.entry_snapshot.plan.tp3) }} · ความเสี่ยงตามแผน {{ money(review.entry_snapshot.plan.risk_amount) }}</p>
        </article>
        <article class="rounded-lg border border-ink-700 p-3 md:col-span-2">
          <h2 class="text-sm font-semibold">ลำดับการแก้แผนและหลักฐานจากโบรกเกอร์</h2>
          <p class="mt-1 text-xs text-muted">{{ review.events?.length || 0 }} เหตุการณ์ · {{ review.deals?.length || 0 }} broker deals</p>
          <div v-for="event in review.events || []" :key="event.event_id" class="mt-2 border-b border-ink-700/60 pb-2 text-xs text-slate-300">
            {{ datetime(event.created_at) }} · {{ event.kind }} · {{ event.status }} · {{ event.actor }}
            <span class="text-muted">{{ event.detail?.reason || '' }}</span>
            <span v-if="event.kind === 'broker_observation'" class="text-amber-300">
              · {{ event.detail?.changed_fields?.join(', ') || 'position state' }}
              · ยังไม่ยืนยันผู้แก้หรือผล fill
            </span>
          </div>
          <p v-if="!review.events?.length" class="mt-2 text-xs text-muted">ยังไม่มีเหตุการณ์การปรับแผนที่บันทึกไว้</p>
          <p class="mt-2 text-[10px] text-amber-300">{{ review.deal_attribution }} · หมายเลข position ตรงกันไม่ได้รับรองว่าเป็นการส่งจากกลยุทธ์นี้</p>
        </article>
        <article class="rounded-lg border border-ink-700 p-3">
          <h2 class="text-sm font-semibold">หลังปิด · ผลจริงและทบทวน</h2>
          <p class="mt-2 text-xs text-slate-300">ผล: {{ review.retrospective.outcome === 'profitable' ? 'กำไร' : review.retrospective.outcome === 'loss' ? 'ขาดทุน' : 'เท่าทุน' }} · {{ money(review.retrospective.pnl) }} · {{ fmt(review.retrospective.points, 0) }} จุด · {{ fmt(review.retrospective.r_multiple, 2) }}R</p>
          <p class="mt-2 text-xs leading-relaxed text-muted">ข้อชวนทบทวน: {{ review.retrospective.suggestions[0] || 'ข้อมูลที่บันทึกยังไม่พอสำหรับข้อเสนอแนะเฉพาะไม้' }}</p>
          <p class="mt-2 text-[10px] text-amber-300">{{ review.retrospective.basis }} · ไม่ใช่คำทำนายหรือคำแนะนำลงทุน</p>
        </article>
        <p class="text-[10px] leading-relaxed text-muted md:col-span-2">ข้อมูลนี้อ่านจากประวัติที่บันทึกในระบบ จึงดูได้เมื่อตลาดปิด แต่ต้องมีแอป/API และฐานข้อมูลที่เก็บประวัติพร้อมใช้งาน; ไม่รับประกันเมื่อเครื่องหรือเซิร์ฟเวอร์หยุดทำงาน</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <div class="panel-title">Broker deals / fills</div>
          <p class="text-[10px] text-muted">หลักฐานการ fill จากบัญชี broker · ไม่ใช่การยืนยัน ownership ของ strategy</p>
        </div>
        <span class="chip">{{ deals.length }} records · {{ dealAccount || 'account unresolved' }}</span>
      </div>
      <div class="overflow-x-auto">
        <table class="table-base">
          <thead><tr><th>Time</th><th>Deal / Order / Position</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Volume</th><th>Price</th><th>Costs</th><th>Profit</th></tr></thead>
          <tbody>
            <tr v-for="d in deals" :key="d.deal_id">
              <td class="num text-[10px]">{{ datetime(d.time_msc / 1000) }}</td>
              <td class="text-[10px]">{{ d.deal_id }} / {{ d.order_id }} / {{ d.position_id }}</td>
              <td>{{ d.symbol }}</td>
              <td>{{ d.side.toUpperCase() }}</td>
              <td>{{ d.entry }}</td>
              <td class="num">{{ fmt(d.volume, 2) }}</td>
              <td class="num">{{ fmt(d.price, 2) }}</td>
              <td class="num">{{ money(d.commission + d.swap + d.fee) }}</td>
              <td class="num">{{ money(d.profit) }}</td>
            </tr>
            <tr v-if="!deals.length"><td colspan="9" class="py-5 text-center text-[11px] text-muted">ยังไม่มี broker deal ที่บันทึกไว้ — กด Refresh เพื่อดึงประวัติ</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import EquityChart from '../components/EquityChart.vue'
import PositionsTable from '../components/PositionsTable.vue'
import Stat from '../components/Stat.vue'
import TradeLevels from '../components/TradeLevels.vue'
import ArchivedTradeChart from '../components/ArchivedTradeChart.vue'
import ArchivedCandleContinuity from '../components/ArchivedCandleContinuity.vue'
import { api } from '../lib/api'
import { datetime, fmt, fmtFactor, money, pct } from '../lib/format'
import { permissions, positions, toast } from '../lib/store'

// Read-only accounts see the buttons disabled — the server refuses them anyway.
const perms = computed(() => permissions.value)

const payload = ref(null)
const trades = ref([])
const deals = ref([])
const dealAccount = ref('')
const selectedTrade = ref(null)
const review = ref(null)
const reviewLoading = ref(false)
const reviewError = ref('')
const selectedOpen = ref('')
const openReview = ref(null)
const openLoading = ref(false)
const openError = ref('')
let reviewRequest = 0
const startingBalance = 10000

const m = computed(() => payload.value?.metrics || {})
const curve = computed(() => payload.value?.equity_curve || [])
const byExit = computed(() => payload.value?.by_exit_reason || {})

async function load() {
  try {
    const [perf, hist] = await Promise.all([api.get('/api/trading/performance'), api.get('/api/trading/trades?limit=100')])
    payload.value = perf
    trades.value = hist.trades || []
  } catch (err) {
    toast(err.message, 'error')
  }
  try {
    const dealHistory = await api.get('/api/trading/deals?limit=500')
    deals.value = dealHistory.deals || []
    dealAccount.value = dealHistory.account_key || ''
  } catch {
    deals.value = []
    dealAccount.value = 'บัญชียังยืนยันไม่ได้'
  }
}
async function selectTrade(trade) {
  const requestId = ++reviewRequest
  selectedTrade.value = trade
  review.value = null
  reviewError.value = ''
  reviewLoading.value = true
  try {
    const result = await api.get(`/api/trading/trades/${encodeURIComponent(trade.ticket)}/review`)
    if (requestId === reviewRequest) review.value = result
  } catch (err) {
    if (requestId === reviewRequest) reviewError.value = err.message || 'อ่านรายละเอียดไม้ไม่ได้'
  } finally {
    if (requestId === reviewRequest) reviewLoading.value = false
  }
}
async function reviewOpen(position) {
  selectedOpen.value = position.ticket
  openReview.value = null
  openError.value = ''
  openLoading.value = true
  try {
    const response = await api.get(`/api/trading/positions/${encodeURIComponent(position.ticket)}/review`)
    if (selectedOpen.value === position.ticket) openReview.value = response
  } catch (err) {
    if (selectedOpen.value === position.ticket) openError.value = err.message || 'ไม่พบหลักฐานไม้ปัจจุบัน'
  } finally {
    if (selectedOpen.value === position.ticket) openLoading.value = false
  }
}
async function closeAll() {
  if (!confirm('ปิดทุกไม้ที่เปิดอยู่?')) return
  try {
    await api.post('/api/trading/close-all')
    toast('ปิดทุกไม้แล้ว', 'success')
    await load()
  } catch (err) {
    toast(err.message, 'error')
  }
}

onMounted(load)
</script>
