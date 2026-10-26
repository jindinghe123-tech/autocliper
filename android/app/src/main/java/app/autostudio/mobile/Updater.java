package app.autostudio.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInstaller;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.LinkedHashSet;
import java.util.Set;

/**
 * Keeps this app up to date without the user downloading anything.
 *
 * The PC carries the newest APK (it gets it with its own automatic updates) and tells paired phones its
 * version at /api/remote/app-update; away from home the website's update.json says the same. A newer APK is
 * downloaded, checked against its SHA-256, and handed to Android's package installer as an update of this
 * very app. From Android 12 on, once the user has allowed AutoStudio to install apps (Android asks the first
 * time), that happens silently; otherwise a notification asks for one tap.
 */
public final class Updater {
    static final String ACTION_INSTALLED = "app.autostudio.mobile.INSTALL_STATUS";
    private static final long CHECK_EVERY_MS = 6 * 3600_000L;
    private static final String CH_UPDATE = "update";
    private static final int NOTIFY_ID = 7300;
    private static volatile boolean running;

    private Updater() {
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences("update", Context.MODE_PRIVATE);
    }

    /** Check now if it has been a while (or force); blocking — call from a background thread. */
    static void check(Context ctx, boolean force) {
        if (running || NotifyJob.pairedPcs(ctx).isEmpty()) return;     // nothing to ask yet (not paired)
        long last = prefs(ctx).getLong("checked", 0);
        if (!force && System.currentTimeMillis() - last < CHECK_EVERY_MS) return;
        running = true;
        try {
            prefs(ctx).edit().putLong("checked", System.currentTimeMillis()).apply();
            JSONObject found = find(ctx);
            if (found == null || found.optInt("version_code") <= BuildConfig.VERSION_CODE) return;
            File apk = download(ctx, found);
            if (apk != null) install(ctx, apk, found.optString("version_name"));
        } catch (Exception ignored) {
        } finally {
            running = false;
        }
    }

    /** Back in the app after allowing installs in Settings: install the waiting update now. */
    static void resumed(Context ctx) {
        boolean waiting = prefs(ctx).getBoolean("awaiting_permission", false);
        boolean allowed = Build.VERSION.SDK_INT < 26 || ctx.getPackageManager().canRequestPackageInstalls();
        if (waiting && allowed) {
            prefs(ctx).edit().putBoolean("awaiting_permission", false).apply();
            checkAsync(ctx, true);
        } else {
            checkAsync(ctx, false);
        }
    }

    static void checkAsync(Context ctx, boolean force) {
        Context app = ctx.getApplicationContext();
        new Thread(() -> check(app, force), "autostudio-update").start();
    }

    /** The newest app version a paired PC (or the website) offers: {version_code, version_name, sha256, url}. */
    private static JSONObject find(Context ctx) {
        JSONObject best = null;
        for (JSONObject pc : NotifyJob.pairedPcs(ctx)) {
            Set<String> bases = new LinkedHashSet<>();
            if (!pc.optString("base").isEmpty()) bases.add(pc.optString("base"));
            JSONArray more = pc.optJSONArray("addresses");
            for (int i = 0; more != null && i < more.length(); i++) bases.add(more.optString(i));
            for (String b : bases) {
                JSONObject r = MainActivity.http("GET", b + "/api/remote/app-update", null, pc.optString("token"), 2500, 6000);
                if (r.optInt("status") == 0) continue;
                JSONObject d = r.optJSONObject("data");
                if (r.optInt("status") == 200 && d != null && d.optInt("version_code") > 0) {
                    try {
                        d.put("url", b + d.optString("url")).put("token", pc.optString("token"));
                    } catch (Exception ignored) {
                    }
                    if (best == null || d.optInt("version_code") > best.optInt("version_code")) best = d;
                }
                break;
            }
            JSONObject relay = Relay.of(ctx, pc.optString("pc_id"));
            if (best == null && relay != null) {        // away from home: the website knows too
                String site = relay.optString("url").replaceAll("/+$", "");
                JSONObject r = MainActivity.http("GET", site + "/update.json", null, null, 6000, 10000);
                JSONObject d = r.optJSONObject("data");
                JSONObject apk = d == null ? null : d.optJSONObject("apk");
                if (apk != null && apk.optInt("version_code") > 0) {
                    try {
                        apk.put("url", site + apk.optString("url"));
                    } catch (Exception ignored) {
                    }
                    best = apk;
                }
            }
        }
        return best;
    }

    private static File download(Context ctx, JSONObject info) throws Exception {
        File dir = new File(ctx.getCacheDir(), "update");
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        File out = new File(dir, "AutoStudio-" + info.optInt("version_code") + ".apk");
        String want = info.optString("sha256").toLowerCase();
        if (out.isFile() && want.equals(sha256(out))) return out;
        HttpURLConnection c = (HttpURLConnection) new URL(info.optString("url")).openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(30000);
        if (!info.optString("token").isEmpty()) c.setRequestProperty("Authorization", "Bearer " + info.optString("token"));
        c.setRequestProperty("User-Agent", "AutoStudioApp/" + BuildConfig.VERSION_NAME);
        try (InputStream in = c.getInputStream(); OutputStream o = new FileOutputStream(out)) {
            byte[] buf = new byte[1 << 16];
            int n;
            long total = 0;
            while ((n = in.read(buf)) > 0) {
                o.write(buf, 0, n);
                total += n;
                if (total > 200L << 20) throw new IllegalStateException("too big");
            }
        } finally {
            c.disconnect();
        }
        if (!want.isEmpty() && !want.equals(sha256(out))) {
            //noinspection ResultOfMethodCallIgnored
            out.delete();
            return null;
        }
        return out;
    }

    private static String sha256(File f) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (InputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static void install(Context ctx, File apk, String versionName) throws Exception {
        PackageInstaller pi = ctx.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(ctx.getPackageName());
        if (Build.VERSION.SDK_INT >= 31) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        int id = pi.createSession(params);
        try (PackageInstaller.Session s = pi.openSession(id)) {
            try (OutputStream out = s.openWrite("base.apk", 0, apk.length()); InputStream in = new FileInputStream(apk)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                s.fsync(out);
            }
            Intent done = new Intent(ctx, InstallReceiver.class).setAction(ACTION_INSTALLED).putExtra("version", versionName);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
            s.commit(PendingIntent.getBroadcast(ctx, id, done, flags).getIntentSender());
        }
    }

    /** Android's answer to an install: done, or the user has to confirm (first time / older Android). */
    public static class InstallReceiver extends BroadcastReceiver {
        @Override
        @SuppressWarnings("deprecation")
        public void onReceive(Context ctx, Intent intent) {
            int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
            if (status != PackageInstaller.STATUS_PENDING_USER_ACTION) return;      // success restarts the app
            Intent confirm = Build.VERSION.SDK_INT >= 33 ? intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class)
                    : intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm == null) return;
            confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            // first time: Android asks to allow installs from AutoStudio (in Settings); try again as soon as that is done
            prefs(ctx).edit().putBoolean("awaiting_permission", true).apply();
            if (MainActivity.inFront()) {
                try {
                    ctx.startActivity(confirm);
                    return;
                } catch (Exception ignored) {
                }
            }
            NotificationManager nm = ctx.getSystemService(NotificationManager.class);
            if (nm == null || !NotifyJob.allowed(ctx)) return;
            if (Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(new NotificationChannel(CH_UPDATE, "App 更新", NotificationManager.IMPORTANCE_DEFAULT));
            }
            PendingIntent tap = PendingIntent.getActivity(ctx, NOTIFY_ID, confirm, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            String v = intent.getStringExtra("version");
            Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(ctx, CH_UPDATE) : new Notification.Builder(ctx);
            b.setSmallIcon(R.drawable.ic_notify).setColor(0xFF8B5CF6)
                    .setContentTitle("AutoStudio 有新版本" + (v == null || v.isEmpty() ? "" : " " + v))
                    .setContentText("点一下安装。允许一次以后，以后的更新会自动装好")
                    .setContentIntent(tap).setAutoCancel(true);
            try {
                nm.notify(NOTIFY_ID, b.build());
            } catch (SecurityException ignored) {
            }
        }
    }

    /** After an update: Android keeps the jobs, but make sure the periodic check is there. */
    public static class UpdatedReceiver extends BroadcastReceiver {
        @Override
        public void onReceive(Context ctx, Intent intent) {
            if (!Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction()) && !Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;
            NotifyJob.schedule(ctx);
            NotificationManager nm = ctx.getSystemService(NotificationManager.class);
            if (nm != null) nm.cancel(NOTIFY_ID);
        }
    }

}
