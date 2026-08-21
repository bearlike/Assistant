package com.mewbo.aura.data.update

import okhttp3.ResponseBody
import retrofit2.http.GET
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.Streaming
import retrofit2.http.Url

/**
 * The release feed, on either forge.
 *
 * **`/releases/latest` is deliberately not here, and that is a measured decision.** On both forges
 * that endpoint returns ONE release and EXCLUDES prereleases — and Aura's newest build is published
 * as a prerelease, so a device already running it would be told a strictly older release is the
 * latest. The tag namespace is shared with the server's own releases as well, so even the release
 * `/latest` returns may not be an Aura build at all. Listing and choosing on the client is the only
 * shape that can answer both, and it costs the same single request.
 *
 * Drafts arrive only for a caller the forge has authenticated, and this client sends no
 * credentials, so in practice the list is public releases — but [AppUpdateRepository] filters
 * `draft` anyway rather than relying on that.
 */
interface ReleaseApi {

    /**
     * One bounded page of releases, newest first.
     *
     * `O(collection)` with a fixed bound ([UpdateChannel.PAGE_SIZE]). Both page-size parameters go
     * on every call because the two forges spell it differently and each ignores the other's — see
     * [UpdateChannel.PAGE_SIZE].
     */
    @GET("repos/{owner}/{repo}/releases")
    suspend fun releases(
        @Path("owner") owner: String,
        @Path("repo") repo: String,
        @Query("per_page") perPage: Int,
        @Query("limit") limit: Int,
    ): List<ReleaseDto>

    /**
     * The APK itself, streamed.
     *
     * `@Streaming` is load-bearing rather than an optimisation: without it Retrofit buffers the
     * whole body into memory before returning, which for a ~100 MB APK is an out-of-memory kill on
     * a modest device and leaves no way to report progress. `@Url` because the download URL is an
     * absolute one the forge handed us, on a host that may not be the API root.
     */
    @Streaming
    @GET
    suspend fun download(@Url url: String): ResponseBody
}
