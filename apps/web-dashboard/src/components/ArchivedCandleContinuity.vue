<template>
  <section class="rounded-lg border border-amber-700/40 bg-amber-950/10 p-3 text-xs" role="status" aria-live="polite">
    <h3 class="font-semibold text-amber-200">ความครบถ้วนของแท่งเทียน M5 ที่บันทึกไว้</h3>
    <p class="mt-1 text-slate-300">
      บันทึกที่ตรวจสอบได้: {{ coverage?.observed_bars ?? archive?.bars?.length ?? 0 }} แท่ง
      · {{ coverage?.gaps?.length ?? 0 }} ช่วงข้อมูลขาดหายระหว่างแท่งที่มี
    </p>
    <p v-if="!coverage" class="mt-1 text-amber-300">
      ยังไม่มีผลตรวจสอบความต่อเนื่องของข้อมูล ไม่สามารถรับรองว่าข้อมูลย้อนหลังครบถ้วน
    </p>
    <template v-else>
      <p v-if="coverage.observed_contiguous" class="mt-1 text-amber-200">
        แท่งที่สังเกตได้เรียงต่อเนื่องกัน แต่ไม่รับรองว่าครอบคลุมตั้งแต่เปิดจนปิดออเดอร์
      </p>
      <p v-else-if="!coverage.gaps?.length" class="mt-1 text-amber-300">
        ไม่มีข้อมูลเพียงพอสำหรับตรวจสอบความต่อเนื่อง
      </p>
      <ul v-if="coverage.gaps?.length" class="mt-2 space-y-1 text-amber-200">
        <li v-for="(gap, index) in coverage.gaps" :key="index">
          ขาด {{ gap.missing_bars }} แท่ง ระหว่าง
          {{ utc(gap.after_open_utc) }} ถึง {{ utc(gap.before_open_utc) }} (UTC)
        </li>
      </ul>
    </template>
    <p class="mt-2 text-[10px] leading-relaxed text-muted">
      ข้อมูลนี้เป็นเพียงแท่งเทียนที่ระบบสังเกตและจัดเก็บ ไม่ใช่ Tick History ที่โบรกเกอร์รับรอง
      และไม่ใช่หลักฐานยืนยันการ Fill ของออเดอร์ ช่วงก่อน/หลังข้อมูลที่มีอาจขาดหาย
    </p>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  archive: { type: Object, default: null },
})
const coverage = computed(() => props.archive?.continuity || null)
function utc(timestamp) {
  if (!Number.isFinite(timestamp)) return 'ไม่ทราบเวลา'
  return new Date(timestamp * 1000).toISOString().replace('T', ' ').slice(0, 16)
}
</script>
