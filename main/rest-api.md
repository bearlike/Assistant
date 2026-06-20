---
title: REST API
template: app.html
search:
  exclude: true
---

<div id="scalar-api-mount"></div>

<script src="../assets/scalar.standalone.min.js"></script>
<script>
  (function () {
    // Mount id is deliberately NOT "api-reference": the Scalar standalone
    // bundle auto-initializes a second, spec-less reference on any element
    // with that magic id, which renders an empty loading skeleton on top of
    // ours. A neutral id leaves the bundle's auto-init with nothing to grab,
    // so this programmatic call is the only mount.
    var isDark = function () {
      return document.documentElement.classList.contains("dark");
    };
    var dark = isDark();
    var create = function () {
      return Scalar.createApiReference("#scalar-api-mount", {
        url: "../openapi.json",
        darkMode: dark,
        hideDarkModeToggle: true,
        hideModels: false,
        // Developers can retarget the reference at their own stack and run
        // live requests from this page: the server URL is an editable
        // {baseUrl} variable (default the self-hosted port), and the API-key
        // panel is surfaced from the spec's `apikey` (X-API-KEY) scheme so a
        // key can be entered once and reused across every "Test Request".
        servers: [
          {
            url: "{baseUrl}",
            description: "Your Mewbo server (edit the base URL below)",
            variables: {
              baseUrl: { default: "http://localhost:5125" }
            }
          }
        ],
        authentication: {
          preferredSecurityScheme: "apikey"
        }
      });
    };
    var app = create();
    // The site header owns the light/dark toggle; follow real flips only.
    // The theme also mutates the html class for unrelated reasons right after
    // load (e.g. layout-fixed), so guard on an actual dark-state change.
    new MutationObserver(function () {
      if (isDark() === dark) return;
      dark = isDark();
      document.getElementById("scalar-api-mount").innerHTML = "";
      if (app && typeof app.destroy === "function") app.destroy();
      app = create();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  })();
</script>
