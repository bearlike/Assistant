package com.mewbo.aura.voice

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.Locale
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow

/**
 * Wraps [SpeechRecognizer]. The recognizer MUST be created, driven, and destroyed on the main
 * thread — all work here is posted to [mainHandler] regardless of the collecting coroutine's
 * dispatcher. Unavailable on redroid: [SpeechRecognizer.isRecognitionAvailable] guards the
 * start and emits [TranscriberError.Unavailable] immediately instead of throwing.
 */
@Singleton
class SpeechRecognizerTranscriber @Inject constructor(
    @ApplicationContext private val context: Context,
) : Transcriber {

    private val mainHandler = Handler(Looper.getMainLooper())

    override fun listen(): Flow<TranscriberEvent> = callbackFlow {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            trySend(TranscriberEvent.Error(TranscriberError.Unavailable))
            close()
            return@callbackFlow
        }

        var recognizer: SpeechRecognizer? = null
        val listener = object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {
                trySend(TranscriberEvent.Ready)
            }

            override fun onRmsChanged(rmsdB: Float) {
                trySend(TranscriberEvent.Rms(rmsdB))
            }

            override fun onPartialResults(partialResults: Bundle) {
                partialResults.bestText()?.let { trySend(TranscriberEvent.Partial(it)) }
            }

            override fun onResults(results: Bundle) {
                results.bestText()?.let { trySend(TranscriberEvent.Final(it)) }
            }

            override fun onError(error: Int) {
                val code = when (error) {
                    SpeechRecognizer.ERROR_NO_MATCH -> TranscriberError.NoMatch
                    SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> TranscriberError.Timeout
                    else -> TranscriberError.Other
                }
                trySend(TranscriberEvent.Error(code))
            }

            override fun onBeginningOfSpeech() = Unit
            override fun onEndOfSpeech() = Unit
            override fun onBufferReceived(buffer: ByteArray?) = Unit
            override fun onEvent(eventType: Int, params: Bundle?) = Unit
        }

        mainHandler.post {
            val r = SpeechRecognizer.createSpeechRecognizer(context)
            recognizer = r
            r.setRecognitionListener(listener)
            r.startListening(recognizerIntent())
        }

        awaitClose {
            mainHandler.post {
                recognizer?.stopListening()
                recognizer?.destroy()
                recognizer = null
            }
        }
    }

    private fun recognizerIntent(): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag())
        }

    private fun Bundle.bestText(): String? =
        getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
}
