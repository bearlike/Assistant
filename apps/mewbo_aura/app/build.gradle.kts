import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import java.util.Base64
import javax.inject.Inject

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
}

// The app version, named once. The release-asset file name below is derived from it, so a version
// bump renames the artifact automatically rather than by hand at release time.
val auraVersionName = "0.0.24.1"

// Where the in-app updater looks for releases, baked per distribution flavor (see the flavors
// below). The running app never decides this — it reads the constant its own build stamped in.
//
// `public` carries a tracked default because api.github.com is a public fact. The ENTERPRISE root
// names a private forge, so it may never appear in a tracked file (this repo is mirrored to a
// public GitHub); it arrives as an argument exactly the way the enterprise CA does, and resolves to
// empty when no source is available. Empty is not a silent fallback: `requireEnterpriseUpdateApiRoot`
// below fails an enterprise BUILD on it, and the app treats an empty root as "updates are not
// configured for this build" rather than as "up to date".
val publicUpdateApiRoot = "https://api.github.com/"
val enterpriseUpdateApiRootSource =
    (findProperty("mewbo.updateApiRoot") as String?)
        ?: System.getenv("AURA_UPDATE_API_ROOT")
        ?: file("${System.getProperty("user.home")}/temp_folder/aura-update-api-root.txt")
            .takeIf { it.isFile }?.readText()
val enterpriseUpdateApiRoot = enterpriseUpdateApiRootSource?.trim().orEmpty()

// Owner/repo are public slugs on both forges and are the same on each, so one tracked default
// serves both flavors. Overridable for a fork without touching the tree.
val updateRepoSlug = (findProperty("mewbo.updateRepo") as String?) ?: "bearlike/Assistant"
val updateRepoOwner = updateRepoSlug.substringBefore('/')
val updateRepoName = updateRepoSlug.substringAfter('/')

android {
    namespace = "com.mewbo.aura"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.mewbo.aura"
        minSdk = 30
        targetSdk = 36
        versionCode = 35
        versionName = auraVersionName

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        buildConfigField("String", "UPDATE_REPO_OWNER", "\"$updateRepoOwner\"")
        buildConfigField("String", "UPDATE_REPO_NAME", "\"$updateRepoName\"")
    }

    // Signing for BOTH build types: a configured keystore (AURA_KEYSTORE_B64 +
    // friends) wins; otherwise fall back to the auto-generated debug keystore so
    // an assemble always yields an installable APK with zero setup.
    //
    // The name says "release" for the build type it started on, but a PUBLISHED
    // Aura artifact is `enterpriseDebug` — so the debug build type has to reach
    // the same config or a keystore configured for release signs nothing that
    // ever ships. Android refuses to update an app across a signature change,
    // and the only way out of one is an uninstall that erases the user's data;
    // that made "which machine cut the release" a property of every install.
    // With the keystore configured, a build cut anywhere chains onto a build cut
    // anywhere else. With it unset the fallback below IS the same keystore,
    // alias and password AGP's built-in debug config uses, so a developer build
    // with no secrets is byte-for-byte what it was.
    signingConfigs {
        create("release") {
            val keystoreB64 = System.getenv("AURA_KEYSTORE_B64")
            val keystorePassword = System.getenv("AURA_KEYSTORE_PASSWORD")
            val configuredAlias = System.getenv("AURA_KEY_ALIAS")
            if (!keystoreB64.isNullOrBlank() && !keystorePassword.isNullOrBlank() && !configuredAlias.isNullOrBlank()) {
                val keystoreFile = layout.buildDirectory.file("aura-release.keystore").get().asFile
                keystoreFile.parentFile.mkdirs()
                keystoreFile.writeBytes(Base64.getDecoder().decode(keystoreB64))
                storeFile = keystoreFile
                storePassword = keystorePassword
                keyAlias = configuredAlias
                keyPassword = System.getenv("AURA_KEY_PASSWORD") ?: keystorePassword
            } else {
                // AGP only auto-generates its own built-in "debug" signing config;
                // a custom config pointed at the same path needs its own bootstrap.
                val debugKeystore = file("${System.getProperty("user.home")}/.android/debug.keystore")
                if (!debugKeystore.exists()) {
                    debugKeystore.parentFile.mkdirs()
                    ProcessBuilder(
                        "keytool", "-genkeypair", "-v",
                        "-keystore", debugKeystore.absolutePath,
                        "-storepass", "android", "-alias", "androiddebugkey", "-keypass", "android",
                        "-dname", "CN=Android Debug,O=Android,C=US",
                        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                    ).inheritIO().start().waitFor()
                }
                storeFile = debugKeystore
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            signingConfig = signingConfigs.getByName("release")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
        debug {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }

    // Distribution flavors (enterprise CA baking): `public` is the default and the ONLY flavor
    // CI builds + ships to GitHub — it carries platform-default trust (system CAs only, see
    // src/main/res/xml/network_security_config.xml). `enterprise` bakes a private, deployment-
    // supplied root CA as a Network Security Config trust anchor (src/enterprise/…) for LAN/enterprise
    // backends; that cert is gitignored + seeded at build time (see seedEnterpriseCa below) so it
    // never reaches the public mirror. NOTE: the umbrella `assembleDebug`/`assembleRelease`
    // build BOTH flavors — use the flavor-qualified task (assemblePublicDebug for public,
    // assembleEnterpriseDebug for enterprise dev) day to day.
    flavorDimensions += "distribution"
    productFlavors {
        create("public") {
            dimension = "distribution"
            isDefault = true
            buildConfigField("String", "UPDATE_API_ROOT", "\"$publicUpdateApiRoot\"")
        }
        create("enterprise") {
            dimension = "distribution"
            versionNameSuffix = "-enterprise"
            buildConfigField("String", "UPDATE_API_ROOT", "\"$enterpriseUpdateApiRoot\"")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        aidl = true // the UserService binder interface
        // The in-app updater's per-flavor release source. It is a BUILD fact, not a runtime one:
        // an APK must not be able to be pointed at a different forge after it ships, and baking the
        // value is also what keeps a private forge's hostname out of the tree. `FLAVOR`/`BUILD_TYPE`
        // come free with this flag and are what the release-asset matcher compares against.
        buildConfig = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }

    testOptions {
        unitTests {
            // Robolectric reads the merged manifest + resources through the
            // `com/android/tools/test_config.properties` this flag emits; without it a
            // Robolectric test loads a default manifest and no app resources, so anything
            // touching `R.` (AuraType's font family, for one) fails at runtime rather than
            // at compile time. Every OTHER suite in this module is plain-JVM and ignores it.
            isIncludeAndroidResources = true
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

// Enterprise CA seeding: the `enterprise` flavor bakes a private, deployment-supplied root CA
// (src/enterprise/res/raw/enterprise_ca.crt) as an NSC trust anchor. That cert is a public cert
// but is kept OUT of git (see .gitignore) so it never reaches the public GitHub mirror — not
// in source, not in any `public`-flavor APK. This task materialises it before an enterprise build
// from an external source; the enterprise* resource-merge depends on it, and it fails loudly when
// no source is available so an enterprise build can never silently ship without its anchor.
// `public*` builds never pull this into their task graph.
val enterpriseCaFile = layout.projectDirectory.file("src/enterprise/res/raw/enterprise_ca.crt")
val seedEnterpriseCa by tasks.registering {
    group = "build setup"
    description = "Materialise the gitignored enterprise root CA into src/enterprise/res/raw."
    val target = enterpriseCaFile.asFile
    val srcPath = (findProperty("mewbo.enterpriseCaSource") as String?)
        ?: System.getenv("AURA_ENTERPRISE_CA_SRC")
        ?: "${System.getProperty("user.home")}/temp_folder/enterprise-ca.crt"
    val b64 = System.getenv("AURA_ENTERPRISE_CA_B64")
    doLast {
        target.parentFile.mkdirs()
        when {
            !b64.isNullOrBlank() -> target.writeBytes(Base64.getDecoder().decode(b64))
            file(srcPath).exists() -> file(srcPath).copyTo(target, overwrite = true)
            target.exists() -> logger.lifecycle("seedEnterpriseCa: reusing existing ${target.name} (no source to refresh from)")
            else -> throw GradleException(
                "enterprise CA not found — the `enterprise` flavor needs a private root CA. Provide it via one of:\n" +
                    "  • ~/temp_folder/enterprise-ca.crt (default source), or\n" +
                    "  • -Pmewbo.enterpriseCaSource=<path>, or env AURA_ENTERPRISE_CA_SRC=<path>, or\n" +
                    "  • env AURA_ENTERPRISE_CA_B64=<base64 of the cert>.\n" +
                    "(`public` builds don't need this — run assemblePublicDebug / assemblePublicRelease instead.)",
            )
        }
    }
}
// The enterprise update root, guarded the same way and for the same reason as the CA above: a
// value that cannot live in the tree must fail the build that needs it rather than resolve to
// nothing. It is a separate task from seedEnterpriseCa because it materialises nothing — the value
// is already baked into BuildConfig at configuration time; this only refuses to let an enterprise
// APK ship with an empty one. The check CANNOT be a `throw` at configuration time: Gradle
// configures every variant, so an enterprise-only failure raised there would break
// `assemblePublicDebug` too.
val requireEnterpriseUpdateApiRoot by tasks.registering {
    group = "build setup"
    description = "Refuse an enterprise build with no in-app update API root."
    val resolved = enterpriseUpdateApiRoot
    doLast {
        if (resolved.isBlank()) {
            throw GradleException(
                "in-app update API root not found — the `enterprise` flavor needs the release API root of its " +
                    "private forge. Provide it via one of:\n" +
                    "  • ~/temp_folder/aura-update-api-root.txt (default source), or\n" +
                    "  • -Pmewbo.updateApiRoot=<url>, or env AURA_UPDATE_API_ROOT=<url>.\n" +
                    "It is the API ROOT with a trailing slash (…/api/v1/ for Gitea/Forgejo), not a repository URL.\n" +
                    "(`public` builds don't need this — they default to api.github.com.)",
            )
        }
    }
}
tasks.matching { it.name.startsWith("merge") && it.name.contains("Enterprise") && it.name.endsWith("Resources") }
    .configureEach { dependsOn(seedEnterpriseCa, requireEnterpriseUpdateApiRoot) }

// Widget-host assets: the offline stlite widget renderer is a self-contained web
// bundle the CONSOLE builds (apps/mewbo_console/dist/widget-host/ — a relocatable base:'./' build,
// vendored Pyodide included). Aura serves it from a WebView via WebViewAssetLoader, so it has to
// live in the APK's assets. Rather than check ~26 MB of Pyodide blobs into git (the console's own
// dist/ is gitignored for the same reason), this copies the built bundle into a build/-scoped
// generated dir (gitignored, never committed). Wired as a GENERATED asset source via the AGP Variant
// API below — one task PER VARIANT — which auto-declares the task dependency for EVERY consumer
// (merge/lint/package); a plain sourceSets.srcDir + manual dependsOn missed lint and tripped
// Gradle's implicit-dependency validation. `sourceFiles` (optional) tracks the console dist for
// up-to-date-ness while tolerating its absence at configuration time.
//
// Missing-dist behavior is VARIANT-SPLIT ([failIfMissing]): a RELEASE variant FAILS HARD (the flag
// defaults ON + advertises `stlite`, so a release with no bundle would ship broken widget cards to
// every user — the advertise-what-you-can't-service trap at the build level; mirrors seedEnterpriseCa's
// "never silently ship a broken build" posture). A DEBUG variant only WARNS, so dev iteration on a
// machine that hasn't built the console isn't blocked (the widget card would show its load-error state).
abstract class SyncWidgetHostAssets : DefaultTask() {
    @get:InputFiles
    @get:Optional
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val sourceFiles: ConfigurableFileCollection

    @get:Internal
    abstract val sourceDir: DirectoryProperty

    @get:Input
    abstract val failIfMissing: Property<Boolean>

    @get:OutputDirectory
    abstract val outputDir: DirectoryProperty

    @get:Inject
    abstract val fs: FileSystemOperations

    @TaskAction
    fun sync() {
        val src = sourceDir.get().asFile
        if (!src.isDirectory) {
            val message =
                "widget-host bundle not found at $src — the stlite widget renderer needs the " +
                    "console's built bundle. Build it first:\n" +
                    "  (cd ../../mewbo_console && npm run build)   # also emits dist/widget-host/\n" +
                    "It carries the offline stlite renderer + vendored Pyodide the widget WebView serves."
            if (failIfMissing.get()) throw GradleException(message)
            // Debug: don't block dev iteration — the card just shows its load-error state.
            logger.warn("syncWidgetHostAssets: $message\n(debug build — the widget renderer will be ABSENT from this APK.)")
            return
        }
        // Sync INTO a `widget-host/` subdir of the generated assets root, so the page resolves at
        // assets/widget-host/widget-host.html (the relocatable base the console build emits).
        fs.sync {
            from(src)
            into(outputDir.get().dir("widget-host"))
        }
    }
}
val widgetHostSourceDir = layout.projectDirectory.dir("../../mewbo_console/dist/widget-host")
androidComponents {
    onVariants { variant ->
        val isRelease = variant.buildType == "release"
        val syncTask = tasks.register<SyncWidgetHostAssets>(
            "syncWidgetHostAssets${variant.name.replaceFirstChar { it.uppercaseChar() }}",
        ) {
            group = "build setup"
            description = "Copy the console's built widget-host bundle into ${variant.name}'s assets."
            sourceDir.set(widgetHostSourceDir)
            sourceFiles.from(fileTree(widgetHostSourceDir)) // empty (not an error) when the dist is absent
            failIfMissing.set(isRelease) // release fails hard; debug only warns
            // outputDir is wired + located by AGP's addGeneratedSourceDirectory below (under build/, gitignored).
        }
        variant.sources.assets?.addGeneratedSourceDirectory(syncTask) { it.outputDir }

        // Deterministic release-asset name: `aura-<version>-<flavor>-<buildType>.apk`.
        //
        // The old name was `app-<flavor>-<buildType>.apk` — it carried no version and nothing
        // saying which product it belonged to, on a forge whose releases also carry the server's
        // own artifacts. The in-app updater has to pick the ONE asset that fits this device out of
        // a release's asset list, and it matches on the `-<flavor>-<buildType>.apk` SUFFIX, which
        // both the old and the new name satisfy — so already-published releases stay visible while
        // new ones also say what they are on disk. `data/update/CLAUDE.md` owns the scheme; keep
        // the two in step.
        //
        // Derived from `auraVersionName` rather than the variant's own versionName, because the
        // enterprise flavor appends `-enterprise` to that and the flavor is already its own segment.
        variant.outputs.forEach { output ->
            output.outputFileName.set("aura-$auraVersionName-${variant.flavorName}-${variant.buildType}.apk")
        }
    }
}

dependencies {
    // Compose
    implementation(platform(libs.compose.bom))
    implementation(libs.material3)
    implementation(libs.material.icons.core)
    implementation(libs.material.icons.extended)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.navigation.compose)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // Hilt / DI
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.hilt.navigation.compose)
    // dagger 2.60 codegen references these but its POM omits the dependency
    implementation(libs.error.prone.annotations)

    // Serialization
    implementation(libs.kotlinx.serialization.json)

    // Networking
    implementation(libs.okhttp)
    implementation(libs.okhttp.sse)
    implementation(libs.retrofit)
    implementation(libs.retrofit.converter.kotlinx.serialization)

    // Markdown rendering
    implementation(libs.markdown.renderer.m3)
    implementation(libs.markdown.renderer.code)
    implementation(libs.markdown.renderer.coil3)

    // Images
    implementation(libs.coil.compose)
    implementation(libs.coil.network.okhttp)

    // Settings / storage
    implementation(libs.datastore.preferences)

    // Device control at shell UID
    implementation(libs.shizuku.api)
    implementation(libs.shizuku.provider)

    // WebView asset loading — the offline stlite widget host
    implementation(libs.androidx.webkit)

    // Test
    testImplementation(libs.junit4)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.turbine)
    // android.net.Uri's real methods throw ("not mocked") under the plain-JVM android.jar stub
    // (no Robolectric in this module) - mockito-core mocks it instead, needed by
    // StagedAttachmentsReducerTest.
    testImplementation("org.mockito:mockito-core:5.14.2")
    // The one dependency with a WindowManager behind it. Kept to the overlay-lifecycle suite on
    // purpose: a Robolectric class pays a real per-class setup cost, so the plain-JVM idioms in
    // src/test/.../CLAUDE.md stay the default and this is the exception for code whose whole
    // behaviour IS adding and removing a window.
    testImplementation(libs.robolectric)
    // Compose semantics assertions on the JVM, under the same Robolectric runner. Version-less:
    // both come from the compose BOM, which has to be applied to these configurations too - the
    // `implementation(platform(...))` above constrains only its own configuration, so without
    // these two lines the artifacts resolve with no version at all.
    testImplementation(platform(libs.compose.bom))
    testImplementation("androidx.compose.ui:ui-test-junit4")
    // Adds the `ComponentActivity` entry that `createComposeRule()` launches into. It is a
    // MANIFEST contribution, not a classpath one, which is why it is `debugImplementation` and not
    // `testImplementation` - the unit test runs against the debug variant's merged manifest.
    debugImplementation(platform(libs.compose.bom))
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
