package com.mewbo.aura.data.device.shizuku

/**
 * The device's true screen geometry, parsed from `wm size` / `wm density`.
 *
 * Read once per session and re-read on rotation. It exists because the model
 * never sees pixels: the client resolves an element index to a centre point
 * locally, and that arithmetic needs the real resolution. A downscaled
 * screenshot's dimensions are NOT it.
 */
data class DisplayGeometry(val width: Int, val height: Int, val density: Int) {

    companion object {
        /**
         * Parse `wm size` output.
         *
         * **`Override size:` wins over `Physical size:` when present.** An
         * override is exactly the case that would silently corrupt the
         * coordinate space — the physical panel is 1440 wide, the window
         * manager is addressing 1080, and taps land proportionally wrong
         * rather than erroring.
         */
        fun parseSize(output: String): Pair<Int, Int>? =
            parseDimension(output, "Override size:") ?: parseDimension(output, "Physical size:")

        /** Parse `wm density` output, same override-wins rule. */
        fun parseDensity(output: String): Int? =
            parseScalar(output, "Override density:") ?: parseScalar(output, "Physical density:")

        fun parse(sizeOutput: String, densityOutput: String): DisplayGeometry? {
            val (width, height) = parseSize(sizeOutput) ?: return null
            return DisplayGeometry(width, height, parseDensity(densityOutput) ?: 0)
        }

        private fun parseDimension(output: String, label: String): Pair<Int, Int>? {
            val value = valueAfter(output, label) ?: return null
            val parts = value.split("x")
            if (parts.size != 2) return null
            val width = parts[0].trim().toIntOrNull() ?: return null
            val height = parts[1].trim().toIntOrNull() ?: return null
            return width to height
        }

        private fun parseScalar(output: String, label: String): Int? =
            valueAfter(output, label)?.toIntOrNull()

        private fun valueAfter(output: String, label: String): String? =
            output.lineSequence()
                .map { it.trim() }
                .firstOrNull { it.startsWith(label) }
                ?.removePrefix(label)
                ?.trim()
                ?.takeIf { it.isNotEmpty() }
    }
}
