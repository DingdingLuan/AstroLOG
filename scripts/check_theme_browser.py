#!/usr/bin/env python3
"""Appearance regression with synthetic API data and real browser media queries."""
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from check_scroll_browser import Handler as ScrollHandler, ROOT
from http.server import ThreadingHTTPServer

BOOTSTRAP = """
const originalThemeFetch=window.fetch;
window.fetch=async(input,options={})=>{
 const url=new URL(String(input),location.href),query=new URL(location).searchParams;
 if(url.pathname.endsWith('/api/auth/status') && query.has('gate'))
  return new Response(JSON.stringify({enabled:true,authenticated:false}),{status:200});
 const response=await originalThemeFetch(input,options);
 if(url.pathname.endsWith('/api/config')){
  const doc=await response.json();doc.display.theme=query.get('server_theme')||'light';
  if(query.has('server_name'))doc.display.server_name=query.get('server_name');
  return new Response(JSON.stringify(doc),{status:200});
 }
 return response;
};
"""


class Handler(ScrollHandler):
    def do_GET(self):
        if urlsplit(self.path).path != '/':
            return super().do_GET()
        html = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        if 'legacy=1' in self.path:
            html = html.replace('  <script src="./static/theme.js"></script>\n', '')
        html = html.replace('<script src="./app.js" defer></script>',
                            '<script src="./demo-adapter.js"></script><script>' + BOOTSTRAP + '</script><script src="./app.js" defer></script>')
        data = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class BrowserFixture:
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel=os.environ.get('ASTROLOG_BROWSER_CHANNEL'))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def page(self, scheme='dark', saved=None, blocked_storage=False, width=1360):
        context = self.browser.new_context(color_scheme=scheme, viewport={'width': width, 'height': 920})
        self.addCleanup(context.close)
        if blocked_storage:
            context.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw new DOMException('Blocked','SecurityError');}})")
        elif saved is not None:
            import json
            context.add_init_script("if(!sessionStorage.getItem('seeded')){localStorage.setItem('logpresent.theme'," + json.dumps(saved) + ");sessionStorage.setItem('seeded','1');}")
        if os.environ.get('ASTROLOG_BROWSER_BASE_URL'):
            context.add_init_script((ROOT / 'demo/demo-adapter.js').read_text(encoding='utf-8') + '\n' + BOOTSTRAP)
            context.route('**/api/**', lambda route: route.abort())
        return context.new_page()

    def url(self, query=''):
        base = os.environ.get('ASTROLOG_BROWSER_BASE_URL', f'http://127.0.0.1:{self.server.server_port}/')
        return base.rstrip('/') + '/?' + query

    def open(self, page, gate=False, extra=''):
        page.goto(self.url(('gate=1&' if gate else '') + extra))
        page.locator('#auth-theme-mode-trigger' if gate else '#theme-mode-trigger').wait_for(state='visible')
        page.wait_for_function("Boolean(document.querySelector('#theme-mode').dataset.bound)")

    def choose(self, page, value, gate=False):
        prefix = '#auth-theme-mode' if gate else '#theme-mode'
        page.locator(prefix + '-trigger').click()
        page.locator(prefix + '-menu').get_by_role('menuitemradio', name={'auto':'System','light':'Light','dark':'Dark'}[value], exact=True).click()

    def expect(self, page, color, mode='auto'):
        page.wait_for_function("expected=>document.documentElement.dataset.theme===expected[0] && document.documentElement.dataset.themeMode===expected[1]", arg=[color, mode])
        self.assertEqual(page.evaluate("getComputedStyle(document.documentElement).colorScheme"), color)
        self.assertEqual(page.locator('#theme-mode').input_value(), mode)
        self.assertEqual(page.locator('#auth-theme-mode').input_value(), mode)


class ThemeTests(BrowserFixture, unittest.TestCase):
    def test_dark_login_defaults_to_system(self):
        page = self.page()
        self.open(page, gate=True)
        self.expect(page, 'dark')
        self.assertIsNone(page.evaluate("localStorage.getItem('logpresent.theme')"))

    def test_light_login_defaults_to_system(self):
        page = self.page('light')
        self.open(page, gate=True)
        self.expect(page, 'light')

    def test_server_light_does_not_override_system_and_changes_are_live(self):
        page = self.page()
        self.open(page, extra='server_theme=light')
        self.expect(page, 'dark')
        page.emulate_media(color_scheme='light')
        self.expect(page, 'light')
        page.emulate_media(color_scheme='dark')
        self.expect(page, 'dark')

    def test_manual_choice_is_remembered_and_system_can_be_restored(self):
        page = self.page()
        self.open(page)
        self.choose(page, 'light')
        self.expect(page, 'light', 'light')
        page.reload()
        self.expect(page, 'light', 'light')
        self.choose(page, 'auto')
        self.expect(page, 'dark')
        page.emulate_media(color_scheme='light')
        self.expect(page, 'light')
        self.assertEqual(page.evaluate("localStorage.getItem('logpresent.theme')"), 'auto')

    def test_existing_manual_preferences_are_preserved(self):
        for saved in ['light', 'dark']:
            with self.subTest(saved=saved):
                page = self.page('dark' if saved == 'light' else 'light', saved=saved)
                self.open(page)
                self.expect(page, saved, saved)

    def test_invalid_saved_value_falls_back_to_system(self):
        page = self.page(saved='unsupported')
        self.open(page)
        self.expect(page, 'dark')

    def test_storage_blocked_still_follows_system(self):
        page = self.page(blocked_storage=True)
        self.open(page)
        self.expect(page, 'dark')
        page.emulate_media(color_scheme='light')
        self.expect(page, 'light')
        self.choose(page, 'dark')
        self.expect(page, 'dark', 'dark')

    def test_login_control_and_workspace_control_share_preference(self):
        page = self.page()
        self.open(page, gate=True)
        self.choose(page, 'light', gate=True)
        self.expect(page, 'light', 'light')
        self.open(page)
        self.expect(page, 'light', 'light')

    def test_preference_changes_sync_between_tabs(self):
        page = self.page()
        self.open(page)
        other = page.context.new_page()
        self.open(other)
        self.choose(page, 'light')
        self.expect(other, 'light', 'light')
        self.choose(other, 'auto')
        self.expect(page, 'dark')

    def test_system_theme_is_applied_before_main_application_loads(self):
        page = self.page()
        page.route('**/app.js', lambda route: route.abort())
        page.goto(self.url('gate=1'))
        page.wait_for_function("document.documentElement.dataset.theme==='dark'")
        self.assertEqual(page.evaluate("getComputedStyle(document.documentElement).colorScheme"), 'dark')

    def test_cached_html_without_theme_script_loads_controller(self):
        page = self.page()
        if os.environ.get('ASTROLOG_BROWSER_BASE_URL'):
            def old_html(route):
                response = route.fetch()
                route.fulfill(response=response, body=response.text().replace('  <script src="./static/theme.js"></script>\n', ''))
            page.route('**/?legacy=1', old_html)
        self.open(page, extra='legacy=1')
        self.expect(page, 'dark')

    def test_mobile_login_and_workspace_fit_in_both_themes(self):
        page = self.page(width=390)
        evidence = os.environ.get('ASTROLOG_BROWSER_EVIDENCE')
        for gate in [True, False]:
            self.open(page, gate=gate)
            for color in ['light', 'dark']:
                page.emulate_media(color_scheme=color)
                self.expect(page, color)
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
                control = page.locator('#auth-theme-mode-trigger' if gate else '#theme-mode-trigger').bounding_box()
                self.assertGreaterEqual(control['x'], 0)
                self.assertLessEqual(control['x'] + control['width'], 391)
                if evidence:
                    Path(evidence).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(evidence) / f'theme-mobile-{color}-{"login" if gate else "workspace"}.png'), full_page=True)

    def test_open_menu_fits_all_viewports_and_themes(self):
        for width in [320, 390, 768, 1360]:
            page = self.page(width=width)
            self.open(page)
            for color in ['light', 'dark']:
                page.emulate_media(color_scheme=color)
                self.expect(page, color)
                page.locator('#theme-mode-trigger').click()
                menu = page.locator('#theme-mode-menu')
                self.assertTrue(menu.is_visible())
                self.assertEqual(menu.get_by_role('menuitemradio').count(), 3)
                for label in ['System', 'Light', 'Dark']:
                    self.assertTrue(menu.get_by_role('menuitemradio', name=label, exact=True).is_visible())
                self.assertEqual(menu.locator('[aria-checked="true"]').inner_text(), 'System')
                layout = menu.evaluate('''e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {
                  fits:r.left>=11 && r.right<=innerWidth-11 && r.top>=11 && r.bottom<=innerHeight-11,
                  solid:s.backgroundColor===getComputedStyle(document.querySelector('.viewer-card')).backgroundColor,
                  labels:[...e.querySelectorAll('.theme-option-label')].every(x=>getComputedStyle(x).display!=='none')
                }}''')
                self.assertTrue(layout['fits'] and layout['labels'], (width,color,layout))
                self.assertNotEqual(menu.evaluate('e=>getComputedStyle(e).backgroundColor'), 'rgba(0, 0, 0, 0)')
                evidence = os.environ.get('ASTROLOG_BROWSER_EVIDENCE')
                if evidence:
                    Path(evidence).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(evidence) / f'appearance-menu-{width}-{color}.png'))
                    self.assertTrue(menu.is_visible())
                page.keyboard.press('Escape')

    def test_menu_keyboard_navigation_selection_and_escape(self):
        page = self.page()
        self.open(page)
        trigger = page.locator('#theme-mode-trigger')
        trigger.focus()
        page.keyboard.press('ArrowDown')
        self.assertEqual(page.locator(':focus').inner_text(), 'System')
        page.keyboard.press('ArrowDown')
        self.assertEqual(page.locator(':focus').inner_text(), 'Light')
        page.keyboard.press('End')
        self.assertEqual(page.locator(':focus').inner_text(), 'Dark')
        page.keyboard.press('Home')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        self.expect(page, 'light', 'light')
        self.assertEqual(page.locator(':focus').get_attribute('id'), 'theme-mode-trigger')
        page.keyboard.press('ArrowUp')
        page.keyboard.press('Escape')
        self.assertFalse(page.locator('#theme-mode-menu').is_visible())
        self.assertEqual(trigger.get_attribute('aria-expanded'), 'false')

    def test_outside_click_and_tab_dismiss_without_changing_preference(self):
        page = self.page()
        self.open(page)
        page.locator('#theme-mode-trigger').click()
        page.locator('#project-title').click()
        self.assertFalse(page.locator('#theme-mode-menu').is_visible())
        self.expect(page, 'dark')
        page.locator('#theme-mode-trigger').click()
        page.keyboard.press('Tab')
        self.assertFalse(page.locator('#theme-mode-menu').is_visible())
        self.assertNotEqual(page.locator(':focus').get_attribute('role'), 'menuitemradio')
        self.expect(page, 'dark')

    def test_login_menu_repositions_in_short_viewport(self):
        page = self.page(width=320)
        page.set_viewport_size({'width':320,'height':600})
        self.open(page, gate=True)
        page.locator('#auth-theme-mode-trigger').click()
        menu = page.locator('#auth-theme-mode-menu')
        self.assertTrue(menu.is_visible())
        box = menu.bounding_box()
        self.assertGreaterEqual(box['y'], 12)
        self.assertLessEqual(box['y'] + box['height'], 589)
        menu.get_by_role('menuitemradio', name='Light', exact=True).click()
        self.expect(page, 'light', 'light')

    def test_rebinding_does_not_duplicate_controls(self):
        page = self.page()
        self.open(page)
        page.evaluate('AstroLOGTheme.bind();AstroLOGTheme.bind()')
        self.assertEqual(page.locator('.theme-trigger').count(), 2)
        self.assertEqual(page.locator('.theme-menu').count(), 2)
        self.choose(page, 'light')
        self.expect(page, 'light', 'light')


if __name__ == '__main__':
    unittest.main(verbosity=2)
