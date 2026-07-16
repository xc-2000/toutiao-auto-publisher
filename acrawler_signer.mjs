import { readFileSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";

const SCRIPT_URL = "https://sf1-cdn-tos.toutiaostatic.com/obj/rc-web-sdk/acrawler.js";

async function main() {
  const input = JSON.parse(readFileSync(0, "utf8"));
  const response = await fetch(SCRIPT_URL, { signal: AbortSignal.timeout(10_000) });
  if (!response.ok) throw new Error(`acrawler download returned HTTP ${response.status}`);
  const source = await response.text();
  const virtualConsole = new VirtualConsole();
  const dom = new JSDOM("<!doctype html><html><head></head><body></body></html>", {
    url: "https://mp.toutiao.com/profile_v4/graphic/publish",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole,
  });

  try {
    const { window } = dom;
    const canvasContext = new Proxy({}, {
      get(_target, property) {
        if (property === "measureText") return (value) => ({ width: String(value || "").length * 7 });
        if (property === "getImageData" || property === "createImageData") {
          return () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 });
        }
        return () => undefined;
      },
    });
    window.HTMLCanvasElement.prototype.getContext = () => canvasContext;
    window.HTMLCanvasElement.prototype.toDataURL = () => "data:image/png;base64,Y2FudmFzLXNpZ25hdHVyZQ==";
    Object.defineProperty(window.navigator, "userAgent", {
      configurable: true,
      value: String(input.user_agent || ""),
    });
    for (const [name, value] of Object.entries(input.cookies || {})) {
      dom.cookieJar.setCookieSync(
        `${name}=${value}; Domain=.toutiao.com; Path=/`,
        "https://mp.toutiao.com/",
      );
    }
    window.console = { log() {}, info() {}, warn() {}, error() {}, debug() {} };
    window.fetch = async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
    window.eval(source);
    const acrawler = window.byted_acrawler;
    if (!acrawler?.init || !acrawler?.sign) throw new Error("acrawler API missing");
    await acrawler.init({ aid: Number(input.aid || 1231), dfp: true, intercept: false });
    const signature = await acrawler.sign({ url: String(input.url) });
    if (!signature) throw new Error("empty signature");
    process.stdout.write(JSON.stringify({ signature }));
  } finally {
    dom.window.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error?.message || error}\n`);
  process.exitCode = 1;
});
