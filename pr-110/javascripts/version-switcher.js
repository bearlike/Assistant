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

  function uniqByValue(items) {
    const seen = new Set();
    return items.filter((item) => {
      if (seen.has(item.value)) {
        return false;
      }
      seen.add(item.value);
      return true;
    });
  }

  function buildOptions(versions, currentVersion) {
    const items = [];
    for (const entry of versions) {
      if (entry.aliases && entry.aliases.length) {
        for (const alias of entry.aliases) {
          items.push({ value: alias, label: alias === 'latest' ? 'latest' : alias });
        }
      }
      items.push({ value: entry.version, label: entry.title || entry.version });
    }

    const unique = uniqByValue(items);
    unique.sort((a, b) => {
      if (a.value === 'latest') return -1;
      if (b.value === 'latest') return 1;
      if (a.value === 'main') return -1;
      if (b.value === 'main') return 1;
      if (a.value === currentVersion) return -1;
      if (b.value === currentVersion) return 1;
      return a.label.localeCompare(b.label);
    });
    return unique;
  }

  function createSwitcher(versions, current) {
    const container = document.createElement('div');
    container.id = 'version-switcher';
    container.className = 'hidden md:flex items-center gap-2';

    const label = document.createElement('span');
    label.className = 'text-muted-foreground text-xs';
    label.textContent = 'Version';

    const select = document.createElement('select');
    select.className =
      'h-8 rounded-md border border-border bg-background px-2 text-sm text-foreground shadow-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50';

    for (const item of versions) {
      const option = document.createElement('option');
      option.value = item.value;
      option.textContent = item.label;
      if (item.value === current) {
        option.selected = true;
      }
      select.appendChild(option);
    }

    select.addEventListener('change', () => {
      const { root, pageParts } = getPathInfo();
      const selected = select.value;
      const pageSuffix = pageParts.length ? `${pageParts.join('/')}/` : '';
      const rootPrefix = root || '';
      const target = `${rootPrefix}/${selected}/${pageSuffix}`.replace(/\/+/g, '/');
      window.location.assign(target);
    });

    container.appendChild(label);
    container.appendChild(select);
    return container;
  }

  async function injectSwitcher() {
    const headerActions = document.querySelector('header .ml-auto');
    if (!headerActions || document.getElementById('version-switcher')) {
      return;
    }

    const { root, version } = getPathInfo();
    const versionsUrl = `${root || ''}/versions.json`;
    try {
      const response = await fetch(versionsUrl, { cache: 'no-store' });
      if (!response.ok) {
        return;
      }
      const versions = await response.json();
      const options = buildOptions(versions, version);
      if (!options.length) {
        return;
      }
      const switcher = createSwitcher(options, version);
      headerActions.prepend(switcher);
    } catch (error) {
      console.warn('Version switcher disabled:', error);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectSwitcher);
  } else {
    injectSwitcher();
  }
})();
