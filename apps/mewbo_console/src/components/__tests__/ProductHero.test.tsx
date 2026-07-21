import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import { ProductHero } from '../ProductHero';
import { TypewriterGreeting } from '../TypewriterGreeting';

// vitest runs without `globals: true`, so RTL auto-cleanup never fires.
afterEach(cleanup);

describe('ProductHero — the one landing hero (Tasks / Wiki / Search)', () => {
  it('renders the brand mark, the title as the h1, and the subtitle in its own <p>', () => {
    const { container } = render(
      <ProductHero title="Agentic Tasks" subtitle="Ask for anything." />
    );

    const h1 = screen.getByRole('heading', { level: 1 });
    expect(h1).toHaveTextContent('Agentic Tasks');

    // The mark is the inline currentColor SVG, not an <img> of the asset — that
    // is what lets it take the primary tint and the glow.
    expect(container.querySelector('svg')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();

    const p = container.querySelector('p');
    expect(p).toHaveTextContent('Ask for anything.');
  });

  it('owns the subtitle typography — a node subtitle brings text, never its own <p>', () => {
    // Tasks passes the animated greeting as its subtitle. If TypewriterGreeting
    // ever re-grows its own <p>/classes, the Tasks subtitle silently stops
    // matching Wiki and Search — so pin it to exactly one <p>, the hero's.
    const { container } = render(
      <ProductHero title="Agentic Tasks" subtitle={<TypewriterGreeting />} />
    );

    expect(container.querySelectorAll('p')).toHaveLength(1);
    expect(container.querySelector('p')).toHaveTextContent("Let's");
  });
});
