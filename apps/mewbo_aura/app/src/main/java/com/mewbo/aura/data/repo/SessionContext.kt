package com.mewbo.aura.data.repo

import com.mewbo.aura.data.device.DeviceToolDefinition
import com.mewbo.aura.data.device.toContextEntry
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * The `context` object shape shared by session creation AND every `/query` call (data/CLAUDE.md,
 * `apps/mewbo_api/CLAUDE.md`'s `SessionQuery.post`): the backend independently re-resolves
 * `project` and re-defaults `model` back to the config default from EACH request's own context -
 * neither is sticky across turns server-side - so callers must resend both on every send, not just
 * at session creation. `mcp_tools` is omitted entirely when the caller hasn't narrowed anything
 * (task brief) so the backend's own default - bind every tool - stays in effect.
 *
 * `device_tools` follows the SAME resend law as `model`/`project`/`mcp_tools`, but
 * ONLY [RunRepository.sendQuery] passes it - `device_tools` isn't part of session-creation's own
 * context (task brief scopes it to "re-enumerated FRESH on every `/query`" specifically), so
 * [deviceTools] defaults to `null`/omitted for [SessionRepository.createSession]'s call site.
 *
 * **Both tool lists are THREE-STATE, and the empty case must survive the wire.** `null` omits the
 * key; an EMPTY list is written as `[]`, because a client that advertises no tools has declared a
 * real ceiling, not an absent one. Testing either list for truthiness (`isNullOrEmpty`) collapses
 * "I turned everything off" into "I have no preference" - a fail-open the server cannot detect,
 * since absence and never-declared are the same bytes. The two ABSENT states do NOT mean the same
 * thing, which is why each key is written on its own `!= null` test rather than one shared helper:
 * - `mcp_tools` absent => no ceiling, the backend binds its default set (`_extract_allowed_tools`).
 * - `device_tools` absent => SILENCE, and the backend falls back to the session's newest context
 *   event that carries the key (`DeviceToolBinding.declaration_for`). So an omitted empty list here
 *   leaves a previously-declared set bound - the user revokes a device tool and it stays live.
 *
 * **[project] carries a RESERVED value as well as real keys**, and this function is the one place
 * that decision reaches the wire. Three states, all expressed through this one field:
 * `null`/blank omits it entirely (a throwaway temp-dir cwd),
 * [com.mewbo.aura.data.model.ComposerScope.AUTO_PROJECT_KEY] sends the auto-select sentinel (no
 * project fixed - Mewbo picks one, and may move the session between projects mid-run), and anything
 * else is a catalogue key (bare name or `managed:<id>`). The sentinel needs no arm of its own here
 * BY DESIGN: it travels as an ordinary non-blank string, so the resolver on the far side owns the
 * whole meaning of it and this side cannot develop a second opinion about what "auto" resolves to.
 */
internal fun buildSessionContext(
    model: String?,
    project: String?,
    mcpTools: List<String>?,
    deviceTools: List<DeviceToolDefinition>? = null,
): JsonObject = buildJsonObject {
    put("client", JsonPrimitive("aura-android"))
    if (!model.isNullOrBlank()) put("model", JsonPrimitive(model))
    if (!project.isNullOrBlank()) put("project", JsonPrimitive(project))
    if (mcpTools != null) put("mcp_tools", JsonArray(mcpTools.map { JsonPrimitive(it) }))
    if (deviceTools != null) put("device_tools", JsonArray(deviceTools.map { it.toContextEntry() }))
}
