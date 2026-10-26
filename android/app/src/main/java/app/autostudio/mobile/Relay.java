package app.autostudio.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * The phone's side of the PC's mailbox on the AutoStudio website (see autostudio/relay.py and relay/worker.js),
 * used when the phone cannot reach the PC directly: news from the PC comes in, the user's 同意 goes out.
 *
 * Every message is sealed with AES-256-GCM under the key this phone got from its PC at pairing (on the local
 * network); the mailbox only sees ciphertext. A PC entry carries its mailbox as "relay":
 * {url, box, device_id, token, key}.
 */
final class Relay {
    private Relay() {
    }

    private static SharedPreferences store(Context ctx) {
        return ctx.getSharedPreferences("relay", Context.MODE_PRIVATE);
    }

    /** This phone's mailbox credentials for a PC (kept apart from the PC list the connect page rewrites). */
    static JSONObject of(Context ctx, String pcId) {
        try {
            JSONObject r = new JSONObject(store(ctx).getString(pcId, "{}"));
            if (r.optString("box").isEmpty() || r.optString("token").isEmpty() || r.optString("key").isEmpty()) return null;
            return r;
        } catch (Exception e) {
            return null;
        }
    }

    static void save(Context ctx, String pcId, JSONObject creds) {
        if (pcId == null || pcId.isEmpty() || creds == null || creds.optString("box").isEmpty()) return;
        store(ctx).edit().putString(pcId, creds.toString()).apply();
    }

    static void forget(Context ctx, String pcId) {
        store(ctx).edit().remove(pcId).apply();
        cursors(ctx).edit().remove(pcId).apply();
    }

    private static byte[] aad(JSONObject r, String direction) {
        return ("autostudio-relay/v1|" + r.optString("box") + "|" + r.optString("device_id") + "|" + direction)
                .getBytes(StandardCharsets.UTF_8);
    }

    static String seal(JSONObject r, JSONObject msg) throws Exception {
        byte[] key = Base64.decode(r.optString("key"), Base64.DEFAULT);
        byte[] nonce = new byte[12];
        new SecureRandom().nextBytes(nonce);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, nonce));
        c.updateAAD(aad(r, "in"));
        byte[] ct = c.doFinal(msg.toString().getBytes(StandardCharsets.UTF_8));
        byte[] out = new byte[nonce.length + ct.length];
        System.arraycopy(nonce, 0, out, 0, nonce.length);
        System.arraycopy(ct, 0, out, nonce.length, ct.length);
        return Base64.encodeToString(out, Base64.NO_WRAP);
    }

    static JSONObject unseal(JSONObject r, String body) throws Exception {
        byte[] raw = Base64.decode(body, Base64.DEFAULT);
        byte[] key = Base64.decode(r.optString("key"), Base64.DEFAULT);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, raw, 0, 12));
        c.updateAAD(aad(r, "out"));
        byte[] pt = c.doFinal(raw, 12, raw.length - 12);
        return new JSONObject(new String(pt, StandardCharsets.UTF_8));
    }

    private static String base(JSONObject r) {
        return r.optString("url").replaceAll("/+$", "") + "/relay/v1/" + r.optString("box");
    }

    private static SharedPreferences cursors(Context ctx) {
        return ctx.getSharedPreferences("relay_cursor", Context.MODE_PRIVATE);
    }

    /** Messages for this phone since last time: {"ok", "messages": [...]} (ok=false: the mailbox was unreachable). */
    static JSONObject fetch(Context ctx, JSONObject pc) {
        JSONObject out = new JSONObject();
        JSONArray msgs = new JSONArray();
        try {
            String pcId = pc.optString("pc_id");
            JSONObject r = of(ctx, pcId);
            out.put("ok", false).put("messages", msgs);
            if (r == null) return out;
            long after = cursors(ctx).getLong(pcId, 0);
            JSONObject res = MainActivity.http("GET", base(r) + "/out?device=" + Uri.encode(r.optString("device_id")) + "&after=" + after,
                    null, r.optString("token"), 8000, 15000);
            if (res.optInt("status") != 200) return out;
            JSONObject d = res.optJSONObject("data");
            JSONArray items = d == null ? null : d.optJSONArray("items");
            long cursor = after;
            for (int i = 0; items != null && i < items.length(); i++) {
                JSONObject it = items.getJSONObject(i);
                cursor = Math.max(cursor, it.optLong("seq"));
                try {
                    msgs.put(unseal(r, it.optString("body")));
                } catch (Exception bad) {
                    // not sealed with our key (an old key after re-pairing): skip it
                }
            }
            cursors(ctx).edit().putLong(pcId, cursor).apply();
            out.put("ok", true);
        } catch (Exception ignored) {
        }
        return out;
    }

    /** Send an answer to the PC through its mailbox (e.g. {"t":"approve","post":id}). */
    static boolean send(Context ctx, String pcId, JSONObject msg) {
        try {
            JSONObject r = of(ctx, pcId);
            if (r == null) return false;
            msg.put("ts", System.currentTimeMillis() / 1000.0);
            String body = new JSONObject().put("body", seal(r, msg)).toString();
            JSONObject res = MainActivity.httpWith("POST", base(r) + "/in", body, r.optString("token"), 8000, 15000,
                    "X-Device", r.optString("device_id"));
            return res.optInt("status") == 200;
        } catch (Exception e) {
            return false;
        }
    }
}
