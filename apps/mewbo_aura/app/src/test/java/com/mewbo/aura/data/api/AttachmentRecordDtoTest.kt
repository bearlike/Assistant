package com.mewbo.aura.data.api

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Round-trips [AttachmentRecordDto]/[AttachmentsResponseDto] against the real wire shape
 * (`attachment_descriptor_model`/`attachments_response_model`, backend.py) and verifies
 * [AttachmentRecordDto] re-encodes with the exact field names the `/query` endpoint's `attachments`
 * array expects back (task brief: "records from (a), verbatim"). */
class AttachmentRecordDtoTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `decodes the upload endpoint's real response shape`() {
        val raw = """
            {"attachments":[
                {"id":"a1b2c3d4e5f6","filename":"spec.pdf","stored_name":"a1b2c3d4e5f6_spec.pdf",
                 "content_type":"application/pdf","size_bytes":20481,
                 "uploaded_at":"2026-06-15T18:24:05.412903+00:00","parsed":true}
            ]}
        """.trimIndent()

        val response = json.decodeFromString(AttachmentsResponseDto.serializer(), raw)
        val record = response.attachments.single()

        assertEquals("a1b2c3d4e5f6", record.id)
        assertEquals("spec.pdf", record.filename)
        assertEquals("a1b2c3d4e5f6_spec.pdf", record.storedName)
        assertEquals("application/pdf", record.contentType)
        assertEquals(20481L, record.sizeBytes)
        assertEquals("2026-06-15T18:24:05.412903+00:00", record.uploadedAt)
        assertTrue(record.parsed)
    }

    @Test
    fun `parsed defaults false when the backend omits it`() {
        val raw = """{"attachments":[{"id":"x","filename":"a.png","stored_name":"x_a.png","content_type":"image/png","size_bytes":10,"uploaded_at":"t"}]}"""

        val record = json.decodeFromString(AttachmentsResponseDto.serializer(), raw).attachments.single()

        assertFalse(record.parsed)
    }

    @Test
    fun `re-encodes with the exact snake_case field names the query endpoint expects back`() {
        val record = AttachmentRecordDto(
            id = "a1b2c3d4e5f6",
            filename = "spec.pdf",
            storedName = "a1b2c3d4e5f6_spec.pdf",
            contentType = "application/pdf",
            sizeBytes = 20481,
            uploadedAt = "2026-06-15T18:24:05.412903+00:00",
            parsed = true,
        )

        val encoded = json.encodeToString(AttachmentRecordDto.serializer(), record)

        assertTrue(encoded.contains("\"stored_name\":\"a1b2c3d4e5f6_spec.pdf\""))
        assertTrue(encoded.contains("\"content_type\":\"application/pdf\""))
        assertTrue(encoded.contains("\"size_bytes\":20481"))
        assertTrue(encoded.contains("\"uploaded_at\":\"2026-06-15T18:24:05.412903+00:00\""))
    }
}
