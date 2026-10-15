# The connect page talks to Java through @JavascriptInterface methods; keep their names.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
# zxing-android-embedded ships optional androidx entry points (ScanContract) we don't use.
-dontwarn androidx.**
