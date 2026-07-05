package com.mewbo.aura.data.device

/** One inbox row, already detached from `android.database.Cursor`/`ContentResolver`. */
data class SmsMessageRow(val from: String, val body: String, val receivedAtEpochMillis: Long)

/**
 * Seam over `ContentResolver.query(Telephony.Sms.Inbox.CONTENT_URI, ...)` so
 * [ReadLatestSmsHandler]'s count-clamping/sender-filter decision logic is unit-testable with a
 * plain in-memory fake instead of a real `ContentResolver`/`Cursor` (neither constructible in a
 * plain-JVM test - no Robolectric, apps/mewbo_aura/CLAUDE.md).
 */
fun interface SmsInboxReader {
    /** Newest-first rows, up to [maxRows]. Filtering/limiting to the caller's requested `count` is
     * the HANDLER's job, not this seam's - keeps this a plain "give me the raw sorted pool" read. */
    fun queryInbox(maxRows: Int): List<SmsMessageRow>
}
