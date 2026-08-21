package com.mewbo.aura.data.update

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One release, as BOTH forges describe it.
 *
 * **Gitea's release API is GitHub-shaped, which is why there is one DTO and not two.** Measured
 * field-for-field against a live Gitea instance and against api.github.com: `tag_name`, `name`,
 * `body`, `draft`, `prerelease`, `published_at`, and `assets[]` with `name`, `size`,
 * `browser_download_url` are spelled identically on both. The ONLY thing that differs between the
 * two is the API root, which is a build-time constant ([UpdateChannel]) — so nothing downstream of
 * here ever asks which forge answered.
 *
 * **This is a trust boundary.** Every field carries a default and the shared `Json` sets
 * `ignoreUnknownKeys`, so a forge that adds a field, renames one it does not own, or omits one it
 * usually sends cannot take the parse — and therefore the Settings screen — down. A missing field
 * degrades to a release the picker will simply not select.
 */
@Serializable
data class ReleaseDto(
    @SerialName("tag_name") val tagName: String = "",
    val name: String? = null,
    val body: String? = null,
    val draft: Boolean = false,
    val prerelease: Boolean = false,
    @SerialName("published_at") val publishedAt: String? = null,
    val assets: List<ReleaseAssetDto> = emptyList(),
)

/**
 * One downloadable file attached to a release.
 *
 * **Neither forge exposes a checksum or digest here**, so [size] is the only integrity signal the
 * API offers and the download is verified against it plus the APK's own manifest. That is a limit
 * of the wire format, not a choice — see [AppUpdateRepository].
 */
@Serializable
data class ReleaseAssetDto(
    val name: String = "",
    val size: Long = 0,
    @SerialName("browser_download_url") val browserDownloadUrl: String = "",
)
