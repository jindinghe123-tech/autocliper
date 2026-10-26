package app.autostudio.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

/**
 * What the phone knows about posts waiting for the user's approval: shown in notifications, and on the
 * connect screen when the PC cannot be reached (approve from there through the relay). Per PC: post id ->
 * {id, title, description, tags, account, state, at}, state = pending | approved | cancelled | published | failed.
 * Cover pictures are files in the app's cache.
 */
final class Posts {
    private static final int KEEP = 20;
    private static final long KEEP_MS = 7 * 86400_000L;

    private Posts() {
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences("posts", Context.MODE_PRIVATE);
    }

    private static JSONObject all(Context ctx, String pcId) {
        try {
            return new JSONObject(prefs(ctx).getString(pcId, "{}"));
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private static void write(Context ctx, String pcId, JSONObject posts) {
        // keep the newest few, and nothing older than a week
        List<String> ids = new ArrayList<>();
        for (Iterator<String> it = posts.keys(); it.hasNext(); ) ids.add(it.next());
        ids.sort((a, b) -> Long.compare(posts.optJSONObject(b).optLong("at"), posts.optJSONObject(a).optLong("at")));
        long now = System.currentTimeMillis();
        for (int i = 0; i < ids.size(); i++) {
            JSONObject p = posts.optJSONObject(ids.get(i));
            if (i >= KEEP || p == null || now - p.optLong("at") > KEEP_MS) {
                posts.remove(ids.get(i));
                cover(ctx, ids.get(i)).delete();
            }
        }
        prefs(ctx).edit().putString(pcId, posts.toString()).apply();
    }

    static synchronized JSONObject get(Context ctx, String pcId, String postId) {
        return all(ctx, pcId).optJSONObject(postId);
    }

    /** Remember a post the PC told us about (keeps a state the user already moved it to). */
    static synchronized JSONObject remember(Context ctx, String pcId, JSONObject brief) {
        JSONObject posts = all(ctx, pcId);
        String id = brief.optString("id");
        JSONObject old = posts.optJSONObject(id);
        JSONObject p = new JSONObject();
        try {
            for (String k : new String[]{"id", "title", "description", "account", "platform", "session_id"}) p.put(k, brief.optString(k));
            p.put("tags", brief.optJSONArray("tags") == null ? new JSONArray() : brief.optJSONArray("tags"));
            p.put("state", old != null ? old.optString("state", "pending") : "pending");
            p.put("at", old != null ? old.optLong("at") : System.currentTimeMillis());
            posts.put(id, p);
        } catch (Exception ignored) {
        }
        write(ctx, pcId, posts);
        return p;
    }

    static synchronized void setState(Context ctx, String pcId, String postId, String state) {
        JSONObject posts = all(ctx, pcId);
        JSONObject p = posts.optJSONObject(postId);
        if (p == null) return;
        try {
            p.put("state", state);
        } catch (Exception ignored) {
        }
        write(ctx, pcId, posts);
    }

    /** Posts the user has not answered yet (as far as this phone knows). */
    static synchronized List<JSONObject> pending(Context ctx, String pcId) {
        List<JSONObject> out = new ArrayList<>();
        JSONObject posts = all(ctx, pcId);
        for (Iterator<String> it = posts.keys(); it.hasNext(); ) {
            JSONObject p = posts.optJSONObject(it.next());
            if (p != null && "pending".equals(p.optString("state"))) out.add(p);
        }
        out.sort((a, b) -> Long.compare(b.optLong("at"), a.optLong("at")));
        return out;
    }

    static synchronized List<String> ids(Context ctx, String pcId) {
        List<String> out = new ArrayList<>();
        for (Iterator<String> it = all(ctx, pcId).keys(); it.hasNext(); ) out.add(it.next());
        return out;
    }

    // ------------------------------------------------------------------ covers

    static File cover(Context ctx, String postId) {
        File dir = new File(ctx.getCacheDir(), "covers");
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        return new File(dir, postId.replaceAll("[^\\w-]", "_") + ".jpg");
    }

    static void saveCover(Context ctx, String postId, byte[] jpeg) {
        if (jpeg == null || jpeg.length == 0) return;
        try (FileOutputStream out = new FileOutputStream(cover(ctx, postId))) {
            out.write(jpeg);
        } catch (Exception ignored) {
        }
    }

    static byte[] loadCover(Context ctx, String postId) {
        File f = cover(ctx, postId);
        if (!f.isFile() || f.length() > 4 << 20) return null;
        byte[] buf = new byte[(int) f.length()];
        try (FileInputStream in = new FileInputStream(f)) {
            int off = 0;
            while (off < buf.length) {
                int n = in.read(buf, off, buf.length - off);
                if (n < 0) break;
                off += n;
            }
            return buf;
        } catch (Exception e) {
            return null;
        }
    }

    static String coverDataUri(Context ctx, String postId) {
        byte[] b = loadCover(ctx, postId);
        return b == null ? "" : "data:image/jpeg;base64," + Base64.encodeToString(b, Base64.NO_WRAP);
    }
}
