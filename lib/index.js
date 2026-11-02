/**
 * dsh-voice-bridge —— 贾维斯语音助手（自带脚本与模型的完整插件）
 *
 * 装进 DSH 启动后自动做两件事：
 *   听：jarvis/jarvis_listen.py —— 唤醒词「贾维斯」→ 录音 → 本地 SenseVoice 转文字
 *       → 写 outbox.json → 界面插件 insertText + submit（自动发送）
 *   说：jarvis/jarvis_speak.py  —— 盯会话文件，把新回答用在线语音（晓晓/云希）念出来，
 *       在线失败自动退回系统语音；支持被打断与「续」续播
 *
 * 起子进程之前先探一次 Python 依赖；跑起来崩了会自动重启（1s/5s/15s），
 * 崩因写进 /status 和 /latest，界面会在输入框那儿显示一行提示，不再静默失效。
 *
 * 目录约定（本插件自带，不依赖外面任何路径）：
 *   <插件目录>/lib/      ← 本文件与界面脚本
 *   <插件目录>/jarvis/   ← Python 脚本、关键词、音色配置
 *   <插件目录>/models/   ← 唤醒词模型
 *
 * 可用接口：
 *   GET  /api/voice-bridge/latest   取新文字（附带 listen/speak/error 健康状态）
 *   POST /api/voice-bridge/ack      插入回执
 *   POST /api/voice-bridge/voice-turn  标记“这轮是语音提问”
 *   GET  /api/voice-bridge/status   看状态
 *   POST /api/voice-bridge/start | stop
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn, execFile } from 'node:child_process'

export const name = 'dsh-voice-bridge'

export const inject = ['webServer']

const HERE = path.dirname(fileURLToPath(import.meta.url)) // .../dsh-voice-bridge/lib
const PKG_DIR = path.resolve(HERE, '..') // .../dsh-voice-bridge
const JARVIS = process.env.JARVIS_DIR || path.join(PKG_DIR, 'jarvis')

const MAX_RESTARTS = 3
const RETRY_DELAYS = [1000, 5000, 15000]
const STABLE_MS = 60_000 // 活过这么久算“稳了”，重启计数清零
const DEP_RETRY_MS = 60_000
const DEP_MAX_RETRY = 10
const LOG_MAX_BYTES = 5 * 1024 * 1024
const REQUIRED_MODULES = 'import sherpa_onnx, sounddevice, zstandard, numpy'

/**
 * 找 DSH 自带的 Python：显式环境变量 > 任意 runtime 目录 > 老的固定名字。
 * 不再写死 dsh-primary-runtime，DSH 换 runtime 名也不用改代码。
 */
function findPython() {
  if (process.env.JARVIS_PYTHON) return process.env.JARVIS_PYTHON
  const root = path.join(os.homedir(), '.dsh', 'dsh-runtimes')
  const exeIn = (dir) => path.join(root, dir, 'dependencies', 'python', 'python.exe')
  const preferred = exeIn('dsh-primary-runtime')
  if (fs.existsSync(preferred)) return preferred
  try {
    for (const dir of fs.readdirSync(root)) {
      const exe = exeIn(dir)
      if (fs.existsSync(exe)) return exe
    }
  } catch {
    /* 没有 runtime 目录就走下面的兜底路径 */
  }
  return preferred
}

function readDevice(defaultDevice) {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(JARVIS, 'device.json'), 'utf8'))
    if (Number.isFinite(Number(cfg.device))) return Number(cfg.device)
  } catch {
    /* 没有就交给脚本用默认设备 */
  }
  return defaultDevice
}

const DEFAULTS = {
  outboxPath: path.join(JARVIS, 'outbox.json'),
  ackPath: path.join(JARVIS, 'outbox.ack.json'),
  pythonExe: findPython(),
  scriptPath: path.join(JARVIS, 'jarvis_listen.py'),
  speakScriptPath: path.join(JARVIS, 'jarvis_speak.py'),
  device: readDevice(-1),
  autoStart: true,
  speakEnabled: true
}

export const Config = {
  '~standard': {
    version: 1,
    vendor: 'dsh-voice-bridge',
    validate(value) {
      const input = value && typeof value === 'object' ? value : {}
      const out = { ...DEFAULTS }
      for (const key of ['outboxPath', 'ackPath', 'pythonExe', 'scriptPath', 'speakScriptPath']) {
        if (typeof input[key] === 'string' && input[key].trim() !== '') out[key] = input[key]
      }
      if (Number.isFinite(Number(input.device))) out.device = Number(input.device)
      if (typeof input.autoStart === 'boolean') out.autoStart = input.autoStart
      if (typeof input.speakEnabled === 'boolean') out.speakEnabled = input.speakEnabled
      return { value: out }
    }
  }
}

const kids = { listen: null, speak: null }

// 每个子进程的看护状态：崩因、重启次数、重启定时器
const sup = {
  listen: { error: null, failures: 0, timer: null, tail: '', stopping: false, startedAt: 0 },
  speak: { error: null, failures: 0, timer: null, tail: '', stopping: false, startedAt: 0 }
}

let activeCfg = null
let depTimer = null
let depAttempts = 0
let deps = null // { ok, message, at, exe }
let childDevice = null
let logStream = null

function firstLine(text) {
  const lines = String(text || '')
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
  const line = lines.length ? lines[lines.length - 1] : ''
  return line.length > 300 ? line.slice(0, 300) : line
}

function recordError(scope, message) {
  const s = sup[scope]
  if (s) s.error = { message, at: Date.now() }
}

function lastProblem() {
  const errs = [sup.listen.error, sup.speak.error].filter(Boolean).sort((a, b) => b.at - a.at)
  if (errs.length) return errs[0].message
  if (deps && !deps.ok) return deps.message
  return null
}

function health() {
  return {
    listen: kids.listen !== null,
    speak: kids.speak !== null,
    error: lastProblem(),
    deps: deps ? { ok: deps.ok, message: deps.message } : null,
    restarts: { listen: sup.listen.failures, speak: sup.speak.failures }
  }
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

function writeJson(file, value) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, JSON.stringify(value), 'utf8')
    return true
  } catch {
    return false
  }
}

function sendJson(res, status, body) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store'
  })
  res.end(JSON.stringify(body))
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = ''
    let settled = false
    const finish = (value) => {
      if (settled) return
      settled = true
      resolve(value)
    }
    req.on('data', (chunk) => {
      if (settled) return
      data += chunk
      // 超限就断开并立刻收尾，否则这个请求会一直挂着不返回
      if (data.length > 1e6) {
        finish('')
        req.destroy()
      }
    })
    req.on('end', () => finish(data))
    req.on('error', () => finish(''))
  })
}

function ensureLog() {
  if (logStream) return logStream
  const file = path.join(JARVIS, 'voice.log')
  try {
    if (fs.statSync(file).size > LOG_MAX_BYTES) {
      fs.rmSync(file + '.1', { force: true })
      fs.renameSync(file, file + '.1')
    }
  } catch {
    /* 文件不存在就不用转 */
  }
  logStream = fs.createWriteStream(file, { flags: 'a' })
  return logStream
}

function closeLog() {
  if (!logStream) return
  try {
    logStream.end()
  } catch {
    /* 忽略 */
  }
  logStream = null
}

function spawnChild(key, exe, scriptArgs) {
  const log = ensureLog()
  const s = sup[key]
  s.stopping = false
  s.tail = ''
  const kid = spawn(exe, scriptArgs, {
    cwd: JARVIS,
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe']
  })
  const collect = (chunk) => {
    s.tail = (s.tail + chunk.toString('utf8')).slice(-2000)
  }
  if (kid.stdout) {
    kid.stdout.on('data', collect)
    kid.stdout.pipe(log, { end: false })
  }
  if (kid.stderr) {
    kid.stderr.on('data', collect)
    kid.stderr.pipe(log, { end: false })
  }
  s.startedAt = Date.now()
  kid.on('close', (code, signal) => {
    if (kids[key] === kid) kids[key] = null
    if (s.stopping) return
    // 活够久说明这次是真的跑起来了，之前几次崩不算连坐
    if (Date.now() - s.startedAt > STABLE_MS) s.failures = 0
    const label = key === 'listen' ? '监听进程' : '朗读进程'
    const reason = firstLine(s.tail) || '退出码 ' + code + (signal ? ' / ' + signal : '')
    recordError(key, label + '退出了：' + reason)
    scheduleRestart(key)
  })
  kids[key] = kid
  return kid
}

function scheduleRestart(key) {
  const s = sup[key]
  if (!activeCfg || s.stopping || s.timer) return
  if (s.failures >= MAX_RESTARTS) {
    if (s.error) s.error.message += '（已自动重启 ' + MAX_RESTARTS + ' 次仍失败，不再重试）'
    return
  }
  const delay = RETRY_DELAYS[Math.min(s.failures, RETRY_DELAYS.length - 1)]
  s.failures += 1
  s.timer = setTimeout(() => {
    s.timer = null
    startOne(key, activeCfg)
  }, delay)
  if (typeof s.timer.unref === 'function') s.timer.unref()
}

function fail(key, message) {
  if (key) recordError(key, message)
  return { ok: false, message }
}

function startOne(key, cfg) {
  const s = sup[key]
  if (!cfg) return fail(key, '插件未启动')
  if (kids[key]) return { ok: true, alreadyRunning: true, pid: kids[key].pid }
  if (s.timer) {
    clearTimeout(s.timer)
    s.timer = null
  }
  s.stopping = false
  if (key === 'speak' && !cfg.speakEnabled) return { ok: true, skipped: 'speakEnabled=false' }
  if (!fs.existsSync(cfg.pythonExe)) return fail(key, '找不到 Python：' + cfg.pythonExe)
  const script = key === 'listen' ? cfg.scriptPath : cfg.speakScriptPath
  if (!fs.existsSync(script)) return fail(key, '找不到脚本：' + script)
  const args = key === 'listen' ? [cfg.scriptPath, '--live'] : [cfg.speakScriptPath]
  if (key === 'listen' && cfg.device >= 0) args.push('--device', String(cfg.device))
  try {
    const kid = spawnChild(key, cfg.pythonExe, args)
    if (key === 'listen') childDevice = cfg.device
    s.error = null
    return { ok: true, pid: kid.pid, device: key === 'listen' ? cfg.device : undefined }
  } catch (err) {
    return fail(key, err instanceof Error ? err.message : String(err))
  }
}

/**
 * 起子进程之前先确认 Python 依赖装好了。
 * 缺包时直接给出“跑 install.ps1”的结论，并且每分钟自己再试一次，
 * 装完依赖不用重启 DSH 也能自己活过来。
 */
function checkDeps(cfg, cb) {
  if (deps && deps.exe === cfg.pythonExe && Date.now() - deps.at < DEP_RETRY_MS) {
    return cb(deps)
  }
  if (!fs.existsSync(cfg.pythonExe)) {
    deps = { ok: false, exe: cfg.pythonExe, at: Date.now(), message: '找不到 Python：' + cfg.pythonExe }
    return cb(deps)
  }
  execFile(
    cfg.pythonExe,
    ['-c', REQUIRED_MODULES],
    { timeout: 30_000, windowsHide: true },
    (err, _stdout, stderr) => {
      deps = err
        ? {
            ok: false,
            exe: cfg.pythonExe,
            at: Date.now(),
            message:
              '缺 Python 依赖（跑一次 install.ps1）：' +
              (firstLine(stderr) || err.message)
          }
        : { ok: true, exe: cfg.pythonExe, at: Date.now(), message: '' }
      cb(deps)
    }
  )
}

function startAll(cfg, forceCheck = false) {
  if (!cfg) return
  if (forceCheck) deps = null
  checkDeps(cfg, (res) => {
    if (!activeCfg) return
    if (!res.ok) {
      if (!depTimer && depAttempts < DEP_MAX_RETRY) {
        depAttempts += 1
        depTimer = setTimeout(() => {
          depTimer = null
          startAll(activeCfg)
        }, DEP_RETRY_MS)
        if (typeof depTimer.unref === 'function') depTimer.unref()
      }
      return
    }
    depAttempts = 0
    startOne('listen', cfg)
    startOne('speak', cfg)
  })
}

function startListener(cfg) {
  return startOne('listen', cfg)
}

function startSpeaker(cfg) {
  return startOne('speak', cfg)
}

function killKey(key) {
  const s = sup[key]
  if (s.timer) {
    clearTimeout(s.timer)
    s.timer = null
  }
  s.stopping = true
  const kid = kids[key]
  if (!kid) return false
  try {
    kid.kill()
  } catch {
    /* 已经退了 */
  }
  kids[key] = null
  return true
}

function stopAll() {
  const a = killKey('listen')
  const b = killKey('speak')
  if (depTimer) {
    clearTimeout(depTimer)
    depTimer = null
  }
  childDevice = null
  for (const key of Object.keys(sup)) {
    sup[key].failures = 0
    sup[key].error = null
  }
  closeLog()
  return a || b
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config && typeof config === 'object' ? config : {}) }
  activeCfg = cfg

  ctx.effect(() => {
    if (cfg.autoStart) startAll(cfg)
    return () => {
      activeCfg = null
      stopAll()
    }
  })

  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'prefix',
      path: '/api/voice-bridge',
      handler: async (req, res) => {
        const url = String(req.url ?? '')
        try {
          // 只接受本机界面（同源）的请求：防止你浏览器里随便一个网页偷偷读写这些接口
          const origin = String(req.headers.origin || '')
          if (origin && !/^(https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?|tauri:\/\/|app:\/\/)/i.test(origin)) {
            return sendJson(res, 403, { ok: false, message: 'cross-origin request blocked' })
          }
          if (req.method === 'GET' && url.startsWith('/api/voice-bridge/latest')) {
            const item = readJson(cfg.outboxPath)
            const ack = readJson(cfg.ackPath)
            if (!item || typeof item.seq !== 'number') {
              return sendJson(res, 200, { ok: true, seq: 0, text: '', acked: true, ...health() })
            }
            const acked = ack && typeof ack.seq === 'number' ? ack.seq : 0
            return sendJson(res, 200, {
              ok: true,
              seq: item.seq,
              text: typeof item.text === 'string' ? item.text : '',
              ts: item.ts ?? null,
              acked: item.seq <= acked,
              ...health()
            })
          }
          if (req.method === 'POST' && url.startsWith('/api/voice-bridge/ack')) {
            const raw = await readBody(req)
            let seq = 0
            try {
              seq = Number(JSON.parse(raw || '{}').seq) || 0
            } catch {
              return sendJson(res, 400, { ok: false, message: 'bad json' })
            }
            const ok = writeJson(cfg.ackPath, { seq, at: Date.now() })
            return sendJson(res, ok ? 200 : 500, { ok, seq })
          }
          if (req.method === 'POST' && url.startsWith('/api/voice-bridge/voice-turn')) {
            const ok = writeJson(path.join(JARVIS, 'voice_turn.json'), { at: Date.now() })
            return sendJson(res, ok ? 200 : 500, { ok })
          }
          if (req.method === 'POST' && url.startsWith('/api/voice-bridge/diag')) {
            const raw = await readBody(req)
            let payload = null
            try {
              payload = JSON.parse(raw || '{}')
            } catch {
              return sendJson(res, 400, { ok: false, message: 'bad json' })
            }
            const ok = writeJson(path.join(JARVIS, 'diag.json'), payload)
            return sendJson(res, ok ? 200 : 500, { ok })
          }
          if (req.method === 'GET' && url.startsWith('/api/voice-bridge/status')) {
            return sendJson(res, 200, {
              ok: true,
              listening: kids.listen !== null,
              listeningPid: kids.listen ? kids.listen.pid : null,
              speaking: kids.speak !== null,
              speakingPid: kids.speak ? kids.speak.pid : null,
              device: childDevice,
              packageDir: PKG_DIR,
              pythonExe: cfg.pythonExe,
              autoStart: cfg.autoStart,
              speakEnabled: cfg.speakEnabled,
              ...health()
            })
          }
          if (req.method === 'POST' && url.startsWith('/api/voice-bridge/start')) {
            startAll(cfg, true)
            return sendJson(res, 200, { ok: true, status: health() })
          }
          if (req.method === 'POST' && url.startsWith('/api/voice-bridge/stop')) {
            return sendJson(res, 200, { ok: true, stopped: stopAll(), status: health() })
          }
          return sendJson(res, 404, { ok: false, message: 'not found' })
        } catch (err) {
          return sendJson(res, 500, {
            ok: false,
            message: err instanceof Error ? err.message : String(err)
          })
        }
      }
    })
  )
}
