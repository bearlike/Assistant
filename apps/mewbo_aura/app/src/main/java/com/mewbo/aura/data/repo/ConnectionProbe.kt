package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.ModelsResponseDto
import java.io.IOException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response

/**
 * Probes a candidate `{baseUrl, apiKey}` pair against `GET /api/models` BEFORE either is
 * persisted (Settings spec: explicit "Validate & save", never write-on-keystroke for the
 * connection fields). Deliberately owns its OWN short-timeout [OkHttpClient] instead of the
 * app-wide one from [com.mewbo.aura.di.DataModule] - that client's interceptors read the
 * ALREADY-persisted [com.mewbo.aura.data.settings.SettingsStore] values, so it can only ever
 * validate what's already saved, never an unsaved draft.
 */
@Singleton
class ConnectionProbe @Inject constructor(private val json: Json) {

    sealed interface Result {
        data class Ok(val modelCount: Int) : Result
        data class Http(val code: Int) : Result
        data class Unreachable(val reason: String) : Result
    }

    suspend fun validate(baseUrl: String, apiKey: String): Result {
        val url = "${baseUrl.trimEnd('/')}/api/models".toHttpUrlOrNull()
            ?: return Result.Unreachable("Invalid server URL")
        val request = Request.Builder().url(url).header("X-API-Key", apiKey).build()
        // Pinned to IO: await() resumes on the CALLER's dispatcher (Main from the ViewModel), and
        // body.string() is a blocking socket read - unpinned it fatally throws
        // NetworkOnMainThreadException whenever the body isn't already buffered (the 401 path).
        return withContext(Dispatchers.IO) {
            try {
                client.newCall(request).await().use { response ->
                    mapResponse(response.code, response.body?.string().orEmpty())
                }
            } catch (e: IOException) {
                Result.Unreachable(reasonFor(e))
            }
        }
    }

    /** Split out from [validate] so the wire-mapping logic is unit-testable without a real socket. */
    internal fun mapResponse(code: Int, body: String): Result {
        if (code != 200) return Result.Http(code)
        return try {
            Result.Ok(json.decodeFromString(ModelsResponseDto.serializer(), body).models.size)
        } catch (e: SerializationException) {
            Result.Unreachable("Malformed response")
        }
    }

    private fun reasonFor(e: IOException): String = when (e) {
        is UnknownHostException -> "Can't resolve host"
        is SocketTimeoutException -> "Connection timed out"
        else -> e.message ?: "Server unreachable"
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(ProbeTimeoutSeconds, TimeUnit.SECONDS)
        .readTimeout(ProbeTimeoutSeconds, TimeUnit.SECONDS)
        .writeTimeout(ProbeTimeoutSeconds, TimeUnit.SECONDS)
        .build()

    private companion object {
        const val ProbeTimeoutSeconds = 5L
    }
}

/** Top-level (not a class member) so `Result` inside resolves to [kotlin.Result], not [ConnectionProbe.Result]. */
private suspend fun Call.await(): Response {
    val call = this
    return suspendCancellableCoroutine { cont ->
        cont.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onResponse(call: Call, response: Response) {
                if (cont.isActive) cont.resumeWith(Result.success(response)) else response.close()
            }

            override fun onFailure(call: Call, e: IOException) {
                if (cont.isActive) cont.resumeWith(Result.failure(e))
            }
        })
    }
}
