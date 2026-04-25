(function () {
  'use strict';

  // ─── SVG helpers ────────────────────────────────────────────────────────────

  function makeSVG(width, height, viewBox, paths) {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('xmlns', ns);
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    svg.setAttribute('viewBox', viewBox);
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    paths.forEach(function (def) {
      var el = document.createElementNS(ns, def.tag);
      Object.keys(def.attrs).forEach(function (k) {
        el.setAttribute(k, def.attrs[k]);
      });
      svg.appendChild(el);
    });
    return svg;
  }

  function fileTextIcon() {
    var svg = makeSVG('14', '14', '0 0 24 24', [
      { tag: 'path', attrs: { d: 'M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z' } },
      { tag: 'path', attrs: { d: 'M14 2v4a2 2 0 0 0 2 2h4' } },
      { tag: 'path', attrs: { d: 'M10 9H8' } },
      { tag: 'path', attrs: { d: 'M16 13H8' } },
      { tag: 'path', attrs: { d: 'M16 17H8' } },
    ]);
    svg.setAttribute('class', 'mewbo-result__file-icon');
    return svg;
  }

  // ─── Markdown renderer ──────────────────────────────────────────────────────

  function escapeHTML(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function applyInline(text) {
    // Order matters: links first so their inner text isn't mangled by inline rules
    // applied to surrounding prose. Then bold > italic > inline-code. Bare URLs are
    // auto-linked too so DeepWiki citations like "https://deepwiki.com/search/..."
    // become clickable.
    return text
      .replace(/\[([^\]]+?)\]\(([^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s)]+)/g,
        '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/`(.+?)`/g, '<code>$1</code>');
  }

  // Open all external links in a new tab. Used by both renderers.
  function externalizeLinks(html) {
    return html.replace(
      /<a (href="https?:\/\/[^"]+")(?![^>]*\btarget=)/g,
      '<a $1 target="_blank" rel="noopener noreferrer"'
    );
  }

  function renderMarkdown(text) {
    // Prefer the marked library when loaded (gfm tables, fenced code, nested
    // lists, numbered lists, blockquotes, hard line breaks). Falls back to the
    // tiny inline renderer below if marked is unavailable for any reason.
    if (window.marked && typeof window.marked.parse === 'function') {
      try {
        var html = window.marked.parse(text, { gfm: true, breaks: true });
        return externalizeLinks(html);
      } catch (_) {
        // Fall through to inline renderer
      }
    }
    return externalizeLinks(renderMarkdownInline(text));
  }

  function renderMarkdownInline(text) {
    var lines = text.split('\n');
    var html = '';
    var inList = false;
    var pendingBlank = false;

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      if (line === '') {
        if (inList) {
          html += '</ul>';
          inList = false;
        }
        pendingBlank = true;
        continue;
      }

      if (/^## /.test(line)) {
        if (inList) { html += '</ul>'; inList = false; }
        html += '<h4>' + applyInline(escapeHTML(line.slice(3))) + '</h4>';
        pendingBlank = false;
        continue;
      }

      if (/^# /.test(line)) {
        if (inList) { html += '</ul>'; inList = false; }
        html += '<h3>' + applyInline(escapeHTML(line.slice(2))) + '</h3>';
        pendingBlank = false;
        continue;
      }

      if (/^[*-] /.test(line)) {
        if (!inList) {
          if (pendingBlank && html !== '') html += '<br>';
          html += '<ul>';
          inList = true;
        }
        html += '<li>' + applyInline(escapeHTML(line.slice(2))) + '</li>';
        pendingBlank = false;
        continue;
      }

      if (inList) {
        html += '</ul>';
        inList = false;
      }

      if (pendingBlank && html !== '') {
        html += '</p><p>';
      } else if (html !== '') {
        html += ' ';
      }
      if (html === '' || html.endsWith('</p><p>')) {
        if (html === '') html += '<p>';
      }
      html += applyInline(escapeHTML(line));
      pendingBlank = false;
    }

    if (inList) html += '</ul>';
    if (html && !html.endsWith('>')) html += '</p>';

    return html;
  }

  // ─── Backend hook ───────────────────────────────────────────────────────────

  // Override window.mewboAskAI before this script loads to connect a real backend:
  // window.mewboAskAI = async (query) => { const r = await fetch(...); return r.text(); }
  async function askAIBackend(query) {
    if (typeof window.mewboAskAI === 'function') {
      return window.mewboAskAI(query);
    }
    await new Promise(function (r) { return setTimeout(r, 1800); });
    return '**Answer (mock)**\n\nThis is a placeholder response for: *' + query +
      '*\n\nConnect a real backend by setting `window.mewboAskAI` before this script loads.';
  }

  // ─── Bootstrap ──────────────────────────────────────────────────────────────

  function init() {
    var dialog = document.getElementById('mewbo-modal');
    if (!dialog) return;

    var searchInput   = document.getElementById('mewbo-search-input');
    var shortcutQuery = document.getElementById('mewbo-shortcut-query');
    var aiInput       = document.getElementById('mewbo-ai-input');
    var aiSend        = document.getElementById('mewbo-ai-send');
    var welcome       = document.getElementById('mewbo-welcome');
    var answer        = document.getElementById('mewbo-answer');
    var skeleton      = document.getElementById('mewbo-skeleton');
    var answerText    = document.getElementById('mewbo-answer-text');
    var askAnother    = document.getElementById('mewbo-ask-another');
    var resultsEl     = document.getElementById('mkdocs-search-results');
    var kbdModifier   = document.getElementById('mewbo-kbd-modifier');

    // ── Kbd modifier label ──────────────────────────────────────────────────
    if (kbdModifier) {
      kbdModifier.textContent = /Mac|iPhone|iPad|iPod/.test(navigator.userAgent) ? '⌘' : 'Ctrl';
    }

    // ── Ask AI state ────────────────────────────────────────────────────────
    var isLoading = false;

    function resetAskAI() {
      if (welcome) welcome.style.display = '';
      if (answer) answer.style.display = 'none';
      if (aiInput) { aiInput.value = ''; aiInput.disabled = false; }
      if (aiSend) { aiSend.disabled = true; aiSend.classList.remove('loading'); }
      if (answerText) answerText.innerHTML = '';
      if (askAnother) askAnother.style.display = 'none';
      if (skeleton) skeleton.style.display = 'none';
      isLoading = false;
    }

    async function submitAskAI(query) {
      if (!query || isLoading) return;
      isLoading = true;

      if (aiInput) aiInput.disabled = true;
      if (aiSend) { aiSend.disabled = true; aiSend.classList.add('loading'); }

      if (welcome) welcome.style.display = 'none';
      if (answer) answer.style.display = 'block';
      if (skeleton) skeleton.style.display = 'block';
      if (answerText) { answerText.style.display = 'none'; answerText.innerHTML = ''; }
      if (askAnother) askAnother.style.display = 'none';

      try {
        var result = await askAIBackend(query);
        if (skeleton) skeleton.style.display = 'none';
        if (answerText) {
          answerText.innerHTML = renderMarkdown(result);
          answerText.style.display = '';
        }
        if (askAnother) askAnother.style.display = '';
      } catch (err) {
        if (skeleton) skeleton.style.display = 'none';
        if (answerText) {
          answerText.innerHTML = '<p class="mewbo-answer__error">' +
            escapeHTML('Error: ' + (err && err.message ? err.message : 'Something went wrong.')) +
            '</p>';
          answerText.style.display = '';
        }
      }

      isLoading = false;
      if (aiInput) aiInput.disabled = false;
      if (aiSend) { aiSend.classList.remove('loading'); updateSendButton(); }
    }

    function updateSendButton() {
      if (!aiSend || !aiInput) return;
      aiSend.disabled = isLoading || aiInput.value.trim() === '';
    }

    // ── Tab switching ───────────────────────────────────────────────────────
    function switchTab(tab) {
      dialog.dataset.active = tab;
      if (tab === 'search') {
        if (searchInput) searchInput.focus();
        if (shortcutQuery && searchInput) {
          shortcutQuery.textContent = searchInput.value.trim();
        }
      } else {
        if (aiInput) aiInput.focus();
        resetAskAI();
      }
    }

    // ── Modal lifecycle ─────────────────────────────────────────────────────
    function openModal(tab) {
      dialog.showModal();
      dialog.dataset.active = tab;
      switchTab(tab);
    }

    function closeModal() {
      dialog.close();
    }

    dialog.addEventListener('click', function (e) {
      if (e.target === dialog) closeModal();
    });

    // Override the theme's onSearchBarClick so Ctrl K / ⌘ K opens our modal
    window.onSearchBarClick = function () { openModal('search'); };

    // Also intercept the theme's searchShortcutHandler keyboard binding by
    // adding our own keydown handler at capture phase (runs before theme's).
    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        openModal('search');
      }
    }, true);

    // ── Nav + FAB wiring ────────────────────────────────────────────────────
    var searchTrigger  = document.getElementById('mewbo-search-trigger');
    var navAskAI       = document.getElementById('mewbo-nav-ask-ai');
    var mobileSearch   = document.getElementById('mewbo-mobile-search');
    var fab            = document.getElementById('mewbo-fab');
    var askAIShortcut  = document.getElementById('mewbo-ask-ai-shortcut');

    if (searchTrigger)  searchTrigger.addEventListener('click', function () { openModal('search'); });
    if (navAskAI)       navAskAI.addEventListener('click',      function () { openModal('ask-ai'); });
    if (mobileSearch)   mobileSearch.addEventListener('click',  function () { openModal('search'); });
    if (fab)            fab.addEventListener('click',           function () { openModal('ask-ai'); });

    // ── Tab bar ─────────────────────────────────────────────────────────────
    dialog.querySelectorAll('button[data-tab]').forEach(function (btn) {
      btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
    });

    // ── Search input → shortcut query mirror ────────────────────────────────
    if (searchInput && shortcutQuery) {
      shortcutQuery.setAttribute('data-placeholder', 'type a question above…');
      searchInput.addEventListener('input', function () {
        shortcutQuery.textContent = searchInput.value.trim();
      });
    }

    // Clicking the Ask AI shortcut row jumps to ask-ai with the current query
    if (askAIShortcut) {
      askAIShortcut.addEventListener('click', function () {
        var q = (searchInput && searchInput.value.trim()) || '';
        switchTab('ask-ai');
        if (q && aiInput) {
          aiInput.value = q;
          submitAskAI(q);
        }
      });
      askAIShortcut.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          askAIShortcut.click();
        }
      });
    }

    // ── Example question pills ──────────────────────────────────────────────
    dialog.querySelectorAll('.mewbo-example-pill, .mewbo-example-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var q = btn.dataset.question;
        if (!q) return;
        if (aiInput) aiInput.value = q;
        submitAskAI(q);
      });
    });

    // ── AI input + send ─────────────────────────────────────────────────────
    if (aiInput) {
      aiInput.addEventListener('input', updateSendButton);
      aiInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          var q = aiInput.value.trim();
          if (q && !isLoading) submitAskAI(q);
        }
      });
    }

    if (aiSend) {
      aiSend.addEventListener('click', function () {
        if (aiInput) {
          var q = aiInput.value.trim();
          if (q && !isLoading) submitAskAI(q);
        }
      });
    }

    if (askAnother) {
      askAnother.addEventListener('click', resetAskAI);
    }

    // ── Search result card enhancer (MutationObserver) ───────────────────────
    if (!resultsEl) return;

    var filtersEl = document.getElementById('mewbo-search-filters');

    function titleCase(segment) {
      return segment.replace(/-/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    }

    function pathSegments(href) {
      var pathname = href;
      try {
        pathname = new URL(href, window.location.origin).pathname;
      } catch (_) {
        var hi = href.indexOf('#');
        if (hi !== -1) pathname = href.slice(0, hi);
      }
      return pathname.split('/').filter(function (s) { return s && !/\.(html?)$/.test(s); });
    }

    function enhanceArticle(article) {
      if (article.dataset.mewboEnhanced) return;
      article.dataset.mewboEnhanced = '1';

      var h3 = article.querySelector('h3');
      var anchor = h3 && h3.querySelector('a');
      if (!anchor) return;

      var href = anchor.getAttribute('href') || '';
      var segments = pathSegments(href);
      var breadcrumbText = segments.map(titleCase).join(' › ');

      // Tag with top-level scope. MkDocs flattens nested pages to hyphenated
      // single segments (e.g. `features-agents`), so we bucket by the prefix
      // before the first hyphen.
      var firstSeg = (segments[0] || 'docs').toLowerCase();
      var scope = firstSeg.split('-')[0] || firstSeg;
      article.dataset.mewboScope = scope;

      var breadcrumb = document.createElement('span');
      breadcrumb.className = 'mewbo-result__breadcrumb';
      breadcrumb.textContent = breadcrumbText || (anchor.textContent || '').trim();
      article.insertBefore(breadcrumb, h3);

      h3.insertBefore(fileTextIcon(), anchor);

      var p = article.querySelector('p');
      if (p) {
        var hint = document.createElement('span');
        hint.className = 'mewbo-result__enter-hint';
        hint.setAttribute('aria-hidden', 'true');
        hint.textContent = '↵';
        p.after(hint);
      }

      article.setAttribute('tabindex', '0');
      article.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          var link = article.querySelector('a');
          if (link) link.click();
        }
      });
    }

    // ── Scope filter chips (rebuilt whenever results change) ────────────────
    var activeScope = 'all';

    function applyScopeFilter() {
      resultsEl.querySelectorAll('article').forEach(function (a) {
        var s = a.dataset.mewboScope || 'docs';
        a.style.display = (activeScope === 'all' || s === activeScope) ? '' : 'none';
      });
    }

    function rebuildFilterChips() {
      if (!filtersEl) return;
      var articles = resultsEl.querySelectorAll('article');
      if (!articles.length) {
        filtersEl.innerHTML = '';
        activeScope = 'all';
        return;
      }
      var counts = {};
      articles.forEach(function (a) {
        var s = a.dataset.mewboScope || 'docs';
        counts[s] = (counts[s] || 0) + 1;
      });
      var scopes = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
      // Drop chips when there's only one bucket — no useful filtering
      if (scopes.length < 2) {
        filtersEl.innerHTML = '';
        activeScope = 'all';
        return;
      }
      // Cap to top 5 scopes — beyond that, chips become noise
      scopes = scopes.slice(0, 5);
      var total = articles.length;
      filtersEl.innerHTML = '';
      function makeChip(key, label, count) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'mewbo-chip';
        b.dataset.scope = key;
        if (key === activeScope) b.setAttribute('data-active', '');
        b.innerHTML = '<span>' + escapeHTML(label) + '</span><span class="mewbo-chip__count">' + count + '</span>';
        b.addEventListener('click', function () {
          activeScope = key;
          filtersEl.querySelectorAll('.mewbo-chip').forEach(function (c) { c.removeAttribute('data-active'); });
          b.setAttribute('data-active', '');
          applyScopeFilter();
        });
        filtersEl.appendChild(b);
      }
      makeChip('all', 'All', total);
      scopes.forEach(function (s) { makeChip(s, titleCase(s), counts[s]); });
      applyScopeFilter();
    }

    var observer = new MutationObserver(function (mutations) {
      var added = false;
      mutations.forEach(function (m) {
        m.addedNodes.forEach(function (node) {
          if (node.nodeType === Node.ELEMENT_NODE) {
            if (node.tagName === 'ARTICLE') {
              enhanceArticle(node);
              added = true;
            } else if (node.querySelectorAll) {
              node.querySelectorAll('article').forEach(function (a) { enhanceArticle(a); added = true; });
            }
          }
        });
      });
      if (added) rebuildFilterChips();
      else if (filtersEl && !resultsEl.querySelector('article')) {
        filtersEl.innerHTML = '';
        activeScope = 'all';
      }
    });

    dialog.addEventListener('close', function () { observer.disconnect(); });
    dialog.addEventListener('toggle', function (e) {
      if (dialog.open) {
        observer.observe(resultsEl, { childList: true, subtree: true });
        resultsEl.querySelectorAll('article').forEach(enhanceArticle);
        rebuildFilterChips();
      } else {
        observer.disconnect();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
