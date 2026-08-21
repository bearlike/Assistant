package com.mewbo.aura.data.settings

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * One `DataStore` per preferences file, across every source set.
 *
 * **This exists because two delegates over one file is a LAUNCH CRASH that no other gate here can
 * see.** `preferencesDataStore(name = …)` CONSTRUCTS a store; it does not join an existing one. A
 * second live instance over the same file throws
 * `IllegalStateException: There are multiple DataStores active for the same file` on first read —
 * and the app dies on the main thread.
 *
 * Every ordinary signal said the build was fine. It compiled, lint passed, the whole unit suite was
 * green, and it launched perfectly on the development container — because the one reader that
 * opened the duplicate sat behind an `isEmulator` short-circuit, so the emulator never reached it
 * and only real hardware did. A source scan is the only check available: the defect is the
 * EXISTENCE of a second declaration, which nothing observes until a device runs the other branch.
 *
 * Deliberately a source scan rather than a Robolectric test: the crash needs two stores to be live
 * in one process, which means the real component graph on a real device, and a JVM test cannot
 * assemble that. Scanning is `O(collection)` over the module's Kotlin sources — a few hundred small
 * files, well under a second.
 */
class DataStoreOwnershipTest {

    @Test
    fun `no preferences file is opened by more than one DataStore delegate`() {
        val declarations = sourceRoot.walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .flatMap { file ->
                DELEGATE.findAll(file.readText()).map { it.groupValues[1] to file.name }
            }
            .toList()

        // Power check: if the scan finds nothing at all it is looking in the wrong place, and an
        // empty grouping would pass this test while proving nothing.
        assertTrue(
            "the scan found no DataStore declarations at all — it is pointed at the wrong tree",
            declarations.isNotEmpty(),
        )

        declarations.groupBy({ it.first }, { it.second }).forEach { (fileName, owners) ->
            assertEquals(
                "the preferences file \"$fileName\" is opened by ${owners.size} DataStore delegates " +
                    "($owners). Two live stores over one file throw at first read, on the main " +
                    "thread. Route the second reader through the class that already owns it.",
                1,
                owners.size,
            )
        }
    }

    private companion object {
        val DELEGATE = Regex("""preferencesDataStore\(\s*name\s*=\s*"([^"]+)"""")

        /** `app/src`, resolved from the module dir Gradle runs unit tests in. */
        val sourceRoot = File("src").takeIf { it.isDirectory } ?: File("app/src")
    }
}
