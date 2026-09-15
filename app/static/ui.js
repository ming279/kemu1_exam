/* ==========================================================================
   科目一考试 · 全站交互增强（零依赖）
   1. 顶部滚动进度条
   2. 滚动揭示（JS 注入 class，避免脚本失效导致内容不可见）
   3. 侧栏折叠（自动包裹文字 + data-label，localStorage 记忆）
   4. 沉浸模式：考试 / 练习 / PK 自动收起侧栏
   5. 键盘答题：A/B/C/D 选择 · ←/→ 切题 · M 标记
   6. 大字号开关
   7. 交卷前未答提醒 / 倒计时紧急态 / 图表骨架屏
   ========================================================================== */
(function () {
  'use strict';

  var body = document.body;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var LS = {
    collapse: 'kemu_sb_collapsed',
    font: 'kemu_big_font'
  };
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  /* ------------------------------------------------------------------
     1. 顶部进度条
  ------------------------------------------------------------------ */
  var bar = document.createElement('div');
  bar.className = 'ui-progress';
  bar.innerHTML = '<span></span>';
  document.body.appendChild(bar);
  var barFill = bar.firstChild;

  /* ------------------------------------------------------------------
     2. 滚动揭示：只给已进入视口的元素补 .in
  ------------------------------------------------------------------ */
  var REVEAL_SEL = '.card, .stat-card, .ach-item, .empty, .chart-card';
  var io = null;
  function setupReveal() {
    var items = document.querySelectorAll(REVEAL_SEL);
    if (!items.length) return;
    if (reduce || !('IntersectionObserver' in window)) {
      // 直接可见，不做位移动画
      return;
    }
    io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add('in');
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    var below = [];
    Array.prototype.forEach.call(items, function (el) {
      var r = el.getBoundingClientRect();
      // 首屏元素直接跳过，避免"先显示再隐藏"的闪烁
      if (r.top < window.innerHeight * 0.95) return;
      below.push(el);
    });
    below.forEach(function (el, i) {
      el.style.setProperty('--d', (Math.min(i % 6, 5) * 60) + 'ms');
      el.classList.add('reveal');
      io.observe(el);
    });
  }

  /* ------------------------------------------------------------------
     3. 侧栏折叠：自动把裸文本包进 <span class="lbl">，并写入 data-label
  ------------------------------------------------------------------ */
  var collapseBtn = null;
  function prepareSidebar() {
    var items = document.querySelectorAll('.sidebar .sb-item');
    Array.prototype.forEach.call(items, function (el) {
      var lbl = el.querySelector('.lbl');
      if (!lbl) {
        lbl = document.createElement('span');
        lbl.className = 'lbl';
        var texts = [];
        Array.prototype.slice.call(el.childNodes).forEach(function (n) {
          var isText = n.nodeType === 3 && n.textContent.trim().length > 0;
          var isLabel = n.nodeType === 1
            && n.nodeName.toLowerCase() !== 'svg'
            && n.tagName !== 'INPUT'
            && !(n.classList && n.classList.contains('sb-badge'));
          if (isText || isLabel) { texts.push(n.textContent); el.removeChild(n); }
        });
        lbl.textContent = texts.join('').trim();
        var badge = el.querySelector('.sb-badge');
        if (badge) el.insertBefore(lbl, badge); else el.appendChild(lbl);
      }
      if (!el.getAttribute('data-label')) {
        el.setAttribute('data-label', lbl.textContent.trim().replace(/\s+/g, ' '));
      }
    });

    // 折叠按钮：优先复用顶栏已有的 #btn-collapse，否则自建
    collapseBtn = document.getElementById('btn-collapse');
    if (!collapseBtn) {
      var topbar = document.querySelector('.topbar');
      if (topbar) {
        collapseBtn = document.createElement('button');
        collapseBtn.className = 'icon-btn';
        collapseBtn.id = 'btn-collapse';
        collapseBtn.title = '折叠 / 展开侧栏';
        collapseBtn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/><path d="m15 10-2 2 2 2"/></svg>';
        topbar.insertBefore(collapseBtn, topbar.firstChild);
      }
    }
    if (collapseBtn) {
      collapseBtn.addEventListener('click', function () { toggleCollapse(); });
    }
    if (lsGet(LS.collapse) === '1') applyCollapse(true);
  }

  function applyCollapse(on) {
    body.classList.toggle('sb-collapsed', on);
    if (collapseBtn) collapseBtn.classList.toggle('active', on);
  }
  function toggleCollapse() {
    var on = !body.classList.contains('sb-collapsed');
    applyCollapse(on);
    lsSet(LS.collapse, on ? '1' : '0');
  }

  /* ------------------------------------------------------------------
     4. 沉浸模式：答题类页面自动收起侧栏
  ------------------------------------------------------------------ */
  var IMMERSIVE_RE = /^\/(exam|practice|pk\/|task\/|wrongbook\/sim)/;
  var focusExit = null;
  function setupImmersive() {
    if (!IMMERSIVE_RE.test(location.pathname)) return;
    if (!document.querySelector('.sidebar')) return;
    body.classList.add('immersive');
    focusExit = document.createElement('button');
    focusExit.className = 'ui-focus-exit';
    focusExit.type = 'button';
    focusExit.textContent = '⇤ 显示侧栏';
    focusExit.addEventListener('click', function () {
      body.classList.remove('immersive');
      focusExit.remove();
    });
    document.body.appendChild(focusExit);
  }

  /* ------------------------------------------------------------------
     5. 键盘答题：A/B/C/D 选择 · ←/→ 切题 · M 标记
  ------------------------------------------------------------------ */
  function currentBlock() {
    var blocks = document.querySelectorAll('.q-block, .card.q-card');
    var best = null, bestTop = -Infinity;
    Array.prototype.forEach.call(blocks, function (b) {
      var r = b.getBoundingClientRect();
      var top = r.top;
      if (top < window.innerHeight * 0.55 && top > bestTop) { bestTop = top; best = b; }
    });
    return best;
  }
  function setupKeys() {
    if (!document.querySelector('.q-block, .opt, .opt-btn')) return;

    // 快捷键提示
    if (document.querySelector('.exam-layout, .q-block')) {
      var hint = document.createElement('div');
      hint.className = 'muted small';
      hint.style.cssText = 'margin:6px 0 0';
      hint.innerHTML = '快捷键：<b>A/B/C/D</b> 选择 · <b>←/→</b> 切题 · <b>M</b> 标记';
      var first = document.querySelector('.exam-questions > h2, .card h2');
      if (first && first.parentNode) first.parentNode.insertBefore(hint, first.nextSibling);
    }

    document.addEventListener('keydown', function (e) {
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      var k = e.key.toLowerCase();

      // ←/→ 切题
      if (k === 'arrowleft' || k === 'arrowright') {
        var blocks = Array.prototype.slice.call(document.querySelectorAll('.q-block[id], .q-block'));
        if (blocks.length > 1) {
          var cur = currentBlock();
          var idx = blocks.indexOf(cur);
          var next = blocks[idx + (k === 'arrowright' ? 1 : -1)];
          if (next) {
            next.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
            e.preventDefault();
          }
        }
        return;
      }

      var block = currentBlock();
      if (!block) return;

      // M 标记
      if (k === 'm') {
        var mark = block.querySelector('.q-mark-btn');
        if (mark) { mark.click(); e.preventDefault(); }
        return;
      }

      // A/B/C/D 或 1-4
      var idxOpt = -1;
      if (k === 'a' || k === '1') idxOpt = 0;
      else if (k === 'b' || k === '2') idxOpt = 1;
      else if (k === 'c' || k === '3') idxOpt = 2;
      else if (k === 'd' || k === '4') idxOpt = 3;
      if (idxOpt >= 0) {
        var opts = block.querySelectorAll('.opt, .opt-btn');
        var target = opts[idxOpt];
        if (target) {
          var input = target.querySelector('input');
          if (input) input.click(); else target.click();
          e.preventDefault();
        }
      }
    });
  }

  /* ------------------------------------------------------------------
     6. 大字号开关
  ------------------------------------------------------------------ */
  function setupFont() {
    var topbar = document.querySelector('.topbar-right');
    if (!topbar) return;
    var btn = document.createElement('button');
    btn.className = 'icon-btn';
    btn.id = 'btn-font';
    btn.title = '大字号';
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20L10 4l6 16"/><path d="M6.5 14h7"/><path d="M18 20l3-9 3 9"/><path d="M19.2 17h3.6"/></svg>';
    btn.addEventListener('click', function () {
      var on = !body.classList.contains('big-font');
      body.classList.toggle('big-font', on);
      btn.classList.toggle('active', on);
      lsSet(LS.font, on ? '1' : '0');
    });
    topbar.insertBefore(btn, topbar.querySelector('#btn-dark') || topbar.firstChild);
    if (lsGet(LS.font) === '1') {
      body.classList.add('big-font');
      btn.classList.add('active');
    }
  }

  /* ------------------------------------------------------------------
     7. 交卷前未答提醒
  ------------------------------------------------------------------ */
  function setupUnanswered() {
    var submit = null;
    var els = document.querySelectorAll('button, a.btn, input[type=submit]');
    Array.prototype.forEach.call(els, function (el) {
      var txt = (el.textContent || el.value || '').trim();
      if (txt === '交卷' || txt.indexOf('交卷并') === 0) submit = submit || el;
    });
    if (!submit) return;
    submit.addEventListener('click', function (e) {
      var cells = document.querySelectorAll('.ac-cell');
      if (!cells.length) return;
      var total = cells.length, done = 0;
      Array.prototype.forEach.call(cells, function (c) { if (c.classList.contains('answered')) done++; });
      var miss = total - done;
      if (miss > 0) {
        var ok = window.confirm('还有 ' + miss + ' 题未作答（共 ' + total + ' 题）。\n\n点击「确定」直接交卷，点击「取消」返回继续作答。');
        if (!ok) { e.preventDefault(); e.stopPropagation(); }
      }
    }, true);
  }

  /* ------------------------------------------------------------------
     8. 倒计时紧急态 + 图表骨架屏
  ------------------------------------------------------------------ */
  function setupCountdown() {
    var box = document.querySelector('.countdown-box');
    if (!box) return;
    var num = box.querySelector('.countdown-num');
    if (!num) return;
    setInterval(function () {
      var txt = (num.textContent || '').trim();
      var m = txt.match(/^(\d+):(\d{2})(?::(\d{2}))?$/);
      if (!m) return;
      var total = m[3] ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3])
                       : (+m[1]) * 60 + (+m[2]);
      box.classList.toggle('urgent', total > 0 && total <= 300);
    }, 3000);
  }
  function setupSkeleton() {
    var boxes = document.querySelectorAll('[id^="chart-"], .gh-heatmap');
    Array.prototype.forEach.call(boxes, function (b) {
      if (b.offsetHeight > 40) return;         // 已渲染
      b.classList.add('skel');
      setTimeout(function () { b.classList.remove('skel'); }, 1200);
    });
  }

  /* ------------------------------------------------------------------
     9. 移动端答题条 + 答题卡底部抽屉（仅窄屏考试/练习页）
  ------------------------------------------------------------------ */
  function setupMobileBar() {
    if (!document.querySelector('.exam-layout')) return;
    if (window.innerWidth > 900) return;
    var cells = document.querySelectorAll('.ac-cell');
    var card = document.querySelector('.answer-card');
    var bar2 = document.createElement('div');
    bar2.className = 'exam-mobilebar';
    bar2.innerHTML =
      '<div class="mb-info"><b>已答 <span id="mb-done">0</span> / ' + cells.length + ' 题</b>' +
      '<span id="mb-mark">标记 0</span></div>' +
      (card ? '<button class="btn secondary small" id="mb-sheet" type="button">答题卡</button>' : '');
    document.body.appendChild(bar2);
    document.body.classList.add('has-mobilebar');

    var sheet = document.getElementById('mb-sheet');
    if (sheet && card) {
      sheet.addEventListener('click', function () { card.classList.toggle('open'); });
      card.addEventListener('click', function (e) {
        if (e.target.closest('.ac-cell')) setTimeout(function () { card.classList.remove('open'); }, 220);
      });
    }
    function recount() {
      var done = 0, mark = 0;
      Array.prototype.forEach.call(cells, function (c) {
        if (c.classList.contains('answered')) done++;
        if (c.classList.contains('marked')) mark++;
      });
      var d = document.getElementById('mb-done'); if (d) d.textContent = done;
      var m = document.getElementById('mb-mark'); if (m) m.textContent = '标记 ' + mark;
    }
    recount();
    setInterval(recount, 2000);
  }

  /* ------------------------------------------------------------------
     滚动帧合并：进度条
  ------------------------------------------------------------------ */
  var ticking = false;
  function frame() {
    ticking = false;
    var y = window.pageYOffset || document.documentElement.scrollTop;
    var h = document.documentElement.scrollHeight - window.innerHeight;
    barFill.style.transform = 'scaleX(' + (h > 0 ? Math.min(y / h, 1) : 0) + ')';
  }
  window.addEventListener('scroll', function () {
    if (!ticking) { ticking = true; requestAnimationFrame(frame); }
  }, { passive: true });

  /* ------------------------------------------------------------------
     启动
  ------------------------------------------------------------------ */
  function boot() {
    prepareSidebar();
    setupImmersive();
    setupReveal();
    setupKeys();
    setupFont();
    setupUnanswered();
    setupCountdown();
    setupSkeleton();
    setupMobileBar();
    frame();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
