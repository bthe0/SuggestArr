<template>
  <div class="lists-page">
    <div class="section-header">
      <h2><i class="fas fa-list-ul" aria-hidden="true"></i> Curated Lists</h2>
      <p>Auto-fill your library from other people's curated Trakt &amp; Letterboxd
        lists. Each list can create a matching Plex collection.</p>
    </div>

    <!-- Add-list form -->
    <form class="settings-card" novalidate @submit.prevent="add">
      <h3 class="card-title"><i class="fas fa-plus-circle" aria-hidden="true"></i> Add a list</h3>

      <div class="field">
        <label for="list-url">List URL <span class="req" aria-hidden="true">*</span></label>
        <div class="url-row">
          <input
            id="list-url"
            ref="urlInput"
            v-model.trim="form.url"
            type="url"
            inputmode="url"
            placeholder="https://trakt.tv/users/… or https://letterboxd.com/…/list/…"
            :aria-invalid="showUrlError ? 'true' : 'false'"
            aria-describedby="list-url-help list-url-error"
            @blur="urlTouched = true"
          />
          <span
            v-if="detectedSource"
            class="source-badge"
            :class="'src-' + detectedSource"
          >{{ sourceLabel(detectedSource) }}</span>
        </div>
        <p id="list-url-help" class="field-hint">
          Paste a public Trakt or Letterboxd list URL — the source is detected automatically.
        </p>
        <p
          v-if="showUrlError"
          id="list-url-error"
          class="field-error"
          role="alert"
        >{{ urlError }}</p>
      </div>

      <div class="field-grid">
        <div class="field">
          <label for="list-name">Name <span class="optional">(optional)</span></label>
          <input id="list-name" v-model.trim="form.name" type="text" :placeholder="derivedName || 'Auto from URL'" />
        </div>

        <div class="field">
          <label for="list-media">Media type</label>
          <select id="list-media" v-model="form.media_type">
            <option value="">Any</option>
            <option value="movie">Movies only</option>
            <option value="tv">TV only</option>
          </select>
        </div>

        <div class="field">
          <label for="list-interval">Sync interval (hours)</label>
          <input id="list-interval" v-model.number="form.sync_interval_hours" type="number" min="1" max="720" />
        </div>

        <div class="field">
          <label for="list-quality">Quality cap <span class="optional">(optional)</span></label>
          <input id="list-quality" v-model.trim="form.quality" type="text" placeholder="e.g. 1080p" />
        </div>
      </div>

      <div class="toggle-option">
        <span class="toggle-label">
          <i class="fas fa-layer-group" aria-hidden="true"></i>
          Create a Plex collection
          <span class="toggle-hint">One collection per list, named after it, kept in sync and removed with the list.</span>
        </span>
        <button
          type="button"
          role="switch"
          class="toggle-switch"
          :class="{ on: form.collection_enabled }"
          :aria-checked="form.collection_enabled ? 'true' : 'false'"
          aria-label="Create a Plex collection"
          @click="form.collection_enabled = !form.collection_enabled"
        >
          <span class="toggle-knob"></span>
        </button>
      </div>

      <div class="actions-row">
        <button class="btn btn-primary" type="submit" :disabled="adding || !canSubmit">
          <i :class="adding ? 'fas fa-spinner fa-spin' : 'fas fa-plus'" aria-hidden="true"></i>
          {{ adding ? 'Adding…' : 'Add list' }}
        </button>
      </div>

      <div
        v-if="addResult"
        class="result-banner"
        :class="{ ok: addResult.status === 'success', err: addResult.status === 'error' }"
        role="status"
      >
        <i :class="addResult.status === 'success' ? 'fas fa-check-circle' : 'fas fa-exclamation-circle'" aria-hidden="true"></i>
        <span>{{ addResult.message }}</span>
      </div>
    </form>

    <!-- Monitored lists -->
    <section class="settings-card" aria-labelledby="monitored-heading">
      <div class="card-header-row">
        <h3 id="monitored-heading"><i class="fas fa-stream" aria-hidden="true"></i> Monitored lists</h3>
        <button class="btn btn-link" :disabled="loading || refreshing" @click="loadLists(true)">
          <i :class="(loading || refreshing) ? 'fas fa-spinner fa-spin' : 'fas fa-sync'" aria-hidden="true"></i> Refresh
        </button>
      </div>

      <!-- Loading skeleton -->
      <div v-if="loading" class="skeleton-list" aria-live="polite" aria-busy="true">
        <span class="sr-only">Loading lists…</span>
        <div v-for="n in 3" :key="n" class="skeleton-row">
          <div class="sk sk-title"></div>
          <div class="sk sk-line"></div>
        </div>
      </div>

      <!-- Error -->
      <div v-else-if="error" class="result-banner err" role="alert">
        <i class="fas fa-exclamation-circle" aria-hidden="true"></i>
        <span>{{ error }}</span>
        <button class="btn btn-sm" @click="loadLists()">
          <i class="fas fa-redo" aria-hidden="true"></i> Retry
        </button>
      </div>

      <!-- First-use empty -->
      <div v-else-if="!lists.length" class="empty-state">
        <i class="fas fa-list-ul empty-icon" aria-hidden="true"></i>
        <p class="empty-title">No lists yet</p>
        <p class="empty-sub">Follow a curated Trakt or Letterboxd list and SuggestArr
          will keep your library topped up with its picks.</p>
        <button class="btn btn-primary" @click="focusUrl">
          <i class="fas fa-plus" aria-hidden="true"></i> Add your first list
        </button>
      </div>

      <!-- Populated -->
      <ul v-else class="list-rows">
        <li v-for="list in lists" :key="list.id" class="list-row">
          <div class="row-main">
            <div class="row-title">
              <span class="source-badge" :class="'src-' + list.source">{{ sourceLabel(list.source) }}</span>
              <span class="list-name">{{ list.name }}</span>
              <span v-if="list.media_type" class="media-badge">{{ list.media_type === 'tv' ? 'TV' : 'Movies' }}</span>
              <span v-if="list.collection_enabled" class="chip" :title="list.collection_id ? 'Plex collection linked' : 'Plex collection pending first sync'">
                <i class="fas fa-layer-group" aria-hidden="true"></i>
                {{ list.collection_id ? 'Collection linked' : 'Collection on' }}
              </span>
            </div>
            <a class="row-url" :href="list.url" :title="list.url" target="_blank" rel="noopener noreferrer">{{ list.url }}</a>
            <div class="row-meta">
              <span :class="['status-pill', statusClass(list.last_status)]">
                <i :class="statusIcon(list.last_status)" aria-hidden="true"></i>
                {{ lastSyncText(list) }}
              </span>
              <span class="meta-dim">Every {{ list.sync_interval_hours }}h</span>
              <span v-if="list.quality" class="meta-dim">· {{ list.quality }}</span>
            </div>
            <div v-if="rowState[list.id] && rowState[list.id].result" class="row-result" :class="rowState[list.id].result.status" role="status">
              {{ rowState[list.id].result.message }}
            </div>
          </div>

          <div class="row-actions">
            <template v-if="confirmingId === list.id">
              <span class="confirm-text">Remove “{{ list.name }}”{{ list.collection_id ? ' and its Plex collection' : '' }}?</span>
              <button class="btn btn-sm" @click="confirmingId = null">Cancel</button>
              <button class="btn btn-sm btn-danger" :disabled="isRemoving(list.id)" @click="remove(list)">
                <i :class="isRemoving(list.id) ? 'fas fa-spinner fa-spin' : 'fas fa-trash-alt'" aria-hidden="true"></i>
                Remove
              </button>
            </template>
            <template v-else>
              <button class="btn btn-sm" :disabled="isSyncing(list.id)" @click="syncNow(list)">
                <i :class="isSyncing(list.id) ? 'fas fa-spinner fa-spin' : 'fas fa-bolt'" aria-hidden="true"></i>
                {{ isSyncing(list.id) ? 'Syncing…' : 'Sync now' }}
              </button>
              <button class="btn btn-sm btn-danger-outline" @click="confirmingId = list.id">
                <i class="fas fa-trash-alt" aria-hidden="true"></i>
                <span class="sr-only">Remove {{ list.name }}</span>
              </button>
            </template>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>

<script>
import { listsApi } from '@/api/listsApi.js';

export default {
  name: 'SettingsLists',

  data() {
    return {
      loading: true,
      refreshing: false,
      error: null,
      adding: false,
      addResult: null,
      lists: [],
      confirmingId: null,
      rowState: {},
      urlTouched: false,
      form: {
        url: '',
        name: '',
        media_type: '',
        quality: '',
        collection_enabled: true,
        sync_interval_hours: 24,
      },
    };
  },

  computed: {
    detectedSource() {
      return this.detectSource(this.form.url);
    },
    derivedName() {
      return this.deriveName(this.form.url);
    },
    urlError() {
      if (!this.form.url) return 'A list URL is required.';
      if (!this.detectedSource) {
        return 'Unsupported URL — use a public trakt.tv or letterboxd.com list.';
      }
      return null;
    },
    showUrlError() {
      return this.urlTouched && !!this.urlError;
    },
    canSubmit() {
      return !!this.form.url && !!this.detectedSource;
    },
  },

  methods: {
    detectSource(url) {
      const u = (url || '').toLowerCase();
      if (u.includes('trakt.tv')) return 'trakt';
      if (u.includes('letterboxd.com')) return 'letterboxd';
      return null;
    },

    deriveName(url) {
      try {
        const path = new URL(url).pathname.split('/').filter(Boolean);
        let slug = path[path.length - 1] || '';
        if (slug === 'rss' && path.length >= 2) slug = path[path.length - 2];
        const pretty = slug.replace(/[-_]/g, ' ').trim();
        return pretty ? pretty.replace(/\b\w/g, (c) => c.toUpperCase()) : '';
      } catch {
        return '';
      }
    },

    sourceLabel(source) {
      return source === 'letterboxd' ? 'Letterboxd' : source === 'trakt' ? 'Trakt' : source;
    },

    async loadLists(isRefresh = false) {
      if (isRefresh) this.refreshing = true;
      else this.loading = true;
      this.error = null;
      try {
        const res = await listsApi.getLists();
        this.lists = res.lists || [];
        // Ensure a reactive row-state slot exists for each list.
        const next = {};
        for (const list of this.lists) next[list.id] = this.rowState[list.id] || {};
        this.rowState = next;
      } catch (err) {
        this.error = this.msg(err, 'Could not load your lists.');
      } finally {
        this.loading = false;
        this.refreshing = false;
      }
    },

    async add() {
      this.urlTouched = true;
      if (!this.canSubmit || this.adding) return;
      this.adding = true;
      this.addResult = null;
      try {
        const payload = {
          url: this.form.url,
          source: this.detectedSource,
          name: this.form.name || this.derivedName || undefined,
          media_type: this.form.media_type || null,
          quality: this.form.quality || null,
          collection_enabled: this.form.collection_enabled,
          sync_interval_hours: Number(this.form.sync_interval_hours) || 24,
        };
        const res = await listsApi.addList(payload);
        this.addResult = { status: 'success', message: `Added “${res.list?.name || payload.url}”.` };
        this.resetForm();
        await this.loadLists(true);
      } catch (err) {
        this.addResult = { status: 'error', message: this.msg(err, 'Could not add that list.') };
      } finally {
        this.adding = false;
      }
    },

    async syncNow(list) {
      this.setRow(list.id, { syncing: true, result: null });
      try {
        const res = await listsApi.syncList(list.id);
        const s = res.summary || {};
        const bits = [];
        if (typeof s.requested === 'number') bits.push(`${s.requested} requested`);
        if (typeof s.fetched === 'number') bits.push(`${s.fetched} found`);
        if (s.collection && s.collection.matched != null) bits.push(`${s.collection.matched} in collection`);
        this.setRow(list.id, { syncing: false, result: { status: 'ok', message: `Synced — ${bits.join(', ') || 'done'}.` } });
        await this.loadLists(true);
      } catch (err) {
        this.setRow(list.id, { syncing: false, result: { status: 'err', message: this.msg(err, 'Sync failed.') } });
      }
    },

    async remove(list) {
      this.setRow(list.id, { removing: true });
      try {
        await listsApi.removeList(list.id);
        this.confirmingId = null;
        await this.loadLists(true);
      } catch (err) {
        this.setRow(list.id, { removing: false, result: { status: 'err', message: this.msg(err, 'Could not remove the list.') } });
      }
    },

    setRow(id, patch) {
      this.rowState = { ...this.rowState, [id]: { ...(this.rowState[id] || {}), ...patch } };
    },
    isSyncing(id) {
      return !!(this.rowState[id] && this.rowState[id].syncing);
    },
    isRemoving(id) {
      return !!(this.rowState[id] && this.rowState[id].removing);
    },

    resetForm() {
      this.form = { url: '', name: '', media_type: '', quality: '', collection_enabled: true, sync_interval_hours: 24 };
      this.urlTouched = false;
    },

    focusUrl() {
      this.$refs.urlInput?.focus();
    },

    statusClass(status) {
      if (status === 'success') return 'ok';
      if (status === 'error') return 'err';
      return 'idle';
    },
    statusIcon(status) {
      if (status === 'success') return 'fas fa-check-circle';
      if (status === 'error') return 'fas fa-exclamation-circle';
      return 'far fa-clock';
    },
    lastSyncText(list) {
      if (!list.last_synced_at) return 'Never synced';
      const when = this.formatTs(list.last_synced_at);
      if (list.last_status === 'error') return `Failed · ${when}`;
      const summary = list.last_summary && typeof list.last_summary === 'object' ? list.last_summary : null;
      const req = summary && typeof summary.requested === 'number' ? ` · ${summary.requested} requested` : '';
      return `Synced ${when}${req}`;
    },

    formatTs(ts) {
      if (!ts) return '—';
      try {
        return new Date(ts.endsWith('Z') ? ts : ts + 'Z').toLocaleString();
      } catch {
        return ts;
      }
    },

    msg(err, fallback) {
      return err?.response?.data?.message || fallback;
    },
  },

  mounted() {
    this.loadLists();
  },
};
</script>

<style scoped>
.lists-page { padding: 0; }
.section-header h2 { font-size: 1.6rem; margin-bottom: 0.25rem; }
.section-header p { color: var(--color-text-muted, #aaa); }

.settings-card {
  background: var(--color-card, #2a2a2a);
  border: 1px solid var(--color-border, #444);
  border-radius: 8px; padding: 16px; margin-bottom: 20px;
}
.card-title { margin: 0 0 12px; font-size: 1.05rem; }

/* Form fields */
.field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.field label { font-size: 0.9rem; color: var(--color-text-primary, #ddd); }
.req { color: var(--color-error, #e74c3c); }
.optional { color: var(--color-text-muted, #888); font-weight: 400; font-size: 0.82rem; }
.field-hint { color: var(--color-text-muted, #888); font-size: 0.82rem; margin: 0; }
.field-error { color: var(--color-error, #e74c3c); font-size: 0.82rem; margin: 0; }

.field input, .field select {
  padding: 8px 10px;
  background: var(--color-bg, #1f1f1f); border: 1px solid var(--color-border, #444);
  border-radius: 6px; color: var(--color-text-primary, white); font-size: 0.9rem;
}
.field input[aria-invalid="true"] { border-color: var(--color-error, #e74c3c); }
.field input:focus-visible, .field select:focus-visible,
.btn:focus-visible, .toggle-switch:focus-visible, .row-url:focus-visible {
  outline: 2px solid var(--color-primary, #3498db); outline-offset: 2px;
}

.url-row { display: flex; align-items: center; gap: 8px; }
.url-row input { flex: 1; min-width: 0; }

.field-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px 16px;
}

.source-badge {
  font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
  padding: 3px 8px; border-radius: 999px; white-space: nowrap;
  border: 1px solid transparent;
}
.src-trakt { background: rgba(237, 34, 36, 0.15); color: #ff6b6d; border-color: rgba(237, 34, 36, 0.4); }
.src-letterboxd { background: rgba(0, 172, 28, 0.15); color: #40c463; border-color: rgba(0, 172, 28, 0.4); }

.media-badge {
  font-size: 0.72rem; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg, #1f1f1f); border: 1px solid var(--color-border, #444);
  color: var(--color-text-muted, #bbb);
}

/* Toggle switch (keyboard-operable button) */
.toggle-option { display: flex; justify-content: space-between; align-items: center; padding: 12px 0 4px; gap: 12px; }
.toggle-label { display: flex; flex-direction: column; gap: 4px; max-width: 70%; }
.toggle-hint { color: var(--color-text-muted, #888); font-size: 0.85rem; }
.toggle-switch {
  position: relative; width: 44px; height: 24px; flex: 0 0 auto;
  border-radius: 999px; border: 1px solid var(--color-border, #555);
  background: var(--color-bg, #1f1f1f); cursor: pointer; padding: 0;
  transition: background 0.15s ease;
}
.toggle-switch.on { background: var(--color-primary, #3498db); border-color: var(--color-primary, #3498db); }
.toggle-knob {
  position: absolute; top: 2px; left: 2px; width: 18px; height: 18px;
  border-radius: 50%; background: #fff; transition: transform 0.15s ease;
}
.toggle-switch.on .toggle-knob { transform: translateX(20px); }

/* Buttons */
.actions-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.btn {
  padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 0.9rem;
  border: 1px solid var(--color-border, #444); background: transparent; color: var(--color-text-primary, #ddd);
  display: inline-flex; align-items: center; gap: 6px;
}
.btn:hover:not(:disabled) { border-color: #888; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-sm { padding: 6px 10px; font-size: 0.82rem; }
.btn-primary { background: var(--color-primary, #3498db); border-color: var(--color-primary, #3498db); color: white; }
.btn-danger { background: var(--color-error, #c0392b); border-color: var(--color-error, #c0392b); color: white; }
.btn-danger-outline { color: var(--color-error, #e74c3c); border-color: rgba(231, 76, 60, 0.5); }
.btn-danger-outline:hover:not(:disabled) { border-color: var(--color-error, #e74c3c); }
.btn-link { background: transparent; border: none; color: var(--color-primary, #3498db); padding: 4px 8px; }

/* Result banners */
.result-banner { margin-top: 12px; padding: 10px; border-radius: 6px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.result-banner.ok { background: rgba(46, 204, 113, 0.12); border: 1px solid #2ecc71; color: #2ecc71; }
.result-banner.err { background: rgba(231, 76, 60, 0.12); border: 1px solid #e74c3c; color: #e74c3c; }

/* List rows */
.card-header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.list-rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.list-row {
  display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;
  padding: 12px; border: 1px solid var(--color-border-soft, #333); border-radius: 8px;
  background: var(--color-bg, #1f1f1f);
}
.row-main { min-width: 0; flex: 1; }
.row-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.list-name { font-weight: 600; }
.chip {
  font-size: 0.72rem; padding: 2px 8px; border-radius: 999px;
  background: rgba(52, 152, 219, 0.12); color: #6cb6e6; border: 1px solid rgba(52, 152, 219, 0.3);
}
.row-url {
  display: block; margin: 4px 0; font-size: 0.82rem; color: var(--color-text-muted, #8ab4d8);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 46ch;
}
.row-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 0.82rem; }
.meta-dim { color: var(--color-text-muted, #888); }
.status-pill { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 999px; }
.status-pill.ok { background: rgba(46, 204, 113, 0.12); color: #2ecc71; }
.status-pill.err { background: rgba(231, 76, 60, 0.12); color: #e74c3c; }
.status-pill.idle { background: var(--color-card, #2a2a2a); color: var(--color-text-muted, #aaa); }
.row-result { margin-top: 6px; font-size: 0.82rem; }
.row-result.ok { color: #2ecc71; }
.row-result.err { color: #e74c3c; }

.row-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; flex: 0 0 auto; }
.confirm-text { font-size: 0.82rem; color: var(--color-text-primary, #ddd); max-width: 24ch; }

/* Empty state */
.empty-state { text-align: center; padding: 28px 12px; }
.empty-icon { font-size: 2.4rem; color: var(--color-primary, #3498db); opacity: 0.7; margin-bottom: 10px; }
.empty-title { font-weight: 600; margin: 0 0 4px; }
.empty-sub { color: var(--color-text-muted, #999); max-width: 42ch; margin: 0 auto 14px; }

/* Skeleton */
.skeleton-list { display: flex; flex-direction: column; gap: 10px; }
.skeleton-row { padding: 12px; border: 1px solid var(--color-border-soft, #333); border-radius: 8px; }
.sk { border-radius: 4px; background: linear-gradient(90deg, #333 25%, #3d3d3d 37%, #333 63%); background-size: 400% 100%; animation: sk 1.4s ease infinite; }
.sk-title { height: 14px; width: 40%; margin-bottom: 8px; }
.sk-line { height: 10px; width: 70%; }
@keyframes sk { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }

.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}

@media (prefers-reduced-motion: reduce) {
  .sk { animation: none; }
  .toggle-knob, .toggle-switch { transition: none; }
}

@media (max-width: 640px) {
  .list-row { flex-direction: column; }
  .row-actions { justify-content: flex-start; }
  .row-url { max-width: 100%; }
}
</style>
