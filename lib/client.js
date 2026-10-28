/**
 * dsh-voice-bridge —— 界面那一面。
 *
 * 挂在 `conversation.input.left`（**列表**槽位）上，不渲染任何东西，
 * 只为拿到标准属性 `inputActions`：
 *   - insertText(text, span) 原生插入，不抢焦点、可编辑、可撤销
 *   - submit()               插入后自动发送
 *
 * 朗读由服务端那边的 jarvis_speak.py 负责，这里不掺和。
 *
 * 注意：绝不能挂 `conversation.input.activity` —— 那是**单占位**槽位，
 * 自带语音输入插件正占着它；再挂一个会让它启动失败，整个应用起不来。
 */
window.__ModuleLoader__.load({
  id: 'dsh-voice-bridge',
  factory: (require) => {
    var module = { exports: {} }

    const React = require('react')
    const STORAGE_KEY = 'dsh-voice-bridge.last-seq'

    function readLast() {
      try {
        return Number(window.localStorage.getItem(STORAGE_KEY) || 0) || 0
      } catch {
        return 0
      }
    }

    function writeLast(seq) {
      try {
        window.localStorage.setItem(STORAGE_KEY, String(seq))
      } catch {
        /* 忽略 */
      }
    }

    function VoiceBridge(props) {
      const inputActions = props && props.inputActions
      const lastRef = React.useRef(readLast())

      React.useEffect(() => {
        if (!inputActions || typeof inputActions.insertText !== 'function') return undefined
        let stopped = false

        const tick = async () => {
          try {
            const res = await fetch('/api/voice-bridge/latest', { cache: 'no-store' })
            if (!res.ok) return
            const data = await res.json()
            if (stopped || !data || data.seq <= lastRef.current) return
            if (!data.text) {
              lastRef.current = data.seq
              writeLast(data.seq)
              return
            }
            const span = inputActions.captureInsertion()
            const inserted = inputActions.insertText(data.text, span)
            // 插入被拒（草稿版本变了 / 编辑器不可编辑）就保持未读，下个周期重试
            if (!inserted) return
            lastRef.current = data.seq
            writeLast(data.seq)
            fetch('/api/voice-bridge/ack', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ seq: data.seq })
            }).catch(() => undefined)
            // 插入成功后再发送；等半秒让草稿状态落定，避免发出空消息
            if (typeof inputActions.submit === 'function') {
              window.setTimeout(() => {
                if (stopped) return
                try {
                  inputActions.submit()
                  // 告诉朗读进程：这一轮是你用嘴说的，回答要念出来
                  fetch('/api/voice-bridge/voice-turn', {
                    method: 'POST',
                    headers: { 'content-type': 'application/json' },
                    body: JSON.stringify({ at: Date.now() })
                  }).catch(() => undefined)
                } catch {
                  /* 发送失败就留给你手动点 */
                }
              }, 500)
            }
          } catch {
            /* 服务端没起来就下一轮再试 */
          }
        }

        tick()
        const timer = window.setInterval(tick, 700)
        return () => {
          stopped = true
          window.clearInterval(timer)
        }
      }, [inputActions])

      return null
    }

    function apply(ctx) {
      ctx.slots.inject('conversation.input.left', () =>
        ctx.slots.register(
          {
            name: 'conversation.input.left',
            id: 'dsh-voice-bridge',
            order: 100
          },
          VoiceBridge
        )
      )
    }

    module.exports = { apply, inject: ['slots'] }
    return module.exports
  }
})
