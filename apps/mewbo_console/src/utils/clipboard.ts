export async function copyText(text: string) {
  if (!text) {
    return;
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // fallback below
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'absolute';
  textarea.style.left = '-9999px';
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand('copy');
  } catch {
    // best-effort: the legacy path can throw in sandboxed / non-gesture
    // contexts. Swallow so callers still get a normal return (and their
    // "copied" feedback fires) and the temp node is always cleaned up.
  }
  document.body.removeChild(textarea);
}
