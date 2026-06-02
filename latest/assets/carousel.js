// Hero product-tour carousel. Swiper is loaded via CDN ahead of this file
// (see mkdocs.yml extra_javascript). Initialised once, respects reduced-motion.
(function () {
  var tries = 0;

  function init() {
    var el = document.querySelector(".ms-shots");
    if (!el || el.dataset.mounted) return;
    el.dataset.mounted = "1";

    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    new window.Swiper(el, {
      loop: true,
      slidesPerView: 1,
      spaceBetween: 24,
      grabCursor: true,
      keyboard: { enabled: true },
      a11y: { enabled: true },
      autoplay: reduce
        ? false
        : { delay: 4000, pauseOnMouseEnter: true, disableOnInteraction: false },
      pagination: { el: ".ms-shots .swiper-pagination", clickable: true },
      navigation: {
        nextEl: ".ms-shots .swiper-button-next",
        prevEl: ".ms-shots .swiper-button-prev",
      },
    });
  }

  // Swiper may still be fetching from the CDN when this runs; poll briefly.
  function ready() {
    if (typeof window.Swiper === "undefined") {
      if (tries++ > 60) return; // ~3s; CDN blocked -> leave the first slide static
      return window.setTimeout(ready, 50);
    }
    init();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }
})();
