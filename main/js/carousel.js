// Image carousel for `.ms-shots` blocks. Swiper is loaded from the CDN ahead of
// this file (emitted by main.html when theme `carousel: true`); we poll briefly
// for it, then mount every carousel on the page. Each instance scopes its own
// pagination/navigation, respects reduced-motion, and guards double-mount. If
// Swiper never loads (CDN blocked) the carousel is inert — the first slide stays.
(() => {
  "use strict";

  class Carousel {
    constructor(root) {
      this.root = root;
      this.swiper = null;
    }

    get prefersReducedMotion() {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }

    options() {
      return {
        loop: true,
        slidesPerView: 1,
        spaceBetween: 24,
        grabCursor: true,
        keyboard: { enabled: true },
        a11y: { enabled: true },
        autoplay: this.prefersReducedMotion
          ? false
          : { delay: 4000, pauseOnMouseEnter: true, disableOnInteraction: false },
        pagination: {
          el: this.root.querySelector(".swiper-pagination"),
          clickable: true,
        },
        navigation: {
          nextEl: this.root.querySelector(".swiper-button-next"),
          prevEl: this.root.querySelector(".swiper-button-prev"),
        },
      };
    }

    mount() {
      if (this.root.dataset.mounted) {
        return null;
      }
      this.root.dataset.mounted = "1";
      this.swiper = new window.Swiper(this.root, this.options());
      return this.swiper;
    }

    static mountAll() {
      document
        .querySelectorAll(".ms-shots")
        .forEach((root) => new Carousel(root).mount());
    }

    // Swiper may still be fetching from the CDN when this runs; poll briefly.
    static whenReady(onReady, tries = 0) {
      if (typeof window.Swiper !== "undefined") {
        return onReady();
      }
      if (tries > 60) {
        return; // ~3s elapsed; give up and leave the first slide static
      }
      window.setTimeout(() => Carousel.whenReady(onReady, tries + 1), 50);
    }
  }

  const boot = () => Carousel.whenReady(() => Carousel.mountAll());

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
