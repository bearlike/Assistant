# Add project specific ProGuard rules here.
# You can control the set of applied configuration files using the
# proguardFiles setting in build.gradle.kts.

# kotlinx.serialization
# https://github.com/Kotlin/kotlinx.serialization/blob/master/rules/common.pro
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt

-keepclassmembers class kotlinx.serialization.json.** {
    *** Companion;
}
-keepclasseswithmembers class kotlinx.serialization.json.** {
    kotlinx.serialization.KSerializer serializer(...);
}

# Keep `Companion` object fields of serializable classes so that they can be found via
# reflection. The `Companion` object is used at runtime to call the `serializer()` method.
-if @kotlinx.serialization.Serializable class **
-keepclassmembers class <1> {
    static <1>$Companion Companion;
}

# Keep `serializer()` on companion objects of serializable classes.
-if @kotlinx.serialization.Serializable class ** {
    static **$Companion Companion;
}
-keepclassmembers class <1>$Companion {
    kotlinx.serialization.KSerializer serializer(...);
}

# Keep the fields of serializable classes so serialization can access them at runtime.
-keepclassmembers @kotlinx.serialization.Serializable class ** {
    <fields>;
}

# Hilt's generated bindings reference error-prone's compile-time-only
# annotations, which aren't shipped as a runtime dependency; R8 only needs
# to know it's safe to skip them.
-dontwarn com.google.errorprone.annotations.**
