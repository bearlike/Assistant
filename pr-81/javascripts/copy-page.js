(() => {
  function getPathInfo() {
    const parts = window.location.pathname.split('/').filter(Boolean);
    let root = '';
    let versionIndex = 0;
    if (parts[0] === 'Assistant') {
      root = '/Assistant';
      versionIndex = 1;
    }
    const version = parts[versionIndex] || 'latest';
    const pageParts = parts.slice(versionIndex + 1);
    return { root, version, pageParts };
  }

  function buildMarkdownUrl() {
    const { version, pageParts } = getPathInfo();
    const ref = version === 'latest' || version === 'main' ? 'main' : version;
    const pagePath = pageParts.length ? `${pageParts.join('/')}.md` : 'index.md';
    return `https://raw.githubusercontent.com/bearlike/Assistant/${ref}/docs/${pagePath}`;
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
