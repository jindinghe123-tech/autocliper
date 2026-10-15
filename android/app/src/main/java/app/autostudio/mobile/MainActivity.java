package app.autostudio.mobile;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.graphics.Insets;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * AutoStudio for phones. Everything runs on the PC: this app is a window onto the PC's own UI.
 *
 * Two WebViews share the screen. The connect screen (assets/connect.html) is local and talks to Java
 * through {@link Bridge}: it scans the pairing QR code, pairs with a PC, remembers paired PCs and finds
 * them again on the network. The PC view shows http://PC:8766/ (the same pages as on the desktop),
 * authenticated by the device token cookie, and never gets the bridge.
 */
public class MainActivity extends Activity {
    private static final String CONNECT_URL = "file:///android_asset/connect.html";
    private static final String COOKIE = "as_device";
    private static final int REQ_FILES = 21;
    private static final int REQ_STORAGE = 22;
    private static final int BG = 0xFF0D0E12;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService pool = Executors.newCachedThreadPool();

    private FrameLayout root;
    private WebView connectView;
    private WebView pcView;
    private String pcBase;      // e.g. http://192.168.1.5:8766
    private String pcId = "";
    private String pcName = "";
    private boolean pcShown;
    private boolean pcStale = true;     // the PC page must be reloaded next time it is shown

    private ValueCallback<Uri[]> fileCallback;
    private View fullscreenView;
    private WebChromeClient.CustomViewCallback fullscreenCallback;
    private Runnable afterStoragePermission;
    private boolean clearHistoryOnLoad;

    // ------------------------------------------------------------------ lifecycle

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        root = new FrameLayout(this);
        root.setBackgroundColor(BG);
        setContentView(root);
        fitSystemBars();

        CookieManager.getInstance().setAcceptCookie(true);
        connectView = newWebView();
        pcView = newWebView();
        CookieManager.getInstance().setAcceptThirdPartyCookies(pcView, false);
        root.addView(pcView, match());
        root.addView(connectView, match());
        pcView.setVisibility(View.GONE);
        setupConnectView();
        setupPcView();
        connectView.loadUrl(CONNECT_URL + "?auto=1");
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (pcShown && pcBase != null) checkPcStillThere();
    }

    @Override
    protected void onDestroy() {
        pool.shutdownNow();
        connectView.destroy();
        pcView.destroy();
        super.onDestroy();
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        if (fullscreenView != null) {
            hideFullscreen();
            return;
        }
        if (!pcShown) {
            super.onBackPressed();
            return;
        }
        // let the page close its drawer / dialog first, then walk back through its history
        pcView.evaluateJavascript(
                "(function(){var s=document.getElementById('sidebar');"
                        + "if(s&&s.classList.contains('open')){s.classList.remove('open');return 1}"
                        + "if(document.querySelector('.modal-bg')){document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));return 1}"
                        + "return 0})()",
                handled -> {
                    if ("1".equals(handled)) return;
                    if (pcView.canGoBack()) pcView.goBack();
                    else moveTaskToBack(true);
                });
    }

    // ------------------------------------------------------------------ layout

    private static FrameLayout.LayoutParams match() {
        return new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
    }

    /** Draw edge to edge and keep the pages clear of the status bar, navigation bar and keyboard. */
    private void fitSystemBars() {
        if (Build.VERSION.SDK_INT < 30) return;     // older versions resize the window themselves
        getWindow().setDecorFitsSystemWindows(false);
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            Insets ime = insets.getInsets(WindowInsets.Type.ime());
            if (fullscreenView != null) v.setPadding(0, 0, 0, 0);
            else v.setPadding(bars.left, bars.top, bars.right, Math.max(bars.bottom, ime.bottom));
            return WindowInsets.CONSUMED;
        });
    }

    @SuppressLint("SetJavaScriptEnabled")
    private WebView newWebView() {
        WebView w = new WebView(this);
        w.setBackgroundColor(BG);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setUserAgentString(s.getUserAgentString() + " AutoStudioApp/" + BuildConfig.VERSION_NAME);
        return w;
    }

    // ------------------------------------------------------------------ connect screen

    @SuppressLint("AddJavascriptInterface")
    private void setupConnectView() {
        connectView.addJavascriptInterface(new Bridge(), "AutoStudio");
        connectView.setWebChromeClient(new WebChromeClient());     // enables confirm() dialogs
        connectView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                if ("file".equals(u.getScheme())) return false;
                openExternally(u);      // help links etc.
                return true;
            }
        });
    }

    /** Show the connect screen; `event` (JSON) tells it why, e.g. the PC went away. */
    private void showConnect(JSONObject event) {
        hideFullscreen();
        pcShown = false;
        pcStale = event == null || !"home".equals(event.optString("type"));
        connectView.setVisibility(View.VISIBLE);
        pcView.setVisibility(View.GONE);
        pcView.stopLoading();
        if (event != null) connectView.evaluateJavascript("window.onAppEvent && window.onAppEvent(" + event + ")", null);
    }

    private JSONObject event(String type, String message) {
        JSONObject o = new JSONObject();
        try {
            o.put("type", type).put("pc_id", pcId).put("pc_name", pcName).put("message", message == null ? "" : message);
        } catch (JSONException ignored) {
        }
        return o;
    }

    private void callJs(String id, JSONObject payload) {
        ui.post(() -> connectView.evaluateJavascript(
                "window.__native && window.__native(" + JSONObject.quote(id) + "," + payload + ")", null));
    }

    /** What the connect page can ask of Java. Only the local connect page has this object. */
    private class Bridge {
        @JavascriptInterface
        public String getPcs() {
            return prefs().getString("pcs", "[]");
        }

        @JavascriptInterface
        public void savePcs(String json) {
            prefs().edit().putString("pcs", json).apply();
        }

        @JavascriptInterface
        public String deviceName() {
            String maker = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER;
            String model = Build.MODEL == null ? "Android" : Build.MODEL;
            String name = model.toLowerCase().startsWith(maker.toLowerCase()) ? model : maker + " " + model;
            return name.trim();
        }

        @JavascriptInterface
        public String version() {
            return BuildConfig.VERSION_NAME;
        }

        /** HTTP request to a PC; answers {status, data|text, error}. */
        @JavascriptInterface
        public void request(String id, String method, String url, String body, String token, int timeoutMs) {
            pool.execute(() -> callJs(id, http(method, url, body, token, timeoutMs)));
        }

        /** Look for AutoStudio PCs on this phone's local networks; answers {pcs: [...]}. */
        @JavascriptInterface
        public void discover(String id, String portsCsv) {
            pool.execute(() -> callJs(id, discoverPcs(portsCsv)));
        }

        @JavascriptInterface
        public void scan() {
            ui.post(() -> {
                IntentIntegrator scan = new IntentIntegrator(MainActivity.this);
                scan.setDesiredBarcodeFormats(IntentIntegrator.QR_CODE);
                scan.setPrompt("扫描电脑上「手机连接」页面的二维码");
                scan.setBeepEnabled(false);
                scan.setOrientationLocked(true);
                scan.initiateScan();
            });
        }

        @JavascriptInterface
        public void open(String base, String token, String id, String name) {
            ui.post(() -> openPc(base, token, id, name));
        }

        @JavascriptInterface
        public void openUrl(String url) {
            ui.post(() -> openExternally(Uri.parse(url)));
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences("autostudio", MODE_PRIVATE);
    }

    // ------------------------------------------------------------------ PC view

    private void setupPcView() {
        pcView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                if ("autostudio".equals(u.getScheme())) {
                    // autostudio://home = switch PC, autostudio://disconnected = this phone was unpaired
                    showConnect(event("disconnected".equals(u.getHost()) ? "disconnected" : "home", null));
                    return true;
                }
                if (isPcUrl(u)) {
                    if ("/pair".equals(u.getPath())) {     // the PC no longer accepts our token
                        showConnect(event("revoked", null));
                        return true;
                    }
                    return false;
                }
                openExternally(u);
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                Uri u = Uri.parse(url);
                if (isPcUrl(u) && "/pair".equals(u.getPath()) && pcShown) {
                    // the PC sent us to its pairing page: our token is no longer valid
                    view.stopLoading();
                    showConnect(event("revoked", null));
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (clearHistoryOnLoad) {     // Back should not lead to a previous PC or session
                    clearHistoryOnLoad = false;
                    view.clearHistory();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame() && pcShown) {
                    showConnect(event("unreachable", String.valueOf(error.getDescription())));
                }
            }
        });
        pcView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                Intent pick = params.createIntent();
                if (params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    startActivityForResult(Intent.createChooser(pick, "选择要发给电脑的文件"), REQ_FILES);
                    return true;
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
            }

            @Override
            public void onShowCustomView(View view, CustomViewCallback callback) {
                showFullscreen(view, callback);
            }

            @Override
            public void onHideCustomView() {
                hideFullscreen();
            }
        });
        pcView.setDownloadListener((url, userAgent, disposition, mime, length) -> saveToPhone(url, userAgent, disposition, mime));
    }

    private boolean isPcUrl(Uri u) {
        if (pcBase == null) return false;
        Uri b = Uri.parse(pcBase);
        return b.getHost() != null && b.getHost().equals(u.getHost()) && b.getPort() == u.getPort();
    }

    private void openPc(String base, String token, String id, String name) {
        pcBase = base.replaceAll("/+$", "");
        pcId = id == null ? "" : id;
        pcName = name == null ? "" : name;
        CookieManager cm = CookieManager.getInstance();
        // Lax rather than Strict: WebView.loadUrl must send it, and this WebView only ever shows the PC
        cm.setCookie(pcBase, COOKIE + "=" + token + "; Path=/; Max-Age=34560000; HttpOnly; SameSite=Lax");
        cm.flush();
        pcShown = true;
        pcView.setVisibility(View.VISIBLE);
        connectView.setVisibility(View.GONE);
        String current = pcView.getUrl();
        if (pcStale || current == null || !isPcUrl(Uri.parse(current))) {
            pcView.loadUrl(pcBase + "/");
            clearHistoryOnLoad = true;
        }
        pcStale = false;
    }

    /** Back from the background: is the PC still reachable, and is this phone still paired? */
    private void checkPcStillThere() {
        String base = pcBase;
        pool.execute(() -> {
            String cookie = CookieManager.getInstance().getCookie(base);
            String token = null;
            if (cookie != null) {
                Matcher m = Pattern.compile(COOKIE + "=([^;]+)").matcher(cookie);
                if (m.find()) token = m.group(1);
            }
            JSONObject r = http("GET", base + "/api/remote/whoami", null, token, 3000);
            int status = r.optInt("status");
            ui.post(() -> {
                if (!pcShown || !base.equals(pcBase)) return;
                if (status == 401) showConnect(event("revoked", null));
                else if (status == 0) showConnect(event("unreachable", r.optString("error")));
            });
        });
    }

    private void openExternally(Uri u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));
        } catch (ActivityNotFoundException e) {
            toast("没有可以打开这个链接的应用");
        }
    }

    // ------------------------------------------------------------------ fullscreen video

    private void showFullscreen(View view, WebChromeClient.CustomViewCallback callback) {
        if (fullscreenView != null) {
            callback.onCustomViewHidden();
            return;
        }
        fullscreenView = view;
        fullscreenCallback = callback;
        view.setBackgroundColor(0xFF000000);
        root.addView(view, match());
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_FULL_SENSOR);
        setSystemBarsHidden(true);
    }

    private void hideFullscreen() {
        if (fullscreenView == null) return;
        root.removeView(fullscreenView);
        fullscreenView = null;
        if (fullscreenCallback != null) fullscreenCallback.onCustomViewHidden();
        fullscreenCallback = null;
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
        setSystemBarsHidden(false);
    }

    @SuppressWarnings("deprecation")
    private void setSystemBarsHidden(boolean hidden) {
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController c = getWindow().getInsetsController();
            if (c == null) return;
            if (hidden) {
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                c.hide(WindowInsets.Type.systemBars());
            } else {
                c.show(WindowInsets.Type.systemBars());
            }
            root.requestApplyInsets();
        } else {
            getWindow().getDecorView().setSystemUiVisibility(hidden
                    ? View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    : 0);
        }
    }

    // ------------------------------------------------------------------ files & downloads

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILES) {
            Uri[] picked = null;
            if (resultCode == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    picked = new Uri[data.getClipData().getItemCount()];
                    for (int i = 0; i < picked.length; i++) picked[i] = data.getClipData().getItemAt(i).getUri();
                } else if (data.getData() != null) {
                    picked = new Uri[]{data.getData()};
                }
            }
            if (fileCallback != null) fileCallback.onReceiveValue(picked);
            fileCallback = null;
            return;
        }
        IntentResult scan = IntentIntegrator.parseActivityResult(requestCode, resultCode, data);
        if (scan != null) {
            JSONObject o = new JSONObject();
            try {
                o.put("text", scan.getContents() == null ? JSONObject.NULL : scan.getContents());
            } catch (JSONException ignored) {
            }
            connectView.evaluateJavascript("window.onScan && window.onScan(" + o + ")", null);
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        if (requestCode == REQ_STORAGE) {
            Runnable next = afterStoragePermission;
            afterStoragePermission = null;
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED && next != null) next.run();
            else toast("没有存储权限，没法把视频存到手机");
        }
    }

    /** "保存到手机": let Android's download manager fetch the file from the PC into the gallery. */
    private void saveToPhone(String url, String userAgent, String disposition, String mime) {
        if (Build.VERSION.SDK_INT < 29
                && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            afterStoragePermission = () -> saveToPhone(url, userAgent, disposition, mime);
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_STORAGE);
            return;
        }
        String type = mime == null ? "" : mime;
        String name = fileName(url, disposition, type);
        String dir = type.startsWith("video/") ? Environment.DIRECTORY_MOVIES
                : type.startsWith("image/") ? Environment.DIRECTORY_PICTURES
                : type.startsWith("audio/") ? Environment.DIRECTORY_MUSIC
                : Environment.DIRECTORY_DOWNLOADS;
        try {
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) r.addRequestHeader("Cookie", cookie);
            r.addRequestHeader("User-Agent", userAgent);
            if (!type.isEmpty()) r.setMimeType(type);
            r.setTitle(name);
            r.setDescription("AutoStudio · 从电脑保存");
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setDestinationInExternalPublicDir(dir, "AutoStudio/" + name);
            ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(r);
            toast("正在保存到手机：" + name + "（在通知栏查看进度）");
        } catch (Exception e) {
            toast("保存失败：" + e.getMessage());
        }
    }

    private static String fileName(String url, String disposition, String mime) {
        String name = null;
        if (disposition != null) {
            Matcher m = Pattern.compile("filename\\*=(?:UTF-8|utf-8)''([^;]+)").matcher(disposition);
            if (m.find()) {
                try {
                    name = URLDecoder.decode(m.group(1).trim(), "UTF-8");
                } catch (Exception ignored) {
                }
            }
        }
        if (name == null || name.isEmpty()) name = URLUtil.guessFileName(url, disposition, mime);
        return name.replaceAll("[\\\\/:*?\"<>|]+", "_");
    }

    private void toast(String msg) {
        ui.post(() -> Toast.makeText(this, msg, Toast.LENGTH_LONG).show());
    }

    // ------------------------------------------------------------------ networking

    /** Blocking HTTP call. Returns {status, data (parsed JSON) or text, error}; status 0 = unreachable. */
    private static JSONObject http(String method, String url, String body, String token, int timeoutMs) {
        JSONObject out = new JSONObject();
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(Math.max(timeoutMs, 4000));
            c.setRequestMethod(method);
            c.setUseCaches(false);
            c.setRequestProperty("X-AutoStudio", "1");
            c.setRequestProperty("User-Agent", "AutoStudioApp/" + BuildConfig.VERSION_NAME + " (Android " + Build.VERSION.RELEASE + "; " + Build.MODEL + ")");
            if (token != null && !token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            if (body != null && !body.isEmpty()) {
                byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                c.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream os = c.getOutputStream()) {
                    os.write(bytes);
                }
            }
            int status = c.getResponseCode();
            InputStream in = status >= 400 ? c.getErrorStream() : c.getInputStream();
            String text = in == null ? "" : readAll(in);
            out.put("status", status);
            try {
                out.put("data", text.trim().startsWith("[") ? new JSONArray(text) : new JSONObject(text));
            } catch (JSONException notJson) {
                out.put("text", text.length() > 2000 ? text.substring(0, 2000) : text);
            }
        } catch (Exception e) {
            try {
                out.put("status", 0);
                out.put("error", e.getClass().getSimpleName() + (e.getMessage() == null ? "" : ": " + e.getMessage()));
            } catch (JSONException ignored) {
            }
        } finally {
            if (c != null) c.disconnect();
        }
        return out;
    }

    private static String readAll(InputStream in) throws IOException {
        try (InputStream s = in; ByteArrayOutputStream buf = new ByteArrayOutputStream()) {
            byte[] chunk = new byte[8192];
            int n;
            while ((n = s.read(chunk)) > 0 && buf.size() < 4 << 20) buf.write(chunk, 0, n);
            return buf.toString("UTF-8");
        }
    }

    /**
     * Sweep this phone's local /24 networks for AutoStudio phone listeners: a quick TCP connect on
     * each address, then GET /api/remote/hello on the ones that answer.
     */
    private static JSONObject discoverPcs(String portsCsv) {
        Set<Integer> ports = new LinkedHashSet<>();
        for (String p : (portsCsv == null ? "" : portsCsv).split(",")) {
            try {
                int v = Integer.parseInt(p.trim());
                if (v > 0 && v < 65536) ports.add(v);
            } catch (NumberFormatException ignored) {
            }
        }
        if (ports.isEmpty()) ports.add(8766);
        List<String> prefixes = new ArrayList<>();
        Set<String> self = new LinkedHashSet<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (!(a instanceof Inet4Address) || !a.isSiteLocalAddress()) continue;
                    String ip = a.getHostAddress();
                    self.add(ip);
                    String prefix = ip.substring(0, ip.lastIndexOf('.') + 1);
                    if (!prefixes.contains(prefix)) prefixes.add(prefix);
                }
            }
        } catch (Exception ignored) {
        }
        JSONArray found = new JSONArray();
        ExecutorService sweep = Executors.newFixedThreadPool(64);
        for (String prefix : prefixes) {
            for (int i = 1; i < 255; i++) {
                String host = prefix + i;
                if (self.contains(host)) continue;
                for (int port : ports) {
                    sweep.execute(() -> {
                        try (Socket s = new Socket()) {
                            s.connect(new InetSocketAddress(host, port), 400);
                        } catch (IOException closed) {
                            return;
                        }
                        String base = "http://" + host + ":" + port;
                        JSONObject r = http("GET", base + "/api/remote/hello", null, null, 1500);
                        JSONObject d = r.optJSONObject("data");
                        if (r.optInt("status") == 200 && d != null && "AutoStudio".equals(d.optString("app"))) {
                            try {
                                d.put("base", base);
                                synchronized (found) {
                                    found.put(d);
                                }
                            } catch (JSONException ignored) {
                            }
                        }
                    });
                }
            }
        }
        sweep.shutdown();
        try {
            sweep.awaitTermination(20, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
        }
        sweep.shutdownNow();
        JSONObject out = new JSONObject();
        try {
            out.put("pcs", found);
            out.put("networks", new JSONArray(prefixes));
        } catch (JSONException ignored) {
        }
        return out;
    }
}
