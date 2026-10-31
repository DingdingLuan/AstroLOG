#!/usr/bin/env python3
"""Verify readable server labels and source links with synthetic workspaces."""
import os
from pathlib import Path
import unittest
from urllib.parse import urlencode

from check_theme_browser import BrowserFixture


class HeaderTests(BrowserFixture, unittest.TestCase):
    def open_header(self, page, name):
        page.goto(self.url(urlencode({'server_name': name})))
        page.locator('#workspace').wait_for(state='visible')
        page.wait_for_function("document.querySelector('#connection-label').textContent==='Connected'")
        self.assertEqual(page.locator('#connection-server-name').inner_text(), name)

    def test_complete_server_label_is_readable_across_viewports_and_themes(self):
        name = 'Tianhe Xingyi · via aureum'
        for width in [320, 390, 768, 1360]:
            page = self.page(width=width)
            self.open_header(page, name)
            for color in ['light', 'dark']:
                page.emulate_media(color_scheme=color)
                self.expect(page, color)
                result = page.evaluate('''()=>{
                  const label=document.querySelector('#connection-server-name'),badge=label.parentElement;
                  const style=getComputedStyle(badge),rect=badge.getBoundingClientRect();
                  const source=document.querySelector('.source-link').getBoundingClientRect();
                  const theme=document.querySelector('#theme-mode-trigger').getBoundingClientRect();
                  const tabs=document.querySelector('.tabs').getBoundingClientRect();
                  return {fits:document.documentElement.scrollWidth<=innerWidth+1,
                    labelFits:label.scrollWidth<=label.clientWidth+1 && label.scrollHeight<=label.clientHeight+1,
                    size:parseFloat(style.fontSize),badgeInside:rect.left>=0 && rect.right<=innerWidth,
                    sourceInside:source.left>=0 && source.right<=innerWidth,
                    actionsSeparate:source.right<=theme.left+1,tabsSeparate:tabs.right<=rect.left || tabs.bottom<=rect.top+1};
                }''')
                self.assertTrue(result['fits'], (width, color, result))
                self.assertTrue(result['labelFits'], (width, color, result))
                self.assertGreaterEqual(result['size'], 12)
                self.assertTrue(result['badgeInside'] and result['sourceInside'] and result['actionsSeparate'] and result['tabsSeparate'], (width, color, result))
                evidence = os.environ.get('ASTROLOG_BROWSER_EVIDENCE')
                if evidence:
                    Path(evidence).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(evidence) / f'header-{width}-{color}.png'), full_page=True)

    def test_long_server_name_wraps_without_hiding_content(self):
        page = self.page(width=320)
        name = 'Tianhe Xingyi · via aureum · ' + 'very-long-machine-name-' * 5
        self.open_header(page, name)
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
        self.assertEqual(page.locator('#connection-server-name').evaluate('(e)=>getComputedStyle(e).textOverflow'), 'clip')
        self.assertGreater(page.locator('#connection-server-name').bounding_box()['height'], 24)

    def test_source_link_is_safe_and_points_to_the_project(self):
        page = self.page()
        self.open_header(page, 'Tianhe Xingyi · via aureum')
        source = page.get_by_role('link', name='View source on GitHub')
        self.assertEqual(source.get_attribute('href'), 'https://github.com/DingdingLuan/AstroLOG')
        self.assertEqual(source.get_attribute('target'), '_blank')
        self.assertEqual(set(source.get_attribute('rel').split()), {'noopener', 'noreferrer'})
        self.assertEqual(page.locator('meta[name="astrolog-ui-version"]').get_attribute('content'), '0.4.2')


if __name__ == '__main__':
    unittest.main(verbosity=2)
