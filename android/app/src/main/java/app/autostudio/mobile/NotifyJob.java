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
import android.net.Uri;
import android.os.Build;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.BooleanSupplier;

/**
 * Notifications for work on the PC: a run finished, failed, or is waiting for the user's approval; content
 * waits to be approved for publishing; a publish went out or failed.
 *
 * Android only keeps a background app connected if it shows a permanent "running" notification, so this
 * is a JobScheduler job instead. A periodic job checks every PC every 15 minutes; a follow-up job long-polls
 * GET /api/remote/events while something on a PC is still running, starting the moment the app is left,
 * and again when a PC's next scheduled task starts (the PC says when), so its news is not 15 minutes late.
 * While the phone is in deep sleep Android holds jobs back to its maintenance windows, so a notification
 * can come a few minutes late then.
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
    private static final long RUN_FOR_MS = 8 * 60_000L;        // Android stops a job after 10 minutes
    private static final int LONG_POLL_S = 50;
    private static final long STALE_MS = 12 * 3600_000L;      // older news is not worth a buzz
    private static final long TASK_GRACE_MS = 20_000L;         // start listening this long after a task starts

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
    private static List<JSONObject> pairedPcs(Context ctx) {
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

    /**
     * Long-poll one PC until nothing runs there any more or time is up. Returns whether something was still
     * running. The first call to a PC only learns where its feed is: old news never buzzes.
     */
    private static boolean pollPc(Context ctx, JSONObject pc, BooleanSupplier stop, long deadline, long[] nextTaskMs) {
        String pcId = pc.optString("pc_id");
        if (!polling.add(pcId)) return false;      // another job has this PC and will carry on
        try {
            SharedPreferences cursors = ctx.getSharedPreferences("notify_cursor", Context.MODE_PRIVATE);
            Set<String> bases = new LinkedHashSet<>();
            if (!pc.optString("base").isEmpty()) bases.add(pc.optString("base"));
            JSONArray more = pc.optJSONArray("addresses");
            for (int i = 0; more != null && i < more.length(); i++) bases.add(more.optString(i));
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
                for (String b : base != null ? Arrays.asList(base) : new ArrayList<>(bases)) {
                    r = MainActivity.http("GET", b + "/api/remote/events" + query, null, pc.optString("token"),
                            first ? 2500 : 5000, (wait + 15) * 1000);
                    if (r.optInt("status") != 0) {
                        base = b;
                        break;
                    }
                }
                if (r == null || r.optInt("status") != 200) return active && !first;   // PC off, or unpaired
                JSONObject d = r.optJSONObject("data");
                if (d == null) return false;
                JSONArray events = d.optJSONArray("events");
                for (int i = 0; events != null && i < events.length(); i++) show(ctx, pc, events.getJSONObject(i));
                cursors.edit().putString(pcId, d.optString("boot") + ":" + d.optLong("cursor")).apply();
                active = d.optInt("active") > 0;
                if (!d.isNull("next_task_in") && d.has("next_task_in")) {
                    synchronized (nextTaskMs) {
                        nextTaskMs[0] = Math.min(nextTaskMs[0], d.optLong("next_task_in") * 1000);
                    }
                }
                if (!active) return false;
            }
            return active;
        } catch (Exception e) {
            return false;
        } finally {
            polling.remove(pcId);
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
        ask.setDescription("Agent 想做一件需要你同意的事，或者做好的内容等你批准发布");
        nm.createNotificationChannels(Arrays.asList(runs, ask));
    }

    @SuppressWarnings("deprecation")
    private static void show(Context ctx, JSONObject pc, JSONObject ev) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        String type = ev.optString("type");
        String sid = ev.optString("session_id");
        String pcId = pc.optString("pc_id");
        boolean hasSession = sid.matches("[\\w-]+");
        String route = ev.optString("route");
        if (!route.matches(ROUTE)) route = hasSession ? "#/chat/" + sid : "";
        if (nm == null || route.isEmpty()) return;
        // one notification per conversation (a task's runs and the posts they made share it), replaced by news
        String key = hasSession ? sid : route;
        int id = (pcId + "/" + key).hashCode();
        boolean waiting = "approval_needed".equals(type) || "posts_pending".equals(type) || "post_attention".equals(type)
                || ev.optInt("pending") > 0;    // still waiting on the user after this news
        if (!waiting) nm.cancel(TAG_APPROVAL, id);     // answered, finished or failed
        long when = (long) (ev.optDouble("ts", 0) * 1000);
        if (when > 0 && System.currentTimeMillis() - when > STALE_MS) return;
        if (MainActivity.isShowing(pcId, route)) return;      // the user is looking at that page
        if (!enabled(ctx) || !allowed(ctx)) return;

        String title = ev.optString("title", "对话");
        String text = ev.optString("text");
        String head;
        switch (type) {
            case "run_done":
                head = ev.optInt("pending") > 0 ? "做好了，等你确认发布：" + title
                        : (ev.optBoolean("task") ? "定时任务做完了：" : "做完了：") + title;
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
            case "post_published":
                head = "已发布：「" + title + "」";
                break;
            case "post_failed":
                head = "发布失败：「" + title + "」";
                break;
            case "post_attention":
                head = "需要你在电脑上操作：「" + title + "」";
                break;
            default:
                return;
        }
        String channel = waiting ? CH_APPROVALS : CH_RUNS;
        String tag = waiting ? TAG_APPROVAL : TAG_RUN;
        if (waiting) nm.cancel(TAG_RUN, id);
        ensureChannels(ctx);
        Intent open = new Intent(ctx, MainActivity.class)
                .setAction(ACTION_OPEN)
                .setData(Uri.parse("autostudio://open/" + pcId + "/" + Uri.encode(key)))    // one PendingIntent each
                .putExtra(EXTRA_PC, pcId)
                .putExtra(EXTRA_ROUTE, route)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent tap = PendingIntent.getActivity(ctx, id, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(ctx, channel)
                : new Notification.Builder(ctx).setPriority(Notification.PRIORITY_HIGH).setDefaults(Notification.DEFAULT_ALL);
        b.setSmallIcon(R.drawable.ic_notify)
                .setColor(0xFF8B5CF6)
                .setContentTitle(head)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSubText(pc.optString("pc_name"))
                .setContentIntent(tap)
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)     // "waiting for approval" turning into "the run is done" buzzes once
                .setCategory(TAG_APPROVAL.equals(tag) ? Notification.CATEGORY_REMINDER : Notification.CATEGORY_STATUS);
        if (when > 0) b.setWhen(when).setShowWhen(true);
        try {
            nm.notify(tag, id, b.build());
        } catch (SecurityException ignored) {      // permission taken away in the meantime
        }
    }
}
