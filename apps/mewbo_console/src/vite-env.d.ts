/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />
/// <reference types="vite-plugin-pwa/react" />

// cytoscape-fcose has no @types/* — register the bare module so the
// canonical ``cytoscape.use(fcose)`` call typechecks. The runtime export
// is a registrar function compatible with cytoscape's extension API.
declare module "cytoscape-fcose" {
  import type cytoscape from "cytoscape";
  const ext: cytoscape.Ext;
  export default ext;
}
