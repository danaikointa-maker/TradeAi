<template>
  <div class="space-y-3">
    <header class="flex flex-wrap items-end gap-2">
      <div>
        <h1 class="text-[16px] font-semibold text-slate-100">Broker Manager</h1>
        <p class="text-[11px] text-muted">เพิ่ม/เปลี่ยนโบรกเกอร์ได้โดยไม่ต้องเขียนระบบใหม่ — ทุกอย่างอ้างจาก adapter + YAML profile</p>
      </div>
      <button class="btn ml-auto py-1.5" @click="load">Refresh</button>
    </header>

    <div class="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
      <article v-for="b in brokers" :key="b.id" class="panel flex flex-col p-3"
               :class="b.id === active ? 'ring-1 ring-gold-500/40' : ''">
        <div class="flex items-start justify-between">
          <div>
            <div class="text-[12.5px] font-semibold text-slate-100">{{ b.label }}</div>
            <div class="num text-[10px] text-muted">{{ b.id }} · {{ b.server }}</div>
          </div>
          <span class="chip" :class="b.health === 'connected' ? 'chip-up' : b.health === 'degraded' ? 'chip-warn' : 'chip-down'">
            {{ b.health }}
          </span>
        </div>
        <dl class="mt-2 text-[10.5px]">
          <div class="kv"><dt>Adapter</dt><dd>{{ b.adapter }}</dd></div>
          <div class="kv"><dt>Type</dt><dd>{{ b.account_type }}</dd></div>
          <div class="kv"><dt>Balance</dt><dd>{{ money(b.account?.balance) }}</dd></div>
          <div class="kv"><dt>Equity</dt><dd>{{ money(b.account?.equity) }}</dd></div>
          <div class="kv"><dt>Leverage</dt><dd>1:{{ b.account?.leverage }}</dd></div>
        </dl>
        <p class="mt-2 line-clamp-3 text-[9.5px] leading-relaxed text-muted">{{ b.regulated_note }}</p>
        <button class="btn mt-2 w-full py-1.5" :disabled="b.id === active" @click="activate(b.id)">
          {{ b.id === active ? 'Active' : 'Use this broker' }}
        </button>
      </article>
    </div>

    <section v-if="unresolvedOrders.length" class="panel border border-amber-500/50" role="alert">
      <div class="panel-head">
        <div class="panel-title text-amber-300">ออเดอร์ที่โบรกเกอร์ยังไม่ยืนยัน</div>
        <span class="chip chip-warn">WAIT · {{ unresolvedOrders.length }} รายการ</span>
      </div>
      <p class="px-4 pt-3 text-[11px] leading-relaxed text-amber-200">
        ห้ามส่งคำสั่งซ้ำหรือเปิดไม้ใหม่ด้วยคีย์อื่นจนกว่าจะตรวจสอบ Order/Deal ID จากโบรกเกอร์
        การพบโพซิชันที่มี signal_id หรือ lot ตรงกันยังไม่ใช่หลักฐานยืนยันว่าออเดอร์นี้สำเร็จ
      </p>
      <ul class="space-y-1 px-4 py-3 text-[11px] text-slate-200">
        <li v-for="order in unresolvedOrders" :key="order.id" class="rounded bg-ink-800/60 px-3 py-2">
          <span class="num">{{ order.id }}</span> · {{ order.symbol }} · {{ order.state }}
        </li>
      </ul>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div class="panel-title">Symbol specifications — {{ active }}</div>
        <span class="chip">จาก adapter · {{ specs[0]?.source || 'declared' }}</span>
      </div>
      <div class="overflow-x-auto">
        <table class="table-base">
          <thead>
            <tr>
              <th>Symbol</th><th>Digits</th><th>Point</th><th>Tick size</th><th>Contract</th>
              <th>Min / Max lot</th><th>Lot step</th><th>Stops level</th><th>Spread (typical)</th>
              <th>Commission / lot</th><th>Swap L/S</th><th>Source</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in specs" :key="s.symbol" class="hover:bg-ink-800/40">
              <td class="font-medium">{{ s.symbol }}</td>
              <td class="num">{{ s.digits }}</td>
              <td class="num">{{ s.point }}</td>
              <td class="num">{{ s.tick_size }}</td>
              <td class="num">{{ fmt(s.contract_size, 0) }}</td>
              <td class="num">{{ fmt(s.min_lot, 2) }} / {{ fmt(s.max_lot, 0) }}</td>
              <td class="num">{{ s.lot_step }}</td>
              <td class="num">{{ fmt(s.stops_level_points, 0) }}</td>
              <td class="num">{{ fmt(s.spread_typical_points, 0) }} pts</td>
              <td class="num">{{ money(s.commission_per_lot_round_turn) }}</td>
              <td class="num">{{ fmt(s.swap_long_per_lot, 2) }} / {{ fmt(s.swap_short_per_lot, 2) }}</td>
              <td><span class="chip" :class="s.source === 'broker' ? 'chip-up' : ''">{{ s.source }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="border-t border-ink-700/60 px-4 py-2 text-[10px] leading-relaxed text-muted">
        ค่า digit / point / tick size / contract size / stops level ต้องอ่านจากบัญชีจริงเสมอ
        ตัวอย่าง: XAUUSD ที่ point 0.01 → 500 points = $5.00 แต่ถ้าโบรกเกอร์ใช้ point 0.001 จะเหลือ $0.50
      </p>
    </section>

    <div class="grid gap-3 lg:grid-cols-2">
      <section class="panel">
        <div class="panel-head"><div class="panel-title">Connection & integrity</div></div>
        <dl class="px-4 py-3">
          <div class="kv"><dt>Health map</dt><dd>{{ JSON.stringify(healthMap) }}</dd></div>
          <div class="kv"><dt>Last reconcile</dt><dd>{{ reconcile?.ok === true ? 'matched' : reconcile?.ok === false ? 'mismatch' : 'not run' }}</dd></div>
          <div class="kv"><dt>Positions at broker</dt><dd>{{ reconcile?.positions ?? '—' }}</dd></div>
          <div class="kv"><dt>Missing at broker</dt><dd>{{ (reconcile?.missing_at_broker || []).join(', ') || '—' }}</dd></div>
          <div class="kv"><dt>Unknown locally</dt><dd>{{ (reconcile?.unknown_locally || []).join(', ') || '—' }}</dd></div>
          <div class="kv"><dt>Changed at broker</dt><dd>{{ (reconcile?.changed_at_broker || []).join(', ') || '—' }}</dd></div>
          <div v-if="reconcile?.reconciliation_required" class="mt-2 text-amber-300" role="alert">ต้องตรวจสอบบัญชีและประวัติ fill ก่อนดำเนินการต่อ ห้ามส่งคำสั่งซ้ำ</div>
        </dl>
        <p class="border-t border-ink-700/60 px-4 py-2 text-[10px] leading-relaxed text-muted">
          ระบบใช้ idempotency key ทุกคำสั่ง ตรวจสอบออร์เดอร์กับโบรกเกอร์หลังส่ง และหยุดรับคำสั่งเมื่อการเชื่อมต่อผิดปกติ
          ไม่ส่งซ้ำโดยพลการ
        </p>
      </section>
      <section class="panel">
        <div class="panel-head"><div class="panel-title">เพิ่มโบรกเกอร์ใหม่</div><span class="chip">no-code</span></div>
        <ol class="space-y-1.5 px-4 py-3 text-[10.5px] leading-relaxed text-slate-300">
          <li>1. เพิ่ม profile ใน <span class="num text-gold-400">config/brokers.yaml</span> (id, adapter, server, symbols + specs)</li>
          <li>2. ตั้งค่า ENV: <span class="num">MT5_LOGIN / MT5_PASSWORD / MT5_SERVER / MT5_TERMINAL_PATH</span></li>
          <li>3. ถ้าเป็น MT5 มาตรฐาน ใช้ adapter <span class="num">mt5</span> ได้ทันที</li>
          <li>4. ถ้าเป็น API เฉพาะ  implement <span class="num">BrokerAdapter</span> 1 ไฟล์ใน <span class="num">integrations/brokers/</span></li>
          <li>5. กด Refresh → Use this broker (ระบบจะดึง symbol specs จาก terminal จริง)</li>
        </ol>
        <p class="border-t border-ink-700/60 px-4 py-2 text-[10px] leading-relaxed text-amber-200/70">
          อย่าเปิด endpoint ส่งคำสั่งให้เข้าถึงจากอินเทอร์เน็ตโดยไม่มี auth — ระบบนี้บังคับ JWT และตรวจ risk gate ทุกครั้ง
        </p>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../lib/api'
import { fmt, money } from '../lib/format'
import { state, toast } from '../lib/store'

const brokers = ref([])
const active = ref('')
const specs = ref([])
const loading = ref(false)

const healthMap = computed(() => Object.fromEntries(brokers.value.map((b) => [b.id, b.health])))
const reconcile = computed(() => state.live?.reconcile || null)
const unresolvedOrders = computed(() => state.live?.account?.broker_id === active.value
  && Array.isArray(state.live?.unresolved_orders) ? state.live.unresolved_orders : [])

async function load() {
  loading.value = true
  try {
    const data = await api.get('/api/brokers')
    brokers.value = data.brokers || []
    active.value = data.active
    const sym = await api.get(`/api/brokers/${encodeURIComponent(active.value)}/symbols`)
    specs.value = sym.symbols || []
  } catch (err) {
    toast(err.message, 'error')
  } finally {
    loading.value = false
  }
}

async function activate(id) {
  try {
    await api.post(`/api/brokers/${encodeURIComponent(id)}/activate`)
    toast(`เปลี่ยนโบรกเกอร์เป็น ${id}`, 'success')
    await load()
  } catch (err) {
    toast(err.message, 'error')
  }
}

onMounted(load)
</script>
