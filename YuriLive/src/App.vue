<template>
  <div ref="pageEl" class="cinema">
    <header class="hd">
      <div class="hd-brand">
        <span class="logo">YuriLive</span>
        <span class="tagline">一起看 · 放映厅</span>
      </div>
      <div class="hd-right">
        <span class="badge" :class="{ host: isHost }">{{ isHost ? '主持人' : myName }}</span>
        <span class="viewers"><i></i>{{ viewerCount }} 在线</span>
        <button v-if="!isHost" class="btn ghost sm" @click="keyDialog = true">主持人登录</button>
      </div>
    </header>

    <main class="main">
      <div ref="stageEl" class="stage">
        <video ref="videoEl" class="screen" :controls="isHost"
               playsinline webkit-playsinline :class="{ guest: !isHost }"></video>

        <div v-if="!isHost && needJoin" class="mask" @click="join">
          <div class="mask-card">
            <div class="mask-title">放映即将开始</div>
            <div class="mask-sub">主持人正在播放《{{ curName || '...' }}》</div>
            <button class="btn primary lg">点击加入观看</button>
            <div class="mask-hint">点击后即可自动跟随播放与暂停</div>
          </div>
        </div>

        <div v-if="!isHost && mutedByPolicy" class="mute-pill" @click="unmute">
          <span class="dot"></span>浏览器限制已静音 · 点击恢复声音
        </div>
      </div>

      <div class="dock">
        <template v-if="isHost">
          <div class="ctrl-row">
            <el-select v-model="curPath" filterable placeholder="选择影片" class="pick"
                       @change="onSelectVideo">
              <el-option v-for="f in videos" :key="f.path" :label="f.name" :value="f.path" />
            </el-select>
            <el-upload :http-request="doUpload" :show-file-list="false" accept="video/*">
              <button class="btn ghost" :disabled="uploading">{{ uploading ? '上传中…' : '上传影片' }}</button>
            </el-upload>
            <button class="btn ghost" :disabled="fetching" @click="fetchDialog = true">
              {{ fetching ? `下载中 ${fetchPct}%…` : '爬取B站' }}
            </button>
            <button class="btn primary" @click="syncNow">同步给大家</button>
          </div>
        </template>
        <template v-else>
          <div class="ctrl-row">
            <div class="now">
              <span class="now-label">正在放映</span>
              <span class="now-title">{{ curName || '等待主持人选片…' }}</span>
            </div>
            <span class="spacer"></span>
            <span class="follow" :class="{ off: !following }">
              <i></i>{{ following ? '跟随中' : '重连中…' }}
            </span>
          </div>
        </template>
      </div>
    </main>

    <el-dialog v-model="fetchDialog" title="爬取 B站视频" width="440px">
      <el-input v-model="fetchUrl" placeholder="粘贴 BV 链接, 如 https://www.bilibili.com/video/BVxxxx 或 b23.tv 短链"
                @keyup.enter="doFetch" />
      <div class="fetch-tip">下载最高画质到片库(需装有 yt-dlp 与 ffmpeg), 完成后自动放映</div>
      <template #footer>
        <button class="btn ghost" @click="fetchDialog = false">取消</button>
        <button class="btn primary" :disabled="!fetchUrl.trim() || fetching" @click="doFetch">开始下载</button>
      </template>
    </el-dialog>

    <el-dialog v-model="keyDialog" title="主持人登录" width="320px">
      <el-input v-model="keyInput" type="password" show-password placeholder="输入 host key"
                @keyup.enter="applyKey" />
      <template #footer>
        <button class="btn ghost" @click="keyDialog = false">取消</button>
        <button class="btn primary" @click="applyKey">连接</button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

const pageEl = ref(null)
const stageEl = ref(null)
const videoEl = ref(null)

const isHost = ref(false)
const myName = ref('')
const viewerCount = ref(1)
const videos = ref([])
const curPath = ref('')
const curName = ref('')
const uploading = ref(false)
const needJoin = ref(true)
const mutedByPolicy = ref(false)
const joined = ref(false)
const following = ref(false)
const keyDialog = ref(false)
const fetchDialog = ref(false)
const fetchUrl = ref('')
const fetching = ref(false)
const fetchPct = ref(0)
let fetchTimer = null
const keyInput = ref(localStorage.getItem('yl-key') || '')
const videoTime = ref(0)
const duration = ref(0)

const KEY = 'yl-key'

let ws = null, reconnectTimer = null, hbTimer = null, driftTimer = null
let sendThrottle = 0, clockOffset = 0, lastState = null, applyLock = false

function connect() {
  if (ws) { try { ws.close() } catch (e) {} }
  const key = localStorage.getItem(KEY) || ''
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws?key=${encodeURIComponent(key)}`)
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data)
    if (m.t === 'hello') {
      isHost.value = m.role === 'host'
      myName.value = m.name
      if (m.state && m.state.v) applyState(m.state, true)
      if (isHost.value) needJoin.value = false
    } else if (m.t === 'state') {
      applyState(m, false)
    } else if (m.t === 'count') {
      viewerCount.value = m.n
    } else if (m.t === 'pong') {
      clockOffset = m.sts - m.cts
    }
  }
  ws.onclose = () => { following.value = false; reconnectTimer = setTimeout(connect, 2000) }
  ws.onopen = () => { following.value = true }
}

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)) }

function nameOf(p) { return videos.value.find(f => f.path === p)?.name || p.split('/').pop() }

function expectedPos(s, atPerf) {
  if (!s.playing) return s.pos
  return s.pos + Math.max(0, (atPerf + clockOffset - s.srv_ts * 1000) / 1000)
}

function applyState(s, force) {
  if (isHost.value) {
    if (s.v) { curPath.value = s.v; curName.value = nameOf(s.v) }
    return
  }
  if (!s.v) return
  lastState = s
  if (s.v !== curPath.value || force) {
    curPath.value = s.v
    curName.value = nameOf(s.v)
    loadVideo(s)
  } else {
    chase(s)
  }
}

function chase(s) {
  const v = videoEl.value
  if (!v || !v.duration) return
  const exp = expectedPos(s, performance.now())
  if (Math.abs(v.currentTime - exp) > 2) safeSeek(exp)
  if (s.playing && v.paused) tryPlay(v)
  if (!s.playing && !v.paused) v.pause()
}

/** 播放被拒 → 降级静音自动播放(保画面同步) → 再不行等用户点击 */
function tryPlay(v) {
  const p = v.play()
  if (p && p.catch) p.catch(() => {
    v.muted = true
    v.play().then(() => { mutedByPolicy.value = true }).catch(() => { needJoin.value = true })
  })
}

function unmute() {
  const v = videoEl.value
  v.muted = false
  mutedByPolicy.value = false
  if (lastState && lastState.playing) tryPlay(v)
}

function loadVideo(s) {
  const v = videoEl.value
  if (!v) return
  applyLock = true
  v.src = '/media/' + encodeURI(s.v)
  v.load()
  const onMeta = () => {
    v.removeEventListener('loadedmetadata', onMeta)
    applyLock = false
    duration.value = v.duration || 0
    const start = Math.max(0, Math.min(expectedPos(s, performance.now()), (v.duration || 0) - 0.5))
    safeSeek(start)
    if (s.playing) {
      if (needJoin.value) return
      tryPlay(v)
    } else {
      v.pause()
    }
  }
  v.addEventListener('loadedmetadata', onMeta)
}

function safeSeek(t) {
  const v = videoEl.value
  if (!v) return
  applyLock = true
  v.currentTime = t
  setTimeout(() => { applyLock = false }, 300)
}

function hostSendState() {
  const v = videoEl.value
  if (!v || !curPath.value) return
  send({ t: 'state', v: curPath.value, playing: !v.paused, pos: v.currentTime })
}
function throttledSend() {
  const now = performance.now()
  if (now - sendThrottle < 3000) return
  sendThrottle = now
  hostSendState()
}

function onSelectVideo(p) {
  if (!isHost.value) return
  curPath.value = p
  curName.value = nameOf(p)
  const v = videoEl.value
  applyLock = true
  v.src = '/media/' + encodeURI(p)
  v.load()
  setTimeout(() => { applyLock = false }, 300)
  v.pause()
  send({ t: 'state', v: p, playing: false, pos: 0 })
}

function syncNow() { hostSendState(); ElMessage.success('已同步给所有人') }

function bindHostEvents() {
  const v = videoEl.value
  v.addEventListener('play', () => { if (!applyLock) hostSendState() })
  v.addEventListener('pause', () => { if (!applyLock) hostSendState() })
  v.addEventListener('seeked', () => { if (!applyLock) hostSendState() })
  v.addEventListener('ended', () => { if (isHost.value) hostSendState() })
  v.addEventListener('timeupdate', () => {
    videoTime.value = v.currentTime
    if (isHost.value) throttledSend()
  })
  v.addEventListener('loadedmetadata', () => { duration.value = v.duration || 0 })
}

function join() {
  needJoin.value = false
  joined.value = true
  const v = videoEl.value
  if (lastState && lastState.v) {
    if (!v.src || curPath.value !== lastState.v) loadVideo(lastState)
    else {
      chase(lastState)
      if (lastState.playing && v.paused) tryPlay(v)
    }
  }
}

async function doUpload(opt) {
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', opt.file)
    const res = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'X-Host-Key': localStorage.getItem(KEY) || '' },
      body: fd,
    })
    const data = await res.json()
    if (!res.ok) { ElMessage.error(`上传失败：${data.error}`); opt.onError(); return }
    ElMessage.success(`已上传：${data.name}`)
    opt.onSuccess(data)
    await refreshVideos()
    if (isHost.value) onSelectVideo(data.path)
  } catch (e) {
    ElMessage.error(`上传失败：${e}`)
    opt.onError(e)
  } finally { uploading.value = false }
}

async function doFetch() {
  if (!fetchUrl.value.trim()) return
  const res = await fetch('/api/fetch', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Host-Key': localStorage.getItem(KEY) || '',
    },
    body: JSON.stringify({ url: fetchUrl.value.trim() }),
  })
  const data = await res.json()
  if (!res.ok) { ElMessage.error(`下载失败：${data.error}`); return }
  fetchDialog.value = false
  fetchUrl.value = ''
  // 后台任务: 轮询进度直到完成
  fetching.value = true
  fetchPct.value = 0
  clearInterval(fetchTimer)
  const job = data.job
  fetchTimer = setInterval(async () => {
    try {
      const r = await fetch(`/api/fetch/${job}`)
      const d = await r.json()
      fetchPct.value = Math.floor(d.pct || 0)
      if (d.status === 'done') {
        clearInterval(fetchTimer); fetching.value = false
        ElMessage.success(`已下载：${d.name}`)
        await refreshVideos()
        if (isHost.value) onSelectVideo(d.path)
      } else if (d.status === 'failed') {
        clearInterval(fetchTimer); fetching.value = false
        ElMessage.error(`下载失败：${d.msg}`)
      }
    } catch (e) { /* 轮询暂时失败,下轮再试 */ }
  }, 1500)
}

async function refreshVideos() {
  const res = await fetch('/api/videos')
  const data = await res.json()
  videos.value = data.files
  if (!curPath.value && data.state && data.state.v) {
    curPath.value = data.state.v
    curName.value = nameOf(data.state.v)
    if (!isHost.value) lastState = Object.assign({}, data.state, { srv_ts: 0 })
  }
}

function onKey(e) {
  if (e.key === ' ' && isHost.value) {
    e.preventDefault()
    const v = videoEl.value
    if (v.paused) v.play(); else v.pause()
  }
}

function fmtClock(s) {
  if (!isFinite(s)) return '--:--'
  const p = n => String(n).padStart(2, '0')
  const h = Math.floor(s / 3600)
  return h > 0
    ? p(h) + ':' + p(Math.floor(s / 60) % 60) + ':' + p(Math.floor(s % 60))
    : p(Math.floor(s / 60)) + ':' + p(Math.floor(s % 60))
}

function applyKey() {
  if (!keyInput.value.trim()) return
  localStorage.setItem(KEY, keyInput.value.trim())
  keyDialog.value = false
  ElMessage.info('正在以主持人身份重连…')
  connect()
}

onMounted(() => {
  connect()
  bindHostEvents()
  refreshVideos()
  hbTimer = setInterval(() => { send({ t: 'ping', cts: performance.now() }) }, 15000)
  driftTimer = setInterval(() => {
    if (!isHost.value && lastState && joined.value && !needJoin.value) {
      const v = videoEl.value
      if (v && v.duration) {
        const exp = expectedPos(lastState, performance.now())
        if (Math.abs(v.currentTime - exp) > 2) safeSeek(exp)
      }
    }
  }, 5000)
  window.addEventListener('keydown', onKey)
})

onUnmounted(() => {
  clearTimeout(reconnectTimer)
  clearInterval(fetchTimer)
  clearInterval(hbTimer)
  clearInterval(driftTimer)
  window.removeEventListener('keydown', onKey)
  if (ws) ws.close()
})
</script>

<style>
:root {
  --yl-accent: #ef4c7e;
  --yl-bg: #f5f4f1;
  --yl-card: #ffffff;
  --yl-text: #26221e;
  --yl-dim: #8f887e;
  --yl-line: rgba(38, 34, 30, .1);
  --el-color-primary: #ef4c7e;
  --el-color-primary-light-3: #f47a9e;
  --el-color-primary-light-5: #f8a6bf;
  --el-color-primary-light-7: #fbcadb;
  --el-color-primary-light-8: #fdd9e5;
  --el-color-primary-light-9: #fdeef3;
  --el-color-primary-dark-2: #d63a6a;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; background: var(--yl-bg); }
body {
  font-family: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  color: var(--yl-text);
  -webkit-font-smoothing: antialiased;
}
button { font-family: inherit; }
</style>
<style scoped>
.cinema {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--yl-bg);
}

.hd {
  display: flex; align-items: baseline; justify-content: space-between;
  width: min(1060px, 94vw); margin: 0 auto; padding: 26px 4px 18px;
}
.logo {
  font-family: "Noto Serif SC", serif; font-weight: 900; font-size: 26px;
  letter-spacing: .04em; color: var(--yl-text);
}
.logo::after { content: ""; display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; background: var(--yl-accent); margin-left: 6px; }
.tagline { margin-left: 12px; font-size: 12px; letter-spacing: .35em; color: var(--yl-dim); }
.hd-right { display: flex; align-items: center; gap: 12px; }
.badge {
  font-size: 12px; padding: 3px 12px; border-radius: 999px;
  border: 1px solid var(--yl-line); color: var(--yl-text); background: var(--yl-card);
}
.badge.host { background: var(--yl-accent); color: #fff; border-color: transparent; font-weight: 700; }
.viewers { font-size: 12px; color: var(--yl-dim); display: flex; align-items: center; gap: 6px; }
.viewers i { width: 6px; height: 6px; border-radius: 50%; background: #3fbf6b; }

.btn { cursor: pointer; border-radius: 9px; border: 1px solid transparent; padding: 9px 18px; font-size: 13px; transition: all .15s; background: var(--yl-card); }
.btn.primary { background: var(--yl-accent); color: #fff; font-weight: 700; }
.btn.primary:hover { filter: brightness(1.06); }
.btn.ghost { background: var(--yl-card); color: var(--yl-text); border-color: var(--yl-line); }
.btn.ghost:hover { border-color: var(--yl-accent); color: var(--yl-accent); }
.btn.sm { padding: 5px 12px; font-size: 12px; }
.btn.lg { padding: 13px 34px; font-size: 15px; }
.btn:disabled { opacity: .5; cursor: default; }

.main { width: min(1060px, 94vw); margin: 0 auto; padding-bottom: 40px; flex: 1; }
.stage {
  position: relative; width: 100%; aspect-ratio: 16 / 9;
  background: #000; border-radius: 14px; overflow: hidden;
  border: 1px solid var(--yl-line);
  box-shadow: 0 20px 50px -18px rgba(38, 34, 30, .18);
}
.screen { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
.screen.guest { pointer-events: none; }

.mask {
  position: absolute; inset: 0; z-index: 10; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  background: rgba(20, 16, 14, .72); backdrop-filter: blur(3px);
}
.mask-card { text-align: center; padding: 20px; max-width: 100%; }
.mask-title { font-family: "Noto Serif SC", serif; font-weight: 900; font-size: 26px; color: #fff; }
.mask-sub { margin: 10px 0 22px; font-size: 13px; color: rgba(255,255,255,.65); max-width: 74vw;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mask-hint { margin-top: 14px; font-size: 11px; color: rgba(255,255,255,.5); }
.mute-pill {
  position: absolute; left: 50%; bottom: 18px; transform: translateX(-50%);
  display: flex; align-items: center; gap: 8px; z-index: 9; cursor: pointer;
  background: rgba(20, 16, 12, .88); border: 1px solid rgba(255,255,255,.12);
  color: #ffd2e0; font-size: 12px; padding: 7px 16px; border-radius: 999px;
}
.mute-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--yl-accent); animation: pulse 1.4s infinite; }
@keyframes pulse { 50% { opacity: .3; } }

.fetch-tip { margin-top: 10px; font-size: 12px; color: var(--yl-dim); }

.dock { margin-top: 14px; }
.ctrl-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pick { width: 300px; }
.spacer { flex: 1; }
.now { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.now-label { font-size: 11px; letter-spacing: .25em; color: var(--yl-dim); flex: none; }
.now-title { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.follow { display: flex; align-items: center; gap: 7px; font-size: 12px; color: #3fbf6b; flex: none; }
.follow i { width: 7px; height: 7px; border-radius: 50%; background: #3fbf6b; animation: pulse 1.4s infinite; }
.follow.off { color: #d05555; }
.follow.off i { background: #d05555; animation: none; }

@media (max-width: 768px) {
  .hd { width: 100%; padding: 14px 16px 10px; }
  .logo { font-size: 20px; }
  .tagline { display: none; }
  .main { width: 100%; padding-bottom: calc(16px + env(safe-area-inset-bottom, 0px)); }
  .stage { border-radius: 0; border-left: none; border-right: none; }
  .dock { padding: 0 16px; }
  .ctrl-row { gap: 8px; }
  .pick { width: 100%; order: -1; }
  .mask-title { font-size: 21px; }
}
@media (prefers-reduced-motion: reduce) {
  .viewers i, .follow i, .mute-pill .dot { animation: none; }
}
</style>
