# The connect page talks to Java through @JavascriptInterface methods; keep their names.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
# zxing-android-embedded ships an optional androidx.activity entry point (ScanContract) we don't use.
# Keep this narrow: a blanket -dontwarn androidx.** once hid that androidx.core was missing, and the
# scanner crashed the app the moment it opened.
-dontwarn androidx.activity.**
