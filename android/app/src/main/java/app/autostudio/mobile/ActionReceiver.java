package app.autostudio.mobile;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashSet;
import java.util.Set;

/**
 * The buttons on a "waiting for your approval" notification: 同意发布 / 不发了.
 *
 * The answer goes straight to the PC when it can be reached (same Wi-Fi), otherwise through the PC's
 * mailbox (Relay), which the PC checks every few seconds while something waits for an answer.
 */
public class ActionReceiver extends BroadcastReceiver {
    static final String APPROVE = "app.autostudio.mobile.APPROVE";
    static final String CANCEL = "app.autostudio.mobile.CANCEL";
    static final String EXTRA_POST = "post_id";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        String pcId = intent.getStringExtra(NotifyJob.EXTRA_PC);
        String postId = intent.getStringExtra(EXTRA_POST);
        boolean approve = APPROVE.equals(intent.getAction());
        if (pcId == null || postId == null || !postId.matches("[\\w-]{1,40}")) return;
        PendingResult async = goAsync();
        NotifyJob.postAnswering(ctx, pcId, postId, approve);
        new Thread(() -> {
            try {
                String result = decide(ctx, pcId, postId, approve);
                NotifyJob.postAnswered(ctx, pcId, postId, approve, result);
                if (approve && !"failed".equals(result)) NotifyJob.follow(ctx, -1, 0);    // listen for how the publish went
            } finally {
                async.finish();
            }
        }, "autostudio-answer").start();
    }

    /**
     * Tell the PC. Returns "direct" (the PC has it), "relay" (on its way through the mailbox), "gone" (the PC
     * says it is no longer waiting, e.g. answered on the PC already) or "failed".
     */
    static String decide(Context ctx, String pcId, String postId, boolean approve) {
        JSONObject pc = NotifyJob.pc(ctx, pcId);
        if (pc == null) return "failed";
        Set<String> bases = new LinkedHashSet<>();
        if (!pc.optString("base").isEmpty()) bases.add(pc.optString("base"));
        JSONArray more = pc.optJSONArray("addresses");
        for (int i = 0; more != null && i < more.length(); i++) bases.add(more.optString(i));
        String path = "/api/social/posts/" + postId + (approve ? "/publish" : "/cancel");
        for (String b : bases) {
            JSONObject r = MainActivity.http("POST", b + path, "{}", pc.optString("token"), 2500, 8000);
            int status = r.optInt("status");
            if (status == 200) {
                Posts.setState(ctx, pcId, postId, approve ? "approved" : "cancelled");
                return "direct";
            }
            if (status == 400 || status == 404) {        // already published / cancelled / deleted on the PC
                Posts.setState(ctx, pcId, postId, "gone");
                return "gone";
            }
            if (status == 401) return "failed";         // this phone was disconnected on the PC
        }
        try {
            if (Relay.send(ctx, pcId, new JSONObject().put("t", approve ? "approve" : "cancel").put("post", postId))) {
                Posts.setState(ctx, pcId, postId, approve ? "approved" : "cancelled");
                return "relay";
            }
        } catch (Exception ignored) {
        }
        return "failed";
    }
}
