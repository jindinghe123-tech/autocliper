// Builds what the website serves next to site/index.html (run by `npx wrangler deploy`, see wrangler.jsonc):
//   site/AutoStudio.zip   the app, packed from the latest commit (so commit before deploying)
//   site/AutoStudio.apk   the Android app
//   site/update.json      what installed copies check to update themselves (autostudio/updater.py)
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, readFileSync, statSync, writeFileSync } from "node:fs";

const git = (...args) => execFileSync("git", args, { encoding: "utf8" }).trim();
const sha256 = (file) => createHash("sha256").update(readFileSync(file)).digest("hex");

execFileSync("git", ["-c", "core.autocrlf=false", "archive", "--format=zip", "--prefix=AutoStudio/", "-o", "site/AutoStudio.zip", "HEAD"],
  { stdio: "inherit" });
const config = git("show", "HEAD:autostudio/config.py");
const version = (config.match(/^VERSION\s*=\s*"([^"]+)"/m) || [])[1];
if (!version) throw new Error("VERSION not found in autostudio/config.py");

const update = {
  app: "AutoStudio",
  version,
  released: git("log", "-1", "--format=%cI"),
  notes: git("log", "-1", "--format=%s"),
  commit: git("rev-parse", "--short", "HEAD"),
  zip: { url: "/AutoStudio.zip", sha256: sha256("site/AutoStudio.zip"), size: statSync("site/AutoStudio.zip").size },
};

if (existsSync("autostudio/app/AutoStudio.apk")) {
  copyFileSync("autostudio/app/AutoStudio.apk", "site/AutoStudio.apk");
  const info = existsSync("autostudio/app/apk.json") ? JSON.parse(readFileSync("autostudio/app/apk.json", "utf8")) : {};
  update.apk = { url: "/AutoStudio.apk", version_code: info.version_code, version_name: info.version_name,
    sha256: sha256("site/AutoStudio.apk"), size: statSync("site/AutoStudio.apk").size };
}

writeFileSync("site/update.json", JSON.stringify(update, null, 2) + "\n");
console.log(`update.json: AutoStudio ${version}` + (update.apk ? `, Android ${update.apk.version_name} (${update.apk.version_code})` : ""));
