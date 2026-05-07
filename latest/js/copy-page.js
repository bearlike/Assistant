(() => {
  // Theme injects these from main.html: MEWBO_REPO is parsed from
  // `config.repo_url` (mkdocs standard, products already set it), and
  // MEWBO_DOCS_PATH defaults to 'docs' (mkdocs's `docs_dir` basename).
  // MEWBO_VERSIONS_ROOT is shared with version-switcher.js for the
  // GitHub-Pages-subpath case (e.g. /Assistant/<version>/...).
  const REPO          = window.MEWBO_REPO || '';
  const DOCS_PATH     = (window.MEWBO_DOCS_PATH || 'docs').replace(/^\/+|\/+$/g, '');
  const VERSIONS_ROOT = (window.MEWBO_VERSIONS_ROOT || '').replace(/^\/+|\/+$/g, '');

  function getPathInfo() {
    const parts = window.location.pathname.split('/').filter(Boolean);
    let versionIndex = 0;
    if (VERSIONS_ROOT && parts[0] === VERSIONS_ROOT) {
      versionIndex = 1;
    }
    const version = parts[versionIndex] || 'latest';
    const pageParts = parts.slice(versionIndex + 1);
    return { version, pageParts };
  }

  function buildMarkdownUrl() {
    if (!REPO) return null;
    const { version, pageParts } = getPathInfo();
    const ref = version === 'latest' || version === 'main' ? 'main' : version;
    const pagePath = pageParts.length ? `${pageParts.join('/')}.md` : 'index.md';
    return `https://raw.githubusercontent.com/${REPO}/${ref}/${DOCS_PATH}/${pagePath}`;
  }

  async function writeClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'absolute';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
  }

  function findCopyButton() {
    const candidates = Array.from(document.querySelectorAll('#page-header button'));
    return candidates.find((button) => button.textContent && button.textContent.includes('Copy Page')) || null;
  }

  function bindCopyHandler(button) {
    button.onclick = async (event) => {
      event.preventDefault();
      event.stopPropagation();
      try {
        const url = buildMarkdownUrl();
        if (!url) {
          console.warn('Copy page disabled: set `repo_url` in mkdocs.yml so the theme can derive the GitHub raw URL.');
          return;
        }
        const response = await fetch(url, { cache: 'no-store' });
        if (!response.ok) {
          console.warn('Copy page markdown failed:', response.status, url);
          return;
        }
        const text = await response.text();
        await writeClipboard(text);
      } catch (error) {
        console.warn('Copy page markdown failed:', error);
      }
    };
  }

  function initCopyButton() {
    const button = findCopyButton();
    if (!button) {
      return;
    }
    bindCopyHandler(button);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCopyButton);
  } else {
    initCopyButton();
  }
})();
