package com.mewbo.aura.data.device

/**
 * One message row, already detached from `android.database.Cursor`/`ContentResolver`.
 *
 * [address] is the OTHER party for BOTH directions - the sender of an inbound message, the
 * recipient of an outbound one - which is why it is not called `from`. Calling it `from` on an
 * outbound row states the opposite of the truth, and a reader with no [outbound] flag cannot
 * catch that: a reply the user wrote and one they received are indistinguishable once quoted
 * back as prose.
 *
 * **The `contact_name` seam, deliberately absent.** A raw number is a poor answer to "who has been
 * in touch" - but number-to-name resolution is ONE primitive shared with the call log, not a
 * per-table lookup. When it lands it belongs beside [address] as a nullable `contactName`,
 * resolved through a single injected resolver bound in `di/DeviceModule` and consumed identically
 * by both readers. Do not resolve it inline here; two independent implementations of "who is this
 * number" is the per-capability drift that produced this whole cluster.
 */
data class SmsMessageRow(
    val address: String,
    val body: String,
    val timestampEpochMillis: Long,
    val outbound: Boolean,
)

/**
 * Seam over `ContentResolver.query(Telephony.Sms.CONTENT_URI, ...)` so [ReadLatestSmsHandler]'s
 * paging/clamping/direction decisions are unit-testable with a plain in-memory fake instead of a
 * real `ContentResolver`/`Cursor` (neither constructible in a plain-JVM test - no Robolectric,
 * apps/mewbo_aura/CLAUDE.md).
 *
 * The name is historical: this reads the whole mailbox (received AND sent), not just the inbox.
 */
fun interface SmsInboxReader {
    /**
     * Newest-first rows matching [senderFilter], skipping [offset] of them and returning at most
     * [limit].
     *
     * **Narrowing and paging happen HERE, together, and that is load-bearing.** A reader that
     * returned a fixed newest-first pool for the caller to filter afterwards can only ever page
     * within that pool, so a conversation older than the pool is invisible no matter which page is
     * asked for - which is exactly how a 52-message thread returned zero rows. `offset` is only
     * meaningful against the SAME set the filter selects.
     *
     * [senderFilter] is a case-insensitive substring of the peer address; `null`/empty means every
     * conversation. Implementations must treat it as untrusted (it is model output) - any wildcard
     * syntax in it is matched literally.
     *
     * Cost: `O(collection)` - the provider scans the mailbox for matches, but at most
     * `offset + limit` rows are ever materialised, and the caller bounds `limit`.
     */
    fun queryMessages(senderFilter: String?, offset: Int, limit: Int): List<SmsMessageRow>
}
