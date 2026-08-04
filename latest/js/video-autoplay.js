/*
 * Play a video while it is on screen, pause it when it scrolls away.
 *
 * Docs pages use short silent clips as illustrations. Left to `autoplay`,
 * every clip on a page starts at load, all of them decode at once, and the
 * one the reader is actually looking at competes for bandwidth with several
 * they cannot see. An IntersectionObserver plays only what is visible.
 *
 * Three constraints, all of them the browser's rather than ours:
 *
 *   - Autoplay is only permitted for a MUTED video, so any clip that opts in
 *     is muted here. A clip with meaningful audio should not opt in.
 *   - iOS refuses to inline-play without `playsinline`, and goes fullscreen
 *     instead, which is worse than not playing.
 *   - `play()` rejects rather than throws, and a rejection is normal (a
 *     background tab, a power-saving mode, a user gesture requirement). It is
 *     swallowed: a clip that will not start is a still frame, not an error.
 *
 * Honours `prefers-reduced-motion`: motion a reader has asked not to see is
 * exactly what this would otherwise start automatically.
 */

(function () {
  const SELECTOR = "article video, .md-content video";

  const boot = () => {
    const videos = Array.from(document.querySelectorAll(SELECTOR)).filter(
      // `data-no-autoplay` opts a single clip out without touching the theme.
      (v) => !v.hasAttribute("data-no-autoplay")
    );
    if (!videos.length) return;

    const reduced =
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    videos.forEach((v) => {
      // Muted and inline are preconditions for playing at all, so they are
      // set here rather than asked of every page author.
      v.muted = true;
      v.setAttribute("muted", "");
      v.setAttribute("playsinline", "");
      // The clip is driven from the observer, so the attribute would only
      // race it — and would start every clip on the page at load.
      v.removeAttribute("autoplay");
      // These clips are short illustrations, so they loop — a one-shot clip
      // that a reader scrolls to a moment late has already finished and
      // reads as a broken still.
      v.loop = true;
      v.setAttribute("loop", "");
      if (reduced) v.setAttribute("controls", "");
    });

    if (reduced || !("IntersectionObserver" in window)) return;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          const v = entry.target;
          if (entry.isIntersecting) {
            const p = v.play();
            if (p && typeof p.catch === "function") p.catch(() => {});
          } else if (!v.paused) {
            v.pause();
          }
        });
      },
      // A little of the clip on screen is enough to be worth starting, and
      // the margin means it is already running by the time it is properly
      // in view rather than visibly starting from a cold frame.
      { threshold: 0.25, rootMargin: "0px 0px -10% 0px" }
    );

    videos.forEach((v) => observer.observe(v));
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
