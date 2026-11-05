/**
 * dsh-voice-bridge —— 界面那一面。
 *
 * 挂在 `conversation.input.left`（**列表**槽位）上，不渲染常驻内容，
 * 只为拿到标准属性 `inputActions`：
 *   - insertText(text, span) 原生插入，不抢焦点、可编辑、可撤销
 *   - submit()               插入后自动发送
 *
 * 这一版加了「语音确认条」：识别出来的话先填进输入框，浮一条
 * “N 秒后自动发送 / 现在发送 / 等等”，到点才发。说一半停了也不会立刻发出去。
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
    const CONFIRM_SECONDS = 2

    // 录音/思考/朗读的状态点，靠服务端算好（它能看到那几个标记文件）
    const STATE_STYLE = {
      idle: { color: '#8a8f98', text: '已就绪', tip: '喊「你好贾维斯」就能说话' },
      followup: { color: '#5b8def', text: '直接说', tip: '免唤醒中，不用喊名字，直接开口' },
      recording: { color: '#ff6b6b', text: '录音中', tip: '正在听你说，停 2.5 秒自动结束' },
      thinking: { color: '#f5a623', text: '思考中', tip: '话已经发出去了，等我回答' },
      speaking: { color: '#30a46c', text: '正在念', tip: '念的时候喊「你好贾维斯」可以打断' },
      down: { color: '#e5484d', text: '未就绪', tip: '麦克风或后台进程没起来' }
    }

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

    const btnStyle = {
      border: '1px solid rgba(255,255,255,0.25)',
      background: 'transparent',
      color: '#fff',
      fontSize: 12,
      padding: '3px 8px',
      borderRadius: 5,
      cursor: 'pointer'
    }

    function VoiceBridge(props) {
      const inputActions = props && props.inputActions
      const lastRef = React.useRef(readLast())
      const missRef = React.useRef(0)
      const actionsRef = React.useRef(inputActions)
      actionsRef.current = inputActions
      const [problem, setProblem] = React.useState(null)
      const [voiceState, setVoiceState] = React.useState('idle')
      // 免唤醒窗口开着时，只要你开始打字，就把它真正关掉（不再监听，要说话得重新喊名字）
      const stateRef = React.useRef('idle')
      const lastOffRef = React.useRef(0)
      // pending: { seq, left, deadline } —— 已填进输入框、等确认的那句话
      const [pending, setPending] = React.useState(null)
      const deadline = pending && pending.deadline

      const sendNow = React.useCallback(() => {
        setPending(null)
        const actions = actionsRef.current
        if (!actions || typeof actions.submit !== 'function') return
        try {
          actions.submit()
          // 告诉朗读进程：这一轮是用嘴说的，回答要念出来
          fetch('/api/voice-bridge/voice-turn', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ at: Date.now() })
          }).catch(() => undefined)
        } catch {
          /* 发送失败就留给你手动点 */
        }
      }, [])

      // 你在打字，就说明这轮不打算用嘴说了：把免唤醒窗口关掉，回到「喊名字」模式
      React.useEffect(() => {
        const onKey = (e) => {
          const typing = e.isComposing || e.key === 'Process' || e.key === 'Enter' ||
            e.key === 'Backspace' || (e.key && e.key.length === 1)
          if (!typing || stateRef.current !== 'followup') return
          if (Date.now() - lastOffRef.current < 800) return
          lastOffRef.current = Date.now()
          stateRef.current = 'idle'
          setVoiceState('idle')
          fetch('/api/voice-bridge/followup-off', { method: 'POST' }).catch(() => undefined)
        }
        window.addEventListener('keydown', onKey, true)
        return () => window.removeEventListener('keydown', onKey, true)
      }, [])

      // 倒计时：到点自动发送；点「等等」或「现在发送」会清掉 pending
      React.useEffect(() => {
        if (!deadline) return undefined
        const id = window.setInterval(() => {
          const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
          if (left > 0) {
            setPending((cur) => (cur ? { ...cur, left } : cur))
            return
          }
          window.clearInterval(id)
          sendNow()
        }, 250)
        return () => window.clearInterval(id)
      }, [deadline, sendNow])

      React.useEffect(() => {
        if (!inputActions || typeof inputActions.insertText !== 'function') return undefined
        let stopped = false

        // 后台没在监听就显示一行提示；连续 8 次（约 5 秒）都没起来才算数，避免启动那一下闪一下
        const noteHealth = (data) => {
          const broken = !data.listen || !!data.error
          missRef.current = broken ? missRef.current + 1 : 0
          if (missRef.current >= 8) setProblem(data.error || '贾维斯没在听（麦克风/进程没起来）')
          else if (missRef.current === 0) setProblem(null)
        }

        const tick = async () => {
          try {
            const res = await fetch('/api/voice-bridge/latest', { cache: 'no-store' })
            if (!res.ok) return
            const data = await res.json()
            if (stopped || !data) return
            noteHealth(data)
            if (typeof data.state === 'string') {
              stateRef.current = data.state
              setVoiceState(data.state)
            }
            if (data.seq <= lastRef.current) return
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
            // 先不发送：浮一条确认条，给几秒钟时间反悔/补话
            setPending({
              seq: data.seq,
              left: CONFIRM_SECONDS,
              deadline: Date.now() + CONFIRM_SECONDS * 1000
            })
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

      // 出问题时挤一小段红字（鼠标悬停看原因）
      const st = STATE_STYLE[voiceState] || STATE_STYLE.idle
      const pill = React.createElement(
        'span',
        {
          title: problem || st.tip,
          style: {
            display: 'inline-flex',
            alignItems: 'center',
            gap: 4,
            fontSize: 11,
            color: st.color,
            marginRight: 6,
            whiteSpace: 'nowrap',
            cursor: 'help'
          }
        },
        React.createElement('span', {
          style: {
            width: 6,
            height: 6,
            borderRadius: 3,
            background: st.color,
            animation: voiceState === 'recording' ? 'dsh-vb-pulse 1s ease-in-out infinite' : undefined
          }
        }),
        st.text
      )

      const anim = React.createElement(
        'style',
        null,
        '@keyframes dsh-vb-pulse{0%,100%{opacity:1}50%{opacity:.2}}'
      )

      // 语音确认条：几秒后自动发送，也可以取消
      const bar = pending
        ? React.createElement(
            'div',
            {
              style: {
                position: 'fixed',
                bottom: 108,
                left: '50%',
                transform: 'translateX(-50%)',
                zIndex: 9999,
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '8px 12px',
                borderRadius: 8,
                background: 'rgba(24,24,28,0.94)',
                color: '#fff',
                fontSize: 12,
                whiteSpace: 'nowrap',
                boxShadow: '0 6px 20px rgba(0,0,0,0.35)'
              }
            },
            React.createElement('span', null, '🎤 ' + pending.left + ' 秒后发送'),
            React.createElement('button', { style: btnStyle, onClick: sendNow }, '发送'),
            React.createElement('button', { style: btnStyle, onClick: () => setPending(null) }, '等等')
          )
        : null

      return React.createElement(React.Fragment, null, anim, pill, bar)
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
