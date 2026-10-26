package app.autostudio.mobile;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Build;
import android.os.SystemClock;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.BooleanSupplier;

/**
 * Notifications for work on the PC: a run finished, failed, or is waiting for the user's approval; a video
 * waits to be approved for publishing (with its cover and 同意发布 / 不发了 buttons); a publish went out or failed.
 *
 * Android only keeps a background app connected if it shows a permanent "running" notification, so this
 * is a JobScheduler job instead. A periodic job checks every PC every 15 minutes; a follow-up job long-polls
 * GET /api/remote/events while something on a PC is still running, starting the moment the app is left,
 * and again when a PC's next scheduled task starts (the PC says when), so its news is not 15 minutes late.
 * When the PC cannot be reached (the phone is out, on mobile data) the same news comes through the PC's
 * encrypted mailbox on the AutoStudio website (Relay). While the phone is in deep sleep Android holds jobs
 * back to its maintenance windows, so a notification can come a few minutes late then.
 */
public class NotifyJob extends JobService {
    static final String ACTION_OPEN = "app.autostudio.mobile.OPEN_SESSION";
    static final String EXTRA_PC = "pc_id";
    static final String EXTRA_SESSION = "session_id";
    static final String EXTRA_ROUTE = "route";
    /** Where in the PC's UI a notification may lead: its own pages only (#/chat/…, #/review/…, #/tasks). */
    static final String ROUTE = "#/[A-Za-z0-9_/,-]{1,300}";

    private static final int JOB_PERIODIC = 7100;
    private static final int[] JOB_FOLLOW = {7101, 7102};      // two ids, so one can schedule the next
    private static final int JOB_WAKE = 7103;                   // when a PC's next scheduled task starts
    private static final String CH_RUNS = "runs";
    private static final String CH_APPROVALS = "approvals";
    private static final String TAG_RUN = "run";
    private static final String TAG_APPROVAL = "approval";
    private static final String TAG_POST = "post";
    private static final long RUN_FOR_MS = 8 * 60_000L;        // Android stops a job after 10 minutes
    private static final int LONG_POLL_S = 50;
    private static final long RELAY_EVERY_MS = 20_000L;        // the mailbox has no long poll: ask this often
    private static final long STALE_MS = 12 * 3600_000L;      // older news is not worth a buzz
    private static final long TASK_GRACE_MS = 20_000L;         // start listening this long after a task starts
    private static final int SEEN_KEEP = 300;

    /** PCs some thread in this process is already polling: two jobs must not show the same news twice. */
    private static final Set<String> polling = ConcurrentHashMap.newKeySet();

    private volatile boolean stopped;

    @Override
    public boolean onStartJob(JobParameters params) {
        stopped = false;
        int self = params.getJobId();
        new Thread(() -> {
            long[] nextTaskMs = {Long.MAX_VALUE};
            boolean active = pollAll(this, () -> stopped, nextTaskMs);
            if (self == JOB_PERIODIC && !stopped) Updater.check(this, false);
            if (!stopped) jobFinished(params, false);
            // something is still running on a PC: keep listening (a stopped job is re-run by Android itself);
            // otherwise come back when a PC's next scheduled task starts
            if (active) follow(this, stopped ? -1 : self, 0);
            else if (nextTaskMs[0] < 24 * 3600_000L) wakeIn(this, nextTaskMs[0] + TASK_GRACE_MS);
        }, "autostudio-notify").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        stopped = true;
        return true;
    }

    // ------------------------------------------------------------------ settings

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences("autostudio", Context.MODE_PRIVATE);
    }

    /** The user's switch on the connect screen (on unless turned off). */
    static boolean enabled(Context ctx) {
        return prefs(ctx).getBoolean("notify", true);
    }

    /** Whether Android lets this app show notifications at all (Android 13+ asks the user). */
    static boolean allowed(Context ctx) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm != null && !nm.areNotificationsEnabled()) return false;
        return Build.VERSION.SDK_INT < 33
                || ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    /** Paired PCs, as the connect screen saves them: [{pc_id, pc_name, token, base, addresses}]. */
    static List<JSONObject> pairedPcs(Context ctx) {
        List<JSONObject> out = new ArrayList<>();
        try {
            JSONArray all = new JSONArray(prefs(ctx).getString("pcs", "[]"));
            for (int i = 0; i < all.length(); i++) {
                JSONObject pc = all.optJSONObject(i);
                if (pc != null && !pc.optString("pc_id").isEmpty() && !pc.optString("token").isEmpty()
                        && !"null".equals(pc.optString("token"))) out.add(pc);
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    static JSONObject pc(Context ctx, String pcId) {
        for (JSONObject pc : pairedPcs(ctx)) if (pc.optString("pc_id").equals(pcId)) return pc;
        return null;
    }

    private static List<String> bases(JSONObject pc) {
        Set<String> out = new LinkedHashSet<>();
        if (!pc.optString("base").isEmpty()) out.add(pc.optString("base"));
        JSONArray more = pc.optJSONArray("addresses");
        for (int i = 0; more != null && i < more.length(); i++) out.add(more.optString(i));
        return new ArrayList<>(out);
    }

    // ------------------------------------------------------------------ scheduling

    /** Make sure the periodic check is set up (or gone, if the user turned notifications off). */
    static void schedule(Context ctx) {
        JobScheduler js = ctx.getSystemService(JobScheduler.class);
        if (js == null) return;
        if (!enabled(ctx) || pairedPcs(ctx).isEmpty()) {
            cancel(ctx);
            return;
        }
        if (js.getPendingJob(JOB_PERIODIC) != null) return;    // rescheduling would stop it if it is running
        js.schedule(new JobInfo.Builder(JOB_PERIODIC, new ComponentName(ctx, NotifyJob.class))
                .setPeriodic(15 * 60_000L)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPersisted(true)
                .build());
    }

    /** Listen to the PCs right away (or after delayMs), unless a follow-up job is already doing it. */
    static void follow(Context ctx, int fromJob, long delayMs) {
        JobScheduler js = ctx.getSystemService(JobScheduler.class);
        if (js == null || !enabled(ctx) || pairedPcs(ctx).isEmpty()) return;
        for (int id : JOB_FOLLOW) if (id != fromJob && js.getPendingJob(id) != null) return;
        int id = fromJob == JOB_FOLLOW[0] ? JOB_FOLLOW[1] : JOB_FOLLOW[0];
        js.schedule(new JobInfo.Builder(id, new ComponentName(ctx, NotifyJob.class))
                .setMinimumLatency(delayMs)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .build());
    }

    /** Listen again once delayMs have passed: a PC's next scheduled task will be running by then. */
    static void wakeIn(Context ctx, long delayMs) {
        JobScheduler js = ctx.getSystemService(JobScheduler.class);
        if (js == null || !enabled(ctx) || pairedPcs(ctx).isEmpty()) return;
        long at = System.currentTimeMillis() + delayMs;
        // already set for about then: rescheduling would only stop the wake job if it is running right now
        if (js.getPendingJob(JOB_WAKE) != null && Math.abs(prefs(ctx).getLong("wake_at", 0) - at) < 60_000L) return;
        prefs(ctx).edit().putLong("wake_at", at).apply();
        js.schedule(new JobInfo.Builder(JOB_WAKE, new ComponentName(ctx, NotifyJob.class))
                .setMinimumLatency(delayMs)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .build());
    }

    static void cancel(Context ctx) {
        JobScheduler js = ctx.getSystemService(JobScheduler.class);
        if (js == null) return;
        js.cancel(JOB_PERIODIC);
        js.cancel(JOB_WAKE);
        for (int id : JOB_FOLLOW) js.cancel(id);
    }

    // ------------------------------------------------------------------ polling

    /**
     * Poll every paired PC; true when something was still running on one of them when time ran out.
     * nextTaskMs[0] becomes the soonest start of a scheduled task on any of them.
     */
    private static boolean pollAll(Context ctx, BooleanSupplier stop, long[] nextTaskMs) {
        if (!enabled(ctx)) return false;
        List<JSONObject> pcs = pairedPcs(ctx);
        if (pcs.isEmpty()) return false;
        long deadline = SystemClock.elapsedRealtime() + RUN_FOR_MS;
        ExecutorService ex = Executors.newFixedThreadPool(pcs.size());
        List<Future<Boolean>> results = new ArrayList<>();
        for (JSONObject pc : pcs) results.add(ex.submit(() -> pollPc(ctx, pc, stop, deadline, nextTaskMs)));
        boolean active = false;
        for (Future<Boolean> f : results) {
            try {
                active |= f.get();
            } catch (Exception ignored) {
            }
        }
        ex.shutdown();
        return active;
    }

    private static void noteNextTask(long[] nextTaskMs, JSONObject status) {
        if (status == null || status.isNull("next_task_in") || !status.has("next_task_in")) return;
        synchronized (nextTaskMs) {
            nextTaskMs[0] = Math.min(nextTaskMs[0], status.optLong("next_task_in") * 1000);
        }
    }

    /**
     * Long-poll one PC until nothing runs there any more or time is up. Returns whether something was still
     * running. The first call to a PC only learns where its feed is: old news never buzzes. A PC that cannot
     * be reached is asked through its mailbox instead.
     */
    private static boolean pollPc(Context ctx, JSONObject pc, BooleanSupplier stop, long deadline, long[] nextTaskMs) {
        String pcId = pc.optString("pc_id");
        if (!polling.add(pcId)) return false;      // another job has this PC and will carry on
        try {
            SharedPreferences cursors = ctx.getSharedPreferences("notify_cursor", Context.MODE_PRIVATE);
            String base = null;
            boolean active = false;
            for (boolean first = true; !stop.getAsBoolean(); first = false) {
                long leftS = (deadline - SystemClock.elapsedRealtime()) / 1000;
                int wait = first ? 0 : (int) Math.min(LONG_POLL_S, leftS - 10);
                if (wait < 1 && !first) return active;
                String[] cur = cursors.getString(pcId, "").split(":", 2);
                String query = cur.length == 2
                        ? "?after=" + cur[1] + "&boot=" + Uri.encode(cur[0]) + "&wait=" + wait : "";
                JSONObject r = null;
                for (String b : base != null ? Arrays.asList(base) : bases(pc)) {
                    r = MainActivity.http("GET", b + "/api/remote/events" + query, null, pc.optString("token"),
                            first ? 2500 : 5000, (wait + 15) * 1000);
                    if (r.optInt("status") != 0) {
                        base = b;
                        break;
                    }
                }
                if (r == null || r.optInt("status") == 0) {
                    // not on the PC's network (or the PC is off): its mailbox may have news
                    return base == null ? pollRelay(ctx, pc, stop, deadline, nextTaskMs) : active && !first;
                }
                if (r.optInt("status") != 200) return false;     // unpaired
                JSONObject d = r.optJSONObject("data");
                if (d == null) return false;
                if (first) backfillRelay(ctx, pc, base);
                JSONArray events = d.optJSONArray("events");
                for (int i = 0; events != null && i < events.length(); i++) show(ctx, pc, events.getJSONObject(i), base);
                cursors.edit().putString(pcId, d.optString("boot") + ":" + d.optLong("cursor")).apply();
                syncPending(ctx, pc, d.optJSONArray("pending"), d.optJSONArray("pending_ids"), base);
                active = d.optInt("active") > 0;
                noteNextTask(nextTaskMs, d);
                if (!active) return false;
            }
            return active;
        } catch (Exception e) {
            return false;
        } finally {
            polling.remove(pcId);
        }
    }

    /** News through the PC's mailbox, for as long as something on the PC is still going. */
    private static boolean pollRelay(Context ctx, JSONObject pc, BooleanSupplier stop, long deadline, long[] nextTaskMs) {
        if (Relay.of(ctx, pc.optString("pc_id")) == null) return false;
        boolean active = false;
        while (!stop.getAsBoolean()) {
            JSONObject got = Relay.fetch(ctx, pc);
            if (!got.optBoolean("ok")) return active;
            JSONArray msgs = got.optJSONArray("messages");
            JSONObject latest = null;
            for (int i = 0; msgs != null && i < msgs.length(); i++) {
                JSONObject m = msgs.optJSONObject(i);
                if (m == null) continue;
                if ("notice".equals(m.optString("t")) && m.optJSONObject("ev") != null) show(ctx, pc, m.optJSONObject("ev"), null);
                if (m.optJSONObject("status") != null) latest = m;
            }
            if (latest != null) {
                JSONObject st = latest.optJSONObject("status");
                syncPending(ctx, pc, null, st.optJSONArray("pending_ids"), null);
                noteNextTask(nextTaskMs, st);
                // a status from a few hours ago says nothing about now
                active = st.optInt("active") > 0 && System.currentTimeMillis() / 1000.0 - latest.optDouble("ts", 0) < 3 * 3600;
            }
            if (!active || SystemClock.elapsedRealtime() + RELAY_EVERY_MS > deadline - 5000) return active;
            SystemClock.sleep(RELAY_EVERY_MS);
        }
        return active;
    }

    /** Phones paired before the relay existed get their mailbox key the next time they are on the PC's network. */
    private static void backfillRelay(Context ctx, JSONObject pc, String base) {
        String pcId = pc.optString("pc_id");
        if (Relay.of(ctx, pcId) != null) return;
        JSONObject r = MainActivity.http("GET", base + "/api/remote/relay", null, pc.optString("token"), 3000, 8000);
        if (r.optInt("status") == 200 && r.optJSONObject("data") != null) Relay.save(ctx, pcId, r.optJSONObject("data"));
    }

    /**
     * What the PC says is waiting for approval right now: notify about posts this phone has not shown yet
     * (it may have missed the news), clear the ones answered meanwhile (on the PC, or on another phone).
     */
    private static void syncPending(Context ctx, JSONObject pc, JSONArray pending, JSONArray pendingIds, String base) {
        String pcId = pc.optString("pc_id");
        Set<String> now = new HashSet<>();
        for (int i = 0; pendingIds != null && i < pendingIds.length(); i++) now.add(pendingIds.optString(i));
        for (int i = 0; pending != null && i < pending.length(); i++) {
            JSONObject p = pending.optJSONObject(i);
            if (p == null) continue;
            now.add(p.optString("id"));
            JSONObject known = Posts.get(ctx, pcId, p.optString("id"));
            if (known == null) showPost(ctx, pc, p, base, null, true);
        }
        if (pendingIds == null && pending == null) return;
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        for (String id : Posts.ids(ctx, pcId)) {
            JSONObject p = Posts.get(ctx, pcId, id);
            if (p != null && "pending".equals(p.optString("state")) && !now.contains(id)) {
                Posts.setState(ctx, pcId, id, "gone");
                if (nm != null) nm.cancel(TAG_POST, postNotificationId(pcId, id));
            }
        }
    }

    // ------------------------------------------------------------------ notifications

    static void ensureChannels(Context ctx) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel runs = new NotificationChannel(CH_RUNS, "任务完成或出错", NotificationManager.IMPORTANCE_HIGH);
        runs.setDescription("电脑上的任务做完了，或者出错停下了");
        NotificationChannel ask = new NotificationChannel(CH_APPROVALS, "等你批准", NotificationManager.IMPORTANCE_HIGH);
        ask.setDescription("做好的视频等你点「同意」发布，或者 Agent 想做一件需要你同意的事");
        nm.createNotificationChannels(Arrays.asList(runs, ask));
    }

    /** Has this news been shown already (it may arrive both directly and through the mailbox)? */
    private static synchronized boolean seen(Context ctx, JSONObject ev) {
        String key = ev.optString("type") + "|" + ev.optString("ts") + "|" + ev.optString("post_id") + "|" + ev.optString("session_id");
        SharedPreferences p = ctx.getSharedPreferences("notify_seen", Context.MODE_PRIVATE);
        List<String> keys = new ArrayList<>(Arrays.asList(p.getString("keys", "").split("\n")));
        if (keys.contains(key)) return true;
        keys.add(key);
        if (keys.size() > SEEN_KEEP) keys = keys.subList(keys.size() - SEEN_KEEP, keys.size());
        p.edit().putString("keys", String.join("\n", keys)).apply();
        return false;
    }

    static int postNotificationId(String pcId, String postId) {
        return (pcId + "/post/" + postId).hashCode();
    }

    private static PendingIntent openIntent(Context ctx, String pcId, String key, String route, int id) {
        Intent open = new Intent(ctx, MainActivity.class)
                .setAction(ACTION_OPEN)
                .setData(Uri.parse("autostudio://open/" + pcId + "/" + Uri.encode(key)))    // one PendingIntent each
                .putExtra(EXTRA_PC, pcId)
                .putExtra(EXTRA_ROUTE, route)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(ctx, id, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    @SuppressWarnings("deprecation")
    private static Notification.Builder builder(Context ctx, String channel) {
        ensureChannels(ctx);
        return Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(ctx, channel)
                : new Notification.Builder(ctx).setPriority(Notification.PRIORITY_HIGH).setDefaults(Notification.DEFAULT_ALL);
    }

    private static void post(Context ctx, String tag, int id, Notification n) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) return;
        try {
            nm.notify(tag, id, n);
        } catch (SecurityException ignored) {      // permission taken away in the meantime
        }
    }

    private static void show(Context ctx, JSONObject pc, JSONObject ev, String base) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        String type = ev.optString("type");
        String sid = ev.optString("session_id");
        String pcId = pc.optString("pc_id");
        boolean hasSession = sid.matches("[\\w-]+");
        String route = ev.optString("route");
        if (!route.matches(ROUTE)) route = hasSession ? "#/chat/" + sid : "";
        if (nm == null || route.isEmpty()) return;
        long when = (long) (ev.optDouble("ts", 0) * 1000);
        if (when > 0 && System.currentTimeMillis() - when > STALE_MS) return;
        if (seen(ctx, ev)) return;

        // a video waiting for approval: one notification per post, with its cover and the answer buttons
        JSONArray posts = ev.optJSONArray("posts");
        if (("posts_pending".equals(type) || "run_done".equals(type)) && posts != null && posts.length() > 0) {
            if (hasSession) {
                nm.cancel(TAG_RUN, (pcId + "/" + sid).hashCode());
                nm.cancel(TAG_APPROVAL, (pcId + "/" + sid).hashCode());
            }
            for (int i = 0; i < posts.length(); i++) {
                JSONObject p = posts.optJSONObject(i);
                if (p != null && Posts.get(ctx, pcId, p.optString("id")) == null) showPost(ctx, pc, p, base, ev, true);
            }
            return;
        }
        if (("post_published".equals(type) || "post_failed".equals(type) || "post_attention".equals(type))
                && ev.optString("post_id").matches("[\\w-]{1,40}")) {
            postResult(ctx, pc, ev, route);
            return;
        }

        // one notification per conversation (a task's runs share it), replaced by news
        String key = hasSession ? sid : route;
        int id = (pcId + "/" + key).hashCode();
        boolean waiting = "approval_needed".equals(type) || ev.optInt("pending") > 0;    // still waiting on the user
        if (!waiting) nm.cancel(TAG_APPROVAL, id);     // answered, finished or failed
        if (MainActivity.isShowing(pcId, route)) return;      // the user is looking at that page
        if (!enabled(ctx) || !allowed(ctx)) return;

        String title = ev.optString("title", "对话");
        String text = ev.optString("text");
        String head;
        switch (type) {
            case "run_done":
                head = (ev.optBoolean("task") ? "定时任务做完了：" : "做完了：") + title;
                if (text.isEmpty()) text = "点开看看结果";
                break;
            case "run_error":
                head = "出错了：" + title;
                if (text.isEmpty()) text = "点开看看哪里出了问题";
                break;
            case "approval_needed":
                head = "等你批准：" + title;
                text = "Agent 想要「" + ev.optString("label", ev.optString("name")) + "」，点开选择允许还是拒绝";
                break;
            case "posts_pending":
                head = "等你确认发布：" + title;
                if (text.isEmpty()) text = "点开预览，一键批准发布";
                break;
            default:
                return;
        }
        String tag = waiting ? TAG_APPROVAL : TAG_RUN;
        if (waiting) nm.cancel(TAG_RUN, id);
        Notification.Builder b = builder(ctx, waiting ? CH_APPROVALS : CH_RUNS);
        b.setSmallIcon(R.drawable.ic_notify)
                .setColor(0xFF8B5CF6)
                .setContentTitle(head)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSubText(pc.optString("pc_name"))
                .setContentIntent(openIntent(ctx, pcId, key, route, id))
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)     // "waiting for approval" turning into "the run is done" buzzes once
                .setCategory(waiting ? Notification.CATEGORY_REMINDER : Notification.CATEGORY_STATUS);
        if (when > 0) b.setWhen(when).setShowWhen(true);
        post(ctx, tag, id, b.build());
    }

    private static byte[] coverBytes(JSONObject p, JSONObject pc, String base) {
        String b64 = p.optString("cover_jpg");
        if (!b64.isEmpty()) {
            try {
                return Base64.decode(b64, Base64.DEFAULT);
            } catch (Exception ignored) {
            }
        }
        String path = p.optString("cover");
        if (base == null || path.isEmpty()) return null;
        return MainActivity.httpBytes(base + "/api/thumb?w=720&path=" + Uri.encode(path), pc.optString("token"), 4000, 15000);
    }

    /** "Waiting for your approval": the cover, the title and the first lines, and 同意发布 / 不发了. */
    @SuppressWarnings("deprecation")
    static void showPost(Context ctx, JSONObject pc, JSONObject p, String base, JSONObject ev, boolean buzz) {
        String pcId = pc.optString("pc_id");
        String postId = p.optString("id");
        if (!postId.matches("[\\w-]{1,40}")) return;
        Posts.remember(ctx, pcId, p);
        byte[] jpeg = coverBytes(p, pc, base);
        if (jpeg != null) Posts.saveCover(ctx, postId, jpeg);
        else jpeg = Posts.loadCover(ctx, postId);
        if (!enabled(ctx) || !allowed(ctx)) return;
        int id = postNotificationId(pcId, postId);
        String title = p.optString("title").isEmpty() ? "新视频" : p.optString("title");
        String account = p.optString("account");
        String desc = p.optString("description");
        String text = (account.isEmpty() ? "" : "发到" + account + "：") + (desc.isEmpty() ? "点开看看" : desc);
        String route = "#/review/" + postId;
        Notification.Builder b = builder(ctx, CH_APPROVALS)
                .setSmallIcon(R.drawable.ic_notify)
                .setColor(0xFFFF2442)
                .setContentTitle("做好了，等你确认：「" + title + "」")
                .setContentText(text)
                .setSubText(pc.optString("pc_name"))
                .setContentIntent(openIntent(ctx, pcId, "post/" + postId, route, id))
                .setAutoCancel(false)
                .setOnlyAlertOnce(!buzz)
                .setCategory(Notification.CATEGORY_REMINDER)
                .addAction(new Notification.Action.Builder(null, "同意发布", answer(ctx, pcId, postId, true)).build())
                .addAction(new Notification.Action.Builder(null, "不发了", answer(ctx, pcId, postId, false)).build());
        Bitmap pic = jpeg == null ? null : BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
        if (pic != null) {
            b.setLargeIcon(pic).setStyle(new Notification.BigPictureStyle().bigPicture(pic).setSummaryText(text));
        } else {
            b.setStyle(new Notification.BigTextStyle().bigText(text));
        }
        long when = ev == null ? 0 : (long) (ev.optDouble("ts", 0) * 1000);
        if (when > 0) b.setWhen(when).setShowWhen(true);
        post(ctx, TAG_POST, id, b.build());
    }

    private static PendingIntent answer(Context ctx, String pcId, String postId, boolean approve) {
        Intent i = new Intent(ctx, ActionReceiver.class)
                .setAction(approve ? ActionReceiver.APPROVE : ActionReceiver.CANCEL)
                .setData(Uri.parse("autostudio://answer/" + pcId + "/" + postId + "/" + approve))
                .putExtra(EXTRA_PC, pcId)
                .putExtra(ActionReceiver.EXTRA_POST, postId);
        return PendingIntent.getBroadcast(ctx, postNotificationId(pcId, postId) + (approve ? 1 : 2), i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private static Notification.Builder quiet(Context ctx, String pcId, String postId, String head, String text) {
        return builder(ctx, CH_RUNS)
                .setSmallIcon(R.drawable.ic_notify)
                .setColor(0xFF8B5CF6)
                .setContentTitle(head)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(openIntent(ctx, pcId, "post/" + postId, "#/review/" + postId, postNotificationId(pcId, postId)))
                .setOnlyAlertOnce(true);
    }

    /** The user tapped an answer: say so at once, before the network round trip. */
    static void postAnswering(Context ctx, String pcId, String postId, boolean approve) {
        JSONObject p = Posts.get(ctx, pcId, postId);
        String title = p == null ? "" : "「" + p.optString("title") + "」";
        Notification.Builder b = quiet(ctx, pcId, postId, approve ? "正在告诉电脑发布" + title : "正在告诉电脑不发" + title, "稍等…")
                .setProgress(0, 0, true).setOngoing(true);
        post(ctx, TAG_POST, postNotificationId(pcId, postId), b.build());
    }

    static void postAnswered(Context ctx, String pcId, String postId, boolean approve, String result) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        int id = postNotificationId(pcId, postId);
        JSONObject p = Posts.get(ctx, pcId, postId);
        String title = p == null ? "" : "「" + p.optString("title") + "」";
        if (!approve && !"failed".equals(result)) {
            if (nm != null) nm.cancel(TAG_POST, id);
            return;
        }
        String head;
        String text;
        switch (result) {
            case "direct":
                head = "正在发布" + title;
                text = "电脑正在上传到小红书，发好了会再告诉你";
                break;
            case "relay":
                head = "已同意，正在发给电脑" + title;
                text = "你不在家里的网络上：电脑收到后（一般不超过半分钟）就会开始发布，发好了会再告诉你";
                break;
            case "gone":
                head = "这条已经处理过了" + title;
                text = "可能已经在电脑上发布或取消了";
                break;
            default:
                if (p != null) Posts.setState(ctx, pcId, postId, "pending");
                head = "没送到电脑" + title;
                text = "电脑可能关机了，或者手机没有网络。点开重试";
        }
        post(ctx, TAG_POST, id, quiet(ctx, pcId, postId, head, text).setAutoCancel(true).build());
    }

    private static void postResult(Context ctx, JSONObject pc, JSONObject ev, String route) {
        String pcId = pc.optString("pc_id");
        String postId = ev.optString("post_id");
        String type = ev.optString("type");
        Posts.setState(ctx, pcId, postId, "post_published".equals(type) ? "published" : "post_failed".equals(type) ? "failed" : "approved");
        if (!enabled(ctx) || !allowed(ctx)) return;
        String title = "「" + ev.optString("title", "视频") + "」";
        String head = "post_published".equals(type) ? "已发布：" + title
                : "post_failed".equals(type) ? "发布失败：" + title : "需要你在电脑上操作：" + title;
        Notification.Builder b = builder(ctx, "post_published".equals(type) ? CH_RUNS : CH_APPROVALS)
                .setSmallIcon(R.drawable.ic_notify)
                .setColor("post_published".equals(type) ? 0xFF22C55E : 0xFFF43F5E)
                .setContentTitle(head)
                .setContentText(ev.optString("text"))
                .setStyle(new Notification.BigTextStyle().bigText(ev.optString("text")))
                .setSubText(pc.optString("pc_name"))
                .setContentIntent(openIntent(ctx, pcId, "post/" + postId, route, postNotificationId(pcId, postId)))
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_STATUS);
        Bitmap pic = null;
        byte[] jpeg = Posts.loadCover(ctx, postId);
        if (jpeg != null) pic = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
        if (pic != null) b.setLargeIcon(pic);
        post(ctx, TAG_POST, postNotificationId(pcId, postId), b.build());
    }
}
