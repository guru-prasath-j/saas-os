plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// ---------------------------------------------------------------------------
// Meta Wearables Device Access Toolkit (DAT) — glasses live capture.
// OFF by default: the SDK lives on GitHub Packages and needs a personal
// access token (read:packages), so normal builds must not depend on it.
// Enable with -PmetaDat.enabled=true (or metaDat.enabled=true in
// gradle.properties) plus GITHUB_TOKEN or metaDat.githubToken set.
// When disabled, a stub MetaGlassesBridge (src/datstub) compiles instead and
// the Dart layer sees reason=sdk_not_bundled. See flutter_app/META_GLASSES.md.
// ---------------------------------------------------------------------------
val metaDatEnabled = (project.findProperty("metaDat.enabled") as String?)?.toBoolean() ?: false

if (metaDatEnabled) {
    repositories {
        maven {
            url = uri("https://maven.pkg.github.com/facebook/meta-wearables-dat-android")
            credentials {
                username = (project.findProperty("metaDat.githubUser") as String?) ?: ""
                password = System.getenv("GITHUB_TOKEN")
                    ?: (project.findProperty("metaDat.githubToken") as String?) ?: ""
            }
        }
    }
}

android {
    namespace = "com.personalos.amy_app"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_17.toString()
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.personalos.amy_app"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName

        // DAT manifest metadata. "0" is the documented development default;
        // real ids come from the Meta Wearables developer center for
        // internal-release builds.
        manifestPlaceholders["mwdat_application_id"] =
            (project.findProperty("metaDat.applicationId") as String?) ?: "0"
        manifestPlaceholders["mwdat_client_token"] =
            (project.findProperty("metaDat.clientToken") as String?) ?: "0"
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    // Exactly one MetaGlassesBridge implementation is compiled: the real DAT
    // one when metaDat.enabled=true, the no-op stub otherwise. Same class
    // name + channel contract either way, so Dart never branches.
    sourceSets {
        getByName("main") {
            java.srcDirs(if (metaDatEnabled) "src/dat/kotlin" else "src/datstub/kotlin")
        }
    }
}

dependencies {
    if (metaDatEnabled) {
        implementation("com.meta.wearable:mwdat-core:0.8.0")
        implementation("com.meta.wearable:mwdat-camera:0.8.0")
        // Mock Device Kit: test without physical glasses (dev/CI).
        implementation("com.meta.wearable:mwdat-mockdevice:0.8.0")
    }
}

flutter {
    source = "../.."
}
