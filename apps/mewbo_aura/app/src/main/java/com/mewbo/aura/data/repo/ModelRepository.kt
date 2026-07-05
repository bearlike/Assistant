package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.model.ModelCapabilities
import com.mewbo.aura.data.model.ModelCatalog
import javax.inject.Inject
import kotlinx.coroutines.CancellationException

/**
 * Fetches + caches the model catalog (`GET api/models`). Degrades gracefully offline: [catalog]
 * returns the last successful fetch, or `null` if there has never been one - callers (the model
 * picker sheet) show the current selection and a "couldn't load" notice rather than crashing.
 */
class ModelRepository @Inject constructor(
    private val api: AuraApi,
) {
    private var cached: ModelCatalog? = null

    suspend fun catalog(): ModelCatalog? {
        cached?.let { return it }
        // Gitea #181 fix wave, finding 2: runCatching catches Throwable, including
        // CancellationException - a caller cancelled mid-fetch (e.g. ChatViewModel's own
        // async{} callers) must still stop instead of quietly resolving to null and letting the
        // caller act on a "failure" that was really a cancellation. Rethrow before falling back
        // to the degrade-to-null path, matching SessionStreamClient's own discipline.
        val response = runCatching { api.getModels() }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull() ?: return null
        return ModelCatalog(
            rawModels = response.models,
            default = response.default,
            capabilities = response.capabilities.mapValues { (_, dto) -> ModelCapabilities(dto.supportsVision) },
        ).also { cached = it }
    }
}
