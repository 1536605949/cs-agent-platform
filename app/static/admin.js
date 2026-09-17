/* ==========================================================================
   Back-office console — vanilla JS, no build step.
   ========================================================================== */

const TOKEN_KEY = 'cs.admin_token';
const NAME_KEY = 'cs.admin_name';

const INTENT_LABELS = {
  refund: '退款',
  order_query: '订单查询',
  tech_support: '技术支持',
  human_handoff: '转人工',
  faq: '常见问题',
  other: '其他',
  unknown: '未识别',
  '': '未识别',
};
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
const ROLE_AVATAR = { user: '我', assistant: 'AI', agent: '人工', system: 'SYS' };

const state = {
  token: localStorage.getItem(TOKEN_KEY) || '',
  page: 1,
  pageSize: 20,
  total: 0,
  currentPage: 'overview',
};

const $ = (id) => document.getElementById(id);

/* ------------------------------ utilities ----------------------------- */
function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = 'toast' + (kind ? ' toast-' + kind : '');
  node.textContent = message;
  $('toast-host').appendChild(node);
  setTimeout(() => node.remove(), 3400);
}

function escapeHtml(text) {
  return String(text == null ? '' : text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function inline(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

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

function fmtTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function pct(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(state.token ? { Authorization: 'Bearer ' + state.token } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401 || response.status === 403) {
    logout();
    throw new Error('登录状态已失效，请重新登录');
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) { /* ignore */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return response.status === 204 ? null : response.json();
}

/* -------------------------------- auth -------------------------------- */
async function login(event) {
  event.preventDefault();
  const button = $('login-btn');
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span>';
  try {
    const data = await api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ username: $('username').value.trim(), password: $('password').value }),
    });
    state.token = data.access_token;
    localStorage.setItem(TOKEN_KEY, state.token);
    localStorage.setItem(NAME_KEY, data.username);
    enterConsole();
  } catch (error) {
    toast('登录失败：' + error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = '登录';
  }
}

function logout() {
  state.token = '';
  localStorage.removeItem(TOKEN_KEY);
  $('console').style.display = 'none';
  $('login-screen').style.display = 'flex';
}

function enterConsole() {
  $('login-screen').style.display = 'none';
  $('console').style.display = 'flex';
  $('admin-name').textContent = '管理员：' + (localStorage.getItem(NAME_KEY) || 'admin');
  loadReadiness();
  refreshCurrentPage();
}

async function loadReadiness() {
  try {
    const data = await api('/api/readyz');
    $('console-sub').textContent =
      `引擎 ${data.engine_mode}${data.llm_enabled ? ' + LLM' : ''} · FAQ ${data.faq_entries} 条 · 会话 ${data.conversations} 个 · 消息 ${data.messages} 条`;
  } catch (_) {
    $('console-sub').textContent = '智能客服管理后台';
  }
}

/* ---------------------------- page routing ---------------------------- */
function switchPage(page) {
  state.currentPage = page;
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.page === page);
  });
  document.querySelectorAll('.page').forEach((section) => {
    section.classList.toggle('active', section.id === 'page-' + page);
  });
  refreshCurrentPage();
}

function refreshCurrentPage() {
  const page = state.currentPage;
  if (page === 'overview') loadOverview();
  else if (page === 'conversations') loadConversations();
  else if (page === 'handoffs') loadHandoffs();
  else if (page === 'refunds') loadRefunds();
  else if (page === 'faqs') loadFaqs();
}

/* ------------------------------ overview ------------------------------ */
function statCard(label, value, foot, unit = '') {
  return `<div class="stat-card">
    <div class="label">${escapeHtml(label)}</div>
    <div class="value">${value}${unit ? `<small>${unit}</small>` : ''}</div>
    <div class="foot">${escapeHtml(foot)}</div>
  </div>`;
}

async function loadOverview() {
  try {
    const [overview, satisfaction, intents, trend] = await Promise.all([
      api('/api/admin/stats/overview'),
      api('/api/admin/stats/satisfaction'),
      api('/api/admin/stats/intents'),
      api(`/api/admin/stats/trend?days=${$('trend-days').value}`),
    ]);

    $('stat-grid').innerHTML = [
      statCard('会话总数', overview.conversations, `进行中 ${overview.active_conversations} · 已解决 ${overview.resolved_conversations}`),
      statCard('消息总数', overview.messages, `平均每会话 ${overview.conversations ? (overview.messages / overview.conversations).toFixed(1) : '0'} 条`),
      statCard('平均满意度', overview.satisfaction_avg || '—', `已评价 ${overview.rated_conversations} 个会话 · 评价率 ${pct(overview.satisfaction_rate)}`, '分'),
      statCard('转人工率', pct(overview.handoff_rate), `转人工会话 ${overview.handoff_conversations} 个`),
      statCard('待处理人工工单', overview.handoffs_pending, '需人工客服跟进', '个'),
      statCard('待审核退款', overview.refunds_pending, '需管理员审批', '笔'),
      statCard('未关闭工单', overview.tickets_open, '技术支持工单', '个'),
    ].join('');

    renderSatisfactionChart(satisfaction);
    renderIntentChart(intents);
    renderTrendChart(trend);

    $('nav-handoff-count').textContent = overview.handoffs_pending;
    $('nav-handoff-count').classList.toggle('zero', !overview.handoffs_pending);
    $('nav-refund-count').textContent = overview.refunds_pending;
    $('nav-refund-count').classList.toggle('zero', !overview.refunds_pending);
  } catch (error) {
    toast('统计数据加载失败：' + error.message, 'error');
  }
}

function renderSatisfactionChart(data) {
  $('sat-summary').textContent = data.rated
    ? `平均 ${data.average} 分 · 好评率 ${pct(data.positive_rate)}`
    : '暂无评价数据';

  if (!data.rated) {
    $('sat-chart').innerHTML = '<div class="empty-state" style="padding:28px 0">暂无满意度数据，用户评价后将显示统计</div>';
    return;
  }

  const colors = { 1: '#d92d20', 2: '#f97066', 3: '#f0b429', 4: '#7dd3a0', 5: '#12a150' };
  const max = Math.max(...Object.values(data.distribution), 1);
  $('sat-chart').innerHTML = [5, 4, 3, 2, 1].map((score) => {
    const count = data.distribution[String(score)] || 0;
    const share = data.rated ? (count / data.rated) * 100 : 0;
    return `<div class="bar-row">
      <div class="name">${score} 分</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(count / max) * 100}%;background:${colors[score]}"></div></div>
      <div class="num">${count} · ${share.toFixed(1)}%</div>
    </div>`;
  }).join('');
}

function renderIntentChart(items) {
  $('intent-summary').textContent = items.length ? `共 ${items.reduce((sum, i) => sum + i.count, 0)} 个会话` : '暂无数据';
  if (!items.length) {
    $('intent-chart').innerHTML = '<div class="empty-state" style="padding:28px 0">暂无意图分布数据</div>';
    return;
  }

  const colors = {
    refund: '#f0b429', order_query: '#2f8fd0', tech_support: '#7c5cf0',
    human_handoff: '#d92d20', faq: '#12a150', other: '#98a2b3', unknown: '#cbd5e1',
  };
  const max = Math.max(...items.map((i) => i.count), 1);
  $('intent-chart').innerHTML = items.map((item) => `
    <div class="bar-row">
      <div class="name">${escapeHtml(INTENT_LABELS[item.intent] || item.label || item.intent)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(item.count / max) * 100}%;background:${colors[item.intent] || '#98a2b3'}"></div></div>
      <div class="num">${item.count} · ${pct(item.share)}</div>
    </div>`).join('');
}

function renderTrendChart(points) {
  if (!points.length) {
    $('trend-chart').innerHTML = '<div class="empty-state" style="padding:28px 0">暂无趋势数据</div>';
    return;
  }

  const maxConversations = Math.max(...points.map((p) => p.conversations), 1);
  const maxMessages = Math.max(...points.map((p) => p.messages), 1);
  const maxValue = Math.max(maxConversations, maxMessages);

  const columns = points.map((point) => {
    const label = point.date.slice(5);
    const convHeight = (point.conversations / maxValue) * 100;
    const msgHeight = (point.messages / maxValue) * 100;
    const sat = point.satisfaction_avg != null ? `<div style="font-size:10px;color:#b45309">★${point.satisfaction_avg}</div>` : '';
    return `<div style="flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:4px">
      <div style="height:18px">${sat}</div>
      <div style="flex:1;width:100%;display:flex;align-items:flex-end;justify-content:center;gap:3px;height:130px">
        <div title="${label} 会话 ${point.conversations}" style="width:42%;height:${convHeight}%;background:#2f5fe0;border-radius:3px 3px 0 0;min-height:2px"></div>
        <div title="${label} 消息 ${point.messages}" style="width:42%;height:${msgHeight}%;background:#98b6f5;border-radius:3px 3px 0 0;min-height:2px"></div>
      </div>
      <div style="font-size:10px;color:#98a2b3;white-space:nowrap">${label}</div>
    </div>`;
  }).join('');

  $('trend-chart').innerHTML = `<div style="display:flex;gap:6px;align-items:flex-end;border-bottom:1px solid #e4e8ef;padding-bottom:4px">${columns}</div>`;
}

/* ---------------------------- conversations --------------------------- */
async function loadConversations() {
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
  });
  const q = $('f-q').value.trim();
  if (q) params.set('q', q);
  if ($('f-intent').value) params.set('intent', $('f-intent').value);
  if ($('f-status').value) params.set('status', $('f-status').value);
  if ($('f-handoff').checked) params.set('handoff_only', 'true');

  try {
    const data = await api('/api/admin/conversations?' + params.toString());
    state.total = data.total;
    $('conv-total').textContent = `共 ${data.total} 个会话`;
    $('conv-page').textContent = `${data.page} / ${Math.max(1, Math.ceil(data.total / data.page_size))}`;

    const body = $('conv-tbody');
    if (!data.items.length) {
      body.innerHTML = '<tr><td colspan="7"><div class="empty-state">没有符合条件的会话记录</div></td></tr>';
      return;
    }

    body.innerHTML = data.items.map((item) => `
      <tr class="row-click" data-id="${escapeHtml(item.conversation_id)}">
        <td>
          <div class="title-cell">${escapeHtml(item.title || '新会话')}</div>
          <div class="sub-cell">${escapeHtml(item.conversation_id)}</div>
        </td>
        <td>${escapeHtml(item.user_id)}</td>
        <td><span class="badge badge-${item.primary_intent || 'other'}">${escapeHtml(INTENT_LABELS[item.primary_intent] || '未识别')}</span></td>
        <td>${item.message_count}${item.handoff_count ? ` <span class="badge badge-warning">转人工 ${item.handoff_count}</span>` : ''}</td>
        <td>${item.satisfaction_score ? `<span style="color:#d97706;font-weight:600">★ ${item.satisfaction_score}</span>` : '<span style="color:#98a2b3">—</span>'}</td>
        <td><span class="badge ${STATUS_CLASS[item.status] || 'badge-neutral'}">${escapeHtml(STATUS_TEXT[item.status] || item.status)}</span></td>
        <td style="color:#667085;white-space:nowrap">${fmtTime(item.updated_at)}</td>
      </tr>`).join('');

    body.querySelectorAll('.row-click').forEach((row) => {
      row.addEventListener('click', () => openConversation(row.dataset.id));
    });
  } catch (error) {
    toast('对话记录加载失败：' + error.message, 'error');
  }
}

async function openConversation(conversationId) {
  try {
    const detail = await api('/api/admin/conversations/' + encodeURIComponent(conversationId));
    $('detail-title').textContent = detail.title || '会话详情';

    const header = `<div class="detail-grid">
      <div class="detail-item"><div class="k">会话 ID</div><div class="v" style="font-family:var(--mono);font-size:11.5px">${escapeHtml(detail.conversation_id)}</div></div>
      <div class="detail-item"><div class="k">用户</div><div class="v">${escapeHtml(detail.user_id)}</div></div>
      <div class="detail-item"><div class="k">主意图</div><div class="v">${escapeHtml(INTENT_LABELS[detail.primary_intent] || '未识别')}</div></div>
      <div class="detail-item"><div class="k">状态</div><div class="v">${escapeHtml(STATUS_TEXT[detail.status] || detail.status)}</div></div>
      <div class="detail-item"><div class="k">消息数</div><div class="v">${detail.message_count}</div></div>
      <div class="detail-item"><div class="k">转人工次数</div><div class="v">${detail.handoff_count}</div></div>
      <div class="detail-item"><div class="k">满意度</div><div class="v">${detail.satisfaction_score ? '★ ' + detail.satisfaction_score : '未评价'}</div></div>
      <div class="detail-item"><div class="k">创建时间</div><div class="v" style="font-size:11.5px">${fmtTime(detail.created_at)}</div></div>
    </div>`;

    const rows = detail.messages.map((message) => {
      const meta = [];
      if (message.role !== 'user') {
        if (message.intent) meta.push(`<span class="badge badge-${message.intent}">${escapeHtml(INTENT_LABELS[message.intent] || message.intent)}</span>`);
        if (message.confidence) meta.push(`<span>置信度 ${(message.confidence * 100).toFixed(0)}%</span>`);
      }
      if (message.handoff) meta.push('<span class="badge badge-danger">触发转人工</span>');
      meta.push(`<span>${fmtTime(message.created_at)}</span>`);

      const sources = (message.sources || []).length
        ? `<div class="source-list" style="margin-top:6px">${message.sources.map((s) => `<span class="chip">📄 ${escapeHtml(s.title || '')}</span>`).join('')}</div>`
        : '';

      return `<div class="t-row ${message.role}">
        <div class="t-avatar">${ROLE_AVATAR[message.role] || '?'}</div>
        <div>
          <div class="t-bubble rich">${renderRich(message.content)}</div>
          <div class="t-meta">${meta.join('')}</div>
          ${sources}
        </div>
      </div>`;
    }).join('');

    $('detail-body').innerHTML = header + `<div class="transcript">${rows || '<div class="empty-state">暂无消息</div>'}</div>`;
    $('detail-mask').style.display = 'flex';
  } catch (error) {
    toast('会话详情加载失败：' + error.message, 'error');
  }
}

/* ------------------------------- handoffs ----------------------------- */
async function loadHandoffs() {
  try {
    const status = $('h-status').value;
    const rows = await api('/api/admin/handoffs' + (status ? '?status=' + status : ''));
    const host = $('handoff-list');

    if (!rows.length) {
      host.innerHTML = '<div class="empty-state">当前没有人工工单</div>';
      return;
    }

    host.innerHTML = rows.map((task) => `
      <div class="handoff-card" data-id="${task.id}">
        <div class="h-top">
          <div>
            <b style="font-size:13px">${escapeHtml(task.user_id)}</b>
            <span class="badge badge-neutral" style="margin-left:6px">${escapeHtml(task.conversation_id)}</span>
            <span class="badge badge-warning" style="margin-left:4px">${escapeHtml(task.reason)}</span>
          </div>
          <div>
            <span class="badge ${task.status === 'pending' ? 'badge-danger' : task.status === 'replied' ? 'badge-primary' : 'badge-success'}">
              ${task.status === 'pending' ? '待处理' : task.status === 'replied' ? '已回复' : '已解决'}
            </span>
            <span style="font-size:11px;color:#98a2b3;margin-left:8px">${fmtTime(task.created_at)}</span>
          </div>
        </div>
        <div class="h-summary">${escapeHtml(task.summary)}</div>
        <div class="h-actions">
          <textarea class="textarea" placeholder="输入回复内容，将作为人工客服消息发送给用户…"></textarea>
          <button class="btn btn-sm btn-primary" data-act="reply">发送回复</button>
          <button class="btn btn-sm" data-act="resolve">标记已解决</button>
        </div>
      </div>`).join('');

    host.querySelectorAll('.handoff-card').forEach((card) => {
      const id = card.dataset.id;
      const textarea = card.querySelector('textarea');
      card.querySelector('[data-act="reply"]').addEventListener('click', () => replyHandoff(id, textarea.value, false));
      card.querySelector('[data-act="resolve"]').addEventListener('click', () => replyHandoff(id, textarea.value, true));
    });
  } catch (error) {
    toast('人工工单加载失败：' + error.message, 'error');
  }
}

async function replyHandoff(id, content, resolve) {
  content = (content || '').trim();
  if (!content && !resolve) {
    toast('请输入回复内容', 'error');
    return;
  }
  try {
    if (content) {
      await api(`/api/admin/handoffs/${id}/reply`, {
        method: 'POST',
        body: JSON.stringify({ content, resolve }),
      });
    } else {
      await api(`/api/admin/handoffs/${id}/resolve`, { method: 'POST' });
    }
    toast(resolve ? '工单已标记为已解决' : '回复已发送给用户', 'success');
    loadHandoffs();
    loadReadiness();
  } catch (error) {
    toast('操作失败：' + error.message, 'error');
  }
}

/* -------------------------------- refunds ----------------------------- */
async function loadRefunds() {
  try {
    const status = $('r-status').value;
    const rows = await api('/api/admin/refunds' + (status ? '?status=' + status : ''));
    const host = $('refund-list');

    if (!rows.length) {
      host.innerHTML = '<div class="empty-state">当前没有退款申请</div>';
      return;
    }

    const badge = { pending_review: 'badge-warning', approved: 'badge-primary', executed: 'badge-success', rejected: 'badge-danger' };
    const label = { pending_review: '待审核', approved: '已通过', executed: '已执行', rejected: '已拒绝' };

    host.innerHTML = rows.map((item) => `
      <div class="refund-card" data-id="${escapeHtml(item.proposal_id)}">
        <div class="r-top">
          <div>
            <b style="font-size:13px;font-family:var(--mono)">${escapeHtml(item.proposal_id)}</b>
            <span class="badge badge-neutral" style="margin-left:6px">订单 ${escapeHtml(item.order_id)}</span>
            <span class="badge badge-neutral" style="margin-left:4px">用户 ${escapeHtml(item.user_id)}</span>
          </div>
          <div>
            <span class="badge ${badge[item.status] || 'badge-neutral'}">${label[item.status] || item.status}</span>
            <span style="font-size:11px;color:#98a2b3;margin-left:8px">${fmtTime(item.created_at)}</span>
          </div>
        </div>
        <div class="r-body">
          退款原因：${escapeHtml(item.reason || '—')}<br />
          ${item.reviewer_id ? `审核人：${escapeHtml(item.reviewer_id)}${item.review_comment ? ' · 备注：' + escapeHtml(item.review_comment) : ''}` : ''}
        </div>
        ${item.status === 'pending_review' ? `
        <div class="r-actions">
          <input class="input" style="max-width:280px;padding:6px 10px;font-size:12.5px" placeholder="审核备注（可选）" />
          <button class="btn btn-sm btn-primary" data-act="approve">通过并执行退款</button>
          <button class="btn btn-sm" data-act="reject">拒绝</button>
        </div>` : ''}
      </div>`).join('');

    host.querySelectorAll('.refund-card').forEach((card) => {
      const id = card.dataset.id;
      const input = card.querySelector('input');
      const approve = card.querySelector('[data-act="approve"]');
      const reject = card.querySelector('[data-act="reject"]');
      if (approve) approve.addEventListener('click', () => reviewRefund(id, 'approve', input.value));
      if (reject) reject.addEventListener('click', () => reviewRefund(id, 'reject', input.value));
    });
  } catch (error) {
    toast('退款申请加载失败：' + error.message, 'error');
  }
}

async function reviewRefund(proposalId, decision, comment) {
  const verb = decision === 'approve' ? '通过并执行' : '拒绝';
  if (!confirm(`确认${verb}退款申请 ${proposalId}？`)) return;
  try {
    await api(`/api/admin/refunds/${encodeURIComponent(proposalId)}/review`, {
      method: 'POST',
      body: JSON.stringify({ decision, comment: comment || '', execute: true }),
    });
    toast(`已${verb}`, 'success');
    loadRefunds();
    loadReadiness();
  } catch (error) {
    toast('操作失败：' + error.message, 'error');
  }
}

/* --------------------------------- faqs ------------------------------- */
async function loadFaqs() {
  try {
    const q = $('faq-search').value.trim();
    const rows = await api('/api/admin/faqs' + (q ? '?q=' + encodeURIComponent(q) : ''));
    const body = $('faq-tbody');

    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="5"><div class="empty-state">暂无 FAQ 条目</div></td></tr>';
      return;
    }

    body.innerHTML = rows.map((item) => `
      <tr>
        <td><span class="badge badge-neutral">${escapeHtml(item.category)}</span></td>
        <td style="max-width:230px">${escapeHtml(item.question)}</td>
        <td style="max-width:360px;color:#667085">${escapeHtml(item.answer.slice(0, 90))}${item.answer.length > 90 ? '…' : ''}</td>
        <td>${item.enabled ? '<span class="badge badge-success">启用</span>' : '<span class="badge badge-neutral">停用</span>'}</td>
        <td>
          <button class="btn btn-sm" data-act="edit">编辑</button>
          <button class="btn btn-sm" data-act="del" style="color:var(--danger)">删除</button>
        </td>
      </tr>`).join('');

    body.querySelectorAll('tr').forEach((row, index) => {
      const item = rows[index];
      row.querySelector('[data-act="edit"]').addEventListener('click', () => fillFaqForm(item));
      row.querySelector('[data-act="del"]').addEventListener('click', () => deleteFaq(item));
    });
  } catch (error) {
    toast('FAQ 加载失败：' + error.message, 'error');
  }
}

function fillFaqForm(item) {
  $('faq-id').value = item.id;
  $('faq-category').value = item.category;
  $('faq-question').value = item.question;
  $('faq-answer').value = item.answer;
  $('faq-keywords').value = item.keywords || '';
  $('faq-enabled').checked = item.enabled;
  $('faq-form-title').textContent = `编辑 FAQ #${item.id}`;
  $('faq-submit').textContent = '更新';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function resetFaqForm() {
  $('faq-id').value = '';
  $('faq-question').value = '';
  $('faq-answer').value = '';
  $('faq-keywords').value = '';
  $('faq-enabled').checked = true;
  $('faq-form-title').textContent = '新增 FAQ';
  $('faq-submit').textContent = '保存';
}

async function saveFaq(event) {
  event.preventDefault();
  const id = $('faq-id').value;
  const payload = {
    category: $('faq-category').value,
    question: $('faq-question').value.trim(),
    answer: $('faq-answer').value.trim(),
    keywords: $('faq-keywords').value.trim(),
    enabled: $('faq-enabled').checked,
  };
  try {
    if (id) {
      await api('/api/admin/faqs/' + id, { method: 'PUT', body: JSON.stringify(payload) });
      toast('FAQ 已更新，检索索引已重建', 'success');
    } else {
      await api('/api/admin/faqs', { method: 'POST', body: JSON.stringify(payload) });
      toast('FAQ 已新增，检索索引已重建', 'success');
    }
    resetFaqForm();
    loadFaqs();
    loadReadiness();
  } catch (error) {
    toast('保存失败：' + error.message, 'error');
  }
}

async function deleteFaq(item) {
  if (!confirm(`确认删除 FAQ「${item.question}」？`)) return;
  try {
    await api('/api/admin/faqs/' + item.id, { method: 'DELETE' });
    toast('FAQ 已删除', 'success');
    loadFaqs();
    loadReadiness();
  } catch (error) {
    toast('删除失败：' + error.message, 'error');
  }
}

/* -------------------------------- wiring ------------------------------ */
function init() {
  $('login-form').addEventListener('submit', login);
  $('btn-logout').addEventListener('click', logout);
  $('btn-refresh').addEventListener('click', () => {
    loadReadiness();
    refreshCurrentPage();
    toast('数据已刷新');
  });

  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => switchPage(item.dataset.page));
  });

  $('trend-days').addEventListener('change', loadOverview);

  $('f-apply').addEventListener('click', () => { state.page = 1; loadConversations(); });
  $('f-reset').addEventListener('click', () => {
    $('f-q').value = '';
    $('f-intent').value = '';
    $('f-status').value = '';
    $('f-handoff').checked = false;
    state.page = 1;
    loadConversations();
  });
  $('f-q').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { state.page = 1; loadConversations(); }
  });
  $('conv-prev').addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; loadConversations(); }
  });
  $('conv-next').addEventListener('click', () => {
    if (state.page * state.pageSize < state.total) { state.page += 1; loadConversations(); }
  });

  $('h-status').addEventListener('change', loadHandoffs);
  $('h-refresh').addEventListener('click', loadHandoffs);
  $('r-status').addEventListener('change', loadRefunds);
  $('r-refresh').addEventListener('click', loadRefunds);

  $('faq-form').addEventListener('submit', saveFaq);
  $('faq-cancel').addEventListener('click', resetFaqForm);
  $('faq-search').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') loadFaqs();
  });
  $('faq-reindex').addEventListener('click', async () => {
    try {
      const data = await api('/api/admin/faqs/reindex', { method: 'POST' });
      toast(`索引已重建，共 ${data.indexed_documents} 条文档`, 'success');
    } catch (error) {
      toast('重建失败：' + error.message, 'error');
    }
  });

  $('detail-close').addEventListener('click', () => { $('detail-mask').style.display = 'none'; });
  $('detail-mask').addEventListener('click', (event) => {
    if (event.target === $('detail-mask')) $('detail-mask').style.display = 'none';
  });

  // Auto-login when a token is already stored.
  if (state.token) {
    api('/api/admin/me')
      .then(enterConsole)
      .catch(() => logout());
  }
}

document.addEventListener('DOMContentLoaded', init);
