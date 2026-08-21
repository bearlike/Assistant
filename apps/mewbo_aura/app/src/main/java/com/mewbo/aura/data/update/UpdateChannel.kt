package com.mewbo.aura.data.update

/**
 * Where this BUILD looks for its own updates, and which released file fits it.
 *
 * Every field is stamped in at build time (`di/UpdateModule` reads them off `BuildConfig`), so the
 * running app never decides where to look. That is what lets one client serve a public GitHub
 * release feed and a private forge's with no `if (isEnterprise)` anywhere: the flavor difference is
 * entirely [apiRoot], and it arrives as data.
 *
 * The picker's questions all live here as members rather than as predicates spread across the
 * repository, so "which release is ours" and "which file is ours" are each answered in one place.
 */
data class UpdateChannel(
    /** Release API root WITH a trailing slash — `https://api.github.com/` or `…/api/v1/`. */
    val apiRoot: String,
    val owner: String,
    val repo: String,
    /** `BuildConfig.FLAVOR` — `public` or `enterprise`. */
    val flavor: String,
    /** `BuildConfig.BUILD_TYPE` — `debug` or `release`. */
    val buildType: String,
) {
    /**
     * Whether this build was given somewhere to look.
     *
     * False is a real, reportable state — an enterprise build whose API root never arrived. It must
     * read as "not configured", never as "up to date": the second is a claim about the world made
     * by a build that never asked. The Gradle build refuses to produce such an APK
     * (`requireEnterpriseUpdateApiRoot`); this is the runtime half, so a build that slipped through
     * some other path still says something honest.
     */
    val isConfigured: Boolean get() = apiRoot.isNotBlank() && owner.isNotBlank() && repo.isNotBlank()

    /**
     * The tail every released Aura APK's file name carries.
     *
     * **Matching on the SUFFIX rather than the whole name is what keeps already-published releases
     * visible.** The scheme the build now produces is `aura-<version>-<flavor>-<buildType>.apk`;
     * every release published before it is `app-<flavor>-<buildType>.apk`. Both end the same way,
     * and the two segments that actually decide whether a file will work on this device — the
     * flavor, which determines whether the APK trusts the deployment's own CA, and the build type,
     * which determines which key signed it — are exactly the two in the suffix. A picker keyed on
     * the full name would see no existing release at all.
     */
    val assetSuffix: String get() = "-$flavor-$buildType.apk"

    /** Whether [asset] is the file this device should install. `O(1)`. */
    fun fits(asset: ReleaseAssetDto): Boolean =
        asset.name.endsWith(assetSuffix, ignoreCase = true) && asset.browserDownloadUrl.isNotBlank()

    /**
     * The app version [release] carries, or `null` when it is not an Aura release at all.
     *
     * **The tag namespace is SHARED with the server's own releases** (`v0.0.12` and friends), so a
     * release list is not a list of Aura builds and "the newest release" is not the newest Aura
     * build. The prefix is the discriminator, and a tag without it is dropped rather than parsed —
     * `v0.0.12` would otherwise read as a perfectly plausible version number for this app.
     */
    fun releaseVersion(release: ReleaseDto): AppVersion? =
        release.tagName
            .takeIf { it.startsWith(TAG_PREFIX, ignoreCase = true) }
            ?.removePrefix(TAG_PREFIX)
            ?.let(AppVersion::parse)

    companion object {
        /** Aura's own release tags — `aura-0.0.20.0`. Set by hand at release time; the release
         * instructions in `apps/mewbo_aura/CLAUDE.md` are the other half of this contract. */
        const val TAG_PREFIX = "aura-"

        /** How many releases one check reads. `O(collection)` with a hard bound, per the root
         * CLAUDE.md's listing law — the newest Aura build is always within a page of the newest
         * release, and an unbounded list would grow with the repository's whole history.
         *
         * Both spellings are sent on every request: GitHub reads `per_page` and ignores `limit`,
         * Gitea reads `limit` and ignores `per_page` (verified against both). One request shape,
         * two forges, no branch. */
        const val PAGE_SIZE = 20
    }
}
