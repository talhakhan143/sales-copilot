/**
 * Small, dependency free helpers for the raw PCM the AudioWorklet hands to the main thread.
 * Kept pure so they can be reasoned about (and unit tested) without a browser.
 */

function clamp01(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  if (value <= 0) {
    return 0;
  }
  if (value >= 1) {
    return 1;
  }
  return value;
}

/**
 * Root mean square level of an Int16 little endian frame, normalised to 0..1.
 *
 * The buffer arrives straight from the worklet, so it is treated defensively: a zero length
 * buffer returns 0, and an odd byteLength is truncated to whole samples. That truncation
 * matters because `new Int16Array(buffer)` throws a RangeError when the byte length is not a
 * multiple of 2, while the three argument form used here simply ignores the stray byte.
 */
export function rmsFromInt16(buffer: ArrayBuffer): number {
  if (!buffer || buffer.byteLength < 2) {
    return 0;
  }

  const count = Math.floor(buffer.byteLength / 2);
  if (count === 0) {
    return 0;
  }

  const samples = new Int16Array(buffer, 0, count);
  let sum = 0;
  for (let i = 0; i < count; i += 1) {
    const sample = samples[i];
    sum += sample * sample;
  }

  // 32768 is the magnitude of the most negative Int16, so the result never exceeds 1.
  return Math.sqrt(sum / count) / 32768;
}

/**
 * Asymmetric envelope follower for the level meters: fast attack, slow release.
 *
 * A symmetric filter either lags behind the first syllable of a word (too slow) or strobes
 * on every consonant (too fast). Rising fast and falling slowly is how hardware VU meters
 * behave, and it is what makes the bars feel attached to the voice instead of flickering.
 */
export function smoothLevel(
  prev: number,
  next: number,
  attack = 0.5,
  release = 0.12,
): number {
  const previous = clamp01(prev);
  const target = clamp01(next);
  const coefficient = clamp01(target > previous ? attack : release);
  return clamp01(previous + (target - previous) * coefficient);
}
