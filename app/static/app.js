/* ==========================================================================
   Customer chat client — vanilla JS, no build step.
   Talks to POST /api/chat/stream (Server-Sent Events over fetch).
   ========================================================================== */

const STORAGE_KEY = 'cs.user_id';
let USER_ID = localStorage.getItem(STORAGE_KEY) || 'user001';

const INTENT_LABELS = {
  refund: '退款',
  order_query: '订单查询',
  tech_support: '技术支持',
  human_handoff: '转人工',
  faq: '常见问题',
  other: '其他',
};

const state = {
  conversationId: '',
  conversations: [],
  sending: false,
  status: 'active',
  intent: '',
};

const $ = (id) => document.getElementById(id);

/* ----------------------------- utilities ------------------------------ */
function toast(message, kind = '') {
  const host = $('toast-host');
  const node = document.createElement('div');
  node.className = 'toast' + (kind ? ' toast-' + kind : '');
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-User-Id': USER_ID,
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function inline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

/** Minimal Markdown: paragraphs, bullet lists, **bold**, `code`. */
function renderRich(text) {
  const out = [];
  let list = null;
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (line.startsWith('- ')) {
      (list = list || []).push('<li>' + inline(line.slice(2)) + '</li>');
      continue;
    }
    if (list) { out.push('<ul>' + list.join('') + '</ul>'); list = null; }
    if (line) out.push('<p>' + inline(line) + '</p>');
  }
  if (list) out.push('<ul>' + list.join('') + '</ul>');
  return out.join('') || '<p></p>';
}

function timeLabel(iso) {
  const date = iso ? new Date(iso) : new Date();
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function intentBadge(intent, label) {
  const text = label || INTENT_LABELS[intent] || intent || '未识别';
  return `<span class="badge badge-${intent || 'other'}">${escapeHtml(text)}</span>`;
}

const STATUS_TEXT = {
  active: '进行中',
  handoff_pending: '等待人工',
  handoff_replied: '人工已回复',
  resolved: '已解决',
  closed: '已结束',
};
const STATUS_CLASS = {
  active: 'badge-neutral',
  handoff_pending: 'badge-warning',
  handoff_replied: 'badge-primary',
  resolved: 'badge-success',
  closed: 'badge-neutral',
};

/* ------------------------------- rendering ---------------------------- */
function appendMessage({ role, content, intent, intentLabel, confidence, sources, createdAt, streaming }) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;

  const avatarText = role === 'user' ? '我' : role === 'agent' ? '人工' : 'AI';
  const metaBits = [];
  if (role !== 'user') {
    metaBits.push(intentBadge(intent, intentLabel));
    if (confidence) metaBits.push(`<span>置信度 ${(confidence * 100).toFixed(0)}%</span>`);
  }
  metaBits.push(`<span>${timeLabel(createdAt)}</span>`);

  const sourceChips = (sources || []).map((source) => {
    const score = source.score ? ` · ${Number(source.score).toFixed(2)}` : '';
    return `<span class="chip">📄 ${escapeHtml(source.title || '知识库')}${score}</span>`;
  }).join('');

  wrap.innerHTML = `
    <div class="msg-avatar">${avatarText}</div>
    <div class="msg-stack">
      <div class="bubble rich">${streaming ? '<span class="typing"><i></i><i></i><i></i></span>' : renderRich(content)}</div>
      <div class="msg-meta">${metaBits.join('')}</div>
      ${sourceChips ? `<div class="source-list">${sourceChips}</div>` : ''}
    </div>`;

  $('messages').appendChild(wrap);
  scrollToBottom();
  return wrap;
}

function appendHandoffBanner(reason) {
  const banner = document.createElement('div');
  banner.className = 'handoff-banner';
  banner.innerHTML = `<span>⚠️</span><div>本次会话已转接人工客服（原因：${escapeHtml(reason || 'unresolved')}）。
    人工客服可以看到完整对话记录，您也可以继续留言。</div>`;
  $('messages').appendChild(banner);
  scrollToBottom();
}

function scrollToBottom() {
  const box = $('messages');
  box.scrollTop = box.scrollHeight;
}

function renderSuggestions(list) {
  const host = $('suggestions');
  host.innerHTML = '';
  (list || []).slice(0, 4).forEach((text) => {
    const button = document.createElement('button');
    button.className = 'suggestion';
    button.textContent = text;
    button.addEventListener('click', () => {
      $('input').value = text;
      send();
    });
    host.appendChild(button);
  });
}

const DEFAULT_SUGGESTIONS = [
  '帮我查一下订单 ORD-1001 的物流',
  '订单 ORD-1002 我想申请退款',
  'App 一直闪退打不开怎么办',
  '怎么开发票？',
];

function renderRating() {
  if (document.getElementById('rating-box')) return;
  const box = document.createElement('div');
  box.className = 'rating-box';
  box.id = 'rating-box';
  box.innerHTML = `
    <div style="font-size:13px;font-weight:600">本次服务是否解决了您的问题？请为我们的服务打分</div>
    <div class="rating-stars">
      ${[1, 2, 3, 4, 5].map((n) => `<button class="star" data-score="${n}">★</button>`).join('')}
    </div>
    <div class="rating-note" id="rating-note">1 分非常不满意 · 5 分非常满意（点击即可提交）</div>`;

  box.querySelectorAll('.star').forEach((star) => {
    star.addEventListener('mouseenter', () => paintStars(Number(star.dataset.score)));
    star.addEventListener('mouseleave', () => paintStars(0));
    star.addEventListener('click', () => submitRating(Number(star.dataset.score)));
  });

  $('messages').appendChild(box);
  scrollToBottom();
}

function paintStars(score) {
  const box = document.getElementById('rating-box');
  if (!box) return;
  box.querySelectorAll('.star').forEach((star) => {
    star.classList.toggle('on', Number(star.dataset.score) <= score);
  });
}

async function submitRating(score) {
  paintStars(score);
  try {
    await api(`/api/conversations/${state.conversationId}/satisfaction`, {
      method: 'POST',
      body: JSON.stringify({ score, comment: '' }),
    });
    const box = document.getElementById('rating-box');
    if (box) {
      box.innerHTML = `<div style="font-size:13px;color:var(--success);font-weight:600">
        ✓ 感谢您的评价（${score} 分），我们会持续改进服务。</div>`;
    }
    toast('评价已提交，感谢反馈', 'success');
    loadConversations();
  } catch (error) {
    toast('评价提交失败：' + error.message, 'error');
  }
}

/* ------------------------------- data flow ---------------------------- */
async function loadProfile() {
  try {
    const data = await api('/api/me');
    $('user-name').textContent = data.display_name ? `${data.display_name}（${data.user_id}）` : data.user_id;
    $('user-meta').textContent = `${data.level} · ${data.order_count} 个订单`;
    $('user-avatar').textContent = (data.display_name || data.user_id).slice(0, 1).toUpperCase();
  } catch (error) {
    $('user-meta').textContent = '加载失败';
  }
}

async function loadConversations() {
  try {
    const list = await api('/api/conversations');
    state.conversations = list;
    const host = $('conv-list');
    if (!list.length) {
      host.innerHTML = '<div class="empty-state" style="padding:24px 10px;font-size:12px">暂无历史会话</div>';
      return;
    }
    host.innerHTML = '';
    list.forEach((conversation) => {
      const node = document.createElement('div');
      node.className = 'conv-item' + (conversation.conversation_id === state.conversationId ? ' active' : '');
      const rating = conversation.satisfaction_score ? `★${conversation.satisfaction_score}` : '';
      node.innerHTML = `
        <div class="c-title">${escapeHtml(conversation.title || '新会话')}</div>
        <div class="c-meta">
          ${intentBadge(conversation.primary_intent, '')}
          <span>${conversation.message_count} 条</span>
          ${rating ? `<span style="color:#d97706">${rating}</span>` : ''}
        </div>`;
      node.addEventListener('click', () => openConversation(conversation.conversation_id));
      host.appendChild(node);
    });
  } catch (error) {
    toast('会话列表加载失败：' + error.message, 'error');
  }
}

async function openConversation(conversationId) {
  try {
    const detail = await api(`/api/conversations/${conversationId}`);
    state.conversationId = conversationId;
    state.status = detail.status;
    state.intent = detail.primary_intent;

    $('messages').innerHTML = '';
    $('chat-title').textContent = detail.title || '在线客服';
    $('chat-sub').textContent = `${detail.user_id} · 共 ${detail.message_count} 条消息`;
    updateHeadBadges();

    detail.messages.forEach((message) => appendMessage({
      role: message.role,
      content: message.content,
      intent: message.intent,
      sources: message.sources,
      createdAt: message.created_at,
    }));
    if (detail.handoff_count > 0) appendHandoffBanner('历史转人工');

    renderSuggestions(DEFAULT_SUGGESTIONS);
    loadConversations();
  } catch (error) {
    toast('打开会话失败：' + error.message, 'error');
  }
}

function updateHeadBadges() {
  const intentEl = $('head-intent');
  intentEl.className = 'badge badge-' + (state.intent || 'other');
  intentEl.textContent = state.intent ? (INTENT_LABELS[state.intent] || state.intent) : '等待提问';

  const statusEl = $('head-status');
  statusEl.className = 'badge ' + (STATUS_CLASS[state.status] || 'badge-neutral');
  statusEl.textContent = STATUS_TEXT[state.status] || state.status;
}

function newConversation() {
  state.conversationId = '';
  state.status = 'active';
  state.intent = '';
  $('messages').innerHTML = '';
  $('chat-title').textContent = '在线客服';
  $('chat-sub').textContent = '多轮上下文 · FAQ 知识库 · 意图识别 · 转人工';
  updateHeadBadges();
  renderSuggestions(DEFAULT_SUGGESTIONS);
  appendMessage({
    role: 'assistant',
    content: '您好，我是智能客服助手。我可以帮您**查询订单物流**、**申请退款**、**排查技术问题**，也可以解答发票、优惠券等常见问题。请问有什么可以帮您？',
  });
  loadConversations();
}

/* ------------------------------- send --------------------------------- */
async function send(forcedText) {
  if (state.sending) return;
  const input = $('input');
  const text = (forcedText !== undefined ? forcedText : input.value).trim();
  if (!text) return;

  state.sending = true;
  $('btn-send').disabled = true;
  $('btn-send').innerHTML = '<span class="spinner"></span>';
  input.value = '';
  input.style.height = 'auto';

  appendMessage({ role: 'user', content: text });
  const placeholder = appendMessage({ role: 'assistant', content: '', streaming: true });

  let answerText = '';
  let meta = null;

  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-User-Id': USER_ID },
      body: JSON.stringify({ message: text, conversation_id: state.conversationId }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || response.statusText);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let index;
      while ((index = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);

        const lines = chunk.split('\n');
        const eventName = (lines.find((l) => l.startsWith('event: ')) || '').slice(7).trim();
        const dataLine = (lines.find((l) => l.startsWith('data: ')) || '').slice(6);
        if (!eventName || !dataLine) continue;

        let data = {};
        try { data = JSON.parse(dataLine); } catch (_) { continue; }

        if (eventName === 'meta') {
          meta = data;
          state.conversationId = data.conversation_id || state.conversationId;
          state.intent = data.intent;
          state.status = data.conversation_status;
          updateHeadBadges();
          if (data.conversation_id) $('chat-sub').textContent = '会话 ' + data.conversation_id;
        } else if (eventName === 'delta') {
          answerText += (answerText ? '\n\n' : '') + data.text;
          placeholder.querySelector('.bubble').innerHTML = renderRich(answerText);
          scrollToBottom();
        } else if (eventName === 'done') {
          answerText = data.answer || answerText;
          placeholder.querySelector('.bubble').innerHTML = renderRich(answerText);
        } else if (eventName === 'error') {
          throw new Error(data.message || '服务异常');
        }
      }
    }

    // Attach intent / source metadata produced by the engine.
    if (meta) {
      const metaHost = placeholder.querySelector('.msg-meta');
      const chips = [];
      chips.push(intentBadge(meta.intent, meta.intent_label));
      if (meta.confidence) chips.push(`<span>置信度 ${(meta.confidence * 100).toFixed(0)}%</span>`);
      chips.push(`<span>${timeLabel()}</span>`);
      metaHost.innerHTML = chips.join('');

      const sources = meta.sources || [];
      if (sources.length) {
        const host = document.createElement('div');
        host.className = 'source-list';
        host.innerHTML = sources.map((source) => {
          const score = source.score ? ` · ${Number(source.score).toFixed(2)}` : '';
          return `<span class="chip">📄 ${escapeHtml(source.title || '知识库')}${score}</span>`;
        }).join('');
        placeholder.querySelector('.msg-stack').appendChild(host);
      }

      if (meta.handoff) appendHandoffBanner(meta.handoff_reason);
      if (meta.ticket_id) toast('已创建工单 ' + meta.ticket_id, 'success');
      if (meta.proposal_id) toast('已提交退款申请 ' + meta.proposal_id, 'success');

      renderSuggestions(meta.suggestions && meta.suggestions.length ? meta.suggestions : DEFAULT_SUGGESTIONS);
      if (meta.ask_satisfaction) renderRating();
    }
  } catch (error) {
    placeholder.querySelector('.bubble').innerHTML =
      `<span style="color:var(--danger)">请求失败：${escapeHtml(error.message)}</span>`;
    toast('发送失败：' + error.message, 'error');
  } finally {
    state.sending = false;
    $('btn-send').disabled = false;
    $('btn-send').textContent = '发送';
    scrollToBottom();
    loadConversations();
  }
}

/* ------------------------------- wiring ------------------------------- */
function autoGrow(event) {
  const el = event.target;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 150) + 'px';
}

function init() {
  $('btn-send').addEventListener('click', () => send());
  $('btn-new').addEventListener('click', newConversation);
  $('btn-handoff').addEventListener('click', () => send('转人工'));

  const input = $('input');
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });

  // Click the user card to switch identity — handy when demoing several accounts.
  $('user-avatar').parentElement.addEventListener('click', () => {
    const next = prompt('切换用户 ID（演示用，例如 user001 / user002 / user003）', USER_ID);
    if (next && next.trim() && next.trim() !== USER_ID) {
      USER_ID = next.trim();
      localStorage.setItem(STORAGE_KEY, USER_ID);
      state.conversationId = '';
      $('messages').innerHTML = '';
      loadProfile();
      loadConversations();
      newConversation();
      toast('已切换用户：' + USER_ID);
    }
  });

  loadProfile();
  loadConversations();
  newConversation();

  fetch('/api/readyz').then((r) => r.json()).then((data) => {
    $('brand-engine').textContent = `引擎 ${data.engine_mode}${data.llm_enabled ? ' + LLM' : ''} · FAQ ${data.faq_entries} 条`;
  }).catch(() => { /* readiness badge is cosmetic */ });
}

document.addEventListener('DOMContentLoaded', init);
