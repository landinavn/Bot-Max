// Local static QA only. Production serves dist/ directly with Nginx.
import http from "node:http";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
const root = path.resolve("dist");
const mime = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".xml": "application/xml",
  ".txt": "text/plain",
  ".mp4": "video/mp4",
  ".mp3": "audio/mpeg",
};
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    let file = path.resolve(root, "." + decodeURIComponent(url.pathname));
    if (file !== root && !file.startsWith(root + path.sep)) {
      res.writeHead(403);
      res.end();
      return;
    }
    let status = 200;
    try {
      if ((await stat(file)).isDirectory())
        file = path.join(file, "index.html");
      await stat(file);
    } catch {
      file = path.join(root, "404.html");
      status = 404;
    }
    const data = await readFile(file);
    res.writeHead(status, {
      "Content-Type": mime[path.extname(file)] ?? "application/octet-stream",
      "X-Robots-Tag": "noindex, nofollow",
      "Cache-Control": "no-store",
    });
    res.end(data);
  } catch {
    res.writeHead(400);
    res.end("Bad request");
  }
});
server.listen(Number(process.env.PORT ?? 4173), "127.0.0.1", () =>
  console.log("Static preview: http://127.0.0.1:" + server.address().port),
);
