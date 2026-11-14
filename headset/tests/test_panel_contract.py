import pathlib
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT/'headset/src/dev/pico/etenhance'


class RootlessPanelContract(unittest.TestCase):
    def test_panel_declares_no_privileged_or_network_permissions(self):
        manifest = ET.parse(ROOT/'headset/AndroidManifest.xml')
        self.assertEqual(manifest.getroot().get('package'), 'dev.pico.etenhance')
        self.assertEqual(manifest.findall('uses-permission'), [])
        for path in (ROOT/'headset/src').rglob('*.java'):
            source = path.read_text(encoding='utf-8')
            for forbidden in ('ProcessBuilder', 'Runtime.getRuntime', 'android.net.', 'java.net.', '/data/adb/', '"su"'):
                with self.subTest(path=path, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_bilingual_resources_match(self):
        values = []
        for directory in ('values', 'values-zh'):
            strings = [item for path in (ROOT/f'headset/res/{directory}').glob('*.xml')
                       for item in ET.parse(path).findall('string')]
            keys = [item.get('name') for item in strings]
            self.assertEqual(len(keys), len(set(keys)))
            values.append(set(keys))
        self.assertEqual(values[0], values[1])
        self.assertGreater(len(values[0]), 300)

    def test_every_native_field_has_one_typed_control_with_matching_bounds_and_default(self):
        native = (ROOT/'hook/src/expression/postprocessing.h').read_text()
        source = (SOURCE/'ProcessingSettings.java').read_text()
        expected = re.findall(r'X\(\w+, "([^"]+)", ([^,]+), ([^,]+), ([^,]+), (true|false)\)', native)
        actual = re.findall(r'new Field\("([^"]+)", "([^"]+)", "([^"]+)", ([^,]+), ([^,]+), ([^,]+), ([^)]+)\)', source)
        self.assertEqual(len(expected), 44)
        self.assertEqual(len(actual), 44)
        self.assertEqual(len({row[0] for row in actual}), 44)
        number = lambda text: float.fromhex(text) if '0x' in text else float(text)
        for (key, initial, low, high, integer), (key2, section, group, initial2, low2, high2, step) in zip(expected, actual):
            with self.subTest(key=key):
                self.assertEqual(key, key2)
                self.assertEqual(tuple(map(number, (initial, low, high))), tuple(map(number, (initial2, low2, high2))))
                self.assertIn(section, ('eye', 'brow', 'mouth', 'tongue'))
                self.assertEqual(integer == 'true', key.endswith('.enabled'))

    @staticmethod
    def resources(directory):
        return {item.get('name'): item.text for path in (ROOT/f'headset/res/{directory}').glob('*.xml')
                for item in ET.parse(path).findall('string')}

    @staticmethod
    def fields():
        return re.findall(r'new Field\("([^"]+)", "([^"]+)", "([^"]+)"', (SOURCE/'ProcessingSettings.java').read_text())

    @staticmethod
    def moments():
        """{movement id: (part, metrics, has a context line)} from Scope and Catalog."""
        scope = (SOURCE/'Scope.java').read_text()
        catalog = (SOURCE/'Catalog.java').read_text()
        ids = re.findall(r'new Moment\("(\w+)"', catalog)
        result = {}
        for sid, part, context, rest in re.findall(r'new Scenario\("(\w+)", "(\w+)", \w+, ([-\w]+), (.*?)\),\n', scope):
            result[sid] = (part, re.findall(r'"(\w+)"', rest), context != '-1')
        assert set(ids) == set(result), (ids, result)
        return result

    def test_every_field_movement_and_step_has_bilingual_text(self):
        moments = self.moments()
        self.assertEqual(len(moments), 15)
        steps = re.findall(r'"(\w+)"', re.search(r'STEPS = \{(.*?)\};', (SOURCE/'Catalog.java').read_text(), re.S)[1])
        self.assertEqual(len(steps), 18)
        for directory in ('values', 'values-zh'):
            resources = self.resources(directory)
            for key, section, group in self.fields():
                label = 'p_'+key.replace('.', '_')
                suffixes = ('', '_hint', '_tech') if key.endswith('.enabled') else ('', '_less', '_more', '_tech')
                for suffix in suffixes:
                    with self.subTest(directory=directory, key=label+suffix):
                        self.assertTrue(resources[label+suffix])
                if not key.endswith('.enabled'):
                    self.assertNotEqual(resources[label+'_less'], resources[label+'_more'])
            for moment, (part, metrics, context) in moments.items():
                for suffix in ('', '_look', '_pro'):
                    with self.subTest(directory=directory, key=f'm_{moment}{suffix}'):
                        self.assertTrue(resources[f'm_{moment}{suffix}'])
                self.assertGreater(len(resources[f'm_{moment}_pro']), 60)
                for metric in metrics:
                    self.assertTrue(resources[f'r_{moment}_{metric}'])
                self.assertEqual(f'ctx_{moment}' in resources, context, moment)
            for group in steps:
                self.assertTrue(resources['step_'+group])
                self.assertTrue(resources['step_'+group+'_hint'])
            for part in ('eye', 'brow', 'mouth', 'tongue'):
                self.assertTrue(resources['part_'+part])
            for page in ('start', 'all', 'guide'):
                self.assertTrue(resources['nav_'+page])
            for preset in ('balanced', 'responsive', 'defaults', 'raw'):
                for suffix in ('', '_hint', '_pro'):
                    self.assertTrue(resources[f'preset_{preset}{suffix}'])

    def test_quoted_names_in_movement_text_exist(self):
        for directory in ('values', 'values-zh'):
            resources = self.resources(directory)
            labels = {resources['p_'+key.replace('.', '_')] for key, section, group in self.fields()}
            labels |= {text for name, text in resources.items()
                       if name.startswith(('m_', 'step_')) and not name.endswith(('_look', '_pro', '_hint'))}
            for name in [n for n in resources if n.endswith('_look')]:
                for quoted in re.findall(r'“([^”]+)”', resources[name]):
                    with self.subTest(directory=directory, name=name, quoted=quoted):
                        self.assertIn(quoted, labels)

    def test_reading_placeholders_match_between_languages(self):
        english, chinese = self.resources('values'), self.resources('values-zh')
        templated = [name for name, text in english.items() if '{0}' in (text or '')]
        self.assertGreaterEqual(len(templated), 8)
        for name in english:
            with self.subTest(name=name):
                self.assertEqual(sorted(set(re.findall(r'\{\d\}', english[name] or ''))),
                                 sorted(set(re.findall(r'\{\d\}', chinese[name] or ''))))

    def test_activity_is_a_settings_form_not_a_multiline_configuration_editor(self):
        source = (SOURCE/'MainActivity.java').read_text()
        for required in ('new Switch(', 'new SeekBar(', 'number.setSingleLine(true)', 'state.canSave()', 'state.canApply()',
                         'Catalog.MOMENTS', 'new ScopeView(', 'Scope.run(', 'field.initial', 'allPage()'):
            self.assertIn(required, source)
        for forbidden in ('TYPE_TEXT_FLAG_MULTI_LINE', 'editor.setText(', 'setMinLines(10)'):
            self.assertNotIn(forbidden, source)

    def test_draft_edits_do_not_submit_module_requests(self):
        source = (SOURCE/'MainActivity.java').read_text()
        edited = re.search(r'private void edited\(\) \{(.*?)\n    \}', source, re.S)[1]
        self.assertNotIn('request(', edited)
        self.assertEqual(re.findall(r'request\("(\w+)"\)', source), ['read', 'write', 'apply'])
        self.assertIn('setOnClickListener(v -> replaceDraft(action))', source)

    def test_literal_percentages_are_not_android_format_substitutions(self):
        for path in (ROOT/'headset/res').rglob('*.xml'):
            for item in ET.parse(path).findall('string'):
                if '%' in (item.text or ''):
                    self.assertEqual(item.get('formatted'), 'false', (path, item.get('name')))

    def test_view_tags_do_not_use_framework_resource_ids(self):
        for name in ('MainActivity.java', 'ScopeView.java'):
            self.assertNotIn('setTag(android.R.id.', (SOURCE/name).read_text())

    def test_technical_text_states_formulas_units_and_verified_examples(self):
        for directory in ('values', 'values-zh'):
            resources = self.resources(directory)
            for key, fragments in {'m_gaze_pro': ('exp(', '8.6%', '59.3%', 'Δt'),
                                   'm_raise_pro': ('w²', '36%', '11.2%', 'Q/R', '61.8%', '22.6%', 'Δt'),
                                   'm_blink_raise_pro': ('83 ms', 'q·m'),
                                   'm_lips_pro': ('MouthClose', '0.09', '0.08'),
                                   'm_lips_look': ('MouthClose', 'JawOpen'),
                                   'm_rest_pro': ('JawOpen', '(0.02, 0.06)', '(0, 0.04)', '60 ms'),
                                   'm_subtle_pro': ('CheekPuff', '0.581', '0.867', '1.025'),
                                   'm_talk_pro': ('45.1%', '23.7%', '10.7%', 'JawOpen', 'MouthLeft'),
                                   'm_tongue_out_pro': ('21.7%', '45 ms', 'Δt'),
                                   'm_mixed_pro': ('16:1', '4:1'),
                                   'm_tongue_slight_pro': ('1.05',),
                                   'guide_time_pro': ('24 Hz', '41.7 ms', '21.7%', '45 ms', '1/90 s', '1.5 s')}.items():
                for fragment in fragments:
                    self.assertIn(fragment, resources[key], (directory, key, fragment))

    def plain_keys(self, resources):
        """Strings shown in simple wording: everything except the technical additions."""
        technical = re.compile(r'(_pro|_tech)$|^(state_|hint_|failed|valid|apply_|discard_|draft_|range|blink_order|invalid_)')
        return [name for name in resources if not technical.search(name)]

    def test_plain_wording_leaves_out_formula_notation(self):
        notation = re.compile(r'α|Q/R|w²|exp\(|\blog|24 ?Hz|Kalman|EMA|\br =|Δ|每帧|per frame|θ|β')
        for directory in ('values', 'values-zh'):
            resources = self.resources(directory)
            keys = self.plain_keys(resources)
            self.assertGreater(len(keys), 250)
            for name in keys:
                with self.subTest(directory=directory, name=name):
                    self.assertIsNone(notation.search(resources[name] or ''))

    def test_technical_additions_only_show_in_technical_wording(self):
        lines = (SOURCE/'MainActivity.java').read_text().splitlines()
        uses = 0
        for index, line in enumerate(lines):
            if re.search(r'"_(pro|tech)"|_pro"\)', line):
                uses += 1
                self.assertTrue(any('if (pro' in previous for previous in lines[index-2:index+1]), line)
        self.assertGreaterEqual(uses, 5)

    def test_switching_wording_changes_display_only(self):
        source = (SOURCE/'MainActivity.java').read_text()
        body = re.search(r'private void setWording\(boolean value\) \{(.*?)\n    \}', source, re.S)[1]
        for forbidden in ('state.', 'request(', 'draft', 'profile', 'replaceDraft', 'edited('):
            self.assertNotIn(forbidden, body)
        self.assertIn('buildScreen()', body)
        self.assertIn('putString("wording"', body)
        for tag in ('"mode_simple"', '"mode_pro"'):
            self.assertIn(tag, source)

    def test_movement_charts_are_labelled_as_fixed_interval_examples(self):
        source = (SOURCE/'MainActivity.java').read_text()
        body = re.search(r'private void momentPage\(Catalog.Moment moment\) \{(.*?)\n    \}', source, re.S)[1]
        self.assertIn('s("scope_example")', body)
        self.assertIn('HZ = 90', (SOURCE/'Scope.java').read_text())
        for directory in ('values', 'values-zh'):
            resources = self.resources(directory)
            self.assertIn('90', resources['scope_example'])
            self.assertIn('90', resources['guide_time'])
            self.assertIn('1/90 s', resources['guide_time_pro'])
            for name, text in resources.items():
                self.assertNotIn('实时', text or '')
                self.assertNotRegex(text or '', r'\blive\b')

    def test_worker_uses_fixed_commands_and_descriptor_relative_mailbox(self):
        worker = (ROOT/'hook/src/picoet-panel.cpp').read_text()
        for required in ('openat(', 'O_NOFOLLOW', 'O_EXCL', 'st.st_uid == uid', 'st.st_nlink == 1',
                         'fchown(', 'fsetxattr(', 'renameat(', '"reload-processing"', 'unsetenv("PICOET_CONTROL_LOCKED")'):
            self.assertIn(required, worker)
        for forbidden in ('system(', 'popen(', '"su"', '"--force"'):
            self.assertNotIn(forbidden, worker)
        self.assertNotIn('kill(', worker)

    def test_module_bundles_and_starts_its_own_panel(self):
        packager = (ROOT/'build-picoet-enhance.ps1').read_text()
        self.assertIn('system/app/PicoETEnhance', packager)
        self.assertIn('bin\\picoet-panel', packager)
        service = (ROOT/'magisk-picoet-enhance/service.sh').read_text()
        self.assertIn('cmd package list packages -U dev.pico.etenhance', service)
        self.assertIn('"$MODDIR/bin/picoet-panel" "$MODDIR" "$panel_uid"', service)


if __name__ == '__main__':
    unittest.main()
