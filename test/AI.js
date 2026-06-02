// ==UserScript==
// @name         Bangumi娘 Powered by AI
// @version      1.1.0
// @description  让Bangumi娘来瑞萍一下
// @author       wataame
// @match        https://bgm.tv/*
// @match        https://chii.in/*
// @match        https://bangumi.tv/*
// @license      MIT
// ==/UserScript==

;(function () {
  'use strict'

  // ==========================================
  // 样式注入 (CSS)
  // ==========================================
  function injectStyles() {
    const css = `
            /* 脉动动画 */
            @keyframes sparkle-pulse {
                0%, 49%, 100% { transform: scale(1); }
                50%, 99% { transform: scale(1.2); }
            }

            .sparkle-animation {
                display: inline-block;
                animation: sparkle-pulse 1s infinite;
            }

            /* 个性化面板内部样式 */
            .bangumi-ai-tab-content {
                padding: 10px;
            }
            .bangumi-ai-tab-content .section {
                margin-bottom: 20px;
                padding-bottom: 15px;
                border-bottom: 1px solid #eee;
            }
            .bangumi-ai-tab-content h3 {
                margin-bottom: 15px;
                font-size: 14px;
                font-weight: bold;
                color: #444;
            }
            .bangumi-ai-tab-content label {
                display: block;
                margin-bottom: 5px;
                font-weight: bold;
                color: #666;
            }
            .bangumi-ai-tab-content input[type="text"],
            .bangumi-ai-tab-content input[type="password"],
            .bangumi-ai-tab-content input[type="number"],
            .bangumi-ai-tab-content select {
                width: 100%;
                padding: 6px;
                border: 1px solid #ddd;
                border-radius: 4px;
                box-sizing: border-box;
                margin-bottom: 10px;
                font-size: 13px;
            }
            .bangumi-ai-tab-content textarea {
                width: 100%;
                min-height: 120px;
                padding: 6px;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 12px;
                line-height: 1.5;
                resize: vertical;
                box-sizing: border-box;
                font-family: monospace;
            }
            .bangumi-ai-tab-content .row {
                display: flex;
                align-items: center;
                gap: 10px;
                margin-bottom: 10px;
            }
            .bangumi-ai-tab-content .btn-group {
                margin-top: 10px;
                display: flex;
                gap: 10px;
            }
            .bangumi-ai-tab-content button {
                cursor: pointer;
                padding: 5px 12px;
                border-radius: 4px;
                border: 1px solid #ccc;
                background: #fff;
                font-size: 12px;
            }
            .bangumi-ai-tab-content button.primary {
                background: #f09199;
                color: white;
                border-color: #f09199;
            }
            .bangumi-ai-tab-content button.primary:hover {
                background: #e07179;
            }
            .bangumi-ai-tab-content button.danger {
                background: #ff4d4f;
                color: white;
                border-color: #ff4d4f;
            }
            .bangumi-ai-tab-content .checkbox-wrapper {
                display: flex;
                align-items: center;
                gap: 8px;
                margin-bottom: 8px;
            }
            .bangumi-ai-tab-content .checkbox-wrapper label {
                margin-bottom: 0;
                font-weight: normal;
            }
            #robot_balloon .speech ul {
                display: flex;
                flex-wrap: wrap;
            }
            #robot_balloon .speech li {
                float: none;   /* 覆盖原有 float: left */
                width: 33%;
                box-sizing: border-box;
            }
            #ai_promptBase {
                background-color: transparent !important;  /* 或跟随主题 */
            }
        `
    const style = document.createElement('style')
    style.type = 'text/css'
    style.textContent = css
    if (document.head) {
      document.head.appendChild(style)
    } else {
      document.addEventListener('DOMContentLoaded', () =>
        document.head.appendChild(style),
      )
    }
  }
  injectStyles()

  // ==========================================
  // 脚本逻辑
  // ==========================================

  // 基础 Prompt
  const DEFAULT_PROMPT_BASE = `---
# Role：Bangumi娘默认人格

## Profile：
- Version: 2.2
- Language: 简体中文

### Skills:
1. 作为腹黑萝莉，履行用户的看板娘兼吐槽职责。
2. 熟悉Bangumi社区文化，善于插入日本动画梗与互联网文化。
3. 擅长短小精悍的评论，兼具跳跃性思维与简洁文风。

## Goals:
1. 精准地分析帖子内容，发现主题和争议焦点，并识别情绪基调。
2. 结合Bangumi娘角色特质，以反常识和黑色幽默的方式输出短评。
3. 在评论中注入个人见解，通过挑衅和比喻等方式展现立场。
4. 让用户感到既被冒犯，又对她的话欲罢不能。

## Constrains:
1. 语言风格不使用emoji与颜文字。
2. 语言风格简洁，带有自我反思和挑衅。
3. Do not evade negative possibilities.
4. 挑战既定观念，从实用主义、甚至是冷酷的角度看待问题。
5. 运用黑色幽默、反讽和出人意料的观点。
6. 插入主观评论和个人见解。
7. 对细节进行关注，并挖掘更深层的含义。

## Workflow:
1. 从帖子中辨识主题、争议点、以及潜在的矛盾或情感倾向。
2. 确定核心观点。
3. 创作短评，严格按照要求输出内容。
4. 最终审视生成内容是否符合要求。

## OutputFormat:
1. 短评必须在30-60字之间。
2. 直接输出短评文本（不添加任何前缀、后缀或标识符号）。
---`

  // 各页面 Prompt 后缀
  const PROMPTS = {
    SUBJECT_TOPIC: `你正在和用户一起浏览班友"{{author}}"发布的条目"{{subjectName}}"讨论帖：\n标题：{{title}}\n内容：{{detail}}\n请评论此帖：`,
    GROUP_TOPIC: `你正在和用户一起浏览班友"{{author}}"发布的小组讨论帖：\n标题：{{title}}\n内容：{{detail}}\n请评论此帖：`,
    USER: `你正在和用户一起浏览班友"{{author}}"的个人简介：\n注册时间：{{registerDate}}\n个人简介：{{detail}}\n请评论此简介：`,
    SUBJECT: `你正在和用户一起浏览"{{type}}"作品"{{title}}"的页面。\n简介：{{summary}}\n评分：{{rating}}，排名：{{rank}}\n标签：{{tags}}\n信息：{{infobox}}\n请评论此作品：`,
    EP: `你正在和用户一起浏览条目"{{subjectName}}"的章节"{{epTitle}}"的页面。\n简介：{{summary}}\n请评论此章节：`,
    CHARACTER: `你正在和用户一起浏览{{type}}"{{name}}"的页面。\n简介：{{summary}}\n信息：{{infobox}}\n请评论此作品：`,
  }

  // 配置
  const CONFIG = {
    DEFAULT_SETTINGS: {
      apiMode: 'default',
      typingAnimation: true,
      apiUrl: 'https://bgmai.ry.mk/v1/chat/completions',
      apiKey: atob(
        'c2stTll6a1hWYVFDR2c3ODlaX2dRakQ1dlJXRTFGcmtZQWJjQllDb05PQm9ybVVQaUtnWF9weWQ3SXN0c1U=',
      ),
      modelName: 'gemini-2.0-flash-exp',
      temperature: 1.2,
      promptBase: DEFAULT_PROMPT_BASE,
      streamResponse: false,
      enableOnSubjectPages: true,
      savedPrompts: {
        default: {
          name: '默认 Prompt',
          content: DEFAULT_PROMPT_BASE,
          isDefault: true,
        },
      },
      currentPrompt: 'default',
    },
    PATHS: {
      SUBJECT_TOPIC: /^\/subject\/topic\/\d+(?:\/(?!edit(?:\/|$)).*)?$/,
      GROUP_TOPIC: /^\/group\/topic\/\d+(?:\/(?!edit(?:\/|$)).*)?$/,
      BLOG: /\/blog\/\d+(?:\/(?!edit(?:\/|$)).*)?$/,
      USER: /^\/user\/[^/]+$/,
      SUBJECT: /^\/subject\/\d+$/,
      EP: /^\/ep\/\d+$/,
      CHARACTER: /^\/(character|person)\/\d+$/,
    },
  }

  // 云端存储工具
  const StorageUtils = {
    KEYS: {
      CONFIG: 'bangumi_ai_config',
    },

    // 检查云端存储是否可用
    isReady: () => typeof chiiApp !== 'undefined' && chiiApp.cloud_settings,

    // 获取设置
    getSettings: () => {
      if (!StorageUtils.isReady()) {
        return CONFIG.DEFAULT_SETTINGS
      }
      try {
        const stored = chiiApp.cloud_settings.get(StorageUtils.KEYS.CONFIG)
        const settings = { ...CONFIG.DEFAULT_SETTINGS, ...(stored || {}) }

        if (settings.temperature !== undefined) {
          settings.temperature = parseFloat(settings.temperature)
        }
        if (settings.streamResponse !== undefined) {
          settings.streamResponse = String(settings.streamResponse) === 'true'
        }

        // 向后兼容：旧版已保存自定义 API Key 的用户自动切换到自定义模式
        if (
          !stored?.apiMode &&
          stored?.apiKey &&
          stored.apiKey !== CONFIG.DEFAULT_SETTINGS.apiKey
        ) {
          settings.apiMode = 'custom'
        }

        return settings
      } catch (e) {
        return CONFIG.DEFAULT_SETTINGS
      }
    },

    // 保存设置
    saveSettings: (settings) => {
      if (!StorageUtils.isReady()) return
      chiiApp.cloud_settings.update({ [StorageUtils.KEYS.CONFIG]: settings })
      chiiApp.cloud_settings.save()
    },
  }

  // 工具函数
  const utils = {
    $: (s, p = document) => p.querySelector(s),
    debounce: (fn, wait) => {
      let t
      return (...args) => {
        clearTimeout(t)
        t = setTimeout(() => fn(...args), wait)
      }
    },
    throttle: (fn, limit) => {
      let inThrottle
      return (...args) => {
        if (!inThrottle) {
          fn(...args)
          inThrottle = true
          setTimeout(() => (inThrottle = false), limit)
        }
      }
    },
    updateButtonText: (sel, newText, dur = 1000) => {
      const btn = utils.$(sel)
      if (btn) {
        const orig = btn.textContent
        btn.textContent = newText
        setTimeout(() => (btn.textContent = orig), dur)
      }
    },
    processText: (text) => {
      return text
        .replace(/<br\s*\/?>/gi, ' ')
        .replace(/<[^>]+>/g, '')
        .replace(/</g, '<')
        .replace(/>/g, '>')
        .replace(/&/g, '&')
        .replace(/"/g, '"')
        .replace(/ /g, ' ')
    },
    createPulsingLoader: () => {
      const loaderSpan = document.createElement('span')
      loaderSpan.className = 'sparkle-animation'
      loaderSpan.textContent = '✨'
      return loaderSpan
    },
  }

  // 获取页面信息 (保持不变)
  const getPageInfo = {
    content: (path) => {
      if (path.startsWith('/blog/')) {
        const el = document.evaluate(
          '//*[@id="entry_content"]',
          document,
          null,
          XPathResult.FIRST_ORDERED_NODE_TYPE,
          null,
        ).singleNodeValue
        return el?.textContent.trim() || ''
      }
      const selector =
        path.match(CONFIG.PATHS.SUBJECT_TOPIC) ||
        path.match(CONFIG.PATHS.GROUP_TOPIC)
          ? 'div.topic_content'
          : ''
      return selector
        ? Array.from(document.querySelectorAll(selector))
            .map((el) => el.textContent)
            .join('\n')
            .trim()
        : ''
    },
    title: () => {
      const path = location.pathname
      if (path.startsWith('/blog/'))
        return (
          document
            .evaluate(
              '//*[@id="pageHeader"]/h1/text()',
              document,
              null,
              XPathResult.FIRST_ORDERED_NODE_TYPE,
              null,
            )
            .singleNodeValue?.textContent.trim() || ''
        )
      if (path.startsWith('/subject/topic/'))
        return document.querySelector('#header > h1')?.textContent.trim() || ''
      if (path.startsWith('/group/topic/')) {
        const el = document.querySelector('#pageHeader > h1')
        if (el) {
          const nodes = Array.from(el.childNodes)
          const brIndex = nodes.findIndex((node) => node.nodeName === 'BR')
          if (brIndex !== -1 && brIndex + 1 < nodes.length)
            return nodes[brIndex + 1].textContent.trim()
        }
      }
      return ''
    },
    userInfo: (xpath) => {
      const el = document.evaluate(
        xpath,
        document,
        null,
        XPathResult.FIRST_ORDERED_NODE_TYPE,
        null,
      ).singleNodeValue
      if (!el) return { name: '未知用户', id: null }
      const userId =
        el.getAttribute('href')?.match(/\/user\/([^/]+)/)?.[1] || null
      const userName = el.textContent.trim().replace(/\s+/g, ' ').trim()
      return { name: userName, id: userId }
    },
    postAuthor: () => {
      const path = location.pathname
      if (
        path.match(CONFIG.PATHS.SUBJECT_TOPIC) ||
        path.match(CONFIG.PATHS.GROUP_TOPIC)
      ) {
        return getPageInfo.userInfo(
          `//*[@id="post_${getPageInfo.postId()}"]/div[2]/strong/a`,
        )
      }
      return null
    },
    blogAuthor: () =>
      location.pathname.startsWith('/blog/')
        ? getPageInfo.userInfo('//*[@id="pageHeader"]/h1/span/a[1]')
        : null,
    profileUser: () =>
      location.pathname.match(CONFIG.PATHS.USER)
        ? getPageInfo.userInfo(
            '//*[@id="headerProfile"]/div/div[contains(@class, "headerContainer")]/h1/div[contains(@class, "inner")]/div[contains(@class, "name")]/a',
          )
        : null,
    subjectInfo: () => {
      if (!location.pathname.match(CONFIG.PATHS.SUBJECT)) return null
      const titleContainer = document.querySelector('#headerSubject > h1')
      const mainTitle =
        titleContainer
          ?.querySelector('a[property="v:itemreviewed"]')
          ?.textContent.trim() || '未知条目'
      const types = Array.from(
        titleContainer?.querySelectorAll('small.grey') || [],
      )
        .map((el) => el.textContent.trim())
        .join('')
        .replace(/\s+/g, '')
      const summary =
        document.querySelector('#subject_summary')?.textContent.trim() || ''
      const rating =
        document
          .querySelector('#panelInterestWrapper span[property="v:average"]')
          ?.textContent.trim() || '暂无评分'
      const rank =
        document
          .querySelector('#panelInterestWrapper small.alarm')
          ?.textContent.trim() || '暂无排名'
      const tags = Array.from(
        document
          .querySelector(
            '#subject_detail > div.subject_tag_section > div.inner',
          )
          ?.querySelectorAll('a.l') || [],
      )
        .map((link) => link.querySelector('span')?.textContent.trim() || '')
        .filter((tag) => tag && tag !== '更多 +')
      const infobox =
        Array.from(
          document.querySelector('#infobox')?.querySelectorAll('li') || [],
        )
          .map((li) => {
            if (li.classList.contains('sub_container')) return ''
            const tip = li
              .querySelector('.tip')
              ?.textContent.replace(/[:：]\s*$/, '')
            const value = li.textContent.substring(tip?.length || 0).trim()
            return tip && value ? `${tip}：${value}` : ''
          })
          .filter((item) => item)
          .join('\n') || '暂无详细信息'
      return {
        title: mainTitle,
        type: types,
        summary,
        rating,
        rank,
        tags: tags.join('、') || '暂无标签',
        infobox,
      }
    },
    episodeInfo: () => {
      if (!location.pathname.match(CONFIG.PATHS.EP)) return null
      const epTitle =
        document
          .querySelector('h2.title')
          ?.textContent.replace(/\[修改\]/g, '')
          .trim() || '未知章节'
      const subjectName =
        document.querySelector('#headerSubject > h1 > a')?.textContent.trim() ||
        '未知条目'
      const summary =
        document.querySelector('.epDesc')?.textContent.trim() || '暂无简介'
      return { epTitle, subjectName, summary }
    },
    characterInfo: () => {
      const isCharacter = location.pathname.match(/^\/character\/\d+$/)
      const isPerson = location.pathname.match(/^\/person\/\d+$/)
      if (!isCharacter && !isPerson) return null
      const nameContainer = document.querySelector('#headerSubject > h1')
      const mainName =
        nameContainer?.querySelector('a')?.textContent.trim() || ''
      const summary =
        document
          .querySelector('#columnCrtB > div.detail')
          ?.textContent.trim() || '暂无简介'
      const infobox =
        Array.from(
          document.querySelector('#infobox')?.querySelectorAll('li') || [],
        )
          .map((li) => {
            if (li.classList.contains('sub_container')) return ''
            const tip = li
              .querySelector('.tip')
              ?.textContent.replace(/[:：]\s*$/, '')
            const value = li.textContent.substring(tip?.length || 0).trim()
            return tip && value ? `${tip}：${value}` : ''
          })
          .filter((item) => item)
          .join('\n') || '暂无详细信息'
      return {
        name: mainName,
        summary,
        infobox,
        type: isCharacter ? '虚拟角色' : '现实人物',
      }
    },
    registerDate: () =>
      document
        .evaluate(
          '//*[@id="user_home"]/div[1]/ul/li[1]/span[2]',
          document,
          null,
          XPathResult.FIRST_ORDERED_NODE_TYPE,
          null,
        )
        .singleNodeValue?.textContent.trim() || '未知时间',
    postId: () => {
      const path = location.pathname
      if (
        !path.match(CONFIG.PATHS.SUBJECT_TOPIC) &&
        !path.match(CONFIG.PATHS.GROUP_TOPIC)
      )
        return null
      const postEl = document.evaluate(
        `//*[starts-with(@id, 'post_')]/div[2]/div[1]`,
        document,
        null,
        XPathResult.FIRST_ORDERED_NODE_TYPE,
        null,
      ).singleNodeValue
      return postEl?.closest('[id^="post_"]')?.id.replace('post_', '') || null
    },
    subjectName: () => {
      if (!location.pathname.match(CONFIG.PATHS.SUBJECT_TOPIC))
        return '未知条目'
      const el = document.evaluate(
        '//*[@id="subject_inner_info"]/a/text()',
        document,
        null,
        XPathResult.FIRST_ORDERED_NODE_TYPE,
        null,
      ).singleNodeValue
      return el?.textContent.trim() || '未知条目'
    },
  }

  // 默认模式：调用服务端 /generate 端点（非 OpenAI 格式，带服务端缓存）
  async function requestAIDefault(
    content,
    prompt = '',
    regenerate = false,
    retries = 3,
    timeout = 15000,
  ) {
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), timeout)
    try {
      const body = { content, regenerate }
      if (prompt) body.prompt = prompt
      const res = await fetch('https://bgmai.ry.mk/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      })
      clearTimeout(timeoutId)
      if (!res.ok) throw new Error(`HTTP错误，状态码: ${res.status}`)
      const data = await res.json()
      return markdownToHtml(data.text || '生成失败，请稍后重试')
    } catch (e) {
      clearTimeout(timeoutId)
      if (e.name === 'AbortError') throw new Error('请求超时')
      if (retries === 0) {
        utils.$('#robot_speech').textContent = `请求失败: ${e.message}`
        return null
      }
      await new Promise((resolve) => setTimeout(resolve, 1000))
      return requestAIDefault(content, prompt, regenerate, retries - 1, timeout)
    }
  }

  async function requestAI(
    content,
    prompt,
    retries = 3,
    timeout = 10000,
    regenerate = false,
  ) {
    // 从云端获取配置 (同步)
    const settings = StorageUtils.getSettings()

    // 默认模式：使用服务端专属端点（带缓存），传入用户自定义 prompt
    if (settings.apiMode !== 'custom') {
      return requestAIDefault(content, prompt, regenerate, retries, timeout)
    }

    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), timeout)
    try {
      const res = await fetch(settings.apiUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${settings.apiKey}`,
        },
        body: JSON.stringify({
          model: settings.modelName,
          messages: [
            { role: 'system', content: prompt },
            { role: 'user', content },
          ],
          temperature: settings.temperature,
          stream: settings.streamResponse,
        }),
        signal: controller.signal,
      })
      clearTimeout(timeoutId)
      if (!res.ok) throw new Error(`HTTP错误，状态码: ${res.status}`)

      if (settings.streamResponse) {
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let fullText = ''
        const robotSpeech = utils.$('#robot_speech')
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          const chunk = decoder.decode(value, { stream: true })
          try {
            const lines = chunk
              .split('\n')
              .filter((line) => line.trim() && line.startsWith('data: '))
            for (const line of lines) {
              const jsonData = JSON.parse(line.slice(6))
              if (jsonData.choices?.[0]?.delta?.content) {
                fullText += jsonData.choices[0].delta.content
                if (robotSpeech)
                  robotSpeech.innerHTML = markdownToHtml(fullText)
              }
            }
          } catch (e) {
            // 忽略流式解析错误
          }
        }
        return fullText
      } else {
        const data = await res.json()
        return markdownToHtml(
          data.choices?.[0]?.message?.content || '生成失败，请检查API返回格式',
        )
      }
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('请求超时')
      if (retries === 0) {
        utils.$('#robot_speech').textContent = `请求失败: ${e.message}`
        return null
      }
      await new Promise((resolve) => setTimeout(resolve, 1000))
      return requestAI(content, prompt, retries - 1, timeout, regenerate)
    } finally {
      clearTimeout(timeoutId)
    }
  }

  function escapeHtml(unsafe) {
    if (!unsafe) return ''
    return unsafe
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;')
  }

  function markdownToHtml(text) {
    let safeText = escapeHtml(text)

    return safeText
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/~~(.*?)~~/g, '<del>$1</del>')
      .replace(/`(.*?)`/g, '<code>$1</code>')
      .replace(/\n/g, ' ')
  }

  // 打字机动画：HTML 标签原子插入，实体符号/普通字符逐个显示
  async function typewriterHTML(element, html, speed = 25) {
    const segments = []
    let i = 0
    while (i < html.length) {
      if (html[i] === '<') {
        const end = html.indexOf('>', i)
        if (end !== -1) {
          segments.push({ s: html.substring(i, end + 1), delay: false })
          i = end + 1
          continue
        }
      } else if (html[i] === '&') {
        const end = html.indexOf(';', i)
        if (end !== -1 && end - i <= 8) {
          segments.push({ s: html.substring(i, end + 1), delay: true })
          i = end + 1
          continue
        }
      }
      segments.push({ s: html[i], delay: true })
      i++
    }
    let displayed = ''
    for (const { s, delay } of segments) {
      displayed += s
      element.innerHTML = displayed
      if (delay) await new Promise((r) => setTimeout(r, speed))
    }
  }

  // 复制功能
  function copyTextToClipboard() {
    const robotSpeech = utils.$('#robot_speech')
    if (!robotSpeech) return

    const text =
      utils.processText(robotSpeech.innerHTML) +
      '[right][url=https://bgm.tv/dev/app/1624]✨[/url][/right]'

    navigator.clipboard
      .writeText(text)
      .then(() => utils.updateButtonText('.copy-button a', '已复制✨', 1000))
      .catch(() => {
        alert('复制失败，请检查浏览器权限设置')
      })
  }

  // 自动总结
  async function autoSummary(force = false) {
    // 从云端获取配置 (同步)
    const settings = StorageUtils.getSettings()
    const robotSpeech = utils.$('#robot_speech')

    if (robotSpeech) {
      robotSpeech.innerHTML = 'Bangumi娘思考中… '
      robotSpeech.appendChild(utils.createPulsingLoader())
    }

    let content = ''
    const path = location.pathname

    if (path.match(CONFIG.PATHS.CHARACTER)) {
      const info = getPageInfo.characterInfo()
      if (!info) {
        if (robotSpeech) robotSpeech.textContent = '获取角色信息失败...'
        return
      }
      content = PROMPTS.CHARACTER.replace(/{{type}}/g, info.type)
        .replace('{{name}}', info.name)
        .replace('{{summary}}', info.summary)
        .replace('{{infobox}}', info.infobox)
    } else if (path.match(CONFIG.PATHS.SUBJECT)) {
      const info = getPageInfo.subjectInfo()
      if (!info) {
        if (robotSpeech) robotSpeech.textContent = '获取条目信息失败...'
        return
      }
      content = PROMPTS.SUBJECT.replace('{{title}}', info.title)
        .replace('{{type}}', info.type)
        .replace('{{rating}}', info.rating)
        .replace('{{rank}}', info.rank)
        .replace('{{summary}}', info.summary)
        .replace('{{tags}}', info.tags)
        .replace('{{infobox}}', info.infobox)
    } else if (path.match(CONFIG.PATHS.EP)) {
      const info = getPageInfo.episodeInfo()
      if (!info) {
        if (robotSpeech) robotSpeech.textContent = '获取章节信息失败...'
        return
      }
      content = PROMPTS.EP.replace('{{subjectName}}', info.subjectName)
        .replace('{{epTitle}}', info.epTitle)
        .replace('{{summary}}', info.summary)
    } else if (path.match(CONFIG.PATHS.USER)) {
      const user = getPageInfo.profileUser()
      const bio =
        document.querySelector('#user_home .intro')?.textContent.trim() || ''
      const registerDate = getPageInfo.registerDate()
      content = PROMPTS.USER.replace('{{author}}', user?.name || '未知用户')
        .replace('{{registerDate}}', registerDate)
        .replace('{{detail}}', bio)
    } else if (path.match(CONFIG.PATHS.SUBJECT_TOPIC)) {
      const title = getPageInfo.title() || '无标题'
      const postContent = getPageInfo.content(path) || ''
      const author = getPageInfo.postAuthor()
      const subjectName = getPageInfo.subjectName()
      content = PROMPTS.SUBJECT_TOPIC.replace(
        '{{author}}',
        author?.name || '未知用户',
      )
        .replace('{{title}}', title)
        .replace('{{detail}}', postContent)
        .replace('{{subjectName}}', subjectName)
    } else if (
      path.match(CONFIG.PATHS.GROUP_TOPIC) ||
      path.startsWith('/blog/')
    ) {
      const title = getPageInfo.title() || '无标题'
      const postContent = getPageInfo.content(path) || ''
      const author = path.startsWith('/blog/')
        ? getPageInfo.blogAuthor()
        : getPageInfo.postAuthor()
      content = PROMPTS.GROUP_TOPIC.replace(
        '{{author}}',
        author?.name || '未知用户',
      )
        .replace('{{title}}', title)
        .replace('{{detail}}', postContent)
    }

    if (!content && robotSpeech) {
      robotSpeech.textContent = '连一个字都打不出来吗…可怜的人类。'
      return
    }

    const prompt = settings.promptBase || DEFAULT_PROMPT_BASE
    const summary = await requestAI(content, prompt, 3, 10000, force)
    if (summary && robotSpeech) {
      if (settings.typingAnimation !== false) {
        await typewriterHTML(robotSpeech, summary)
      } else {
        robotSpeech.innerHTML = summary
      }
    } else if (robotSpeech) {
      robotSpeech.textContent = '生成评论失败，请检查设置和网络连接'
    }
  }

  function registerSettingsTab() {
    window.chiiLib.ukagaka.addPanelTab({
      tab: 'bangumi_ai',
      label: 'AI班娘',
      type: 'custom',
      customContent: function () {
        return `
                    <div class="bangumi-ai-tab-content">
                        <div class="section">
                            <h3>基本设置</h3>
                            <label>API模式：</label>
                            <select id="ai_apiMode">
                                <option value="default">默认</option>
                                <option value="custom">自定义</option>
                            </select>

                            <div id="ai_customFields">
                                <label>API地址：</label><input type="text" id="ai_apiUrl">
                                <label>API密钥：</label><input type="password" id="ai_apiKey">
                                <label>模型名称：</label><input type="text" id="ai_modelName">

                                <label>温度 (Temperature)：<span id="ai_tempVal">1.2</span></label>
                                <div class="row">
                                    <input type="range" id="ai_temperatureRange" min="0" max="2" step="0.1" style="flex:1;">
                                </div>

                                <div class="checkbox-wrapper">
                                    <input type="checkbox" id="ai_streamResponse">
                                    <label for="ai_streamResponse">启用流式响应 (Stream)</label>
                                </div>
                            </div>

                            <div class="checkbox-wrapper">
                                <input type="checkbox" id="ai_enableOnSubjectPages">
                                <label for="ai_enableOnSubjectPages">在条目页面启用</label>
                            </div>
                            <div class="checkbox-wrapper">
                                <input type="checkbox" id="ai_typingAnimation">
                                <label for="ai_typingAnimation">启用打字机动画</label>
                            </div>
                        </div>

                        <div class="section">
                            <h3>人格设定 (Prompt)</h3>
                            <div class="row">
                                <select id="ai_promptSelector" style="flex:1; margin-bottom:0;"></select>
                                <button class="primary" id="ai_addPromptBtn">新增</button>
                                <button class="danger" id="ai_delPromptBtn">删除</button>
                            </div>
                            <textarea id="ai_promptBase"></textarea>
                        </div>

                        <div class="btn-group" style="justify-content: flex-end;">
                            <button class="primary" id="ai_saveBtn">保存</button>
                            <button id="ai_resetBtn">恢复默认</button>
                        </div>
                        <div id="ai_statusMsg" style="text-align:right; margin-top:5px; color:var(--primary-color); height:20px;"></div>
                    </div>
                `
      },
      onInit: function (tabSelector, $tabContent) {
        const container = $tabContent[0]
        let currentSettings = null

        // 元素引用
        const els = {
          apiMode: container.querySelector('#ai_apiMode'),
          customFields: container.querySelector('#ai_customFields'),
          apiUrl: container.querySelector('#ai_apiUrl'),
          apiKey: container.querySelector('#ai_apiKey'),
          modelName: container.querySelector('#ai_modelName'),
          tempRange: container.querySelector('#ai_temperatureRange'),
          tempVal: container.querySelector('#ai_tempVal'),
          stream: container.querySelector('#ai_streamResponse'),
          subjectEnable: container.querySelector('#ai_enableOnSubjectPages'),
          typingAnim: container.querySelector('#ai_typingAnimation'),
          promptSel: container.querySelector('#ai_promptSelector'),
          promptAdd: container.querySelector('#ai_addPromptBtn'),
          promptDel: container.querySelector('#ai_delPromptBtn'),
          promptText: container.querySelector('#ai_promptBase'),
          save: container.querySelector('#ai_saveBtn'),
          reset: container.querySelector('#ai_resetBtn'),
          status: container.querySelector('#ai_statusMsg'),
        }

        // 根据当前 apiMode 显示/隐藏自定义字段
        const updateModeVisibility = () => {
          const isCustom = els.apiMode.value === 'custom'
          els.customFields.style.display = isCustom ? 'block' : 'none'
        }

        // 加载数据
        const loadData = () => {
          currentSettings = StorageUtils.getSettings()

          els.apiMode.value = currentSettings.apiMode || 'default'
          els.apiUrl.value = currentSettings.apiUrl
          els.apiKey.value = currentSettings.apiKey
          els.modelName.value = currentSettings.modelName
          els.tempRange.value = currentSettings.temperature
          els.tempVal.textContent = currentSettings.temperature
          els.stream.checked = currentSettings.streamResponse
          els.subjectEnable.checked =
            currentSettings.enableOnSubjectPages !== false
          els.typingAnim.checked = currentSettings.typingAnimation !== false

          updateModeVisibility()
          renderPromptSelect()
          updatePromptText()
        }

        // 渲染 Prompt 下拉框
        const renderPromptSelect = () => {
          els.promptSel.innerHTML = Object.entries(
            currentSettings.savedPrompts || {},
          )
            .map(
              ([key, prompt]) =>
                `<option value="${key}" ${currentSettings.currentPrompt === key ? 'selected' : ''}>${prompt.name}</option>`,
            )
            .join('')
          updatePromptControls()
        }

        // 更新 Prompt 文本域
        const updatePromptText = () => {
          const key = els.promptSel.value
          if (currentSettings.savedPrompts[key]) {
            els.promptText.value = currentSettings.savedPrompts[key].content
          }
        }

        // 更新 Prompt 控件状态
        const updatePromptControls = () => {
          const key = els.promptSel.value
          const isDefault = currentSettings.savedPrompts[key]?.isDefault
          els.promptText.readOnly = isDefault
          els.promptDel.disabled = isDefault
          if (isDefault) {
            els.promptText.style.backgroundColor = '#f9f9f9'
            els.promptDel.style.opacity = '0.5'
          } else {
            els.promptText.style.backgroundColor = '#fff'
            els.promptDel.style.opacity = '1'
          }
        }

        // 事件绑定
        els.apiMode.addEventListener('change', updateModeVisibility)
        els.tempRange.addEventListener('input', () => {
          els.tempVal.textContent = els.tempRange.value
        })

        els.promptSel.addEventListener('change', () => {
          currentSettings.currentPrompt = els.promptSel.value
          updatePromptText()
          updatePromptControls()
        })

        els.promptAdd.addEventListener('click', async () => {
          const name = prompt('请输入新 Prompt 名称：')
          if (!name) return
          const key = 'prompt_' + Date.now()
          currentSettings.savedPrompts[key] = {
            name: name,
            content: currentSettings.promptBase,
            isDefault: false,
          }
          currentSettings.currentPrompt = key
          renderPromptSelect()
          updatePromptText()
        })

        els.promptDel.addEventListener('click', () => {
          const key = els.promptSel.value
          if (currentSettings.savedPrompts[key]?.isDefault) return
          if (confirm('确定删除此 Prompt？')) {
            delete currentSettings.savedPrompts[key]
            currentSettings.currentPrompt = 'default'
            renderPromptSelect()
            updatePromptText()
          }
        })

        els.save.addEventListener('click', async () => {
          const key = els.promptSel.value
          if (!currentSettings.savedPrompts[key].isDefault) {
            currentSettings.savedPrompts[key].content = els.promptText.value
          }

          const newSettings = {
            ...currentSettings,
            apiMode: els.apiMode.value,
            apiUrl: els.apiUrl.value,
            apiKey: els.apiKey.value,
            modelName: els.modelName.value,
            temperature: parseFloat(els.tempRange.value),
            streamResponse: els.stream.checked,
            enableOnSubjectPages: els.subjectEnable.checked,
            typingAnimation: els.typingAnim.checked,
            promptBase: currentSettings.savedPrompts[key].content,
          }

          StorageUtils.saveSettings(newSettings)
          currentSettings = newSettings
          els.status.textContent = '设置已保存至云端！'
          setTimeout(() => (els.status.textContent = ''), 2000)
        })

        els.reset.addEventListener('click', async () => {
          if (confirm('确定要恢复默认设置吗？')) {
            StorageUtils.saveSettings(CONFIG.DEFAULT_SETTINGS)
            loadData()
            els.status.textContent = '已恢复默认！'
            setTimeout(() => (els.status.textContent = ''), 2000)
          }
        })

        // 初始化加载
        loadData()
      },
    })
  }

  // 添加按钮
  function addButtons() {
    const targetList = utils.$('#robot_speech_js > ul')
    if (!targetList) {
      setTimeout(addButtons, 500)
      return
    }
    if (targetList.querySelector('.regenerate-button')) return

    targetList
      .querySelector('.ukagaka_speech_dismiss')
      ?.parentElement?.insertAdjacentHTML(
        'beforebegin',
        `
            <li class="ai-settings-button"><span>◆ <a href="javascript:void(0);" class="nav ai-settings-link">AI 设置</a></span></li>
            <li class="regenerate-button"><span>◇ <a href="javascript:void(0);" class="nav regenerate-link">重新生成</a></span></li>
            <li class="copy-button"><span>◇ <a href="javascript:void(0);" class="nav copy-link">复制锐评</a></span></li>
        `,
      )

    targetList.querySelector('.ai-settings-link').onclick = () => {
      chiiLib.ukagaka.showCustomizePanelWithTab('bangumi_ai')
    }
    targetList.querySelector('.regenerate-link').onclick = utils.throttle(
      () => {
        autoSummary(true)
      },
      3000,
    )
    targetList.querySelector('.copy-link').onclick = utils.throttle(
      copyTextToClipboard,
      1000,
    )
  }

  // 初始化
  function init() {
    const path = location.pathname
    const isRelevant = Object.values(CONFIG.PATHS).some((p) =>
      typeof p === 'string' ? path.startsWith(p) : p.test(path),
    )

    if (
      document.readyState === 'complete' ||
      document.readyState === 'interactive'
    ) {
      registerSettingsTab()
    } else {
      window.addEventListener('DOMContentLoaded', registerSettingsTab)
    }

    if (!isRelevant) return

    const initialize = () => {
      const robot = utils.$('#robot')
      if (robot) {
        new IntersectionObserver(
          (entries) => {
            entries.forEach((entry) => {
              if (entry.isIntersecting) {
                autoSummary()
                // observer.disconnect();
              }
            })
          },
          { threshold: 0.1 },
        ).observe(robot)
      }

      const debouncedAddButton = utils.debounce(addButtons, 250)
      new MutationObserver(debouncedAddButton).observe(
        utils.$('#robot_speech_js')?.parentNode || document.body,
        {
          childList: true,
          subtree: true,
        },
      )
    }

    if (
      path.match(CONFIG.PATHS.SUBJECT) ||
      path.match(CONFIG.PATHS.CHARACTER) ||
      path.match(CONFIG.PATHS.EP)
    ) {
      // 同步读取
      const settings = StorageUtils.getSettings()
      if (settings.enableOnSubjectPages !== false) initialize()
    } else {
      initialize()
    }
  }

  // 启动
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init)
  } else {
    init()
  }
})()
