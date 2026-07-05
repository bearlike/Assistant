package com.mewbo.aura.voice

import android.os.Bundle
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService

/** Factory the platform calls to obtain the orb overlay's [VoiceInteractionSession] (§5.1). */
class AuraSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession = AuraSession(this)
}
