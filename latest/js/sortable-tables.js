/*
 * Click a table header to sort by that column.
 *
 * Thin glue over Tablesort (MIT, loaded from CDN alongside this file when
 * `theme.sortable_tables` is on). Tablesort owns the comparison and the DOM
 * reordering; this only decides WHICH tables get it and marks them so CSS can
 * show the affordance.
 *
 * Prose tables only. A pygments line-number gutter is a <table> too, and
 * sorting one would scramble a code block — hence the class exclusion rather
 * than a bare `article table`.
 */

(function () {
  const boot = () => {
    if (typeof window.Tablesort !== "function") return;
    const tables = document.querySelectorAll(
      "article .table-wrapper > table:not(.codehilitetable)"
    );
    tables.forEach((table) => {
      // A single-row table has nothing to sort, and a table with no header
      // has nothing to sort BY.
      if (!table.tHead || table.tBodies.length === 0) return;
      if (table.tBodies[0].rows.length < 2) return;
      // `data-no-sort` opts one table out.
      if (table.hasAttribute("data-no-sort")) return;
      new window.Tablesort(table);
      table.classList.add("is-sortable");
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
