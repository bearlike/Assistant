// Full-screen image viewer for content images. GLightbox does the viewing; this
// file decides WHAT is viewable, builds the gallery, and puts a visible
// affordance on the page. The library is loaded from the CDN ahead of this file
// (emitted when theme `lightbox: true`). If it never loads, images stay
// ordinary images and no affordance is shown, which is the same failure mode
// the carousel has.
(() => {
  "use strict";

  /**
   * One content image, and everything the viewer needs to know about it.
   *
   * State plus the rules over that state. Eligibility, source and caption are
   * properties of the image, not of the binder, so they live here rather than
   * as free functions the binder happens to call.
   */
  class ImageTarget {
    static OPT_OUT = ".no-lightbox, [data-lightbox='false']";
    // An icon or a badge is not a picture worth a full screen.
    static MIN_EDGE = 96;

    constructor(el) {
      this.el = el;
    }

    static from(node) {
      const el = node && node.closest ? node.closest("img") : null;
      return el ? new ImageTarget(el) : null;
    }

    get src() {
      return this.el.currentSrc || this.el.src || "";
    }

    /** The figcaption if there is one, else the alt text. */
    get caption() {
      const fig = this.el.closest("figure");
      const cap = fig && fig.querySelector("figcaption");
      return (cap && cap.textContent.trim()) || this.el.getAttribute("alt") || "";
    }

    get rect() {
      return this.el.getBoundingClientRect();
    }

    get isEligible() {
      if (this.el.closest(ImageTarget.OPT_OUT)) return false;
      // An image that is already a link belongs to that link.
      if (this.el.closest("a")) return false;
      if (!this.src) return false;
      const r = this.rect;
      return r.width >= ImageTarget.MIN_EDGE && r.height >= ImageTarget.MIN_EDGE;
    }

    /** The slide strip this image belongs to, or null when it stands alone. */
    get carousel() {
      return this.el.closest(".swiper");
    }

    toSlide() {
      return { href: this.src, type: "image", description: this.caption };
    }

    /**
     * Every distinct picture in the same carousel, in slide order, so the
     * viewer's arrows walk the strip instead of dead-ending on one slide.
     *
     * Deduplicated BY SOURCE because Swiper runs with `loop: true` and clones
     * slides. Wrapping each image in an anchor, which is what the library's
     * own docs suggest, would list the same picture two or three times.
     */
    gallery() {
      const strip = this.carousel;
      if (!strip) return [this];
      const seen = new Set();
      const out = [];
      strip.querySelectorAll(".swiper-slide img").forEach((node) => {
        const target = new ImageTarget(node);
        if (!target.src || seen.has(target.src)) return;
        seen.add(target.src);
        out.push(target);
      });
      return out.length ? out : [this];
    }
  }

  /**
   * The hover affordance. A single floating button reused across every image,
   * rather than one injected per image.
   *
   * Injecting a wrapper per image would mutate slide markup Swiper owns, and a
   * cursor change alone is not an affordance: it says nothing until you are
   * already hovering and it says nothing about what will happen.
   */
  class ZoomHint {
    static MARGIN = 10;

    constructor() {
      /** Set by the bootstrap once the viewers exist. */
      this.onActivate = () => {};
      this.target = null;
      this.el = this.build();
      this.track = this.track.bind(this);
    }

    build() {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ms-zoom-hint";
      btn.setAttribute("aria-label", "View image full screen");
      btn.hidden = true;
      btn.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
        'aria-hidden="true"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/>' +
        '<path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>';
      btn.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (this.target) this.onActivate(this.target);
      });
      document.body.appendChild(btn);
      return btn;
    }

    show(target) {
      this.target = target;
      this.el.hidden = false;
      this.place();
      window.addEventListener("scroll", this.track, { passive: true });
      window.addEventListener("resize", this.track, { passive: true });
    }

    place() {
      if (!this.target) return;
      const r = this.target.rect;
      this.el.style.top = `${r.top + ZoomHint.MARGIN}px`;
      this.el.style.left = `${r.right - this.el.offsetWidth - ZoomHint.MARGIN}px`;
    }

    track() {
      if (!this.target) return this.hide();
      const r = this.target.rect;
      // Scrolled out of view entirely, so the button has nothing to sit on.
      if (r.bottom < 0 || r.top > window.innerHeight) return this.hide();
      this.place();
    }

    hide() {
      this.el.hidden = true;
      this.target = null;
      window.removeEventListener("scroll", this.track);
      window.removeEventListener("resize", this.track);
    }

    owns(node) {
      return this.el.contains(node);
    }
  }

  /**
   * Binds one content root. Collaborators are injected rather than constructed
   * here, so the viewer can be exercised without the library present.
   */
  class ImageViewer {
    static CHROME = ".swiper-button-prev, .swiper-button-next, .swiper-pagination";

    constructor(root, { factory, hint }) {
      this.root = root;
      this.factory = factory;
      this.hint = hint;
    }

    bind() {
      this.root.addEventListener("click", (event) => this.onClick(event));
      this.root.addEventListener("mouseover", (event) => this.onHover(event));
      this.root.addEventListener("mouseout", (event) => this.onLeave(event));
      this.mark();
      // Swiper mounts after this file runs and images decode later still, so
      // the affordance is re-applied once things settle.
      window.setTimeout(() => this.mark(), 600);
      window.addEventListener("load", () => this.mark(), { once: true });
    }

    /** Only images the click handler will actually open get the cursor. */
    mark() {
      this.root.querySelectorAll("img").forEach((node) => {
        const target = new ImageTarget(node);
        node.classList.toggle("ms-zoomable", target.isEligible);
      });
    }

    onClick(event) {
      if (event.target.closest(ImageViewer.CHROME)) return;
      const target = ImageTarget.from(event.target);
      if (!target || !target.isEligible) return;
      event.preventDefault();
      this.open(target);
    }

    onHover(event) {
      const target = ImageTarget.from(event.target);
      if (!target || !target.isEligible) return;
      this.hint.show(target);
    }

    onLeave(event) {
      const next = event.relatedTarget;
      if (next && (this.hint.owns(next) || ImageTarget.from(next))) return;
      this.hint.hide();
    }

    open(target) {
      this.hint.hide();
      const slides = target.gallery();
      const index = Math.max(0, slides.findIndex((s) => s.src === target.src));
      const many = slides.length > 1;
      const lb = this.factory({
        // No zoom. The ask is to see the picture at full size, nothing more.
        zoomable: false,
        draggable: many,
        loop: many,
        touchNavigation: many,
        openEffect: "fade",
        closeEffect: "fade",
        elements: slides.map((s) => s.toSlide()),
        startAt: index,
      });
      lb.open();
      // Each open builds its own instance, so drop it once it closes rather
      // than leaving a stack of detached modals behind.
      lb.on("close", () => window.setTimeout(() => lb.destroy(), 0));
    }
  }

  const start = (attempt = 0) => {
    if (typeof window.GLightbox !== "function") {
      if (attempt > 40) return; // ~4s, then give up quietly
      window.setTimeout(() => start(attempt + 1), 100);
      return;
    }
    const roots = document.querySelectorAll("article, .md-content");
    if (!roots.length) return;
    const hint = new ZoomHint();
    const viewers = [...roots].map(
      (root) => new ImageViewer(root, { factory: window.GLightbox, hint })
    );
    // Wired after construction rather than passed in, because the hint is
    // shared by every root and must not hold a reference to one of them.
    hint.onActivate = (target) => viewers[0].open(target);
    viewers.forEach((viewer) => viewer.bind());
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => start(), { once: true });
  } else {
    start();
  }
})();
