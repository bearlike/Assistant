package com.mewbo.aura.data.device

import android.content.Context
import android.provider.Telephony
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject

/** Production [SmsInboxReader]: sorted newest-first by [Telephony.Sms.DATE], capped at [maxRows]
 * while iterating the cursor rather than via a raw SQL `LIMIT` (avoids depending on an
 * undocumented sort-order string suffix). Never logs a row's [SmsMessageRow.from]/[SmsMessageRow.body]
 * - message content is personal data and only ever flows into the result POST (task brief). */
class ContentResolverSmsInboxReader @Inject constructor(
    @ApplicationContext private val context: Context,
) : SmsInboxReader {
    override fun queryInbox(maxRows: Int): List<SmsMessageRow> {
        val projection = arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE)
        val rows = mutableListOf<SmsMessageRow>()
        context.contentResolver.query(
            Telephony.Sms.Inbox.CONTENT_URI,
            projection,
            null,
            null,
            "${Telephony.Sms.DATE} DESC",
        )?.use { cursor ->
            val addressIndex = cursor.getColumnIndexOrThrow(Telephony.Sms.ADDRESS)
            val bodyIndex = cursor.getColumnIndexOrThrow(Telephony.Sms.BODY)
            val dateIndex = cursor.getColumnIndexOrThrow(Telephony.Sms.DATE)
            while (rows.size < maxRows && cursor.moveToNext()) {
                rows += SmsMessageRow(
                    from = cursor.getString(addressIndex) ?: "",
                    body = cursor.getString(bodyIndex) ?: "",
                    receivedAtEpochMillis = cursor.getLong(dateIndex),
                )
            }
        }
        return rows
    }
}
