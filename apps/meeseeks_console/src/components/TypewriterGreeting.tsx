import { useState, useEffect, useRef, useCallback } from 'react';

const PHRASES = [
  'check on the servers.',
  'automate that workflow.',
  'build Rome in a day.',
  'set up a new service.',
  'make it more reliable.',
  'make interns obsolete.',
  'dig into those logs.',
  'break something on purpose.',
];

const JOKE_SET = new Set([
  'build Rome in a day.',
  'make interns obsolete.',
  'break something on purpose.',
]);

const TYPING_MS = 70;
const BACKSPACE_MS = 45;
const HOLD_MS = 2500;
const INITIAL_HOLD_MS = 3500;
const DELETED_PAUSE_MS = 250;
const BLINK_MS = 530;

type Phase = 'initial-hold' | 'holding' | 'deleting' | 'pause-deleted' | 'typing';

/** Fisher-Yates shuffle with two post-conditions:
 *  1. first element !== lastPhrase (no repeat across cycles)
 *  2. no two jokes appear back-to-back */
function shuffleConstrained(phrases: string[], lastPhrase: string): string[] {
  const result = [...phrases];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  if (result[0] === lastPhrase) {
    const idx = result.findIndex((p, i) => i > 0 && p !== lastPhrase);
    if (idx > 0) [result[0], result[idx]] = [result[idx], result[0]];
  }
  for (let i = 1; i < result.length; i++) {
    if (JOKE_SET.has(result[i]) && JOKE_SET.has(result[i - 1])) {
      for (let j = i + 1; j < result.length; j++) {
        if (!JOKE_SET.has(result[j])) {
          [result[i], result[j]] = [result[j], result[i]];
          break;
        }
      }
    }
  }
  return result;
}

export function TypewriterGreeting() {
  const [text, setText] = useState(PHRASES[0]);
  const [cursorOn, setCursorOn] = useState(true);

  const phase = useRef<Phase>('initial-hold');
  const queue = useRef([...PHRASES]);
  const queueIdx = useRef(0);
  const fullText = useRef(PHRASES[0]);
  const charIdx = useRef(PHRASES[0].length);

  const nextPhrase = useCallback((): string => {
    queueIdx.current += 1;
    if (queueIdx.current >= queue.current.length) {
      const last = queue.current[queue.current.length - 1];
      queue.current = shuffleConstrained(PHRASES, last);
      queueIdx.current = 0;
    }
    return queue.current[queueIdx.current];
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let blinker: ReturnType<typeof setInterval> | null = null;

    function cleanup() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (blinker) { clearInterval(blinker); blinker = null; }
    }

    function startBlink() {
      if (blinker) clearInterval(blinker);
      setCursorOn(true);
      blinker = setInterval(() => setCursorOn((v) => !v), BLINK_MS);
    }

    function stopBlink() {
      if (blinker) { clearInterval(blinker); blinker = null; }
      setCursorOn(true);
    }

    function step() {
      switch (phase.current) {
        case 'initial-hold':
          startBlink();
          timer = setTimeout(() => {
            phase.current = 'deleting';
            stopBlink();
            step();
          }, INITIAL_HOLD_MS);
          break;

        case 'holding':
          startBlink();
          timer = setTimeout(() => {
            phase.current = 'deleting';
            stopBlink();
            step();
          }, HOLD_MS);
          break;

        case 'deleting':
          if (charIdx.current > 0) {
            timer = setTimeout(() => {
              charIdx.current -= 1;
              setText(fullText.current.slice(0, charIdx.current));
              step();
            }, BACKSPACE_MS);
          } else {
            fullText.current = nextPhrase();
            phase.current = 'pause-deleted';
            step();
          }
          break;

        case 'pause-deleted':
          startBlink();
          timer = setTimeout(() => {
            phase.current = 'typing';
            stopBlink();
            step();
          }, DELETED_PAUSE_MS);
          break;

        case 'typing':
          if (charIdx.current < fullText.current.length) {
            timer = setTimeout(() => {
              charIdx.current += 1;
              setText(fullText.current.slice(0, charIdx.current));
              step();
            }, TYPING_MS);
          } else {
            phase.current = 'holding';
            step();
          }
          break;
      }
    }

    stopBlink();
    step();
    return cleanup;
  }, [nextPhrase]);

  return (
    <p className="text-base sm:text-lg font-normal text-[hsl(var(--muted-foreground))] mb-10 whitespace-nowrap">
      {"Let's "}<span>{text}</span><span style={{ opacity: cursorOn ? 0.7 : 0 }}>|</span>
    </p>
  );
}
