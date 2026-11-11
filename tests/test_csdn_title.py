"""Offline title activation contract. No live click, browser, model or account."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from personal_web_agent import csdn


def controls(visible):
    return {'input_count':1,'activator_count':1,'input_visible':visible,'activator_visible':not visible}


def state(index=847, visible=True):
    node=SimpleNamespace(attributes={'class':'article-bar__title--input'},is_visible=visible,
                         get_all_children_text=lambda:'')
    return SimpleNamespace(url=csdn.EDITOR_URL,dom_state=SimpleNamespace(selector_map={index:node}))


class TitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_native_activation_then_fresh_index_without_typing(self):
        element=SimpleNamespace(click=AsyncMock())
        page=SimpleNamespace(get_elements_by_css_selector=AsyncMock(return_value=[element]))
        browser=SimpleNamespace(get_browser_state_summary=AsyncMock(return_value=state()))
        saved=[]
        with patch.object(csdn,'title_controls',new=AsyncMock(side_effect=[controls(False),controls(True)])):
            result=await csdn.prepare_title_control(page,browser,lambda n,d:saved.append((n,d)),draft_authorized=True)
        element.click.assert_awaited_once_with()
        page.get_elements_by_css_selector.assert_awaited_once_with('.article-bar__title-display')
        browser.get_browser_state_summary.assert_awaited_once_with(include_screenshot=False,cached=False)
        self.assertEqual(result['title_index'],847)
        self.assertTrue(result['activated'])
        self.assertEqual(saved[-1][0],'title_readiness.json')

    async def test_already_visible_title_uses_observed_index_without_extra_click(self):
        page=SimpleNamespace(get_elements_by_css_selector=AsyncMock())
        browser=SimpleNamespace(get_browser_state_summary=AsyncMock(return_value=state(432)))
        with patch.object(csdn,'title_controls',new=AsyncMock(return_value=controls(True))):
            result=await csdn.prepare_title_control(page,browser,lambda *a:None,draft_authorized=True)
        self.assertFalse(result['activated']);self.assertEqual(result['title_index'],432)
        page.get_elements_by_css_selector.assert_not_called()

    async def test_unauthorized_activation_has_no_page_access(self):
        with patch.object(csdn,'title_controls',new=AsyncMock()) as read:
            with self.assertRaises(ValueError):await csdn.prepare_title_control(object(),object(),lambda *a:None)
            read.assert_not_called()

    async def test_ambiguous_activator_stops_for_manual_check(self):
        element=SimpleNamespace(click=AsyncMock())
        page=SimpleNamespace(get_elements_by_css_selector=AsyncMock(return_value=[element,element]))
        saved=[]
        with patch.object(csdn,'title_controls',new=AsyncMock(return_value=controls(False))):
            with self.assertRaisesRegex(ValueError,'MANUAL_REQUIRED'):
                await csdn.prepare_title_control(page,object(),lambda n,d:saved.append(d),draft_authorized=True)
        element.click.assert_not_called();self.assertIn('error',saved[-1])

    async def test_activation_does_not_invent_index_if_fresh_map_has_no_title(self):
        browser=SimpleNamespace(get_browser_state_summary=AsyncMock(return_value=SimpleNamespace(dom_state=SimpleNamespace(selector_map={}))))
        with patch.object(csdn,'title_controls',new=AsyncMock(return_value=controls(True))):
            with self.assertRaisesRegex(ValueError,'NATIVE_INDEX_UNAVAILABLE'):
                await csdn.prepare_title_control(object(),browser,lambda *a:None,draft_authorized=True)


class IndexGuardTests(unittest.TestCase):
    def test_nonexistent_zero_is_rejected_not_remapped_and_evidence_is_redacted(self):
        s=state();s.dom_state.selector_map[847].attributes['value']='PRIVATE_VALUE'
        output=SimpleNamespace(action=[SimpleNamespace(model_dump=lambda **kw:{'input':{'index':0,'text':'PRIVATE_TEXT'}})])
        evidence=csdn.index_evidence(s,output)
        self.assertEqual(evidence['observed_indices'],[847])
        self.assertFalse(evidence['proposed_actions'][0]['index_present'])
        self.assertNotIn('PRIVATE',json.dumps(evidence))
        with self.assertRaisesRegex(ValueError,'INDEX_NOT_OBSERVED:0'):
            csdn.validate_actions(SimpleNamespace(),s,output,set())

    def test_hidden_input_rejected_even_if_index_exists(self):
        output=SimpleNamespace(action=[SimpleNamespace(model_dump=lambda **kw:{'input':{'index':847,'text':'test'}})])
        with self.assertRaisesRegex(ValueError,'INPUT_NOT_VISIBLE'):
            csdn.validate_actions(SimpleNamespace(),state(visible=False),output,set())
