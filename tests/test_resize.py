"""Offline regression suite: python -m unittest discover -s tests -v."""

from copy import deepcopy
import importlib
import json
from math import cos, isfinite, radians, sin
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

engine = importlib.import_module('engine.resize')
ROOT = Path(__file__).resolve().parents[1]
FORMATS = [(1080, 1920), (1280, 720), (728, 90), (820, 312),
           (1000, 1500), (300, 600), (1600, 400), (1080, 1080)]
TASKS = [json.loads(path.read_text()) for path in sorted((ROOT / 'input').glob('*.json'))]


def variants(source):
    yield 'original', deepcopy(source)
    for name in ('long_headline', 'long_subhead', 'extra_badges', 'many_texts',
                 'missing_asset', 'one_image', 'no_cta', 'no_groups'):
        result = deepcopy(source)
        texts = sorted((e for e in result['elements'] if e['type'] == 'text'),
                       key=lambda e: -e['font_size'])
        if name in ('long_headline', 'long_subhead'):
            text = texts[min(1 if name == 'long_subhead' else 0, len(texts) - 1)]
            text['content'] = (text['content'] + ' ') * 6
        elif name == 'extra_badges':
            for i in range(4):
                group = f'extra_badge_{i}'
                x, y = 20 + i * 150, source['canvas']['height'] * 0.3
                result['elements'].extend([
                    dict(id=group, type='shape', shape_type='rectangle', fill_color='rgba(90,40,20,0.5)',
                         x=x, y=y, width=140, height=40, group_id=group, z_index=3),
                    dict(id=group + '_text', type='text', content=f'BADGE {i}', font_family='Solstice Sans',
                         font_size=14, color='rgba(255,255,255,1)', x=x + 5, y=y + 5,
                         width=130, height=30, group_id=group, z_index=4),
                ])
        elif name == 'many_texts':
            for i in range(12):
                text = deepcopy(texts[-1])
                text.update(id=f'small_text_{i}', content=f'Additional detail {i}', font_size=12,
                            x=20 + (i % 4) * 200, y=150 + (i // 4) * 80, width=180, height=45)
                text.pop('group_id', None)
                result['elements'].append(text)
        elif name == 'missing_asset':
            next(e for e in result['elements'] if e['type'] == 'image')['asset_id'] = 'missing-test.png'
        elif name == 'one_image':
            images = [e for e in result['elements'] if e['type'] == 'image']
            keep = images[-1]['id']
            result['elements'] = [e for e in result['elements'] if e['type'] != 'image' or e['id'] == keep]
        elif name == 'no_cta':
            result['elements'] = [e for e in result['elements'] if not e['id'].startswith('cta')]
        elif name == 'no_groups':
            for element in result['elements']:
                element.pop('group_id', None)
        yield name, result


class ResizeTests(unittest.TestCase):
    def assert_contract(self, source, target, output):
        self.assertEqual(output['canvas'], target)
        self.assertEqual([e['id'] for e in output['elements']], [e['id'] for e in source['elements']])
        self.assertEqual(len(output['elements']), len(source['elements']))
        self.assertEqual(set(output), set(source))
        lowest_z = min(e.get('z_index', 0) for e in source['elements'])
        sw, sh = source['canvas']['width'], source['canvas']['height']
        width, height = target['width'], target['height']
        for original, result in zip(source['elements'], output['elements']):
            allowed = {'x', 'y', 'width', 'height'} | ({'font_size'} if original['type'] == 'text' else set())
            self.assertEqual({k: v for k, v in original.items() if k not in allowed},
                             {k: v for k, v in result.items() if k not in allowed})
            for key in ('x', 'y', 'width', 'height'):
                self.assertTrue(isfinite(result[key]) and result[key] >= 0, (result['id'], key))
            self.assertLessEqual(result['x'] + result['width'], width + 1e-8)
            self.assertLessEqual(result['y'] + result['height'], height + 1e-8)
            angle = radians(result.get('rotation', 0))
            rw = abs(cos(angle)) * result['width'] + abs(sin(angle)) * result['height']
            rh = abs(sin(angle)) * result['width'] + abs(cos(angle)) * result['height']
            cx, cy = result['x'] + result['width'] / 2, result['y'] + result['height'] / 2
            self.assertGreaterEqual(cx - rw / 2, -1e-8)
            self.assertGreaterEqual(cy - rh / 2, -1e-8)
            self.assertLessEqual(cx + rw / 2, width + 1e-8)
            self.assertLessEqual(cy + rh / 2, height + 1e-8)
            if original['type'] == 'image':
                covered = (max(0, min(sw, original['x'] + original['width']) - max(0, original['x'])) *
                           max(0, min(sh, original['y'] + original['height']) - max(0, original['y'])))
                crop = original.get('fit') == 'cover' and covered >= sw * sh * 0.9 and original.get('z_index', 0) == lowest_z
                if not crop and original['width'] and original['height']:
                    self.assertAlmostEqual(result['width'] / result['height'], original['width'] / original['height'], places=8)
            if original['type'] == 'text':
                self.assertTrue(isfinite(result['font_size']) and result['font_size'] >= 1)
        self.assertEqual(engine.validate_output(source, output, target), [])
        groups = {}
        for original, result in zip(source['elements'], output['elements']):
            if original.get('group_id') is not None:
                groups.setdefault(original['group_id'], []).append((original, result))
        for group in groups.values():
            original, result = group[0]
            scale = result['width'] / original['width']
            for old, new in group[1:]:
                self.assertAlmostEqual(new['x'] - result['x'], (old['x'] - original['x']) * scale)
                self.assertAlmostEqual(new['y'] - result['y'], (old['y'] - original['y']) * scale)
                self.assertAlmostEqual(new['width'], old['width'] * scale)
                self.assertAlmostEqual(new['height'], old['height'] * scale)

    def test_common_formats_and_variants(self):
        cases = 0
        for task in TASKS:
            for name, source in variants(task['source']):
                before = deepcopy(source)
                for width, height in FORMATS:
                    with self.subTest(task=task['task_id'], variant=name, size=(width, height)):
                        target = {'width': width, 'height': height}
                        output = engine.resize(source, target)
                        self.assert_contract(source, target, output)
                        self.assertEqual(source, before)
                        self.assertEqual(target, {'width': width, 'height': height})
                        self.assertIsNot(output['canvas'], target)
                        if name == 'original':
                            self.assertEqual(output, engine.resize(source, target))
                        cases += 1
        print(f'Validated {cases} sample/variant/format combinations.', flush=True)

    def test_common_sample_readability_and_banner_flow(self):
        for task in TASKS[:2]:
            source, target = task['source'], task['target_canvas']
            output = engine.resize(source, target)
            boxes = {e['id']: e for e in output['elements']}
            for element in output['elements']:
                if element['type'] == 'text':
                    self.assertGreaterEqual(element['font_size'], engine.readable_minimum(target))
                    mw, mh, _ = engine.text_extent(element, element['font_size'], element['width'])
                    self.assertLessEqual(mw, element['width'] + 1e-6)
                    self.assertLessEqual(mh, element['height'] + 1e-6)
            if 'product' in boxes:
                self.assertLess(boxes['product']['x'], boxes['headline']['x'])
                self.assertLess(boxes['headline']['x'], boxes['cta_bg']['x'])
            else:
                self.assertLess(boxes['logo']['y'], boxes['headline']['y'])
                self.assertLess(boxes['headline']['y'], boxes['cta']['y'])

    def test_inferred_buttons_and_layered_art(self):
        source = deepcopy(TASKS[1]['source'])
        for element in source['elements']:
            element.pop('group_id', None)
        target = TASKS[1]['target_canvas']
        output = engine.resize(source, target)
        boxes = {e['id']: e for e in output['elements']}
        for prefix in ('badge', 'cta'):
            bg, label = boxes[prefix + '_bg'], boxes[prefix + '_label']
            self.assertGreaterEqual(label['x'], bg['x'])
            self.assertGreaterEqual(label['y'], bg['y'])
            self.assertLessEqual(label['x'] + label['width'], bg['x'] + bg['width'] + 1e-8)
            self.assertLessEqual(label['y'] + label['height'], bg['y'] + bg['height'] + 1e-8)
        hero = boxes['product']
        for name in ('deco_circle_1', 'deco_circle_2'):
            decoration = boxes[name]
            intersection = (min(hero['x'] + hero['width'], decoration['x'] + decoration['width']) - max(hero['x'], decoration['x']))
            intersection *= (min(hero['y'] + hero['height'], decoration['y'] + decoration['height']) - max(hero['y'], decoration['y']))
            self.assertGreater(intersection, 0)

    def test_secondary_badge_does_not_expand_to_fill_footer(self):
        source = TASKS[1]['source']
        original = next(e for e in source['elements'] if e['id'] == 'badge_bg')
        for width, height in ((1080, 1920), (300, 600), (1600, 400)):
            target = {'width': width, 'height': height}
            output = engine.resize(source, target)
            badge = next(e for e in output['elements'] if e['id'] == 'badge_bg')
            area_scale = width * height / (source['canvas']['width'] * source['canvas']['height'])
            self.assertLessEqual(badge['width'] * badge['height'],
                                 original['width'] * original['height'] * area_scale + 1e-6)

    def test_failures_use_safe_baseline(self):
        for task in TASKS:
            for helper in ('detect_roles', 'group_elements', 'generate_candidates', 'score_layout'):
                with self.subTest(task=task['task_id'], failure=helper):
                    with patch.object(engine, helper, side_effect=RuntimeError('injected failure')):
                        output = engine.resize(task['source'], task['target_canvas'])
                    self.assert_contract(task['source'], task['target_canvas'], output)

    def test_missing_fonts_and_independent_working_directory(self):
        source = deepcopy(TASKS[0]['source'])
        for element in source['elements']:
            if element['type'] == 'text':
                element['font_family'] = 'Unavailable Test Font'
        target = TASKS[0]['target_canvas']
        self.assert_contract(source, target, engine.resize(source, target))
        previous = os.getcwd()
        try:
            with TemporaryDirectory() as directory:
                os.chdir(directory)
                engine._load_font.cache_clear()
                self.assertIsNotNone(engine._load_font('Solstice Serif', 'bold', 'normal'))
                self.assert_contract(source, target, engine.resize(source, target))
        finally:
            os.chdir(previous)

    def test_rotated_background_fallback_and_offline_execution(self):
        source = deepcopy(TASKS[0]['source'])
        source['elements'][0]['rotation'] = 17
        target = {'width': 1280, 'height': 720}
        with patch('socket.socket.connect', side_effect=AssertionError('network access')):
            self.assert_contract(source, target, engine.resize(source, target))
            with patch.object(engine, 'generate_candidates', side_effect=RuntimeError('injected failure')):
                self.assert_contract(source, target, engine.resize(source, target))

    def test_long_tokens_newlines_and_tight_leading(self):
        source = deepcopy(TASKS[0]['source'])
        headline = next(e for e in source['elements'] if e['id'] == 'headline')
        headline.update(content='Supercalifragilisticexpialidocious\nA second line', letter_spacing=1, line_height=1.05)
        target = {'width': 300, 'height': 600}
        output = engine.resize(source, target)
        self.assert_contract(source, target, output)
        headline = next(e for e in output['elements'] if e['id'] == 'headline')
        width, height, _ = engine.text_extent(headline, headline['font_size'], headline['width'])
        self.assertLessEqual(width, headline['width'] + 1e-6)
        self.assertLessEqual(height, headline['height'] + 1e-6)

    def test_validator_rejects_broken_contract(self):
        task = TASKS[1]
        output = engine.resize(task['source'], task['target_canvas'])
        for mutation in ('content', 'ratio', 'bounds', 'font', 'group'):
            broken = deepcopy(output)
            boxes = {e['id']: e for e in broken['elements']}
            if mutation == 'content':
                boxes['headline']['content'] = 'Changed copy'
            elif mutation == 'ratio':
                boxes['product']['width'] *= 1.2
            elif mutation == 'bounds':
                boxes['headline']['x'] = -10
            elif mutation == 'font':
                boxes['headline']['font_size'] = 0
            else:
                boxes['cta_label']['x'] += 1
            self.assertTrue(engine.validate_output(task['source'], broken, task['target_canvas']), mutation)


if __name__ == '__main__':
    unittest.main()
