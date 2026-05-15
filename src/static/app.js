// Externalized app script (moved from inline in index.html)
// This file is loaded with `defer` so DOM is available.
const authControls = document.getElementById('authControls')
const requestsTable = document.getElementById('requestsTable')
const updatesEl = document.getElementById('updates')
const searchInput = document.getElementById('searchInput')
const pageSizeSelect = document.getElementById('pageSize')
const paginationEl = document.getElementById('pagination')
const statusCtx = document.getElementById('statusChart')
const topChatsCtx = document.getElementById('topChatsChart')
const durationCtx = document.getElementById('durationChart')
const statusFilterSelect = document.getElementById('statusFilter')
const queueQueuedEl = document.getElementById('queueQueued')
const queueRunningEl = document.getElementById('queueRunning')
const refreshQueueBtn = document.getElementById('refreshQueueBtn')
const clearQueueBtn = document.getElementById('clearQueueBtn')
const startQueueBtn = document.getElementById('startQueueBtn')
const showConfigBtn = document.getElementById('showConfigBtn')

let timeSeriesChart = null
let statusChart = null
let loginInlineInit = false

// helper: use cookie-based session (include credentials for widest compatibility)
function fetchWithCreds(url, opts) {
  opts = opts || {}
  opts.credentials = opts.credentials || 'include'
  return fetch(url, opts)
}

async function loadRequests(page = 1) {
  try {
    const limit = parseInt(pageSizeSelect.value, 10)
    const offset = (page - 1) * limit
    let q = `/requests?limit=${limit}&offset=${offset}`
    try {
      const sVal = (searchInput && searchInput.value) ? searchInput.value.trim() : ''
      if (sVal) q += '&q=' + encodeURIComponent(sVal)
    } catch (e) {}
    try {
      const sf = (statusFilterSelect && statusFilterSelect.value) ? statusFilterSelect.value : 'all'
      if (sf && sf !== 'all') q += '&status=' + encodeURIComponent(sf)
    } catch (e) {}
    const resp = await fetchWithCreds(q)
    if (!resp.ok) throw new Error('unauthorized')
    const data = await resp.json()
    return data
  } catch (err) {
    requestsTable.innerHTML = '<tr><td colspan="9">Could not fetch requests (invalid token?)</td></tr>'
    return { items: [], total: 0, offset: 0, limit: parseInt(pageSizeSelect.value, 10) }
  }
}

async function loadUpdates(page = 1) {
  try {
    const limit = 50
    const offset = (page - 1) * limit
    const resp = await fetchWithCreds(`/api/updates?limit=${limit}&offset=${offset}`)
    if (!resp.ok) throw new Error('unauthorized')
    const data = await resp.json()
    updatesEl.textContent = data.items.slice(0,50).map(u => `${u.created_at}\n${u.raw}\n---`).join('\n')
    return data
  } catch (err) {
    updatesEl.textContent = 'Could not fetch updates (invalid token?)'
    return { items: [] }
  }
}

function renderStatusChart(counts) {
  const labels = Object.keys(counts)
  const values = Object.values(counts)
  const ctx = statusCtx.getContext('2d')
  const colors = ['#0d6efd','#198754','#ffc107','#dc3545','#6c757d','#6f42c1','#0dcaf0']
  if (!statusChart) {
    statusChart = new Chart(ctx, {
      type: 'doughnut',
      data: { labels, datasets: [{ data: values, backgroundColor: colors.slice(0, values.length) }] },
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'bottom', labels: { boxWidth: 12, padding: 6 } },
          tooltip: { mode: 'index', intersect: false }
        },
        cutout: '50%'
      }
    })
  } else {
    statusChart.data.labels = labels
    statusChart.data.datasets[0].data = values
    statusChart.data.datasets[0].backgroundColor = colors.slice(0, values.length)
    statusChart.update()
  }
}

function computeTimeSeries(reqs, hours = 24) {
  const now = Date.now()
  const buckets = new Array(hours).fill(0)
  const labels = []
  for (let i = hours - 1; i >= 0; i--) {
    const t = new Date(now - i * 3600 * 1000)
    labels.push(t.getHours() + ':' + String(t.getMinutes()).padStart(2, '0'))
  }
  for (const r of reqs) {
    try {
      const ts = new Date(r.created_at).getTime()
      const diffHours = Math.floor((now - ts) / (3600 * 1000))
      if (diffHours >= 0 && diffHours < hours) {
        buckets[hours - 1 - diffHours] += 1
      }
    } catch (e) {
      // ignore parse errors
    }
  }
  return { labels, buckets }
}

function renderTimeSeries(reqs) {
  const series = computeTimeSeries(reqs, 24)
  const canvas = document.getElementById('timeSeriesChart')
  if (!canvas) return
  const c = canvas.getContext('2d')
  const gradient = c.createLinearGradient(0,0,0,160)
  gradient.addColorStop(0,'rgba(13,110,253,0.18)')
  gradient.addColorStop(1,'rgba(13,110,253,0.03)')
  if (!timeSeriesChart) {
    timeSeriesChart = new Chart(c, {
      type: 'line',
      data: { labels: series.labels, datasets: [{ label: 'Requests', data: series.buckets, borderColor: '#0d6efd', backgroundColor: gradient, fill: true, tension: 0.35, pointRadius: 2 }] },
      options: { responsive: true, scales: { x: { ticks: { maxRotation: 0 } } }, plugins: { legend:{ display:false }, tooltip: { mode:'index', intersect:false } } }
    })
  } else {
    timeSeriesChart.data.labels = series.labels
    timeSeriesChart.data.datasets[0].data = series.buckets
    timeSeriesChart.update()
  }
}

async function loadAll() {
  try {
    const mainEl = document.getElementById('mainContainer')
    if (mainEl && window.getComputedStyle && window.getComputedStyle(mainEl).display === 'none') {
      // main UI hidden (login/not-admin shown) — don't call protected APIs
      return
    }
    // check whether the session is admin to avoid repeated 403s
    const meResp = await fetchWithCreds('/api/me')
    if (!meResp.ok) {
      // not logged in or session expired — let initAuth handle UI
      return
    }
    const me = await meResp.json()
    if (!me.is_admin) {
      requestsTable.innerHTML = '<tr><td colspan="9">Admin privileges required to view requests. Use the "Grant admin" button if available.</td></tr>'
      updatesEl.textContent = 'Admin privileges required to fetch updates.'
      return
    }

    await renderTablePage(currentPage)
    await loadQueue()
    const reqData = await loadRequests(currentPage)
    await loadUpdates()
    await loadStats()
    const items = (reqData && reqData.items) || []
    // prefer server-side aggregates when available
    await loadAggregates()
    await loadRateLimits()
    renderTimeSeries(items)
  } catch (err) {
    // ignore errors — keep UI readable
  }
}

async function loadQueue() {
  try {
    if (!queueQueuedEl && !queueRunningEl) return
    const resp = await fetchWithCreds('/api/queue')
    if (!resp.ok) {
      queueQueuedEl.innerHTML = '<li class="list-group-item">No access</li>'
      queueRunningEl.innerHTML = ''
      return
    }
    const d = await resp.json()
    renderQueue(d)
  } catch (e) {
    try { if (queueQueuedEl) queueQueuedEl.innerHTML = '<li class="list-group-item">Error</li>' } catch (e) {}
  }
}

function formatTimeAgo(iso) {
  try {
    const t = new Date(iso).getTime()
    const diff = Math.floor((Date.now() - t) / 1000)
    if (diff < 5) return 'now'
    if (diff < 60) return diff + 's'
    if (diff < 3600) return Math.floor(diff/60) + 'm'
    if (diff < 86400) return Math.floor(diff/3600) + 'h'
    return Math.floor(diff/86400) + 'd'
  } catch (e) { return '' }
}

function renderQueue(d) {
  try {
    const queued = (d && d.queued) || []
    const running = (d && d.running) || []
    if (queueQueuedEl) {
      queueQueuedEl.innerHTML = ''
      if (queued.length === 0) {
        const li = document.createElement('li'); li.className='list-group-item small text-muted'; li.textContent='(vuota)'; queueQueuedEl.appendChild(li)
      } else {
        for (const it of queued.slice(0,50)) {
          const li = document.createElement('li')
          li.className = 'list-group-item d-flex justify-content-between align-items-start'
          const left = document.createElement('div')
          left.style.maxWidth = '60%'
          const a = document.createElement('a')
          a.href = it.url || '#'
          a.target = '_blank'
          a.className = 'text-truncate-link'
          a.textContent = it.url || ('id:' + (it.request_id || ''))
          left.appendChild(a)
          const meta = document.createElement('div')
          meta.className = 'small text-muted'
          meta.textContent = it.chat_id ? ('chat:' + it.chat_id) : ''
          left.appendChild(meta)
          const right = document.createElement('div')
          right.className = 'small text-muted text-end'
          right.textContent = formatTimeAgo(it.enqueued_at || it.created_at || new Date().toISOString())
          li.appendChild(left)
          li.appendChild(right)
          queueQueuedEl.appendChild(li)
        }
      }
    }
    if (queueRunningEl) {
      queueRunningEl.innerHTML = ''
      if (running.length === 0) {
        const li = document.createElement('li'); li.className='list-group-item small text-muted'; li.textContent='(nessun job in esecuzione)'; queueRunningEl.appendChild(li)
      } else {
        for (const it of running.slice(0,50)) {
          const li = document.createElement('li')
          li.className = 'list-group-item d-flex justify-content-between align-items-start'
          const left = document.createElement('div')
          left.style.maxWidth = '60%'
          const a = document.createElement('a')
          a.href = it.url || '#'
          a.target = '_blank'
          a.className = 'text-truncate-link'
          a.textContent = it.url || ('id:' + (it.request_id || ''))
          left.appendChild(a)
          const meta = document.createElement('div')
          meta.className = 'small text-muted'
          meta.textContent = it.chat_id ? ('chat:' + it.chat_id) : ''
          left.appendChild(meta)
          const right = document.createElement('div')
          right.className = 'small text-muted text-end'
          right.textContent = formatTimeAgo(it.enqueued_at || new Date().toISOString())
          li.appendChild(left)
          li.appendChild(right)
          queueRunningEl.appendChild(li)
        }
      }
    }
  } catch (e) {
    // ignore
  }
}

async function loadAggregates() {
  try {
    const resp = await fetchWithCreds('/api/aggregates')
    if (!resp.ok) return
    const d = await resp.json()
    // populate status filter options
    if (statusFilterSelect) {
      const existing = new Set(Array.from(statusFilterSelect.options).map(o=>o.value))
      Object.keys(d.status_counts || {}).forEach(s => {
        const v = s || 'unknown'
        if (!existing.has(v)) {
          const opt = document.createElement('option')
          opt.value = v
          opt.textContent = v
          statusFilterSelect.appendChild(opt)
        }
      })
    }
    if (d.status_counts) renderStatusChart(d.status_counts)
    if (d.top_chats) renderTopChats(d.top_chats)
    if (d.duration_histogram) renderDurationHist(d.duration_histogram)
  } catch (e) {
    // ignore
  }
}

async function loadRateLimits() {
  try {
    const resp = await fetchWithCreds('/api/rate_limits')
    const el = document.getElementById('rateLimitsList')
    if (!el) return
    if (!resp.ok) {
      el.innerHTML = '<div class="small text-muted">No access</div>'
      return
    }
    const d = await resp.json()
    renderRateLimits(d)
  } catch (e) {
    const el = document.getElementById('rateLimitsList')
    if (el) el.innerHTML = '<div class="small text-danger">Error fetching rate limits</div>'
  }
}

function renderRateLimits(d) {
  const el = document.getElementById('rateLimitsList')
  if (!el) return
  el.innerHTML = ''
  try {
    // Global unlimit action
    const topActions = document.createElement('div')
    topActions.className = 'd-flex justify-content-end mb-2'
    const unlimitAllBtn = document.createElement('button')
    unlimitAllBtn.className = 'btn btn-sm btn-outline-primary'
    unlimitAllBtn.textContent = 'Sblocca tutti (requeue)'
    unlimitAllBtn.onclick = async () => {
      try {
        unlimitAllBtn.disabled = true
        await fetchWithCreds('/api/unlimit_all', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ requeue: true }) })
        await loadRateLimits(); await loadQueue()
      } catch (e) { console.debug('unlimit_all failed', e) }
      unlimitAllBtn.disabled = false
    }
    topActions.appendChild(unlimitAllBtn)
    el.appendChild(topActions)
    // currently limited chats (in-memory worker data)
    const curr = d.currently_limited || []
    if (curr.length > 0) {
      const hdr = document.createElement('div'); hdr.className='mb-2 small text-muted'; hdr.textContent = `Currently limited chats: ${curr.length}`; el.appendChild(hdr)
        for (const c of curr.slice(0,50)) {
        const div = document.createElement('div'); div.className='d-flex justify-content-between align-items-center mb-1'
        const left = document.createElement('div'); left.className='small'; left.textContent = `chat: ${c.chat_id}`
        const right = document.createElement('div'); right.className='d-flex gap-2 align-items-center'
        const info = document.createElement('div'); info.className='small text-muted'; info.textContent = c.next_in_seconds ? `blocked for ${c.next_in_seconds}s` : ''
        const btn = document.createElement('button'); btn.className='btn btn-sm btn-outline-primary'; btn.style.fontSize='0.8rem'; btn.textContent = 'Sblocca';
        btn.onclick = async () => {
          try {
            btn.disabled = true
            const requeue = confirm('Rimettere in coda le richieste persistenti per questa chat? OK = Sì, Annulla = No')
            await unlimitChat(c.chat_id, requeue)
            await loadRateLimits(); await loadQueue()
          } catch(e){ console.debug('unlimit failed', e) }
          finally { btn.disabled = false }
        }
        right.appendChild(info); right.appendChild(btn);
        div.appendChild(left); div.appendChild(right); el.appendChild(div)
      }
      const sep = document.createElement('hr'); sep.className='my-2'; el.appendChild(sep)
    }
    const per = d.per_chat || []
    if (per.length === 0) {
      const div = document.createElement('div'); div.className='small text-muted'; div.textContent='No recent activity'; el.appendChild(div);
    } else {
      for (const p of per.slice(0,50)) {
        const item = document.createElement('div')
        item.className = 'mb-2'
        const h = document.createElement('div'); h.className='d-flex justify-content-between align-items-center'
        const left = document.createElement('div'); left.className='small'; left.textContent = 'chat: ' + p.chat_id
        const right = document.createElement('div'); right.className='d-flex gap-2 align-items-center small text-muted';
        const nowSec = Math.floor(Date.now()/1000)
        let rightText = ''
        if (p.last_rate_limited_next) {
          const next = Math.floor(p.last_rate_limited_next)
          if (next > nowSec) {
            rightText = 'blocked for ' + (next - nowSec) + 's'
          }
        } else if (p.next_available_seconds) {
          rightText = 'next_in ' + p.next_available_seconds + 's'
        }
        const infoDiv = document.createElement('div'); infoDiv.textContent = rightText
        const btn = document.createElement('button'); btn.className='btn btn-sm btn-outline-primary'; btn.style.fontSize='0.8rem'; btn.textContent = 'Sblocca';
        btn.onclick = async () => { try { btn.disabled = true; await unlimitChat(p.chat_id, true); await loadRateLimits(); await loadQueue() } catch(e){ console.debug('unlimit failed', e) } finally { btn.disabled = false } }
        right.appendChild(infoDiv); right.appendChild(btn)
        h.appendChild(left); h.appendChild(right)
        item.appendChild(h)
        // counters (compact)
        const cnts = document.createElement('div'); cnts.className = 'small text-muted'
        const parts = []
        for (const c of (p.counters || [])) {
          parts.push(`${c.count}/${c.limit}@${Math.floor(c.window_seconds/60) || c.window_seconds}s`)
        }
        cnts.textContent = parts.join(' | ')
        item.appendChild(cnts)
        el.appendChild(item)
      }
    }

    // show a short link to persisted rate_limited rows (no per-link actions)
    const persisted = d.persisted_limited_chats || []
    if (persisted.length > 0) {
      const hdr2 = document.createElement('div'); hdr2.className='mt-2 small text-muted'; hdr2.textContent = 'Persisted blocked chats (DB):'; el.appendChild(hdr2)
      const list = document.createElement('div'); list.className = 'small';
      list.style.maxHeight = '160px'; list.style.overflow = 'auto'; list.style.paddingLeft = '0.5rem'
      for (const p of persisted.slice(0,50)) {
        const dline = document.createElement('div'); dline.style.marginBottom='6px'; dline.className='d-flex justify-content-between align-items-center'
        const span = document.createElement('div'); span.textContent = `chat: ${p.chat_id} (${p.count} items)`
        const actions = document.createElement('div')
        const btnRe = document.createElement('button'); btnRe.className='btn btn-sm btn-outline-primary me-1'; btnRe.textContent = 'Sblocca (requeue)'
        btnRe.onclick = async () => { try { btnRe.disabled = true; await unlimitChat(p.chat_id, true); await loadRateLimits(); await loadQueue() } catch(e){ console.debug('unlimit failed', e) } finally { btnRe.disabled = false } }
        const btnNo = document.createElement('button'); btnNo.className='btn btn-sm btn-outline-secondary'; btnNo.textContent = 'Sblocca (no requeue)'
        btnNo.onclick = async () => { try { btnNo.disabled = true; await unlimitChat(p.chat_id, false); await loadRateLimits(); await loadQueue() } catch(e){ console.debug('unlimit failed', e) } finally { btnNo.disabled = false } }
        actions.appendChild(btnRe); actions.appendChild(btnNo)
        dline.appendChild(span); dline.appendChild(actions); list.appendChild(dline)
      }
      el.appendChild(list)
    }
  } catch (e) {
    el.innerHTML = '<div class="small text-danger">Error rendering rate limits</div>'
  }
}

async function unlimitChat(chatId, requeue) {
  try {
    await fetchWithCreds('/api/unlimit_chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: chatId, requeue: !!requeue }) })
    await loadRateLimits(); await loadQueue()
  } catch (e) { console.debug('unlimitChat failed', e) }
}

async function clearQueue(chatId) {
  const btn = document.getElementById('clearQueueBtn')
  try {
    if (btn) { btn.disabled = true; btn.textContent = 'Svuotando…' }
    const body = chatId ? { chat_id: chatId } : {}
    const resp = await fetchWithCreds('/api/clear_queue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    if (!resp.ok) throw new Error('server error')
    // refresh only the queue and rate-limits to avoid triggering broad reloads
    await loadQueue()
    await loadRateLimits()
  } catch (e) {
    console.debug('clearQueue failed', e)
    alert('Errore durante lo svuotamento della coda')
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Svuota coda' }
  }
}

async function showConfig() {
  try {
    const resp = await fetchWithCreds('/config')
    if (!resp.ok) return
    const cfg = await resp.json()
    const el = document.getElementById('configDump')
    if (el) el.textContent = JSON.stringify(cfg, null, 2)
  } catch (e) { console.debug('showConfig failed', e) }
}

let topChatsChart = null
function renderTopChats(topChats) {
  try {
    const labels = topChats.map(t => String(t.chat_id))
    const data = topChats.map(t => t.count)
    const c = topChatsCtx.getContext('2d')
    if (!topChatsChart) {
      topChatsChart = new Chart(c, { type: 'bar', data: { labels, datasets: [{ label: 'Requests', data, backgroundColor: '#0d6efd' }] }, options: { indexAxis: 'y', responsive:true, plugins:{legend:{display:false}} } })
    } else {
      topChatsChart.data.labels = labels
      topChatsChart.data.datasets[0].data = data
      topChatsChart.update()
    }
  } catch (e) {}
}

let durationChart = null
function renderDurationHist(hist) {
  try {
    const labels = hist.labels || []
    const data = hist.counts || []
    const c = durationCtx.getContext('2d')
    if (!durationChart) {
      durationChart = new Chart(c, { type: 'bar', data: { labels, datasets: [{ label: 'Count', data, backgroundColor: '#198754' }] }, options: { responsive:true, plugins:{legend:{display:false}}, scales:{ y: { beginAtZero:true } } } })
    } else {
      durationChart.data.labels = labels
      durationChart.data.datasets[0].data = data
      durationChart.update()
    }
  } catch (e) {}
}

// server-side pagination & search (search handled server-side later)
let currentPage = 1

async function renderTablePage(page = 1) {
  const limit = parseInt(pageSizeSelect.value, 10)
  const data = await loadRequests(page)
  const total = data.total || 0
  const items = data.items || []
  requestsTable.innerHTML = ''
    for (const r of items) {
    const tr = document.createElement('tr')
      const comp = r.compressed ? 'yes' : (r.compressed === false ? 'no' : '')
    const proc = r.processing_duration_seconds ? (r.processing_duration_seconds.toFixed(1) + 's') : ''
    const eventsSummary = (r.events || []).map(e => `${e.type}${e.duration_seconds ? `:${e.duration_seconds.toFixed(1)}s` : ''}`).join(', ')
    let statusClass = 'bg-secondary'
    if (r.status === 'finished' || r.status === 'done' || r.status === 'completed') statusClass = 'bg-success'
    else if (r.status === 'failed' || r.status === 'error') statusClass = 'bg-danger'
    else if (r.status === 'running' || r.status === 'processing' || r.status === 'in_progress') statusClass = 'bg-info'
    else if (r.status === 'queued' || r.status === 'pending') statusClass = 'bg-warning text-dark'

    // build cells safely to avoid HTML injection
    const tdId = document.createElement('td'); tdId.textContent = String(r.id); tr.appendChild(tdId)
    const tdChat = document.createElement('td'); tdChat.textContent = String(r.chat_id); tr.appendChild(tdChat)
    const tdUrl = document.createElement('td'); const a = document.createElement('a'); a.href = r.url || '#'; a.target = '_blank'; a.className = 'text-truncate-link'; a.rel = 'noopener noreferrer'; a.textContent = r.url || ''; tdUrl.appendChild(a); tr.appendChild(tdUrl)
    // original size (MB)
    const tdOrig = document.createElement('td'); tdOrig.className = 'mono'; tdOrig.textContent = (r.original_size_mb !== null && r.original_size_mb !== undefined) ? (String(r.original_size_mb) + ' MB') : '—'; tr.appendChild(tdOrig)
    // final size (MB)
    const tdFinal = document.createElement('td'); tdFinal.className = 'mono'; tdFinal.textContent = (r.final_size_mb !== null && r.final_size_mb !== undefined) ? (String(r.final_size_mb) + ' MB') : '—'; tr.appendChild(tdFinal)
    // compressed flag
    const tdComp = document.createElement('td'); tdComp.textContent = (r.compressed === true) ? 'yes' : (r.compressed === false ? 'no' : '—'); tr.appendChild(tdComp)
    const tdStatus = document.createElement('td'); const span = document.createElement('span'); span.className = 'badge ' + statusClass; span.textContent = r.status || ''; tdStatus.appendChild(span); tr.appendChild(tdStatus)
    const tdProc = document.createElement('td'); tdProc.textContent = proc; tr.appendChild(tdProc)
    const tdCreated = document.createElement('td'); tdCreated.textContent = r.created_at || ''; tr.appendChild(tdCreated)
    if (eventsSummary) tr.title = eventsSummary
    requestsTable.appendChild(tr)
  }
  // render pagination using server total
  const totalPages = Math.max(1, Math.ceil(total / limit))
  paginationEl.innerHTML = ''
  const makeLi = (text, disabled, cb) => {
    const li = document.createElement('li')
    li.className = 'page-item' + (disabled ? ' disabled' : '')
    const a = document.createElement('a')
    a.className = 'page-link'
    a.href = '#'
    a.textContent = text
    a.onclick = (e) => { e.preventDefault(); if (!disabled) cb(); }
    li.appendChild(a)
    return li
  }
  paginationEl.appendChild(makeLi('«', page === 1, () => { currentPage = 1; renderTablePage(1) }))
  paginationEl.appendChild(makeLi('‹', page === 1, () => { currentPage = Math.max(1, page - 1); renderTablePage(currentPage) }))
  const startPage = Math.max(1, page - 2)
  const endPage = Math.min(totalPages, startPage + 4)
  for (let p = startPage; p <= endPage; p++) {
    const li = makeLi(String(p), false, () => { currentPage = p; renderTablePage(p) })
    if (p === page) li.classList.add('active')
    paginationEl.appendChild(li)
  }
  paginationEl.appendChild(makeLi('›', page === totalPages, () => { currentPage = Math.min(totalPages, page + 1); renderTablePage(currentPage) }))
  paginationEl.appendChild(makeLi('»', page === totalPages, () => { currentPage = totalPages; renderTablePage(totalPages) }))
}

async function loadStats() {
  try {
    const resp = await fetchWithCreds('/stats')
    if (!resp.ok) return
    const s = await resp.json()
    document.getElementById('statAvgOrig').textContent = s.avg_original_size_mb ? (s.avg_original_size_mb + ' MB') : '—'
    document.getElementById('statAvgFinal').textContent = s.avg_final_size_mb ? (s.avg_final_size_mb + ' MB') : '—'
    document.getElementById('statAvgProc').textContent = s.avg_processing_seconds ? (s.avg_processing_seconds.toFixed(1) + ' s') : '—'
    try { document.getElementById('statTotalDownloaded').textContent = s.total_downloaded !== undefined ? String(s.total_downloaded) : '—' } catch(e){}
    try { document.getElementById('statTotalSent').textContent = s.total_sent !== undefined ? String(s.total_sent) : '—' } catch(e){}
    try { document.getElementById('statTotalDownloadedMB').textContent = s.total_original_mb !== undefined ? (String(s.total_original_mb) + ' MB') : '—' } catch(e){}
    try { document.getElementById('statTotalUploadedMB').textContent = s.total_final_mb !== undefined ? (String(s.total_final_mb) + ' MB') : '—' } catch(e){}
    try { document.getElementById('statTotalProcessedMB').textContent = s.total_processed_mb !== undefined ? (String(s.total_processed_mb) + ' MB') : '—' } catch(e){}
    try { document.getElementById('statTotalRateLimited').textContent = s.total_rate_limited_files !== undefined ? String(s.total_rate_limited_files) : '—' } catch(e){}
    try { document.getElementById('statTotalChatsLimited').textContent = s.total_chats_rate_limited !== undefined ? String(s.total_chats_rate_limited) : '—' } catch(e){}
    try { document.getElementById('statTotalDuplicates').textContent = s.total_duplicates !== undefined ? String(s.total_duplicates) : '—' } catch(e){}
  } catch (e) {
    // ignore
  }
}

// search events
let searchTimeout = null
searchInput.addEventListener('input', () => { currentPage = 1; clearTimeout(searchTimeout); searchTimeout = setTimeout(() => renderTablePage(1), 250) })
pageSizeSelect.addEventListener('change', () => { currentPage = 1; renderTablePage(1) })
if (statusFilterSelect) statusFilterSelect.addEventListener('change', () => { currentPage = 1; renderTablePage(1) })

// display login if oauth configured
async function initAuth() {
  try {
    const cfg = await (await fetchWithCreds('/config')).json()
    const me = await (await fetchWithCreds('/api/me')).json()
    const main = document.getElementById('mainContainer')
    const loginBox = document.getElementById('loginBox')
    const notAdminBox = document.getElementById('notAdminBox')
    // clear previous controls
    authControls.innerHTML = ''

    function isAuthenticated(meObj) {
      if (!meObj) return false
      if (meObj.is_admin) return true
      if (!meObj.user) return false
      const u = meObj.user
      return Boolean(u.email || u.preferred_username || u.sub || u.name)
    }

    if (cfg.oauth_configured || cfg.admin_token_set) {
      // not logged-in -> show login box
        if (!isAuthenticated(me)) {
        if (main) main.style.display = 'none'
        if (notAdminBox) notAdminBox.style.display = 'none'
        if (loginBox) loginBox.style.display = 'block'
        const big = document.getElementById('loginBtnLarge')
        const loginInline = document.getElementById('loginInline')
        if (cfg.oauth_configured) {
          // show OAuth button
          if (loginInline) loginInline.style.display = 'none'
          if (big) { big.style.display = ''; big.href = '/login'; big.textContent = 'Login with OAuth' }
        } else if (cfg.admin_token_set) {
          // show inline token form for SPA login
          if (big) big.style.display = 'none'
          if (loginInline) loginInline.style.display = ''
          if (!loginInlineInit) {
            loginInlineInit = true
            const tokenInput = document.getElementById('adminTokenInput')
            const pasteBtn = document.getElementById('adminTokenPaste')
            const toggleBtn = document.getElementById('adminTokenToggle')
            const submitBtn = document.getElementById('adminTokenSubmit')
            const errEl = document.getElementById('adminTokenError')
            if (pasteBtn) pasteBtn.onclick = async () => {
              try {
                const text = await navigator.clipboard.readText()
                if (text) tokenInput.value = text.trim()
              } catch (e) { tokenInput.focus() }
            }
            if (toggleBtn) toggleBtn.onclick = () => {
              if (tokenInput.type === 'password') { tokenInput.type = 'text'; toggleBtn.textContent = '🔒' } else { tokenInput.type = 'password'; toggleBtn.textContent = '👁' }
            }
            async function submitToken() {
              if (!tokenInput.value || !tokenInput.value.trim()) {
                errEl.style.display = ''
                errEl.textContent = 'Inserisci il token'
                return
              }
              submitBtn.disabled = true
              errEl.style.display = 'none'
              try {
                const body = new URLSearchParams({ token: tokenInput.value.trim() })
                const r = await fetchWithCreds('/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body })
                if (r.status === 403) {
                  errEl.style.display = ''
                  errEl.textContent = 'Token non valido'
                  submitBtn.disabled = false
                } else {
                  location.reload()
                }
              } catch (e) {
                errEl.style.display = ''
                errEl.textContent = 'Errore di rete'
                submitBtn.disabled = false
              }
            }
            if (submitBtn) submitBtn.onclick = submitToken
            if (tokenInput) tokenInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitToken() })
          }
        } else {
          if (loginInline) loginInline.style.display = 'none'
          if (big) { big.style.display = ''; big.href = '/login'; big.textContent = 'Login' }
        }
        return
      }

      // authenticated
      if (me.is_admin) {
        // admin -> show main UI
        if (main) main.style.display = ''
        if (loginBox) loginBox.style.display = 'none'
        if (notAdminBox) notAdminBox.style.display = 'none'

        // refresh button
        const refreshBtn = document.createElement('button')
        refreshBtn.className = 'btn btn-sm btn-outline-secondary ms-2'
        refreshBtn.textContent = 'Aggiorna'
        refreshBtn.onclick = () => { loadAll() }
        authControls.appendChild(refreshBtn)

        // clear DB button (admin only)
        const clearDbBtn = document.createElement('button')
        clearDbBtn.className = 'btn btn-sm btn-outline-danger ms-2'
        clearDbBtn.textContent = 'Pulisci DB'
        clearDbBtn.title = 'Elimina lo storico delle richieste (solo per amministratori)'
        clearDbBtn.onclick = async () => {
          if (!confirm('Sei sicuro? Questa operazione eliminerà lo storico delle richieste.')) return
          clearDbBtn.disabled = true
          try {
            const r = await fetchWithCreds('/api/db/clear', { method: 'POST' })
            if (r.ok) {
              alert('Storico cancellato')
              loadAll()
            } else {
              alert('Errore durante la cancellazione')
            }
          } catch (e) {
            alert('Errore di rete')
          } finally {
            clearDbBtn.disabled = false
          }
        }
        authControls.appendChild(clearDbBtn)

        const btn = document.createElement('a')
        btn.className = 'btn btn-sm btn-outline-primary ms-2'
        let display = ''
        if (me.user) {
          display = me.user.name || me.user.preferred_username || me.user.email || (me.user.sub || '')
        }
        btn.href = '/logout'
        btn.textContent = display ? ('Logout ' + display) : 'Logout'
        authControls.appendChild(btn)

        const span = document.createElement('span')
        span.className = 'ms-2 small text-muted'
        span.textContent = 'Admin'
        authControls.appendChild(span)
        return
      }

      // authenticated but NOT admin -> show centered not-admin box
      if (main) main.style.display = 'none'
      if (loginBox) loginBox.style.display = 'none'
      if (notAdminBox) notAdminBox.style.display = 'block'

      let display = ''
      if (me.user) {
        display = me.user.name || me.user.preferred_username || me.user.email || (me.user.sub || '')
      }
      const dispEl = document.getElementById('notAdminDisplay')
      if (dispEl) dispEl.textContent = `Autenticato come ${display || '<unknown>'} — non sei amministratore.`

      const grantBtn = document.getElementById('notAdminGrantBtn')
      if (grantBtn) {
        grantBtn.onclick = async () => {
          grantBtn.disabled = true
          try {
            const r = await fetchWithCreds('/api/session/grant_admin', { method: 'POST' })
            if (r.ok) {
              location.reload()
            } else if (r.status === 403) {
              alert('Non puoi ottenere i privilegi: account non idoneo.')
            } else {
              alert('Errore server durante la richiesta di privilegi')
            }
          } catch (err) {
            alert('Impossibile contattare il server')
          } finally {
            grantBtn.disabled = false
          }
        }
      }
    }
  } catch (e) {
    // ignore errors — keep UI usable as read-only
  }
}

// WebSocket for realtime updates with polling fallback
let ws = null
function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws/updates`
}

function setupWs() {
  try {
    ws = new WebSocket(wsUrl())
  } catch (e) {
    console.debug('WebSocket not available', e)
    return
  }
  ws.onopen = () => { console.debug('ws open') }
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'initial') {
        if (msg.requests) {
          currentPage = 1
          renderTablePage(1)
        }
        if (msg.updates) loadUpdates()
      } else if (msg.type === 'initial_updates') {
        // server may send initial_updates separately
        try { loadUpdates() } catch (e) { }
      } else if (msg.type === 'request_created' || msg.type === 'request_started' || msg.type === 'request_finished' || msg.type === 'request_event') {
        // refresh current page on request lifecycle changes
        renderTablePage(currentPage)
        try { loadQueue() } catch (e) {}
      } else if (msg.type === 'request_status' || msg.type === 'request_updated') {
        // more generic request update (status or metadata) -> refresh table and queue
        try { renderTablePage(currentPage) } catch (e) {}
        try { loadQueue() } catch (e) {}
      } else if (msg.type === 'queue_cleared') {
        // minimal refresh to avoid triggering broad reload loops
        try { loadQueue() } catch (e) {}
        try { loadRateLimits() } catch (e) {}
        try { renderTablePage(currentPage) } catch (e) {}
      } else if (msg.type === 'unlimited') {
        // a chat was unlimited by admin; refresh rate limits and queue
        try { loadRateLimits() } catch (e) {}
        try { loadQueue() } catch (e) {}
      } else if (msg.type === 'rate_limit_changed') {
        // worker in-memory rate limit changed
        try { loadRateLimits() } catch (e) {}
        try { loadQueue() } catch (e) {}
      } else if (msg.type === 'queue_rehydrated') {
        // worker rehydrated persisted queue on startup
        try { loadQueue() } catch (e) {}
        try { loadRateLimits() } catch (e) {}
        try { renderTablePage(currentPage) } catch (e) {}
      } else if (msg.type === 'update_created') {
        loadUpdates()
      }
    } catch (e) {
      console.debug('ws message parse error', e)
    }
  }
  ws.onclose = () => { console.debug('ws closed, will fallback to polling') }
  ws.onerror = (e) => { console.debug('ws error', e) }
}

// Run auth initialization first, then start WS and load data.
initAuth().finally(() => {
  try { setupWs() } catch (e) { /* ignore */ }
  try { loadAll() } catch (e) { /* ignore */ }
})
// fallback polling every 10s if ws not available or closed
setInterval(async () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    await loadAll()
  }
}, 10000)
// sidebar refresh button
try {
  const refreshBtnSidebar = document.getElementById('refreshBtnSidebar')
  if (refreshBtnSidebar) refreshBtnSidebar.onclick = () => { loadAll() }
  if (refreshQueueBtn) refreshQueueBtn.onclick = () => { loadQueue() }
  const refreshRateLimitsBtn = document.getElementById('refreshRateLimitsBtn')
  if (refreshRateLimitsBtn) refreshRateLimitsBtn.onclick = () => { loadRateLimits() }
    if (startQueueBtn) startQueueBtn.onclick = async () => {
      try {
        startQueueBtn.disabled = true
        startQueueBtn.textContent = 'Avviando…'
        await fetchWithCreds('/api/queue/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rehydrate: true }) })
        await loadQueue(); await loadRateLimits()
      } catch (e) {
        console.debug('startQueue failed', e)
        alert('Errore avviando la coda')
      } finally {
        startQueueBtn.disabled = false
        startQueueBtn.textContent = 'Avvia coda'
      }
    }
    if (clearQueueBtn) clearQueueBtn.onclick = async () => { if (confirm('Svuotare la coda? Questa operazione cancella le richieste in coda.')) { await clearQueue(); } }
    if (showConfigBtn) showConfigBtn.onclick = async () => { await showConfig() }
} catch (e) { /* ignore */ }
