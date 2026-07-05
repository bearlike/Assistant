// Top-level build file where common configuration options for all sub-projects/modules can be added.
//
// AGP 9's built-in Kotlin support means org.jetbrains.kotlin.android is no longer applied
// (AGP now hosts the Kotlin Android compilation itself). The buildscript classpath override
// below pins the Kotlin compiler AGP uses internally to the same version as the
// kotlin.plugin.compose / kotlin.plugin.serialization plugins applied in :app, so every
// Kotlin-compiler-dependent plugin in this project agrees on one Kotlin version.
buildscript {
    dependencies {
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:${libs.versions.kotlin.get()}")
    }
}

plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.ksp) apply false
    alias(libs.plugins.hilt) apply false
}
