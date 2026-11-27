/*
 * Admin console of social-media-reuploader.
 *
 * - No inline handlers and no innerHTML with data: every node is built with
 *   DOM APIs and text goes through textContent. Links are rendered only for
 *   http(s) URLs, with rel="noopener noreferrer".
 * - Data comes from the JSON API (relative URLs, cookie session). 401 shows
 *   the login overlay, 403 the "not an administrator" overlay; other errors
 *   are shown as toasts.
 * - Live updates: one WebSocket (event-log batches from the server), with
 *   exponential backoff + jitter on reconnect. Every connection, the first
 *   one included, ends with a full resync when the server's "hello" arrives
 *   (it is sent once the subscription exists), so changes made before the
 *   subscription are never missed.
 *   Bursts of events are coalesced: each resource is refreshed at most once
 *   per short window, one request in flight per resource, and sequence guards
 *   drop stale responses.
 * - Timestamps from the server are ISO 8601 with an offset; they are shown in
 *   the browser's local time.
 */
(function () {
  'use strict'

  // ---------------------------------------------------------------------
  // Labels (Italian UI)
  // ---------------------------------------------------------------------
  const STATUS_META = {
    queued: { label: 'In coda', badge: 'text-bg-warning', color: '#f0ad4e' },
    deferred: { label: 'Differita', badge: 'text-bg-info', color: '#5bc0de' },
    processing: { label: 'In elaborazione', badge: 'text-bg-primary', color: '#0d6efd' },
    sending: { label: 'In invio', badge: 'text-bg-primary', color: '#6610f2' },
    done: { label: 'Completata', badge: 'text-bg-success', color: '#198754' },
    failed: { label: 'Fallita', badge: 'text-bg-danger', color: '#dc3545' },
    rate_limited: { label: 'Rifiutata (limite)', badge: 'text-bg-warning', color: '#fd7e14' },
    duplicate: { label: 'Duplicato', badge: 'text-bg-secondary', color: '#6c757d' },
    cancelled: { label: 'Annullata', badge: 'text-bg-dark', color: '#343a40' },
    aborted: { label: 'Interrotta', badge: 'text-bg-dark', color: '#868e96' },
    pending: { label: 'In attesa (versione precedente)', badge: 'text-bg-light', color: '#adb5bd' },
    unlimited: { label: 'Sbloccata (versione precedente)', badge: 'text-bg-light', color: '#ced4da' },
    unknown: { label: 'Sconosciuto', badge: 'text-bg-light', color: '#dee2e6' }
  }
  const REQUEUEABLE = ['failed', 'rate_limited', 'cancelled', 'aborted', 'duplicate', 'pending', 'unlimited']
  const ERROR_LABELS = {
    failed: 'Errore generico',
    unsupported: 'Link non supportato',
    blocked: 'Sito disattivato dall\'amministratore',
    auth_required: 'Accesso o cookie richiesti',
    unavailable: 'Contenuto non disponibile',
    geo_blocked: 'Bloccato nel paese del server',
    live: 'Diretta in corso',
    too_long: 'Contenuto troppo lungo',
    too_large: 'File troppo grande',
    no_media: 'Nessun video o audio',
    timeout: 'Tempo scaduto',
    conversion_failed: 'Conversione fallita',
    conversion_timeout: 'Conversione troppo lenta',
    rate_limited: 'Troppi link in poco tempo',
    pending_cap: 'Troppe richieste in attesa',
    duplicate: 'Link già inviato di recente',
    interrupted: 'Interrotta da un riavvio',
    interrupted_while_sending: 'Interrotta durante l\'invio',
    send_failed: 'Invio a Telegram non riuscito',
    forbidden: 'Il bot non può scrivere nella chat',
    invalid_request: 'Richiesta non valida (chat o link mancante)'
  }
  // What a running job is doing (pipeline stages in the worker heartbeat).
  const STAGE_LABELS = {
    starting: 'avvio',
    extracting: 'analisi del link',
    server_fetch: 'download da parte di Telegram',
    downloading: 'download',
    converting: 'conversione',
    sending: 'invio a Telegram',
    finishing: 'completamento'
  }
  // Request history entries that are not statuses (the others use STATUS_META).
  const HISTORY_LABELS = {
    requeued: 'Rimessa in coda',
    interrupted: 'Interrotta da un riavvio'
  }
  const EVENT_LABELS = {
    request_created: 'Richiesta creata',
    request_status: 'Cambio di stato',
    request_updated: 'Richiesta aggiornata',
    request_event: 'Evento della richiesta',
    queue_cleared: 'Coda annullata',
    rate_limit_changed: 'Limite della chat',
    chat_unlimited: 'Chat sbloccata',
    chat_unlimited_all: 'Tutte le chat sbloccate',
    history_cleared: 'Storico eliminato',
    history_pruned: 'Pulizia automatica', // retention (UPDATES/REQUESTS_RETENTION_DAYS)
    gui_login: 'Accesso alla console',
    gui_logout: 'Uscita dalla console',
    gui_sessions_revoked: 'Sessioni terminate',
    update_created: 'Riga non strutturata'
  }
  const FIELD_LABELS = {
    original_size: 'dimensione originale',
    final_size: 'dimensione finale',
    compressed: 'compressione',
    provider: 'sito',
    media_type: 'tipo di media',
    error_code: 'codice errore',
    error_message: 'messaggio di errore',
    sent_message_id: 'messaggio inviato'
  }
  const LIMIT_SOURCES = {
    worker: 'riportati dal worker',
    worker_last_seen: 'ultimo heartbeat del worker',
    gui_config: 'configurazione della console (worker non disponibile)'
  }

  // ---------------------------------------------------------------------
  // DOM helpers
  // ---------------------------------------------------------------------
  const $ = (id) => document.getElementById(id)

  function el (tag, opts, ...children) {
    const node = document.createElement(tag)
    const o = opts || {}
    if (o.className) node.className = o.className
    if (o.text !== undefined && o.text !== null) node.textContent = String(o.text)
    if (o.title) node.title = String(o.title)
    if (o.type) node.type = o.type
    if (o.attrs) {
      for (const [key, value] of Object.entries(o.attrs)) node.setAttribute(key, String(value))
    }
    if (o.onClick) node.addEventListener('click', o.onClick)
    for (const child of children) {
      if (child !== null && child !== undefined && child !== false) node.append(child)
    }
    return node
  }

  function button (label, className, onClick, title) {
    return el('button', { className: 'btn btn-sm ' + className, text: label, type: 'button', title, onClick })
  }

  function safeLink (url, text) {
    if (typeof url === 'string' && url) {
      try {
        const parsed = new URL(url)
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
          const a = el('a', { text: text || url, className: 'text-truncate-link', title: url })
          a.href = parsed.href
          a.target = '_blank'
          a.rel = 'noopener noreferrer'
          return a
        }
      } catch (e) { /* not a URL: plain text below */ }
    }
    return el('span', { text: text || url || '—', className: 'text-truncate-link', title: url || '' })
  }

  function emptyRow (columns, text) {
    return el('tr', null, el('td', { className: 'empty-note', text, attrs: { colspan: columns } }))
  }

  function emptyItem (text) {
    return el('li', { className: 'list-group-item empty-note', text })
  }

  function definition (dl, label, value) {
    dl.append(el('dt', { className: 'col-sm-5', text: label }))
    const dd = el('dd', { className: 'col-sm-7' })
    if (value instanceof Node) dd.append(value)
    else dd.textContent = value === null || value === undefined || value === '' ? '—' : String(value)
    dl.append(dd)
  }

  // ---------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------
  const dateFmt = new Intl.DateTimeFormat('it-IT', { dateStyle: 'short', timeStyle: 'medium' })
  const hourFmt = new Intl.DateTimeFormat('it-IT', { hour: '2-digit', minute: '2-digit' })
  const relFmt = new Intl.RelativeTimeFormat('it', { numeric: 'auto' })
  const numFmt = new Intl.NumberFormat('it-IT')
  const pctFmt = new Intl.NumberFormat('it-IT', { style: 'percent', maximumFractionDigits: 1 })

  function parseDate (iso) {
    if (!iso) return null
    const d = new Date(iso)
    return Number.isNaN(d.getTime()) ? null : d
  }

  function fmtDate (iso) {
    const d = parseDate(iso)
    return d ? dateFmt.format(d) : '—'
  }

  function fmtAgo (iso) {
    const d = parseDate(iso)
    if (!d) return '—'
    const seconds = Math.round((d.getTime() - Date.now()) / 1000)
    const abs = Math.abs(seconds)
    if (abs < 45) return relFmt.format(seconds, 'second')
    if (abs < 45 * 60) return relFmt.format(Math.round(seconds / 60), 'minute')
    if (abs < 22 * 3600) return relFmt.format(Math.round(seconds / 3600), 'hour')
    return relFmt.format(Math.round(seconds / 86400), 'day')
  }

  // A <time> element whose relative text is refreshed periodically.
  function timeAgo (iso, prefix) {
    const node = el('time', { text: (prefix || '') + fmtAgo(iso), title: fmtDate(iso) })
    if (iso) {
      node.dateTime = iso
      node.dataset.ago = iso
      if (prefix) node.dataset.prefix = prefix
    }
    return node
  }

  function refreshRelativeTimes () {
    for (const node of document.querySelectorAll('time[data-ago]')) {
      node.textContent = (node.dataset.prefix || '') + fmtAgo(node.dataset.ago)
    }
  }

  function fmtNumber (n) {
    return n === null || n === undefined ? '—' : numFmt.format(n)
  }

  function fmtBytes (n) {
    if (n === null || n === undefined) return '—'
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    let value = Number(n)
    let unit = 0
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024
      unit += 1
    }
    const digits = unit === 0 || value >= 100 ? 0 : 1
    return value.toLocaleString('it-IT', { maximumFractionDigits: digits }) + ' ' + units[unit]
  }

  function fmtSeconds (seconds) {
    if (seconds === null || seconds === undefined) return '—'
    const s = Number(seconds)
    if (s < 60) return s.toLocaleString('it-IT', { maximumFractionDigits: s < 10 ? 1 : 0 }) + ' s'
    const minutes = Math.floor(s / 60)
    if (minutes < 60) return minutes + ' min ' + Math.round(s % 60) + ' s'
    const hours = Math.floor(minutes / 60)
    if (hours < 48) return hours + ' h ' + (minutes % 60) + ' min'
    return Math.round(hours / 24) + ' giorni'
  }

  function fmtWindow (seconds) {
    const s = Number(seconds)
    const units = [[86400, 'giorno', 'giorni'], [3600, 'ora', 'ore'], [60, 'minuto', 'minuti'], [1, 'secondo', 'secondi']]
    for (const [size, one, many] of units) {
      if (s >= size && s % size === 0) {
        const n = s / size
        return n + ' ' + (n === 1 ? one : many)
      }
    }
    return s + ' secondi'
  }

  function statusMeta (status) {
    return STATUS_META[status] || { label: status || '—', badge: 'text-bg-light', color: '#dee2e6' }
  }

  function statusBadge (item) {
    const key = item.deferred ? 'deferred' : item.status
    const meta = statusMeta(key)
    const title = item.deferred && item.not_before ? 'Differita fino a ' + fmtDate(item.not_before) : (item.status || '')
    return el('span', { className: 'badge ' + meta.badge, text: meta.label, title })
  }

  function errorLabel (code) {
    if (!code) return ''
    return ERROR_LABELS[code] || code
  }

  function stageLabel (stage) {
    return STAGE_LABELS[stage] || stage
  }

  function historyLabel (type) {
    return HISTORY_LABELS[type] || (STATUS_META[type] && STATUS_META[type].label) || type || ''
  }

  function chatLabel (chatId, chatType) {
    const types = { private: 'privata', group: 'gruppo', supergroup: 'gruppo', channel: 'canale' }
    const suffix = chatType && types[chatType] ? ' (' + types[chatType] + ')' : ''
    return chatId === null || chatId === undefined ? '—' : String(chatId) + suffix
  }

  // ---------------------------------------------------------------------
  // Toasts
  // ---------------------------------------------------------------------
  const recentToasts = new Map()

  function toast (message, kind) {
    const level = kind || 'info'
    const now = Date.now()
    const last = recentToasts.get(message)
    if (last && now - last < 10000) return
    recentToasts.set(message, now)
    const container = $('toasts')
    const close = el('button', { className: 'btn-close btn-close-white me-2 m-auto', type: 'button', attrs: { 'aria-label': 'Chiudi' } })
    const node = el('div', {
      className: 'toast show align-items-center border-0 text-bg-' + level,
      attrs: { role: level === 'danger' ? 'alert' : 'status' }
    }, el('div', { className: 'd-flex' }, el('div', { className: 'toast-body', text: message }), close))
    close.addEventListener('click', () => node.remove())
    container.append(node)
    while (container.children.length > 5) container.firstElementChild.remove()
    setTimeout(() => node.remove(), level === 'danger' ? 9000 : 5000)
  }

  // ---------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------
  class ApiError extends Error {
    constructor (status, detail) {
      super(detail)
      this.status = status
      this.detail = detail
    }
  }

  function detailText (data, status) {
    if (data && typeof data.detail === 'string') return data.detail
    if (data && Array.isArray(data.detail)) {
      return data.detail.map((d) => {
        const where = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body' && p !== 'query').join('.') : ''
        return (where ? where + ': ' : '') + (d.msg || 'valore non valido')
      }).join('; ')
    }
    if (status === 0) return 'Server non raggiungibile'
    return 'Errore del server (HTTP ' + status + ')'
  }

  async function api (path, options) {
    const o = options || {}
    const init = { method: o.method || 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' } }
    if (o.body !== undefined) {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(o.body)
    }
    let response
    try {
      response = await fetch(path, init)
    } catch (e) {
      const error = new ApiError(0, 'Server non raggiungibile')
      if (!o.quiet) toast(error.detail, 'danger')
      throw error
    }
    let data = null
    const text = await response.text()
    if (text) {
      try { data = JSON.parse(text) } catch (e) { data = null }
    }
    if (response.ok) return data
    const error = new ApiError(response.status, detailText(data, response.status))
    if (o.authHandling !== false && response.status === 401) {
      showLogin()
    } else if (o.authHandling !== false && response.status === 403) {
      recheckAccess(error)
    } else if (!o.quiet) {
      toast(error.detail, 'danger')
    }
    throw error
  }

  function query (params) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') search.set(key, String(value))
    }
    const text = search.toString()
    return text ? '?' + text : ''
  }

  // ---------------------------------------------------------------------
  // Modal dialogs
  // ---------------------------------------------------------------------
  let modalResolve = null
  let modalActions = ''

  // Footer buttons: each one closes the modal with its value.
  function renderModalButtons (buttons) {
    const footer = $('modalFooter')
    footer.replaceChildren()
    for (const spec of buttons) {
      footer.append(button(spec.label, spec.className || 'btn-secondary', () => closeModal(spec.value)))
    }
    modalActions = buttons.map((spec) => spec.value).join('|')
    return footer
  }

  function openModal (title, body, buttons, onOpen) {
    closeModal(null)
    $('modalTitle').textContent = title
    const bodyEl = $('modalBody')
    bodyEl.replaceChildren()
    if (typeof body === 'string') bodyEl.append(el('p', { className: 'mb-0 pre-wrap', text: body }))
    else if (body) bodyEl.append(body)
    const promise = new Promise((resolve) => { modalResolve = resolve })
    const footer = renderModalButtons(buttons)
    $('modal').hidden = false
    const focusTarget = footer.querySelector('button:last-child') || $('modalClose')
    focusTarget.focus()
    if (onOpen) onOpen()
    return promise
  }

  function closeModal (value) {
    const resolve = modalResolve
    modalResolve = null
    detailOpenId = null
    $('modal').hidden = true
    if (resolve) resolve(value)
  }

  function confirmDialog (title, message, confirmLabel, danger) {
    return openModal(title, message, [
      { label: 'Annulla', value: false, className: 'btn-outline-secondary' },
      { label: confirmLabel, value: true, className: danger ? 'btn-danger' : 'btn-primary' }
    ])
  }

  // ---------------------------------------------------------------------
  // Refresh scheduling: single flight per resource, coalesced bursts,
  // sequence guards against stale responses.
  // ---------------------------------------------------------------------
  const STALE = Symbol('stale')
  const sequences = new Map()

  async function latest (key, fn) {
    const mine = (sequences.get(key) || 0) + 1
    sequences.set(key, mine)
    const result = await fn()
    return sequences.get(key) === mine ? result : STALE
  }

  const PANEL_RESOURCES = {
    overview: ['stats', 'aggregates'],
    requests: ['requests'],
    queue: ['queue'],
    limits: ['limits'],
    events: ['events'],
    users: ['users'],
    system: ['system']
  }
  const ALL_RESOURCES = ['worker', 'detail', ...new Set(Object.values(PANEL_RESOURCES).flat())]
  const loaders = {}
  const dirty = new Set()
  const inFlight = new Map()
  const rerun = new Set()
  let flushTimer = null
  let currentTab = 'overview'

  function activeResources () {
    const names = new Set(['worker', ...(PANEL_RESOURCES[currentTab] || [])])
    if (detailOpenId !== null) names.add('detail')
    return names
  }

  function invalidate (names) {
    for (const name of names) dirty.add(name)
    if (!flushTimer) flushTimer = setTimeout(flush, 400)
  }

  function flush () {
    flushTimer = null
    if (!authed) return
    const active = activeResources()
    for (const name of [...dirty]) {
      if (active.has(name)) {
        dirty.delete(name)
        runLoader(name)
      }
    }
  }

  function runLoader (name, force) {
    const loader = loaders[name]
    if (!loader || !authed) return Promise.resolve()
    if (inFlight.has(name) && !force) {
      rerun.add(name)
      return inFlight.get(name)
    }
    const task = (async () => {
      try {
        await loader()
      } catch (e) {
        if (!(e instanceof ApiError)) {
          console.error('Loader ' + name + ' failed', e)
          toast('Errore durante l\'aggiornamento della pagina', 'danger')
        }
      } finally {
        if (inFlight.get(name) === task) inFlight.delete(name)
        if (rerun.delete(name)) runLoader(name)
      }
    })()
    inFlight.set(name, task)
    return task
  }

  function resyncAll () {
    for (const name of ALL_RESOURCES) dirty.add(name)
    flush()
  }

  // ---------------------------------------------------------------------
  // Authentication flow
  // ---------------------------------------------------------------------
  let authed = false
  let me = null
  let publicConfig = null

  function showOnly (id) {
    for (const name of ['boot', 'app', 'loginOverlay', 'notAdminOverlay']) {
      $(name).hidden = name !== id
    }
  }

  async function initAuth () {
    try {
      const results = await Promise.all([
        api('config', { quiet: true, authHandling: false }),
        api('api/me', { quiet: true, authHandling: false })
      ])
      publicConfig = results[0]
      me = results[1]
    } catch (e) {
      showOnly('boot')
      $('boot').replaceChildren(el('div', { className: 'alert alert-danger', text: 'Impossibile contattare il server della console. Riprova più tardi.' }))
      setTimeout(initAuth, 10000)
      return
    }
    if (me && me.is_admin) startApp()
    else if (me && me.authenticated) showNotAdmin()
    else showLogin()
  }

  function stopApp () {
    authed = false
    wsWanted = false
    clearTimeout(wsTimer)
    if (ws) {
      const socket = ws
      ws = null
      socket.close()
    }
    closeModal(null)
  }

  function showLogin () {
    stopApp()
    showOnly('loginOverlay')
    const cfg = publicConfig || {}
    $('oauthLoginBtn').hidden = !cfg.oauth_configured
    $('tokenForm').hidden = !cfg.admin_token_set
    $('noLoginMethod').hidden = Boolean(cfg.oauth_configured || cfg.admin_token_set)
    $('tokenError').hidden = true
    if (cfg.admin_token_set) $('tokenInput').focus()
  }

  function displayName (user) {
    if (!user) return ''
    return user.name || user.username || user.email || user.sub || ''
  }

  function showNotAdmin () {
    stopApp()
    showOnly('notAdminOverlay')
    const name = displayName(me && me.user)
    $('notAdminText').textContent = name
      ? 'Sei autenticato come ' + name + ' ma non sei amministratore.'
      : 'Sei autenticato ma non sei amministratore.'
  }

  // A 403 can be a lost privilege or a refused action: ask the server.
  async function recheckAccess (error) {
    try {
      me = await api('api/me', { quiet: true, authHandling: false })
    } catch (e) {
      return
    }
    if (me && me.is_admin) toast(error.detail, 'danger')
    else if (me && me.authenticated) showNotAdmin()
    else showLogin()
  }

  function startApp () {
    authed = true
    showOnly('app')
    const name = displayName(me && me.user)
    $('userLabel').textContent = name ? 'Accesso: ' + name : ''
    activateTab(tabFromHash(), true)
    resyncAll()
    wsWanted = true
    connectWs()
  }

  async function submitToken (event) {
    event.preventDefault()
    const input = $('tokenInput')
    const errorEl = $('tokenError')
    const token = input.value.trim()
    if (!token) {
      errorEl.textContent = 'Inserisci il token'
      errorEl.hidden = false
      return
    }
    $('tokenSubmit').disabled = true
    errorEl.hidden = true
    try {
      await api('login', { method: 'POST', body: { token }, quiet: true, authHandling: false })
      input.value = ''
      me = null
      await initAuth()
      if (me && !me.authenticated) {
        // The token was right, but the next request carried no session cookie.
        errorEl.textContent = 'Token corretto, ma il browser non ha salvato il cookie di sessione. ' +
          'Con SESSION_COOKIE_SECURE=true la console va aperta in HTTPS; controlla anche che il browser accetti i cookie di questo sito.'
        errorEl.hidden = false
      }
    } catch (e) {
      errorEl.textContent = e instanceof ApiError ? e.detail : 'Errore imprevisto'
      errorEl.hidden = false
    } finally {
      $('tokenSubmit').disabled = false
    }
  }

  async function logout () {
    try {
      await api('logout', { method: 'POST', body: {}, quiet: true, authHandling: false })
    } catch (e) {
      toast('Uscita non riuscita: ' + (e.detail || 'errore'), 'danger')
      return
    }
    me = null
    await initAuth()
  }

  // ---------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------
  function tabFromHash () {
    const name = (location.hash || '').replace('#', '')
    return PANEL_RESOURCES[name] ? name : 'overview'
  }

  function activateTab (name, initial) {
    currentTab = PANEL_RESOURCES[name] ? name : 'overview'
    for (const tab of document.querySelectorAll('[data-tab]')) {
      const active = tab.dataset.tab === currentTab
      tab.classList.toggle('active', active)
      tab.setAttribute('aria-selected', active ? 'true' : 'false')
    }
    for (const panel of document.querySelectorAll('[data-panel]')) {
      panel.hidden = panel.dataset.panel !== currentTab
    }
    if (!initial && location.hash !== '#' + currentTab) history.replaceState(null, '', '#' + currentTab)
    flush()
  }

  // ---------------------------------------------------------------------
  // WebSocket (live updates)
  // ---------------------------------------------------------------------
  let ws = null
  let wsWanted = false
  let wsTimer = null
  let wsAttempts = 0

  function wsUrl () {
    const url = new URL('ws/updates', location.href)
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    url.hash = ''
    return url.href
  }

  function setLive (state) {
    const badge = $('liveBadge')
    const states = {
      online: ['text-bg-success', 'In tempo reale', 'Aggiornamenti in tempo reale attivi'],
      connecting: ['text-bg-secondary', 'Connessione…', 'Connessione agli aggiornamenti in corso'],
      offline: ['text-bg-warning', 'Non in tempo reale', 'Connessione persa: aggiornamento periodico finché non si riconnette']
    }
    const [cls, text, title] = states[state] || states.offline
    badge.className = 'badge ' + cls
    badge.textContent = text
    badge.title = title
  }

  function connectWs () {
    if (!wsWanted) return
    clearTimeout(wsTimer)
    setLive('connecting')
    let socket
    try {
      socket = new WebSocket(wsUrl())
    } catch (e) {
      scheduleReconnect()
      return
    }
    ws = socket
    socket.addEventListener('open', () => {
      if (ws !== socket) return
      wsAttempts = 0
      setLive('online')
    })
    socket.addEventListener('message', (event) => {
      if (ws === socket) handleMessage(event.data)
    })
    socket.addEventListener('close', () => {
      if (ws !== socket) return
      ws = null
      setLive('offline')
      if (wsWanted) scheduleReconnect()
    })
  }

  function scheduleReconnect () {
    const base = Math.min(30000, 1000 * Math.pow(2, Math.min(wsAttempts, 5)))
    const delay = base / 2 + Math.random() * (base / 2)
    wsAttempts += 1
    clearTimeout(wsTimer)
    wsTimer = setTimeout(async () => {
      if (!wsWanted) return
      // A refused handshake looks like a network error: check the session.
      try {
        me = await api('api/me', { quiet: true, authHandling: false })
      } catch (e) {
        scheduleReconnect()
        return
      }
      if (!me || !me.is_admin) {
        if (me && me.authenticated) showNotAdmin()
        else showLogin()
        return
      }
      connectWs()
    }, delay)
  }

  function handleMessage (text) {
    let message
    try { message = JSON.parse(text) } catch (e) { return }
    if (!message || typeof message !== 'object') return
    // Events published before this subscription never reach it: reload.
    if (message.kind === 'hello') resyncAll()
    else if (message.kind === 'events') onEvents(Array.isArray(message.events) ? message.events : [])
    else if (message.kind === 'worker' && message.worker) renderWorker(message.worker)
  }

  function onEvents (events) {
    if (!events.length) return
    const names = new Set(['events'])
    for (const event of events) {
      switch (event.type) {
        case 'request_created':
        case 'request_status':
        case 'request_updated':
          ['requests', 'queue', 'stats', 'aggregates', 'limits'].forEach((n) => names.add(n))
          if (detailOpenId !== null && event.request_id === detailOpenId) names.add('detail')
          break
        case 'request_event':
          if (detailOpenId !== null && event.request_id === detailOpenId) names.add('detail')
          break
        case 'queue_cleared':
          ['requests', 'queue', 'stats', 'aggregates'].forEach((n) => names.add(n))
          break
        case 'rate_limit_changed':
        case 'chat_unlimited':
        case 'chat_unlimited_all':
          names.add('limits')
          names.add('stats')
          break
        case 'history_cleared':
        case 'history_pruned':
          ALL_RESOURCES.forEach((n) => names.add(n))
          break
        case 'gui_login':
        case 'gui_logout':
        case 'gui_sessions_revoked':
          names.add('users')
          break
        default:
          break
      }
    }
    invalidate(names)
  }

  // ---------------------------------------------------------------------
  // Worker status (header badge, alert, queue tab)
  // ---------------------------------------------------------------------
  let lastWorker = null

  loaders.worker = async () => {
    const worker = await latest('worker', () => api('api/worker'))
    if (worker !== STALE) renderWorker(worker)
  }

  function renderWorker (worker) {
    lastWorker = worker
    const badge = $('workerBadge')
    const alertEl = $('workerAlert')
    const heartbeat = worker.heartbeat_at ? 'ultimo segnale ' + fmtAgo(worker.heartbeat_at) : 'nessun segnale'
    const states = {
      online: ['text-bg-success', 'Worker attivo'],
      offline: ['text-bg-danger', 'Worker non raggiungibile'],
      stopped: ['text-bg-secondary', 'Worker fermo'],
      unknown: ['text-bg-secondary', 'Worker mai visto']
    }
    const [cls, text] = states[worker.state] || states.unknown
    badge.className = 'badge ' + cls
    badge.textContent = text
    badge.title = heartbeat
    alertEl.replaceChildren()
    if (worker.state === 'online') {
      alertEl.hidden = true
    } else if (worker.state === 'unknown') {
      alertEl.hidden = false
      alertEl.append('Nessun heartbeat del worker in questo database. Il bot è in esecuzione e usa lo stesso file REQUESTS_DB della console? Finché il worker non è attivo le richieste restano in coda.')
    } else {
      alertEl.hidden = false
      alertEl.append(
        (worker.state === 'stopped' ? 'Il worker è fermo' : 'Il worker non invia segnali') + ' (' + heartbeat + ', soglia ' +
        fmtSeconds(worker.stale_after_seconds) + '). Le richieste in coda resteranno ferme finché il bot non riparte; quelle in esecuzione verranno recuperate al riavvio.'
      )
    }
    if (currentTab === 'queue') renderWorkerInfo(worker)
  }

  function renderWorkerInfo (worker) {
    const dl = $('workerInfo')
    dl.replaceChildren()
    const states = { online: 'Attivo', offline: 'Non raggiungibile', stopped: 'Fermo', unknown: 'Mai visto' }
    definition(dl, 'Stato', states[worker.state] || worker.state)
    definition(dl, 'Ultimo heartbeat', worker.heartbeat_at ? timeAgo(worker.heartbeat_at) : null)
    definition(dl, 'Avviato', worker.started_at ? timeAgo(worker.started_at) : null)
    definition(dl, 'Versione', worker.version)
    definition(dl, 'PID', worker.pid)
    definition(dl, 'Modalità', worker.mode)
    definition(dl, 'Worker paralleli', worker.workers)
    definition(dl, 'Job in esecuzione', Array.isArray(worker.running) ? worker.running.length : null)
  }

  // ---------------------------------------------------------------------
  // Charts (created once, then updated in place)
  // ---------------------------------------------------------------------
  const charts = {}

  function chartsAvailable () {
    return typeof window.Chart === 'function'
  }

  function markChartUnavailable (canvasId) {
    const canvas = $(canvasId)
    if (!canvas || canvas.dataset.unavailable) return
    canvas.dataset.unavailable = '1'
    canvas.hidden = true
    canvas.parentElement.append(el('div', { className: 'chart-unavailable', text: 'Grafico non disponibile: impossibile caricare Chart.js dalla CDN.' }))
  }

  function setChart (key, canvasId, type, labels, datasets, options) {
    if (!chartsAvailable()) {
      markChartUnavailable(canvasId)
      return
    }
    const chart = charts[key]
    if (chart) {
      chart.data.labels = labels
      datasets.forEach((dataset, index) => {
        if (chart.data.datasets[index]) Object.assign(chart.data.datasets[index], dataset)
        else chart.data.datasets.push(dataset)
      })
      chart.data.datasets.length = datasets.length
      chart.update('none')
      return
    }
    charts[key] = new window.Chart($(canvasId), {
      type,
      data: { labels, datasets },
      options: Object.assign({ responsive: true, maintainAspectRatio: false, animation: false }, options || {})
    })
  }

  const legendBottom = { legend: { position: 'bottom', labels: { boxWidth: 12 } } }
  const noLegend = { legend: { display: false } }

  // ---------------------------------------------------------------------
  // Overview
  // ---------------------------------------------------------------------
  loaders.stats = async () => {
    const stats = await latest('stats', () => api('api/stats'))
    if (stats !== STALE) renderStats(stats)
  }

  function renderStats (s) {
    const t = s.totals
    const day = s.last_24h
    $('statTotal').textContent = fmtNumber(t.total)
    $('statTotalSub').textContent = 'ultime 24 h: ' + fmtNumber(day.total)
    $('statDone').textContent = fmtNumber(t.done)
    $('statDoneSub').textContent = s.success_rate === null ? 'nessuna richiesta conclusa' : 'successo: ' + pctFmt.format(s.success_rate)
    $('statFailed').textContent = fmtNumber(t.failed)
    $('statFailedSub').textContent = 'ultime 24 h: ' + fmtNumber(day.failed)
    $('statActive').textContent = fmtNumber(t.queued + t.deferred + t.running)
    $('statActiveSub').textContent = 'in coda ' + fmtNumber(t.queued) + ' · differite ' + fmtNumber(t.deferred) + ' · in corso ' + fmtNumber(t.running)
    $('statRefused').textContent = fmtNumber(t.rate_limited + t.duplicate)
    $('statRefusedSub').textContent = 'per limite ' + fmtNumber(t.rate_limited) + ' · duplicati ' + fmtNumber(t.duplicate)
    $('statLimited').textContent = fmtNumber(s.limited_chats)
    $('statBytes').textContent = fmtBytes(s.bytes.downloaded)
    $('statBytesSub').textContent = 'inviati a Telegram: ' + fmtBytes(s.bytes.uploaded) + ' · compressi: ' + fmtNumber(s.compressed)
    $('statAvg').textContent = fmtSeconds(s.averages.processing_seconds)
    const avg = s.averages
    $('statAvgSub').textContent = avg.original_size === null
      ? 'nessuna richiesta completata'
      : 'file medio ' + fmtBytes(avg.original_size) + ' → ' + fmtBytes(avg.final_size)
  }

  loaders.aggregates = async () => {
    const data = await latest('aggregates', () => api('api/aggregates?hours=24&top=10'))
    if (data !== STALE) renderAggregates(data)
  }

  function renderAggregates (d) {
    const statuses = Object.keys(d.status_counts || {})
    updateStatusOptions(statuses)
    setChart('status', 'chartStatus', 'doughnut',
      statuses.map((s) => statusMeta(s).label),
      [{ data: statuses.map((s) => d.status_counts[s]), backgroundColor: statuses.map((s) => statusMeta(s).color) }],
      { plugins: legendBottom, cutout: '55%' })

    const points = (d.timeseries && d.timeseries.points) || []
    const labels = points.map((p) => hourFmt.format(new Date(p.t)))
    const other = points.map((p) => Math.max(0, p.total - p.done - p.failed - p.refused))
    setChart('timeline', 'chartTimeline', 'bar', labels, [
      { label: 'Completate', data: points.map((p) => p.done), backgroundColor: STATUS_META.done.color, stack: 'r' },
      { label: 'Fallite', data: points.map((p) => p.failed), backgroundColor: STATUS_META.failed.color, stack: 'r' },
      { label: 'Rifiutate', data: points.map((p) => p.refused), backgroundColor: STATUS_META.rate_limited.color, stack: 'r' },
      { label: 'Altre', data: other, backgroundColor: STATUS_META.queued.color, stack: 'r' }
    ], { plugins: legendBottom, scales: { x: { stacked: true, ticks: { maxRotation: 0, autoSkip: true } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } } })

    const chats = d.top_chats || []
    setChart('chats', 'chartChats', 'bar', chats.map((c) => chatLabel(c.chat_id, c.chat_type)),
      [{ label: 'Richieste', data: chats.map((c) => c.count), backgroundColor: '#0d6efd' }],
      { indexAxis: 'y', plugins: noLegend, scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } })

    const providers = d.providers || []
    setChart('providers', 'chartProviders', 'bar', providers.map((p) => p.name || p.provider || 'sconosciuto'),
      [{ label: 'Richieste', data: providers.map((p) => p.count), backgroundColor: '#6f42c1' }],
      { indexAxis: 'y', plugins: noLegend, scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } })

    const hist = d.duration_histogram || { labels: [], counts: [] }
    setChart('duration', 'chartDuration', 'bar', hist.labels,
      [{ label: 'Richieste', data: hist.counts, backgroundColor: '#198754' }],
      { plugins: noLegend, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } })
  }

  // ---------------------------------------------------------------------
  // Requests
  // ---------------------------------------------------------------------
  const requestState = { page: 1 }

  function requestFilters () {
    const chat = $('chatFilter').value.trim()
    return {
      q: $('searchInput').value.trim(),
      status: $('statusFilter').value,
      chat_id: /^-?\d+$/.test(chat) ? chat : '',
      limit: parseInt($('pageSize').value, 10) || 25
    }
  }

  function updateStatusOptions (extra) {
    const select = $('statusFilter')
    const known = new Set(Array.from(select.options).map((o) => o.value))
    const wanted = ['queued', 'processing', 'sending', 'done', 'failed', 'rate_limited', 'duplicate', 'cancelled', 'aborted', ...(extra || [])]
    for (const status of wanted) {
      if (!status || status === 'unknown' || known.has(status)) continue
      known.add(status)
      select.append(el('option', { text: statusMeta(status).label, attrs: { value: status } }))
    }
  }

  loaders.requests = async () => {
    const filters = requestFilters()
    const offset = (requestState.page - 1) * filters.limit
    const params = { limit: filters.limit, offset, q: filters.q, status: filters.status, chat_id: filters.chat_id }
    let data
    try {
      data = await latest('requests', () => api('api/requests' + query(params)))
    } catch (e) {
      if (e instanceof ApiError && e.status !== 401 && e.status !== 403) {
        $('requestsBody').replaceChildren(emptyRow(9, 'Impossibile caricare le richieste: ' + e.detail))
      }
      throw e
    }
    if (data === STALE) return
    const pages = Math.max(1, Math.ceil(data.total / filters.limit))
    if (requestState.page > pages && data.total > 0) {
      requestState.page = pages
      runLoader('requests', true)
      return
    }
    renderRequests(data, filters)
  }

  function sizeText (r) {
    if (r.original_size === null && r.final_size === null) return '—'
    if (r.final_size !== null && r.final_size !== r.original_size) return fmtBytes(r.original_size) + ' → ' + fmtBytes(r.final_size)
    return fmtBytes(r.original_size !== null ? r.original_size : r.final_size)
  }

  function requestActions (r) {
    const cell = el('td', { className: 'text-end' })
    cell.append(button('Dettagli', 'btn-outline-secondary', () => openDetail(r.id)))
    if (r.requeueable) {
      cell.append(' ', button('Riprova', 'btn-outline-primary', () => requeueIds([r.id]), 'Rimetti in coda'))
    }
    return cell
  }

  function renderRequests (data, filters) {
    const body = $('requestsBody')
    body.replaceChildren()
    if (!data.items.length) body.append(emptyRow(9, 'Nessuna richiesta corrisponde ai filtri.'))
    for (const r of data.items) {
      const tr = el('tr')
      tr.append(
        el('td', { className: 'mono', text: r.id }),
        el('td', null, timeAgo(r.created_at)),
        el('td', { className: 'mono', text: chatLabel(r.chat_id, r.chat_type) }),
        el('td', null, safeLink(r.url), r.provider_name ? el('div', { className: 'small text-body-secondary', text: r.provider_name }) : null),
        el('td', null, statusBadge(r)),
        el('td', { className: 'mono', text: sizeText(r) }),
        el('td', { text: fmtSeconds(r.processing_duration_seconds) }),
        el('td', { className: 'wrap small', text: errorLabel(r.error_code), title: r.error_message || '' }),
        requestActions(r)
      )
      body.append(tr)
    }
    const from = data.total ? data.offset + 1 : 0
    const to = Math.min(data.offset + data.items.length, data.total)
    $('requestsSummary').textContent = fmtNumber(from) + '–' + fmtNumber(to) + ' di ' + fmtNumber(data.total)
    renderPager($('requestsPager'), requestState.page, Math.max(1, Math.ceil(data.total / filters.limit)), (page) => {
      requestState.page = page
      runLoader('requests', true)
    })
    const bulk = $('bulkRequeueBtn')
    bulk.hidden = !(REQUEUEABLE.includes(filters.status) && data.total > 0)
  }

  function renderPager (container, page, pages, go) {
    container.replaceChildren()
    const item = (label, target, disabled, active, aria) => {
      const li = el('li', { className: 'page-item' + (disabled ? ' disabled' : '') + (active ? ' active' : '') })
      const b = el('button', { className: 'page-link', text: label, type: 'button', attrs: aria ? { 'aria-label': aria } : null })
      if (disabled) b.disabled = true
      else b.addEventListener('click', () => go(target))
      if (active) b.setAttribute('aria-current', 'page')
      li.append(b)
      return li
    }
    container.append(item('«', 1, page <= 1, false, 'Prima pagina'), item('‹', page - 1, page <= 1, false, 'Pagina precedente'))
    const start = Math.max(1, Math.min(page - 2, pages - 4))
    const end = Math.min(pages, start + 4)
    for (let p = start; p <= end; p++) container.append(item(String(p), p, false, p === page))
    container.append(item('›', page + 1, page >= pages, false, 'Pagina successiva'), item('»', pages, page >= pages, false, 'Ultima pagina'))
  }

  function debounce (fn, ms) {
    let timer = null
    return () => {
      clearTimeout(timer)
      timer = setTimeout(fn, ms)
    }
  }

  const reloadRequests = () => {
    requestState.page = 1
    runLoader('requests', true)
  }

  // ---------------------------------------------------------------------
  // Request detail (modal)
  // ---------------------------------------------------------------------
  let detailOpenId = null

  async function openDetail (id) {
    let data
    try {
      data = await latest('detail', () => api('api/requests/' + encodeURIComponent(id)))
    } catch (e) {
      return
    }
    if (data === STALE) return
    showDetail(data)
  }

  loaders.detail = async () => {
    if (detailOpenId === null) return
    const id = detailOpenId
    const data = await latest('detail', () => api('api/requests/' + encodeURIComponent(id), { quiet: true }))
    if (data !== STALE && detailOpenId === id) fillDetail(data)
  }

  function detailBody (r) {
    const wrap = el('div')
    const dl = el('dl', { className: 'row small detail-grid mb-3' })
    definition(dl, 'Stato', statusBadge(r))
    definition(dl, 'Link', safeLink(r.url))
    definition(dl, 'Sito', r.provider_name || r.provider)
    definition(dl, 'Chat', chatLabel(r.chat_id, r.chat_type))
    definition(dl, 'Utente Telegram', r.user_id)
    definition(dl, 'Creata', fmtDate(r.created_at))
    definition(dl, 'Aggiornata', fmtDate(r.updated_at))
    // requeued rows also get a not_before (the requeue time): only future ones are deferrals
    if (r.deferred && r.not_before) definition(dl, 'Differita fino a', fmtDate(r.not_before))
    definition(dl, 'Tentativi', r.attempts)
    definition(dl, 'Inizio elaborazione', fmtDate(r.processing_started_at))
    definition(dl, 'Fine elaborazione', fmtDate(r.processing_finished_at))
    definition(dl, 'Durata', fmtSeconds(r.processing_duration_seconds))
    definition(dl, 'Dimensioni', sizeText(r))
    definition(dl, 'Compressa', r.compressed === null ? null : (r.compressed ? 'sì' : 'no'))
    definition(dl, 'Tipo di media', r.media_type)
    definition(dl, 'Messaggio originale', r.original_message_id)
    definition(dl, 'Messaggio inviato', r.sent_message_id)
    if (r.error_code || r.error_message) {
      definition(dl, 'Errore', el('div', null, el('div', { text: errorLabel(r.error_code) + (r.error_code ? ' (' + r.error_code + ')' : '') }), el('div', { className: 'pre-wrap text-body-secondary', text: r.error_message || '' })))
    }
    if (r.description) definition(dl, 'Testo del messaggio', el('div', { className: 'pre-wrap', text: r.description }))
    wrap.append(dl, el('h3', { className: 'h6', text: 'Cronologia (più recenti prima)' }))
    const table = el('table', { className: 'table table-sm small mb-0' })
    const tbody = el('tbody')
    table.append(el('thead', null, el('tr', null, el('th', { text: 'Data' }), el('th', { text: 'Evento' }), el('th', { text: 'Dettagli' }), el('th', { text: 'Durata' }))), tbody)
    if (!r.events || !r.events.length) tbody.append(emptyRow(4, 'Nessun evento registrato.'))
    for (const e of r.events || []) {
      tbody.append(el('tr', null,
        el('td', { text: fmtDate(e.created_at) }),
        el('td', { text: historyLabel(e.type) }),
        el('td', { className: 'wrap', text: e.details || '' }),
        el('td', { text: e.duration_seconds === null ? '' : fmtSeconds(e.duration_seconds) })))
    }
    wrap.append(el('div', { className: 'table-responsive' }, table))
    return wrap
  }

  function detailButtons (r) {
    const buttons = [{ label: 'Chiudi', value: 'close', className: 'btn-outline-secondary' }]
    if (r.cancellable) buttons.unshift({ label: 'Annulla la coda di questa chat…', value: 'cancel-chat', className: 'btn-outline-danger' })
    if (r.requeueable) buttons.unshift({ label: 'Rimetti in coda', value: 'requeue', className: 'btn-primary' })
    return buttons
  }

  async function showDetail (r) {
    const choice = openModal('Richiesta #' + r.id, detailBody(r), detailButtons(r), () => { detailOpenId = r.id })
    const value = await choice
    if (value === 'requeue') await requeueIds([r.id])
    else if (value === 'cancel-chat') await cancelChat(r.chat_id)
  }

  // Live refresh of the open detail: the actions follow the new state too.
  function fillDetail (r) {
    if ($('modal').hidden) return
    $('modalBody').replaceChildren(detailBody(r))
    const buttons = detailButtons(r)
    if (buttons.map((spec) => spec.value).join('|') === modalActions) return
    const footer = $('modalFooter')
    const hadFocus = footer.contains(document.activeElement)
    renderModalButtons(buttons)
    if (hadFocus) footer.querySelector('button:last-child').focus()
  }

  // ---------------------------------------------------------------------
  // Queue
  // ---------------------------------------------------------------------
  let lastQueue = null

  loaders.queue = async () => {
    const data = await latest('queue', () => api('api/queue?limit=100'))
    if (data === STALE) return
    lastQueue = data
    renderQueue(data)
  }

  function queueItem (r, metaNodes, actions) {
    const left = el('div', { className: 'me-2 overflow-hidden' },
      el('div', null, el('span', { className: 'mono me-2', text: '#' + r.id }), safeLink(r.url)),
      el('div', { className: 'item-meta' }, ...metaNodes))
    return el('li', { className: 'list-group-item d-flex justify-content-between align-items-start' }, left,
      el('div', { className: 'text-nowrap' }, ...actions))
  }

  function renderQueue (d) {
    renderWorkerInfo(d.worker)
    renderWorker(d.worker)
    const c = d.counts
    $('queueCounts').textContent = 'in coda ' + fmtNumber(c.queued) + ' · differite ' + fmtNumber(c.deferred) + ' · in esecuzione ' + fmtNumber(c.running)
    $('cancelAllBtn').disabled = c.queued + c.deferred === 0

    const running = $('runningList')
    running.replaceChildren()
    if (!d.running.length) running.append(emptyItem('Nessun job in esecuzione.'))
    for (const r of d.running) {
      const meta = ['chat ' + chatLabel(r.chat_id, r.chat_type) + ' · ', statusMeta(r.status).label]
      if (r.stage) meta.push(' · fase: ' + stageLabel(r.stage))
      meta.push(' · iniziata ', timeAgo(r.processing_started_at))
      if (r.stale) meta.push(el('span', { className: 'badge text-bg-warning ms-2', text: 'worker non attivo' }))
      running.append(queueItem(r, meta, [button('Dettagli', 'btn-outline-secondary', () => openDetail(r.id))]))
    }
    const queued = $('queuedList')
    queued.replaceChildren()
    if (!d.queued.length) queued.append(emptyItem('La coda è vuota.'))
    for (const r of d.queued) {
      queued.append(queueItem(r, ['chat ' + chatLabel(r.chat_id, r.chat_type) + ' · ricevuta ', timeAgo(r.created_at)], [
        button('Annulla chat…', 'btn-outline-danger', () => cancelChat(r.chat_id), 'Annulla le richieste in coda di questa chat')
      ]))
    }
    const deferred = $('deferredList')
    deferred.replaceChildren()
    if (!d.deferred.length) deferred.append(emptyItem('Nessuna richiesta differita.'))
    for (const r of d.deferred) {
      deferred.append(queueItem(r, ['chat ' + chatLabel(r.chat_id, r.chat_type) + ' · partirà ', timeAgo(r.not_before)], [
        button('Annulla chat…', 'btn-outline-danger', () => cancelChat(r.chat_id), 'Annulla le richieste in coda di questa chat')
      ]))
    }
    const retry = $('retryableList')
    retry.replaceChildren()
    if (!d.retryable.length) retry.append(emptyItem('Nessuna richiesta fallita o rifiutata.'))
    for (const r of d.retryable) {
      const meta = [statusMeta(r.status).label, ' · chat ' + chatLabel(r.chat_id, r.chat_type) + ' · ', timeAgo(r.created_at)]
      if (r.error_code) meta.push(' · ' + errorLabel(r.error_code))
      retry.append(queueItem(r, meta, [
        button('Dettagli', 'btn-outline-secondary', () => openDetail(r.id)), ' ',
        button('Rimetti in coda', 'btn-outline-primary', () => requeueIds([r.id]))
      ]))
    }
    $('requeueRetryableBtn').disabled = !d.retryable.some((r) => r.requeueable)
  }

  async function cancelAll () {
    const counts = lastQueue ? lastQueue.counts : null
    const total = counts ? counts.queued + counts.deferred : null
    const ok = await confirmDialog('Annullare la coda?',
      'Verranno annullate ' + (total === null ? 'tutte le' : fmtNumber(total)) + ' richieste in coda, comprese quelle differite. I job già in esecuzione non vengono interrotti.',
      'Annulla la coda', true)
    if (!ok) return
    try {
      const result = await api('api/queue/cancel', { method: 'POST', body: {} })
      toast(fmtNumber(result.cancelled) + ' richieste annullate.', 'success')
    } catch (e) { return }
    invalidate(['queue', 'requests', 'stats'])
  }

  async function cancelChat (chatId) {
    const ok = await confirmDialog('Annullare la coda della chat?',
      'Verranno annullate tutte le richieste in coda (anche differite) della chat ' + chatId + '.', 'Annulla le richieste', true)
    if (!ok) return
    try {
      const result = await api('api/queue/cancel', { method: 'POST', body: { chat_id: chatId } })
      toast(fmtNumber(result.cancelled) + ' richieste della chat ' + chatId + ' annullate.', 'success')
    } catch (e) { return }
    invalidate(['queue', 'requests', 'stats'])
  }

  function requeueSummary (result) {
    let text = fmtNumber(result.requeued.length) + ' richieste rimesse in coda.'
    if (result.skipped.length) text += ' ' + fmtNumber(result.skipped.length) + ' saltate (stato non valido o link già in coda).'
    return text
  }

  async function requeueIds (ids) {
    try {
      const result = await api('api/requests/requeue', { method: 'POST', body: { ids } })
      toast(requeueSummary(result), result.requeued.length ? 'success' : 'warning')
    } catch (e) { return }
    invalidate(['queue', 'requests', 'stats', 'limits'])
  }

  async function requeueRetryable () {
    const ids = lastQueue ? lastQueue.retryable.filter((r) => r.requeueable).map((r) => r.id) : []
    if (!ids.length) return
    const ok = await confirmDialog('Rimettere in coda?', 'Verranno rimesse in coda ' + ids.length + ' richieste fallite o rifiutate. I link già presenti in coda vengono saltati.', 'Rimetti in coda')
    if (ok) await requeueIds(ids)
  }

  async function bulkRequeue () {
    const filters = requestFilters()
    const ok = await confirmDialog('Rimettere in coda i risultati?',
      'Verranno rimesse in coda fino a 500 richieste con stato "' + statusMeta(filters.status).label + '" che corrispondono ai filtri attuali.',
      'Rimetti in coda')
    if (!ok) return
    try {
      const body = { status: filters.status, q: filters.q || null, chat_id: filters.chat_id ? Number(filters.chat_id) : null }
      const result = await api('api/requests/requeue', { method: 'POST', body })
      toast(requeueSummary(result), result.requeued.length ? 'success' : 'warning')
    } catch (e) { return }
    invalidate(['queue', 'requests', 'stats', 'limits'])
  }

  // ---------------------------------------------------------------------
  // Rate limits
  // ---------------------------------------------------------------------
  let lastLimits = null

  loaders.limits = async () => {
    const data = await latest('limits', () => api('api/rate_limits'))
    if (data !== STALE) renderLimits(data)
  }

  function renderLimits (d) {
    lastLimits = d
    const limits = d.limits || {}
    $('limitsSource').textContent = LIMIT_SOURCES[limits.source] || limits.source || ''
    const list = $('limitsList')
    list.replaceChildren()
    const windows = limits.rate_limits || []
    if (!windows.length) list.append(el('li', { text: 'Nessun limite di frequenza configurato.' }))
    for (const w of windows) list.append(el('li', { text: 'Al massimo ' + w.count + ' link ogni ' + fmtWindow(w.seconds) + ' per chat' }))
    if (limits.max_pending_per_chat) list.append(el('li', { text: 'Al massimo ' + limits.max_pending_per_chat + ' richieste in attesa per chat' }))
    if (limits.duplicate_window_seconds) list.append(el('li', { text: 'Lo stesso link è un duplicato per ' + fmtWindow(limits.duplicate_window_seconds) }))
    if (limits.max_defer_seconds) list.append(el('li', { text: 'Le richieste oltre il limite vengono differite fino a ' + fmtWindow(limits.max_defer_seconds) + ', poi rifiutate' }))

    const body = $('limitedBody')
    body.replaceChildren()
    if (!d.limited.length) body.append(emptyRow(5, 'Nessuna chat è limitata in questo momento.'))
    for (const c of d.limited) {
      const latestRefused = c.latest_refused
      body.append(el('tr', null,
        el('td', { className: 'mono', text: c.chat_id }),
        el('td', null, el('span', { text: fmtDate(c.blocked_until) + ' (' }), timeAgo(c.blocked_until), ')'),
        el('td', { text: fmtNumber(c.refused_24h) }),
        el('td', null, latestRefused ? safeLink(latestRefused.url) : '—'),
        el('td', { className: 'text-end' }, button('Sblocca…', 'btn-outline-primary', () => unlimit(c.chat_id)))))
    }
    $('unlimitAllBtn').disabled = false

    const refused = $('refusedBody')
    refused.replaceChildren()
    if (!d.recent_refused.length) refused.append(emptyRow(5, 'Nessun rifiuto recente.'))
    for (const r of d.recent_refused) {
      const reason = r.status === 'duplicate' ? errorLabel('duplicate') : errorLabel(r.error_code || 'rate_limited')
      const actions = el('td', { className: 'text-end' }, button('Dettagli', 'btn-outline-secondary', () => openDetail(r.id)))
      if (r.requeueable) actions.append(' ', button('Rimetti in coda', 'btn-outline-primary', () => requeueIds([r.id])))
      refused.append(el('tr', null,
        el('td', null, timeAgo(r.created_at)),
        el('td', { className: 'mono', text: chatLabel(r.chat_id, r.chat_type) }),
        el('td', null, safeLink(r.url)),
        el('td', { text: reason }),
        actions))
    }
  }

  // The requeue option of the unlimit dialog (GUI_REQUEUE_MAX_AGE_SECONDS).
  function requeueChoice () {
    const maxAge = lastLimits ? lastLimits.requeue_max_age_seconds : null
    if (typeof maxAge === 'number' && maxAge <= 0) {
      return { text: 'Rimettere in coda le richieste rifiutate è disattivato (GUI_REQUEUE_MAX_AGE_SECONDS = 0).', offered: false }
    }
    const age = typeof maxAge === 'number' ? ', se non è più vecchia di ' + fmtWindow(maxAge) : ' recente'
    return {
      text: 'Puoi anche rimettere in coda, per ogni link, l\'ultima richiesta rifiutata' + age + ' (solo se il link non è stato inviato di nuovo nel frattempo).',
      offered: true
    }
  }

  async function unlimit (chatId) {
    const message = chatId === null
      ? 'Tutte le chat ripartono da zero con i limiti di frequenza.'
      : 'La chat ' + chatId + ' riparte da zero con i limiti di frequenza.'
    const requeueOption = requeueChoice()
    const buttons = [
      { label: 'Annulla', value: null, className: 'btn-outline-secondary' },
      { label: 'Solo sblocca', value: 'unlimit', className: 'btn-outline-primary' }
    ]
    if (requeueOption.offered) buttons.push({ label: 'Sblocca e rimetti in coda', value: 'requeue', className: 'btn-primary' })
    const choice = await openModal(chatId === null ? 'Sbloccare tutte le chat?' : 'Sbloccare la chat?',
      message + '\n\n' + requeueOption.text, buttons)
    if (!choice) return
    const requeue = choice === 'requeue'
    try {
      const result = chatId === null
        ? await api('api/rate_limits/unlimit_all', { method: 'POST', body: { requeue } })
        : await api('api/rate_limits/unlimit', { method: 'POST', body: { chat_id: chatId, requeue } })
      let text = chatId === null ? fmtNumber(result.chats) + ' chat sbloccate.' : 'Chat ' + chatId + ' sbloccata.'
      if (requeue) text += ' ' + fmtNumber(result.requeued.length) + ' richieste rimesse in coda.'
      toast(text, 'success')
    } catch (e) { return }
    invalidate(['limits', 'queue', 'requests', 'stats'])
  }

  // ---------------------------------------------------------------------
  // Event log
  // ---------------------------------------------------------------------
  const eventState = { page: 1, limit: 50 }

  function intOrEmpty (value) {
    const text = value.trim()
    return /^-?\d+$/.test(text) ? text : ''
  }

  loaders.events = async () => {
    const params = {
      limit: eventState.limit,
      offset: (eventState.page - 1) * eventState.limit,
      type: $('eventType').value,
      chat_id: intOrEmpty($('eventChat').value),
      request_id: intOrEmpty($('eventRequest').value)
    }
    const data = await latest('events', () => api('api/events' + query(params)))
    if (data !== STALE) renderEvents(data)
  }

  function eventSummary (e) {
    switch (e.type) {
      case 'request_created':
        return 'Nuova richiesta – ' + statusMeta(e.status).label + ': ' + (e.url || '')
      case 'request_status': {
        let text = statusMeta(e.previous).label + ' → ' + statusMeta(e.status).label
        if (e.error_code) text += ' · ' + errorLabel(e.error_code)
        if (e.attempts) text += ' · tentativo ' + e.attempts
        if (e.not_before) text += ' · fino a ' + fmtDate(e.not_before)
        if (e.actor) text += ' · da ' + e.actor
        return text
      }
      case 'request_updated': {
        const fields = Object.keys(e).filter((k) => FIELD_LABELS[k]).map((k) => FIELD_LABELS[k])
        return fields.length ? 'Aggiornati: ' + fields.join(', ') : 'Aggiornata'
      }
      case 'request_event':
        return historyLabel(e.event_type) + (e.duration_seconds ? ' (' + fmtSeconds(e.duration_seconds) + ')' : '')
      case 'queue_cleared':
        return fmtNumber(e.count) + ' richieste annullate' + (e.chat_id ? ' della chat ' + e.chat_id : '') + (e.actor ? ' da ' + e.actor : '')
      case 'rate_limit_changed':
        return e.blocked_until ? 'Limitata fino a ' + fmtDate(e.blocked_until) : 'Limite rimosso'
      case 'chat_unlimited':
        return 'Sbloccata' + (e.actor ? ' da ' + e.actor : '')
      case 'chat_unlimited_all':
        return fmtNumber(e.count) + ' chat sbloccate' + (e.actor ? ' da ' + e.actor : '')
      case 'history_cleared':
        return 'Eliminate ' + fmtNumber(e.requests) + ' richieste e ' + fmtNumber(e.updates) + ' eventi' + (e.actor ? ' da ' + e.actor : '')
      case 'history_pruned':
        return 'Retention: ' + fmtNumber(e.requests) + ' richieste e ' + fmtNumber(e.updates) + ' eventi eliminati'
      case 'gui_login':
        return (e.auth === 'token' ? 'Accesso con il token' : 'Accesso OAuth' + (e.who ? ' di ' + e.who : '')) + (e.client ? ' da ' + e.client : '') + ' (sessione ' + e.session_id + ')'
      case 'gui_logout':
        return 'Uscita (sessione ' + (e.session_ids || []).join(', ') + ')'
      case 'gui_sessions_revoked':
        return fmtNumber((e.session_ids || []).length) + ' sessioni terminate' + (e.actor ? ' da ' + e.actor : '')
      case 'update_created':
        return String(e.raw || '').slice(0, 300)
      default: {
        const rest = Object.assign({}, e)
        for (const key of ['id', 'type', 'created_at']) delete rest[key]
        return JSON.stringify(rest).slice(0, 300)
      }
    }
  }

  function renderEvents (data) {
    const body = $('eventsBody')
    body.replaceChildren()
    if (!data.items.length) body.append(emptyRow(6, 'Nessun evento.'))
    for (const e of data.items) {
      const requestCell = el('td')
      if (e.request_id !== undefined && e.request_id !== null) {
        requestCell.append(el('button', { className: 'btn btn-link btn-sm p-0', text: '#' + e.request_id, type: 'button', onClick: () => openDetail(e.request_id) }))
      }
      body.append(el('tr', null,
        el('td', { className: 'mono', text: e.id }),
        el('td', null, timeAgo(e.created_at)),
        el('td', null, el('span', { className: 'badge text-bg-light', text: EVENT_LABELS[e.type] || e.type })),
        el('td', { className: 'mono', text: e.chat_id === undefined || e.chat_id === null ? '' : e.chat_id }),
        requestCell,
        el('td', { className: 'wrap', text: eventSummary(e) })))
    }
    const pages = Math.max(1, Math.ceil(data.total / eventState.limit))
    $('eventsSummary').textContent = fmtNumber(data.total) + ' eventi'
    renderPager($('eventsPager'), eventState.page, pages, (page) => {
      eventState.page = page
      runLoader('events', true)
    })
  }

  function setupEventFilters () {
    const select = $('eventType')
    for (const [type, label] of Object.entries(EVENT_LABELS)) select.append(el('option', { text: label, attrs: { value: type } }))
    const reload = () => {
      eventState.page = 1
      runLoader('events', true)
    }
    select.addEventListener('change', reload)
    const debounced = debounce(reload, 350)
    $('eventChat').addEventListener('input', debounced)
    $('eventRequest').addEventListener('input', debounced)
    $('eventFilters').addEventListener('submit', (event) => {
      event.preventDefault()
      reload()
    })
  }

  // ---------------------------------------------------------------------
  // Users
  // ---------------------------------------------------------------------
  loaders.users = async () => {
    const data = await latest('users', () => Promise.all([api('api/users?limit=200'), api('api/sessions')]))
    if (data === STALE) return
    renderUsers(data[0])
    renderSessions(data[1])
  }

  function renderSessions (data) {
    const body = $('sessionsBody')
    body.replaceChildren()
    const others = data.items.filter((s) => !s.current).length
    $('sessionsSummary').textContent = fmtNumber(data.items.length) + ' sessioni attive'
    $('revokeOthersBtn').disabled = others === 0
    if (!data.items.length) body.append(emptyRow(6, 'Nessuna sessione attiva.'))
    for (const s of data.items) {
      const who = el('td', null, el('span', { text: s.who || '—' }))
      if (s.groups && s.groups.length) who.append(el('div', { className: 'small text-body-secondary', text: 'gruppi: ' + s.groups.join(', ') }))
      const device = el('td', { className: 'small' },
        el('div', { text: s.client || '—' }),
        el('div', { className: 'text-body-secondary text-truncate session-agent', text: s.user_agent || '', title: s.user_agent || '' }))
      const actions = el('td', { className: 'text-end' })
      if (s.current) actions.append(el('span', { className: 'badge text-bg-success', text: 'Questa sessione' }))
      else actions.append(button('Termina', 'btn-outline-danger', () => revokeSession(s)))
      body.append(el('tr', null,
        who,
        el('td', null, el('span', { className: 'badge text-bg-light', text: s.auth === 'token' ? 'Token' : 'OAuth' })),
        device,
        el('td', null, timeAgo(s.created_at)),
        el('td', null, timeAgo(s.last_seen_at)),
        actions))
    }
  }

  async function revokeSession (session) {
    const ok = await confirmDialog('Terminare la sessione?', 'La sessione di ' + (session.who || session.id) + ' verrà chiusa alla sua prossima richiesta.', 'Termina', true)
    if (!ok) return
    try {
      await api('api/sessions/' + encodeURIComponent(session.id) + '/revoke', { method: 'POST' })
      toast('Sessione terminata.', 'success')
    } catch (e) { return }
    runLoader('users', true)
  }

  async function revokeOtherSessions () {
    const ok = await confirmDialog('Terminare le altre sessioni?', 'Tutte le sessioni tranne questa verranno chiuse: chi le usa dovrà accedere di nuovo.', 'Termina tutte', true)
    if (!ok) return
    try {
      const result = await api('api/sessions/revoke_others', { method: 'POST' })
      toast(fmtNumber(result.revoked.length) + ' sessioni terminate.', 'success')
    } catch (e) { return }
    runLoader('users', true)
  }

  function renderUsers (data) {
    const body = $('usersBody')
    body.replaceChildren()
    $('usersSummary').textContent = fmtNumber(data.total) + ' utenti, ' + fmtNumber(data.admins) + ' amministratori'
    if (!data.items.length) body.append(emptyRow(5, 'Nessun utente registrato.'))
    for (const u of data.items) {
      const isAdmin = u.role === 'admin'
      body.append(el('tr', null,
        el('td', { text: u.email || '—' }),
        el('td', { text: u.username || '—' }),
        el('td', null, el('span', { className: 'badge ' + (isAdmin ? 'text-bg-primary' : 'text-bg-light'), text: isAdmin ? 'Amministratore' : 'Utente' })),
        el('td', null, timeAgo(u.created_at)),
        el('td', { className: 'text-end' },
          button(isAdmin ? 'Rendi utente' : 'Rendi amministratore', 'btn-outline-primary', () => setRole(u, isAdmin ? 'user' : 'admin')), ' ',
          button('Elimina…', 'btn-outline-danger', () => deleteUser(u)))))
    }
  }

  async function createUser (event) {
    event.preventDefault()
    const body = { email: $('userEmail').value.trim(), username: $('userName').value.trim() || null, role: $('userRole').value }
    if (!body.email) return
    try {
      await api('api/users', { method: 'POST', body })
      toast('Utente ' + body.email + ' aggiunto.', 'success')
      $('userForm').reset()
    } catch (e) { return }
    runLoader('users', true)
  }

  async function setRole (user, role) {
    if (role === 'user' && me && me.user && me.user.email && user.email && me.user.email.toLowerCase() === user.email.toLowerCase()) {
      const ok = await confirmDialog('Rimuovere i tuoi privilegi?', 'Stai togliendo il ruolo di amministratore al tuo stesso account: potresti perdere l\'accesso alla console.', 'Rimuovi', true)
      if (!ok) return
    }
    try {
      await api('api/users/' + encodeURIComponent(user.id) + '/role', { method: 'PUT', body: { role } })
      toast('Ruolo di ' + (user.email || user.username) + ' aggiornato.', 'success')
    } catch (e) { return }
    runLoader('users', true)
  }

  async function deleteUser (user) {
    const ok = await confirmDialog('Eliminare l\'utente?', 'L\'utente ' + (user.email || user.username || user.id) + ' verrà eliminato.', 'Elimina', true)
    if (!ok) return
    try {
      await api('api/users/' + encodeURIComponent(user.id), { method: 'DELETE' })
      toast('Utente eliminato.', 'success')
    } catch (e) { return }
    runLoader('users', true)
  }

  // ---------------------------------------------------------------------
  // System
  // ---------------------------------------------------------------------
  let systemConfig = null

  loaders.system = async () => {
    const data = await latest('system', () => Promise.all([api('api/config'), api('health', { quiet: true }).catch((e) => ({ ok: false, db: 'error', error: e.detail }))]))
    if (data !== STALE) renderSystem(data[0], data[1])
  }

  function renderSystem (cfg, health) {
    systemConfig = cfg
    const dl = $('healthInfo')
    dl.replaceChildren()
    definition(dl, 'Database', health.db === 'ok' ? 'raggiungibile' : 'errore')
    const workerStates = { online: 'attivo', offline: 'non raggiungibile', stopped: 'fermo', unknown: 'mai visto' }
    definition(dl, 'Worker', workerStates[cfg.worker.state] || cfg.worker.state)
    definition(dl, 'Versione della console', cfg.version)
    definition(dl, 'Versione del worker', cfg.worker.version)
    definition(dl, 'Client in tempo reale', cfg.gui.ws_clients)
    definition(dl, 'Ultimo evento letto', cfg.gui.last_event_id)

    const warnings = $('configWarnings')
    warnings.replaceChildren()
    const list = (cfg.warnings || []).slice()
    if (cfg.auth.secret_key_ephemeral) list.unshift('SECRET_KEY non impostata: le sessioni non sopravvivono ai riavvii della console.')
    if (list.length) {
      const ul = el('ul', { className: 'mb-0' })
      for (const w of list) ul.append(el('li', { text: w }))
      warnings.append(el('div', { className: 'alert alert-warning small' }, el('strong', { text: 'Avvisi di configurazione' }), ul))
    }

    const body = $('configBody')
    body.replaceChildren()
    const section = (title, rows) => {
      const sdl = el('dl', { className: 'row mb-0' })
      for (const [label, value] of rows) definition(sdl, label, value)
      body.append(el('div', { className: 'col-lg-6' }, el('h3', { className: 'h6', text: title }), sdl))
    }
    const yes = (v) => (v ? 'sì' : 'no')
    section('Accesso', [
      ['Token amministratore', yes(cfg.auth.token)],
      ['OAuth/OIDC', yes(cfg.auth.oauth)],
      ['Gruppi amministratore', (cfg.auth.oauth_admin_groups || []).join(', ') || null],
      ['E-mail senza verifica dichiarata usate per il ruolo', yes(cfg.auth.oauth_assume_email_verified)],
      ['Redirect OAuth', cfg.auth.oauth_redirect_uri],
      ['Cookie solo HTTPS', yes(cfg.auth.session_cookie_secure)],
      ['Durata della sessione', fmtWindow(cfg.auth.session_max_age_seconds)]
    ])
    const limits = cfg.limits || {}
    section('Limiti effettivi (' + (LIMIT_SOURCES[limits.source] || limits.source || '') + ')', [
      ['Finestre', (limits.rate_limits || []).map((w) => w.count + ' / ' + fmtWindow(w.seconds)).join(', ') || 'nessuna'],
      ['Richieste in attesa per chat', limits.max_pending_per_chat],
      ['Finestra duplicati', limits.duplicate_window_seconds ? fmtWindow(limits.duplicate_window_seconds) : null],
      ['Differimento massimo', limits.max_defer_seconds ? fmtWindow(limits.max_defer_seconds) : null]
    ])
    const settings = cfg.settings || {}
    section('Impostazioni lette dalla console', Object.keys(settings).sort().map((key) => {
      const value = settings[key]
      return [key, Array.isArray(value) || (value && typeof value === 'object') ? JSON.stringify(value) : (typeof value === 'boolean' ? yes(value) : value)]
    }))
  }

  // What a history clear keeps besides the active requests (see /api/config).
  function keptByClear () {
    const keep = systemConfig && systemConfig.gui ? systemConfig.gui.history_keep_seconds : null
    const base = 'Restano le richieste in coda o in corso, gli utenti e i limiti delle chat'
    if (keep === null || keep === undefined) {
      return base + ', oltre alle richieste recenti che servono ai limiti di frequenza e al controllo dei duplicati.'
    }
    if (keep > 0) {
      return base + ', oltre alle richieste completate o fallite più recenti di ' + fmtWindow(keep) +
        ', che servono ai limiti di frequenza e al controllo dei duplicati.'
    }
    return base + '.'
  }

  async function clearHistory () {
    const ok = await confirmDialog('Pulire lo storico?',
      'Verranno eliminate definitivamente le richieste concluse con i loro eventi e il registro eventi. ' + keptByClear(),
      'Elimina lo storico', true)
    if (!ok) return
    try {
      const result = await api('api/history/clear', { method: 'POST', body: {} })
      const d = result.deleted || {}
      toast('Storico eliminato: ' + fmtNumber(d.requests) + ' richieste, ' + fmtNumber(d.updates) + ' eventi.', 'success')
    } catch (e) { return }
    resyncAll()
  }

  // ---------------------------------------------------------------------
  // Wiring
  // ---------------------------------------------------------------------
  function setup () {
    $('tokenForm').addEventListener('submit', submitToken)
    $('tokenToggle').addEventListener('click', () => {
      const input = $('tokenInput')
      const show = input.type === 'password'
      input.type = show ? 'text' : 'password'
      $('tokenToggle').textContent = show ? 'Nascondi' : 'Mostra'
      $('tokenToggle').setAttribute('aria-pressed', show ? 'true' : 'false')
    })
    $('logoutBtn').addEventListener('click', logout)
    $('notAdminLogoutBtn').addEventListener('click', logout)
    $('recheckBtn').addEventListener('click', initAuth)
    $('refreshBtn').addEventListener('click', resyncAll)
    for (const tab of document.querySelectorAll('[data-tab]')) {
      tab.addEventListener('click', () => activateTab(tab.dataset.tab))
    }
    window.addEventListener('hashchange', () => activateTab(tabFromHash(), true))

    $('requestFilters').addEventListener('submit', (event) => {
      event.preventDefault()
      reloadRequests()
    })
    $('searchInput').addEventListener('input', debounce(reloadRequests, 300))
    $('chatFilter').addEventListener('input', debounce(reloadRequests, 300))
    $('statusFilter').addEventListener('change', reloadRequests)
    $('pageSize').addEventListener('change', reloadRequests)
    $('bulkRequeueBtn').addEventListener('click', bulkRequeue)
    updateStatusOptions([])

    $('cancelAllBtn').addEventListener('click', cancelAll)
    $('requeueRetryableBtn').addEventListener('click', requeueRetryable)
    $('unlimitAllBtn').addEventListener('click', () => unlimit(null))
    $('userForm').addEventListener('submit', createUser)
    $('revokeOthersBtn').addEventListener('click', revokeOtherSessions)
    $('clearHistoryBtn').addEventListener('click', clearHistory)
    setupEventFilters()

    $('modalClose').addEventListener('click', () => closeModal(null))
    $('modalDialog').addEventListener('click', (event) => {
      if (event.target === $('modalDialog')) closeModal(null)
    })
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('modal').hidden) closeModal(null)
    })
    document.addEventListener('visibilitychange', () => {
      if (document.hidden || !authed) return
      refreshRelativeTimes()
      if (!ws && wsWanted) connectWs()
      invalidate(['worker', ...(PANEL_RESOURCES[currentTab] || [])])
    })

    // Safety net: periodic refresh (every resource when the live channel is down).
    setInterval(() => {
      if (document.hidden || !authed) return
      refreshRelativeTimes()
      const live = ws && ws.readyState === WebSocket.OPEN
      invalidate(live ? ['worker'] : ['worker', ...(PANEL_RESOURCES[currentTab] || [])])
    }, 15000)
    setInterval(() => {
      if (!document.hidden && authed && currentTab === 'overview') invalidate(['stats', 'aggregates'])
    }, 60000)
    initAuth()
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup)
  else setup()
})()
