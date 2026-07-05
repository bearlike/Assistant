package com.mewbo.aura.voice

import android.service.voice.VoiceInteractionService

/**
 * Long-lived marker service bound by the system once Aura is selected as the default digital
 * assistant (`RoleManager.ROLE_ASSISTANT`, §5.1). All real work happens in [AuraSessionService] /
 * [AuraSession] - this class exists because the platform requires ONE registered
 * `VoiceInteractionService` component, pointed at by `res/xml/voice_interaction_service.xml`'s
 * `sessionService`. v1 has no hotword/always-on listening (§14, out of scope), so there is nothing
 * to override here.
 */
class AuraVoiceInteractionService : VoiceInteractionService()
