/**
 * dsh-voice-bridge —— 贾维斯语音助手（自带脚本与模型的完整插件）
 *
 * 装进 DSH 启动后自动做两件事：
 *   听：jarvis/jarvis_listen.py —— 唤醒词「贾维斯」→ 录音 → 本地 SenseVoice 转文字
 *       → 写 outbox.json → 界面插件 insertText + submit（自动发送）
 *   说：jarvis/jarvis_speak.py  —— 盯会话文件，把新回答用在线语音（晓晓/云希）念出来，
 *       在线失败自动退回系统语音；支持被打断与「续」续播
 *
 * 目录约定（本插件自带，不依赖外面任何路径）：
 *   <插件目录>/lib/      ← 本文件与界面脚本
 *   <插件目录>/jarvis/   ← Python 脚本、关键词、音色配置
 *   <插件目录>/models/   ← 唤醒词模型
 *
 * 可用接口：
 *   GET  /api/voice-bridge/latest   取新文字
 *   POST /api/voice-bridge/ack      插入回执
 *   POST /api/voice-bridge/voice-turn  标记“这轮是语音提问”
 *   GET  /api/voice-bridge/status   看状态
 *   POST /api/voice-bridge/start | stop
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'

export const name = 'dsh-voice-bridge'

export const inject = ['webServer']

const HERE = path.dirname(fileURLToPath(import.meta.url)) // .../dsh-voice-bridge/lib
const PKG_DIR = path.resolve(HERE, '..') // .../dsh-voice-bridge
const JARVIS = process.env.JARVIS_DIR || path.join(PKG_DIR, 'jarvis')

const DEFAULT_PYTHON = path.join(
  os.homedir(),
  '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'
)

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
  pythonExe: process.env.JARVIS_PYTHON || DEFAULT_PYTHON,
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
let childDevice = null
let logStream = null

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
    req.on('data', (chunk) => {
      data += chunk
      if (data.length > 1e6) req.destroy()
    })
    req.on('end', () => resolve(data))
    req.on('error', () => resolve(''))
  })
}

function ensureLog() {
  if (logStream) return logStream
  logStream = fs.createWriteStream(path.join(JARVIS, 'voice.log'), { flags: 'a' })
  return logStream
}

function spawnChild(key, exe, scriptArgs) {
  const log = ensureLog()
  const kid = spawn(exe, scriptArgs, {
    cwd: JARVIS,
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe']
  })
  if (kid.stdout) kid.stdout.pipe(log, { end: false })
  if (kid.stderr) kid.stderr.pipe(log, { end: false })
  const clear = () => {
    if (kids[key] === kid) kids[key] = null
  }
  kid.on('exit', clear)
  kid.on('error', clear)
  kids[key] = kid
  return kid
}

function startListener(cfg) {
  if (kids.listen) return { ok: true, alreadyRunning: true, pid: kids.listen.pid }
  if (!fs.existsSync(cfg.pythonExe)) return { ok: false, message: 'python 不存在: ' + cfg.pythonExe }
  if (!fs.existsSync(cfg.scriptPath)) return { ok: false, message: '脚本不存在: ' + cfg.scriptPath }
  try {
    const args = [cfg.scriptPath, '--live']
    if (cfg.device >= 0) args.push('--device', String(cfg.device))
    const kid = spawnChild('listen', cfg.pythonExe, args)
    childDevice = cfg.device
    return { ok: true, pid: kid.pid, device: cfg.device }
  } catch (err) {
    kids.listen = null
    return { ok: false, message: err instanceof Error ? err.message : String(err) }
  }
}

function startSpeaker(cfg) {
  if (!cfg.speakEnabled) return { ok: true, skipped: 'speakEnabled=false' }
  if (kids.speak) return { ok: true, alreadyRunning: true, pid: kids.speak.pid }
  if (!fs.existsSync(cfg.speakScriptPath)) {
    return { ok: false, message: '脚本不存在: ' + cfg.speakScriptPath }
  }
  try {
    const kid = spawnChild('speak', cfg.pythonExe, [cfg.speakScriptPath])
    return { ok: true, pid: kid.pid }
  } catch (err) {
    kids.speak = null
    return { ok: false, message: err instanceof Error ? err.message : String(err) }
  }
}

function killKey(key) {
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
  childDevice = null
  if (logStream) {
    try {
      logStream.end()
    } catch {
      /* 忽略 */
    }
    logStream = null
  }
  return a || b
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config && typeof config === 'object' ? config : {}) }

  ctx.effect(() => {
    if (cfg.autoStart) {
      startListener(cfg)
      startSpeaker(cfg)
    }
    return () => stopAll()
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
              return sendJson(res, 200, { ok: true, seq: 0, text: '', acked: true })
            }
            const acked = ack && typeof ack.seq === 'number' ? ack.seq : 0
            return sendJson(res, 200, {
              ok: true,
              seq: item.seq,
              text: typeof item.text === 'string' ? item.text : '',
              ts: item.ts ?? null,
              acked: item.seq <= acked
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
              speakEnabled: cfg.speakEnabled
            })
          }
          if (req.method === 'POST' && url.startsWith('/api/voice-bridge/start')) {
            return sendJson(res, 200, { listen: startListener(cfg), speak: startSpeaker(cfg) })
          }
          if (req.method === 'POST' && url.startsWith('/api/voice-bridge/stop')) {
            return sendJson(res, 200, { ok: true, stopped: stopAll() })
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
