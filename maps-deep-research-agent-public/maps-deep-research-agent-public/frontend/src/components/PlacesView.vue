<script setup lang="ts">
import { allPlaces } from "../stores/research";

const priceLabel = (lvl: number | null) =>
  lvl == null ? "" : "¥".repeat(Math.max(1, lvl));

function stars(rating: number | null): { full: number; half: boolean } {
  if (rating == null) return { full: 0, half: false };
  const full = Math.floor(rating);
  return { full, half: rating - full >= 0.5 };
}
</script>

<template>
  <section class="places">
    <header class="ph">
      <p class="eyebrow">寻得佳处 · Places</p>
      <span class="mono cnt" v-if="allPlaces.length">{{ allPlaces.length }}</span>
    </header>

    <div v-if="!allPlaces.length" class="empty">
      ○ 调研开始后，寻得的地点会在此列卷展开
    </div>

    <div class="grid">
      <article
        v-for="(p, i) in allPlaces"
        :key="p.place_id"
        class="card rise"
        :style="{ animationDelay: `${Math.min(i, 12) * 0.03}s` }"
      >
        <span class="idx mono">{{ i + 1 }}</span>

        <div class="main">
          <h3 class="name">{{ p.name }}</h3>

          <div class="rate" v-if="p.rating != null">
            <span class="num mono">{{ p.rating.toFixed(1) }}</span>
            <span class="stars">
              <span
                v-for="n in 5"
                :key="n"
                class="star"
                :class="{
                  full: n <= stars(p.rating).full,
                  half: n === stars(p.rating).full + 1 && stars(p.rating).half,
                }"
                >★</span
              >
            </span>
            <span class="reviews" v-if="p.user_ratings_total">
              {{ p.user_ratings_total.toLocaleString() }} 评
            </span>
            <span class="price" v-if="p.price_level != null">{{ priceLabel(p.price_level) }}</span>
          </div>

          <p class="editorial" v-if="p.editorial_summary">{{ p.editorial_summary }}</p>

          <p class="addr" v-if="p.address">{{ p.address }}</p>

          <div class="tags" v-if="p.categories?.length">
            <span v-for="c in p.categories.slice(0, 4)" :key="c" class="chip">{{ c }}</span>
          </div>

          <blockquote class="review" v-if="p.reviews?.length">
            “{{ p.reviews[0] }}”
          </blockquote>

          <div class="hours" v-if="p.opening_hours?.length">
            <span class="hk">营业</span>
            <span class="hv">{{ p.opening_hours[0] }}<template v-if="p.opening_hours.length > 1"> …</template></span>
          </div>

          <div class="links">
            <a
              v-if="p.google_maps_url"
              :href="p.google_maps_url"
              target="_blank"
              rel="noopener"
              class="lk lk--map"
              >📍 在 Google 地图打开 ↗</a
            >
            <a v-if="p.website" :href="p.website" target="_blank" rel="noopener" class="lk">官网 ↗</a>
            <a v-if="p.phone" :href="`tel:${p.phone}`" class="lk">{{ p.phone }}</a>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.places {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ph {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.cnt {
  font-size: 0.78rem;
  color: var(--accent);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
  overflow: auto;
  padding-right: 2px;
}
.card {
  position: relative;
  display: flex;
  gap: 10px;
  background: var(--bg-3);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 14px 14px 12px;
  transition: border-color 0.18s;
}
.card:hover {
  border-color: var(--accent-dim);
}
.idx {
  flex: none;
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  font-size: 0.74rem;
  font-weight: 700;
  color: #fdf6ec;
  background: var(--accent);
  border-radius: 6px;
  box-shadow: inset 0 0 0 1.5px rgba(253, 246, 236, 0.5);
}
.main {
  min-width: 0;
  flex: 1;
}
.name {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 1.02rem;
  font-weight: 700;
  line-height: 1.3;
  color: var(--text-hi);
}
.rate {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 6px 0;
}
.num {
  font-size: 0.84rem;
  font-weight: 700;
  color: var(--accent);
}
.stars {
  display: inline-flex;
  font-size: 0.78rem;
  letter-spacing: 1px;
}
.star {
  color: var(--line-strong);
}
.star.full {
  color: var(--accent-2);
}
.star.half {
  background: linear-gradient(90deg, var(--accent-2) 50%, var(--line-strong) 50%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
.reviews {
  font-size: 0.7rem;
  color: var(--text-lo);
}
.price {
  font-size: 0.74rem;
  color: var(--jade);
  font-weight: 600;
}
.editorial {
  margin: 6px 0;
  font-size: 0.82rem;
  color: var(--text-hi);
  line-height: 1.5;
  font-style: italic;
}
.addr {
  margin: 0;
  font-size: 0.78rem;
  color: var(--text-mid);
  line-height: 1.45;
}
.review {
  margin: 8px 0 0;
  padding: 7px 10px;
  font-size: 0.76rem;
  color: var(--text-mid);
  line-height: 1.55;
  background: var(--bg-2);
  border-left: 2px solid var(--accent-dim);
  border-radius: 0 var(--radius-xs) var(--radius-xs) 0;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin: 8px 0 0;
}
.hours {
  display: flex;
  gap: 7px;
  margin: 8px 0 0;
  font-size: 0.74rem;
}
.hk {
  color: var(--text-lo);
  flex: none;
}
.hv {
  color: var(--text-mid);
}
.links {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 11px 0 0;
  padding-top: 10px;
  border-top: 1px dashed var(--line);
}
.lk {
  font-size: 0.76rem;
  color: var(--accent);
}
.lk--map {
  font-weight: 600;
  padding: 5px 11px;
  border: 1px solid var(--accent-dim);
  border-radius: 999px;
  background: var(--accent-ghost);
  transition: background 0.15s, border-color 0.15s;
}
.lk--map:hover {
  background: var(--accent);
  border-color: var(--accent);
  color: #fdf6ec;
  text-decoration: none;
}
</style>
