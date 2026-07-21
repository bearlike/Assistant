import type { ReactNode } from "react";

import { BrandMark } from "./BrandMark";

/**
 * The landing hero shared by every product surface — Agentic Tasks, Agentic
 * Wiki, Agentic Search, Agentic Apps.
 *
 * It owns the brand mark, the title and the subtitle: their size, their colour,
 * their spacing, and the hero's vertical offset. That ownership is the whole
 * point. Each landing used to hand-roll this block, and they had drifted into
 * separate products — several mark sizes, several title sizes, and several
 * different distances from the top of the viewport. Anything page-specific
 * (composer, form, gallery) is `children` and hangs below the subtitle.
 *
 * So: no `className` escape hatch on purpose. A per-page override is exactly
 * the seam the drift came through.
 *
 * The block is deliberately SHORT. A hero is an orientation cue, not the
 * screen: every pixel it spends pushes the landing's actual content (the
 * recents list on Tasks, the gallery on Apps) below the fold, and a landing
 * whose content you have to scroll to reach reads as an empty product. The
 * measured contract is that the first content row clears the fold at
 * 1512x900 — treat the paddings and the `text-2xl` title as load-bearing,
 * not as taste.
 */
interface ProductHeroProps {
  /** Title case, and it names the product: "Agentic Tasks". */
  title: string;
  /**
   * A node, not a string, so a page can animate its subtitle (Tasks types
   * through a rotating phrase) without owning the typography around it.
   */
  subtitle: ReactNode;
  children?: ReactNode;
}

export function ProductHero({ title, subtitle, children }: ProductHeroProps) {
  return (
    <section className="mx-auto flex w-full max-w-[720px] flex-col items-center px-4 pt-[clamp(24px,5vh,56px)] pb-[clamp(20px,3vh,32px)] text-center sm:px-6">
      <BrandMark
        size={48}
        className="mb-3 text-[hsl(var(--primary))] drop-shadow-[0_0_40px_hsl(var(--primary)/0.30)]"
      />
      <h1 className="mb-2 text-2xl font-semibold tracking-tight [text-wrap:balance]">
        {title}
      </h1>
      <p className="mb-4 max-w-[480px] text-sm leading-[1.5] text-[hsl(var(--muted-foreground))] [text-wrap:balance]">
        {subtitle}
      </p>
      {children}
    </section>
  );
}
