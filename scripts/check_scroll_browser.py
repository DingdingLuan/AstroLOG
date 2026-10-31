#!/usr/bin/env python3
"""Real UI plus synthetic logs: test scroll intent with delayed tail replies."""
from __future__ import annotations

import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HARNESS = """<script>
window.__tails=0;window.__tailAborts=0;
window.__tailDelay=Number(new URL(location).searchParams.get('delay'))||0;
const syntheticFetch=window.fetch;
window.fetch=async(input,options={})=>{
 const response=syntheticFetch(input,options);
 if(new URL(String(input),location.href).pathname.endsWith('/tail')){
  window.__tails++;const delay=window.__tailDelay;
  if(delay)await new Promise((resolve,reject)=>{
   const timer=setTimeout(resolve,delay),signal=options.signal;
   const abort=()=>{clearTimeout(timer);window.__tailAborts++;reject(new DOMException('Aborted','AbortError'));};
   if(!window.__ignoreAbort){if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true});}
  });
 }
 return response;
};
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        name = urlsplit(self.path).path
        if name == "/demo-adapter.js":
            source = ROOT / "demo/demo-adapter.js"
        elif name == "/static/theme.js":
            source = ROOT / "web/theme.js"
        else:
            source = ROOT / "web" / ("index.html" if name == "/" else name.lstrip("/"))
        if source.parent not in {ROOT / "web", ROOT / "demo"} or not source.is_file():
            self.send_error(404)
            return
        data = source.read_bytes()
        if name == "/":
            data = data.decode("utf-8").replace(
                '<script src="./app.js" defer></script>',
                '<script src="/demo-adapter.js"></script>' + HARNESS + '<script src="./app.js" defer></script>',
            ).encode("utf-8")
        mime = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}
        self.send_response(200)
        self.send_header("Content-Type", mime.get(source.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel=os.environ.get("ASTROLOG_BROWSER_CHANNEL"))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1360, "height": 920}, reduced_motion="reduce")
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        if os.environ.get("ASTROLOG_BROWSER_BASE_URL"):
            adapter = (ROOT / "demo/demo-adapter.js").read_text(encoding="utf-8")
            harness = HARNESS.removeprefix("<script>").removesuffix("</script>")
            self.page.add_init_script(adapter + "\n" + harness)
            self.page.route("**/api/**", lambda route: route.abort())

    def url(self, delay=0):
        base = os.environ.get("ASTROLOG_BROWSER_BASE_URL", f"http://127.0.0.1:{self.server.server_port}/")
        return base.rstrip("/") + f"/?delay={delay}"

    def open(self, delay=0):
        self.page.goto(self.url(delay))
        self.page.locator("#log-lines .log-line").first.wait_for()
        self.page.wait_for_timeout(350)

    def snapshot(self):
        return self.page.evaluate("""()=>{const v=document.querySelector('#log-viewport');return {
          following:document.querySelector('#follow-toggle').getAttribute('aria-pressed')==='true',
          top:v.scrollTop,distance:v.scrollHeight-v.clientHeight-v.scrollTop,
          loading:!document.querySelector('#log-loading').hidden,tails:window.__tails,aborts:window.__tailAborts,
          first:document.querySelector('#log-lines .log-line')?.dataset.start,
          last:document.querySelector('#log-lines .log-line:last-child')?.dataset.start
        };}""")

    def pending_resume(self, ignore_abort=False):
        self.open()
        self.page.evaluate("ignore=>{window.__tailDelay=1500;window.__ignoreAbort=ignore;}", ignore_abort)
        self.page.locator("#follow-toggle").click()
        self.page.locator("#jump-latest-button").click()
        self.page.wait_for_function("window.__tails===2")
        evidence = os.environ.get("ASTROLOG_BROWSER_EVIDENCE")
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            width = self.page.viewport_size["width"]
            self.page.screenshot(path=str(Path(evidence) / f"loading-latest-{width}.png"), full_page=True)

    def scroll_up(self):
        box = self.page.locator("#log-viewport").bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        self.page.mouse.wheel(0, -500)
        self.page.wait_for_timeout(180)

    def test_scroll_up_cancels_pending_latest_without_replacing_visible_lines(self):
        self.pending_resume()
        self.scroll_up()
        paused = self.snapshot()
        self.assertFalse(paused["following"])
        self.assertFalse(paused["loading"], "Loading must stop when the reader scrolls away")
        self.assertGreater(paused["distance"], 100, "Loading overlay must not swallow the wheel")
        self.assertEqual(paused["aborts"], 1)
        self.page.wait_for_timeout(1750)
        after = self.snapshot()
        self.assertFalse(after["following"])
        self.assertAlmostEqual(after["top"], paused["top"], delta=3)
        self.assertEqual((after["first"], after["last"]), (paused["first"], paused["last"]))
        self.assertEqual(after["tails"], 2)
        evidence = os.environ.get("ASTROLOG_BROWSER_EVIDENCE")
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(evidence) / "paused-after-slow-reply.png"), full_page=True)
        self.assertEqual(self.errors, [])

    def test_programmatic_bottom_scroll_does_not_resume(self):
        self.open()
        self.page.locator("#follow-toggle").click()
        self.page.evaluate("document.querySelector('#log-viewport').dispatchEvent(new Event('scroll'))")
        self.page.wait_for_timeout(400)
        result = self.snapshot()
        self.assertFalse(result["following"])
        self.assertEqual(result["tails"], 1)

    def test_late_reply_cannot_override_a_pause_even_if_transport_ignores_abort(self):
        self.pending_resume(ignore_abort=True)
        self.scroll_up()
        before = self.snapshot()
        self.page.wait_for_timeout(1750)
        result = self.snapshot()
        self.assertFalse(result["following"])
        self.assertAlmostEqual(result["top"], before["top"], delta=3)
        self.assertEqual(result["tails"], 2)
        self.assertEqual(result["aborts"], 0)

    def test_scrollbar_can_interrupt_pending_latest(self):
        self.pending_resume()
        self.page.evaluate("""()=>{const v=document.querySelector('#log-viewport');v.dispatchEvent(new PointerEvent('pointerdown'));v.scrollTop-=400;}""")
        self.page.wait_for_timeout(180)
        self.page.evaluate("window.dispatchEvent(new PointerEvent('pointerup'))")
        result = self.snapshot()
        self.assertFalse(result["following"])
        self.assertFalse(result["loading"])
        self.assertEqual(result["aborts"], 1)

    def test_touch_can_interrupt_pending_latest(self):
        self.pending_resume()
        self.page.evaluate("""()=>{const v=document.querySelector('#log-viewport');
          for(const [type,y] of [['touchstart',250],['touchmove',450]]){
            const touch=new Touch({identifier:0,target:v,clientX:100,clientY:y});
            v.dispatchEvent(new TouchEvent(type,{touches:[touch],bubbles:true}));
          }
          v.scrollTop-=300;
        }""")
        self.page.wait_for_timeout(180)
        result = self.snapshot()
        self.assertFalse(result["following"])
        self.assertFalse(result["loading"])
        self.assertEqual(result["aborts"], 1)

    def test_explicit_resume_still_reaches_latest(self):
        self.open()
        self.scroll_up()
        self.assertFalse(self.snapshot()["following"])
        self.page.evaluate("window.__tailDelay=450")
        self.page.locator("#jump-latest-button").click()
        self.page.wait_for_function("document.querySelector('#follow-toggle').getAttribute('aria-pressed')==='true' && document.querySelector('#log-loading').hidden")
        self.page.wait_for_timeout(150)
        result = self.snapshot()
        self.assertTrue(result["following"])
        self.assertLess(result["distance"], 5)
        self.assertEqual(result["tails"], 2)

    def test_scrolling_back_to_bottom_can_resume(self):
        self.open()
        self.scroll_up()
        self.assertFalse(self.snapshot()["following"])
        self.page.mouse.wheel(0, 4000)
        self.page.wait_for_function("document.querySelector('#follow-toggle').getAttribute('aria-pressed')==='true'")
        self.assertLess(self.snapshot()["distance"], 5)

    def test_keyboard_can_interrupt_pending_latest(self):
        self.pending_resume()
        self.page.locator("#log-viewport").focus()
        self.page.keyboard.press("PageUp")
        self.page.wait_for_timeout(180)
        result = self.snapshot()
        self.assertFalse(result["following"])
        self.assertFalse(result["loading"])
        self.assertEqual(result["aborts"], 1)
        self.assertGreater(result["distance"], 100)

    def test_initial_slow_open_respects_pause_intent(self):
        self.page.goto(self.url(1500))
        self.page.wait_for_function("window.__tails===1")
        self.scroll_up()
        self.page.locator("#log-lines .log-line").first.wait_for()
        self.page.wait_for_timeout(200)
        self.assertFalse(self.snapshot()["following"])

    def test_mobile_pending_latest_can_be_interrupted(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.pending_resume()
        self.scroll_up()
        self.assertFalse(self.snapshot()["loading"])
        self.assertFalse(self.snapshot()["following"])
        self.page.wait_for_timeout(1750)
        self.assertFalse(self.snapshot()["following"])
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))

    def snapshot_file(self):
        self.open()
        self.page.evaluate("""async()=>{
          const base=new URL('./',document.baseURI),original=window.fetch;
          window.__snapshot=await (await original(new URL('api/logs/observatory.run/tail?lines=100',base))).json();
          window.__snapshot.mtime=Date.now()/1000-120;
          window.__snapshotDelay=0;window.__snapshotStreams=[];
          window.fetch=async(input,options={})=>{
            if(new URL(String(input),base).pathname.endsWith('/tail')){
              const body=JSON.stringify(window.__snapshot);
              await new Promise(resolve=>setTimeout(resolve,window.__snapshotDelay));
              return new Response(body,{status:200,headers:{'Content-Type':'application/json'}});
            }
            return original(input,options);
          };
          window.EventSource=class extends EventTarget{
            constructor(){super();this.closed=false;window.__snapshotStreams.push(this);
              queueMicrotask(()=>{if(!this.closed){this.onopen?.(new Event('open'));this.emit('status',{connected:true,exists:true,size_bytes:window.__snapshot.size_bytes,mtime:window.__snapshot.mtime});}});
            }
            emit(type,data){this.dispatchEvent(new MessageEvent(type,{data:JSON.stringify(data)}));}
            close(){this.closed=true;}
          };
          window.__rewriteSnapshot=change=>{
            window.__snapshot.generation+='-replacement';
            if(change){const row=window.__snapshot.lines[10];row.text=row.text.replace('INFO','WARN');}
            window.__snapshotStreams.at(-1).emit('reset',{file_id:'observatory.run',generation:window.__snapshot.generation});
          };
        }""")
        self.page.locator("#follow-toggle").click()
        self.page.locator("#jump-latest-button").click()
        self.page.wait_for_function("window.__snapshotStreams.length===1 && document.querySelector('#log-loading').hidden")
        self.page.wait_for_timeout(180)
        self.page.evaluate("""()=>{
          window.__originalRows=[...document.querySelector('#log-lines').children];window.__rowMutations=0;window.__animationChanges=0;
          new MutationObserver(rows=>{window.__rowMutations+=rows.filter(r=>r.type==='childList').length;window.__animationChanges+=rows.filter(r=>r.type==='attributes').length;})
            .observe(document.querySelector('#log-lines'),{childList:true,attributes:true,attributeFilter:['class']});
        }""")

    def test_identical_atomic_replacement_keeps_rows_without_flashing(self):
        self.snapshot_file()
        title = self.page.locator("#log-update-time").get_attribute("title")
        for count in range(3):
            self.page.evaluate("window.__rewriteSnapshot(false)")
            self.page.wait_for_function("n=>window.__snapshotStreams.length===n", arg=count + 2)
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.evaluate("window.__rowMutations"), 0)
        self.assertEqual(self.page.evaluate("window.__animationChanges"), 0)
        self.assertEqual(self.page.locator("#log-update-time").get_attribute("title"), title)
        self.assertTrue(self.page.locator("#viewer-notice").is_hidden())

    def test_changed_snapshot_only_replaces_changed_row(self):
        self.snapshot_file()
        self.page.evaluate("window.__rewriteSnapshot(true)")
        self.page.wait_for_function("window.__snapshotStreams.length===2")
        self.assertIn("WARN", self.page.locator("#log-lines .log-line").nth(10).inner_text())
        self.assertEqual(self.page.evaluate("[...document.querySelector('#log-lines').children].filter((row,i)=>row===window.__originalRows[i]).length"), 99)
        self.assertEqual(self.page.evaluate("window.__animationChanges"), 0)
        self.assertEqual(self.errors, [])

    def test_idle_status_does_not_claim_a_new_file_update(self):
        self.snapshot_file()
        title = self.page.locator("#log-update-time").get_attribute("title")
        for _ in range(3):
            self.page.evaluate("window.__snapshotStreams.at(-1).emit('status',{connected:true,exists:true,size_bytes:window.__snapshot.size_bytes,mtime:window.__snapshot.mtime})")
        self.assertEqual(self.page.locator("#log-update-time").get_attribute("title"), title)
        self.assertNotIn("just now", self.page.locator("#log-update-time").inner_text())
        self.assertEqual(self.page.evaluate("window.__rowMutations"), 0)

    def test_paused_snapshot_is_not_replaced_until_resume(self):
        self.snapshot_file()
        self.scroll_up()
        before = self.snapshot()
        self.page.evaluate("window.__rewriteSnapshot(true)")
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.evaluate("window.__snapshotStreams.length"), 1)
        self.assertEqual(self.page.evaluate("window.__rowMutations"), 0)
        self.assertFalse(self.snapshot()["following"])
        self.assertAlmostEqual(self.snapshot()["top"], before["top"], delta=3)

    def test_slow_snapshot_reply_does_not_override_scroll_pause(self):
        self.snapshot_file()
        self.page.evaluate("window.__snapshotDelay=700;window.__rewriteSnapshot(true)")
        self.scroll_up()
        before = self.snapshot()
        self.page.wait_for_timeout(900)
        self.assertFalse(self.snapshot()["following"])
        self.assertAlmostEqual(self.snapshot()["top"], before["top"], delta=3)
        self.assertEqual(self.page.evaluate("window.__rowMutations"), 0)

    def test_snapshot_can_shrink_to_empty_without_stale_rows(self):
        self.snapshot_file()
        self.page.evaluate("window.__snapshot.lines=window.__snapshot.lines.slice(0,3);window.__rewriteSnapshot(false)")
        self.page.wait_for_function("document.querySelector('#log-lines').children.length===3")
        self.page.evaluate("window.__snapshot.lines=[];window.__snapshot.size_bytes=0;window.__snapshot.cursor=0;window.__snapshot.older_cursor=0;window.__snapshot.newer_cursor=0;window.__snapshot.has_more=false;window.__rewriteSnapshot(false)")
        self.page.wait_for_function("document.querySelector('#log-lines').children.length===0")
        self.assertTrue(self.page.locator("#log-empty").is_visible())
        self.assertTrue(self.snapshot()["following"])
        self.assertEqual(self.errors, [])

    def test_mobile_snapshot_refresh_does_not_flash_or_overflow(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.snapshot_file()
        self.page.evaluate("window.__rewriteSnapshot(false)")
        self.page.wait_for_function("window.__snapshotStreams.length===2")
        self.assertEqual(self.page.evaluate("window.__rowMutations"), 0)
        self.assertEqual(self.page.evaluate("window.__animationChanges"), 0)
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
