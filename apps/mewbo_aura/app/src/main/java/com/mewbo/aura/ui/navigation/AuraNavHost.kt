package com.mewbo.aura.ui.navigation

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.RectangleShape
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import androidx.hilt.navigation.compose.hiltViewModel
import com.mewbo.aura.ui.apps.AppCreateScreen
import com.mewbo.aura.ui.apps.AppDetailScreen
import com.mewbo.aura.ui.apps.AppDetailViewModel
import com.mewbo.aura.ui.apps.AppsGalleryScreen
import com.mewbo.aura.ui.aurora.LivenessShowcase
import com.mewbo.aura.ui.chat.ChatScreen
import com.mewbo.aura.ui.chat.ChatViewModel
import com.mewbo.aura.ui.common.LocalNoticeController
import com.mewbo.aura.ui.common.NoticeController
import com.mewbo.aura.ui.common.NoticeHost
import com.mewbo.aura.ui.orb.OrbShowcase
import com.mewbo.aura.ui.search.SearchChatsScreen
import com.mewbo.aura.ui.settings.SettingsScreen
import com.mewbo.aura.ui.theme.AuraColors
import com.mewbo.aura.ui.theme.AuraSpacing
import kotlinx.coroutines.launch

/** Route names + argument keys — the single source of truth for the nav graph below. */
private object AuraRoutes {
    const val SETTINGS = "settings"
    const val SEARCH = "search"
    const val ORB_GALLERY = "orb-gallery"
    const val LIVENESS_GALLERY = "liveness-gallery"

    // Mewbo Apps (design spec §4D) — a peer destination to Search/Settings, pushed WITHOUT the
    // drawer (see this object's own KDoc precedent below).
    const val APPS_GALLERY = "apps"
    const val APPS_CREATE = "apps/new"
    const val APP_ID_ARG = AppDetailViewModel.APP_ID_ARG
    const val APPS_DETAIL = "apps/detail/{$APP_ID_ARG}"
    fun appDetail(appId: String) = "apps/detail/$appId"

    const val SESSION_ID_ARG = "sessionId"
    /** the assist-overlay handoff's raw `InputModality.name` ("Voice"/"Text"),
     * baked into the route itself (not re-read off `pendingHandoffModality` after navigation) so it
     * survives `MainActivity`'s `onHandoffConsumed` nulling that prop out right after `navigate()`
     * fires below - the exact same reason [SESSION_ID_ARG] itself is a route arg, not a raw prop. */
    const val MODALITY_ARG = "handoffModality"
    const val CHAT = "chat?$SESSION_ID_ARG={$SESSION_ID_ARG}&$MODALITY_ARG={$MODALITY_ARG}"

    fun chat(sessionId: String?, modality: String? = null): String {
        val query = buildList {
            if (sessionId != null) add("$SESSION_ID_ARG=$sessionId")
            if (modality != null) add("$MODALITY_ARG=$modality")
        }
        return if (query.isEmpty()) "chat" else "chat?" + query.joinToString("&")
    }
}

/**
 * Rail width and scrim opacity are Rev E §E-6 ground-truth numbers (on-device frame F2), not named
 * [com.mewbo.aura.ui.theme.AuraSpacing]/[AuraColors] tokens yet — this task doesn't own `ui/theme/`,
 * so they live here with a citation rather than as an invented theme addition (flagged in the task
 * report as a follow-up for W1-A).
 */
private const val DRAWER_WIDTH_FRACTION = 0.78f
private const val DRAWER_SCRIM_ALPHA = 0.32f

/**
 * `SavedStateHandle` key the assist-overlay pull-up handoff stashes its carried composer draft under,
 * on the chat destination's back-stack entry (user directive 2026-07-04). Public so the chat lane's
 * `ChatViewModel` can read it and seed its composer (via the existing `DictationState.Final` ->
 * composer-draft path) - a raw value, deliberately not a URL-unsafe nav-route arg.
 */
const val HANDOFF_DRAFT_KEY = "com.mewbo.aura.handoffDraft"

/**
 * Compose Navigation graph (spec §6.7, §8, Rev E §E-6): `chat` (nullable `sessionId`, start
 * destination — there is no standalone landing/sessions screen) wrapped in a `ModalNavigationDrawer`
 * → `search` / `settings` pushed on top. `orb-gallery`/`liveness-gallery` only exist in debug
 * builds, reachable from Settings' debug section. [NoticeController] (spec §6.9) is provided here,
 * high enough that every destination below can reach it via [LocalNoticeController].
 */
@Composable
fun AuraNavHost(
    modifier: Modifier = Modifier,
    navController: NavHostController = rememberNavController(),
    /** Assist-overlay handoff target (`MainActivity.EXTRA_HANDOFF_SESSION_ID`) - a
     * session id to navigate to on cold AND warm start, reusing the drawer's own
     * `onOpenSession` navigate call below. `null` (the default) is a no-op, so every other
     * `AuraNavHost` caller (previews, tests) is unaffected. */
    pendingHandoffSessionId: String? = null,
    /** Paired with [pendingHandoffSessionId] (`MainActivity.EXTRA_HANDOFF_MODALITY`) -
     * the raw `InputModality.name` string; baked into the navigated route below (see
     * [AuraRoutes.MODALITY_ARG]'s KDoc), never re-read after that. `null` (the default) is a no-op,
     * same as [pendingHandoffSessionId]. */
    pendingHandoffModality: String? = null,
    /** The composer draft carried by a pull-up handoff (user directive 2026-07-04,
     * `MainActivity.EXTRA_HANDOFF_DRAFT`) - stashed as a RAW value on the chat destination's
     * `SavedStateHandle` (see [HANDOFF_DRAFT_KEY]), never baked into the route (arbitrary composer
     * text isn't URL-safe). `null` (the default) is a no-op. */
    pendingHandoffDraft: String? = null,
    /** `true` when a pull-up handoff had no session yet (user directive 2026-07-04,
     * `MainActivity.EXTRA_HANDOFF_NEW_CHAT`): forces navigation to a fresh new chat even when there's
     * no session id to route to. `false` (the default) leaves ordinary/session handoffs unchanged. */
    pendingHandoffNewChat: Boolean = false,
    onHandoffConsumed: () -> Unit = {},
) {
    val noticeController = remember { NoticeController() }

    LaunchedEffect(pendingHandoffSessionId, pendingHandoffNewChat) {
        // Fire for a session handoff (the existing second-interaction path) OR a pull-up that had no
        // session yet (land on a fresh new chat). An ordinary launch (both absent) is a no-op, so
        // every other AuraNavHost caller (previews, tests) is unaffected.
        if (pendingHandoffSessionId == null && !pendingHandoffNewChat) return@LaunchedEffect
        navController.navigate(AuraRoutes.chat(pendingHandoffSessionId, pendingHandoffModality)) {
            popUpTo(AuraRoutes.CHAT) { inclusive = true }
            launchSingleTop = true
        }
        // Pull-up draft: carried as a RAW SavedStateHandle value on the chat destination, NOT a route
        // arg (arbitrary composer text isn't URL-safe for a nav-route query string - the modality arg
        // can be a route arg only because it's a fixed enum name). The chat lane's ChatViewModel seeds
        // its composer from this key via its existing DictationState.Final -> composer-draft path;
        // that final read is chat-lane (ChatScreen/ChatSurface/ChatViewModel) and is intentionally
        // NOT wired here - those are another agent's files. See the handoff report's draft-carry flag.
        pendingHandoffDraft?.let { draft ->
            navController.currentBackStackEntry?.savedStateHandle?.set(HANDOFF_DRAFT_KEY, draft)
        }
        onHandoffConsumed()
    }

    CompositionLocalProvider(LocalNoticeController provides noticeController) {
        Box(modifier = modifier.fillMaxSize()) {
            NavHost(navController = navController, startDestination = AuraRoutes.CHAT, modifier = Modifier.fillMaxSize()) {
                composable(
                    route = AuraRoutes.CHAT,
                    arguments = listOf(
                        navArgument(AuraRoutes.SESSION_ID_ARG) {
                            type = NavType.StringType
                            nullable = true
                            defaultValue = null
                        },
                        navArgument(AuraRoutes.MODALITY_ARG) {
                            type = NavType.StringType
                            nullable = true
                            defaultValue = null
                        },
                    ),
                ) { backStackEntry ->
                    val sessionId = backStackEntry.arguments?.getString(AuraRoutes.SESSION_ID_ARG)
                    val handoffModality = backStackEntry.arguments?.getString(AuraRoutes.MODALITY_ARG)
                    // Pull-up draft consume side (user directive 2026-07-04): observed as a
                    // StateFlow, not a one-shot read, because a launchSingleTop handoff onto an
                    // ALREADY-current chat entry reuses the entry - a plain LaunchedEffect(entry)
                    // would never re-fire for a second pull-up. hiltViewModel() here resolves the
                    // SAME entry-scoped ChatViewModel instance ChatScreen uses.
                    val chatViewModel: ChatViewModel = hiltViewModel()
                    val handoffDraft by backStackEntry.savedStateHandle
                        .getStateFlow<String?>(HANDOFF_DRAFT_KEY, null)
                        .collectAsState()
                    LaunchedEffect(handoffDraft) {
                        val text = handoffDraft?.takeIf { it.isNotBlank() } ?: return@LaunchedEffect
                        chatViewModel.seedHandoffDraft(text)
                        backStackEntry.savedStateHandle[HANDOFF_DRAFT_KEY] = null
                    }
                    ChatHomeDestination(
                        sessionId = sessionId,
                        handoffModality = handoffModality,
                        onOpenSearch = { navController.navigate(AuraRoutes.SEARCH) },
                        onOpenSettings = { navController.navigate(AuraRoutes.SETTINGS) },
                        onOpenApps = { navController.navigate(AuraRoutes.APPS_GALLERY) },
                        onOpenSession = { id ->
                            navController.navigate(AuraRoutes.chat(id)) {
                                popUpTo(AuraRoutes.CHAT) { inclusive = true }
                                launchSingleTop = true
                            }
                        },
                        onNewChat = {
                            navController.navigate(AuraRoutes.chat(null)) {
                                popUpTo(AuraRoutes.CHAT) { inclusive = true }
                                launchSingleTop = true
                            }
                        },
                    )
                }
                composable(AuraRoutes.SEARCH) {
                    SearchChatsScreen(
                        onOpenSession = { id ->
                            navController.navigate(AuraRoutes.chat(id)) {
                                popUpTo(AuraRoutes.CHAT) { inclusive = true }
                            }
                        },
                        onBack = navController::popBackStack,
                    )
                }
                composable(AuraRoutes.SETTINGS) {
                    SettingsScreen(
                        onOpenOrbGallery = { navController.navigate(AuraRoutes.ORB_GALLERY) },
                        onOpenLivenessGallery = { navController.navigate(AuraRoutes.LIVENESS_GALLERY) },
                        onBack = navController::popBackStack,
                    )
                }
                // Mewbo Apps (design spec §4D): gallery -> detail / create, pushed WITHOUT the
                // drawer, same "drawer only on home" pattern as Search/Settings above.
                composable(AuraRoutes.APPS_GALLERY) {
                    AppsGalleryScreen(
                        onBack = navController::popBackStack,
                        onOpenApp = { appId -> navController.navigate(AuraRoutes.appDetail(appId)) },
                        onCreateApp = { navController.navigate(AuraRoutes.APPS_CREATE) },
                    )
                }
                composable(AuraRoutes.APPS_CREATE) {
                    AppCreateScreen(
                        onBack = navController::popBackStack,
                        onAppReady = { appId ->
                            // Replace the creation entry with the new app's detail screen (spec §5
                            // flow 1: "gallery card live" -> open it) - a completed build has
                            // nothing left to resume by navigating back to.
                            navController.navigate(AuraRoutes.appDetail(appId)) {
                                popUpTo(AuraRoutes.APPS_CREATE) { inclusive = true }
                            }
                        },
                    )
                }
                composable(
                    route = AuraRoutes.APPS_DETAIL,
                    arguments = listOf(navArgument(AuraRoutes.APP_ID_ARG) { type = NavType.StringType }),
                ) {
                    AppDetailScreen(onBack = navController::popBackStack)
                }
                if (IS_DEBUG_BUILD) {
                    composable(AuraRoutes.ORB_GALLERY) {
                        OrbShowcase()
                    }
                    composable(AuraRoutes.LIVENESS_GALLERY) {
                        LivenessShowcase()
                    }
                }
            }
            NoticeHost(controller = noticeController, modifier = Modifier.align(Alignment.BottomCenter))
        }
    }
}

/**
 * The chat route's own drawer wrapper (spec §6.7: "`ModalNavigationDrawer` wraps the chat host").
 * Search/Settings are separate pushed destinations without the drawer, matching a Gmail-style
 * "drawer only on the home surface" pattern.
 */
@Composable
private fun ChatHomeDestination(
    sessionId: String?,
    /** forwarded straight to [ChatScreen] as the raw handoff-modality string; see
     * [AuraRoutes.MODALITY_ARG]'s KDoc for why this travels via the nav route. */
    handoffModality: String?,
    onOpenSearch: () -> Unit,
    onOpenSettings: () -> Unit,
    onOpenApps: () -> Unit,
    onOpenSession: (String) -> Unit,
    onNewChat: () -> Unit,
) {
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()

    ModalNavigationDrawer(
        drawerState = drawerState,
        scrimColor = AuraColors.surfaceCanvas.copy(alpha = DRAWER_SCRIM_ALPHA),
        drawerContent = {
            ModalDrawerSheet(
                // material3 1.4.0's ModalDrawerSheet forwards drawerTonalElevation into an inner
                // Surface that never sets shadowElevation, so it casts no shadow by default
                // (byte-verified against NavigationDrawer.kt — see AuraSpacing.DrawerSheet's KDoc).
                // Modifier.shadow(...) reaches the same built-in Surface primitive one layer down.
                modifier = Modifier
                    .fillMaxWidth(DRAWER_WIDTH_FRACTION)
                    .shadow(elevation = AuraSpacing.DrawerSheet.shadowElevation, shape = RectangleShape, clip = false),
                drawerContainerColor = AuraColors.surfaceDrawer,
                drawerShape = RectangleShape,
            ) {
                AuraDrawerContent(
                    currentSessionId = sessionId,
                    isOpen = drawerState.currentValue == DrawerValue.Open,
                    onNewChat = {
                        scope.launch { drawerState.close() }
                        onNewChat()
                    },
                    onOpenSearch = {
                        scope.launch { drawerState.close() }
                        onOpenSearch()
                    },
                    onOpenSession = { id ->
                        scope.launch { drawerState.close() }
                        onOpenSession(id)
                    },
                    onOpenSettings = {
                        scope.launch { drawerState.close() }
                        onOpenSettings()
                    },
                    onOpenApps = {
                        scope.launch { drawerState.close() }
                        onOpenApps()
                    },
                )
            }
        },
    ) {
        val noticeController = LocalNoticeController.current
        Box(Modifier.fillMaxSize()) {
            ChatScreen(
                sessionId = sessionId,
                handoffModality = handoffModality,
                // Chat is the drawer's home destination now (spec §6.7) - no back target
                // (navigating between chats replaces this entry, never pushes a second one).
                onMenuTap = { scope.launch { drawerState.open() } },
                onNewChat = onNewChat,
                onNotice = noticeController::show,
                // The SAME lambda the drawer's session rows use - a fork lands in a brand-new
                // session and MessageActionsSheet navigates to it exactly the way opening one from
                // Recents does (replace this entry, never push a second chat onto the back stack).
                onOpenSession = onOpenSession,
            )
        }
    }
}
