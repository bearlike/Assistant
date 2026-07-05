import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import java.util.Base64

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
}

android {
    namespace = "com.mewbo.aura"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.mewbo.aura"
        minSdk = 33
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    // Release signing: a configured keystore (AURA_KEYSTORE_B64 + friends) wins;
    // otherwise fall back to the auto-generated debug keystore so
    // `assembleRelease` always yields an installable APK with zero setup.
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
        }
        create("enterprise") {
            dimension = "distribution"
            versionNameSuffix = "-enterprise"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
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
tasks.matching { it.name.startsWith("merge") && it.name.contains("Enterprise") && it.name.endsWith("Resources") }
    .configureEach { dependsOn(seedEnterpriseCa) }

dependencies {
    // Compose
    implementation(platform(libs.compose.bom))
    implementation(libs.material3)
    implementation(libs.material.icons.core)
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

    // Test
    testImplementation(libs.junit4)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.turbine)
    // android.net.Uri's real methods throw ("not mocked") under the plain-JVM android.jar stub
    // (no Robolectric in this module) - mockito-core mocks it instead, needed by
    // StagedAttachmentsReducerTest (Gitea #177 W2).
    testImplementation("org.mockito:mockito-core:5.14.2")
}
