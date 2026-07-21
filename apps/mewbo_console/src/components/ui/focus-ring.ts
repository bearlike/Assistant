/**
 * Focus ring for every interactive surface in the console, rail or otherwise.
 *
 * The colour is the UNDILUTED `--ring`. An earlier `/50` halved it to roughly
 * 1.7:1, well under the 3:1 WCAG 2.4.11 wants of a focus indicator — the one
 * affordance a keyboard user cannot work around. It survived every automated
 * check because the console's contrast guard is scoped to `text-` position, so
 * `ring-` is exempt by design and nothing will catch a reintroduction. **If it
 * ever reads too heavy, spend ring WIDTH or offset**; thinning the colour is
 * precisely what destroys the contrast.
 */
export const FOCUS_RING =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))]";
