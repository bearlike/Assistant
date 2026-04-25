import { cn } from '../utils/cn';

// Petal path centered at (100,100), pointing up. Five copies are rotated
// around the center by the outer <g> wrappers to form the Meeseeks bloom.
const PETAL_D =
  'M 100 100 C 76 100, 62 86, 62 62 C 62 42, 80 26, 100 26 C 120 26, 138 42, 138 62 C 138 86, 124 100, 100 100 Z';

const ROTATIONS = [0, 72, 144, 216, 288];

interface FiveLeafBloomLoaderProps {
  size?: number;
  label?: string;
  className?: string;
}

export function FiveLeafBloomLoader({
  size = 40,
  label = 'Agent is working',
  className,
}: FiveLeafBloomLoaderProps) {
  return (
    <svg
      viewBox="0 0 200 200"
      width={size}
      height={size}
      role="img"
      aria-label={label}
      className={cn('block', className)}
    >
      <g className="mss-bloom-spin">
        {ROTATIONS.map((deg, i) => (
          <g key={deg} transform={`rotate(${deg} 100 100)`}>
            <path className={`mss-bloom-leaf mss-bloom-leaf-${i + 1}`} d={PETAL_D} />
          </g>
        ))}
      </g>
      <circle cx="100" cy="100" r="10" fill="hsl(var(--surface))" />
    </svg>
  );
}
