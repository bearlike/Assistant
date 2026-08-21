package com.mewbo.aura.data.device

import android.content.Context
import android.provider.Telephony
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject

/**
 * Production [SmsInboxReader], reading `Telephony.Sms.CONTENT_URI` sorted newest-first by
 * [Telephony.Sms.DATE].
 *
 * **`CONTENT_URI`, not `Inbox.CONTENT_URI`** - the inbox URI hides `MESSAGE_TYPE_SENT` rows, so a
 * conversation read through it silently omits every reply the user sent. Verified on device: the
 * same 12-message thread returns 12 rows here and 9 through the inbox URI, with nothing in the
 * response marking the 3 as missing. Drafts, outbox and failed rows are still excluded (the
 * `TYPE IN (inbox, sent)` selection) - a draft is not something either party said.
 *
 * `offset`/`limit` are applied by walking the cursor rather than through a raw SQL `LIMIT`/`OFFSET`
 * suffix on the sort order, which is undocumented for a `ContentProvider`. Cursor windows fill
 * lazily, so this materialises `offset + limit` rows, not the whole match set.
 *
 * Never logs [SmsMessageRow.address]/[SmsMessageRow.body] - message content is personal data and
 * only ever flows into the result POST.
 */
class ContentResolverSmsInboxReader @Inject constructor(
    @ApplicationContext private val context: Context,
) : SmsInboxReader {
    override fun queryMessages(senderFilter: String?, offset: Int, limit: Int): List<SmsMessageRow> {
        if (limit <= 0) return emptyList()
        val projection = arrayOf(
            Telephony.Sms.ADDRESS,
            Telephony.Sms.BODY,
            Telephony.Sms.DATE,
            Telephony.Sms.TYPE,
        )
        val selection = StringBuilder("${Telephony.Sms.TYPE} IN (?, ?)")
        val selectionArgs = mutableListOf(
            Telephony.Sms.MESSAGE_TYPE_INBOX.toString(),
            Telephony.Sms.MESSAGE_TYPE_SENT.toString(),
        )
        if (!senderFilter.isNullOrEmpty()) {
            selection.append(" AND ${Telephony.Sms.ADDRESS} LIKE ? ESCAPE '$LIKE_ESCAPE'")
            selectionArgs += "%${senderFilter.escapedForLike()}%"
        }

        val rows = mutableListOf<SmsMessageRow>()
        context.contentResolver.query(
            Telephony.Sms.CONTENT_URI,
            projection,
            selection.toString(),
            selectionArgs.toTypedArray(),
            "${Telephony.Sms.DATE} DESC",
        )?.use { cursor ->
            val addressIndex = cursor.getColumnIndexOrThrow(Telephony.Sms.ADDRESS)
            val bodyIndex = cursor.getColumnIndexOrThrow(Telephony.Sms.BODY)
            val dateIndex = cursor.getColumnIndexOrThrow(Telephony.Sms.DATE)
            val typeIndex = cursor.getColumnIndexOrThrow(Telephony.Sms.TYPE)
            var skipped = 0
            while (skipped < offset && cursor.moveToNext()) skipped++
            while (rows.size < limit && cursor.moveToNext()) {
                rows += SmsMessageRow(
                    address = cursor.getString(addressIndex) ?: "",
                    body = cursor.getString(bodyIndex) ?: "",
                    timestampEpochMillis = cursor.getLong(dateIndex),
                    outbound = cursor.getInt(typeIndex) == Telephony.Sms.MESSAGE_TYPE_SENT,
                )
            }
        }
        return rows
    }

    private companion object {
        const val LIKE_ESCAPE = '\\'

        /** The filter is model output, so its `%`/`_` are matched LITERALLY - an unescaped `%`
         * would silently widen a "one conversation" read back into a mailbox-wide one, which is
         * the failure this tool exists to close. */
        fun String.escapedForLike(): String = buildString(length) {
            for (character in this@escapedForLike) {
                if (character == LIKE_ESCAPE || character == '%' || character == '_') append(LIKE_ESCAPE)
                append(character)
            }
        }
    }
}
