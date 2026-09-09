/**
 * detector.js — AI 检测子页面（/ai-detect/）交互
 * Depends on: common.js (_csrfFetch), auth.js (showAuthModal/logout/checkLoginStatus)
 * 与 Huma 首页共用登录态；充值复用支付宝接口。
 */

/* ========== 常量 ========== */
const DET_PRICE_PER_1000 = 4.9;                 // 与 config_detector 保持一致
const DET_PACKAGES = [1000, 2000, 5000];        // 与 config_detector 保持一致
let detOrderId = null;                          // 当前轮询中的订单
let detPollTimer = null;
let detPendingAnalyze = null;                   // 登录/充值到账后自动重试的检测
let detRechargeCtx = { mode: 'manual', needWords: 0 }; // auto=被额度拦截按需自动充值

/* ========== 工具 ========== */
function detEscapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function detToast(msg, type) {
    if (typeof showToast === 'function') { showToast(msg, type || 'info'); return; }
    alert(msg);
}

function zoneOf(risk) {
    if (risk > 60) return { color: '#dc2626', label: 'AI 特征明显', cls: 'high' };
    if (risk >= 20) return { color: '#d97706', label: '存在 AI 特征', cls: 'mid' };
    return { color: '#059669', label: '自然度高', cls: 'low' };
}

/* 半圆罗盘 SVG（0% 左端 → 100% 右端，绿→琥珀→红三段） */
function renderGauge(risk) {
    const L = Math.PI * 80;           // 半圆弧长 (r=80)
    const seg = pct => L * pct / 100; // 百分比对应弧长
    const arcPath = 'M20 100 A80 80 0 0 1 180 100';
    const segs = [
        { color: '#10b981', len: seg(20), off: 0 },                       // 0-20%
        { color: '#f59e0b', len: seg(40), off: seg(20) },                 // 20-60%
        { color: '#ef4444', len: seg(40), off: seg(60) },                 // 60-100%
    ];
    const bands = segs.map(s =>
        `<path d="${arcPath}" fill="none" stroke="${s.color}" stroke-width="14" stroke-linecap="butt"
               stroke-dasharray="${s.len} ${L}" stroke-dashoffset="${s.off}" opacity="0.25"/>`
    ).join('');
    const z = zoneOf(risk);
    const deg = 180 - risk * 1.8;                 // 0%→180°(左) 100%→0°(右)
    const rad = deg * Math.PI / 180;
    const px = 100 + 66 * Math.cos(rad);
    const py = 100 - 66 * Math.sin(rad);
    return `
    <svg viewBox="0 0 200 118" aria-label="AI 风险 ${risk}%">
      <defs>
        <linearGradient id="nd-needle" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stop-color="#4f46e5"/><stop offset="100%" stop-color="#06b6d4"/>
        </linearGradient>
      </defs>
      ${bands}
      <path d="${arcPath}" fill="none" stroke="#e5e7eb" stroke-width="1.5"/>
      <line x1="100" y1="100" x2="${px.toFixed(1)}" y2="${py.toFixed(1)}" stroke="url(#nd-needle)" stroke-width="4" stroke-linecap="round"/>
      <circle cx="100" cy="100" r="6" fill="#4f46e5"/>
      <text x="20"  y="115" font-size="11" fill="#9ca3af" text-anchor="middle">0</text>
      <text x="180" y="115" font-size="11" fill="#9ca3af" text-anchor="middle">100%</text>
    </svg>`;
}

/* ========== 结果渲染 ========== */
function renderResult(data) {
    const s = data.summary || {};
    const risk = s.risk_percent || 0;
    const z = zoneOf(risk);
    const attr = s.attribution || { summary_text: '', groups: [] };
    const showHigh = risk >= 20;  // 与路由口径一致：<20% 才是低风险保护档

    // 归因条按方向分流显示：
    // · 高风险且有 raisers → 显示 raisers（红色越靠前越优先处理）
    // · 高风险但无 raisers → 显示 lowers（让用户看到模型认为的「人类写作特征」）
    // · 低风险 → 显示 lowers（绿色 = 在压低风险）
    const allGroups = attr.groups || [];
    const raisers = allGroups.filter(g => g.direction === 'raise');
    const lowers = allGroups.filter(g => g.direction === 'lower');
    let displayGroups;
    if (showHigh) {
        displayGroups = raisers.length ? raisers : lowers;   // 高风险：无抬升因素则显示人类特征
    } else {
        displayGroups = lowers;                              // 低风险：只显示压低风险的特征
    }
    const groups = displayGroups.slice(0, 5);
    const bars = groups.map(g => `
          <div class="attr-row">
            <span class="attr-label">${detEscapeHtml(g.label)}</span>
            <span class="attr-track"><span class="attr-fill ${g.direction === 'lower' ? 'protective' : 'raise'}" style="width:${g.contribution_percent}%"></span></span>
            <span class="attr-pct">${g.contribution_percent}%</span>
          </div>`).join('');

    // 人话总结：根据实际显示的方向生成（与归因条内容一致，避免「建议说主要问题、列表全绿」矛盾）
    const topLabels = groups.slice(0, 2).map(g => '「' + g.label + '」').join('、');
    let takeaway;
    let takeawayWord;
    if (raisers.length) {
        // 有抬升因素：建议优先处理 top raisers
        takeaway = topLabels
            ? `整体 AI 风险 ${risk}%，主要问题在 ${topLabels}，建议优先调整这些表达以降低风险。`
            : `整体 AI 风险 ${risk}%，建议优先重写标黄的高风险段落。`;
        takeawayWord = '建议';
    } else if (lowers.length) {
        // 无抬升因素（全是人类特征），高/低风险两种表述
        takeaway = showHigh
            ? (topLabels
                ? `整体 AI 风险 ${risk}%，未识别到显著抬升因素，文本的人类写作特征主要体现在 ${topLabels}，建议结合段落明细综合判断。`
                : `整体 AI 风险 ${risk}%，未识别到显著抬升因素，建议结合段落明细综合判断。`)
            : `整体 AI 风险仅 ${risk}%，文本较自然，主要人类写作特征体现在 ${topLabels}，保持现有写作习惯即可。`;
        takeawayWord = showHigh ? '小结' : '小结';
    } else {
        takeaway = `整体 AI 风险 ${risk}%，参考段落明细综合判断。`;
        takeawayWord = '小结';
    }

    // caption：按实际显示方向调整文案，与归因条颜色对应
    let caption;
    if (raisers.length) {
        caption = '下列百分比为各因素对判定结果的相对贡献（取贡献最大的 5 项）。红色条越靠前越值得优先处理。';
    } else if (showHigh) {
        caption = '下列百分比为各因素对判定结果的相对贡献（取贡献最大的 5 项）。当前未识别到显著抬升因素，下列为相对贡献最大的人类写作特征（绿色）。';
    } else {
        caption = '下列百分比为各因素对判定结果的相对贡献（取贡献最大的 5 项）。绿色表示这些维度写得很像人、在压低风险。';
    }

    // 段落 pill
    const paras = (data.paragraphs || []).map(p => {
        const cls = p.risk_level === '高' || p.risk_percent >= 60 ? 'high' : 'low';
        return `
        <article class="paragraph ${cls === 'high' ? 'high-risk' : ''}">
          <header>
            <span class="p-meta">${detEscapeHtml(p.id)} · ${p.word_count} 词</span>
            <span class="pill ${cls}">${p.risk_level}风险 ${p.risk_percent}%</span>
          </header>
          <p>${detEscapeHtml(p.text)}</p>
        </article>`;
    }).join('');

    document.getElementById('det-score-card').innerHTML = `
        ${renderGauge(risk)}
        <div class="det-risk-readout">
            <div class="det-risk-label">整体 AI 风险率</div>
            <div class="det-risk-num" style="color:${z.color}">${risk}%</div>
            <div class="det-risk-level" style="color:${z.color}">${z.label}</div>
        </div>`;

    document.getElementById('det-chips').innerHTML = `
        <span class="det-chip">有效覆盖 <b>${s.coverage}%</b></span>
        <span class="det-chip">分析段落 <b>${s.paragraphs}</b></span>
        <span class="det-chip">高风险段 <b>${s.high_risk_paragraphs}</b></span>
        ${s.remaining_words !== undefined
            ? `<span class="det-chip">剩余检测额度 <b>${s.remaining_words}</b> 词</span>` : ''}`;

    const attrEl = document.getElementById('det-attribution');
    // 只要存在归因数据（分析句或维度条）就显示整卡，避免整体分析被连带隐藏
    const hasAttrData = (attr.summary_text || '').trim() || groups.length;
    attrEl.hidden = !hasAttrData;
    attrEl.innerHTML = `
        <h3>🧭 风险归因</h3>
        <p class="attr-lead">${detEscapeHtml(attr.summary_text || '')}</p>
        <p class="attr-caption">${detEscapeHtml(caption)}</p>
        <div class="attr-list">${bars}</div>
        <div class="attr-takeaway"><b>${takeawayWord}：</b>${detEscapeHtml(takeaway)}</div>`;

    document.getElementById('det-paragraphs').innerHTML = paras || '<p style="color:var(--gray-500);text-align:center;">无有效段落。</p>';
    document.getElementById('det-result-meta').textContent = `模型 ${detEscapeHtml(s.model_version || 'v2')} · ${data.words_detected !== undefined ? '本次消耗 ' + data.words_detected + ' 词' : ''}`;
    document.getElementById('det-result').style.display = 'block';
    document.getElementById('det-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
    refreshNavQuota();
}

function refreshNavQuota() {
    if (typeof checkLoginStatus === 'function') checkLoginStatus();
}

/* ========== 检测流程 ========== */
async function runDetect() {
    const btn = document.getElementById('det-analyze-btn');
    const fileInput = document.getElementById('det-file-input');
    const textInput = document.getElementById('det-text-input');
    btn.disabled = true; btn.textContent = '⏳ 检测中…';

    try {
        let resp;
        if (fileInput.files && fileInput.files[0]) {
            const fd = new FormData();
            fd.append('file', fileInput.files[0]);
            resp = await fetch('/ai-detect/api/analyze-file', { method: 'POST', body: fd });
        } else {
            const text = textInput.value.trim();
            if (!text) { detToast('请先粘贴英文文本或上传文件。', 'error'); return; }
            resp = await _csrfFetch('/ai-detect/api/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text })
            });
        }
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 401) {
            detPendingAnalyze = runDetect;          // 登录成功后自动重试
            document.getElementById('det-login-hint-holder')?.remove?.();
            showAuthModal('login');
            detToast('请先登录后使用检测（注册送 1000 词/月）。', 'info');
            return;
        }
        if (resp.status === 402) {
            detPendingAnalyze = runDetect;          // 充值到账后自动重试（无需再点检测）
            openDetRechargeModal(data.need_words || null, data);
            detToast(data.error || '检测额度不足，请充值后继续。', 'error');
            return;
        }
        if (!resp.ok) { detToast(data.error || '检测失败，请重试。', 'error'); return; }
        // 消耗展示以扣费口径为准（短段可能被过滤，按段落求和会少报）
        const analysis = data.analysis || {};
        const consumed = analysis.summary && analysis.summary.words_charged !== undefined
            ? analysis.summary.words_charged
            : (analysis.paragraphs || []).reduce((n, p) => n + (p.word_count || 0), 0);
        renderResult({ ...analysis, words_detected: consumed });
    } catch (err) {
        console.error(err);
        detToast('网络错误，请重试。', 'error');
    } finally {
        btn.disabled = false; btn.textContent = '🔍 开始检测';
    }
}

/* ========== 充值 ========== */
let detChosenWords = 1000;

function detPriceOf(words) { return Math.round(DET_PRICE_PER_1000 * words / 1000 * 100) / 100; }

function renderDetPackages() {
    const wrap = document.getElementById('det-package-options');
    wrap.innerHTML = DET_PACKAGES.map(w =>
        `<button type="button" class="recharge-option ${w === detChosenWords ? 'active' : ''}" data-words="${w}"
            onclick="pickDetPackage(${w})">${w} 词<br><small>¥${detPriceOf(w)}</small></button>`
    ).join('') +
    `<button type="button" class="recharge-option recharge-custom" onclick="customDetPackage()">自定义<br><small>¥/词</small></button>`;
}

function pickDetPackage(w) {
    detChosenWords = w;
    renderDetPackages();
    updateDetPaySummary();
}

function customDetPackage() {
    const input = prompt('输入要充值的检测词数（≥100）：', '1000');
    const w = parseInt(input, 10);
    if (isNaN(w) || w < 100) { detToast('请输入 ≥100 的词数。', 'error'); return; }
    detChosenWords = Math.min(w, 100000);
    renderDetPackages();
    updateDetPaySummary();
}

function updateDetPaySummary() {
    const words = detChosenWords;
    const price = detPriceOf(words);
    document.getElementById('det-pay-words').textContent = words + ' 词';
    document.getElementById('det-pay-price').textContent = '¥' + price.toFixed(2);
    // auto/manual 按钮文案均在模板中固定，无需此处改写
}

/* 自动充值模式（顶部一句提示+居中按钮，隐藏档位选择）/ 手动档位模式切换 */
function setDetRechargeMode(mode) {
    detRechargeCtx.mode = mode;
    const auto = mode === 'auto';
    const layout = document.getElementById('det-pay-layout');
    if (layout) {
        layout.classList.toggle('auto', auto);
        // paying 态不受模式切换影响（用户已下单），但 setDetRechargeMode 通常在打开弹窗/主动切档位时调用
    }
    const autoBox = document.getElementById('det-auto-box');
    if (autoBox) autoBox.style.display = auto ? 'flex' : 'none';
    const picker = document.getElementById('det-package-picker');
    if (picker) picker.style.display = auto ? 'none' : '';
    if (auto) updateDetPaySummary();  // 更新手动摘要仍可能用到
}

function toggleDetPackagePicker() {
    setDetRechargeMode('manual');
}

async function openDetRechargeModal(needWords, errData) {
    const el = document.getElementById('det-recharge-modal');
    const layout = document.getElementById('det-pay-layout');
    if (layout) { layout.classList.remove('paying'); layout.classList.remove('auto'); }
    // 刷新当前额度
    let quota = { detection_words: 0, free_words: 0, paid_words: 0 };
    try {
        const r = await _csrfFetch('/ai-detect/api/quota');
        const q = await r.json();
        if (!q.error) {
            quota = { detection_words: q.detection_words || 0, free_words: q.free_words || 0, paid_words: q.paid_words || 0 };
            document.getElementById('det-pay-balance').textContent =
                quota.detection_words + ' 词（免费 ' + quota.free_words + ' + 充值 ' + quota.paid_words + '）';
        }
    } catch (_) { /* 静默 */ }

    // 被额度拦截（needWords>0）→ 自动按需精确购买（最小 100 词）；主动充值 → 手动档位
    const need = (needWords && needWords > 0) ? Math.ceil(needWords) : 0;
    const remaining = (errData && Number.isFinite(errData.remaining_words)) ? errData.remaining_words : quota.detection_words;
    const totalRequired = remaining + need;  // 本次要检测的词数（由 remaining + need 还原）
    detRechargeCtx = {
        mode: need > 0 ? 'auto' : 'manual',
        needWords: need,
        totalRequired,
        balance: quota.detection_words,
        free: quota.free_words,
        paid: quota.paid_words,
    };
    detChosenWords = need > 0 ? Math.max(need, 100) : 1000;  // 按需精确，最小 100 词（后端下限）

    // 渲染 auto 模式的三行明细
    const setTxt = (id, v) => { const n = document.getElementById(id); if (n) n.textContent = v; };
    const autoPrice = detPriceOf(detChosenWords);
    setTxt('det-auto-needwords', totalRequired.toLocaleString('en-US') + ' 词');
    setTxt('det-auto-balance', quota.detection_words.toLocaleString('en-US') + ' 词（免费 ' + quota.free_words + ' + 充值 ' + quota.paid_words + '）');
    setTxt('det-auto-needpay', need.toLocaleString('en-US') + ' 词');
    setTxt('det-auto-price', '¥' + autoPrice.toFixed(2));

    document.getElementById('det-qr-section').style.display = 'none';
    document.getElementById('det-recharge-error').style.display = 'none';
    document.getElementById('det-poll-status').textContent = '⏳ 等待支付中…';
    const submitBtn = document.getElementById('det-recharge-submit-btn');
    if (submitBtn) submitBtn.disabled = false;
    const autoSubmitBtn = document.getElementById('det-auto-submit-btn');
    if (autoSubmitBtn) autoSubmitBtn.disabled = false;
    renderDetPackages();
    setDetRechargeMode(detRechargeCtx.mode);

    el.style.display = 'flex';
    document.body.style.overflow = 'hidden';

    // mock 模式检测（仅开发可见模拟支付按钮）
    try {
        const r = await fetch('/api/payment-config', { credentials: 'same-origin' });
        const cfg = await r.json();
        document.getElementById('det-mock-section').style.display = cfg.is_mock ? 'block' : 'none';
    } catch (_) { /* 静默 */ }
}

function closeDetRechargeModal(opts) {
    const keepPending = !!(opts && opts.keepPending);
    const layout = document.getElementById('det-pay-layout');
    if (layout) layout.classList.remove('paying');
    document.getElementById('det-recharge-modal').style.display = 'none';
    document.body.style.overflow = '';
    if (detPollTimer) { clearInterval(detPollTimer); detPollTimer = null; }
    if (!keepPending) detPendingAnalyze = null;   // 手动关闭 = 放弃自动续测
}

/* 登录成功 / 充值到账后：自动继续被拦截的检测 */
function detResumePendingAnalysis() {
    if (!detPendingAnalyze) return;
    const fn = detPendingAnalyze;
    detPendingAnalyze = null;
    setTimeout(fn, 300);
}

let detSubmitting = false;   // 防连点：下单中禁止重复提交

async function submitDetRecharge() {
    if (detSubmitting) return;
    detSubmitting = true;
    const words = detChosenWords;
    const errEl = document.getElementById('det-recharge-error');
    errEl.style.display = 'none';
    // auto 模式按钮与 manual 模式按钮是同一逻辑的两入口，共用防连点锁
    const submitBtns = [
        document.getElementById('det-recharge-submit-btn'),
        document.getElementById('det-auto-submit-btn'),
    ].filter(Boolean);
    submitBtns.forEach(b => b.disabled = true);
    try {
        const resp = await _csrfFetch('/ai-detect/api/recharge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ words })
        });
        const data = await resp.json();
        if (!resp.ok) {
            errEl.textContent = data.error || '下单失败';
            errEl.style.display = 'block';
            submitBtns.forEach(b => b.disabled = false);
            return;
        }
        const order = data.order;
        detOrderId = order.order_id;
        // 下单成功 → 进入支付视图（隐藏左侧档位/摘要，二维码居中直出）
        const layout = document.getElementById('det-pay-layout');
        if (layout) layout.classList.add('paying');
        // 渲染二维码
        const qrBox = document.getElementById('det-qrcode-container');
        qrBox.innerHTML = '';
        document.getElementById('det-qr-section').style.display = 'block';
        if (typeof QRCode !== 'undefined' && order.qr_code) {
            new QRCode(qrBox, { text: order.qr_code, width: 200, height: 200 });
        } else if (order.form_html) {
            qrBox.innerHTML = order.form_html;
        } else {
            qrBox.innerHTML = '<span style="color:#999;">二维码生成失败，请刷新页面重试。</span>';
        }
        // 绑定 mock 支付（若 mock adapter）
        const mockBtn = document.getElementById('det-mock-pay-btn');
        mockBtn.onclick = async () => {
            await _csrfFetch('/api/test/mock-payment/' + order.order_id, { method: 'POST' });
        };
        startDetPolling(order.order_id);
    } catch (err) {
        console.error(err);
        errEl.textContent = '网络错误，请重试。';
        errEl.style.display = 'block';
        submitBtns.forEach(b => b.disabled = false);
    } finally {
        detSubmitting = false;
        // paying 态下两按钮均隐藏（CSS .paying .det-auto-box{display:none} 与 .paying .payment-summary{display:none}），无需再 enable；异常路径里上面已恢复。
    }
}

function startDetPolling(orderId) {
    if (detPollTimer) clearInterval(detPollTimer);
    const statusEl = document.getElementById('det-poll-status');
    let tries = 0;
    detPollTimer = setInterval(async () => {
        tries++;
        try {
            const resp = await _csrfFetch('/ai-detect/api/recharge-status/' + orderId);
            const data = await resp.json();
            if (data.payment_status === 'paid') {
                clearInterval(detPollTimer); detPollTimer = null;
                document.getElementById('det-qr-section').style.display = 'none';
                statusEl.innerHTML = '<span class="det-paid-ok">✅ 支付成功，额度已到账，正在自动继续检测…</span>';
                closeDetRechargeModal({ keepPending: true });   // 保留 pending 用于自动续测
                refreshNavQuota();
                detToast('充值成功！已自动继续检测。', 'success');
                detResumePendingAnalysis();   // 自动重跑被拦截的检测，无需再次点击
                return;
            }
            if (tries > 120) { clearInterval(detPollTimer); detPollTimer = null; statusEl.textContent = '⏳ 二维码已过期，请关闭后重新下单'; }
        } catch (_) { /* 轮询失败静默，下轮重试 */ }
    }, 3000);   // 3s 轮询 = 20/min，恰好不超后端 recharge-status 限流
}

document.getElementById('det-recharge-modal').addEventListener('click', (e) => {
    if (e.target.id === 'det-recharge-modal') closeDetRechargeModal();
});

/* 用户手动关闭登录框（点 X / 遮罩）→ 放弃被拦的自动续测；
   登录成功路径由 handleLogin/handleRegister 直接调用 closeAuthModal()，不会触发这里。 */
(function bindAuthCancel() {
    const modal = document.getElementById('auth-modal');
    if (!modal) return;
    modal.addEventListener('click', (e) => {
        if (e.target === modal || e.target.classList.contains('modal-close')) {
            detPendingAnalyze = null;
        }
    });
})();

/* ========== 事件绑定 ========== */
document.addEventListener('DOMContentLoaded', () => {
    const dropZone = document.getElementById('det-drop-zone');
    const fileInput = document.getElementById('det-file-input');
    const textInput = document.getElementById('det-text-input');
    const form = document.getElementById('det-form');

    const DROP_TEXT_DEFAULT = '拖拽文档到此处，或 <span class="drop-link">点击选择文件</span>';
    const DROP_HINT_DEFAULT = '支持 .txt、.docx，最大 20MB';
    const TEXT_PLACEHOLDER_DEFAULT = '直接粘贴英文文本，检测 AI 写作特征…';

    const dropTextEl = dropZone.querySelector('.drop-text');
    const dropHintEl = dropZone.querySelector('.drop-hint');

    // 选中文件后，在上传区（drop-zone）内就地展示文件名 —— 绿色高亮态
    function applySelectedFile(file) {
        if (!file) return;
        dropZone.classList.add('has-file');
        if (dropTextEl) {
            dropTextEl.innerHTML = `📄 ${detEscapeHtml(file.name)} <span class="file-size">(${(file.size / 1024).toFixed(1)} KB)</span>`;
        }
        if (dropHintEl) {
            dropHintEl.innerHTML = '文件已就绪，点击可更换 · <a href="javascript:void(0)" class="drop-link remove-file" id="det-file-remove">移除文件</a>';
            const rm = document.getElementById('det-file-remove');
            if (rm) rm.addEventListener('click', (e) => { e.stopPropagation(); clearSelectedFile(); });
        }
        if (textInput) { textInput.value = ''; textInput.placeholder = TEXT_PLACEHOLDER_DEFAULT; }
        detToast(`已选择文件：${file.name}`, 'success');
    }
    function clearSelectedFile() {
        fileInput.value = '';
        dropZone.classList.remove('has-file');
        if (dropTextEl) dropTextEl.innerHTML = DROP_TEXT_DEFAULT;
        if (dropHintEl) dropHintEl.innerHTML = DROP_HINT_DEFAULT;
        detToast('已移除文件，可粘贴文本或重新选择', 'info');
    }

    dropZone.addEventListener('click', (e) => {
        if (e.target.id === 'det-file-remove') return;   // 移除链接自行处理
        fileInput.click();
    });
    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', e => {
        e.preventDefault(); dropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length) {
            fileInput.files = e.dataTransfer.files;
            applySelectedFile(e.dataTransfer.files[0]);
        }
    });
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length) applySelectedFile(fileInput.files[0]);
    });

    form.addEventListener('submit', e => { e.preventDefault(); runDetect(); });
});
