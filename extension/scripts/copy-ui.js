// Copy the shared single-source UI (../ui/theater.html) into media/ so it ships
// inside the .vsix. At dev time (F5) the extension also falls back to ../ui.
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const src = path.resolve(root, "..", "ui", "theater.html");
const destDir = path.join(root, "media");
const dest = path.join(destDir, "theater.html");

fs.mkdirSync(destDir, { recursive: true });
fs.copyFileSync(src, dest);
console.log("copied " + src + " -> " + dest);
