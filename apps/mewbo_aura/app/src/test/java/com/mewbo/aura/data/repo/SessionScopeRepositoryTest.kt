package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.api.ToolDto
import com.mewbo.aura.data.api.ToolsResponseDto
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.mockito.Mockito.mock
import org.mockito.Mockito.`when`

/**
 * [SessionScopeRepository.tools] widens from MCP-only to also admit
 * capability-gated product tools (`scope == "plugin"`), while continuing to exclude plain core
 * builtins (`kind == "builtin"`, `scope == "builtin"` or unset) that would otherwise flood the
 * picker with entries nobody can usefully toggle.
 */
class SessionScopeRepositoryTest {

    private fun tool(
        id: String,
        kind: String = "builtin",
        scope: String? = "builtin",
        server: String? = null,
    ) = ToolDto(toolId = id, name = id, kind = kind, server = server, scope = scope)

    @Test
    fun `mcp tools are included regardless of scope`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.getTools(null)).thenReturn(
            ToolsResponseDto(tools = listOf(tool("github_search", kind = "mcp", scope = "project"))),
        )
        val repo = SessionScopeRepository(api)

        val result = repo.tools()

        assertEquals(listOf("github_search"), result?.map { it.toolId })
    }

    @Test
    fun `product tools at scope plugin are included`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.getTools(null)).thenReturn(
            ToolsResponseDto(
                tools = listOf(
                    tool("wiki_search_pages", kind = "builtin", scope = "plugin", server = "Wiki"),
                    tool("agentic_search", kind = "builtin", scope = "plugin", server = "Agentic Search"),
                ),
            ),
        )
        val repo = SessionScopeRepository(api)

        val result = repo.tools()

        assertEquals(setOf("wiki_search_pages", "agentic_search"), result?.map { it.toolId }?.toSet())
    }

    @Test
    fun `plain core builtins at scope builtin are excluded`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.getTools(null)).thenReturn(
            ToolsResponseDto(tools = listOf(tool("shell", kind = "builtin", scope = "builtin"))),
        )
        val repo = SessionScopeRepository(api)

        val result = repo.tools()

        assertEquals(emptyList<String>(), result?.map { it.toolId })
    }

    @Test
    fun `a builtin with no scope at all is excluded (defensive default)`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.getTools(null)).thenReturn(
            ToolsResponseDto(tools = listOf(tool("edit", kind = "builtin", scope = null))),
        )
        val repo = SessionScopeRepository(api)

        val result = repo.tools()

        assertEquals(emptyList<String>(), result?.map { it.toolId })
    }

    @Test
    fun `mixed catalog keeps mcp and product tools, drops core builtins`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.getTools(null)).thenReturn(
            ToolsResponseDto(
                tools = listOf(
                    tool("shell", kind = "builtin", scope = "builtin"),
                    tool("github_search", kind = "mcp", scope = "project"),
                    tool("wiki_search_pages", kind = "builtin", scope = "plugin", server = "Wiki"),
                ),
            ),
        )
        val repo = SessionScopeRepository(api)

        val result = repo.tools()

        assertEquals(setOf("github_search", "wiki_search_pages"), result?.map { it.toolId }?.toSet())
    }

    @Test
    fun `a cancellation while fetching tools propagates rather than degrading to null`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.getTools(null)).thenThrow(CancellationException("cancelled"))
        val repo = SessionScopeRepository(api)

        try {
            repo.tools()
            throw AssertionError("expected CancellationException to propagate")
        } catch (e: CancellationException) {
            // expected - see the discipline this file's own KDoc documents.
        }
    }

    @Test
    fun `a non-cancellation failure degrades to null`() = runTest {
        val api = mock(AuraApi::class.java)
        `when`(api.getTools(null)).thenThrow(RuntimeException("network error"))
        val repo = SessionScopeRepository(api)

        assertNull(repo.tools())
    }
}
