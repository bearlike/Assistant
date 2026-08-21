package com.mewbo.aura.ui.orb

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Pins [AuraShaders.supported]'s threshold directly, rather than rendering a shader composable
 * under Robolectric.
 *
 * **Why not render [Orb]/[AuraSpark] at `sdk = [30]` instead — tried, withdrawn.** Such a test was
 * written and never produced a result: it HUNG, and a hang here is silent (no failure, no output,
 * a worker pinned for minutes — the frame-loop trap in this tree's `test/CLAUDE.md`). A test that
 * hangs is worse than no test, because it reads forever after as a slow suite.
 *
 * Its power was also never established, in either direction. The related `VibratorManager` case was
 * mutation-proven to have NO power — reverting the fix left it green — but by a mechanism that does
 * not obviously carry over: `null as? VibratorManager` never forces the class to resolve, whereas
 * `RuntimeShader(SRC)` is a direct constructor call that would. **Whether an sdk-30 pin actually
 * hides `android.graphics.RuntimeShader` is UNVERIFIED** — the experiment that would settle it is
 * the one that hung. Treat it as an open question, not as the settled fact an earlier draft of this
 * comment claimed.
 *
 * So the honest coverage position: the `NoClassDefFoundError` failure mode has no JVM witness here.
 * Lint's `NewApi` at `minSdk 30` is the static witness (it found all 63 of these), and a real API-30
 * device is the runtime one (out of reach — see the device matrix in the app-root `CLAUDE.md`).
 *
 * What DOES have power on the JVM: the gate's own threshold. `Build.VERSION.SDK_INT` IS faithfully
 * shadowed by `@Config(sdk = ...)`, so this pins `supported` to `false` one level below
 * `TIRAMISU` and `true` at it — the exact boundary a future edit (e.g. `>` instead of `>=`) could
 * silently get wrong with no other test noticing, since every call site trusts this one property.
 */
@RunWith(RobolectricTestRunner::class)
class AuraShadersTest {

    @Config(sdk = [30])
    @Test
    fun `unsupported below API 33`() {
        assertFalse(AuraShaders.supported)
    }

    @Config(sdk = [33])
    @Test
    fun `supported at API 33`() {
        assertTrue(AuraShaders.supported)
    }
}
